from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import stat
import struct
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional


PHONEBRIDGE_SOURCE = Path("/opt/phonebridge/phonebridge.mjs")
PHONEBRIDGE_NODE = "/usr/bin/node"
PHONEBRIDGE_SHA256 = "562f66266da07844255ccb9c0fcf9346c62ab00807a58c8b26be34bd271af9f9"
PHONEBRIDGE_BROKER_SOCKET = Path("/home/cheby/.codex/runtime/phonebridge-broker.sock")
PHONEBRIDGE_BROKER_MAX_REQUEST = 64 * 1024
PHONEBRIDGE_BROKER_MAX_RESPONSE = 12 * 1024 * 1024
PHONEBRIDGE_BROKER_TOOL_ARGS = {
    "status": set(),
    "ui_tree": set(),
    "screenshot": set(),
    "tap": {"x", "y"},
    "swipe": {"start_x", "start_y", "end_x", "end_y", "duration_ms"},
    "input_text": {"text"},
    "back": set(),
    "home": set(),
    "recents": set(),
    "open_app": {"package_name"},
    "open_url": {"url"},
}


def validate_phonebridge_runtime(environ: Mapping[str, str]) -> bool:
    enabled = environ.get("CHEBY_PHONEBRIDGE_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise RuntimeError("CHEBY_PHONEBRIDGE_ENABLED must be true or false")
    if enabled == "false":
        return False
    env_path = Path(environ.get("PHONEBRIDGE_ENV_FILE", ""))
    if not env_path.is_absolute():
        raise RuntimeError("PHONEBRIDGE_ENV_FILE must be absolute")
    metadata = env_path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_size > 64 * 1024
    ):
        raise RuntimeError("PhoneBridge env file permissions or size are unsafe")
    source_metadata = PHONEBRIDGE_SOURCE.lstat()
    if (
        not stat.S_ISREG(source_metadata.st_mode)
        or stat.S_ISLNK(source_metadata.st_mode)
        or source_metadata.st_uid != 0
        or source_metadata.st_mode & 0o222
    ):
        raise RuntimeError("reviewed PhoneBridge source mount is missing or unsafe")
    if hashlib.sha256(PHONEBRIDGE_SOURCE.read_bytes()).hexdigest() != PHONEBRIDGE_SHA256:
        raise RuntimeError("reviewed PhoneBridge source hash does not match")
    return True


async def start_phonebridge(
    environ: Optional[Mapping[str, str]] = None,
) -> asyncio.subprocess.Process | None:
    values = os.environ if environ is None else environ
    if not validate_phonebridge_runtime(values):
        return None
    process = await asyncio.create_subprocess_exec(
        PHONEBRIDGE_NODE,
        str(PHONEBRIDGE_SOURCE),
        "serve",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(values),
    )
    try:
        await _wait_until_ready(process)
        return process
    except Exception:
        await stop_phonebridge(process)
        raise


