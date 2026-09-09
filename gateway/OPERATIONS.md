# Gateway pairing recovery

Production pairing grants are one-time and fail closed. A successful exchange
atomically consumes the configured secret before the new device credentials are
returned. Reusing the same code cannot pair another phone or revoke the active
phone. Invalid requests are throttled by source and globally with bounded
backoff, but never mutate or permanently lock the grant.

There is deliberately no unauthenticated HTTP endpoint that resets pairing. To
open a new pairing window after a lost response or phone loss:

1. Generate a new high-entropy pairing secret on the Turkey server.
2. Replace the protected `CHEBY_PAIRING_SECRET_FILE` contents (or the container
   secret mounted at that path); do not reuse the previous value.
3. Restart only the Gateway container. Startup registers the new secret hash as
   a fresh one-time grant while preserving conversation state.
4. Pair the intended phone once, then remove the plaintext code from operator
   notes and shell history.

Changing the secret and restarting is the explicit administrative recovery
boundary. Never delete the SQLite database to reopen pairing: that would also
discard recovery cursors, idempotency records, and approval state.

## Rate-limit operations

Rate-limit buckets are durable SQLite rows. Subjects (device IDs and network
sources) are one-way hashed before storage; no token, pairing secret, prompt, or
action token is stored in a bucket. An atomic group decision prevents a blocked
source from draining the global allowance and prevents a blocked global bucket
from creating unbounded source rows. Every bucket persists its exact expiry as
the later of its fixed-window end and active backoff end. Before applying the
4,096-subject ceiling, Gateway deletes only expired, non-blocked rows in the
target scope; it never evicts an active window or backoff bucket. A novel
subject is throttled instead of growing SQLite if no expired slot is available.

| Environment variable | Production default | Scope |
| --- | ---: | --- |
| `CHEBY_PAIRING_SOURCE_LIMIT` | 8 / 60s | pairing source |
| `CHEBY_PAIRING_GLOBAL_LIMIT` | 40 / 60s | all pairing requests |
| `CHEBY_PAIRING_BACKOFF_BASE_SECONDS` | 2 | invalid pairing source |
| `CHEBY_PAIRING_BACKOFF_MAX_SECONDS` | 60 | invalid pairing source |
| `CHEBY_REFRESH_SOURCE_LIMIT` | 20 / 60s | refresh source |
| `CHEBY_REFRESH_DEVICE_LIMIT` | 10 / 60s | registered device |
| `CHEBY_REFRESH_GLOBAL_LIMIT` | 100 / 60s | all refresh requests |
| `CHEBY_SEND_DEVICE_LIMIT` | 30 / 60s | authenticated sends |
| `CHEBY_APPROVAL_DEVICE_LIMIT` | 20 / 60s | approval decisions |
| `CHEBY_WEBSOCKET_CONNECT_SOURCE_LIMIT` | 30 / 60s | WSS source |
| `CHEBY_WEBSOCKET_CONNECT_DEVICE_LIMIT` | 20 / 60s | authenticated device |
| `CHEBY_WEBSOCKET_CONNECT_GLOBAL_LIMIT` | 200 / 60s | all WSS attempts |
| `CHEBY_WEBSOCKET_MESSAGE_DEVICE_LIMIT` | 120 / 60s | inbound WSS frames |

The corresponding `*_WINDOW_SECONDS` variables are documented in the compose
file. Every limit and window must be a finite positive integer; configuration
cannot disable throttling with zero or an unbounded sentinel. Development may
explicitly tune values, but must keep a finite gate.

HTTP throttling returns `429`, `Retry-After`, and `retryable=true`. WSS
throttling accepts the application handshake and closes `4429` with
`retry-after=<seconds>`. The current Android client must parse and honor that
hint before this WSS behavior is promoted to the Turkey production gate;
otherwise its fixed reconnect delay can amplify a throttle into repeated
connections.

`CHEBY_TRUSTED_PROXY_CIDRS` is empty by default. Do not add a public CIDR. Only
list the private socket addresses of an Edge proxy that replaces or sanitizes
`X-Forwarded-For`; otherwise Gateway derives the source from the socket peer.

