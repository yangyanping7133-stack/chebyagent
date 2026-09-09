from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import android_exact_crash_gate as gate


RUN_TOKEN = "a" * 32
NONCE = "b" * 32


def key(
    thread: str,
    client: str,
    state: str,
    *,
    turn: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "scope": "scope-fixed",
        "generation": 0,
        "threadId": thread,
        "clientMessageId": client,
        "state": state,
    }
    if turn is not None:
        value["turnId"] = turn
    return value


def key_value(raw: dict[str, object]) -> gate.BusinessKey:
    return gate.BusinessKey(
        str(raw["scope"]),
        int(raw["generation"]),
        str(raw["threadId"]),
        str(raw["clientMessageId"]),
    )


def checkpoint_document(boundary: str) -> dict[str, object]:
    state = {
        "ENQUEUE": "QUEUED",
        "NETWORK": "QUEUED",
        "ACCEPTED": "QUEUED",
        "ANDROID": "ACCEPTED",
        "CLEANED": "ABSENT",
    }[boundary]
    turn = "turn-a1" if boundary in {"ANDROID", "CLEANED"} else None
    target = key("thread-a", "client-a1", state, turn=turn)
    pending = [
        key("thread-a", "client-a2", "QUEUED"),
        key("thread-b", "client-b1", "QUEUED"),
    ]
    return {
        "schema": gate.CHECKPOINT_SCHEMA,
        "runToken": RUN_TOKEN,
        "boundary": boundary,
        "iteration": 1,
        "nonce": NONCE,
        "pid": 1234,
        "target": target,
        "pending": pending,
        "businessKeySha256": gate.digest_business_keys(
            [key_value(target), *(key_value(value) for value in pending)]
        ),
        "normalJunitMustNotReturn": True,
    }


def parsed_checkpoint(boundary: str = "ANDROID") -> gate.Checkpoint:
    return gate.parse_checkpoint(
        checkpoint_document(boundary),
        run_token=RUN_TOKEN,
        boundary=boundary,
        iteration=1,
        nonce=NONCE,
    )


def recovery_document(
    checkpoint: gate.Checkpoint,
) -> dict[str, object]:
    rows = []
    labels = ("A1", "A2", "B1")
    keys = (checkpoint.target, *checkpoint.pending)
    tuples: list[gate.BusinessTuple] = []
    for index, (label, business_key) in enumerate(zip(labels, keys), start=1):
        turn = f"turn-{index}"
        rows.append(
            {
                "label": label,
                "scope": business_key.scope,
                "generation": business_key.generation,
                "threadId": business_key.thread_id,
                "clientMessageId": business_key.client_message_id,
                "turnId": turn,
                "turnCount": 1,
            }
        )
        tuples.append(
            gate.BusinessTuple(
                business_key.thread_id,
                business_key.client_message_id,
                turn,
            )
        )
    return {
        "schema": gate.RECOVERY_SCHEMA,
        "runToken": RUN_TOKEN,
        "boundary": checkpoint.boundary,
        "iteration": checkpoint.iteration,
        "nonce": checkpoint.nonce,
        "killedPid": checkpoint.pid,
        "messages": rows,
        "tupleSha256": gate.digest_tuples(tuples),
        "businessKeySha256": gate.digest_business_keys(keys),
        "outboxEntries": 0,
        "duplicateBubbles": 0,
        "sameThreadFifo": True,
        "otherThreadIndependent": True,
    }


