# Relay-only fixed-IP Edge

This bundle is the single-phone, single-Codex public rendezvous boundary. The
public identity is `192.0.0.8`; the production APK may connect only to
`https://192.0.0.8:27461`. An independently signed Gate APK may connect
only to port `27462`.

The ECS uses NAT: its local address is `192.168.0.43`, while the certificate and
HTTP Host identity remain `192.0.0.8`. Docker must bind the local address,
never the EIP. `preflight_host.py` verifies the configured bind address is
assigned locally and fails if TCP 80, TCP 443, 27461, or 27462 is already
listening.

TCP 80 and TCP 443 are never published by this bundle. There is no Certbot,
ACME, Let's Encrypt, cleartext redirect, or public bootstrap listener.

## Trust model

The signed APK contains `relay-trust-bundle.json`, not a TOFU value. Its schema
is:

```json
{
  "version": 1,
  "publicIp": "192.0.0.8",
  "servicePorts": [27461],
  "authorities": [
    {
      "authorityId": "ca_...",
      "certificateDerBase64": "...",
      "certificateSha256": "...",
      "spkiSha256": "...",
      "spkiPin": "sha256/..."
    }
  ]
}
```

Production has exactly `[27461]`; Gate has exactly `[27462]`. Each bundle
contains one current private CA and may temporarily contain one next CA during
rotation. Android must build exact trust anchors from this signed asset, require
IP endpoint identification, and reject user CAs, system CAs, TOFU, other ports,
and any chain not ending at one listed CA.

`certificate_manager.py` creates the private CA and signs IP-SAN leaf
certificates. Run CA creation and issuance only on an offline, operator-
controlled workstation. `ca-key.pem` is mode `0400`; it must never be copied to
Turkey, Git, logs, chat, or an APK. Turkey receives only:

- the public production trust bundle;
- a short-lived leaf candidate (`fullchain.pem`, `leaf.pem`, `ca.pem`,
  `privkey.pem`, and `release.json`).

The server importer has no network. It verifies strict file permissions, exact
IP SAN, CA/leaf constraints, signatures, key correspondence, expiry floor,
manifest hashes, and that the signing CA is already present in the APK trust
bundle. Releases are immutable and addressed by the leaf SHA-256. `current` and
`previous` are atomically managed relative symlinks, and file, release-directory,
and pointer renames are directory-fsynced. If power is lost after an immutable
release directory is durable but before `current` changes, activation resumes
only when every byte of that release matches the staged candidate.

## Offline generation

Use the Relay Python environment, which contains the reviewed Cryptography
version. The following paths are examples outside Git:

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

The default leaf validity is 720 hours. The CA remains offline; there is no
impossible claim of unattended signing. Every 12 hours Turkey checks for an
already staged candidate and activates it if present. With no candidate it
verifies the current public endpoint. The hourly monitor verifies exact leaf
fingerprint, leaf SPKI, signing CA, IP SAN, TLS version, and remaining lifetime.
Crossing the 48-hour floor is a NO-GO and stops Edge.

For CA rotation, generate the next CA offline, build production and Gate bundles
containing current plus next, ship and install signed APKs first, then install
the same public production bundle on Turkey and activate a next-CA leaf. Retain
the prior APK CA and server release through the rollback window. The installer
refuses a public bundle that would distrust the active leaf.

The committed files under `tests/fixtures/` are public test CAs only. They
define the cross-platform schema and are not production trust material.

## Host preparation and activation

Build and scan immutable Relay and Edge images first. Put their exact image IDs
in `/etc/chebycodex-relay-edge/relay-edge.env`. Production migration reuses the
active R18 Relay bind `/srv/chebycodex-r18/relay-data`; do not create a second
empty production data directory. Verify it is the exact mount used by
`chebycodex-r18-relay`, owned by `10001:10001`, and private. Prepare only the
new bind paths:

```sh
sudo test -d /srv/chebycodex-r18/relay-data
sudo install -d -m 0700 -o 10001 -g 10001 \
  /var/lib/chebycodex-relay-gate/data \
  /etc/chebycodex-relay/bootstrap \
  /etc/chebycodex-relay/gate-bootstrap
sudo install -d -m 0700 -o 101 -g 101 \
  /etc/chebycodex-relay-edge/tls \
  /etc/chebycodex-relay-edge/staging
```

Transfer the public production trust bundle and leaf candidate through an
authenticated encrypted channel. The staging tree must be owned by UID/GID
`101:101`; candidate private keys remain mode `0400`. Expected layout:

```text
/etc/chebycodex-relay-edge/staging/
  relay-trust-bundle.production.json
  candidate/
    ca.pem
    fullchain.pem
    leaf.pem
    privkey.pem
    release.json
```

Run static gates and render/activate without touching the historical Turkey
runtime:

