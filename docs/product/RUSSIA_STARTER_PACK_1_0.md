# Russia Starter Pack 1.0

Status: frozen product acceptance baseline

Date: 2026-08-09

## Product outcome

A Chinese tourist or short-term worker newly arrived in Russia can describe a goal in Chinese or
attach a photo, let ChebyAgent operate a small reviewed set of Android applications, and receive a
useful Chinese result without learning the Russian interface first.

The user sees one freestyle conversation. Internally, Codex selects a bounded scenario workflow,
uses PhoneBridge to operate the phone, and requests explicit confirmation before a consequential
final action.

## Delivery boundary

- App-first: use the installed Android app whenever it provides the required capability. Web is a
  fallback, not the default route.
- The engineering Mac preinstalls and updates test dependencies with ADB. Automatic application
  download or installation is not a 1.0 Agent capability.
- Initial reviewed apps: Yandex Go, Yandex Maps, Cian, and one hotel-booking Android app selected
  during the device preflight.
- ChebyAgent handles camera/gallery input, image understanding, Chinese/Russian presentation, and
  user confirmation without requiring a separate translation app.
- CAPTCHA, SMS/MFA, identity documents, biometric prompts, payment authorization, and final
  booking/order/message submission remain user-controlled.
- The renderer and gateway remain business-agnostic. Russia-specific behavior belongs in scenario
  instructions and acceptance tests, not in transport or rich-message parsing.

## Common end-to-end pass criteria

Every case must prove all of the following on the target phone:

1. A natural Chinese prompt or photo starts the case; no hidden engineering command is required.
2. Codex selects the intended app and does not replace an available app with a website.
3. PhoneBridge reaches the correct foreground package and performs only the required interactions.
4. The result contains enough Chinese information for the user to make the next decision.
5. Any consequential final action stops at an explicit confirmation boundary.
6. After an app switch, keyboard dismissal, or return to ChebyAgent, the same Thread remains usable.
7. The evidence record contains the prompt, app/package, final screenshot or structured result, and
   pass/fail reason, but no credential, internal Thread ID, private path, or raw tool payload.

## Scenario A: mobility

### A1 Airport to hotel by taxi

- Prompt: `从机场打车去这个酒店：<酒店名称或地址>`
- Primary app: Yandex Go.
- Expected: destination is resolved, ride options show price and ETA, and the flow stops before the
  final order action.
- Confirmation boundary: place the ride order or authorize payment.

### A2 Navigate to an attraction or workplace

- Prompt: `从这里去红场，比较步行、公共交通和打车。`
- Primary app: Yandex Maps.
- Expected: valid route choices are compared in Chinese and the selected route is ready to start.
- Confirmation boundary: start navigation only after the user selects a route.

### A3 Route from a photographed Russian address

- Input: a photo containing a Russian address, invitation, or business card.
- Primary app: Yandex Maps.
- Expected: the address is extracted, shown in Chinese and Russian for verification, then located
  and routed from the current position.
- Confirmation boundary: user confirms the extracted destination before navigation starts.

## Scenario B: accommodation

### B1 Find a hotel

- Prompt: `下周住三晚，每晚不超过 6000 卢布，离地铁近，评分高一点。`
- Primary app: the reviewed hotel-booking Android app.
- Expected: three to five comparable candidates contain price, rating, location, cancellation
  information when visible, and a clear recommendation rationale.
- Confirmation boundary: open checkout freely; booking, identity, and payment remain user actions.

### B2 Find a short-term or monthly rental

- Prompt: `找公司 40 分钟通勤以内的月租房，预算 <金额>，需要独立卫生间。`
- Primary app: Cian.
- Expected: filters are applied, viable listings are compared, and a shortlist is returned.
- Confirmation boundary: contacting the landlord, calling, or scheduling a viewing.

### B3 Understand and answer a landlord

- Input: a Russian landlord message, check-in instruction, or photographed agreement.
- Primary surface: ChebyAgent, with the existing messaging app opened only when sending is needed.
- Expected: deposits, utilities, dates, cancellation/termination terms, and unresolved questions are
  summarized in Chinese; a Russian reply is drafted.
- Confirmation boundary: send the message or place a call.

## Scenario C: dining

### C1 Find a nearby restaurant or cafe

- Prompt: `附近找一家现在营业、评分高的咖啡厅，步行不要超过 15 分钟。`
- Primary app: Yandex Maps.
- Expected: candidates include distance, rating, opening state, address, and a route action.
- Confirmation boundary: user chooses the destination before navigation starts.

### C2 Translate and select from a photographed menu

