#!/usr/bin/env python3
"""Verify the public endpoint presents a trusted certificate for its IP SAN."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import re
import socket
import ssl


def verify_expected_fingerprint(certificate_der: bytes, expected: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise RuntimeError("expected certificate fingerprint must be 64 lowercase hex characters")
    actual = hashlib.sha256(certificate_der).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("served leaf certificate fingerprint does not match activated release")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-ip", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--expected-sha256")
    identity.add_argument("--expect-unavailable", action="store_true")
    parser.add_argument("--minimum-hours", type=int, default=48)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()

    address = ipaddress.ip_address(args.public_ip)
    if address.version != 4 or not address.is_global:
        raise RuntimeError("public IP must be a globally routable IPv4 address")
    if not 1 <= args.minimum_hours <= 144:
        raise RuntimeError("minimum-hours must be between 1 and 144")

    if args.expect_unavailable:
        unavailable_context = ssl._create_unverified_context()
        try:
            with socket.create_connection(
                (str(address), 443), timeout=args.timeout_seconds
            ) as connection:
                with unavailable_context.wrap_socket(
                    connection, server_hostname=str(address)
                ):
                    pass
        except (OSError, ssl.SSLError):
            print("TLS endpoint is unavailable as required by bootstrap recovery")
            return 0
        raise RuntimeError("a TLS certificate remains reachable in bootstrap recovery mode")

    context = ssl.create_default_context()
    with socket.create_connection(
        (str(address), 443), timeout=args.timeout_seconds
    ) as connection:
        with context.wrap_socket(connection, server_hostname=str(address)) as secure:
            certificate = secure.getpeercert()
            certificate_der = secure.getpeercert(binary_form=True)
            ssl.match_hostname(certificate, str(address))
    verify_expected_fingerprint(certificate_der, args.expected_sha256)

    expires_at = datetime.fromtimestamp(
        ssl.cert_time_to_seconds(certificate["notAfter"]), tz=timezone.utc
    )
    remaining = expires_at - datetime.now(timezone.utc)
    if remaining.total_seconds() < args.minimum_hours * 3600:
        raise RuntimeError("served certificate is inside the renewal safety floor")
    print(
        "trusted IP TLS endpoint healthy; remaining_hours="
        f"{int(remaining.total_seconds() // 3600)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
