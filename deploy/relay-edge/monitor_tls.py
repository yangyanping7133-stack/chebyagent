#!/usr/bin/env python3
"""Verify the fixed-IP TLS endpoint against the accepted leaf and private CA."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
from pathlib import Path
import re
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID


HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _port(value: str) -> int:
    parsed = int(value)
    if parsed not in {27461, 27462}:
        raise argparse.ArgumentTypeError("port must be explicitly 27461 or 27462")
    return parsed


def _expected(value: str) -> str:
    if not HEX_SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("expected digest must be 64 lowercase hex characters")
    return value


def verify_peer_certificate(
    certificate_der: bytes,
    *,
    ca_certificate: x509.Certificate,
    public_ip: ipaddress.IPv4Address,
    expected_certificate_sha256: str,
    expected_spki_sha256: str,
    expected_ca_sha256: str,
    minimum_hours: int,
    now: datetime | None = None,
) -> x509.Certificate:
    certificate = x509.load_der_x509_certificate(certificate_der)
    actual_certificate = hashlib.sha256(certificate_der).hexdigest()
    if not hmac.compare_digest(actual_certificate, expected_certificate_sha256):
        raise RuntimeError("served certificate fingerprint does not match current release")
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise RuntimeError("served certificate key is not EC")
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not hmac.compare_digest(hashlib.sha256(spki).hexdigest(), expected_spki_sha256):
        raise RuntimeError("served certificate SPKI does not match the APK pin")
    ca_der = ca_certificate.public_bytes(serialization.Encoding.DER)
    if not hmac.compare_digest(hashlib.sha256(ca_der).hexdigest(), expected_ca_sha256):
        raise RuntimeError("accepted CA certificate does not match current release")
    ca_constraints = ca_certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    )
    ca_usage = ca_certificate.extensions.get_extension_for_class(x509.KeyUsage)
    ca_public = ca_certificate.public_key()
    current = now or datetime.now(timezone.utc)
    if (
        not ca_constraints.critical
        or not ca_constraints.value.ca
        or ca_constraints.value.path_length != 0
        or not ca_usage.critical
        or not ca_usage.value.key_cert_sign
        or ca_certificate.subject != ca_certificate.issuer
        or ca_certificate.not_valid_before_utc > current
        or ca_certificate.not_valid_after_utc <= current
        or not isinstance(ca_public, ec.EllipticCurvePublicKey)
    ):
        raise RuntimeError("accepted private CA constraints are invalid")
    ca_public.verify(
        ca_certificate.signature,
        ca_certificate.tbs_certificate_bytes,
        ec.ECDSA(ca_certificate.signature_hash_algorithm),
    )
    san = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    if san.get_values_for_type(x509.IPAddress) != [public_ip]:
        raise RuntimeError("served certificate IP SAN is invalid")
    if san.get_values_for_type(x509.DNSName):
        raise RuntimeError("served certificate contains an unexpected DNS identity")
    if certificate.issuer != ca_certificate.subject or certificate.subject == certificate.issuer:
        raise RuntimeError("served certificate issuer is invalid")
    leaf_constraints = certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    )
    leaf_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    leaf_extended = certificate.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value
    if (
        not leaf_constraints.critical
        or leaf_constraints.value.ca
        or not leaf_usage.critical
        or not leaf_usage.value.digital_signature
        or leaf_usage.value.key_cert_sign
        or list(leaf_extended) != [ExtendedKeyUsageOID.SERVER_AUTH]
    ):
        raise RuntimeError("served certificate constraints are invalid")
    ca_public.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        ec.ECDSA(certificate.signature_hash_algorithm),
    )
    if certificate.not_valid_before_utc > current:
        raise RuntimeError("served certificate is not valid yet")
    if certificate.not_valid_after_utc - current < timedelta(hours=minimum_hours):
        raise RuntimeError("served certificate is inside the renewal safety floor")
    return certificate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--port", required=True, type=_port)
    parser.add_argument("--expected-sha256", required=True, type=_expected)
    parser.add_argument("--expected-spki-sha256", required=True, type=_expected)
    parser.add_argument("--expected-ca-sha256", required=True, type=_expected)
    parser.add_argument("--ca-certificate", required=True, type=Path)
    parser.add_argument("--minimum-hours", type=int, default=48)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()

    address = ipaddress.ip_address(args.public_ip)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise RuntimeError("public IP must be a globally routable IPv4 address")
    if not 1 <= args.minimum_hours <= 144:
        raise RuntimeError("minimum-hours must be between 1 and 144")
    if not 0.1 <= args.timeout_seconds <= 30:
        raise RuntimeError("timeout-seconds must be between 0.1 and 30")
    ca_path = args.ca_certificate
    if ca_path.is_symlink() or not ca_path.is_file():
        raise RuntimeError("CA certificate path is unsafe")
    ca_certificate = x509.load_pem_x509_certificate(ca_path.read_bytes())

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(
        (str(address), args.port), timeout=args.timeout_seconds
    ) as connection:
        with context.wrap_socket(
            connection, server_hostname=str(address)
        ) as secure:
            certificate_der = secure.getpeercert(binary_form=True)
            protocol = secure.version()
    if protocol not in {"TLSv1.2", "TLSv1.3"}:
        raise RuntimeError("TLS protocol is below the minimum")
    certificate = verify_peer_certificate(
        certificate_der,
        ca_certificate=ca_certificate,
        public_ip=address,
        expected_certificate_sha256=args.expected_sha256,
        expected_spki_sha256=args.expected_spki_sha256,
        expected_ca_sha256=args.expected_ca_sha256,
        minimum_hours=args.minimum_hours,
    )
    remaining = certificate.not_valid_after_utc - datetime.now(timezone.utc)
    print(
        "pinned IP TLS endpoint healthy; "
        f"port={args.port} remaining_hours={int(remaining.total_seconds() // 3600)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
