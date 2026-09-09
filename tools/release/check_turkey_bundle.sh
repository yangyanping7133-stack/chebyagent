#!/bin/sh
set -eu

build=0
manifest_output=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --build) build=1; shift ;;
        --manifest-output) manifest_output=${2:-}; shift 2 ;;
        *) echo "usage: $0 [--build] [--manifest-output FILE]" >&2; exit 2 ;;
    esac
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
bundle_dir="$repo_root/deploy/turkey"
compose_file="$bundle_dir/docker-compose.yml"
temporary_dir=$(mktemp -d "${TMPDIR:-/tmp}/chebycodex-release-check.XXXXXX")
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
install -d -m 0700 "$temporary_dir/pycache" "$temporary_dir/python-tmp"
PYTHONPYCACHEPREFIX="$temporary_dir/pycache"
TMPDIR="$temporary_dir/python-tmp"
export PYTHONPYCACHEPREFIX TMPDIR
if find "$repo_root" \( -type d -name __pycache__ -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) \
    -print -quit | grep -q .; then
    echo "STATIC BLOCKED: release source contains Python bytecode or __pycache__" >&2
    exit 1
fi

python_mode=host
checker_image=local/chebycodex-release-checker:py312-v1
if ! python3 -c 'import cryptography, yaml' >/dev/null 2>&1; then
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        echo "STATIC BLOCKED: Python cryptography/PyYAML are missing and Docker is unavailable" >&2
        exit 3
    fi
    if ! docker build --pull --platform linux/amd64 --network host \
        --file "$bundle_dir/Dockerfile.checker" \
        --tag "$checker_image" "$repo_root"; then
        echo "STATIC BLOCKED: pinned release-checker build failed" >&2
        exit 3
    fi
    python_mode=container
fi

run_python() {
    if [ "$python_mode" = host ]; then
        PYTHONPYCACHEPREFIX="$temporary_dir/pycache" \
            TMPDIR="$temporary_dir/python-tmp" python3 "$@"
        return
    fi
    docker run --rm --interactive \
        --network none \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --pids-limit 64 \
        --memory 512m \
        --cpus 1 \
        --tmpfs /tmp:size=64m,mode=1777 \
        --env "PYTHONPYCACHEPREFIX=$temporary_dir/pycache" \
        --env "TMPDIR=$temporary_dir/python-tmp" \
        --volume "$repo_root:$repo_root:ro" \
        --volume "$temporary_dir:$temporary_dir:rw" \
        --workdir "$repo_root" \
        "$checker_image" "$@"
}

public_ip=8.8.8.8
bind_ip=192.0.2.10
secret_file="$temporary_dir/pairing_secret"
umask 077
run_python "$script_dir/validate_pairing_secret.py" generate "$secret_file" \
    --owner-uid "$(id -u)" --owner-gid "$(id -g)" >/dev/null

for script in \
    "$script_dir/bootstrap_ip_certificate.sh" \
    "$script_dir/activate_turkey_release.sh" \
    "$script_dir/renew_ip_certificate.sh" \
    "$script_dir/monitor_turkey_certificate.sh" \
    "$script_dir/report_cert_failure.sh" \
    "$script_dir/preflight_turkey_host.sh" \
    "$script_dir/recover_turkey_tls.sh" \
    "$script_dir/probe_turkey_edge_body_limits.sh" \
    "$script_dir/scan_turkey_images.sh" \
    "$script_dir/turkey_release_common.sh" \
    "$script_dir/validate_cert_alert_hook.sh" \
    "$script_dir/check_turkey_bundle.sh"
do
    sh -n "$script"
done

