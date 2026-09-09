from __future__ import annotations

import json
import unittest
from pathlib import Path

from cheby_gateway.models import (
    ApprovalDTO,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    EventEnvelope,
    ErrorDTO,
    RichMessageSnapshot,
    StartTurnRequest,
    TurnDTO,
    ThreadDetailResponse,
    UserHistoryMessageSnapshot,
)

from .test_protocol_contract import load_protocol_validator


GOLDEN_DIR = Path(__file__).with_name("goldens")


def load_golden(name: str):
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


class GatewayWireGoldenTests(unittest.TestCase):
    def test_manifest_is_complete_and_every_event_matches_protocol(self) -> None:
        manifest = load_golden("manifest.json")
        expected = {
            "approval_requested_event.json",
            "approval_resolved_event.json",
            "approval_expired_event.json",
            "approval_decision_http.json",
            "rate_limited_http.json",
            "websocket_unauthorized.json",
            "websocket_rate_limited.json",
            "turn_accepted_http.json",
            "turn_lifecycle_transcript.json",
            "thread_recovery_snapshot.json",
        }
        self.assertEqual(expected, set(manifest["cases"]))
        validator, _ = load_protocol_validator()
        for name in (
            "approval_requested_event.json",
            "approval_resolved_event.json",
            "approval_expired_event.json",
        ):
            event = load_golden(name)
            EventEnvelope.model_validate(event)
            self.assertEqual([], validator.validate("event", event), name)
            self.assertNotIn(None, event.values())
            if event["type"] in {"approval.requested", "approval.resolved"}:
                ApprovalDTO.model_validate(event["payload"])
                self.assertEqual(
                    [],
                    validator.validate("approval", event["payload"]),
                    name,
                )

    def test_approval_decision_http_golden_matches_gateway_models(self) -> None:
        golden = load_golden("approval_decision_http.json")
        self.assertEqual("POST", golden["request"]["method"])
        self.assertEqual(200, golden["response"]["status"])
        ApprovalDecisionRequest.model_validate(golden["request"]["body"])
        ApprovalDecisionResponse.model_validate(golden["response"]["body"])

    def test_turn_202_golden_matches_gateway_models(self) -> None:
        golden = load_golden("turn_accepted_http.json")
        self.assertEqual("POST", golden["request"]["method"])
        self.assertEqual(202, golden["response"]["status"])
        StartTurnRequest.model_validate(golden["request"]["body"])
        TurnDTO.model_validate(golden["response"]["body"])

    def test_turn_lifecycle_transcript_keeps_stable_public_correlation(self) -> None:
        golden = load_golden("turn_lifecycle_transcript.json")
        submission = TurnDTO.model_validate(golden["submission"])
        events = [EventEnvelope.model_validate(value) for value in golden["events"]]
        self.assertEqual(
            ["turn.started", "turn.completed"],
            [event.type for event in events],
        )
        self.assertEqual(
            {(submission.thread_id, submission.id)},
            {(event.thread_id, event.turn_id) for event in events},
        )
        serialized = json.dumps(golden)
        self.assertNotIn("raw-thread", serialized)
        self.assertNotIn("raw-turn", serialized)

    def test_websocket_4401_golden_has_no_pre_auth_messages(self) -> None:
        golden = load_golden("websocket_unauthorized.json")
        self.assertTrue(golden["serverAcceptedHandshake"])
        self.assertEqual(4401, golden["closeCode"])
        self.assertEqual([], golden["messagesBeforeClose"])

    def test_rate_limit_goldens_freeze_retry_semantics(self) -> None:
        http = load_golden("rate_limited_http.json")["response"]
        self.assertEqual(429, http["status"])
        self.assertGreaterEqual(int(http["headers"]["Retry-After"]), 1)
        error = ErrorDTO.model_validate(http["body"]["error"])
        self.assertEqual("RATE_LIMITED", error.code)
        self.assertTrue(error.retryable)

        websocket = load_golden("websocket_rate_limited.json")
        self.assertTrue(websocket["serverAcceptedHandshake"])
        self.assertEqual(4429, websocket["closeCode"])
        self.assertRegex(websocket["closeReason"], r"^retry-after=\d+$")
        self.assertEqual([], websocket["messagesBeforeClose"])
        self.assertTrue(websocket["retryable"])

    def test_thread_recovery_snapshot_golden_matches_models_and_contract(self) -> None:
        golden = load_golden("thread_recovery_snapshot.json")
        ThreadDetailResponse.model_validate(golden)
        UserHistoryMessageSnapshot.model_validate(golden["messages"][0])
        RichMessageSnapshot.model_validate(golden["messages"][1])
        ApprovalDTO.model_validate(golden["approvals"][0])
        validator, _ = load_protocol_validator()
        for message in golden["messages"]:
            self.assertEqual([], validator.validate("richMessage", message))
        self.assertEqual(
            [],
            validator.validate("approval", golden["approvals"][0]),
        )


if __name__ == "__main__":
    unittest.main()