`BEGIN IMMEDIATE` makes buckets consistent across processes sharing one local
SQLite database. This is not a multi-region or independent-replica limit. The
Turkey Edge/WAF must enforce a second pairing/WSS global gate, request body
limits, and connection limits before traffic reaches any Gateway replica.

## Device proof-of-possession gate

Pairing accepts only a canonical DER SubjectPublicKeyInfo for a P-256 key,
encoded as padded standard Base64. The phone must prove possession of the
matching private key on the pairing request itself. Every subsequent
authenticated HTTP request, refresh request, and WSS upgrade has the same
ECDSA-SHA256 proof requirement. Refresh JSON includes `deviceId`; Gateway
verifies the registered device key and binds the old refresh-token hash to that
device before the atomic rotation. Unknown devices, bad keys, malformed proofs,
replayed proofs, and bad refresh credentials return only the corresponding
generic authentication denial and never echo a key or token.

The exact header and canonicalization contract is frozen in
[`SECURITY_PROTOCOL.md`](SECURITY_PROTOCOL.md). The durable nonce table is part
of SQLite schema version 11, so a valid proof cannot be replayed concurrently or
after a Gateway restart. `CHEBY_PROOF_CLOCK_SKEW_SECONDS` defaults to 300 and
has a hard maximum of 900; keep the Turkey host and phone clocks synchronized.

## Public transport budgets

Gateway rejects an oversized declared `Content-Length` before parsing and also
counts the actual ASGI body chunks, so an absent length or chunked transfer
cannot bypass the cap. Defaults are 16 KiB for pairing, 4 KiB for refresh,
256 KiB for turn creation, and 64 KiB for every other HTTP route. The
respective settings are `CHEBY_PAIRING_BODY_LIMIT_BYTES`,
`CHEBY_REFRESH_BODY_LIMIT_BYTES`, `CHEBY_TURN_BODY_LIMIT_BYTES`, and
`CHEBY_REQUEST_BODY_LIMIT_BYTES`. Those values are also hard configuration
maxima; startup fails if an environment override raises one. The Turkey Edge
must use equal or smaller limits.

Each serialized outbound WSS event is capped at 512 KiB
(`CHEBY_WEBSOCKET_EVENT_MAX_BYTES`, also a hard configuration maximum). Gateway
does not transmit an oversized event; it advances the stream with a small
`sync.required` event whose reason is `eventTooLarge`. Start Uvicorn with
`--ws-max-size 16384 --ws-max-queue 4 --ws-per-message-deflate false`, and
configure every Turkey reverse proxy to strip/decline `permessage-deflate`.
Gateway also rejects decoded inbound text above
`CHEBY_WEBSOCKET_INBOUND_MAX_BYTES` (16 KiB default and hard maximum), but the
Uvicorn size and queue caps are required to bound memory before application
decoding; an Nginx WebSocket tunnel is not itself the pre-decode frame-size
gate. Compression is not part of this protocol and must not be negotiated. This
avoids compression-amplification ambiguity and makes the serialized byte gate
match the public wire budget.

`GET /v1/threads/{threadId}` returns the most recent 20 messages by default, so
an older client that sends no pagination parameters is bounded. New clients may
send `messageLimit` (hard maximum 100) and the opaque, thread-bound
`messageCursor`; `nextMessageCursor` and `hasMoreMessages` describe the next
older page. Responses are trimmed at a hard public 4 MiB serialized budget.
This does not lower the private Codex `thread/read` frame cap described below.

## Local-image input gate

Local images are opt-in with `CHEBY_LOCAL_IMAGE_ENABLED=true`. Gateway advertises
`inputs.localImage` from the proof-protected, `Cache-Control: no-store`
`GET /v1/capabilities` only when all gates pass: Pillow is importable, the
configured staging root is an owned non-symlink directory with mode `0700`, and
the running Codex executable reports exactly `0.144.6`. A failed gate returns an
empty `inputs` object and upload attempts fail closed.

