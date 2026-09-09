from __future__ import annotations

import hashlib
import json
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import server_sigkill_release_gate as gate


def passing_android_document(
    gate_apk_sha256: str = "a" * 64,
    test_apk_sha256: str = "b" * 64,
) -> dict[str, object]:
    return {
        "schema": gate.ANDROID_SCHEMA,
        "outcome": "PASS",
        "artifacts": {
            "gateApk": {
                "sha256": gate_apk_sha256,
                "installedSha256": gate_apk_sha256,
            },
            "testApk": {
                "sha256": test_apk_sha256,
                "installedSha256": test_apk_sha256,
            },
        },
        "evidence": {
            "boundaries": dict(gate.ANDROID_BOUNDARIES),
            "hostForceStops": 125,
            "abnormalPrepareTerminations": 125,
            "independentRecoveryProcesses": 125,
            "turns": 375,
            "threads": 250,
            "maxTurnCountPerBusinessKey": 1,
            "maxEffectCountPerBusinessKey": 1,
            "duplicateBubbles": 0,
            "finalOutboxEntries": 0,
            "outboxZeroRecoveryProcesses": 125,
        },
        "safeguards": {
            "activityLaunches": 0,
            "screenCoordinatesSent": 0,
            "screenClicksSent": 0,
            "screenTextInputsSent": 0,
            "arbitraryRemoteCommands": 0,
            "fixedRepetitions": 25,
        },
    }


def passing_result() -> gate.MatrixResult:
    cases = tuple(
        gate.CaseResult(
            name=checkpoint.junit_name,
            outcome="PASSED",
            duration_seconds=1.0,
        )
        for checkpoint in gate.CHECKPOINTS
    )
    return gate.MatrixResult(
        pytest_exit_code=0,
        cases=cases,
        passed=8,
        failed=0,
        errors=0,
        skipped=0,
        missing=(),
        unexpected=(),
        duplicate=(),
    )


def context(tmp_path: Path) -> gate.Context:
    artifact = gate.Artifact(
        sha256="c" * 64,
        size_bytes=123,
        inode=1,
        device=1,
    )
    return gate.Context(
        repo_root=tmp_path,
        python=Path("/usr/bin/python3"),
        source=gate.Source(
            revision="d" * 40,
            tree="e" * 40,
            crash_test_sha256="f" * 64,
        ),
        android_manifest=artifact,
        gate_apk=gate.Artifact("a" * 64, 456, 2, 1),
        test_apk=gate.Artifact("b" * 64, 789, 3, 1),
        android_document=passing_android_document(),
    )


def write_junit(
    path: Path,
    cases: list[tuple[str, str]],
) -> None:
    suite = ET.Element("testsuite")
    for name, outcome in cases:
        case = ET.SubElement(suite, "testcase", name=name, time="0.25")
        if outcome == "FAILED":
            ET.SubElement(case, "failure")
        elif outcome == "ERROR":
            ET.SubElement(case, "error")
        elif outcome == "SKIPPED":
            ET.SubElement(case, "skipped")
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def test_fixed_matrix_is_eight_checkpoints_and_two_hundred_sigkills() -> None:
    assert len(gate.CHECKPOINTS) == 8
    assert gate.REPETITIONS == 25
    assert len(gate.CHECKPOINTS) * gate.REPETITIONS == 200
    assert len(gate.EXPECTED_JUNIT_NAMES) == 8
    assert [item.public_boundary for item in gate.CHECKPOINTS] == [
        3,
        5,
        6,
        7,
        9,
        None,
        None,
        None,
    ]
    assert [item[0] for item in gate.PUBLIC_BOUNDARIES] == list(range(1, 11))


