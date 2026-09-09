# Turkey fixed-IP Relay Edge runbook

## Authority and release status

This is the operator runbook for the current one-phone, one-Codex Relay
architecture. The historical `deploy/turkey/docker-compose.yml` direct
Gateway/Codex Edge and its Certbot workflow are retained only as audit evidence.
They must not be activated and must not be mixed with this procedure.

The current deployment inputs are:

- `deploy/relay-edge/docker-compose.yml` for production Relay/Edge and the
  isolated Gate profile;
- `deploy/turkey/docker-compose.connector.yml` for the production Connector;
- `deploy/turkey/docker-compose.connector-gate.yml` for the isolated fake
  Connector;
- `tools/gate/edge_gate.py` for the credential-safe public boundary gate.

Static validation or a successful image build is not production evidence. The
release remains **NO-GO** until the public, Gate, certificate-recovery, migration,
and signed-app gates in this document have fresh evidence.

## Fixed topology and ports

```text
Android production APK
        |
        | TLS/WSS 192.0.0.8:27461
        v
production Edge ingress 172.31.60.2
production Edge backend 172.31.61.2
        |
        | private HTTP/WS
        v
production Relay 172.31.61.3
        ^
        | private Node WS
production Connector 172.31.61.4

Gate APK -- TLS/WSS :27462 --> Gate Edge ingress 172.31.63.2
                                  Gate Edge backend 172.31.62.2
                                           |
                                           v
                                     Gate Relay 172.31.62.3
                                           ^
                                           |
                                fake Gate Connector 172.31.62.4
```

Production and Gate use separate non-internal ingress bridges
(`172.31.60.0/28` and `172.31.63.0/28`) and separate internal backend bridges
(`172.31.61.0/28` and `172.31.62.0/28`). Only Edge joins an ingress bridge.
Each Connector remains backend-only. Each Relay additionally joins its own
single-container, non-masqueraded publication bridge (`172.31.64.0/28` for
production, `172.31.65.0/28` for Gate) solely to materialize the host-loopback
publication; neither Relay joins an ingress bridge.

The required host/cloud policy is exact:

| Port | Policy |
| --- | --- |
| TCP `27461` | Production Relay Edge; open only after the production boundary gate is ready |
| TCP `27462` | Isolated Gate Edge; temporary, then close the cloud rule and stop Gate |
| TCP `18080` | Relay loopback for migration/host diagnostics; never public |
| TCP `18081` | Gate Relay loopback; never public |
| TCP `80` | Closed for this private-CA design; no ACME listener |
| TCP `443` | Closed for this product; do not disturb the existing VPN on UDP `443` |
| Existing SSH, VPN, OpenClaw `3438`, PhoneBridge `3448` | Preserve unchanged |

The current signed APK trusts a private Relay CA carried in its signed
`relay-trust-bundle.json`. Consequently this bundle has no Certbot, ACME,
Let's Encrypt, plaintext redirect, or standard-port listener. The Delivery Owner
selected this design after the user explicitly required product TCP `80` and
`443` to remain closed; that later requirement supersedes the earlier
Let's Encrypt IP-certificate proposal. Do not describe the staged offline leaf
check as Let's Encrypt renewal.

## Immutable inputs and host ownership

Install the reviewed checkout at `/opt/chebycodex-mobile`. The checkout, this
bundle, systemd unit sources, generated Nginx files, and every parent directory
must be root-owned and not writable by group or others before a root systemd
unit is enabled.

Create `/etc/chebycodex-relay-edge/relay-edge.env` from
`deploy/relay-edge/.env.example`. It is root-owned mode `0600` and contains only
non-secret paths, fixed addresses, resource limits, and exact immutable image
IDs. Never place Relay credentials, pairing material, a CA private key, or a leaf
private key in the environment file.

The Edge image must retain the exact reviewed patched Alpine packages:

- `c-ares=1.34.8-r0`;
- `curl=8.20.0-r0`;
- `libcurl=8.20.0-r0`;
- `libexpat=2.8.2-r0`.

Rebuild and rescan if the Dockerfile, base digest, package pins, Nginx files, or
any copied input changes. Activation requires zero unresolved HIGH/CRITICAL
vulnerabilities and zero embedded secrets for the exact immutable Relay and
Edge image IDs. A previous scan does not cover a rebuilt image.

Production must reuse the active R18 Relay bind
`/srv/chebycodex-r18/relay-data`; do not create an empty replacement. Verify it
is the exact mount of `chebycodex-r18-relay`, owned by `10001:10001`, and
private. Prepare only the new paths:

```sh
sudo test -d /srv/chebycodex-r18/relay-data
sudo install -d -m 0700 -o 10001 -g 10001 \
  /var/lib/chebycodex-relay-gate/data \
  /etc/chebycodex-relay/bootstrap \
  /etc/chebycodex-relay/gate-bootstrap
sudo install -d -m 0700 -o 101 -g 101 \
  /etc/chebycodex-relay-edge/tls \
  /etc/chebycodex-relay-edge/staging
sudo install -o root -g root -m 0600 \
  deploy/relay-edge/.env.example \
  /etc/chebycodex-relay-edge/relay-edge.env
```

Replace the image placeholders before use. The staging tree is UID/GID
`101:101`; `candidate/privkey.pem` is mode `0400`. The offline CA private key
never enters Turkey, Git, an APK, logs, or chat.

On Turkey, the following ownership check must print only
`0:0:600` and then no paths:

```sh
stat -c '%u:%g:%a' /etc/chebycodex-relay-edge/relay-edge.env
find /opt/chebycodex-mobile/deploy/relay-edge -xdev \
  \( ! -user root -o -perm /022 -o -type l \) -print
```

