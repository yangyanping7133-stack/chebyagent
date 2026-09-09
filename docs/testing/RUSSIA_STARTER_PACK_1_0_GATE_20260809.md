# Russia Starter Pack 1.0 Anchor Gate

Date: 2026-08-09

Branch: `codex/russia-starter-pack-1.0`

Device: Huawei JUY-AL00 (`48FYD25327409846`)

Product baseline: `docs/product/RUSSIA_STARTER_PACK_1_0.md`

## Verdict

- C2 photographed-menu anchor: **PASS**. Both camera and gallery
  images reached the local Codex app-server as official `localImage` turn inputs. The first real
  menu run recognized 28 item-by-item reviewed dishes with matching visible prices. A second run
  used per-turn structured output to preserve the complete legible source order in 9 categories
  and 49 entries, including partially visible drink names whose missing prices were explicitly
  marked. The installed renderer displayed bilingual dish cards, representative-photo labels,
  selection and quantity controls, a selected-items review, a running total, and a Russian-first
  waiter card after one confirmation. A corrected manual audit confirmed that the source price for
  `Солянка мясная` is `890 ₽`, not `870 ₽`; both structured runs read it correctly, and the other 27
  priced left-page entries also matched the source.
- A1 airport-to-hotel taxi anchor: **PARTIAL / BLOCKED**. Yandex Go was installed from RuStore,
  opened in Chinese, accepted a pickup and destination, and stopped before ordering. Price and ETA
  were not obtained because the app returned `加载路线失败 / 请稍后再试` twice, including one
  explicit refresh. The evidence does not establish whether the cause is current network routing,
  the Yandex route service, or local app state.
- Remaining ten product cases: **NOT RUN**. This gate validates the two selected architecture
  anchors; it is not a claim that all twelve cases pass.

No taxi was ordered, no payment action was entered, and no message, booking, or call was submitted.

## Implemented product capability

- Camera or gallery selection in the ChebyCodex conversation UI.
- Full-size camera capture through an Android `FileProvider` URI.
- Bounded image decode, orientation correction, downscaling, JPEG re-encoding, and metadata removal.
- Private crash-recoverable image staging under the embedded runtime rootfs, with opaque file names,
  quotas, expiry cleanup, and completion cleanup.
- Local gateway support for text-plus-image and image-only Codex turns using `localImage`.
- Official per-turn structured output for complete browse/select collections without exposing JSON
  or Markdown syntax to the user.
- A business-agnostic selectable-collection rich block shared by local and remote gateways, with
  category order, bilingual labels, quantity, review, totals, and presentation mode.
- Lazy supplemental dish-image lookup with strict HTTPS host checks, bounded image decode, explicit
  `菜品示意图，不代表本店实际摆盘` labelling, and an honest placeholder when no relevant photo is found.
- A frozen app-first matrix covering mobility, accommodation, dining, and daily services, with three
  acceptance cases and explicit confirmation boundaries per scenario.

## Automated gate

| Check | Result |
| --- | --- |
| `:app:testDebugUnitTest` | PASS, 334 tests |
| `:app:compileGateAndroidTestKotlin` | PASS |
| `ImageAttachmentReaderInstrumentedTest` | PASS, 4 tests on JUY-AL00 |
| `:app:lintDebug` | PASS |
| `:appliance:assembleDebug` | PASS |
| `:appliance:lintDebug` | PASS |
| `tools/standalone/appliance_static_gate.py` | PASS |
| `git diff --check` | PASS |

Instrumented coverage includes an actual trusted-camera URI whose rotated input is re-encoded and
whose EXIF/GPS metadata is absent from the attachment handed to the gateway.

## Installable artifact

- Path: `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`
- Package: `com.termux`
- Launcher: `com.cheby.codex.mobile.MainActivity`
- Size: `285688791` bytes
- SHA-256: `44e86fd27c7ee8879d15d5979544b140843763495378f77b5ce7b005935bef85`

This is a debug engineering artifact on a feature branch, not a signed public release.

## Real-device evidence

### C2 image path

Prompt: `Describe this image in one short sentence. If it is not a menu, say so.`

Observed reply: `This is not a menu; it is an almost entirely black image.`

The physical capture happened to be dark; the purpose of this anchor was to prove that the image,
not only placeholder text, crossed the local end-to-end path. The UI showed one selected image and
the projected user turn included `[图片 × 1]`.

