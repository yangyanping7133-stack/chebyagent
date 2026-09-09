# Menu multi-page low-effort gate — 2026-08-09

## Outcome

PASS for the standalone Android appliance development checkpoint.

The user flow is now: attach 1–10 menu pages, type `点菜`, and receive one
general rich `selection` response. Menu behavior remains in the
`russia-menu-assistant` Skill; the Agent and renderer only implement generic
attachments and rich-message behavior.

## Changes under test

- Raised one-turn local and remote image input from 4 to 10 pages.
- Raised local structured selection capacity from 64 to 256 items and 32 groups.
- Preserved all original user pages in an ordered, horizontally lazy source strip.
- Bounded thumbnail decoding so ten originals are not decoded at full size together.
- Kept selection cards text-only for both text-only menus and menus with source photos.
- Made `点菜` sufficient to trigger the complete multi-page Skill workflow.
- Passed `approvalPolicy=never`, `sandboxPolicy.type=dangerFullAccess`, and
  `effort=low` on every app-server `turn/start` request.

## Real-device cases

Device: Huawei JUY-AL00, Android 10, USB-connected only for installation and test
orchestration. The product flow itself used the Android system document picker,
the standalone app, the embedded official Codex CLI 0.147.0, and the embedded
PhoneBridge/Node runtime.

### Eight-page text-only menu

- Source: Bering banquet menu PDF, rendered to eight uploaded JPEG pages.
- User text: exactly `点菜`.
- Attachment UI ready: approximately 5 seconds, including automation inspection.
- Codex task: 2026-08-09T08:13:00.266Z to 08:15:21.263Z, 141 seconds.
- Result: 6 groups and 81 selectable items.
- Rendering: no item images and no image placeholders.

### Ten-page menu with source dish photos

- Source: Garnets Cafe main menu PDF, first ten pages rendered to uploaded JPEG pages.
- User text: exactly `点菜`.
- Attachment UI ready: approximately 5 seconds, including automation inspection.
- Codex task: 2026-08-09T08:02:12.115Z to 08:04:23.216Z, 131 seconds.
- Result: page 1 correctly treated as an introduction; pages 2–10 produced 5 groups
  and 27 selectable items.
- Rendering: selection cards stayed text-only; the ten uploaded pages remained the
  only images and were available unchanged in the ordered source strip.

### Runtime evidence

- Confirmed turn context: `gpt-5.6-sol`, effort `low`, approval `never`, sandbox
  `danger-full-access`, permission profile disabled.
- Sampled combined `com.termux`, Codex, and Node RSS peaked at approximately 389 MB.
- Before the turn-level sandbox fix, Skill reads retried three times and produced
  failed tool cards. After the fix, the Skill read succeeded once.

## Automated gates

- Android unit tests: 337 passed, 0 failed, 0 skipped.
- Selected real-device instrumentation tests: 17 passed, including the ten-page
  lazy source strip, first/last source preview, attachment reader boundaries,
  activity recreation, and accessibility labels.
- Skill quick validation: PASS.
- Standalone APK static gate: PASS for all checks.
- Python gateway files: syntax compilation PASS.
- Full Python local-image gateway tests were not executed because the repository
  locks Pillow 12.3.0 and that version was unavailable from the configured package
  source. This does not gate the local standalone appliance path.

## Artifact identity

- APK: `ChebyCodex-0.6.0-menu-multipage-low-dev4.apk`
- Application id: `com.termux`
- Version name: `0.6.0-embedded-offline-dev`
- APK SHA-256: `4eea20cb15e9c44f76f3baabbc4d919eed8976306b3496603843200bf11cb617`
- Runtime marker: `4.1.0-dev4`

This is a private development checkpoint. The APK still uses the development
signing path and is not the final public GitHub delivery artifact.
