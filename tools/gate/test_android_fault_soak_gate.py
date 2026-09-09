from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

import android_fault_soak_gate as gate
import android_relay_gate as relay_gate


def event(
    checkpoint: str,
    component: str,
    iteration: int,
    *,
    nonce: str = "a" * 32,
    turns: int = 0,
    outbox_entries: int = 0,
    duplicate_bubbles: int = 0,
    observed: bool = False,
    phase_digest: str | None = None,
) -> gate.GateEvent:
    return gate.GateEvent(
        checkpoint=checkpoint,
        nonce=nonce,
        component=component,
        iteration=iteration,
        turns=turns,
        outbox_entries=outbox_entries,
        duplicate_bubbles=duplicate_bubbles,
        action_observed=observed,
        phase_digest=phase_digest,
    )


def passing_transport_run() -> gate.InstrumentationRun:
    events: list[gate.GateEvent] = []
    for iteration in range(1, gate.REQUIRED_REPETITIONS + 1):
        for component in (*gate.REMOTE_COMPONENTS, "wifi"):
            nonce = hashlib.sha256(
                f"{component}:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            events.extend(
                (
                    event(
                        "fault_down_required",
                        component,
                        iteration,
                        nonce=nonce,
                    ),
                    event(
                        "fault_outbox_ready",
                        component,
                        iteration,
                        nonce=nonce,
                        outbox_entries=3,
                    ),
                    event(
                        "fault_up_required",
                        component,
                        iteration,
                        nonce=nonce,
                        outbox_entries=3,
                    ),
                    event(
                        "fault_recovered",
                        component,
                        iteration,
                        nonce=nonce,
                        turns=5,
                        observed=True,
                    ),
                )
            )
    events.append(
        event(
            "fault_gate_complete",
            "all",
            gate.REQUIRED_REPETITIONS,
            turns=gate.REQUIRED_REPETITIONS * 4 * 5,
            observed=True,
        )
    )
    return gate.InstrumentationRun(
        events=events,
        status_codes=[
            1,
            *([gate.EVIDENCE_STATUS_CODE] * len(events)),
            0,
        ],
        instrumentation_code=-1,
        junit_ok=True,
    )


def artifact(tmp_path: Path, name: str) -> relay_gate.Artifact:
    path = tmp_path / name
    path.write_bytes(name.encode("ascii"))
    return relay_gate.inspect_apk(str(path.resolve()), name)


def passing_fault_audit() -> dict[str, object]:
    digest = "b" * 64
    return {
        "schema": gate.REMOTE_FAULT_AUDIT_SCHEMA,
        "outcome": "PASS",
        "runToken": "a" * 32,
        "counts": {
            "turns": gate.REQUIRED_FAULT_TURNS,
            "threads": gate.REQUIRED_FAULT_THREADS,
            "relayTuples": gate.REQUIRED_FAULT_TURNS,
            "connectorTuples": gate.REQUIRED_FAULT_TURNS,
            "gatewayTuples": gate.REQUIRED_FAULT_TURNS,
            "fakeCodexTuples": gate.REQUIRED_FAULT_TURNS,
            "bindingGenerationMappings": 1,
            "relevantConnectorIntents": 0,
            "relevantConnectorOutbox": 0,
            "relevantConnectorProcessedDeliveries": 0,
            "cycles": gate.REQUIRED_FAULT_CYCLES,
            "businessKeys": gate.REQUIRED_FAULT_TURNS,
            "maxTurnCountPerBusinessKey": 1,
            "maxEffectCountPerBusinessKey": 1,
            "orphanDeliveries": 0,
            "orphanOutbox": 0,
            "orphanIntents": 0,
        },
        "digests": {
            "tupleSha256": digest,
            "relayTupleSha256": digest,
            "connectorTupleSha256": digest,
            "gatewayTupleSha256": digest,
            "fakeCodexTupleSha256": digest,
            "bindingGenerationMappingSha256": "d" * 64,
        },
    }


