#!/bin/sh
set -eu

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_RELAY_PUBLIC_PORT:?CHEBY_RELAY_PUBLIC_PORT is required}"
: "${CHEBY_RELAY_GATE_PORT:?CHEBY_RELAY_GATE_PORT is required}"
: "${CHEBY_RELAY_LOOPBACK_PORT:?CHEBY_RELAY_LOOPBACK_PORT is required}"
: "${CHEBY_RELAY_TLS_DIR:?CHEBY_RELAY_TLS_DIR is required}"
: "${CHEBY_RELAY_CERT_STAGE_DIR:?CHEBY_RELAY_CERT_STAGE_DIR is required}"

[ "$CHEBY_RELAY_PUBLIC_PORT" = "27461" ] \
    || { echo "production port must be exactly 27461" >&2; exit 2; }
[ "$CHEBY_RELAY_GATE_PORT" = "27462" ] \
    || { echo "Gate port must be exactly 27462" >&2; exit 2; }
[ "$CHEBY_RELAY_LOOPBACK_PORT" = "18080" ] \
    || { echo "production Relay loopback port must be exactly 18080" >&2; exit 2; }
case ${CHEBY_RELAY_LEGACY_HOST:-} in
    ""|chemicals-submission-capital-indianapolis.trycloudflare.com) ;;
    *)
        echo "legacy Relay Host must be empty or the exact migration hostname" >&2
        exit 2
        ;;
esac

bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
compose_file="$bundle_dir/docker-compose.yml"

cert_tool() {
    docker compose --file "$compose_file" --profile maintenance run --rm \
        --no-deps cert-tool \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --service-port "$CHEBY_RELAY_PUBLIC_PORT" "$@"
}

cert_consumer() {
    docker compose --file "$compose_file" --profile maintenance run --rm \
        --no-deps cert-consumer \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --service-port "$CHEBY_RELAY_PUBLIC_PORT" "$@"
}

current_field() {
    cert_tool show --tls-root /tls --field "$1"
}

render_configs() {
    python3 "$bundle_dir/render_nginx_config.py" \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --service-port "$CHEBY_RELAY_PUBLIC_PORT" \
        --backend-ip 172.31.61.3 \
        --output "$bundle_dir/generated/nginx.production.conf"
    python3 "$bundle_dir/render_nginx_config.py" \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --service-port "$CHEBY_RELAY_GATE_PORT" \
        --backend-ip 172.31.62.3 \
        --output "$bundle_dir/generated/nginx.gate.conf"
}

monitor_port() {
    monitored_port=$1
    leaf_sha=$(current_field leaf-sha256)
    leaf_spki=$(current_field leaf-spki-sha256)
    ca_sha=$(current_field ca-sha256)
    docker compose --file "$compose_file" --profile maintenance run --rm \
        --no-deps tls-monitor \
        --public-ip "$CHEBY_PUBLIC_IP" \
        --port "$monitored_port" \
        --expected-sha256 "$leaf_sha" \
        --expected-spki-sha256 "$leaf_spki" \
        --expected-ca-sha256 "$ca_sha" \
        --ca-certificate /tls/current/ca.pem \
        --minimum-hours 48
}

gate_is_running() {
    gate_container_ids=$(docker compose --file "$compose_file" --profile gate \
        ps --status running --quiet edge-gate) || return 2
    [ -n "$gate_container_ids" ]
}

reload_edges() {
    docker compose --file "$compose_file" exec --no-TTY edge nginx -t \
        || return "$?"
    docker compose --file "$compose_file" exec --no-TTY edge nginx -s reload \
        || return "$?"
    if gate_is_running; then
        docker compose --file "$compose_file" --profile gate \
            exec --no-TTY edge-gate nginx -t || return "$?"
        docker compose --file "$compose_file" --profile gate \
            exec --no-TTY edge-gate nginx -s reload || return "$?"
    else
        gate_check=$?
        [ "$gate_check" -eq 1 ] || return "$gate_check"
    fi
}

monitor_running_edges() {
    monitor_port "$CHEBY_RELAY_PUBLIC_PORT" || return "$?"
    if gate_is_running; then
        monitor_port "$CHEBY_RELAY_GATE_PORT" || return "$?"
    else
        gate_check=$?
        [ "$gate_check" -eq 1 ] || return "$gate_check"
    fi
}

stop_edges() {
    stop_status=0
    docker compose --file "$compose_file" stop edge || stop_status=1
    docker compose --file "$compose_file" --profile gate \
        stop edge-gate || stop_status=1
    return "$stop_status"
}
