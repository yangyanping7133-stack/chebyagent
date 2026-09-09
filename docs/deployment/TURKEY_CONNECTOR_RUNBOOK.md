# Turkey outbound Connector runbook

## Current architecture

The Android APK connects to the fixed public Relay edge. This private host runs
Connector, which reaches Relay at `172.31.61.3:8080` over
`chebycodex-relay-edge_relay-backend` and retains a separate outbound network
for Codex. The production Compose hard-pins that private endpoint and ignores
any stale public Relay URL in an env file. Connector owns the
typed Gateway service, starts Codex app-server over stdio with Code Mode Host
disabled so 5.6-sol uses native server-side tools, and
supervises the extracted PhoneBridge in the same container. PhoneBridge is the
only Codex-stack inbound surface: host `3448/WSS` maps to container `3438`, with mandatory mTLS and a fixed
device/certificate binding. No Edge, reverse proxy, raw Codex socket, Gateway
HTTP service, or second application port is needed.

The Codex container is runtime-isolated from OpenClaw: no shared Docker
network, filesystem mount, credential, OAuth state, process, or API call is
allowed. The reviewed one-time Memory/Skill file import is the only migration
step and creates no continuing relationship.
OpenClaw remains active on its own host port `3438`; neither stack joins the
other's Docker network or shares credentials.

The historical `deploy/turkey/docker-compose.yml` and Edge/certbot artifacts are
retained only as prior release evidence. They are superseded by
`deploy/turkey/docker-compose.connector.yml`.

## Preflight

1. Run `python3 deploy/turkey/validate_connector_bundle.py`.
2. Build, vulnerability-scan, and secret-scan the exact Connector image; record
   its immutable image ID.
3. Verify host clock synchronization and that relay-edge created
   `chebycodex-relay-edge_relay-backend` with Relay fixed at `172.31.61.3`.
   Do not use host networking, `host-gateway`, host loopback, or public TLS
   hairpinning for the Node link.
4. Verify the cloud security group and host firewall allow the Codex
   PhoneBridge host TCP `3448`; no Connector,
   Gateway, Codex, Memory, Skill, or local command port is public. Reserve the
   dedicated outbound bridge `172.30.0.0/28` with Connector address
   `172.30.0.2`, plus Connector `172.31.61.4` on the Relay backend; verify
   it does not overlap any host, VPN, Docker, or site route. Install the
   reviewed A/B firewall policy below before activation. TCP `80` and `443`
   must have neither a host INPUT allow nor a public listener.
5. Prepare private data/workspace directories as UID/GID `10002` and install
   Node credentials with `install_node_bootstrap.py --relay-url
   ws://172.31.61.3:8080/relay/v1/node`. Verify the generated
   `0400` runtime token, then securely remove the original bootstrap from the
   host and transfer channel so no second Node credential copy remains.
6. Complete Codex login in the persistent `CHEBY_CODEX_HOME_DIR`; never inject
   ChatGPT session material through Compose environment values.
7. Verify the reviewed PhoneBridge source and SHA-256 under
   `connector/phonebridge`; the image build copies and verifies it, so no
   executable source is mounted from the host. Copy the current PhoneBridge
   CA/server material into a Connector-owned private certificate directory. Build one
   mode-`0400` merged env file with container certificate paths, mandatory
   mTLS, fixed device/fingerprint binding, HMAC pairing token, loopback `3437`,
   public `3438`, and the separate local-token file path. Mount the local bearer
   separately for both the server and MCP child; the server maps that credential
   to controller `chebycodex` and ignores caller-supplied identity headers. Do
   not copy OpenClaw OAuth, Feishu credentials, or old Memory/Skill MCP tokens.
8. Copy only reviewed offline data into
   `$CHEBY_CODEX_HOME_DIR/local-mcp`: `memory/mem0-export.json` and formal
   `skill/skills/*/SKILL.md`. Do not copy OAuth state, `.openclaw`, session
   JSONL, plugin symlinks, workshop drafts, service credentials, or private
   keys. Files are mode `0600`, directories mode `0700`, owned by `10002`.
9. Back up `config.toml`. On startup the Connector idempotently installs the
   reviewed managed block from `deploy/turkey/codex-mcp.connector.toml` and
   runs real `codex mcp list --json`; startup fails unless `phonebridge`,
   `offline_memory`, and `offline_skill` are all enabled. Do not append a second
   unmanaged copy manually.

Before activation, render the effective config and inspect it without printing
container environment or secret file contents:

```sh
docker compose \
  --env-file /etc/chebycodex-connector/operator.env \
  --env-file /etc/chebycodex-connector/node.env \
  --file deploy/turkey/docker-compose.connector.yml config --quiet
```

