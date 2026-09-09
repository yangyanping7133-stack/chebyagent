#!/bin/sh
set -eu

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_RELEASE_MANIFEST_PATH:?CHEBY_RELEASE_MANIFEST_PATH is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/deploy/turkey/docker-compose.yml"
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime
validate_turkey_root_execution
load_turkey_release_images
verify_turkey_release_images
"$script_dir/validate_cert_alert_hook.sh"
expected_fingerprint=$(current_turkey_tls_fingerprint)
python3 "$script_dir/monitor_ip_tls.py" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --minimum-hours 48 \
    --expected-sha256 "$expected_fingerprint"
