from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import android_relay_gate


def passing_headless_evidence() -> android_relay_gate.InstrumentationEvidence:
    return android_relay_gate.InstrumentationEvidence(
        checkpoints=[
            android_relay_gate.RESTART_CHECKPOINT,
            android_relay_gate.HEADLESS_COMPLETE_CHECKPOINT,
        ],
        values={
            "cheby_gate_restart_observed": "true",
            "cheby_gate_restart_causal_ack": "true",
            "cheby_gate_remembered_marker_verified": "true",
            "cheby_gate_threads": "2",
            "cheby_gate_turns": "2",
        },
        status_codes=[
            1,
            android_relay_gate.EVIDENCE_STATUS_CODE,
            android_relay_gate.EVIDENCE_STATUS_CODE,
            0,
        ],
        protocol_transcript=[
            {"kind": "status", "code": 1},
            {"kind": "status", "code": android_relay_gate.EVIDENCE_STATUS_CODE},
            {"kind": "status", "code": android_relay_gate.EVIDENCE_STATUS_CODE},
            {"kind": "status", "code": 0},
            {"kind": "final", "code": -1},
        ],
        instrumentation_code=-1,
        junit_ok=True,
        restart_executed=True,
        restart_succeeded=True,
        restart_nonce="a" * 32,
        restart_ack_written=True,
        connector_epoch_before_sha256="1" * 64,
        connector_epoch_after_sha256="2" * 64,
    )


def passing_load_evidence() -> android_relay_gate.InstrumentationEvidence:
    return android_relay_gate.InstrumentationEvidence(
        checkpoints=[android_relay_gate.LOAD_COMPLETE_CHECKPOINT],
        values={
            "cheby_gate_threads": "8",
            "cheby_gate_turns": "500",
            "cheby_gate_business_keys": "500",
            "cheby_gate_outbox_entries": "0",
            "cheby_gate_duplicate_bubbles": "0",
            "cheby_gate_first_last_verified": "true",
            "cheby_gate_server_barrier_threads": "2",
            "cheby_gate_production_queue": "true",
            "cheby_gate_run_token": "a" * 32,
            "cheby_gate_tuple_sha256": "b" * 64,
            "cheby_gate_scope_generation_sha256": "c" * 64,
            "cheby_gate_scope_generation_pairs": "1",
            "cheby_gate_scope_generation_observations": "500",
        },
        status_codes=[1, android_relay_gate.EVIDENCE_STATUS_CODE, 0],
        protocol_transcript=[
            {"kind": "status", "code": 1},
            {"kind": "status", "code": android_relay_gate.EVIDENCE_STATUS_CODE},
            {"kind": "status", "code": 0},
            {"kind": "final", "code": -1},
        ],
        instrumentation_code=-1,
        junit_ok=True,
    )


def passing_quick_demo_evidence() -> android_relay_gate.InstrumentationEvidence:
    evidence = passing_load_evidence()
    evidence.values.update(
        {
            "cheby_gate_turns": "16",
            "cheby_gate_business_keys": "16",
            "cheby_gate_scope_generation_observations": "16",
        },
    )
    return evidence


def test_quick_demo_is_fixed_to_bounded_eight_thread_sixteen_turn_gate(tmp_path) -> None:
    parser = android_relay_gate.create_parser()
    common = [
        "--mode", "quick-demo",
        "--adb", __file__,
        "--serial", "SERIAL-1",
        "--gate-apk", __file__,
        "--test-apk", __file__,
        "--manifest", str(tmp_path / "manifest.json"),
        "--ssh", __file__,
        "--ssh-host", "turkey",
    ]
    args = parser.parse_args([*common, "--threads", "8", "--turns", "16"])
    # APK inspection is intentionally outside this argument-only contract. The
    # timeout/default validation below is covered through the pure evidence path.
    args.timeout_seconds = 480.0
    android_relay_gate.validate_instrumentation_evidence(
        passing_quick_demo_evidence(),
        "quick-demo",
        expected_threads=8,
        expected_turns=16,
    )
    command = android_relay_gate.build_instrumentation_command(
        Path("/opt/android/adb"),
        "SERIAL-1",
        "quick-demo",
        android_relay_gate.GATE_REPLY_MARKER,
        8,
        16,
    )
    assert "RelayLoadGateInstrumentedTest" in " ".join(command)
    assert command[command.index("cheby_relay_load_turns") + 1] == "16"


