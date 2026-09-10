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

# Glibc inside PRoot cannot consult Android's netd resolver. Refresh the guest
# resolver from the active Android network before every entry so cellular use
# does not depend on a stale public DNS server or a Mac-side proxy.
RESOLV_STAGING="$ROOTFS/etc/.resolv.conf.cheby.$$"
: >"$RESOLV_STAGING"
for dns_key in \
  net.dns1 net.dns2 \
  net.rmnet0.dns1 net.rmnet0.dns2 \
  net.rmnet_data0.dns1 net.rmnet_data0.dns2 \
  net.wlan0.dns1 net.wlan0.dns2
do
  dns_value="$(/system/bin/getprop "$dns_key" 2>/dev/null || true)"
  case "$dns_value" in
    ''|*[!0-9A-Fa-f:.]*) continue ;;
  esac
  printf 'nameserver %s\n' "$dns_value" >>"$RESOLV_STAGING"
done
if test -s "$RESOLV_STAGING"; then
  chmod 644 "$RESOLV_STAGING"
  rm -f "$ROOTFS/etc/resolv.conf"
  mv "$RESOLV_STAGING" "$ROOTFS/etc/resolv.conf"
else
  rm -f "$RESOLV_STAGING"
fi

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
