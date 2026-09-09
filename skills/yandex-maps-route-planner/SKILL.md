---
name: yandex-maps-route-planner
description: Use when a Chinese-speaking user asks Yandex Maps to take or navigate them from the phone's current location to a named destination or address, including requests such as 带我去冬宫, 导航到这个地址, 开车去机场, 走路去地铁站, or follow-ups in the same navigation conversation. Operate the installed Yandex Maps Android app and start live navigation. Choose driving only when the user explicitly asks to drive; otherwise default to walking. Do not use for route comparisons, discovering an unspecified nearby business, or taxi booking.
---

For this ALN build, use readable UI nodes where sufficient. When a phone screenshot
or user attachment is returned, require the main GLM model to receive the actual image
bytes in its next input before relying on pixels. A path, filename, or
unsupported-image placeholder is not visual evidence. The independent vision MCP is
disabled for this release; if neither UI nodes nor GLM native image input can establish
the needed facts, report that blocker. Prior device results do not establish ALN acceptance.

# Yandex Maps Navigator

Navigate in the installed Yandex Maps app from the phone's current position to the user's named
destination. Treat a short destination request as an instruction to act, not as a request for a
route report.

## Workflow

1. Call `android_phone_status`. Require the phone and Accessibility bridge to be available. Open
   `ru.yandex.yandexmaps` with `android_open_app` and verify that Yandex Maps is in the foreground.
   Use only the registered local `phonebridge` MCP server for Android actions. Do not call a cloud
   ChebyNode connector or another Android tool provider.
2. Observe the live screen with `android_capture_screenshot`; also call `android_ui_tree` when it
   exposes useful nodes. Treat the screenshot as primary when map controls have no Accessibility
   nodes. Never reuse coordinates or route facts from an earlier screen.
3. Open the visible search or directions control and enter a locale-appropriate form of the user's
   destination. Preserve an exact address. For a landmark, use a well-known Russian or English
   name only when needed for Yandex search; do not silently substitute another destination.
4. Inspect the result name and visible address before selecting it. If multiple materially
   different places match and the user's intended one cannot be established from context, ask one
   concise clarification instead of choosing. After each action, wait for the page to settle and
   observe it again. Recover from an unexpected page with `android_press_back`, then re-observe.
5. Open the route from Yandex Maps' current location and choose exactly one travel mode from the
   user's intent:
   - choose driving only when the user explicitly says to drive or use a car;
   - choose walking when the user explicitly says to walk;
   - choose walking when the user does not state a mode.
   Do not inspect, compare, recommend, or report other modes.
6. Press the visible Start, Go, Navigate, or equivalent control. Starting navigation is the requested
   action and does not need a second confirmation. Observe the next screen and verify that live
   navigation has started in the selected mode.
7. Leave Yandex Maps in the foreground on the live-navigation screen. The navigation screen is the
   result. Do not return a comparison card, route report, screenshot, ETA, distance, recommendation,
   or explanatory summary. If the conversation protocol requires a non-empty completion, return only
   `已开始导航`.

## Location and recovery

- Treat Yandex Maps' current-location state as authoritative. Do not first ask the user to type a
  starting address.
- If Yandex Maps explicitly lacks location permission or cannot determine the position, report
  that exact blocker and ask the user to enable location. Do not invent an origin.
- If the destination is not found after one reasonable retry with an equivalent locale-aware query,
  state the failure concisely; do not fall back to a browser.
- If loading or network failure prevents navigation from starting, state the observed blocker
  concisely rather than pretending the action succeeded.

## Boundaries

- Do not turn a navigation request into route research or a report.
- Do not share location, call, message, book, order, or pay.
- Do not broaden this Skill to nearby-place discovery, coffee recommendations, taxi ordering,
  hotels, rentals, or other verticals. Those belong to their own tested Skills.
