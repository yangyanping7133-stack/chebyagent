#!/usr/bin/env python3
"""Install Relay Node bootstrap material without printing credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit


NODE_ID = re.compile(r"^node_[A-Za-z0-9_-]{22}$")
ASSISTANT_ID = re.compile(r"^asst_[A-Za-z0-9_-]{22}$")
NODE_TOKEN = re.compile(r"^rly1_[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$")
PRIVATE_RELAY_NODE_URLS = frozenset(
    {
        "ws://127.0.0.1:18080/relay/v1/node",
        "ws://[::1]:18080/relay/v1/node",
        "ws://172.31.61.3:8080/relay/v1/node",
        "ws://172.31.62.3:8080/relay/v1/node",
    }
)


def read_private_json(path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise ValueError("bootstrap path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or not 1 <= metadata.st_size <= 4096
        ):
            raise ValueError("bootstrap file permissions or size are unsafe")
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            raise ValueError("bootstrap file is too large")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("bootstrap must be a JSON object")
    return value


def relay_node_url(origin: object) -> str:
    if not isinstance(origin, str):
        raise ValueError("relay origin is invalid")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("relay origin must be a credential-free HTTPS origin")
    port = "" if parsed.port is None else f":{parsed.port}"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"wss://{host}{port}/relay/v1/node"


def private_relay_node_url(value: str) -> str:
    if value not in PRIVATE_RELAY_NODE_URLS:
        raise ValueError("private relay URL is not an approved fixed endpoint")
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("private relay URL must not contain credentials")
    return value


def validate_output(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("output paths must be absolute")
    if not path.parent.is_dir():
        raise ValueError("every output parent must already exist")
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise ValueError("output parent directories must be private")
    if any(character in str(path) for character in ("\n", "\r", "\x00", "#", "=")):
        raise ValueError("output paths contain unsafe env-file characters")


def write_private(path: Path, value: str, mode: int, uid: int, gid: int) -> None:
    validate_output(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            descriptor = -1
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def install(args: argparse.Namespace) -> None:
    bootstrap = read_private_json(Path(args.bootstrap))
    if set(bootstrap) != {"v", "relayOrigin", "assistantId", "nodeId", "nodeToken"}:
        raise ValueError("bootstrap fields are invalid")
    if bootstrap["v"] != 1:
        raise ValueError("bootstrap version is unsupported")
    assistant_id = bootstrap["assistantId"]
    node_id = bootstrap["nodeId"]
    node_token = bootstrap["nodeToken"]
    if (
        not isinstance(assistant_id, str)
        or ASSISTANT_ID.fullmatch(assistant_id) is None
    ):
        raise ValueError("assistant identity is invalid")
    if not isinstance(node_id, str) or NODE_ID.fullmatch(node_id) is None:
        raise ValueError("Node identity is invalid")
    if not isinstance(node_token, str) or NODE_TOKEN.fullmatch(node_token) is None:
        raise ValueError("Node credential is invalid")
    relay_url_override = getattr(args, "relay_url", None)
    relay_url = (
        private_relay_node_url(relay_url_override)
        if relay_url_override is not None
        else relay_node_url(bootstrap["relayOrigin"])
    )

    env_path = Path(args.env_output)
    token_path = Path(args.token_output)
    gateway_path = Path(args.gateway_secret_output)
    outputs = {env_path, token_path, gateway_path}
    if len(outputs) != 3:
        raise ValueError("outputs must be three separate files")
    for path in outputs:
        validate_output(path)
        if path.exists() or path.is_symlink():
            raise ValueError("output already exists")

    created: list[Path] = []
    try:
        write_private(token_path, node_token, 0o400, args.runtime_uid, args.runtime_gid)
        created.append(token_path)
        write_private(
            gateway_path,
            "pair_" + secrets.token_urlsafe(32),
            0o400,
            args.runtime_uid,
            args.runtime_gid,
        )
        created.append(gateway_path)
        env_value = "\n".join(
            (
                f"CHEBY_CONNECTOR_RELAY_URL={relay_url}",
                f"CHEBY_CONNECTOR_ID={node_id}",
                f"CHEBY_CONNECTOR_NODE_TOKEN_PATH={token_path}",
                f"CHEBY_CONNECTOR_GATEWAY_SECRET_PATH={gateway_path}",
            )
        )
        write_private(env_path, env_value, 0o600, 0, 0)
        created.append(env_path)
    except Exception:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise

    print("Connector configuration installed; credential values were not printed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install a private ChebyCodex Relay Node bootstrap"
    )
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--env-output", required=True)
    parser.add_argument("--token-output", required=True)
    parser.add_argument("--gateway-secret-output", required=True)
    parser.add_argument(
        "--relay-url",
        help=(
            "override the public bootstrap origin with one approved local/private "
            "Relay Node URL; the value is never printed"
        ),
    )
    parser.add_argument("--runtime-uid", type=int, default=10002)
    parser.add_argument("--runtime-gid", type=int, default=10002)
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("install_node_bootstrap.py must run as root")
    if arguments.runtime_uid <= 0 or arguments.runtime_gid <= 0:
        raise SystemExit("runtime UID/GID must be positive non-root values")
    try:
        install(arguments)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise SystemExit(f"Connector bootstrap installation failed: {exc}") from None


if __name__ == "__main__":
    main()