```sh
python3 deploy/relay-edge/validate_bundle.py
relay/.venv/bin/python -m pytest -q \
  relay/tests/test_admin.py \
  deploy/relay-edge/tests/test_certificate_tools.py \
  deploy/relay-edge/tests/test_certificate_shell_recovery.py \
  tools/gate/test_edge_gate.py \
  tools/gate/test_certificate_failure_gate.py
mkdir -m 0700 /absolute/private/certificate-evidence
relay/.venv/bin/python tools/gate/certificate_failure_gate.py \
  --python "$(pwd)/relay/.venv/bin/python" \
  --manifest \
    /absolute/private/certificate-evidence/negative-certificate.json
docker build --pull=false --platform linux/amd64 \
  --file deploy/relay-edge/Dockerfile.edge \
  --tag local/chebycodex-relay-edge:0.1.0 .
sudo -i
cd /opt/chebycodex-mobile
set -a
. /etc/chebycodex-relay-edge/relay-edge.env
set +a
deploy/relay-edge/bootstrap_edge.sh
python3 deploy/relay-edge/verify_runtime_networks.py \
  --bind-ip "$CHEBY_BIND_IP" \
  --production-port "$CHEBY_RELAY_PUBLIC_PORT"
```

During the fixed-IP migration only, set `CHEBY_RELAY_LEGACY_HOST` to the exact
existing Quick Tunnel hostname from `.env.example`. Relay accepts that Host
only on its host-loopback publication; Edge still accepts only the exact
`192.0.0.8:27461` authority. Clear the variable after the fixed-IP APK
passes cutover gates.

The bootstrap starts only production `relay` and `edge`. Edge has two fixed,
purpose-specific interfaces: non-internal ingress
`chebycodex-relay-edge_edge-ingress` (`172.31.60.2` on
`172.31.60.0/28`) receives Docker DNAT for host `27461`, while internal
`chebycodex-relay-edge_relay-backend` (`172.31.61.2` on
`172.31.61.0/28`) reaches Relay. Relay joins only the backend at
`172.31.61.3` and a dedicated publication-only bridge at `172.31.64.2`; it
never joins ingress. That non-internal `172.31.64.0/28` bridge contains only
Relay, disables IPv6 and masquerading, and exists solely so Docker can
materialize `127.0.0.1:18080 -> 172.31.64.2:8080`. Relay remains available to
host diagnostics at `ws://127.0.0.1:18080/relay/v1/node` and trusts only
backend Edge `172.31.61.2/32` for the sanitized `X-Relay-Client-IP`. A
Dockerized Connector must join only the internal backend as its second
interface and use the fixed Relay address `172.31.61.3`; it must not join
ingress, the loopback-publication bridge, or use host networking.

`verify_runtime_networks.py` is a required postflight, not optional diagnostics.
It rejects the original failure mode where Docker reports a requested
`HostConfig.PortBindings` entry but has no active
`NetworkSettings.Ports` publication or matching DNAT rule. It checks both
public Edge and host-loopback Relay publications, rejects extra/source-limited
DNAT matches, proves the loopback health endpoint is reachable, and verifies
the actual routes and fixed Docker memberships.

The public Edge permits only:

- `GET /healthz`;
- `POST /relay/v1/pairings/exchange`;
- `POST /relay/v1/auth/refresh`;
- `GET /relay/v1/device` with WebSocket upgrade.

`/relay/v1/node`, unknown paths, query strings, browser Origin, incorrect
`IP:port` Host, and wrong methods fail closed. Edge removes general forwarding
headers and writes exactly one `X-Relay-Client-IP`. The Node WebSocket is not
public: host tools may use the loopback publication, while the production
Docker Connector uses only the internal Relay backend.

## Gate and recovery

Gate is a separate Relay database/network/profile. Bootstrap it only into the
empty Gate data and bootstrap directories, using invocation-scoped overrides.
The authoritative fail-closed commands, including the silent public-identity
validator and Gate Connector Node installation, are in
`docs/deployment/TURKEY_EDGE_RUNBOOK.md`. The required role split is:

- `edge-full`: CXC1 only for the full credentialed Edge gate;
- `reload-continuity`: CXC1 only for certificate reload continuity;
- `android-e2e`: CXC1 only for the Gate APK, with this role's matching Node
  bootstrap installed into the Gate Connector.

All three are created serially in the same initially empty Gate database while
every Gate service is stopped. They are different assistants, not copies of
one invitation:

```sh
set -a
. /etc/chebycodex-relay-edge/relay-edge.env || {
  echo "Cannot load the Relay Edge environment" >&2
  exit 1
}
set +a
set -eu
set +x
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

for gate_role in edge-full reload-continuity android-e2e; do
  install -d -m 0700 -o 10001 -g 10001 \
    "/etc/chebycodex-relay/gate-bootstrap/$gate_role"
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
    echo "Discard the entire partial Gate bootstrap set" >&2
    exit 1
  fi
done
```

