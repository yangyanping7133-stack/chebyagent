#!/bin/sh
set -eu

usage() {
    echo "usage: $0 --context DIR --context-sha256 HEX --runtime-image ID --edge-image ID --scanner-image ID --cosign-image ID --manifest-output FILE" >&2
    exit 2
}

context=
context_sha256=
runtime_image=
edge_image=
scanner_image=
cosign_image=
manifest_output=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --context) context=${2:-}; shift 2 ;;
        --context-sha256) context_sha256=${2:-}; shift 2 ;;
        --runtime-image) runtime_image=${2:-}; shift 2 ;;
        --edge-image) edge_image=${2:-}; shift 2 ;;
        --scanner-image) scanner_image=${2:-}; shift 2 ;;
        --cosign-image) cosign_image=${2:-}; shift 2 ;;
        --manifest-output) manifest_output=${2:-}; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$context" ] && [ -d "$context" ] && [ ! -L "$context" ] || usage
[ -n "$context_sha256" ] && [ -n "$runtime_image" ] \
    && [ -n "$edge_image" ] && [ -n "$scanner_image" ] \
    && [ -n "$cosign_image" ] \
    && [ -n "$manifest_output" ] || usage
[ "$(id -u)" -eq 0 ] || {
    echo "SCAN BLOCKED: private Docker archive scans must run as root" >&2
    exit 4
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
official_trivy_reference='ghcr.io/aquasecurity/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f'
official_cosign_reference='ghcr.io/sigstore/cosign/cosign:v3.1.2@sha256:d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849'
cosign_identity_regexp='https://github\.com/aquasecurity/trivy/\.github/workflows/.+'
cosign_issuer='https://token.actions.githubusercontent.com'
custom_scanner_version='0.72.0-cheby.1'
upstream_scanner_version='0.72.0'
expected_scanner_binary_sha256='30329a9bfa5c4b29e7f0b3552ae7430ef011b73963033e9b028e2003bcd08a39'
expected_cosign_binary_sha256='7ba7d877672635f2d7e537ca3d20e751c088d86265c0f8bc6698d0084899c0af'
certbot_reference='certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4'
alpine_reference='alpine:3.23.5@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40'
scanner_lock="$context/deploy/turkey/scanner.lock"
cosign_lock="$context/deploy/turkey/cosign.lock"
findings_lock="$context/deploy/turkey/trivy-official-findings.lock.json"
scanner_dockerfile="$context/deploy/turkey/Dockerfile.scanner"
cosign_dockerfile="$context/deploy/turkey/Dockerfile.cosign"

umask 077
temporary_dir=$(mktemp -d "${TMPDIR:-/tmp}/chebycodex-trivy.XXXXXX")
trap 'rm -rf -- "$temporary_dir"' EXIT HUP INT TERM
install -d -m 0750 -o 0 -g 65532 "$temporary_dir/archives"
install -d -m 0700 -o 65532 -g 65532 "$temporary_dir/cache"
install -d -m 0750 -o 0 -g 65532 "$temporary_dir/scan-context"
install -d -m 0700 "$temporary_dir/pycache" "$temporary_dir/python-tmp" "$temporary_dir/reports"
PYTHONPYCACHEPREFIX="$temporary_dir/pycache"
TMPDIR="$temporary_dir/python-tmp"
export PYTHONPYCACHEPREFIX TMPDIR

case "${DOCKER_HOST:-}" in
    ''|unix:///var/run/docker.sock|unix:///run/docker.sock) ;;
    *) echo "SCAN BLOCKED: Docker endpoint must be the reviewed local Unix socket" >&2; exit 4 ;;
esac
case "${DOCKER_CONTEXT:-}" in
    ''|default) ;;
    *) echo "SCAN BLOCKED: Docker context must be empty or default" >&2; exit 4 ;;
esac
[ -S /var/run/docker.sock ] || {
    echo "SCAN BLOCKED: reviewed Docker socket /var/run/docker.sock is unavailable" >&2
    exit 4
}
command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 || {
    echo "SCAN BLOCKED: reachable local Docker daemon is unavailable" >&2
    exit 4
}

