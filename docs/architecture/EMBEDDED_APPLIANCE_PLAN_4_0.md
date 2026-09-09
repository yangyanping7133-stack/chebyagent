# Embedded Android Appliance 4.0

Status: implementation plan for the isolated `codex/standalone-4.0-embedded-runtime` branch.

The frozen 3.5 checkpoint remains the rollback baseline:

- tag: `standalone-3.5-stable-20260808`
- commit: `cf9146bd534d2c426a9f152ff26c2cc1798fe267`
- private archive: `artifacts/private/releases/standalone-3.5-stable-20260808/`

This branch must continue to build the old standalone APKs while the appliance APK is incomplete.

## Product boundary

The 4.0 appliance is one installable APK with one launcher and one Android UID. It contains:

- the ChebyCodex Compose conversation UI;
- the Termux process/runtime implementation;
- a PRoot Debian guest;
- the unmodified official Codex CLI binary;
- the ChebyNode accessibility, screenshot, and app-control services.

There is no separately installed ChebyNode APK and no separately installed Termux APK in the final gate. Codex remains an upstream binary; the integration changes only its runtime ownership and startup path.

## Module boundary

Only `:appliance` is an Android application in the appliance artifact:

```text
:appliance                       com.android.application, applicationId com.termux
  -> :chebyUiEmbedded            Cheby UI source adapter
  -> :phoneNodeEmbedded          ChebyNode source adapter
  -> :embeddedRuntimeApi         narrow startup/provisioning contract
  -> :termuxShared               pinned upstream source
  -> :termuxTerminalView         pinned upstream source
  -> :termuxTerminalEmulator     pinned upstream source
```

The existing `:app` and `:phoneNode` application modules remain unchanged as rollback/reference build targets. The embedded adapter libraries compile the same product sources under their original namespaces, so the 4.0 experiment does not force an early rewrite of the validated 3.5 applications.

## Fixed upstream inputs

- Termux app source: `v0.118.3`, commit `5b657c6adf4304e5198951ce815fe0205dcac29c`
- Termux bootstrap release: `2025.03.28-r1+apt-android-7`
- target ABI for the first appliance gate: `arm64-v8a`
- target phone: Huawei JUY-AL00, Android 10 / API 29
- Codex CLI: official `0.147.0` `aarch64-musl`, unchanged
- proot-distro: `5.5.0`
- guest: Debian `13.6`, `aarch64`

The Termux fork is kept below `third_party/termux-app/` with its upstream license and provenance. Product code must talk to it through `:embeddedRuntimeApi`; Termux implementation types must not spread through the Cheby UI or ChebyNode modules.

## Compatibility decisions

### Package and prefix

The first internal appliance uses application ID `com.termux`. This preserves the upstream `/data/data/com.termux/files/usr` prefix and allows the exact pinned bootstrap binaries to run without rebuilding the full Termux package repository.

Consequences:

- the appliance cannot coexist with a separately installed Termux package;
- installing it over an existing Termux signed by another publisher is impossible;
- uninstalling the existing Termux would delete its private data, so that operation is a separate destructive gate and is not part of the build phase;
- a public Cheby package name requires a rebuilt bootstrap and package set in a later phase.

### Android target

The first appliance targets SDK 28 and compiles with SDK 35. The target is intentional: Android 10 blocks direct execution of files written under app-private storage for applications targeting SDK 29 or later, while the pinned Termux/PRoot layout executes from that prefix.

The existing distributed and 3.5 application modules keep target SDK 35. No target downgrade is applied to them.

### Runtime startup

The appliance registers an implementation of `:embeddedRuntimeApi` from its `Application`. `MainActivity` asks the API to prepare the bootstrap before constructing the local gateway. The runtime queues the capability-token start request until bootstrap extraction completes, then starts `TermuxService` directly inside the same app rather than using the exported `RUN_COMMAND` interface.

Only pinned entrypoints are callable. Prompt text or model output never becomes a shell command.

### Provisioning

The implementation is split into two independently testable steps:

1. APK-contained Termux bootstrap extraction and a native shell smoke test.
2. An app-owned, checksum-pinned provisioner for PRoot Debian, Codex, Code Mode Host, Python/Node dependencies, and the existing local PhoneBridge MCP bridge.

The build must not embed login credentials. A later UI step imports `auth.json` through Android's document picker into private storage. Logs, reports, and release archives must never contain its contents.

## Execution gates

### Gate 4.0A: source and build boundary

- pinned upstream source and license are present;
- only the new appliance module has application ID `com.termux`;
- existing `:app:assembleStandalone` and `:phoneNode:assembleStandalone` still pass;
- `:appliance:assembleDebug` produces one APK;
- manifest inspection shows one Cheby launcher, embedded Termux services, and embedded ChebyNode services;
- the APK contains no credential material.

### Gate 4.0B: embedded shell

- install on the target phone after an explicit data-migration/uninstall decision;
- no separate Termux or ChebyNode package is installed;
- first launch extracts bootstrap without a terminal UI;
- an app-internal fixed command proves native shell execution;
- process restart and phone reboot recover the runtime owner.

### Gate 4.0C: Codex local chain

- the provisioner installs the pinned Debian and official Codex CLI;
- imported `auth.json` is readable only from private storage;
- Cheby UI starts and reconnects to Codex app-server on loopback;
- thread creation, resume, Code Mode Host, and MCP pass;
- no raw localhost address, token, internal path, or debug JSON enters the UI.

### Gate 4.0D: one-APK phone execution

- embedded ChebyNode starts as a foreground service;
- its accessibility service is enabled under the appliance package;
- Codex uses the installed Yandex Maps app, selects a nearby cafe, captures a screenshot, and returns it inline;
- force-stop/relaunch restores the thread and screenshot;
- the final artifact, source/provenance bundle, checksums, install guide, and 3.5 rollback guide are archived.

## Stop conditions

Stop the current slice and preserve evidence when any one step approaches one hour, when the old 3.5 build regresses, or before deleting/replacing the existing `com.termux` package or its private data without explicit approval.

Public distribution is a separate gate. It requires a Cheby-owned package/prefix strategy and a compliant corresponding-source and third-party-notice delivery for the embedded GPL components.
