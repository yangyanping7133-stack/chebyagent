---
name: yandex-go-taxi-booker
description: Prepare a taxi in native Yandex Go from the phone's current location to the user's destination and handle corrections or fare-confirmation follow-ups. Use the user's requested class, otherwise choose the lowest-priced ordinary available class. This private acceptance build stops before submitting a ride, including after confirmation. Do not use for navigation, food or delivery.
---

For this ALN build, use readable UI nodes where sufficient. When a phone screenshot
or user attachment is returned, require the main GLM model to receive the actual image
bytes in its next input before relying on pixels. A path, filename, or
unsupported-image placeholder is not visual evidence. The independent vision MCP is
disabled for this release; if neither UI nodes nor GLM native image input can establish
the needed facts, report that blocker. Prior device results do not establish ALN acceptance.

# Yandex Go Taxi Booker

Prepare a taxi in the installed Yandex Go app. This private acceptance build stops before submission,
including after the user confirms a displayed fare. Treat the
app's live order screen as the primary interface; keep chat output to the minimum needed for the
confirmation boundary.

## Prepare the ride

1. Call `android_phone_status`. Require the phone and Accessibility bridge to be available. Open
   `ru.yandex.taxi` with `android_open_app` and verify that Yandex Go is in the foreground. Use only
   the registered local `phonebridge` MCP server for Android actions.
2. Observe each screen with `android_capture_screenshot`; also call `android_ui_tree` when it exposes
   useful nodes. Use fresh observations after every action. Never reuse coordinates, prices, or
   availability from an earlier screen.
3. Enter the Taxi service when Yandex Go opens on its multi-service home screen. Keep the pickup as
   the app's current-location suggestion unless the user provides another pickup. Do not ask the
   user to type their current address, and do not repeat their precise pickup address in chat.
4. Enter the user's destination. Preserve an exact address. For a landmark, use a well-known Russian
   or English form only when needed for Yandex search. Verify the visible result name and locality
   before selecting it. Ask one concise clarification when materially different places match.
5. Select the car class from the user's intent:
   - select the explicitly requested class when one is stated;
   - when no class is stated, compare the currently available ordinary passenger classes and
     select the lowest-priced one; this is commonly Economy or Comfort, but rely on the live labels
     and fares rather than a remembered class name;
   - never inherit a previously selected premium class such as Business, Premier, or Elite merely
     because Yandex Go kept it selected from an earlier session;
   - do not silently upgrade to a premium class. If only premium or otherwise unusual options are
     available, stop at the review screen and ask the user to choose.
6. Review the live order screen. Verify the destination, selected class, current visible fare,
   pickup estimate, and payment state when exposed. Do not change the payment method unless the user
   asks. Never transcribe account, phone, card, or other payment details into chat or evidence.
7. Stop before the control that places the ride request. Do not press Order, Request, Confirm, or an
   equivalent final control. Leave Yandex Go on this screen and state briefly, for example,
   `已到最终确认前：Business，当前 1,250 ₽，未叫车。` Include a pickup estimate only when
   clearly visible. For acceptance, retain real screenshot evidence of this exact review
   boundary; do not publish precise pickup/payment details. Do not ask for a confirmation
   that this private build cannot execute, or replace the real final screen with a mock.

## Confirmation in this private acceptance build

Re-observe the pickup, destination, selected class and fare after a follow-up. If anything
changed, show the new values for review. Leave the app before its final order control.
Tell the user `信息已核对，本次私测不实际叫车。` Do not press any control that submits
a ride, initiates payment or contacts a driver. Never report `已叫车` from a review screen.

## Blockers and recovery

- If location access is unavailable, state that location is required and ask the user to enable it.
- If login, phone verification, payment setup, or a required Yandex Go consent blocks preparation,
  state the exact user action needed and stop. Do not invent account state or bypass the app.
- If the destination cannot be found after one locale-aware retry, state the failure concisely. Do
  not fall back to a browser or silently choose another place.
- If loading or network failure prevents a live fare or order screen from appearing, report that
  blocker instead of fabricating availability.
- Recover from an unexpected page with `android_press_back`, then re-observe before continuing.

## Boundaries

- Never submit a ride in this private acceptance build, including after price confirmation.
- Never silently upgrade an unspecified request to a premium class.
- Do not cancel a ride, contact the driver, share location, change payment, or accept a cancellation
  fee without a separate explicit user request and any required confirmation.
- Do not broaden this Skill to navigation, coffee search, delivery, food, scooters, car sharing, or
  other Yandex Go services. Those require their own tested Skills.
