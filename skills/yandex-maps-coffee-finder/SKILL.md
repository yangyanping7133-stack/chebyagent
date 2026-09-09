---
name: yandex-maps-coffee-finder
description: Find and compare nearby coffee shops in the installed Yandex Maps Android app for a Chinese-speaking user, including follow-up refinements. Progressively evaluate atmosphere, ratings and walking access, then deliver a concise photo-rich recommendation with exact place links. Not for booking, ordering, calling, or routing to an already chosen destination.
---

# Nearby coffee, thoughtfully chosen

Use the installed `ru.yandex.yandexmaps` app through local `phonebridge` tools.
Start from the phone's actual location; ask for an address only if Maps cannot locate
the phone. A request to find coffee is sufficient to start. First use `ace_recall`
for `yandex-maps-coffee-finder`; current user requests override recalled preferences.

## Search intelligently

- Default to walking unless the user specifies otherwise. Walking time, not
  straight-line distance or driving time, is the useful access measure.
- Check phone/accessibility availability, open Maps and observe the current screen.
  Use readable UI nodes when sufficient, actual screenshots for photos/canvas.
  Use native image input for supported multimodal providers, including GLM. The
  main model must receive image bytes before judging visual evidence.
  No independent vision MCP, remembered coordinates or browser fallback.
- Broadly scan the initial nearby results, then inspect promising contenders in
  depth. For an ordinary short request, scan at most about eight distinct visible
  cards, shortlist at most three, and fully verify the strongest two or three.
  Expand only when the shortlist is weak/uncertain or the user asks, never to fill
  a quota.
- Treat three minutes as the default end-to-end service budget, including the
  poster. Stop discovery after roughly two minutes and spend the remaining minute
  finishing photos, walking checks, exact links and the answer. If two candidates
  already have strong credible ratings, branch-specific interior photos, verified
  walking access and exact links, deliver them immediately; a third candidate is
  optional. Allow at most one short overrun to finish an action already in flight.
  Never keep browsing merely because more stores exist. When evidence is still
  insufficient at the budget boundary, return the smaller honestly verified set
  instead of guessing. Do not insist on 30–50 stores or stop after the first two
  high scores before checking their photos and walking access. Expand when requested.
  Describe limited coverage honestly; do not claim an absolute best.
  Scan the results list before spending most of the budget on the first place's
  photos/share controls. If finalists require a long walk, check whether nearer
  credible options exist before calling one the best fit for a walking request.
  A few long routes do not prove there are no closer cafes. Keep absence/nearest
  claims scoped to the actual candidates checked, including in the poster title.
- Treat the stop rules as hard gates, not suggestions. The moment two venues
  satisfy the rating, real-photo, walking and exact-link gates, stop opening new
  venues and build the answer. After the results list is visible, use no more
  than 24 phone actions total; at the cap, return the strongest fully verified
  subset immediately. Do not spend extra actions trying to improve a venue that
  already has sufficient evidence.
- Judge atmosphere from actual venue photos and relevant reviews, coffee suitability,
  rating credibility including review count, opening status and walking effort.
  A high score alone is insufficient. Mixed-category places may fit a coffee visit;
  do not exclude them solely for having a bakery/restaurant label.
  Recommendation admission is stricter than search inclusion: a venue needs a
  verified representative real photo of this branch and credible strong ratings.
  No real photo means exclusion from both the main pick and recommended alternatives;
  an AI summary image, missing-photo label or unrelated photo does not qualify.
  Do not promote a clearly weak-rated option (such as 4.3 in this comparison)
  merely because it saves a few walking minutes or fills a second card. Prefer
  one genuinely qualified recommendation to padding with lower-quality venues.
- Re-observe changed screens after actions; avoid repeated unchanged screenshots.
  In code-mode, forward screenshot image blocks with `image(block)` and print only
  `structuredContent` for metadata; never stringify the whole screenshot response
  (it includes base64), then call `view_image` just to see that same capture.
  If a tap does not open the intended control, inspect fresh bounds or expand the
  place sheet before retrying; do not repeatedly probe a partly hidden bottom bar.
  Check recent negative reviews when they could change the choice. Keep exact
  branch/name/address to avoid confusing different locations of a chain.

## Ground photos, routes and links

For finalists, collect a representative real photo, rating/review count,
observed opening status, walking time and a short route summary when available.
Open a representative individual interior photo when possible, rather than a
gallery grid dominated by tabs, buttons and food thumbnails. Capture the photo
page if a direct photo URL is unavailable; use the
returned `artifact_image_uri` and caption it as a Maps photo-page screenshot.
Never substitute stock/generated venue photos or invent image URLs.
Use static thumbnails or static gallery images only. Never tap a video, a play
icon, an autoplay hero, or a moving story/reel. Never wait for a slideshow to
advance. If video or autoplay opens accidentally, press Back immediately and use
the already visible static thumbnail as evidence. Inspect at most three static
photos per finalist; one clearly representative interior photo is enough for the
poster.

Use the place card's Share/Copy link interface to obtain its actual HTTPS place
link from visible UI/tool output, without sending anything to anyone. A URL
invented from a name is not an exact branch link. Verify branch identity.
After closing photos or returning from a route, confirm the place title before
collecting a link: a map tap can select a different venue at the same address.
Reuse already verified links instead of reopening Share to obtain the same URL.
For each finalist, open Share only once after its identity is confirmed. Read the
visible HTTPS URL from the share sheet/UI output, then press Back and continue; do
not wait in, repeatedly reopen, or probe the Huawei chooser after the URL is already
observed. If the chooser does not expose a usable URL after one recovery attempt,
drop that candidate or report the link gap instead of consuming the whole service
budget.
Inspect walking route preview for time and route summary, without starting live
navigation. Include a route link only if actually obtained; the verified place
link remains useful without it. Do not draw invented turn-by-turn routes.

## Deliver the result

Read [the poster contract](references/poster.md) before the final answer.
Give one attractive, clickable HTML poster: a clear first recommendation, usually
two or three genuinely worthwhile choices in total, real photos, brief reasons,
walking access and exact Maps links. Adjust the count to evidence and the request.
Do not return obsolete `comparison` JSON or plain text alone when a poster is
possible. Avoid repeating a long report beneath it.

Remove candidates without verified real photos or with clearly weak ratings from
the recommendation poster. If only one qualifies, recommend one; if none qualify,
say no candidate met the criteria within the checked set, not that none exist nearby.
Other missing facts such as route detail/time/link must be explicit and remain a
partial-delivery gap; never pad the result or invent evidence.

When corrected, use ACE to update this Skill's persistent learning overlay,
not the bundled file. Distinguish general preferences from a one-off request.

## Boundaries

Do not book, call, message, order, pay, share externally or start live navigation.
Leave Maps on a useful place card or results list. Search/route preview does
not authorize those side effects. Report location/accessibility failures precisely
and ask only for the smallest needed user action.
