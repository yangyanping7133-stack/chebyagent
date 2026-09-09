#!/bin/sh
set -eu

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_PAIRING_SECRET_PATH:?CHEBY_PAIRING_SECRET_PATH is required}"
: "${CHEBY_RELEASE_MANIFEST_PATH:?CHEBY_RELEASE_MANIFEST_PATH is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/deploy/turkey/docker-compose.yml"
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime

validate_turkey_root_execution
python3 "$script_dir/turkey_release_manifest.py" validate \
    --manifest "$CHEBY_RELEASE_MANIFEST_PATH" --expected-owner-uid 0
load_turkey_release_images
verify_turkey_release_images

"$script_dir/validate_cert_alert_hook.sh"

arch=$(uname -m)
[ "$arch" = "x86_64" ] || {
    echo "BLOCKED: expected x86_64, found $arch" >&2
    exit 1
}

docker_server_version=$(docker version --format '{{.Server.Version}}')
docker_server_major=${docker_server_version%%.*}
case "$docker_server_major" in
    ''|*[!0-9]*)
        echo "BLOCKED: could not parse Docker Server version: $docker_server_version" >&2
        exit 1
        ;;
esac
[ "$docker_server_major" -ge 26 ] || {
    echo "BLOCKED: Docker Server 26 or newer is required; found $docker_server_version" >&2
    exit 1
}
docker compose version >/dev/null

docker_root_dir=$(docker info --format '{{.DockerRootDir}}')
case "$docker_root_dir" in
    /*) ;;
    *)
        echo "BLOCKED: Docker root directory is not an absolute path" >&2
        exit 1
        ;;
esac
[ -d "$docker_root_dir" ] && [ ! -L "$docker_root_dir" ] || {
    echo "BLOCKED: Docker root directory must be a real directory" >&2
    exit 1
}
docker_available_kib=$(df -Pk -- "$docker_root_dir" | awk 'NR == 2 {print $4}')
case "$docker_available_kib" in
    ''|*[!0-9]*)
        echo "BLOCKED: could not determine Docker data-disk free space" >&2
        exit 1
        ;;
esac
[ "$docker_available_kib" -ge 20971520 ] || {
    echo "BLOCKED: Docker data disk needs at least 20 GiB free for build and scan" >&2
    exit 1
}
checkout_available_kib=$(df -Pk -- "$repo_root" | awk 'NR == 2 {print $4}')
case "$checkout_available_kib" in
    ''|*[!0-9]*)
        echo "BLOCKED: could not determine release-checkout free space" >&2
        exit 1
        ;;
esac
[ "$checkout_available_kib" -ge 2097152 ] || {
    echo "BLOCKED: release checkout filesystem needs at least 2 GiB free" >&2
    exit 1
}

cpu_count=$(getconf _NPROCESSORS_ONLN)
[ "$cpu_count" -ge 8 ] || {
    echo "BLOCKED: combined Gateway+Codex xhigh runtime requires at least 8 CPUs" >&2
    exit 1
}
memory_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
[ "$memory_kib" -ge 20971520 ] || {
    echo "BLOCKED: host needs at least 20 GiB RAM for the 18 GiB combined runtime and Edge" >&2
    exit 1
}
available_kib=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
[ "$available_kib" -ge 18874368 ] || {
    echo "BLOCKED: at least 18 GiB RAM must be available at cutover" >&2
    exit 1
}

python3 - "$CHEBY_PUBLIC_IP" "$CHEBY_BIND_IP" <<'PY'
import ipaddress
import sys
public = ipaddress.ip_address(sys.argv[1])
bind = ipaddress.ip_address(sys.argv[2])
if public.version != 4 or not public.is_global:
    raise SystemExit("BLOCKED: CHEBY_PUBLIC_IP must be global IPv4")
if bind.version != 4 or bind.is_unspecified:
    raise SystemExit("BLOCKED: CHEBY_BIND_IP must be a specific local IPv4")
PY

ip -4 address show | grep -F " $CHEBY_BIND_IP/" >/dev/null || {
    echo "BLOCKED: CHEBY_BIND_IP is not assigned to a local ECS interface" >&2
    exit 1
}

python3 "$script_dir/validate_pairing_secret.py" validate \
    "$CHEBY_PAIRING_SECRET_PATH" --expected-uid 10002

if ss -H -lnt | awk '{print $4}' | grep -Eq '(^|:)(80|443)$'; then
    echo "BLOCKED: TCP 80 or 443 already has a listener; no service was stopped" >&2
    exit 1
fi

python3 "$script_dir/validate_turkey_docker_networks.py" --inspect-host-routes
python3 "$script_dir/validate_turkey_docker_networks.py" --inspect-docker

curl --fail --silent --show-error --output /dev/null \
    --connect-timeout 5 --max-time 15 \
    https://acme-v02.api.letsencrypt.org/directory || {
    echo "BLOCKED: Let's Encrypt ACME egress is unavailable" >&2
    exit 1
}
curl --silent --show-error --output /dev/null \
    --connect-timeout 5 --max-time 15 \
    https://api.openai.com/ || {
    echo "BLOCKED: OpenAI HTTPS egress is unavailable" >&2
    exit 1
}

umask 077
preflight_output=$(mktemp "${TMPDIR:-/tmp}/chebycodex-nginx-preflight.XXXXXX")
trap 'rm -f -- "$preflight_output"' EXIT HUP INT TERM
python3 "$script_dir/render_turkey_edge_config.py" \
    --public-ip "$CHEBY_PUBLIC_IP" \
    --mode bootstrap \
    --output "$preflight_output"
[ ! -L "$preflight_output" ] && [ -f "$preflight_output" ] || {
    echo "BLOCKED: preflight renderer did not produce a regular file" >&2
    exit 1
}
[ "$(stat -c '%a' "$preflight_output")" = "600" ] || {
    echo "BLOCKED: preflight render output must remain mode 0600" >&2
    exit 1
}

echo "Turkey host preflight: PASS (read-only checks only)"
