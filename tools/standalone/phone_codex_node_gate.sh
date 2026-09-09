#!/bin/sh
set -eu

LOG_PATH="${CHEBY_CODEX_NODE_GATE_LOG:-/sdcard/Download/cheby-standalone-codex-node-gate.log}"
RUN_LOG="/tmp/cheby-standalone-codex-node-run.log"

exec >"$LOG_PATH" 2>&1

cleanup() {
  rm -f "$RUN_LOG"
}
trap cleanup EXIT HUP INT TERM

echo "gate=standalone_codex_to_chebynode"
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
codex mcp get phonebridge --json

codex exec \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  "Call the phonebridge MCP tool android_phone_status. If and only if phone_online is true and the connected package is com.chebysight.chebyagent.phonenode.standalone, reply exactly CHEBY_MCP_NODE_ONLINE. Do not answer from memory." \
  >"$RUN_LOG" 2>&1
cat "$RUN_LOG"

grep -Fq 'android_phone_status' "$RUN_LOG"
grep -Fq 'CHEBY_MCP_NODE_ONLINE' "$RUN_LOG"
echo "codex_mcp_phone_status=PASS"
echo "outcome=PASS"