def test_fault_audit_command_is_fixed_root_program_and_run_token_only() -> None:
    command = gate.build_fault_audit_command(
        Path("/usr/bin/ssh"),
        "remote-213",
        "a" * 32,
    )
    assert command == [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "remote-213",
        "sudo",
        "-n",
        "/usr/local/sbin/chebycodex-audit-gate-business-keys",
        "--mode",
        "fault",
        "--run-token",
        "a" * 32,
    ]
    assert "--command" not in command
    assert "--path" not in command


def test_fault_audit_requires_exact_counts_and_cross_layer_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = passing_fault_audit()
    monkeypatch.setattr(
        relay_gate,
        "run_bounded",
        lambda *args, **kwargs: json.dumps(document).encode("utf-8"),
    )
    plan = relay_gate.AuditPlan(
        ssh=Path("/usr/bin/ssh"),
        ssh_host="remote-213",
    )
    assert gate.read_fault_audit(plan, "a" * 32) == document

    for key, invalid in (
        ("maxTurnCountPerBusinessKey", 2),
        ("maxEffectCountPerBusinessKey", 2),
        ("orphanDeliveries", 1),
        ("orphanOutbox", 1),
        ("orphanIntents", 1),
        ("cycles", gate.REQUIRED_FAULT_CYCLES - 1),
    ):
        original = document["counts"][key]
        document["counts"][key] = invalid
        with pytest.raises(relay_gate.GateError, match="fixed run contract"):
            gate.read_fault_audit(plan, "a" * 32)
        document["counts"][key] = original
    document["digests"]["gatewayTupleSha256"] = "c" * 64
    with pytest.raises(relay_gate.GateError, match="digests diverged"):
        gate.read_fault_audit(plan, "a" * 32)


def test_app_kill_phases_are_distinct_instrumentation_processes() -> None:
    adb = Path("/opt/android/adb")
    prepare = gate.build_app_kill_phase_command(
        adb,
        "SERIAL-1",
        phase="app-kill-prepare",
        run_token="a" * 32,
        action_nonce="b" * 32,
        iteration=3,
        expected_reply=relay_gate.GATE_REPLY_MARKER,
    )
    recover = gate.build_app_kill_phase_command(
        adb,
        "SERIAL-1",
        phase="app-kill-recover",
        run_token="a" * 32,
        action_nonce="b" * 32,
        iteration=3,
        expected_reply=relay_gate.GATE_REPLY_MARKER,
    )
    assert f"{gate.FAULT_TEST_CLASS}#{gate.APP_KILL_PREPARE_METHOD}" in prepare
    assert f"{gate.FAULT_TEST_CLASS}#{gate.APP_KILL_RECOVER_METHOD}" in recover
    assert prepare != recover
    for command in (prepare, recover):
        assert command.count("b" * 32) == 1
        assert "input" not in command
        assert "monkey" not in command
        assert "uiautomator" not in command


def test_only_wifi_infrastructure_commands_can_change_device_state() -> None:
    adb = Path("/opt/android/adb")
    disable = gate.build_wifi_command(adb, "SERIAL-1", False)
    enable = gate.build_wifi_command(adb, "SERIAL-1", True)
    assert disable[-4:] == ["shell", "svc", "wifi", "disable"]
    assert enable[-4:] == ["shell", "svc", "wifi", "enable"]
    commands = [
        disable,
        enable,
        gate.build_fault_ack_command(adb, "SERIAL-1"),
        gate.build_transport_command(
            adb,
            "SERIAL-1",
            run_token="a" * 32,
            expected_reply=relay_gate.GATE_REPLY_MARKER,
        ),
    ]
    for command in commands:
        assert "tap" not in command
        assert "swipe" not in command
        assert "keyevent" not in command
        assert "monkey" not in command
        assert "uiautomator" not in command
        assert not any(
            command[index : index + 2] == ["shell", "input"]
            for index in range(len(command) - 1)
        )


