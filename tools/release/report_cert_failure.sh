#!/bin/sh
set -eu

unit=${1:-unknown-unit}
logger --priority daemon.err --tag chebycodex-cert \
    "ChebyCodex certificate gate failed for $unit"

hook=/etc/chebycodex/cert-alert-hook
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/validate_cert_alert_hook.sh" "$hook"
exec "$hook" "$unit"
