#!/bin/sh
set -eu

LOG_PATH="${CHEBY_APP_SERVER_INSTALL_LOG:-/sdcard/Download/cheby-standalone-appserver-install.log}"
TRANSFER_ROOT="${CHEBY_TRANSFER_ROOT:-/sdcard/Download}"
SOURCE_PATH="$TRANSFER_ROOT/cheby-start-codex-appserver.sh"
BRIDGE_SOURCE_PATH="$TRANSFER_ROOT/cheby-codex-stdio-bridge.mjs"
EXPECTED_SHA256="ebb3849f6cfe454edb195e08cb12496a59c08943c3a3618fdc03abaf5b37d091"
EXPECTED_BRIDGE_SHA256="4704c08a8d2b2568b20fcab56d0868de2256634f4c6a97a650fd8c22301823d0"
TARGET_PATH="/opt/cheby/bin/start-codex-appserver"
BRIDGE_TARGET_PATH="/opt/cheby/appserver/codex-stdio-bridge.mjs"

exec >"$LOG_PATH" 2>&1

echo "gate=standalone_appserver_runtime_install"
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

actual_sha256="$(sha256sum "$SOURCE_PATH" | awk '{print $1}')"
test "$actual_sha256" = "$EXPECTED_SHA256"
actual_bridge_sha256="$(sha256sum "$BRIDGE_SOURCE_PATH" | awk '{print $1}')"
test "$actual_bridge_sha256" = "$EXPECTED_BRIDGE_SHA256"
install -d -m 755 /opt/cheby/bin /opt/cheby/appserver
install -m 755 "$SOURCE_PATH" "$TARGET_PATH"
install -m 644 "$BRIDGE_SOURCE_PATH" "$BRIDGE_TARGET_PATH"
test "$(sha256sum "$TARGET_PATH" | awk '{print $1}')" = "$EXPECTED_SHA256"
test "$(sha256sum "$BRIDGE_TARGET_PATH" | awk '{print $1}')" = "$EXPECTED_BRIDGE_SHA256"

/usr/bin/node --check "$BRIDGE_TARGET_PATH"
codex app-server --help >/dev/null

rm -f "$SOURCE_PATH" "$BRIDGE_SOURCE_PATH"
echo "start_script_sha256=$EXPECTED_SHA256"
echo "stdio_bridge_sha256=$EXPECTED_BRIDGE_SHA256"
echo "outcome=PASS"