def test_fault_event_schema_preserves_queued_keys_and_production_queue() -> None:
    values = {
        "cheby_gate_checkpoint": "app_kill_ready",
        "cheby_gate_action_nonce": "a" * 32,
        "cheby_gate_component": "app",
        "cheby_gate_iteration": "1",
        "cheby_gate_phase_digest": "b" * 64,
        "cheby_gate_turns": "1",
        "cheby_gate_outbox_entries": "3",
        "cheby_gate_duplicate_bubbles": "0",
        "cheby_gate_production_queue": "true",
        "cheby_gate_action_observed": "false",
    }
    parsed = gate.parse_gate_event(values)
    assert parsed.phase_digest == "b" * 64
    assert parsed.outbox_entries == 3
    duplicate = dict(values)
    duplicate["cheby_gate_duplicate_bubbles"] = "1"
    assert gate.parse_gate_event(duplicate).duplicate_bubbles == 1
    invalid = dict(values)
    invalid["cheby_gate_production_queue"] = "false"
    with pytest.raises(relay_gate.GateError):
        gate.parse_gate_event(invalid)


def test_status_sequence_rejects_skips_and_missing_final() -> None:
    passing = passing_transport_run()
    gate.validate_fault_run(passing)
    skipped = passing_transport_run()
    skipped.status_codes.insert(-1, -3)
    with pytest.raises(relay_gate.GateError, match="skipped"):
        gate.validate_fault_run(skipped)
    missing_final = passing_transport_run()
    missing_final.instrumentation_code = None
    with pytest.raises(relay_gate.GateError, match="JUnit"):
        gate.validate_fault_run(missing_final)


def test_transport_sequence_requires_ten_causal_cycles_per_component() -> None:
    assert (
        gate.TRANSPORT_METHOD
        == "recoverFaultHeldQueuedTurnsAfterCausalTransportFaults"
    )
    passing = passing_transport_run()
    gate.validate_transport_sequence(passing)
    wrong_nonce = passing_transport_run()
    original = wrong_nonce.events[1]
    wrong_nonce.events[1] = gate.GateEvent(
        checkpoint=original.checkpoint,
        nonce="f" * 32,
        component=original.component,
        iteration=original.iteration,
        turns=original.turns,
        outbox_entries=original.outbox_entries,
        duplicate_bubbles=original.duplicate_bubbles,
        action_observed=original.action_observed,
    )
    with pytest.raises(relay_gate.GateError, match="out of order"):
        gate.validate_transport_sequence(wrong_nonce)
    truncated = passing_transport_run()
    truncated.events.pop()
    with pytest.raises(relay_gate.GateError, match="count"):
        gate.validate_transport_sequence(truncated)
    duplicate = passing_transport_run()
    recovered = duplicate.events[3]
    duplicate.events[3] = gate.GateEvent(
        checkpoint=recovered.checkpoint,
        nonce=recovered.nonce,
        component=recovered.component,
        iteration=recovered.iteration,
        turns=recovered.turns,
        outbox_entries=recovered.outbox_entries,
        duplicate_bubbles=1,
        action_observed=recovered.action_observed,
    )
    with pytest.raises(relay_gate.GateError, match="out of order"):
        gate.validate_transport_sequence(duplicate)


def test_app_recovery_requires_matching_durable_phase_digest() -> None:
    run = gate.InstrumentationRun(
        events=[
            event(
                "app_kill_recovered",
                "app",
                4,
                nonce="c" * 32,
                turns=5,
                observed=True,
                phase_digest="d" * 64,
            )
        ],
        status_codes=[1, 2, 0],
        instrumentation_code=-1,
        junit_ok=True,
    )
    parsed = gate.validate_app_recovery(
        run,
        iteration=4,
        nonce="c" * 32,
    )
    assert parsed.phase_digest == "d" * 64
    with pytest.raises(relay_gate.GateError, match="did not match"):
        gate.validate_app_recovery(
            run,
            iteration=5,
            nonce="c" * 32,
        )


def test_prepare_must_be_killed_live_with_three_queued_business_keys() -> None:
    run = gate.InstrumentationRun(
        events=[
            event(
                "app_prepare_offline_required",
                "edge",
                2,
                nonce="a" * 32,
            ),
            event(
                "app_kill_outbox_ready",
                "app",
                2,
                nonce="a" * 32,
                outbox_entries=3,
                phase_digest="b" * 64,
            ),
        ],
        status_codes=[1, 2, 2],
        instrumentation_code=None,
        junit_ok=False,
    )
    gate.validate_expected_host_kill(
        run,
        "app_kill_outbox_ready",
        checkpoint_seen_while_alive=True,
    )
    prepared = gate.validate_app_prepare(
        run,
        iteration=2,
        nonce="a" * 32,
    )
    assert prepared.outbox_entries == 3
    clean_junit = gate.InstrumentationRun(
        events=run.events,
        status_codes=[1, 2, 2, 0],
        instrumentation_code=-1,
        junit_ok=True,
    )
    with pytest.raises(relay_gate.GateError, match="live checkpoint"):
        gate.validate_expected_host_kill(
            clean_junit,
            "app_kill_outbox_ready",
            checkpoint_seen_while_alive=True,
        )


