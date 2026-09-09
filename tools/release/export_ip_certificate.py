#!/usr/bin/env python3
"""Validate a Certbot IP lineage and atomically publish it to the Edge volume."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


STATE_ROOT = Path("/etc/letsencrypt")
SOURCE_ROOT = STATE_ROOT / "live"
DESTINATION_ROOT = Path("/tls")
LETS_ENCRYPT_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
CERTIFICATE_PATTERN = re.compile(
    br"-----BEGIN CERTIFICATE-----\s+.+?-----END CERTIFICATE-----\s*",
    re.DOTALL,
)


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def read_regular_file(path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    expected_root = STATE_ROOT.resolve(strict=True)
    if expected_root not in resolved.parents:
        raise RuntimeError(f"certificate lineage escaped {expected_root}")
    mode = resolved.stat().st_mode
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"expected regular file: {path}")
    return resolved.read_bytes()


def renewal_parameters(content: bytes) -> dict[str, str]:
    """Read the flat values in Certbot's ConfigObj renewalparams section."""
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RuntimeError("Certbot renewal configuration is malformed") from error

    inside_renewal_parameters = False
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            inside_renewal_parameters = line == "[renewalparams]"
            continue
        if not inside_renewal_parameters:
            continue
        if "=" not in line:
            raise RuntimeError("Certbot renewal configuration is malformed")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value or key in values:
            raise RuntimeError("Certbot renewal configuration is malformed")
        values[key] = value

    if not values:
        raise RuntimeError("Certbot renewal parameters are missing")
    return values


def certificate_chain(fullchain_pem: bytes) -> list[tuple[bytes, x509.Certificate]]:
    blocks = CERTIFICATE_PATTERN.findall(fullchain_pem)
    if not blocks or CERTIFICATE_PATTERN.sub(b"", fullchain_pem).strip():
        raise RuntimeError("full chain contains malformed or non-certificate content")
    certificates: list[tuple[bytes, x509.Certificate]] = []
    fingerprints: set[bytes] = set()
    for block in blocks:
        certificate = x509.load_pem_x509_certificate(block)
        fingerprint = certificate.fingerprint(hashes.SHA256())
        if fingerprint in fingerprints:
            raise RuntimeError("full chain contains a duplicate certificate")
        fingerprints.add(fingerprint)
        certificates.append((block, certificate))
    return certificates


def require_extension(
    certificate: x509.Certificate,
    extension_type: type[x509.ExtensionType],
    description: str,
) -> x509.Extension[x509.ExtensionType]:
    try:
        return certificate.extensions.get_extension_for_class(extension_type)
    except x509.ExtensionNotFound as error:
        raise RuntimeError(f"certificate is missing required {description}") from error


def validate_chain_constraints(chain: list[tuple[bytes, x509.Certificate]]) -> None:
    if len(chain) < 2:
        raise RuntimeError("full chain must contain the leaf and at least one intermediate")
    now = datetime.now(timezone.utc)
    for index, (_, certificate) in enumerate(chain):
        if certificate.not_valid_before_utc > now or certificate.not_valid_after_utc <= now:
            raise RuntimeError("full chain contains a certificate outside its validity period")
        basic_constraints = require_extension(
            certificate, x509.BasicConstraints, "basic constraints"
        )
        key_usage = require_extension(certificate, x509.KeyUsage, "key usage")
        if index == 0:
            if basic_constraints.value.ca:
                raise RuntimeError("leaf certificate must not be a CA")
            if not key_usage.value.digital_signature or key_usage.value.key_cert_sign:
                raise RuntimeError("leaf certificate key usage is invalid for a TLS server")
            extended_usage = require_extension(
                certificate, x509.ExtendedKeyUsage, "extended key usage"
            )
            if x509.oid.ExtendedKeyUsageOID.SERVER_AUTH not in extended_usage.value:
                raise RuntimeError("leaf certificate is not authorized for TLS server use")
        else:
            if not basic_constraints.value.ca or not basic_constraints.critical:
                raise RuntimeError("intermediate certificate lacks critical CA constraints")
            if not key_usage.value.key_cert_sign:
                raise RuntimeError("intermediate certificate cannot sign certificates")
        if index + 1 < len(chain) and certificate.issuer != chain[index + 1][1].subject:
            raise RuntimeError("full chain is not an ordered issuer path")