actual_context_sha256=$(python3 "$script_dir/prepare_turkey_build_context.py" \
    --hash-context "$context") || {
    echo "SCAN BLOCKED: exact build context could not be rehashed" >&2
    exit 4
}
[ "$actual_context_sha256" = "$context_sha256" ] || {
    echo "SCAN BLOCKED: build context changed between build and scan" >&2
    exit 4
}
scanner_lock_sha256=$(python3 "$script_dir/validate_turkey_scanner_lock.py" \
    --lock "$scanner_lock") || {
    echo "SCAN BLOCKED: scanner supply-chain lock validation failed" >&2
    exit 4
}
cosign_lock_sha256=$(python3 "$script_dir/validate_turkey_cosign_lock.py" \
    --lock "$cosign_lock") || {
    echo "SCAN BLOCKED: Cosign supply-chain lock validation failed" >&2
    exit 4
}

sha256_file() {
    output=$(sha256sum "$1") || exit 4
    set -- $output
    [ "$#" -eq 2 ] || exit 4
    case "$1" in
        ????????????????????????????????????????????????????????????????) printf '%s\n' "$1" ;;
        *) exit 4 ;;
    esac
}

scanner_dockerfile_sha256=$(sha256_file "$scanner_dockerfile")
cosign_dockerfile_sha256=$(sha256_file "$cosign_dockerfile")
findings_lock_sha256=$(sha256_file "$findings_lock")

cp -R -- "$context/." "$temporary_dir/scan-context"
chown -R 0:65532 "$temporary_dir/scan-context"
find "$temporary_dir/scan-context" -type d -exec chmod 0750 {} +
find "$temporary_dir/scan-context" -type f -exec chmod 0640 {} +
copied_context_sha256=$(python3 "$script_dir/prepare_turkey_build_context.py" \
    --hash-context "$temporary_dir/scan-context")
[ "$copied_context_sha256" = "$context_sha256" ] || {
    echo "SCAN BLOCKED: nonroot scan copy differs from exact build context" >&2
    exit 4
}

immutable_id() {
    value=$(docker image inspect --format '{{.Id}}' "$1")
    case "$value" in sha256:????????????????????????????????????????????????????????????????) ;;
        *) echo "SCAN BLOCKED: image identity is unavailable" >&2; exit 4 ;;
    esac
    printf '%s\n' "$value"
}

runtime_id=$(immutable_id "$runtime_image")
edge_id=$(immutable_id "$edge_image")
scanner_id=$(immutable_id "$scanner_image")
cosign_id=$(immutable_id "$cosign_image")
[ "$runtime_id" = "$runtime_image" ] \
    && [ "$edge_id" = "$edge_image" ] \
    && [ "$scanner_id" = "$scanner_image" ] \
    && [ "$cosign_id" = "$cosign_image" ] || {
    echo "SCAN BLOCKED: build outputs are not exact immutable image IDs" >&2
    exit 4
}

docker pull "$official_trivy_reference" >/dev/null || {
    echo "SCAN BLOCKED: signed official Trivy bootstrap could not be pulled" >&2
    exit 4
}
official_trivy_id=$(immutable_id "$official_trivy_reference")

# The custom scanner is executable release tooling. Bind its exact image config,
# two-file rootfs, labels, CA, and binary hash before its first execution.
scanner_archive="$temporary_dir/archives/custom-scanner.tar"
docker image save --output "$scanner_archive" "$scanner_id" || {
    echo "SCAN BLOCKED: custom scanner could not be privately exported" >&2
    exit 4
}
chown 0:0 "$scanner_archive"
chmod 0600 "$scanner_archive"
scanner_binary_hash=$(python3 "$script_dir/validate_trivy_report.py" scanner-image \
    --archive "$scanner_archive" --expected-image-id "$scanner_id" \
    --expected-owner-uid 0 --expected-mode 0600) || exit 4
[ "$scanner_binary_hash" = "$expected_scanner_binary_sha256" ] || exit 4
chown 65532:65532 "$scanner_archive"
chmod 0400 "$scanner_archive"
scanner_binary_hash=$(python3 "$script_dir/validate_trivy_report.py" scanner-image \
    --archive "$scanner_archive" --expected-image-id "$scanner_id" \
    --expected-owner-uid 65532 --expected-mode 0400) || exit 4
[ "$scanner_binary_hash" = "$expected_scanner_binary_sha256" ] || exit 4

db_download_status=0
docker run --rm \
    --network host \
    --read-only \
    --user 65532:65532 \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 128 \
    --memory 2g \
    --cpus 1 \
    --env HOME=/tmp \
    --tmpfs /tmp:size=512m,uid=65532,gid=65532,mode=0700 \
    --volume "$temporary_dir/cache:/cache:rw" \
    "$scanner_id" --cache-dir /cache image --timeout 15m --download-db-only \
    || db_download_status=$?
