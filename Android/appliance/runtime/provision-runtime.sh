#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

PREFIX='/data/data/com.termux/files/usr'
HOME_ROOT='/data/data/com.termux/files/home'
ASSET_ROOT="$HOME_ROOT/.cheby/provision/assets"
LOCK_FILE="$ASSET_ROOT/runtime.lock"
STATE_ROOT="$HOME_ROOT/.cheby/runtime"
READY_MARKER="$STATE_ROOT/ready-4.1.0-dev34"
FAILED_MARKER="$STATE_ROOT/provision-failed"
LOG_FILE="$STATE_ROOT/provision.log"
ENTER_DEBIAN="$PREFIX/bin/cheby-enter-debian"
RUNTIME_ROOT="$PREFIX/var/lib/cheby-runtime"
DEBIAN_ROOT="$RUNTIME_ROOT/debian-rootfs"
DEBIAN_NEXT="$RUNTIME_ROOT/.debian-rootfs.next"
DEBIAN_PREVIOUS="$RUNTIME_ROOT/.debian-rootfs.previous"
DEBIAN_FAILED="$RUNTIME_ROOT/.debian-rootfs.failed"
MIGRATION_ACTIVE=0
STEP='starting'

mkdir -p "$STATE_ROOT"
chmod 700 "$STATE_ROOT"
: >"$LOG_FILE"
exec >>"$LOG_FILE" 2>&1
chmod 600 "$LOG_FILE"

fail() {
  status=$?
  trap - ERR
  set +e
  if test "$MIGRATION_ACTIVE" = 1 && test -d "$DEBIAN_PREVIOUS"; then
    rm -rf "$DEBIAN_FAILED"
    if test -e "$DEBIAN_ROOT"; then
      mv "$DEBIAN_ROOT" "$DEBIAN_FAILED"
    fi
    mv "$DEBIAN_PREVIOUS" "$DEBIAN_ROOT"
    rm -rf "$DEBIAN_FAILED"
  fi
  temporary="$FAILED_MARKER.tmp"
  printf '%s\n' "$STEP" >"$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$FAILED_MARKER"
  exit "$status"
}
trap fail ERR

exec 9>"$STATE_ROOT/provision.lock"
if ! "$PREFIX/bin/flock" -n 9; then
  exit 0
fi

STEP='verify_assets'
test -r "$LOCK_FILE"
# shellcheck disable=SC1090
. "$LOCK_FILE"
test "$CHEBY_RUNTIME_VERSION" = '4.1.0-dev34'
test "$PROOT_VERSION" = '5.1.107.89'
test "$BASE_CODEX_VERSION" = '0.147.0'
test "$CODEX_VERSION" = '0.153.4'
test "$CODEX_ARCHIVE_ASSET" = 'codex-linux-arm64.tgz'
test "$TERMUX_PROOT_OVERLAY_ASSET" = 'termux-proot-overlay-aarch64.tar.zst'
test "$DEBIAN_ROOTFS_ASSET" = 'debian-rootfs-aarch64.tar.zst'
(
  cd "$ASSET_ROOT"
  "$PREFIX/bin/sha256sum" -c runtime-assets.sha256
)
printf '%s  %s\n' \
  "$TERMUX_PROOT_OVERLAY_SHA256" \
  "$ASSET_ROOT/$TERMUX_PROOT_OVERLAY_ASSET" | "$PREFIX/bin/sha256sum" -c -
printf '%s  %s\n' \
  "$DEBIAN_ROOTFS_SHA256" \
  "$ASSET_ROOT/$DEBIAN_ROOTFS_ASSET" | "$PREFIX/bin/sha256sum" -c -
printf '%s  %s\n' \
  "$CODEX_ARCHIVE_SHA256" \
  "$ASSET_ROOT/$CODEX_ARCHIVE_ASSET" | "$PREFIX/bin/sha256sum" -c -

if test -r "$READY_MARKER" && test "$(cat "$READY_MARKER")" = "$CHEBY_RUNTIME_VERSION"; then
  rm -f "$FAILED_MARKER"
  exit 0
fi

STEP='install_proot_overlay'
"$PREFIX/bin/zstd" -q -d -c "$ASSET_ROOT/$TERMUX_PROOT_OVERLAY_ASSET" | \
  "$PREFIX/bin/tar" --no-same-owner -xpf - -C "$PREFIX"
test -x "$PREFIX/bin/proot"
test -r "$PREFIX/lib/libandroid-shmem.so"
test -r "$PREFIX/lib/libtalloc.so.2"
install -m 700 "$ASSET_ROOT/enter-debian.sh" "$ENTER_DEBIAN"