@pytest.mark.parametrize("timeout", (119, 481))
def test_quick_demo_timeout_outside_two_to_eight_minutes_is_rejected(
    monkeypatch,
    tmp_path,
    timeout,
) -> None:
    artifact = android_relay_gate.Artifact(Path(__file__), "a" * 64, 1)
    monkeypatch.setattr(android_relay_gate, "require_regular_file", lambda *args, **kwargs: Path(__file__))
    monkeypatch.setattr(android_relay_gate, "inspect_apk", lambda *args, **kwargs: artifact)
    args = android_relay_gate.create_parser().parse_args(
        [
            "--mode", "quick-demo",
            "--adb", __file__,
            "--serial", "SERIAL-1",
            "--gate-apk", __file__,
            "--test-apk", __file__,
            "--manifest", str(tmp_path / "manifest.json"),
            "--ssh", __file__,
            "--ssh-host", "turkey",
            "--threads", "8",
            "--turns", "16",
            "--timeout-seconds", str(timeout),
        ],
    )
    with pytest.raises(android_relay_gate.ConfigError, match="timeout"):
        android_relay_gate.validate_args(args)


def passing_remote_audit() -> dict[str, object]:
    digest = "b" * 64
    return {
        "schema": android_relay_gate.REMOTE_AUDIT_SCHEMA,
        "outcome": "PASS",
        "runToken": "a" * 32,
        "counts": {
            "turns": 500,
            "threads": 8,
            "relayTuples": 500,
            "connectorTuples": 500,
            "gatewayTuples": 500,
            "fakeCodexTuples": 500,
            "bindingGenerationMappings": 1,
            "relevantConnectorIntents": 0,
            "relevantConnectorOutbox": 0,
            "relevantConnectorProcessedDeliveries": 0,
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


def test_android_commands_never_operate_the_screen_or_wifi() -> None:
    adb = Path("/opt/android/adb")
    commands = [
        android_relay_gate.build_install_command(adb, "SERIAL-1", Path("/tmp/gate.apk")),
        *android_relay_gate.build_force_stop_commands(adb, "SERIAL-1"),
        android_relay_gate.build_restart_ack_command(adb, "SERIAL-1"),
        android_relay_gate.build_instrumentation_command(
            adb=adb,
            serial="SERIAL-1",
            mode="headless-restart",
            expected_reply=android_relay_gate.GATE_REPLY_MARKER,
            threads=8,
            turns=500,
        ),
    ]
    forbidden_sequences = {
        ("shell", "input"),
        ("shell", "svc", "wifi"),
        ("shell", "settings"),
        ("shell", "monkey"),
    }
    for command in commands:
        joined = tuple(command)
        assert all(
            not any(
                joined[index : index + len(forbidden)] == forbidden
                for index in range(len(joined) - len(forbidden) + 1)
            )
            for forbidden in forbidden_sequences
        )
        assert "uiautomator" not in joined


def test_gate_readme_locks_safe_one_use_cxc1_provisioning() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "tools/gate/README.md").read_text(encoding="utf-8")
    section = source.split(
        "## One-time headless Gate APK provisioning",
        maxsplit=1,
    )[1].split("## Android restart and load evidence", maxsplit=1)[0]
    shell_blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    commands = "\n".join(shell_blocks)

    assert "distinct, unconsumed one-use CXC1" in section
    assert "Never reuse a CXC1 already consumed by `edge_gate.py`" in section
    assert "`android-e2e/device.cxc1`" in section
    assert "Never retry an outcome-unknown CXC1" in section
    assert "only after the reload-continuity" in section
    assert "set +x" in commands
    assert "CXC1_FILE=/absolute/private/android-e2e/device.cxc1" in commands
    assert '"$ADB" -s "$SERIAL" install -r -t "$GATE_APK"' in commands
    assert commands.count("shell pm clear com.cheby.codex.mobile.gate") == 2
    assert "first provisioning" in section
    assert "Do not run `pm clear`" in section
    assert "exec-out" in commands
    assert "run-as com.cheby.codex.mobile.gate" in commands
    assert "files/cheby-gate-enrollment.cxc1" in commands
    assert "-e cheby_relay_gate_provision true" in commands
    assert "RelayGateProvisioningInstrumentedTest#" in commands
    instrumentation = next(
        block for block in shell_blocks if "am instrument" in block
    )
    assert "CXC1_FILE" not in instrumentation
    assert "CXC1." not in instrumentation
    for forbidden in (
        " shell input ",
        " input tap ",
        " input text ",
        " keyevent ",
        " uiautomator ",
        " monkey ",
        " am start ",
    ):
        assert forbidden not in f" {commands.lower()} "


def test_load_gate_source_reuses_production_queue_and_server_barrier() -> None:
    root = Path(__file__).resolve().parents[2]
    load_source = (
        root
        / "Android/app/src/androidTest/java/com/cheby/codex/mobile/gateway"
        / "RelayLoadGateInstrumentedTest.kt"
    ).read_text(encoding="utf-8")
    bridge_source = (
        root / "connector/cheby_connector/gate_bridge.py"
    ).read_text(encoding="utf-8")
    assert "initialModel.send(marker)" in load_source
    assert "SharedPreferencesDurableOutboxStore(context)" in load_source
    assert "conversationTimeline(visibleMessages, accepted, pending)" in load_source
    assert ".sendTurnInput(" not in load_source
    assert "_await_load_concurrency_barrier" in bridge_source
    assert "cross-thread submission barrier was not satisfied" in bridge_source
    assert android_relay_gate.LOAD_CLASS.endswith(
        "#pairedGateSupportsProductionQueueLoadAndRecovery"
    )
    assert "fun pairedGateSupportsProductionQueueLoadAndRecovery()" in load_source


def test_expected_reply_is_only_sent_as_canonical_base64url() -> None:
    hostile = "Fake reply; $(touch /tmp/never) & < > ' \""
    command = android_relay_gate.build_instrumentation_command(
        adb=Path("/opt/android/adb"),
        serial="SERIAL-1",
        mode="headless-restart",
        expected_reply=hostile,
        threads=8,
        turns=500,
    )
    assert hostile not in command
    argument_index = command.index("cheby_relay_expected_reply_b64url")
    encoded = command[argument_index + 1]
    assert encoded.isascii()
    assert set(encoded) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )
    assert android_relay_gate.base64.urlsafe_b64decode(
        encoded + "=" * (-len(encoded) % 4)
    ).decode("utf-8") == hostile


