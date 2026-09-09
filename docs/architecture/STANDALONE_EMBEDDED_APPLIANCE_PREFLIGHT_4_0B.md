# Standalone Embedded Appliance 4.0B Preflight

Date: 2026-08-08
Status: SUPERSEDED by `STANDALONE_EMBEDDED_APPLIANCE_GATE_4_0B.md`

This checkpoint records what is implemented before the first destructive `com.termux` replacement gate. It does not claim that the embedded shell, Codex, or Yandex Maps chain has run on the target phone.

## Implemented boundary

- One APK and one Android UID contain the ChebyCodex UI, pinned Termux runtime source, embedded ChebyNode services, provisioner, and local PhoneBridge MCP path.
- First launch extracts the pinned Termux bootstrap and provisions pinned PRoot/Debian inputs.
- The provisioner downloads and verifies the complete, official Codex `0.147.0` `aarch64-unknown-linux-musl` package. The Codex binary is not patched.
- Runtime scripts, MCP code, locks, and their generated SHA-256 manifest are packaged as verified assets.
- The UI reports bootstrap, install, login-required, ready, unavailable, and failed states without exposing internal paths or tokens.
- `auth.json` can be selected with Android's document picker and is validated, copied atomically, and stored owner-only inside the private guest home. No credential is compiled into the APK or written to reports.
- The embedded Node starts as a foreground service. The existing boot receiver plus the appliance runtime owner restore an already provisioned Codex chain after process restart, package replacement, user unlock, or phone boot.
- Runtime notifications open the ChebyCodex launcher and do not expose the internal terminal UI or terminal-specific exit controls.

Implementation checkpoints:

- `44862d7` - in-process embedded runtime ownership
- `7918f89` - pinned Codex appliance provisioner
- `1f83228` - phone-side Codex login import
- `0479041` - first-run runtime status
- `15fd2bc` - restart recovery and appliance notification

## Build evidence

- `:embedded:runtimeApi:testDebugUnitTest`: PASS
- `:app:testStandaloneUnitTest`: PASS (325 tests)
- `:phoneNode:testStandaloneUnitTest`: PASS (31 tests)
- `:app:assembleStandalone`: PASS
- `:phoneNode:assembleStandalone`: PASS
- `:appliance:assembleDebug`: PASS
- `:appliance:lintDebug`: PASS
- `tools/standalone/appliance_static_gate.py`: PASS, including locked runtime assets, single launcher, boot receiver, services, native libraries, and no credential-like file
- `git diff --check`: PASS

Development artifact at this preflight checkpoint:

- path: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- version: `0.5.0-embedded-dev` (`1`)
- size: 67,754,922 bytes
- SHA-256: `4116bb9ecc60b9f6bc1c8ff662a146e9132040151df35c10ad5a2d1e01679026`

## Target-phone preflight

Read-only inspection confirmed Huawei JUY-AL00 on Android 10. Under the user's prior explicit authorization, the old separate ChebyAgent and ChebyNode standalone packages were uninstalled; their stable 3.5 APKs and source bundle remain in the frozen private release archive.

The phone still has Termux `0.118.3` installed as `com.termux`. Its signing certificate differs from the development appliance certificate, and its version code is `1002` while the internal appliance uses version code `1`. Android therefore cannot install or update the appliance alongside or over that package.

No Termux private file or credential was read, migrated, or deleted. Installation is stopped until the user separately chooses to preserve/migrate that data or explicitly discard it and uninstall the existing package.

## First real-device gate

After that decision, the gate is:

1. install the single appliance APK directly on the phone;
2. launch ChebyCodex and let bootstrap/provisioning finish on the phone's configured network;
3. import a valid `auth.json` from the phone's document picker;
4. enable the ChebyCodex accessibility service in Android settings;
5. create/resume a Codex thread and verify Code Mode Host plus PhoneBridge MCP;
6. ask Codex to open the installed Yandex Maps app, select a nearby cafe, capture a screenshot, and return it inline;
7. force-stop/relaunch and reboot to verify recovery.

No computer-side setup is part of the eventual user flow. ADB is used only for development evidence in this internal gate.

## Public-delivery boundary

This APK is an internal debug artifact, not the GitHub delivery candidate. Public delivery still requires a Cheby-owned package/prefix rebuild, release signing, corresponding-source and third-party-notice packaging, phone-only install instructions, and a passed 4.0B-4.0D real-device gate.
