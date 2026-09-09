# ChebyCodex Mobile project memory

Last updated: 2026-08-07

This file is the project fact source for durable delivery decisions, gates,
recoverable blockers, and the next safe action. It must not contain pairing
secrets, OAuth material, bearer tokens, internal Thread IDs, or raw logs.

## Product direction

- Android-only for the current milestone: one Huawei JUY-AL00 phone connects to
  one dedicated Codex service on the Turkey server.
- This is permanently a one-person self-hosted product, not a hosted platform:
  users install the server bundle and APK, enter one public server IPv4 and
  bind with AK/SK. iOS, multi-tenancy, billing/central control, HA clusters,
  simultaneous multi-server management, app-store publication, and long soak
  or stress tests are not product goals. A verification round must finish in
  at most ten minutes.
- The mobile identity behaves like one dedicated Feishu-style bot. Codex Thread
  is the conversation identity: continue the last Thread by default, while
  allowing users to create, switch, rename, archive, and delete Threads.
- The accepted transport model is Feishu-style outbound relay, not direct
  remote access to Turkey. The Android app and one dedicated Turkey Connector
  each establish an authenticated outbound WebSocket to a managed Cheby Relay.
  The relay enforces a unique assistant-account-to-Turkey-node binding and
  multiplexes Codex Threads over those connections.
- Turkey must not expose ChebyCodex TCP 80/443. Codex app-server and its local
  bridge remain private and are never exposed directly. A reverse tunnel alone
  does not satisfy the product model because account identity, offline delivery,
  routing, and session discovery belong at the relay boundary.
- The fixed Relay identity is `https://192.0.0.8:27461`; temporary Gate is
  `27462`. The user later explicitly required product TCP `80` and `443` to
  remain closed, superseding the earlier Let's Encrypt IP/ACME proposal.
  Therefore Android trusts a signed private-CA bundle, Turkey receives only an
  offline-signed short-lived leaf, a 12-hour job checks for a staged candidate,
  and an hourly public monitor verifies fingerprint, SPKI, IP SAN, CA, protocol,
  and remaining lifetime. Below 48 hours without a verified replacement, Edge
  stops fail-closed. This is not Let's Encrypt renewal.
- Quick Tunnel is migration-only. Fixed Edge temporarily accepts its exact Host
  at the Relay backend while both public routes share one quiesced/migrated
  SQLite state. Old and new Relay workers must never run concurrently against
  that database. Remove Quick Tunnel and the legacy Host only after the signed
  production APK passes fresh Turn and restart/network gates.
- R18 does not yet provide a supported atomic "replace phone" administration
  flow. The staging recovery was performed only after backups by reissuing the
  one-time pairing, resetting the device delivery stream, and preserving the
  existing device identity. Productized re-pair must coordinate credential
  rotation, device cursor reset, Connector binding, and rollback as one audited
  operation; manual SQL is not a production procedure.

## Current fixed-IP candidate

- The v0.4.0 final APK accepts `https://<public-ip>:27461` plus AK/SK. An
  already-bound phone can authenticate and atomically change only the server
  IP while preserving its device key, credentials, Threads, cursors, receipts,
  and durable Outbox.
- Production Relay Edge v05 is live at `192.0.0.8:27461`; the isolated
  Gate is `27462`. Product TCP 80/443 remain closed. Connector uses the private
  Node WSS path; Codex PhoneBridge remains isolated at host port 3448 while
  OpenClaw remains independently active at 3438.
- Signed Android final candidate:
  `Android/app/build/outputs/apk/release/app-release.apk`, version code 7,
  version name `0.4.0`, SHA-256
  `2f6ee750956b6e7d0971311b4555a4be00f7cbf3a3cebac2b7f51f1cb0b38f66`.
- Signed migration candidate:
  `Android/app/build/outputs/apk/migration/app-migration.apk`, version code 6,
  version name `0.4.0-migration`, SHA-256
  `2acc273c8204c38875767f5589822eaceff21eecfa6c7ae06fc2b4e35c2b4b7f`.
- Final and migration signing certificate SHA-256:
  `2b8462d96b405b4f8a3625c0c2837cb7fbe1ba9f32690a9b51cba3751d95ca64`.
- Automated regression is green: 345 Python Relay/Connector/Edge/Gate tests and
  309 Android tests in each of Debug, Migration, Release, and Gate, with zero
  failure/error/skip. The quick-demo contract is fixed at 8 Threads/16 Turns
  with a 480-second instrumentation ceiling. Independent review is P0=0/P1=0.