The service must have exactly one IPv4-only published port,
`0.0.0.0:3448:3438/tcp`, and no `expose`,
privileged mode, added capability, host network, Docker socket, Edge, or
certbot. Port `3437` remains same-container loopback only. The effective service
must attach only to `chebycodex-turkey-connector_outbound` and
`chebycodex-relay-edge_relay-backend`; it must not attach to any OpenClaw
network.

After the container is started, do not infer the runtime contract from rendered
Compose alone. Prove actual memberships, addresses, routes, fixed private Relay
URL, and the exact active IPv4 PhoneBridge mapping:

```sh
sudo python3 deploy/turkey/verify_connector_runtime.py \
  --profile production \
  --env-file /etc/chebycodex-connector/operator.env \
  --env-file /etc/chebycodex-connector/node.env
```

## Host firewall activation

The Turkey host persists its firewall through
`/usr/local/sbin/codex-security-lockdown.sh`. Install the reviewed Python
executable at that legacy systemd path; its shebang is authoritative. It builds
inactive A/B chains first, switches each dispatcher with one rule replacement,
verifies the resulting policy, and rolls dispatchers back if activation fails.
The prior executable is retained as
`/usr/local/sbin/codex-security-lockdown.sh.previous`.

During the isolated `27462` gate:

```sh
sudo python3 deploy/turkey/private_relay_firewall.py \
  --install --gate-enabled
sudo /usr/local/sbin/codex-security-lockdown.sh \
  --verify --gate-enabled
```

Install the reviewed boot ordering only after that live apply and verification
passes:

```sh
sudo install -o root -g root -m 0644 \
  deploy/turkey/systemd/chebycodex-firewall.service \
  /etc/systemd/system/chebycodex-firewall.service
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/docker.service.d
sudo install -o root -g root -m 0644 \
  deploy/turkey/systemd/docker.service.d/10-chebycodex-firewall.conf \
  /etc/systemd/system/docker.service.d/10-chebycodex-firewall.conf
sudo systemctl daemon-reload
sudo systemctl enable chebycodex-firewall.service
sudo systemctl is-enabled --quiet chebycodex-firewall.service
sudo systemctl cat docker.service
```

Do not restart Docker merely to install these units. The already verified
runtime policy remains active. On the next host boot or Docker start,
`docker.service` requires and starts after `chebycodex-firewall.service`; the
firewall unit itself is ordered before Docker. If Docker has not yet created
`DOCKER-USER`, the firewall creates only that documented user chain, installs
its dispatcher as rule 1, completes and verifies the final-DROP policy, and
only then lets Docker start. This pre-Docker verification intentionally does not
require a `FORWARD` hook that Docker has not created yet. Docker then adds its
`FORWARD` jump to `DOCKER-USER`; the Docker drop-in runs the full `--verify` as
`ExecStartPost` and requires both `FORWARD` rule 1 to jump to `DOCKER-USER` and
`DOCKER-USER` rule 1 to jump to the reviewed dispatcher. A failed pre-load or
post-start hook check fails the Docker start instead of accepting an
unfiltered restart-policy Edge.
`ConfigurationDirectory=chebycodex-firewall` creates the mode directory with
mode `0700`, while the sole `ReadWritePaths` exception permits the hardened
service to persist `/etc/chebycodex-firewall/mode` despite
`ProtectSystem=full`.

The active `DOCKER-USER` policy permits only:

- public `eth0` TCP with original destination port `27461` to production
  ingress Edge `172.31.60.2:8443`;
- public `eth0` TCP with original destination port `27462` to Gate ingress
  Edge `172.31.63.2:8443` only in Gate mode;
- production backend Edge `172.31.61.2` and Connector `172.31.61.4` to Relay
  `172.31.61.3:8080`;
- Gate backend Edge `172.31.62.2` and Gate Connector `172.31.62.4` to Gate Relay
  `172.31.62.3:8080`;
- host loopback with original port `18080` to production Relay
  `172.31.64.2:8080`, plus original port `18081` to Gate Relay
  `172.31.65.2:8080` only in Gate mode;
- the pre-existing OpenClaw, Codex, PhoneBridge, DNS, outbound HTTPS, and VPN
  flows, followed by the pre-existing unconditional final DROP.

