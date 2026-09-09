#!/usr/bin/env python3
"""Offline private-CA issuance and fail-closed Relay TLS release management.

CA private keys stay in an operator-controlled offline directory. Turkey
receives only a short-lived leaf candidate and the public trust bundle that is
also compiled into the signed APK. No command prints private material.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Callable, Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ALLOWED_SERVICE_PORTS = frozenset({27461, 27462})
DEFAULT_LEAF_VALID_HOURS = 720
MINIMUM_LEAF_VALID_HOURS = 48
RELEASES_DIRECTORY = "releases"
TRUST_DIRECTORY = "trust"
TRUST_FILENAME = "relay-trust-bundle.json"
CONSUMED_CANDIDATE_NAME = ".candidate-consumed"
RELEASE_FILE_MODES = {
    "ca.pem": 0o444,
    "fullchain.pem": 0o444,
    "leaf.pem": 0o444,
    "privkey.pem": 0o400,
    "release.json": 0o444,
}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PEM_CERTIFICATE = re.compile(
    br"-----BEGIN CERTIFICATE-----\s+.+?-----END CERTIFICATE-----\s*",
    re.DOTALL,
)


def service_port(value: str) -> int:
    parsed = int(value)
    if parsed not in ALLOWED_SERVICE_PORTS:
        raise argparse.ArgumentTypeError("service port must be explicitly 27461 or 27462")
    return parsed


def public_ipv4(value: str) -> ipaddress.IPv4Address:
    parsed = ipaddress.ip_address(value)
    if not isinstance(parsed, ipaddress.IPv4Address) or not parsed.is_global:
        raise argparse.ArgumentTypeError("public IP must be a globally routable IPv4 address")
    return parsed


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise RuntimeError(f"unsafe certificate directory: {path}")
    if metadata.st_mode & 0o022:
        raise RuntimeError(f"certificate directory is group/world writable: {path}")
    return path


def _regular_file(path: Path, *, private: bool = False) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RuntimeError(f"unsafe certificate file: {path.name}")
    if private and metadata.st_mode & 0o077:
        raise RuntimeError(f"private-key permissions are too broad: {path.name}")
    return path.read_bytes()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_file(path: Path, content: bytes, mode: int) -> None:
    _safe_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_symlink(path: Path, target: str) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}"
    try:
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _spki(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _spki_pin(public_key: ec.EllipticCurvePublicKey) -> str:
    digest = hashlib.sha256(_spki(public_key)).digest()
    return "sha256/" + base64.b64encode(digest).decode("ascii")


def _authority_id(certificate: x509.Certificate) -> str:
    digest = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)
    ).digest()
    token = base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")
    return "ca_" + token


def _certificate_blocks(content: bytes) -> list[x509.Certificate]:
    blocks = PEM_CERTIFICATE.findall(content)
    if not blocks or PEM_CERTIFICATE.sub(b"", content).strip():
        raise RuntimeError("PEM certificate bundle is malformed")
    return [x509.load_pem_x509_certificate(block) for block in blocks]


def validate_ca(certificate: x509.Certificate, *, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    if certificate.not_valid_before_utc > current:
        raise RuntimeError("private CA is not valid yet")
    if certificate.not_valid_after_utc - current < timedelta(days=30):
        raise RuntimeError("private CA is too close to expiry")
    if certificate.subject != certificate.issuer:
        raise RuntimeError("private CA must be self-issued")
    constraints = certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    )
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    if (
        not constraints.critical
        or not constraints.value.ca
        or constraints.value.path_length != 0
        or not usage.critical
        or not usage.value.key_cert_sign
        or not usage.value.crl_sign
    ):
        raise RuntimeError("private CA constraints are invalid")
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise RuntimeError("private CA key must be P-256")
    public_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        ec.ECDSA(certificate.signature_hash_algorithm),
    )


def validate_leaf(
    leaf: x509.Certificate,
    ca: x509.Certificate,
    key: ec.EllipticCurvePrivateKey,
    public_ip: ipaddress.IPv4Address,
    *,
    minimum_hours: int,
    now: datetime | None = None,
) -> None:
    validate_ca(ca, now=now)
    if minimum_hours < 1 or minimum_hours > 144:
        raise RuntimeError("minimum validity must be between 1 and 144 hours")
    current = now or datetime.now(timezone.utc)
    if leaf.not_valid_before_utc > current:
        raise RuntimeError("leaf certificate is not valid yet")
    if leaf.not_valid_after_utc - current < timedelta(hours=minimum_hours):
        raise RuntimeError("leaf certificate is inside the validity safety floor")
    if leaf.issuer != ca.subject or leaf.subject == leaf.issuer:
        raise RuntimeError("leaf certificate issuer is invalid")
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if san.get_values_for_type(x509.IPAddress) != [public_ip]:
        raise RuntimeError("leaf IP SAN does not exactly match the public IP")
    if san.get_values_for_type(x509.DNSName):
        raise RuntimeError("leaf certificate must not contain DNS identities")
    constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = leaf.extensions.get_extension_for_class(x509.KeyUsage)
    extended = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if (
        not constraints.critical
        or constraints.value.ca
        or not usage.critical
        or not usage.value.digital_signature
        or usage.value.key_cert_sign
        or usage.value.crl_sign
        or list(extended) != [ExtendedKeyUsageOID.SERVER_AUTH]
    ):
        raise RuntimeError("leaf certificate constraints are invalid")
    leaf_public = leaf.public_key()
    ca_public = ca.public_key()
    if not isinstance(leaf_public, ec.EllipticCurvePublicKey) or not isinstance(
        ca_public, ec.EllipticCurvePublicKey
    ):
        raise RuntimeError("certificate keys must be EC")
    if _spki(leaf_public) != _spki(key.public_key()):
        raise RuntimeError("leaf certificate and private key do not match")
    ca_public.verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        ec.ECDSA(leaf.signature_hash_algorithm),
    )


def create_ca(
    ca_directory: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
    *,
    valid_days: int,
) -> dict[str, object]:
    if valid_days < 365 or valid_days > 3650:
        raise RuntimeError("private CA validity must be between 365 and 3650 days")
    if ca_directory.exists():
        raise RuntimeError("private CA directory already exists")
    ca_directory.mkdir(mode=0o700, parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    current = datetime.now(timezone.utc)
    suffix = secrets.token_hex(8)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"ChebyCodex Relay CA {suffix}")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - timedelta(minutes=5))
        .not_valid_after(current + timedelta(days=valid_days))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    validate_ca(certificate)
    key_content = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_content = certificate.public_bytes(serialization.Encoding.PEM)
    _atomic_file(ca_directory / "ca-key.pem", key_content, 0o400)
    _atomic_file(ca_directory / "ca.pem", certificate_content, 0o444)
    entry = _trust_entry(certificate)
    metadata = {
        "version": 1,
        "publicIp": str(public_ip),
        "servicePort": service_port_value,
        "authority": entry,
    }
    _atomic_file(
        ca_directory / "ca-public.json",
        (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        0o444,
    )
    return entry


def _load_ca(ca_directory: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    _safe_directory(ca_directory)
    key_content = _regular_file(ca_directory / "ca-key.pem", private=True)
    certificate_content = _regular_file(ca_directory / "ca.pem")
    key = serialization.load_pem_private_key(key_content, password=None)
    certificate = x509.load_pem_x509_certificate(certificate_content)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise RuntimeError("private CA key must be P-256")
    validate_ca(certificate)
    public = certificate.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    if _spki(public) != _spki(key.public_key()):
        raise RuntimeError("private CA certificate and key do not match")
    return key, certificate


def issue_candidate(
    ca_directory: Path,
    candidate_directory: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
    *,
    valid_hours: int,
) -> dict[str, object]:
    if valid_hours < MINIMUM_LEAF_VALID_HOURS or valid_hours > 720:
        raise RuntimeError("leaf validity must be between 48 and 720 hours")
    if candidate_directory.exists():
        raise RuntimeError("candidate directory already exists")
    candidate_directory.mkdir(mode=0o700, parents=True)
    ca_key, ca = _load_ca(ca_directory)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    current = datetime.now(timezone.utc)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, str(public_ip))]
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - timedelta(minutes=5))
        .not_valid_after(
            min(current + timedelta(hours=valid_hours), ca.not_valid_after_utc)
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(public_ip)]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    validate_leaf(
        leaf,
        ca,
        leaf_key,
        public_ip,
        minimum_hours=MINIMUM_LEAF_VALID_HOURS,
    )
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM)
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    private_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    manifest = _release_manifest(leaf, ca, public_ip, service_port_value)
    _atomic_file(candidate_directory / "fullchain.pem", leaf_pem + ca_pem, 0o444)
    _atomic_file(candidate_directory / "leaf.pem", leaf_pem, 0o444)
    _atomic_file(candidate_directory / "ca.pem", ca_pem, 0o444)
    _atomic_file(candidate_directory / "privkey.pem", private_pem, 0o400)
    _atomic_file(
        candidate_directory / "release.json",
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        0o444,
    )
    return manifest


def _trust_entry(certificate: x509.Certificate) -> dict[str, str]:
    validate_ca(certificate)
    der = certificate.public_bytes(serialization.Encoding.DER)
    public_key = certificate.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    return {
        "authorityId": _authority_id(certificate),
        "certificateDerBase64": base64.b64encode(der).decode("ascii"),
        "certificateSha256": _sha256(der),
        "spkiSha256": _sha256(_spki(public_key)),
        "spkiPin": _spki_pin(public_key),
    }


def build_trust_bundle(
    ca_paths: Iterable[Path],
    public_ip: ipaddress.IPv4Address,
    allowed_ports: Iterable[int],
) -> dict[str, object]:
    certificates = [
        x509.load_pem_x509_certificate(_regular_file(path)) for path in ca_paths
    ]
    if len(certificates) not in {1, 2}:
        raise RuntimeError("trust bundle must contain current and optional next CA")
    entries = [_trust_entry(certificate) for certificate in certificates]
    if len({entry["certificateSha256"] for entry in entries}) != len(entries):
        raise RuntimeError("trust bundle contains a duplicate CA")
    ports = sorted(set(allowed_ports))
    if not ports or not set(ports).issubset(ALLOWED_SERVICE_PORTS):
        raise RuntimeError("trust bundle port allowlist is invalid")
    return {
        "version": 1,
        "publicIp": str(public_ip),
        "servicePorts": ports,
        "authorities": entries,
    }


def _parse_trust_bundle(
    content: bytes,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> tuple[dict[str, object], dict[str, x509.Certificate]]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("trust bundle is malformed") from error
    if not isinstance(document, dict) or set(document) != {
        "version",
        "publicIp",
        "servicePorts",
        "authorities",
    }:
        raise RuntimeError("trust bundle fields are invalid")
    if (
        not isinstance(document["version"], int)
        or not isinstance(document["publicIp"], str)
        or not isinstance(document["servicePorts"], list)
        or not all(isinstance(port, int) for port in document["servicePorts"])
        or not isinstance(document["authorities"], list)
        or document["version"] != 1
        or document["publicIp"] != str(public_ip)
        or document["servicePorts"] != [service_port_value]
        or len(document["authorities"]) not in {1, 2}
    ):
        raise RuntimeError("trust bundle binding is invalid")
    authorities: dict[str, x509.Certificate] = {}
    for raw in document["authorities"]:
        if not isinstance(raw, dict) or set(raw) != {
            "authorityId",
            "certificateDerBase64",
            "certificateSha256",
            "spkiSha256",
            "spkiPin",
        }:
            raise RuntimeError("trust authority fields are invalid")
        try:
            der = base64.b64decode(raw["certificateDerBase64"], validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("trust authority certificate is malformed") from error
        certificate = x509.load_der_x509_certificate(der)
        if raw != _trust_entry(certificate):
            raise RuntimeError("trust authority metadata is inconsistent")
        fingerprint = raw["certificateSha256"]
        if fingerprint in authorities:
            raise RuntimeError("trust bundle contains a duplicate CA")
        authorities[fingerprint] = certificate
    return document, authorities


def install_trust_bundle(
    tls_root: Path,
    source: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> dict[str, object]:
    _safe_directory(tls_root, create=True)
    content = _regular_file(source)
    document, authorities = _parse_trust_bundle(
        content, public_ip, service_port_value
    )
    current = tls_root / "current"
    if current.exists() or current.is_symlink():
        manifest = _raw_release_manifest(tls_root, "current")
        if manifest["caCertificateSha256"] not in authorities:
            raise RuntimeError("new trust bundle would distrust the active certificate")
    trust = _safe_directory(tls_root / TRUST_DIRECTORY, create=True)
    normalized = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _atomic_file(trust / TRUST_FILENAME, normalized, 0o444)
    return document


def _installed_trust(
    tls_root: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> dict[str, x509.Certificate]:
    content = _regular_file(tls_root / TRUST_DIRECTORY / TRUST_FILENAME)
    _, authorities = _parse_trust_bundle(content, public_ip, service_port_value)
    return authorities


def _release_manifest(
    leaf: x509.Certificate,
    ca: x509.Certificate,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> dict[str, object]:
    leaf_der = leaf.public_bytes(serialization.Encoding.DER)
    ca_der = ca.public_bytes(serialization.Encoding.DER)
    leaf_public = leaf.public_key()
    ca_public = ca.public_key()
    assert isinstance(leaf_public, ec.EllipticCurvePublicKey)
    assert isinstance(ca_public, ec.EllipticCurvePublicKey)
    return {
        "version": 1,
        "publicIp": str(public_ip),
        "servicePort": service_port_value,
        "leafCertificateSha256": _sha256(leaf_der),
        "leafSpkiSha256": _sha256(_spki(leaf_public)),
        "caCertificateSha256": _sha256(ca_der),
        "caSpkiSha256": _sha256(_spki(ca_public)),
        "authorityId": _authority_id(ca),
        "notBefore": leaf.not_valid_before_utc.isoformat(),
        "notAfter": leaf.not_valid_after_utc.isoformat(),
    }


def _load_candidate(
    candidate: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
    authorities: dict[str, x509.Certificate],
    *,
    minimum_hours: int,
) -> tuple[dict[str, object], bytes, bytes, bytes]:
    _safe_directory(candidate)
    fullchain = _regular_file(candidate / "fullchain.pem")
    leaf_content = _regular_file(candidate / "leaf.pem")
    ca_content = _regular_file(candidate / "ca.pem")
    key_content = _regular_file(candidate / "privkey.pem", private=True)
    manifest_content = _regular_file(candidate / "release.json")
    chain = _certificate_blocks(fullchain)
    if len(chain) != 2:
        raise RuntimeError("candidate full chain must contain leaf and private CA")
    leaf = x509.load_pem_x509_certificate(leaf_content)
    ca = x509.load_pem_x509_certificate(ca_content)
    if (
        chain[0].public_bytes(serialization.Encoding.DER)
        != leaf.public_bytes(serialization.Encoding.DER)
        or chain[1].public_bytes(serialization.Encoding.DER)
        != ca.public_bytes(serialization.Encoding.DER)
    ):
        raise RuntimeError("candidate full chain order is invalid")
    key = serialization.load_pem_private_key(key_content, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise RuntimeError("candidate private key is invalid")
    validate_leaf(
        leaf, ca, key, public_ip, minimum_hours=minimum_hours
    )
    expected = _release_manifest(leaf, ca, public_ip, service_port_value)
    try:
        manifest = json.loads(manifest_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("candidate release manifest is malformed") from error
    if not isinstance(manifest, dict) or manifest != expected:
        raise RuntimeError("candidate release manifest is inconsistent")
    trusted_ca = authorities.get(str(expected["caCertificateSha256"]))
    if trusted_ca is None:
        raise RuntimeError("candidate CA is absent from the APK trust bundle")
    if trusted_ca.public_bytes(serialization.Encoding.DER) != ca.public_bytes(
        serialization.Encoding.DER
    ):
        raise RuntimeError("candidate CA does not match the trusted authority")
    return expected, fullchain, ca_content, key_content


def _release_target(pointer: Path, releases: Path) -> Path:
    if not pointer.is_symlink():
        raise RuntimeError(f"certificate pointer is missing or unsafe: {pointer.name}")
    target_text = os.readlink(pointer)
    prefix = RELEASES_DIRECTORY + "/"
    if not target_text.startswith(prefix):
        raise RuntimeError("certificate pointer escaped the releases directory")
    name = target_text.removeprefix(prefix)
    if not HEX_SHA256.fullmatch(name):
        raise RuntimeError("certificate pointer target is malformed")
    target = (pointer.parent / target_text).resolve(strict=True)
    if target.parent != releases.resolve(strict=True) or not target.is_dir():
        raise RuntimeError("certificate pointer target is unsafe")
    return target


def _raw_release_manifest(tls_root: Path, pointer_name: str) -> dict[str, object]:
    releases = _safe_directory(tls_root / RELEASES_DIRECTORY)
    release = _release_target(tls_root / pointer_name, releases)
    try:
        return json.loads(_regular_file(release / "release.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("certificate release manifest is malformed") from error


def read_release(
    tls_root: Path,
    pointer_name: str,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
    *,
    minimum_hours: int = 1,
) -> dict[str, object]:
    releases = _safe_directory(tls_root / RELEASES_DIRECTORY)
    release = _release_target(tls_root / pointer_name, releases)
    authorities = _installed_trust(tls_root, public_ip, service_port_value)
    manifest, _, _, _ = _load_candidate(
        release,
        public_ip,
        service_port_value,
        authorities,
        minimum_hours=minimum_hours,
    )
    if release.name != manifest["leafCertificateSha256"]:
        raise RuntimeError("certificate release directory is inconsistent")
    return manifest


def activate_candidate(
    tls_root: Path,
    candidate: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> dict[str, object]:
    _safe_directory(tls_root, create=True)
    authorities = _installed_trust(tls_root, public_ip, service_port_value)
    manifest, fullchain, ca_content, key_content = _load_candidate(
        candidate,
        public_ip,
        service_port_value,
        authorities,
        minimum_hours=MINIMUM_LEAF_VALID_HOURS,
    )
    fingerprint = str(manifest["leafCertificateSha256"])
    releases = _safe_directory(tls_root / RELEASES_DIRECTORY, create=True)
    release = releases / fingerprint
    if release.exists():
        existing = _load_candidate(
            release,
            public_ip,
            service_port_value,
            authorities,
            minimum_hours=MINIMUM_LEAF_VALID_HOURS,
        )
        if existing != (manifest, fullchain, ca_content, key_content):
            raise RuntimeError(
                "existing certificate release does not match the candidate"
            )
        current_pointer = tls_root / "current"
        if (
            current_pointer.is_symlink()
            and _release_target(current_pointer, releases).name == fingerprint
        ):
            return manifest
    else:
        temporary = releases / f".{fingerprint}.{os.getpid()}"
        temporary.mkdir(mode=0o700)
        try:
            leaf = _certificate_blocks(fullchain)[0]
            _atomic_file(temporary / "fullchain.pem", fullchain, 0o444)
            _atomic_file(
                temporary / "leaf.pem",
                leaf.public_bytes(serialization.Encoding.PEM),
                0o444,
            )
            _atomic_file(temporary / "ca.pem", ca_content, 0o444)
            _atomic_file(temporary / "privkey.pem", key_content, 0o400)
            _atomic_file(
                temporary / "release.json",
                (
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode(),
                0o444,
            )
            os.replace(temporary, release)
            _fsync_directory(releases)
        finally:
            if temporary.exists():
                for child in temporary.iterdir():
                    child.unlink()
                temporary.rmdir()
    current = tls_root / "current"
    previous = tls_root / "previous"
    if current.exists() or current.is_symlink():
        old = _release_target(current, releases)
        _atomic_symlink(previous, f"{RELEASES_DIRECTORY}/{old.name}")
    _atomic_symlink(current, f"{RELEASES_DIRECTORY}/{fingerprint}")
    return manifest


def _canonical_private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("certificate directory must be absolute")
    _safe_directory(path)
    if path.resolve(strict=True) != path:
        raise RuntimeError("certificate directory path is aliased")
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeError("certificate directory mode must be 0700")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError("certificate directory owner is invalid")
    return path


def _release_file_snapshot(directory: Path) -> dict[str, tuple[os.stat_result, bytes]]:
    _canonical_private_directory(directory)
    entries = {entry.name for entry in directory.iterdir()}
    if entries != set(RELEASE_FILE_MODES):
        raise RuntimeError("certificate release file set is invalid")
    snapshot: dict[str, tuple[os.stat_result, bytes]] = {}
    for name, expected_mode in RELEASE_FILE_MODES.items():
        path = directory / name
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("certificate release file metadata is invalid")
        snapshot[name] = (metadata, path.read_bytes())
    return snapshot


def _purge_consumed_candidate(
    tombstone: Path,
    *,
    protected_directories: set[tuple[int, int]],
    protected_files: set[tuple[int, int]],
    fault_hook: Callable[[str], None] | None = None,
) -> None:
    if not tombstone.exists() and not tombstone.is_symlink():
        return
    _canonical_private_directory(tombstone)
    tombstone_metadata = tombstone.stat()
    if (
        tombstone_metadata.st_dev,
        tombstone_metadata.st_ino,
    ) in protected_directories:
        raise RuntimeError("consumed candidate aliases protected TLS state")
    entries = {entry.name for entry in tombstone.iterdir()}
    if not entries.issubset(RELEASE_FILE_MODES):
        raise RuntimeError("consumed candidate contains an unexpected entry")
    for name in sorted(entries):
        path = tombstone / name
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(metadata.st_mode) != RELEASE_FILE_MODES[name]
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) in protected_files
        ):
            raise RuntimeError("consumed candidate file metadata is invalid")
        path.unlink()
        if fault_hook is not None:
            fault_hook(f"after-unlink:{name}")
        _fsync_directory(tombstone)
    tombstone.rmdir()
    if fault_hook is not None:
        fault_hook("after-rmdir")
    _fsync_directory(tombstone.parent)


def consume_candidate(
    tls_root: Path,
    candidate: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
    *,
    resume_only: bool = False,
    _fault_hook: Callable[[str], None] | None = None,
) -> dict[str, object] | None:
    tls_root = _canonical_private_directory(tls_root)
    staging = _canonical_private_directory(candidate.parent)
    if candidate.name != "candidate":
        raise RuntimeError("staged certificate directory must be named candidate")
    releases = _canonical_private_directory(tls_root / RELEASES_DIRECTORY)
    current_release = _release_target(tls_root / "current", releases)
    current_release = _canonical_private_directory(current_release)
    current_directory_metadata = current_release.stat()
    current_snapshot = _release_file_snapshot(current_release)
    protected_directories = {
        (tls_root.stat().st_dev, tls_root.stat().st_ino),
        (releases.stat().st_dev, releases.stat().st_ino),
    }
    protected_files: set[tuple[int, int]] = set()
    for release_entry in releases.iterdir():
        release_metadata = release_entry.lstat()
        if not stat.S_ISDIR(release_metadata.st_mode) or release_entry.is_symlink():
            continue
        protected_directories.add(
            (release_metadata.st_dev, release_metadata.st_ino)
        )
        for release_file in release_entry.iterdir():
            release_file_metadata = release_file.lstat()
            if stat.S_ISREG(release_file_metadata.st_mode):
                protected_files.add(
                    (
                        release_file_metadata.st_dev,
                        release_file_metadata.st_ino,
                    )
                )
    staging_metadata = staging.stat()
    if (
        staging_metadata.st_dev,
        staging_metadata.st_ino,
    ) in protected_directories:
        raise RuntimeError("certificate staging aliases active TLS state")
    tombstone = staging / CONSUMED_CANDIDATE_NAME
    _purge_consumed_candidate(
        tombstone,
        protected_directories=protected_directories,
        protected_files=protected_files,
        fault_hook=_fault_hook,
    )
    if resume_only or (not candidate.exists() and not candidate.is_symlink()):
        return None
    candidate = _canonical_private_directory(candidate)
    candidate_directory_metadata = candidate.stat()
    if (
        candidate_directory_metadata.st_dev,
        candidate_directory_metadata.st_ino,
    ) in protected_directories:
        raise RuntimeError("candidate aliases protected TLS state")
    candidate_snapshot = _release_file_snapshot(candidate)
    for name in RELEASE_FILE_MODES:
        candidate_metadata, candidate_content = candidate_snapshot[name]
        current_metadata, current_content = current_snapshot[name]
        if os.path.samestat(candidate_metadata, current_metadata):
            raise RuntimeError("candidate file aliases the current release")
        if candidate_content != current_content:
            raise RuntimeError("candidate does not match the current release")
    manifest = read_release(
        tls_root,
        "current",
        public_ip,
        service_port_value,
    )
    current_after_validation = _release_target(tls_root / "current", releases)
    if not os.path.samestat(
        current_directory_metadata,
        current_after_validation.stat(),
    ):
        raise RuntimeError("current certificate changed during candidate validation")
    os.rename(candidate, tombstone)
    if _fault_hook is not None:
        _fault_hook("after-rename")
    _fsync_directory(staging)
    if _fault_hook is not None:
        _fault_hook("after-rename-fsync")
    _purge_consumed_candidate(
        tombstone,
        protected_directories=protected_directories,
        protected_files=protected_files,
        fault_hook=_fault_hook,
    )
    return manifest


def rollback(
    tls_root: Path,
    public_ip: ipaddress.IPv4Address,
    service_port_value: int,
) -> dict[str, object]:
    current = read_release(tls_root, "current", public_ip, service_port_value)
    previous = read_release(tls_root, "previous", public_ip, service_port_value)
    current_fingerprint = str(current["leafCertificateSha256"])
    previous_fingerprint = str(previous["leafCertificateSha256"])
    _atomic_symlink(
        tls_root / "current", f"{RELEASES_DIRECTORY}/{previous_fingerprint}"
    )
    _atomic_symlink(
        tls_root / "previous", f"{RELEASES_DIRECTORY}/{current_fingerprint}"
    )
    return previous


def deactivate(tls_root: Path) -> None:
    current = tls_root / "current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError("refusing to deactivate an unsafe current path")
    current.unlink(missing_ok=True)
    _fsync_directory(tls_root)


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-ip", required=True, type=public_ipv4)
    parser.add_argument("--service-port", required=True, type=service_port)
    actions = parser.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create-ca")
    create.add_argument("--ca-dir", required=True, type=Path)
    create.add_argument("--valid-days", type=int, default=1825)

    issue = actions.add_parser("issue")
    issue.add_argument("--ca-dir", required=True, type=Path)
    issue.add_argument("--candidate-dir", required=True, type=Path)
    issue.add_argument("--valid-hours", type=int, default=DEFAULT_LEAF_VALID_HOURS)

    trust = actions.add_parser("build-trust-bundle")
    trust.add_argument("--ca-certificate", required=True, type=Path)
    trust.add_argument("--next-ca-certificate", type=Path)
    trust.add_argument(
        "--allowed-port",
        required=True,
        action="append",
        type=service_port,
        dest="allowed_ports",
    )
    trust.add_argument("--output", required=True, type=Path)

    install = actions.add_parser("install-trust")
    install.add_argument("--tls-root", required=True, type=Path)
    install.add_argument("--trust-bundle", required=True, type=Path)

    activate = actions.add_parser("activate")
    activate.add_argument("--tls-root", required=True, type=Path)
    activate.add_argument("--candidate-dir", required=True, type=Path)

    consume = actions.add_parser("consume")
    consume.add_argument("--tls-root", required=True, type=Path)
    consume.add_argument("--candidate-dir", required=True, type=Path)
    consume.add_argument("--resume-only", action="store_true")

    show = actions.add_parser("show")
    show.add_argument("--tls-root", required=True, type=Path)
    show.add_argument("--pointer", choices=("current", "previous"), default="current")
    show.add_argument(
        "--field",
        required=True,
        choices=(
            "leaf-sha256",
            "leaf-spki-sha256",
            "ca-sha256",
            "ca-spki-sha256",
            "not-after",
            "json",
        ),
    )

    rollback_parser = actions.add_parser("rollback")
    rollback_parser.add_argument("--tls-root", required=True, type=Path)

    deactivate_parser = actions.add_parser("deactivate")
    deactivate_parser.add_argument("--tls-root", required=True, type=Path)
    return parser


def main() -> int:
    args = _base_parser().parse_args()
    if args.action == "create-ca":
        entry = create_ca(
            args.ca_dir.resolve(),
            args.public_ip,
            args.service_port,
            valid_days=args.valid_days,
        )
        print(
            "private CA created; public identity "
            f"authority_id={entry['authorityId']} "
            f"certificate_sha256={entry['certificateSha256']} "
            f"spki_sha256={entry['spkiSha256']}"
        )
        return 0
    if args.action == "issue":
        manifest = issue_candidate(
            args.ca_dir.resolve(),
            args.candidate_dir.resolve(),
            args.public_ip,
            args.service_port,
            valid_hours=args.valid_hours,
        )
        print(
            "leaf candidate issued; "
            f"leaf_sha256={manifest['leafCertificateSha256']} "
            f"ca_sha256={manifest['caCertificateSha256']} "
            f"not_after={manifest['notAfter']}"
        )
        return 0
    if args.action == "build-trust-bundle":
        ca_paths = [args.ca_certificate.resolve()]
        if args.next_ca_certificate is not None:
            ca_paths.append(args.next_ca_certificate.resolve())
        document = build_trust_bundle(
            ca_paths, args.public_ip, args.allowed_ports
        )
        if document["servicePorts"] != [args.service_port]:
            raise RuntimeError(
                "public trust bundle must authorize only its explicit service port"
            )
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_file(
            args.output,
            (
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
            0o444,
        )
        print(
            "public APK trust bundle written; "
            f"authorities={len(document['authorities'])}"
        )
        return 0
    if args.action == "install-trust":
        document = install_trust_bundle(
            args.tls_root.resolve(),
            args.trust_bundle.resolve(),
            args.public_ip,
            args.service_port,
        )
        print(
            "public trust bundle installed; "
            f"authorities={len(document['authorities'])}"
        )
        return 0
    if args.action == "activate":
        manifest = activate_candidate(
            args.tls_root.resolve(),
            args.candidate_dir.resolve(),
            args.public_ip,
            args.service_port,
        )
        print(
            "certificate release activated; "
            f"leaf_sha256={manifest['leafCertificateSha256']} "
            f"ca_sha256={manifest['caCertificateSha256']}"
        )
        return 0
    if args.action == "consume":
        manifest = consume_candidate(
            args.tls_root,
            args.candidate_dir,
            args.public_ip,
            args.service_port,
            resume_only=args.resume_only,
        )
        if manifest is None:
            print("consumed certificate staging is clean")
        else:
            print(
                "activated certificate candidate consumed; "
                f"leaf_sha256={manifest['leafCertificateSha256']}"
            )
        return 0
    if args.action == "show":
        manifest = read_release(
            args.tls_root.resolve(),
            args.pointer,
            args.public_ip,
            args.service_port,
        )
        fields = {
            "leaf-sha256": "leafCertificateSha256",
            "leaf-spki-sha256": "leafSpkiSha256",
            "ca-sha256": "caCertificateSha256",
            "ca-spki-sha256": "caSpkiSha256",
            "not-after": "notAfter",
        }
        if args.field == "json":
            print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        else:
            print(manifest[fields[args.field]])
        return 0
    if args.action == "rollback":
        manifest = rollback(
            args.tls_root.resolve(), args.public_ip, args.service_port
        )
        print(
            "previous certificate restored; "
            f"leaf_sha256={manifest['leafCertificateSha256']}"
        )
        return 0
    if args.action == "deactivate":
        deactivate(args.tls_root.resolve())
        print("current certificate deactivated")
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
