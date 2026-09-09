#!/usr/bin/env python3
"""Materialize and hash the exact deny-by-default Turkey Docker context."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat


FILES = (
    ".dockerignore",
    "deploy/turkey/Dockerfile.cosign",
    "deploy/turkey/Dockerfile.edge",
    "deploy/turkey/Dockerfile.runtime",
    "deploy/turkey/Dockerfile.scanner",
    "deploy/turkey/requirements.lock",
    "deploy/turkey/cosign.lock",
    "deploy/turkey/scanner.lock",
    "deploy/turkey/trivy-official-findings.lock.json",
)
DIRECTORIES = (
    "gateway/cheby_gateway",
    "deploy/turkey/edge/includes",
)


def source_files(root: Path) -> list[tuple[str, Path]]:
    selected: list[tuple[str, Path]] = []
    for relative in FILES:
        selected.append((relative, root / relative))
    for relative_directory in DIRECTORIES:
        directory = root / relative_directory
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"build context directory is not real: {relative_directory}")
        for candidate in sorted(directory.rglob("*")):
            relative = candidate.relative_to(root).as_posix()
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(f"build context must not contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                if candidate.name == "__pycache__":
                    raise RuntimeError(f"build context source contains Python bytecode cache: {relative}")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"build context contains a special file: {relative}")
            if candidate.suffix in {".pyc", ".pyo"}:
                raise RuntimeError(f"build context source contains Python bytecode: {relative}")
            if relative_directory == "gateway/cheby_gateway" and candidate.suffix != ".py":
                continue
            if relative_directory == "deploy/turkey/edge/includes" and candidate.suffix != ".conf":
                continue
            selected.append((relative, candidate))
    for relative, candidate in selected:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"build context input is not a regular file: {relative}")
    return sorted(selected)


def materialize(root: Path, output: Path) -> str:
    if output.exists() or output.is_symlink():
        raise RuntimeError("build context output must not already exist")
    output.mkdir(mode=0o700, parents=False)
    digest = hashlib.sha256()
    for relative, source in source_files(root):
        content = source.read_bytes()
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        destination = output / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(stat.S_IMODE(source.stat().st_mode) & 0o755)
    return digest.hexdigest()


def hash_materialized_context(context: Path) -> str:
    metadata = context.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("materialized context must be a real directory")
    digest = hashlib.sha256()
    for candidate in sorted(context.rglob("*")):
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("materialized context contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("materialized context contains a special file")
        relative = candidate.relative_to(context).as_posix()
        content = candidate.read_bytes()
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hash-context", type=Path)
    args = parser.parse_args()
    if args.hash_context is not None:
        if args.repository_root is not None or args.output is not None:
            parser.error("--hash-context cannot be combined with materialization arguments")
        print(hash_materialized_context(args.hash_context))
        return 0
    if args.repository_root is None or args.output is None:
        parser.error("--repository-root and --output are required")
    root = args.repository_root.resolve(strict=True)
    print(materialize(root, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