Before those legacy allows, the policy blocks
`172.30.0.0/28` in both directions with OpenClaw `172.27.0.0/16`, and catches
all other source or destination traffic for ingress `172.31.60.0/28`,
production backend `172.31.61.0/28`, Gate backend `172.31.62.0/28`, and Gate
ingress `172.31.63.0/28`, plus production/Gate loopback publication bridges
`172.31.64.0/28` and `172.31.65.0/28`. Host INPUT also rejects traffic sourced
by those subnets unless it is an established reply. In `production-only`, Gate ingress/backend/loopback source and
destination drops precede the established-flow accept so existing Gate flows
are fenced immediately. Public-interface Docker traffic with original TCP
destination `80` or `443` is dropped before every legacy allow, and full
verification rejects any TCP `80/443` DNAT in Docker's NAT chain. Legacy
OpenClaw `3438` and PhoneBridge `3448` accepts bind the expected public
interface and original destination port. Outbound Connector HTTPS on TCP `443`
remains allowed and is unrelated to public host ingress.

IPv6 INPUT ends in TCP/UDP DROP. Missing `ip6tables` is fatal unless both host
`all` and `default` IPv6 disable controls are explicitly `1`; an absent tool is
never treated as proof that IPv6 is disabled.

After Gate evidence is captured, stop the Gate Connector, Relay, and Edge,
remove cloud TCP `27462`, then atomically remove the active Gate accepts and
persist the reboot mode:

```sh
sudo /usr/local/sbin/codex-security-lockdown.sh --production-only
sudo /usr/local/sbin/codex-security-lockdown.sh \
  --verify --production-only
```

No-argument execution and the reviewed systemd unit reload the persisted mode.
If the mode file is absent, the fail-safe default is `production-only`.

Keep `CHEBY_PROVISIONAL_THREAD_TTL_SECONDS` explicit and bounded. Codex may make
a newly started empty Thread readable before it appears in `thread/list`; the
Gateway preserves that mapping provisionally, promotes it when the catalog sees
it, and removes it after the TTL or an explicit `thread not loaded` response.
Codex 0.144.6 accepts the sandbox enum `workspace-write` for Thread start and
resume; `workspaceWrite` is not a valid app-server protocol value.

## Acceptance gates

Start Connector and prove all of the following:

- The container remains non-root, read-only, capability-free, and healthy.
- Runtime token and internal Gateway secret mounts are regular, read-only mode
  `0400` files visible only to UID/GID `10002`.
- Connector establishes private WS only to the fixed Relay container address,
  with no credential in its URL or logs.
- A phone message reaches a real Codex Thread and its streamed result returns.
- Codex calls PhoneBridge status, captures a real screenshot, and the phone
  itself performs one harmless navigation action over WSS (not USB input).
- Phone-side policy blocks a synthetic payment URL and call/SMS URL; the
  Bridge public endpoint refuses to start without mTLS, and its same-container
  local endpoint requires the private bearer token.
- Offline Memory and Skill each pass list/search/fetch plus a synthetic write,
  read-back, and cleanup or quarantined test-record check; all data survives a
  Connector restart and no MCP listener is published.
- An empty Thread can be renamed and survive an app Gateway reconstruction
  before its first turn without changing its public conversation identity.
- App/Connector/Relay restarts resume the same Thread and replay only unacked
  transport deliveries.
- With Relay stopped, Connector remains healthy and reconnects after recovery.
- The Codex-stack listener/firewall check shows only host `3448` to
  PhoneBridge; the independent OpenClaw stack may retain `3438`. No Gateway,
  Connector, Codex, Memory, Skill, or local command listener is published.

## Temporary 27462 Gate

The Gate Connector is a second Compose project and must not reuse production
Node credentials, pairing secret, data, Gateway database, or scripted state.
It joins only `chebycodex-relay-edge_gate-backend` as `172.31.62.4`, connects
to Gate Relay `172.31.62.3:8080`, and has no port or outbound network.

Install only the Node bootstrap from the `android-e2e` role created by the
three-role Gate flow in `TURKEY_EDGE_RUNBOOK.md`. Its paired CXC1 is the sole
Android Gate invitation. Never install the revoked `edge-full` or
`reload-continuity` Node JSON:

```sh
sudo python3 deploy/turkey/install_node_bootstrap.py \
  --bootstrap /etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json \
  --env-output /etc/chebycodex-gate-connector/node.env \
  --token-output /etc/chebycodex-gate-connector/secrets/node_token \
  --gateway-secret-output /etc/chebycodex-gate-connector/secrets/gateway_internal_secret \
  --relay-url ws://172.31.62.3:8080/relay/v1/node
rm -f -- \
  /etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json
test ! -e \
  /etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json

docker compose \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env \
  --file deploy/turkey/docker-compose.connector-gate.yml \
  config --quiet
docker compose \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env \
  --file deploy/turkey/docker-compose.connector-gate.yml \
  up --detach gate-connector
```

