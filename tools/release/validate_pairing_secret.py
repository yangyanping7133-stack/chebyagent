#!/usr/bin/env python3
"""Generate or validate the canonical 256-bit Turkey pairing secret."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path
import re
import secrets
import stat


CANONICAL_PATTERN = re.compile(rb"[A-Za-z0-9_-]{43}")


def validate_secret_bytes(value: bytes) -> None:
    if CANONICAL_PATTERN.fullmatch(value) is None:
        raise RuntimeError(
            "pairing secret must be exactly 43 unpadded base64url characters"
        )
    decoded = base64.urlsafe_b64decode(value + b"=")
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != value:
        raise RuntimeError("pairing secret is not canonical 256-bit base64url")


def validate_secret_file(
    path: Path,
    expected_uid: int | None = None,
    expected_parent_uid: int | None = 0,
) -> None:
    if not path.is_absolute():
        raise RuntimeError("pairing secret path must be absolute")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("pairing secret must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o400:
        raise RuntimeError("pairing secret file mode must be 0400")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise RuntimeError(f"pairing secret file must be owned by UID {expected_uid}")
    if expected_parent_uid is not None:
        for parent in [path.absolute().parent, *path.absolute().parent.parents]:
            parent_metadata = parent.lstat()
            if (
                stat.S_ISLNK(parent_metadata.st_mode)
                or not stat.S_ISDIR(parent_metadata.st_mode)
                or parent_metadata.st_uid != expected_parent_uid
                or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise RuntimeError("pairing secret parent tree must be root-controlled")
    validate_secret_bytes(path.read_bytes())


def generate_secret(path: Path, owner_uid: int, owner_gid: int) -> None:
    if not path.is_absolute():
        raise RuntimeError("pairing secret output path must be absolute")
    parent = path.parent
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise RuntimeError("pairing secret parent must be a real directory")
    if parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("pairing secret parent must not be group/world-writable")
    value = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=")
    validate_secret_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o400)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("failed to write pairing secret")
            view = view[written:]
        os.fsync(descriptor)
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    print(f"generated canonical pairing secret at {path} (value not printed)")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("path", type=Path)
    validate.add_argument("--expected-uid", type=int, default=10002)
    generate = subparsers.add_parser("generate")
    generate.add_argument("path", type=Path)
    generate.add_argument("--owner-uid", type=int, default=10002)
    generate.add_argument("--owner-gid", type=int, default=10002)
    args = parser.parse_args()
    if args.command == "validate":
        validate_secret_file(args.path, args.expected_uid, 0)
        print("pairing secret format and ownership: PASS")
    else:
        generate_secret(args.path, args.owner_uid, args.owner_gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
