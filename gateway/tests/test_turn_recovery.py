from __future__ import annotations

import unittest

from cheby_gateway.bridge import BridgeDeliveryUnknown, BridgeEvent, BridgeNotAccepted
from cheby_gateway.service import GatewayError, GatewayService, turn_request_fingerprint

from .helpers import make_context, pair


class TurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context()
        await self.context.service.start()
        _, self.device = pair(self.context)

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.context.close()

    async def _restart_service(self) -> None:
        await self.context.service.close()
        self.context.service = GatewayService(
            self.context.settings,
            self.context.store,
            self.context.bridge,
        )
        await self.context.service.start()

    async def test_definitely_not_accepted_reuses_same_turn_id_atomically(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Safe retry")
        original = self.context.bridge.start_turn
        calls = 0

        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BridgeNotAccepted("explicit rejection")
            return await original(*args, **kwargs)

        self.context.bridge.start_turn = fail_once
        client_message_id = "client-safe-retry-001"
        with self.assertRaises(GatewayError) as first:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                client_message_id,
                [{"type": "text", "text": "Retry safely"}],
            )
        self.assertEqual("TURN_NOT_ACCEPTED", first.exception.code)
        self.assertTrue(first.exception.retryable)
        failed_row = self.context.store.turn_by_client_message(
            self.device.id,
            thread.id,
            client_message_id,
        )
        self.assertIsNotNone(failed_row)
        self.assertEqual("retryable", failed_row["status"])
        self.assertEqual("notAccepted", failed_row["delivery_state"])
        failed_detail = await self.context.service.read_thread(thread.id, self.device)
        failed_user = next(
            message for message in failed_detail.messages if message["role"] == "user"
        )
        self.assertEqual("failed", failed_user["state"])

        retried = await self.context.service.start_turn(
            self.device,
            thread.id,
            client_message_id,
            [{"type": "text", "text": "Retry safely"}],
        )

        self.assertEqual(failed_row["public_id"], retried.id)
        retried_row = self.context.store.turn_by_public_id(retried.id)
        self.assertEqual(2, retried_row["attempt_count"])
        self.assertEqual("accepted", retried_row["delivery_state"])
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        accepted_detail = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual(
            "completed",
            next(
                message
                for message in accepted_detail.messages
                if message.get("clientMessageId") == client_message_id
            )["state"],
        )

    async def test_unknown_delivery_never_resends_and_requires_resync(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Ambiguous")
        original = self.context.bridge.start_turn
        calls = 0

        async def unknown_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BridgeDeliveryUnknown("timeout after write")
            return await original(*args, **kwargs)

        self.context.bridge.start_turn = unknown_once
        client_message_id = "client-ambiguous-001"
        with self.assertRaises(GatewayError) as first:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                client_message_id,
                [{"type": "text", "text": "Do not duplicate"}],
            )
        self.assertEqual(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            first.exception.code,
        )
        row = self.context.store.turn_by_client_message(
            self.device.id,
            thread.id,
            client_message_id,
        )
        self.assertEqual("ambiguous", row["status"])

        with self.assertRaises(GatewayError) as duplicate:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                client_message_id,
                [{"type": "text", "text": "Do not duplicate"}],
            )
        self.assertEqual(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            duplicate.exception.code,
        )
        with self.assertRaises(GatewayError) as blocked_until_sync:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "client-ambiguous-new",
                [{"type": "text", "text": "Wait for resync"}],
            )
        self.assertEqual(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            blocked_until_sync.exception.code,
        )
        self.assertEqual(1, calls)

        await self._restart_service()
        ambiguous_detail = await self.context.service.read_thread(
            thread.id,
            self.device,
        )
        ambiguous_user = next(
            message
            for message in ambiguous_detail.messages
            if message.get("clientMessageId") == client_message_id
        )
        self.assertEqual("failed", ambiguous_user["state"])
        with self.assertRaises(GatewayError) as still_ambiguous:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "client-after-unmatched-resync",
                [{"type": "text", "text": "Still unsafe"}],
            )
        self.assertEqual(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            still_ambiguous.exception.code,
        )
        self.assertEqual(1, calls)

    async def test_startup_releases_never_dispatched_reservation_for_same_id(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Crash reserved")
        input_parts = [{"type": "text", "text": "Resume safely"}]
        reserved = self.context.store.reserve_turn(
            self.device.id,
            thread.id,
            "client-crash-reserved",
            request_fingerprint=turn_request_fingerprint(input_parts),
        )

        await self._restart_service()

        row = self.context.store.turn_by_public_id(reserved.public_id)
        self.assertEqual("retryable", row["status"])
        recovered = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-crash-reserved",
            input_parts,
        )
        self.assertEqual(reserved.public_id, recovered.id)
        self.assertEqual(1, self.context.bridge.start_turn_calls)

    async def test_cold_resume_historical_turn_event_does_not_claim_pending_reservation(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Cold resume replay",
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        original_resume = self.context.bridge.resume_thread
        historical_raw_turn_id = "raw-turn-historical-startup"

        async def resume_with_historical_event(raw_id: str):
            resumed = await original_resume(raw_id)
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="mcpServer/startupStatus/updated",
                    params={
                        "threadId": raw_id,
                        "turnId": historical_raw_turn_id,
                        "status": "ready",
                    },
                )
            )
            return resumed

        self.context.bridge.resume_thread = resume_with_historical_event
        started = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-after-cold-resume",
            [{"type": "text", "text": "hi"}],
        )

        row = self.context.store.turn_by_public_id(started.id)
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        self.assertEqual("accepted", row["delivery_state"])
        self.assertNotEqual(historical_raw_turn_id, row["raw_id"])
        self.assertEqual(
            0,
            self.context.store._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE raw_id = ?",
                (historical_raw_turn_id,),
            ).fetchone()[0],
        )
        self.assertEqual(raw_thread_id, self.context.store.thread_raw_id(thread.id))

    async def test_early_turn_started_event_claims_dispatching_reservation(self) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Early turn started",
        )
        original_start = self.context.bridge.start_turn

        async def start_with_early_event(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            raw_turn = await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="turn/started",
                    params={
                        "threadId": raw_thread_id,
                        "turn": {
                            "id": raw_turn.raw_id,
                            "status": "inProgress",
                            "items": [],
                        },
                    },
                )
            )
            return raw_turn

        self.context.bridge.start_turn = start_with_early_event
        started = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-early-turn-started",
            [{"type": "text", "text": "hi"}],
        )

        row = self.context.store.turn_by_public_id(started.id)
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        self.assertEqual("accepted", row["delivery_state"])
        self.assertEqual(started.id, self.context.store.turn_public_id(row["raw_id"]))
        self.assertEqual(
            1,
            self.context.store._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE thread_public_id = ?",
                (thread.id,),
            ).fetchone()[0],
        )
        lifecycle = [
            event
            for event in self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                0,
            ).events
            if event.turn_id == started.id and event.type.startswith("turn.")
        ]
        self.assertEqual(
            [("turn.started", thread.id, started.id)],
            [
                (event.type, event.thread_id, event.turn_id)
                for event in lifecycle
            ],
        )

    async def test_early_terminal_event_never_regresses_after_start_response(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Terminal before start response",
        )
        original_start = self.context.bridge.start_turn

        async def start_with_early_terminal(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            raw_turn = await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="turn/started",
                    params={
                        "threadId": raw_thread_id,
                        "turn": {
                            "id": raw_turn.raw_id,
                            "status": "inProgress",
                            "items": [],
                        },
                    },
                )
            )
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="turn/completed",
                    params={
                        "threadId": raw_thread_id,
                        "turn": {
                            "id": raw_turn.raw_id,
                            "status": "completed",
                            "items": [],
                        },
                    },
                )
            )
            return raw_turn

        self.context.bridge.start_turn = start_with_early_terminal
        completed = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-early-terminal",
            [{"type": "text", "text": "finish immediately"}],
        )

        row = self.context.store.turn_by_public_id(completed.id)
        self.assertEqual("completed", completed.status)
        self.assertEqual("completed", row["status"])
        self.assertEqual("terminal", row["delivery_state"])
        self.assertEqual("idle", self.context.store.thread_by_public_id(thread.id).status.value)
        lifecycle = [
            event
            for event in self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                0,
            ).events
            if event.turn_id == completed.id and event.type.startswith("turn.")
        ]
        self.assertEqual(
            ["turn.started", "turn.completed"],
            [event.type for event in lifecycle],
        )

        replayed = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-early-terminal",
            [{"type": "text", "text": "finish immediately"}],
        )
        self.assertEqual(completed.id, replayed.id)
        self.assertEqual("completed", replayed.status)
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        user_message = next(
            message
            for message in self.context.store.list_message_snapshots(thread.id)
            if message.get("clientMessageId") == "client-early-terminal"
        )
        self.assertEqual("completed", user_message["state"])

        following = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-after-early-terminal",
            [{"type": "text", "text": "the thread is released"}],
        )
        self.assertNotEqual(completed.id, following.id)
        self.assertEqual(2, self.context.bridge.start_turn_calls)

    async def test_unsolicited_started_event_cannot_create_orphan_or_block_thread(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Ignore orphan lifecycle",
        )
        cursor = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        ).current_seq

        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": self.context.store.thread_raw_id(thread.id),
                    "turn": {
                        "id": "raw-turn-unsolicited",
                        "status": "inProgress",
                        "items": [],
                    },
                },
            )
        )

        self.assertIsNone(self.context.store.turn_public_id("raw-turn-unsolicited"))
        self.assertIsNone(
            self.context.store.mapped_public_id(
                "turn",
                "raw-turn-unsolicited",
                thread.id,
            )
        )
        self.assertEqual(
            cursor,
            self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                cursor,
            ).current_seq,
        )
        self.assertEqual(
            "idle",
            self.context.store.thread_by_public_id(thread.id).status.value,
        )
        started = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-after-unsolicited",
            [{"type": "text", "text": "still available"}],
        )
        self.assertEqual("accepted", self.context.store.turn_by_public_id(started.id)["delivery_state"])

    async def test_early_turn_started_proof_survives_lost_start_response(self) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Accepted before response loss",
        )
        original_start = self.context.bridge.start_turn
        self.context.bridge.auto_events = False

        async def start_emit_then_lose_response(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            raw_turn = await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="turn/started",
                    params={
                        "threadId": raw_thread_id,
                        "turn": {
                            "id": raw_turn.raw_id,
                            "status": "inProgress",
                            "items": [],
                        },
                    },
                )
            )
            raise BridgeDeliveryUnknown("response lost after turn/started")

        self.context.bridge.start_turn = start_emit_then_lose_response
        started = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-accepted-before-loss",
            [{"type": "text", "text": "hi"}],
        )

        row = self.context.store.turn_by_public_id(started.id)
        self.assertIsNotNone(row["raw_id"])
        self.assertEqual("accepted", row["delivery_state"])
        self.assertEqual("inProgress", row["status"])
        self.assertEqual(
            1,
            self.context.store._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE thread_public_id = ?",
                (thread.id,),
            ).fetchone()[0],
        )

    async def test_authoritative_history_reconciles_exact_ambiguous_client_turn(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "History reconciliation",
        )
        original_start = self.context.bridge.start_turn
        self.context.bridge.auto_events = False
        accepted_raw_id = None

        async def start_then_lose_response(
            raw_thread_id: str,
            client_message_id: str,
            input_parts,
        ):
            nonlocal accepted_raw_id
            raw_turn = await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )
            accepted_raw_id = raw_turn.raw_id
            raise BridgeDeliveryUnknown("response lost after Codex persisted history")

        self.context.bridge.start_turn = start_then_lose_response
        client_message_id = "client-history-reconcile"
        with self.assertRaises(GatewayError):
            await self.context.service.start_turn(
                self.device,
                thread.id,
                client_message_id,
                [{"type": "text", "text": "hi"}],
            )
        ambiguous = self.context.store.turn_by_client_message(
            self.device.id,
            thread.id,
            client_message_id,
        )
        self.assertEqual("ambiguous", ambiguous["delivery_state"])
        self.assertIsNone(ambiguous["raw_id"])

        detail = await self.context.service.read_thread(thread.id, self.device)

        reconciled = self.context.store.turn_by_public_id(ambiguous["public_id"])
        self.assertEqual(accepted_raw_id, reconciled["raw_id"])
        self.assertEqual("accepted", reconciled["delivery_state"])
        self.assertEqual(
            "completed",
            next(
                message
                for message in detail.messages
                if message.get("clientMessageId") == client_message_id
            )["state"],
        )
        self.assertEqual(
            0,
            self.context.store._conn.execute(
                "SELECT needs_resync FROM threads WHERE public_id = ?",
                (thread.id,),
            ).fetchone()[0],
        )

    async def test_history_event_without_client_id_cannot_claim_ambiguous_turn(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Uncorrelated late event",
        )
        reservation = self.context.store.reserve_turn(
            self.device.id,
            thread.id,
            "client-must-match-history",
        )
        self.assertTrue(
            self.context.store.mark_turn_dispatching(reservation.public_id)
        )
        self.context.store.mark_turn_ambiguous(reservation.public_id)
        raw_thread_id = self.context.store.thread_raw_id(thread.id)

        await self.context.bridge.emit_event(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": "raw-turn-uncorrelated-history",
                        "status": "inProgress",
                        "items": [],
                    },
                },
            )
        )

        row = self.context.store.turn_by_public_id(reservation.public_id)
        self.assertIsNone(row["raw_id"])
        self.assertEqual("ambiguous", row["delivery_state"])
        self.assertTrue(self.context.store.thread_has_ambiguous_turns(thread.id))

    async def test_exact_late_bind_keeps_resync_for_other_legacy_ambiguous_turn(
        self,
    ) -> None:
        thread = await self.context.service.create_thread(
            self.device,
            "Legacy ambiguous rows",
        )
        first = self.context.store.reserve_turn(
            self.device.id,
            thread.id,
            "client-legacy-ambiguous-first",
        )
        self.assertTrue(self.context.store.mark_turn_dispatching(first.public_id))
        self.context.store.mark_turn_ambiguous(first.public_id)
        first_row = self.context.store.turn_by_public_id(first.public_id)
        with self.context.store._transaction() as conn:
            conn.execute(
                """
                INSERT INTO turns(
                    public_id, raw_id, device_id, thread_public_id,
                    client_message_id, status, delivery_state,
                    attempt_count, created_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, 'ambiguous', 'ambiguous', 1, ?, ?)
                """,
                (
                    "turn_legacy_ambiguous_second",
                    self.device.id,
                    thread.id,
                    "client-legacy-ambiguous-second",
                    first_row["created_at"],
                    first_row["updated_at"],
                ),
            )

        bound = self.context.store.bind_pending_turn(
            thread.id,
            "raw-turn-late-exact-first",
            "client-legacy-ambiguous-first",
        )

        self.assertEqual(first.public_id, bound)
        self.assertTrue(self.context.store.thread_has_ambiguous_turns(thread.id))
        self.assertEqual(
            1,
            self.context.store._conn.execute(
                "SELECT needs_resync FROM threads WHERE public_id = ?",
                (thread.id,),
            ).fetchone()[0],
        )

    async def test_startup_marks_dispatching_without_identity_ambiguous(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Crash dispatch")
        reserved = self.context.store.reserve_turn(
            self.device.id,
            thread.id,
            "client-crash-dispatch",
        )
        self.assertTrue(self.context.store.mark_turn_dispatching(reserved.public_id))

        await self._restart_service()

        row = self.context.store.turn_by_public_id(reserved.public_id)
        self.assertEqual("ambiguous", row["status"])
        with self.assertRaises(GatewayError) as retry:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "client-crash-dispatch",
                [{"type": "text", "text": "Must not resend"}],
            )
        self.assertEqual(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            retry.exception.code,
        )
        self.assertEqual(0, self.context.bridge.start_turn_calls)

    async def test_startup_rebuilds_known_active_or_terminates_idle_unknown(self) -> None:
        thread = await self.context.service.create_thread(self.device, "Known accepted")
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-known-accepted",
            [{"type": "text", "text": "Remain active"}],
        )
        raw_thread_id = self.context.store.thread_raw_id(thread.id)
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=9901,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-orphan-approval",
                    "command": ["publish", "unknown"],
                    "reason": "Must close with the orphan",
                },
            )
        )
        approval = self.context.store._conn.execute(
            "SELECT public_id FROM approvals WHERE turn_public_id = ?",
            (turn.id,),
        ).fetchone()
        self.assertIsNotNone(approval)

        await self._restart_service()
        active = self.context.store.turn_by_public_id(turn.id)
        self.assertEqual("accepted", active["delivery_state"])
        self.assertEqual("inProgress", active["status"])
        with self.assertRaises(GatewayError) as busy:
            await self.context.service.start_turn(
                self.device,
                thread.id,
                "client-while-known-active",
                [{"type": "text", "text": "Busy"}],
            )
        self.assertEqual("THREAD_BUSY", busy.exception.code)

        self.context.bridge.threads[raw_thread_id]["status"] = "idle"
        await self._restart_service()
        unknown = self.context.store.turn_by_public_id(turn.id)
        self.assertEqual("failed", unknown["status"])
        self.assertEqual("terminal", unknown["delivery_state"])
        self.assertEqual("operationOutcomeUnknown", unknown["recovery_reason"])
        thread_row = self.context.store._conn.execute(
            "SELECT status, needs_resync FROM threads WHERE public_id = ?",
            (thread.id,),
        ).fetchone()
        self.assertEqual("idle", thread_row["status"])
        self.assertEqual(0, thread_row["needs_resync"])
        approval_row = self.context.store.approval_by_public_id(
            str(approval["public_id"])
        )
        self.assertEqual("expired", approval_row["state"])
        self.assertEqual("reject", approval_row["decision"])
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        terminal = [
            event
            for event in replay.events
            if event.turn_id == turn.id and event.type == "turn.failed"
        ]
        self.assertEqual(1, len(terminal))
        self.assertEqual(
            "OPERATION_OUTCOME_UNKNOWN",
            terminal[0].payload["error"]["code"],
        )
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in replay.events
                    if event.turn_id == turn.id
                    and event.type == "approval.expired"
                ]
            ),
        )
        detail = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual([], detail.approvals)
        self.assertEqual(
            "failed",
            next(
                message
                for message in detail.messages
                if message.get("clientMessageId") == "client-known-accepted"
            )["state"],
        )
        self.assertTrue(
            any(
                message.get("role") == "assistant"
                and message.get("state") == "failed"
                and "OPERATION_OUTCOME_UNKNOWN"
                in str(message.get("fallback", {}).get("text") or "")
                for message in detail.messages
            )
        )
        replayed = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-known-accepted",
            [{"type": "text", "text": "Remain active"}],
        )
        self.assertEqual(turn.id, replayed.id)
        self.assertEqual("failed", replayed.status)
        after_replay = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual(
            "failed",
            next(
                message
                for message in after_replay.messages
                if message.get("clientMessageId") == "client-known-accepted"
            )["state"],
        )
        next_turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            "client-after-unknown",
            [{"type": "text", "text": "Continue safely"}],
        )
        self.assertNotEqual(turn.id, next_turn.id)


if __name__ == "__main__":
    unittest.main()
