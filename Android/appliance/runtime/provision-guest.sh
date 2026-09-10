#!/bin/sh
set -eu

ASSET_ROOT='/mnt/cheby-assets'
LOCK_FILE="$ASSET_ROOT/runtime.lock"
PHONEBRIDGE_STATE='/root/.cheby/phonebridge'
MCP_PATH='/opt/cheby/connector/cheby_connector/local_mcp.py'

test -r "$LOCK_FILE"
# shellcheck disable=SC1090
. "$LOCK_FILE"
test "$CHEBY_RUNTIME_VERSION" = '4.1.0-dev35'
test "$CODEX_VERSION" = '0.153.4'
CODEX_VERSION_ROOT="/opt/cheby/runtime/codex-$CODEX_VERSION"
python3 -I "$ASSET_ROOT/install-codex-overlay.py" \
  --archive "$ASSET_ROOT/$CODEX_ARCHIVE_ASSET" \
  --sha256 "$CODEX_ARCHIVE_SHA256" --version "$CODEX_VERSION" \
  --runtime-root /opt/cheby/runtime
test -x "$CODEX_VERSION_ROOT/bin/codex"
test -x "$CODEX_VERSION_ROOT/bin/codex-code-mode-host"
test -x "$CODEX_VERSION_ROOT/codex-path/rg"
grep -F "\"version\": \"$CODEX_VERSION\"" "$CODEX_VERSION_ROOT/codex-package.json"
grep -F '"target": "aarch64-unknown-linux-musl"' \
  "$CODEX_VERSION_ROOT/codex-package.json"

# Check the new upstream payload before changing the active version links.
"$CODEX_VERSION_ROOT/bin/codex" --version | grep -Fx "codex-cli $CODEX_VERSION"
"$CODEX_VERSION_ROOT/bin/codex" app-server --help >/dev/null
"$CODEX_VERSION_ROOT/bin/codex-code-mode-host" --help >/dev/null

ln -sfn "$CODEX_VERSION_ROOT" /opt/codex
ln -sfn /opt/codex/bin/codex /usr/local/bin/codex
ln -sfn /opt/codex/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host

install -d -m 755 \
  /opt/cheby/appserver \
  /opt/cheby/bin \
  /opt/cheby/connector/cheby_connector \
  /opt/cheby/phonebridge
install -m 755 "$ASSET_ROOT/start-codex-appserver.sh" \
  /opt/cheby/bin/start-codex-appserver
install -m 755 "$ASSET_ROOT/start-phonebridge.sh" /opt/cheby/bin/start-phonebridge
install -m 644 "$ASSET_ROOT/provider-launcher.py" \
  /opt/cheby/appserver/provider-launcher.py
install -m 644 "$ASSET_ROOT/glm-chat-adapter.py" \
  /opt/cheby/appserver/glm-chat-adapter.py
install -m 644 "$ASSET_ROOT/local_mcp.py" "$MCP_PATH"
install -m 644 "$ASSET_ROOT/ace_memory.py" /opt/cheby/connector/cheby_connector/ace_memory.py
install -d -m 755 /opt/cheby/connector/cheby_connector/ace_core
for ace_file in __init__.py skillbook.py insight_source.py LICENSE UPSTREAM.md; do
  install -m 644 "$ASSET_ROOT/ace-core-$ace_file" "/opt/cheby/connector/cheby_connector/ace_core/$ace_file"
done
install -m 644 "$ASSET_ROOT/mobile-experience-instructions.md" /opt/cheby/appserver/mobile-experience-instructions.md
install -m 644 "$ASSET_ROOT/codex-base-instructions.md" /opt/cheby/appserver/codex-base-instructions.md
install -m 644 "$ASSET_ROOT/phonebridge.mjs" /opt/cheby/phonebridge/phonebridge.mjs

install_skill() {
  skill_name="$1"
  skill_root="/root/.codex/skills/$skill_name"
  skill_asset="$ASSET_ROOT/skill-$skill_name.md"
  metadata_asset="$ASSET_ROOT/skill-$skill_name.openai.yaml"
  test -r "$skill_asset"
  test -r "$metadata_asset"
  grep -F "name: $skill_name" "$skill_asset" >/dev/null
  install -d -m 755 "$skill_root" "$skill_root/agents"
  install -m 644 "$skill_asset" "$skill_root/SKILL.md"
  install -m 644 "$metadata_asset" "$skill_root/agents/openai.yaml"
}

