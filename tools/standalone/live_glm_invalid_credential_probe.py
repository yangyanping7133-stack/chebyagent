#!/usr/bin/env python3
"""Verify the bundled GLM adapter's live invalid-credential error boundary."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
from http.server import ThreadingHTTPServer
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = ROOT / "Android/appliance/runtime/glm-chat-adapter.py"


def load_adapter():
    spec = importlib.util.spec_from_file_location("live_glm_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--upstream", default="https://api.z.ai/api/paas/v4")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Choose a new output path")

    adapter = load_adapter()
    local_token = secrets.token_urlsafe(24)
    invalid_marker = "cheby-invalid-" + secrets.token_hex(20)
    server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.Handler)
    server.local_token = local_token
    server.upstream_key = invalid_marker
    server.upstream_base = args.upstream.rstrip("/")
    server.opener = urllib.request.build_opener(adapter.NoRedirect())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    payload = {
        "model": "glm-5.3-flash",
        "instructions": "Answer briefly.",
        "reasoning": {"effort": "low"},
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "2+2"}],
        }],
        "tools": [],
        "tool_choice": "auto",
    }
    try:
        started = time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
        body = json.dumps(payload, separators=(",", ":")).encode()
        connection.request("POST", "/responses", body=body, headers={
            "Authorization": "Bearer " + local_token,
            "Content-Type": "application/json",
        })
        response = connection.getresponse()
        response_body = response.read().decode("utf-8")
        duration_ms = round((time.monotonic() - started) * 1000)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    parsed = json.loads(response_body)
    message = parsed.get("error", {}).get("message")
    if response.status not in (401, 403) or message not in (
            "GLM provider returned HTTP 401", "GLM provider returned HTTP 403"):
        raise SystemExit("Unexpected invalid-credential result")
    if invalid_marker in response_body or local_token in response_body:
        raise SystemExit("Credential marker leaked into adapter response")
    report = {
        "outcome": "PASS",
        "upstream_origin": args.upstream,
        "http_status": response.status,
        "client_message": message,
        "credential_echo": False,
        "duration_ms": duration_ms,
        "adapter_sha256": hashlib.sha256(ADAPTER_PATH.read_bytes()).hexdigest(),
        "boundary": "Uses an intentionally invalid disposable marker; no user credential is read or changed.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
