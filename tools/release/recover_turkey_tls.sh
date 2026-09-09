#!/bin/sh
set -eu

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_PAIRING_SECRET_PATH:?CHEBY_PAIRING_SECRET_PATH is required}"

reason=${1:-certificate activation failed}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/deploy/turkey/docker-compose.yml"
generated="$repo_root/deploy/turkey/generated/nginx.conf"
render="$script_dir/render_turkey_edge_config.py"
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime

validate_turkey_root_execution
load_turkey_release_images
verify_turkey_release_images

echo "TLS activation rejected: $reason" >&2

rollback_ok=0
rollback_available=0
if docker compose --file "$compose_file" --profile maintenance run --rm \
    --env CHEBY_CERT_EXPORT_ACTION=rollback cert-export; then
    rollback_available=1
    rollback_fingerprint=$(current_turkey_tls_fingerprint) || rollback_fingerprint=
    if python3 "$render" \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --mode production \
        --output "$generated" \
        && docker compose --file "$compose_file" run --rm --no-deps edge nginx -t; then
        if docker compose --file "$compose_file" exec --no-TTY edge nginx -t \
            && docker compose --file "$compose_file" exec --no-TTY edge nginx -s reload \
            && python3 "$script_dir/monitor_ip_tls.py" \
                --public-ip "$CHEBY_PUBLIC_IP" --minimum-hours 48 \
                --expected-sha256 "$rollback_fingerprint"; then
            rollback_ok=1
            echo "TLS rollback verified: previous certificate reloaded and trusted online" >&2
        elif docker compose --file "$compose_file" up \
            --detach --force-recreate --no-build --pull never --no-deps edge \
            && docker compose --file "$compose_file" exec --no-TTY edge nginx -t \
            && python3 "$script_dir/monitor_ip_tls.py" \
                --public-ip "$CHEBY_PUBLIC_IP" --minimum-hours 48 \
                --expected-sha256 "$rollback_fingerprint"; then
            rollback_ok=1
            echo "TLS rollback verified after fail-safe Edge recreation" >&2
        fi
    fi
fi

if [ "$rollback_ok" -eq 1 ]; then
    exit 1
fi
if [ "$rollback_available" -eq 1 ]; then
    docker compose --file "$compose_file" stop edge >/dev/null 2>&1 || true
    echo "TLS fail-closed: previous release existed but fingerprint-bound rollback could not be verified; Edge was stopped" >&2
    exit 1
fi

# A first deployment has no previous pointer. Remove the unaccepted current
# pointer when possible, then restore the non-TLS bootstrap listener. If any
# bootstrap operation fails, stop Edge so the rejected certificate cannot stay
# reachable on TCP 443.
docker compose --file "$compose_file" --profile maintenance run --rm \
    --env CHEBY_CERT_EXPORT_ACTION=deactivate cert-export >/dev/null 2>&1 || true

if ! python3 "$render" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --mode bootstrap \
    --output "$generated" \
    || ! docker compose --file "$compose_file" run --rm --no-deps edge nginx -t \
    || ! docker compose --file "$compose_file" up --detach --force-recreate \
        --no-build --pull never --no-deps edge \
    || ! docker compose --file "$compose_file" exec --no-TTY edge nginx -t; then
    docker compose --file "$compose_file" stop edge >/dev/null 2>&1 || true
    echo "TLS fail-closed: Edge was stopped because bootstrap recovery failed" >&2
    exit 1
fi

if ! python3 "$script_dir/monitor_ip_tls.py" \
    --public-ip "$CHEBY_PUBLIC_IP" --minimum-hours 1 \
    --expect-unavailable >/dev/null 2>&1; then
    docker compose --file "$compose_file" stop edge >/dev/null 2>&1 || true
    echo "TLS fail-closed: unexpected TLS remained reachable; Edge was stopped" >&2
    exit 1
fi

echo "TLS fail-closed: bootstrap HTTP-01 mode restored; TCP 443 has no TLS listener" >&2
exit 1
