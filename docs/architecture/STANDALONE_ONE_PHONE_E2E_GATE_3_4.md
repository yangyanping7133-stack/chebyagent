# Standalone One-Phone End-to-End Gate 3.4

Status: PASS on the target Android phone for UI, local Codex, MCP, ChebyNode status, a real Android action, and UI reconnect.

This checkpoint completes the first real single-phone path. It does not claim final runtime packaging, final credential enrollment, reboot recovery, or protection against an explicit Android force-stop of Termux.

## Final topology

```text
ChebyAgent standalone UI
  -> bearer-authenticated ws://127.0.0.1:4500
  -> Cheby authenticated WebSocket-to-stdio bridge
  -> official codex app-server over stdio
  -> short-lived PhoneBridge MCP child
  -> authenticated PhoneBridge on 127.0.0.1:3437
  -> standalone ChebyNode
  -> Android Accessibility executor
```

The UI remains the Android lifecycle owner. It invokes the Termux runtime entrypoint with `RUN_COMMAND`, and the entrypoint remains in the foreground so Termux does not kill its PRoot child when the command service exits.

Direct `codex app-server --listen ws://...` was rejected for this gate after it accepted `turn/start` but failed to complete the turn on the target phone. The [Codex app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) documents stdio as the supported local transport and TCP WebSocket as experimental. The bridge therefore exposes only the narrow authenticated loopback WebSocket required by the Android client and starts an official stdio app-server child for the authenticated connection.

The bridge:

- accepts IPv4 loopback only;
- compares only a SHA-256 bearer verifier using a timing-safe comparison;
- permits one authenticated client and replaces the old client on an authenticated reconnect;
- validates bounded, masked WebSocket frames and bounded JSON lines;
- suppresses child stderr and never writes bearer values, raw JSON, prompts, tool arguments, or results to logs;
- owns and terminates the corresponding Codex child with the connection.

## Android fixes

Two real-device lifecycle defects were fixed:

1. The Termux command now runs the runtime entrypoint with `--foreground`. This prevents `proot-distro --kill-on-exit` from killing a background Codex process as soon as the external command returns.
2. A newly-created empty Codex thread is retained locally until it appears in authoritative `thread/list`. Codex omits an empty thread from the list until its first turn is persisted, so a name-triggered refresh could previously erase the new row and disable the composer.

The second case has a fixture regression test that emits `thread/name/updated`, returns a second empty `thread/list`, and asserts that the provisional row remains visible.

## Runtime recovery

The on-device entrypoint now repairs two local prerequisites safely:

- PhoneBridge creates its controller token on-device with mode `0600` if the token is missing.
- The Codex runtime recreates the exact local `phonebridge` stdio MCP registration if it is missing.

Health is checked before stale PID files. A stale PID file is removed without killing a potentially unrelated reused PID.

Normal UI process restart is supported: a newly authenticated connection replaces the stale UI connection, starts a fresh stdio app-server child, and reloads persisted Codex threads. Android's explicit force-stop semantics remain a boundary: force-stopping Termux marks the package stopped, so another app cannot restart it until Termux is opened again.

## Explicit permission gate

The Founder explicitly approved both permission-bearing actions for this checkpoint:

- `com.termux.permission.RUN_COMMAND` granted to `com.cheby.codex.mobile.standalone`;
- `android.permission.WRITE_SECURE_SETTINGS` granted to `com.chebysight.chebyagent.phonenode.standalone`, followed by enabling and binding `com.chebysight.chebyagent.android.AgentAccessibilityService`.

After the final APK overwrite, `RUN_COMMAND` remained granted, `WRITE_SECURE_SETTINGS` remained granted, and the standalone Accessibility service remained enabled and bound.

## Real-device evidence

The complete UI path passed twice with model-selected MCP calls:

1. `android_phone_status` returned the standalone Node online. The MCP item completed and the UI rendered `CHEBY_UI_MCP_STATUS_OK`.
2. `android_open_app` opened `com.android.settings`. Android reported `com.android.settings/.HWSettings` in the foreground; the MCP item completed and the UI rendered `CHEBY_UI_NODE_ACTION_OK`.

The persisted thread contained completed `mcpToolCall` items for both tools and a completed turn. After force-stopping and relaunching only the standalone UI, the app reconnected immediately and restored the thread. After installing the final APK with `adb install -r`, the app reconnected on the second two-second poll and restored the completed MCP conversation.

Evidence screenshots and protocol probes are retained outside the repository under `/tmp/cheby-standalone-evidence/`.

## Build and artifact verification

- `:app:testStandaloneUnitTest`: PASS.
- `:app:assembleStandalone`: PASS.
- `:app:lintStandalone`: PASS.
- Distributed `:app:compileDebugKotlin`: PASS.
- Shell syntax validation for the four modified installer/start scripts: PASS.
- Phone Node syntax validation for `phone_codex_stdio_bridge.mjs`: PASS.
- Standalone APK SHA-256: `2de4b6d550dc8a0ea3a187dad9c3c22e1cf687327dd1fee9eeeeff94b71f5f97`.
- Installed base APK hash matched the local standalone APK: PASS.
- Signing certificate SHA-256: `190d4bd967692d845a233995b924b150449f98a66babec481babdebe60266ccb`.
- Installed package/version: `com.cheby.codex.mobile.standalone`, `0.4.5-standalone` (`12`).
- WebSocket-to-stdio bridge SHA-256: `4704c08a8d2b2568b20fcab56d0868de2256634f4c6a97a650fd8c22301823d0`.
- Runtime start script SHA-256: `ebb3849f6cfe454edb195e08cb12496a59c08943c3a3618fdc03abaf5b37d091`.
- PhoneBridge start script SHA-256: `71acc635eb292316bc338efde351ad5f6fd1e5c334e0f3f20ccb29a9e8d997f2`.

## Remaining product work

The current gate installation still stages runtime files through exact, hash-checked `/sdcard/Download` transfer files. The product path must package these files as signed APK assets and refresh them through a loopback-only installer flow; the UI must never depend on shared storage.

Codex currently uses the Founder-approved copied ChatGPT login from the existing server. Only `auth.json` was copied; server configuration, sessions, MCP configuration, keys, and other state were not. Final sign-in, refresh, rotation, logout, and recovery remain separate work.

Long-duration keepalive, reboot recovery, battery-policy handling, runtime upgrade rollback, and corrupted-state recovery are not yet closed by this checkpoint.
