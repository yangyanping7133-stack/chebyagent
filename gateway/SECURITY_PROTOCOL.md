# Cheby Gateway device proof protocol v1

This document is the interoperability contract for Android and Gateway. Any
change to these bytes is a protocol version change.

## Key and encoding rules

- Algorithm: ECDSA with SHA-256 on NIST P-256 (`secp256r1`).
- Registered public key: canonical DER SubjectPublicKeyInfo, padded RFC 4648
  standard Base64. PEM, raw points, Base64url, non-P-256 keys, trailing data,
  and non-canonical DER are rejected.
- Nonce: 16 through 32 random bytes, padded standard Base64.
- Signature: strict ASN.1 DER ECDSA `(r,s)`, padded standard Base64. Both values
  must be in range and `s` must be low (`s <= n/2`).
- Timestamp: canonical unsigned decimal Unix seconds (no sign, whitespace, or
  leading zero). The absolute server/client difference must not exceed
  `CHEBY_PROOF_CLOCK_SKEW_SECONDS`.

Every protected request carries:

```text
X-Cheby-Signature-Version: 1
X-Cheby-Timestamp: <unix seconds>
X-Cheby-Nonce: <standard padded Base64>
X-Cheby-Signature: <standard padded Base64 DER ECDSA signature>
```

## Canonical bytes

The signed value is UTF-8/ASCII bytes joined by a single LF, with no final LF:

```text
CHEBY-POP-1
<UPPERCASE HTTP METHOD>
<raw path, followed by ? and the raw query exactly when present>
<X-Cheby-Timestamp exactly>
<X-Cheby-Nonce exactly>
<lowercase hex SHA-256 of the exact HTTP body bytes>
<lowercase hex SHA-256 of the bearer token UTF-8 bytes>
```

Query parameter order, percent-encoding, and repeated parameters are not
normalized. The phone signs the exact request target it transmits. Pairing and
refresh have no bearer token, so the final field is SHA-256 of the empty byte
string. WSS signs `GET`, the exact `/v1/events?...` target, an empty body, and
the access token. Other authenticated HTTP requests hash the token characters
after `Bearer `, without the scheme or surrounding whitespace.

Pairing JSON contains the same `devicePublicKey` whose private key signs the
request. Refresh JSON contains both `deviceId` and `refreshToken`; the exact JSON
bytes are covered by the body hash. A refresh token is rotated only if its hash
belongs to that `deviceId` and the device proof is valid.

## Replay and error contract

Gateway atomically claims `(device/SPKI subject, nonce)` in durable SQLite
before performing the operation. The retention window covers the full accepted
timestamp interval, preventing concurrent and post-restart replay. Clients must
use a cryptographically random new nonce for every attempt, including retries.

Malformed keys, signatures, timestamps, nonces, bearer binding, or duplicate
nonces fail closed. Public denials do not distinguish whether a device, key,
signature, or credential was responsible and never include submitted secrets.

## Public size contract

- HTTP request bodies: pairing 16 KiB; refresh 4 KiB; turn creation 256 KiB;
  other state-changing routes 64 KiB by default.
- Local-image upload: exactly one canonical `Content-Length`, no
  `Transfer-Encoding`, exact `application/octet-stream`, and 8 MiB maximum. The
  proof body hash covers the raw bytes exactly.
- WSS outbound event: 512 KiB serialized UTF-8 JSON maximum. An oversized event
  is replaced with `sync.required {"reason":"eventTooLarge"}`.
- WSS inbound text: 16 KiB maximum; acknowledgements are intentionally tiny.
- Thread-detail response: 4 MiB serialized maximum, default 20 recent messages,
  maximum requested page size 100.
- Codex-internal newline-delimited frame: separate 64 MiB cap. It is never sent
  directly to the phone.

WebSocket `permessage-deflate` is outside the contract and must be disabled in
Uvicorn and at the Edge proxy. The required Uvicorn flags are
`--ws-max-size 16384 --ws-max-queue 4 --ws-per-message-deflate false`.

## Local-image reference contract

The phone may send only an opaque `assetRef`; it never sends or receives a
server filename/path. Upload idempotency is the binding
`(deviceId, threadId, clientMessageId, clientAssetId, body SHA-256)`. The same
binding and bytes return the same reference; changed bytes conflict. Turn claim
is transactional with the ordered request fingerprint, and cross-device,
cross-thread, or cross-message references are rejected.

JPEG/PNG detection is based on decoded content, not a client filename or MIME
claim. Animated/multi-frame, truncated, polyglot/trailing-data, decompression
bomb, SVG, GIF, HEIC, or metadata-bearing output is rejected or normalized to a
fresh metadata-free raster. Before Codex dispatch, Gateway revalidates the
owned regular file's mode, link count, device/inode, size, and SHA-256. Only the
private bridge receives `/asset-staging/<random server name>`; WSS, public
history, event outbox, and error payloads do not.
