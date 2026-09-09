# Standalone worktree baseline

## Purpose

Develop and validate the one-phone ChebyCodex mode without changing the reviewed distributed delivery path.

## Frozen distributed baseline

- Source branch: `codex/fixed-ip-relay-27461`
- Source commit: `7afa658d99ba468bb6fd48ac3d82dfd4e73a82e9`
- Immutable local tag: `distributed-go-20260808`
- Source worktree: `ChebyCodex-Mobile`

The source worktree remains the reference for the existing Relay, Connector, Codex Runtime, and remote PhoneBridge chain.

## Standalone development boundary

- Branch: `codex/standalone-agent-phone`
- Worktree: `ChebyCodex-Mobile-standalone`
- ChebyCodex application ID: `com.cheby.codex.mobile.standalone`
- ChebyNode application ID: `com.chebysight.chebyagent.phonenode.standalone`

The distinct Android application IDs create separate app UIDs and therefore separate app-private files, preferences, databases, and Android Keystore access. The standalone APKs can be installed beside the reviewed distributed APKs without upgrading or overwriting them.

## Current phase boundary

The isolated Android build identity remains the standalone development boundary. Phase 3.4 has now proved the complete real-device chain:

`local UI -> authenticated local bridge -> Codex stdio app-server -> MCP -> local ChebyNode -> real app action -> verified result`

See [Standalone One-Phone End-to-End Gate 3.4](STANDALONE_ONE_PHONE_E2E_GATE_3_4.md).

The remaining boundary is productization: runtime files still need to ship as signed APK assets, the temporary copied ChatGPT login needs a real enrollment and lifecycle, and reboot, battery, long-duration keepalive, upgrade rollback, and corrupted-state recovery still need dedicated gates.

The distributed build types and their existing package identities remain unchanged.