## Local and remote preflight

Run from the exact checkout:

```sh
python3 deploy/relay/validate_bundle.py
python3 deploy/relay-edge/validate_bundle.py
relay/.venv/bin/python -m pytest -q \
  relay/tests/test_admin.py \
  deploy/relay/tests/test_validate_bundle.py \
  deploy/relay-edge/tests/test_certificate_tools.py \
  deploy/relay-edge/tests/test_certificate_shell_recovery.py \
  tools/gate/test_edge_gate.py \
  tools/gate/test_certificate_failure_gate.py
docker build --pull=false --platform linux/amd64 \
  --file deploy/relay-edge/Dockerfile.edge \
  --tag local/chebycodex-relay-edge:reviewed .
```

Record the exact resulting image ID and its vulnerability/secret scan evidence.
On Turkey, load the environment without displaying it and run the host preflight:

```sh
sudo -i
cd /opt/chebycodex-mobile
set -a
. /etc/chebycodex-relay-edge/relay-edge.env
set +a
docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml config --quiet
systemd-analyze verify deploy/relay-edge/systemd/*.service \
  deploy/relay-edge/systemd/*.timer
deploy/relay-edge/preflight_host.py \
  --bind-ip "$CHEBY_BIND_IP" \
  --public-ip "$CHEBY_PUBLIC_IP" \
  --production-port "$CHEBY_RELAY_PUBLIC_PORT" \
  --gate-port "$CHEBY_RELAY_GATE_PORT"
```

Preflight must prove the fixed NAT bind address `192.168.0.43`, public identity
`192.0.0.8`, exact ports `27461/27462`, and no existing TCP listener on
`80`, `443`, `27461`, or `27462`. It never stops a conflicting service.
Before any Edge bootstrap, apply and verify the reviewed host firewall and
install the firewall service plus Docker drop-in exactly as specified in
`TURKEY_CONNECTOR_RUNBOOK.md`. Docker must require and start after the firewall;
the firewall must be ordered before Docker. This guarantees that Docker
restart-policy restoration cannot publish an Edge until the complete
`DOCKER-USER` final-DROP policy is active. The pre-Docker unit verifies the
preloaded chains; the Docker drop-in then requires the live `FORWARD` and
`DOCKER-USER` first-rule hooks in `ExecStartPost`. Installing the units does not
require restarting a currently running Docker daemon.

## Offline certificate preparation

Use the reviewed Relay Python environment on the offline operator machine:

```sh
relay/.venv/bin/python deploy/relay-edge/certificate_manager.py \
  --public-ip 192.0.0.8 --service-port 27461 \
  create-ca --ca-dir /secure/offline/relay-ca

relay/.venv/bin/python deploy/relay-edge/certificate_manager.py \
  --public-ip 192.0.0.8 --service-port 27461 \
  build-trust-bundle \
  --ca-certificate /secure/offline/relay-ca/ca.pem \
  --allowed-port 27461 \
  --output /secure/public/relay-trust-bundle.production.json

relay/.venv/bin/python deploy/relay-edge/certificate_manager.py \
  --public-ip 192.0.0.8 --service-port 27462 \
  build-trust-bundle \
  --ca-certificate /secure/offline/relay-ca/ca.pem \
  --allowed-port 27462 \
  --output /secure/public/relay-trust-bundle.gate.json

relay/.venv/bin/python deploy/relay-edge/certificate_manager.py \
  --public-ip 192.0.0.8 --service-port 27461 \
  issue --ca-dir /secure/offline/relay-ca \
  --candidate-dir /secure/transfer/candidate
```

Build the production, migration, and Gate APKs from the matching public trust
bundles before server activation. Transfer only the public production bundle and
the short-lived candidate to Turkey through an authenticated encrypted channel.
The expected Turkey staging layout is documented in
`deploy/relay-edge/README.md`.

## Lossless migration from the Quick Tunnel Relay

The old Relay and the new fixed-IP Relay use the same loopback port and must
never run concurrently against the same SQLite database. Initial fixed-IP
migration must use the exact already-reviewed Relay image ID and the same
`CHEBY_RELAY_DATA_DIR`. If delivery fixes require a new Relay image, its SQLite
forward/backward compatibility must pass against a disposable copy before
cutover; never discover schema compatibility on the only production database.

Before changing state:

1. Read-only inspect the old Relay container, immutable image ID, bind-mounted
   data directory, Quick Tunnel container, Connector, and current health.
2. Confirm the new Edge environment points to the reviewed migration Relay image
   and the exact existing `/srv/chebycodex-r18/relay-data` bind.
3. Back up the complete stopped Relay data directory, including
   `relay.sqlite3`, `relay.sqlite3-wal`, and `relay.sqlite3-shm` when present.
4. Preserve Connector, Gateway, Codex, and PhoneBridge state independently.
5. Keep the Quick Tunnel process and hostname unchanged through phone cutover.

The quiesced cutover is:

```sh
docker stop --time 30 chebycodex-r18-relay || {
  echo "Cannot stop the standalone production Relay" >&2
  exit 1
}
if [ "$(docker inspect --format '{{.State.Running}}' \
  chebycodex-r18-relay)" != "false" ]; then
  echo "Standalone production Relay did not reach stopped state" >&2
  exit 1
fi
loopback_listeners=$(ss -H -ltn 'sport = :18080') || {
  echo "Cannot inspect the production Relay loopback port" >&2
  exit 1
}
if [ -n "$loopback_listeners" ]; then
  echo "Production Relay loopback port 18080 is still occupied" >&2
  exit 1
fi

sudo install -d -o root -g root -m 0700 \
  /var/backups/chebycodex-relay/pre-fixed-edge
sudo test ! -e /var/backups/chebycodex-relay/pre-fixed-edge/data
sudo cp -a -- /srv/chebycodex-r18/relay-data \
  /var/backups/chebycodex-relay/pre-fixed-edge/data

deploy/relay-edge/bootstrap_edge.sh
```

