# ChebyCodex Relay

This directory contains the public, durable transport between Android and one
dedicated Turkey Node. Both clients make outbound WSS connections. Turkey does
not expose Gateway, Codex app-server, or an inbound application port.

Relay authenticates and binds identities, orders/replays messages, and enforces
typed operation and resource limits. It never executes Codex or stores ChatGPT
OAuth. `threadId` remains business data inside a command/event and does not
select a connection or transport session.

The frozen protocol is in `../contracts/relay-v1/PROTOCOL.md`; executable
validators are in `cheby_relay/models.py`, and interoperability examples are in
`../contracts/relay-v1/golden-frames.json`.

## Offline bootstrap

Create the database plus separate private bootstrap files:

```bash
python -m cheby_relay.admin bootstrap \
  --database /var/lib/cheby-relay/relay.sqlite3 \
  --relay-origin https://relay.example \
  --device-output /secure/device-pairing-code.txt \
  --node-output /secure/node-bootstrap.json
```

The device file contains only the `CXC1...` one-time text pairing code. The
server address is entered separately in Android. The Node file contains its
Relay origin and long random credential. Both files are created exclusively as
`0600`, and the CLI prints neither value. Relay SQLite stores salted verifiers,
not raw credentials.

Node rotation is also offline and fail-closed:

```bash
python -m cheby_relay.admin rotate-node \
  --database /var/lib/cheby-relay/relay.sqlite3 \
  --assistant-id asst_<22-base64url> \
  --node-id node_<22-base64url> \
  --output /secure/node-rotated.json
```

## Production process

Terminate TLS at a hardened reverse proxy and run a single Relay worker:

```bash
uvicorn cheby_relay.main:app \
  --host 127.0.0.1 --port 8080 --workers 1 \
  --ws-max-size 12582913 --ws-max-queue 4 \
  --ws-per-message-deflate false
```

Set `CHEBY_RELAY_DB_PATH` to a file in a private directory and
`CHEBY_RELAY_ALLOWED_HOSTS` to the exact public host. Interactive docs and
OpenAPI are disabled in code. CORS is not enabled; WebSocket requests carrying
an `Origin` header are rejected because the clients are native applications.
The transport message limit is intentionally one byte above the 12 MiB Relay
business limit: a 12 MiB + 1 byte message is closed by Relay with `4409`, while
larger messages remain bounded and are rejected by the WebSocket transport.

Behind TLS ingress, also set `CHEBY_RELAY_TRUSTED_PROXY_CIDRS` to only the
immediate proxy network Relay sees and set `CHEBY_RELAY_CLIENT_IP_HEADER` to
`x-relay-client-ip` (or `cf-connecting-ip` for a verified direct Cloudflare
boundary). The proxy must delete any client-supplied copy and inject exactly
one validated address. Relay does not trust general forwarded-IP headers.
Pairing and refresh HTTP buckets combine this verified source with the target
assistant/device; WebSocket limits use the authenticated credential identity,
so clients behind one proxy do not share a single bucket. Stable global guards
run before compound public keys, limiter cardinality is capped by
`rate_limiter_max_keys`, and idle buckets are evicted after their window.

One worker is a deliberate v1 constraint: live connection ownership is local
while durable delivery state is SQLite. Horizontal scaling requires a shared
presence/router and database design before increasing workers.

Bounded retention sweeps keep compact idempotency fingerprints longer than
large payload rows. Defaults retain acked payload bytes for one hour, expired
payload bytes for one minute, fingerprints for seven days, requests for thirty
days, and obsolete credentials for seven days. `RelaySettings` exposes every
interval, batch, and retention value. A Node-confirmed active
`events.subscribe` request is never removed by request GC, even after a long
idle period; an unconfirmed provisional request has normal bounded retention.
A replacement remains provisional until Node reports success, so failure or
delivery expiry does not strand the prior stream. Successful replacement closes
the previous request with a bounded late-event handoff grace before normal
retention applies. Its command payload also becomes eligible for acknowledged
payload GC even if the separate cumulative Node ack is lost.

After Android has completed Relay enrollment and a real round trip, revoke the
old REMOTE/direct credentials. Gateway bearer, refresh, pairing, public-key,
and PoP fields are forbidden in Relay WSS payloads; the Turkey Connector calls
the local typed service boundary.

## Tests

```bash
PYTHONPATH=relay pytest relay/tests
```

The suite covers PoP enrollment/refresh recovery, wrong roles/bindings,
cross-assistant isolation, strict schemas, duplicate IDs, reconnect replay,
cumulative ack, offline policy, queue/frame/rate/connection limits, credential
revocation/expiry, and Node generation fencing.