def test_release_gate_rejects_a_caller_weakened_reply_marker() -> None:
    assert (
        android_relay_gate.validate_expected_reply(
            android_relay_gate.GATE_REPLY_MARKER,
        )
        == android_relay_gate.GATE_REPLY_MARKER
    )
    with pytest.raises(
        android_relay_gate.ConfigError,
        match="frozen Gate contract",
    ):
        android_relay_gate.validate_expected_reply("ok")


def test_restart_command_is_fixed_argv_without_a_shell() -> None:
    command = android_relay_gate.build_restart_command(
        Path("/usr/bin/ssh"),
        "remote-213",
        "chebycodex-turkey-gate-connector-gate-connector-1",
    )
    assert command == [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "remote-213",
        "docker",
        "restart",
        "--time",
        "20",
        "chebycodex-turkey-gate-connector-gate-connector-1",
    ]


def test_load_audit_command_is_fixed_root_owned_program_without_a_shell() -> None:
    command = android_relay_gate.build_gate_audit_command(
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
        "load",
        "--run-token",
        "a" * 32,
    ]


def test_quick_demo_audit_command_selects_only_fixed_remote_quick_contract() -> None:
    command = android_relay_gate.build_gate_audit_command(
        Path("/usr/bin/ssh"),
        "remote-213",
        "a" * 32,
        "quick-demo",
    )
    assert command[-4:] == ["--mode", "quick-demo", "--run-token", "a" * 32]
    assert all(character not in " ;|&$`" for character in command[-1])


def test_remote_audit_requires_android_digest_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = passing_remote_audit()
    monkeypatch.setattr(
        android_relay_gate,
        "run_bounded",
        lambda *args, **kwargs: json.dumps(document).encode("utf-8"),
    )
    plan = android_relay_gate.AuditPlan(
        ssh=Path("/usr/bin/ssh"),
        ssh_host="remote-213",
    )
    assert android_relay_gate.read_gate_audit(
        plan,
        "a" * 32,
        expected_threads=8,
        expected_turns=500,
        android_tuple_sha256="b" * 64,
    ) == document
    with pytest.raises(android_relay_gate.GateError, match="digests"):
        android_relay_gate.read_gate_audit(
            plan,
            "a" * 32,
            expected_threads=8,
            expected_turns=500,
            android_tuple_sha256="c" * 64,
        )