- The remaining release blocker is physical Android trust, not application or
  server logic: USB serial `48FYD25327409846` is `unauthorized` and the live
  Codex PhoneBridge reports `phone_online=false` with no devices. The latest APK
  cannot be installed or the real-device quick-demo executed until the phone
  grants the one-time USB-debugging authorization; the Mac must not operate the
  phone screen.
- No Git remote is
  configured; do not invent or push one until the Founder declares this local
  repository mature enough for GitHub.

## Historical R18 accepted evidence

- Relay: 36/36 PASS with no skipped tests. This includes a 5000-request public
  high-cardinality probe, a 4096-key hard cap and expiry reclamation probe,
  payload cleanup after a lost cumulative ACK, and provisional subscription GC.
- Connector: 22/22 PASS. Gateway in Python 3.12 with the pinned Pillow 12.3.0:
  190/190 PASS.
- Android Debug and Release unit tests: 273/273 each. Release Lint, release APK
  build, signing, signature identity, zip alignment, and exact installed APK
  byte hash: PASS.
- Independent final review: GO, with P0=0, P1=0, P2=0. The review separately
  reproduced Relay memory-DoS protection, subscription cleanup, custom-port
  HTTP CONNECT, post-TLS credential placement, and proxy-route DNS isolation.
- Relay and Turkey Connector static deployment validators: PASS.
- Latest Relay immutable image:
  `sha256:38d0bb22428e5472471202b3a520f4254830c83ac965c23beb5e5d6e98209646`.
  Exact Trivy scan: 0 HIGH/CRITICAL vulnerabilities and 0 secrets.
- The exact installed final release passed two paired-phone UI gates. A unique
  marker prompt rendered the exact new marker in the dedicated answer semantic
  node (73.954 seconds total), and a separate literal `hi` rendered a new
  completed Codex answer `Hi! What are we working on today?` (133.168 seconds
  total). Evidence: `artifacts/private/device-evidence/real-ui-20260721T105750Z/`
  and `artifacts/private/device-evidence/real-ui-20260721T105910Z/`.
- Staging end-to-end delivery is GO. Production Relay remains NO-GO only for the
  staging infrastructure limitations recorded below.

## Historical R18 staging state

- Active Relay `chebycodex-r18-relay` runs the exact R18 final image above,
  healthy, non-root, read-only, capability-free, and bound only to host loopback
  `127.0.0.1:18080`. The previous image is retained as the stopped rollback
  container `chebycodex-r18-relay-pre-final1`. Its SQLite bind is
  `/srv/chebycodex-r18/relay-data`; fixed-IP migration must reuse that exact
  directory and must not start a second Relay worker against it.
- Active `chebycodex-r18-connector` is healthy and runs immutable image
  `sha256:29ea088d17009b04ed6cee04beaae435e916d1c61099cc9ee01e4be2c232da82`
  (`local/chebycodex-connector:r23-final-review1`), with zero restarts, a
  read-only root filesystem, UID/GID `10002:10002`, all capabilities dropped,
  and `no-new-privileges`.
  Its exact offline Trivy scan found 0 HIGH/CRITICAL vulnerabilities and 0
  secrets. It initiates outbound WSS, has zero listeners/published ports, uses
  Codex 0.144.6 with `gpt-5.6-sol` / `xhigh`, and sets the provisional Thread TTL
  to 3600 seconds. Its exact r23 scan report found 0 HIGH/CRITICAL and 0 secrets.
  Because this host's Docker bridge cannot reach the Internet, the staging
  Connector and image builds use a documented host-network exception; this is
  not accepted for a managed production Relay deployment.
- A Cloudflare Quick Tunnel provides temporary public TLS/WSS for staging only.
  Public and loopback health checks pass, and Connector reconnected after the
  final Relay restart. Quick Tunnel has no stable hostname or SLA and is never
  a production GO condition.
- The phone is paired to the current assistant/device binding. The consumed
  one-use recovery pairing code was deleted from both Turkey and local private
  evidence after final verification.
- Huawei-Guest is validated and supplies direct phone Internet. Android global
  proxy is `null`; no ADB reverse or USB HTTP CONNECT proxy is in the active
  route.
- During recovery, backed-up state was used to align an expired Node delivery
  gap from 223 to the locally processed cursor 258 and remove two stale
  Connector outbox events whose subscription requests had already been erased.
  Gateway/Codex Thread and turn data were preserved.
- The existing `codex-standalone` remains healthy and untouched. Connector uses
  persistent Codex state configured for `gpt-5.6-sol` with reasoning `xhigh`.
  Turkey still has no ChebyCodex public TCP 80/443 listener or firewall rule.

## R26 Codex PhoneBridge extension

