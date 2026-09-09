from __future__ import annotations

import asyncio
import json
import shutil
import struct
import tempfile
from pathlib import Path

import pytest

from cheby_connector import local_mcp, phonebridge_runtime


@pytest.mark.asyncio
async def test_broker_holds_token_and_forwards_only_allowlisted_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    token.chmod(0o400)
    short_root = Path(tempfile.mkdtemp(prefix="cheby-broker-", dir="/tmp"))
    socket_path = short_root / "phonebridge.sock"
    observed = {}

    def fake_http(endpoint, secret, tool, body):
        observed.update(endpoint=endpoint, secret=secret, tool=tool, body=body)
        return {"ok": True, "result": {"package_name": "com.android.settings"}}

    monkeypatch.setattr(phonebridge_runtime, "_broker_http_request", fake_http)
    server = await phonebridge_runtime.start_phonebridge_broker({
        "PHONEBRIDGE_LOCAL_TOKEN_FILE": str(token),
        "CHEBY_PHONEBRIDGE_URL": "http://127.0.0.1:3437",
        "CHEBY_PHONEBRIDGE_BROKER_SOCKET": str(socket_path),
    })
    try:
        result = await asyncio.to_thread(
            local_mcp._phone_broker_request,
            socket_path,
            "open_app",
            {"package_name": "com.android.settings", "device_id": "device-1"},
        )
        assert result["ok"] is True
        assert observed == {
            "endpoint": "http://127.0.0.1:3437",
            "secret": "t" * 32,
            "tool": "open_app",
            "body": {
                "tool": "open_app",
                "arguments": {"package_name": "com.android.settings"},
                "device_id": "device-1",
            },
        }
    finally:
        await phonebridge_runtime.stop_phonebridge_broker(server)
        shutil.rmtree(short_root)
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_broker_rejects_unknown_tool_and_argument_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    token.chmod(0o400)
    short_root = Path(tempfile.mkdtemp(prefix="cheby-broker-", dir="/tmp"))
    socket_path = short_root / "phonebridge.sock"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("rejected broker request reached raw PhoneBridge")

    monkeypatch.setattr(phonebridge_runtime, "_broker_http_request", forbidden)
    server = await phonebridge_runtime.start_phonebridge_broker({
        "PHONEBRIDGE_LOCAL_TOKEN_FILE": str(token),
        "CHEBY_PHONEBRIDGE_URL": "http://127.0.0.1:3437",
        "CHEBY_PHONEBRIDGE_BROKER_SOCKET": str(socket_path),
    })
    try:
        for request in (
            {"tool": "shell", "arguments": {}},
            {"tool": "open_app", "arguments": {"package_name": "com.android.settings", "raw": True}},
        ):
            reader, writer = await asyncio.open_unix_connection(socket_path)
            body = json.dumps(request).encode("utf-8")
            writer.write(struct.pack("!I", len(body)) + body)
            await writer.drain()
            size = struct.unpack("!I", await reader.readexactly(4))[0]
            response = json.loads(await reader.readexactly(size))
            writer.close()
            await writer.wait_closed()
            assert response["ok"] is False
            assert "allowlist" in response["error"]
    finally:
        await phonebridge_runtime.stop_phonebridge_broker(server)
        shutil.rmtree(short_root)
