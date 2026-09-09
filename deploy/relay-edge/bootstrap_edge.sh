#!/bin/sh
set -eu

bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$bundle_dir/relay_edge_common.sh"

python3 "$bundle_dir/preflight_host.py" \
    --bind-ip "$CHEBY_BIND_IP" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --production-port "$CHEBY_RELAY_PUBLIC_PORT" \
    --gate-port "$CHEBY_RELAY_GATE_PORT" \
    --require-free-loopback-port "$CHEBY_RELAY_LOOPBACK_PORT"

render_configs
cert_tool install-trust \
    --tls-root /tls \
    --trust-bundle /staging/relay-trust-bundle.production.json
cert_tool activate --tls-root /tls --candidate-dir /staging/candidate

if ! docker compose --file "$compose_file" run --rm --no-deps edge nginx -t; then
    "$bundle_dir/recover_certificate.sh" "offline Nginx validation failed"
fi
if ! docker compose --file "$compose_file" up --detach --no-build --pull never relay edge; then
    "$bundle_dir/recover_certificate.sh" "Edge activation failed"
fi
if ! monitor_port "$CHEBY_RELAY_PUBLIC_PORT"; then
    "$bundle_dir/recover_certificate.sh" "public pin verification failed"
fi
if ! cert_consumer consume \
    --tls-root /tls \
    --candidate-dir /staging/candidate; then
    echo "Certificate activated but its staging candidate could not be consumed" >&2
    exit 1
fi
echo "Relay Edge activated on the pinned high port"