def test_container_restart_requires_changed_ready_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(
        (
            gate.ContainerState(
                started_at="2026-07-26T01:00:00.000000000Z",
                running=True,
                health="healthy",
            ),
            gate.ContainerState(
                started_at="2026-07-26T01:01:00.000000000Z",
                running=True,
                health="healthy",
            ),
        )
    )
    monkeypatch.setattr(gate, "read_container_state", lambda *args: next(states))
    monkeypatch.setattr(
        relay_gate,
        "run_bounded",
        lambda command, **kwargs: f"{command[-1]}\n".encode("ascii"),
    )
    evidence = gate.restart_container_causally(
        Path("/usr/bin/ssh"),
        "remote-213",
        "gate-edge",
        "edge",
        2,
    )
    assert evidence.component == "edge"
    assert evidence.iteration == 2
    assert evidence.before_sha256 != evidence.after_sha256


def test_container_stop_start_holds_down_then_changes_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = gate.ContainerState(
        started_at="2026-07-26T01:00:00.000000000Z",
        running=True,
        health="healthy",
    )
    stopped_state = gate.ContainerState(
        started_at=running.started_at,
        running=False,
        health=None,
    )
    started = gate.ContainerState(
        started_at="2026-07-26T01:02:00.000000000Z",
        running=True,
        health="healthy",
    )
    states = iter((running, stopped_state))
    monkeypatch.setattr(gate, "read_container_state", lambda *args: next(states))
    monkeypatch.setattr(
        relay_gate,
        "run_bounded",
        lambda command, **kwargs: f"{command[-1]}\n".encode("ascii"),
    )
    intents: list[gate.StoppedContainer] = []
    held = gate.stop_container_causally(
        Path("/usr/bin/ssh"),
        "remote-213",
        "gate-edge",
        "edge",
        1,
        register_intent=intents.append,
    )
    assert intents == [held]
    states = iter((stopped_state, started))
    monkeypatch.setattr(gate, "read_container_state", lambda *args: next(states))
    evidence = gate.start_container_causally(
        Path("/usr/bin/ssh"),
        "remote-213",
        held,
    )
    assert evidence.before_sha256 != evidence.after_sha256


def test_stop_intent_is_registered_before_lost_stop_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = gate.ContainerState(
        started_at="2026-07-26T01:00:00.000000000Z",
        running=True,
        health="healthy",
    )
    monkeypatch.setattr(gate, "read_container_state", lambda *args: running)
    intents: list[gate.StoppedContainer] = []

    def lose_ack(*args, **kwargs):
        raise relay_gate.GateError("lost stop acknowledgement")

    monkeypatch.setattr(relay_gate, "run_bounded", lose_ack)
    with pytest.raises(relay_gate.GateError, match="lost stop"):
        gate.stop_container_causally(
            Path("/usr/bin/ssh"),
            "remote-213",
            "gate-edge",
            "edge",
            1,
            register_intent=intents.append,
        )
    assert len(intents) == 1
    assert intents[0].container == "gate-edge"