if [ "$db_download_status" -ne 0 ]; then
    echo "SCAN BLOCKED: vulnerability DB download failed (scanner exit $db_download_status)" >&2
    exit 4
fi

database_hashes_before=$(python3 "$script_dir/validate_trivy_report.py" database-cache \
    --cache "$temporary_dir/cache" --expected-owner-uid 65532) || {
    echo "SCAN BLOCKED: downloaded vulnerability DB cache is malformed" >&2
    exit 4
}
set -- $database_hashes_before
[ "$#" -eq 2 ] || {
    echo "SCAN BLOCKED: vulnerability DB hashes are incomplete" >&2
    exit 4
}
db_metadata_sha256=$1
db_sha256=$2

run_scanner_context() {
    scanner=$1
    shift
    docker run --rm \
        --network none \
        --read-only \
        --user 65532:65532 \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 256 \
        --memory 2g \
        --cpus 2 \
        --env HOME=/tmp \
        --tmpfs /tmp:size=1g,uid=65532,gid=65532,mode=0700 \
        --volume "$temporary_dir/cache:/cache:ro" \
        --volume "$temporary_dir/scan-context:/scan:ro" \
        "$scanner" --cache-dir /cache "$@"
}

run_scanner_archive() {
    scanner=$1
    shift
    docker run --rm \
        --network none \
        --read-only \
        --user 65532:65532 \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 256 \
        --memory 2g \
        --cpus 2 \
        --env HOME=/tmp \
        --tmpfs /tmp:size=1g,uid=65532,gid=65532,mode=0700 \
        --volume "$temporary_dir/cache:/cache:ro" \
        --volume "$temporary_dir/archives:/evidence:ro" \
        "$scanner" --cache-dir /cache "$@"
}

run_scanner_metadata() {
    scanner=$1
    shift
    docker run --rm \
        --network none \
        --read-only \
        --user 65532:65532 \
        --security-opt no-new-privileges:true \
        --cap-drop ALL \
        --pids-limit 32 \
        --memory 128m \
        --cpus 1 \
        --env HOME=/tmp \
        --tmpfs /tmp:size=16m,uid=65532,gid=65532,mode=0700 \
        --volume "$temporary_dir/cache:/cache:ro" \
        "$scanner" --cache-dir /cache "$@"
}

validate_clean_report() {
    report_path=$1
    label=$2
    artifact_name=$3
    artifact_type=$4
    target=$5
    scanner_version=$6
    set -- python3 "$script_dir/validate_trivy_report.py" scan \
        --report "$report_path" \
        --label "$label" \
        --expected-artifact-name "$artifact_name" \
        --expected-artifact-type "$artifact_type" \
        --expected-scanner-version "$scanner_version"
    if [ -n "$target" ]; then
        set -- "$@" --expected-target "$target"
    fi
    "$@"
}

export_archive() {
    label=$1
    image_id=$2
    tool_type=${3:-archive}
    archive="$temporary_dir/archives/$label.tar"
    docker image save --output "$archive" "$image_id" || {
        echo "SCAN BLOCKED: exact immutable image could not be privately exported: $label" >&2
        exit 4
    }
    chown 0:0 "$archive"
    chmod 0600 "$archive"
    case "$tool_type" in
      scanner)
        validator=scanner-image
        expected_binary_hash=$expected_scanner_binary_sha256
        ;;
      cosign)
        validator=cosign-image
        expected_binary_hash=$expected_cosign_binary_sha256
        ;;
      archive)
        validator=archive
        expected_binary_hash=
        ;;
      *) echo "SCAN BLOCKED: unknown private archive policy: $tool_type" >&2; exit 4 ;;
    esac
    if [ "$validator" != archive ]; then
        binary_hash=$(python3 "$script_dir/validate_trivy_report.py" "$validator" \
            --archive "$archive" --expected-image-id "$image_id" \
            --expected-owner-uid 0 --expected-mode 0600) || exit 4
        [ "$binary_hash" = "$expected_binary_hash" ] || exit 4
    else
        python3 "$script_dir/validate_trivy_report.py" archive \
            --archive "$archive" --expected-image-id "$image_id" \
            --expected-owner-uid 0 --expected-mode 0600 || exit 4
    fi
    chown 65532:65532 "$archive"
    chmod 0400 "$archive"
    if [ "$validator" != archive ]; then
        binary_hash=$(python3 "$script_dir/validate_trivy_report.py" "$validator" \
            --archive "$archive" --expected-image-id "$image_id" \
            --expected-owner-uid 65532 --expected-mode 0400) || exit 4
        [ "$binary_hash" = "$expected_binary_hash" ] || exit 4
    else
        python3 "$script_dir/validate_trivy_report.py" archive \
            --archive "$archive" --expected-image-id "$image_id" \
            --expected-owner-uid 65532 --expected-mode 0400 || exit 4
    fi
}

