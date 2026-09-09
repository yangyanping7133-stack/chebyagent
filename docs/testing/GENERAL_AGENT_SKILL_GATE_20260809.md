# General Agent and Bundled Skill Gate — 2026-08-09

## Decision

General Agent rich-message foundation and first bundled scenario Skill: **PASS**.

The twelve Russia Starter Pack cases remain the scenario convergence suite. This gate does not
claim that the remaining eleven cases are implemented or passed.

## Candidate

- package: `com.termux`
- version: `0.6.0-embedded-offline-dev` (`versionCode 2`)
- embedded runtime marker: `ready-4.1.0-dev2`
- APK: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- APK size: 272 MiB
- APK SHA-256: `d8e45a68abf321034b5ffc03cb468dc2a3ae1c8af1b2876612e34c138c5d89de`
- target device: Huawei JUY-AL00, Android 10, ARM64

## Layer boundary

Production Android source was scanned for scenario vocabulary after removing the old
`menu/restaurant` migration aliases and the menu-specific camera filename. The general runtime now
contains no Russia, Yandex, menu, dining, waiter, or ruble behavior. Scenario output remains free to
contain those words because it is produced by a Skill.

The generic final-turn `TaskResponse` supports:

- text/Markdown;
- reference gallery with bounded image lookup and explicit uncertainty labels;
- candidate comparison with local selection;
- grouped selectable collections with local quantity, review, total, and presentation state;
- source-language/Chinese presentation cards.

The schema is applied to every local Codex turn and constrains only the final answer. Codex tool and
PhoneBridge use during the turn remains available.

## First bundled Skill

`skills/russia-menu-assistant` passed the Skill validator and was embedded in the one APK. After
the exact APK was installed with data preservation, offline provisioning reached
`ready-4.1.0-dev2`. The installed guest files were present at:

- `/root/.codex/skills/russia-menu-assistant/SKILL.md`;
- `/root/.codex/skills/russia-menu-assistant/agents/openai.yaml`.

The phone-side and repository `SKILL.md` SHA-256 values matched:
`7375232fb9ff02c3ee4c0f9f039d11cd03d28eaf25febbbe483f00c90e066ab3`.

No runtime, Skill, Codex, Debian, Node.js, or Python download was required by this upgrade.

## Automated checks

- `:app:testDebugUnitTest`: PASS;
- `:app:compileGateAndroidTestKotlin`: PASS;
- `:appliance:lintDebug`: PASS;
- `:appliance:assembleDebug`: PASS;
- standalone appliance static gate: PASS, including bundled-runtime hashes and required Skill
  assets;
- `RichContentSemanticsInstrumentedTest` on JUY-AL00: **7/7 PASS**.

The real-device Compose set covers safe Markdown/literal text, media fallback accessibility,
reference-image fallback, comparison selection, grouped selection and quantity, full-screen
presentation, and bilingual handoff.

## Real local Codex turn

The appliance recovered as the foreground application and displayed `本机 Codex 已连接`. A fresh
thread sent `Reply_with_one_word_ready`; the local Codex chain returned `ready`, which rendered as
normal text rather than raw JSON.

An older persisted menu message still contains the pre-TaskResponse raw JSON format. It was not
used as evidence for this gate and was not removed because clearing all application data would also
remove the currently usable login state. New turns use the generic protocol.

## Twelve-case convergence rule

The full mapping is recorded in `docs/architecture/GENERAL_AGENT_SKILL_BOUNDARY.md`. A failure in
one of the twelve cases may produce only:

1. a reusable, business-agnostic Agent/UI capability; or
2. a fix to the owning scenario Skill.

Adding app or scenario knowledge to the renderer, gateway, or conversation core is a gate failure,
even if the individual use case appears to work.

## Remaining product work

- Use A1 as the next external-app anchor and create the Yandex Go ride Skill from the observed app
  workflow.
- Then run A2/A3 and C1 to separate reusable Yandex Maps tool knowledge from route/local-search
  scenario rules.
- Continue through the remaining cases, adding general primitives only when at least one case
  proves the need.
- Replace debug/application signing before a public delivery candidate.
- Keep long-term keepalive, corrupt-state recovery, public redistribution notices, and interactive
  ChatGPT login outside this gate.
