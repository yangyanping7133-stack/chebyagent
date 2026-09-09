#!/usr/bin/env python3
"""Render the IP-specific Nginx configuration without templating Nginx variables."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import secrets
import stat


MARKER = "__CHEBY_PUBLIC_IP__"


def public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("public IP is not a valid IP address") from exc
    if address.version != 4:
        raise argparse.ArgumentTypeError("this release bundle currently supports IPv4 only")
    if not address.is_global:
        raise argparse.ArgumentTypeError("public IP must be a globally routable IPv4 address")
    return str(address)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-ip", required=True, type=public_ipv4)
    parser.add_argument("--mode", choices=("bootstrap", "production"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "deploy" / "turkey",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template_name = (
        "nginx.bootstrap.conf.template"
        if args.mode == "bootstrap"
        else "nginx.conf.template"
    )
    template_path = args.bundle_root / "edge" / template_name
    template = template_path.read_text(encoding="utf-8")
    rendered = template.replace(MARKER, args.public_ip)
    if MARKER in rendered:
        raise RuntimeError("not all public-IP markers were rendered")
    if args.mode == "production" and template.count(MARKER) < 3:
        raise RuntimeError("production template is missing expected IP identity checks")

    requested_output = args.output
    if not requested_output.is_absolute():
        requested_output = Path.cwd() / requested_output
    if requested_output.name in {"", ".", ".."}:
        raise RuntimeError("output must name a configuration file")

    # Resolve the already-existing parent, never the output itself. Resolving the
    # output would follow an attacker-controlled final symlink before replace().
    parent = requested_output.parent.resolve(strict=True)
    parent_mode = parent.stat()
    if not stat.S_ISDIR(parent_mode.st_mode):
        raise RuntimeError("output parent must be a directory")
    if parent_mode.st_uid not in {0, os.geteuid()}:
        raise RuntimeError("output parent is not owned by root or the current user")
    shared_sticky_parent = bool(
        parent_mode.st_mode & stat.S_IWOTH and parent_mode.st_mode & stat.S_ISVTX
    )
    if parent_mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not shared_sticky_parent:
        raise RuntimeError("output parent must not be writable by another user")

    output = parent / requested_output.name
    final_mode = 0o644
    try:
        output_mode = output.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(output_mode.st_mode) or not stat.S_ISREG(output_mode.st_mode):
            raise RuntimeError("output must be absent or an existing regular file")
        if output_mode.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("existing output must not be group/world-writable")
        if shared_sticky_parent and output_mode.st_uid != os.geteuid():
            raise RuntimeError("output in a shared directory is not owned by this process")
        final_mode = stat.S_IMODE(output_mode.st_mode)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    temporary_path: Path | None = None
    file_descriptor: int | None = None
    for _ in range(32):
        candidate = parent / f".nginx-{secrets.token_hex(16)}.conf"
        try:
            file_descriptor = os.open(candidate, flags, 0o600)
            temporary_path = candidate
            break
        except FileExistsError:
            continue
    if temporary_path is None or file_descriptor is None:
        raise RuntimeError("could not allocate an exclusive temporary output")

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), final_mode)
        os.replace(temporary_path, output)
        temporary_path = None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