The active R18 Relay is a standalone container, not a Compose service.
Do not substitute the historical `deploy/relay/docker-compose.yml` stop command:
it does not stop `chebycodex-r18-relay`. The stopped-state and empty-listener
assertions above are mandatory before the backup. `bootstrap_edge.sh` repeats
the read-only `18080` listener and active Docker-publication checks immediately
before activation and fails before starting Docker services if another process
or container owns the port.

`CHEBY_RELAY_LEGACY_HOST` must be exactly
`chemicals-submission-capital-indianapolis.trycloudflare.com` during this window.
The existing cloudflared process continues forwarding to loopback `18080`; after
the brief single-worker handoff, the old phone path and fixed-IP Edge reach the
same Relay database. Edge itself accepts only
`192.0.0.8:27461`.

If activation fails before phone migration, stop only the new Edge/Relay and
restart the old Relay image against the current database. Do not restore the
backup as a routine rollback and do not run both Relay workers:

```sh
docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  stop edge relay || {
    echo "Cannot stop the replacement Relay stack; old Relay remains stopped" >&2
    exit 1
  }
new_stack_running=$(docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  ps --status running --quiet edge relay) || {
    echo "Cannot verify the replacement Relay stopped; old Relay remains stopped" >&2
    exit 1
  }
if [ -n "$new_stack_running" ]; then
  echo "Replacement Relay stack is still running; old Relay remains stopped" >&2
  exit 1
fi
docker start chebycodex-r18-relay || {
  echo "Cannot restart the standalone production Relay" >&2
  exit 1
}
if [ "$(docker inspect --format '{{.State.Running}}' \
  chebycodex-r18-relay)" != "true" ]; then
  echo "Standalone production Relay did not restart" >&2
  exit 1
fi
```

Always stop and verify the new project first, including after a partial
`bootstrap_edge.sh` failure, before restarting the standalone old container.
Keep the old container present throughout the rollback window. Never use
`docker compose down`, remove either Relay container, or restore the Relay
database merely to switch images.

After the migration APK authenticates the fixed origin and the final signed APK
passes fresh Turn/restart/network gates, stop the Quick Tunnel, clear
`CHEBY_RELAY_LEGACY_HOST`, and recreate only the production Relay so the legacy
Host is no longer accepted. Never roll the Relay database backward after new
deliveries have been accepted.

## Production and isolated Gate activation

Production bootstrap is initial-activation only and intentionally refuses an
occupied `27461` or `27462`:

```sh
deploy/relay-edge/bootstrap_edge.sh
python3 deploy/relay-edge/verify_runtime_networks.py \
  --bind-ip "$CHEBY_BIND_IP" \
  --production-port "$CHEBY_RELAY_PUBLIC_PORT"
```

Start Gate only after production is healthy:

```sh
set -a
. /etc/chebycodex-relay-edge/relay-edge.env || {
  echo "Cannot load the Relay Edge environment" >&2
  exit 1
}
set +a
set -eu
gate_stack_running=$(docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile gate ps --status running --quiet relay-gate edge-gate) || {
    echo "Cannot verify the Gate stack is stopped" >&2
    exit 1
  }
if [ -n "$gate_stack_running" ]; then
  echo "Gate stack is already running" >&2
  exit 1
fi
gate_data_entry=$(find "$CHEBY_RELAY_GATE_DATA_DIR" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot inspect the Gate Relay data directory" >&2
    exit 1
  }
if [ -n "$gate_data_entry" ]; then
  echo "Gate Relay data directory is not empty" >&2
  exit 1
fi
gate_bootstrap_entry=$(find /etc/chebycodex-relay/gate-bootstrap \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot inspect the Gate bootstrap directory" >&2
    exit 1
  }
if [ -n "$gate_bootstrap_entry" ]; then
  echo "Gate bootstrap directory is not empty" >&2
  exit 1
fi

set +x
for gate_role in edge-full reload-continuity android-e2e; do
  install -d -m 0700 -o 10001 -g 10001 \
    "/etc/chebycodex-relay/gate-bootstrap/$gate_role" || {
      echo "Cannot create a private Gate role directory" >&2
      exit 1
    }
  if ! CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR" \
    CHEBY_RELAY_BOOTSTRAP_DIR=/etc/chebycodex-relay/gate-bootstrap \
    docker compose \
      --env-file /etc/chebycodex-relay-edge/relay-edge.env \
      --file deploy/relay-edge/docker-compose.yml \
      --profile admin run --rm --no-deps relay-admin \
      bootstrap \
      --database /data/relay.sqlite3 \
      --relay-origin https://192.0.0.8:27462 \
      --device-format cxc1 \
      --device-output "/bootstrap/$gate_role/device.cxc1" \
      --node-output "/bootstrap/$gate_role/node-bootstrap.json"; then
    echo "A Gate role bootstrap failed; discard the entire Gate bootstrap set" >&2
    exit 1
  fi
done

python3 - /etc/chebycodex-relay/gate-bootstrap <<'PY'
import base64
import json
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
roles = ("edge-full", "reload-continuity", "android-e2e")
assistant_pattern = re.compile(r"asst_[A-Za-z0-9_-]{22}")
node_pattern = re.compile(r"node_[A-Za-z0-9_-]{22}")
identities = {}
for role in roles:
    device_path = root / role / "device.cxc1"
    node_path = root / role / "node-bootstrap.json"
    documents = []
    for path, limit in ((device_path, 1025), (node_path, 4097)):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != 10001
            ):
                raise SystemExit("Gate bootstrap privacy validation failed")
            raw = os.read(descriptor, limit)
            if os.read(descriptor, 1):
                raise SystemExit("Gate bootstrap size validation failed")
        finally:
            os.close(descriptor)
        documents.append(raw)
    envelope = documents[0]
    if envelope.endswith(b"\n"):
        envelope = envelope[:-1]
    if b"\n" in envelope or b"\r" in envelope or not envelope.startswith(b"CXC1."):
        raise SystemExit("Gate CXC1 envelope validation failed")
    encoded = envelope.split(b".", 1)[1]
    try:
        pairing = json.loads(
            base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        )
        node = json.loads(documents[1])
    except Exception:
        raise SystemExit("Gate bootstrap structure validation failed") from None
    if (
        set(pairing) != {"v", "assistantId", "pairingSecret"}
        or pairing["v"] != 1
        or set(node) != {"v", "relayOrigin", "assistantId", "nodeId", "nodeToken"}
        or node["v"] != 1
        or not isinstance(pairing["assistantId"], str)
        or not isinstance(node["assistantId"], str)
        or not isinstance(node["nodeId"], str)
        or pairing["assistantId"] != node["assistantId"]
        or assistant_pattern.fullmatch(pairing["assistantId"]) is None
        or node_pattern.fullmatch(node["nodeId"]) is None
    ):
        raise SystemExit("Gate public identity pairing validation failed")
    identities[role] = {
        "assistantId": pairing["assistantId"],
        "nodeId": node["nodeId"],
    }
if len({value["assistantId"] for value in identities.values()}) != len(roles):
    raise SystemExit("Gate role assistant identities are not distinct")
manifest = root / "public-identities.json"
descriptor = os.open(
    manifest,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
with os.fdopen(descriptor, "w", encoding="ascii") as output:
    json.dump({"v": 1, "roles": identities}, output, separators=(",", ":"))
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

gate_public_ids=/etc/chebycodex-relay/gate-bootstrap/public-identities.json
edge_full_node_id=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["roles"]["edge-full"]["nodeId"])' \
  "$gate_public_ids") || {
    echo "Cannot read the public edge-full Node identity" >&2
    exit 1
  }
reload_node_id=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["roles"]["reload-continuity"]["nodeId"])' \
  "$gate_public_ids") || {
    echo "Cannot read the public reload-continuity Node identity" >&2
    exit 1
  }
for unused_node_id in "$edge_full_node_id" "$reload_node_id"; do
  if ! CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR" \
    CHEBY_RELAY_BOOTSTRAP_DIR=/etc/chebycodex-relay/gate-bootstrap \
    docker compose \
      --env-file /etc/chebycodex-relay-edge/relay-edge.env \
      --file deploy/relay-edge/docker-compose.yml \
      --profile admin run --rm --no-deps relay-admin \
      revoke --database /data/relay.sqlite3 \
      --principal-id "$unused_node_id"; then
    echo "Cannot revoke an unused Gate Node credential" >&2
    exit 1
  fi
done
unset edge_full_node_id reload_node_id unused_node_id
rm -f -- \
  /etc/chebycodex-relay/gate-bootstrap/edge-full/node-bootstrap.json \
  /etc/chebycodex-relay/gate-bootstrap/reload-continuity/node-bootstrap.json

install -d -m 0700 -o 10002 -g 10002 \
  /var/lib/chebycodex-gate-connector/data
install -d -m 0700 -o root -g root \
  /etc/chebycodex-gate-connector \
  /etc/chebycodex-gate-connector/secrets
test "$(stat -c '%u:%g:%a' \
  /etc/chebycodex-gate-connector/operator.env)" = "0:0:600"
python3 deploy/turkey/install_node_bootstrap.py \
  --bootstrap /etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json \
  --env-output /etc/chebycodex-gate-connector/node.env \
  --token-output /etc/chebycodex-gate-connector/secrets/node_token \
  --gateway-secret-output /etc/chebycodex-gate-connector/secrets/gateway_internal_secret \
  --relay-url ws://172.31.62.3:8080/relay/v1/node
rm -f -- \
  /etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json

docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile gate up --detach relay-gate edge-gate || {
    echo "Cannot start the Gate Relay stack" >&2
    exit 1
  }
python3 deploy/relay-edge/verify_runtime_networks.py \
  --bind-ip "$CHEBY_BIND_IP" \
  --production-port "$CHEBY_RELAY_PUBLIC_PORT" \
  --gate-port "$CHEBY_RELAY_GATE_PORT" \
  --include-gate

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
python3 deploy/turkey/verify_connector_runtime.py \
  --profile gate \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env
```

The three bootstrap calls run serially against the same initially empty,
stopped Gate database. Each call creates a different assistant and one-use
CXC1. The silent validator verifies mode/owner, matches only the public
`assistantId` in each CXC1/Node pair, proves all three assistants differ, and
writes only public IDs to `public-identities.json`; it never prints a pairing
secret or Node token. The `edge-full` and `reload-continuity` Node credentials
are revoked before Gate starts and their raw JSON files are removed. Only the
`android-e2e` Node is installed into Gate Connector; its raw JSON is removed
immediately after installation.