scan_custom_archive() {
    label=$1
    image_id=$2
    report_path="$temporary_dir/reports/$label.json"
    if ! run_scanner_archive "$scanner_id" image --cache-backend memory --offline-scan --timeout 15m \
        --input "/evidence/$label.tar" \
        --skip-db-update --skip-java-db-update \
        --scanners vuln,secret --format json --quiet > "$report_path"; then
        echo "SCAN BLOCKED: custom socket-free image archive scan failed: $label" >&2
        exit 4
    fi
    validate_clean_report "$report_path" "immutable-$label" \
        "/evidence/$label.tar" container_image "$image_id" \
        "$custom_scanner_version"
}

context_report="$temporary_dir/reports/context.json"
if ! run_scanner_context "$scanner_id" filesystem --cache-backend memory --offline-scan --timeout 15m \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet /scan > "$context_report"; then
    echo "SCAN BLOCKED: exact build-context scan operation failed" >&2
    exit 4
fi
validate_clean_report "$context_report" exact-build-context /scan filesystem '' \
    "$custom_scanner_version"

certbot_id=$(immutable_id "$certbot_reference")
alpine_id=$(immutable_id "$alpine_reference")
export_archive official-trivy "$official_trivy_id"
export_archive cosign "$cosign_id" cosign
export_archive runtime "$runtime_id"
export_archive edge "$edge_id"
export_archive certbot "$certbot_id"
export_archive alpine "$alpine_id"

# The verifier is executable release tooling, so it must pass the custom
# scanner before it is allowed to verify or bootstrap the official scanner.
scan_custom_archive cosign "$cosign_id"

signature_report="$temporary_dir/reports/cosign-signature.json"
if ! docker run --rm \
    --network host \
    --read-only \
    --user 65532:65532 \
    --security-opt no-new-privileges:true \
    --cap-drop ALL \
    --pids-limit 64 \
    --memory 256m \
    --cpus 1 \
    --env HOME=/tmp \
    --tmpfs /tmp:size=128m,uid=65532,gid=65532,mode=0700 \
    "$cosign_id" verify --timeout 3m \
        --certificate-identity-regexp "$cosign_identity_regexp" \
        --certificate-oidc-issuer "$cosign_issuer" \
        "$official_trivy_reference" > "$signature_report"; then
    echo "SCAN BLOCKED: official Trivy Cosign keyless verification failed" >&2
    exit 4
fi
[ -s "$signature_report" ] || {
    echo "SCAN BLOCKED: official Trivy signature evidence is empty" >&2
    exit 4
}
signature_report_sha256=$(sha256_file "$signature_report")

official_cosign_report="$temporary_dir/reports/official-to-cosign.json"
if ! run_scanner_archive "$official_trivy_id" image --cache-backend memory --offline-scan --timeout 15m \
    --input /evidence/cosign.tar \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet > "$official_cosign_report"; then
    echo "SCAN BLOCKED: signed official scanner could not scan hardened Cosign" >&2
    exit 4
fi
validate_clean_report "$official_cosign_report" official-to-cosign \
    /evidence/cosign.tar container_image "$cosign_id" \
    "$upstream_scanner_version"

official_custom_report="$temporary_dir/reports/official-to-custom.json"
if ! run_scanner_archive "$official_trivy_id" image --cache-backend memory --offline-scan --timeout 15m \
    --input /evidence/custom-scanner.tar \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet > "$official_custom_report"; then
    echo "SCAN BLOCKED: official scanner could not scan custom scanner" >&2
    exit 4
fi
validate_clean_report "$official_custom_report" official-to-custom \
    /evidence/custom-scanner.tar container_image "$scanner_id" \
    "$upstream_scanner_version"

custom_custom_report="$temporary_dir/reports/custom-to-custom.json"
if ! run_scanner_archive "$scanner_id" image --cache-backend memory --offline-scan --timeout 15m \
    --input /evidence/custom-scanner.tar \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet > "$custom_custom_report"; then
    echo "SCAN BLOCKED: custom scanner self-scan operation failed" >&2
    exit 4
fi
validate_clean_report "$custom_custom_report" custom-to-custom \
    /evidence/custom-scanner.tar container_image "$scanner_id" \
    "$custom_scanner_version"

