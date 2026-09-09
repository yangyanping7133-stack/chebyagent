#!/bin/sh
set -eu

reason=${1:-certificate activation failed}
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$bundle_dir/relay_edge_common.sh"

echo "Relay Edge TLS activation rejected: $reason" >&2
if cert_tool rollback --tls-root /tls \
    && reload_edges \
    && monitor_running_edges; then
    echo "Previous certificate release restored and publicly verified" >&2
    exit 1
fi

cert_tool deactivate --tls-root /tls >/dev/null 2>&1 || true
stop_edges
echo "Relay Edge failed closed: no verifiable previous certificate is active" >&2
exit 1