async def _wait_until_ready(process: asyncio.subprocess.Process) -> None:
    for _ in range(20):
        if process.returncode is not None:
            raise RuntimeError("PhoneBridge failed its startup gate")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 3437), timeout=0.25
            )
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=0.25)
            writer.close()
            await writer.wait_closed()
            if status.startswith(b"HTTP/1.1 200"):
                return
        except (OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError("PhoneBridge local health gate timed out")


async def stop_phonebridge(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def start_phonebridge_broker(
    environ: Optional[Mapping[str, str]] = None,
) -> asyncio.AbstractServer:
    values = os.environ if environ is None else environ
    socket_path = Path(values.get("CHEBY_PHONEBRIDGE_BROKER_SOCKET", str(PHONEBRIDGE_BROKER_SOCKET)))
    if not socket_path.is_absolute():
        raise RuntimeError("CHEBY_PHONEBRIDGE_BROKER_SOCKET must be absolute")
    token = _read_private_token(Path(values.get(
        "PHONEBRIDGE_LOCAL_TOKEN_FILE",
        "/run/secrets/phonebridge_local_token",
    )))
    endpoint = values.get("CHEBY_PHONEBRIDGE_URL", "http://127.0.0.1:3437").rstrip("/")
    if endpoint != "http://127.0.0.1:3437":
        raise RuntimeError("PhoneBridge broker endpoint must remain on private loopback")

    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readexactly(4), timeout=5)
            size = struct.unpack("!I", header)[0]
            if size <= 0 or size > PHONEBRIDGE_BROKER_MAX_REQUEST:
                raise ValueError("PhoneBridge broker request exceeds the size limit")
            raw = await asyncio.wait_for(reader.readexactly(size), timeout=5)
            request = json.loads(raw)
            tool, body = _validate_broker_request(request)
            response = await asyncio.to_thread(_broker_http_request, endpoint, token, tool, body)
        except Exception as error:
            response = {"ok": False, "error": _safe_error(error)}
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > PHONEBRIDGE_BROKER_MAX_RESPONSE:
            payload = b'{"ok":false,"error":"PhoneBridge broker response exceeds the size limit"}'
        writer.write(struct.pack("!I", len(payload)) + payload)
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    setattr(server, "_cheby_socket_path", socket_path)
    os.chmod(socket_path, 0o600)
    return server


async def stop_phonebridge_broker(server: asyncio.AbstractServer | None) -> None:
    socket_path = PHONEBRIDGE_BROKER_SOCKET
    if server is not None:
        socket_path = getattr(server, "_cheby_socket_path", socket_path)
        server.close()
        await server.wait_closed()
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()


def _read_private_token(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o077:
        raise RuntimeError("PhoneBridge token file permissions are unsafe")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 24 or len(value) > 4096:
        raise RuntimeError("PhoneBridge token length is invalid")
    return value


def _validate_broker_request(value: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) - {"tool", "arguments", "device_id"}:
        raise ValueError("PhoneBridge broker request shape is invalid")
    tool = value.get("tool")
    arguments = value.get("arguments", {})
    device_id = value.get("device_id", "")
    if tool not in PHONEBRIDGE_BROKER_TOOL_ARGS or not isinstance(arguments, dict):
        raise ValueError("PhoneBridge broker tool is not allowlisted")
    allowed_args = PHONEBRIDGE_BROKER_TOOL_ARGS[tool]
    if set(arguments) - allowed_args:
        raise ValueError("PhoneBridge broker arguments are not allowlisted")
    if not isinstance(device_id, str) or len(device_id) > 256:
        raise ValueError("PhoneBridge broker device id is invalid")
    if tool == "open_app" and not re.fullmatch(r"[A-Za-z0-9_.]{1,255}", str(arguments.get("package_name", ""))):
        raise ValueError("PhoneBridge broker package is invalid")
    if tool == "open_url" and (not isinstance(arguments.get("url"), str) or not 1 <= len(arguments["url"]) <= 8192):
        raise ValueError("PhoneBridge broker URL is invalid")
    if tool == "input_text" and len(str(arguments.get("text", ""))) > 8192:
        raise ValueError("PhoneBridge broker text is too large")
    body = {"tool": tool, "arguments": arguments}
    if device_id:
        body["device_id"] = device_id
    return tool, body


def _broker_http_request(
    endpoint: str,
    token: str,
    tool: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    del tool
    request = urllib.request.Request(
        f"{endpoint}/command",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        payload = response.read(PHONEBRIDGE_BROKER_MAX_RESPONSE + 1)
    if len(payload) > PHONEBRIDGE_BROKER_MAX_RESPONSE:
        raise RuntimeError("PhoneBridge response exceeds the size limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("PhoneBridge response is invalid")
    return value


def _safe_error(error: Exception) -> str:
    message = error.args[0] if error.args else error.__class__.__name__
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(message)).strip()[:256]
