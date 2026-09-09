# Rich Message V1

Status: frozen for the first vertical slice
Date: 2026-07-19

## Design Intent

The visual signature is one task card that evolves in place while work moves across Codex, tools, files, websites, and phone apps. The interface emphasizes semantic state changes, evidence, and outcome instead of decorative animation or raw logs.

## Envelope

```json
{
  "schema": "cheby.rich-message/1.0",
  "messageId": "msg_public_id",
  "threadId": "thr_public_id",
  "turnId": "turn_public_id",
  "sourceItemId": "item_public_id",
  "role": "assistant",
  "state": "streaming",
  "revision": 7,
  "rootBlockIds": ["status", "steps", "evidence"],
  "blocks": {},
  "fallback": {"text": "任务正在执行"},
  "createdAt": "2026-07-19T00:00:00Z",
  "updatedAt": "2026-07-19T00:01:00Z"
}
```

Message states: `queued`, `streaming`, `waitingInput`, `completed`, `failed`, `interrupted`, `cancelled`.

Message roles are `assistant` and `user`. User messages use the same envelope so a
reloaded Codex Thread preserves the complete conversation and the original
`clientMessageId`/Turn association; user messages normally render only `text`
and attachment blocks.

Blocks are stored by stable `blockId`; `rootBlockIds` controls ordering. This avoids fragile array-index patching.

## Block Set

P0 native blocks:

- `text`: plain text or safe Markdown
- `status`: label, detail, tone, progress
- `steps`: stable step IDs with pending/running/completed/failed state
- `metrics`: label/value/unit/trend
- `facts`: label/value/note
- `media`: opaque asset references and captions
- `source`: label, validated URI reference, capture time, trust label
- `relay`: execution nodes, edges, and active node
- `notice`: neutral/info/success/warning/danger result
- `actions`: approve, reject, retry, open, or submit actions

Safe fallback in P0:

- `table`, `code`, `terminal`, `diff`, `file`, `form`, `chart`, `location`, `extension`

Every unknown or unsupported block must provide `fallbackText`. Unknown message schema major versions render `fallback.text` only.

## Safety Constraints

- No server-provided colors, fonts, CSS, HTML, JavaScript, WebView, or executable UI.
- `tone` is limited to `neutral`, `info`, `success`, `warning`, `danger`.
- Extension renderer IDs are compiled into an App allowlist.
- Groups may nest at most two levels.
- Remote URI, file, media, and action targets are resolved through Gateway validation.
- Terminal, diff, and tool detail surfaces redact credentials and are collapsed by default.

## Signature Motion

The execution relay is the only prominent animation:

```text
● 理解
│
● 调用工具
│
● 获得证据
│
○ 生成结果
```

- Active segment: signal blue.
- Completed segment: green.
- Failed node: red.
- One light point moves to the next node only on a real semantic transition.
- Duration: 260–420 ms, never looping.
- Restored history is static.
- Reduced Motion changes state without animation.

## Codex R0 Text Projection

One Codex Turn projects to zero or more `user` text bubbles and exactly one
`assistant` LiveWorkPanel. The panel uses stable `status`, `activity`, `metrics`,
and `answer` block IDs; command, file-change, tool, and unknown Items are reduced
to those safe blocks rather than exposing raw terminal, diff, tool payload, path,
or app-server identifiers.

Codex 0.145 `agentMessage.phase` is interpreted as follows:

- `commentary` contributes to `activity`.
- `final_answer` contributes to `answer`.
- `null` remains activity while a Turn is live. When a Turn is terminal and has
  no explicit final answer, its last null-phase agent message is promoted to
  `answer` for compatibility with providers that omit phase.

The first live projection emits its current state as one `message.snapshot`;
there is no empty-snapshot/instant-patch pair. Raw `item/started` text and
`agentMessage/delta` fragments never cross or enter the durable public stream,
because a credential can span fragments. Only a complete authoritative
`item/completed` body contributes text after whole-item redaction. Later
authoritative Items and Turn completion emit contiguous `message.patch`
revisions. Turn completion prunes synthetic or stale projection Items against
its authoritative Item set before that final patch. Repeated authoritative
upstream payloads verify materialization and are semantic no-ops once the panel
is current.

Within a Turn, original user Item ordinals sort before one assistant panel in
both live storage and restored history. Each public message is capped by its
serialized JSON size at 256 KiB. If two legal 64 KiB text blocks plus duplicated
fallbacks exceed that envelope, projection deterministically compacts fallbacks
and then activity before final-answer text; metrics and readable fallback remain,
and reduced text is marked `truncated`.

A restored `thread/read` uses the same projection fold without emitting
historical replay events.

## Visual Tokens

| Token | Value |
| --- | --- |
| Ink | `#1F2329` |
| Canvas | `#F5F6F8` |
| Paper | `#FFFFFF` |
| Signal Blue | `#3370FF` |
| Complete Green | `#00A870` |
| Warning Amber | `#D97706` |
| Risk Red | `#F54A45` |

- Spacing: 4/8/12/16/24/32 dp.
- Message corner: 16 dp; card: 12 dp; label: 8 dp.
- Touch targets: at least 48×48 dp.
- Body: 16 sp; support: 13–14 sp; title: 20 sp; key metric: 28–32 sp.

## Mobile Layout Rules

- Fixed ChebyAgent identity in the header; current Thread title is secondary.
- Thread title opens the session drawer.
- Cards stack vertically on 360–430 dp screens.
- Media is a cropped preview and opens full screen.
- Terminal, diff, and large tables are collapsed by default.
- Auto-scroll follows deltas only when the user was already at the bottom.
- Keyboard must not cover composer controls.
- Large font size reflows metrics from three columns to two or one.

## Required Fixtures

Keep fixtures business-independent:

1. Software delivery: plan, command, diff, tests.
2. Data research: sources, facts, table, metrics.
3. General assistance: relay, media, approval, final outcome.

Required failure fixtures include duplicate event, sequence gap, revision conflict, missing media, unknown block, expired approval, reconnect, and non-retryable error.
