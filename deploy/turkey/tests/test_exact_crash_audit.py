from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest


PROGRAM = Path(__file__).resolve().parents[1] / "audit_gate_business_keys.py"
SPEC = importlib.util.spec_from_file_location("exact_crash_audit", PROGRAM)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


RUN_TOKEN = "c" * 32


def exact_matrix():
    tuples = set()
    markers = {}
    relay_sequences = {}
    effect_sequences = {}
    sequence = 0
    for boundary in audit.EXACT_CRASH_BOUNDARIES:
        for iteration in range(1, audit.EXACT_CRASH_REPETITIONS + 1):
            thread_a = f"thread-{boundary}-{iteration}-a"
            thread_b = f"thread-{boundary}-{iteration}-b"
            values = {
                "A1": audit.BusinessTuple(
                    thread_a,
                    f"client-{boundary}-{iteration}-a1",
                    f"turn-{boundary}-{iteration}-a1",
                ),
                "A2": audit.BusinessTuple(
                    thread_a,
                    f"client-{boundary}-{iteration}-a2",
                    f"turn-{boundary}-{iteration}-a2",
                ),
                "B1": audit.BusinessTuple(
                    thread_b,
                    f"client-{boundary}-{iteration}-b1",
                    f"turn-{boundary}-{iteration}-b1",
                ),
            }
            # B1 may legitimately race before or after A1. A2 must remain after A1.
            for label in ("B1", "A1", "A2"):
                sequence += 1
                value = values[label]
                tuples.add(value)
                markers[value] = (
                    f"EXACT-{RUN_TOKEN}-{boundary}-{iteration}-{label}"
                )
                relay_sequences[value] = sequence
                effect_sequences[value] = sequence
    relay = audit.RelayAudit(
        tuples=frozenset(tuples),
        commands={},
        markers=markers,
        delivery_sequences=relay_sequences,
        response_message_ids={},
        relevant_message_ids=frozenset(),
        max_node_delivery_seq=sequence,
        assistant_id="assistant",
        device_id="device",
        node_id="node",
        active_generation=1,
    )
    fake = audit.FakeAudit(
        tuples=frozenset(tuples),
        markers=markers,
        effect_sequences=effect_sequences,
    )
    return relay, fake


def test_exact_crash_contract_is_fixed() -> None:
    assert audit.EXACT_CRASH_EXPECTED_CYCLES == 125
    assert audit.EXACT_CRASH_EXPECTED_TURNS == 375
    assert audit.EXACT_CRASH_EXPECTED_THREADS == 250
    assert audit.EXACT_CRASH_CONTRACT.expected_turns == 375
    assert audit.EXACT_CRASH_CONTRACT.expected_threads == 250


def test_fixed_exact_crash_contract_reaches_read_only_data_validation(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    paths = audit.AuditPaths(
        relay_db=missing / "relay.sqlite3",
        connector_db=missing / "connector.sqlite3",
        gateway_db=missing / "gateway.sqlite3",
        fake_state=missing / "fake.json",
        relay_uid=os.geteuid(),
        connector_uid=os.geteuid(),
    )
    with pytest.raises(audit.AuditFailure, match="audit input is unavailable"):
        audit.perform_exact_crash_audit(paths, RUN_TOKEN)


def test_forged_exact_crash_contract_is_rejected_before_data_access(
    tmp_path: Path,
) -> None:
    forged = replace(
        audit.EXACT_CRASH_CONTRACT,
        schema="chebycodex.gate-exact-crash-audit.forged",
    )
    missing = tmp_path / "missing"
    paths = audit.AuditPaths(
        relay_db=missing / "relay.sqlite3",
        connector_db=missing / "connector.sqlite3",
        gateway_db=missing / "gateway.sqlite3",
        fake_state=missing / "fake.json",
        relay_uid=os.geteuid(),
        connector_uid=os.geteuid(),
    )
    with pytest.raises(audit.AuditFailure, match="fixed Gate modes"):
        audit.perform_audit(paths, RUN_TOKEN, forged)


def test_exact_crash_matrix_accepts_cross_thread_race_but_proves_fifo() -> None:
    relay, fake = exact_matrix()
    counts = audit.audit_exact_crash_matrix(relay, fake, RUN_TOKEN)
    assert counts == {
        "cycles": 125,
        "businessKeys": 375,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }


def test_exact_crash_matrix_rejects_same_thread_fifo_bypass() -> None:
    relay, fake = exact_matrix()
    a1 = next(
        key
        for key, marker in relay.markers.items()
        if marker.endswith("-ENQUEUE-1-A1")
    )
    a2 = next(
        key
        for key, marker in relay.markers.items()
        if marker.endswith("-ENQUEUE-1-A2")
    )
    broken_sequences = dict(relay.delivery_sequences)
    broken_sequences[a1], broken_sequences[a2] = (
        broken_sequences[a2],
        broken_sequences[a1],
    )
    with pytest.raises(audit.AuditFailure, match="FIFO"):
        audit.audit_exact_crash_matrix(
            replace(relay, delivery_sequences=broken_sequences),
            fake,
            RUN_TOKEN,
        )


def test_exact_crash_matrix_rejects_missing_effect() -> None:
    relay, fake = exact_matrix()
    missing = next(iter(fake.markers))
    broken_markers = dict(fake.markers)
    broken_markers.pop(missing)
    with pytest.raises(audit.AuditFailure, match="incomplete"):
        audit.audit_exact_crash_matrix(
            relay,
            replace(fake, markers=broken_markers),
            RUN_TOKEN,
        )


def test_cli_exposes_only_fixed_exact_crash_mode() -> None:
    parser = audit.create_parser()
    args = parser.parse_args(["--mode", "exact-crash", "--run-token", RUN_TOKEN])
    assert args.mode == "exact-crash"
    assert not hasattr(args, "turns")
    assert not hasattr(args, "threads")
    assert not hasattr(args, "repetitions")
