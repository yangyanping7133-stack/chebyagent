# Current Turkey deployment: outbound Connector

This is the current APK companion for a user's private Codex machine. It runs
one supervised container containing Connector, the typed `GatewayService`
adapter, a Codex app-server child over stdio, and the extracted PhoneBridge.
Connector reaches Relay directly at `172.31.61.3:8080` over the external
internal network `chebycodex-relay-edge_relay-backend`; it also retains its
dedicated outbound network for Codex. It never hairpins through host
`127.0.0.1` or the public TLS edge. The production Compose hard-pins this
private endpoint, so a stale public bootstrap value cannot silently restore
the Quick Tunnel path. PhoneBridge publishes this Codex stack's
only inbound surface, host `3448/WSS` mapped to container `3438`, protected by mandatory mTLS, device/certificate
binding, HMAC replay protection, and strict size limits. Its local command port
`3437` is bound only to same-container loopback.

This container has no runtime dependency on OpenClaw: it joins no OpenClaw
network and mounts no OpenClaw directory, credential, session, or socket. The
one-time reviewed Memory/Skill content migration is data import, not an
integration. OpenClaw being stopped, upgraded, or removed cannot affect this
Codex-to-phone path.

The independent OpenClaw container remains active on host port `3438` while
this Codex stack uses host port `3448`. Dual-active does not mean shared
networking, state, or credentials.

The earlier direct Gateway/Codex public-IP Edge/certbot bundle remains in this
directory for audit evidence, but is **SUPERSEDED** and must not be activated.
Current public ingress is the separate `deploy/relay-edge` fixed-IP Relay Edge.
Use `docker-compose.connector.yml`, not `docker-compose.yml`.

## Build gate

From the repository root:

```sh
python3 deploy/turkey/validate_connector_bundle.py
docker build --pull=false --platform linux/amd64 \
  --file deploy/turkey/Dockerfile.connector \
  --tag local/chebycodex-connector:0.1.0 .
docker image inspect --format '{{.Id}}' local/chebycodex-connector:0.1.0
```

The static gate works without Docker. The image gate additionally requires the
hash-locked build to pass, Codex CLI to report the pinned version, an image scan
to show no unresolved HIGH/CRITICAL vulnerability or embedded secret, and the
exact resulting image ID to be used in the private operator env file. The
dependency hashes target CPython 3.12 on Linux amd64/musllinux.
The reviewed PhoneBridge source is copied into the immutable image and checked
against `connector/phonebridge/phonebridge.sha256`; it is never an executable
host bind mount.

## Install the Node bootstrap

Prepare host directories with fixed ownership. These paths are examples and may
be moved, but every path supplied to Compose must be absolute:

```sh
sudo install -d -m 0700 -o 10002 -g 10002 \
  /var/lib/chebycodex-connector/data \
  /var/lib/chebycodex-connector/codex-home \
  /var/lib/chebycodex-connector/workspace \
  /var/lib/chebycodex-connector/asset-staging
sudo install -d -m 0700 -o root -g root \
  /etc/chebycodex-connector \
  /etc/chebycodex-connector/secrets
```

Place Relay's transferred `node-bootstrap.json` in a root-only `0700` directory
as mode `0600`, then run:

```sh
sudo python3 deploy/turkey/install_node_bootstrap.py \
  --bootstrap /root/node-bootstrap.json \
  --env-output /etc/chebycodex-connector/node.env \
  --token-output /etc/chebycodex-connector/secrets/node_token \
  --gateway-secret-output /etc/chebycodex-connector/secrets/gateway_internal_secret \
  --relay-url ws://172.31.61.3:8080/relay/v1/node
```

Install the pinned bubblewrap seccomp profile before rendering Compose. The
installer downloads only the matching Moby v26.1.3 default, verifies its pinned
SHA-256, appends the two reviewed compatibility rules, and writes atomically:

```sh
sudo python3 deploy/turkey/install_codex_seccomp_profile.py \
  --output /etc/chebycodex-connector/codex-bwrap-seccomp.json
```

The runtime postflight verifies the generated profile's exact SHA-256 and
returns failure if it is missing or edited; the deployment workflow must then
stop or restore the previously reviewed Connector image.