def test_undersized_instrumentation_budget_is_rejected(tmp_path: Path) -> None:
    adb = tmp_path / "adb"
    ssh = tmp_path / "ssh"
    gate = tmp_path / "gate.apk"
    test = tmp_path / "test.apk"
    adb.write_text("#!/bin/sh\n", encoding="ascii")
    ssh.write_text("#!/bin/sh\n", encoding="ascii")
    adb.chmod(0o700)
    ssh.chmod(0o700)
    gate.write_bytes(b"gate")
    test.write_bytes(b"test")
    args = android_relay_gate.create_parser().parse_args(
        [
            "--mode",
            "headless-restart",
            "--adb",
            str(adb.resolve()),
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            str(gate.resolve()),
            "--test-apk",
            str(test.resolve()),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--ssh",
            str(ssh.resolve()),
            "--ssh-host",
            "remote-213",
            "--timeout-seconds",
            "900",
        ]
    )
    with pytest.raises(android_relay_gate.ConfigError, match="timeout"):
        android_relay_gate.validate_args(args)


@pytest.mark.parametrize(
    "container",
    (
        "chebycodex-r18-connector",
        "chebycodex-turkey-gate-connector-gate-connector-2",
        "chebycodex-turkey-gate-connector-gate-connector-1-copy",
    ),
)
def test_headless_gate_rejects_every_non_gate_connector_target(
    tmp_path: Path,
    container: str,
) -> None:
    adb = tmp_path / "adb"
    ssh = tmp_path / "ssh"
    gate = tmp_path / "gate.apk"
    test = tmp_path / "test.apk"
    for executable in (adb, ssh):
        executable.write_text("#!/bin/sh\n", encoding="ascii")
        executable.chmod(0o700)
    gate.write_bytes(b"gate")
    test.write_bytes(b"test")
    args = android_relay_gate.create_parser().parse_args(
        [
            "--mode",
            "headless-restart",
            "--adb",
            str(adb.resolve()),
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            str(gate.resolve()),
            "--test-apk",
            str(test.resolve()),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--ssh",
            str(ssh.resolve()),
            "--ssh-host",
            "remote-213",
            "--connector-container",
            container,
        ]
    )

    with pytest.raises(android_relay_gate.ConfigError, match="isolated Gate"):
        android_relay_gate.validate_args(args)


def test_headless_gate_accepts_only_the_fixed_gate_connector(
    tmp_path: Path,
) -> None:
    adb = tmp_path / "adb"
    ssh = tmp_path / "ssh"
    gate = tmp_path / "gate.apk"
    test = tmp_path / "test.apk"
    for executable in (adb, ssh):
        executable.write_text("#!/bin/sh\n", encoding="ascii")
        executable.chmod(0o700)
    gate.write_bytes(b"gate")
    test.write_bytes(b"test")
    args = android_relay_gate.create_parser().parse_args(
        [
            "--mode",
            "headless-restart",
            "--adb",
            str(adb.resolve()),
            "--serial",
            "SERIAL-1",
            "--gate-apk",
            str(gate.resolve()),
            "--test-apk",
            str(test.resolve()),
            "--manifest",
            str((tmp_path / "manifest.json").resolve()),
            "--ssh",
            str(ssh.resolve()),
            "--ssh-host",
            "remote-213",
            "--connector-container",
            "chebycodex-turkey-gate-connector-gate-connector-1",
        ]
    )

    _, _, _, restart_plan, audit_plan = android_relay_gate.validate_args(args)

    assert restart_plan is not None
    assert restart_plan.restart_command[-1] == (
        "chebycodex-turkey-gate-connector-gate-connector-1"
    )
    assert audit_plan is None


