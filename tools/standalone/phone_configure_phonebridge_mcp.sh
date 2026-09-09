#!/bin/sh
set -eu

LOG_PATH="${CHEBY_MCP_CONFIG_LOG:-/sdcard/Download/cheby-standalone-mcp-config.log}"
MCP_PATH="/opt/cheby/connector/cheby_connector/local_mcp.py"
STATE_ROOT="/root/.cheby/phonebridge"
TOKEN_PATH="$STATE_ROOT/local-controller-token"
ARTIFACT_ROOT="$STATE_ROOT/artifacts"
SMOKE_LOG="/tmp/cheby-phonebridge-mcp-smoke.log"

exec >"$LOG_PATH" 2>&1

cleanup() {
  rm -f "$SMOKE_LOG"
}
trap cleanup EXIT HUP INT TERM

echo "gate=standalone_phonebridge_mcp_config"
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
test -r "$MCP_PATH"
test -r "$TOKEN_PATH"
test "$(stat -c '%a' "$TOKEN_PATH")" = "600"
install -d -m 700 "$ARTIFACT_ROOT"

if codex mcp get phonebridge --json >/tmp/cheby-existing-phonebridge-mcp.json 2>/dev/null; then
  echo "phonebridge_mcp_already_configured=true"
  cat /tmp/cheby-existing-phonebridge-mcp.json
  rm -f /tmp/cheby-existing-phonebridge-mcp.json
else
  rm -f /tmp/cheby-existing-phonebridge-mcp.json
  codex mcp add phonebridge \
    --env CHEBY_PHONEBRIDGE_URL=http://127.0.0.1:3437 \
    --env CHEBY_PHONEBRIDGE_TOKEN_FILE="$TOKEN_PATH" \
    --env CHEBY_PHONEBRIDGE_ARTIFACT_DIR="$ARTIFACT_ROOT" \
    -- /usr/bin/python3 -I "$MCP_PATH" phonebridge
fi

codex mcp get phonebridge --json

printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"android_phone_status","arguments":{}}}' \
  | env \
      CHEBY_PHONEBRIDGE_URL=http://127.0.0.1:3437 \
      CHEBY_PHONEBRIDGE_TOKEN_FILE="$TOKEN_PATH" \
      CHEBY_PHONEBRIDGE_ARTIFACT_DIR="$ARTIFACT_ROOT" \
      /usr/bin/python3 -I "$MCP_PATH" phonebridge \
      >"$SMOKE_LOG"

cat "$SMOKE_LOG"
grep -Fq '"name":"phonebridge"' "$SMOKE_LOG"
grep -Fq '"phone_online":true' "$SMOKE_LOG"
grep -Fq '"package_name":"com.chebysight.chebyagent.phonenode.standalone"' "$SMOKE_LOG"
echo "direct_mcp_phone_status=PASS"
echo "outcome=PASS"
