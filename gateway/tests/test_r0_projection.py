from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from cheby_gateway.bridge import BridgeEvent, StdioCodexBridge
from cheby_gateway.projection import (
    MAX_PUBLIC_MESSAGE_BYTES,
    ProjectionItem,
    project_live_work_panel,
    serialized_public_message_bytes,
)
from cheby_gateway.service import GatewayService

from .helpers import make_context, pair


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "v1"
    / "projection"
    / "live_work_panel_v0_145.json"
)


class R0ProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context(event_retention=2000)
        await self.context.service.start()
        _, self.device = pair(self.context)

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.context.close()

    def test_real_0145_fixture_retains_phase_time_ordinal_and_projects_one_panel(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw = StdioCodexBridge._parse_thread(fixture["thread"])
        turn = raw.turns[0]
        self.assertEqual("2026-07-19T00:00:01Z", turn.started_at)
        self.assertEqual("2026-07-19T00:00:08Z", turn.completed_at)
        self.assertEqual(list(range(6)), [item.ordinal for item in turn.items])
        self.assertEqual("commentary", turn.items[2].phase)
        self.assertEqual("final_answer", turn.items[-1].phase)
        self.assertEqual(240, turn.items[3].duration_ms)
        self.assertEqual(0, turn.items[3].exit_code)
        self.assertEqual(1, turn.items[4].change_count)

    async def test_shared_fixture_projects_multiple_users_and_one_live_work_panel(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        thread = await self.context.service.create_thread(self.device, "Fixture")
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        source = copy.deepcopy(fixture["thread"])
        source["id"] = raw_thread_id
        source["turns"][0]["items"][0]["createdAt"] = 1784419207
        source["turns"][0]["items"][1]["createdAt"] = 1784419206
        self.context.bridge.threads[raw_thread_id] = source

        detail = await self.context.service.read_thread(thread.id, self.device)
        expected = fixture["expected"]
        self.assertEqual(expected["roles"], [message["role"] for message in detail.messages])
        users = [message for message in detail.messages if message["role"] == "user"]
        self.assertEqual(2, len({message["clientMessageId"] for message in users}))
        assistants = [message for message in detail.messages if message["role"] == "assistant"]
        self.assertEqual(1, len(assistants))
        panel = assistants[0]
        self.assertEqual(["status", "activity"], panel["rootBlockIds"][:2])
        self.assertEqual(["metrics", "answer"], panel["rootBlockIds"][-2:])
        typed = [
            panel["blocks"][block_id]["type"]
            for block_id in panel["rootBlockIds"]
            if block_id.startswith("evidence-")
        ]
        self.assertEqual(["tool", "diff"], typed)
        self.assertEqual(expected["activity"], panel["blocks"]["activity"]["text"])
        self.assertEqual(expected["answer"], panel["blocks"]["answer"]["text"])
        metrics = {
            item["label"]: item["value"] for item in panel["blocks"]["metrics"]["items"]
        }
        self.assertEqual(expected["metrics"], metrics)
        public_json = json.dumps(detail.model_dump(by_alias=True), ensure_ascii=False)
        self.assertNotIn(source["turns"][0]["id"], public_json)
        for item in source["turns"][0]["items"]:
            self.assertNotIn(item["id"], public_json)
        self.assertNotIn("/workspace", public_json)

    async def test_first_live_projection_is_current_snapshot_and_user_sorts_first(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Live order")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-live-order",
            [{"type": "text", "text": "Start"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": "019c0044-0044-7000-8000-000000000001",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "status": "completed",
                        "text": "Current projection",
                    },
                },
            )
        )
        message_events = [
            event
            for event in self.context.store.replay_events(
                self.device.id, self.device.stream_id, 0
            ).events
            if event.turn_id == turn.id and event.type.startswith("message.")
        ]
        self.assertEqual(["message.snapshot"], [event.type for event in message_events])
        self.assertEqual(
            "Current projection",
            message_events[0].payload["blocks"]["activity"]["text"],
        )
        snapshots = self.context.store.list_message_snapshots(thread.id)
        self.assertEqual(["user", "assistant"], [value["role"] for value in snapshots])
        rows = self.context.store._conn.execute(
            """
            SELECT role, sort_order FROM message_snapshots
            WHERE turn_public_id = ? ORDER BY sort_order ASC
            """,
            (turn.id,),
        ).fetchall()
        self.assertEqual(
            [("user", 0), ("assistant", 2_000_000_000)],
            [(row["role"], row["sort_order"]) for row in rows],
        )

    async def test_authoritative_replay_repairs_crash_after_projection_ledger_write(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Repair ledger")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-repair-ledger",
            [{"type": "text", "text": "Repair"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        event = BridgeEvent(
            method="item/completed",
            params={
                "threadId": raw_thread_id,
                "turnId": raw_turn_id,
                "item": {
                    "id": "019c0055-0055-7000-8000-000000000001",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "status": "completed",
                    "text": "Replay-safe authoritative text",
                },
            },
        )
        original_sync = self.context.service._sync_live_work_panel
        failed = False

        def fail_once(**kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("fault after ledger commit")
            return original_sync(**kwargs)

        self.context.service._sync_live_work_panel = fail_once
        with self.assertRaisesRegex(RuntimeError, "fault after ledger commit"):
            await self.context.service.handle_bridge_event(event)
        self.context.service._sync_live_work_panel = original_sync
        self.assertTrue(self.context.store.list_projection_items(turn.id))
        self.assertEqual(
            ["user"],
            [message["role"] for message in self.context.store.list_message_snapshots(thread.id)],
        )

        await self.context.service.handle_bridge_event(event)
        snapshots = self.context.store.list_message_snapshots(thread.id)
        self.assertEqual(["user", "assistant"], [message["role"] for message in snapshots])
        self.assertEqual(
            "Replay-safe authoritative text",
            snapshots[-1]["blocks"]["activity"]["text"],
        )

    async def test_delta_fragments_never_enter_outbox_and_authoritative_text_is_safe(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Secret fragments")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-secret-fragments",
            [{"type": "text", "text": "Inspect safely"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        raw_uuid = "019c0066-0066-7000-8000-000000000001"
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/started",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": raw_uuid,
                        "type": "agentMessage",
                        "status": "inProgress",
                        "text": "Bearer started-secret",
                    },
                },
            )
        )
        for fragment in ("Bearer cross-", "delta-secret token=split-", "secret"):
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="item/agentMessage/delta",
                    params={
                        "threadId": raw_thread_id,
                        "turnId": raw_turn_id,
                        "itemId": raw_uuid,
                        "delta": fragment,
                    },
                )
            )
        before_completed = json.dumps(
            [
                event.model_dump(by_alias=True, exclude_none=True, mode="json")
                for event in self.context.store.replay_events(
                    self.device.id, self.device.stream_id, 0
                ).events
            ],
            ensure_ascii=False,
        )
        for forbidden in ("started-secret", "cross-", "delta-secret", "split-secret"):
            self.assertNotIn(forbidden, before_completed)

        authoritative = (
            "Bearer cross-delta-secret token=split-secret "
            + raw_uuid
            + " source https://docs.example.com/reference?q=codex "
            + "/Users/Jane Doe/private file.txt"
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": raw_uuid,
                        "type": "agentMessage",
                        "phase": "commentary",
                        "status": "completed",
                        "text": authoritative,
                    },
                },
            )
        )
        public_history = json.dumps(
            {
                "events": [
                    event.model_dump(by_alias=True, exclude_none=True, mode="json")
                    for event in self.context.store.replay_events(
                        self.device.id, self.device.stream_id, 0
                    ).events
                ],
                "messages": self.context.store.list_message_snapshots(thread.id),
                "durableOutbox": [
                    row["payload_json"]
                    for row in self.context.store._conn.execute(
                        "SELECT payload_json FROM event_outbox ORDER BY seq ASC"
                    ).fetchall()
                ],
            },
            ensure_ascii=False,
        )
        for forbidden in (
            "cross-delta-secret",
            "split-secret",
            raw_uuid,
            "/Users/Jane Doe/private file.txt",
        ):
            self.assertNotIn(forbidden, public_history)
        self.assertIn("https://docs.example.com/reference?q=codex", public_history)
        self.assertIn("[INTERNAL_ID]", public_history)
        self.assertIn("[PATH]", public_history)

    def test_generic_posix_paths_with_spaces_are_fully_redacted(self) -> None:
        examples = (
            "/mnt/My Project/file.txt",
            "/usr/local/My Project/file.txt",
            "/Library/Application Support/app/config.json",
            "/app/runtime data/result.log",
            "/data/customer exports/final.csv",
        )
        for path in examples:
            redacted = self.context.service._redact_sensitive(
                "Before %s, after." % path
            )
            self.assertEqual("Before [PATH], after.", redacted)
            for fragment in path.split():
                self.assertNotIn(fragment, redacted)

    def test_path_tokenizer_preserves_urls_punctuation_and_ordinary_slashes(self) -> None:
        source = (
            "First /mnt/My Project/file.txt; "
            "source https://docs.example.com/guides/agent?q=codex, "
            "second /Library/Application Support/app/config.json! "
            "third /data/cache directory; ratios 1/2 and/or docs/readme stay."
        )
        redacted = self.context.service._redact_sensitive(source)
        self.assertEqual(
            "First [PATH]; "
            "source https://docs.example.com/guides/agent?q=codex, "
            "second [PATH]! third [PATH]; "
            "ratios 1/2 and/or docs/readme stay.",
            redacted,
        )
        for forbidden in (
            "/mnt/My Project/file.txt",
            "/Library/Application Support/app/config.json",
            "/data/cache directory",
        ):
            self.assertNotIn(forbidden, redacted)

    async def test_turn_completion_prunes_synthetic_plan_via_public_patch(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Plan prune")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-plan-prune",
            [{"type": "text", "text": "Finish"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/plan/updated",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "plan": [{"id": "one", "step": "One", "status": "pending"}],
                },
            )
        )
        final_item = {
            "id": "019c0077-0077-7000-8000-000000000001",
            "type": "agentMessage",
            "phase": "final_answer",
            "status": "completed",
            "text": "Finished safely.",
        }
        cursor_before = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        ).events[-1].seq
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": raw_turn_id,
                        "status": "completed",
                        "completedAt": 1784419208,
                        "items": [final_item],
                    },
                },
            )
        )
        replay = self.context.store.replay_events(
            self.device.id, self.device.stream_id, cursor_before
        )
        self.assertIn("message.patch", [event.type for event in replay.events])
        ledger = self.context.store.list_projection_items(turn.id)
        self.assertEqual(["agentMessage"], [row["item_type"] for row in ledger])
        live = next(
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message["role"] == "assistant"
        )
        revision_before_get = live["revision"]
        self.context.bridge.turns[raw_turn_id]["status"] = "completed"
        self.context.bridge.turns[raw_turn_id]["completedAt"] = 1784419208
        self.context.bridge.turns[raw_turn_id]["items"] = [final_item]
        self.context.bridge.threads[raw_thread_id]["status"] = "idle"
        detail = await self.context.service.read_thread(thread.id, self.device)
        historical = next(message for message in detail.messages if message["role"] == "assistant")
        self.assertEqual(revision_before_get, historical["revision"])
        self.assertEqual(live["blocks"], historical["blocks"])

    async def test_empty_terminal_item_list_preserves_live_final_answer(self) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Empty terminal items",
        )
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-empty-terminal-items",
            [{"type": "text", "text": "hi"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        final_item = {
            "id": "raw-agent-empty-terminal-items",
            "type": "agentMessage",
            "phase": "final_answer",
            "status": "completed",
            "text": "Hi! What would you like to work on?",
        }
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": final_item,
                },
            )
        )

        before_completion = next(
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message["role"] == "assistant"
        )
        self.assertEqual(
            final_item["text"],
            before_completion["blocks"]["answer"]["text"],
        )
        cursor_before_completion = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        ).events[-1].seq

        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": raw_turn_id,
                        "status": "completed",
                        "completedAt": 1784419208,
                        "items": [],
                    },
                },
            )
        )

        completed = next(
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message["role"] == "assistant"
        )
        self.assertEqual("completed", completed["state"])
        self.assertEqual(final_item["text"], completed["blocks"]["answer"]["text"])
        self.assertEqual(
            ["agentMessage"],
            [
                row["item_type"]
                for row in self.context.store.list_projection_items(turn.id)
            ],
        )
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            cursor_before_completion,
        )
        terminal_patch = next(
            event for event in replay.events if event.type == "message.patch"
        )
        self.assertNotIn(
            "answer",
            [
                operation.get("blockId")
                for operation in terminal_patch.payload["ops"]
                if operation.get("op") == "block.remove"
            ],
        )

    async def test_commentary_storm_snapshot_then_patches_and_history_equivalence(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Storm")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-commentary-storm",
            [{"type": "text", "text": "Run the task"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        completed_items = []
        for index in range(111):
            item = {
                "id": "019c0022-0022-7000-8000-%012d" % index,
                "type": "agentMessage",
                "text": "Progress update %03d." % index,
                "phase": "commentary",
            }
            completed_items.append(item)
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="item/completed",
                    params={
                        "threadId": raw_thread_id,
                        "turnId": raw_turn_id,
                        "item": {**item, "status": "completed"},
                    },
                )
            )
        final_item = {
            "id": "019c0022-0022-7000-8000-999999999999",
            "type": "agentMessage",
            "text": "The task is complete.",
            "phase": "final_answer",
        }
        completed_items.append(final_item)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {**final_item, "status": "completed"},
                },
            )
        )
        raw_turn = self.context.bridge.turns[raw_turn_id]
        raw_turn["status"] = "completed"
        raw_turn["completedAt"] = 1784419208
        raw_turn["items"] = raw_turn["items"] + completed_items
        self.context.bridge.threads[raw_thread_id]["status"] = "idle"
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": raw_turn_id,
                        "status": "completed",
                        "completedAt": 1784419208,
                        "items": raw_turn["items"],
                    },
                },
            )
        )

        snapshots = [
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message["role"] == "assistant"
        ]
        self.assertEqual(1, len(snapshots))
        live = snapshots[0]
        self.assertEqual("completed", live["state"])
        self.assertEqual("The task is complete.", live["blocks"]["answer"]["text"])
        replay = self.context.store.replay_events(self.device.id, self.device.stream_id, 0)
        panel_snapshots = [event for event in replay.events if event.type == "message.snapshot"]
        panel_patches = [event for event in replay.events if event.type == "message.patch"]
        self.assertEqual(1, len(panel_snapshots))
        self.assertTrue(panel_patches)
        self.assertEqual(
            list(range(len(panel_patches))),
            [event.payload["baseRevision"] for event in panel_patches],
        )
        revision_before_duplicate = live["revision"]
        patches_before_duplicate = len(panel_patches)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {**final_item, "status": "completed"},
                },
            )
        )
        after_duplicate = self.context.store.message_snapshot(live["messageId"])
        self.assertEqual(revision_before_duplicate, after_duplicate["revision"])
        replay_after = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        )
        self.assertEqual(
            patches_before_duplicate,
            len([event for event in replay_after.events if event.type == "message.patch"]),
        )

        before_history = {
            key: value
            for key, value in after_duplicate.items()
            if key not in {"revision", "updatedAt"}
        }
        detail = await self.context.service.read_thread(thread.id, self.device)
        historical = next(message for message in detail.messages if message["role"] == "assistant")
        after_history = {
            key: value
            for key, value in historical.items()
            if key not in {"revision", "updatedAt"}
        }
        self.assertEqual(before_history, after_history)

    async def test_null_phase_promotes_last_message_and_redacts_unsafe_content(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Compatibility")
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        unsafe = (
            "secret=visible Bearer visible sk-abcdefghijklmnop "
            "/Users/example/private/file.txt raw-turn-private\x01"
        )
        self.context.bridge.threads[raw_thread_id]["turns"] = [
            {
                "id": "019c0033-0033-7000-8000-000000000001",
                "status": "completed",
                "startedAt": 1784419201,
                "completedAt": 1784419202,
                "items": [
                    {
                        "id": "019c0033-0033-7000-8000-000000000002",
                        "type": "unknownFutureItem",
                        "status": "completed",
                    },
                    {
                        "id": "019c0033-0033-7000-8000-000000000003",
                        "type": "agentMessage",
                        "text": unsafe,
                        "phase": None,
                    },
                ],
            }
        ]
        detail = await self.context.service.read_thread(thread.id, self.device)
        panel = next(message for message in detail.messages if message["role"] == "assistant")
        self.assertIn("answer", panel["blocks"])
        self.assertEqual("Codex activity updated.", panel["blocks"]["activity"]["text"])
        public = json.dumps(panel, ensure_ascii=False)
        for forbidden in (
            "visible",
            "sk-abcdefghijklmnop",
            "/Users/example",
            "raw-turn-private",
            "unknownFutureItem",
            "\x01",
        ):
            self.assertNotIn(forbidden, public)
        self.assertIn("[REDACTED]", public)
        self.assertIn("[PATH]", public)
        self.assertIn("[INTERNAL_ID]", public)

    async def test_structured_tool_diff_and_test_evidence_is_typed_and_sanitized(
        self,
    ) -> None:
        self.context.bridge.auto_events = False
        thread = await self.context.service.create_thread(
            self.device,
            "Structured evidence",
        )
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-structured-evidence",
            [{"type": "text", "text": "Verify with structured evidence"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        items = [
            {
                "id": "raw-command-private",
                "type": "commandExecution",
                "status": "completed",
                "durationMs": 240,
                "exitCode": 0,
                "command": "pytest /Users/alice/private --token=secretvalue",
                "aggregatedOutput": "12 tests passed at /Users/alice/private",
            },
            {
                "id": "raw-mcp-private",
                "type": "mcpToolCall",
                "status": "completed",
                "toolName": "coffee.search",
                "result": {
                    "token": "secretvalue",
                    "path": "/Users/alice/private/result.json",
                },
            },
            {
                "id": "raw-file-private",
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {
                        "path": "/Users/alice/private/Main.kt",
                        "additions": 8,
                        "deletions": 2,
                        "preview": (
                            "- password=hunter22\n"
                            "+ token=secretvalue\n"
                            "+ read /Users/alice/private/Main.kt"
                        ),
                    }
                ],
            },
            {
                "id": "raw-test-private",
                "type": "testResult",
                "status": "completed",
                "testEvidence": {
                    "title": "Regression checks",
                    "summary": "token=secretvalue at /Users/alice/private",
                    "passed": 12,
                    "failed": 0,
                    "skipped": 1,
                    "durationMs": 1234,
                    "status": "completed",
                },
            },
        ]
        for item in items:
            await self.context.service.handle_bridge_event(
                BridgeEvent(
                    method="item/completed",
                    params={
                        "threadId": raw_thread_id,
                        "turnId": raw_turn_id,
                        "item": item,
                    },
                )
            )

        panel = next(
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message["role"] == "assistant"
        )
        typed = [
            block
            for block in panel["blocks"].values()
            if block["type"] in {"tool", "diff", "test"}
        ]
        self.assertEqual(["tool", "tool", "diff", "test"], [block["type"] for block in typed])
        self.assertEqual("Command", typed[0]["label"])
        self.assertEqual("coffee.search", typed[1]["label"])
        self.assertEqual("Main.kt", typed[2]["fileLabel"])
        self.assertEqual(8, typed[2]["additions"])
        self.assertEqual(2, typed[2]["deletions"])
        self.assertEqual(12, typed[3]["passed"])
        self.assertEqual(0, typed[3]["failed"])
        self.assertEqual(1, typed[3]["skipped"])
        public = json.dumps(panel, ensure_ascii=False)
        for forbidden in (
            "hunter22",
            "secretvalue",
            "/Users/alice",
            "raw-command-private",
            "raw-mcp-private",
            "aggregatedOutput",
        ):
            self.assertNotIn(forbidden, public)
        # The command output contains test-like prose, but only the explicit
        # structured testEvidence object may create a Test block.
        self.assertEqual(1, sum(block["type"] == "test" for block in typed))

    def test_unstructured_terminal_test_prose_never_creates_test_block(self) -> None:
        snapshot = project_live_work_panel(
            message_id="msg_no_heuristics",
            thread_id="thr_no_heuristics",
            turn_id="turn_no_heuristics",
            source_item_id="item_no_heuristics",
            turn_status="completed",
            items=[
                ProjectionItem(
                    item_id="command",
                    ordinal=0,
                    type="commandExecution",
                    status="completed",
                    text="812 tests passed; 0 failed",
                    evidence={
                        "kind": "tool",
                        "label": "Command",
                        "detail": "Command completed",
                    },
                )
            ],
            created_at="2026-07-19T00:00:00Z",
            updated_at="2026-07-19T00:00:01Z",
        )
        self.assertNotIn(
            "test",
            {block["type"] for block in snapshot["blocks"].values()},
        )

    async def test_structured_evidence_survives_gateway_restart_exactly(self) -> None:
        self.context.bridge.auto_events = False
        thread = await self.context.service.create_thread(self.device, "Evidence restart")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-evidence-restart",
            [{"type": "text", "text": "Persist structured evidence"}],
        )
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": self.context.store.thread_raw_id(thread.id),
                    "turnId": self.context.store.turn_raw_id(turn.id),
                    "item": {
                        "id": "raw-test-restart",
                        "type": "testResult",
                        "status": "completed",
                        "testEvidence": {
                            "passed": 7,
                            "failed": 0,
                            "skipped": 0,
                            "durationMs": 321,
                        },
                    },
                },
            )
        )
        before = self.context.store.list_message_snapshots(thread.id)

        await self.context.service.close()
        self.context.service = GatewayService(
            self.context.settings,
            self.context.store,
            self.context.bridge,
        )
        await self.context.service.start()

        after = self.context.store.list_message_snapshots(thread.id)
        self.assertEqual(before, after)
        panel = next(message for message in after if message["role"] == "assistant")
        test_block = next(
            block for block in panel["blocks"].values() if block["type"] == "test"
        )
        self.assertEqual((7, 0, 0), (
            test_block["passed"],
            test_block["failed"],
            test_block["skipped"],
        ))

    def test_371_commentary_and_994_file_change_stress_is_bounded(self) -> None:
        items = [
            ProjectionItem(
                item_id="item_commentary_%04d" % index,
                ordinal=index,
                type="agentMessage",
                phase="commentary",
                text=("Safe progress %04d. " % index) + ("x" * 300),
                status="completed",
            )
            for index in range(371)
        ]
        items.extend(
            ProjectionItem(
                item_id="item_file_%04d" % index,
                ordinal=371 + index,
                type="fileChange",
                status="completed",
                change_count=1,
            )
            for index in range(994)
        )
        snapshot = project_live_work_panel(
            message_id="msg_public",
            thread_id="thr_public",
            turn_id="turn_public",
            source_item_id="item_public",
            turn_status="completed",
            items=items,
            created_at="2026-07-19T00:00:00Z",
            updated_at="2026-07-19T00:01:00Z",
        )
        encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_PUBLIC_MESSAGE_BYTES)
        self.assertLessEqual(len(snapshot["blocks"]["activity"]["text"].encode("utf-8")), 65_536)
        self.assertTrue(snapshot["blocks"]["activity"]["truncated"])
        metrics = {
            item["label"]: item["value"]
            for item in snapshot["blocks"]["metrics"]["items"]
        }
        self.assertEqual("371", metrics["Updates"])
        self.assertEqual("994", metrics["File changes"])
        self.assertLessEqual(
            sum(block["type"] == "diff" for block in snapshot["blocks"].values()),
            48,
        )

    def test_two_exact_64k_text_blocks_fit_serialized_message_budget(self) -> None:
        items = [
            ProjectionItem(
                item_id="activity",
                ordinal=0,
                type="agentMessage",
                phase="commentary",
                text="a" * 65_536,
            ),
            ProjectionItem(
                item_id="answer",
                ordinal=1,
                type="agentMessage",
                phase="final_answer",
                text="b" * 65_536,
            ),
        ]
        snapshot = project_live_work_panel(
            message_id="msg_budget",
            thread_id="thr_budget",
            turn_id="turn_budget",
            source_item_id="item_budget",
            turn_status="completed",
            items=items,
            created_at="2026-07-19T00:00:00Z",
            updated_at="2026-07-19T00:01:00Z",
        )
        self.assertLessEqual(
            serialized_public_message_bytes(snapshot),
            MAX_PUBLIC_MESSAGE_BYTES,
        )
        self.assertEqual(65_536, len(snapshot["blocks"]["activity"]["text"]))
        self.assertEqual(65_536, len(snapshot["blocks"]["answer"]["text"]))
        self.assertTrue(snapshot["fallback"]["text"])
        self.assertIn("metrics", snapshot["blocks"])

    def test_escaped_double_boundary_degrades_deterministically_without_throwing(self) -> None:
        items = [
            ProjectionItem(
                item_id="activity",
                ordinal=0,
                type="agentMessage",
                phase="commentary",
                text='"' * 70_000,
            ),
            ProjectionItem(
                item_id="answer",
                ordinal=1,
                type="agentMessage",
                phase="final_answer",
                text="\\" * 70_000,
            ),
        ]
        arguments = dict(
            message_id="msg_escaped_budget",
            thread_id="thr_escaped_budget",
            turn_id="turn_escaped_budget",
            source_item_id="item_escaped_budget",
            turn_status="completed",
            items=items,
            created_at="2026-07-19T00:00:00Z",
            updated_at="2026-07-19T00:01:00Z",
        )
        first = project_live_work_panel(**arguments)
        second = project_live_work_panel(**arguments)
        self.assertEqual(first, second)
        self.assertLessEqual(
            serialized_public_message_bytes(first),
            MAX_PUBLIC_MESSAGE_BYTES,
        )
        self.assertTrue(first["blocks"]["activity"]["truncated"])
        self.assertTrue(first["blocks"]["answer"]["truncated"])
        self.assertTrue(first["fallback"]["text"].endswith("[truncated]"))
        self.assertIn("metrics", first["blocks"])


if __name__ == "__main__":
    unittest.main()
