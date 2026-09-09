#!/usr/bin/env python3
"""Credential-safe black-box gate for the fixed-IP Relay Edge.

The command deliberately accepts credentials only through a mode-0600 pairing
bootstrap file. It never serializes the generated device private key and never
includes request/response bodies, headers, credentials, or exception messages
in its output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import stat
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.x509.oid import ExtensionOID


ALLOWED_EDGE_PORTS = frozenset({27461, 27462})
GATE_PORT = 27462
MAX_FRAME_BYTES = 12 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 64 * 1024
MAX_BOOTSTRAP_BYTES = 4096
MAX_CA_BYTES = 64 * 1024
MAX_SIGNAL_BYTES = 64
MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_RELOAD_WAIT_SECONDS = 300.0
MAX_RELOAD_WAIT_SECONDS = 900.0
FULL_GATE_NETWORK_TIMEOUT_SECONDS = 60.0
RELOAD_HEARTBEAT_SECONDS = 15.0
REVOCATION_WAIT_SECONDS = 60.0
SIGNAL_POLL_SECONDS = 0.25
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORGED_FORWARDING_HEADERS = (
    ("Forwarded", "for=192.0.2.1"),
    ("X-Forwarded-For", "192.0.2.1"),
    ("X-Forwarded-Host", "example.invalid"),
    ("X-Forwarded-Port", "443"),
    ("X-Forwarded-Proto", "https"),
    ("X-Real-IP", "192.0.2.1"),
    ("CF-Connecting-IP", "192.0.2.1"),
    ("X-Relay-Client-IP", "192.0.2.1"),
)
P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ASSISTANT_PREFIX = "asst_"
DEVICE_PREFIX = "dev_"
TOKEN_MIN_BYTES = 32
GATE_DEVICE_NAME = "Cheby Edge black-box gate"
PRODUCTION_CONTINUITY_DEVICE_NAME = (
    "Cheby Edge production reload continuity"
)
CONTINUITY_MANIFEST_SCHEMA = "chebycodex.edge-reload-continuity.v2"


class GateError(RuntimeError):
    """A deliberately sanitized failure safe to print."""


class HttpRejected(GateError):
    def __init__(self, status: int) -> None:
        super().__init__("WebSocket handshake was rejected")
        self.status = status


class WebSocketClosed(GateError):
    def __init__(self, code: int) -> None:
        super().__init__("WebSocket was closed")
        self.code = code


class WebSocketSendInterrupted(GateError):
    """The peer closed while a client frame was still being transmitted."""


@dataclass(frozen=True)
class Target:
    ip: ipaddress.IPv4Address
    port: int
    timeout_seconds: float

    @property
    def authority(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True)
class PairingBootstrap:
    assistant_id: str
    pairing_secret: str = field(repr=False)


@dataclass
class EnrolledDevice:
    assistant_id: str
    device_id: str
    access_token: str = field(repr=False)
    key: ec.EllipticCurvePrivateKey = field(repr=False)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes = field(repr=False)

    def header_values(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]


@dataclass(frozen=True)
class CheckResult:
    name: str
    state: str
    detail: str = ""


class Recorder:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []
        self.evidence: dict[str, object] = {}

    def check(self, name: str, operation: Callable[[], None]) -> bool:
        try:
            operation()
        except GateError as error:
            self.results.append(CheckResult(name, "FAIL", str(error)))
            return False
        except Exception as error:  # Never render exception text; it may carry a request.
            self.results.append(
                CheckResult(name, "FAIL", f"internal error ({type(error).__name__})")
            )
            return False
        self.results.append(CheckResult(name, "PASS"))
        return True

    def fail(self, name: str, detail: str) -> None:
        self.results.append(CheckResult(name, "FAIL", detail))

    @property
    def failed(self) -> bool:
        return any(result.state == "FAIL" for result in self.results)


def parse_target(ip_value: str, port: int, timeout_seconds: float) -> Target:
    try:
        public_ip = ipaddress.ip_address(ip_value)
    except ValueError as error:
        raise GateError("target must be an explicit IPv4 address") from error
    if not isinstance(public_ip, ipaddress.IPv4Address):
        raise GateError("target must be an explicit IPv4 address")
    if port not in ALLOWED_EDGE_PORTS:
        raise GateError("port must be explicitly set to 27461 or 27462")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise GateError("timeout must be greater than zero and at most 60 seconds")
    return Target(public_ip, port, timeout_seconds)


def require_full_gate_timeout(
    bootstrap: PairingBootstrap | None,
    timeout_seconds: float,
) -> None:
    if (
        bootstrap is not None
        and timeout_seconds != FULL_GATE_NETWORK_TIMEOUT_SECONDS
    ):
        raise GateError("full Gate requires an explicit --timeout 60")


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate member")
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise GateError("JSON document is malformed") from error
    if not isinstance(value, dict):
        raise GateError("JSON document must be an object")
    return value


def _is_public_id(value: Any, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value[len(prefix) :]
    return len(suffix) == 22 and all(
        character.isalnum() or character in "_-" for character in suffix
    )


def parse_pairing_envelope(raw: bytes) -> PairingBootstrap:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw or not raw.startswith(b"CXC1."):
        raise GateError("pairing bootstrap format is invalid")
    encoded = raw[5:]
    if not encoded or b"=" in encoded:
        raise GateError("pairing bootstrap encoding is invalid")
    if any(
        byte
        not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for byte in encoded
    ):
        raise GateError("pairing bootstrap encoding is invalid")
    padding = b"=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except ValueError as error:
        raise GateError("pairing bootstrap encoding is invalid") from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=")
    if canonical != encoded:
        raise GateError("pairing bootstrap encoding is not canonical")
    document = _strict_json_object(decoded)
    if set(document) != {"v", "assistantId", "pairingSecret"} or document["v"] != 1:
        raise GateError("pairing bootstrap fields are invalid")
    if not _is_public_id(document["assistantId"], ASSISTANT_PREFIX):
        raise GateError("pairing bootstrap assistant identity is invalid")
    secret = document["pairingSecret"]
    if (
        not isinstance(secret, str)
        or len(secret.encode("utf-8")) < 32
        or len(secret.encode("utf-8")) > 128
        or any(character.isspace() or ord(character) < 0x20 for character in secret)
    ):
        raise GateError("pairing bootstrap credential is invalid")
    return PairingBootstrap(document["assistantId"], secret)


def read_pairing_bootstrap(path_value: str) -> PairingBootstrap:
    path = Path(path_value)
    if not path.is_absolute():
        raise GateError("pairing bootstrap path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GateError("pairing bootstrap file could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GateError("pairing bootstrap must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise GateError("pairing bootstrap mode must be exactly 0600")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise GateError("pairing bootstrap must be owned by the current user")
        if metadata.st_size <= 0 or metadata.st_size > MAX_BOOTSTRAP_BYTES:
            raise GateError("pairing bootstrap size is invalid")
        chunks: list[bytes] = []
        remaining = MAX_BOOTSTRAP_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_BOOTSTRAP_BYTES:
            raise GateError("pairing bootstrap is too large")
        return parse_pairing_envelope(raw)
    finally:
        os.close(descriptor)


def parse_leaf_sha256(value: str) -> str:
    if not HEX_SHA256.fullmatch(value):
        raise GateError(
            "expected new leaf fingerprint must be 64 lowercase hex characters"
        )
    return value


def _private_signal_path(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise GateError("reload signal path must be absolute")
    try:
        parent_metadata = path.parent.lstat()
    except OSError as error:
        raise GateError("reload signal directory is unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or path.parent.is_symlink()
        or parent_metadata.st_mode & 0o077
    ):
        raise GateError("reload signal directory must be private")
    if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
        raise GateError("reload signal directory must be owned by the current user")
    return path


def ensure_private_signal_absent(path_value: str) -> Path:
    path = _private_signal_path(path_value)
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    except OSError as error:
        raise GateError("reload signal path could not be checked safely") from error
    raise GateError("reload signal must not exist before the continuity gate")


def write_private_signal(path_value: str, content: bytes) -> None:
    if not content or len(content) > MAX_SIGNAL_BYTES:
        raise GateError("reload signal content is invalid")
    path = _private_signal_path(path_value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise GateError("reload ready signal could not be created safely") from error
    persisted = False
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short signal write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        persisted = True
    except OSError as error:
        raise GateError("reload ready signal could not be persisted") from error
    finally:
        os.close(descriptor)
        if not persisted:
            path.unlink(missing_ok=True)
    try:
        directory = os.open(
            path.parent,
            os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        path.unlink(missing_ok=True)
        raise GateError("reload ready signal could not be made durable") from error


def read_private_signal_if_present(path_value: str, expected: bytes) -> bool:
    path = _private_signal_path(path_value)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise GateError("reload completion signal could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GateError("reload completion signal must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise GateError("reload completion signal mode must be exactly 0600")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise GateError(
                "reload completion signal must be owned by the current user"
            )
        if metadata.st_size <= 0 or metadata.st_size > MAX_SIGNAL_BYTES:
            raise GateError("reload completion signal size is invalid")
        raw = os.read(descriptor, MAX_SIGNAL_BYTES + 1)
        if raw != expected:
            raise GateError("reload completion signal content is invalid")
        return True
    finally:
        os.close(descriptor)


def _private_manifest_path(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise GateError("manifest path must be absolute")
    try:
        parent_metadata = path.parent.lstat()
    except OSError as error:
        raise GateError("manifest directory is unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or path.parent.is_symlink()
        or parent_metadata.st_mode & 0o077
    ):
        raise GateError("manifest directory must be private")
    if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
        raise GateError("manifest directory must be owned by the current user")
    return path


def ensure_immutable_manifest_absent(path_value: str) -> Path:
    path = _private_manifest_path(path_value)
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    except OSError as error:
        raise GateError("manifest path could not be checked safely") from error
    raise GateError("manifest path already exists")


def write_immutable_manifest(
    path_value: str,
    document: dict[str, Any],
) -> None:
    path = ensure_immutable_manifest_absent(path_value)
    try:
        raw = (
            json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise GateError("manifest document is invalid") from error
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise GateError("manifest size is invalid")
    directory_descriptor = -1
    descriptor = -1
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    temporary_device = -1
    temporary_inode = -1
    published_created = False
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise GateError("manifest path already exists")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        temporary = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary.st_mode)
            or stat.S_IMODE(temporary.st_mode) != 0o600
            or temporary.st_nlink != 1
        ):
            raise GateError("manifest temporary file is invalid")
        temporary_device = temporary.st_dev
        temporary_inode = temporary.st_ino
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short manifest write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise GateError("manifest path already exists") from error
        published_created = True
        published = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_dev != temporary_device
            or published.st_ino != temporary_inode
            or published.st_nlink != 2
        ):
            raise GateError("manifest published file is invalid")
        os.fsync(directory_descriptor)
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_created = False
        os.fsync(directory_descriptor)
        published_created = False
    except GateError:
        raise
    except OSError as error:
        raise GateError("manifest could not be persisted safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            cleanup_changed_directory = False
            if published_created:
                published_descriptor = -1
                try:
                    published_descriptor = os.open(
                        path.name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_descriptor,
                    )
                    published = os.fstat(published_descriptor)
                    if (
                        stat.S_ISREG(published.st_mode)
                        and published.st_dev == temporary_device
                        and published.st_ino == temporary_inode
                    ):
                        os.unlink(path.name, dir_fd=directory_descriptor)
                        cleanup_changed_directory = True
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                finally:
                    if published_descriptor >= 0:
                        os.close(published_descriptor)
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                    cleanup_changed_directory = True
                except OSError:
                    pass
            if cleanup_changed_directory:
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)


def load_ca_certificate(path_value: str) -> tuple[x509.Certificate, str]:
    path = Path(path_value)
    if not path.is_absolute():
        raise GateError("CA certificate path must be absolute")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise GateError("CA certificate could not be read") from error
    if not raw or len(raw) > MAX_CA_BYTES:
        raise GateError("CA certificate size is invalid")
    try:
        if b"-----BEGIN CERTIFICATE-----" in raw:
            certificates = x509.load_pem_x509_certificates(raw)
        else:
            certificates = [x509.load_der_x509_certificate(raw)]
        if len(certificates) not in {1, 2}:
            raise ValueError("unexpected CA count")
        fingerprints: set[bytes] = set()
        for certificate in certificates:
            constraints = certificate.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            ).value
            if not constraints.ca:
                raise ValueError("certificate is not a CA")
            fingerprint = certificate.fingerprint(hashes.SHA256())
            if fingerprint in fingerprints:
                raise ValueError("duplicate CA")
            fingerprints.add(fingerprint)
    except (ValueError, x509.ExtensionNotFound) as error:
        raise GateError("CA certificate is invalid") from error
    pem = b"".join(
        certificate.public_bytes(serialization.Encoding.PEM)
        for certificate in certificates
    ).decode("ascii")
    return certificates[0], pem


def create_tls_context(ca_pem: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    try:
        context.load_verify_locations(cadata=ca_pem)
    except ssl.SSLError as error:
        raise GateError("CA certificate could not be installed") from error
    return context


def _open_tls(target: Target, context: ssl.SSLContext) -> ssl.SSLSocket:
    plain: socket.socket | None = None
    try:
        plain = socket.create_connection(
            (str(target.ip), target.port), timeout=target.timeout_seconds
        )
        plain.settimeout(target.timeout_seconds)
        return context.wrap_socket(plain, server_hostname=str(target.ip))
    except (OSError, ssl.SSLError) as error:
        if plain is not None:
            plain.close()
        raise GateError("TLS connection failed") from error


def verify_tls_identity(target: Target, context: ssl.SSLContext) -> None:
    with _open_tls(target, context) as connection:
        leaf_der = connection.getpeercert(binary_form=True)
        version = connection.version()
    if version not in {"TLSv1.2", "TLSv1.3"}:
        raise GateError("TLS protocol version is outside the approved set")
    try:
        leaf = x509.load_der_x509_certificate(leaf_der)
        san = leaf.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
    except (ValueError, x509.ExtensionNotFound) as error:
        raise GateError("served leaf certificate SAN is invalid") from error
    ip_sans = san.get_values_for_type(x509.IPAddress)
    dns_sans = san.get_values_for_type(x509.DNSName)
    if ip_sans != [target.ip] or dns_sans:
        raise GateError("served leaf certificate does not have the exact IP SAN")


def build_raw_request(
    method: str,
    raw_target: str,
    *,
    headers: Sequence[tuple[str, str]],
    body: bytes = b"",
) -> bytes:
    if (
        not method
        or not method.isascii()
        or not method.isupper()
        or any(character.isspace() for character in method)
    ):
        raise GateError("request method is invalid")
    if (
        not raw_target.startswith("/")
        or not raw_target.isascii()
        or "\r" in raw_target
        or "\n" in raw_target
        or " " in raw_target
    ):
        raise GateError("request target is invalid")
    lines = [f"{method} {raw_target} HTTP/1.1"]
    for name, value in headers:
        if (
            not name
            or not name.isascii()
            or any(character not in "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for character in name)
            or not value.isascii()
            or "\r" in value
            or "\n" in value
        ):
            raise GateError("request header is invalid")
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _read_response_head(
    connection: ssl.SSLSocket,
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    received = bytearray()
    delimiter = b"\r\n\r\n"
    while delimiter not in received:
        if len(received) > MAX_HTTP_RESPONSE_BYTES:
            raise GateError("HTTP response headers are too large")
        chunk = connection.recv(4096)
        if not chunk:
            raise GateError("HTTP response ended before its headers")
        received.extend(chunk)
    head, tail = bytes(received).split(delimiter, 1)
    lines = head.split(b"\r\n")
    try:
        status_line = lines[0].decode("ascii")
        version, status_text, _ = status_line.split(" ", 2)
        status_value = int(status_text)
    except (UnicodeDecodeError, ValueError, IndexError) as error:
        raise GateError("HTTP response status is malformed") from error
    if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status_value <= 599:
        raise GateError("HTTP response status is malformed")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if b":" not in line:
            raise GateError("HTTP response header is malformed")
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii").strip()
            value = raw_value.decode("latin-1").strip()
        except UnicodeDecodeError as error:
            raise GateError("HTTP response header is malformed") from error
        headers.append((name, value))
    return status_value, tuple(headers), tail


def _read_to_close(connection: ssl.SSLSocket, initial: bytes) -> bytes:
    body = bytearray(initial)
    while True:
        if len(body) > MAX_HTTP_RESPONSE_BYTES:
            raise GateError("HTTP response body is too large")
        try:
            chunk = connection.recv(4096)
        except socket.timeout as error:
            raise GateError("HTTP response did not close") from error
        if not chunk:
            break
        body.extend(chunk)
    if len(body) > MAX_HTTP_RESPONSE_BYTES:
        raise GateError("HTTP response body is too large")
    return bytes(body)


def https_request(
    target: Target,
    context: ssl.SSLContext,
    method: str,
    raw_target: str,
    *,
    headers: Sequence[tuple[str, str]] = (),
    body: bytes = b"",
    host_values: Sequence[str] | None = None,
) -> HttpResponse:
    hosts = list(host_values) if host_values is not None else [target.authority]
    request_headers: list[tuple[str, str]] = [("Host", host) for host in hosts]
    request_headers.extend(
        [
            ("Connection", "close"),
            ("User-Agent", "cheby-edge-gate/1"),
            ("Accept", "application/json"),
        ]
    )
    request_headers.extend(headers)
    request = build_raw_request(
        method, raw_target, headers=request_headers, body=body
    )
    with _open_tls(target, context) as connection:
        try:
            connection.sendall(request)
            status, response_headers, tail = _read_response_head(connection)
            response_body = _read_to_close(connection, tail)
        except (OSError, ssl.SSLError) as error:
            raise GateError("HTTPS request failed") from error
    return HttpResponse(status, response_headers, response_body)


def require_status(response: HttpResponse, expected: Iterable[int]) -> None:
    allowed = set(expected)
    if response.status not in allowed:
        raise GateError("unexpected HTTP status")


def check_health(target: Target, context: ssl.SSLContext) -> None:
    response = https_request(target, context, "GET", "/healthz")
    require_status(response, {200})
    document = _strict_json_object(response.body)
    if document != {"status": "ok"}:
        raise GateError("health response body is invalid")
    if "no-store" not in ",".join(response.header_values("cache-control")).lower():
        raise GateError("health response is missing no-store")


def check_forged_forwarding_headers(
    target: Target,
    context: ssl.SSLContext,
) -> None:
    for name, value in FORGED_FORWARDING_HEADERS:
        response = https_request(
            target,
            context,
            "GET",
            "/healthz",
            headers=[(name, value)],
        )
        require_status(response, {400})


def proof_canonical_request(
    *,
    method: str,
    raw_target: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    bearer_token: str,
) -> bytes:
    if not raw_target.startswith("/") or "\r" in raw_target or "\n" in raw_target:
        raise GateError("proof request target is invalid")
    return b"\n".join(
        (
            b"CHEBY-POP-1",
            method.upper().encode("ascii"),
            raw_target.encode("ascii"),
            timestamp.encode("ascii"),
            nonce.encode("ascii"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
            hashlib.sha256(bearer_token.encode("utf-8")).hexdigest().encode("ascii"),
        )
    )


def proof_headers(
    key: ec.EllipticCurvePrivateKey,
    *,
    method: str,
    raw_target: str,
    body: bytes = b"",
    bearer_token: str = "",
) -> list[tuple[str, str]]:
    timestamp = str(int(time.time()))
    nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    canonical = proof_canonical_request(
        method=method,
        raw_target=raw_target,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
        bearer_token=bearer_token,
    )
    signature = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(signature)
    if s_value > P256_ORDER // 2:
        s_value = P256_ORDER - s_value
    low_s_signature = encode_dss_signature(r_value, s_value)
    return [
        ("X-Cheby-Signature-Version", "1"),
        ("X-Cheby-Timestamp", timestamp),
        ("X-Cheby-Nonce", nonce),
        ("X-Cheby-Signature", base64.b64encode(low_s_signature).decode("ascii")),
    ]


def _canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=False
    ).encode("ascii")


def enroll_device(
    target: Target,
    context: ssl.SSLContext,
    bootstrap: PairingBootstrap,
    *,
    allow_production_ephemeral: bool = False,
    device_name: str = GATE_DEVICE_NAME,
) -> EnrolledDevice:
    if target.port == GATE_PORT:
        if allow_production_ephemeral or device_name != GATE_DEVICE_NAME:
            raise GateError("Gate pairing identity is invalid")
    elif (
        target.port != 27461
        or not allow_production_ephemeral
        or device_name != PRODUCTION_CONTINUITY_DEVICE_NAME
    ):
        raise GateError("pairing bootstrap is accepted only for Gate port 27462")
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    body = _canonical_json(
        {
            "assistantId": bootstrap.assistant_id,
            "pairingSecret": bootstrap.pairing_secret,
            "deviceName": device_name,
            "devicePublicKey": base64.b64encode(spki).decode("ascii"),
        }
    )
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        *proof_headers(
            key,
            method="POST",
            raw_target="/relay/v1/pairings/exchange",
            body=body,
        ),
    ]
    response = https_request(
        target,
        context,
        "POST",
        "/relay/v1/pairings/exchange",
        headers=headers,
        body=body,
    )
    if response.status != 200:
        raise GateError("Gate device enrollment was rejected")
    if "no-store" not in ",".join(response.header_values("cache-control")).lower():
        raise GateError("Gate device enrollment response is missing no-store")
    document = _strict_json_object(response.body)
    expected_fields = {
        "assistantId",
        "deviceId",
        "accessToken",
        "accessExpiresAt",
        "refreshToken",
        "refreshExpiresAt",
    }
    if set(document) != expected_fields:
        raise GateError("Gate device enrollment response fields are invalid")
    if document["assistantId"] != bootstrap.assistant_id:
        raise GateError("Gate device enrollment returned the wrong assistant")
    if not _is_public_id(document["deviceId"], DEVICE_PREFIX):
        raise GateError("Gate device enrollment returned an invalid device identity")
    access_token = document["accessToken"]
    refresh_token = document.pop("refreshToken", None)
    if (
        not isinstance(access_token, str)
        or len(access_token.encode("utf-8")) < TOKEN_MIN_BYTES
        or len(access_token.encode("utf-8")) > 256
        or any(
            character.isspace() or ord(character) < 0x20
            for character in access_token
        )
        or not isinstance(refresh_token, str)
        or len(refresh_token.encode("utf-8")) < TOKEN_MIN_BYTES
        or len(refresh_token.encode("utf-8")) > 256
        or any(
            character.isspace() or ord(character) < 0x20
            for character in refresh_token
        )
    ):
        raise GateError("Gate device enrollment returned invalid credentials")
    refresh_token = None
    return EnrolledDevice(
        assistant_id=bootstrap.assistant_id,
        device_id=document["deviceId"],
        access_token=access_token,
        key=key,
    )


class WebSocketConnection:
    def __init__(self, connection: ssl.SSLSocket, initial: bytes) -> None:
        self.connection = connection
        self.buffer = bytearray(initial)
        self.closed = False

    def _read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            try:
                chunk = self.connection.recv(max(4096, count - len(self.buffer)))
            except (OSError, ssl.SSLError) as error:
                raise GateError("WebSocket receive failed") from error
            if not chunk:
                raise GateError("WebSocket closed without a close frame")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def receive_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        if first & 0x70:
            raise GateError("WebSocket frame uses unsupported reserved bits")
        if not first & 0x80:
            raise GateError("fragmented server frames are not supported by the gate")
        if second & 0x80:
            raise GateError("server sent an invalid masked WebSocket frame")
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_HTTP_RESPONSE_BYTES:
            raise GateError("server WebSocket frame is unexpectedly large")
        payload = self._read_exact(length)
        if opcode == 0x8:
            try:
                self.send_frame(0x8, payload)
            except GateError:
                # The peer may close its transport immediately after its close
                # frame.  Best-effort acknowledgement still avoids leaving the
                # normal closing handshake incomplete when the socket is open.
                pass
            self.closed = True
            code = 1005 if len(payload) < 2 else struct.unpack("!H", payload[:2])[0]
            raise WebSocketClosed(code)
        if opcode == 0x9:
            self.send_frame(0xA, payload)
            return self.receive_frame()
        if opcode == 0xA:
            return self.receive_frame()
        return opcode, payload

    def receive_json(self) -> dict[str, Any]:
        opcode, payload = self.receive_frame()
        if opcode != 0x1:
            raise GateError("server did not send a text WebSocket frame")
        return _strict_json_object(payload)

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise GateError("WebSocket is already closed")
        length = len(payload)
        if length < 126:
            length_field = bytes([0x80 | length])
        elif length <= 0xFFFF:
            length_field = bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            length_field = bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        masked = bytearray(payload)
        for offset, mask_byte in enumerate(mask):
            table = bytes(value ^ mask_byte for value in range(256))
            masked[offset::4] = bytes(masked[offset::4]).translate(table)
        frame = bytes([0x80 | opcode]) + length_field + mask + masked
        try:
            self.connection.sendall(frame)
        except (OSError, ssl.SSLError) as error:
            raise WebSocketSendInterrupted("WebSocket send failed") from error

    def close(self) -> None:
        if not self.closed:
            try:
                self.send_frame(0x8, struct.pack("!H", 1000))
            except GateError:
                pass
        self.closed = True
        try:
            self.connection.close()
        except OSError:
            pass

    def __enter__(self) -> "WebSocketConnection":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_websocket(
    target: Target,
    context: ssl.SSLContext,
    raw_target: str,
    *,
    headers: Sequence[tuple[str, str]] = (),
) -> WebSocketConnection:
    websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request_headers = [
        ("Host", target.authority),
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Key", websocket_key),
        ("Sec-WebSocket-Version", "13"),
        ("User-Agent", "cheby-edge-gate/1"),
        *headers,
    ]
    request = build_raw_request("GET", raw_target, headers=request_headers)
    connection = _open_tls(target, context)
    try:
        connection.sendall(request)
        status, response_headers, tail = _read_response_head(connection)
        if status != 101:
            raise HttpRejected(status)
        values: dict[str, list[str]] = {}
        for name, value in response_headers:
            values.setdefault(name.lower(), []).append(value)
        expected_accept = base64.b64encode(
            hashlib.sha1(websocket_key.encode("ascii") + WEBSOCKET_GUID).digest()
        ).decode("ascii")
        if (
            values.get("upgrade", [""])[0].lower() != "websocket"
            or "upgrade" not in values.get("connection", [""])[0].lower()
            or values.get("sec-websocket-accept") != [expected_accept]
        ):
            raise GateError("WebSocket handshake response is invalid")
        return WebSocketConnection(connection, tail)
    except Exception:
        connection.close()
        raise


def authenticated_device_headers(device: EnrolledDevice) -> list[tuple[str, str]]:
    return [
        ("Authorization", "Bearer " + device.access_token),
        *proof_headers(
            device.key,
            method="GET",
            raw_target="/relay/v1/device",
            bearer_token=device.access_token,
        ),
    ]


def require_device_ready(
    connection: WebSocketConnection,
    device: EnrolledDevice,
) -> None:
    ready = connection.receive_json()
    if (
        ready.get("v") != 1
        or ready.get("type") != "ready"
        or ready.get("assistantId") != device.assistant_id
        or ready.get("principalId") != device.device_id
        or ready.get("role") != "device"
    ):
        raise GateError("authenticated WebSocket ready frame is invalid")


def websocket_ping_pong(connection: WebSocketConnection) -> None:
    nonce = secrets.token_urlsafe(18)
    connection.send_frame(
        0x1,
        _canonical_json({"v": 1, "type": "ping", "nonce": nonce}),
    )
    if connection.receive_json() != {"v": 1, "type": "pong", "nonce": nonce}:
        raise GateError("authenticated WebSocket pong frame is invalid")


def socket_leaf_sha256(connection: ssl.SSLSocket) -> str:
    try:
        leaf_der = connection.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as error:
        raise GateError("served leaf fingerprint could not be read") from error
    if not leaf_der:
        raise GateError("served leaf fingerprint is missing")
    return hashlib.sha256(leaf_der).hexdigest()


def served_leaf_sha256(target: Target, context: ssl.SSLContext) -> str:
    with _open_tls(target, context) as connection:
        return socket_leaf_sha256(connection)


def wait_for_reload_signal(
    path_value: str,
    connection: WebSocketConnection,
    timeout_seconds: float,
) -> None:
    if timeout_seconds < 1 or timeout_seconds > MAX_RELOAD_WAIT_SECONDS:
        raise GateError("reload wait timeout must be between 1 and 900 seconds")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if read_private_signal_if_present(path_value, b"RELOADED\n"):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("reload completion signal timed out")
        websocket_ping_pong(connection)
        time.sleep(min(RELOAD_HEARTBEAT_SECONDS, remaining))


def wait_for_revocation_signal(path_value: str) -> None:
    deadline = time.monotonic() + REVOCATION_WAIT_SECONDS
    while True:
        if read_private_signal_if_present(path_value, b"REVOKED\n"):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GateError("ephemeral identity revocation signal timed out")
        time.sleep(min(SIGNAL_POLL_SECONDS, remaining))


def require_existing_connection_revoked(
    connection: WebSocketConnection,
) -> None:
    nonce = secrets.token_urlsafe(18)
    connection.send_frame(
        0x1,
        _canonical_json({"v": 1, "type": "ping", "nonce": nonce}),
    )
    try:
        connection.receive_json()
    except WebSocketClosed as closed:
        if closed.code == 4401:
            return
        raise GateError(
            "revoked production identity used the wrong close code"
        ) from closed
    raise GateError("revoked production identity remained usable")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_reload_continuity_gate(
    target: Target,
    ca_pem: str,
    bootstrap: PairingBootstrap,
    *,
    expected_new_leaf_sha256: str,
    ready_signal: str,
    reloaded_signal: str,
    wait_timeout_seconds: float,
    manifest_path: str,
    cleanup_ready_signal: str | None = None,
    revoked_signal: str | None = None,
) -> Recorder:
    production = target.port == 27461
    if production and (
        cleanup_ready_signal is None or revoked_signal is None
    ):
        raise GateError(
            "production reload continuity requires cleanup-ready and "
            "revoked signals"
        )
    if not production and (
        cleanup_ready_signal is not None or revoked_signal is not None
    ):
        raise GateError(
            "ephemeral revocation signals are accepted only on port 27461"
        )
    artifact_paths = [
        ready_signal,
        reloaded_signal,
        manifest_path,
        *(
            [cleanup_ready_signal, revoked_signal]
            if production
            else []
        ),
    ]
    if len(
        {
            Path(path_value).resolve(strict=False)
            for path_value in artifact_paths
            if path_value is not None
        }
    ) != len(artifact_paths):
        raise GateError("reload continuity artifact paths must be distinct")
    expected_fingerprint = parse_leaf_sha256(expected_new_leaf_sha256)
    ensure_private_signal_absent(ready_signal)
    ensure_private_signal_absent(reloaded_signal)
    ensure_immutable_manifest_absent(manifest_path)
    if production:
        assert cleanup_ready_signal is not None
        assert revoked_signal is not None
        ensure_private_signal_absent(cleanup_ready_signal)
        ensure_private_signal_absent(revoked_signal)
    context = create_tls_context(ca_pem)
    recorder = Recorder()
    recorder.evidence.update(
        {
            "existingAuthenticatedWss": False,
            "preReloadPing": False,
            "postReloadPing": False,
            "newConnectionExpectedLeaf": False,
            "ephemeralIdentityRevoked": False if production else None,
            "initialLeafSha256": None,
            "newLeafSha256": None,
        }
    )

    def continuity() -> None:
        device = enroll_device(
            target,
            context,
            bootstrap,
            allow_production_ephemeral=production,
            device_name=(
                PRODUCTION_CONTINUITY_DEVICE_NAME
                if production
                else GATE_DEVICE_NAME
            ),
        )
        with open_websocket(
            target,
            context,
            "/relay/v1/device",
            headers=authenticated_device_headers(device),
        ) as connection:
            initial_fingerprint = socket_leaf_sha256(connection.connection)
            if secrets.compare_digest(initial_fingerprint, expected_fingerprint):
                raise GateError(
                    "expected new leaf was already served before the reload signal"
                )
            recorder.evidence["initialLeafSha256"] = initial_fingerprint
            require_device_ready(connection, device)
            recorder.evidence["existingAuthenticatedWss"] = True
            websocket_ping_pong(connection)
            recorder.evidence["preReloadPing"] = True
            write_private_signal(ready_signal, b"READY\n")
            wait_for_reload_signal(
                reloaded_signal,
                connection,
                wait_timeout_seconds,
            )
            websocket_ping_pong(connection)
            recorder.evidence["postReloadPing"] = True
            observed_fingerprint = served_leaf_sha256(target, context)
            recorder.evidence["newLeafSha256"] = observed_fingerprint
            if (
                not secrets.compare_digest(
                    observed_fingerprint, expected_fingerprint
                )
                or secrets.compare_digest(
                    observed_fingerprint, initial_fingerprint
                )
            ):
                raise GateError(
                    "new TLS connection did not receive the expected rotated leaf"
                )
            recorder.evidence["newConnectionExpectedLeaf"] = True
            if production:
                assert cleanup_ready_signal is not None
                assert revoked_signal is not None
                write_private_signal(cleanup_ready_signal, b"REVOKE\n")
                wait_for_revocation_signal(revoked_signal)
                require_existing_connection_revoked(connection)
                recorder.evidence["ephemeralIdentityRevoked"] = True

    recorder.check(
        "authenticated WSS survives reload and new TLS receives expected leaf",
        continuity,
    )
    write_immutable_manifest(
        manifest_path,
        {
            "schema": CONTINUITY_MANIFEST_SCHEMA,
            "createdAt": _utc_now(),
            "outcome": "FAIL" if recorder.failed else "PASS",
            "target": {
                "ip": str(target.ip),
                "port": target.port,
            },
            "expectedNewLeafSha256": expected_fingerprint,
            "identityScope": (
                "ephemeral-production"
                if production
                else "isolated-gate"
            ),
            "checks": [
                {"name": result.name, "state": result.state}
                for result in recorder.results
            ],
            "evidence": recorder.evidence,
        },
    )
    return recorder


def expect_websocket_http_rejection(
    target: Target,
    context: ssl.SSLContext,
    raw_target: str,
    expected_status: int,
    *,
    headers: Sequence[tuple[str, str]] = (),
) -> None:
    try:
        connection = open_websocket(
            target, context, raw_target, headers=headers
        )
    except HttpRejected as rejected:
        if rejected.status != expected_status:
            raise GateError("unexpected WebSocket rejection status")
        return
    connection.close()
    raise GateError("WebSocket endpoint unexpectedly upgraded")


def expect_websocket_close(
    target: Target,
    context: ssl.SSLContext,
    *,
    headers: Sequence[tuple[str, str]],
    expected_code: int,
) -> None:
    try:
        connection = open_websocket(
            target, context, "/relay/v1/device", headers=headers
        )
    except HttpRejected as rejected:
        raise GateError("WebSocket was rejected before Relay close semantics") from rejected
    with connection:
        try:
            connection.receive_frame()
        except WebSocketClosed as closed:
            if closed.code != expected_code:
                raise GateError("unexpected WebSocket close code")
            return
    raise GateError("WebSocket did not close as required")


def _oversized_ping(message_size: int) -> bytes:
    prefix = b'{"v":1,"type":"ping","nonce":"'
    suffix = b'"}'
    fill_size = message_size - len(prefix) - len(suffix)
    if fill_size < 0:
        raise GateError("oversized message construction failed")
    message = prefix + (b"A" * fill_size) + suffix
    if len(message) != message_size:
        raise GateError("oversized message construction failed")
    return message


def _expect_oversized_close(
    connection: WebSocketConnection,
    *,
    message_size: int,
    expected_code: int,
) -> None:
    try:
        connection.send_frame(0x1, _oversized_ping(message_size))
    except WebSocketSendInterrupted:
        # The receiver may reject from the declared message length before the
        # client finishes writing.  An interruption is acceptable only when
        # this authenticated socket still yields the exact close below.
        pass
    try:
        connection.receive_frame()
    except WebSocketClosed as closed:
        if closed.code != expected_code:
            raise GateError("oversized message used the wrong close code")
        return
    raise GateError("oversized message did not close the WebSocket")


def check_authenticated_and_oversize(
    target: Target,
    context: ssl.SSLContext,
    device: EnrolledDevice,
) -> None:
    with open_websocket(
        target,
        context,
        "/relay/v1/device",
        headers=authenticated_device_headers(device),
    ) as connection:
        require_device_ready(connection, device)
        _expect_oversized_close(
            connection,
            message_size=MAX_FRAME_BYTES + 1,
            expected_code=4409,
        )
    with open_websocket(
        target,
        context,
        "/relay/v1/device",
        headers=authenticated_device_headers(device),
    ) as connection:
        require_device_ready(connection, device)
        _expect_oversized_close(
            connection,
            message_size=MAX_FRAME_BYTES + 2,
            expected_code=1009,
        )


def run_gate(
    target: Target,
    ca_pem: str,
    bootstrap: PairingBootstrap | None,
) -> Recorder:
    context = create_tls_context(ca_pem)
    recorder = Recorder()
    recorder.check("TLS chain and exact IP SAN", lambda: verify_tls_identity(target, context))
    recorder.check("GET /healthz", lambda: check_health(target, context))
    recorder.check(
        "incorrect Host rejected",
        lambda: require_status(
            https_request(
                target,
                context,
                "GET",
                "/healthz",
                host_values=[f"127.0.0.1:{target.port}"],
            ),
            {421},
        ),
    )
    other_port = 27462 if target.port == 27461 else 27461
    recorder.check(
        "wrong-port Host rejected",
        lambda: require_status(
            https_request(
                target,
                context,
                "GET",
                "/healthz",
                host_values=[f"{target.ip}:{other_port}"],
            ),
            {421},
        ),
    )
    recorder.check(
        "repeated Host rejected",
        lambda: require_status(
            https_request(
                target,
                context,
                "GET",
                "/healthz",
                host_values=[target.authority, target.authority],
            ),
            {400, 421},
        ),
    )
    recorder.check(
        "query token rejected",
        lambda: expect_websocket_http_rejection(
            target, context, "/relay/v1/device?access_token=not-a-token", 400
        ),
    )
    recorder.check(
        "each forged forwarding header rejected",
        lambda: check_forged_forwarding_headers(target, context),
    )
    recorder.check(
        "browser Origin rejected",
        lambda: expect_websocket_http_rejection(
            target,
            context,
            "/relay/v1/device",
            403,
            headers=[("Origin", "https://example.invalid")],
        ),
    )
    recorder.check(
        "unknown path rejected",
        lambda: require_status(
            https_request(target, context, "GET", "/relay/v1/unknown"), {404}
        ),
    )
    recorder.check(
        "public Node WebSocket rejected",
        lambda: expect_websocket_http_rejection(
            target, context, "/relay/v1/node", 404
        ),
    )
    recorder.check(
        "missing Authorization closes",
        lambda: expect_websocket_close(
            target, context, headers=[], expected_code=4401
        ),
    )

    if bootstrap is None:
        # This is the explicitly selected boundary-only mode. Credentialed
        # checks are covered by the isolated Gate run and the production App
        # E2E; do not manufacture skipped cases in a zero-skip release gate.
        return recorder

    enrolled: EnrolledDevice | None = None

    def enroll() -> None:
        nonlocal enrolled
        enrolled = enroll_device(target, context, bootstrap)

    if not recorder.check("Gate device enrollment", enroll):
        reason = "enrollment did not complete"
        recorder.fail("missing DeviceProofV1 closes", reason)
        recorder.fail("incorrect DeviceProofV1 closes", reason)
        recorder.fail("authenticated WSS and oversized-message boundaries", reason)
        return recorder
    assert enrolled is not None
    recorder.check(
        "missing DeviceProofV1 closes",
        lambda: expect_websocket_close(
            target,
            context,
            headers=[("Authorization", "Bearer " + enrolled.access_token)],
            expected_code=4401,
        ),
    )
    recorder.check(
        "incorrect DeviceProofV1 closes",
        lambda: expect_websocket_close(
            target,
            context,
            headers=[
                ("Authorization", "Bearer " + enrolled.access_token),
                *proof_headers(
                    enrolled.key,
                    method="GET",
                    raw_target="/relay/v1/not-device",
                    bearer_token=enrolled.access_token,
                ),
            ],
            expected_code=4401,
        ),
    )
    recorder.check(
        "authenticated WSS and oversized-message boundaries",
        lambda: check_authenticated_and_oversize(target, context, enrolled),
    )
    return recorder


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Credential-safe black-box gate for Relay Edge ports 27461 and 27462"
        )
    )
    parser.add_argument("--ip", required=True, help="explicit public IPv4 address")
    parser.add_argument(
        "--port",
        required=True,
        type=int,
        choices=sorted(ALLOWED_EDGE_PORTS),
        help="explicit Relay Edge port (27461 or 27462; never defaults to 443)",
    )
    parser.add_argument(
        "--ca-cert",
        required=True,
        help=(
            "absolute path to one private CA certificate in PEM/DER, or a "
            "current-plus-next PEM bundle"
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--boundary-only",
        action="store_true",
        help=(
            "run only credential-free Edge checks; production authentication "
            "is verified separately by the signed App E2E"
        ),
    )
    mode.add_argument(
        "--pairing-bootstrap",
        help=(
            "absolute path to an optional mode-0600 one-time Gate device "
            "pairing bootstrap; valid only with port 27462"
        ),
    )
    mode.add_argument(
        "--reload-continuity-bootstrap",
        help=(
            "absolute path to a mode-0600 one-use pairing bootstrap used to "
            "hold one authenticated WSS open across a coordinated reload; "
            "port 27461 requires an ephemeral production identity"
        ),
    )
    parser.add_argument(
        "--manifest",
        help=(
            "absolute new path for the immutable mode-0600 reload-continuity "
            "manifest"
        ),
    )
    parser.add_argument(
        "--expected-new-leaf-sha256",
        help=(
            "expected lowercase SHA-256 fingerprint for the leaf served by new "
            "connections after the coordinated reload"
        ),
    )
    parser.add_argument(
        "--ready-signal",
        help=(
            "absolute path for the mode-0600 READY signal created after the old "
            "authenticated WSS is healthy"
        ),
    )
    parser.add_argument(
        "--reloaded-signal",
        help=(
            "absolute path to the mode-0600 RELOADED signal created by the "
            "operator only after Nginx reload completes"
        ),
    )
    parser.add_argument(
        "--cleanup-ready-signal",
        help=(
            "absolute path for the mode-0600 REVOKE signal created only after "
            "port 27461 continuity and new-certificate checks pass"
        ),
    )
    parser.add_argument(
        "--revoked-signal",
        help=(
            "absolute path to the mode-0600 REVOKED signal created only after "
            "the ephemeral production identity is revoked"
        ),
    )
    parser.add_argument(
        "--reload-wait-timeout",
        type=float,
        default=DEFAULT_RELOAD_WAIT_SECONDS,
        help="reload signal wait limit in seconds (default: 300; maximum: 900)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-operation network timeout in seconds (default: 10)",
    )
    return parser


def _render_report(recorder: Recorder) -> None:
    for result in recorder.results:
        suffix = f": {result.detail}" if result.detail else ""
        print(f"[{result.state}] {result.name}{suffix}")
    passed = sum(result.state == "PASS" for result in recorder.results)
    skipped = sum(result.state == "SKIP" for result in recorder.results)
    failed = sum(result.state == "FAIL" for result in recorder.results)
    print(f"edge gate summary: pass={passed} fail={failed} skip={skipped}")


def main(argv: Sequence[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        target = parse_target(args.ip, args.port, args.timeout)
        _, ca_pem = load_ca_certificate(args.ca_cert)
        bootstrap = (
            read_pairing_bootstrap(args.pairing_bootstrap)
            if args.pairing_bootstrap
            else None
        )
        continuity_bootstrap = (
            read_pairing_bootstrap(args.reload_continuity_bootstrap)
            if args.reload_continuity_bootstrap
            else None
        )
        require_full_gate_timeout(bootstrap, args.timeout)
        if continuity_bootstrap is not None:
            if (
                args.expected_new_leaf_sha256 is None
                or args.ready_signal is None
                or args.reloaded_signal is None
                or args.manifest is None
            ):
                raise GateError(
                    "reload continuity requires expected leaf, ready signal, "
                    "reloaded signal, and manifest"
                )
            recorder = run_reload_continuity_gate(
                target,
                ca_pem,
                continuity_bootstrap,
                expected_new_leaf_sha256=args.expected_new_leaf_sha256,
                ready_signal=args.ready_signal,
                reloaded_signal=args.reloaded_signal,
                wait_timeout_seconds=args.reload_wait_timeout,
                manifest_path=args.manifest,
                cleanup_ready_signal=args.cleanup_ready_signal,
                revoked_signal=args.revoked_signal,
            )
        else:
            if (
                args.expected_new_leaf_sha256 is not None
                or args.ready_signal is not None
                or args.reloaded_signal is not None
                or args.manifest is not None
                or args.cleanup_ready_signal is not None
                or args.revoked_signal is not None
                or args.reload_wait_timeout != DEFAULT_RELOAD_WAIT_SECONDS
            ):
                raise GateError(
                    "reload manifest, leaf, and signal options require reload "
                    "continuity mode"
                )
            if bootstrap is not None and target.port != GATE_PORT:
                raise GateError(
                    "pairing bootstrap is accepted only for Gate port 27462"
                )
            recorder = run_gate(target, ca_pem, bootstrap)
    except GateError as error:
        print(f"edge gate setup failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # Never print arbitrary exception messages.
        print(
            f"edge gate setup failed: internal error ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2
    _render_report(recorder)
    return 1 if recorder.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
