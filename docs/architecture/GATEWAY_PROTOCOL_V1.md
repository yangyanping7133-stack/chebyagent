# Gateway Protocol V1

Status: frozen for the first vertical slice
Date: 2026-07-19

## Boundary

```text
Android App -- HTTPS/WSS --> ChebyCodex Gateway -- stdio/Unix socket --> Codex app-server
```

The phone never connects to Codex app-server directly. The Gateway exposes a small product API, not generic JSON-RPC. It must not proxy `thread/shellCommand`, arbitrary `fs/*`, `process/*`, or other unrestricted methods.

Codex WebSocket transport is experimental; Gateway-to-Codex uses the stable stdio/Unix boundary. Mobile WSS is owned by this project and may evolve independently.

## Thread Model

Each mobile conversation maps one-to-one to a Codex Thread.

```json
{
  "id": "thr_public_id",
  "title": "App 开发讨论",
  "preview": "正在运行测试",
  "createdAt": "2026-07-19T00:00:00Z",
  "updatedAt": "2026-07-19T00:01:00Z",
  "status": "running",
  "lastTurnId": "turn_public_id",
  "unread": false
}
```

Allowed status values: `idle`, `running`, `waitingApproval`, `waitingUser`, `failed`, `archived`.

On startup, the App restores its local `lastThreadId`. If unavailable, it opens the most recently updated non-archived Thread.

## Event Envelope

```json
{
  "v": 1,
  "streamId": "stream_uuid",
  "eventId": "evt_uuid",
  "seq": 42,
  "occurredAt": "2026-07-19T00:01:02Z",
  "type": "message.patch",
  "threadId": "thr_public_id",
  "turnId": "turn_public_id",
  "itemId": "item_public_id",
  "payload": {}
}
```

- `eventId` is globally unique and used for duplicate suppression.
- `seq` is monotonically increasing inside one `streamId` for one paired device.
- WSS resume uses `/v1/events?streamId=<streamId>&afterSeq=<lastAppliedSeq>`.
- The App acknowledges the latest contiguous sequence.
- A gap pauses mutation and requests replay. A changed `streamId` or expired replay window makes Gateway emit `sync.required`, and the App fetches a snapshot.
- Historical restoration renders the saved final state and does not replay presentation animations.

## Message Idempotency

Every send carries a UUID `clientMessageId`.

Gateway deduplicates on `(deviceId, threadId, clientMessageId)` and always returns the same Turn for retries. The real adapter maps it to `turn/start.clientUserMessageId` when supported and keeps its own durable key regardless of Codex version.

## Rich Message Patch

```json
{
  "messageId": "msg_public_id",
  "baseRevision": 6,
  "nextRevision": 7,
  "ops": [
    {
      "op": "block.merge",
      "blockId": "steps",
      "value": {"activeItemId": "test"}
    },
    {
      "op": "message.state.set",
      "value": "streaming"
    }
  ]
}
```

V1 operations:

- `block.put`
- `block.merge`
- `block.text.append`
- `block.terminal.append`
- `block.remove`
- `root.set`
- `message.state.set`
- `action.resolve`

`baseRevision` must equal the current message revision. A mismatch requests the full message snapshot.

## Approvals

```json
{
  "approvalId": "approval_public_id",
  "threadId": "thr_public_id",
  "turnId": "turn_public_id",
  "itemId": "item_public_id",
  "kind": "command",
  "summary": "运行测试命令",
  "reason": "验证本次修改",
  "decisions": ["approve", "reject"],
  "state": "pending",
  "expiresAt": "2026-07-19T00:06:00Z"
}
```

Approvals use Gateway-owned public IDs and single-use action tokens. Resolved or expired approvals cannot be submitted again.

## API Surface

- `GET /v1/threads`
- `POST /v1/threads`
- `GET /v1/threads/{threadId}`
- `PATCH /v1/threads/{threadId}` for rename/archive/unarchive
- `DELETE /v1/threads/{threadId}` after explicit permanent-delete confirmation
- `POST /v1/threads/{threadId}/turns`
- `POST /v1/threads/{threadId}/turns/{turnId}/interrupt`
- `POST /v1/approvals/{approvalId}/decision`
- `GET /v1/events?streamId=<streamId>&afterSeq=<seq>` using WSS upgrade

## Codex Mapping

| Product operation | Codex app-server method/event |
| --- | --- |
| Create/list/read/resume Thread | `thread/start`, `thread/list`, `thread/read`, `thread/resume` |
| Rename/archive/unarchive/delete | corresponding `thread/*` method |
| Send/interrupt | `turn/start`, `turn/interrupt` |
| Assistant text stream | agent message item and delta events |
| Plan progress | `turn/plan/updated` and plan item events |
| Terminal/Diff | command execution and file change events |
| Approval | command/file approval server requests |
| Final status | item completion first, then `turn/completed` |

`item/completed` is authoritative for an Item. Gateway must not rely on `turn/completed` alone to reconstruct message content.

## Security Gates

- TLS only outside a private device tunnel.
- Pairing secret and access token never appear in logs, screenshots, URLs, or UI.
- Public IDs must not reveal raw Codex Thread/Turn/Item identifiers.
- Allowlisted methods and validated payload sizes only.
- Rate limits for pairing, sends, event reconnect, media, and approvals.
- Media/file references are opaque Gateway asset IDs; the App never opens arbitrary server paths.
- Security-sensitive actions produce an audit event without secret payloads.

### Rate-limit recovery contract

HTTP throttling is a recoverable response with this stable public shape:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 30
Content-Type: application/json

{"error":{"code":"RATE_LIMITED","message":"Too many requests; retry later","retryable":true}}
```

`Retry-After` is an integer number of seconds. The App retains the same
`clientMessageId` and action state, waits at least that long, then retries; it
must not convert `RATE_LIMITED` into a permanent send or approval failure.

Because the WSS handshake is accepted before application authentication, an
event channel throttle closes with code `4429`, no preceding message, and the
public reason `retry-after=<delta-seconds>`. Clients must parse that bounded
decimal hint and wait at least the greater of it and their current reconnect
backoff. `4401` remains the refresh/re-pair signal and `4409` remains the
snapshot-sync signal.

Gateway persists fixed-window counters and bounded pairing backoff in SQLite.
Pairing is limited atomically by source and globally; invalid requests never
consume or permanently lock the one-time grant. Sends and approvals are
limited by authenticated device. WSS connection attempts are limited by
source, device, and globally; inbound WSS messages are limited by device.

`X-Forwarded-For` is ignored unless the socket peer is within an explicitly
configured trusted-proxy CIDR. The trusted Edge must replace or sanitize the
header. These application limits are a single-SQLite safety gate; production
Edge/WAF limits are still required before a multi-replica deployment.
