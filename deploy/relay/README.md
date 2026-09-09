# Public Relay deployment

This bundle runs the public rendezvous service used by the Android APK and the
user's private Connector. It is deliberately not the Codex host. The Relay
stores routing/delivery state and credential verifiers only; ChatGPT login data,
Codex workspaces, and raw Gateway credentials remain on the user's machine.

`docker-compose.yml` starts exactly one Relay worker as UID/GID `10001`, with a
read-only root filesystem, no capabilities, bounded resources, and persistent
SQLite under `/data`. Its HTTP port binds only to host loopback. A separately
managed TLS ingress must expose `https://<relay-host>` and forward WebSocket
upgrades to that loopback address. Do not publish port 8080 or use plain `ws://`
for a phone. A Connector on the same host may use plain WS only to the literal
loopback address; its configuration rejects every non-loopback cleartext URL.

The ingress must delete any client-supplied `X-Relay-Client-IP`, then set
exactly one value. Configure `CHEBY_RELAY_TRUSTED_PROXY_CIDRS` as only the
immediate proxy CIDR Relay actually sees. Relay ignores source headers from
every other peer and rejects a missing, repeated, comma-list, or invalid value
from a trusted peer. It never trusts arbitrary `Forwarded`, `X-Forwarded-For`,
or `X-Real-IP`. `cf-connecting-ip` is permitted only when Cloudflare connects
directly and the configured CIDRs are its current authoritative egress ranges.

The application disables debug, OpenAPI, interactive docs, CORS, WebSocket
compression, proxy-header trust, and access logging. It enforces a 12 MiB WSS
message business ceiling and rejects browser `Origin` headers. Uvicorn's
transport message ceiling is exactly one byte higher so the first oversized
message reaches Relay and receives the deterministic application close `4409`;
still larger payloads are hard-closed by the transport. The TLS ingress must retain
the `Authorization` header, preserve the raw path, accept 12 MiB frames, avoid
logging query strings/headers/bodies, and use an idle timeout longer than the
Connector heartbeat interval.

## Build gate

From the repository root:

```sh
python3 deploy/relay/validate_bundle.py
docker build --pull=false --platform linux/amd64 \
  --file deploy/relay/Dockerfile \
  --tag local/chebycodex-relay:0.1.0 .
docker image inspect --format '{{.Id}}' local/chebycodex-relay:0.1.0
```

The static gate works without Docker. A releasable image additionally requires
the image build to complete with `pip --require-hashes`, an image scanner to
report no unresolved HIGH/CRITICAL vulnerability or embedded secret, and the
exact resulting image ID to be placed in the private `.env`. Do not deploy by a
mutable tag. The pinned hashes target CPython 3.12 on Linux amd64/musllinux.

## Bootstrap and activation

Copy `.env.example` to a root-readable file outside Git and replace every
placeholder. Prepare the bind directories without broad permissions:

```sh
sudo install -d -m 0700 -o 10001 -g 10001 \
  /var/lib/chebycodex-relay/data \
  /etc/chebycodex-relay/bootstrap
```

After TLS ingress is ready, create one assistant binding. This command writes
the phone's AK/SK JSON and a separate Node bootstrap file as mode `0600`; it
prints neither credential:

```sh
docker compose --env-file /etc/chebycodex-relay/relay.env \
  --file deploy/relay/docker-compose.yml --profile admin run --rm relay-admin \
  bootstrap --database /data/relay.sqlite3 \
  --relay-origin https://relay.example.com \
  --device-output /bootstrap/device-access.json \
  --node-output /bootstrap/node-bootstrap.json
```

Transfer `node-bootstrap.json` to the private Codex machine over an authenticated
encrypted channel. In the APK, the user types the Relay server address plus the
`accessKey` and `secretKey` values from `device-access.json`; no QR code is
required. Then start Relay:

```sh
docker compose --env-file /etc/chebycodex-relay/relay.env \
  --file deploy/relay/docker-compose.yml up -d relay
```

See `docs/deployment/RELAY_RUNBOOK.md` for acceptance and recovery gates.
