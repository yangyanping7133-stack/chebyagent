---
name: yandex-go-food-order
description: Prepare a food order in the native Yandex Go delivery flow, checking address, items, preferences, delivery and total cost, and stop before purchase.
---

# Yandex Go 外卖

Before opening any app or reading an existing cart, classify whether the request
already supplies all three required inputs: delivery address, food preference, and
budget. If any input is missing, ask for every missing input in one concise reply and
stop the turn without calling a phone tool. For a fully underspecified request such as
`帮我点个外卖`, explicitly ask for address, preference, and budget. Do not open
Yandex Go merely to inspect or reuse saved state for that request.

Only after the three required inputs are known, call `android_phone_status` and require
the phone and Accessibility bridge to be available. Open the installed Yandex Go
package `ru.yandex.taxi` with `android_open_app`, then verify that `ru.yandex.taxi` is
the foreground package before continuing. Never use `android_open_url` or a browser as
a substitute for the native app. If the native package cannot be launched, report the
observed package/tool error; do not infer that it is uninstalled from a browser
handoff.

Inspect whether food delivery is available in the app's current region. If Go requires
a separate native app, explain the observed handoff and obtain user agreement to that
route rather than silently switching.
Confirm the delivery address, budget and relevant preferences from the user; do not
invent an address or treat a map pin as a confirmed apartment. A saved address can
substitute for an explicit address only when the user says the saved/current address
is confirmed. Never inherit an item, cart, fulfillment mode, tip, or payment choice
from an earlier conversation as the intent of a new request. If a cart is already
non-empty, disclose the stale cart and ask whether to keep or discard it before using
it. When the user says the address is already confirmed, inspect and use the app's
saved delivery address instead of asking them to repeat it. Ask for an address only if
no saved address is visible or multiple saved addresses remain materially ambiguous.
Inspect restaurant
availability, minimum order and estimated delivery. Select items/options matching
the request, checking quantities and any extras. For an allergy, do not infer safety
from a name or picture; use stated ingredient information and leave uncertainty clear.
Treat requested cuisine as a hard constraint. Verify it from visible category,
restaurant, or item text; a hot dish alone is not evidence that it is Russian. If the
app does not expose qualifying Russian cuisine, report it as unavailable or unknown
rather than substituting Italian or other food. Re-check every hard constraint after
selection and before presenting the cart.
Cart changes are reversible preparation. Before presenting a confirmation, inspect
the actual cart: items, options, quantities, subtotal, delivery/service fees, discounts,
tip and total payable. State any unresolved fee and expected delivery window.
After an address, item or budget correction, recheck availability, fees and total.
Stop on the submission review screen. Never tap order/pay or any control that might
submit the order during this private test, even if the user confirms the displayed
price. Do not claim the order is placed, paid or being delivered.

## Phone operation and evidence

Use the installed native app through the local `phonebridge` MCP. Check phone and
Accessibility status, open `ru.yandex.taxi` with `android_open_app`, and observe current
UI before acting. Use UI nodes
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