def write_private_temporary(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("failed to write private chain verification input")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def system_trust_arguments() -> list[str]:
    # Use only conventional operating-system trust locations. Do not honor
    # SSL_CERT_FILE/SSL_CERT_DIR overrides supplied by the invoking environment.
    candidate_files = (
        Path("/etc/ssl/certs/ca-certificates.crt"),
        Path("/etc/ssl/cert.pem"),
        Path("/etc/pki/tls/certs/ca-bundle.crt"),
    )
    candidate_directories = (Path("/etc/ssl/certs"), Path("/etc/pki/tls/certs"))
    arguments: list[str] = []
    for candidate in candidate_files:
        if candidate.is_file() and not candidate.is_symlink():
            arguments.extend(("-CAfile", str(candidate)))
            break
    for candidate in candidate_directories:
        if candidate.is_dir() and not candidate.is_symlink():
            arguments.extend(("-CApath", str(candidate)))
            break
    if not arguments:
        # The diagnostic includes Python's compiled defaults without trusting
        # environment-selected paths as verification inputs.
        defaults = ssl.get_default_verify_paths()
        raise RuntimeError(
            "no supported system trust-anchor location exists "
            f"(compiled cafile={defaults.openssl_cafile}, capath={defaults.openssl_capath})"
        )
    return arguments


def verify_trusted_chain(
    certificate_pem: bytes,
    fullchain_pem: bytes,
) -> list[tuple[bytes, x509.Certificate]]:
    chain = certificate_chain(fullchain_pem)
    leaf = x509.load_pem_x509_certificate(certificate_pem)
    if chain[0][1].fingerprint(hashes.SHA256()) != leaf.fingerprint(hashes.SHA256()):
        raise RuntimeError("full chain does not start with the validated leaf certificate")
    validate_chain_constraints(chain)

    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("OpenSSL CLI is required for system-trust chain verification")
    with tempfile.TemporaryDirectory(prefix="cheby-chain-verify-") as temporary_name:
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o700)
        leaf_path = temporary / "leaf.pem"
        intermediates_path = temporary / "intermediates.pem"
        write_private_temporary(leaf_path, chain[0][0])
        write_private_temporary(
            intermediates_path, b"\n".join(block for block, _ in chain[1:])
        )
        command = [
            openssl,
            "verify",
            "-x509_strict",
            "-purpose",
            "sslserver",
            "-verify_depth",
            "5",
            *system_trust_arguments(),
            "-untrusted",
            str(intermediates_path),
            str(leaf_path),
        ]
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = detail[-1] if detail else f"exit {result.returncode}"
            raise RuntimeError(f"certificate chain is not trusted by system anchors: {suffix}")
    return chain


def validate_lineage(public_ip: str, minimum_hours: int) -> tuple[bytes, bytes, bytes, str]:
    address = ipaddress.ip_address(public_ip)
    if address.version != 4 or not address.is_global:
        raise RuntimeError("CHEBY_PUBLIC_IP must be a globally routable IPv4 address")

    lineage = SOURCE_ROOT / public_ip
    renewal_path = STATE_ROOT / "renewal" / f"{public_ip}.conf"
    renewal = renewal_parameters(read_regular_file(renewal_path))
    if renewal.get("server") != LETS_ENCRYPT_PRODUCTION:
        raise RuntimeError("certificate lineage is not Let's Encrypt production")
    if renewal.get("authenticator") != "webroot":
        raise RuntimeError("certificate lineage does not use webroot validation")

    certificate_pem = read_regular_file(lineage / "cert.pem")
    fullchain_pem = read_regular_file(lineage / "fullchain.pem")
    private_key_pem = read_regular_file(lineage / "privkey.pem")

    certificate = x509.load_pem_x509_certificate(certificate_pem)
    issuer_organizations = certificate.issuer.get_attributes_for_oid(
        x509.NameOID.ORGANIZATION_NAME
    )
    if not issuer_organizations or issuer_organizations[0].value != "Let's Encrypt":
        raise RuntimeError("certificate issuer is not Let's Encrypt")
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    certificate_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_key != private_public_key:
        raise RuntimeError("certificate and private key do not match")

    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    general_names = list(names)
    ip_names = set(names.get_values_for_type(x509.IPAddress))
    dns_names = names.get_values_for_type(x509.DNSName)
    if (
        len(general_names) != 1
        or not isinstance(general_names[0], x509.IPAddress)
        or ip_names != {address}
        or dns_names
    ):
        raise RuntimeError("certificate SAN must contain only the dedicated public IP")

    now = datetime.now(timezone.utc)
    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    remaining_seconds = (not_after - now).total_seconds()
    if not_before > now:
        raise RuntimeError("certificate is not valid yet")
    if remaining_seconds < minimum_hours * 3600:
        raise RuntimeError("certificate remaining lifetime is below the deployment floor")
    # Let's Encrypt IP certificates use the 160-hour shortlived profile. A wider
    # bound detects accidentally exporting a different certificate class.
    lifetime_hours = (not_after - not_before).total_seconds() / 3600
    if not 150 <= lifetime_hours <= 170:
        raise RuntimeError("certificate lifetime is not the expected 160-hour profile")

    verify_trusted_chain(certificate_pem, fullchain_pem)

    release_id = certificate.fingerprint(hashes.SHA256()).hex()[:32]
    return certificate_pem, fullchain_pem, private_key_pem, release_id


