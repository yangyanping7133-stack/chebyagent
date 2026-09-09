# Standalone Embedded Appliance Gate 4.0B

Date: 2026-08-09
Status: PARTIAL PASS - INSTALL, PROVISION, AUTH, REAL CODEX TURN, AND PROCESS RECOVERY PASS; REBOOT AND DEVICE CONTROL PENDING

This is real-device evidence from Huawei JUY-AL00 on Android 10. It proves the embedded shell, first-run appliance path, real ChatGPT login import, local Codex execution, UI turn delivery, and force-stop recovery. It does not claim accessibility control, authenticated PhoneBridge MCP use, the Yandex Maps end-to-end case, or device reboot recovery.

## Installed artifact

- package: `com.termux` (internal compatibility identity)
- version: `0.5.0-embedded-dev` (`1`)
- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- size: 67,754,922 bytes
- SHA-256: `1159c7a4b2733bebbd82ea7b1b1b80f564f45528cf9624e434db5904ce8e6543`
- source checkpoint: `0000195b0bf5`

The user explicitly authorized discarding the previous Termux data. The old `com.termux`, separate ChebyAgent, and separate ChebyNode packages were removed. Package inspection then confirmed that only the new one-APK `com.termux` appliance is installed for this product chain.

## First-run result

The first launch passed these real-device layers:

1. the embedded Termux bootstrap native library loaded and extracted the private prefix;
2. the in-process Termux service and embedded AgentNode foreground service started under the same Android UID;
3. Termux packages installed `proot-distro 5.5.0`;
4. `debian:13.6-slim` resolved to the expected ARM64 config and rootfs layer, and the cached layer SHA-256 was recomputed before any guest command ran;
5. PRoot entered the Debian guest and installed the required Node, Python, Git, curl, CA, and SSH client packages;
6. the complete official Codex `0.147.0` `aarch64-unknown-linux-musl` package downloaded and passed its archive checksum, package metadata, CLI version, app-server help, and Code Mode Host help checks;
7. the runtime marker `ready-4.0.0-dev1` was committed;
8. the localhost app-server returned `ready` and the local PhoneBridge reported the embedded execution phone online.

The stable process shape after startup is exactly:

- one Android appliance process;
- one PRoot supervisor;
- two Node processes: Codex stdio bridge and PhoneBridge;
- one official Codex child process;
- two required foreground services.

## Failures found and fixed during the gate

Three failures were captured rather than bypassed:

- `4d61a9b`: replaced unsupported `proot-distro --version` with an exact installed-package version check;
- `cb486ce`: adapted to `proot-distro 5.5.0` tag-only image parsing while retaining content pinning through the expected ARM64 config digest, rootfs layer digest, and a local SHA-256 recomputation;
- `0000195`: deduplicated simultaneous application-resume and activity-resume Codex starts. This removed a PRoot race that could leave the background PhoneBridge child in tracing-stop state.

Each fix passed appliance build, lint, static asset-lock, sensitive-file, and diff checks before installation.

## Recovery result

Force-stop removed the entire Android process and all guest children. A cold launcher start restored the saved runtime without reinstalling Debian or Codex. Within 20 seconds:

- app-server: ready;
- execution phone: online;
- PRoot count: 1;
- Node count: 2;
- Codex count: 1;
- AgentNode and Termux runtime services: foreground.

The result was repeated after the race fix. No duplicate runtime was observed. After the valid login was installed, another force-stop and cold launch restored the successful thread and its first completed model response. A second turn in that same restored thread completed successfully. Device reboot recovery remains untested.

## Phone-only setup surfaces

- The product UI shows `请导入 Codex 登录文件` and no terminal, localhost address, token, or debug payload.
- A synthetic, explicitly non-working auth fixture was first selected through Android DocumentsUI. The UI confirmed import, and the private guest file was a regular owner-only `0600` file. The synthetic guest file and every staging copy were then deleted.
- The Founder-authorized Turkey `codex-standalone` login file was imported next, but a real phone-side Codex call proved that its access token had expired and its refresh token was invalid. This is recorded as a rejected source, not a login pass.
- The active Turkey Connector login file was separately validated by a successful model call, then selected through the same Android DocumentsUI path. The guest copy remained owner-only `0600`; the Mac temporary file and phone Download copy were deleted immediately after import. The server source was not modified or deleted.
- Phone-side official Codex `0.147.0` returned `CHEBY_ANDROID_AUTH_OK` in a direct runtime smoke.
- The product UI then created a new Codex thread, submitted a real turn, and rendered the model response. After force-stop and cold launch, the same thread and response were restored and a second turn returned `CHEBYUIRESUMEOK`.
- The in-app menu successfully opened Android's accessibility settings. Accessibility was not enabled because no separate permission-escalation instruction was given.

Private screenshot evidence:

- `artifacts/private/real-device/standalone-4.0b-20260809/chebycodex-4.0b-login-required.png`
- SHA-256: `fa27e9b1fd915f64d4d011243077d9a93b10228c0330cf07fdbe7d58ad899276`
- `artifacts/private/real-device/standalone-4.0b-20260809/chebycodex-4.0b-real-codex-turn.png`
- SHA-256: `a773dd29772c4b891603068e98050c935ce7db12c8a6e2b335377eb07f81e460`
- `artifacts/private/real-device/standalone-4.0b-20260809/chebycodex-4.0b-thread-resume.png`
- SHA-256: `17b389a513ceed15dd51ccb0c44a47c5b1ce9786b38584b76a42563ada0e6037`

## Remaining gates

Gate 4.0C has passed the real phone-selected `auth.json`, thread create/resume, and real Codex turn checks. It still requires Code Mode Host use and authenticated PhoneBridge MCP use.

Gate 4.0D still requires manual accessibility approval under the appliance package, the Yandex Maps nearby-cafe screenshot case, force-stop restoration of that thread and image, and a phone reboot recovery check.

The current APK is still an internal debug artifact. It is not the GitHub delivery candidate until the Cheby-owned package/prefix, release signing, corresponding-source bundle, notices, checksums, phone-only install guide, and all remaining real-device gates are complete.