install_skill russia-menu-assistant
install_skill phone-ui-recovery
install_skill yandex-maps-restaurant-finder
install_skill yandex-maps-supermarket-finder
install_skill yandex-maps-haircut-finder
install_skill yandex-maps-massage-finder
install_skill yandex-maps-dog-grooming
install_skill cian-rental-finder
install_skill yandex-go-food-order
install_skill yandex-go-taxi-booker
install_skill yandex-maps-coffee-finder
install_skill yandex-maps-route-planner
install -d -m 755 /root/.codex/skills/yandex-maps-coffee-finder/references
install -m 644 "$ASSET_ROOT/coffee-poster-reference.md" /root/.codex/skills/yandex-maps-coffee-finder/references/poster.md
install -d -m 755 /root/.codex/skills/cian-rental-finder/references
install -m 644 "$ASSET_ROOT/cian-rental-report-reference.md" /root/.codex/skills/cian-rental-finder/references/report.md

python3 -m py_compile "$MCP_PATH"
python3 -m py_compile /opt/cheby/appserver/provider-launcher.py \
  /opt/cheby/appserver/glm-chat-adapter.py
node --check /opt/cheby/phonebridge/phonebridge.mjs
codex --version | grep -F "$CODEX_VERSION"
codex app-server --help >/dev/null
codex-code-mode-host --help >/dev/null

LOCAL_MCP_DATA_ROOT='/root/.codex/local-mcp'
install -d -m 700 \
  "$PHONEBRIDGE_STATE" \
  "$PHONEBRIDGE_STATE/artifacts" \
  "$LOCAL_MCP_DATA_ROOT" \
  "$LOCAL_MCP_DATA_ROOT/memory" \
  "$LOCAL_MCP_DATA_ROOT/skill" \
  "$LOCAL_MCP_DATA_ROOT/skill/skills"
if ! test -s "$PHONEBRIDGE_STATE/local-controller-token"; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))' \
    >"$PHONEBRIDGE_STATE/local-controller-token"
fi
chmod 600 "$PHONEBRIDGE_STATE/local-controller-token"

if ! codex mcp get phonebridge --json >/dev/null 2>&1; then
  codex mcp add phonebridge \
    --env CHEBY_PHONEBRIDGE_URL=http://127.0.0.1:3437 \
    --env CHEBY_PHONEBRIDGE_TOKEN_FILE="$PHONEBRIDGE_STATE/local-controller-token" \
    --env CHEBY_PHONEBRIDGE_ARTIFACT_DIR="$PHONEBRIDGE_STATE/artifacts" \
    --env CHEBY_LOCAL_MCP_DATA_ROOT="$LOCAL_MCP_DATA_ROOT" \
    -- /usr/bin/python3 -I "$MCP_PATH" phonebridge \
    >/dev/null
fi
codex mcp get phonebridge --json >/dev/null

if ! codex mcp get offline_memory --json >/dev/null 2>&1; then
  codex mcp add offline_memory \
    --env CHEBY_LOCAL_MCP_DATA_ROOT="$LOCAL_MCP_DATA_ROOT" \
    -- /usr/bin/python3 -I "$MCP_PATH" memory \
    >/dev/null
fi
codex mcp get offline_memory --json >/dev/null

if ! codex mcp get offline_skill --json >/dev/null 2>&1; then
  codex mcp add offline_skill \
    --env CHEBY_LOCAL_MCP_DATA_ROOT="$LOCAL_MCP_DATA_ROOT" \
    -- /usr/bin/python3 -I "$MCP_PATH" skill \
    >/dev/null
fi
codex mcp get offline_skill --json >/dev/null

if ! codex mcp get ace --json >/dev/null 2>&1; then
  codex mcp add ace \
    --env CHEBY_LOCAL_MCP_DATA_ROOT="$LOCAL_MCP_DATA_ROOT" \
    -- /usr/bin/python3 -I "$MCP_PATH" ace >/dev/null
fi
codex mcp get ace --json >/dev/null