Every Compose invocation includes the explicit Relay Edge env file, and every
Relay admin invocation includes both invocation-scoped Gate mount overrides.
Every Gate Connector Compose invocation includes both its explicit
`operator.env` and `node.env`. Gate-role admin commands must not use the
production `/srv/chebycodex-r18/relay-data` or run while Gate Relay is active.
The only live production admin operations permitted in this Runbook are the
three purpose-built ephemeral-continuity commands below; their store guards
cannot select the phone assistant. If any bootstrap, validation, revocation,
installation, transfer, or enrollment result is failed or unknown, stop: do
not reuse a CXC1 and do not continue from a partial set. With every Gate service
stopped, remove the entire isolated Gate Relay database, Gate Connector
state/secrets, Gate APK state, all three role directories and
`public-identities.json`, then regenerate all three roles from an empty Gate
database. Any unguarded production database change during this flow is a
release-blocking failure.

Gate has a separate Relay database, network, pairing identity, Node identity,
Connector state, and `.gate` APK package. It must not mount or reuse production
state. Its acknowledged delivery retention is fixed at 43,200 seconds so the
final cross-layer audit remains possible after the eight-hour Gate soak;
production Relay retains the normal one-hour bound. Do not accept
`HostConfig.PortBindings` as sufficient evidence:
postflight must also find the exact active `NetworkSettings.Ports` publication
and Docker DNAT to production ingress `172.31.60.2:8443` (and Gate ingress
`172.31.63.2:8443` during the Gate window), plus
`127.0.0.1:18080 -> 172.31.64.2:8080` (and Gate
`127.0.0.1:18081 -> 172.31.65.2:8080`). The postflight rejects source-limited
or extra-match DNAT variants and must reach each loopback `/healthz`. Run the
public boundary probes:

```sh
python tools/gate/edge_gate.py \
  --ip 192.0.0.8 --port 27461 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --boundary-only

python tools/gate/edge_gate.py \
  --ip 192.0.0.8 --port 27462 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --pairing-bootstrap /absolute/private/edge-full/device.cxc1 \
  --timeout 60
```

Both successful reports must have `fail=0 skip=0`. The boundary gate tests every
forwarding header independently, not as one masked aggregate. The full Gate also
proves enrollment, DeviceProofV1 failures, authenticated ready, the exact
12 MiB + 1 byte application close `4409`, and the next byte's transport close
`1009`. Its explicit 60-second per-operation timeout prevents a slow public
12 MiB uplink from being misclassified as a Relay failure; ordinary boundary
checks keep the 10-second default. Delete every local, Turkey, and transfer copy
of the `edge-full` CXC1
after the first enrollment attempt, including timeout or unknown outcome; it is
never a retry credential.

Keep Gate running. Complete the `reload-continuity` proof below before
provisioning `android-e2e`; do not stop Gate or enable certificate timers yet.

## Certificate rotation, recovery, and monitoring

Every 12 hours `rotate_certificate.sh` imports only an already staged,
offline-signed candidate. Hourly `monitor_certificate.sh` verifies the actual
public port, exact leaf fingerprint and SPKI, trusted CA, IP SAN, TLS version,
and a 48-hour lifetime floor. Neither script defaults to port `443`.

After reload and public verification succeed, the no-network `cert-consumer`
mounts `/tls` read-only and `/staging` writable. It requires the five candidate
files to be byte-for-byte identical to the active release with distinct
inodes, atomically renames `candidate` to `.candidate-consumed`, fsyncs the
staging directory, and removes only that fixed tombstone. The next invocation
resumes a crash-interrupted tombstone cleanup before inspecting a new
candidate. A cleanup failure makes the one-shot job fail and alert, but never
rolls back or stops a publicly verified healthy Edge. Do not manually remove a
candidate or tombstone.

Rotation and monitoring share a bounded host lock. A successful rotation must
additionally prove:

- one already established authenticated WSS connection continues exchanging
  ping/pong across `nginx -s reload`;
- a new TLS/WSS connection receives the newly activated leaf;
- production and a running Gate endpoint both match the same activated leaf;
- no plaintext or system-trust fallback appeared.

Run one isolated `27462` connection and one ephemeral authenticated production
`27461` connection across the same reload. Before it, install only the reviewed
one-shot rotation service on Turkey; do not install or enable either timer yet.
The unit explicitly loads
`/etc/chebycodex-relay-edge/relay-edge.env` and takes the shared lock:

```sh
sudo install -o root -g root -m 0644 \
  deploy/relay-edge/systemd/chebycodex-relay-cert-rotate.service \
  /etc/systemd/system/chebycodex-relay-cert-rotate.service
sudo systemctl daemon-reload
```

On Turkey, create the single-use production continuity identity. This command
uses one short SQLite WAL transaction against the live production Relay. It
creates a different assistant, a Node credential already revoked at birth, one
CXC1, and one file containing only public IDs. It never reads, rotates, or
updates the phone assistant:

```sh
set -a
. /etc/chebycodex-relay-edge/relay-edge.env || {
  echo "Cannot load the Relay Edge environment" >&2
  exit 1
}
set +a
set -eu
set +x
production_continuity_root=/etc/chebycodex-relay/production-continuity
test "$CHEBY_RELAY_DATA_DIR" = /srv/chebycodex-r18/relay-data
install -d -m 0700 -o 10001 -g 10001 "$production_continuity_root"
if [ "$(realpath -e -- "$production_continuity_root")" != \
  "$production_continuity_root" ] \
  || [ "$(stat -c '%u:%g:%a' "$production_continuity_root")" != \
  "10001:10001:700" ]; then
  echo "Production continuity root failed its exact path or owner-mode guard" >&2
  exit 1
fi
production_continuity_entry=$(find "$production_continuity_root" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot inspect the production continuity directory" >&2
    exit 1
  }
if [ -n "$production_continuity_entry" ]; then
  echo "Production continuity directory is not empty" >&2
  exit 1
fi
CHEBY_RELAY_BOOTSTRAP_DIR="$production_continuity_root" \
docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile admin run --rm --no-deps relay-admin \
  bootstrap-ephemeral-continuity \
  --database /data/relay.sqlite3 \
  --device-output /bootstrap/device.cxc1 \
  --metadata-output /bootstrap/metadata.json
for production_continuity_file in device.cxc1 metadata.json; do
  if [ "$(stat -c '%u:%g:%a' \
    "$production_continuity_root/$production_continuity_file")" != \
    "10001:10001:600" ]; then
    echo "Production continuity artifact failed its owner-mode guard" >&2
    exit 1
  fi
done
```

