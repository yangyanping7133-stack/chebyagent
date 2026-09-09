#!/usr/bin/env python3
"""Deterministic negative-certificate gate with an immutable safe manifest."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Sequence
import xml.etree.ElementTree as ET

import edge_gate


REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST_SCHEMA = "chebycodex.certificate-failure-gate.v1"
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
MAX_JUNIT_BYTES = 1024 * 1024
GROUP_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class CaseGroup:
    case_id: str
    pytest_nodes: tuple[str, ...]
    expected_tests: int
    coverage: tuple[str, ...]


CASE_GROUPS = (
    CaseGroup(
        "candidate-rejections",
        (
            "deploy/relay-edge/tests/test_certificate_tools.py::"
            "test_candidate_with_wrong_private_key_is_rejected_without_changing_current",
            "deploy/relay-edge/tests/test_certificate_tools.py::"
            "test_candidate_with_wrong_ip_san_is_rejected_without_changing_current",
            "deploy/relay-edge/tests/test_certificate_tools.py::"
            "test_candidate_with_wrong_chain_is_rejected_without_changing_current",
        ),
        3,
        ("wrong-private-key", "wrong-ip-san", "wrong-chain"),
    ),
    CaseGroup(
        "previous-release-validation",
        (
            "deploy/relay-edge/tests/test_certificate_tools.py::"
            "test_missing_previous_release_fails_without_changing_current",
            "deploy/relay-edge/tests/test_certificate_tools.py::"
            "test_invalid_previous_release_fails_without_changing_current",
        ),
        2,
        ("previous-missing", "previous-invalid"),
    ),
    CaseGroup(
        "recovery-fail-closed",
        (
            "deploy/relay-edge/tests/test_certificate_shell_recovery.py::"
            "test_recovery_failures_deactivate_current_and_stop_both_edges",
        ),
        6,
        (
            "previous-rollback-failure",
            "previous-missing-recovery",
            "previous-invalid-recovery",
            "nginx-config-failure",
            "reload-failure",
            "fingerprint-mismatch",
        ),
    ),
    CaseGroup(
        "verified-previous-recovery",
        (
            "deploy/relay-edge/tests/test_certificate_shell_recovery.py::"
            "test_verified_previous_reloads_without_deactivation_or_stop",
        ),
        1,
        ("verified-previous-restored",),
    ),
    CaseGroup(
        "bootstrap-nginx-rollback",
        (
            "deploy/relay-edge/tests/test_certificate_shell_recovery.py::"
            "test_bootstrap_nginx_validation_failure_invokes_verified_rollback",
        ),
        1,
        ("bootstrap-nginx-config-failure",),
    ),
    CaseGroup(
        "rotation-post-activation-rollback",
        (
            "deploy/relay-edge/tests/test_certificate_shell_recovery.py::"
            "test_rotation_post_activation_failure_invokes_verified_rollback",
        ),
        2,
        ("rotation-reload-failure", "rotation-fingerprint-mismatch"),
    ),
)

SOURCE_PATHS = (
    "deploy/relay-edge/certificate_manager.py",
    "deploy/relay-edge/relay_edge_common.sh",
    "deploy/relay-edge/recover_certificate.sh",
    "deploy/relay-edge/rotate_certificate.sh",
    "deploy/relay-edge/tests/test_certificate_tools.py",
    "deploy/relay-edge/tests/test_certificate_shell_recovery.py",
    "tools/gate/edge_gate.py",
    "tools/gate/certificate_failure_gate.py",
)


class FailureGateError(edge_gate.GateError):
    pass


def _python_executable(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise FailureGateError("Python executable path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise FailureGateError("Python executable is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise FailureGateError("Python executable is unsafe")
    # Preserve the reviewed venv entry path. Resolving the final symlink before
    # execution would bypass its adjacent pyvenv.cfg and silently lose pytest.
    return path


def _source_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in SOURCE_PATHS:
        path = REPOSITORY / relative
        try:
            metadata = path.lstat()
            content = path.read_bytes()
        except OSError as error:
            raise FailureGateError("negative-gate source is unavailable") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or len(content) > MAX_JUNIT_BYTES
        ):
            raise FailureGateError("negative-gate source is unsafe")
        result[relative] = hashlib.sha256(content).hexdigest()
    return result


def parse_junit(path: Path) -> tuple[int, int, int, int]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_size <= 0
            or metadata.st_size > MAX_JUNIT_BYTES
        ):
            raise FailureGateError("negative-gate JUnit artifact is unsafe")
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as error:
        raise FailureGateError("negative-gate JUnit artifact is invalid") from error
    testcases = list(root.iter("testcase"))
    failures = sum(
        any(child.tag == "failure" for child in testcase)
        for testcase in testcases
    )
    errors = sum(
        any(child.tag == "error" for child in testcase)
        for testcase in testcases
    )
    skipped = sum(
        any(child.tag == "skipped" for child in testcase)
        for testcase in testcases
    )
    return len(testcases), failures, errors, skipped


def run_group(
    python: Path,
    group: CaseGroup,
    artifact_directory: Path,
) -> dict[str, object]:
    junit = artifact_directory / f"{group.case_id}.xml"
    command = [
        str(python),
        "-I",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-o",
        "xfail_strict=true",
        "--disable-warnings",
        f"--junitxml={junit}",
        *group.pytest_nodes,
    ]
    environment = os.environ.copy()
    for inherited in (
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_PLUGINS",
    ):
        environment.pop(inherited, None)
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPYCACHEPREFIX": str(artifact_directory / "pycache"),
        }
    )
    try:
        completed = subprocess.run(
            command,
            cwd=REPOSITORY,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=GROUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "caseId": group.case_id,
            "coverage": list(group.coverage),
            "expectedTests": group.expected_tests,
            "observedTests": 0,
            "state": "FAIL",
        }
    if (
        len(completed.stdout) > MAX_PROCESS_OUTPUT_BYTES
        or len(completed.stderr) > MAX_PROCESS_OUTPUT_BYTES
    ):
        return {
            "caseId": group.case_id,
            "coverage": list(group.coverage),
            "expectedTests": group.expected_tests,
            "observedTests": 0,
            "state": "FAIL",
        }
    try:
        tests, failures, errors, skipped = parse_junit(junit)
    except FailureGateError:
        tests, failures, errors, skipped = 0, 1, 0, 0
    passed = (
        completed.returncode == 0
        and tests == group.expected_tests
        and failures == 0
        and errors == 0
        and skipped == 0
    )
    return {
        "caseId": group.case_id,
        "coverage": list(group.coverage),
        "expectedTests": group.expected_tests,
        "observedTests": tests,
        "state": "PASS" if passed else "FAIL",
    }


def run_gate(python: Path, manifest_path: str) -> dict[str, object]:
    edge_gate.ensure_immutable_manifest_absent(manifest_path)
    source_hashes_before = _source_hashes()
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(
            prefix="cheby-certificate-negative-"
        ) as temporary:
            artifact_directory = Path(temporary)
            artifact_directory.chmod(0o700)
            groups = [
                run_group(python, group, artifact_directory)
                for group in CASE_GROUPS
            ]
    finally:
        os.umask(previous_umask)
    source_hashes_after = _source_hashes()
    source_integrity = (
        "PASS"
        if source_hashes_after == source_hashes_before
        else "FAIL"
    )
    outcome = (
        "PASS"
        if (
            source_integrity == "PASS"
            and all(group["state"] == "PASS" for group in groups)
        )
        else "FAIL"
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "createdAt": edge_gate._utc_now(),
        "outcome": outcome,
        "expectedTests": sum(group.expected_tests for group in CASE_GROUPS),
        "observedTests": sum(
            int(group["observedTests"]) for group in groups
        ),
        "sourceIntegrity": source_integrity,
        "groups": groups,
        "sourceSha256": source_hashes_before,
    }
    edge_gate.write_immutable_manifest(manifest_path, manifest)
    return manifest


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed negative-certificate suite and write an immutable "
            "sanitized manifest"
        )
    )
    parser.add_argument(
        "--python",
        required=True,
        help="absolute reviewed Python executable containing pytest dependencies",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="absolute new path in a private directory for the mode-0600 manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        python = _python_executable(args.python)
        manifest = run_gate(python, args.manifest)
    except edge_gate.GateError as error:
        print(f"certificate failure gate setup failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            "certificate failure gate setup failed: "
            f"internal error ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2
    print(
        "certificate failure gate: "
        f"{manifest['outcome']} tests={manifest['observedTests']}/"
        f"{manifest['expectedTests']}"
    )
    return 0 if manifest["outcome"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