Validate and start `deploy/turkey/docker-compose.connector-gate.yml`. Its
effective service must have exactly one internal network, no published port,
no PhoneBridge/MCP/Codex settings or mounts, and
`CHEBY_BRIDGE_MODE=fake`. No Codex login is performed. Run the headless APK
gate with the frozen high-entropy reply marker; a second Turn in the same
Thread must contain the first marker under `Remembered:` and retain it across
a Gate Connector restart. Use `tools/gate/android_relay_gate.py` in
`headless-restart` mode: it waits for Android's first-Turn checkpoint, restarts
only the isolated Gate Connector, then requires Android to observe the offline
and online transition before the second Turn. Run its `load` mode separately
for the eight-Thread, 500-Turn evidence manifest. The runner installs,
force-stops and instruments the Gate packages only; it never operates the
screen or Wi-Fi. After every audit has completed, follow the authoritative
teardown in `TURKEY_EDGE_RUNBOOK.md`: stop and verify Gate Connector first,
then stop and verify Gate Edge/Relay, revoke the `android-e2e` Node offline,
delete `node.env`, the Node token, Gateway secret, and Gate Connector data,
then empty and verify the isolated Gate Relay DB/WAL/SHM and bootstrap tree.
After evidence capture, remove and verify the two test-only Gate packages over
USB ADB without operating the screen. Finally apply and verify the
production-only host firewall and remove the cloud TCP `27462` rule. Both Gate
state roots must be empty before another bootstrap.

Before load mode, install the reviewed auditor as a root-owned fixed program;
the Android runner never accepts a remote audit path or arbitrary command:

```sh
sudo install -o root -g root -m 0755 \
  deploy/turkey/audit_gate_business_keys.py \
  /usr/local/sbin/chebycodex-audit-gate-business-keys
sudo test "$(stat -c '%u:%g:%a' \
  /usr/local/sbin/chebycodex-audit-gate-business-keys)" = "0:0:755"
```

The reviewed Connector runner sets `umask 077` before importing Gateway or
Connector code, so newly created database, WAL and SHM files are private. For a
Gate volume created by an older image, stop only Gate Connector, make the
existing `gate-connector.sqlite3*` and `gate-gateway.sqlite3*` files owned by
`10002:10002` and mode `0600`, verify every matching file, then start the new
immutable image. Do not change the production Connector volume as part of this
temporary Gate repair. The auditor fails closed on a symlink, unexpected owner,
or group/world-readable Gate database family.

Load mode requires the reviewed SSH alias. After Android reports its public
run token and tuple digest, the runner invokes only that fixed program with
`--mode load --run-token`. It opens fixed Gate Relay/Connector/Gateway SQLite
files in read-only WAL-aware transactions and the fixed scripted-Codex state,
then emits only counts and SHA-256 digests. PASS requires exactly 500 Turns across eight
Threads and identical tuple sets at Relay, Connector, Gateway, fake Codex and
Android, with no relevant intent, outbox, or processed-delivery orphan; the
durable Connector ACK cursor must cover every command. Gate Relay alone retains
acknowledged payloads for 43,200 seconds so this audit remains available
through the required eight-hour soak. Production keeps its normal bounded
retention. Android separately proves one stable local scope/generation pair;
the host proves one authenticated
assistant/device/node generation mapping because the Android scope string is
not persisted under the same name server-side.

Fault mode uses the same fixed root-owned program with only
`--mode fault --run-token`; it accepts no count or persistence-path override.
PASS requires
the exact App/Edge/Relay/Connector/Wi-Fi × ten × A1/A2/A3/B1/B2 matrix, 250
unique business tuples over 100 cycle-local Threads, identical four-layer
tuple digests, one Gateway Turn and at most one fake-Codex effect per key, no
relevant delivery/intent/outbox orphan, and both Relay delivery order and
fake-Codex persisted effect order for each Thread.

Immediately after Gate Connector starts, run its distinct runtime postflight:

```sh
sudo python3 deploy/turkey/verify_connector_runtime.py \
  --profile gate \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env
```

It must prove exact backend-only membership at `172.31.62.4`, no published or
exposed port, no non-backend network, the Gate Relay route source and fixed
private Relay URL. A default route on Docker's internal backend is not treated
as Internet access; any route through a non-backend gateway is rejected.

## Upgrade and rollback

Stop only the Connector container, back up the private Connector/Gateway SQLite
files and Codex home, build and scan the candidate, then change only the pinned
image ID. Preserve the previous image ID and data snapshot until acceptance
passes. If startup fails, stop the candidate, restore the matching snapshot,
and start the previous immutable image. Never run old Gateway/Edge and Connector
simultaneously against the same Gateway database.

Node rotation is a separate credential operation: generate a new credential at
Relay, install it as a new `0400` file, update the path atomically, restart
Connector, and verify the previous open socket is fenced. Do not overwrite a
secret file in place.
