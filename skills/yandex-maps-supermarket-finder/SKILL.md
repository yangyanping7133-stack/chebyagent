---
name: yandex-maps-supermarket-finder
description: Find nearby supermarkets in native Yandex Maps for 找超市、附近超市、附近买菜, using walking access and current hours without inventing shopping preferences or placing orders.
---

# Yandex Maps 附近超市

Start directly on a short supermarket intent. Call `ace_recall` for this Skill,
check `android_phone_status`, open native `ru.yandex.yandexmaps`, verify foreground
and observe current UI. Search `супермаркет` near current location or the specified
area. Walking is the default. Do not ask for a budget, brand or product when the
owner merely wants a nearby supermarket.

Opening Maps does not reset its map viewport or prior filters. Before comparing,
return from any old route/place page, use the current-location control, and verify
the search area around the current position. Do not silently keep an old `Open now`
or 24-hour filter: a bare nearby request does not mean shopping immediately at night.
Report closed-but-nearby stores and their next opening alongside any open alternative.

Search-list order may favor promoted chain branches. If initial options require
many kilometres or roughly an hour walking, treat that as a search-area/filter
problem, not a satisfactory nearby winner. Recenter, inspect more local map pins,
and try an equivalent `продукты`/`продуктовый магазин` search. Verify actual walking
routes for the genuinely local candidates. Do not call a distant option convenient
merely because another checked option is even farther. If local coverage still
cannot be established, report that specific search failure without a false winner.

Compare a few relevant nearby branches by verified walking route and current
opening hours; prioritize practical access rather than an elaborate city-wide
rating contest. Explicit product needs may require stock checks; a supermarket
category alone cannot prove an item is stocked. Distinguish opening hours from
live stock and never invent either. Observe exact branch links and real storefront
photos when available, checking branch identity after each route or photo screen.
A detail-page screenshot containing ratings, buttons or a photo count is not a
storefront photo. Open one actual photo from the gallery, verify that the single
photo viewer is visible, and use its fresh artifact; do not stop at the thumbnail
grid or let a cropped grid masquerade as a clean hero photo. Otherwise disclose
the missing photo instead of decorating the poster with interface controls.
Check hours against the phone's local date/time.
For a branch link, inspect the place detail's Share menu and its copy-link/URL
surface without selecting a recipient or sending anything. Lack of a dedicated
clipboard tool is not proof the app has no share link. If extraction remains
unavailable, mark link delivery incomplete rather than claiming full acceptance.

Deliver a compact visual result, normally the nearest practical choice plus one
alternative: name, actual walking time, hours, real photo when available, one short
reason, and a verified native Maps link. Clearly state missing photo/stock evidence;
do not create images or silently claim full visual acceptance. Review facts,
photo/link identity and clean layout before sending.
Put the walking time and useful hours immediately below a short name, before the
photo; avoid duplicate transliterations in an oversized title. A verified nearby
option can be recommended as practical, but do not label it “walking best” or
“most suitable” from one nearby store versus one remote store. Limit comparative
claims to the alternatives actually checked. Presentation-only feedback should
reuse verified facts and links, checking only the missing photo when necessary.

Use readable UI nodes and main-model native screenshot vision; no independent
vision MCP. Re-observe changed screens instead of reusing stale coordinates.
Feed screenshot native image content to the active multimodal model (GLM or the
owner-selected alternative); an artifact filename alone is not visual evidence.
No browser, calls/messages, orders, navigation start or payment.
