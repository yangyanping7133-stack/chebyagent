from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


PROOF_VERSION = "1"
CANONICAL_PREFIX = b"CHEBY-POP-1"
P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)


class ProofError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedProof:
    timestamp: int
    nonce: bytes


def strict_base64_decode(value: str, *, maximum_bytes: int) -> bytes:
    if not value or len(value) > ((maximum_bytes + 2) // 3) * 4:
        raise ProofError("invalid base64 length")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProofError("invalid base64") from exc
    if len(decoded) > maximum_bytes or base64.b64encode(decoded) != encoded:
        raise ProofError("non-canonical base64")
    return decoded


def load_p256_spki(value: str) -> ec.EllipticCurvePublicKey:
    der = strict_base64_decode(value, maximum_bytes=512)
    try:
        key = serialization.load_der_public_key(der)
    except (TypeError, ValueError) as exc:
        raise ProofError("invalid public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ProofError("unsupported public key")
    canonical = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical != der:
        raise ProofError("non-canonical public key")
    return key


def canonical_request(
    *,
    method: str,
    raw_target: bytes,
    timestamp: str,
    nonce: str,
    body: bytes,
    bearer_token: str,
) -> bytes:
    if not raw_target.startswith(b"/") or b"\n" in raw_target or b"\r" in raw_target:
        raise ProofError("invalid request target")
    try:
        method_bytes = method.upper().encode("ascii")
        timestamp_bytes = timestamp.encode("ascii")
        nonce_bytes = nonce.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProofError("invalid canonical field") from exc
    return b"\n".join(
        (
            CANONICAL_PREFIX,
            method_bytes,
            raw_target,
            timestamp_bytes,
            nonce_bytes,
            hashlib.sha256(body).hexdigest().encode("ascii"),
            hashlib.sha256(bearer_token.encode("utf-8")).hexdigest().encode("ascii"),
        )
    )


def verify_proof(
    *,
    public_key_spki: str,
    headers: Mapping[str, str],
    method: str,
    raw_target: bytes,
    body: bytes,
    bearer_token: str,
    now_epoch: int,
    allowed_skew_seconds: int,
) -> VerifiedProof:
    version = headers.get("x-cheby-signature-version", "")
    timestamp_value = headers.get("x-cheby-timestamp", "")
    nonce_value = headers.get("x-cheby-nonce", "")
    signature_value = headers.get("x-cheby-signature", "")
    if version != PROOF_VERSION:
        raise ProofError("unsupported proof version")
    if (
        not timestamp_value.isascii()
        or not timestamp_value.isdecimal()
        or len(timestamp_value) > 12
        or str(int(timestamp_value)) != timestamp_value
    ):
        raise ProofError("invalid timestamp")
    timestamp = int(timestamp_value)
    if abs(now_epoch - timestamp) > allowed_skew_seconds:
        raise ProofError("timestamp outside proof window")
    nonce = strict_base64_decode(nonce_value, maximum_bytes=32)
    if len(nonce) < 16:
        raise ProofError("nonce is too short")
    signature = strict_base64_decode(signature_value, maximum_bytes=80)
    try:
        r_value, s_value = decode_dss_signature(signature)
    except ValueError as exc:
        raise ProofError("invalid signature encoding") from exc
    if encode_dss_signature(r_value, s_value) != signature:
        raise ProofError("non-canonical signature encoding")
    if not (1 <= r_value < P256_ORDER and 1 <= s_value <= P256_ORDER // 2):
        raise ProofError("non-canonical signature value")
    canonical = canonical_request(
        method=method,
        raw_target=raw_target,
        timestamp=timestamp_value,
        nonce=nonce_value,
        body=body,
        bearer_token=bearer_token,
    )
    key = load_p256_spki(public_key_spki)
    try:
        key.verify(signature, canonical, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ProofError("signature mismatch") from exc
    return VerifiedProof(timestamp=timestamp, nonce=nonce)


def proof_subject_for_spki(public_key_spki: str) -> str:
    key = load_p256_spki(public_key_spki)
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "spki:" + hashlib.sha256(der).hexdigest()
