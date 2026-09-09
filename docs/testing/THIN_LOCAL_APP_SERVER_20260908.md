# Thin local app-server delivery — 2026-09-08

## Scope

Local Android input uses official Codex app-server thread/resume, thread/read,
turn/start and turn/interrupt. Remote/relay execution remains on its existing path.
No service protocol was added. Existing credentials, chats, memory, skills and
provider settings are neither cleared nor migrated by this change.

## Implemented

- Dedicated local submission function bypasses the legacy dispatcher state machine.
  An official receipt completes local delivery, not the model task.
- Local mode does not drain historical queued entries, reconcile delivery in the
  background, wait for previous turns, or replay unconfirmed requests on reconnect.
- Historical local records remain read-only in the UI. A newly observed official
  busy rejection permits an explicit retry; unknown delivery does not.
- Distinct feedback for sending, official rejection/busy, disconnected delivery,
  and unconfirmed delivery. Request timeouts leave the connection intact.
- Opening a conversation resumes the official subscription then reads its history
  and current status. Active turn identity comes from upstream, enabling stop after
  restoration. Stop acknowledgement is not displayed as task completion.
- Removed the obsolete stall-reconnect parameter and local successful-submission
  cache. Quiet-time notification is presentation only, never a service restart.
- Catalog display merges preserve newer official events received during a read;
  catalog refresh no longer reconciles the local execution map.
- Saved model and effort are explicitly passed with each turn. User-selected
  medium effort is not overwritten. No real-device claim is made for this build.
- Blue user bubbles, collapsible process details, rich results, Linux startup,
  phone capabilities and encrypted settings remain in place.

## Validation

Final build/test and signed APK details are recorded in
`artifacts/private/owner-commerce-20260908/thin-client-r7-acceptance.md`.

Regression coverage includes old uncertain delivery not blocking new input,
no reconnect replay, acknowledgement finishing delivery, local image receipt,
explicit retry after busy rejection, exact stop target, active-session restoration,
per-turn low-to-medium setting change and an actual 30-second request timeout.
Remote/relay regressions are included in the full standalone unit suite.

During implementation, the suite exposed a catalog/event display race; its merge
now uses atomic UI updates. One intermediate relay test also timed out; later full
runs passed. Unit tests use a mock official endpoint, not a live phone/model.

## Remaining device acceptance

Phone is offline. On reconnection: verify existing phone VPN without changing Mac
VPN, cover-install using the existing signing identity without clearing data,
check accessibility, send one real request with medium effort, inspect process
events and response, then exit/re-enter the same conversation. Verify persisted
login/history/memory/skills. This device check remains pending.

## Limitations

This is still an Android/runtime/phone adapter, not literally UI-only code: RPC
transport, image files, credentials, rendering and legacy remote compatibility
remain necessary. A delivery timeout cannot prove whether a task started; users
must inspect the restored official conversation before submitting again. Matching
signatures enable an in-place update but do not replace actual device acceptance.
