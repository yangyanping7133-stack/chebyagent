# ChebyCodex Relay wire protocol v1

Status: frozen for the first Relay implementation slice.

Relay is a durable, role-aware message switch. It authenticates the Android
device and the dedicated Turkey Node, enforces their binding, and forwards
typed messages. It does not execute Codex, hold ChatGPT OAuth, interpret
`threadId` as a transport session, or accept arbitrary HTTP paths/headers.

## Identity and enrollment

Public identifiers contain 128 random bits encoded as unpadded base64url:

| Identity | Exact syntax |
| --- | --- |
| assistant | `asst_[A-Za-z0-9_-]{22}` |
| node | `node_[A-Za-z0-9_-]{22}` |
| device | `dev_[A-Za-z0-9_-]{22}` |
| credential | `cred_[A-Za-z0-9_-]{22}` |
| message | `msg_[A-Za-z0-9_-]{22}` |
| request | `req_[A-Za-z0-9_-]{22}` |

The offline admin CLI creates an assistant, its unique Node, a one-use pairing
secret, and a long random Node credential. It writes separate device and Node
bootstrap files with mode `0600` and never prints raw credentials. Android then
calls `POST /relay/v1/pairings/exchange` with `assistantId`, the one-use secret,
device name, and canonical P-256 SPKI. This request carries DeviceProofV1 over
the exact request body. It returns short-lived access and rotating refresh
credentials with `Cache-Control: no-store`.

The device bootstrap file contains exactly one line:

```text
CXC1.<base64url-no-padding(strict UTF-8 JSON)>
```

The decoded object is exactly `{v:1,assistantId,pairingSecret}`. The server
address is entered separately on Android's binding page and is not embedded in
the one-time pairing code. The Node bootstrap is a separate `0600` JSON file;
it includes `relayOrigin` as an HTTPS origin with no path, query, fragment, or
userinfo. Neither file content is printed by the CLI. This is a text pairing
code; the product does not require or generate a QR code.

`POST /relay/v1/auth/refresh` carries `deviceId` and the current refresh token
in the strictly bounded body, plus DeviceProofV1 with an empty bearer field.
Refresh atomically revokes every old device access credential and rotates the
refresh verifier. Node rotation is an offline management operation. An old
access, refresh, Node, or pairing credential fails closed.

Relay persists only salted credential verifiers, never raw bearer, refresh,
Node, or pairing secrets. It stores the enrolled device public key because the
Android WebSocket handshake requires proof of possession.

Exactly one `nodeId` is bound to an `assistantId`. A `deviceId` belongs to one
assistant. Relay credentials replace the old REMOTE transport credentials.
Gateway bearer/refresh tokens, pairing secrets, public keys, and PoP headers
MUST NOT appear in Relay commands. A Turkey Connector invokes a local typed
service boundary instead of proxying arbitrary HTTP.

## Connections and authentication

- Device endpoint: `wss://<relay>/relay/v1/device`
- Node endpoint: `wss://<relay>/relay/v1/node`
- Device authentication: `Authorization: Bearer <short-lived access token>`
  plus DeviceProofV1 handshake headers.
- Node authentication: `Authorization: Bearer <long random Node credential>`.
- Query strings are rejected, including query credentials.
- A connection is ephemeral transport state, never a conversation session.
- Production OpenAPI, Swagger, and ReDoc endpoints are disabled.

DeviceProofV1 is byte-for-byte compatible with the Gateway v1 proof:

```text
CHEBY-POP-1
<UPPERCASE METHOD>
<exact raw target>
<X-Cheby-Timestamp>
<X-Cheby-Nonce>
<sha256 exact body bytes>
<sha256 bearer token bytes, or sha256 empty bytes for exchange/refresh>
```

Headers are `X-Cheby-Signature-Version: 1`, `X-Cheby-Timestamp`,
`X-Cheby-Nonce`, and `X-Cheby-Signature`. The key is canonical DER SPKI for
P-256, the nonce is 16–32 random bytes, and the signature is canonical strict
DER ECDSA-SHA256 with low-S. Relay enforces clock skew and atomically claims
each `(proof subject, nonce)` in durable SQLite before the operation. The WSS
proof signs `GET`, exact `/relay/v1/device`, empty body, and the access token.
Node does not use DeviceProofV1.

On connection the server sends:

```json
{
  "v": 1,
  "type": "ready",
  "assistantId": "asst_0000000000000000000000",
  "principalId": "dev_0000000000000000000000",
  "role": "device",
  "ackCursor": 41,
  "nextDeliverySeq": 45,
  "nodeStatus": "online"
}
```

`nodeStatus` is sent only to a device. A device also receives
`{"v":1,"type":"node.status","status":"online|offline"}` when the Node
presence changes.

## Durable transport envelope

A sender submits:

```json
{"v":1,"type":"message","messageId":"msg_0000000000000000000000","payload":{}}
```

Relay durably assigns a sequence in the target stream and replies:

```json
{"v":1,"type":"accepted","messageId":"msg_0000000000000000000000","deliverySeq":45,"duplicate":false,"queued":false}
```

The target receives:

```json
{"v":1,"type":"delivery","deliverySeq":45,"messageId":"msg_0000000000000000000000","payload":{}}
```

`deliverySeq` is monotonic per `(assistantId, recipientRole)`. Device-to-node
and node-to-device sequences are intentionally separate. `messageId` is
idempotent per `(assistantId, senderRole)`: an exact retry returns the original
sequence; reuse with different content is rejected.

The receiver sends cumulative acknowledgement:

```json
{"v":1,"type":"ack","deliverySeq":45}
```

The cursor is persisted. A reconnect automatically replays every delivery with
`deliverySeq > ackCursor` in ascending order. Business `eventSeq` is not a
Relay acknowledgement cursor.

Ping and pong are application frames:

```json
{"v":1,"type":"ping","nonce":"b64url-opaque"}
{"v":1,"type":"pong","nonce":"b64url-opaque"}
```

## Typed payloads

Device-to-node payloads are `command`; node-to-device payloads are `response`
or `event`. Every model rejects unknown fields.

```json
{
  "kind": "command",
  "requestId": "req_0000000000000000000000",
  "deviceId": "dev_0000000000000000000000",
  "operation": "threads.read",
  "params": {"threadId":"opaque-business-id","messageLimit":20}
}
```

```json
{
  "kind": "response",
  "requestId": "req_0000000000000000000000",
  "deviceId": "dev_0000000000000000000000",
  "operation": "threads.read",
  "ok": true,
  "result": {}
}
```

```json
{
  "kind": "event",
  "requestId": "req_0000000000000000000000",
  "deviceId": "dev_0000000000000000000000",
  "streamId": "opaque-stream-id",
  "eventId": "opaque-event-id",
  "eventSeq": 18,
  "eventType": "message.patch",
  "threadId": "opaque-business-id",
  "data": {"occurredAt":"2026-07-20T12:00:00Z","payload":{}}
}
```

Relay enforces that command `deviceId` is the authenticated device, that a
node response/event targets a device in the same assistant, and that
`requestId` correlates to an accepted command. Events correlate only to an
`events.subscribe` request. `threadId` occurs only inside typed business
payloads; it never selects a socket, queue, credential, or transport session.

An accepted `events.subscribe` request remains active across reconnects and
idle periods, so request-retention GC cannot break a long-lived event stream.
A replacement remains provisional until Node returns a successful subscribe
response; the previous request stays active if the replacement fails or its
delivery expires. Success atomically closes the previous request. Relay accepts
late events for that previous request during a short configured handoff grace;
afterward correlation fails closed. Revoking the device closes its active
subscriptions.

### Operation whitelist

| Operation | Strict `params` |
| --- | --- |
| `server.info` | `{}` |
| `capabilities.get` | `{}` |
| `threads.list` | `{archived: boolean}` |
| `threads.create` | `{title?: string}` |
| `threads.read` | `{threadId, messageLimit?, messageCursor?}` |
| `threads.resume` | `{threadId}` |
| `threads.patch` | `{threadId, title?, archived?}` with at least one change |
| `threads.delete.preview` | `{threadId}` |
| `threads.delete.confirm` | `{threadId, confirmPermanentDelete: true, impactToken}` |
| `turns.start` | `{threadId, clientMessageId, input:[text|image]}`; success returns stable public `{id, threadId, clientMessageId, status, createdAt}` |
| `assets.upload` | `{threadId, clientMessageId, clientAssetId, mediaType, bodyBase64}` |
| `turns.interrupt` | `{threadId, turnId}` |
| `approvals.decide` | `{approvalId, decision: approve|reject, actionToken}` |
| `events.subscribe` | `{afterSeq >= 0}` |
| `events.ack` | `{streamId, seq >= 0}` |