run_python -m py_compile \
    "$script_dir/render_turkey_edge_config.py" \
    "$script_dir/export_ip_certificate.py" \
    "$script_dir/monitor_ip_tls.py" \
    "$script_dir/edge_body_limit_upstream.py" \
    "$script_dir/validate_edge_probe_config.py" \
    "$script_dir/validate_edge_probe_script.py" \
    "$script_dir/activate_turkey_release.py" \
    "$script_dir/test_turkey_release_security.py" \
    "$script_dir/validate_turkey_docker_networks.py" \
    "$script_dir/validate_turkey_compose.py" \
    "$script_dir/validate_requirements_lock.py" \
    "$script_dir/validate_pairing_secret.py" \
    "$script_dir/validate_trivy_report.py" \
    "$script_dir/validate_turkey_scanner_lock.py" \
    "$script_dir/validate_turkey_cosign_lock.py" \
    "$script_dir/validate_turkey_host_trust.py" \
    "$script_dir/prepare_turkey_build_context.py" \
    "$script_dir/turkey_release_manifest.py"

run_python "$script_dir/validate_requirements_lock.py" \
    --lock "$bundle_dir/requirements.lock" \
    --direct "$repo_root/gateway/requirements.txt"
run_python "$script_dir/validate_turkey_scanner_lock.py" \
    --lock "$bundle_dir/scanner.lock" >/dev/null
run_python "$script_dir/validate_turkey_cosign_lock.py" \
    --lock "$bundle_dir/cosign.lock" >/dev/null
run_python "$script_dir/validate_turkey_compose.py" \
    --compose-yaml "$compose_file"
run_python "$script_dir/test_turkey_release_security.py"

run_python "$script_dir/render_turkey_edge_config.py" \
    --public-ip "$public_ip" \
    --mode bootstrap \
    --output "$temporary_dir/nginx.bootstrap.conf"
run_python "$script_dir/render_turkey_edge_config.py" \
    --public-ip "$public_ip" \
    --mode production \
    --output "$temporary_dir/nginx.production.conf"

grep -F 'access_log off;' "$temporary_dir/nginx.production.conf" >/dev/null
for nginx_temp_path in \
    'client_body_temp_path /tmp/client_body;' \
    'proxy_temp_path /tmp/proxy;' \
    'fastcgi_temp_path /tmp/fastcgi;' \
    'uwsgi_temp_path /tmp/uwsgi;' \
    'scgi_temp_path /tmp/scgi;'
do
    grep -F "$nginx_temp_path" "$temporary_dir/nginx.bootstrap.conf" >/dev/null
    grep -F "$nginx_temp_path" "$temporary_dir/nginx.production.conf" >/dev/null
done
grep -F 'proxy_set_header X-Forwarded-For $remote_addr;' \
    "$bundle_dir/edge/includes/proxy-http.conf" >/dev/null
grep -F 'proxy_set_header Sec-WebSocket-Extensions "";' \
    "$bundle_dir/edge/includes/proxy-websocket.conf" >/dev/null
grep -F 'proxy_hide_header Sec-WebSocket-Extensions;' \
    "$bundle_dir/edge/includes/proxy-websocket.conf" >/dev/null
grep -F 'ssl_certificate /tls/current/fullchain.pem;' \
    "$temporary_dir/nginx.production.conf" >/dev/null
grep -F "hashlib.file_digest(f, 'sha512')" "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F 'e04ec49f30a0d0e9c1c42c989f027eaa767058761ed1849caf9b9c2966e7804f6ee3ed17ae95842eaab62a7f639791992bc3038d4ecfac9c05230bac1ce1e3bb' \
    "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F 'apk add --no-cache openssl=3.5.7-r0' "$bundle_dir/Dockerfile.checker" >/dev/null
grep -F '/sbin/apk add --no-cache' "$bundle_dir/Dockerfile.runtime" >/dev/null
for alpine_package in \
    'bubblewrap=0.11.0-r2' \
    'ca-certificates=20260611-r0' \
    'git=2.52.0-r0' \
    'openssh-client-default=10.2_p1-r0' \
    'ripgrep=15.1.0-r0'
do
    grep -F "$alpine_package" "$bundle_dir/Dockerfile.runtime" >/dev/null
done
grep -F '/usr/sbin/addgroup -S -g 10002 cheby' "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F '/usr/sbin/adduser -S -D -u 10002 -G cheby' "$bundle_dir/Dockerfile.runtime" >/dev/null
for edge_package in \
    'c-ares=1.34.8-r0' \
    'curl=8.20.0-r0' \
    'libcurl=8.20.0-r0' \
    'libexpat=2.8.2-r0'
