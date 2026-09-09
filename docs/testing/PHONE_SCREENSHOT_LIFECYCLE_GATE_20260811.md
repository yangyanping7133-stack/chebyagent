# Embedded Phone Screenshot Lifecycle Gate — 2026-08-11

## Decision

The Android 10 screenshot path for the single-APK appliance is **PASS**.

## Failure found

After PhoneBridge opened Yandex Maps, `android_capture_screenshot` could not establish the Android
10 MediaProjection permission. The embedded Node searched the launcher for its internal product
name, while the installed appliance icon uses the host application label `ChebyCodex`. After the
icon match was corrected, the target launch still stopped at the host conversation Activity because
only the legacy Node Activity dispatched pending foreground launches.

## Fix

- Launcher fallback now matches the exact installed application label and the resolved launcher
  package.
- The appliance installs a narrow Activity lifecycle bridge that dispatches only an already-pending
  one-shot Node launch. No scenario or app-specific behavior was added to the UI or Gateway.

## Verification

- `:phoneNode:testDebugUnitTest`: PASS.
- `:phoneNode:testStandaloneUnitTest`: PASS.
- `:embedded:phoneNode:testDebugUnitTest`: no test sources; compilation PASS.
- `:appliance:assembleDebug`: PASS.
- Standalone appliance static gate: PASS.
- APK upgrade install with data preservation: PASS.
- Accessibility service remained enabled and bound after the upgrade.
- PhoneBridge MCP opened package `ru.yandex.yandexmaps` and verified it in the foreground.
- The first screenshot request established Android 10 MediaProjection through the exact own-app
  consent flow.
- A second open-and-capture cycle returned a 720 x 1604 JPEG of the live Yandex Maps screen.
- The private screenshot artifact was owner-only and its SHA-256 is
  `7a778c5ce2266fea7f1d8dd96c463cbfb48bee9fb24f14d62fe031fa0c173108`.

No login state, bearer token, PhoneBridge token, internal Thread id, or raw tool payload is included
in this evidence record.