def write_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # The exporter has no capabilities. Files stay root-owned and grant only
    # read access to the dedicated Edge mount; no other service mounts /tls.
    descriptor = os.open(path, flags, 0o404)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"failed to write certificate file: {path}")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o404)
    finally:
        os.close(descriptor)


def require_real_directory(path: Path, owner_uid: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"required directory is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"path must be a real directory: {path}")
    if owner_uid is not None and metadata.st_uid != owner_uid:
        raise RuntimeError(f"directory has an unexpected owner: {path}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(f"directory must not be group/world-writable: {path}")
    return metadata


def destination_root() -> Path:
    require_real_directory(DESTINATION_ROOT, os.geteuid())
    return DESTINATION_ROOT


def prepare_releases() -> Path:
    root = destination_root()
    releases = root / "releases"
    try:
        releases.mkdir(mode=0o711)
    except FileExistsError:
        pass
    require_real_directory(releases, os.geteuid())
    os.chmod(releases, 0o711)
    root_descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)
    return releases


def require_releases() -> Path:
    releases = destination_root() / "releases"
    require_real_directory(releases, os.geteuid())
    return releases


def release_target(pointer: Path, releases: Path) -> tuple[str, Path]:
    try:
        pointer_mode = pointer.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"certificate pointer is missing: {pointer.name}") from error
    if not stat.S_ISLNK(pointer_mode.st_mode):
        raise RuntimeError(f"certificate pointer is not a symlink: {pointer.name}")
    raw_target = os.readlink(pointer)
    target = Path(raw_target)
    if target.parent != Path("releases") or not re.fullmatch(r"[0-9a-f]{32}", target.name):
        raise RuntimeError(f"certificate pointer is not a controlled relative link: {pointer.name}")
    candidate = DESTINATION_ROOT / target
    try:
        candidate_mode = candidate.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"certificate pointer target is missing: {pointer.name}") from error
    if stat.S_ISLNK(candidate_mode.st_mode) or not stat.S_ISDIR(candidate_mode.st_mode):
        raise RuntimeError(f"certificate pointer target is not a real release: {pointer.name}")
    if candidate.parent != releases:
        raise RuntimeError(f"certificate pointer escaped the releases directory: {pointer.name}")
    return raw_target, candidate


def optional_release_target(pointer: Path, releases: Path) -> tuple[str, Path] | None:
    try:
        pointer.lstat()
    except FileNotFoundError:
        return None
    return release_target(pointer, releases)


def release_directories(releases: Path) -> list[Path]:
    candidates: list[Path] = []
    for entry in releases.iterdir():
        entry_mode = entry.lstat()
        if (
            stat.S_ISLNK(entry_mode.st_mode)
            or not stat.S_ISDIR(entry_mode.st_mode)
            or not re.fullmatch(r"[0-9a-f]{32}", entry.name)
        ):
            raise RuntimeError("releases directory contains an uncontrolled candidate")
        candidates.append(entry)
    return candidates


def remove_temporary_release(temporary: Path) -> None:
    try:
        temporary_mode = temporary.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(temporary_mode.st_mode) and not stat.S_ISLNK(temporary_mode.st_mode):
        shutil.rmtree(temporary)
    else:
        temporary.unlink()


