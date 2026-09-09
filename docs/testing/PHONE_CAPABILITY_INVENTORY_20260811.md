# Phone Capability Inventory — 2026-08-11

## Outcome

The single-APK execution substrate is usable. The main product gap is scenario coverage in Skills,
not Android process permission or PhoneBridge availability.

## Device cleanup

Removed four packages that were confirmed to be obsolete or unrelated on the dedicated test phone:

- UC Browser;
- the old standalone ChebyCodex gate APK;
- its instrumentation test package;
- the old Tempo experiment.

Retained only the current ChebyCodex appliance, v2rayNG, Open Camera, RuStore, Yandex Go, and Yandex
Maps. Open Camera remains required because it is the phone's only installed handler for the Android
image-capture intent. RuStore remains the phone-local installation and update path for reviewed
Russian apps.

## General Agent substrate

| Capability | Current evidence | Status |
| --- | --- | --- |
| Embedded Codex runtime | CLI 0.147.0 and local app-server processes running | PASS |
| Reasoning and speed defaults | `xhigh` and Fast configured; appliance turns override to xhigh/priority | PASS |
| PhoneBridge MCP | registered, enabled, and exposes twelve Android tools | PASS |
| Embedded Node | phone online, accessibility enabled and bound | PASS |
| Open installed app | Yandex Maps foreground package verified through MCP | PASS |
| Read UI tree | works generally; Yandex Maps home can expose an empty accessibility tree | PARTIAL |
| Screenshot observation | 720 x 1604 JPEG captured after Android 10 consent repair | PASS |
| Camera intent | Open Camera is the sole verified handler | PASS |
| Online model turn | OpenAI returned region 403 while the user-managed VPN was off | BLOCKED: NETWORK |

## Scenario coverage

| Cases | Current state | Owning next change |
| --- | --- | --- |
| A1 taxi | Yandex Go installed; no ride Skill | Yandex Go ride Skill |
| A2-A3 route | Yandex Maps installed; no route Skill | Yandex Maps route Skill |
| B1 hotel | no reviewed hotel app or Skill | app preflight, then hotel Skill |
| B2 rental | Cian not installed; no rental Skill | app installation, then Cian Skill |
| B3 landlord | generic OCR/bilingual primitives exist; no workflow Skill | landlord communication Skill |
| C1 nearby search | direct MCP opened nearby results with name, rating, open state, and route action; no Skill | Yandex Maps local-search Skill |
| C2 menu selection | bundled Russia menu Skill and prior end-to-end gate exist; not rerun without VPN | rerun after network returns |
| C3 dietary handoff | same Skill owns the domain, but no separate current acceptance run | extend and gate menu Skill |
| D1 barber | tools and comparison UI exist; no workflow Skill | local-service booking Skill |
| D2 pharmacy | tools, OCR, and bilingual UI exist; no safety workflow Skill | pharmacy availability Skill |
| D3 clinic | tools and bilingual UI exist; no safety workflow Skill | clinic visit Skill |

## Direct Yandex Maps component proof

Without invoking a model, the registered PhoneBridge MCP:

1. opened Yandex Maps and verified it in the foreground;
2. established Android 10 screenshot consent through the single-APK lifecycle;
3. selected the visible restaurant category;
4. observed nearby search results containing a venue name, rating, opening state, and route action.

This is a component proof only. C1 remains incomplete until a natural Chinese prompt makes Codex
select a reviewed Skill, perform the flow, return a useful generic comparison, and stop before route
start. The owner-only screenshot evidence has SHA-256
`c8e9286515e021d2ab383105d9fe956bbbd284cbae888a178d8ce7beca276bd5`.

## Implementation order

1. Implement and bundle the Yandex Maps local-search Skill, then gate C1.
2. Implement the Yandex Maps route Skill and cover A2/A3.
3. Implement the Yandex Go ride Skill and cover A1.
4. Install and preflight the selected hotel app and Cian before starting B1/B2.
5. Continue B3 and D1-D3, adding only generic rich-message primitives or scenario-owned Skills.