- Input: one to four clear photos of a Russian menu.
- Primary surface: ChebyAgent image input.
- Expected: dishes are grouped into bilingual cards with price, a plain Chinese description, and a
  useful image; selection supports quantity and an estimated total without inventing obscured
  information.
- Dish-image policy: prefer the menu's own dish image, then a real photo published by the restaurant
  or reviewed Android app. When neither exists, add a representative image clearly labelled
  `菜品示意图，不代表本店实际摆盘`. Never infer this restaurant's portion size, plating,
  ingredients, or availability from a supplemental image. If dish identity is uncertain, show a
  neutral placeholder rather than a potentially wrong food image.
- Confirmation boundary: generate the final order only after the user reviews selected items.

#### Required interaction flow

1. **Capture**: the user photographs or selects every relevant menu page and sends the images to
   ChebyAgent.
2. **Translate the complete menu**: ChebyAgent preserves the menu's category structure and visible
   item order, then presents every legible dish as a bilingual card. It must not reduce the menu to
   an Agent-selected shortlist. Unclear names, ingredients, or prices remain visible and explicitly
   marked as uncertain rather than being silently dropped or invented.
3. **Browse and choose**: the user scrolls through the translated menu, opens dish details, and taps
   cards to select or deselect dishes. Quantity changes update a local running total immediately.
   Recommendations may be offered as optional guidance but must never pre-empt the user's choice.
4. **Review while choosing**: an always-available `已选菜单` sheet contains only the selected
   dishes, their Russian and Chinese names, quantities, visible unit prices, estimated total, and
   any dietary constraints. The user can keep editing without leaving the translated menu.
5. **Confirm once and show the waiter**: tapping `确定点菜` once turns the current selection into a
   full-screen, high-contrast Russian-first order card that can be handed to the waiter. It includes
   dish names, quantities, and dietary notes, with Chinese retained for the user. This produces a
   presentation card only; it does not claim that the restaurant accepted an order or that payment
   occurred.

#### Interaction acceptance criteria

- The initial translated view contains the complete legible menu, not only recommended dishes.
- Dish selection and quantity changes do not require a new model turn and survive ordinary
  scrolling, detail expansion, and temporary app switching.
- A selected dish is visually unmistakable, and the running total is labelled as an estimate when
  any price is missing or uncertain.
- The user can edit until tapping `确定点菜` and cannot accidentally modify the menu while the
  waiter card is displayed.
- Returning from the waiter card restores the reviewed selection instead of starting over.

### C3 Communicate dietary constraints

- Prompt: `我不吃猪肉，对花生过敏，不要香菜。帮我检查刚才选的菜并给服务员看。`
- Primary surface: ChebyAgent bilingual presentation.
- Expected: uncertain dishes are clearly marked, unsafe choices are not silently accepted, and a
  full-screen Chinese/Russian waiter card states the constraints and selected order.
- Confirmation boundary: user approves the final wording; the Agent does not claim medical safety.

## Scenario D: daily services

### D1 Find and book a barber

- Prompt: `附近找一家今天能预约的理发店，评价好，价格别太高。`
- Primary app: Yandex Maps plus the booking or messaging surface exposed by the selected business.
- Expected: options are compared and the appointment details are prepared.
- Confirmation boundary: submit the booking, send a message, or place a call.

### D2 Find a pharmacy from a medicine box or prescription

- Input: a medicine box, written medicine name, or prescription photo.
- Primary app: Yandex Maps; ChebyAgent handles recognition and Russian phrasing.
- Expected: the visible medicine name is extracted, nearby pharmacies are found, and an explicit
  Russian availability/price question is prepared.
- Confirmation boundary: sending, calling, or purchasing. No diagnosis or substitution advice.

### D3 Prepare for a clinic visit

- Prompt: an explicit user-described need plus preferred distance and time.
- Primary app: Yandex Maps and the clinic's Android booking or communication surface when present.
- Expected: relevant clinics are compared and a bilingual factual description is prepared from the
  user's words without adding a diagnosis.
- Confirmation boundary: booking, sending medical information, identity, and payment.

## Anchor implementation order

1. C2 first: connect standalone image input to the local Codex app-server and return a readable
   bilingual menu result. This proves the no-external-app, multimodal path.
2. A1 second: preinstall Yandex Go from the Mac, fill a real destination, return price/ETA evidence,
   and stop before ordering. This proves the app-control and confirmation path.
3. Add the reusable comparison and bilingual-confirmation presentation needed by the other cases.
4. Execute the remaining ten cases as short real-device gates; long keepalive and corrupt-state
   testing remain outside the 1.0 gate.
