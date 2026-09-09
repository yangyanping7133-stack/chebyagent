from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .config import RelaySettings
from .store import RelayStore


PUBLIC_ASSISTANT_ID = re.compile(r"^asst_[A-Za-z0-9_-]{22}$")
PUBLIC_NODE_ID = re.compile(r"^node_[A-Za-z0-9_-]{22}$")


def validate_relay_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("relay origin must be an HTTPS origin without path or credentials")
    port = "" if parsed.port is None else f":{parsed.port}"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}{port}"


def pairing_envelope(
    *, assistant_id: str, pairing_secret: str
) -> str:
    payload = {
        "v": 1,
        "assistantId": assistant_id,
        "pairingSecret": pairing_secret,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "CXC1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def access_credentials(*, assistant_id: str, pairing_secret: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "accessKey": assistant_id,
            "secretKey": pairing_secret,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _open_private(path: str) -> int:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("bootstrap output path must be absolute")
    if not target.parent.is_dir():
        raise ValueError("bootstrap output parent must already exist")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


def _write_descriptor(descriptor: int, value: str) -> None:
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
        handle.write(value)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_ephemeral_continuity_metadata(
    path_value: str,
) -> tuple[str, str]:
    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata field")
            result[key] = value
        return result

    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("ephemeral metadata path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size <= 0
            or metadata.st_size > 1024
        ):
            raise ValueError("ephemeral metadata file is unsafe")
        raw = os.read(descriptor, 1025)
        if len(raw) > 1024:
            raise ValueError("ephemeral metadata file is too large")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("ephemeral metadata is malformed") from error
    if (
        not isinstance(document, dict)
        or set(document)
        != {"v", "assistantId", "nodeId", "purpose"}
        or document["v"] != 1
        or document["purpose"] != "production-reload-continuity"
        or not isinstance(document["assistantId"], str)
        or PUBLIC_ASSISTANT_ID.fullmatch(document["assistantId"]) is None
        or not isinstance(document["nodeId"], str)
        or PUBLIC_NODE_ID.fullmatch(document["nodeId"]) is None
    ):
        raise ValueError("ephemeral metadata fields are invalid")
    return document["assistantId"], document["nodeId"]


def bootstrap(
    *,
    database: str,
    relay_origin: str,
    device_output: str,
    node_output: str,
    device_format: str = "access",
) -> None:
    if device_format not in {"access", "cxc1"}:
        raise ValueError("unsupported device bootstrap format")
    if Path(device_output) == Path(node_output):
        raise ValueError("device and node outputs must be separate files")
    device_fd = _open_private(device_output)
    try:
        node_fd = _open_private(node_output)
    except Exception:
        os.close(device_fd)
        os.unlink(device_output)
        raise
    store = RelayStore(RelaySettings(db_path=database))
    try:
        bundle = store.bootstrap_assistant()
        device_value = (
            access_credentials(
                assistant_id=bundle.assistant_id,
                pairing_secret=bundle.pairing_secret,
            )
            if device_format == "access"
            else pairing_envelope(
                assistant_id=bundle.assistant_id,
                pairing_secret=bundle.pairing_secret,
            )
        )
        node_value = json.dumps(
            {
                "v": 1,
                "relayOrigin": validate_relay_origin(relay_origin),
                "assistantId": bundle.assistant_id,
                "nodeId": bundle.node_id,
                "nodeToken": bundle.node_token,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        current_device_fd = device_fd
        device_fd = -1
        _write_descriptor(current_device_fd, device_value)
        current_node_fd = node_fd
        node_fd = -1
        _write_descriptor(current_node_fd, node_value)
    finally:
        if device_fd >= 0:
            os.close(device_fd)
        if node_fd >= 0:
            os.close(node_fd)
        store.close()


def bootstrap_ephemeral_continuity(
    *,
    database: str,
    device_output: str,
    metadata_output: str,
) -> None:
    if Path(device_output) == Path(metadata_output):
        raise ValueError("device and metadata outputs must be separate files")
    device_fd = _open_private(device_output)
    try:
        metadata_fd = _open_private(metadata_output)
    except Exception:
        os.close(device_fd)
        os.unlink(device_output)
        raise
    store: RelayStore | None = None
    bundle = None
    artifacts_ready = False
    completed = False
    try:
        store = RelayStore(RelaySettings(db_path=database))
        bundle = store.prepare_ephemeral_continuity_bootstrap()
        current_device_fd = device_fd
        device_fd = -1
        _write_descriptor(
            current_device_fd,
            pairing_envelope(
                assistant_id=bundle.assistant_id,
                pairing_secret=bundle.pairing_secret,
            ),
        )
        _fsync_parent(Path(device_output))
        current_metadata_fd = metadata_fd
        metadata_fd = -1
        _write_descriptor(
            current_metadata_fd,
            json.dumps(
                {
                    "v": 1,
                    "assistantId": bundle.assistant_id,
                    "nodeId": bundle.node_id,
                    "purpose": "production-reload-continuity",
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
        _fsync_parent(Path(metadata_output))
        artifacts_ready = True
        store.bootstrap_ephemeral_continuity(bundle)
        completed = True
    except Exception:
        if artifacts_ready and bundle is not None and store is not None:
            try:
                store.revoke_ephemeral_continuity_assistant(
                    assistant_id=bundle.assistant_id,
                    node_id=bundle.node_id,
                )
            except Exception:
                # Preserve both durable files so the operator can safely retry
                # the guarded fence after an uncertain database outcome.
                pass
        raise
    finally:
        if device_fd >= 0:
            os.close(device_fd)
            Path(device_output).unlink(missing_ok=True)
        if metadata_fd >= 0:
            os.close(metadata_fd)
        if not completed and not artifacts_ready:
            Path(device_output).unlink(missing_ok=True)
            Path(metadata_output).unlink(missing_ok=True)
        if store is not None:
            store.close()


def rotate_node(
    *, database: str, assistant_id: str, node_id: str, output: str
) -> None:
    descriptor = _open_private(output)
    completed = False
    store = RelayStore(RelaySettings(db_path=database))
    try:
        token = store.rotate_node_credential(assistant_id, node_id)
        current_descriptor = descriptor
        descriptor = -1
        _write_descriptor(
            current_descriptor,
            json.dumps(
                {
                    "v": 1,
                    "assistantId": assistant_id,
                    "nodeId": node_id,
                    "nodeToken": token,
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        store.close()
        if not completed:
            Path(output).unlink(missing_ok=True)


def revoke_ephemeral_continuity(
    *,
    database: str,
    assistant_id: str,
    node_id: str,
) -> None:
    store = RelayStore(RelaySettings(db_path=database))
    try:
        store.revoke_ephemeral_continuity_assistant(
            assistant_id=assistant_id,
            node_id=node_id,
        )
    finally:
        store.close()


def purge_ephemeral_continuity(
    *,
    database: str,
    assistant_id: str,
    node_id: str,
) -> None:
    store = RelayStore(RelaySettings(db_path=database))
    try:
        store.purge_ephemeral_continuity_assistant(
            assistant_id=assistant_id,
            node_id=node_id,
        )
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ChebyCodex Relay administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("bootstrap")
    create.add_argument("--database", required=True)
    create.add_argument("--relay-origin", required=True)
    create.add_argument("--device-output", required=True)
    create.add_argument("--node-output", required=True)
    create.add_argument(
        "--device-format",
        choices=("access", "cxc1"),
        default="access",
    )
    create_ephemeral = subparsers.add_parser(
        "bootstrap-ephemeral-continuity"
    )
    create_ephemeral.add_argument("--database", required=True)
    create_ephemeral.add_argument("--device-output", required=True)
    create_ephemeral.add_argument("--metadata-output", required=True)
    rotate = subparsers.add_parser("rotate-node")
    rotate.add_argument("--database", required=True)
    rotate.add_argument("--assistant-id", required=True)
    rotate.add_argument("--node-id", required=True)
    rotate.add_argument("--output", required=True)
    revoke = subparsers.add_parser("revoke")
    revoke.add_argument("--database", required=True)
    revoke.add_argument("--principal-id", required=True)
    revoke_ephemeral = subparsers.add_parser(
        "revoke-ephemeral-continuity"
    )
    revoke_ephemeral.add_argument("--database", required=True)
    revoke_ephemeral.add_argument("--metadata", required=True)
    purge_ephemeral = subparsers.add_parser(
        "purge-ephemeral-continuity"
    )
    purge_ephemeral.add_argument("--database", required=True)
    purge_ephemeral.add_argument("--metadata", required=True)
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap(
            database=args.database,
            relay_origin=args.relay_origin,
            device_output=args.device_output,
            node_output=args.node_output,
            device_format=args.device_format,
        )
    elif args.command == "bootstrap-ephemeral-continuity":
        bootstrap_ephemeral_continuity(
            database=args.database,
            device_output=args.device_output,
            metadata_output=args.metadata_output,
        )
    elif args.command == "rotate-node":
        rotate_node(
            database=args.database,
            assistant_id=args.assistant_id,
            node_id=args.node_id,
            output=args.output,
        )
    elif args.command == "revoke-ephemeral-continuity":
        assistant_id, node_id = read_ephemeral_continuity_metadata(
            args.metadata
        )
        revoke_ephemeral_continuity(
            database=args.database,
            assistant_id=assistant_id,
            node_id=node_id,
        )
    elif args.command == "purge-ephemeral-continuity":
        assistant_id, node_id = read_ephemeral_continuity_metadata(
            args.metadata
        )
        purge_ephemeral_continuity(
            database=args.database,
            assistant_id=assistant_id,
            node_id=node_id,
        )
    else:
        store = RelayStore(RelaySettings(db_path=args.database))
        try:
            store.revoke_principal(args.principal_id)
        finally:
            store.close()


if __name__ == "__main__":
    main()
