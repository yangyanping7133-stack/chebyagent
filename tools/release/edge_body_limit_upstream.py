#!/usr/bin/env python3
"""Minimal private upstream used only by the Turkey Edge body-limit gate."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re


PROBE_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    probe_token = ""

    def send_probe_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("X-Cheby-Probe-Upstream-Token", self.probe_token)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/ready":
            self.send_error(404)
            return
        self.send_probe_status(204)

    def do_PUT(self) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdecimal():
            self.send_error(400)
            return
        remaining = int(raw_length)
        while remaining:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                self.send_error(400)
                return
            remaining -= len(chunk)
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


def server_address() -> tuple[str, int]:
    bind = os.environ.get("CHEBY_PROBE_BIND", "0.0.0.0")
    raw_port = os.environ.get("CHEBY_PROBE_PORT", "8080")
    if bind not in {"0.0.0.0", "127.0.0.1"}:
        raise RuntimeError("probe bind address is not reviewed")
    if not raw_port.isdecimal() or str(int(raw_port)) != raw_port:
        raise RuntimeError("probe port is not a canonical integer")
    port = int(raw_port)
    if not 1024 <= port <= 65535:
        raise RuntimeError("probe port is outside the reviewed unprivileged range")
    return bind, port


def probe_token() -> str:
    token = os.environ.get("CHEBY_PROBE_TOKEN", "")
    if PROBE_TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("probe identity token must be exactly 256 bits of lowercase hex")
    return token


if __name__ == "__main__":
    Handler.probe_token = probe_token()
    ThreadingHTTPServer(server_address(), Handler).serve_forever()