do
    grep -F "$edge_package" "$bundle_dir/Dockerfile.edge" >/dev/null
done
if grep -Eq 'apt-get|snapshot\.debian' "$bundle_dir/Dockerfile.runtime"; then
    echo "FAIL: vulnerable Debian runtime path reintroduced" >&2
    exit 1
fi
grep -F 'COPY gateway/cheby_gateway /app/gateway/cheby_gateway' \
    "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F 'PIL.__version__ != "12.3.0"' "$repo_root/gateway/cheby_gateway/assets.py" >/dev/null
grep -F '"--ws-max-size", "16384"' "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F '"--ws-max-queue", "4"' "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F '"--ws-per-message-deflate", "false"' "$bundle_dir/Dockerfile.runtime" >/dev/null
grep -F 'ghcr.io/aquasecurity/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f' \
    "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F 'ghcr.io/sigstore/cosign/cosign:v3.1.2@sha256:d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849' \
    "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F '"$scanner_id" --cache-dir /cache image --timeout 15m --download-db-only' \
    "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F -- '--network host' "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F -- '--network none' "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F -- '--skip-db-update --skip-java-db-update' \
    "$script_dir/scan_turkey_images.sh" >/dev/null
grep -F 'alpine:3.23.5@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40' \
    "$script_dir/scan_turkey_images.sh" >/dev/null
if grep -E '^FROM ' "$bundle_dir/Dockerfile.runtime" "$bundle_dir/Dockerfile.edge" \
    "$bundle_dir/Dockerfile.checker" "$bundle_dir/Dockerfile.scanner" \
    "$bundle_dir/Dockerfile.cosign" \
    | grep -Ev 'Dockerfile\.(scanner|cosign):FROM scratch$|@sha256:[0-9a-f]{64}([[:space:]]+AS[[:space:]]+[[:alnum:]_-]+)?$' >/dev/null; then
    echo "FAIL: every container base must be digest-pinned" >&2
    exit 1
fi
if grep -Eq 'ssl_verify_client[[:space:]]+off|proxy_ssl_verify[[:space:]]+off' \
    "$temporary_dir/nginx.production.conf"; then
    echo "FAIL: TLS verification-disabling directive found" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "STATIC PASS: shell, Python, render, and policy checks passed"
    echo "BUILD BLOCKED: a reachable Docker daemon is not available"
    [ "$build" -eq 0 ] && exit 0
    exit 3
fi
docker_server_version=$(docker version --format '{{.Server.Version}}')
docker_server_major=${docker_server_version%%.*}
case "$docker_server_major" in
    ''|*[!0-9]*)
        echo "BUILD BLOCKED: could not parse Docker Server version: $docker_server_version" >&2
        exit 3
        ;;
esac
if [ "$docker_server_major" -lt 26 ]; then
    echo "BUILD BLOCKED: Docker Server 26 or newer is required; found $docker_server_version" >&2
    exit 3
fi

if [ "$build" -eq 0 ]; then
    echo "STATIC PASS: raw Compose, shell, Python, render, and policy checks passed"
    echo "BUILD SKIPPED: rerun with --build to pull and build pinned images"
    exit 0
fi

context_dir="$temporary_dir/context"
context_sha256=$(run_python "$script_dir/prepare_turkey_build_context.py" \
    --repository-root "$repo_root" --output "$context_dir")
