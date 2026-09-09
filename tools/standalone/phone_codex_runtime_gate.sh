#!/bin/sh
set -eu

LOG_PATH="${CHEBY_RUNTIME_GATE_LOG:-/sdcard/Download/cheby-standalone-codex-runtime-gate.log}"
HOST_PATH="${CHEBY_CODE_MODE_HOST:-/usr/local/bin/codex-code-mode-host}"
EXPECTED_HOST_SHA256="${CHEBY_CODE_MODE_HOST_SHA256:-c8fd26e2ddb0243d79d7c3dfa8bcd47b6a30b14695083790fc51884e82e8ebc2}"
PROOF_PATH="/tmp/cheby-standalone-codex-tool-proof"
RUN_LOG="/tmp/cheby-standalone-codex-tool-run.log"

exec >"$LOG_PATH" 2>&1

cleanup() {
  rm -f "$PROOF_PATH" "$RUN_LOG"
}
trap cleanup EXIT HUP INT TERM

echo "gate=standalone_codex_runtime"
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

test -x "$HOST_PATH"
actual_host_sha256="$(sha256sum "$HOST_PATH" | awk '{print $1}')"
test "$actual_host_sha256" = "$EXPECTED_HOST_SHA256"
echo "code_mode_host_sha256=$actual_host_sha256"

"$HOST_PATH" --help >/dev/null
codex --version
codex login status

rm -f "$PROOF_PATH" "$RUN_LOG"
codex exec \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  "Use a local shell or file tool to create $PROOF_PATH containing exactly CHEBY_TOOL_OK with no extra whitespace. Then reply exactly CHEBY_TOOL_OK." \
  >"$RUN_LOG" 2>&1
cat "$RUN_LOG"

test "$(cat "$PROOF_PATH")" = "CHEBY_TOOL_OK"
if grep -Eiq 'could not find.*codex-code-mode-host|codex-code-mode-host.*not found' "$RUN_LOG"; then
  echo "unexpected_missing_code_mode_host_warning=true"
  exit 1
fi

rm -f "$PROOF_PATH"
test ! -e "$PROOF_PATH"
echo "tool_proof=created_read_deleted"
echo "outcome=PASS"
