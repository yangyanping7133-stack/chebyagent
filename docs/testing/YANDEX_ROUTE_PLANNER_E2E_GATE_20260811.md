# Yandex Maps Route Planner E2E Gate — 2026-08-11

## Outcome

PASS with one recovered delivery fault. A short Chinese prompt caused the standalone phone to use
the installed Yandex Maps app, plan a live route from the phone's current location to Winter
Palace, compare visible travel modes, and stop before starting navigation.

The Codex task produced a valid generic comparison response in 2 minutes 21 seconds. Its first
completion notification was not delivered to the live Gateway, leaving a streaming placeholder.
A non-destructive app-process restart recovered the complete persisted response. Version 0.6.3
adds a generic stalled-turn reconnect and reloads a still-streaming selected conversation after a
real offline-to-online transition.

## Device and build

- Primary device: Huawei JUY-AL00, Android/HarmonyOS compatibility mode.
- Existing app under test: Yandex Maps `ru.yandex.yandexmaps`, version 30.2.1.
- No additional scenario app was required or installed.
- ChebyCodex APK: `0.6.3-route-recovery-dev`, version code 5.
- Embedded runtime: `4.1.0-dev8`.
- Codex reasoning effort: `xhigh`; service tier: priority/fast.

## User flow

Prompt submitted at 20:02:12 CST:

> 带我从当前位置去冬宫

Observed behavior:

1. Codex invoked only the registered local PhoneBridge MCP server.
2. It opened the installed Yandex Maps app and recovered from the prior coffee-results screen.
3. It handled the Android Yandex Go / Yandex Maps chooser and selected Maps.
4. It planned `Current location` to `Winter Palace`.
5. It inspected live route modes and left the app before the `Go` / `Let's go` action.
6. It returned a general `comparison` TaskResponse rather than route-specific UI code.

Observed route facts at test time:

- Driving: 24 minutes, 6 km; a 23-minute, 7 km alternative was also visible.
- Public transport: 44 minutes; the final result reported 26 minutes walking and a 130₽
  Podorozhnik fare from the visible route details.
- Walking: 1 hour 3 minutes, 5 km, approximately 7049 steps.

These are time-sensitive observations from the test run, not durable route guarantees.

## Delivery-fault evidence and fix

- The persisted Codex session wrote the final structured result at 20:04:33 CST.
- The live conversation retained an empty streaming assistant placeholder and did not receive the
  terminal payload.
- Restarting the app process did not clear data and restored the final comparison from the Codex
  session.
- The generic fix reconnects a local app-server connection only after an active turn has emitted no
  progress for 6 minutes.
- After an actual offline-to-online transition, the selected conversation is reloaded only when it
  still contains a streaming assistant message. Normal reconnect subscriptions are not duplicated.

## Verification

- Skill structure validation passed for all three bundled Skills.
- `:app:testStandaloneUnitTest`: 343 tests passed.
- Added regression coverage for a silent active turn and a missed terminal conversation event.
- `:appliance:assembleDebug`: passed.
- Appliance static gate: PASS, including offline runtime assets, no sensitive files, launcher,
  services, receiver, target SDK, and locked assets.
- APK SHA-256: `2f8d4956ee3146bcf858c5e3e97eb8b51a6e3b16a7765e1ba3bc3382ec883ba6`.
- Coverage install to version code 5 passed and preserved the route conversation and comparison.

## Evidence

Private evidence is archived under:

`artifacts/private/releases/standalone-route-planner-dev-20260811/`

The archive contains the exact APK, the final result card, the expanded comparison dialog, static
gate output, and SHA-256 checksums. It intentionally contains no authentication material.
