#!/bin/sh
set -eu

bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$bundle_dir/relay_edge_common.sh"

if ! monitor_running_edges; then
    stop_edges
    echo "Relay Edge stopped: hourly certificate identity/expiry gate failed" >&2
    exit 1
fi