def test_checkpoint_status_two_is_ordered_and_skip_statuses_fail() -> None:
    evidence = passing_headless_evidence()
    android_relay_gate.validate_instrumentation_evidence(
        evidence,
        "headless-restart",
    )
    for rejected in (-3, -4):
        failed = passing_headless_evidence()
        failed.status_codes.append(rejected)
        with pytest.raises(
            android_relay_gate.GateError,
            match="failed, skipped or unknown",
        ):
            android_relay_gate.validate_instrumentation_evidence(
                failed,
                "headless-restart",
            )
    incomplete = passing_headless_evidence()
    incomplete.status_codes = [android_relay_gate.EVIDENCE_STATUS_CODE]
    incomplete.protocol_transcript = [
        {"kind": "status", "code": android_relay_gate.EVIDENCE_STATUS_CODE},
        {"kind": "final", "code": -1},
    ]
    with pytest.raises(android_relay_gate.GateError, match="sequence"):
        android_relay_gate.validate_instrumentation_evidence(
            incomplete,
            "headless-restart",
        )
    reordered = passing_headless_evidence()
    reordered.status_codes = [
        1,
        0,
        android_relay_gate.EVIDENCE_STATUS_CODE,
    ]
    reordered.protocol_transcript = [
        {"kind": "status", "code": 1},
        {"kind": "status", "code": 0},
        {"kind": "status", "code": android_relay_gate.EVIDENCE_STATUS_CODE},
        {"kind": "final", "code": -1},
    ]
    with pytest.raises(android_relay_gate.GateError, match="sequence"):
        android_relay_gate.validate_instrumentation_evidence(
            reordered,
            "headless-restart",
        )


def test_sha256_evidence_keys_with_digits_are_parsed() -> None:
    evidence = android_relay_gate.InstrumentationEvidence()
    android_relay_gate.parse_instrumentation_line(
        "INSTRUMENTATION_STATUS: cheby_gate_tuple_sha256=" + "a" * 64 + "\n",
        evidence,
    )
    android_relay_gate.parse_instrumentation_line(
        "INSTRUMENTATION_STATUS: cheby_gate_scope_generation_sha256="
        + "b" * 64
        + "\n",
        evidence,
    )
    assert evidence.values["cheby_gate_tuple_sha256"] == "a" * 64
    assert evidence.values["cheby_gate_scope_generation_sha256"] == "b" * 64


def test_parser_keeps_only_whitelisted_evidence_and_status_codes() -> None:
    evidence = android_relay_gate.InstrumentationEvidence()
    android_relay_gate.parse_instrumentation_line(
        "INSTRUMENTATION_STATUS: cheby_gate_checkpoint=connector_restart_required\n",
        evidence,
    )
    android_relay_gate.parse_instrumentation_line(
        f"INSTRUMENTATION_STATUS: cheby_gate_restart_nonce={'a' * 32}\n",
        evidence,
    )
    android_relay_gate.parse_instrumentation_line(
        "INSTRUMENTATION_STATUS: stream=credential-looking-value\n",
        evidence,
    )
    android_relay_gate.parse_instrumentation_line(
        "INSTRUMENTATION_STATUS_CODE: 2\n",
        evidence,
    )
    assert evidence.checkpoints == [android_relay_gate.RESTART_CHECKPOINT]
    assert evidence.values == {}
    assert evidence.restart_nonce == "a" * 32
    assert evidence.status_codes == [2]


def test_restart_is_not_ready_without_checkpoint_nonce_and_status_two() -> None:
    evidence = android_relay_gate.InstrumentationEvidence()
    assert not android_relay_gate.restart_checkpoint_ready(evidence)
    evidence.checkpoints.append(android_relay_gate.RESTART_CHECKPOINT)
    assert not android_relay_gate.restart_checkpoint_ready(evidence)
    evidence.restart_nonce = "a" * 32
    assert not android_relay_gate.restart_checkpoint_ready(evidence)
    evidence.status_codes.append(android_relay_gate.EVIDENCE_STATUS_CODE)
    assert android_relay_gate.restart_checkpoint_ready(evidence)


def test_load_evidence_requires_exact_counts_and_convergence() -> None:
    evidence = passing_load_evidence()
    android_relay_gate.validate_instrumentation_evidence(
        evidence,
        "load",
        expected_threads=8,
        expected_turns=500,
    )
    evidence.values["cheby_gate_outbox_entries"] = "1"
    with pytest.raises(android_relay_gate.GateError, match="outbox"):
        android_relay_gate.validate_instrumentation_evidence(
            evidence,
            "load",
            expected_threads=8,
            expected_turns=500,
        )


