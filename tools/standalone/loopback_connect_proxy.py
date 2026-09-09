#!/usr/bin/env python3
"""Loopback-only HTTP CONNECT proxy for phone model traffic over ADB reverse."""

from __future__ import annotations

import argparse
import select
import socket
import socketserver


MAX_HEADER_BYTES = 16 * 1024
BUFFER_BYTES = 64 * 1024
ALLOWED_PORT = 443


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(15)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = client.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > MAX_HEADER_BYTES:
                self._reply(b"431 Request Header Fields Too Large")
                return

        request_head, _, buffered = bytes(header).partition(b"\r\n\r\n")
        try:
            request_line = request_head.split(b"\r\n", 1)[0].decode("ascii")
            method, authority, version = request_line.split(" ", 2)
            host, port_text = authority.rsplit(":", 1)
            port = int(port_text)
            if method != "CONNECT" or not version.startswith("HTTP/1."):
                raise ValueError
            if not host or port != ALLOWED_PORT:
                self._reply(b"403 Forbidden")
                return
        except (UnicodeDecodeError, ValueError):
            self._reply(b"400 Bad Request")
            return

        try:
            upstream = socket.create_connection((host.strip("[]"), port), timeout=15)
        except OSError:
            self._reply(b"502 Bad Gateway")
            return

        with upstream:
            upstream.settimeout(None)
            client.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if buffered:
                upstream.sendall(buffered)
            sockets = (client, upstream)
            while True:
                readable, _, _ = select.select(sockets, (), (), 300)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(BUFFER_BYTES)
                    if not data:
                        return
                    target = upstream if source is client else client
                    target.sendall(data)

    def _reply(self, status: bytes) -> None:
        self.request.sendall(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n\r\n")


class LoopbackProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17890)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    with LoopbackProxy(("127.0.0.1", args.port), ConnectHandler) as proxy:
        proxy.serve_forever()


if __name__ == "__main__":
    main()