Both files are current-UID, regular mode `0600`. The metadata has exactly
`v`, `assistantId`, `nodeId`, and the fixed
`production-reload-continuity` purpose; it contains no token or secret.
Transfer only the CXC1 over the reviewed authenticated channel without printing
its contents; keep `metadata.json` on Turkey for guarded cleanup. If the
bootstrap outcome is unknown, use that metadata—written durably before the
database transaction—to run the guarded revoke immediately; never retry the
CXC1.

On the external runner, create a fresh private signal directory, place the
production CXC1 there as `production-device.cxc1` with mode `0600`, and derive
the public expected digest from the reviewed candidate manifest:

```sh
SIGNAL_DIR=$(mktemp -d /tmp/cheby-relay-reload.XXXXXX)
chmod 0700 "$SIGNAL_DIR"
EXPECTED_NEW_LEAF_SHA256=$(
  python3 -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["leafCertificateSha256"])' \
    /absolute/path/to/candidate/release.json
)

python tools/gate/edge_gate.py \
  --ip 192.0.0.8 --port 27462 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --reload-continuity-bootstrap /absolute/private/reload-continuity/device.cxc1 \
  --expected-new-leaf-sha256 "$EXPECTED_NEW_LEAF_SHA256" \
  --ready-signal "$SIGNAL_DIR/gate-ready" \
  --reloaded-signal "$SIGNAL_DIR/gate-reloaded" \
  --manifest "$SIGNAL_DIR/gate-continuity.json" \
  --reload-wait-timeout 600

python tools/gate/edge_gate.py \
  --ip 192.0.0.8 --port 27461 \
  --ca-cert /absolute/path/to/relay-ca.pem \
  --reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1" \
  --expected-new-leaf-sha256 "$EXPECTED_NEW_LEAF_SHA256" \
  --ready-signal "$SIGNAL_DIR/production-ready" \
  --reloaded-signal "$SIGNAL_DIR/production-reloaded" \
  --cleanup-ready-signal "$SIGNAL_DIR/production-cleanup-ready" \
  --revoked-signal "$SIGNAL_DIR/production-revoked" \
  --manifest "$SIGNAL_DIR/production-continuity.json" \
  --reload-wait-timeout 600
```

Run those processes in separate terminals. In a third terminal, wait until
both ready files exist with mode `0600`. Then invoke the Turkey rotation
through the already authenticated operator channel. This starts only the
one-shot service with its explicit EnvironmentFile; no timer is active. Only
after the remote command returns success, create both completion files:

```sh
python3 -c \
  'import os,stat,sys; ss=[os.stat(p,follow_symlinks=False) for p in sys.argv[1:]]; assert all(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==os.getuid() for s in ss)' \
  "$SIGNAL_DIR/gate-ready" "$SIGNAL_DIR/production-ready"
TURKEY_SSH_HOST=replace-with-reviewed-ssh-alias
ssh "$TURKEY_SSH_HOST" \
  'sudo systemctl start chebycodex-relay-cert-rotate.service'
(umask 077
 printf 'RELOADED\n' > "$SIGNAL_DIR/gate-reloaded"
 printf 'RELOADED\n' > "$SIGNAL_DIR/production-reloaded")
```

The production process next creates `production-cleanup-ready`; it then waits
only 60 seconds. Verify that signal on the external runner:

```sh
python3 -c \
  'import os,stat,sys; p=sys.argv[1]; s=os.stat(p,follow_symlinks=False); assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==os.getuid() and open(p,"rb").read()==b"REVOKE\n"' \
  "$SIGNAL_DIR/production-cleanup-ready"
```

On Turkey, revoke the exact metadata-bound ephemeral identity:

```sh
CHEBY_RELAY_BOOTSTRAP_DIR="$production_continuity_root" \
docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile admin run --rm --no-deps relay-admin \
  revoke-ephemeral-continuity \
  --database /data/relay.sqlite3 \
  --metadata /bootstrap/metadata.json
```

Only after that command succeeds, create the final local signal:

```sh
(umask 077; printf 'REVOKED\n' > "$SIGNAL_DIR/production-revoked")
```

The production gate now sends an application ping on the original `27461`
socket and requires a Relay `4401` close. Both continuity processes must exit
zero and both new immutable mode-`0600` manifests must say `PASS`. The
production manifest must have `existingAuthenticatedWss`,
`preReloadPing`, `postReloadPing`, `newConnectionExpectedLeaf`, and
`ephemeralIdentityRevoked` all true; it records no credentials or principal
IDs. Then remove the isolated production rows and verify the metadata/CXC1
files are gone:

```sh
CHEBY_RELAY_BOOTSTRAP_DIR="$production_continuity_root" \
docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile admin run --rm --no-deps relay-admin \
  purge-ephemeral-continuity \
  --database /data/relay.sqlite3 \
  --metadata /bootstrap/metadata.json
rm -f -- \
  "$production_continuity_root/device.cxc1" \
  "$production_continuity_root/metadata.json"
production_continuity_entry=$(find "$production_continuity_root" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot verify production continuity cleanup" >&2
    exit 1
  }
if [ -n "$production_continuity_entry" ]; then
  echo "Production continuity residue remains" >&2
  exit 1
fi
```

