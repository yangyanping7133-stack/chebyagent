#!/usr/bin/env python3
"""Create, validate, resolve, and atomically record scanned Turkey releases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable


SCHEMA_VERSION = 3
STATE_SCHEMA_VERSION = 1
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_REFERENCE_PATTERN = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")
HEX_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_RELEASE_JSON_BYTES = 16 * 1024 * 1024
EXPECTED_IMAGES = {"runtime", "edge", "certbot", "alpine", "trivy"}
EXPECTED_SCANNERS = ["secret", "vuln"]
EXPECTED_SUPPORT_REFERENCES = {
    "certbot": "certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4",
    "alpine": "alpine:3.23.5@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40",
}
OFFICIAL_TRIVY_REFERENCE = (
    "ghcr.io/aquasecurity/trivy:0.72.0@"
    "sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
)
OFFICIAL_COSIGN_REFERENCE = (
    "ghcr.io/sigstore/cosign/cosign:v3.1.2@"
    "sha256:d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849"
)
COSIGN_IDENTITY_REGEXP = r"https://github\.com/aquasecurity/trivy/\.github/workflows/.+"
COSIGN_ISSUER = "https://token.actions.githubusercontent.com"
SCANNER_FIXED_PROVENANCE = {
    "upstreamVersion": "0.72.0",
    "customVersion": "0.72.0-cheby.1",
    "upstreamCommit": "8a32853686209a428179bb3a1688802b25691564",
    "sourceUrl": (
        "https://github.com/aquasecurity/trivy/archive/"
        "8a32853686209a428179bb3a1688802b25691564.tar.gz"
    ),
    "sourceArchiveSha256": (
        "5a922c388846d11345ce8283e4373be312458f002abc667c3cd1f77c43163725"
    ),
    "upstreamGoModSha256": (
        "4f1207875bb6102121cba9871f7da8af158096e6fff0759a3a37adf1beea7d24"
    ),
    "upstreamGoSumSha256": (
        "4c0326b979bd53518d23944a1ceed5d177a1c5c792074dd82f0fbb3b98e3a879"
    ),
    "patchedGoModSha256": (
        "2a11b375757e277f3a56a3125b8c6adf373241614afeb777ca50264e004a65e2"
    ),
    "patchedGoSumSha256": (
        "5340573f39cba663800d92e85b6cac2ae001307c4c1d0b084ae29d4f5fec9831"
    ),
    "moduleGraphSha256": (
        "0f5e6b935f789441b411705543bed99ef41ca57cd10adea7c6a23dd581400767"
    ),
    "orasModule": "oras.land/oras-go/v2",
    "orasVersion": "v2.6.2",
    "builderReference": (
        "golang:1.26.5-alpine3.24@"
        "sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2"
    ),
    "goVersion": "1.26.5",
    "goEnv": "off",
    "goWork": "off",
    "goToolchain": "local",
    "goProxy": "https://proxy.golang.org",
    "goSumDb": "sum.golang.org",
    "goPrivate": "",
    "goNoSumDb": "",
    "goNoProxy": "",
    "goInsecure": "",
    "binarySha256": (
        "30329a9bfa5c4b29e7f0b3552ae7430ef011b73963033e9b028e2003bcd08a39"
    ),
    "caBundleSha256": (
        "b8d837841b88bfaa1a0fa827cbca8e2576418dd47c9fc4bb7f1f9d89c83111b9"
    ),
    "dockerfileSha256": (
        "5cc182b841568dd29ab07bbad600fd2f5fe95e1a8115d52e1adffe42d2bd0092"
    ),
    "lockSha256": (
        "35142658934a5bbd77315ac6befda0d6d0f5eb1a98c6815f5b7b1b320c0c5a31"
    ),
}
VERIFIER_FIXED_PROVENANCE = {
    "upstreamVersion": "3.1.2",
    "customVersion": "3.1.2-cheby.1",
    "tagObject": "dc80df70da727f4abdd843640594025584a270ae",
    "upstreamCommit": "193d2153431f8bb0d945a4c1ee721872f73add67",
    "releaseUrl": "https://github.com/sigstore/cosign/releases/tag/v3.1.2",
    "sourceUrl": (
        "https://github.com/sigstore/cosign/archive/"
        "193d2153431f8bb0d945a4c1ee721872f73add67.tar.gz"
    ),
    "sourceArchiveSha256": (
        "566154a32bd9fb05b6893a4bf5fac57c3c92b5be36d5d177b5e3cc91e1dfa438"
    ),
    "upstreamGoModSha256": (
        "62ee3f278a7ee61f5a4b5aedd6af3291465ff45035267afc1224c04bd206a939"
    ),
    "upstreamGoSumSha256": (
        "cfd81b60e95b440397f37e1db9cc0961fac72e32f7d3362dc4ed7432a6852f70"
    ),
    "materializedGoSumSha256": (
        "e05fc44fa4275f87b4b6c3c1d35945abd96e1ef27bb739b255090b508a0a7528"
    ),
    "moduleGraphSha256": (
        "6e7e69a8924042ea95a0f9ffc6956772581db453deb3512ddf7e844090e44547"
    ),
    "builderReference": (
        "golang:1.26.5-alpine3.24@"
        "sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2"
    ),
    "goVersion": "1.26.5",
    "goEnv": "off",
    "goWork": "off",
    "goToolchain": "local",
    "goProxy": "https://proxy.golang.org",
    "goSumDb": "sum.golang.org",
    "goPrivate": "",
    "goNoSumDb": "",
    "goNoProxy": "",
    "goInsecure": "",
    "buildDate": "2026-07-17T14:32:20Z",
    "binarySha256": (
        "7ba7d877672635f2d7e537ca3d20e751c088d86265c0f8bc6698d0084899c0af"
    ),
    "caBundleSha256": (
        "b8d837841b88bfaa1a0fa827cbca8e2576418dd47c9fc4bb7f1f9d89c83111b9"
    ),
    "dockerfileSha256": (
        "a3f369a8943ab14ef31e6084046a234f87dd671bead7b5bdb7c931ae4080d3e0"
    ),
    "lockSha256": (
        "c92888202d054e6c8f8461ca5aaa6d2f775258ce97f42bb19b14d4b1dad69969"
    ),
    "officialReference": OFFICIAL_COSIGN_REFERENCE,
}
FINDINGS_LOCK_SHA256 = (
    "62fbb647e45c16f7a7040a0d3d48c11b0afa3ba852034ad3037c02f7b02cd443"
)
OFFICIAL_FINDINGS_SHA256 = (
    "fb8a3de7c31a6f8f8775202a39a3761542eb9b7436d329a71c5dae1ae0d48bb3"
)


class DuplicateJSONKeyError(ValueError):
    """Raised when security evidence relies on JSON last-key-wins behavior."""


def reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise DuplicateJSONKeyError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def read_bounded_json(
    path: Path,
    *,
    maximum_bytes: int = MAX_RELEASE_JSON_BYTES,
    expected_uid: int | None = None,
    expected_mode: int | None = None,
    reject_group_world_writable: bool = False,
    opener: Callable[[Path, int], int] | None = None,
) -> object:
    """Read one immutable regular JSON file without following or racing symlinks."""
    metadata = path.lstat()
    if (
        maximum_bytes <= 0
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > maximum_bytes
        or (expected_uid is not None and metadata.st_uid != expected_uid)
        or (expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode)
        or (
            reject_group_world_writable
            and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    ):
        raise RuntimeError("release JSON file metadata is not trusted")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (opener or os.open)(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_mode != metadata.st_mode
            or opened.st_uid != metadata.st_uid
            or opened.st_gid != metadata.st_gid
            or opened.st_size != metadata.st_size
            or opened.st_mtime_ns != metadata.st_mtime_ns
            or opened.st_ctime_ns != metadata.st_ctime_ns
            or opened.st_size > maximum_bytes
        ):
            raise RuntimeError("release JSON file changed before bounded reading")
        encoded = handle.read(maximum_bytes + 1)
        after = os.fstat(handle.fileno())
    if (
        len(encoded) > maximum_bytes
        or len(encoded) != opened.st_size
        or after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or after.st_ctime_ns != opened.st_ctime_ns
    ):
        raise RuntimeError("release JSON file changed during bounded reading")
    try:
        return json.loads(encoded, object_pairs_hook=reject_duplicate_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJSONKeyError) as error:
        raise RuntimeError("release JSON file is malformed") from error


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("release timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("release timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise RuntimeError("release timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_document(
    document: object, maximum_db_age_hours: int | None = 48
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "createdAt",
        "buildContextSha256",
        "images",
        "scanner",
        "verifier",
        "scan",
    }:
        raise RuntimeError("release manifest has an unexpected top-level shape")
    if document["schemaVersion"] != SCHEMA_VERSION:
        raise RuntimeError("release manifest schema is unsupported")
    created_at = parse_timestamp(document["createdAt"])
    if created_at > datetime.now(timezone.utc):
        raise RuntimeError("release manifest creation time is in the future")
    if HEX_SHA256_PATTERN.fullmatch(str(document["buildContextSha256"])) is None:
        raise RuntimeError("release manifest context digest is invalid")

    images = document["images"]
    if not isinstance(images, dict) or set(images) != EXPECTED_IMAGES:
        raise RuntimeError("release manifest image set is not exact")
    for name, image in images.items():
        if not isinstance(image, dict) or set(image) != {"id", "reference"}:
            raise RuntimeError(f"release image metadata is malformed: {name}")
        if IMAGE_ID_PATTERN.fullmatch(str(image["id"])) is None:
            raise RuntimeError(f"release image ID is not immutable: {name}")
        reference = str(image["reference"])
        if name in {"runtime", "edge", "trivy"}:
            if reference != image["id"]:
                raise RuntimeError(f"locally built image reference must equal image ID: {name}")
        elif (
            DIGEST_REFERENCE_PATTERN.fullmatch(reference) is None
            or reference != EXPECTED_SUPPORT_REFERENCES[name]
        ):
            raise RuntimeError(f"support image differs from the reviewed digest: {name}")

    scanner = document["scanner"]
    expected_scanner_keys = set(SCANNER_FIXED_PROVENANCE) | {"imageId"}
    if not isinstance(scanner, dict) or set(scanner) != expected_scanner_keys:
        raise RuntimeError("release scanner provenance has an unexpected shape")
    for field, expected in SCANNER_FIXED_PROVENANCE.items():
        if scanner[field] != expected:
            raise RuntimeError(f"release scanner provenance differs: {field}")
    if scanner["imageId"] != images["trivy"]["id"]:
        raise RuntimeError("release scanner provenance differs from its immutable image")

    verifier = document["verifier"]
    expected_verifier_keys = set(VERIFIER_FIXED_PROVENANCE) | {"imageId", "reference"}
    if not isinstance(verifier, dict) or set(verifier) != expected_verifier_keys:
        raise RuntimeError("release verifier provenance has an unexpected shape")
    for field, expected in VERIFIER_FIXED_PROVENANCE.items():
        if verifier[field] != expected:
            raise RuntimeError(f"release verifier provenance differs: {field}")
    if (
        IMAGE_ID_PATTERN.fullmatch(str(verifier["imageId"])) is None
        or verifier["reference"] != verifier["imageId"]
    ):
        raise RuntimeError("release verifier is not bound to its immutable local image")

    scan = document["scan"]
    if not isinstance(scan, dict) or set(scan) != {
        "result",
        "scanners",
        "scannerVersion",
        "bootstrap",
        "database",
        "antiBlindness",
    }:
        raise RuntimeError("release scan metadata is malformed")
    if scan["result"] != "pass" or scan["scanners"] != EXPECTED_SCANNERS:
        raise RuntimeError("release scan did not pass the exact required scanners")
    if not re.fullmatch(r"0\.72\.0-cheby\.1", str(scan["scannerVersion"])):
        raise RuntimeError("release scanner version is not the reviewed version")
    bootstrap = scan["bootstrap"]
    if not isinstance(bootstrap, dict) or set(bootstrap) != {
        "officialTrivyId",
        "officialTrivyReference",
        "cosignId",
        "cosignReference",
        "officialCosignReference",
        "certificateIdentityRegexp",
        "certificateOidcIssuer",
        "signatureEvidenceSha256",
        "customCosignScanResult",
        "officialCosignScanResult",
        "verified",
    }:
        raise RuntimeError("release scanner bootstrap evidence is malformed")
    for field in ("officialTrivyId", "cosignId"):
        if IMAGE_ID_PATTERN.fullmatch(str(bootstrap[field])) is None:
            raise RuntimeError("release scanner bootstrap image ID is invalid")
    if (
        bootstrap["officialTrivyReference"] != OFFICIAL_TRIVY_REFERENCE
        or bootstrap["cosignId"] != verifier["imageId"]
        or bootstrap["cosignReference"] != verifier["reference"]
        or bootstrap["officialCosignReference"] != OFFICIAL_COSIGN_REFERENCE
        or bootstrap["certificateIdentityRegexp"] != COSIGN_IDENTITY_REGEXP
        or bootstrap["certificateOidcIssuer"] != COSIGN_ISSUER
        or bootstrap["customCosignScanResult"] != "pass"
        or bootstrap["officialCosignScanResult"] != "pass"
        or bootstrap["verified"] is not True
        or HEX_SHA256_PATTERN.fullmatch(str(bootstrap["signatureEvidenceSha256"])) is None
    ):
        raise RuntimeError("release scanner bootstrap trust evidence differs")
    database = scan["database"]
    if not isinstance(database, dict) or set(database) != {
        "schemaVersion",
        "updatedAt",
        "downloadedAt",
        "nextUpdate",
        "metadataSha256",
        "trivyDbSha256",
    }:
        raise RuntimeError("release vulnerability DB evidence is malformed")
    if database["schemaVersion"] != 2 or any(
        HEX_SHA256_PATTERN.fullmatch(str(database[field])) is None
        for field in ("metadataSha256", "trivyDbSha256")
    ):
        raise RuntimeError("release vulnerability DB identity is invalid")
    database_updated_at = parse_timestamp(database["updatedAt"])
    database_downloaded_at = parse_timestamp(database["downloadedAt"])
    database_next_update = parse_timestamp(database["nextUpdate"])
    age = datetime.now(timezone.utc) - database_updated_at
    if age.total_seconds() < 0 or (
        maximum_db_age_hours is not None
        and age.total_seconds() > maximum_db_age_hours * 3600
    ):
        raise RuntimeError("Trivy vulnerability database is outside the freshness window")
    if database_downloaded_at < database_updated_at or database_next_update <= database_updated_at:
        raise RuntimeError("release vulnerability DB chronology is invalid")
    anti_blindness = scan["antiBlindness"]
    if not isinstance(anti_blindness, dict) or anti_blindness != {
        "officialToCustom": "pass",
        "customToCustom": "pass",
        "customToOfficial": "exact-lock",
        "blockingFindingCount": 16,
        "secretCount": 0,
        "findingsLockSha256": FINDINGS_LOCK_SHA256,
        "officialFindingsSha256": OFFICIAL_FINDINGS_SHA256,
    }:
        raise RuntimeError("release scanner anti-blindness evidence differs")
    return document


def load_manifest(
    path: Path,
    expected_uid: int | None = None,
    maximum_db_age_hours: int | None = 48,
) -> dict[str, Any]:
    return validate_document(
        read_bounded_json(
            path,
            expected_uid=expected_uid,
            reject_group_world_writable=True,
        ),
        maximum_db_age_hours,
    )


def validate_directory_tree(
    path: Path, expected_uid: int, stop_at: Path | None = None
) -> None:
    for component in [path.absolute(), *path.absolute().parents]:
        metadata = component.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("release state directory tree is not root-controlled")
        if stop_at is not None and component == stop_at.absolute():
            return
    if stop_at is not None:
        raise RuntimeError("release state validation boundary is not a parent")


def ensure_state_directory(
    path: Path, expected_uid: int, tree_root: Path | None = None
) -> None:
    """Create a private state directory without following any path symlink."""
    if not path.is_absolute() or path == Path("/") or any(
        component in {"", ".", ".."} for component in path.parts[1:]
    ):
        raise RuntimeError("release state directory must be a canonical absolute child path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    trusted_root = Path("/") if tree_root is None else tree_root
    if not trusted_root.is_absolute():
        raise RuntimeError("release state validation root must be absolute")
    try:
        relative_parts = path.relative_to(trusted_root).parts
    except ValueError as error:
        raise RuntimeError("release state directory is outside its trusted root") from error
    descriptor = os.open(trusted_root, flags)
    try:
        root_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
            or root_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("release state root is not trusted")
        for component in relative_parts:
            created = False
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
                created = True
            except OSError as error:
                raise RuntimeError(
                    "release state directory contains a symlink or non-directory"
                ) from error
            try:
                metadata = os.fstat(child)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != expected_uid
                    or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise RuntimeError("release state directory tree is not root-controlled")
                if created:
                    os.fchmod(child, 0o700)
                    os.fsync(descriptor)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        final_metadata = os.fstat(descriptor)
        if stat.S_IMODE(final_metadata.st_mode) != 0o700:
            raise RuntimeError("release state directory must have mode 0700")
    finally:
        os.close(descriptor)


def atomic_write(path: Path, document: dict[str, Any], mode: int = 0o600) -> None:
    parent = path.parent
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise RuntimeError("release manifest parent must be a real directory")
    if parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("release manifest parent must not be group/world-writable")
    encoded = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("failed to write release manifest")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create_document(args: argparse.Namespace) -> dict[str, Any]:
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "buildContextSha256": args.context_sha256,
        "images": {
            "runtime": {"id": args.runtime_id, "reference": args.runtime_id},
            "edge": {"id": args.edge_id, "reference": args.edge_id},
            "certbot": {"id": args.certbot_id, "reference": args.certbot_reference},
            "alpine": {"id": args.alpine_id, "reference": args.alpine_reference},
            "trivy": {"id": args.trivy_id, "reference": args.trivy_reference},
        },
        "scanner": {
            **SCANNER_FIXED_PROVENANCE,
            "dockerfileSha256": args.scanner_dockerfile_sha256,
            "lockSha256": args.scanner_lock_sha256,
            "binarySha256": args.scanner_binary_sha256,
            "imageId": args.trivy_id,
        },
        "verifier": {
            **VERIFIER_FIXED_PROVENANCE,
            "dockerfileSha256": args.cosign_dockerfile_sha256,
            "lockSha256": args.cosign_lock_sha256,
            "binarySha256": args.cosign_binary_sha256,
            "imageId": args.cosign_id,
            "reference": args.cosign_reference,
        },
        "scan": {
            "result": "pass",
            "scanners": EXPECTED_SCANNERS,
            "scannerVersion": args.scanner_version,
            "bootstrap": {
                "officialTrivyId": args.official_trivy_id,
                "officialTrivyReference": args.official_trivy_reference,
                "cosignId": args.cosign_id,
                "cosignReference": args.cosign_reference,
                "officialCosignReference": args.official_cosign_reference,
                "certificateIdentityRegexp": COSIGN_IDENTITY_REGEXP,
                "certificateOidcIssuer": COSIGN_ISSUER,
                "signatureEvidenceSha256": args.signature_evidence_sha256,
                "customCosignScanResult": "pass",
                "officialCosignScanResult": "pass",
                "verified": True,
            },
            "database": {
                "schemaVersion": args.db_schema_version,
                "updatedAt": args.db_updated_at,
                "downloadedAt": args.db_downloaded_at,
                "nextUpdate": args.db_next_update,
                "metadataSha256": args.db_metadata_sha256,
                "trivyDbSha256": args.db_sha256,
            },
            "antiBlindness": {
                "officialToCustom": "pass",
                "customToCustom": "pass",
                "customToOfficial": "exact-lock",
                "blockingFindingCount": 16,
                "secretCount": 0,
                "findingsLockSha256": args.findings_lock_sha256,
                "officialFindingsSha256": args.official_findings_sha256,
            },
        },
    }
    return validate_document(document)


def record_state(
    candidate_path: Path,
    state_directory: Path,
    expected_uid: int,
    tree_root: Path | None = None,
    writer: Callable[[Path, dict[str, Any], int], None] = atomic_write,
) -> None:
    candidate = load_manifest(candidate_path, expected_uid)
    validate_directory_tree(state_directory, expected_uid, tree_root)
    state_path = state_directory / "state.json"
    original = load_state(state_directory, expected_uid)
    if original is not None and original["current"] == candidate:
        return
    replacement = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "generation": 1 if original is None else int(original["generation"]) + 1,
        "current": candidate,
        "previous": None if original is None else original["current"],
    }
    try:
        writer(state_path, replacement, 0o600)
        if load_state(state_directory, expected_uid) != replacement:
            raise RuntimeError("recorded release state did not verify")
    except BaseException as error:
        try:
            if original is None:
                if state_path.exists() or state_path.is_symlink():
                    metadata = state_path.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                        raise RuntimeError("failed state path is uncontrolled")
                    state_path.unlink()
                    fsync_directory(state_directory)
                if load_state(state_directory, expected_uid) is not None:
                    raise RuntimeError("empty state snapshot was not restored")
            else:
                writer(state_path, original, 0o600)
                if load_state(state_directory, expected_uid) != original:
                    raise RuntimeError("previous state snapshot was not restored")
        except BaseException as restore_error:
            raise RuntimeError(
                "release state transaction failed and original snapshot could not be restored"
            ) from restore_error
        raise error


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_state_document(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "generation",
        "current",
        "previous",
    }:
        raise RuntimeError("release state has an unexpected shape")
    if document["schemaVersion"] != STATE_SCHEMA_VERSION:
        raise RuntimeError("release state schema is unsupported")
    if not isinstance(document["generation"], int) or document["generation"] < 1:
        raise RuntimeError("release state generation is invalid")
    validate_document(document["current"], None)
    if document["previous"] is not None:
        validate_document(document["previous"], None)
    return document


def load_state(
    state_directory: Path, expected_uid: int
) -> dict[str, Any] | None:
    state_path = state_directory / "state.json"
    try:
        document = read_bounded_json(
            state_path,
            expected_uid=expected_uid,
            expected_mode=0o600,
        )
    except FileNotFoundError:
        return None
    return validate_state_document(document)


def validate_running_release(
    running_runtime: str,
    running_edge: str,
    current: dict[str, Any] | None,
    allow_all_stopped: bool = False,
) -> None:
    running = {"runtime": running_runtime, "edge": running_edge}
    if current is None:
        if any(running.values()):
            raise RuntimeError("running application has no trusted release record")
        return
    if not any(running.values()):
        if allow_all_stopped:
            return
        raise RuntimeError("recorded application is unexpectedly fully stopped")
    for name, actual in running.items():
        expected = str(current["images"][name]["reference"])
        if not actual:
            raise RuntimeError(f"recorded {name} container is missing")
        if actual != expected:
            raise RuntimeError(f"running {name} differs from trusted release record")


def add_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context-sha256", required=True)
    for name in ("runtime", "edge", "certbot", "alpine", "trivy"):
        parser.add_argument(f"--{name}-id", required=True)
    for name in ("certbot", "alpine", "trivy"):
        parser.add_argument(f"--{name}-reference", required=True)
    parser.add_argument("--scanner-version", required=True)
    parser.add_argument("--scanner-dockerfile-sha256", required=True)
    parser.add_argument("--scanner-lock-sha256", required=True)
    parser.add_argument("--scanner-binary-sha256", required=True)
    parser.add_argument("--official-trivy-id", required=True)
    parser.add_argument("--official-trivy-reference", required=True)
    parser.add_argument("--cosign-id", required=True)
    parser.add_argument("--cosign-reference", required=True)
    parser.add_argument("--official-cosign-reference", required=True)
    parser.add_argument("--cosign-dockerfile-sha256", required=True)
    parser.add_argument("--cosign-lock-sha256", required=True)
    parser.add_argument("--cosign-binary-sha256", required=True)
    parser.add_argument("--signature-evidence-sha256", required=True)
    parser.add_argument("--findings-lock-sha256", required=True)
    parser.add_argument("--official-findings-sha256", required=True)
    parser.add_argument("--db-schema-version", type=int, required=True)
    parser.add_argument("--db-updated-at", required=True)
    parser.add_argument("--db-downloaded-at", required=True)
    parser.add_argument("--db-next-update", required=True)
    parser.add_argument("--db-metadata-sha256", required=True)
    parser.add_argument("--db-sha256", required=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    add_create_arguments(create)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--expected-owner-uid", type=int)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--manifest", type=Path, required=True)
    resolve.add_argument("--image", choices=sorted(EXPECTED_IMAGES), required=True)
    resolve.add_argument("--expected-owner-uid", type=int)
    resolve.add_argument("--allow-stale-scan", action="store_true")
    record = subparsers.add_parser("record")
    record.add_argument("--candidate", type=Path, required=True)
    record.add_argument("--state-directory", type=Path, required=True)
    record.add_argument("--expected-owner-uid", type=int, default=0)
    state = subparsers.add_parser("validate-state")
    state.add_argument("--state-directory", type=Path, required=True)
    state.add_argument("--expected-owner-uid", type=int, default=0)
    prepare_state = subparsers.add_parser("prepare-state")
    prepare_state.add_argument("--state-directory", type=Path, required=True)
    prepare_state.add_argument("--expected-owner-uid", type=int, default=0)
    args = parser.parse_args()
    if args.command == "create":
        atomic_write(args.output, create_document(args))
        print(f"immutable scanned release manifest written to {args.output}")
    elif args.command == "validate":
        load_manifest(args.manifest, args.expected_owner_uid)
        print("immutable scanned release manifest: PASS")
    elif args.command == "resolve":
        document = load_manifest(
            args.manifest,
            args.expected_owner_uid,
            None if args.allow_stale_scan else 48,
        )
        print(document["images"][args.image]["reference"])
    elif args.command == "record":
        record_state(args.candidate, args.state_directory, args.expected_owner_uid)
        print("active and previous release state recorded atomically")
    elif args.command == "validate-state":
        validate_directory_tree(args.state_directory, args.expected_owner_uid)
        load_state(args.state_directory, args.expected_owner_uid)
        print("release state directory tree: PASS")
    elif args.command == "prepare-state":
        ensure_state_directory(args.state_directory, args.expected_owner_uid)
        print("private release state directory prepared without following symlinks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
