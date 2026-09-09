#!/usr/bin/env python3
"""Fail closed unless every root-executed Turkey release input is trusted."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat


ROOT_EXECUTED_GLOBS = (
    "tools/release/*.sh",
    "tools/release/*.py",
    "deploy/turkey/edge/*.template",
    "deploy/turkey/edge/includes/*.conf",
    "deploy/turkey/systemd/*.service",
    "deploy/turkey/systemd/*.timer",
)
FORBIDDEN_ENVIRONMENT_KEYS = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "CHEBY_RUNTIME_IMAGE",
    "CHEBY_EDGE_IMAGE",
}
ALLOWED_ENVIRONMENT_KEYS = {
    "CHEBY_PUBLIC_IP",
    "CHEBY_BIND_IP",
    "CHEBY_ACME_EMAIL",
    "CHEBY_PAIRING_SECRET_PATH",
    "CHEBY_RELEASE_MANIFEST_PATH",
}


def require_trusted_component(path: Path, expected_uid: int, directory: bool) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"trusted execution path must not contain symlinks: {path}")
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise RuntimeError(f"trusted execution path has the wrong type: {path}")
    if metadata.st_uid != expected_uid:
        raise RuntimeError(f"trusted execution path has the wrong owner: {path}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(f"trusted execution path is group/world-writable: {path}")


def validate_tree_path(path: Path, expected_uid: int = 0) -> None:
    absolute = path.absolute()
    components = [absolute]
    components.extend(absolute.parents)
    for component in reversed(components):
        require_trusted_component(
            component,
            expected_uid,
            directory=component != absolute or absolute.is_dir(),
        )


def validate_environment_file(path: Path, expected_uid: int = 0) -> None:
    validate_tree_path(path, expected_uid)
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("Turkey environment file mode must be 0600")
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].removeprefix("export ").strip()
        if key in FORBIDDEN_ENVIRONMENT_KEYS:
            raise RuntimeError(f"Turkey environment must not override {key}")
        if key not in ALLOWED_ENVIRONMENT_KEYS or key in seen:
            raise RuntimeError("Turkey environment contains an unexpected or duplicate key")
        seen.add(key)
    if seen != ALLOWED_ENVIRONMENT_KEYS:
        raise RuntimeError("Turkey environment is missing a required key")


def validate_docker_socket(path: Path = Path("/var/run/docker.sock")) -> None:
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host not in {"", "unix:///var/run/docker.sock", "unix:///run/docker.sock"}:
        raise RuntimeError("DOCKER_HOST must use the reviewed local Unix socket")
    if os.environ.get("DOCKER_CONTEXT", "") not in {"", "default"}:
        raise RuntimeError("DOCKER_CONTEXT must be empty or default")
    resolved = path.resolve(strict=True)
    if resolved not in {Path("/run/docker.sock"), Path("/var/run/docker.sock")}:
        raise RuntimeError("Docker socket resolved outside the reviewed path")
    metadata = resolved.stat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError("Docker socket must be a root-owned Unix socket")
    if metadata.st_mode & stat.S_IWOTH:
        raise RuntimeError("Docker socket must not be world-writable")


def release_inputs(repository_root: Path) -> list[Path]:
    paths = [
        repository_root,
        repository_root / "deploy/turkey/docker-compose.yml",
        repository_root / "deploy/turkey/generated",
        repository_root / ".dockerignore",
    ]
    generated = repository_root / "deploy/turkey/generated/nginx.conf"
    if generated.exists() or generated.is_symlink():
        paths.append(generated)
    for pattern in ROOT_EXECUTED_GLOBS:
        matches = sorted(repository_root.glob(pattern))
        if not matches:
            raise RuntimeError(f"trusted release input pattern is empty: {pattern}")
        paths.extend(matches)
    for name in (
        "chebycodex-cert-renew.service",
        "chebycodex-cert-renew.timer",
        "chebycodex-cert-monitor.service",
        "chebycodex-cert-monitor.timer",
        "chebycodex-cert-alert@.service",
    ):
        installed = Path("/etc/systemd/system") / name
        if installed.exists() or installed.is_symlink():
            paths.append(installed)
    return paths


def reject_python_bytecode(repository_root: Path) -> None:
    for directory, names, files in os.walk(repository_root, followlinks=False):
        if "__pycache__" in names:
            raise RuntimeError(
                f"production release tree contains Python bytecode cache: {Path(directory) / '__pycache__'}"
            )
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                raise RuntimeError(
                    f"production release tree contains Python bytecode: {Path(directory) / name}"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument(
        "--environment-file", type=Path, default=Path("/etc/chebycodex/turkey.env")
    )
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--require-production-root", action="store_true")
    parser.add_argument("--require-docker-socket", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Turkey production trust validation must run as root")
    repository_root = args.repository_root.absolute()
    if args.require_production_root and repository_root != Path("/opt/chebycodex"):
        raise RuntimeError("Turkey production checkout must be /opt/chebycodex")
    reject_python_bytecode(repository_root)
    for path in release_inputs(repository_root):
        validate_tree_path(path, 0)
    validate_environment_file(args.environment_file, 0)
    validate_tree_path(args.release_manifest, 0)
    if args.require_docker_socket:
        validate_docker_socket()
    print("root execution tree and Docker endpoint trust: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
