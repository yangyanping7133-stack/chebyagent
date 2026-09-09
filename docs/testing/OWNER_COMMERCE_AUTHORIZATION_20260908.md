# Owner-managed checkout consent

## Requested behavior

- More menu → 操作授权 → 付款、下单免逐次确认.
- Default off. The owner may enable standing checkout consent manually and revoke it at any time.
- The setting is local, survives ordinary APK updates, and is not a PhoneBridge setter.
- Search-only tasks remain search-only; this setting is not authorization to turn recommendations into purchases or reservations.

## Implemented behavior and boundaries

- Native, non-exported settings/approval Activity. Activation uses the Android biometric/device-credential prompt with a per-use hardware-backed signing operation.
- Persistent standing consent is a verified signature, not an editable boolean. Disabling removes its key, invalidating copied receipts.
- One-shot consent is bound to the observed screen/target/coordinates, expires after two minutes, and is consumed once. A changed screen invalidates it.
- Known checkout payments and order/booking commits request owner consent instead of being treated as completed actions. The r4 follow-up removes ordinary-action app-category, browser, financial-keyword, call/SMS, and unknown-target refusals. Missing accessibility semantics no longer prevents visually located taps.
- PhoneBridge mutation tools cannot operate the owner authorization or identity screen. System/third-party identity verification is unchanged.
- Deep tap-target lookup now reaches 32 levels with a 4096-node budget; incomplete traversal returns an observation failure instead of selecting an ancestor. The CIAN filter location control at (582, 1774) now opens the location page through PhoneBridge.
- Russian debt matching uses complete inflected words rather than matching `долг` inside `Надолго` / long-term search. This is a language-level fix, not a CIAN package allowlist.

## Current r4 acceptance on 2026-09-08

- Foreground offline build succeeded: `:phoneNode:testDebugUnitTest :appliance:assembleDebug`. Native tests: 68 passed, including actual JSON policy decisions for formerly blocked app launches, unlabeled taps, checkout pending/approved responses and human-only owner UI. Connector: 39 passed (one existing pytest configuration warning). Provider/startup contracts: 27 passed.
- Signed with the existing private update identity and installed using `adb install -r --no-incremental`: `Success`. APK: `artifacts/private/owner-commerce-20260908/owner-direct-r4-signed.apk`, SHA-256 `b9a1affcf5512075407c1ac3ce29257cd3c1ff5fa11a463ce679f635c9ec2088`. Credential scan: 854 members, zero candidates (`secret-scan-r4.json`). No data clearing or uninstall.
- Live PhoneBridge at 12:04:06 UTC reports `direct_with_checkout_consent`, `action_constraints_enabled=false`, `fund_protection_enabled=false`, `call_sms_allowed=true`, `browser_launch_allowed=true`. Checkout standing consent remains false and checkout confirmation required remains true. Accessibility enabled/bound and phone connected.
- Main UI restored the existing rental conversation and displayed `本机 Codex 已连接`. Its old refusal message is historical, not a new failure. Returned the app to the foreground after the native regression.
- Normal PhoneBridge path: CIAN launch succeeded (`cmd_mtsmg65l_5bcdb952fb`); city/location tap succeeded (`cmd_mtsmgh38_5c94a03dc2`); focused address search accepted `Санкт` (`cmd_mtsmgprs_a3ae532c60`). Cleared temporary input and returned to unchanged Санкт-Петербург search. Listing swipe succeeded with verified content displacement (`cmd_mtsmi21n_4201717af1`). Visual evidence: `cian-direct-r4-final.png`. This is control-path acceptance, not rental-shortlist acceptance.
- One initial swipe reached the newly appeared keyboard and added `7` to temporary search text; screenshot review caught it. Cleared the field, dismissed search, re-observed and verified actual listing scrolling. Preserved the initial screenshot as `cian-direct-r4.png`; do not use it as scrolling proof.
- Retention check: 111 baseline files, 111 current files, zero changed/missing (same bounded coverage as r3 below). No VPN change. No actual browser launch, bank interaction, call, message, booking, order or payment was made.
- Four active guest files were updated with `.before-owner-direct-20260908` per-file backups: base instructions, mobile extension, provider launcher and `local_mcp.py`. SHA-256 values match local sources. The now-reviewed base instruction digest is `9a9ca460506b513d03552a5ec4d5ffce576b7c9d8fcb19289092901c78f39b75`; launcher integrity validation remains enabled and corruption tests pass. Unlike r3's accidental mismatch, this is an explicit coordinated base-policy and pin revision. The optional Python broker module is not installed/used on this appliance; its source allowlist and tests were updated for URL parity.
- Native checkout recognition is semantic, not a universal payment detector. Unlabeled checkout controls must be recognized visually by the model and require conversational confirmation when standing consent is off; the mobile instruction extension explicitly enforces this. It is not an unconditional transaction-prevention guarantee.