def test_reviewed_crash_test_source_proves_fixed_twenty_five_iteration_contract(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / gate.TEST_FILE
    assert gate.SHA256.fullmatch(gate.validate_crash_test_contract(source))

    mutated = tmp_path / "test_production_crash_matrix.py"
    payload = source.read_text(encoding="utf-8")
    mutated.write_text(
        payload.replace("builtins.range(25)", "builtins.range(24)", 1),
        encoding="utf-8",
    )
    with pytest.raises(gate.ConfigError, match="repetition contract"):
        gate.validate_crash_test_contract(mutated)

    mutated.write_text(
        payload.replace(
            "for iteration in builtins.range(25):",
            "for iteration in builtins.range(25):\n            break",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.ConfigError, match="repetition contract"):
        gate.validate_crash_test_contract(mutated)

    mutated.write_text(
        payload.replace(
            "import builtins",
            "import builtins\nbuiltins = type('Fake', (), {'range': lambda _n: ()})",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.ConfigError, match="builtin range contract"):
        gate.validate_crash_test_contract(mutated)

    mutated.write_text(
        payload.replace(
            "assert completed_iterations == 25",
            "assert completed_iterations == 24",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.ConfigError, match="runtime count contract"):
        gate.validate_crash_test_contract(mutated)

    mutated.write_text(
        payload.replace(
            "            await harness.sigkill_relay_command_commit(a1)",
            (
                "            if True:\n"
                "                await harness.sigkill_relay_command_commit(a1)"
            ),
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(gate.ConfigError, match="repetition contract"):
        gate.validate_crash_test_contract(mutated)


def test_pytest_command_has_only_exact_nodes_and_disables_retry_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--lf --reruns 9")
    monkeypatch.setenv("PYTEST_PLUGINS", "pytest_rerunfailures")
    command = gate.build_pytest_command(
        Path("/private/python"),
        tmp_path,
        tmp_path / "report.xml",
    )
    assert command[-8:] == [item.pytest_node_id for item in gate.CHECKPOINTS]
    assert "--maxfail=1" in command
    assert "pytest_asyncio.plugin" in command
    assert "addopts=" in command
    assert "xfail_strict=true" in command
    environment = gate.pytest_environment(tmp_path)
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_ADDOPTS" not in environment
    assert "PYTEST_PLUGINS" not in environment
    assert environment["PYTHONHASHSEED"] == "0"


def test_complete_junit_is_accepted(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    write_junit(
        report,
        [(item.junit_name, "PASSED") for item in gate.CHECKPOINTS],
    )
    result = gate.parse_junit(report, 0)
    gate.require_passing_matrix(result)
    assert result.passed == 8
    assert result.skipped == 0
    assert not result.partial


@pytest.mark.parametrize(
    ("mutation", "exit_code", "failure_code"),
    (
        ("skip", 0, "MATRIX_SKIPPED"),
        ("failure", 1, "MATRIX_FAILED"),
        ("missing", 1, "MATRIX_PARTIAL"),
        ("duplicate", 0, "MATRIX_PARTIAL"),
        ("unexpected", 0, "MATRIX_PARTIAL"),
    ),
)
def test_skip_failure_flaky_or_partial_report_fails_closed(
    tmp_path: Path,
    mutation: str,
    exit_code: int,
    failure_code: str,
) -> None:
    cases = [(item.junit_name, "PASSED") for item in gate.CHECKPOINTS]
    if mutation == "skip":
        cases[0] = (cases[0][0], "SKIPPED")
    elif mutation == "failure":
        cases[0] = (cases[0][0], "FAILED")
    elif mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases[-1] = cases[0]
    elif mutation == "unexpected":
        cases[-1] = ("test_not_in_release_gate", "PASSED")
    report = tmp_path / f"{mutation}.xml"
    write_junit(report, cases)
    result = gate.parse_junit(report, exit_code)
    with pytest.raises(gate.GateFailure) as caught:
        gate.require_passing_matrix(result)
    assert caught.value.code == failure_code
    assert caught.value.result is result


def test_invalid_or_non_finite_junit_duration_is_rejected(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    write_junit(report, [(gate.CHECKPOINTS[0].junit_name, "PASSED")])
    tree = ET.parse(report)
    next(tree.getroot().iter("testcase")).set("time", "nan")
    tree.write(report, encoding="utf-8")
    with pytest.raises(gate.GateFailure, match="PYTEST_REPORT_INVALID"):
        gate.parse_junit(report, 0)


def test_android_manifest_must_bind_complete_matrix_and_both_apks() -> None:
    document = passing_android_document()
    assert gate.validate_android_document(document, "a" * 64, "b" * 64) is document

    partial = passing_android_document()
    evidence = partial["evidence"]
    assert isinstance(evidence, dict)
    boundaries = evidence["boundaries"]
    assert isinstance(boundaries, dict)
    boundaries["CLEANED"] = 24
    with pytest.raises(gate.ConfigError, match="boundary matrix"):
        gate.validate_android_document(partial, "a" * 64, "b" * 64)

    wrong_apk = passing_android_document()
    with pytest.raises(gate.ConfigError, match="APK evidence"):
        gate.validate_android_document(wrong_apk, "f" * 64, "b" * 64)


def test_android_manifest_rejects_skip_equivalent_and_bad_invariants() -> None:
    document = passing_android_document()
    evidence = document["evidence"]
    assert isinstance(evidence, dict)
    evidence["hostForceStops"] = 124
    with pytest.raises(gate.ConfigError, match="incomplete"):
        gate.validate_android_document(document, "a" * 64, "b" * 64)

    document = passing_android_document()
    evidence = document["evidence"]
    assert isinstance(evidence, dict)
    evidence["maxEffectCountPerBusinessKey"] = 2
    with pytest.raises(gate.ConfigError, match="invariants"):
        gate.validate_android_document(document, "a" * 64, "b" * 64)


def test_artifact_hash_mode_and_symlink_are_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "android.json"
    source.write_bytes(b"evidence")
    source.chmod(0o600)
    digest = hashlib.sha256(b"evidence").hexdigest()
    artifact = gate.inspect_artifact(
        str(source.resolve()),
        digest,
        "Android exact-crash manifest",
        require_mode_0600=True,
    )
    assert artifact.sha256 == digest

    source.chmod(0o644)
    with pytest.raises(gate.ConfigError, match="mode 0600"):
        gate.inspect_artifact(
            str(source.resolve()),
            digest,
            "Android exact-crash manifest",
            require_mode_0600=True,
        )

    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(gate.ConfigError, match="regular file"):
        gate.inspect_artifact(str(link), digest, "Android exact-crash manifest")

    with pytest.raises(gate.ConfigError, match="did not match"):
        gate.inspect_artifact(str(source.resolve()), "f" * 64, "Gate APK")


def test_manifest_binds_revision_hashes_checkpoints_and_public_boundaries(
    tmp_path: Path,
) -> None:
    document = gate.manifest_document(
        context(tmp_path),
        outcome="PASS",
        duration_seconds=12.5,
        result=passing_result(),
    )
    assert document["outcome"] == "PASS"
    assert document["source"] == {
        "revision": "d" * 40,
        "tree": "e" * 40,
        "crashTestSha256": "f" * 64,
        "worktreeCleanBefore": True,
        "worktreeCleanAfterVerified": True,
    }
    matrix = document["matrix"]
    assert isinstance(matrix, dict)
    assert matrix["checkpointCount"] == 8
    assert matrix["expectedSigkills"] == 200
    assert matrix["observedPassingIterations"] == 200
    checkpoints = matrix["checkpoints"]
    assert isinstance(checkpoints, list)
    assert [item["checkpoint"] for item in checkpoints] == [
        item.checkpoint for item in gate.CHECKPOINTS
    ]
    assert len(document["publicBoundaryCoverage"]) == 10
    payload = json.dumps(document, sort_keys=True)
    safeguards = document["safeguards"]
    assert isinstance(safeguards, dict)
    assert safeguards["rawPytestOutputPersisted"] is False
    assert "captured stdout" not in payload
    assert "/private/" not in payload


def test_fail_manifest_is_not_pass_evidence_and_contains_only_failure_code(
    tmp_path: Path,
) -> None:
    result = passing_result()
    failed = gate.MatrixResult(
        pytest_exit_code=1,
        cases=result.cases[:-1],
        passed=7,
        failed=0,
        errors=0,
        skipped=0,
        missing=(gate.CHECKPOINTS[-1].junit_name,),
        unexpected=(),
        duplicate=(),
    )
    document = gate.manifest_document(
        context(tmp_path),
        outcome="FAIL",
        duration_seconds=1,
        result=failed,
        failure_code="MATRIX_PARTIAL",
    )
    assert document["outcome"] == "FAIL"
    assert document["failure"] == {"code": "MATRIX_PARTIAL"}
    results = document["results"]
    assert isinstance(results, dict)
    assert results["partial"] is True
    assert document["source"]["worktreeCleanAfterVerified"] is False


def test_main_writes_fresh_private_manifest_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    manifest = private / "server-sigkill.json"
    prepared = context(tmp_path)
    monkeypatch.setattr(gate, "prepare_context", lambda _args: prepared)
    monkeypatch.setattr(
        gate,
        "run_matrix",
        lambda _context, _junit_path: passing_result(),
    )
    monkeypatch.setattr(gate, "verify_unchanged", lambda _context, _args: None)
    arguments = [
        "--source-revision",
        "d" * 40,
        "--android-manifest",
        str((tmp_path / "android.json").resolve()),
        "--android-manifest-sha256",
        "c" * 64,
        "--gate-apk",
        str((tmp_path / "gate.apk").resolve()),
        "--gate-apk-sha256",
        "a" * 64,
        "--test-apk",
        str((tmp_path / "test.apk").resolve()),
        "--test-apk-sha256",
        "b" * 64,
        "--manifest",
        str(manifest.resolve()),
    ]
    assert gate.main(arguments) == 0
    payload = manifest.read_bytes()
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert json.loads(payload)["outcome"] == "PASS"

    assert gate.main(arguments) == 2
    assert manifest.read_bytes() == payload


def test_shared_manifest_writer_forces_mode_0600_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "restrictive-umask.json"
    previous = os.umask(0o777)
    try:
        written = gate.relay_gate.write_manifest(
            str(manifest.resolve()),
            {"schema": gate.SCHEMA, "outcome": "PASS"},
        )
    finally:
        os.umask(previous)
    assert written == manifest.resolve()
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert json.loads(manifest.read_text(encoding="utf-8"))["outcome"] == "PASS"


def test_no_retry_or_flaky_markers_can_be_smuggled_through_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in (
        ("PYTEST_ADDOPTS", "--reruns 100"),
        ("PYTEST_PLUGINS", "pytest_rerunfailures"),
        ("PYTEST_CURRENT_TEST", "fake"),
    ):
        monkeypatch.setenv(key, value)
    environment = gate.pytest_environment(tmp_path)
    assert all(
        key not in environment
        for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST")
    )
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert os.pathsep.join(
        str(tmp_path / name) for name in ("connector", "gateway", "relay")
    ) == environment["PYTHONPATH"]