def test_final_restoration_probes_all_and_never_starts_initially_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "2026-07-26T01:00:00.000000000Z"
    containers = dict(gate.EXPECTED_GATE_CONTAINERS)
    initial = {
        "edge": gate.ContainerState(epoch, True, "healthy"),
        "relay": gate.ContainerState(epoch, True, "healthy"),
        "connector": gate.ContainerState(epoch, False, None),
    }
    current = {
        containers["edge"]: gate.ContainerState(epoch, False, None),
        containers["relay"]: gate.ContainerState(epoch, False, None),
        containers["connector"]: gate.ContainerState(epoch, False, None),
    }
    probed: list[str] = []
    started: list[str] = []

    def read_state(ssh, host, container):
        probed.append(container)
        return current[container]

    def start(ssh, host, stopped):
        started.append(stopped.container)
        return gate.RestartEvidence(
            component=stopped.component,
            iteration=stopped.iteration,
            before_sha256="a" * 64,
            after_sha256="b" * 64,
        )

    monkeypatch.setattr(gate, "read_container_state", read_state)
    monkeypatch.setattr(gate, "start_container_causally", start)
    evidence, failed = gate.restore_initially_running_gate_containers(
        Path("/usr/bin/ssh"),
        "remote-213",
        containers,
        initial,
        {},
    )
    assert failed is False
    assert probed == [containers[value] for value in gate.REMOTE_COMPONENTS]
    assert started == [containers["edge"], containers["relay"]]
    assert len(evidence) == 2


def test_final_restoration_waits_for_running_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "2026-07-26T01:00:00.000000000Z"
    containers = dict(gate.EXPECTED_GATE_CONTAINERS)
    initial = {
        component: gate.ContainerState(epoch, True, "healthy")
        for component in gate.REMOTE_COMPONENTS
    }
    relay_states = iter(
        (
            gate.ContainerState(epoch, True, "starting"),
            gate.ContainerState(epoch, True, "healthy"),
        )
    )
    probed: list[str] = []

    def read_state(ssh, host, container):
        probed.append(container)
        if container == containers["relay"]:
            return next(relay_states)
        return gate.ContainerState(epoch, True, None)

    monkeypatch.setattr(gate, "read_container_state", read_state)
    monkeypatch.setattr(
        gate,
        "start_container_causally",
        lambda *args: pytest.fail("A running container must not be started"),
    )
    evidence, failed = gate.restore_initially_running_gate_containers(
        Path("/usr/bin/ssh"),
        "remote-213",
        containers,
        initial,
        {},
        health_timeout_seconds=1.0,
        health_poll_seconds=0.0,
    )
    assert failed is False
    assert evidence == []
    assert probed == [
        containers["edge"],
        containers["relay"],
        containers["relay"],
        containers["connector"],
    ]


def test_final_restoration_fails_closed_for_unhealthy_running_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "2026-07-26T01:00:00.000000000Z"
    containers = dict(gate.EXPECTED_GATE_CONTAINERS)
    initial = {
        component: gate.ContainerState(epoch, True, "healthy")
        for component in gate.REMOTE_COMPONENTS
    }
    current = {
        containers["edge"]: gate.ContainerState(epoch, True, None),
        containers["relay"]: gate.ContainerState(epoch, True, "unhealthy"),
        containers["connector"]: gate.ContainerState(epoch, True, None),
    }
    started: list[str] = []
    monkeypatch.setattr(
        gate,
        "read_container_state",
        lambda ssh, host, container: current[container],
    )
    monkeypatch.setattr(
        gate,
        "start_container_causally",
        lambda ssh, host, stopped: started.append(stopped.container),
    )
    evidence, failed = gate.restore_initially_running_gate_containers(
        Path("/usr/bin/ssh"),
        "remote-213",
        containers,
        initial,
        {},
        health_timeout_seconds=0.0,
        health_poll_seconds=0.0,
    )
    assert failed is True
    assert evidence == []
    assert started == []


def test_final_restoration_rejects_missing_relay_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "2026-07-26T01:00:00.000000000Z"
    containers = dict(gate.EXPECTED_GATE_CONTAINERS)
    initial = {
        component: gate.ContainerState(epoch, True, "healthy")
        for component in gate.REMOTE_COMPONENTS
    }
    monkeypatch.setattr(
        gate,
        "read_container_state",
        lambda *args: gate.ContainerState(epoch, True, None),
    )
    evidence, failed = gate.restore_initially_running_gate_containers(
        Path("/usr/bin/ssh"),
        "remote-213",
        containers,
        initial,
        {},
        health_timeout_seconds=0.0,
        health_poll_seconds=0.0,
    )
    assert failed is True
    assert evidence == []


