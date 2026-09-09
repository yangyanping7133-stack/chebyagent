#!/usr/bin/env python3
"""Scan every decompressed APK member for redacted credential candidates.

The APK is treated only as a ZIP container. No code is executed and no member is
written to disk. Findings contain names, rules, and byte offsets, never values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

from runtime_archive_audit import scan_stream
from source_secret_scan import SENSITIVE_NAME


MAX_MEMBERS = 100_000
MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


def safe_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not str(path):
        raise ValueError("Unsafe APK member name")
    return str(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_apk(path: Path) -> dict:
    findings: list[dict] = []
    members = 0
    scanned_bytes = 0
    names: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            name = safe_member_name(entry.filename)
            if name in names:
                raise ValueError("Duplicate APK member: " + name)
            names.add(name)
            members += 1
            if members > MAX_MEMBERS:
                raise ValueError("APK has too many members")
            if entry.is_dir():
                continue
            if SENSITIVE_NAME.search(name):
                findings.append({"path": name, "rule": "sensitive_filename"})
            if entry.file_size < 0 or scanned_bytes + entry.file_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("APK uncompressed size exceeds scan limit")
            with archive.open(entry) as stream:
                count, _digest, matches = scan_stream(stream)
            if count != entry.file_size:
                raise ValueError("APK member size mismatch: " + name)
            scanned_bytes += count
            findings.extend({"path": name, **match} for match in matches)
    return {
        "apk": str(path.resolve()),
        "apk_sha256": file_sha256(path),
        "members": members,
        "decompressed_member_bytes_scanned": scanned_bytes,
        "findings": findings,
        "outcome": "REVIEW" if findings else "NO_CANDIDATES_FOUND",
        "boundary": (
            "Every top-level APK ZIP member was scanned after ZIP decompression. "
            "Embedded tar and zstd payloads were scanned as their stored archive bytes; their "
            "decompressed contents require the separate pinned runtime archive audit. No "
            "credential values are emitted, and no-candidate results are not a complete secrecy proof."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.apk.is_file():
        raise ValueError("APK does not exist")
    if args.output.exists():
        raise ValueError("Refusing to overwrite a scan report")
    result = scan_apk(args.apk)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({
        "apk_sha256": result["apk_sha256"],
        "members": result["members"],
        "bytes": result["decompressed_member_bytes_scanned"],
        "candidate_count": len(result["findings"]),
        "outcome": result["outcome"],
        "report": str(args.output),
    }))


if __name__ == "__main__":
    main()
