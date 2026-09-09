from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

import certificate_failure_gate


def test_negative_gate_has_fixed_complete_coverage() -> None:
    coverage = {
        item
        for group in certificate_failure_gate.CASE_GROUPS
        for item in group.coverage
    }
    assert {
        "wrong-private-key",
        "wrong-ip-san",
        "wrong-chain",
        "nginx-config-failure",
        "reload-failure",
        "fingerprint-mismatch",
        "previous-missing",
        "previous-invalid",
        "previous-rollback-failure",
    }.issubset(coverage)
    assert sum(
        group.expected_tests
        for group in certificate_failure_gate.CASE_GROUPS
    ) == 15
    assert all(
        node.startswith("deploy/relay-edge/tests/")
        for group in certificate_failure_gate.CASE_GROUPS
        for node in group.pytest_nodes
    )


def test_junit_parser_counts_failures_errors_and_skips(tmp_path: Path) -> None:
    junit = tmp_path / "results.xml"
    junit.write_text(
        """<?xml version="1.0"?>
<testsuite>
  <testcase name="pass"/>
  <testcase name="fail"><failure/></testcase>
  <testcase name="error"><error/></testcase>
  <testcase name="skip"><skipped/></testcase>
</testsuite>
""",
        encoding="ascii",
    )
    assert certificate_failure_gate.parse_junit(junit) == (4, 1, 1, 1)


def test_negative_gate_writes_private_immutable_sanitized_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    manifest_path = private / "certificate-negative.json"

    def fake_group(_python, group, _artifact_directory):
        return {
            "caseId": group.case_id,
            "coverage": list(group.coverage),
            "expectedTests": group.expected_tests,
            "observedTests": group.expected_tests,
            "state": "PASS",
        }

    monkeypatch.setattr(certificate_failure_gate, "run_group", fake_group)
    document = certificate_failure_gate.run_gate(
        Path("/reviewed/python"),
        str(manifest_path),
    )
    assert document["outcome"] == "PASS"
    assert document["sourceIntegrity"] == "PASS"
    assert document["expectedTests"] == document["observedTests"] == 15
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    serialized = manifest_path.read_text()
    assert "/reviewed/python" not in serialized
    assert "pytest" not in serialized
    assert json.loads(serialized) == document
    with pytest.raises(
        certificate_failure_gate.edge_gate.GateError,
        match="already exists",
    ):
        certificate_failure_gate.run_gate(
            Path("/reviewed/python"),
            str(manifest_path),
        )


def test_group_clears_pytest_injection_and_forces_strict_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_PLUGINS",
    ):
        monkeypatch.setenv(name, "attacker-controlled")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "0")

    group = certificate_failure_gate.CaseGroup(
        case_id="environment-isolation",
        pytest_nodes=("deploy/relay-edge/tests/test_certificate_tools.py",),
        expected_tests=1,
        coverage=("environment-isolation",),
    )

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        junit_argument = next(
            item for item in command if item.startswith("--junitxml=")
        )
        Path(junit_argument.split("=", 1)[1]).write_text(
            "<testsuite><testcase name=\"pass\"/></testsuite>",
            encoding="ascii",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(certificate_failure_gate.subprocess, "run", fake_run)
    result = certificate_failure_gate.run_group(
        Path("/reviewed/python"),
        group,
        tmp_path,
    )

    assert result["state"] == "PASS"
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert all(
        name not in environment
        for name in (
            "PYTEST_ADDOPTS",
            "PYTEST_CURRENT_TEST",
            "PYTEST_PLUGINS",
        )
    )
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    command = observed["command"]
    assert isinstance(command, list)
    assert command.count("-o") == 2
    assert "addopts=" in command
    assert "xfail_strict=true" in command


def test_source_mutation_forces_failure_and_manifest_binds_pretest_hashes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    manifest_path = private / "certificate-negative.json"
    reviewed_source = tmp_path / "reviewed.py"
    reviewed_source.write_text("reviewed-before\n", encoding="ascii")
    before = {
        "reviewed.py": hashlib.sha256(
            reviewed_source.read_bytes()
        ).hexdigest()
    }
    monkeypatch.setattr(certificate_failure_gate, "REPOSITORY", tmp_path)
    monkeypatch.setattr(
        certificate_failure_gate,
        "SOURCE_PATHS",
        ("reviewed.py",),
    )

    def fake_group(_python, group, _artifact_directory):
        reviewed_source.write_text("mutated-during-test\n", encoding="ascii")
        return {
            "caseId": group.case_id,
            "coverage": list(group.coverage),
            "expectedTests": group.expected_tests,
            "observedTests": group.expected_tests,
            "state": "PASS",
        }

    monkeypatch.setattr(certificate_failure_gate, "run_group", fake_group)
    document = certificate_failure_gate.run_gate(
        Path("/reviewed/python"),
        str(manifest_path),
    )
    assert document["outcome"] == "FAIL"
    assert document["sourceIntegrity"] == "FAIL"
    assert document["sourceSha256"] == before
    assert json.loads(manifest_path.read_text())["sourceSha256"] == before
