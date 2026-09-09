#!/bin/sh
set -eu

bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$bundle_dir/relay_edge_common.sh"

# The private CA never resides on Turkey. Every 12 hours this job activates a
# securely pre-staged offline-signed candidate, or proves the current leaf is
# still outside the 48-hour NO-GO floor. A missing candidate is not an
# unattended-signing dependency.
if ! cert_consumer consume \
    --tls-root /tls \
    --candidate-dir /staging/candidate \
    --resume-only; then
    echo "Cannot resume cleanup of a previously consumed certificate candidate" >&2
    exit 1
fi
if [ ! -f "$CHEBY_RELAY_CERT_STAGE_DIR/candidate/release.json" ]; then
    if ! monitor_running_edges; then
        stop_edges
        echo "Relay Edge stopped: no candidate and current certificate failed the 48-hour gate" >&2
        exit 1
    fi
    echo "No staged leaf candidate; current certificate remains outside the safety floor"
    exit 0
fi

if ! cert_tool activate --tls-root /tls --candidate-dir /staging/candidate; then
    if ! monitor_running_edges; then
        stop_edges
        echo "Relay Edge stopped: rejected candidate and current certificate failed the safety gate" >&2
        exit 1
    fi
    echo "Staged certificate candidate was rejected; current release unchanged" >&2
    exit 1
fi
if ! reload_edges || ! monitor_running_edges; then
    "$bundle_dir/recover_certificate.sh" "candidate reload or public verification failed"
fi
if ! cert_consumer consume \
    --tls-root /tls \
    --candidate-dir /staging/candidate; then
    echo "Certificate activated but its staging candidate could not be consumed" >&2
    exit 1
fi
echo "Offline-signed leaf candidate activated and publicly verified"