def test_final_restores_pre_registered_stop_after_ack_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = "2026-07-26T01:00:00.000000000Z"
    containers = dict(gate.EXPECTED_GATE_CONTAINERS)
    initial = {
        component: gate.ContainerState(epoch, True, "healthy")
        for component in gate.REMOTE_COMPONENTS
    }
    current = dict(initial)
    intents: dict[tuple[str, int, str], gate.StoppedContainer] = {}

    def read_state(ssh, host, container):
        component = next(
            key for key, value in containers.items() if value == container
        )
        return current[component]

    def stop_without_ack(*args, **kwargs):
        current["edge"] = gate.ContainerState(epoch, False, None)
        raise relay_gate.GateError("lost stop acknowledgement")

    monkeypatch.setattr(gate, "read_container_state", read_state)
    monkeypatch.setattr(relay_gate, "run_bounded", stop_without_ack)
    key = ("edge", 1, "a" * 32)
    with pytest.raises(relay_gate.GateError, match="lost stop"):
        gate.stop_container_causally(
            Path("/usr/bin/ssh"),
            "remote-213",
            containers["edge"],
            "edge",
            1,
            register_intent=lambda intent: intents.__setitem__(key, intent),
        )

    started: list[str] = []

    def start(ssh, host, stopped):
        started.append(stopped.container)
        current["edge"] = gate.ContainerState(
            "2026-07-26T01:02:00.000000000Z",
            True,
            "healthy",
        )
        return gate.RestartEvidence(
            component=stopped.component,
            iteration=stopped.iteration,
            before_sha256="a" * 64,
            after_sha256="b" * 64,
        )

    monkeypatch.setattr(gate, "start_container_causally", start)
    evidence, failed = gate.restore_initially_running_gate_containers(
        Path("/usr/bin/ssh"),
        "remote-213",
        containers,
        initial,
        intents,
    )
    assert failed is False
    assert started == [containers["edge"]]
    assert len(evidence) == 1


def test_container_commands_are_fixed_and_do_not_embed_shell_operators() -> None:
    commands = (
        gate.build_container_inspect_command(
            Path("/usr/bin/ssh"),
            "remote-213",
            "gate-edge",
        ),
        gate.build_container_restart_command(
            Path("/usr/bin/ssh"),
            "remote-213",
            "gate-edge",
        ),
        gate.build_container_stop_command(
            Path("/usr/bin/ssh"),
            "remote-213",
            "gate-edge",
        ),
        gate.build_container_start_command(
            Path("/usr/bin/ssh"),
            "remote-213",
            "gate-edge",
        ),
    )
    for command in commands:
        assert all(
            all(character not in token for character in ";|&$`\n\r")
            for token in command
        )
    assert commands[0][-2] == "--format={{json .State}}"
    assert commands[1][-4:-1] == ["restart", "--time", "20"]
    assert commands[2][-4:-1] == ["stop", "--time", "20"]
    assert commands[3][-2:] == ["start", "gate-edge"]


def test_wifi_state_probe_has_strict_vendor_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate,
        "_run_state_probe",
        lambda command: (0, "Wi-Fi is enabled"),
    )
    assert gate.read_wifi_enabled(Path("/opt/android/adb"), "SERIAL-1")
    calls = iter(((1, "unsupported"), (0, "0")))
    monkeypatch.setattr(gate, "_run_state_probe", lambda command: next(calls))
    assert not gate.read_wifi_enabled(Path("/opt/android/adb"), "SERIAL-1")
    calls = iter(((1, "unsupported"), (0, "unexpected")))
    monkeypatch.setattr(gate, "_run_state_probe", lambda command: next(calls))
    with pytest.raises(relay_gate.GateError, match="not observable"):
        gate.read_wifi_enabled(Path("/opt/android/adb"), "SERIAL-1")


def test_soak_command_requires_exact_eight_hours_500_turns_8_threads() -> None:
    command = gate.build_soak_command(
        Path("/opt/android/adb"),
        "SERIAL-1",
        relay_gate.GATE_REPLY_MARKER,
    )
    assert relay_gate.LOAD_CLASS in command
    arguments = {
        command[index + 1]: command[index + 2]
        for index, value in enumerate(command[:-2])
        if value == "-e"
    }
    assert arguments["cheby_relay_load_threads"] == "8"
    assert arguments["cheby_relay_load_turns"] == "500"
    assert (
        arguments[gate.SOAK_DURATION_ARGUMENT]
        == str(gate.REQUIRED_SOAK_MILLIS)
    )