runtime_iid="$temporary_dir/runtime.iid"
edge_iid="$temporary_dir/edge.iid"
scanner_iid="$temporary_dir/scanner.iid"
cosign_iid="$temporary_dir/cosign.iid"
if ! docker build --platform linux/amd64 --network host \
    --file "$context_dir/deploy/turkey/Dockerfile.runtime" \
    --iidfile "$runtime_iid" \
    --tag local/chebycodex-runtime:0.1.0-codex-0.144.6 "$context_dir" \
    || ! docker build --platform linux/amd64 --network host \
    --file "$context_dir/deploy/turkey/Dockerfile.edge" \
    --iidfile "$edge_iid" \
    --tag local/chebycodex-edge:0.1.0-nginx-1.30 "$context_dir" \
    || ! docker build --platform linux/amd64 --network host \
    --file "$context_dir/deploy/turkey/Dockerfile.scanner" \
    --iidfile "$scanner_iid" \
    --tag local/chebycodex-scanner:0.72.0-cheby.1 "$context_dir" \
    || ! docker build --platform linux/amd64 --network host \
    --file "$context_dir/deploy/turkey/Dockerfile.cosign" \
    --iidfile "$cosign_iid" \
    --tag local/chebycodex-cosign:3.1.2-cheby.1 "$context_dir"; then
    echo "BUILD BLOCKED: pinned image build or registry/package egress failed" >&2
    exit 3
fi
runtime_image=$(cat "$runtime_iid")
edge_image=$(cat "$edge_iid")
scanner_image=$(cat "$scanner_iid")
cosign_image=$(cat "$cosign_iid")
if ! docker pull certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4; then
    echo "BUILD BLOCKED: digest-pinned Certbot pull or registry egress failed" >&2
    exit 3
fi
if ! docker run --rm --entrypoint /bin/sh \
    certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4 \
    -eu -c 'command -v openssl >/dev/null; test -d /etc/ssl/certs'; then
    echo "BUILD BLOCKED: pinned Certbot image lacks OpenSSL/system trust required by exporter" >&2
    exit 3
fi
if ! docker pull alpine:3.23.5@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40; then
    echo "BUILD BLOCKED: digest-pinned migration Alpine pull or registry egress failed" >&2
    exit 3
fi

docker run --rm \
    --read-only \
    --user 101:101 \
    --tmpfs /tmp:size=32m,uid=101,gid=101,mode=0700 \
    --volume "$temporary_dir/nginx.bootstrap.conf:/etc/nginx/nginx.conf:ro" \
    "$edge_image" \
    nginx -t
docker run --rm "$runtime_image" \
    codex --version | grep -F '0.144.6' >/dev/null
docker run --rm --entrypoint /bin/sh \
    "$runtime_image" -eu -c '
        test ! -e /app/gateway/tests
        test ! -e /app/gateway/.venv
        if find /app -type d -name __pycache__ -o -type f \( -name "*.pyc" -o -name "*.pyo" \) | grep -q .; then
            echo "runtime image contains Python cache artifacts under /app" >&2
            exit 1
        fi
    '

"$script_dir/probe_turkey_edge_body_limits.sh" \
    --runtime-image "$runtime_image" \
    --edge-image "$edge_image" \
    --certbot-image certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4 \
    --production-config "$temporary_dir/nginx.production.conf"

if [ -z "$manifest_output" ]; then
    manifest_output="$bundle_dir/generated/release-manifest.json"
fi
"$script_dir/scan_turkey_images.sh" \
    --context "$context_dir" \
    --context-sha256 "$context_sha256" \
    --runtime-image "$runtime_image" \
    --edge-image "$edge_image" \
    --scanner-image "$scanner_image" \
    --cosign-image "$cosign_image" \
    --manifest-output "$manifest_output"

CHEBY_PUBLIC_IP="$public_ip" \
CHEBY_BIND_IP="$bind_ip" \
CHEBY_ACME_EMAIL=operator@example.invalid \
CHEBY_PAIRING_SECRET_PATH="$secret_file" \
CHEBY_RUNTIME_IMAGE="$runtime_image" \
CHEBY_EDGE_IMAGE="$edge_image" \
docker compose --file "$compose_file" --profile maintenance \
    config --format json > "$temporary_dir/compose.json"
run_python "$script_dir/validate_turkey_compose.py" \
    --compose-json "$temporary_dir/compose.json" \
    --public-ip "$public_ip" \
    --bind-ip "$bind_ip" \
    --pairing-secret-path "$secret_file" \
    --release-manifest "$manifest_output"

echo "BUILD PASS: pinned, minimized, scanned release and migration images are feasible"
