#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'usage: capture_embedded_runtime_bundle.sh --adb PATH [--serial SERIAL] --output DIR' >&2
  exit 2
}

ADB_BIN=''
DEVICE_SERIAL=''
OUTPUT_DIR=''
while (($#)); do
  case "$1" in
    --adb)
      (($# >= 2)) || usage
      ADB_BIN=$2
      shift 2
      ;;
    --serial)
      (($# >= 2)) || usage
      DEVICE_SERIAL=$2
      shift 2
      ;;
    --output)
      (($# >= 2)) || usage
      OUTPUT_DIR=$2
      shift 2
      ;;
    *) usage ;;
  esac
done

test -x "$ADB_BIN" || usage
test -n "$OUTPUT_DIR" || usage

ADB=("$ADB_BIN")
if test -n "$DEVICE_SERIAL"; then
  ADB+=(-s "$DEVICE_SERIAL")
fi

PREFIX='/data/data/com.termux/files/usr'
DEB_CACHE='/data/data/com.termux/cache/apt/archives'
SOURCE_ROOT="$PREFIX/var/lib/proot-distro/containers/debian/rootfs"
OVERLAY_STAGE="$PREFIX/tmp/cheby-overlay-4.1-stage"
OVERLAY_DEVICE="$PREFIX/tmp/cheby-overlay-4.1.tar.zst"
ROOTFS_DEVICE="$PREFIX/tmp/cheby-debian-rootfs-4.1.tar.zst"
OVERLAY_OUTPUT="$OUTPUT_DIR/termux-proot-overlay-aarch64.tar.zst"
ROOTFS_OUTPUT="$OUTPUT_DIR/debian-rootfs-aarch64.tar.zst"
OVERLAY_PART="$OVERLAY_OUTPUT.part"
ROOTFS_PART="$ROOTFS_OUTPUT.part"

cleanup_device() {
  "${ADB[@]}" shell run-as com.termux "$PREFIX/bin/rm" -rf "$OVERLAY_STAGE" \
    >/dev/null 2>&1 || true
  "${ADB[@]}" shell run-as com.termux "$PREFIX/bin/rm" -f \
    "$OVERLAY_DEVICE" "$ROOTFS_DEVICE" >/dev/null 2>&1 || true
  rm -f "$OVERLAY_PART" "$ROOTFS_PART"
}

test ! -e "$OVERLAY_OUTPUT"
test ! -e "$ROOTFS_OUTPUT"
test ! -e "$OVERLAY_PART"
test ! -e "$ROOTFS_PART"
mkdir -p "$OUTPUT_DIR"

"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/test" -x "$SOURCE_ROOT/bin/sh"
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/test" ! -e "$OVERLAY_STAGE"
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/test" ! -e "$OVERLAY_DEVICE"
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/test" ! -e "$ROOTFS_DEVICE"
trap cleanup_device EXIT

"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/sha256sum" \
  "$DEB_CACHE/proot_5.1.107.89_aarch64.deb" | \
  grep -F 'ec9fe38c50cfd49dd31fe360ffbcc3124a945dc1ea16293a8a769303dd724f46' \
  >/dev/null
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/sha256sum" \
  "$DEB_CACHE/libandroid-shmem_0.7_aarch64.deb" | \
  grep -F '0da3a24d558b93c92bcf8d611e0826a99ff96e396b148e6cdf33b47c47c57ff6' \
  >/dev/null
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/sha256sum" \
  "$DEB_CACHE/libtalloc_2.4.3_aarch64.deb" | \
  grep -F 'ac81ad623d74c209718b9f3acb2dd702cc8a88c431e820d212229910b4db29da' \
  >/dev/null

"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/mkdir" -m 700 "$OVERLAY_STAGE"
for package_name in \
  proot_5.1.107.89_aarch64.deb \
  libandroid-shmem_0.7_aarch64.deb \
  libtalloc_2.4.3_aarch64.deb; do
  "${ADB[@]}" shell run-as com.termux "$PREFIX/bin/env" PATH="$PREFIX/bin" \
    "$PREFIX/bin/dpkg-deb" -x "$DEB_CACHE/$package_name" "$OVERLAY_STAGE"
done
"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/env" PATH="$PREFIX/bin" \
  "$PREFIX/bin/tar" --sort=name --mtime=@1735689600 --owner=0 --group=0 \
  --numeric-owner --zstd -C "$OVERLAY_STAGE/data/data/com.termux/files/usr" \
  -cf "$OVERLAY_DEVICE" .

"${ADB[@]}" shell run-as com.termux "$PREFIX/bin/env" PATH="$PREFIX/bin" \
  ZSTD_CLEVEL=7 "$PREFIX/bin/tar" --sort=name --owner=0 --group=0 \
  --numeric-owner --zstd \
  --exclude=./root \
  --exclude=./home \
  --exclude=./tmp \
  --exclude=./run \
  --exclude=./var/cache \
  --exclude=./var/lib/apt/lists \
  --exclude=./var/log \
  --exclude=./var/tmp \
  --exclude=./dev \
  --exclude=./proc \
  --exclude=./sys \
  --exclude=./data \
  --exclude=./storage \
  --exclude=./apex \
  --exclude=./odm \
  --exclude=./system \
  --exclude=./vendor \
  --exclude=./etc/machine-id \
  --exclude=./etc/ssh/ssh_host_key \
  --exclude=./etc/ssh/ssh_host_rsa_key \
  --exclude=./etc/ssh/ssh_host_ecdsa_key \
  --exclude=./etc/ssh/ssh_host_ed25519_key \
  -C "$SOURCE_ROOT" -cf "$ROOTFS_DEVICE" .

if "${ADB[@]}" exec-out run-as com.termux "$PREFIX/bin/env" PATH="$PREFIX/bin" \
  "$PREFIX/bin/tar" --zstd -tf "$ROOTFS_DEVICE" | \
  grep -Eq '^\./(root|home|tmp|run|var/cache|var/lib/apt/lists|var/log|var/tmp|dev|proc|sys|data|storage|apex|odm|system|vendor)(/|$)'; then
  echo 'captured rootfs contains a forbidden state path' >&2
  exit 1
fi
"${ADB[@]}" exec-out run-as com.termux "$PREFIX/bin/env" PATH="$PREFIX/bin" \
  "$PREFIX/bin/tar" --zstd -tf "$ROOTFS_DEVICE" | \
  grep -Fx './opt/cheby/runtime/codex-0.147.0/bin/codex' >/dev/null

"${ADB[@]}" exec-out run-as com.termux "$PREFIX/bin/cat" "$OVERLAY_DEVICE" \
  >"$OVERLAY_PART"
"${ADB[@]}" exec-out run-as com.termux "$PREFIX/bin/cat" "$ROOTFS_DEVICE" \
  >"$ROOTFS_PART"
mv "$OVERLAY_PART" "$OVERLAY_OUTPUT"
mv "$ROOTFS_PART" "$ROOTFS_OUTPUT"

shasum -a 256 "$OVERLAY_OUTPUT" "$ROOTFS_OUTPUT"
