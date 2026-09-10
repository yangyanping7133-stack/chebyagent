---
name: cian-rental-finder
description: Find and visually compare rentals in native Cian for 找房子、找租房、想搬家 and similar rental intents, applying the owner's work-walking-distance, rent and commission defaults without contacting landlords.
---

# Cian 租房

## Choose the mode before acting

Use **quick nearby mode** for a short, unconstrained request such as
`找我附近的房子`, `附近租房` or `给我看一个附近房源`. This mode is deliberately
bounded for a useful mobile answer:

- Use the phone's current Cian area/location and stay inside native Cian. Do not
  load the private work-location record and do not open Yandex Maps.
- Inspect at most three distinct real listings. Open one strong listing in detail,
  view enough of its gallery to show at least the living/sleeping area plus one
  other material room when available, and record only facts actually visible.
  A complete every-photo gallery pass is not required in this mode.
- As soon as one usable real sample has been inspected, stop browsing and render
  the final HTML report. Hard stop after 24 phone actions or six minutes, whichever
  comes first; use the best observed sample rather than starting another search.
- The completed report may contain one to three homes and must be titled as a
  quick nearby report with a clearly labelled `真实房源样例`. Show rent, visible
  costs/conditions, Cian-displayed location or metro/walk information, photos
  inspected, and all unknowns. Exact walking-route verification and a source URL
  are optional; never invent either.
- If Cian cannot expose even one usable detail page before the hard stop, return
  the polished zero-result HTML with the observed blocker and coverage counts.
  Do not continue into another app and do not turn the quick request into a broad
  audit.

Use **comprehensive work-distance mode** only when the user explicitly asks for
the saved work location, the 1.5 km walking constraint, a Top 5/Top 10, exhaustive
coverage, or a multi-home comparison. All coverage floors, full-gallery passes,
Yandex walking routes and 5–10-home ranking requirements below apply only in that
comprehensive mode. Quick nearby mode overrides those requirements.

## Comprehensive owner defaults and coverage

In comprehensive mode, recall `ace_recall` for this Skill and read the
private work-location record at `/root/.codex/private/user-preferences.md` when
present. Monthly long-term rental is the discovery default, not a confirmed lease
duration. The latest rent ceiling is 105,000 RUB/month. Check minimum term against
any supplied duration; ask only when a missing duration materially affects a choice.
Every qualified listing must be at most 1.5 km by an actual walking route from work,
not a straight-line circle. Resolve the recorded work pin in native Maps before
route checking; ask if absent/ambiguous rather than substituting the current phone
location. Do not publish the owner's precise work coordinates in the poster.

Seek refined, attractive finished interiors: inspect every available photo in each
candidate, checking the whole apartment and consistency with the description,
not just its hero image. Prefer no/low agency commission; the normal target is
at most 50% of one month's rent. Especially good higher-fee listings can appear
in a separately labelled exception section, including fees above 100%; never
silently impose a 100% cap or accept the fee for the user. Explain the exception.

Opening a listing detail or capturing its hero image is not a photo review. Enter
the listing gallery and advance through every image until the observed counter
reaches the final `N/N` image, recording that observed total. Judge the visible
living area, bedroom, kitchen and bathroom when those rooms are present; if a
material room is absent from the gallery, mark the visual evidence incomplete.
Do not infer unseen photos from thumbnails, filenames or the listing's advertised
photo count. A candidate without a completed gallery pass cannot enter the ranking.
Keep an internal photo-pass ledger for each opened candidate: listing ID, observed
final `N/N` counter, material rooms seen, visual rejection reason when applicable,
and the strongest representative interior image chosen only after the full pass.
This ledger supports the decision but is not process content for the final report.

Screen progressively so coverage remains efficient. First reject obvious card-level
mismatches such as rooms, daily rentals, over-budget homes or duplicate listing IDs.
Next review every photo for candidates that survive; an unmistakably dated, sparse,
damaged or inconsistent interior is an immediate quality rejection. Only survivors
need the slower full fee, term, exact-link and Yandex walking-route audit. Record the
stage and reason for every early rejection, but do not spend route tools merely to
prove that a home already failed a decisive earlier gate.

Aim for about ten distinct suitable homes, without padding when fewer qualify.
Allow a longer search than nearby shops. Enumerate and deduplicate platform-visible
listings in the relevant area, recording listing IDs, inspected photo counts,
route distance, costs and eligibility/rejection reasons. Continue through results
and relevant map areas; disclose inaccessible/unchecked coverage. Only claim all
visible candidates checked when enumeration evidence supports it. A radius filter
can discover candidates but cannot prove walking eligibility.

Do not end the task merely because the exact work pin returns zero results, one
candidate fails, or one listing has unknown fees. Keep the 1.5 km walking limit and
105,000 RUB ceiling fixed, but search overlapping nearby streets/complexes in Cian,
then route-check each promising listing from the recorded work pin in native Maps.
A rejected or pending listing is progress evidence, not a stopping condition.
Before `task_complete`, either deliver about ten fully qualified listings or show
credible exhaustion evidence: how many distinct listings were enumerated, which
nearby result areas were covered, how many were opened, and a categorized rejection
count. Interim findings must be presented as progress and followed by more tool use.
Never ask to raise the known budget or expand the known walking limit unless the
owner explicitly requests a tradeoff after the fixed-scope search is exhausted.

