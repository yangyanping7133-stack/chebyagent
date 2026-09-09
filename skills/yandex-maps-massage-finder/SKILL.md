---
name: yandex-maps-massage-finder
description: Find professional massage in native Yandex Maps for 找按摩店、想按摩 and similar intents; apply the owner's non-Thai 90-minute full-body preferences and verify practitioner, price and times without booking.
---

# Yandex Maps 按摩

## Owner defaults and acceptance

Short intent starts discovery. Recall `ace_recall` for this Skill. The owner's
recorded request is 90-minute full-body relaxation massage, not Thai-style
treatment, with a female practitioner and total price at most 7,000 RUB.
Non-Thai describes treatment, not the practitioner's nationality. Old one-off
dates/times are not reusable defaults: ask which time suits the owner while
progressing with service/price comparison. Use the requested area or nearby;
when the user says near work use the private work-location record, not GPS.

Maps retains old searches and the first results may be distant promoted listings.
Reset the old search/route, recenter on the visible current-location indicator and
check local coverage before deep inspection. A city-wide map at kilometre scale
does not prove nearby coverage. When search auto-zooms out, inspect nearer pins or
use a local equivalent query; do not label a remote shop nearby from list order.
Recentring changes the centre, not necessarily the zoom. Verify street-level
context and zoom to a walkable neighbourhood if necessary. Native category
discovery can recover local places missed by repeated broad text searches: use
the visible salon/spa category or local `массаж` query as complementary discovery,
then inspect the actual massage service rather than treating every spa as eligible.
Check the viewport again AFTER results arrive: search can zoom out even when the
pre-search map was local. Keep the query, collapse the results sheet, recenter and
zoom on the map; wait for its search refresh, then reopen the list or inspect local
pins. A live audit changed the distant promoted list into local listings this way.
Do not replace this correction with another broad text search or ask whether to
search nearby when nearby was already requested.

Saved collections are an additional discovery source, not proof of eligibility or
nearby coverage. Listing-only triage does not complete a requested comparison:
Open promising non-excluded entries beyond the collapsed overview: expand the
place sheet and inspect available native service, staff and photo sections before
calling their evidence unavailable. An overview's generic "massage from" advert
does not complete that check. If useful inspection cannot continue, state the
specific observed obstacle rather than treating unopened sections as missing data.
Do not turn an instruction to check a collection first into a collection-only
search. If its useful entries are distant, resume local discovery unless the owner
explicitly limited the scope; do not claim no nearby matches from that list alone.
A venue name containing "Thai" is a lead to check treatment type, not proof that
every service is Thai. Exclude on actual incompatible service evidence, not name
alone; an unverified treatment remains unknown rather than eligible.

Inspect an actual identified practitioner's portrait for the owner's subjective
young-adult/pretty preference; no stock photos or inferred age, gender or skill.
Use listed provider information for gender/service matching and show the photo
for the owner's judgment. A photo-tab thumbnail grid is discovery, not a completed
photo audit: open relevant still photos at readable size and inspect visible native
captions/profile links for attribution. A person receiving a treatment is not proof
of who provides it. If attribution remains unavailable, keep that requirement open;
do not claim there are no practitioner photos from inspecting only the first grid.
Missing portrait, exact 90-minute tariff, or practitioner
service evidence means a pending lead, not a verified eligible winner. Compare
credible reviews and walking access; return real photos, total price, duration,
practitioner, time status, one useful reason and exact native shop links in a
concise visual poster. Audit the matching evidence before recommending.
The poster hero must be an actual venue or identified practitioner photo, not a
price-list screenshot, thumbnail grid or service catalogue. A tariff screenshot
may support a price claim but does not satisfy the photo requirement. Do not
headline a lead as “verified” when its essential matching conditions are unknown;
with no qualified photographed option, return a short accurate gap report.
For that negative result, lead with the unmet requirement and summarize only useful
near-misses; do not include ACE revisions, map scale or a narration of tool steps.
Say “not verified in the inspected evidence”, not “the platform has no such data”
unless the wider claim was actually checked. Keep established facts distinct from
unverified conditions, and never decorate an ineligible lead as the winner.

Open `ru.yandex.yandexmaps`; search for professional relaxation massage such as
`расслабляющий массаж` in the requested area or current location. Inspect service
menus and reviews relevant to that service. Match the requested duration exactly:
a 60-minute price is not evidence for 90 minutes, and combined packages are not a
single massage unless described that way. Separate listed price, duration, provider
preference and available appointments; an open business does not prove a free slot.
Before quoting a tariff, retain and check the same listing row's exact service
name, duration and single-session price together. Never borrow “90 minutes” from
a gift-certificate bundle or search query and attach an adjacent 60-minute price.
Open the item or re-observe if the row is truncated. Do not describe a treatment
as explicitly non-Thai unless the observed service description establishes it;
a hot-stone label alone does not prove its full technique or full-body coverage.
When the relevant native service category is visible at the bottom of the screen,
scroll it into view and inspect its listed item before concluding that duration or
price is unavailable. A category heading with an item count is a discovery lead,
not proof that the service details are absent.
Do not infer provider gender, qualifications or availability from names or photos.
Present inspected options and unknowns without medical treatment claims. Do not
call, message, reserve or pay in this private acceptance workflow. Never launch a
website or external browser. Read-only service, provider and availability steps
inside the native app may be inspected, but stop before any appointment-creation
control or communication with a provider. A generic Book online/Make an appointment
online button may open an external browser, even from Maps. Its label alone is not
proof of a native calendar: do not probe destination-unknown booking links. Use
clearly native staff/service surfaces and record an unavailable native calendar.
If one listing lacks requested details, record its gap and compare other plausible
local candidates rather than concluding all providers are unverifiable. Do not
open a browser to fill the gap. Verify finalists' actual walking route previews,
not driving or listing-card estimates; never press Start/Let's go.
If reporting an incomplete lead without checking its route, label any quoted
listing-card distance/time explicitly as an unverified estimate, or omit it.

## Phone operation and evidence

Use the installed native app through the local `phonebridge` MCP. Check phone and
Accessibility status, open the app, and observe current UI before acting. Use UI nodes
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
