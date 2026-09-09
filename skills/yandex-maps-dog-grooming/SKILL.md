---
name: yandex-maps-dog-grooming
description: Find dog washing or grooming in native Yandex Maps for intents such as 找洗狗店、给狗洗澡、找宠物店; verify breed, weight and service price without booking. Explicit pet-supply shopping is not grooming.
---

# Yandex Maps 大型犬洗护

## Owner defaults and acceptance

A short grooming intent is enough to start; do not ask the owner to repeat known
preferences. Latest owner-supplied profile: Samoyed, about 10 months old, 22 kg
(September 2026; do not treat age/weight as permanently current). Prefer a complete
bath/dry/brush price at or below 4,500 RUB; hard ceiling 5,000 RUB unless explicitly
changed. Current user conditions override defaults. Nearby means current phone
location unless an area was specified; walking is the default comparison mode.
Recall `ace_recall` for this Skill before choosing candidates. Progressively inspect
several plausible shops; confirm the Samoyed/22 kg tariff, coat surcharges and full
service, rather than promoting an attractive generic starting price. A shop whose
price or breed acceptance is unknown is a pending lead, not a qualified winner.

Return a concise visual shortlist with actual shop/service photos, verified native
Maps links, walking time, applicable total price and one reason per choice. Audit
photo identity, price applicability and evidence before delivery; reject missing
photo entries from the qualified shortlist rather than padding it. Use the shared
mobile rich-result format. Distinguish best among compared eligible options from
an unsupported city-wide best. No booking or contact.

Open `ru.yandex.yandexmaps` and search `груминг крупных собак` or a suitable grooming
category near the requested area/current location. Inspect actual service details,
not just the shop category. For a Samoyed check bath, thorough drying and brushing,
and whether the listing explicitly accepts large or double-coated dogs.
If weight is unknown, say so; ask for it when needed to determine a stated weight
band. Never choose a weight or equate a `до 30 кг` service with acceptance of every
Samoyed. Prices may vary with weight, coat condition or extra de-matting; distinguish
published starting prices from a final quote. Do not substitute shaving for the
requested bath/dry/brush service. Return traceable candidates, supported acceptance
limits and unresolved details. Do not contact the groomer or book during acceptance.
Use walking route previews to verify travel time; never press Start/Go/Поехали or
enter turn-by-turn navigation during discovery. A car-route distance is not proof
of walking distance or that no suitable nearby shop exists. If the first search
returns remote chains, re-center on the observed location and try the local grooming
category before concluding that no nearby option qualifies.
Promoted chain results are not a distance-ranked local search. Close the old search,
observe the current-location marker and local map extent, then compare nearby pins
or a local query such as `зоосалон` / `груминг`. Merely replacing the old search text
does not establish local coverage. Do not call a shop the closest from a few promoted
cards, and label listing-card travel estimates separately from verified route previews.

For a finalist, inspect a clear single photo from its native Photos view. A blurred
autoplay video, advertisement or full listing-page screenshot is not a usable venue
or service photo. If only unqualified leads remain, give a concise gap report rather
than a winner-shaped poster. Any visual lead card still needs its observed exact
branch link; an open app page alone does not provide the owner a reusable link.

## Phone operation and evidence

Use the installed native app through the local `phonebridge` MCP. Check phone and
Accessibility status, open the app, and observe current UI before acting. Use UI nodes
when readable. After `android_capture_screenshot` returns an artifact, use its native
image content in the next GLM turn; do not call an independent vision MCP and do not
claim to have seen an image from its filename. If the image is not delivered to GLM
and the relevant screen cannot be read from UI nodes, explain the blocker and stop.
Re-observe after each navigation or material UI change; do not reuse stale coordinates.
In code mode emit a UI observation once, not both the text content and the same full
structured tree. If it is truncated, recover a compact set of observed labels,
bounds and relevant state or a native screenshot before acting on the missing area.
For `android_swipe` the fields are `start_x`, `start_y`, `end_x`, `end_y`, plus
`coordinate_space`; `x1/y1/x2/y2` are invalid. Inspect every action result before
issuing another action. On an error or unexpected screen, stop the planned sequence
and re-observe; do not blindly retry coordinates. If navigation starts accidentally,
exit it using the observed stop control, report the incident, and do not call the run
accepted merely because a final poster was produced.
Login, permission or location obstacles require the user's normal participation.
Do not substitute another app or browser silently. Use observed names, prices and
conditions; keep unknowns explicit. Return concise Chinese results through existing
generic rich messages, with enough place/listing detail to find the evidence again.

Validation status: draft workflow; no ALN real-Agent acceptance is claimed.
