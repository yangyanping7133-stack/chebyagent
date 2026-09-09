from __future__ import annotations

import io
import json
import os
import select
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from cheby_connector import local_mcp, phonebridge_runtime


def test_frame_reader_accepts_line_and_content_length() -> None:
    first = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    second = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    second_bytes = json.dumps(second).encode()
    stream = io.BytesIO(
        json.dumps(first).encode()
        + b"\nContent-Length: "
        + str(len(second_bytes)).encode()
        + b"\r\n\r\n"
        + second_bytes
    )
    assert list(local_mcp.FrameReader(stream)) == [first, second]


def test_frame_reader_rejects_oversized_content_length() -> None:
    stream = io.BytesIO(f"Content-Length: {local_mcp.MAX_RPC_BYTES + 1}\r\n\r\n".encode())
    with pytest.raises(ValueError, match="size limit"):
        list(local_mcp.FrameReader(stream))


def test_memory_search_fetch_and_remember_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEBY_LOCAL_MCP_DATA_ROOT", str(tmp_path))
    path = tmp_path / "memory" / "mem0-export.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"default_scope": {"user_id": "owner"}, "memories": [
        {"id": "m1", "memory": "ChebyNode phone gate blocks payment actions", "categories": ["chebynode"], "metadata": {"source": "migration"}, "created_at": "2026-07-01T00:00:00Z", "updated_at": "2026-07-01T00:00:00Z"}
    ]}), encoding="utf-8")

    found = local_mcp._memory_call("search", {"query": "ChebyNode payment"})
    assert found["structuredContent"]["results"][0]["id"] == "m1"
    fetched = local_mcp._memory_call("fetch", {"id": "m1"})
    assert "blocks payment" in fetched["structuredContent"]["text"]

    remembered = local_mcp._memory_call("memory_remember", {"content": "Keep credentials in files.", "categories": ["security"]})
    assert remembered["structuredContent"]["ok"] is True
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["count"] == 2
    assert stat_mode(path) == 0o600


def test_skill_search_fetch_and_remember_ignore_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEBY_LOCAL_MCP_DATA_ROOT", str(tmp_path))
    root = tmp_path / "skill" / "skills"
    source = root / "phone-gate"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: Phone gate\ntags: phone, safety\n---\n\nAlways verify the real device.\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (root / "unsafe").symlink_to(outside)

    found = local_mcp._skill_call("search", {"query": "real device"})
    assert found["structuredContent"]["results"][0]["id"] == "phone-gate"
    created = local_mcp._skill_call("skill_remember", {"title": "Release Gate", "content": "Run build, lint, and device tests.", "tags": ["gate"]})
    created_id = created["structuredContent"]["skill"]["id"]
    created_path = root / created_id / "SKILL.md"
    assert created_path.is_file()
    assert stat_mode(created_path) == 0o600
    assert "status: proposed" in created_path.read_text(encoding="utf-8")
    after = local_mcp._skill_call("search", {"query": "Run build lint"})
    assert all(item["id"] != created_id for item in after["structuredContent"]["results"])


def test_phone_endpoint_and_secret_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEBY_PHONEBRIDGE_URL", "http://example.com:3437")
    with pytest.raises(ValueError, match="private PhoneBridge"):
        local_mcp._phone_endpoint()

    token = tmp_path / "token"
    token.write_text("a" * 32, encoding="utf-8")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="group/world"):
        local_mcp._read_secret_file(token)
    token.chmod(0o400)
    assert local_mcp._read_secret_file(token) == "a" * 32


def test_phone_url_tool_dispatches_without_category_filter() -> None:
    names = {tool["name"] for tool in local_mcp._phone_tools()}
    assert "android_open_url" in names
    assert local_mcp.PHONE_TOOL_MAP["android_open_url"] == "open_url"
    with patch.object(local_mcp, "_phone_request", return_value={"ok": True, "result": {"ok": True}}) as request:
        local_mcp._phone_call("android_open_url", {"url": "https://example.com"})
        request.assert_called_once_with("open_url", {"url": "https://example.com"})

    for url in ("https://example.com", "tel:+10000000000", "bankapp://account"):
        tool, body = phonebridge_runtime._validate_broker_request({
            "tool": "open_url",
            "arguments": {"url": url},
        })
        assert tool == "open_url"
        assert body["arguments"] == {"url": url}


