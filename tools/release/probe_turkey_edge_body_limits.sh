#!/bin/sh
set -eu

usage() {
    echo "usage: $0 --runtime-image ID --edge-image ID --certbot-image REF --production-config FILE" >&2
    exit 2
}

runtime_image=
edge_image=
certbot_image=
production_config=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --runtime-image) runtime_image=${2:-}; shift 2 ;;
        --edge-image) edge_image=${2:-}; shift 2 ;;
        --certbot-image) certbot_image=${2:-}; shift 2 ;;
        --production-config) production_config=${2:-}; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$runtime_image" ] && [ -n "$edge_image" ] && [ -n "$certbot_image" ] \
    && [ -f "$production_config" ] && [ ! -L "$production_config" ] || usage

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -B "$script_dir/validate_edge_probe_script.py" --script "$0" >/dev/null
umask 077
probe_directory=$(mktemp -d "${TMPDIR:-/tmp}/chebycodex-edge-probe.XXXXXX")
probe_name="chebycodex-edge-probe-$$"
upstream_container="$probe_name-upstream"
edge_container="$probe_name-edge"
same_source_client="$probe_name-source"
global_client_one="$probe_name-global-1"
global_client_two="$probe_name-global-2"
global_client_three="$probe_name-global-3"
upstream_port=28081
http_port=28080
tls_port=28443
cleanup() {
    docker container rm --force \
        "$same_source_client" "$global_client_one" "$global_client_two" \
        "$global_client_three" "$edge_container" "$upstream_container" \
        >/dev/null 2>&1 || true
    rm -rf -- "$probe_directory"
}
trap cleanup EXIT HUP INT TERM

probe_token=$(python3 -B -c 'import secrets; print(secrets.token_hex(32))')
python3 -B "$script_dir/validate_edge_probe_config.py" \
    --config "$production_config" --mode production \
    --rewrite-output "$probe_directory/nginx.conf" \
    --probe-token "$probe_token" >/dev/null
expected_upstream='        server 172.31.42.3:8080 max_fails=2 fail_timeout=10s;'
expected_http_listen='        listen 8080 default_server;'
expected_tls_listen='        listen 8443 ssl default_server;'
[ "$(grep -Fxc "$expected_upstream" "$production_config")" -eq 1 ] \
    && [ "$(grep -Fxc "$expected_http_listen" "$production_config")" -eq 1 ] \
    && [ "$(grep -Fxc "$expected_tls_listen" "$production_config")" -eq 1 ] || {
    echo "BUILD BLOCKED: production Edge upstream shape drifted" >&2
    exit 3
}
for probe_directive in \
    "        server 127.0.0.1:$upstream_port max_fails=2 fail_timeout=10s;" \
    "        listen 127.0.0.1:$http_port default_server;" \
    "        listen 127.0.0.1:$tls_port ssl default_server;"
do
    [ "$(grep -Fxc "$probe_directive" "$probe_directory/nginx.conf")" -eq 1 ] || {
        echo "BUILD BLOCKED: loopback-only Edge probe rendering failed" >&2
        exit 3
    }
done
python3 -B "$script_dir/validate_edge_probe_config.py" \
    --config "$probe_directory/nginx.conf" --mode probe >/dev/null
chmod 0644 "$probe_directory/nginx.conf"

install -d -m 0700 "$probe_directory/tls/current"
docker run --rm \
    --network none \
    --read-only \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 32 \
    --memory 128m \
    --cpus 1 \
    --tmpfs /tmp:size=16m,mode=1777 \
    --volume "$probe_directory/tls:/out" \
    --entrypoint /bin/sh \
    "$certbot_image" -eu -c '
        openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
            -subj /CN=8.8.8.8 -addext subjectAltName=IP:8.8.8.8 \
            -keyout /out/current/privkey.pem \
            -out /out/current/fullchain.pem >/dev/null 2>&1
        chmod 0644 /out/current/privkey.pem /out/current/fullchain.pem
    '
chmod 0755 "$probe_directory/tls" "$probe_directory/tls/current"

