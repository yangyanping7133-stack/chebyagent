#!/bin/sh
set -eu

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_PAIRING_SECRET_PATH:?CHEBY_PAIRING_SECRET_PATH is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/deploy/turkey/docker-compose.yml"
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime

validate_turkey_root_execution
load_turkey_release_images
verify_turkey_release_images

docker compose --file "$compose_file" --profile maintenance run --rm certbot \
    renew \
    --non-interactive \
    --no-random-sleep-on-renew
if ! docker compose --file "$compose_file" --profile maintenance run --rm cert-export; then
    "$script_dir/recover_turkey_tls.sh" "renewed certificate export or trust-chain validation failed"
fi
if ! expected_fingerprint=$(current_turkey_tls_fingerprint); then
    "$script_dir/recover_turkey_tls.sh" "renewed certificate fingerprint was unavailable"
fi
if ! docker compose --file "$compose_file" exec --no-TTY edge nginx -t; then
    "$script_dir/recover_turkey_tls.sh" "renewed certificate failed online Nginx validation"
fi
if ! docker compose --file "$compose_file" exec --no-TTY edge nginx -s reload; then
    "$script_dir/recover_turkey_tls.sh" "Nginx rejected the renewed certificate reload"
fi
if ! python3 "$script_dir/monitor_ip_tls.py" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --minimum-hours 48 \
    --expected-sha256 "$expected_fingerprint"; then
    "$script_dir/recover_turkey_tls.sh" "public trusted-TLS monitor rejected the renewed certificate"
fi