official_baseline_report="$temporary_dir/reports/official-to-official.json"
custom_official_report="$temporary_dir/reports/custom-to-official.json"
if ! run_scanner_archive "$official_trivy_id" image --cache-backend memory --offline-scan --timeout 15m \
    --input /evidence/official-trivy.tar \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet > "$official_baseline_report" \
    || ! run_scanner_archive "$scanner_id" image --cache-backend memory --offline-scan --timeout 15m \
    --input /evidence/official-trivy.tar \
    --skip-db-update --skip-java-db-update \
    --scanners vuln,secret --format json --quiet > "$custom_official_report"; then
    echo "SCAN BLOCKED: official anti-blindness cross-scan operation failed" >&2
    exit 4
fi
official_findings_sha256=$(python3 "$script_dir/validate_trivy_report.py" \
    compare-official-findings \
    --official-report "$official_baseline_report" \
    --custom-report "$custom_official_report" \
    --findings-lock "$findings_lock" \
    --expected-artifact-name /evidence/official-trivy.tar \
    --expected-target "$official_trivy_id") || {
    echo "SCAN BLOCKED: custom scanner anti-blindness evidence differs" >&2
    exit 4
}

scan_custom_archive runtime "$runtime_id"
scan_custom_archive edge "$edge_id"
scan_custom_archive certbot "$certbot_id"
scan_custom_archive alpine "$alpine_id"

version_report="$temporary_dir/reports/version.json"
if ! run_scanner_metadata "$scanner_id" version --format json > "$version_report"; then
    echo "SCAN BLOCKED: custom scanner/database metadata is unavailable" >&2
    exit 4
fi
metadata=$(python3 "$script_dir/validate_trivy_report.py" version \
    --report "$version_report" \
    --expected-scanner-version "$custom_scanner_version") || {
    echo "SCAN BLOCKED: custom scanner/database freshness metadata failed validation" >&2
    exit 4
}
set -- $metadata
[ "$#" -eq 5 ] || {
    echo "SCAN BLOCKED: custom scanner/database freshness metadata is incomplete" >&2
    exit 4
}
scanner_version=$1
db_updated_at=$2
db_downloaded_at=$3
db_next_update=$4
db_schema_version=$5

database_hashes_after=$(python3 "$script_dir/validate_trivy_report.py" database-cache \
    --cache "$temporary_dir/cache" --expected-owner-uid 65532) || exit 4
[ "$database_hashes_after" = "$database_hashes_before" ] || {
    echo "SCAN BLOCKED: read-only vulnerability DB changed during offline scans" >&2
    exit 4
}

python3 "$script_dir/turkey_release_manifest.py" create \
    --output "$manifest_output" \
    --context-sha256 "$context_sha256" \
    --runtime-id "$runtime_id" --edge-id "$edge_id" \
    --certbot-id "$certbot_id" --certbot-reference "$certbot_reference" \
    --alpine-id "$alpine_id" --alpine-reference "$alpine_reference" \
    --trivy-id "$scanner_id" --trivy-reference "$scanner_id" \
    --scanner-dockerfile-sha256 "$scanner_dockerfile_sha256" \
    --scanner-lock-sha256 "$scanner_lock_sha256" \
    --scanner-binary-sha256 "$expected_scanner_binary_sha256" \
    --official-trivy-id "$official_trivy_id" \
    --official-trivy-reference "$official_trivy_reference" \
    --cosign-id "$cosign_id" --cosign-reference "$cosign_id" \
    --official-cosign-reference "$official_cosign_reference" \
    --cosign-dockerfile-sha256 "$cosign_dockerfile_sha256" \
    --cosign-lock-sha256 "$cosign_lock_sha256" \
    --cosign-binary-sha256 "$expected_cosign_binary_sha256" \
    --signature-evidence-sha256 "$signature_report_sha256" \
    --findings-lock-sha256 "$findings_lock_sha256" \
    --official-findings-sha256 "$official_findings_sha256" \
    --scanner-version "$scanner_version" \
    --db-schema-version "$db_schema_version" \
    --db-updated-at "$db_updated_at" \
    --db-downloaded-at "$db_downloaded_at" \
    --db-next-update "$db_next_update" \
    --db-metadata-sha256 "$db_metadata_sha256" \
    --db-sha256 "$db_sha256"

echo "SCAN PASS: signed bootstrap, hardened custom scanner, exact anti-blindness set, and immutable release manifest verified"
