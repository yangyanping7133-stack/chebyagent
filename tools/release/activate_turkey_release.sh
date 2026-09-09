#!/bin/sh
set -eu

usage() {
    echo "usage: $0 [--allow-all-stopped-recovery]" >&2
    exit 2
}

recovery_argument=
if [ "$#" -eq 1 ] && [ "$1" = "--allow-all-stopped-recovery" ]; then
    recovery_argument=$1
elif [ "$#" -ne 0 ]; then
    usage
fi

: "${CHEBY_PUBLIC_IP:?CHEBY_PUBLIC_IP is required}"
: "${CHEBY_BIND_IP:?CHEBY_BIND_IP is required}"
: "${CHEBY_PAIRING_SECRET_PATH:?CHEBY_PAIRING_SECRET_PATH is required}"
: "${CHEBY_RELEASE_MANIFEST_PATH:?CHEBY_RELEASE_MANIFEST_PATH is required}"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$repo_root/deploy/turkey/docker-compose.yml"
state_directory=${CHEBY_RELEASE_STATE_DIRECTORY:-/var/lib/chebycodex/release-state}
. "$script_dir/turkey_release_common.sh"
prepare_turkey_python_runtime

validate_turkey_root_execution
python3 "$script_dir/turkey_release_manifest.py" validate \
    --manifest "$CHEBY_RELEASE_MANIFEST_PATH" --expected-owner-uid 0
load_turkey_release_images
verify_turkey_release_images

python3 "$script_dir/turkey_release_manifest.py" prepare-state \
    --state-directory "$state_directory" --expected-owner-uid 0
python3 "$script_dir/turkey_release_manifest.py" validate-state \
    --state-directory "$state_directory" --expected-owner-uid 0

set -- \
    --compose-file "$compose_file" \
    --manifest "$CHEBY_RELEASE_MANIFEST_PATH" \
    --state-directory "$state_directory" \
    --expected-owner-uid 0
if [ -n "$recovery_argument" ]; then
    set -- "$@" "$recovery_argument"
fi
python3 "$script_dir/activate_turkey_release.py" "$@"