`PUT /v1/threads/{threadId}/turn-inputs/{clientMessageId}/images/{clientAssetId}`
accepts only exact `application/octet-stream`, one canonical `Content-Length`,
no transfer encoding, and at most 8 MiB. The body is covered by the ordinary
device proof. Only decoded, single-frame JPEG and PNG are supported: a bounded
worker verifies the container, enforces 25 megapixels and a 12,000-pixel edge,
applies EXIF orientation, and writes a fresh RGB/RGBA image without EXIF, GPS,
ICC, XMP, or comments. Never add multipart, URL fetch, Base64, or public download
routes to this protocol.

Provision `CHEBY_ASSET_STAGING_DIR` (production: `/asset-staging`) as a dedicated
owned `0700` mount. Files use random server names and mode `0600`. Ready assets
expire after 24 hours; claimed assets are queued for deletion when their turn
becomes terminal; the hard ceiling is seven days. Startup and the periodic
sweeper drain the transactional cleanup queue and remove stale parts/orphans.
Per-device limits are 32 retained assets and 128 MiB. Do not inspect or copy
filenames into logs, events, history, or diagnostics.

Turn idempotency covers the canonical ordered public input array. Reusing a
`clientMessageId` with changed text, asset references, or order returns
`TURN_IDEMPOTENCY_CONFLICT`. The public `{type:image,assetRef}` value is resolved
only after device/thread/message binding and inode/hash checks, then mapped to a
private Codex `{type:localImage,path,detail:auto}` object. Public history stores
only `[图片 × N]`; public events never contain an asset reference or path.

Approval summaries and reasons are never copied directly from Codex. Gateway
first redacts the complete command/change/reason strings, then performs UTF-8
safe per-field truncation (1 KiB summary, 2 KiB reason) and a 3 KiB combined
budget. Only those sanitized values may reach SQLite, events, or diagnostics.
On every startup, every retained approval row is re-sanitized and every retained
approval event is rebuilt from that authoritative row in one SQLite transaction.
Pending approvals receive the current deterministic action token; terminal
approvals receive a disabled token hash and their historical requested payload
remains `pending` without an action token so Contract V1 clients can decode the
original mutation; a following resolved or expired event carries the terminal
state. Orphaned, malformed, or state-inconsistent approval events are deleted
and the affected stream replay floor advances to the removed sequence, forcing
older cursors through `sync.required` rather than exposing a sequence hole. This
closes upgrades from older Gateway versions whose stored summaries or retained
replay events used weaker redaction.

## Thread lifecycle safety

Exactly one Gateway process may own a SQLite database. Startup takes a
non-blocking OS advisory lock on `<database>.service.lock` before starting the
Codex bridge or accepting lifecycle work. A second Service instance fails
closed with `GATEWAY_INSTANCE_CONFLICT`, and direct lifecycle calls on an
unstarted/non-owner instance are rejected as well. Pairing-grant initialization,
pairing exchange, and refresh-token rotation are also owner-only writes;
`authenticate` remains the explicitly read-only credential check. The bridge
event handler is registered only after lock acquisition and is cleared by
identity, so constructing, failing, or closing a non-owner cannot hijack or
remove the active owner's callback. This makes the in-process async lifecycle
mutex valid for patch, resume, archive, delete, create, turn, read-projection,
and bridge-event boundaries.

The lock has no TTL and cannot be stolen from a slow but live destructive
request; the kernel releases it automatically if the owner exits or crashes.
An `after_in_child` fork hook closes inherited lock descriptors without issuing
`LOCK_UN`, so an idle prefork child cannot keep a dead owner's lock alive or
release a live parent's lock. The small lock file may remain on disk after a
crash, but an unlocked file does not prevent restart.

