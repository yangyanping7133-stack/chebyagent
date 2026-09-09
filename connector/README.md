# ChebyCodex Connector

The Connector is the private half of the APK + Connector product. It runs on
the user's own agent machine and never opens a public application listener.
In the fixed-IP deployment it reaches Relay directly over the shared private
Docker backend while retaining a separate outbound network for Codex. Relay
authentication selects the dedicated assistant; a Relay transport connection
is not a Codex Thread.

## Boundary

```text
Android -- WSS --> public Edge --> Relay <-- private WS -- Connector
                                                    |
                                                    +-- typed GatewayBackend
                                                        (Codex in this slice)
                                                    |
                                                    +-- PhoneBridge host :3448
                                                        -> container :3438/WSS
                                                        (same container)
```

The Codex adapter instantiates `GatewayService`, `GatewayStore`, and the Codex
bridge in the Connector process. It does not proxy loopback HTTP, arbitrary
paths, URLs, methods, or headers. The legacy Gateway HTTP/WSS API remains
available in the repository but must not run against the same Gateway database:
`GatewayService` deliberately enforces one process owner.

`GatewayBackend` is the adapter boundary. A future OpenClaw package can provide
another backend while preserving the frozen Relay operations and Android
experience; Relay, UI, and this interface contain no Turkey host name or Codex
installation path.

Codex also starts three in-image, standard-library stdio MCP children:

- `phonebridge` reaches only the authenticated same-container loopback
  endpoint and exposes Android observation/control tools. Sensitive payments,
  finance, call, and SMS actions remain blocked by ChebyNode on the phone.
- `offline_memory` searches and appends curated local JSON memories.
- `offline_skill` searches and creates reviewed local `SKILL.md` records.

The dedicated single-user Connector is headless, so all tools in its explicit
PhoneBridge, Memory, and Skill allow-lists run without an unavailable human
approval UI. Codex runs headlessly behind a read-only outer filesystem view;
the Connector-owned broker holds PhoneBridge credentials, and ChebyNode's phone-side
PolicyGate remains the final authority for sensitive device actions.

The MCP children open no listeners. Memory/Skill never use the network. The
PhoneBridge MCP child talks only to the Connector-owned, allowlisted Unix-socket
broker; only that broker reads the private bearer file. The supervised
PhoneBridge runtime publishes container `3438/WSS` through host port `3448`,
with mandatory mTLS, fixed device/certificate binding, HMAC replay protection,
and bounded frames. Its command endpoint remains loopback-only.
The container does not join an OpenClaw network or call an OpenClaw API, and it
mounts none of OpenClaw's runtime state or credentials.
OpenClaw may stay active on its own host port `3438`; both stacks remain active
without joining each other's Docker networks.

## Enrollment and configuration

Relay's offline admin flow writes a Node bootstrap file and one-use phone
pairing material as separate mode-`0600` files. An installer consumes the Node
bootstrap and configures:

- `CHEBY_CONNECTOR_RELAY_URL`: exact credential-free Node endpoint. Public
  endpoints require `wss://`. Plain WS is accepted only for the two reviewed
  Docker identities (`172.31.61.3:8080` production and
  `172.31.62.3:8080` gate) or the two literal development loopback endpoints;
  a hostname, adjacent private address, changed port/path, query, or fragment
  fails closed.
- `CHEBY_CONNECTOR_RUNTIME_PROFILE`: `production` starts the reviewed MCP,
  PhoneBridge, and stdio Codex path. `gate` accepts only the fixed Gate Relay
  endpoint and starts the durable scripted fake without MCP, PhoneBridge, or
  Codex.
- `CHEBY_CONNECTOR_ID`: enrolled `node_...` identifier.
- `CHEBY_CONNECTOR_RELAY_TOKEN_FILE`: absolute, regular, non-symlink token file
  with no group/world permissions. Direct token environment configuration is
  supported for managed secret injection but the file is preferred.
- `CHEBY_CONNECTOR_STATE`: absolute private SQLite path.
- `CHEBY_CONNECTOR_HEALTH_PATH`: absolute watchdog path (default
  `/data/connector.health`). The event loop atomically refreshes it every five
  seconds even while Relay is offline; the configurable interval cannot exceed
  ten seconds.
- `CHEBY_CONNECTOR_IDEMPOTENCY_RETENTION_SECONDS`: completed dedupe retention,
  default 30 days and minimum seven days. It must be at least Relay's maximum
  delivery replay retention.

