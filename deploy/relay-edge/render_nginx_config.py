#!/usr/bin/env python3
"""Render one exact-authority Relay Edge configuration."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile


BUNDLE = Path(__file__).resolve().parent
TEMPLATE = BUNDLE / "edge" / "nginx.conf.template"


def _public_ip(value: str) -> ipaddress.IPv4Address:
    parsed = ipaddress.ip_address(value)
    if not isinstance(parsed, ipaddress.IPv4Address) or not parsed.is_global:
        raise argparse.ArgumentTypeError("public IP must be a globally routable IPv4 address")
    return parsed


def _backend_ip(value: str) -> ipaddress.IPv4Address:
    parsed = ipaddress.ip_address(value)
    if not isinstance(parsed, ipaddress.IPv4Address) or not parsed.is_private:
        raise argparse.ArgumentTypeError("Relay backend must be a private IPv4 address")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if parsed not in {27461, 27462}:
        raise argparse.ArgumentTypeError("service port must be explicitly 27461 or 27462")
    return parsed


def render(
    public_ip: ipaddress.IPv4Address,
    service_port: int,
    backend_ip: ipaddress.IPv4Address,
) -> str:
    authority = f"{public_ip}:{service_port}"
    rendered = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__CHEBY_PUBLIC_IP__", str(public_ip))
        .replace("__CHEBY_PUBLIC_AUTHORITY__", authority)
        .replace("__CHEBY_RELAY_BACKEND_IP__", str(backend_ip))
    )
    if "__CHEBY_" in rendered:
        raise RuntimeError("Nginx template contains an unresolved placeholder")
    if re.search(r"\blisten\s+(?:[^; ]+:)?(?:80|443)\b", rendered):
        raise RuntimeError("forbidden public listener appeared in rendered config")
    return rendered


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("refusing to replace a symlinked Nginx config")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        # Bootstrap runs as root while Edge is deliberately UID/GID 101:101.
        # The bind-mounted config contains no secrets and therefore must be
        # world-readable; otherwise the non-root Nginx process cannot open it.
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-ip", required=True, type=_public_ip)
    parser.add_argument("--service-port", required=True, type=_port)
    parser.add_argument("--backend-ip", required=True, type=_backend_ip)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    _atomic_write(
        args.output,
        render(args.public_ip, args.service_port, args.backend_ip),
    )
    print(f"rendered Relay Edge config for {args.public_ip}:{args.service_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