- R26 adds a Codex-side PhoneBridge control path without changing the accepted
  outbound Relay architecture for mobile chat. Relay remains the chat/session
  transport; PhoneBridge is the separately authenticated path by which Codex
  can inspect and operate the dedicated Android device.
- Active candidate container:
  `chebycodex-turkey-connector-connector-1`, immutable image
  `sha256:ab9617a7d0918bcd6a1dc691a87756e13ebb8c1c3debf652ced21369e1c12a58`.
  It supervises Connector, Codex app-server, and PhoneBridge in one isolated
  container and publishes only host `3448` to container `3438`. It is non-root,
  read-only, capability-free, `no-new-privileges`, resource bounded, healthy,
  and on dedicated network `172.30.0.0/28`.
- OpenClaw remains independently active on host `3438`; it shares no Docker
  network, mount, token, certificate directory, OAuth/session state, or runtime
  API with the Codex container. The prior Codex connector image and stopped
  container remain available for rollback.
- The R26 image's retained offline Trivy report matches the exact image ID and
  reports 0 HIGH/CRITICAL vulnerabilities and 0 secrets. The rollback snapshot
  has a root-only SHA-256 manifest covering 378 files and verifies cleanly.
- Local MCP is container-private and persistent: offline memory contains 147
  records and offline skill contains 7 skills. Their complete directory chain
  is owned by UID/GID `10002:10002` with mode `0700`; list/search/fetch,
  persistence across restart, and quarantine cleanup passed real stdio tests.
- ChebyNode managed-device candidate is version code 10,
  `0.2.6-phonebridge`, APK SHA-256
  `03fcc9886ea46237a037db00e0580999c4338b088a703e0e3cecefa3cd45f9bf`,
  from pushed commit `87de311`. It requests the Android development permission
  `WRITE_SECURE_SETTINGS` so an explicitly ADB-managed phone can grant it once;
  self-enable preserves every pre-existing accessibility component and fails
  closed if the service-list write fails. Independent review: P0=0, P1=0;
  10 unit tests, build, and lint pass.
- R26 remains NO-GO for real-device delivery. Huawei Cloud currently blocks
  public TCP `3448` upstream even though the host listener, firewall, mTLS, and
  container route pass locally. The JUY-AL00 also requires its OEM first-install
  confirmation for the staged ChebyNode APK; host-driven screen taps are
  prohibited. Therefore phone mTLS/HMAC connection, real screenshot/action,
  literal `hi`, second-turn Thread reuse, and restart reconnection are not yet
  accepted evidence.
- Until phone provisioning succeeds, root-only bootstrap material remains on
  Turkey. After successful import, delete the server copy of the client private
  key, PKCS#12 file/password, and one-time provisioning material. Connector Codex
  auth was seeded from `codex-standalone`; it is isolated from OpenClaw but still
  needs a fresh independent device login before production GO.

## Rich-content decisions

- Safe Markdown is pure Compose: no WebView, executable HTML, remote image load,
  or clickable URI. Links render as visible label plus target text.
- Markdown parsing has hard block/list/render-node limits; excess content
  degrades to one explicit plain-text remainder.
- Media identifiers remain opaque. Until Gateway provides an authenticated,
  device/Thread-authorized output-media resolver, media cards honestly show
  `UNAVAILABLE` plus readable fallback. Do not claim real assistant-media
  rendering is complete.
- `asset.unavailable` accepts the frozen output-media and current local-input
  schemas as two strict, mutually exclusive variants and produces a safe,
  deterministic SYSTEM notice.
- Turkey requests local-image input with `CHEBY_LOCAL_IMAGE_ENABLED=true`, but
  capability advertisement still fails closed unless Codex 0.144.6, Pillow
  12.3.0, and the private runtime-owned `0700` staging directory all match.

## Next safe actions

1. Complete the fixed-IP private-CA Relay cutover on `27461`, close temporary
   Gate `27462`, remove Quick Tunnel and its legacy Host, retain TCP `80/443`
   closed, and repeat reconnect/outage/restart/certificate-recovery gates before
   production GO.
2. Implement and test an atomic replace-phone administration flow plus automatic
   expired-gap/outbox reconciliation; the staging-only manual recovery must not
   become an operator runbook.
3. Keep the current rollback containers and prior immutable images until the
   managed Relay deployment passes the same real-device acceptance gate.

## Reusable engineering lessons

- Pairing needs a mutex around the complete initial transaction. A UI disabled
  state is not an adequate concurrency boundary.
- Client rollback cannot restore credentials already revoked server-side.
  Honest re-pair requires a two-phase server protocol, not local CAS alone.
