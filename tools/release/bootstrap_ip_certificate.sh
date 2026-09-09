#!/bin/sh
set -eu

usage() {
    echo "usage: $0 staging|production" >&2
    exit 2
}

[ "$#" -eq 1 ] || usage
mode=$1
[ "$mode" = "staging" ] || [ "$mode" = "production" ] || usage

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_ACME_EMAIL:?CHEBY_ACME_EMAIL is required}"
: "${CHEBY_PAIRING_SECRET_PATH:?CHEBY_PAIRING_SECRET_PATH is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
bundle_dir="$repo_root/deploy/turkey"
compose_file="$bundle_dir/docker-compose.yml"
render="$script_dir/render_turkey_edge_config.py"
generated="$bundle_dir/generated/nginx.conf"
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime

validate_turkey_root_execution
load_turkey_release_images
verify_turkey_release_images

python3 "$render" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --mode bootstrap \
    --output "$generated"

"$script_dir/activate_turkey_release.sh"

if [ "$mode" = "staging" ]; then
    docker compose --file "$compose_file" --profile maintenance run --rm certbot \
        certonly \
        --staging \
        --non-interactive \
        --agree-tos \
        --email "$CHEBY_ACME_EMAIL" \
        --preferred-profile shortlived \
        --webroot \
        --webroot-path /var/www/acme \
        --ip-address "$CHEBY_PUBLIC_IP" \
        --config-dir /etc/letsencrypt/staging \
        --work-dir /var/lib/letsencrypt/staging \
        --logs-dir /var/log/letsencrypt/staging
    echo "staging IP challenge succeeded; production certificate was not changed"
    exit 0
fi

docker compose --file "$compose_file" --profile maintenance run --rm certbot \
    certonly \
    --non-interactive \
    --agree-tos \
    --email "$CHEBY_ACME_EMAIL" \
    --preferred-profile shortlived \
    --webroot \
    --webroot-path /var/www/acme \
    --ip-address "$CHEBY_PUBLIC_IP" \
    --cert-name "$CHEBY_PUBLIC_IP"

if ! docker compose --file "$compose_file" --profile maintenance run --rm cert-export; then
    "$script_dir/recover_turkey_tls.sh" "certificate export or trust-chain validation failed"
fi
if ! expected_fingerprint=$(current_turkey_tls_fingerprint); then
    "$script_dir/recover_turkey_tls.sh" "activated certificate fingerprint was unavailable"
fi
if ! python3 "$render" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --mode production \
    --output "$generated"; then
    "$script_dir/recover_turkey_tls.sh" "production Nginx configuration render failed"
fi
if ! docker compose --file "$compose_file" run --rm --no-deps edge nginx -t; then
    "$script_dir/recover_turkey_tls.sh" "offline Nginx validation failed"
fi
if ! docker compose --file "$compose_file" up --detach --force-recreate \
    --no-build --pull never --no-deps edge; then
    "$script_dir/recover_turkey_tls.sh" "Edge activation failed"
fi
if ! docker compose --file "$compose_file" exec --no-TTY edge nginx -t; then
    "$script_dir/recover_turkey_tls.sh" "online Nginx validation failed"
fi
if ! python3 "$script_dir/monitor_ip_tls.py" \
    --public-ip "$CHEBY_PUBLIC_IP" --minimum-hours 48 \
    --expected-sha256 "$expected_fingerprint"; then
    "$script_dir/recover_turkey_tls.sh" "public trusted-TLS monitor rejected the new certificate"
fi
