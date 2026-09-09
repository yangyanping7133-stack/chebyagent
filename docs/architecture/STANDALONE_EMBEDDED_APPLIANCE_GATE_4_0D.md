# Standalone Embedded Appliance Gate 4.0D

Date: 2026-08-09
Status: APPLICATION GATE PASS; UNATTENDED NETWORK RECOVERY PARTIAL

This gate extends the 4.0B checkpoint on the Huawei JUY-AL00 running Android 10. It proves authenticated PhoneBridge MCP use, Android control, screenshot return, a real Yandex Maps nearby-cafe task, device reboot recovery of the embedded appliance, and a real post-reboot Codex turn. It also records one external operating requirement honestly: the separately installed v2rayNG client did not restart its VPN tunnel after the phone reboot, so OpenAI access resumed only after the existing `Turkey-213-SS` profile was started manually. No VPN route rule was changed.

## Installed artifact

- package: `com.termux` (temporary compatibility identity)
- version: `0.5.0-embedded-dev` (`1`)
- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- size: 67,754,922 bytes
- SHA-256: `cbf31b49b0762b712299349fc2c6b5555555a0e3b3256396c8cd06eb0fde173b`
- screenshot-consent fix: `3ae5b6e`

This is still an internal debug artifact, not a public GitHub release candidate.

## Screenshot-consent failure and fix

The first Android 10 PhoneBridge screenshot request reached the system MediaProjection dialog but returned HTTP 500. The audit trail recorded `media_projection_permission_launch` followed by `phonebridge_command_failed`.

The cause was an exact safety-policy mismatch introduced by the one-APK merge. The system dialog named the installed application `ChebyCodex`, while the policy accepted only the legacy literal `ChebyNode`. The fix did not weaken the consent boundary. It now requires all of the following:

- screen and node package are exactly `com.android.systemui`;
- the target is the clickable `android.widget.Button` with id `android:id/button1` and text `允许`;
- the same dialog contains `android:id/button2` and `禁止`;
- the question contains the live application label loaded from this installed APK.

The negative test proves that a `ChebyCodex` dialog is rejected when the expected application label is `ChebyNode`.

Verification passed:

- `:phoneNode:testDebugUnitTest`
- `:phoneNode:testStandaloneUnitTest`
- `:embedded:phoneNode:testDebugUnitTest`
- `:appliance:assembleDebug`
- `git diff --check`

The rebuilt APK was installed with `adb install -r`; the private runtime, sessions, and owner-only `auth.json` were preserved.

## Accessibility and authenticated MCP

The device reported the embedded service as enabled and bound:

- component: `com.termux/com.chebysight.chebyagent.android.AgentAccessibilityService`
- secure accessibility state: `1`
- PhoneBridge status: online, policy-gated, fund protection enabled

Official Codex `0.147.0` started its registered `phonebridge` MCP child and completed both `android_phone_status` and `android_ui_tree`, returning `PHONEBRIDGE_MCP_OK`. A later real UI task also started `codex-code-mode-host` and exercised `android_open_app`, `android_ui_tree`, and `android_capture_screenshot` through the authenticated MCP path.

After the label fix, PhoneBridge screenshot returned HTTP 200 with a 720 x 1604 JPEG. The phone audit recorded, in order:

1. `media_projection_permission_launch`;
2. `media_projection_consent_allowed` for `android:id/button1` under `com.android.systemui`;
3. `capture_screenshot`;
4. `phonebridge_command_completed`.

## Yandex Maps end-to-end result

A new ChebyCodex UI thread submitted this task to the phone-resident Codex runtime:

> Use only the installed Yandex Maps Android app, package ru.yandex.yandexmaps. Find one nearby coffee shop based on the phone's current location. Open its detail card. Use PhoneBridge tools, capture a screenshot, and include the screenshot in your final reply with the cafe name. Do not use any browser or URL.

Observed result:

- foreground package: `ru.yandex.yandexmaps`;
- no browser package or URL was opened;
- selected place: `Секретер`;
- category: Cafe, Coffee shop, Restaurant;
- displayed rating: 5.0;
- displayed distance: 580 m;
- the Yandex detail card was captured at 720 x 1604;
- ChebyCodex rendered the MCP image as a visible `手机截图` card and returned the place name and distance.

Private evidence, deliberately excluded from public source control:

- `artifacts/private/real-device/standalone-4.0b-20260809/yandex-sekreter-detail.jpg`
  - SHA-256: `5f0e9a47776c5ec7bf45836a1ca45d2d67b53231a20ab9a1b84ec3fc327e6b80`
- `artifacts/private/real-device/standalone-4.0b-20260809/chebycodex-yandex-reply.jpg`
  - SHA-256: `96f9500fd4e3b19e2a9b9b1a5b3d5dccacba0a72006864832632d451b4ad5ee3`

No PhoneBridge token, Codex login file, access token, refresh token, VPN credential, or pairing secret is present in the evidence or this document.

## Device reboot recovery

The device boot id changed from `4a0d0002-6a02-4ad2-9c03-2e8afb8cd0ed` to `06c84123-9800-4433-bbce-99b8798b8f95`. After `sys.boot_completed=1`:

- Android user 0 was `RUNNING_UNLOCKED`;
- the accessibility component and enabled state persisted;
- the single APK process restarted automatically;
- exactly one PRoot supervisor and two Node processes returned;
- PhoneBridge health returned `ok=true` with the Android execution device connected;
- the Codex bridge returned `ready` on its loopback readiness endpoint;
- `auth.json` still existed as an owner-only `0600` regular file;
- the prior Yandex thread, result text, and returned image were restored in the UI;
- a new screenshot rebuilt the Android 10 projection permission and completed successfully.

The first post-reboot OpenAI turn did not complete because no VPN process or `tun0` existed. Inspection confirmed that Wi-Fi was healthy but v2rayNG had not restarted. Starting the already configured `Turkey-213-SS` profile restored `tun0` without changing the existing split-routing rules. Re-submitting the fixed-response turn then returned `POSTREBOOTCODEX_OK` in ChebyCodex.

Post-reboot private evidence:

- `artifacts/private/real-device/standalone-4.0b-20260809/chebycodex-post-reboot-codex-ok.jpg`
  - SHA-256: `88395cd6c0eae269180c741e827bda46ecded0c9c7e9219ef8f28765a616f011`

## Remaining delivery work

The embedded application path itself has passed the current 4.0D real-device gate. A phone-only delivery package still needs:

1. a manual, computer-free accessibility-enable walkthrough validated on a clean phone; this development gate used the explicitly authorized `WRITE_SECURE_SETTINGS` grant;
2. a supported decision for VPN startup after reboot: document one manual tap, configure a reliable auto-start path, or embed a Cheby-owned network component later;
3. Cheby-owned package and private-prefix migration away from the temporary `com.termux` identity;
4. release signing, reproducible corresponding-source bundle, third-party notices, checksums, and a phone-only installation guide;
5. a final clean-device install gate using the actual delivery artifact.

The standalone and non-standalone code lines remain intentionally separate. The deferred corrupt-file scenario and short-term 3.6 work are not prerequisites for this checkpoint.