Every active or archived list is rebuilt from both complete Codex
`thread/list` partitions. Gateway explicitly requests every 0.145 source kind,
including all subAgent variants, does not apply a `cwd` filter, and follows the
opaque `nextCursor` until it is null. Protocol 0.145 does not provide an
ancestor selector, so this global catalog plus `parentThreadId` closure is the
authority for descendants, including those resumed in another worktree.
Invalid entries, duplicate Threads, repeated cursors, more than 1,000 pages,
or a lifecycle-closure change between the two safety scans fail closed without
replacing local authority. Titles, previews, timestamps, and unrelated Thread
metadata are deliberately excluded from the delete-stability fingerprint. A
root-scoped stable scan reconciles only the union of that root's verified
remote closure and existing local closure. An unrelated Thread that moves
between the active and archived partitions may be absent from one combined
scan, but that absence cannot delete or tombstone it; only an unscoped global
sync may replace full-catalog authority.

Missing or explicitly deleted Codex Threads are tombstoned. The same SQLite
transaction purges retained events belonging to the deleted closure, removes
its Thread, Turn, Item, approval, projection, and raw-ID mappings, then emits
public `thread.deleted` markers. Unrelated retained events are not deleted. A
durable per-stream replay floor makes selective sequence holes safe: a cursor
from before the last removed event receives `sync_required`, while a current
cursor can receive the terminal markers and later unrelated events. Tombstones
make duplicate delete notifications idempotent and prevent delayed
turn/item/archive notifications or a later catalog refresh from recreating a
deleted identity.

Archive and unarchive use `PATCH /v1/threads/{threadId}` with `archived=true`
or `false`; the response is reconciled from the complete catalog rather than
assuming the requested transition succeeded. Codex may cascade lifecycle
operations to spawned descendants, so permanent deletion is a two-step flow:

1. Archive the root and all descendants.
2. Call `POST /v1/threads/{threadId}/delete-preview`. The no-store response
   includes only public IDs/titles/statuses plus a short-lived impact token.
3. Show the complete affected list to the user.
4. Send `DELETE /v1/threads/{threadId}` with a JSON body containing
   `confirmPermanentDelete=true` and the exact `impactToken`.

Never put an impact token in a URL, query string, log, or diagnostic. SQLite
stores only its SHA-256 hash. The token is bound to one device, root Thread,
and exact recursive descendant sets; it is single-use at the dispatch
boundary, expires after `CHEBY_DELETE_IMPACT_TTL_SECONDS` (60 seconds by
default, hard maximum 300 seconds, with equality treated as expired), and
survives Gateway restart. Claiming the token atomically moves the complete
closure to `pendingDelete`; new turns and late event mutations are rejected
until the result is resolved. If Codex accepts a turn but a `thread/deleted`
notification is processed before the successful turn response, Gateway keeps
the tombstone authoritative and returns a non-retryable
`TURN_ACCEPTED_THREAD_DELETED` error; it never recreates or resends the turn.

If Codex rejects the request before accepting it, Gateway releases the same
claim and the caller may retry that token while it remains within its TTL. If
delivery is unknown, or Codex accepted deletion but the following catalog
reconciliation failed, Gateway never resends the destructive RPC. The durable
claim remains non-retryable and startup performs a stable authoritative scan:
absent remote Threads are purged/tombstoned; still-present remote Threads have
their barrier cleared and may be previewed again. A newer preview invalidates
the previous pending preview for the same device/root. Any expiry, replay,
archive drift, new descendant, lifecycle-closure mutation, or verified
post-delete mismatch requires a fresh preview.

## Codex history memory gate

`thread/read(includeTurns=true)` is one newline-delimited JSON frame; the
current app-server protocol does not expose history pagination. Real workspace
measurements reached 48,638,397 compact JSON bytes. Gateway therefore defaults
`CHEBY_CODEX_FRAME_LIMIT_BYTES` to 64 MiB and fails closed with
`BridgeFrameTooLarge` above it.

The Turkey deployment must not use the earlier 512 MiB same-container budget.
The observed large-thread probe peaked at 344.48 MiB in Gateway and 285.66 MiB
in its Codex child. Peaks are not guaranteed to coincide, so the supported
container memory limit is at least 1 GiB; 1.5 GiB is the recommended production
budget. Treat a 512 MiB deployment as a failed release gate.

Before release, run `tools/history_memory_probe.py` against the largest known
thread from the Gateway virtual environment. The script prints counts and peak
RSS only; it never prints prompts, item content, raw responses, or credentials.
