from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path

from cheby_gateway.bridge import (
    BridgeEvent,
    BridgeFrameTooLarge,
    BridgeMethodDenied,
    BridgeThreadNotFound,
    FakeCodexBridge,
    StdioCodexBridge,
)
from cheby_gateway.models import ApprovalDecision
from cheby_gateway.service import _AsyncLifecycleGate, GatewayError, GatewayService
from cheby_gateway.store import GatewayStore

from .helpers import make_context, pair
from .test_protocol_contract import load_protocol_validator


def projected_text(message):
    for block_id in ("text", "answer", "activity"):
        block = message.get("blocks", {}).get(block_id)
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return ""


class GatewayServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context()
        await self.context.service.start()
        self.pairing, self.device = pair(self.context)

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.context.close()

    async def test_duplicate_client_message_creates_one_codex_turn(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Idempotency")
        input_parts = [{"type": "text", "text": "Run once"}]

        first = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-message-12345",
            input_parts,
        )
        second = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-message-12345",
            input_parts,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        self.assertNotIn("raw-turn", json.dumps(first.model_dump(by_alias=True)))
        messages = self.context.store.list_message_snapshots(thread.id)
        user_messages = [message for message in messages if message["role"] == "user"]
        self.assertEqual(1, len(user_messages))
        self.assertEqual("client-message-12345", user_messages[0]["clientMessageId"])
        self.assertEqual(first.id, user_messages[0]["turnId"])

        with self.assertRaises(GatewayError) as busy:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "a-different-client-message",
                input_parts,
            )
        self.assertEqual("THREAD_BUSY", busy.exception.code)

    def test_pairing_grant_is_one_time_and_replay_does_not_revoke_phone(self) -> None:
        active_before = self.context.store.active_device()
        with self.assertRaises(GatewayError) as replayed:
            self.context.service.exchange_pairing(
                pairing_secret="test-pairing-secret",
                device_name="Attacker",
                device_public_key="attacker-public-key-material",
            )
        self.assertEqual("PAIRING_DENIED", replayed.exception.code)
        active_after = self.context.store.active_device()
        self.assertEqual(active_before.id, active_after.id)

    async def test_pairing_failures_do_not_permanently_lock_grant(self) -> None:
        context = make_context()
        await context.service.start()
        try:
            with self.assertRaises(GatewayError) as first:
                context.service.exchange_pairing(
                    pairing_secret="wrong-pairing-secret-one",
                    device_name="Probe",
                    device_public_key="probe-public-key-material",
                )
            self.assertEqual("PAIRING_DENIED", first.exception.code)
            with self.assertRaises(GatewayError) as second:
                context.service.exchange_pairing(
                    pairing_secret="wrong-pairing-secret-two",
                    device_name="Probe",
                    device_public_key="probe-public-key-material",
                )
            self.assertEqual("PAIRING_DENIED", second.exception.code)
            paired = context.service.exchange_pairing(
                pairing_secret="test-pairing-secret",
                device_name="Phone",
                device_public_key=context.device_public_key,
            )
            self.assertEqual(paired.device_id, context.store.active_device().id)
        finally:
            await context.service.close()
            context.close()

    async def test_approval_can_be_decided_only_once(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Approval")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-message-approval",
            [{"type": "text", "text": "Run tests"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        self.assertIsNotNone(raw_thread_id)
        self.assertIsNotNone(raw_turn_id)

        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=77,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-approval",
                    "command": ["python", "-m", "unittest"],
                    "reason": "Verify the gateway",
                },
            )
        )
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        approval_events = [
            event for event in replay.events if event.type == "approval.requested"
        ]
        self.assertEqual(1, len(approval_events))
        approval_payload = approval_events[0].payload
        pending_detail = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual(1, len(pending_detail.approvals))
        self.assertEqual(
            approval_payload["approvalId"],
            pending_detail.approvals[0].approval_id,
        )
        self.assertEqual(
            approval_payload["actionToken"],
            pending_detail.approvals[0].action_token,
        )

        resolved = await self.context.service.resolve_approval(
            self.device,
            approval_payload["approvalId"],
            approval_payload["actionToken"],
            ApprovalDecision.APPROVE,
        )
        self.assertEqual("approved", resolved.state)
        resolved_detail = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual([], resolved_detail.approvals)

        with self.assertRaises(GatewayError) as raised:
            await self.context.service.resolve_approval(
                self.device,
                approval_payload["approvalId"],
                approval_payload["actionToken"],
                ApprovalDecision.APPROVE,
            )
        self.assertEqual("APPROVAL_ALREADY_RESOLVED", raised.exception.code)
        self.assertEqual(1, len(self.context.bridge.approval_responses))

    async def test_concurrent_distinct_messages_reserve_only_one_turn(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Concurrency")
        entered = asyncio.Event()
        release = asyncio.Event()
        original_start_turn = self.context.bridge.start_turn

        async def blocked_start_turn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_start_turn(*args, **kwargs)

        self.context.bridge.start_turn = blocked_start_turn
        first_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                thread.id,
                "client-concurrent-first",
                [{"type": "text", "text": "First"}],
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        with self.assertRaises(GatewayError) as busy:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "client-concurrent-second",
                [{"type": "text", "text": "Second"}],
            )
        self.assertEqual(409, busy.exception.status_code)
        self.assertEqual("THREAD_BUSY", busy.exception.code)
        self.assertTrue(busy.exception.retryable)
        self.assertEqual(2, busy.exception.retry_after_seconds)

        release.set()
        await first_task
        self.assertEqual(1, self.context.bridge.start_turn_calls)

    async def test_concurrent_same_business_key_waits_for_canonical_acceptance(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Concurrent idempotent retry",
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_start_turn = self.context.bridge.start_turn

        async def blocked_start_turn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_start_turn(*args, **kwargs)

        self.context.bridge.start_turn = blocked_start_turn
        input_parts = [{"type": "text", "text": "Execute once"}]
        first_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                thread.id,
                "client-concurrent-same-key",
                input_parts,
            )
        )
        await entered.wait()
        replay_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                thread.id,
                "client-concurrent-same-key",
                input_parts,
            )
        )
        for _ in range(20):
            if replay_task.done():
                break
            await asyncio.sleep(0)
        self.assertFalse(
            replay_task.done(),
            "an in-flight retry must not expose a dispatching reservation",
        )
        release.set()
        first, replay = await asyncio.gather(first_task, replay_task)
        self.assertEqual(first.id, replay.id)
        self.assertEqual("inProgress", first.status)
        self.assertEqual("inProgress", replay.status)
        self.assertEqual(1, self.context.bridge.start_turn_calls)

    async def test_thread_barrier_keeps_same_thread_single_active_and_other_thread_live(
        self,
    ) -> None:
        self.context.bridge.auto_events = False
        alpha = await self.context.service.create_thread(self.device, "Alpha")
        beta = await self.context.service.create_thread(self.device, "Beta")

        alpha_first = await self.context.service.start_turn(
            self.device,
            alpha.id,
            "client-alpha-first",
            [{"type": "text", "text": "Hold Alpha"}],
        )
        beta_first = await self.context.service.start_turn(
            self.device,
            beta.id,
            "client-beta-first",
            [{"type": "text", "text": "Hold Beta"}],
        )
        active = self.context.store._conn.execute(
            """
            SELECT thread_public_id, COUNT(*) AS count
            FROM turns
            WHERE status IN ('pending', 'dispatching', 'inProgress', 'running')
            GROUP BY thread_public_id
            """
        ).fetchall()
        self.assertEqual(
            {alpha.id: 1, beta.id: 1},
            {str(row["thread_public_id"]): int(row["count"]) for row in active},
        )

        raw_alpha = self.context.store.thread_raw_id(alpha.id)
        raw_beta_turn = self.context.store.turn_raw_id(beta_first.id)
        self.assertIsNotNone(raw_beta_turn)
        cursor_before_wrong = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        ).current_seq
        for wrong_raw_turn in ("raw-turn-stale-terminal", raw_beta_turn):
            await self.context.service.handle_bridge_event(
                BridgeEvent(
                    method="turn/completed",
                    params={
                        "threadId": raw_alpha,
                        "turn": {
                            "id": wrong_raw_turn,
                            "status": "completed",
                            "items": [],
                        },
                    },
                )
            )
        self.assertEqual(
            cursor_before_wrong,
            self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                0,
            ).current_seq,
        )
        self.assertEqual(
            "inProgress",
            self.context.store.turn_by_public_id(alpha_first.id)["status"],
        )

        with self.assertRaises(GatewayError) as busy:
            await self.context.service.start_turn(
                self.device,
                alpha.id,
                "client-alpha-second",
                [{"type": "text", "text": "Must wait for Alpha"}],
            )
        self.assertEqual("THREAD_BUSY", busy.exception.code)
        self.assertTrue(busy.exception.retryable)
        self.assertEqual(2, busy.exception.retry_after_seconds)
        self.assertEqual(2, self.context.bridge.start_turn_calls)

        raw_alpha_turn = self.context.store.turn_raw_id(alpha_first.id)
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_alpha,
                    "turn": {
                        "id": raw_alpha_turn,
                        "status": "completed",
                        "items": [],
                    },
                },
            )
        )
        alpha_second = await self.context.service.start_turn(
            self.device,
            alpha.id,
            "client-alpha-second",
            [{"type": "text", "text": "Must wait for Alpha"}],
        )
        self.assertNotEqual(alpha_first.id, alpha_second.id)
        self.assertEqual(3, self.context.bridge.start_turn_calls)
        self.assertIn(
            beta_first.id,
            {
                str(row["public_id"])
                for row in self.context.store._conn.execute(
                    """
                    SELECT public_id FROM turns
                    WHERE status IN ('pending', 'dispatching', 'inProgress', 'running')
                    """
                ).fetchall()
            },
        )

        lifecycle = [
            event
            for event in self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                0,
            ).events
            if event.type.startswith("turn.")
        ]
        alpha_events = [
            (event.type, event.thread_id, event.turn_id)
            for event in lifecycle
            if event.turn_id == alpha_first.id
        ]
        self.assertEqual(
            [
                ("turn.started", alpha.id, alpha_first.id),
                ("turn.completed", alpha.id, alpha_first.id),
            ],
            alpha_events,
        )
        public = json.dumps(
            [
                event.model_dump(by_alias=True, exclude_none=True)
                for event in lifecycle
            ]
        )
        self.assertNotIn(str(raw_alpha), public)
        self.assertNotIn(str(raw_alpha_turn), public)

    async def test_distinct_thread_submissions_reach_codex_concurrently(self) -> None:
        self.context.bridge.auto_events = False
        alpha = await self.context.service.create_thread(self.device, "Concurrent Alpha")
        beta = await self.context.service.create_thread(self.device, "Concurrent Beta")
        raw_alpha = self.context.store.thread_raw_id(alpha.id)
        original_start = self.context.bridge.start_turn
        alpha_entered = asyncio.Event()
        release_alpha = asyncio.Event()
        beta_entered = asyncio.Event()

        async def controlled_start(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            if raw_thread_id == raw_alpha:
                alpha_entered.set()
                await release_alpha.wait()
            else:
                beta_entered.set()
            return await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )

        self.context.bridge.start_turn = controlled_start
        alpha_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                alpha.id,
                "client-concurrent-alpha",
                [{"type": "text", "text": "Hold Alpha"}],
            )
        )
        await alpha_entered.wait()
        beta_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                beta.id,
                "client-concurrent-beta",
                [{"type": "text", "text": "Start Beta now"}],
            )
        )
        try:
            for _ in range(20):
                if beta_entered.is_set():
                    break
                await asyncio.sleep(0)
            self.assertTrue(
                beta_entered.is_set(),
                "a blocked Alpha Turn must not serialize Beta",
            )
        finally:
            release_alpha.set()
        alpha_turn, beta_turn = await asyncio.gather(alpha_task, beta_task)
        self.assertEqual(2, self.context.bridge.start_turn_calls)
        self.assertNotEqual(alpha_turn.thread_id, beta_turn.thread_id)

    async def test_distinct_thread_snapshot_does_not_globally_block_turns(self) -> None:
        self.context.bridge.auto_events = False
        alpha = await self.context.service.create_thread(self.device, "Snapshot Alpha")
        beta = await self.context.service.create_thread(self.device, "Snapshot Beta")
        raw_alpha = self.context.store.thread_raw_id(alpha.id)
        original_start = self.context.bridge.start_turn
        alpha_entered = asyncio.Event()
        release_alpha = asyncio.Event()
        beta_entered = asyncio.Event()

        async def controlled_start(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            if raw_thread_id == raw_alpha:
                alpha_entered.set()
                await release_alpha.wait()
            else:
                beta_entered.set()
            return await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )

        self.context.bridge.start_turn = controlled_start
        alpha_task = asyncio.create_task(
            self.context.service.start_turn(
                self.device,
                alpha.id,
                "client-snapshot-alpha",
                [{"type": "text", "text": "Hold Alpha"}],
            )
        )
        await alpha_entered.wait()

        beta_task = None
        body_failed = True
        try:
            beta_detail = await asyncio.wait_for(
                self.context.service.read_thread(beta.id, self.device),
                timeout=1,
            )
            self.assertEqual(beta.id, beta_detail.thread.id)
            beta_task = asyncio.create_task(
                self.context.service.start_turn(
                    self.device,
                    beta.id,
                    "client-snapshot-beta",
                    [{"type": "text", "text": "Start Beta now"}],
                )
            )
            await asyncio.wait_for(beta_entered.wait(), timeout=1)
            body_failed = False
        finally:
            release_alpha.set()
            if body_failed:
                tasks = [alpha_task]
                if beta_task is not None:
                    tasks.append(beta_task)
                await asyncio.gather(*tasks, return_exceptions=True)
        assert beta_task is not None
        alpha_turn, beta_turn = await asyncio.gather(alpha_task, beta_task)
        self.assertNotEqual(alpha_turn.thread_id, beta_turn.thread_id)

    async def test_lifecycle_gate_prefers_writer_over_late_submission(self) -> None:
        gate = _AsyncLifecycleGate()
        release_first_reader = asyncio.Event()
        first_reader_entered = asyncio.Event()
        writer_entered = asyncio.Event()
        release_writer = asyncio.Event()
        late_reader_entered = asyncio.Event()

        async def first_reader() -> None:
            async with gate.read():
                first_reader_entered.set()
                await release_first_reader.wait()

        async def writer() -> None:
            async with gate.write():
                writer_entered.set()
                await release_writer.wait()

        async def late_reader() -> None:
            async with gate.read():
                late_reader_entered.set()

        first_task = asyncio.create_task(first_reader())
        await first_reader_entered.wait()
        writer_task = asyncio.create_task(writer())
        for _ in range(20):
            if gate._waiting_writers == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(1, gate._waiting_writers)
        late_task = asyncio.create_task(late_reader())
        release_first_reader.set()
        await writer_entered.wait()
        self.assertFalse(late_reader_entered.is_set())
        release_writer.set()
        await asyncio.gather(first_task, writer_task, late_task)
        self.assertTrue(late_reader_entered.is_set())

    async def test_cancelled_lifecycle_writer_unblocks_waiting_submissions(self) -> None:
        gate = _AsyncLifecycleGate()
        release_reader = asyncio.Event()
        reader_entered = asyncio.Event()
        late_reader_entered = asyncio.Event()

        async def reader() -> None:
            async with gate.read():
                reader_entered.set()
                await release_reader.wait()

        async def writer() -> None:
            async with gate.write():
                self.fail("cancelled writer must not enter")

        async def late_reader() -> None:
            async with gate.read():
                late_reader_entered.set()

        reader_task = asyncio.create_task(reader())
        await reader_entered.wait()
        writer_task = asyncio.create_task(writer())
        for _ in range(20):
            if gate._waiting_writers == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(1, gate._waiting_writers)
        late_task = asyncio.create_task(late_reader())
        writer_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await writer_task
        await late_reader_entered.wait()
        release_reader.set()
        await asyncio.gather(reader_task, late_task)
        self.assertEqual(0, gate._waiting_writers)

    async def test_raw_thread_id_never_becomes_public_thread_id(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Public mapping")
        raw_id = self.context.store.thread_raw_id(thread.id)

        self.assertIsNotNone(raw_id)
        self.assertNotEqual(raw_id, thread.id)
        self.assertTrue(thread.id.startswith("thr_"))
        self.assertNotIn(str(raw_id), json.dumps(thread.model_dump(by_alias=True)))

    async def test_stdio_bridge_rejects_generic_rpc_before_transport(self) -> None:
        bridge = StdioCodexBridge(
            command=("false",),
            cwd="/workspace",
        )
        for method in ("fs/readFile", "process/spawn", "thread/shellCommand"):
            with self.assertRaises(BridgeMethodDenied):
                await bridge.request_method(method, {})

    async def test_stdio_bridge_initialize_and_allowlisted_request(self) -> None:
        fake_server = Path(__file__).with_name("fake_app_server.py")
        bridge = StdioCodexBridge(
            command=(sys.executable, "-u", str(fake_server)),
            cwd=str(fake_server.parent),
            request_timeout_seconds=2,
        )
        await bridge.start()
        try:
            threads = await bridge.list_threads()
            history = await bridge.read_thread("raw-thread-from-stdio")
        finally:
            await bridge.close()

        self.assertEqual(1, len(threads))
        self.assertEqual("Stdio thread", threads[0].title)
        self.assertEqual("raw-thread-from-stdio", threads[0].raw_id)
        self.assertEqual(2, len(history.turns))
        self.assertEqual(2, len(history.turns[0].items))
        self.assertEqual("client-history-one", history.turns[0].items[0].client_message_id)
        self.assertEqual(
            [{"type": "text", "text": "First question"}],
            history.turns[0].items[0].content,
        )
        self.assertTrue(history.turns[0].items[1].text.startswith("First answer"))
        self.assertGreater(len(history.turns[0].items[1].text), 65536)
        self.assertEqual("2026-07-19T00:00:00Z", history.turns[0].created_at)

    async def test_stdio_bridge_fails_closed_when_frame_exceeds_hard_cap(self) -> None:
        fake_server = Path(__file__).with_name("fake_app_server.py")
        bridge = StdioCodexBridge(
            command=(sys.executable, "-u", str(fake_server)),
            cwd=str(fake_server.parent),
            request_timeout_seconds=2,
            protocol_frame_limit_bytes=4096,
        )
        await bridge.start()
        try:
            with self.assertRaises(BridgeFrameTooLarge):
                await bridge.read_thread("raw-thread-from-stdio")
            self.assertFalse(bridge.connected)
        finally:
            await bridge.close()

    async def test_stdio_bridge_classifies_explicit_thread_not_loaded(self) -> None:
        fake_server = Path(__file__).with_name("fake_app_server.py")
        bridge = StdioCodexBridge(
            command=(sys.executable, "-u", str(fake_server)),
            cwd=str(fake_server.parent),
            request_timeout_seconds=2,
        )
        await bridge.start()
        try:
            with self.assertRaises(BridgeThreadNotFound):
                await bridge.read_thread("raw-thread-not-loaded")
        finally:
            await bridge.close()

    async def test_stdio_bridge_accepts_observed_49mb_history_class(self) -> None:
        fake_server = Path(__file__).with_name("fake_app_server.py")
        bridge = StdioCodexBridge(
            command=(sys.executable, "-u", str(fake_server)),
            cwd=str(fake_server.parent),
            request_timeout_seconds=10,
        )
        await bridge.start()
        try:
            history = await bridge.read_thread("raw-thread-large-frame")
            self.assertEqual(1, len(history.turns))
            self.assertEqual(49000000, len(history.turns[0].items[0].text))
        finally:
            await bridge.close()

    async def test_read_thread_projects_two_turn_codex_history(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Old history")
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        self.context.bridge.threads[raw_thread_id]["turns"] = [
            {
                "id": "raw-turn-old-one",
                "status": "completed",
                "startedAt": 1784419200,
                "completedAt": 1784419201,
                "items": [
                    {
                        "id": "raw-user-old-one",
                        "type": "userMessage",
                        "clientId": "client-old-one",
                        "content": [{"type": "text", "text": "First question"}],
                    },
                    {
                        "id": "raw-agent-old-one",
                        "type": "agentMessage",
                        "text": "First answer",
                    },
                ],
            },
            {
                "id": "raw-turn-old-two",
                "status": "completed",
                "startedAt": 1784419202,
                "completedAt": 1784419203,
                "items": [
                    {
                        "id": "raw-user-old-two",
                        "type": "userMessage",
                        "clientId": "client-old-two",
                        "content": [{"type": "text", "text": "Second question"}],
                    },
                    {
                        "id": "raw-agent-old-two",
                        "type": "agentMessage",
                        "text": "Second answer",
                    },
                ],
            },
        ]

        detail = await self.context.service.read_thread(thread.id, self.device)

        self.assertEqual(
            ["user", "assistant", "user", "assistant"],
            [message["role"] for message in detail.messages],
        )
        self.assertEqual(
            ["First question", "First answer", "Second question", "Second answer"],
            [projected_text(message) for message in detail.messages],
        )
        self.assertEqual(self.device.stream_id, detail.stream_id)
        self.assertGreaterEqual(detail.cursor, 1)
        self.assertNotIn("raw-", json.dumps(detail.model_dump(by_alias=True)))

    async def test_user_message_survives_gateway_process_restart(self) -> None:
        context = make_context()
        reopened_store = None
        reopened_service = None
        try:
            await context.service.start()
            pairing, device = pair(context)
            thread = await context.service.create_thread(device, "Restart history")
            turn = await context.service.start_turn(
                device,
                thread.id,
                "client-restart-history",
                [{"type": "text", "text": "Persist me"}],
            )
            bridge_threads = copy.deepcopy(context.bridge.threads)
            db_path = context.settings.db_path
            await context.service.close()
            context.store.close()

            reopened_store = GatewayStore(db_path)
            reopened_bridge = FakeCodexBridge(auto_events=False)
            reopened_bridge.threads = bridge_threads
            reopened_service = GatewayService(
                context.settings,
                reopened_store,
                reopened_bridge,
            )
            await reopened_service.start()
            reopened_device = reopened_service.authenticate(pairing.access_token)
            detail = await reopened_service.read_thread(thread.id, reopened_device)

            user_messages = [
                message for message in detail.messages if message["role"] == "user"
            ]
            self.assertEqual(1, len(user_messages))
            self.assertEqual("client-restart-history", user_messages[0]["clientMessageId"])
            self.assertEqual(turn.id, user_messages[0]["turnId"])
            self.assertEqual("Persist me", user_messages[0]["blocks"]["text"]["text"])
        finally:
            if reopened_service is not None:
                await reopened_service.close()
            if reopened_store is not None:
                reopened_store.close()
            context.tempdir.cleanup()

    def test_waiting_user_status_shapes_are_not_downgraded(self) -> None:
        for value in (
            "waitingUser",
            "waitingOnUser",
            "waitingOnUserInput",
            {"type": "active", "activeFlags": ["waitingOnUserInput"]},
        ):
            self.assertEqual("waitingUser", self.context.service._public_status(value).value)

    async def test_history_and_later_event_share_canonical_turn_id(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Canonical turn")
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        self.context.bridge.threads[raw_thread_id]["turns"] = [
            {
                "id": "raw-turn-canonical",
                "status": "inProgress",
                "startedAt": 1784419200,
                "items": [
                    {
                        "id": "raw-user-canonical",
                        "type": "userMessage",
                        "clientId": "client-canonical",
                        "content": [{"type": "text", "text": "Continue"}],
                    },
                    {
                        "id": "raw-agent-canonical",
                        "type": "agentMessage",
                        "text": "Partial ",
                    },
                ],
            }
        ]
        detail = await self.context.service.read_thread(thread.id, self.device)
        projected_turn_id = detail.messages[0]["turnId"]
        assistant_before = next(
            message for message in detail.messages if message["role"] == "assistant"
        )
        self.assertEqual("streaming", assistant_before["state"])

        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": "raw-turn-canonical",
                    "item": {
                        "id": "raw-agent-canonical",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "status": "completed",
                        "text": "Partial Later event",
                    },
                },
            )
        )
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            detail.cursor,
        )
        self.assertTrue(replay.events)
        self.assertTrue(
            all(event.turn_id == projected_turn_id for event in replay.events)
        )
        assistant_after = self.context.store.message_snapshot(
            assistant_before["messageId"]
        )
        self.assertEqual("streaming", assistant_after["state"])
        self.assertEqual(
            "Partial Later event",
            assistant_after["blocks"]["activity"]["text"],
        )

    async def test_long_history_is_bounded_for_public_contract(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Long history")
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        self.context.bridge.threads[raw_thread_id]["turns"] = [
            {
                "id": "raw-turn-long",
                "status": "completed",
                "startedAt": 1784419200,
                "completedAt": 1784419201,
                "items": [
                    {
                        "id": "raw-agent-long",
                        "type": "agentMessage",
                        "text": "x" * 70000,
                    }
                ],
            }
        ]
        detail = await self.context.service.read_thread(thread.id, self.device)
        message = detail.messages[0]

        self.assertEqual(65536, len(message["blocks"]["answer"]["text"]))
        self.assertTrue(message["blocks"]["answer"]["truncated"])
        self.assertTrue(message["blocks"]["answer"]["text"].endswith("[truncated]"))
        self.assertLessEqual(len(message["fallback"]["text"]), 65536)
        validator, _ = load_protocol_validator()
        self.assertEqual([], validator.validate("richMessage", message))

    async def test_live_delta_overflow_matches_cold_recovery_snapshot(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Long live")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-long-live",
            [{"type": "text", "text": "Stream a lot"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        complete_text = ("a" * 40000) + ("b" * 40000)
        for delta in ("a" * 40000, "b" * 40000):
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="item/agentMessage/delta",
                    params={
                        "threadId": raw_thread_id,
                        "turnId": raw_turn_id,
                        "itemId": "raw-agent-long-live",
                        "delta": delta,
                    },
                )
            )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": "raw-agent-long-live",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "status": "completed",
                        "text": complete_text,
                    },
                },
            )
        )
        self.context.bridge.turns[raw_turn_id]["items"].append(
            {
                "id": "raw-agent-long-live",
                "type": "agentMessage",
                "phase": "commentary",
                "status": "completed",
                "text": complete_text,
            }
        )

        detail = await self.context.service.read_thread(thread.id, self.device)
        assistant = next(
            message for message in detail.messages if message["role"] == "assistant"
        )
        self.assertEqual(65536, len(assistant["blocks"]["activity"]["text"]))
        self.assertTrue(assistant["blocks"]["activity"]["truncated"])
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        activity_values = [
            operation["value"]["text"]
            for event in replay.events
            if event.type == "message.patch"
            for operation in event.payload.get("ops", [])
            if operation.get("op") == "block.put"
            and operation.get("blockId") == "activity"
        ]
        self.assertEqual(assistant["blocks"]["activity"]["text"], activity_values[-1])

    async def test_thread_read_repairs_partial_streaming_assistant_snapshot(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Repair partial")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-repair-partial",
            [{"type": "text", "text": "Give the full answer"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/agentMessage/delta",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-agent-repair",
                    "delta": "partial",
                },
            )
        )
        raw_turn = self.context.bridge.turns[raw_turn_id]
        raw_turn["status"] = "completed"
        raw_turn["completedAt"] = 1784419201
        raw_turn["items"].append(
            {
                "id": "raw-agent-repair",
                "type": "agentMessage",
                "text": "authoritative full answer",
            }
        )
        self.context.bridge.threads[raw_thread_id]["status"] = "idle"

        detail = await self.context.service.read_thread(thread.id, self.device)
        assistant = next(
            message for message in detail.messages if message["role"] == "assistant"
        )
        self.assertEqual("completed", assistant["state"])
        self.assertEqual(
            "authoritative full answer",
            assistant["blocks"]["answer"]["text"],
        )
        self.assertGreaterEqual(assistant["revision"], 1)
        persisted = self.context.store.message_snapshot(assistant["messageId"])
        self.assertEqual(assistant, persisted)
        cold_replay = self.context.store.replay_events(
            self.device.id,
            detail.stream_id,
            detail.cursor,
        )
        self.assertEqual([], cold_replay.events)


if __name__ == "__main__":
    unittest.main()