On any failure, timeout, or unknown outcome, run the guarded revoke before
purge and retain the FAIL manifest; never signal `REVOKED` before the admin
command succeeds. Delete every local, Turkey, and transfer CXC1 copy after its
first attempt. `READY\n`, `RELOADED\n`, `REVOKE\n`, and `REVOKED\n` are the
only signal payloads.

Before this live proof, run the fixed executable negative suite:

```sh
mkdir -m 0700 /absolute/private/certificate-evidence
relay/.venv/bin/python tools/gate/certificate_failure_gate.py \
  --python "$(pwd)/relay/.venv/bin/python" \
  --manifest \
    /absolute/private/certificate-evidence/negative-certificate.json
```

Its immutable mode-`0600` manifest must report 15/15 zero-skip tests, matching
pre/post-test source hashes, and PASS. The gate clears inherited pytest
injection, disables plugin autoload, and forces empty addopts plus strict xfail.
The fixed cases cover wrong private key, wrong IP SAN, wrong chain, missing or
invalid `previous`, rollback failure, Nginx validation failure, reload failure,
and public fingerprint mismatch. A failed activation must restore and publicly
verify `previous`; if that cannot be proven, `current` is deactivated and both
Edge listeners stop fail-closed.

Only after reload continuity passes, provision the Gate APK with
`/absolute/private/android-e2e/device.cxc1`, following the private stdin flow in
`tools/gate/README.md`. Its assistant was already matched to the sole installed
Gate Connector Node. The phone deletes its invitation before sending the
enrollment request. Delete every remaining local, Turkey, and transfer copy
after that first attempt. On timeout or unknown outcome, do not retry or clear
only the App: rebuild all three Gate roles and all isolated Gate state. Keep
Gate running through the Android headless, load, fault, and soak evidence.

After Full Edge, reload-continuity, and Android evidence have all passed, stop
Gate and verify it is down:

```sh
set -a
. /etc/chebycodex-relay-edge/relay-edge.env || {
  echo "Cannot load the Relay Edge environment" >&2
  exit 1
}
set +a
set -eu
set +x

docker compose \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env \
  --file deploy/turkey/docker-compose.connector-gate.yml \
  stop gate-connector || {
    echo "Cannot stop the Gate Connector" >&2
    exit 1
  }
gate_connector_running=$(docker compose \
  --env-file /etc/chebycodex-gate-connector/operator.env \
  --env-file /etc/chebycodex-gate-connector/node.env \
  --file deploy/turkey/docker-compose.connector-gate.yml \
  ps --status running --quiet gate-connector) || {
    echo "Cannot verify the Gate Connector stopped" >&2
    exit 1
  }
if [ -n "$gate_connector_running" ]; then
  echo "Gate Connector is still running" >&2
  exit 1
fi

docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile gate stop edge-gate relay-gate || {
    echo "Cannot stop the Gate Relay stack" >&2
    exit 1
  }
gate_stack_running=$(docker compose \
  --env-file /etc/chebycodex-relay-edge/relay-edge.env \
  --file deploy/relay-edge/docker-compose.yml \
  --profile gate ps --status running --quiet relay-gate edge-gate) || {
    echo "Cannot verify the Gate stack stopped" >&2
    exit 1
  }
if [ -n "$gate_stack_running" ]; then
  echo "Gate Relay stack is still running" >&2
  exit 1
fi

gate_public_ids=/etc/chebycodex-relay/gate-bootstrap/public-identities.json
android_node_id=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["roles"]["android-e2e"]["nodeId"])' \
  "$gate_public_ids") || {
    echo "Cannot read the public android-e2e Node identity" >&2
    exit 1
  }
if ! CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR" \
  CHEBY_RELAY_BOOTSTRAP_DIR=/etc/chebycodex-relay/gate-bootstrap \
  docker compose \
    --env-file /etc/chebycodex-relay-edge/relay-edge.env \
    --file deploy/relay-edge/docker-compose.yml \
    --profile admin run --rm --no-deps relay-admin \
    revoke --database /data/relay.sqlite3 \
    --principal-id "$android_node_id"; then
  echo "Cannot revoke the android-e2e Node credential" >&2
  exit 1
fi
unset android_node_id

rm -f -- \
  /etc/chebycodex-gate-connector/node.env \
  /etc/chebycodex-gate-connector/secrets/node_token \
  /etc/chebycodex-gate-connector/secrets/gateway_internal_secret
gate_connector_data=/var/lib/chebycodex-gate-connector/data
if [ "$(realpath -e -- "$gate_connector_data")" != "$gate_connector_data" ]; then
  echo "Gate Connector data path failed its exact realpath guard" >&2
  exit 1
fi
find "$gate_connector_data" -xdev -mindepth 1 -delete || {
  echo "Cannot clear Gate Connector data" >&2
  exit 1
}
gate_connector_data_entry=$(find "$gate_connector_data" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot verify Gate Connector data cleanup" >&2
    exit 1
  }
if [ -n "$gate_connector_data_entry" ] \
  || [ -e /etc/chebycodex-gate-connector/node.env ] \
  || [ -e /etc/chebycodex-gate-connector/secrets/node_token ] \
  || [ -e /etc/chebycodex-gate-connector/secrets/gateway_internal_secret ]; then
  echo "Gate Connector credential or data residue remains" >&2
  exit 1
fi

gate_relay_data=/var/lib/chebycodex-relay-gate/data
gate_bootstrap=/etc/chebycodex-relay/gate-bootstrap
if [ "${CHEBY_RELAY_GATE_DATA_DIR:-}" != "$gate_relay_data" ] \
  || [ "$(realpath -e -- "$gate_relay_data")" != "$gate_relay_data" ] \
  || [ "$(realpath -e -- "$gate_bootstrap")" != "$gate_bootstrap" ]; then
  echo "Gate Relay cleanup path failed its exact realpath guard" >&2
  exit 1
fi
find "$gate_relay_data" -xdev -mindepth 1 -delete || {
  echo "Cannot clear the Gate Relay database directory" >&2
  exit 1
}
find "$gate_bootstrap" -xdev -mindepth 1 -delete || {
  echo "Cannot clear Gate bootstrap roles and public identities" >&2
  exit 1
}
gate_relay_data_entry=$(find "$gate_relay_data" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot verify the Gate Relay data directory" >&2
    exit 1
  }
gate_bootstrap_entry=$(find "$gate_bootstrap" \
  -mindepth 1 -maxdepth 1 -print -quit) || {
    echo "Cannot verify the Gate bootstrap directory" >&2
    exit 1
  }
if [ -n "$gate_relay_data_entry" ] \
  || [ -n "$gate_bootstrap_entry" ] \
  || [ "$(stat -c '%u:%g:%a' "$gate_relay_data")" != "10001:10001:700" ] \
  || [ "$(stat -c '%u:%g:%a' "$gate_bootstrap")" != "10001:10001:700" ]; then
  echo "Gate Relay or bootstrap residue remains; the empty-state preflight would fail" >&2
  exit 1
fi
```