def test_manifest_is_private_and_contains_no_raw_instrumentation(tmp_path: Path) -> None:
    gate = tmp_path / "gate.apk"
    test = tmp_path / "gate-test.apk"
    gate.write_bytes(b"gate")
    test.write_bytes(b"test")
    gate_artifact = android_relay_gate.inspect_apk(str(gate.resolve()), "Gate APK")
    test_artifact = android_relay_gate.inspect_apk(str(test.resolve()), "Test APK")
    document = android_relay_gate.manifest_document(
        mode="headless-restart",
        outcome="PASS",
        gate_apk=gate_artifact,
        test_apk=test_artifact,
        installed_gate_sha256=gate_artifact.sha256,
        installed_test_sha256=test_artifact.sha256,
        evidence=passing_headless_evidence(),
        failure=None,
        duration_seconds=12.5,
    )
    manifest = tmp_path / "evidence" / "manifest.json"
    android_relay_gate.write_manifest(str(manifest.resolve()), document)
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["outcome"] == "PASS"
    assert payload["safeguards"]["screenCoordinatesSent"] == 0
    assert payload["safeguards"]["wifiCommandsSent"] == 0
    assert "rawOutput" not in payload["evidence"]


def test_execute_preserves_sanitized_partial_instrumentation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = android_relay_gate.Artifact(Path(__file__), "a" * 64, 1)
    args = SimpleNamespace(
        mode="quick-demo",
        serial="SERIAL-1",
        expected_reply=android_relay_gate.GATE_REPLY_MARKER,
        threads=8,
        turns=16,
        timeout_seconds=480.0,
    )
    monkeypatch.setattr(
        android_relay_gate,
        "validate_args",
        lambda unused: (Path(__file__), artifact, artifact, None, None),
    )
    monkeypatch.setattr(
        android_relay_gate,
        "install_and_verify",
        lambda *unused, **unused_kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        android_relay_gate,
        "run_bounded",
        lambda *unused, **unused_kwargs: b"",
    )

    def fail_with_partial_evidence(*unused, evidence, **unused_kwargs):
        android_relay_gate.parse_instrumentation_line(
            "INSTRUMENTATION_STATUS_CODE: 1\n",
            evidence,
        )
        raise android_relay_gate.GateError("Instrumentation timed out")

    monkeypatch.setattr(
        android_relay_gate,
        "run_instrumentation",
        fail_with_partial_evidence,
    )

    document, exit_code = android_relay_gate.execute(args)

    assert exit_code == 1
    assert document["failure"] == "Instrumentation timed out"
    assert document["evidence"]["statusCodes"] == [1]
    assert document["evidence"]["protocolTranscript"] == [
        {"kind": "status", "code": 1},
    ]


