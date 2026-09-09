from __future__ import annotations

import json
from pathlib import Path

from cheby_relay.models import INBOUND_FRAME_ADAPTER, Operation


REPOSITORY = Path(__file__).resolve().parents[2]


def test_golden_client_frames_validate_against_executable_contract():
    goldens = json.loads(
        (REPOSITORY / "contracts/relay-v1/golden-frames.json").read_text()
    )
    for name in ("command", "response", "event", "ack", "ping"):
        INBOUND_FRAME_ADAPTER.validate_python(goldens[name])


def test_frozen_operation_manifest_matches_executable_enum():
    manifest = json.loads(
        (REPOSITORY / "contracts/relay-v1/operations.json").read_text()
    )
    assert manifest["schema"] == "cheby.relay-operations/1"
    assert set(manifest["operations"]) == {operation.value for operation in Operation}