The optional private override accepts only reviewed fixed endpoints and is
never printed. The installer validates the bootstrap and writes the two runtime credentials as
UID/GID `10002`, mode `0400`; it never prints their values. The generated
`node.env` contains only the credential-free endpoint, opaque Node ID, and host
file paths.
Copy `.env.connector.example` to `/etc/chebycodex-connector/operator.env` and set
the reviewed image ID and resource/path choices. Never paste the Node token into
an env file. After the generated files and permissions have been verified,
securely remove the original `node-bootstrap.json` from the target host and the
transfer channel; it is a second usable copy of the Node credential and must not
remain beside the runtime secret.

## Codex login and start

Authenticate Codex once into the persistent Codex home using the supported
Codex device login flow. Do not copy ChatGPT cookies into the image or
repository:

```sh
docker compose \
  --env-file /etc/chebycodex-connector/operator.env \
  --env-file /etc/chebycodex-connector/node.env \
  --file deploy/turkey/docker-compose.connector.yml run --rm --no-deps \
  connector codex login --device-auth
```

Then start the Connector with both non-secret env files:

```sh
docker compose \
  --env-file /etc/chebycodex-connector/operator.env \
  --env-file /etc/chebycodex-connector/node.env \
  --file deploy/turkey/docker-compose.connector.yml up -d connector
sudo python3 deploy/turkey/verify_connector_runtime.py \
  --profile production \
  --env-file /etc/chebycodex-connector/operator.env \
  --env-file /etc/chebycodex-connector/node.env
```

Startup atomically installs the reviewed managed MCP block into the persistent
Codex config, preserves unrelated model/login settings, and asks the real
`codex mcp list --json` command to verify all three required servers. A name
conflict, malformed managed block, or rejected MCP config fails startup closed.
This dedicated single-user Connector is headless, so all tools in the explicit
PhoneBridge, Memory, and Skill allow-lists run without an unavailable human
approval UI. Every Thread and Turn selects the reviewed `cheby_mobile`
permission profile with approval policy `never`. Native tools can write the
workspace and use the network without prompts, while ChatGPT login files,
Connector secrets, and Connector data remain unreadable. The outer bubblewrap
launcher separately masks runtime secret mounts and exposes writable procfs
only to the trusted app-server so its nested user namespace can create UID/GID
maps. The permission profile hides `/proc` from every native tool, and the
reviewed inner bubblewrap wrapper denies every Linux
signal-delivery syscall, so native tools cannot enumerate or signal Connector,
Gateway, or PhoneBridge parent processes.
The container remains capability-free and read-only outside reviewed mounts, and ChebyNode's
phone-side PolicyGate remains authoritative for payments, calls, SMS, and other
sensitive device actions.

Production keeps Codex headless with approval policy `never`. Codex's named
per-turn permission profile enforces the credential and process-visibility
boundaries while preserving workspace write and network access. The outer
namespace exposes a read-only root, reviewed writable directories, and masked
runtime secret mounts; the inner command sandbox adds the signal boundary.
Docker's default syscall profile blocks the unprivileged user namespace needed
by bubblewrap, so the production service uses the pinned profile generated by
`install_codex_seccomp_profile.py`: the matching Moby v26.1.3 default plus the
reviewed user-namespace clone and mount syscall exceptions the outer launcher
requires. Those exceptions are broader than Docker's default and are accepted
only together with the remaining container controls; `seccomp=unconfined` is
forbidden. The service still runs as UID 10002 with
every capability dropped, `no-new-privileges`, a read-only root, and only the
reviewed writable mounts. Runtime postflight exercises the complete outer
bubblewrap to nested managed-profile sandbox path and checks credential,
process, signal, workspace, and network boundaries. The release gate separately
must prove the same profile through a real native-shell Turn. A deployment that
cannot prove both layers must stop or roll back the new Connector.

Container logs contain lifecycle status only. Health is based on the fresh
`/data/connector.health` heartbeat written by the Connector's main event loop.
Relay downtime must leave the container healthy and trigger bounded reconnect
backoff, not a restart loop.

## Isolated 27462 Gate Connector

Gate uses the same reviewed Connector code but a separate runtime profile,
Node identity/token, pairing secret, data directory, Gateway database, and
durable scripted state. It joins only
`chebycodex-relay-edge_gate-backend` at `172.31.62.4`, connects to Gate Relay
`172.31.62.3:8080`, and publishes no port. It does not install MCP
configuration, start PhoneBridge/Codex, or require a Codex login. Its reply
always contains the frozen high-entropy Gate reply marker, echoes the current marker, and recalls
the previous marker in the same Thread after restart.