@pytest.mark.parametrize("url", [None, 12, "", "x" * 8193])
def test_phone_url_tool_rejects_malformed_arguments(url) -> None:
    with pytest.raises(ValueError, match="URL is invalid"):
        phonebridge_runtime._validate_broker_request({"tool": "open_url", "arguments": {"url": url}})


def test_screenshot_tool_guidance_preserves_native_images() -> None:
    tool = next(t for t in local_mcp._phone_tools() if t['name'] == 'android_capture_screenshot')
    assert 'image(block)' in tool['description']
    assert 'Never JSON-stringify' in tool['description']
    assert 'structuredContent separately' in tool['description']


def test_phone_screenshot_is_bounded_private_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHEBY_PHONEBRIDGE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    value = {"ok": True, "result": {"mime_type": "image/jpeg", "width": 1, "height": 1, "screen_width": 2, "screen_height": 3, "data_base64": "YWJj"}}
    with patch.object(local_mcp, "_phone_request", return_value=value):
        result = local_mcp._phone_call("android_capture_screenshot", {})
    artifact = Path(result["structuredContent"]["result"]["artifact_path"])
    assert artifact.read_bytes() == b"abc"
    assert stat_mode(artifact) == 0o600
    assert result["content"][1]["type"] == "image"
    assert "Image coordinates are 1x1" in result["content"][0]["text"]
    assert "physical screen is 2x3" in result["content"][0]["text"]


def test_phone_screenshot_coordinates_map_to_physical_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_mcp, "_latest_screenshot_transform", None)
    local_mcp._remember_screenshot_transform({
        "width": 600,
        "height": 1280,
        "screen_width": 1260,
        "screen_height": 2720,
    })
    response = {"ok": True, "result": {"ok": True}}
    with patch.object(local_mcp, "_phone_request", return_value=response) as request:
        local_mcp._phone_call("android_tap", {
            "x": 500,
            "y": 1100,
            "coordinate_space": "screenshot",
        })
    request.assert_called_once_with("tap", {"x": 1050.0, "y": 2337.5})


def test_phone_screen_coordinates_are_not_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_mcp, "_latest_screenshot_transform", None)
    response = {"ok": True, "result": {"ok": True}}
    with patch.object(local_mcp, "_phone_request", return_value=response) as request:
        local_mcp._phone_call("android_swipe", {
            "start_x": 100,
            "start_y": 200,
            "end_x": 300,
            "end_y": 400,
            "duration_ms": 350,
            "coordinate_space": "screen",
        })
    request.assert_called_once_with("swipe", {
        "start_x": 100,
        "start_y": 200,
        "end_x": 300,
        "end_y": 400,
        "duration_ms": 350,
    })


def test_phone_coordinate_space_is_required() -> None:
    tools = {tool["name"]: tool for tool in local_mcp._phone_tools()}
    assert "coordinate_space" in tools["android_tap"]["inputSchema"]["required"]
    with pytest.raises(ValueError, match="coordinate_space"):
        local_mcp._phone_call("android_tap", {"x": 1, "y": 2})


@pytest.mark.parametrize("space", ["screen", "screenshot"])
@pytest.mark.parametrize("bad", [None, True, "12", float("nan"), float("inf"), float("-inf")])
def test_invalid_coordinates_never_reach_phone(space: str, bad: object) -> None:
    with patch.object(local_mcp, "_phone_request") as request:
        with pytest.raises(ValueError, match="x must be a finite number"):
            local_mcp._phone_call("android_tap", {"x": bad, "y": 2, "coordinate_space": space})
    request.assert_not_called()


