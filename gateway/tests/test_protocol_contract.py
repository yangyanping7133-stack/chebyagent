from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from cheby_gateway.bridge import BridgeEvent
from cheby_gateway.models import ApprovalDecision

from .helpers import make_context, pair


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_protocol_validator():
    module_path = REPO_ROOT / "tools" / "run_protocol_gate.py"
    spec = importlib.util.spec_from_file_location("cheby_protocol_gate", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protocol gate could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contract = json.loads(
        (REPO_ROOT / "contracts" / "v1" / "contracts.json").read_text(
            encoding="utf-8"
        )
    )
    return module.ProtocolValidator(contract), contract


class GatewayProtocolArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_service_events_pass_frozen_protocol_contract(self) -> None:
        context = make_context()
        await context.service.start()
        self.addAsyncCleanup(context.service.close)
        self.addCleanup(context.close)
        _, device = pair(context)
        thread = await context.service.create_thread(device, "Protocol artifact")
        turn = await context.service.start_turn(
            device,
            thread.id,
            "client-protocol-artifact",
            [{"type": "text", "text": "Verify the public stream"}],
        )
        raw_thread_id = context.store.thread_raw_id(thread.id)
        raw_turn_id = context.store.turn_raw_id(turn.id)
        self.assertIsNotNone(raw_thread_id)
        self.assertIsNotNone(raw_turn_id)

        await context.bridge.emit_event(
            BridgeEvent(
                method="item/agentMessage/delta",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-answer",
                    "delta": "Contract-safe response",
                },
            )
        )
        await context.bridge.emit_event(
            BridgeEvent(
                method="turn/plan/updated",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "plan": [
                        {"id": "verify", "label": "Verify", "state": "running"}
                    ],
                },
            )
        )
        await context.bridge.emit_event(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": "raw-item-answer",
                        "type": "agentMessage",
                        "status": "completed",
                        "text": "Contract-safe authoritative response",
                    },
                },
            )
        )
        await context.bridge.emit_event(
            BridgeEvent(
                method="item/commandExecution/requestApproval",
                request_id=901,
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": "raw-item-approval",
                    "command": ["python", "-m", "pytest"],
                    "reason": "Run the verification suite.",
                },
            )
        )
        replay = context.store.replay_events(device.id, device.stream_id, 0)
        approval_event = next(
            event for event in replay.events if event.type == "approval.requested"
        )
        await context.service.resolve_approval(
            device,
            approval_event.payload["approvalId"],
            approval_event.payload["actionToken"],
            ApprovalDecision.APPROVE,
        )
        await context.bridge.emit_event(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {"id": raw_turn_id, "status": "completed"},
                },
            )
        )
        await context.service.patch_thread(
            device,
            thread.id,
            title="Protocol artifact complete",
            archived=None,
        )

        validator, contract = load_protocol_validator()
        replay = context.store.replay_events(device.id, device.stream_id, 0)
        self.assertFalse(replay.sync_required)
        self.assertGreater(len(replay.events), 0)
        allowed_types = set(contract["entities"]["event"]["enums"]["type"])
        for event in replay.events:
            public_event = event.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            )
            self.assertEqual([], validator.validate("event", public_event))
            self.assertIn(public_event["type"], allowed_types)
            self.assertNotIn(None, public_event.values())
            if public_event["type"] == "thread.snapshot":
                self.assertEqual(
                    [],
                    validator.validate("thread", public_event["payload"]),
                )
            elif public_event["type"] == "message.snapshot":
                self.assertEqual(
                    [],
                    validator.validate("richMessage", public_event["payload"]),
                )
            elif public_event["type"] == "message.patch":
                self.assertEqual(
                    [],
                    validator.validate("patch", public_event["payload"]),
                )
            elif public_event["type"] in {
                "approval.requested",
                "approval.resolved",
            }:
                self.assertEqual(
                    [],
                    validator.validate("approval", public_event["payload"]),
                )


if __name__ == "__main__":
    unittest.main()
