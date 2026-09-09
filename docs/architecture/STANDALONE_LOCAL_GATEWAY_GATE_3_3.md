# Standalone Local Gateway Gate 3.3

> Historical checkpoint. Gate 3.4 replaces the direct experimental Codex WebSocket listener with an authenticated loopback WebSocket-to-stdio bridge and completes the real-device UI-to-Node gate. See [Standalone One-Phone End-to-End Gate 3.4](STANDALONE_ONE_PHONE_E2E_GATE_3_4.md).

Status: PASS for the Android adapter, authenticated protocol fixture, standalone build, and on-device runtime entrypoint installation.

The final real-device conversation gate is intentionally pending the separately approved Termux `RUN_COMMAND` runtime permission. No permission was granted by this checkpoint.

## Product path

```text
ChebyAgent standalone UI
  -> LocalCodexGateway
  -> authenticated ws://127.0.0.1:4500
  -> Codex app-server 0.147.x
  -> configured stdio PhoneBridge MCP
  -> local PhoneBridge
  -> standalone ChebyNode
```

`GatewayFactory` selects this path only when `BuildConfig.STANDALONE_MODE` is true. Distributed builds continue to select their existing Relay gateway.

## Local authentication

- ChebyAgent generates a 48-byte random capability token.
- The raw bearer is AES-GCM encrypted under a non-exportable AndroidKeyStore key.
- The raw bearer is used only in the loopback WebSocket handshake.
- Termux receives only the 64-character SHA-256 verifier.
- Codex app-server starts with `--ws-auth capability-token --ws-token-sha256 HEX`.
- Neither the token nor verifier is rendered in the UI or written to application logs.

The standalone APK permits cleartext traffic because the current Codex WebSocket listener is plain WebSocket. The production endpoint is a private constant fixed to IPv4 loopback; it is not user-configurable and is bearer-authenticated. The distributed APK remains on its existing TLS policy.

## Protocol adapter

The adapter follows the Codex app-server v2 lifecycle:

1. WebSocket bearer handshake.
2. `initialize` request and `initialized` notification.
3. `thread/list`, `thread/start`, `thread/read`, `thread/resume`, `thread/name/set`, archive, and delete.
4. `turn/start` with `clientUserMessageId` for durable mobile correlation.
5. Ordered projection of `turn/*`, `item/*`, and `item/agentMessage/delta` notifications into the existing reducer contract.

Raw app-server thread IDs remain internal `ThreadKey` values. Commands, tool arguments, raw results, debug JSON, endpoints, and stack traces are not rendered.

## Verification

- Authenticated MockWebServer WebSocket fixture: PASS.
- Bearer handshake assertion: PASS.
- Thread create/name, turn submit, user correlation, streaming delta, terminal lifecycle, and history projection: PASS.
- `:app:testStandaloneUnitTest`: PASS.
- `:app:assembleStandalone`: PASS.
- `:app:lintStandalone`: PASS.
- Distributed `:app:compileDebugKotlin`: PASS.
- Standalone APK SHA-256: `9235076aeb76ce0dbaca4e5054e198839f3b2fc18ef9e1a5391d95e09beca592`.
- Installed base APK hash matched the local standalone APK: PASS.
- Local and installed signing certificate SHA-256: `190d4bd967692d845a233995b924b150449f98a66babec481babdebe60266ccb`.
- Installed package/version: `com.cheby.codex.mobile.standalone`, `0.4.5-standalone` (`12`).
- APK permissions are limited to Internet, Termux `RUN_COMMAND`, and the AndroidX private dynamic-receiver permission.
- On-device `/opt/cheby/bin/start-codex-appserver` installation: PASS.
- Runtime install evidence SHA-256: `6e9776ae992337a24e1d61cdc4013d6f2a58d81a565164d93399c89c1ac0f143`.

## Permission boundary

`com.termux.permission.RUN_COMMAND` is a dangerous permission declared by the installed Termux package. The standalone APK requests it but does not open a permission dialog or grant it itself. Until the Founder separately approves that grant, ChebyAgent safely remains disconnected and retries the local authenticated endpoint.

ChebyNode Accessibility is a second, independent permission gate. It is required for actual Android mutations but not for Codex chat, thread history, or read-only phone status.
