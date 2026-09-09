#!/bin/sh
set -eu

LOG_PATH="${CHEBY_TOOL_RUNTIME_LOG:-/sdcard/Download/cheby-standalone-tool-runtime.log}"
TRANSFER_ROOT="${CHEBY_TRANSFER_ROOT:-/sdcard/Download}"
MCP_SOURCE="$TRANSFER_ROOT/cheby-local-mcp.py"
BRIDGE_SOURCE="$TRANSFER_ROOT/cheby-phonebridge.mjs"
START_SOURCE="$TRANSFER_ROOT/cheby-start-phonebridge.sh"
MCP_SHA256="ff7ada39551ba3837c3ca47d8fe8bf617641cea07c1e762cce3471cb3dc5e3a3"
BRIDGE_SHA256="562f66266da07844255ccb9c0fcf9346c62ab00807a58c8b26be34bd271af9f9"
START_SHA256="71acc635eb292316bc338efde351ad5f6fd1e5c334e0f3f20ccb29a9e8d997f2"
RUNTIME_ROOT="/opt/cheby"
STATE_ROOT="/root/.cheby/phonebridge"

exec >"$LOG_PATH" 2>&1

verify_sha256() {
  expected="$1"
  path="$2"
  actual="$(sha256sum "$path" | awk '{print $1}')"
  test "$actual" = "$expected"
}

echo "gate=standalone_tool_runtime_prepare"
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

verify_sha256 "$MCP_SHA256" "$MCP_SOURCE"
verify_sha256 "$BRIDGE_SHA256" "$BRIDGE_SOURCE"
verify_sha256 "$START_SHA256" "$START_SOURCE"
echo "transfer_hashes=PASS"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 nodejs curl ca-certificates

install -d -m 755 "$RUNTIME_ROOT/connector/cheby_connector" "$RUNTIME_ROOT/phonebridge" "$RUNTIME_ROOT/bin"
install -m 644 "$MCP_SOURCE" "$RUNTIME_ROOT/connector/cheby_connector/local_mcp.py"
install -m 644 "$BRIDGE_SOURCE" "$RUNTIME_ROOT/phonebridge/phonebridge.mjs"
install -m 755 "$START_SOURCE" "$RUNTIME_ROOT/bin/start-phonebridge"

verify_sha256 "$MCP_SHA256" "$RUNTIME_ROOT/connector/cheby_connector/local_mcp.py"
verify_sha256 "$BRIDGE_SHA256" "$RUNTIME_ROOT/phonebridge/phonebridge.mjs"
verify_sha256 "$START_SHA256" "$RUNTIME_ROOT/bin/start-phonebridge"

install -d -m 700 "$STATE_ROOT" "$STATE_ROOT/artifacts"
if ! test -s "$STATE_ROOT/local-controller-token"; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$STATE_ROOT/local-controller-token"
fi
chmod 600 "$STATE_ROOT/local-controller-token"

python3 --version
node --version
curl --version | sed -n '1p'

"$RUNTIME_ROOT/bin/start-phonebridge"
curl -fsS --max-time 3 http://127.0.0.1:3437/health

rm -f "$MCP_SOURCE" "$BRIDGE_SOURCE" "$START_SOURCE"
echo "outcome=PASS"
