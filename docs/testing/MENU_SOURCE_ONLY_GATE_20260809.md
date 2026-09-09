# Menu source-only presentation gate — 2026-08-09

## Decision

The revised menu flow is **PASS** on Huawei JUY-AL00.

The product rule for this gate is:

- never search for, retrieve, or generate supplemental dish images;
- render a text-only source as a text-only selectable menu without an image placeholder;
- preserve the exact uploaded source image when the source contains text and pictures;
- translate visible text, allow local quantity selection, and produce a waiter-facing handoff card.

Small OCR price deviations are a known, non-blocking limitation for this gate. The Skill re-reads
price digits and marks conflicts uncertain. A user correction remains authoritative.

## Candidate

- package: `com.termux`;
- version: `0.6.0-embedded-offline-dev` (`versionCode 2`);
- embedded runtime marker: `ready-4.1.0-dev3`;
- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`;
- APK bytes: `285712238`;
- APK SHA-256: `720215372ea73ed1b182dc411b936b98cdc68f144d59e5e1ef2a82c4a75d1f00`;
- bundled menu Skill SHA-256: `7a21130d09a42b2baa08c115fe7d84e048ff6aa85580f3898e88dd0e1db39f0a`.

## Implementation boundary

The general Agent/UI now preserves trusted local image input as an opaque process-private media
asset. User messages render that asset directly and allow full-screen inspection. The display model
does not expose a filesystem path or data URI.

The generic selection renderer omits its image region when `imageKind` is `none` and `imageQuery`
is empty. The menu-specific no-supplement rule remains in `skills/russia-menu-assistant`; no menu,
restaurant, or Russia behavior was added to the renderer.

Original input bytes are bounded and process-private. If the Android process dies, a historical
source image currently degrades to the readable `原始图片暂不可用` fallback; durable encrypted
source-media recovery is separate future work.

## Real-device run

Input source SHA-256:
`6a0d978701385d0783df6c2581314f09c71ba48b417136f8aaf0d60cb52e42fc`.

The installed appliance completed a real local Codex turn and produced:

- the unchanged Russian menu image inside the user message;
- a translated menu with 9 categories and 49 selectable items;
- pure-text item cards with no supplemental image and no image placeholder;
- two selected soups, quantity 2, total 1680;
- a waiter-facing presentation card with Russian names, quantities, prices, total, and a Russian
  safety note;
- a return-to-edit action that retained both selections and the total.

The evidence archive is local-only under
`artifacts/private/evidence/general-agent-skill-c2-source-only-20260809/rerun-pass/`:

- `02-turn-started.png`: unchanged source image rendered with the user request;
- `09-corrected-menu-text-only.png`: translated text-only menu;
- `10-selected-two-total.png`: two selected items and total 1680;
- `11-russian-handoff-card.png`: waiter-facing handoff card;
- `12-return-edit-state.png`: selection state retained after returning to edit.

## Automated gates

- `:app:testDebugUnitTest`: 335 tests, 0 failures, 0 errors;
- `:appliance:lintDebug`: PASS;
- `:appliance:assembleDebug`: PASS;
- standalone appliance static gate: PASS, including locked bundled runtime, single launcher,
  required services, boot receiver, native libraries, offline provisioning, and no sensitive file;
- installed `base.apk` SHA-256 equals the built candidate SHA-256, and the phone reports
  `ready-4.1.0-dev3`;
- `RichContentSemanticsInstrumentedTest` on JUY-AL00: 8/8 PASS, including exact user-original
  media rendering and full-screen presentation;
- `git diff --check`: PASS.

## Scope

This gate proves the revised menu image policy and interaction flow. It does not claim durable
source-image restoration after process death, exact OCR for every price, or completion of the
remaining Russia Starter Pack scenarios.