In comprehensive mode, the final deliverable is a single polished HTML report containing the best 5–10
fully qualified homes from the verified pool, not the first 5–10 opened and not a
sequence of plain-text listing notes. Rank only after the coverage pass. Do not pad
the ranking with pending or rejected homes; credible exhaustion may produce fewer
than five only when the report gives the required coverage evidence.

Reserve HTML exclusively for the actual ranked deliverable, but never withhold that
deliverable merely because fewer than five homes qualify. Five to ten is the target,
not a minimum acceptance gate. After credible fixed-scope exhaustion, immediately
produce the complete polished HTML with every actually qualified home, even when
there are only one to four. If none qualify, still produce the polished HTML with a
clear zero-result conclusion, coverage evidence and concise next-step tradeoffs;
never substitute a plain-text "Top 5 not ready" message. Do not pad the ranking with
pending or rejected homes. A normal per-listing rejection, missing field, stale
retained record or need to revisit another listing is not a tool blocker and is not
credible exhaustion. Treat exhaustion as credible only after an adaptive coverage
pass has enumerated a substantial deduplicated pool across the relevant nearby
result areas (normally at least 15 distinct in-budget cards), opened every plausible
survivor, and reported the exact counts and covered areas. If a real tool blocker
prevents continuation, return a short plain-text blocker with the last successful
action and remaining work; never disguise it as the final HTML deliverable.

In comprehensive mode, apply an explicit early-stop gate when Cian exposes a large result pool. A single
opened or rejected listing can never establish exhaustion while other in-budget
cards remain accessible. Before returning fewer than five qualified homes, first
scan at least 20 distinct plausible cards when available, then open and inspect at
least 10 of the strongest distinct candidates unless fewer than 10 plausible cards
survive the card-level pass. Fully audit photos, costs, source links and walking
routes for the strongest survivors. These are coverage floors, not a reason to stop
when more work is needed to find the best homes. If UI interference interrupts one
listing, recover and continue from the recorded candidate ledger rather than
converting the interruption into a zero-result conclusion. The final coverage
section must make these counts auditable.

Read [the rental report contract](references/report.md) before the final answer.
Deliver real interior photos and observed Cian and Yandex links for every selected
home, with rent, deposit, commission, other costs, walking-distance check and a
short reason. Review photos, costs, route, link identity and HTML rendering
constraints before delivery. Unknown decisive facts remain pending leads, not
silently accepted homes. Never contact/apply/book/pay.

Open the installed Cian package `ru.cian.main` with `android_open_app` through the
local phonebridge, then verify that `ru.cian.main` is the foreground package. Never use
`android_open_url` or a browser as a substitute for the native app. If the package
cannot be launched, report the observed package/tool error rather than guessing that
the app is absent. Use the defaults and prior supplied conditions above before asking
for missing information; do not ask the owner to repeat known budget or area.
Distinguish monthly rent, daily stays and minimum lease periods. Apply appropriate
filters and open actual listing details. A search card alone is not enough to claim
lease compatibility or total costs.
Record rent, utilities and whether included, deposit, agent commission, other fees,
minimum term, availability and applicable tenant/pet restrictions when shown.
Calculate recurring monthly cost separately from cash needed at move-in, identifying
refundable deposit separately. If a component is unknown, show a known subtotal and
the gap rather than an invented all-in total. Compare real listings by user fit,
not just headline price. Preserve listing identifier/link if visible and enough
address/title information to retrace it. Recheck the selected listing after the
user changes budget, area or duration. Do not contact, apply, send personal data,
schedule viewing or pay. Missing login or region support is an explicit blocker.

## Phone operation and evidence

Use the installed native app through the local `phonebridge` MCP. Check phone and
Accessibility status, open `ru.cian.main` with `android_open_app`, and observe current
UI before acting. Use UI nodes
when readable. After `android_capture_screenshot` returns an artifact, use its native
image content in the next GLM turn; do not call an independent vision MCP and do not
claim to have seen an image from its filename. If the image is not delivered to GLM
and the relevant screen cannot be read from UI nodes, explain the blocker and stop.
Re-observe after each navigation or material UI change; do not reuse stale coordinates.
Login, permission or location obstacles require the user's normal participation.
Do not substitute another app or browser silently. Use observed names, prices and
conditions; keep unknowns explicit. Return concise Chinese progress through existing
generic messages. The final answer must always use the HTML report contract for the
selected mode. In quick nearby mode, one fully observed sample is a completed
coverage pass; do not keep browsing to satisfy comprehensive-mode counts. In
comprehensive mode, keep enough place/listing detail to retrace every item and do
not substitute plain text merely because fewer than five listings qualified.

Validation status: draft workflow; no ALN real-Agent acceptance is claimed.