Before starting Gate, run the Runbook's silent validator. It checks private
file ownership/mode, matches the public `assistantId` in each CXC1/Node pair,
requires three distinct assistants, and writes only public assistant/Node IDs
to a private manifest; it never prints a pairing secret or Node token. Revoke
the `edge-full` and `reload-continuity` Node principals offline with
`relay-admin revoke`, using the same two Gate mount overrides and explicit
Relay Edge env file on every Compose call, then remove both raw Node JSON
files. Install only `android-e2e/node-bootstrap.json` with
`deploy/turkey/install_node_bootstrap.py`, and remove that raw JSON immediately
after successful installation.

Only after all validation, revocation, installation, and deletion postconditions
pass may Gate start:

```sh

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
```

Each CXC1 is spent after its first enrollment attempt, including timeout or
unknown outcome. Delete its Turkey, transfer, and runner copies and never use
the 120-second same-key retry as an operator retry mechanism. Any partial or
unknown result requires all Gate services to stop and the complete isolated
Gate Relay DB, Gate Connector state/secrets, Gate APK state, bootstrap roles,
and public manifest to be discarded before regenerating all three roles from
empty state. Production data is never part of that cleanup.

Gate uses its own non-internal ingress L2
`chebycodex-relay-edge_gate-edge-ingress` (`172.31.63.0/28`, Edge `.2`)
and its own internal backend (`172.31.62.0/28`, Edge `.2`, Relay `.3`).
Gate Relay also has its own single-container, non-masqueraded publication
bridge (`172.31.65.0/28`, Relay `.2`) for host loopback `18081` only.
Production and Gate never share an ingress bridge. Gate uses host port `27462`
and host diagnostic loopback `18081`; Gate Connector uses only its internal
backend.

`rotate_certificate.sh` validates and imports a pre-staged leaf, runs `nginx
-t`, reloads production and a running Gate Edge, then verifies the actual public
ports. `recover_certificate.sh` restores `previous`, reloads, and verifies its
exact fingerprint. If no verifiable previous release exists, both Edges stop;
there is no plaintext or unpinned fallback.

Only after public verification, the separate no-network `cert-consumer` reads
`/tls` through a read-only mount and writes only `/staging`. It verifies the
candidate and current five-file releases are byte-identical but inode-distinct,
atomically renames the candidate to `.candidate-consumed`, and durably removes
that tombstone. A later invocation resumes any interrupted tombstone cleanup.
Consumer failure reports a failed one-shot without rolling back or stopping the
already verified Edge; operators must not manually delete staging material.

Install the systemd units only after a successful real-device gate. The rotation
check runs every 12 hours and the public monitor hourly. Both jobs take the same
bounded host lock so a monitor cannot observe `current` between activation and
Nginx reload. A release gate must also hold a WSS connection open across
`nginx -s reload`, prove the same socket continues to exchange ping/pong, and
prove a new socket receives the new leaf.

The executable continuity procedure is the
`--reload-continuity-bootstrap` mode documented in
`tools/gate/README.md`. One process uses the isolated `27462`
`reload-continuity` role while a second uses a one-use
`bootstrap-ephemeral-continuity` identity on production `27461`; they surround
the same reload. The production identity is distinct from the phone, has no
active Node credential, and can pair only while fresh and bound to the fixed
continuity device name. Guarded revoke/purge stays idempotent even after it is
stale, but still requires the exact unused continuity identity. Its immutable manifest proves the
same authenticated `27461` WSS exchanges ping/pong before and after reload, a
new `27461` connection receives the exact candidate leaf, and revocation closes
the old socket with `4401`. No production phone credential is used or changed.

`tools/gate/certificate_failure_gate.py` is the executable negative harness.
It runs the fixed 15-test wrong-key/SAN/chain, Nginx, reload, fingerprint, and
previous-release recovery matrix and creates a new mode-`0600` manifest with
exact counts and pre-test source hashes after proving the sources were unchanged
by every test group. Inherited pytest injection is cleared, plugin autoload is
disabled, addopts are empty, and xfail is strict. It never stores raw pytest
output.

The fixed order is Full Edge with `edge-full`, reload continuity with
`reload-continuity`, Android evidence with `android-e2e`, and only then Gate
teardown. Install only the one-shot rotation service for continuity; enable
certificate timers only after teardown. The transport-stop portion begins
only after all three evidence groups:

```sh
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
```

This stop is not the complete teardown. Follow the exact realpath-guarded
procedure in `docs/deployment/TURKEY_EDGE_RUNBOOK.md`: stop and verify Gate
Connector first, stop and verify Gate Edge/Relay, revoke the Android Node
offline, then empty and verify Gate Connector data/secrets, the Gate Relay
DB/WAL/SHM directory, all three bootstrap role directories, and
`public-identities.json`. After the evidence is captured, uninstall the
test-only `.gate.test` and `.gate` packages over USB ADB and verify no package
or process remains; no Activity or screen operation is permitted. Switch the
host firewall to verified production-only mode, close cloud TCP `27462`, and
leave both Gate state roots empty so the next-run preflight passes.

The Quick Tunnel may be removed only after the production APK receives fresh
Codex responses through port 27461 and restart/network fault gates pass.
