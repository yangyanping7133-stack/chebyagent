# Standalone Codex Runtime Gate 3.1

## Verdict

PASS for the narrow Phase 3.1 gate: the official Codex Runtime runs locally on the target Android phone, accepts an existing ChatGPT login state, completes a real OpenAI model call, and initializes app-server over stdio.

This is runtime feasibility evidence, not the standalone product gate.

## Tested target

- Date: 2026-08-08
- Device: Huawei JUY-AL00
- Android: 10 / API 29
- ABI: `arm64-v8a`
- Terminal host: Termux `0.118.3` from F-Droid
- Linux layer: `proot-distro 5.5.0`
- Distribution: Debian `13.6`, `aarch64`
- Codex: official `codex-cli 0.147.0` aarch64 musl release
- Codex release archive SHA-256: `eb677c80f666b1ab8b4b1d083b66e8d614b1281d960bb6f9fd8ca98f58b38b90`

The minimal Debian rootfs did not initially contain `/etc/ssl/certs/ca-certificates.crt`. Installing Debian `ca-certificates` fixed the observed `UnknownIssuer` failures for both WebSocket and HTTPS Responses transports.

## Authentication boundary

Phase 3.1 temporarily reused the active ChebyCodex Connector ChatGPT login state from the Turkey server. Only `auth.json` was copied; server configuration, sessions, MCP data, private keys, and unrelated credentials were not copied.

The credential moved through a short-lived Mac loopback HTTP listener carried to the phone by `adb reverse`. The listener, reverse mapping, and Mac temporary credential copy were removed immediately after transfer. Token values were not printed or recorded. Phone-side verification returned:

```text
Logged in using ChatGPT
```

This temporary seeding method is acceptable for the feasibility gate. It is not the final standalone enrollment or credential-lifecycle design.

## Real Codex execution

The phone ran a non-interactive smoke with the inner sandbox intentionally disabled for this empty spare-phone experiment:

```text
OpenAI Codex v0.147.0
model: gpt-5.6-sol
provider: openai
approval: never
sandbox: danger-full-access
codex
CHEBY_CODEX_ANDROID_OK
```

The result proves the local binary, reused ChatGPT login, phone VPN path, TLS trust store, Responses transport fallback, and model response path together.

Codex also reported that `codex-code-mode-host` was not installed. The core CLI request still completed, but Code Mode Host is not part of the 3.1 pass claim.

## App-server initialization

The phone started:

```text
codex app-server --listen stdio://
```

A JSONL client sent `initialize` followed by `initialized`. App-server returned:

```json
{
  "id": 0,
  "result": {
    "userAgent": "cheby_mobile_31/0.147.0 (Debian 13.0.0; aarch64) xterm-256color (cheby_mobile_31; 0.1.0)",
    "codexHome": "/root/.codex",
    "platformFamily": "unix",
    "platformOs": "linux"
  }
}
```

App-server warned that system `bubblewrap` was not on `PATH` and selected its bundled fallback. Inner-sandbox compatibility remains deliberately outside this gate.

## Device changes

- Huawei Pure Mode enhanced protection was already off.
- Huawei Developer Options `Monitor ADB-installed apps` was turned off so the verified F-Droid APK could be installed non-interactively.
- Termux storage permission was granted for the one-time binary transfer.
- Temporary APK, Codex release, copied binary, and JSONL probe artifacts were removed after installation.

## Not yet proved

- ChebyAgent UI launching and supervising the local app-server
- local IPC and approval routing between ChebyAgent, Codex, and ChebyNode
- ChebyNode action execution in the same phone architecture
- Android foreground-service, battery, reboot, and crash recovery behavior
- Code Mode Host availability
- a reviewed inner sandbox policy
- standalone credential refresh, rotation, logout, and recovery
- the end-to-end chain `local UI -> local Codex runtime -> local ChebyNode -> real app action -> verified result`

The next phase should use this installed runtime as the fixed execution base and prove the narrow local IPC/supervision contract before merging application modules.
