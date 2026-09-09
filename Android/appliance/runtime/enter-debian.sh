#!/data/data/com.termux/files/usr/bin/sh
set -eu

PREFIX='/data/data/com.termux/files/usr'
FILES_ROOT='/data/data/com.termux/files'
ROOTFS="$PREFIX/var/lib/cheby-runtime/debian-rootfs"
PROOT="$PREFIX/bin/proot"

test -x "$PROOT"
test -x "$ROOTFS/bin/sh"

mkdir -p \
  "$ROOTFS/.l2s" \
  "$ROOTFS/dev/shm" \
  "$ROOTFS/home" \
  "$ROOTFS/mnt/cheby-assets" \
  "$ROOTFS/proc" \
  "$ROOTFS/root" \
  "$ROOTFS/run" \
  "$ROOTFS/sys" \
  "$ROOTFS/tmp"
chmod 700 "$ROOTFS/root"
chmod 1777 "$ROOTFS/dev/shm" "$ROOTFS/tmp"

if test -n "${CHEBY_ASSET_BIND:-}"; then
  case "$CHEBY_ASSET_BIND" in
    "$FILES_ROOT"/*) ;;
    *)
      echo 'CHEBY_ASSET_BIND must stay inside the private Termux prefix' >&2
      exit 2
      ;;
  esac
  test -d "$CHEBY_ASSET_BIND"
  set -- "--bind=$CHEBY_ASSET_BIND:/mnt/cheby-assets" "$@"
fi

exec "$PREFIX/bin/env" -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME=/root \
  USER=root \
  TERM="${TERM:-xterm-256color}" \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  TMPDIR=/tmp \
  PROOT_L2S_DIR="$ROOTFS/.l2s" \
  "$PROOT" \
  --kill-on-exit \
  --link2symlink \
  -L \
  --change-id=0:0 \
  --rootfs="$ROOTFS" \
  --cwd=/root \
  --bind=/dev \
  --bind=/proc \
  --bind=/sys \
  --bind=/dev/urandom:/dev/random \
  --bind="$ROOTFS/tmp:/dev/shm" \
  "$@"