Pairing and refresh are intentionally absent from the WebSocket operation
whitelist. They use the two bounded public HTTP endpoints above. Gateway
credentials still never traverse Relay.

## Limits and failure behavior

- Wire frame hard limit: 12 MiB UTF-8 JSON.
- Non-upload message limit: 256 KiB canonical JSON.
- `assets.upload.bodyBase64`: strict canonical base64, decoded maximum 8 MiB.
- JSON structure depth and node count are bounded.
- Total and per-principal concurrent connections are bounded.
- WebSocket connection admission and inbound frame rates are bounded by the
  authenticated role and credential identity, not a shared reverse-proxy IP.
- Pairing and refresh HTTP rates use the verified client source plus the
  assistant or device identity, with a separate high-capacity global guard.
- Public global guards run before attacker-influenced compound buckets. Rate
  limiter key cardinality is capped, and idle buckets are removed when their
  window expires.
- Each recipient stream has both unacknowledged message-count and byte limits.
- Each live socket has a bounded outbound queue; slow consumers are closed.
- When Node is offline, accepted device messages remain in the same bounded,
  persistent node delivery stream and report `queued:true`.
- Authentication/binding failures close with 4401/4403; malformed frames 4400;
  rate limit 4408; oversized frames 4409; queue exhaustion 4410.
- Public errors are stable codes without credentials, internal IDs, payload
  echoes, stack traces, or debug JSON.
- `THREAD_BUSY` is a retryable per-Thread wait condition. The caller waits for
  that Thread's terminal lifecycle event before issuing a new Relay request
  with the same `clientMessageId`; replaying the original request returns its
  exact durable busy response and never executes a Turn.

### Offline and replay execution policy

Only `turns.start` may be accepted while Node is offline, for at most five
minutes and only with its existing `clientMessageId` idempotency field. Every
other operation returns `NODE_OFFLINE`; especially reads/subscription, event
acks, approval decisions, permanent delete, interrupt, patch, and asset upload
are never blindly queued while known offline. Commands accepted while online
also carry a Relay expiry: 15 seconds for approval/delete/interrupt, 30 seconds
for reads/subscription/patch, 60 seconds for ordinary mutations, 120 seconds
for asset upload, and 300 seconds for turn start. Expired deliveries are never
replayed to Node. Expiry is terminal for that Relay `messageId`: a retry gets
`DELIVERY_EXPIRED` instead of `accepted`. Relay also persists an ordered
failure response with the original `requestId`, `deviceId`, and operation so
Android can recover when the first expiry notification was missed.

Relay replay preserves the original `messageId`, `requestId`, and payload
bytes. The Connector MUST durably deduplicate commands by those identifiers
before invoking the local typed service, so a lost Node ack cannot repeat a
side effect. A newly connected Node increments the persisted active generation;
an older generation is fenced and closed before it can receive or acknowledge
another delivery. An expired or revoked credential similarly closes an already
open socket before its next delivery.

Credential/revocation and active Node generation are revalidated inside the
same SQLite write transaction that assigns a sequence or advances an ack. The
single-worker routing critical section also covers ready/replay/live ordering
and GC failure notices. A failed writer is immediately removed from presence;
it cannot leave `nodeStatus=online`.

Acked/expired delivery payload rows are garbage-collected in bounded batches.
Small message-id/fingerprint/state records remain for the longer configured
idempotency window, reclaiming large payloads without allowing an acknowledged
message to execute again. Requests, obsolete credentials, and proof nonces
have separate bounded retention passes. Active subscriptions are excluded from
request GC only after Node has confirmed them; provisional subscriptions use
normal bounded request retention. A correlated subscribe response also permits
the processed command payload to age out after the normal acknowledged-payload
retention even if Node's separate cumulative transport ack was lost. Closed
subscriptions age from their last activity. Pairing and refresh bodies are
accumulated incrementally and rejected as soon as their byte limit is crossed.

When Relay is behind TLS ingress, it derives HTTP source identity only from the
immediate ASGI peer unless that peer is inside an explicitly configured trusted
proxy CIDR. A trusted proxy must provide exactly one sanitized
`X-Relay-Client-IP` or `CF-Connecting-IP` value. Missing, repeated, comma-list,
or invalid values fail closed. Relay never interprets arbitrary `Forwarded`,
`X-Forwarded-For`, or `X-Real-IP` values.
