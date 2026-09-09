# Standalone Embedded Appliance Gate 4.0A

Status: PASS for the source, build, and static single-APK boundary. No device install, bootstrap extraction, or Codex execution is claimed by this checkpoint.

## Artifact

- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- application ID: `com.termux`
- version: `0.5.0-embedded-dev` (`1`)
- minimum SDK: 28
- target SDK: 28
- ABI: `arm64-v8a`
- APK size: 67,754,922 bytes (approximately 64.6 MiB)
- APK SHA-256: `1159c7a4b2733bebbd82ea7b1b1b80f564f45528cf9624e434db5904ce8e6543`

The application ID is an internal compatibility choice, not the public Cheby identity. It preserves the pinned Termux private prefix for the first runtime gate and means this APK cannot coexist with a separately signed `com.termux` installation.

## Static boundary

`tools/standalone/appliance_static_gate.py` passed all checks:

- `com.cheby.codex.mobile.MainActivity` is the only actual launcher activity;
- `com.termux.app.ChebyApplianceApplication` owns the Android application lifecycle and installs the narrow embedded-runtime adapter;
- `TermuxService` and the non-exported `RunCommandService` are present;
- embedded `AgentNodeService` and `AgentAccessibilityService` are present;
- the embedded `BootStartupReceiver` is present for post-provision restart recovery;
- the APK contains `lib/arm64-v8a/libtermux.so`;
- the APK contains `lib/arm64-v8a/libtermux-bootstrap.so`;
- no credential-like filename is packaged.

The merged manifest contains one package/UID and the Cheby launcher only. The Termux terminal activity is compiled for runtime compatibility but is not exported and has no launcher filter. The old ChebyNode activity is compiled as source compatibility only and is not declared.

## Pinned bootstrap and native build

- upstream Termux source: `v0.118.3`, commit `5b657c6adf4304e5198951ce815fe0205dcac29c`
- bootstrap release: `2025.03.28-r1+apt-android-7`
- `bootstrap-aarch64.zip` SHA-256: `c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb`
- native host toolchain: NDK `27.2.12479018`

NDK 22.1 from upstream rejects the Apple Silicon build host. NDK 27.2 produced stripped ARM aarch64 ELF libraries successfully. This is only build evidence; loading both libraries and extracting the bootstrap on JUY-AL00 is Gate 4.0B.

## Regression evidence

- `:appliance:assembleDebug`: PASS
- `:appliance:lintDebug`: PASS
- `:app:testStandaloneUnitTest`: PASS, 325 tests
- `:phoneNode:testStandaloneUnitTest`: PASS, 31 tests
- `:app:assembleStandalone`: PASS
- `:phoneNode:assembleStandalone`: PASS
- `git diff --check`: PASS

This confirms that introducing the appliance adapters did not break the frozen 3.5 build targets.

## Remaining boundary

Gate 4.0B must add the app-internal bootstrap/startup contract and prove it on the real phone. Before installation, the existing `com.termux` data must be migrated or deliberately discarded under a separately confirmed destructive operation. Until then, the phone is intentionally unchanged.