def test_invented_swipe_coordinate_names_fail_before_dispatch() -> None:
    with patch.object(local_mcp, "_phone_request") as request:
        with pytest.raises(ValueError, match="start_x, start_y, end_x, end_y"):
            local_mcp._phone_call("android_swipe", {
                "x1": 100, "y1": 800, "x2": 100, "y2": 200, "coordinate_space": "screen",
            })
    request.assert_not_called()


@pytest.mark.parametrize("value", [
    {"ok": False, "error": "unavailable"},
    {"ok": True, "result": {"ok": False, "error": "gesture rejected"}},
    {"ok": True, "result": {"allowed": False, "status": "blocked", "policy_reason": "fund policy"}},
    {"ok": True, "result": {"allowed": False}},
    {"ok": True, "result": {"status": "blocked"}},
    {"ok": True, "allowed": False},
])
def test_failed_phone_action_is_mcp_error(value: dict) -> None:
    with patch.object(local_mcp, "_phone_request", return_value=value):
        result = local_mcp._phone_call("android_press_back", {})
    assert result["isError"] is True
    assert result["structuredContent"] == value
    assert "Do not work around a policy refusal" in result["content"][-1]["text"]


def test_successful_phone_action_does_not_report_policy_failure() -> None:
    value = {"ok": True, "result": {"ok": True, "allowed": True}}
    with patch.object(local_mcp, "_phone_request", return_value=value):
        result = local_mcp._phone_call("android_press_back", {})
    assert not result.get("isError", False)
    assert result["structuredContent"] == value


def test_checkout_confirmation_is_not_reported_as_an_executed_action() -> None:
    value = {"ok": True, "result": {
        "allowed": False, "status": "awaiting_owner_confirmation", "confirmation_required": True,
    }}
    with patch.object(local_mcp, "_phone_request", return_value=value):
        result = local_mcp._phone_call("android_tap", {"x": 100, "y": 100, "coordinate_space": "screen"})
    assert result["isError"] is True
    assert result["structuredContent"] == value
    assert "NOT executed" in result["content"][-1]["text"]
    assert "Wait for the owner" in result["content"][-1]["text"]
    assert "Do not operate the authorization" in result["content"][-1]["text"]


def test_phone_http_error_preserves_bounded_actionable_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "token"
    token.write_text("a" * 32, encoding="utf-8")
    token.chmod(0o400)
    monkeypatch.setenv("CHEBY_PHONEBRIDGE_TOKEN_FILE", str(token))
    body = json.dumps({
        "ok": False,
        "error": "Android 10 permission prompt is open.\nApprove it and retry." + "x" * 400,
    }).encode()
    response = urllib.error.HTTPError(
        "http://127.0.0.1:3437/command",
        500,
        "failed",
        {},
        io.BytesIO(body),
    )
    with patch("urllib.request.urlopen", side_effect=response):
        with pytest.raises(RuntimeError) as raised:
            local_mcp._phone_request("screenshot", {})
    message = str(raised.value)
    assert message.startswith("PhoneBridge returned HTTP 500: Android 10 permission prompt is open. Approve it")
    assert "\n" not in message
    assert len(message) <= 300


def test_stdio_server_lists_mode_specific_tools() -> None:
    request = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode() + b"\n"
    output = io.BytesIO()
    local_mcp.serve("phonebridge", io.BytesIO(request), output)
    response = json.loads(output.getvalue())
    names = {item["name"] for item in response["result"]["tools"]}
    assert {"android_phone_status", "android_tap", "android_capture_screenshot"}.issubset(names)


def test_real_stdio_subprocess_responds_while_stdin_stays_open() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "cheby_connector.local_mcp", "memory"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        request = {"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 2.0)
        assert ready, "MCP did not respond while its stdin remained open"
        response = json.loads(process.stdout.readline())
        assert response["id"] == 9
        assert response["result"]["serverInfo"]["name"] == "chebycodex-offline-memory"
    finally:
        process.terminate()
        process.wait(timeout=2)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
