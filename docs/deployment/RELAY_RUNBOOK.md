# Relay operations runbook

## Purpose and current boundary

Relay is the rendezvous and durable delivery service. It does not execute Codex
or hold ChatGPT OAuth state. Codex Thread IDs remain reusable conversation
identities; a WebSocket is replaceable transport.

For the current Turkey deployment, Android reaches only
`https://192.0.0.8:27461`. Public Edge permits health, pairing, refresh,
and device WSS. Public `/relay/v1/node` is always `404`; Connector reaches Node
WS only through the internal Relay network. Temporary Gate uses `27462` with a
separate database, credentials, Connector, and APK.

This private-CA deployment exposes no product TCP `80` or `443`. The historical
DNS/public-Node generic Relay ingress is not the current Turkey procedure. Use
`TURKEY_EDGE_RUNBOOK.md` and `TURKEY_CONNECTOR_RUNBOOK.md`.

## Preconditions

1. The exact Relay, Edge, and Connector image IDs have passed their static,
   vulnerability, and secret gates.
2. Production and Gate trust bundles authorize only `27461` and `27462`
   respectively and match the signed APK artifacts.
3. Relay SQLite, Connector/Gateway state, and Codex state have root-only
   backups. Secrets are never printed or copied into environment files.
4. The cloud/host policy preserves existing SSH, VPN, OpenClaw, and PhoneBridge
   rules; production adds only `27461`, and `27462` is temporary.
5. Relay port `8080` is never public. Host `18080/18081` remain loopback-only.

## Initial fixed-IP activation

The existing Quick Tunnel Relay and fixed-IP Relay must not run as two workers
against one SQLite database. Initial cutover uses the same reviewed Relay image
and data directory:

1. Inspect and record the old immutable Relay image and bind-mounted data path.
2. Stop the old Relay, then copy the complete stopped data directory, including
   WAL/SHM files when present, into a private backup.
3. Keep cloudflared running and set only the exact migration Host in
   `CHEBY_RELAY_LEGACY_HOST`.
4. Start `deploy/relay-edge/bootstrap_edge.sh`. The new Relay takes loopback
   `18080`, so Quick Tunnel resumes against the same database while fixed Edge
   serves `27461`.
5. Never restore the backup as a routine rollback after new deliveries. Roll
   back the image/Edge while retaining the current Relay database.

The exact commands and recovery ordering are in
`docs/deployment/TURKEY_EDGE_RUNBOOK.md`.

## End-to-end acceptance

Do not call the deployment ready until all are true:

- The production boundary-only gate reports `fail=0 skip=0`.
- The isolated Gate full credentialed gate reports `fail=0 skip=0`.
- Connector authenticates through the internal network without a credential in
  its URL or logs.
- The signed Android app authenticates the fixed origin, creates and reuses real
  Codex Threads, and receives fresh results after app/network/service restarts.
- Relay replay does not duplicate a Codex Turn or side effect.
- Wrong Host/port, duplicate Host, every forged forwarding header, query token,
  Origin, unknown path, public Node WSS, missing Authorization/PoP, and
  oversized frames fail closed.
- Certificate reload preserves an established WSS connection while a new
  connection receives the new leaf.

## Backup, rotation, and recovery

Quiesce the single Relay worker before copying SQLite. Treat the database,
`-wal`, and `-shm` as one state set. Restore only to one worker. After fixed-IP
acceptance, do not roll the database backward; use immutable image or certificate
rollback against current state.

Node rotation is an explicit credential operation: generate a new credential
offline through Relay admin, install a new mode-`0400` token atomically, restart
Connector, and verify the old socket is fenced. Pairing codes remain one use.

Certificate signing occurs offline. Turkey only checks every 12 hours for an
already staged candidate and monitors the public fingerprint/lifetime hourly.
Below 48 hours without a verified replacement, or when `previous` cannot be
restored and publicly verified, Edge stops fail-closed. There is no system-trust,
plaintext, Let's Encrypt, or unpinned fallback.

## Quick Tunnel retirement

Keep Quick Tunnel only through the fixed-origin migration and fault gates. Once
the final signed APK has fresh production evidence:

1. stop Quick Tunnel;
2. clear `CHEBY_RELAY_LEGACY_HOST`;
3. recreate only production Relay with the same current database;
4. prove the legacy Host is rejected and `27461` still passes;
5. retain the old immutable Relay container/image until the rollback window
   closes, but never start it concurrently.
