#!/usr/bin/env python3
"""Install an intact pinned upstream ARM64 npm payload beside the base runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile


TARGET = "aarch64-unknown-linux-musl"
VENDOR_PREFIX = f"package/vendor/{TARGET}/"
REQUIRED_EXECUTABLES = (
    "bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
    "codex-resources/bwrap", "codex-resources/zsh/bin/zsh",
)
MARKER = ".cheby-codex-archive-sha256"


def validate_install(directory: Path, version: str) -> None:
    metadata = json.loads((directory / "codex-package.json").read_text())
    if any(metadata.get(key) != value for key, value in {
        "layoutVersion": 1, "version": version, "target": TARGET,
        "entrypoint": "bin/codex", "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }.items()):
        raise ValueError("Codex package metadata does not match the pinned runtime")
    package = json.loads((directory / "npm-package.json").read_text())
    if package.get("name") != "@openai/codex" or package.get("version") != version + "-linux-arm64":
        raise ValueError("Unexpected npm package identity")
    for name in REQUIRED_EXECUTABLES:
        executable = directory / name
        if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
            raise ValueError("Codex executable is missing or unusable: " + name)


def install(archive_path: Path, runtime_root: Path, version: str, expected_sha256: str) -> Path:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("A stable numeric Codex version is required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Invalid pinned Codex SHA-256")
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("Codex archive SHA-256 mismatch")

    runtime_root.mkdir(parents=True, exist_ok=True)
    destination = runtime_root / ("codex-" + version)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or (destination / MARKER).read_text().strip() != expected_sha256:
            raise ValueError("Existing Codex version directory is not this verified archive")
        validate_install(destination, version)
        return destination

    staging = runtime_root / (".codex-" + version + ".next")
    if staging.is_symlink():
        raise ValueError("Refusing a symlink at the Codex staging path")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o755)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names: set[str] = set()
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or str(path) != member.name:
                    raise ValueError("Unsafe Codex archive path")
                if not member.isfile():
                    raise ValueError("Unexpected link or non-file in Codex archive")
                if member.name.startswith(VENDOR_PREFIX):
                    name = member.name.removeprefix(VENDOR_PREFIX)
                elif member.name == "package/package.json":
                    name = "npm-package.json"
                elif member.name == "package/README.md":
                    name = "npm-README.md"
                else:
                    raise ValueError("Unexpected file outside the ARM64 Codex payload")
                if not name or name in names:
                    raise ValueError("Duplicate Codex archive destination")
                names.add(name)
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Unreadable Codex archive entry")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        validate_install(staging, version)
        (staging / MARKER).write_text(expected_sha256 + "\n")
        (staging / MARKER).chmod(0o600)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        install(args.archive, args.runtime_root, args.version, args.sha256)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit("Codex offline overlay failed: " + str(error)) from None


if __name__ == "__main__":
    main()
