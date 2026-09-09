#!/bin/sh

# Sourced only by the root-controlled Turkey release scripts.
prepare_turkey_python_runtime() {
    umask 077
    CHEBY_PYTHON_RUNTIME_DIRECTORY=$(mktemp -d /tmp/chebycodex-python.XXXXXX)
    install -d -m 0700 \
        "$CHEBY_PYTHON_RUNTIME_DIRECTORY/pycache" \
        "$CHEBY_PYTHON_RUNTIME_DIRECTORY/tmp"
    PYTHONPYCACHEPREFIX="$CHEBY_PYTHON_RUNTIME_DIRECTORY/pycache"
    TMPDIR="$CHEBY_PYTHON_RUNTIME_DIRECTORY/tmp"
    export PYTHONPYCACHEPREFIX TMPDIR
    trap 'rm -rf -- "$CHEBY_PYTHON_RUNTIME_DIRECTORY"' EXIT HUP INT TERM
}

load_turkey_release_images() {
    : "${CHEBY_RELEASE_MANIFEST_PATH:?CHEBY_RELEASE_MANIFEST_PATH is required}"
    case "$CHEBY_RELEASE_MANIFEST_PATH" in
        /*) ;;
        *) echo "BLOCKED: release manifest path must be absolute" >&2; return 1 ;;
    esac
    CHEBY_RUNTIME_IMAGE=$(python3 "$script_dir/turkey_release_manifest.py" resolve \
        --manifest "$CHEBY_RELEASE_MANIFEST_PATH" \
        --image runtime --expected-owner-uid 0 --allow-stale-scan)
    CHEBY_EDGE_IMAGE=$(python3 "$script_dir/turkey_release_manifest.py" resolve \
        --manifest "$CHEBY_RELEASE_MANIFEST_PATH" \
        --image edge --expected-owner-uid 0 --allow-stale-scan)
    export CHEBY_RUNTIME_IMAGE CHEBY_EDGE_IMAGE
}

verify_turkey_release_images() {
    for immutable_image in "$CHEBY_RUNTIME_IMAGE" "$CHEBY_EDGE_IMAGE"; do
        actual_image=$(docker image inspect --format '{{.Id}}' "$immutable_image")
        [ "$actual_image" = "$immutable_image" ] || {
            echo "BLOCKED: local image identity differs from scanned release manifest" >&2
            return 1
        }
    done
}

current_turkey_tls_fingerprint() {
    fingerprint=$(docker compose --file "$compose_file" --profile maintenance run --rm \
        --env CHEBY_CERT_EXPORT_ACTION=fingerprint cert-export)
    python3 - "$fingerprint" <<'PY'
import re
import sys
if re.fullmatch(r"[0-9a-f]{64}", sys.argv[1]) is None:
    raise SystemExit("BLOCKED: exporter did not return one complete SHA-256 fingerprint")
PY
    printf '%s\n' "$fingerprint"
}

validate_turkey_root_execution() {
    python3 "$script_dir/validate_turkey_host_trust.py" \
        --repository-root "$repo_root" \
        --environment-file "${CHEBY_ENV_FILE_PATH:-/etc/chebycodex/turkey.env}" \
        --release-manifest "$CHEBY_RELEASE_MANIFEST_PATH" \
        --require-production-root \
        --require-docker-socket
}