## Earlier r3 acceptance on 2026-09-08

- Final foreground build: `:phoneNode:testDebugUnitTest :appliance:assembleDebug`, offline, successful in 2 seconds. Native tests: 62 passed, no failures/errors/skips. Connector suite: 35 passed (one warning); provider/startup contract suite: 27 passed. These are bounded automated checks, not hardware-authentication acceptance.
- Same-certificate, data-preserving update on managed ALN phone `YOUR_DEVICE_SERIAL`, package `com.termux`; final streamed installation returned `Success`. No uninstall/data clearing. Final APK: `artifacts/private/owner-commerce-20260908/owner-commerce-r3-signed.apk`; SHA-256 `d30d884db28ba5e6580e4c3acab875372c841c1bb39accbe10cb89d28e56e324`.
- APK member credential scan: 854 members, zero candidates. Report: `artifacts/private/owner-commerce-20260908/secret-scan-r3.json`; this is not a comprehensive security assessment.
- App launch restored the previous rental conversation and displayed `本机 Codex 已连接`. PhoneBridge reconnected after the final update; status at 11:37:26 UTC reported accessibility enabled/bound and `checkout_preapproved_by_owner=false`.
- More → 操作授权 opened normally. Native UI snapshot shows the switch is checkable and `checked=false`. The switch was never enabled by automation. Snapshot: `artifacts/private/owner-commerce-20260908/owner-settings.xml`.
- Retention comparison after both corrected APK updates: 111 baseline files, 111 current files, zero changed/missing. Coverage: Codex auth/config, local MCP data, installed skills and the selected preference directory. This is not a full phone backup or a claim about every file. VPN configuration was not modified.
- Installed base instructions, mobile extension and connector files exactly match local source hashes. Base instruction hash remains the launcher's pinned `33cc72f38c5120b61a907cdc538a085a959204a01b0b1696afbb153605b7c367`.
- Native CIAN regression: location page → city list → Санкт-Петербург → apply all succeeded. Search screen confirmed `г. Санкт-Петербург`. In the full filter screen, `Надолго` tap at (220, 766) and the previously failing deep location control at (582, 1774) both dispatched successfully through the normal PhoneBridge policy path; a fresh observation confirmed the location page. Command IDs: `cmd_mtslkkcm_3640a55ee6`, `cmd_mtslkkv3_11d76fd157`. No ADB gesture was used to bypass a CIAN refusal.
- Visual evidence: `artifacts/private/owner-commerce-20260908/cian-deep-filter-ready.png` and `cian-search-r3.png`. This proves navigation/filter recovery, not a completed rental shortlist.

## Failures found and corrected

- The first default-debug-signed update was rejected with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`; no data was cleared. Re-signed using the existing local private appliance identity and verified certificate continuity against the installed APK.
- New consent instructions were initially placed in the SHA-pinned base instructions, causing launcher configuration failure. Restored the exact pinned base and moved consent rules into the mobile developer-instruction extension. Added provider contract assertions and revalidated startup. Do not change/bypass the pinned checksum for extension instructions.
- Existing ready-runtime provisioning stages assets but does not automatically replace installed guest files. Applied the connector and instruction extension to the existing guest with per-file backups, and verified hashes. No credential/rootfs reset. General future overlay automation is outside this change.
- Live `Снять надолго` was incorrectly classified as debt by substring matching; added complete-word matching plus a regression test and shipped r3.

## Owner-operated acceptance remaining

- The owner must personally test enabling and revoking standing consent using the device's biometric/lock-screen prompt. Hardware-backed authentication has not been completed on the owner's behalf.
- No real payment, order, booking, call or message was submitted; single-use checkout UI and hardware-backed standing consent remain unverified end-to-end on a real checkout.
- r4 removes the earlier ordinary-action guards. Android and third-party identity/security requirements remain; the agent cannot operate its own authorization UI. Real checkout and owner activation/revocation acceptance remain owner-operated as described above.

## Continuation checkpoint

- Source identity: `codex/aln-russia-agent-1.0`, HEAD `16cc161`; unrelated pre-existing dirty changes remain untouched.
- Build, correction, installation and phone checks were completed in the foreground after the user explicitly declined detached process jobs. No new detached job was launched.
- `git diff --check` passes. Existing overlapping dirty changes are preserved; no whole-file staging/commit was used to absorb unrelated work.