- Screenshot: `artifacts/private/evidence/russia-starter-pack-20260809/cheby-local-image-response.png`
- Screenshot SHA-256: `4bc4316d2be8b5c056821e6e3428614cc7203d81bae5d145efd4d412e519edbb`
- UI tree SHA-256: `cf3a46da189a2a28d354bca7ad487fa2aaafe0c4c1925a59777e4c556a77fa9f`

The test phone did not expose a system `ACTION_IMAGE_CAPTURE` handler, so Open Camera 1.56.2 was
installed from F-Droid for this gate. That dependency remains a delivery gap for phones without a
camera handler; a built-in camera surface is not part of this change.

### C2 realistic Russian menu retest (pre-interactive baseline)

Input source: a photographed Russian menu published on Restoclub's page for the Veter restaurant
in Saint Petersburg: `https://www.restoclub.ru/spb/place/veter-1`. It visibly contains soups, hot
dishes, cutlets, sides, desserts, and a partial drinks page. The source image was downloaded on the
Mac, copied to the phone's Pictures directory, and selected through the normal Android gallery
picker. No camera application was used.

Prompt: `Analyze the attached Russian menu. Reply in Chinese. Extract all clearly visible dishes
and prices. Group them by category. For each item include Russian name, Chinese translation, ruble
price, and short explanation. Mark uncertain text. Recommend a three dish order for one person and
calculate total. End with one concise Russian order sentence for the waiter. Do not invent text.`

Observed content result:

- Extracted 4 soups, 13 hot dishes, 3 cutlets, 3 sides, and 5 desserts: 28 items total.
- The extracted Russian names and 28 visible prices matched the source image during manual review.
- It explicitly omitted the partially visible drinks page because those prices were outside the
  image instead of inventing values.
- Recommended seafood soup `790 ₽`, beef cheeks `850 ₽`, and Pavlova `480 ₽`.
- Calculated the total correctly: `790 + 850 + 480 = 2,120 ₽`.
- Produced: `Пожалуйста, суп с морепродуктами, говяжьи щёчки и Павлову с ягодами.`

Observed presentation result at that checkpoint:

- Chinese and Cyrillic text, headings, bullets, bold text, and totals are readable.
- Markdown table syntax is not rendered as a table. Pipes and alignment markers are exposed as
  wrapped text, producing a very long result that is difficult to scan on a phone.
- There are no per-dish images, image-source labels, dish-selection controls, quantity steppers,
  running total, or final waiter-card confirmation. Therefore the C2 recognition/translation layer
  passes, while the intended product interaction remains partial.
- The current result also leads with a model-generated three-dish recommendation. The target flow
  must instead present the complete translated menu first, preserve user choice, and turn only the
  user's confirmed selection into the waiter-facing order card.

- Input: `artifacts/private/evidence/russia-starter-pack-20260809-menu/menu-source-restoclub.jpg`
- Input SHA-256: `6a0d978701385d0783df6c2581314f09c71ba48b417136f8aaf0d60cb52e42fc`
- Middle screenshot SHA-256: `586f5ce4a1668a927fb5a8f8c099303d6f8ba23538d202265f9b5fbd714c2650`
- Final screenshot SHA-256: `85a6574a6a0db88e6ad092423ffc87498147fd38f583e0b247c4f04385414cd7`

### C2 interactive menu closed loop

The same gallery image was submitted in a new Thread after the generic selectable-collection block
and official app-server per-turn `outputSchema` projection were installed.

Prompt: `I cannot read this Russian menu. Show me the complete translated menu and let me choose
dishes.`

Observed result:

- Codex returned a structured complete collection rather than raw JSON or a Markdown table.
- The renderer showed 9 categories and 49 source-ordered entries. It retained legible names from
  the partial drinks page and marked unavailable prices instead of inventing them.
- Russian name, Chinese translation, explanation, visible ruble price, and uncertainty state were
  separately rendered on each card.
- Two visible cards obtained supplemental reference photos. They were explicitly labelled
  `菜品示意图，不代表本店实际摆盘`; unmatched cards remained `图片待补充`.
- The test selected `Солянка мясная`, increased it to two portions, opened the selected-items view,
  and observed a total of `1780 ₽`.
- One tap on `确定点餐` changed the full screen into a Russian-first card showing `Ваш заказ`,
  `2× Солянка мясная`, `Итого 1780 ₽`, and a Russian waiter note. No second confirmation was added.