@pytest.mark.parametrize("boundary", gate.BOUNDARIES)
def test_exact_checkpoint_accepts_all_five_fixed_boundaries(boundary: str) -> None:
    checkpoint = parsed_checkpoint(boundary)
    assert checkpoint.boundary == boundary
    assert checkpoint.target_state == checkpoint_document(boundary)["target"]["state"]
    assert len(checkpoint.pending) == 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["target"].update(state="ACCEPTED"), "state"),
        (lambda value: value.update(pending=value["pending"][:1]), "pending"),
        (
            lambda value: value["pending"][1].update(threadId="thread-a"),
            "isolated",
        ),
        (
            lambda value: value.update(businessKeySha256="0" * 64),
            "digest",
        ),
        (lambda value: value["target"].update(turnId="turn-early"), "Turn"),
        (
            lambda value: value["pending"][0].update(state="ACCEPTED"),
            "QUEUED",
        ),
    ],
)
def test_checkpoint_negative_controls_fail(
    mutation,
    message: str,
) -> None:
    document = checkpoint_document("NETWORK")
    mutation(document)
    with pytest.raises(gate.GateError, match=message):
        gate.parse_checkpoint(
            document,
            run_token=RUN_TOKEN,
            boundary="NETWORK",
            iteration=1,
            nonce=NONCE,
        )


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(gate.GateError, match="duplicate"):
        gate.strict_json(b'{"schema":"a","schema":"b"}', "fixture")


def test_recovery_proves_exact_business_keys_and_one_turn_each() -> None:
    checkpoint = parsed_checkpoint()
    recovery = gate.parse_recovery(
        recovery_document(checkpoint),
        checkpoint,
        RUN_TOKEN,
    )
    assert len(recovery.tuples) == 3
    assert recovery.tuple_sha256 == gate.digest_tuples(recovery.tuples)
    assert recovery.business_key_sha256 == gate.digest_business_keys(recovery.keys)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["messages"][0].update(turnCount=2),
            "count",
        ),
        (
            lambda value: value["messages"][1].update(clientMessageId="wrong-client"),
            "business key",
        ),
        (
            lambda value: value["messages"][1].update(turnId="turn-1"),
            "duplicate",
        ),
        (
            lambda value: value.update(outboxEntries=1),
            "invariants",
        ),
        (
            lambda value: value.update(tupleSha256="0" * 64),
            "digest",
        ),
        (
            lambda value: value.update(otherThreadIndependent=False),
            "invariants",
        ),
    ],
)
def test_recovery_negative_controls_fail(mutation, message: str) -> None:
    checkpoint = parsed_checkpoint()
    document = recovery_document(checkpoint)
    mutation(document)
    with pytest.raises(gate.GateError, match=message):
        gate.parse_recovery(document, checkpoint, RUN_TOKEN)


def test_instrumentation_command_is_fixed_and_has_no_screen_operation() -> None:
    command = gate.instrumentation_command(
        Path("/opt/android/adb"),
        "SERIAL",
        phase="prepare",
        run_token=RUN_TOKEN,
        boundary="ENQUEUE",
        iteration=25,
        nonce=NONCE,
        expected_reply=gate.relay_gate.GATE_REPLY_MARKER,
    )
    joined = " ".join(command)
    assert "am instrument" in joined
    assert gate.PREPARE_METHOD in joined
    assert "input tap" not in joined
    assert "input text" not in joined
    assert "monkey" not in joined
    assert "am start" not in joined


def test_force_stop_targets_only_isolated_gate_packages(monkeypatch) -> None:
    commands: list[list[str]] = []

    def bounded(command, timeout_seconds, label):
        commands.append(list(command))
        return b""

    monkeypatch.setattr(gate.relay_gate, "run_bounded", bounded)
    gate.force_stop(Path("/opt/android/adb"), "SERIAL")
    assert [command[-1] for command in commands] == [
        gate.TARGET_PACKAGE,
        gate.TEST_PACKAGE,
    ]
    assert all(command[-3:-1] == ["am", "force-stop"] for command in commands)