STEP='install_debian_rootfs'
mkdir -p "$RUNTIME_ROOT"

# Recover an interrupted migration before beginning a new attempt. The previous
# runtime remains authoritative until the replacement reaches the ready marker.
if test -e "$DEBIAN_PREVIOUS"; then
  test -d "$DEBIAN_PREVIOUS"
  test ! -L "$DEBIAN_PREVIOUS"
  rm -rf "$DEBIAN_FAILED"
  if test -e "$DEBIAN_ROOT"; then
    mv "$DEBIAN_ROOT" "$DEBIAN_FAILED"
  fi
  mv "$DEBIAN_PREVIOUS" "$DEBIAN_ROOT"
  rm -rf "$DEBIAN_FAILED"
fi

install_rootfs_next() {
  rm -rf "$DEBIAN_NEXT"
  mkdir -p "$DEBIAN_NEXT"
  "$PREFIX/bin/zstd" -q -d -c "$ASSET_ROOT/$DEBIAN_ROOTFS_ASSET" | \
    "$PREFIX/bin/tar" --no-same-owner -xpf - -C "$DEBIAN_NEXT"
  test -x "$DEBIAN_NEXT/bin/sh"
  test -x "$DEBIAN_NEXT/opt/cheby/runtime/codex-0.147.0/bin/codex"
  test -x "$DEBIAN_NEXT/opt/cheby/runtime/codex-0.147.0/bin/codex-code-mode-host"
  test -x "$DEBIAN_NEXT/usr/bin/node"
  test -x "$DEBIAN_NEXT/usr/bin/python3"
  test ! -e "$DEBIAN_NEXT/root/.codex"
  test ! -e "$DEBIAN_NEXT/root/.cheby"
  printf '%s\n' "$DEBIAN_ROOTFS_SHA256" >"$DEBIAN_NEXT/.cheby-runtime-pack"
  chmod 600 "$DEBIAN_NEXT/.cheby-runtime-pack"
}

if test -r "$DEBIAN_ROOT/.cheby-runtime-pack" && \
    test "$(cat "$DEBIAN_ROOT/.cheby-runtime-pack")" = "$DEBIAN_ROOTFS_SHA256"; then
  :
elif ! test -e "$DEBIAN_ROOT"; then
  install_rootfs_next
  mv "$DEBIAN_NEXT" "$DEBIAN_ROOT"
else
  STEP='migrate_debian_rootfs'
  test -d "$DEBIAN_ROOT"
  test ! -L "$DEBIAN_ROOT"
  test -x "$DEBIAN_ROOT/bin/sh"
  test -d "$DEBIAN_ROOT/opt/cheby"
  test ! -e "$DEBIAN_PREVIOUS"
  install_rootfs_next

  # Preserve only product-owned durable state. Provider ciphertext remains in
  # Android app storage and never crosses this rootfs migration boundary.
  for relative in .codex .cheby; do
    source_path="$DEBIAN_ROOT/root/$relative"
    if test -e "$source_path"; then
      test -d "$source_path"
      test ! -L "$source_path"
      test ! -e "$DEBIAN_NEXT/root/$relative"
      "$PREFIX/bin/cp" -a "$source_path" "$DEBIAN_NEXT/root/"
    fi
  done

  MIGRATION_ACTIVE=1
  mv "$DEBIAN_ROOT" "$DEBIAN_PREVIOUS"
  mv "$DEBIAN_NEXT" "$DEBIAN_ROOT"
fi
test "$(cat "$DEBIAN_ROOT/.cheby-runtime-pack")" = "$DEBIAN_ROOTFS_SHA256"

STEP='finalize_guest'
CHEBY_ASSET_BIND="$ASSET_ROOT" \
  "$ENTER_DEBIAN" /bin/sh /mnt/cheby-assets/provision-guest.sh

STEP='complete'
temporary="$READY_MARKER.tmp"
printf '%s\n' "$CHEBY_RUNTIME_VERSION" >"$temporary"
chmod 600 "$temporary"
mv -f "$temporary" "$READY_MARKER"
MIGRATION_ACTIVE=0
trap - ERR
rm -rf "$DEBIAN_PREVIOUS" || true
rm -f \
  "$ASSET_ROOT/$TERMUX_PROOT_OVERLAY_ASSET" \
  "$ASSET_ROOT/$DEBIAN_ROOTFS_ASSET" \
  "$ASSET_ROOT/$CODEX_ARCHIVE_ASSET" \
  "$FAILED_MARKER" || true