- Release trees extracted from Git archives may inherit group-writable directory
  modes. Tighten ownership and `go-w` before generating a root-trusted manifest;
  the manifest gate must remain fail-closed.
- Test release behavior in the immutable production runtime with network
  disabled, while mounting tests read-only and keeping test-only dependencies
  outside the deployable image.
- Feishu's fixed bot account, transport connection, and conversation session are
  separate identities. Preserve that separation: an assistant account binds to
  a node, a single long connection multiplexes many Threads, and reconnecting
  must not create a new Thread.
- A test reported as green is not evidence if the framework skipped it. Every
  security regression must be checked for zero skips; the limiter async test was
  converted to `asyncio.run` so it executes without an optional pytest plugin.
- Shell `set -e` is disabled in important ways when a function is evaluated
  inside `if`, `!`, `&&`, or `||`. Certificate validation, reload, and public
  fingerprint checks must each use an explicit nonzero return path; otherwise a
  later successful command can overwrite the failure and falsely report a
  verified rollback. Deterministic fake-Docker tests must cover Nginx `-t`,
  reload, monitor, and missing/invalid `previous`.
- Certificate reload continuity is a separate isolated `27462` gate. It opens
  one authenticated WSS on the old leaf, proves ping/pong, coordinates only
  through fresh current-user-owned mode-`0600` `READY`/`RELOADED` files, proves
  another ping/pong on the same socket after reload, and requires a new trusted
  connection to serve the candidate's exact leaf SHA-256. Signals never contain
  credentials or principal identifiers, and stale signals fail closed.
- Public rate limiting must bound both request rate and key cardinality. Run a
  fixed global guard before attacker-scoped allocation, cap tracked keys, reclaim
  expired empty buckets, and test thousands of distinct identities.
- A correlated successful subscription response is durable evidence that the
  Node processed the command even if its cumulative ACK is lost. Payload GC must
  honor that evidence, while provisional subscriptions must not receive the
  permanent active-subscription GC exemption.
- System HTTP proxies must receive only CONNECT authority. Relay Bearer and PoP
  headers are sent only after target-authenticated TLS; custom HTTPS ports must
  be preserved, and proxy routing must not resolve the Relay hostname locally.
- A newly started Codex Thread may be readable before `thread/list` persists it.
  Preserve its public mapping as a bounded provisional state, serialize reads
  with destructive lifecycle operations, promote on catalog visibility, and
  clean up on TTL expiry or an explicit not-loaded response.
- Codex app-server protocol enums are exact wire values. For Codex 0.144.6,
  Thread start/resume must use `workspace-write`, not `workspaceWrite`.
- A pending mobile turn must never be claimed by an unrelated historical Codex
  event. Bind only a turn still in the explicit dispatching state, and do not
  erase a live projected answer when Codex 0.144.x sends terminal `items: []`.
- Relay delivery ACKs must not head-of-line block rich-message events. Use a
  conflated acknowledgement worker while preserving ordered durable transport
  cursor commits.
- Credential refresh rotation is process-wide, not gateway-instance-local.
  Serialize it across gateway instances and re-read durable state inside the
  lock so a waiter adopts the winner's rotated credential instead of invoking
  the Relay lost-response grace twice.
- Reset reconnect backoff only after a connection survives a heartbeat interval.
  A brief READY followed by failure is not recovery and otherwise creates a
  reconnect/rate-limit storm.
- Retry the selected conversation snapshot and authoritative Thread catalog
  after reconnect. A transient failure between catalog selection and
  `threads.read` must not leave the UI permanently in recovery.
- A UI smoke gate must take its baseline after the new conversation is actually
  visible. An empty composer can still belong to the previous Thread for several
  frames; require the prior conversation content to disappear before sending.
- Real-device acceptance must use the exact installed signed artifact, a fresh
  conversation or a proven-empty baseline, and either a unique prompt with an
  exact new answer or a dedicated answer semantic node with a non-empty result.
  Unit/integration tests and an old completion card are not substitutes.
- Under the no-host-screen-control policy, the Mac may start a phone-side agent
  and retrieve evidence but must not send coordinates or input events. Every
  phone action must re-read one fresh UI snapshot and bind package, focus,
  clickable root resource ID, enabled state, valid bounds, ONLINE state,
  conversation readiness, and the exact composer value before acting.
- OEM package installation must be artifact- and transaction-bound. The
  phone-side installer verifies the exact APK hash, owns the sole live package
  install PID, revalidates the transaction and hash before every Install action,
  accepts Done only after exact `Success`, and becomes wait-only after Done.
