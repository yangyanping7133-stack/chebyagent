from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from cheby_gateway.api import create_app
from cheby_gateway.bridge import BridgeDeliveryUnknown, BridgeEvent
from cheby_gateway.projection import bounded_text
from cheby_gateway.store import secret_hash

from .helpers import install_proof_auth, make_context, pair


class ItemAndApprovalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context()
        await self.context.service.start()
        self.pairing, self.device = pair(self.context)

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.assertIsNone(self.context.service._approval_sweeper_task)
        self.context.close()

    async def _start_turn(self, title: str, client_message_id: str):
        thread = await self.context.service.create_thread(self.device, title)
        turn = await self.context.service.start_turn(
            self.device,
            thread.id,
            client_message_id,
            [{"type": "text", "text": "Test recovery"}],
        )
        return (
            thread,
            turn,
            self.context.store.thread_raw_id(thread.id),
            self.context.store.turn_raw_id(turn.id),
        )

    async def test_item_completed_emits_authoritative_terminal_snapshot(self) -> None:
        thread, turn, raw_thread_id, raw_turn_id = await self._start_turn(
            "Authoritative item",
            "client-authoritative-item",
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/agentMessage/delta",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-authoritative",
                    "delta": "partial text",
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
                        "id": "raw-item-authoritative",
                        "type": "agentMessage",
                        "status": "completed",
                        "content": [
                            {"type": "outputText", "text": "authoritative full text"}
                        ],
                    },
                },
            )
        )

        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        snapshots = [
            event
            for event in replay.events
            if event.type == "message.snapshot" and event.turn_id == turn.id
        ]
        self.assertEqual(1, len(snapshots))
        patches = [
            event
            for event in replay.events
            if event.type == "message.patch" and event.turn_id == turn.id
        ]
        self.assertEqual(1, len(patches))
        message_id = snapshots[0].payload["messageId"]
        authoritative = self.context.store.message_snapshot(message_id)
        self.assertEqual("streaming", authoritative["state"])
        self.assertEqual(
            "authoritative full text",
            authoritative["blocks"]["activity"]["text"],
        )
        persisted = self.context.store.message_snapshot(message_id)
        self.assertEqual(authoritative, persisted)
        self.assertEqual([0], [patch.payload["baseRevision"] for patch in patches])
        self.assertEqual([1], [patch.payload["nextRevision"] for patch in patches])
        self.assertFalse(
            any(event.type == "turn.completed" for event in replay.events)
        )

    async def test_expiry_sweep_rejects_once_and_emits_expired(self) -> None:
        thread, turn, raw_thread_id, raw_turn_id = await self._start_turn(
            "Expiry",
            "client-approval-expiry",
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=1201,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-expiry",
                    "command": ["publish", "result"],
                    "reason": "External side effect",
                },
            )
        )

        first = await self.context.service.sweep_expired_approvals(
            now="2999-01-01T00:00:00Z"
        )
        second = await self.context.service.sweep_expired_approvals(
            now="2999-01-01T00:00:01Z"
        )

        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertEqual(1, len(self.context.bridge.approval_responses))
        self.assertEqual("reject", next(iter(self.context.bridge.approval_responses.values())))
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        expired = [event for event in replay.events if event.type == "approval.expired"]
        self.assertEqual(1, len(expired))
        approval_id = expired[0].payload["approvalId"]
        row = self.context.store.approval_by_public_id(approval_id)
        self.assertEqual("expired", row["state"])
        self.assertEqual("reject", row["decision"])
        self.assertEqual(thread.id, expired[0].thread_id)
        self.assertEqual(turn.id, expired[0].turn_id)
        detail = await self.context.service.read_thread(thread.id, self.device)
        self.assertEqual([], detail.approvals)

    async def test_expiry_delivery_unknown_is_terminal_and_never_retried(self) -> None:
        _, _, raw_thread_id, raw_turn_id = await self._start_turn(
            "Expiry fail closed",
            "client-approval-expiry-unknown",
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/fileChange/requestApproval",
                request_id=1202,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-expiry-unknown",
                    "reason": "External file mutation",
                },
            )
        )
        calls = 0

        async def delivery_unknown(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise BridgeDeliveryUnknown("response delivery unknown")

        self.context.bridge.resolve_approval = delivery_unknown
        first = await self.context.service.sweep_expired_approvals(
            now="2999-01-01T00:00:00Z"
        )
        second = await self.context.service.sweep_expired_approvals(
            now="2999-01-01T00:00:01Z"
        )

        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertEqual(1, calls)

    async def test_approval_fields_redact_whole_values_before_utf8_budgets(self) -> None:
        thread, _, raw_thread_id, raw_turn_id = await self._start_turn(
            "Approval privacy",
            "client-approval-privacy",
        )
        split_boundary_secret = "sk-" + "A" * 80
        jwt = "eyJabcdefghij.abcdefghij.abcdefghij"
        local_path = "/Users/alice/Private Project/keys.txt"
        command = "x" * 1006 + split_boundary_secret + " " + local_path
        internal_url = "http://127.0.0.1:3111/private"
        credential_url = "https://api.example.com/cb?access_token=url-secret-value"
        reason = (
            internal_url
            + " "
            + credential_url
            + " "
            + ("理由🔐" * 700)
            + " Bearer "
            + jwt
            + " "
            + local_path
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=1203,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-privacy",
                    "command": [command],
                    "reason": reason,
                },
            )
        )

        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        event = next(value for value in replay.events if value.type == "approval.requested")
        summary = event.payload["summary"]
        public_reason = event.payload["reason"]
        combined = summary + public_reason
        self.assertNotIn("sk-", combined)
        self.assertNotIn(jwt, combined)
        self.assertNotIn("/Users/", combined)
        self.assertNotIn("127.0.0.1", combined)
        self.assertNotIn("url-secret-value", combined)
        self.assertIn("[INTERNAL_URL]", combined)
        self.assertIn("[REDACTED]", combined)
        self.assertLessEqual(len(summary.encode("utf-8")), 1024)
        self.assertLessEqual(len(public_reason.encode("utf-8")), 2048)
        self.assertLessEqual(len(combined.encode("utf-8")), 3072)
        combined.encode("utf-8").decode("utf-8")

        detail = await self.context.service.read_thread(thread.id, self.device)
        approval = detail.approvals[0]
        self.assertEqual(summary, approval.summary)
        self.assertEqual(public_reason, approval.reason)
        dump = "\n".join(self.context.store._conn.iterdump())
        self.assertNotIn(split_boundary_secret, dump)
        self.assertNotIn(jwt, dump)
        self.assertNotIn(local_path, dump)
        self.assertNotIn(internal_url, dump)
        self.assertNotIn("url-secret-value", dump)

    async def test_named_quoted_credentials_and_unclosed_quotes_fail_safe(self) -> None:
        summary, reason = self.context.service._sanitize_approval_public_fields(
            'deploy password="correct horse battery staple" '
            "token='alpha beta gamma' api_key=plain-value",
            'secret="unterminated secret with spaces and /Users/alice/key.txt',
        )
        self.assertEqual(
            "deploy password=[REDACTED] token=[REDACTED] api_key=[REDACTED]",
            summary,
        )
        self.assertEqual("secret=[REDACTED]", reason)
        combined = summary + reason
        for forbidden in (
            "correct horse battery staple",
            "alpha beta gamma",
            "plain-value",
            "unterminated secret",
            "/Users/alice",
        ):
            self.assertNotIn(forbidden, combined)

    async def test_lone_unicode_surrogates_are_replaced_before_persistence(self) -> None:
        safe, _ = bounded_text("before\ud800after")
        self.assertEqual("before\ufffdafter", safe)
        safe.encode("utf-8")

        _, _, raw_thread_id, raw_turn_id = await self._start_turn(
            "Approval invalid unicode",
            "client-approval-invalid-unicode",
        )
        password = "surrogate password with spaces"
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=1204,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-invalid-unicode",
                    "command": ['run\ud800 password="%s"' % password],
                    "reason": "reason\udfff remains readable",
                },
            )
        )
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        event = next(
            value
            for value in replay.events
            if value.type == "approval.requested"
            and value.payload["itemId"]
            != ""
        )
        serialized = json.dumps(event.payload, ensure_ascii=False)
        serialized.encode("utf-8")
        self.assertNotIn(password, serialized)
        self.assertNotRegex(serialized, "[\ud800-\udfff]")
        self.assertIn("\ufffd", serialized)

    async def test_restart_atomically_sanitizes_pending_approval_and_outbox(self) -> None:
        _, _, raw_thread_id, raw_turn_id = await self._start_turn(
            "Approval startup migration",
            "client-approval-startup-migration",
        )
        await self.context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=1205,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-startup-migration",
                    "command": ["safe initial command"],
                    "reason": "safe initial reason",
                },
            )
        )
        before = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        requested = next(
            event for event in before.events if event.type == "approval.requested"
        )
        approval_id = requested.payload["approvalId"]
        old_password = "legacy quoted password with spaces"
        old_token = "legacy quoted token with spaces"
        old_unclosed = "legacy unterminated secret with spaces"
        old_action_token = "legacy-action-token-plaintext"
        old_summary = (
            'deploy password="%s" token=\'%s\'' % (old_password, old_token)
        )
        old_reason = 'secret="%s' % old_unclosed
        old_payload = dict(requested.payload)
        old_payload.update(
            {
                "summary": old_summary,
                "reason": old_reason,
                "actionToken": old_action_token,
                "unsafeDebug": old_password,
            }
        )
        self.context.store._conn.execute(
            """
            UPDATE approvals SET summary = ?, reason = ?, action_token_hash = ?
            WHERE public_id = ?
            """,
            (old_summary, old_reason, "legacy-hash", approval_id),
        )
        self.context.store._conn.execute(
            """
            UPDATE event_outbox SET payload_json = ?
            WHERE type = 'approval.requested' AND seq = ?
            """,
            (
                json.dumps(old_payload, ensure_ascii=False, separators=(",", ":")),
                requested.seq,
            ),
        )

        await self.context.service.close()
        await self.context.service.start()

        expected_action_token = self.context.service._approval_action_token(approval_id)
        row = self.context.store.approval_by_public_id(approval_id)
        self.assertEqual("deploy password=[REDACTED] token=[REDACTED]", row["summary"])
        self.assertEqual("secret=[REDACTED]", row["reason"])
        self.assertEqual(secret_hash(expected_action_token), row["action_token_hash"])

        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            0,
        )
        repaired = next(
            event
            for event in replay.events
            if event.type == "approval.requested"
            and event.payload["approvalId"] == approval_id
        )
        self.assertEqual(expected_action_token, repaired.payload["actionToken"])
        self.assertNotIn("unsafeDebug", repaired.payload)
        public_replay = json.dumps(
            [
                event.model_dump(by_alias=True, exclude_none=True, mode="json")
                for event in replay.events
            ],
            ensure_ascii=False,
        )
        logical_database = "\n".join(self.context.store._conn.iterdump())
        for forbidden in (
            old_password,
            old_token,
            old_unclosed,
            old_action_token,
            old_summary,
            old_reason,
        ):
            self.assertNotIn(forbidden, public_replay)
            self.assertNotIn(forbidden, logical_database)

    async def test_upgrade_repairs_all_terminal_pending_and_orphan_replay(self) -> None:
        thread, turn, _, _ = await self._start_turn(
            "Approval retained replay upgrade",
            "client-approval-retained-upgrade",
        )
        orphan_secret = "orphan retained approval secret"
        orphan = self.context.store.append_event(
            self.device.id,
            "approval.requested",
            {
                "approvalId": "approval_orphan_upgrade",
                "summary": orphan_secret,
                "unsafeExtra": orphan_secret,
            },
            thread_id=thread.id,
            turn_id=turn.id,
            item_id="item_orphan_upgrade",
        )
        corrupt_secret = "corrupt retained approval secret"
        corrupt = self.context.store.append_event(
            self.device.id,
            "approval.requested",
            {"approvalId": "approval_corrupt_upgrade"},
            thread_id=thread.id,
            turn_id=turn.id,
            item_id="item_corrupt_upgrade",
        )
        self.context.store._conn.execute(
            """
            UPDATE event_outbox SET payload_json = ?
            WHERE stream_id = ? AND seq = ?
            """,
            (
                '{"approvalId":"approval_corrupt_upgrade","secret":"%s"'
                % corrupt_secret,
                self.device.stream_id,
                corrupt.seq,
            ),
        )
        removed_floor = max(orphan.seq, corrupt.seq)

        statuses = ("pending", "approved", "rejected", "expired")
        secrets_by_status = {}
        old_tokens = {}
        for index, state in enumerate(statuses):
            approval_id = "approval_upgrade_%s" % state
            value_secret = "%s quoted credential with spaces" % state
            reason_secret = "%s reason token with spaces" % state
            old_token = "old-action-token-%s" % state
            secrets_by_status[state] = (value_secret, reason_secret)
            old_tokens[state] = old_token
            summary = 'deploy password="%s"' % value_secret
            reason = "token='%s'" % reason_secret
            old_payload = {
                "approvalId": approval_id,
                "threadId": thread.id,
                "turnId": turn.id,
                "itemId": "item_upgrade_%s" % state,
                "kind": "command",
                "summary": summary,
                "reason": reason,
                "decisions": ["approve", "reject"],
                "state": "pending",
                "expiresAt": "2999-01-01T00:00:00Z",
                "actionToken": old_token,
                "unsafeExtra": value_secret,
            }
            self.context.store.create_approval(
                approval_id=approval_id,
                raw_request_id=json.dumps("raw-request-upgrade-%d" % index),
                thread_id=thread.id,
                turn_id=turn.id,
                item_id="item_upgrade_%s" % state,
                kind="command",
                summary=summary,
                reason=reason,
                decisions=["approve", "reject"],
                action_token=old_token,
                expires_at="2999-01-01T00:00:00Z",
                device_id=self.device.id,
                events=[{"type": "approval.requested", "payload": old_payload}],
            )
            if state != "pending":
                self.context.store._conn.execute(
                    """
                    UPDATE approvals SET state = ?, decision = ?, resolved_at = ?
                    WHERE public_id = ?
                    """,
                    (
                        state,
                        "approve" if state == "approved" else "reject",
                        "2026-07-19T00:00:00Z",
                        approval_id,
                    ),
                )
                terminal_payload = {
                    **old_payload,
                    "state": state,
                    "unsafeExtra": reason_secret,
                }
                self.context.store.append_event(
                    self.device.id,
                    (
                        "approval.expired"
                        if state == "expired"
                        else "approval.resolved"
                    ),
                    terminal_payload,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id="item_upgrade_%s" % state,
                )

        later = self.context.store.append_event(
            self.device.id,
            "audit.action",
            {"action": "after.approval.repair"},
            thread_id=thread.id,
            turn_id=turn.id,
        )
        await self.context.service.close()

        app = create_app(
            self.context.settings,
            self.context.store,
            self.context.bridge,
        )
        with TestClient(app) as client:
            install_proof_auth(client, self.context)
            authorization = {
                "Authorization": "Bearer " + self.pairing.access_token,
            }
            gap_path = "/v1/events?streamId=%s&afterSeq=0" % self.device.stream_id
            with client.websocket_connect(gap_path, headers=authorization) as socket:
                gap = socket.receive_json()
                self.assertEqual("sync.required", gap["type"])
                self.assertEqual("eventGap", gap["payload"]["reason"])

            replay = self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                removed_floor,
            )
            self.assertFalse(replay.sync_required)
            resume_path = "/v1/events?streamId=%s&afterSeq=%d" % (
                self.device.stream_id,
                removed_floor,
            )
            with client.websocket_connect(resume_path, headers=authorization) as socket:
                wire_events = [socket.receive_json() for _ in replay.events]

            serialized_wire = json.dumps(wire_events, ensure_ascii=False)
            logical_database = "\n".join(self.context.store._conn.iterdump())
            for forbidden in (orphan_secret, corrupt_secret, *old_tokens.values()):
                self.assertNotIn(forbidden, serialized_wire)
                self.assertNotIn(forbidden, logical_database)
            for values in secrets_by_status.values():
                for forbidden in values:
                    self.assertNotIn(forbidden, serialized_wire)
                    self.assertNotIn(forbidden, logical_database)

            requested_events = {
                event["payload"]["approvalId"]: event["payload"]
                for event in wire_events
                if event["type"] == "approval.requested"
            }
            approval_ids = {
                state: "approval_upgrade_%s" % state for state in statuses
            }
            self.assertEqual(set(approval_ids.values()), set(requested_events))
            strict_keys = {
                "approvalId",
                "threadId",
                "turnId",
                "itemId",
                "kind",
                "summary",
                "reason",
                "decisions",
                "state",
                "expiresAt",
            }
            requested_seq = {}
            for current_state, approval_id in approval_ids.items():
                payload = requested_events[approval_id]
                expected_keys = strict_keys | (
                    {"actionToken"} if current_state == "pending" else set()
                )
                self.assertEqual(expected_keys, set(payload))
                self.assertNotIn("unsafeExtra", payload)
                # ContractV1Codec.decodeApprovalRequested accepts only pending;
                # actionToken is optional so historical terminal requests stay
                # replayable without restoring a usable credential.
                self.assertEqual("pending", payload["state"])
                self.assertTrue(payload["decisions"])
                self.assertTrue(
                    set(payload["decisions"]).issubset({"approve", "reject"})
                )
                if current_state != "pending":
                    self.assertNotIn("actionToken", payload)

                requested_seq[approval_id] = next(
                    event["seq"]
                    for event in wire_events
                    if event["type"] == "approval.requested"
                    and event["payload"]["approvalId"] == approval_id
                )

            terminal_events = {
                event["payload"]["approvalId"]: event
                for event in wire_events
                if event["type"] in {"approval.resolved", "approval.expired"}
            }
            self.assertEqual(
                {approval_ids[state] for state in statuses if state != "pending"},
                set(terminal_events),
            )
            for state in ("approved", "rejected"):
                approval_id = approval_ids[state]
                terminal = terminal_events[approval_id]
                self.assertEqual("approval.resolved", terminal["type"])
                self.assertEqual(state, terminal["payload"]["state"])
                self.assertNotIn("actionToken", terminal["payload"])
                self.assertGreater(terminal["seq"], requested_seq[approval_id])
            expired_id = approval_ids["expired"]
            self.assertEqual("approval.expired", terminal_events[expired_id]["type"])
            self.assertEqual(
                {"approvalId": expired_id}, terminal_events[expired_id]["payload"]
            )
            self.assertGreater(
                terminal_events[expired_id]["seq"], requested_seq[expired_id]
            )
            self.assertTrue(any(event["seq"] == later.seq for event in wire_events))


if __name__ == "__main__":
    unittest.main()
