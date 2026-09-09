---
name: yandex-maps-restaurant-finder
description: Find and compare nearby restaurants in native Yandex Maps for 找餐馆、找餐厅、附近吃什么, returning real photos and native links; not delivery ordering or menu-only translation.
---

# Yandex Maps 餐馆发现

A short dining intent starts discovery. Call `ace_recall` for this Skill, check
`android_phone_status`, open `ru.yandex.yandexmaps` with `android_open_app`, and
verify the foreground app with a fresh observation. Default to nearby current
location and walking unless the user gives an area or transport mode. Do not invent
a cuisine, budget or party size; only ask if a missing choice materially blocks
selection. Explicit user conditions override defaults.

Progressively scan several plausible restaurants, then inspect the strongest by
relevant recent reviews, credible rating and review count, food, atmosphere,
applicable prices and walking access. Broaden when the shortlist is weak or
homogeneous, stop when additional inspection adds little. A roughly three-minute
search is a useful soft target, not permission to skip verification or a forced
30–50-place quota. Do not label the first result or unverified rating leader best.
If the first screen mainly contains banquet-only venues or fast-food counters,
continue nearby list/map discovery for ordinary dine-in restaurants before
declaring a winner. Fast food is not categorically excluded, but a detailed menu
alone does not establish the best dining experience. Do not require the user to
choose a cuisine merely to finish this default comparison.
Use two stages: compare candidate quality and fit first, then collect full evidence
for the strongest shortlist. Ease of obtaining photos/menu/links is not dining
quality. Missing details about an alternative are not evidence it is worse.

Inspect real photos of the exact branch, including dining environment and relevant
food. No generic/stock/generated photos; a weak 4.3-rated or photo-less shop should
not be included merely to fill a card. Compare credibility, not a fabricated hard
rating threshold. Check listed menu prices as examples, not an invented bill.
The owner explicitly rejected 4.3-rated selections: do not promote another
4.3-rated restaurant as the default winner just because its information is complete.
Continue discovery for stronger options, or explain that no convincing selection
was verified; a relaxed exception requires the owner's explicit choice.
Open a clear single venue/environment photo rather than using a gallery grid,
menu screen or unrelated dish as proof of atmosphere. Report rating counts and
written-review counts separately when the app distinguishes them.
Use actual walking route previews for time/path and observed shop/share links;
opening a share sheet to copy a link is not permission to send it to anyone.
Recheck branch identity after browsing photos or routes. Native screenshots may
be displayed using their actual `artifact_image_uri`, honestly captioned.
Distinguish detail-card distance estimates from verified walking route previews.
Read opening status literally: "opens at 12:00" means noon opening, not "open
until 12:00". A bare dining request does not imply an open-now filter.

Return usually 1–3 well-supported options in the mobile HTML poster format: short
headline, branch name, walking time, rating with count, meaningful price evidence,
real photo, one specific reason and an exact native Maps link. Keep observed
opening status and rating/sample count when shortening a result; remove repeated
explanations and ACE implementation notes instead of decision-critical facts.
Before sending,
audit the chosen constraints, photo/link identity and readable concise layout.
For the proposed winner, check the evidence before publishing: did you actually
open its walking route preview, and inspect recent written reviews rather than
only the Maps-generated summary? A detail-card time remains an estimate even if
the share link and photo are already complete. Fill these two gaps for the
existing shortlist instead of restarting discovery. Report any unavailable
evidence plainly; do not silently label it verified or claim a stronger comparison
than the candidate inspections support.
No qualified matches means a clear gap, not padded recommendations. State the
best among compared eligible options, not a city-wide optimum.

Use UI nodes when readable and the main model's native screenshot vision where
necessary; no independent vision MCP. Re-observe after material screen changes.
Feed screenshot native image content to the active multimodal model (GLM or the
owner-selected alternative); an artifact filename alone is not visual evidence.
Do not open a browser, contact/reserve/order/pay or start navigation. Business
opening hours do not prove a free table. Preserve unknowns explicitly.
