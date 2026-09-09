#!/usr/bin/env python3
"""Prove phone Codex sends image bytes through the GLM Chat adapter.

This probe is deliberately local-only. It runs inside the appliance Debian guest,
starts an IPv4-loopback fake GLM Chat Completions endpoint, invokes the installed
provider launcher and adapter with a fixture credential, and reports only
request-shape evidence.
It does not establish real-provider or semantic image-understanding acceptance.
"""
import base64
import binascii
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import sys
import threading
import time
import urllib.request
import zlib


def png_chunk(name, data):
    return struct.pack(">I", len(data)) + name + data + struct.pack(">I", binascii.crc32(name + data) & 0xFFFFFFFF)


def make_png():
    width = height = 64
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((255, 64, 32) if (x // 8 + y // 8) % 2 else (20, 120, 255))
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n" +
            png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) +
            png_chunk(b"IEND", b""))


PNG = make_png()
STATE = {"requests": [], "error": None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 8 * 1024 * 1024:
                raise ValueError("invalid request length")
            body = self.rfile.read(length)
            request = {
                "path": self.path,
                "authorization": self.headers.get("authorization", ""),
                "payload": json.loads(body),
            }
            STATE["requests"].append(request)
            request_index = len(STATE["requests"])
            if request_index == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_phone_status_probe", "type": "function",
                    "function": {"name": "mcp__phonebridge__android_phone_status", "arguments": "{}"},
                }]}
                finish = "tool_calls"
            elif request_index == 2:
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_phone_screenshot_probe", "type": "function",
                    "function": {"name": "mcp__phonebridge__android_capture_screenshot", "arguments": "{}"},
                }]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "DEVICE_IMAGE_TRANSPORT_OK"}
                finish = "stop"
            data = json.dumps({
                "id": "chatcmpl_phone_image_probe", "object": "chat.completion",
                "created": int(time.time()), "model": "glm-5.3-flash",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as error:
            STATE["error"] = type(error).__name__
            self.send_response(400)
            self.send_header("content-length", "0")
            self.end_headers()


def image_urls(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("image_url", "url") and isinstance(item, str) and item.startswith("data:image/"):
                found.append(item)
            found.extend(image_urls(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(image_urls(item))
    return found


def item_types(value):
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("type"), str):
            found.append(value["type"])
        for item in value.values():
            found.extend(item_types(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(item_types(item))
    return found


def tool_names(payload):
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return []
    return sorted(item["function"]["name"] for item in tools
                  if isinstance(item, dict) and isinstance(item.get("function"), dict)
                  and isinstance(item["function"].get("name"), str))


def selected_tool_shapes(payload):
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return []
    return sorted(item["function"]["name"] for item in tools
                  if isinstance(item, dict) and isinstance(item.get("function"), dict)
                  and item["function"].get("name") in (
                      "exec_command", "mcp__phonebridge__android_phone_status",
                      "mcp__phonebridge__android_capture_screenshot",
                  ))


def tool_message_present(payload):
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return isinstance(messages, list) and any(
        isinstance(item, dict) and item.get("role") == "tool" for item in messages
    )


def string_values(value):
    found = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(string_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(string_values(item))
    return found


def function_outputs(value):
    found = []
    if isinstance(value, dict):
        if value.get("type") == "function_call_output" and "output" in value:
            found.append(value["output"])
        for item in value.values():
            found.extend(function_outputs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(function_outputs(item))
    return found


def object_keys(value):
    found = set()
    if isinstance(value, dict):
        found.update(str(key) for key in value)
        for item in value.values():
            found.update(object_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(object_keys(item))
    elif isinstance(value, str) and len(value) <= 1024 * 1024:
        try:
            found.update(object_keys(json.loads(value)))
        except (json.JSONDecodeError, TypeError):
            pass
    return found


def main():
    probe_started = time.monotonic()
    report_only = False
    report_url = None
    args = sys.argv[1:]
    while args:
        argument = args.pop(0)
        if argument == "--report-only":
            report_only = True
        elif argument == "--report-url" and args:
            report_url = args.pop(0)
            if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}/?", report_url):
                raise SystemExit("--report-url must use loopback HTTP")
        else:
            raise SystemExit(
                "usage: phone_codex_image_transport_probe.py "
                "[--report-only] [--report-url http://127.0.0.1:PORT/]"
            )
    if os.geteuid() != 0 or Path("/opt/cheby/appserver/provider-launcher.py").is_file() is False:
        raise SystemExit("Run this probe inside the provisioned appliance Debian guest")
    root = Path("/root/.cheby/turn-inputs")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    image = root / "transport-probe.png"
    image.write_bytes(PNG)
    os.chmod(image, 0o600)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    settings = {
        "provider": "glm",
        "apiKey": "fixture-phone-" + "transport-key",
        "baseUrl": f"http://127.0.0.1:{port}",
        "reasoningEffort": "max",
    }
    env = os.environ.copy()
    env["CHEBY_PROVIDER_SETTINGS"] = json.dumps(settings)
    bridge = subprocess.run(["/opt/cheby/bin/start-phonebridge"], capture_output=True,
                            text=True, timeout=15)
    phonebridge_online = False
    if bridge.returncode == 0:
        # PhoneBridgeClient backs off for at most 60 seconds after repeated
        # connection failures. Keep the guest-side bridge alive long enough
        # to cover one full retry window on an already-running appliance.
        for _ in range(280):
            try:
                with urllib.request.urlopen("http://127.0.0.1:3437/health", timeout=1) as response:
                    health = json.loads(response.read())
                phonebridge_online = health.get("phone_online") is True
                if phonebridge_online:
                    break
            except Exception:
                pass
            time.sleep(0.25)
    command = [
        "/usr/bin/python3", "-I", "/opt/cheby/appserver/provider-launcher.py",
        "exec", "--skip-git-repo-check", "--ephemeral", "--json",
        "Reply with the transport probe marker and do not call tools.",
        "--image", str(image),
    ]
    codex_timed_out = False
    try:
        try:
            result = subprocess.run(command, env=env, cwd="/root", capture_output=True,
                                    text=True, timeout=45)
        except subprocess.TimeoutExpired as error:
            codex_timed_out = True
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(command, 124, stdout, stderr)
    finally:
        server.shutdown()
        server.server_close()
        image.unlink(missing_ok=True)

    requests = STATE["requests"]
    request = requests[0] if requests else {}
    payload = request.get("payload") or {}
    status_payload = requests[1].get("payload", {}) if len(requests) > 1 else {}
    screenshot_payload = requests[2].get("payload", {}) if len(requests) > 2 else {}
    urls = image_urls(payload)
    screenshot_urls = image_urls(screenshot_payload)
    expected = "data:image/png;base64," + base64.b64encode(PNG).decode()
    status_strings = string_values(status_payload)
    screenshot_strings = string_values(screenshot_payload)
    screenshot_outputs = function_outputs(screenshot_payload)
    screenshot_output_text = json.dumps(screenshot_outputs, ensure_ascii=False)
    artifact_root = Path("/root/.cheby/phonebridge/artifacts").resolve()
    artifact_candidates = sorted(set(re.findall(
        r"/root/\.cheby/phonebridge/artifacts/[A-Za-z0-9._-]+",
        "\n".join(screenshot_strings),
    )))
    artifact_path = None
    artifact_sha256 = None
    artifact_size = 0
    artifact_magic_ok = False
    artifact_data = b""
    for candidate in artifact_candidates:
        try:
            path = Path(candidate).resolve(strict=True)
            if not path.is_relative_to(artifact_root) or not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if not data or len(data) > 8 * 1024 * 1024:
                continue
            artifact_path = path
            artifact_data = data
            artifact_size = len(data)
            artifact_sha256 = hashlib.sha256(data).hexdigest()
            artifact_magic_ok = data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
            break
        except (OSError, RuntimeError):
            continue
    report = {
        "probe": "phone_codex_image_transport",
        "duration_ms": round((time.monotonic() - probe_started) * 1000),
        "local_only": True,
        "codex_exit_code": result.returncode,
        "codex_timed_out": codex_timed_out,
        "request_count": len(requests),
        "request_path": request.get("path"),
        "authorization_present": bool(requests) and all(
            item.get("authorization") == "Bearer fixture-phone-transport-key" for item in requests
        ),
        "model": payload.get("model"),
        "stream": payload.get("stream"),
        "image_data_url_count": len(urls),
        "request_item_types": sorted(set(item_types(payload))),
        "tool_names": tool_names(payload),
        "selected_tool_shapes": selected_tool_shapes(payload),
        "status_function_output_present": tool_message_present(status_payload),
        "screenshot_function_output_present": tool_message_present(screenshot_payload),
        "phonebridge_online_before_codex": phonebridge_online,
        "phonebridge_result_present": any('"name":"phonebridge"' in value or
                                          '"name": "phonebridge"' in value
                                          for value in status_strings),
        "live_phone_status_present": any('"phone_online":true' in value or
                                         '"phone_online": true' in value
                                         for value in status_strings),
        "live_screenshot_artifact_present": any('"artifact_path":' in value or
                                                  '"artifact_path": ' in value
                                                  for value in screenshot_strings),
        "live_screenshot_digest_present": any('"sha256":' in value or
                                                '"sha256": ' in value
                                                for value in screenshot_strings),
        "live_screenshot_file_present": artifact_path is not None,
        "live_screenshot_file_name": artifact_path.name if artifact_path else None,
        "live_screenshot_file_bytes": artifact_size,
        "live_screenshot_file_sha256": artifact_sha256,
        "live_screenshot_file_magic_ok": artifact_magic_ok,
        "live_screenshot_native_image_present": bool(artifact_data) and any(
            url.partition(",")[2] == base64.b64encode(artifact_data).decode()
            for url in screenshot_urls if url.startswith("data:image/")
        ),
        "screenshot_output_keys": sorted(object_keys(screenshot_outputs)),
        "screenshot_output_error": any(term in screenshot_output_text.lower() for term in (
            '"iserror": true', '"error":', "unavailable", "timed out", "permission",
            "without image data", "failed",
        )),
        "screenshot_error_class": next((term for term in (
            "accessibility service is unavailable", "timed out", "permission",
            "without image data", "screenshot failed", "phonebridge request failed",
        ) if term in screenshot_output_text.lower()), "unclassified"),
        "exact_image_bytes_present": expected in urls,
        "mock_marker_observed": "DEVICE_IMAGE_TRANSPORT_OK" in result.stdout,
        "server_error": STATE["error"],
    }
    report_json = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(report_json)
    if report_url:
        port = int(report_url.rsplit(":", 1)[1].rstrip("/"))
        body = (report_json + "\n").encode()
        request = (
            b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request)
    if not all((
        result.returncode == 0,
        request.get("path") == "/chat/completions",
        report["authorization_present"],
        payload.get("model") == "glm-5.3-flash",
        payload.get("stream") is False,
        report["exact_image_bytes_present"],
        report["mock_marker_observed"],
        len(requests) == 3,
        report["status_function_output_present"],
        report["screenshot_function_output_present"],
        report["phonebridge_online_before_codex"],
        report["phonebridge_result_present"],
        report["live_phone_status_present"],
        report["live_screenshot_artifact_present"],
        report["live_screenshot_file_present"],
        report["live_screenshot_file_magic_ok"],
        report["live_screenshot_native_image_present"],
        STATE["error"] is None,
    )):
        if result.stderr:
            print(result.stderr[-4000:], file=sys.stderr)
        if not report_only:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
