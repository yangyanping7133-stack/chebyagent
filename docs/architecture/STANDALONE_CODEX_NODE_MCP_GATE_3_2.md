# Standalone Codex to ChebyNode MCP Gate 3.2

> Historical checkpoint. The explicit permission gate and real Android mutation were completed in [Standalone One-Phone End-to-End Gate 3.4](STANDALONE_ONE_PHONE_E2E_GATE_3_4.md).

Status: PASS for runtime, local transport, MCP discovery, and model-driven status.

Real Android mutations remain gated on the standalone ChebyNode Accessibility service. This gate does not grant or change that permission.

## Frozen topology

```text
Codex CLI 0.147.0
  -> stdio MCP adapter: local_mcp.py
  -> authenticated PhoneBridge controller: 127.0.0.1:3437
  -> PhoneBridge device socket: 127.0.0.1:3448
  -> com.chebysight.chebyagent.phonenode.standalone
  -> Android Accessibility executor
```

The MCP process is a short-lived child of Codex. PhoneBridge and ChebyNode are the resident runtime components. The distributed Relay path is not used by this standalone chain.

## Runtime evidence

- Device Linux environment: Debian 13.6 aarch64 under Termux.
- Codex CLI: `0.147.0`, temporarily authenticated with the user-approved existing ChatGPT login.
- Python: `3.13.5`.
- Node.js: `20.19.2`.
- Code Mode Host binary SHA-256: `c8fd26e2ddb0243d79d7c3dfa8bcd47b6a30b14695083790fc51884e82e8ebc2`.
- Official release asset digest: `dfd4ff98ea4db30ed078af9c31b6f86e3da4836d0573aa87e225e5a5b54d3c7c`.
- Codex created, read, and deleted an exact local proof file through a real tool call: PASS.

The controller bearer token is generated on-device, stored with mode `0600`, and is never committed or printed by the gate scripts.

## ChebyNode evidence

- Package: `com.chebysight.chebyagent.phonenode.standalone`.
- Version: `0.4.11-phonebridge-standalone` (`37`).
- APK SHA-256: `c2686d2f3d7353ef69364c68a00ae4349d53937f59fe0cbbce159d562d37af4f`.
- Signing certificate SHA-256: `190d4bd967692d845a233995b924b150449f98a66babec481babdebe60266ccb`.
- Installed base APK hash and signing certificate matched the local build: PASS.
- Foreground `AgentNodeService` was running: PASS.
- PhoneBridge reported the exact standalone package online: PASS.

The legacy distributed packages were uninstalled after the standalone identity was verified:

- `com.cheby.codex.mobile`
- `com.chebysight.chebyagent.phonenode`

The standalone Node, Termux/Codex runtime, and VPN remain installed.

## MCP evidence

- MCP adapter source SHA-256: `ff7ada39551ba3837c3ca47d8fe8bf617641cea07c1e762cce3471cb3dc5e3a3`.
- PhoneBridge source SHA-256: `562f66266da07844255ccb9c0fcf9346c62ab00807a58c8b26be34bd271af9f9`.
- Direct MCP initialization and `android_phone_status`: PASS.
- Codex model-selected `phonebridge/android_phone_status`: PASS.
- The returned package was the standalone Node and `phone_online` was true: PASS.

Reusable device scripts are under `tools/standalone/`. Gate logs are retained outside the repository in the local evidence directory; transfer copies on shared phone storage are removed after use.

## Remaining gate

`accessibility_enabled=false` and `accessibility_bound=false` were observed. `openApp`, tap, text input, and other Android actions intentionally fail closed until the standalone Node Accessibility service is explicitly enabled. Enabling it is a separate permission-bearing action, not part of this checkpoint.