def test_recovery_status_requires_exact_protocol() -> None:
    checkpoint = parsed_checkpoint()
    recovery = gate.parse_recovery(
        recovery_document(checkpoint),
        checkpoint,
        RUN_TOKEN,
    )
    evidence = gate.InstrumentationOutput(
        values={
            "cheby_exact_checkpoint": "exact_crash_recovered",
            "cheby_exact_boundary": checkpoint.boundary,
            "cheby_exact_run_token": RUN_TOKEN,
            "cheby_exact_iteration": "1",
            "cheby_exact_turns": "3",
            "cheby_exact_threads": "2",
            "cheby_exact_outbox_entries": "0",
            "cheby_exact_duplicate_bubbles": "0",
            "cheby_exact_same_thread_fifo": "true",
            "cheby_exact_other_thread_independent": "true",
            "cheby_exact_tuple_sha256": recovery.tuple_sha256,
            "cheby_exact_business_key_sha256": recovery.business_key_sha256,
        },
        status_codes=[1, 2, 0],
        final_code=-1,
        junit_ok=True,
    )
    gate.validate_recovery_output(evidence, checkpoint, RUN_TOKEN)
    evidence.status_codes = [1, 0]
    with pytest.raises(gate.GateError, match="clean pass"):
        gate.validate_recovery_output(evidence, checkpoint, RUN_TOKEN)


def test_prepare_negative_controls_reject_object_reconstruction_and_live_process() -> None:
    checkpoint = parsed_checkpoint()
    normal = gate.InstrumentationOutput(
        final_code=-1,
        junit_ok=True,
    )
    with pytest.raises(gate.GateError, match="normal JUnit"):
        gate.validate_live_prepare_checkpoint(
            normal,
            process_returncode=None,
            live_pid=checkpoint.pid,
            checkpoint=checkpoint,
        )
    with pytest.raises(gate.GateError, match="ended normally"):
        gate.validate_live_prepare_checkpoint(
            gate.InstrumentationOutput(),
            process_returncode=0,
            live_pid=None,
            checkpoint=checkpoint,
        )
    with pytest.raises(gate.GateError, match="PID"):
        gate.validate_live_prepare_checkpoint(
            gate.InstrumentationOutput(),
            process_returncode=None,
            live_pid=checkpoint.pid + 1,
            checkpoint=checkpoint,
        )
    with pytest.raises(gate.GateError, match="survived"):
        gate.validate_killed_prepare(
            gate.InstrumentationOutput(),
            live_pid_after=checkpoint.pid,
        )
    with pytest.raises(gate.GateError, match="misreported"):
        gate.validate_killed_prepare(
            normal,
            live_pid_after=None,
        )


def test_exact_gate_constants_cannot_be_lowered_by_cli() -> None:
    options = {action.dest for action in gate.parser()._actions}
    assert "repetitions" not in options
    assert "boundaries" not in options
    assert gate.REPETITIONS == 25
    assert gate.EXPECTED_CYCLES == 125
    assert gate.EXPECTED_TURNS == 375
    assert gate.EXPECTED_THREADS == 250


def test_exact_crash_runbook_has_fixed_runner_and_screen_ban() -> None:
    root = TOOLS.parents[1]
    source = (
        root / "docs/testing/ANDROID_EXACT_CRASH_GATE.md"
    ).read_text(encoding="utf-8")
    section = source.split(
        "## Preconditions and runner entry",
        maxsplit=1,
    )[1].split("## Public ten-boundary mapping", maxsplit=1)[0]
    commands = "\n".join(
        re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    )

    assert "unconsumed one-use CXC1" in section
    assert "do not run `pm clear`" in section
    assert "/usr/local/sbin/chebycodex-audit-gate-business-keys" in section
    assert "support `--mode exact-crash`" in section
    assert "python3 tools/gate/android_exact_crash_gate.py" in commands
    for argument in (
        "--adb",
        "--serial",
        "--gate-apk",
        "--test-apk",
        "--ssh",
        "--ssh-host",
        "--expected-reply",
        "--manifest",
    ):
        assert argument in commands
    assert "--repetitions" not in commands
    assert "--boundaries" not in commands
    assert re.search(
        r"exactly\s+125 live prepare checkpoints",
        section,
    )
    assert "Mac must not operate the phone screen" in section
    for forbidden in (
        " pm clear ",
        " shell input ",
        " input tap ",
        " input text ",
        " keyevent ",
        " uiautomator ",
        " monkey ",
        " am start ",
    ):
        assert forbidden not in f" {commands.lower()} "