The deletion above is destructive only to the isolated Gate state and occurs
after all sanitized manifests and cross-layer audits have been captured. Do
not archive the active Gate SQLite database by default. If a raw database is
required for incident evidence, copy it before deletion into a root-only,
encrypted evidence archive, verify that archive, and still remove the active
DB/WAL/SHM files before the next run.

On the Mac, remove the two test-only packages after all Android evidence is
captured. This destroys the Gate session, app-private enrollment state, and
package-scoped keys without launching an Activity or operating the screen:

```sh
set -eu
: "${ADB:?set ADB to the absolute platform-tools adb path}"
: "${SERIAL:?set SERIAL to the authorized USB device serial}"
test -x "$ADB"
test "$("$ADB" -s "$SERIAL" get-state)" = device
for gate_package in \
  com.cheby.codex.mobile.gate.test \
  com.cheby.codex.mobile.gate
do
  "$ADB" -s "$SERIAL" shell am force-stop "$gate_package"
  gate_package_paths=$("$ADB" -s "$SERIAL" shell pm path "$gate_package") || {
    echo "Cannot inspect a Gate package" >&2
    exit 1
  }
  case "$gate_package_paths" in
    "") ;;
    package:*)
      "$ADB" -s "$SERIAL" uninstall "$gate_package" >/dev/null || {
        echo "Cannot uninstall a Gate package" >&2
        exit 1
      }
      ;;
    *)
      echo "Unexpected Gate package inspection result" >&2
      exit 1
      ;;
  esac
  gate_package_paths=$("$ADB" -s "$SERIAL" shell pm path "$gate_package") || {
    echo "Cannot verify Gate package removal" >&2
    exit 1
  }
  if [ -n "$gate_package_paths" ] \
    || "$ADB" -s "$SERIAL" shell pidof "$gate_package" >/dev/null 2>&1; then
    echo "Gate package state or process remains" >&2
    exit 1
  fi
done
test "$("$ADB" -s "$SERIAL" get-state)" = device
unset gate_package gate_package_paths
```

On Turkey, immediately fence Gate traffic with the production-only firewall:

```sh
sudo /usr/local/sbin/codex-security-lockdown.sh --production-only
sudo /usr/local/sbin/codex-security-lockdown.sh \
  --verify --production-only
```

Then close the Huawei Cloud TCP `27462` rule and verify that port from an
external network. Production TCP `27461` remains reachable; TCP `80`, `443`,
and `27462` are unreachable. A later Gate run must begin with both the Gate
Relay data directory and Gate bootstrap directory empty; the start preflight
above is the executable verification.
Only now install the complete reviewed unit set and enable the timers:

```sh
sudo install -o root -g root -m 0644 \
  deploy/relay-edge/systemd/*.service \
  deploy/relay-edge/systemd/*.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  chebycodex-relay-cert-rotate.timer \
  chebycodex-relay-cert-monitor.timer
```

## Final release gates

Production is ready only when all are fresh and associated with immutable
artifacts:

1. Relay Edge and Connector static validators pass.
2. Exact Relay, Edge, and Connector images have zero unresolved
   HIGH/CRITICAL vulnerabilities and zero secrets.
3. Huawei rules expose production `27461`, expose Gate `27462` only during its
   window, and do not add product TCP `80/443`.
4. Wrong Host/port, duplicate Host, each forged forwarding header, query token,
   Origin, unknown path, public Node WSS, missing Authorization/PoP, and oversized
   frame all fail as specified.
5. Gate APK and fake Connector pass deterministic same-Thread recovery and
   cross-Thread isolation with no production state.
6. The signed production APK returns a fresh real Codex result through `27461`
   and proves PhoneBridge status, screenshot, and one harmless phone action.
7. Relay, Edge, Connector, Wi-Fi, and app restart/recovery gates show no duplicate
   Turn, no lost message, no cross-thread delivery, and no orphan queue item.
8. Certificate reload continuity and forced rollback/fail-closed evidence pass.
9. Quick Tunnel is removed only after fixed-IP acceptance, and the legacy Host is
   then removed from Relay.
10. An independent reviewer reports zero P0/P1 findings.

Do not call a static pass, a boundary-only pass, or an AI text response alone
“production ready.”