def publish(
    certificate_pem: bytes,
    fullchain_pem: bytes,
    private_key_pem: bytes,
    release_id: str,
) -> None:
    releases = prepare_releases()

    release = releases / release_id
    try:
        release_mode = release.lstat()
    except FileNotFoundError:
        release_mode = None
    if release_mode is not None:
        if stat.S_ISLNK(release_mode.st_mode) or not stat.S_ISDIR(release_mode.st_mode):
            raise RuntimeError("certificate release path is not a real directory")
        expected = {
            "cert.pem": certificate_pem,
            "fullchain.pem": fullchain_pem,
            "privkey.pem": private_key_pem,
        }
        for name, content in expected.items():
            candidate = release / name
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError("existing certificate release is malformed")
            if candidate.read_bytes() != content:
                raise RuntimeError("existing certificate release content mismatch")
    else:
        temporary = releases / f".{release_id}.{os.getpid()}"
        temporary.mkdir(mode=0o700)
        try:
            write_file(temporary / "cert.pem", certificate_pem)
            write_file(temporary / "fullchain.pem", fullchain_pem)
            write_file(temporary / "privkey.pem", private_key_pem)
            os.chmod(temporary, 0o501)
            temporary_descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(temporary_descriptor)
            finally:
                os.close(temporary_descriptor)
            os.replace(temporary, release)
            releases_descriptor = os.open(releases, os.O_RDONLY)
            try:
                os.fsync(releases_descriptor)
            finally:
                os.close(releases_descriptor)
        finally:
            remove_temporary_release(temporary)

    current = DESTINATION_ROOT / "current"
    previous = DESTINATION_ROOT / "previous"
    current_release = optional_release_target(current, releases)
    optional_release_target(previous, releases)
    if current_release is not None:
        current_target = current_release[0]
        if current_target != str(Path("releases") / release_id):
            previous_replacement = DESTINATION_ROOT / f".previous.{os.getpid()}"
            previous_replacement.symlink_to(current_target)
            os.replace(previous_replacement, previous)

    replacement = DESTINATION_ROOT / f".current.{os.getpid()}"
    replacement.symlink_to(Path("releases") / release_id)
    os.replace(replacement, current)
    directory_descriptor = os.open(DESTINATION_ROOT, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

    retained = {release_id}
    for link_name in ("current", "previous"):
        link = DESTINATION_ROOT / link_name
        linked_release = optional_release_target(link, releases)
        if linked_release is not None:
            retained.add(linked_release[1].name)
    candidates = sorted(
        release_directories(releases),
        key=lambda entry: entry.lstat().st_mtime,
        reverse=True,
    )
    for old_release in candidates[3:]:
        if old_release.name not in retained:
            shutil.rmtree(old_release)
    releases_descriptor = os.open(releases, os.O_RDONLY)
    try:
        os.fsync(releases_descriptor)
    finally:
        os.close(releases_descriptor)


def current_fingerprint() -> str:
    releases = require_releases()
    _, release = release_target(DESTINATION_ROOT / "current", releases)
    certificate_path = release / "cert.pem"
    metadata = certificate_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("current certificate is not a regular file")
    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex()


def rollback() -> None:
    releases = require_releases()
    previous = DESTINATION_ROOT / "previous"
    previous_release = optional_release_target(previous, releases)
    if previous_release is None:
        raise RuntimeError("no previous certificate release is available")
    target = Path(previous_release[0])
    replacement = DESTINATION_ROOT / f".current.rollback.{os.getpid()}"
    replacement.symlink_to(target)
    os.replace(replacement, DESTINATION_ROOT / "current")
    directory_descriptor = os.open(DESTINATION_ROOT, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    print(f"rolled back certificate release to {target.name}")


def deactivate() -> None:
    root = destination_root()
    previous = DESTINATION_ROOT / "previous"
    try:
        previous_mode = previous.lstat()
    except FileNotFoundError:
        previous_mode = None
    if previous_mode is not None:
        if not stat.S_ISLNK(previous_mode.st_mode):
            raise RuntimeError("previous certificate pointer is not a symlink")
        releases = require_releases()
        release_target(previous, releases)
        raise RuntimeError("refusing to deactivate while a previous release is available")
    current = DESTINATION_ROOT / "current"
    try:
        current_mode = current.lstat()
    except FileNotFoundError:
        current_mode = None
    if current_mode is not None:
        if not stat.S_ISLNK(current_mode.st_mode):
            raise RuntimeError("current certificate pointer is not a symlink")
        releases = require_releases()
        release_target(current, releases)
        current.unlink()
    directory_descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    print("deactivated current certificate pointer; bootstrap mode is required")


def main() -> int:
    action = os.environ.get("CHEBY_CERT_EXPORT_ACTION", "publish").strip().lower()
    if action == "rollback":
        rollback()
        return 0
    if action == "deactivate":
        deactivate()
        return 0
    if action == "fingerprint":
        print(current_fingerprint())
        return 0
    if action != "publish":
        raise RuntimeError(
            "CHEBY_CERT_EXPORT_ACTION must be publish, rollback, deactivate, or fingerprint"
        )

    public_ip = required_environment("CHEBY_PUBLIC_IP")
    minimum_hours = int(os.environ.get("CHEBY_CERT_MIN_REMAINING_HOURS", "24"))
    if not 1 <= minimum_hours <= 144:
        raise RuntimeError("CHEBY_CERT_MIN_REMAINING_HOURS must be between 1 and 144")

    certificate, fullchain, private_key, release_id = validate_lineage(
        public_ip, minimum_hours
    )
    publish(certificate, fullchain, private_key, release_id)
    print(f"published validated IP certificate release {release_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