truncate -s 8388608 "$probe_directory/exact.bin"
truncate -s 8388609 "$probe_directory/over.bin"
truncate -s 1048576 "$probe_directory/near-miss.bin"
chmod 0644 "$probe_directory"/*.bin

if ! docker run --rm --interactive \
    --network host \
    --no-healthcheck \
    --read-only \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 16 \
    --memory 64m \
    --cpus 1 \
    --tmpfs /tmp:size=8m,uid=10002,gid=10002,mode=0700 \
    --entrypoint python \
    "$runtime_image" -B - "$upstream_port" "$http_port" "$tls_port" <<'PY'
import socket
import sys

sockets = []
try:
    for raw_port in sys.argv[1:]:
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        candidate.bind(("127.0.0.1", int(raw_port)))
        sockets.append(candidate)
except OSError as error:
    raise SystemExit(f"loopback probe port is unavailable: {error}")
finally:
    for candidate in sockets:
        candidate.close()
PY
then
    echo "BUILD BLOCKED: a reviewed loopback probe port is already in use" >&2
    exit 3
fi

upstream_container_id=$(docker run --detach --rm \
    --name "$upstream_container" \
    --network host \
    --no-healthcheck \
    --read-only \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 32 \
    --memory 128m \
    --cpus 1 \
    --tmpfs /tmp:size=16m,uid=10002,gid=10002,mode=0700 \
    --env PYTHONPYCACHEPREFIX=/tmp/pycache \
    --env CHEBY_PROBE_BIND=127.0.0.1 \
    --env "CHEBY_PROBE_PORT=$upstream_port" \
    --env "CHEBY_PROBE_TOKEN=$probe_token" \
    --volume "$script_dir/edge_body_limit_upstream.py:/probe.py:ro" \
    --entrypoint python \
    "$runtime_image" -B /probe.py)

edge_container_id=$(docker run --detach --rm \
    --name "$edge_container" \
    --network host \
    --no-healthcheck \
    --read-only \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 64 \
    --memory 128m \
    --cpus 1 \
    --tmpfs /tmp:size=32m,uid=101,gid=101,mode=0700 \
    --volume "$probe_directory/nginx.conf:/etc/nginx/nginx.conf:ro" \
    --volume "$probe_directory/tls:/tls:ro" \
    "$edge_image")

require_container_running() {
    expected_id=$1
    label=$2
    actual=$(docker inspect --format '{{.Id}}:{{.State.Running}}' \
        "$expected_id" 2>/dev/null || true)
    [ "$actual" = "$expected_id:true" ] || {
        echo "BUILD BLOCKED: owned $label probe container is not running" >&2
        docker inspect --format '{{json .State}}' "$expected_id" >&2 || true
        docker logs "$expected_id" >&2 || true
        exit 3
    }
}

require_probe_services_running() {
    require_container_running "$upstream_container_id" upstream
    require_container_running "$edge_container_id" Edge
}

ready=0
attempt=0
while [ "$attempt" -lt 15 ]; do
    require_container_running "$upstream_container_id" upstream
    upstream_status=$(docker run --rm \
        --network host \
        --no-healthcheck \
        --read-only \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 16 \
        --memory 64m \
        --cpus 1 \
        --tmpfs /tmp:size=8m,uid=101,gid=101,mode=0700 \
        --entrypoint curl \
        "$edge_image" --noproxy '*' --fail --silent --show-error \
        --connect-timeout 1 --max-time 2 \
        --output /dev/null \
        --write-out '%{http_code}:%header{x-cheby-probe-upstream-token}' \
        "http://127.0.0.1:$upstream_port/ready" 2>/dev/null || true)
    if [ "$upstream_status" = "204:$probe_token" ]; then
        require_container_running "$upstream_container_id" upstream
        ready=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
[ "$ready" -eq 1 ] || {
    echo "BUILD BLOCKED: Edge body-limit probe upstream did not become ready" >&2
    docker inspect --format '{{json .State}}' "$upstream_container" >&2 || true
    docker logs "$upstream_container" >&2 || true
    exit 3
}

ready=0
attempt=0
while [ "$attempt" -lt 15 ]; do
    require_container_running "$edge_container_id" Edge
    edge_status=$(docker exec "$edge_container" curl --noproxy '*' \
        --silent --show-error --insecure \
        --connect-timeout 1 --max-time 2 \
        --header 'Host: 8.8.8.8' --output /dev/null \
        --write-out '%{http_code}:%header{x-cheby-probe-edge-token}' \
        "https://127.0.0.1:$tls_port/probe-ready" 2>/dev/null || true)
    if [ "$edge_status" = "404:$probe_token" ]; then
        require_container_running "$edge_container_id" Edge
        ready=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
[ "$ready" -eq 1 ] || {
    echo "BUILD BLOCKED: production Edge probe configuration did not become ready" >&2
    docker logs "$edge_container" >&2 || true
    exit 3
}

probe_status() {
    body_name=$1
    path=$2
    docker run --rm \
        --network host \
        --no-healthcheck \
        --read-only \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 16 \
        --memory 64m \
        --cpus 1 \
        --tmpfs /tmp:size=8m,uid=101,gid=101,mode=0700 \
        --volume "$probe_directory/$body_name:/probe-body:ro" \
        --entrypoint curl \
        "$edge_image" --noproxy '*' --silent --show-error --insecure \
        --connect-timeout 2 --max-time 30 --retry 5 --retry-connrefused --retry-delay 1 \
        --request PUT \
        --header 'Host: 8.8.8.8' \
        --header 'Content-Type: image/png' \
        --data-binary @/probe-body \
        --interface 127.0.0.2 \
        --output /dev/null \
        --write-out '%{http_code}:%header{x-cheby-probe-edge-token}' \
        "https://127.0.0.1:$tls_port$path"
}

start_probe_client() {
    client_name=$1
    docker run --detach --rm \
        --name "$client_name" \
        --network host \
        --no-healthcheck \
        --read-only \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 16 \
        --memory 64m \
        --cpus 1 \
        --tmpfs /tmp:size=8m,uid=101,gid=101,mode=0700 \
        --volume "$probe_directory/exact.bin:/probe-exact:ro" \
        --volume "$probe_directory/near-miss.bin:/probe-small:ro" \
        --entrypoint /bin/sh \
        "$edge_image" -c 'while :; do sleep 3600; done' >/dev/null
}

client_probe_status() {
    client_name=$1
    body_path=$2
    path=$3
    source_address=$4
    docker exec "$client_name" curl --noproxy '*' --silent --show-error --insecure \
        --connect-timeout 2 --max-time 30 --limit-rate 1024k \
        --request PUT \
        --header 'Host: 8.8.8.8' \
        --header 'Content-Type: image/png' \
        --data-binary "@$body_path" \
        --interface "$source_address" \
        --output /dev/null \
        --write-out '%{http_code}:%header{x-cheby-probe-edge-token}' \
        "https://127.0.0.1:$tls_port$path"
}

image_path='/v1/threads/thread-1/turn-inputs/message-1/images/asset-1'
require_probe_services_running
exact_status=$(probe_status exact.bin "$image_path")
over_status=$(probe_status over.bin "$image_path")
near_miss_status=$(probe_status near-miss.bin "$image_path/extra")
require_probe_services_running
[ "$exact_status" = "204:$probe_token" ] || {
    echo "BUILD BLOCKED: Edge rejected the exact 8 MiB image body with HTTP $exact_status" >&2
    exit 3
}
[ "$over_status" = "413:$probe_token" ] || {
    echo "BUILD BLOCKED: Edge did not reject 8 MiB + 1 byte with HTTP 413 (got $over_status)" >&2
    exit 3
}
[ "$near_miss_status" = "413:$probe_token" ] || {
    echo "BUILD BLOCKED: non-exact image path escaped the generic 64 KiB ceiling" >&2
    exit 3
}

start_probe_client "$same_source_client"
start_probe_client "$global_client_one"
start_probe_client "$global_client_two"
start_probe_client "$global_client_three"

require_probe_services_running
same_first_status_file="$probe_directory/same-first.status"
client_probe_status \
    "$same_source_client" /probe-exact "$image_path-source-first" 127.0.0.2 \
    > "$same_first_status_file" &
same_first_pid=$!
sleep 1
kill -0 "$same_first_pid" 2>/dev/null || {
    echo "BUILD BLOCKED: source-limit control request did not remain in flight" >&2
    exit 3
}
if ! same_second_status=$(client_probe_status \
    "$same_source_client" /probe-small "$image_path-source-second" 127.0.0.2); then
    echo "BUILD BLOCKED: excess same-source request did not return an HTTP status" >&2
    exit 3
fi
if ! wait "$same_first_pid"; then
    echo "BUILD BLOCKED: source-limit control request failed" >&2
    exit 3
fi
same_first_status=$(cat "$same_first_status_file")
require_probe_services_running
[ "$same_first_status" = "204:$probe_token" ] \
    && [ "$same_second_status" = "429:$probe_token" ] || {
    echo "BUILD BLOCKED: image source connection limit was not 1 " \
        "(first=$same_first_status second=$same_second_status)" >&2
    exit 3
}

require_probe_services_running
global_one_status_file="$probe_directory/global-one.status"
global_two_status_file="$probe_directory/global-two.status"
client_probe_status \
    "$global_client_one" /probe-exact "$image_path-global-one" 127.0.0.2 \
    > "$global_one_status_file" &
global_one_pid=$!
client_probe_status \
    "$global_client_two" /probe-exact "$image_path-global-two" 127.0.0.3 \
    > "$global_two_status_file" &
global_two_pid=$!
sleep 1
kill -0 "$global_one_pid" 2>/dev/null && kill -0 "$global_two_pid" 2>/dev/null || {
    echo "BUILD BLOCKED: global-limit control requests did not remain in flight" >&2
    exit 3
}
if ! global_three_status=$(client_probe_status \
    "$global_client_three" /probe-small "$image_path-global-three" 127.0.0.4); then
    echo "BUILD BLOCKED: excess global request did not return an HTTP status" >&2
    exit 3
fi
if ! wait "$global_one_pid" || ! wait "$global_two_pid"; then
    echo "BUILD BLOCKED: global-limit control request failed" >&2
    exit 3
fi
global_one_status=$(cat "$global_one_status_file")
global_two_status=$(cat "$global_two_status_file")
require_probe_services_running
[ "$global_one_status" = "204:$probe_token" ] \
    && [ "$global_two_status" = "204:$probe_token" ] \
    && [ "$global_three_status" = "429:$probe_token" ] || {
    echo "BUILD BLOCKED: image global connection limit was not 2 " \
        "(first=$global_one_status second=$global_two_status third=$global_three_status)" >&2
    exit 3
}

echo "EDGE BODY PASS: 8 MiB boundary and source=1/global=2 concurrent limits hold"