def test_manifest_writer_rejects_existing_regular_file_and_symlink(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    existing = evidence / "existing.json"
    existing.write_text("preserve-existing\n", encoding="utf-8")
    with pytest.raises(android_relay_gate.ConfigError, match="already exists"):
        android_relay_gate.write_manifest(str(existing.resolve()), {"outcome": "PASS"})
    assert existing.read_text(encoding="utf-8") == "preserve-existing\n"

    victim = evidence / "victim.json"
    victim.write_text("preserve-victim\n", encoding="utf-8")
    linked = evidence / "linked.json"
    linked.symlink_to(victim)
    with pytest.raises(android_relay_gate.ConfigError, match="already exists"):
        android_relay_gate.write_manifest(str(linked.absolute()), {"outcome": "PASS"})
    assert linked.is_symlink()
    assert victim.read_text(encoding="utf-8") == "preserve-victim\n"


@pytest.mark.parametrize("unsafe_mode", (0o770, 0o707, 0o777))
def test_manifest_writer_rejects_unsafe_parent_directory(
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(unsafe_mode)
    manifest = unsafe / "manifest.json"

    with pytest.raises(
        android_relay_gate.ConfigError,
        match="private owned directory",
    ):
        android_relay_gate.write_manifest(
            str(manifest.resolve()),
            {"outcome": "PASS"},
        )
    assert not manifest.exists()


def test_manifest_writer_rejects_symlink_parent(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    manifest = linked / "manifest.json"

    with pytest.raises(
        android_relay_gate.ConfigError,
        match="private owned directory",
    ):
        android_relay_gate.write_manifest(
            str(manifest.absolute()),
            {"outcome": "PASS"},
        )
    assert not (private / "manifest.json").exists()


def test_manifest_writer_does_not_overwrite_target_created_during_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "evidence" / "manifest.json"
    real_link = os.link
    real_open = os.open

    def racing_link(
        source,
        destination,
        *,
        src_dir_fd,
        dst_dir_fd,
        follow_symlinks,
    ):
        descriptor = real_open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(b"preserve-racer\n")
        return real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(android_relay_gate.os, "link", racing_link)
    with pytest.raises(android_relay_gate.ConfigError, match="already exists"):
        android_relay_gate.write_manifest(str(manifest.resolve()), {"outcome": "PASS"})
    assert manifest.read_text(encoding="utf-8") == "preserve-racer\n"
    assert list(manifest.parent.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    ("failure_kind", "failure_call"),
    (
        ("stat", 2),
        ("fsync", 2),
        ("fsync", 3),
    ),
)
def test_manifest_writer_removes_published_pass_after_commit_failure(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
    failure_call: int,
) -> None:
    manifest = tmp_path / "evidence" / "manifest.json"
    real_stat = os.stat
    real_fsync = os.fsync
    matching_calls = 0
    injected = False

    def failing_stat(path, *args, **kwargs):
        nonlocal matching_calls, injected
        if (
            path == manifest.name
            and kwargs.get("dir_fd") is not None
            and not kwargs.get("follow_symlinks", True)
        ):
            matching_calls += 1
            if matching_calls == failure_call and not injected:
                injected = True
                raise OSError("injected published stat failure")
        return real_stat(path, *args, **kwargs)

    def failing_fsync(descriptor):
        nonlocal matching_calls, injected
        matching_calls += 1
        if matching_calls == failure_call and not injected:
            injected = True
            raise OSError("injected directory fsync failure")
        return real_fsync(descriptor)

    if failure_kind == "stat":
        monkeypatch.setattr(android_relay_gate.os, "stat", failing_stat)
    else:
        monkeypatch.setattr(android_relay_gate.os, "fsync", failing_fsync)

    with pytest.raises(
        android_relay_gate.ConfigError,
        match="could not be written safely",
    ):
        android_relay_gate.write_manifest(
            str(manifest.resolve()),
            {"outcome": "PASS"},
        )
    assert injected
    assert not manifest.exists()
    assert list(manifest.parent.glob(".*.tmp")) == []

    android_relay_gate.write_manifest(
        str(manifest.resolve()),
        {"outcome": "FAIL"},
    )
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "outcome": "FAIL"
    }


def test_manifest_writer_removes_final_after_published_inode_validation_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "evidence" / "manifest.json"
    real_stat = os.stat
    matching_calls = 0

    def mismatched_stat(path, *args, **kwargs):
        nonlocal matching_calls
        result = real_stat(path, *args, **kwargs)
        if (
            path == manifest.name
            and kwargs.get("dir_fd") is not None
            and not kwargs.get("follow_symlinks", True)
        ):
            matching_calls += 1
            if matching_calls == 1:
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino + 1,
                    st_nlink=result.st_nlink,
                )
        return result

    monkeypatch.setattr(android_relay_gate.os, "stat", mismatched_stat)
    with pytest.raises(
        android_relay_gate.ConfigError,
        match="published file is invalid",
    ):
        android_relay_gate.write_manifest(
            str(manifest.resolve()),
            {"outcome": "PASS"},
        )
    assert not manifest.exists()
    assert list(manifest.parent.glob(".*.tmp")) == []

    android_relay_gate.write_manifest(
        str(manifest.resolve()),
        {"outcome": "FAIL"},
    )
    assert json.loads(manifest.read_text()) == {"outcome": "FAIL"}


def test_relay_runner_rejects_existing_manifest_before_execute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("preserve\n", encoding="utf-8")
    executed = False

    def forbidden_execute(args):
        nonlocal executed
        executed = True
        raise AssertionError("execute must not run")

    monkeypatch.setattr(android_relay_gate, "execute", forbidden_execute)
    result = android_relay_gate.main(
        [
            "--mode",
            "headless-restart",
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
        ]
    )
    assert result == 2
    assert executed is False
    assert manifest.read_text(encoding="utf-8") == "preserve\n"