Create separate private directories and install only the `android-e2e` Node
bootstrap generated by the three-role Gate flow. Never install the revoked
`edge-full` or `reload-continuity` Node JSON:

```sh
sudo install -d -m 0700 -o 10002 -g 10002 \
  /var/lib/chebycodex-gate-connector/data
sudo install -d -m 0700 -o root -g root \
  /etc/chebycodex-gate-connector \
  /etc/chebycodex-gate-connector/secrets
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
```

Copy `.env.connector-gate.example` to
`/etc/chebycodex-gate-connector/operator.env`, set the same reviewed immutable
image ID, and start only after the relay-edge Gate backend exists:

```sh
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
sudo python3 deploy/turkey/verify_connector_runtime.py \
  --profile gate \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env
```

Run `docker compose ... config --quiet` first. The rendered Gate service must
have no `ports`, `expose`, outbound network, PhoneBridge/MCP/Codex variables,
or production mounts. The runtime postflight additionally proves exact Docker
network membership and IP, backend route source, fixed private Relay URL,
and no public listener, or for production the outbound default route and exact
IPv4 `3448:3438` mapping. After all Gate audits, use the authoritative teardown
in `docs/deployment/TURKEY_EDGE_RUNBOOK.md`: stop and verify Gate Connector
before Gate Edge/Relay, revoke the `android-e2e` Node offline, and remove
`node.env`, its token, Gateway secret, and Gate Connector data only after the
audit evidence is complete. The same teardown then empties and verifies the
isolated Gate Relay DB/WAL/SHM and bootstrap roots and uninstalls the two
test-only Gate packages over USB ADB without screen operations. Switch to the
verified production-only host firewall and remove the cloud `27462` rule after
the isolated gate passes; preserve its evidence separately from production.

Before starting either fixed-IP stack, install the reviewed host firewall in
Gate mode:

```sh
sudo python3 deploy/turkey/private_relay_firewall.py \
  --install --gate-enabled
sudo /usr/local/sbin/codex-security-lockdown.sh \
  --verify --gate-enabled
```

After that live policy passes, install and enable the reviewed
`deploy/turkey/systemd/chebycodex-firewall.service` and install
`deploy/turkey/systemd/docker.service.d/10-chebycodex-firewall.conf` under the
matching `/etc/systemd/system` paths, then run `systemctl daemon-reload`. Do not
restart Docker just to activate the drop-in. The current policy is already
active; on every later boot or Docker start, Docker requires and starts after
the firewall service. Before Docker exists, the firewall safely creates
`DOCKER-USER`, puts the dispatcher first, and runs `--verify-pre-docker` over
the final-DROP policy. Docker then adds its `FORWARD` jump; its drop-in runs the
full `--verify` and fails startup unless both that hook and the
`DOCKER-USER` dispatcher are rule 1.
The hardened service creates its `0700` configuration directory and grants
write access only to `/etc/chebycodex-firewall`, so persisting the mode remains
compatible with `ProtectSystem=full`.

This replaces the stale host TCP `443` INPUT allow with dispatcher-backed A/B
policies. Public post-DNAT traffic is accepted only from the public interface
with original destination `27461` to production ingress Edge
`172.31.60.2:8443`, plus `27462` to the L2-separated temporary Gate ingress
Edge `172.31.63.2:8443` in Gate mode. Each Edge uses its distinct backend
address (`172.31.61.2` or `172.31.62.2`) to reach only its own Relay `:8080`;
Relay and Connector never join either ingress bridge. Existing OpenClaw,
PhoneBridge, outbound Connector HTTPS, and VPN flows remain explicit before
the final DROP. Dedicated non-masqueraded loopback publication subnets
`172.31.64.0/28` and `172.31.65.0/28` allow only original host-loopback ports
`18080/18081` to the matching Relay `:8080`, then drop both directions. Public
original TCP `80/443` is dropped before legacy allows and the full verifier
rejects matching Docker NAT entries; missing `ip6tables` fails unless host IPv6
is explicitly disabled globally. After closing Gate and cloud port `27462`, run
`sudo /usr/local/sbin/codex-security-lockdown.sh --production-only` and verify
the same mode; this also fences already-established Gate ingress/backend
connections before the general established-flow accept. The mode is persisted
for the reviewed fail-closed systemd boot unit.
See `docs/deployment/TURKEY_CONNECTOR_RUNBOOK.md` for acceptance and rollback.