The first authenticated command supplies Relay's generated `deviceId`.
Connector atomically creates the matching private Gateway device projection and
persists the binding. A later command for another device fails closed; no
plaintext device ID is preconfigured. Gateway/Codex settings retain their
existing `CHEBY_*` names, including the database, workspace, and stdio command.

The pairing code is delivered from Relay's one-use phone bootstrap. It is not
printed by Connector and Gateway bearer/refresh/PoP credentials never traverse
Relay.

## Reliability and limits

- Only the 15 frozen Relay operations have parameter models; every model rejects
  unknown fields.
- Commands use a bounded four-worker pool. Operations for the same Thread
  execute in Relay receive order; independent Threads may run in parallel.
  Operations without a Thread identity stay on one serialized global lane.
- Successful `turns.start` responses preserve Gateway's stable public
  `id/threadId/clientMessageId/status/createdAt` tuple. Lifecycle events carry
  the same public `threadId` and `turnId`; raw Codex identifiers never cross
  the Connector boundary.
- `THREAD_BUSY` is cached as a retryable per-Thread wait response. A retry
  after the matching terminal event uses a new Relay request identity and the
  same `clientMessageId`; replay of the old request remains the exact busy
  response and cannot execute a Turn.
- `(messageId, requestId, exact command fingerprint)` and the exact response are
  durable before a cumulative Relay acknowledgement is sent.
- A durable `executing` intent converts a crash-ambiguous generic operation to
  non-retryable `OPERATION_OUTCOME_UNKNOWN`; it never repeats create, patch,
  delete, approval, or interrupt side effects. `turns.start` alone may re-enter
  Gateway because its durable `(device, thread, clientMessageId)` reservation
  either returns the canonical Turn or fences the Thread for resync without a
  second Codex submission.
- Responses/events remain in a count- and byte-bounded SQLite outbox until Relay
  returns `accepted`; reconnect replays them with stable `messageId` values.
  Each socket permits one in-flight outbound message, and unsolicited or
  out-of-order `accepted` frames fail the connection.
- Gateway events retain their own `streamId/eventSeq` cursor. `events.ack` is
  separate from Relay's transport acknowledgement.
- WSS uses Authorization headers, normal certificate verification, application
  ping/pong deadlines, exponential reconnect backoff with jitter, disabled
  compression, a 12 MiB hard frame cap, a 256 KiB non-upload cap, and strict
  canonical Base64 decoded to at most 8 MiB for assets.
- Logs contain only generic lifecycle status. They never include credentials,
  payloads, public conversation identifiers, Codex internal identifiers, or raw
  exception/debug JSON.

Expired completed idempotency rows are pruned in bounded transactions only when
their response has left the outbox, no delivery-cursor gap references them, and
no reserved or executing intent exists. Legacy rows without durable response
correlation are retained. This makes pruning safe locally; operators must still
keep Connector retention at least as long as Relay's replay retention.

## Development gate

Use Python 3.12 for the production-equivalent gate:

```sh
python3.12 -m venv connector/.venv
connector/.venv/bin/pip install -r connector/requirements-dev.txt
PYTHONPATH=connector:gateway:relay connector/.venv/bin/python -m pytest \
  -c connector/pyproject.toml connector/tests
```

The tests cover strict allowlisting, dynamic one-device binding, per-Thread FIFO
with cross-Thread parallelism, cumulative ACK gaps, command
order, message/request deduplication, lost-ack replay, crash ambiguity, outbox
limits, event projection, and a real in-process `GatewayService` integration.

Crash evidence is intentionally tiered:

- `DeliveryCrashBoundaryReferenceTest` is model-only mutation coverage and is
  never release evidence.
- `test_production_store_reconstruction_matrix_runs_twenty_five_times` invokes
  the real Relay, Connector, Gateway, and scripted Codex stores but uses a
  synthetic exception followed by orderly reconstruction.
- The production crash tests use an actual child-process `SIGKILL` 25 times at
  eight exact durable checkpoints: Relay command commit, Connector execution
  intent, Gateway Turn reservation, scripted Codex acceptance, Gateway
  terminal commit, Connector response commit, Relay response commit, and
  Connector response-outbox removal. Each recovery consumes both Relay
  directions and rejects any remaining delivery, intent, reservation, or
  outbox orphan.
- Android instrumentation reconstructs the real SharedPreferences outbox for
  enqueue, accepted, and terminal cleanup. It is not Android process-death
  evidence; the signed-APK real-device process-kill gate remains separate.