def test_android_sources_use_production_queue_and_real_monotonic_soak() -> None:
    root = Path(__file__).resolve().parents[2]
    fault_source = (
        root
        / "Android/app/src/androidTest/java/com/cheby/codex/mobile/gateway"
        / "RelayFaultGateInstrumentedTest.kt"
    ).read_text(encoding="utf-8")
    load_source = (
        root
        / "Android/app/src/androidTest/java/com/cheby/codex/mobile/gateway"
        / "RelayLoadGateInstrumentedTest.kt"
    ).read_text(encoding="utf-8")
    runner_source = (
        root / "tools/gate/android_fault_soak_gate.py"
    ).read_text(encoding="utf-8")
    audit_source = (
        root / "deploy/turkey/audit_gate_business_keys.py"
    ).read_text(encoding="utf-8")
    assert "model.send(marker)" in fault_source
    assert "SharedPreferencesDurableOutboxStore(context)" in fault_source
    assert "prepareAppKillRecoveryPhase" in fault_source
    assert "recoverAppKillRecoveryPhase" in fault_source
    assert "recoverFaultHeldQueuedTurnsAfterCausalTransportFaults" in fault_source
    assert "recoverInFlightTurnsFromCausalTransportFaults" not in fault_source
    assert "must not be counted as evidence for those boundaries" in fault_source
    assert "awaitCancellation()" in fault_source
    assert "queued.all { it.state == OutboxState.QUEUED }" in fault_source
    assert fault_source.count("assertBusinessKeysRemoved(") == 3
    assert "store.list().none { it.clientMessageId in clientMessageIds }" in fault_source
    assert "requireGlobalOutboxCount(outboxStore, 0" in fault_source
    assert "requireGlobalOutboxCount(outboxStore, 0" in load_source
    assert "assistantBubbleEvidence(messages, turnId)" in fault_source
    assert "assistantBubbleEvidence(messages, turnId)" in load_source
    assert "FAULT-$runToken-$label-$iteration-A1" in fault_source
    assert "FAULT-$runToken-$label-$iteration-A2" in fault_source
    assert "FAULT-$runToken-$label-$iteration-B1" in fault_source
    assert "Mock" not in fault_source
    assert "SystemClock.elapsedRealtime()" in load_source
    assert "minimumDurationMillis * pacedOrdinal.toLong()" in load_source
    assert "REQUIRED_SOAK_DURATION_MILLIS = 8 * 60 * 60 * 1_000L" in load_source
    assert "audit = read_fault_audit(audit_plan, run_token)" in runner_source
    assert "perform_fault_audit(paths, args.run_token)" in audit_source
    assert '"--mode"' in audit_source
    assert '"fault"' in audit_source
    assert "fault.final_outbox_entries = 0" not in runner_source
    assert "fault.duplicate_bubbles = 0" not in runner_source
    assert "fault.permanent_accepted_entries = 0" not in runner_source