def test_exact_runner_rejects_existing_and_symlink_manifest_before_execute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    existing = evidence / "existing.json"
    existing.write_text("preserve-existing\n", encoding="utf-8")
    executed = False

    def forbidden_execute(args):
        nonlocal executed
        executed = True
        raise AssertionError("execute must not run")

    monkeypatch.setattr(gate, "execute", forbidden_execute)
    arguments = [
        "--adb",
        "/absolute/adb",
        "--serial",
        "SERIAL-1",
        "--gate-apk",
        "/absolute/gate.apk",
        "--test-apk",
        "/absolute/test.apk",
        "--manifest",
        str(existing.resolve()),
    ]
    assert gate.main(arguments) == 2
    assert executed is False
    assert existing.read_text(encoding="utf-8") == "preserve-existing\n"

    victim = evidence / "victim.json"
    victim.write_text("preserve-victim\n", encoding="utf-8")
    linked = evidence / "linked.json"
    linked.symlink_to(victim)
    arguments[-1] = str(linked.absolute())
    assert gate.main(arguments) == 2
    assert executed is False
    assert linked.is_symlink()
    assert victim.read_text(encoding="utf-8") == "preserve-victim\n"


def test_manifest_host_counts_are_derived_from_observed_evidence() -> None:
    checkpoint_digests = ["1" * 64, "2" * 64]
    recoveries = [
        gate.Recovery((), (), "3" * 64, "4" * 64, 0, 0),
        gate.Recovery((), (), "5" * 64, "6" * 64, 0, 0),
    ]
    tuples = [
        gate.BusinessTuple("thread-a", "client-a1", "turn-a1"),
        gate.BusinessTuple("thread-a", "client-a2", "turn-a2"),
        gate.BusinessTuple("thread-b", "client-b1", "turn-b1"),
    ]
    keys = [
        gate.BusinessKey("scope", 1, value.thread_id, value.client_message_id)
        for value in tuples
    ]
    assert gate.observed_host_counts(
        {"ENQUEUE": 1, "NETWORK": 1},
        checkpoint_digests,
        recoveries,
        tuples,
        keys,
    ) == {
        "cycles": 2,
        "hostForceStops": 2,
        "abnormalPrepareTerminations": 2,
        "independentRecoveryProcesses": 2,
        "turns": 3,
        "threads": 2,
    }


def test_shipping_code_guards_non_noop_hook_and_android_test_owns_implementation() -> None:
    root = TOOLS.parent.parent
    hook_source = (
        root
        / "Android/app/src/main/java/com/cheby/codex/mobile/gateway/"
        "DeliveryCrashTestHook.kt"
    ).read_text(encoding="utf-8")
    test_source = (
        root
        / "Android/app/src/androidTest/java/com/cheby/codex/mobile/gateway/"
        "DeliveryExactCrashGateInstrumentedTest.kt"
    ).read_text(encoding="utf-8")
    release_source = (
        root
        / "Android/app/src/main/java/com/cheby/codex/mobile/gateway/"
        "RelayCodexGateway.kt"
    ).read_text(encoding="utf-8")
    assert 'BuildConfig.APPLICATION_ID == "com.cheby.codex.mobile.gate"' in hook_source
    assert "BuildConfig.RELAY_SERVICE_PORT == 27462" in hook_source
    assert "class ExactCrashHook" in test_source
    assert "Runtime.getRuntime()" not in test_source
    assert "UiDevice" not in test_source
    assert "executeShellCommand" not in test_source
    assert "deliveryCrashTestHook.reachedForGate" in release_source
    assert "NoOpDeliveryCrashTestHook" in release_source


def test_business_key_digest_uses_all_four_fields() -> None:
    baseline = gate.BusinessKey("scope", 1, "thread", "client")
    baseline_digest = gate.digest_business_keys([baseline])
    variants = (
        gate.BusinessKey("scope-2", 1, "thread", "client"),
        gate.BusinessKey("scope", 2, "thread", "client"),
        gate.BusinessKey("scope", 1, "thread-2", "client"),
        gate.BusinessKey("scope", 1, "thread", "client-2"),
    )
    assert all(gate.digest_business_keys([value]) != baseline_digest for value in variants)
