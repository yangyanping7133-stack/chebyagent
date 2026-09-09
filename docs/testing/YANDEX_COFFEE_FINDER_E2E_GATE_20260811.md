# Yandex Maps Coffee Finder End-to-End Gate

Status: PASS

Date: 2026-08-11

Target: Huawei JUY-AL00 standalone one-APK appliance

## Reported failure

The natural prompt below previously produced a chat answer that asked the user to provide an
address or Yandex Maps link. No Android tool was called and Yandex Maps was not opened.

`你可以打开yandex map，找一下附近的咖啡厅吗`

The phone Codex runtime, local PhoneBridge MCP, Accessibility node, VPN, and Yandex Maps app were
independently healthy. The missing boundary was a scenario Skill that made this natural request an
actionable workflow.

## Implemented scope

- Added the bundled `yandex-maps-coffee-finder` Skill.
- Kept the Android UI and final `comparison` TaskResponse business-agnostic.
- Kept the Gateway as a transport and the Node as atomic Android actions.
- Restricted the workflow to the installed `ru.yandex.yandexmaps` app and the local `phonebridge`
  MCP server.
- Accepted only results whose visible Yandex category explicitly contained `Coffee shop` or
  `Cafe`; a result marked only `Confectionary` was excluded.
- Stopped before route start, call, reservation, ordering, or payment.
- Bundled and offline-provisioned the Skill in appliance runtime `4.1.0-dev7`.

## Static and build gates

- Skill structure validation: PASS.
- `:app:testStandaloneUnitTest`: PASS.
- `:appliance:assembleDebug`: PASS.
- Appliance static gate: PASS, including offline runtime, locked assets, sensitive-file scan,
  required services, single launcher, and package ownership.
- Upgrade install with retained app data: PASS.
- Runtime provisioning marker and installed Skill content: PASS.

## Natural-prompt phone gate

The exact reported prompt was submitted in a fresh ChebyCodex conversation with no hidden workflow
prompt. The final run:

- invoked only the local PhoneBridge Android provider;
- opened the installed Yandex Maps app;
- searched `Coffee shop` using the phone's current Yandex location;
- read multiple result pages;
- excluded the visible `Confectionary`-only result;
- returned a generic five-candidate comparison;
- left navigation unstarted;
- completed in approximately 1 minute 48 seconds.

The final candidates and visible evidence were:

| Candidate | Rating | Open until | Drive time | Visible coffee category |
| --- | ---: | ---: | ---: | --- |
| Секретер | 5.0 (733) | 22:00 | 3 min | Cafe, Coffee shop |
| Kos. Place | 5.0 (259) | 21:00 | 3 min | Coffee shop |
| Avcd | 4.7 (916) | 21:05 | 3 min | Cafe, Coffee shop |
| Communa | 4.9 (453) | 21:00 | 5 min | Cafe, Coffee shop |
| British Bro Coffee | 5.0 (1317) | 22:30 | 7 min | Coffee shop, Cafe |

Every final fact above was rechecked against the captured Yandex Maps result cards. Clipped category
text was marked uncertain in the final comparison rather than completed from assumptions.

## Evidence

Private real-device evidence is archived under
`artifacts/private/evidence/yandex-coffee-finder-e2e-20260811/` with SHA-256 checksums. It contains
two Yandex result screenshots, the ChebyCodex summary card, the expanded comparison card, and the
APK static-gate report. No authentication material or internal conversation identifier is included.
