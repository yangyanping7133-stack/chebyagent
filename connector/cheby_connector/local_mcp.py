"""Local stdio MCP servers for PhoneBridge, offline memory, and offline skills.

The servers intentionally use only Python's standard library.  They run as
Codex child processes, open no listener, and keep credentials in read-only
files rather than command arguments or environment values.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Iterator


MAX_RPC_BYTES = 1 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
MAX_PHONE_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_MEMORY_FILE_BYTES = 8 * 1024 * 1024
MAX_MEMORY_RECORDS = 10_000
MAX_MEMORY_CONTENT_BYTES = 64 * 1024
MAX_SKILL_FILE_BYTES = 512 * 1024
MAX_SKILL_RECORDS = 2_000


_latest_screenshot_transform: dict[str, float] | None = None


def _env_path(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _data_root() -> Path:
    return _env_path("CHEBY_LOCAL_MCP_DATA_ROOT", "/home/cheby/.codex/local-mcp")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(payload) > MAX_MEMORY_FILE_BYTES:
        raise ValueError("memory store exceeds the configured size limit")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _text_result(value: Any, text: str | None = None) -> dict[str, Any]:
    rendered = text if text is not None else json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": rendered}], "structuredContent": value}


def _tool(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    read_only: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties or {},
            **({"required": required} if required else {}),
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": False,
        },
    }


def _read_secret_file(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("PhoneBridge token path must be a regular non-symlink file")
    if info.st_mode & 0o077:
        raise ValueError("PhoneBridge token file must not be group/world accessible")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 24 or len(value) > 4096:
        raise ValueError("PhoneBridge token file has an invalid length")
    return value


PHONE_TOOL_MAP = {
    "android_phone_status": "status",
    "android_ui_tree": "ui_tree",
    "android_capture_screenshot": "screenshot",
    "android_tap": "tap",
    "android_swipe": "swipe",
    "android_input_text": "input_text",
    "android_press_back": "back",
    "android_press_home": "home",
    "android_press_recents": "recents",
    "android_open_app": "open_app",
}


def _phone_tools() -> list[dict[str, Any]]:
    device = {"device_id": {"type": "string", "maxLength": 256}}
    coordinate_space = {
        "coordinate_space": {
            "type": "string",
            "enum": ["screenshot", "screen"],
            "description": (
                "Use screenshot for coordinates read from the latest Android screenshot; "
                "use screen for full-size bounds returned by android_ui_tree."
            ),
        }
    }
    return [
        _tool("android_phone_status", "Android phone status", "Read live PhoneBridge and ChebyNode state.", device, read_only=True),
        _tool("android_capabilities", "Android capabilities", "List the available Android control tools and owner checkout confirmation settings.", read_only=True),
        _tool("android_ui_tree", "Android UI tree", "Read the current Accessibility UI tree.", device, read_only=True),
        _tool("android_capture_screenshot", "Android screenshot", "Capture the phone screen as native image content and save a private artifact. In code mode forward each image content block with image(block), text blocks with text(block.text), and structuredContent separately. Never JSON-stringify screenshot image data into text.", device, read_only=True),
        _tool(
            "android_tap",
            "Android tap",
            "Tap coordinates in the declared coordinate space; screenshot coordinates are mapped to the physical phone screen. Checkout commits use the local owner confirmation setting; ordinary taps dispatch directly.",
            {"x": {"type": "number"}, "y": {"type": "number"}, **coordinate_space, **device},
            ["x", "y", "coordinate_space"],
            read_only=False,
        ),
        _tool(
            "android_swipe",
            "Android swipe",
            "Swipe in the declared coordinate space; screenshot coordinates are mapped to the physical phone screen.",
            {"start_x": {"type": "number"}, "start_y": {"type": "number"}, "end_x": {"type": "number"}, "end_y": {"type": "number"}, "duration_ms": {"type": "integer", "minimum": 80, "maximum": 5000}, **coordinate_space, **device},
            ["start_x", "start_y", "end_x", "end_y", "coordinate_space"],
            read_only=False,
        ),
        _tool("android_input_text", "Android input text", "Set text on the focused editable field.", {"text": {"type": "string", "maxLength": 8192}, **device}, ["text"], read_only=False),
        _tool("android_press_back", "Android Back", "Press Android Back.", device, read_only=False),
        _tool("android_press_home", "Android Home", "Press Android Home.", device, read_only=False),
        _tool("android_press_recents", "Android Recents", "Open Android recent apps.", device, read_only=False),
        _tool("android_open_app", "Android open app", "Launch an installed app package without app-category restrictions.", {"package_name": {"type": "string", "pattern": "^[A-Za-z0-9_.]+$", "maxLength": 255}, **device}, ["package_name"], read_only=False),
    ]


def _phone_endpoint() -> str:
    raw = os.environ.get("CHEBY_PHONEBRIDGE_URL", "http://127.0.0.1:3437")
    parsed = urllib.parse.urlsplit(raw)
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed_hosts
        or parsed.port != 3437
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("CHEBY_PHONEBRIDGE_URL must be the private PhoneBridge http endpoint on port 3437")
    return raw.rstrip("/")


def _phone_request(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    broker = os.environ.get("CHEBY_PHONEBRIDGE_BROKER_SOCKET", "").strip()
    if broker:
        return _phone_broker_request(Path(broker), tool_name, arguments)
    token_path = _env_path("CHEBY_PHONEBRIDGE_TOKEN_FILE", "/run/secrets/phonebridge_local_token")
    token = _read_secret_file(token_path)
    args = dict(arguments)
    device_id = str(args.pop("device_id", ""))
    if len(device_id) > 256:
        raise ValueError("device_id is too long")
    body = _json_bytes({"tool": tool_name, "arguments": args, **({"device_id": device_id} if device_id else {})})
    if len(body) > 64 * 1024:
        raise ValueError("PhoneBridge request is too large")
    request = urllib.request.Request(
        f"{_phone_endpoint()}/command",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = response.read(MAX_PHONE_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error_payload = error.read(64 * 1024)
        detail = ""
        try:
            error_value = json.loads(error_payload)
            if isinstance(error_value, dict) and isinstance(error_value.get("error"), str):
                detail = re.sub(r"[\x00-\x1f\x7f]+", " ", error_value["error"]).strip()[:256]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"PhoneBridge returned HTTP {error.code}{suffix}") from None
    except urllib.error.URLError:
        raise RuntimeError("PhoneBridge is unavailable") from None
    if len(payload) > MAX_PHONE_RESPONSE_BYTES:
        raise RuntimeError("PhoneBridge response exceeds the size limit")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("PhoneBridge returned an invalid response") from None
    if not isinstance(value, dict) or value.get("ok") is False:
        raise RuntimeError("PhoneBridge rejected the command")
    return value


def _phone_broker_request(
    socket_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not socket_path.is_absolute():
        raise ValueError("CHEBY_PHONEBRIDGE_BROKER_SOCKET must be absolute")
    metadata = socket_path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("PhoneBridge broker socket permissions are unsafe")
    args = dict(arguments)
    device_id = str(args.pop("device_id", ""))
    body = _json_bytes({
        "tool": tool_name,
        "arguments": args,
        **({"device_id": device_id} if device_id else {}),
    })
    if len(body) > 64 * 1024:
        raise ValueError("PhoneBridge broker request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(35)
            client.connect(str(socket_path))
            client.sendall(struct.pack("!I", len(body)) + body)
            size = struct.unpack("!I", _recv_exact(client, 4))[0]
            if size <= 0 or size > MAX_PHONE_RESPONSE_BYTES:
                raise RuntimeError("PhoneBridge broker response exceeds the size limit")
            payload = _recv_exact(client, size)
    except (OSError, TimeoutError):
        raise RuntimeError("PhoneBridge broker is unavailable") from None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("PhoneBridge broker returned an invalid response") from None
    if not isinstance(value, dict) or value.get("ok") is False:
        detail = value.get("error") if isinstance(value, dict) else ""
        suffix = f": {str(detail)[:256]}" if detail else ""
        raise RuntimeError(f"PhoneBridge broker rejected the command{suffix}")
    return value


def _recv_exact(client: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = client.recv(remaining)
        if not chunk:
            raise RuntimeError("PhoneBridge broker response ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _phone_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "android_capabilities":
        return _text_result({
            "ok": True,
            "transport_mode": "private_phonebridge_wss",
            "tools": list(PHONE_TOOL_MAP),
            "phone_side_policy": "Checkout requires local owner consent; read android_phone_status for owner preapproval. Ordinary actions are not blocked by app category, keywords, or missing semantics. Owner authorization UI remains human-only; Android permissions and technical errors still apply.",
        })
    bridge_name = PHONE_TOOL_MAP.get(name)
    if bridge_name is None:
        raise ValueError("unknown PhoneBridge tool")
    request_arguments = _map_phone_coordinates(name, arguments)
    value = _phone_request(bridge_name, request_arguments)
    if name != "android_capture_screenshot":
        response = _text_result(value)
        result = value.get("result")
        awaiting_owner = (
            isinstance(result, dict)
            and result.get("status") == "awaiting_owner_confirmation"
            and result.get("confirmation_required") is True
            and result.get("allowed") is False
        )
        if any(
            isinstance(item, dict) and (
                item.get("ok") is False
                or item.get("allowed") is False
                or item.get("status") == "blocked"
            )
            for item in (value, result)
        ):
            response["isError"] = True
            response["content"].append({
                "type": "text",
                "text": (
                    "The action has NOT executed. Wait for the owner to authorize it on the phone. "
                    "Do not operate the authorization or identity verification UI yourself. "
                    "Only after the owner confirms, re-observe the original app and retry the same action."
                    if awaiting_owner else
                    "The phone action did not succeed. Stop dependent actions and inspect the failure. "
                    "Do not work around a policy refusal with keyboard taps or another input method."
                ),
            })
        return response
    result = value.get("result") if isinstance(value.get("result"), dict) else {}
    _remember_screenshot_transform(result)
    encoded = result.pop("data_base64", "")
    if not isinstance(encoded, str) or not encoded:
        return _text_result(value, "PhoneBridge screenshot completed without image data.")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError("PhoneBridge screenshot encoding is invalid") from None
    if len(binary) > MAX_SCREENSHOT_BYTES:
        raise RuntimeError("PhoneBridge screenshot exceeds the size limit")
    mime = str(result.get("mime_type", "image/jpeg"))
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        mime = "image/jpeg"
    artifact_dir = _env_path("CHEBY_PHONEBRIDGE_ARTIFACT_DIR", "/workspace/.cheby/phone-artifacts")
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    filename = f"phone-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.{suffix}"
    path = artifact_dir / filename
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(binary)
    structured = dict(value)
    structured["result"] = {**result, "artifact_path": str(path), "artifact_image_uri": "cheby-image:" + filename}
    width = int(result.get("width", 0) or 0)
    height = int(result.get("height", 0) or 0)
    screen_width = int(result.get("screen_width", width) or width)
    screen_height = int(result.get("screen_height", height) or height)
    guidance = (
        f"Android screenshot saved to {path}. Image coordinates are {width}x{height}; "
        f"the physical screen is {screen_width}x{screen_height}. For coordinates chosen "
        "from this image, call android_tap/android_swipe with coordinate_space='screenshot'. "
        "Use coordinate_space='screen' only for full-size android_ui_tree bounds."
    )
    return {
        "content": [
            {"type": "text", "text": guidance},
            {"type": "image", "data": encoded, "mimeType": mime},
        ],
        "structuredContent": structured,
    }


def _remember_screenshot_transform(result: dict[str, Any]) -> None:
    global _latest_screenshot_transform
    try:
        width = float(result["width"])
        height = float(result["height"])
        screen_width = float(result.get("screen_width", result.get("source_width", width)))
        screen_height = float(result.get("screen_height", result.get("source_height", height)))
    except (KeyError, TypeError, ValueError):
        _latest_screenshot_transform = None
        return
    if min(width, height, screen_width, screen_height) <= 0:
        _latest_screenshot_transform = None
        return
    _latest_screenshot_transform = {
        "scale_x": screen_width / width,
        "scale_y": screen_height / height,
        "screen_width": screen_width,
        "screen_height": screen_height,
    }


def _map_phone_coordinates(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in {"android_tap", "android_swipe"}:
        return dict(arguments)
    output = dict(arguments)
    coordinate_space = output.pop("coordinate_space", None)
    if coordinate_space not in {"screenshot", "screen"}:
        raise ValueError("coordinate_space must be screenshot or screen")
    coordinate_keys = ("x", "y") if name == "android_tap" else ("start_x", "start_y", "end_x", "end_y")
    for key in coordinate_keys:
        value = output.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name}: {key} must be a finite number; required coordinates: {', '.join(coordinate_keys)}")
    if coordinate_space == "screen":
        return output
    transform = _latest_screenshot_transform
    if transform is None:
        raise RuntimeError("Capture an Android screenshot before using screenshot coordinates")

    def map_axis(key: str, scale_key: str, limit_key: str) -> None:
        try:
            value = float(output[key]) * transform[scale_key]
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        output[key] = min(max(value, 0.0), transform[limit_key] - 1.0)

    if name == "android_tap":
        map_axis("x", "scale_x", "screen_width")
        map_axis("y", "scale_y", "screen_height")
    else:
        map_axis("start_x", "scale_x", "screen_width")
        map_axis("start_y", "scale_y", "screen_height")
        map_axis("end_x", "scale_x", "screen_width")
        map_axis("end_y", "scale_y", "screen_height")
    return output


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _memory_path() -> Path:
    return _data_root() / "memory" / "mem0-export.json"


def _memory_store() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = _memory_path()
    if not path.exists():
        return {"memories": [], "count": 0}, []
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > MAX_MEMORY_FILE_BYTES:
        raise ValueError("offline memory store is not a safe regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("memories", []), list):
        raise ValueError("offline memory store has an invalid format")
    memories = [item for item in value.get("memories", []) if isinstance(item, dict)]
    if len(memories) > MAX_MEMORY_RECORDS:
        raise ValueError("offline memory store has too many records")
    return value, memories


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _search_terms(value: str) -> list[str]:
    return [item for item in re.split(r"[^\w\u4e00-\u9fff-]+", _normalize(value).lower()) if item]


def _score(text: str, query: str) -> int:
    haystack = _normalize(text).lower()
    return sum(3 if len(term) > 2 else 1 for term in _search_terms(query) if term in haystack)


def _memory_text(item: dict[str, Any]) -> str:
    return _normalize(" ".join([
        str(item.get("memory", item.get("text", ""))),
        str(item.get("id", "")),
        json.dumps(item.get("metadata", {}), ensure_ascii=False),
        " ".join(str(value) for value in item.get("categories", []) if isinstance(item.get("categories"), list)),
    ]))


def _memory_record(item: dict[str, Any]) -> dict[str, Any]:
    content = str(item.get("memory", item.get("text", "")))
    title = _normalize(content)[:90] or str(item.get("id", ""))
    item_id = str(item.get("id", ""))
    metadata = {
        "categories": item.get("categories", []),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
        "metadata": item.get("metadata", {}),
        "offline": True,
    }
    return {
        "id": item_id,
        "title": title,
        "text": f"# {title}\n\n{content}\n\nMetadata:\n{json.dumps(metadata, ensure_ascii=False, indent=2)}",
        "url": f"offline-memory://{urllib.parse.quote(item_id, safe='')}",
        "metadata": metadata,
    }


def _memory_tools() -> list[dict[str, Any]]:
    limit = {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
    return [
        _tool("search", "Search offline memories", "Search the private local memory archive without network access.", {"query": {"type": "string", "maxLength": 4096}, "limit": limit}, ["query"], read_only=True),
        _tool("fetch", "Fetch offline memory", "Fetch one private local memory by exact id.", {"id": {"type": "string", "maxLength": 256}}, ["id"], read_only=True),
        _tool("memory_list_recent", "List recent memories", "List recent private local memory records.", {"limit": {**limit, "maximum": 100}, "offset": {"type": "integer", "minimum": 0, "maximum": MAX_MEMORY_RECORDS, "default": 0}}, read_only=True),
        _tool("memory_remember", "Remember local memory", "Append one curated durable fact to private local storage.", {"content": {"type": "string", "maxLength": MAX_MEMORY_CONTENT_BYTES}, "metadata": {"type": "object"}, "categories": {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 128}}}, ["content"], read_only=False),
    ]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _memory_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload, memories = _memory_store()
    if name == "search":
        query = str(arguments.get("query", ""))[:4096]
        limit = _bounded_int(arguments.get("limit"), 10, 1, 50)
        found = sorted(memories, key=lambda item: _score(_memory_text(item), query), reverse=True)
        if query.strip():
            found = [item for item in found if _score(_memory_text(item), query) > 0]
        results = [{"id": record["id"], "title": record["title"], "url": record["url"]} for record in map(_memory_record, found[:limit])]
        return _text_result({"results": results})
    if name == "fetch":
        item_id = str(arguments.get("id", ""))[:256]
        item = next((record for record in memories if str(record.get("id", "")) == item_id), None)
        if item is None:
            raise ValueError("offline memory was not found")
        return _text_result(_memory_record(item))
    if name == "memory_list_recent":
        limit = _bounded_int(arguments.get("limit"), 20, 1, 100)
        offset = _bounded_int(arguments.get("offset"), 0, 0, MAX_MEMORY_RECORDS)
        ordered = sorted(memories, key=lambda item: str(item.get("updated_at", item.get("created_at", ""))), reverse=True)
        items = [_memory_record(item) for item in ordered[offset:offset + limit]]
        more = offset + len(items) < len(ordered)
        return _text_result({"items": items, "total_count": len(ordered), "limit": limit, "offset": offset, "has_more": more, "next_offset": offset + len(items) if more else None})
    if name == "memory_remember":
        content = str(arguments.get("content", "")).strip()
        if not content or len(content.encode("utf-8")) > MAX_MEMORY_CONTENT_BYTES:
            raise ValueError("memory content is empty or too large")
        metadata = arguments.get("metadata", {})
        categories = arguments.get("categories", [])
        if not isinstance(metadata, dict) or len(_json_bytes(metadata)) > MAX_MEMORY_CONTENT_BYTES:
            raise ValueError("memory metadata is invalid or too large")
        if not isinstance(categories, list) or len(categories) > 32:
            raise ValueError("memory categories are invalid")
        with _exclusive_lock(_memory_path().with_suffix(".lock")):
            payload, memories = _memory_store()
            if len(memories) >= MAX_MEMORY_RECORDS:
                raise ValueError("offline memory record limit reached")
            stamp = _now()
            record = {
                "id": f"offline_{secrets.token_hex(16)}",
                "memory": content,
                "user_id": str(payload.get("default_scope", {}).get("user_id", "local")),
                "agent_id": "codex",
                "app_id": "chebycodex-offline-memory",
                "run_id": "",
                "categories": [str(item)[:128] for item in categories],
                "metadata": {"source": "chebycodex-local-mcp", **metadata},
                "created_at": stamp,
                "updated_at": stamp,
                "expiration_date": None,
            }
            payload["memories"] = [*memories, record]
            payload["count"] = len(payload["memories"])
            payload["updated_at"] = stamp
            _atomic_json(_memory_path(), payload)
        return _text_result({"ok": True, "id": record["id"]})
    raise ValueError("unknown memory tool")


def _skills_dir() -> Path:
    return _data_root() / "skill" / "skills"


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and re.fullmatch(r"[A-Za-z0-9_-]+", key.strip()):
            metadata[key.strip()] = value.strip().strip("\"'")[:1024]
    return metadata, text[end + 4:].lstrip("\n")


def _load_skills() -> list[dict[str, Any]]:
    root = _skills_dir()
    if not root.exists():
        return []
    root_resolved = root.resolve()
    records: list[dict[str, Any]] = []
    entries = sorted(root.iterdir(), key=lambda item: item.name)
    if len(entries) > MAX_SKILL_RECORDS:
        raise ValueError("offline skill store has too many records")
    for directory in entries:
        if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[\w\u4e00-\u9fff.-]{1,128}", directory.name):
            continue
        path = directory / "SKILL.md"
        if not path.exists() or path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if root_resolved not in resolved.parents or path.stat().st_size > MAX_SKILL_FILE_BYTES:
            continue
        text = path.read_text(encoding="utf-8")
        metadata, body = _parse_front_matter(text)
        records.append({"id": directory.name, "path": path, "text": text, "metadata": metadata, "body": body, "mtime": path.stat().st_mtime})
    return records


def _skill_record(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item["metadata"]
    title = metadata.get("title") or metadata.get("name") or _normalize(item["body"])[:90] or item["id"]
    return {
        "id": item["id"],
        "title": title,
        "text": item["text"],
        "url": f"offline-skill://{urllib.parse.quote(item['id'], safe='')}",
        "metadata": {
            "kind": metadata.get("kind", "skill"),
            "scope": metadata.get("scope", "global"),
            "project": metadata.get("project", ""),
            "tags": [value.strip() for value in metadata.get("tags", "").split(",") if value.strip()],
            "status": metadata.get("status", "active"),
            "offline": True,
        },
    }


def _skill_tools() -> list[dict[str, Any]]:
    return [
        _tool("search", "Search offline skills", "Search local reviewed skills, workflows, and gates without network access.", {"query": {"type": "string", "maxLength": 4096}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}, ["query"], read_only=True),
        _tool("fetch", "Fetch offline skill", "Fetch one local reviewed SKILL.md record by exact id.", {"id": {"type": "string", "maxLength": 128}}, ["id"], read_only=True),
        _tool("skill_list_recent", "List recent skills", "List recent local reviewed skills.", {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}, "offset": {"type": "integer", "minimum": 0, "maximum": MAX_SKILL_RECORDS, "default": 0}}, read_only=True),
        _tool("skill_remember", "Remember local skill", "Create one private local SKILL.md from a curated reusable lesson.", {"title": {"type": "string", "maxLength": 200}, "content": {"type": "string", "maxLength": MAX_SKILL_FILE_BYTES}, "kind": {"type": "string", "maxLength": 64}, "scope": {"type": "string", "maxLength": 64}, "project": {"type": "string", "maxLength": 128}, "tags": {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 128}}}, ["title", "content"], read_only=False),
    ]


def _front_value(value: Any, limit: int) -> str:
    return re.sub(r"[\r\n:]+", " ", str(value or "")).strip()[:limit]


def _skill_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    skills = _load_skills()
    active_skills = [item for item in skills if item["metadata"].get("status", "active") == "active"]
    if name == "search":
        query = str(arguments.get("query", ""))[:4096]
        limit = _bounded_int(arguments.get("limit"), 10, 1, 50)
        ordered = sorted(active_skills, key=lambda item: _score(f"{item['id']} {json.dumps(item['metadata'], ensure_ascii=False)} {item['text']}", query), reverse=True)
        if query.strip():
            ordered = [item for item in ordered if _score(f"{item['id']} {json.dumps(item['metadata'], ensure_ascii=False)} {item['text']}", query) > 0]
        results = [{"id": record["id"], "title": record["title"], "url": record["url"]} for record in map(_skill_record, ordered[:limit])]
        return _text_result({"results": results})
    if name == "fetch":
        item_id = str(arguments.get("id", ""))[:128]
        item = next((record for record in active_skills if record["id"] == item_id), None)
        if item is None:
            raise ValueError("offline skill was not found")
        return _text_result(_skill_record(item))
    if name == "skill_list_recent":
        limit = _bounded_int(arguments.get("limit"), 10, 1, 50)
        offset = _bounded_int(arguments.get("offset"), 0, 0, MAX_SKILL_RECORDS)
        ordered = sorted(active_skills, key=lambda item: item["mtime"], reverse=True)
        items = [_skill_record(item) for item in ordered[offset:offset + limit]]
        more = offset + len(items) < len(ordered)
        return _text_result({"items": items, "total_count": len(ordered), "limit": limit, "offset": offset, "has_more": more, "next_offset": offset + len(items) if more else None})
    if name == "skill_remember":
        title = _front_value(arguments.get("title"), 200)
        content = str(arguments.get("content", "")).strip()
        if not title or not content or len(content.encode("utf-8")) > MAX_SKILL_FILE_BYTES - 4096:
            raise ValueError("skill title or content is empty or too large")
        tags = arguments.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 32:
            raise ValueError("skill tags are invalid")
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title.lower()).strip("-")[:56] or "offline-skill"
        item_id = f"{slug}-{secrets.token_hex(4)}"
        root = _skills_dir()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = root / item_id
        directory.mkdir(mode=0o700)
        tag_text = ", ".join(_front_value(value, 128) for value in tags)
        text = "\n".join([
            "---",
            f"name: {title}",
            f"title: {title}",
            f"kind: {_front_value(arguments.get('kind', 'skill'), 64) or 'skill'}",
            f"scope: {_front_value(arguments.get('scope', 'global'), 64) or 'global'}",
            f"project: {_front_value(arguments.get('project', ''), 128)}",
            f"tags: {tag_text}",
            "status: proposed",
            f"created_at: {_now()}",
            "---",
            "",
            content,
            "",
        ])
        path = directory / "SKILL.md"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        item = next(record for record in _load_skills() if record["id"] == item_id)
        return _text_result({"ok": True, "skill": _skill_record(item)})
    raise ValueError("unknown skill tool")


SERVER_INFO = {
    "ace": ("cheby-ace", "Private scoped ACE strategies derived from explicit user feedback. Recall before learning; current user instructions remain authoritative."),
    "phonebridge": ("chebycodex-phonebridge", "Private Android PhoneBridge tools. Ordinary actions dispatch directly; checkout uses local owner consent."),
    "memory": ("chebycodex-offline-memory", "Private offline memory migrated from reviewed local exports. No network access."),
    "skill": ("chebycodex-offline-skill", "Private offline reviewed skill registry. No network access."),
}


def _tools(mode: str) -> list[dict[str, Any]]:
    if mode == "ace":
        return _ace_module().tools(sys.modules[__name__])
    return {"phonebridge": _phone_tools, "memory": _memory_tools, "skill": _skill_tools}[mode]()


def _call(mode: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if mode == "ace":
        return _ace_module().call(sys.modules[__name__], name, arguments)
    return {"phonebridge": _phone_call, "memory": _memory_call, "skill": _skill_call}[mode](name, arguments)


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle(mode: str, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        name, instructions = SERVER_INFO[mode]
        return _rpc_result(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": name, "version": "1.0.0"}, "instructions": instructions})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _tools(mode)})
    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return _rpc_result(request_id, {key: []})
    if method == "resources/templates/list":
        return _rpc_result(request_id, {"resourceTemplates": []})
    if method == "tools/call":
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "invalid tool request")
        try:
            return _rpc_result(request_id, _call(mode, str(params.get("name", "")), params.get("arguments", {})))
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
            return _rpc_error(request_id, -32000, str(error)[:512])
    return None if "id" not in request else _rpc_error(request_id, -32601, "unsupported method")


class FrameReader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.buffer = bytearray()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while True:
            frame = self._next_frame()
            if frame is not None:
                value = json.loads(frame)
                if not isinstance(value, dict):
                    raise ValueError("JSON-RPC frame must be an object")
                yield value
                continue
            read = getattr(self.stream, "read1", self.stream.read)
            chunk = read(65536)
            if not chunk:
                if self.buffer.strip():
                    raise ValueError("incomplete JSON-RPC frame")
                return
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_RPC_BYTES + MAX_HEADER_BYTES:
                raise ValueError("JSON-RPC input exceeds the size limit")

    def _next_frame(self) -> bytes | None:
        while self.buffer.startswith((b"\r", b"\n", b" ", b"\t")):
            del self.buffer[0]
        if not self.buffer:
            return None
        if bytes(self.buffer[:15]).lower().startswith(b"content-length"):
            marker = self.buffer.find(b"\r\n\r\n")
            if marker < 0:
                if len(self.buffer) > MAX_HEADER_BYTES:
                    raise ValueError("JSON-RPC headers exceed the size limit")
                return None
            headers = bytes(self.buffer[:marker]).decode("ascii")
            match = re.search(r"(?im)^Content-Length:\s*(\d+)\s*$", headers)
            if match is None:
                raise ValueError("Content-Length is required")
            length = int(match.group(1))
            if length > MAX_RPC_BYTES:
                raise ValueError("JSON-RPC frame exceeds the size limit")
            start = marker + 4
            if len(self.buffer) < start + length:
                return None
            frame = bytes(self.buffer[start:start + length])
            del self.buffer[:start + length]
            return frame
        newline = self.buffer.find(b"\n")
        if newline < 0:
            if len(self.buffer) > MAX_RPC_BYTES:
                raise ValueError("JSON-RPC frame exceeds the size limit")
            return None
        frame = bytes(self.buffer[:newline]).strip()
        del self.buffer[:newline + 1]
        return frame or self._next_frame()


def _ace_module():
    # -I omits the script directory. Only add the installed code directory, never cwd.
    import importlib
    directory = str(Path(__file__).resolve().parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module("ace_memory")


def serve(mode: str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    source = stdin or sys.stdin.buffer
    sink = stdout or sys.stdout.buffer
    for request in FrameReader(source):
        response = _handle(mode, request)
        if response is not None:
            sink.write(_json_bytes(response) + b"\n")
            sink.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(SERVER_INFO))
    arguments = parser.parse_args(argv)
    try:
        serve(arguments.mode)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"local MCP stopped: {str(error)[:256]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