def test_manifest_is_private_sanitized_and_has_no_nonces(
    tmp_path: Path,
) -> None:
    gate_apk = artifact(tmp_path, "gate.apk")
    test_apk = artifact(tmp_path, "test.apk")
    fault = gate.FaultEvidence(
        app_prepare_runs=10,
        app_recovery_runs=10,
        app_force_stops=10,
        transport_instrumentation_runs=1,
        recovered={
            "app": 10,
            "wifi": 10,
            "edge": 10,
            "relay": 10,
            "connector": 10,
        },
        wifi_disable_commands=10,
        wifi_enable_commands=10,
        wifi_restored=True,
        final_outbox_entries=0,
        duplicate_bubbles=0,
        phase_digests=["a" * 64] * 10,
        audited_cycles=gate.REQUIRED_FAULT_CYCLES,
        permanent_accepted_entries=0,
        remote_audit=gate._sanitized_remote_audit(passing_fault_audit()),
    )
    document = gate.manifest_document(
        mode="faults",
        outcome="PASS",
        gate_apk=gate_apk,
        test_apk=test_apk,
        installed_gate_sha256=gate_apk.sha256,
        installed_test_sha256=test_apk.sha256,
        duration_seconds=1.0,
        fault=fault,
        soak=None,
        failure=None,
    )
    manifest = tmp_path / "evidence" / "faults.json"
    relay_gate.write_manifest(str(manifest.resolve()), document)
    payload = manifest.read_text(encoding="utf-8")
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert "nonce" not in payload.lower()
    assert "rawOutput" not in payload
    assert "a" * 32 not in payload
    assert "/Users/" not in payload
    parsed = json.loads(payload)
    assert parsed["safeguards"]["screenCoordinatesSent"] == 0
    assert parsed["safeguards"]["screenTextInputsSent"] == 0
    assert parsed["evidence"]["faults"]["wifiRestored"] is True
    assert (
        parsed["evidence"]["faults"]["hostDatabaseAudit"]["counts"]
        ["maxEffectCountPerBusinessKey"]
        == 1
    )
    assert parsed["evidence"]["faults"]["permanentAcceptedEntries"] == 0
    fault.final_outbox_entries = 1
    with pytest.raises(relay_gate.GateError, match="measured global"):
        gate.manifest_document(
            mode="faults",
            outcome="PASS",
            gate_apk=gate_apk,
            test_apk=test_apk,
            installed_gate_sha256=gate_apk.sha256,
            installed_test_sha256=test_apk.sha256,
            duration_seconds=1.0,
            fault=fault,
            soak=None,
            failure=None,
        )


@pytest.mark.parametrize("mode", ("faults", "soak"))
def test_fault_and_soak_runner_reject_existing_manifest_before_execute(
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    manifest = tmp_path / f"{mode}.json"
    manifest.write_text("preserve\n", encoding="utf-8")
    executed = False

    def forbidden_execute(args):
        nonlocal executed
        executed = True
        raise AssertionError("execute must not run")

    monkeypatch.setattr(gate, "execute", forbidden_execute)
    result = gate.main(
        [
            "--mode",
            mode,
            "--adb",
            "/absolute/adb",
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            "/absolute/gate.apk",
            "--test-apk",
            "/absolute/test.apk",
            "--manifest",
            str(manifest.resolve()),
            "--ssh",
            "/absolute/ssh",
            "--ssh-host",
            "remote-213",
        ]
    )
    assert result == 2
    assert executed is False
    assert manifest.read_text(encoding="utf-8") == "preserve\n"


def test_validation_rejects_timeout_that_cannot_cover_real_soak(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb"
    ssh = tmp_path / "ssh"
    app = tmp_path / "app.apk"
    test = tmp_path / "test.apk"
    for executable in (adb, ssh):
        executable.write_text("#!/bin/sh\n", encoding="ascii")
        executable.chmod(0o700)
    app.write_bytes(b"app")
    test.write_bytes(b"test")
    args = gate.create_parser().parse_args(
        [
            "--mode",
            "soak",
            "--adb",
            str(adb.resolve()),
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            str(app.resolve()),
            "--test-apk",
            str(test.resolve()),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--ssh",
            str(ssh.resolve()),
            "--ssh-host",
            "remote-213",
            "--soak-timeout-seconds",
            "28000",
        ]
    )
    with pytest.raises(relay_gate.ConfigError, match="Soak timeout"):
        gate.validate_args(args)


def test_validation_rejects_non_gate_container_target(tmp_path: Path) -> None:
    adb = tmp_path / "adb"
    ssh = tmp_path / "ssh"
    app = tmp_path / "app.apk"
    test = tmp_path / "test.apk"
    for executable in (adb, ssh):
        executable.write_text("#!/bin/sh\n", encoding="ascii")
        executable.chmod(0o700)
    app.write_bytes(b"app")
    test.write_bytes(b"test")
    args = gate.create_parser().parse_args(
        [
            "--mode",
            "faults",
            "--adb",
            str(adb.resolve()),
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            str(app.resolve()),
            "--test-apk",
            str(test.resolve()),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--ssh",
            str(ssh.resolve()),
            "--ssh-host",
            "remote-213",
            "--edge-container",
            "production-edge",
        ]
    )
    with pytest.raises(relay_gate.ConfigError, match="isolated Gate"):
        gate.validate_args(args)