- `返回修改` remained available and the selection stayed client-owned; selection and quantity taps
  did not create new Codex Turns or invoke a Gateway side effect.
- Installing the rebuilt APK with `adb install -r` preserved the embedded runtime, Codex login,
  session history, and the structured result across process restart.

Content accuracy retest:

- The first structured run read `Солянка мясная` as `890 ₽`; a corrected manual review confirmed
  that the source crop also shows `890 ₽`. The other 27 priced left-page entries matched the source.
- The schema was tightened to require English image queries, exact visible punctuation, same-row
  price alignment, and a second digit check. A new empty Thread was then created from the drawer
  and the same image was submitted again with the same constraints in the user prompt.
- The clean rerun again returned the correct `890 ₽`. Its prose summary said 29 priced left-page
  entries although the five priced categories contain 28; this is a summary-count inconsistency,
  not an item-price or renderer defect.
- English image queries improved supplemental-photo retrieval.
- Source evidence, uncertainty labels, and a cheap correction path remain useful hardening, but this
  case no longer provides evidence of a price-recognition failure.

UX findings from the repeated run:

- After a very long collection finishes, the conversation does not reliably reposition the user at
  the new result; the tester had to scroll manually before opening the collection.
- The top-bar new-Thread action did not provide enough visible feedback to prove that an empty
  Thread had been created. The drawer's `新建会话` action did create a verifiably empty Thread.

Evidence:

- Full collection screenshot SHA-256:
  `2f8713a4a56ead9c36a6cbe7dcd4bc7da2b68716ba25641e220dc2ce479edb59`
- Selected-items screenshot SHA-256:
  `4cb15e7bc91b44e9eb5311a754d5ef04b2b5df01977196a7e8a4af770c6e299f`
- Waiter-card screenshot SHA-256:
  `718ea56e31342a99c3018822e634f71ad350b3e60ed5d0938845ee9a08537339`
- Private evidence directory:
  `artifacts/private/evidence/russia-starter-pack-20260809-interactive-menu/`
- Clean-rerun collection screenshot SHA-256:
  `41f49a8dd97b6a2f572ba367baa029b645d839427d77d7780745cdff366c53f7`
- Source price crop SHA-256:
  `8ae958a0dc3b227b8a8e832d9f3bfbe7e6bf20761b26cc0f2123583eb4ffae45`
- Exact installed-artifact waiter-card retest SHA-256:
  `2ecb68ae9273cb42344174b40314b146623a1aa32ed48ed2516137e6298c369c`

### A1 app-control path

- RuStore 1.107.0.3 was installed from the official RuStore installer.
- Yandex Go (`ru.yandex.taxi`) was then installed through RuStore's Android package flow.
- Installed-app-list permission was denied.
- Location was allowed only while Yandex Go was in use.
- Pickup: Pulkovo Airport arrivals/lower level.
- Destination: Goncharnaya 1, Saint Petersburg.
- The order action was not pressed.

- Screenshot: `artifacts/private/evidence/russia-starter-pack-20260809/yandex-go-route-failed.png`
- Screenshot SHA-256: `d987cc0f1bac89cdbf105617da15c66dbfbb7b194406b9b270ab4bd82352ca3f`
- UI tree SHA-256: `ed091c315cf21151dd8bffb6b1d92643a105842b20d6b8b95d82098c1054c84f`

The route-loading failure should be diagnosed separately with read-only network and app evidence.
VPN configuration remains user-owned and was not changed by this gate.

## Next product slice

1. Diagnose the Yandex Go route-load failure without changing the user's VPN or Mac networking;
   then rerun A1 to the price/ETA checkpoint.
2. Fix long-result positioning and make new-Thread creation visibly unambiguous, then include both
   in the UI regression gate.
3. Extend the same business-agnostic rich-message vocabulary with comparison, bilingual notice,
   table, and image-gallery blocks as the next real scenarios require them.
4. Execute one short real-device case from each remaining scenario before expanding to all twelve.
5. Add source-aware price evidence and correction as defensive hardening for genuinely uncertain
   or missing prices; it is not a blocker established by this menu case.

Long keepalive tests, corrupt-state recovery, standalone/distributed consolidation, automatic app
installation, payment, and unattended final actions remain outside this gate.
