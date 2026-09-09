---
name: yandex-maps-haircut-finder
description: Find nearby haircuts in native Yandex Maps for 找理发店、想剪头发 and similar intents, using the owner's men's-cut and female-stylist preferences; verify price and available times without booking or contacting.
---

# Yandex Maps 理发

## Owner defaults and acceptance

Start on a short haircut intent. The owner is a man: seek a female stylist who
explicitly offers men's haircuts, total service price at most 4,500 RUB, nearby,
with an available appointment within the next seven days in local time. Do not
confuse this with needing a haircut immediately. Current explicit time, price or
area overrides the corresponding default. Recall `ace_recall` for this Skill.
Search men's cuts in salons as well as barbershops; do not restrict discovery to
male-only barber teams. Compare relevant reviews, price and walking access.

Opening Maps preserves old searches, routes and filters. Close the old search or
route, recenter on the current-location control, and verify the local search area
before comparing. Recenter does not reset zoom: a live audit stayed at a 1.6 km
scale after recentering. Inspect the scale and nearby street labels, zoom to a
walkable neighbourhood when necessary, and try the native Categories → Beauty
salons entry. This recovered local salons that the previous broad text searches
missed. The scale bar is not a search-radius guarantee; verify candidate routes.
A promoted city-wide result with a driving estimate is not
nearby evidence. If the first results are many kilometres or about an hour walking
away, correct the viewport and inspect local pins/results with an equivalent
`парикмахерская` or `салон красоты` search before spending time on their menus.
Do not retain an Open now filter: this request normally allows the next seven days.
Reject a verified distant candidate as the nearby default; do not ask the owner
whether to search closer when nearby is already the request.

“Business moved” does not by itself invalidate the business: if the native Maps
card offers “New address”, inspect that in-app destination before rejecting the
lead. Reconcile the resulting name/address with previously inspected branches;
a renamed or relocated duplicate is not an additional candidate. Reuse its known
evidence gaps rather than repeat the full inspection unless new information appears.
Describe coverage by the results actually inspected, not the map scale bar.

Verify the named stylist's listed service and gender from profile information,
not appearance alone. Inspect an actual photo of that person (not a stock model,
shop interior or an unidentified team member). The owner's attractive/pretty
preference is subjective: show the real portrait for their judgment and describe
visible presentation neutrally, never claim objective beauty or infer competence.
Verify live slots through read-only native service/provider/availability screens
if available. Opening hours are not appointment evidence; unknown slots or missing
portrait/service evidence mean a pending lead, not a fully qualified recommendation.
Do not launch a browser, contact the salon or press an appointment-creation control.

For plausible local salons, inspect the native listing's staff/services and any
clearly in-app read-only availability flow. A service price list alone is not a
completed attempt to verify the stylist or appointments. Viewing staff, a service
or a calendar is distinct from submitting a reservation; stop before any control
that creates one. Do not follow an external booking website under the no-browser
constraint. If the app has no usable native staff/calendar surface, record the
observed limitation and compare another plausible local salon rather than assume
all salons lack it. Missing identity, portrait or slots remains unqualified; state
the exact gap without asking the owner to repeat their default date range.
The generic “Make an appointment online” button can open a browser custom tab
(observed for Wow For You). Its label alone does not establish a native calendar.
Do not probe a destination-unknown booking link to discover where it opens; use
clearly native staff/service surfaces, or report the native-evidence limitation.

Deliver a compact beautiful poster with real photos, stylist and shop, men's-cut
total price, verified date/time, walking time, short reasons and observed native
shop links. Review every criterion and branch/photo identity before delivery;
compare plausible alternatives and do not pad the shortlist with disqualified shops.

Open `ru.yandex.yandexmaps`; use the local salon category and relevant service
searches such as `мужская стрижка` or `салон красоты` as complementary discovery,
not repeated identical broad searches when they keep returning distant shops.
Use the app's current location unless the user supplies an area.
Compare inspected candidates by the requested service, recent relevant reviews,
review count, distance and current hours. Rating alone does not establish quality.
A salon listing does not prove it offers the requested cut. Inspect the selected
shop's services or price list; distinguish a listed starting price from the actual
cut's price. If today is requested, verify today's hours and distinguish being open
from having a free appointment. Report observed booking methods without calling,
messaging or reserving. For finalists verify the actual walking route after
identifying the selected place unambiguously; a driving estimate or listing-card
estimate is not that evidence. Preview only, never press Start/Let's go.

## Phone operation and evidence

Use the installed native app through the local `phonebridge` MCP. At the start of every
user turn, including a short follow-up in the same conversation, call
`android_phone_status`, open `ru.yandex.yandexmaps` with `android_open_app`, verify that
Yandex Maps is actually in the foreground, and capture a fresh observation before the
first tap. Never assume the app or card from the previous turn is still foreground and
never make the first action from remembered coordinates or prior UI state. Use UI nodes
when readable. After `android_capture_screenshot` returns an artifact, use its native
image content in the next GLM turn; do not call an independent vision MCP and do not
claim to have seen an image from its filename. If the image is not delivered to GLM
and the relevant screen cannot be read from UI nodes, explain the blocker and stop.
Re-observe after each navigation or material UI change; do not reuse stale coordinates.
Login, permission or location obstacles require the user's normal participation.
Do not substitute another app or browser silently. Use observed names, prices and
conditions; keep unknowns explicit. Return concise Chinese results through existing
generic rich messages, with enough place/listing detail to find the evidence again.

Validation status: draft workflow; no ALN real-Agent acceptance is claimed.
