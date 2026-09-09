#!/usr/bin/env bash
set -euo pipefail

release_tag="${1:-v0.7.0}"
release_repo="${CHEBY_RELEASE_REPO:-yangyanping7133-stack/chebyagent}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
download_dir="$(mktemp -d "${TMPDIR:-/tmp}/chebyagent-release.XXXXXX")"
trap 'rm -rf -- "$download_dir"' EXIT

command -v gh >/dev/null || {
  echo "GitHub CLI (gh) is required." >&2
  exit 1
}

gh release download "$release_tag" \
  --repo "$release_repo" \
  --dir "$download_dir" \
  --pattern 'debian-rootfs-aarch64.tar.zst' \
  --pattern 'termux-proot-overlay-aarch64.tar.zst' \
  --pattern 'codex-linux-arm64.tgz' \
  --pattern 'bootstrap-aarch64.zip'

verify() {
  local expected="$1"
  local file="$2"
  local actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "SHA-256 mismatch for $(basename "$file")" >&2
    exit 1
  fi
}

verify ef6acc3d5842700dfc879e3ca83532a4b2c495ebb8a4a7cac645cbfd7aa56c7e \
  "$download_dir/debian-rootfs-aarch64.tar.zst"
verify 334a0e6aa93cf3264f416879d4a95ee5ad9df52a9c202f12436b07eaf7bc067c \
  "$download_dir/termux-proot-overlay-aarch64.tar.zst"
verify 439c0dd0d6923f607b4e5cd1e3079c12f0b86f6e5007f07e377d6ad25e2d7bb9 \
  "$download_dir/codex-linux-arm64.tgz"
verify c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb \
  "$download_dir/bootstrap-aarch64.zip"

mkdir -p \
  "$repo_root/artifacts/private/runtime/standalone-4.1" \
  "$repo_root/artifacts/private/runtime/codex-0.153.4" \
  "$repo_root/third_party/termux-app/app/src/main/cpp"
install -m 0644 "$download_dir/debian-rootfs-aarch64.tar.zst" \
  "$repo_root/artifacts/private/runtime/standalone-4.1/debian-rootfs-aarch64.tar.zst"
install -m 0644 "$download_dir/termux-proot-overlay-aarch64.tar.zst" \
  "$repo_root/artifacts/private/runtime/standalone-4.1/termux-proot-overlay-aarch64.tar.zst"
install -m 0644 "$download_dir/codex-linux-arm64.tgz" \
  "$repo_root/artifacts/private/runtime/codex-0.153.4/codex-linux-arm64.tgz"
install -m 0644 "$download_dir/bootstrap-aarch64.zip" \
  "$repo_root/third_party/termux-app/app/src/main/cpp/bootstrap-aarch64.zip"

echo "Verified release inputs installed for $release_tag."
