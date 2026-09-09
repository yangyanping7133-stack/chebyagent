#!/usr/bin/env python3
"""Fail-closed release evidence for the eight server SIGKILL checkpoints.

The gate runs only the reviewed production crash tests.  Each test owns a
fixed 25-iteration loop.  Pytest plugin auto-loading and retry plugins are
disabled, the exact eight test node IDs are required, and a complete JUnit
report is validated before a PASS manifest can be created.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import android_relay_gate as relay_gate


SCHEMA = "chebycodex.server-sigkill-release-gate.v1"
ANDROID_SCHEMA = "chebycodex.android-exact-crash-gate.v1"
REPETITIONS = 25
PYTEST_TIMEOUT_SECONDS = 3_600
SHA256 = re.compile(r"^[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40,64}$")


@dataclass(frozen=True)
class Checkpoint:
    checkpoint: str
    pytest_node_id: str
    junit_name: str
    public_boundary: int | None


TEST_FILE = "connector/tests/test_production_crash_matrix.py"
PARAMETER_TEST = (
    "test_production_subprocess_is_sigkilled_at_committed_checkpoint_25_times"
)
CHECKPOINTS = (
    Checkpoint(
        checkpoint="relay_command_committed",
        pytest_node_id=(
            f"{TEST_FILE}::"
            "test_relay_command_commit_is_sigkilled_and_recovered_twenty_five_times"
        ),
        junit_name=(
            "test_relay_command_commit_is_sigkilled_and_recovered_twenty_five_times"
        ),
        public_boundary=3,
    ),
    Checkpoint(
        checkpoint="connector_execution_intent",
        pytest_node_id=(
            f"{TEST_FILE}::{PARAMETER_TEST}[connector_execution_intent]"
        ),
        junit_name=f"{PARAMETER_TEST}[connector_execution_intent]",
        public_boundary=5,
    ),
    Checkpoint(
        checkpoint="gateway_turn_reservation",
        pytest_node_id=(
            f"{TEST_FILE}::{PARAMETER_TEST}[gateway_turn_reservation]"
        ),
        junit_name=f"{PARAMETER_TEST}[gateway_turn_reservation]",
        public_boundary=6,
    ),
    Checkpoint(
        checkpoint="scripted_codex_accepted",
        pytest_node_id=(
            f"{TEST_FILE}::{PARAMETER_TEST}[scripted_codex_accepted]"
        ),
        junit_name=f"{PARAMETER_TEST}[scripted_codex_accepted]",
        public_boundary=7,
    ),
    Checkpoint(
        checkpoint="gateway_terminal_committed",
        pytest_node_id=(
            f"{TEST_FILE}::{PARAMETER_TEST}[gateway_terminal_committed]"
        ),
        junit_name=f"{PARAMETER_TEST}[gateway_terminal_committed]",
        public_boundary=9,
    ),
    Checkpoint(
        checkpoint="connector_response_committed",
        pytest_node_id=(
            f"{TEST_FILE}::{PARAMETER_TEST}[connector_response_committed]"
        ),
        junit_name=f"{PARAMETER_TEST}[connector_response_committed]",
        public_boundary=None,
    ),
    Checkpoint(
        checkpoint="relay_response_committed",
        pytest_node_id=(
            f"{TEST_FILE}::"
            "test_relay_response_commit_is_sigkilled_and_replayed_twenty_five_times"
        ),
        junit_name=(
            "test_relay_response_commit_is_sigkilled_and_replayed_twenty_five_times"
        ),
        public_boundary=None,
    ),
    Checkpoint(
        checkpoint="connector_response_outbox_removed",
        pytest_node_id=(
            f"{TEST_FILE}::"
            "test_connector_outbox_removal_is_sigkilled_and_durable_twenty_five_times"
        ),
        junit_name=(
            "test_connector_outbox_removal_is_sigkilled_and_durable_twenty_five_times"
        ),
        public_boundary=None,
    ),
)
EXPECTED_JUNIT_NAMES = frozenset(item.junit_name for item in CHECKPOINTS)
ANDROID_BOUNDARIES = {
    "ENQUEUE": 25,
    "NETWORK": 25,
    "ACCEPTED": 25,
    "ANDROID": 25,
    "CLEANED": 25,
}
PUBLIC_BOUNDARIES = (
    (1, "ENQUEUE_PERSISTED", "android", "ENQUEUE"),
    (2, "NETWORK_SENT_UNCONFIRMED", "android", "NETWORK"),
    (3, "RELAY_SUBMITTED", "server", "relay_command_committed"),
    (4, "ACCEPTED_SENT/RECEIVED", "android", "ACCEPTED"),
    (5, "CONNECTOR_INTENT", "server", "connector_execution_intent"),
    (6, "GATEWAY_RESERVATION", "server", "gateway_turn_reservation"),
    (7, "CODEX_ACCEPTED", "server", "scripted_codex_accepted"),
    (8, "ANDROID_ACCEPTED", "android", "ANDROID"),
    (9, "TERMINAL_COMMITTED", "server", "gateway_terminal_committed"),
    (10, "OUTBOX_CLEANED", "android", "CLEANED"),
)


class ConfigError(RuntimeError):
    """Sanitized configuration error."""


class GateFailure(RuntimeError):
    """Sanitized release-gate failure."""

    def __init__(self, code: str, result: MatrixResult | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class Artifact:
    sha256: str
    size_bytes: int
    inode: int
    device: int


@dataclass(frozen=True)
class Source:
    revision: str
    tree: str
    crash_test_sha256: str


@dataclass(frozen=True)
class CaseResult:
    name: str
    outcome: str
    duration_seconds: float


@dataclass(frozen=True)
class MatrixResult:
    pytest_exit_code: int
    cases: tuple[CaseResult, ...]
    passed: int
    failed: int
    errors: int
    skipped: int
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    duplicate: tuple[str, ...]

    @property
    def partial(self) -> bool:
        return bool(self.missing or self.unexpected or self.duplicate)


@dataclass(frozen=True)
class Context:
    repo_root: Path
    python: Path
    source: Source
    android_manifest: Artifact
    gate_apk: Artifact
    test_apk: Artifact
    android_document: Mapping[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    mapping: Mapping[str, Any],
    key: str,
    *,
    exact: int | None = None,
    maximum: int | None = None,
) -> int:
    value = mapping.get(key)
    if not _is_int(value) or int(value) < 0:
        raise ConfigError("Android exact-crash manifest counters are invalid")
    number = int(value)
    if exact is not None and number != exact:
        raise ConfigError("Android exact-crash manifest counters are incomplete")
    if maximum is not None and number > maximum:
        raise ConfigError("Android exact-crash manifest invariants failed")
    return number


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} is invalid")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_artifact(
    path_text: str,
    expected_sha256: str,
    label: str,
    *,
    require_mode_0600: bool = False,
    maximum_size: int | None = None,
) -> Artifact:
    if not SHA256.fullmatch(expected_sha256):
        raise ConfigError(f"{label} SHA-256 is invalid")
    path = Path(path_text)
    if not path.is_absolute():
        raise ConfigError(f"{label} path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigError(f"{label} must be a regular file")
    if require_mode_0600 and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigError(f"{label} must be mode 0600")
    if metadata.st_size <= 0 or (
        maximum_size is not None and metadata.st_size > maximum_size
    ):
        raise ConfigError(f"{label} size is invalid")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ConfigError(f"{label} SHA-256 did not match")
    after = path.lstat()
    if (
        after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or not stat.S_ISREG(after.st_mode)
    ):
        raise ConfigError(f"{label} changed during inspection")
    return Artifact(
        sha256=actual,
        size_bytes=metadata.st_size,
        inode=metadata.st_ino,
        device=metadata.st_dev,
    )


def validate_android_document(
    document: object,
    gate_apk_sha256: str,
    test_apk_sha256: str,
) -> Mapping[str, Any]:
    root = _require_mapping(document, "Android exact-crash manifest")
    if root.get("schema") != ANDROID_SCHEMA or root.get("outcome") != "PASS":
        raise ConfigError("Android exact-crash manifest is not PASS evidence")
    artifacts = _require_mapping(root.get("artifacts"), "Android artifacts")
    gate = _require_mapping(artifacts.get("gateApk"), "Android Gate APK evidence")
    test = _require_mapping(artifacts.get("testApk"), "Android test APK evidence")
    if (
        gate.get("sha256") != gate_apk_sha256
        or gate.get("installedSha256") != gate_apk_sha256
        or test.get("sha256") != test_apk_sha256
        or test.get("installedSha256") != test_apk_sha256
    ):
        raise ConfigError("Android exact-crash APK evidence did not match")
    evidence = _require_mapping(root.get("evidence"), "Android crash evidence")
    if evidence.get("boundaries") != ANDROID_BOUNDARIES:
        raise ConfigError("Android exact-crash boundary matrix is incomplete")
    for key, expected in (
        ("hostForceStops", 125),
        ("abnormalPrepareTerminations", 125),
        ("independentRecoveryProcesses", 125),
        ("turns", 375),
        ("threads", 250),
        ("maxTurnCountPerBusinessKey", 1),
        ("duplicateBubbles", 0),
        ("finalOutboxEntries", 0),
        ("outboxZeroRecoveryProcesses", 125),
    ):
        _require_int(evidence, key, exact=expected)
    _require_int(evidence, "maxEffectCountPerBusinessKey", maximum=1)
    safeguards = _require_mapping(root.get("safeguards"), "Android safeguards")
    for key in (
        "activityLaunches",
        "screenCoordinatesSent",
        "screenClicksSent",
        "screenTextInputsSent",
        "arbitraryRemoteCommands",
    ):
        _require_int(safeguards, key, exact=0)
    _require_int(safeguards, "fixedRepetitions", exact=REPETITIONS)
    return root


def read_android_document(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ConfigError("Android exact-crash manifest size is invalid")
        return _require_mapping(
            json.loads(raw.decode("utf-8")),
            "Android exact-crash manifest",
        )
    except ConfigError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("Android exact-crash manifest is invalid") from error


def _run_git(repo_root: Path, git: Path, arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            [str(git), "-C", str(repo_root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigError("Git source inspection failed") from error
    if result.returncode != 0:
        raise ConfigError("Git source inspection failed")
    return result.stdout.strip()


def validate_crash_test_contract(path: Path) -> str:
    expected_functions = {
        (
            "test_relay_command_commit_is_sigkilled_and_recovered_"
            "twenty_five_times"
        ): "sigkill_relay_command_commit",
        (
            "test_relay_response_commit_is_sigkilled_and_replayed_"
            "twenty_five_times"
        ): "sigkill_relay_response_commit",
        (
            "test_connector_outbox_removal_is_sigkilled_and_durable_"
            "twenty_five_times"
        ): "sigkill_connector_outbox_removal",
        PARAMETER_TEST: "sigkill_at",
    }
    expected_parameters = (
        "ExactSigkillBoundary.CONNECTOR_EXECUTION_INTENT.value",
        "ExactSigkillBoundary.GATEWAY_TURN_RESERVATION.value",
        "ExactSigkillBoundary.SCRIPTED_CODEX_ACCEPTED.value",
        "ExactSigkillBoundary.GATEWAY_TERMINAL_COMMITTED.value",
        "ExactSigkillBoundary.CONNECTOR_RESPONSE_COMMITTED.value",
    )
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > 2 * 1024 * 1024
        ):
            raise ConfigError("Server crash-test source is invalid")
        raw = path.read_bytes()
        tree = ast.parse(raw, filename=TEST_FILE)
    except ConfigError:
        raise
    except (OSError, SyntaxError) as error:
        raise ConfigError("Server crash-test source is invalid") from error
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    builtins_imports = [
        alias
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "builtins"
    ]
    builtins_rebound = any(
        (
            isinstance(node, ast.Name)
            and node.id == "builtins"
            and isinstance(node.ctx, (ast.Store, ast.Del))
        )
        or (
            isinstance(node, ast.arg)
            and node.arg == "builtins"
        )
        or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "builtins"
            and node.attr == "range"
            and isinstance(node.ctx, (ast.Store, ast.Del))
        )
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "builtins"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "range"
        )
        for node in ast.walk(tree)
    )
    if (
        len(builtins_imports) != 1
        or builtins_imports[0].asname is not None
        or builtins_rebound
    ):
        raise ConfigError("Server SIGKILL builtin range contract changed")
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    if not set(expected_functions).issubset(functions):
        raise ConfigError("Server SIGKILL test contract is incomplete")
    for function_name, sigkill_method in expected_functions.items():
        function = functions[function_name]
        loops = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "iteration"
            and isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Attribute)
            and isinstance(node.iter.func.value, ast.Name)
            and node.iter.func.value.id == "builtins"
            and node.iter.func.attr == "range"
            and len(node.iter.args) == 1
            and isinstance(node.iter.args[0], ast.Constant)
            and node.iter.args[0].value == REPETITIONS
            and not node.iter.keywords
        ]
        if len(loops) != 1:
            raise ConfigError("Server SIGKILL test repetition contract changed")
        loop = loops[0]
        loop_parent = parents.get(loop)
        if (
            not isinstance(loop_parent, ast.Try)
            or loop not in loop_parent.body
            or parents.get(loop_parent) is not function
        ):
            raise ConfigError("Server SIGKILL test repetition contract changed")
        loop_index = loop_parent.body.index(loop)
        before = loop_parent.body[loop_index - 1] if loop_index > 0 else None
        after = (
            loop_parent.body[loop_index + 1]
            if loop_index + 1 < len(loop_parent.body)
            else None
        )
        counter_initialized = (
            isinstance(before, ast.Assign)
            and len(before.targets) == 1
            and isinstance(before.targets[0], ast.Name)
            and before.targets[0].id == "completed_iterations"
            and isinstance(before.value, ast.Constant)
            and before.value.value == 0
        )
        counter_incremented = (
            bool(loop.body)
            and isinstance(loop.body[-1], ast.AugAssign)
            and isinstance(loop.body[-1].target, ast.Name)
            and loop.body[-1].target.id == "completed_iterations"
            and isinstance(loop.body[-1].op, ast.Add)
            and isinstance(loop.body[-1].value, ast.Constant)
            and loop.body[-1].value.value == 1
        )
        counter_asserted = (
            isinstance(after, ast.Assert)
            and isinstance(after.test, ast.Compare)
            and isinstance(after.test.left, ast.Name)
            and after.test.left.id == "completed_iterations"
            and len(after.test.ops) == 1
            and isinstance(after.test.ops[0], ast.Eq)
            and len(after.test.comparators) == 1
            and isinstance(after.test.comparators[0], ast.Constant)
            and after.test.comparators[0].value == REPETITIONS
        )
        if not (counter_initialized and counter_incremented and counter_asserted):
            raise ConfigError("Server SIGKILL runtime count contract changed")
        if any(
            isinstance(node, (ast.Break, ast.Continue, ast.Return))
            for node in ast.walk(function)
        ):
            raise ConfigError("Server SIGKILL test repetition contract changed")
        sigkill_calls = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == sigkill_method
        ]
        if len(sigkill_calls) != 1:
            raise ConfigError("Server SIGKILL test repetition contract changed")
        call = sigkill_calls[0]
        awaited = parents.get(call)
        expression = parents.get(awaited)
        if (
            not isinstance(awaited, ast.Await)
            or not isinstance(expression, ast.Expr)
            or parents.get(expression) is not loop
            or expression not in loop.body
        ):
            raise ConfigError("Server SIGKILL test repetition contract changed")
    parameter_assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "SIGKILL_BOUNDARIES"
                for target in node.targets
            )
        ),
        None,
    )
    if (
        parameter_assignment is None
        or not isinstance(parameter_assignment.value, ast.Tuple)
        or tuple(ast.unparse(value) for value in parameter_assignment.value.elts)
        != expected_parameters
    ):
        raise ConfigError("Server SIGKILL checkpoint contract changed")
    parameter_function = functions[PARAMETER_TEST]
    has_exact_parameterization = any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "parametrize"
        and len(decorator.args) >= 2
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == "boundary"
        and isinstance(decorator.args[1], ast.Name)
        and decorator.args[1].id == "SIGKILL_BOUNDARIES"
        for decorator in parameter_function.decorator_list
    )
    if not has_exact_parameterization:
        raise ConfigError("Server SIGKILL parameterization contract changed")
    after = path.lstat()
    if (
        after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or not stat.S_ISREG(after.st_mode)
    ):
        raise ConfigError("Server crash-test source changed during inspection")
    return hashlib.sha256(raw).hexdigest()


def inspect_source(
    repo_root: Path,
    git: Path,
    expected_revision: str,
) -> Source:
    if not repo_root.is_absolute() or not repo_root.is_dir():
        raise ConfigError("Repository root is invalid")
    if not git.is_absolute() or not git.is_file():
        raise ConfigError("Git executable is invalid")
    if not REVISION.fullmatch(expected_revision):
        raise ConfigError("Source revision is invalid")
    revision = _run_git(repo_root, git, ("rev-parse", "--verify", "HEAD"))
    tree = _run_git(repo_root, git, ("rev-parse", "--verify", "HEAD^{tree}"))
    status = _run_git(
        repo_root,
        git,
        ("status", "--porcelain=v1", "--untracked-files=all"),
    )
    if revision != expected_revision or not REVISION.fullmatch(tree):
        raise ConfigError("Source revision did not match HEAD")
    if status:
        raise ConfigError("Source worktree must be clean")
    return Source(
        revision=revision,
        tree=tree,
        crash_test_sha256=validate_crash_test_contract(
            repo_root / TEST_FILE
        ),
    )


def build_pytest_command(
    python: Path,
    repo_root: Path,
    junit_path: Path,
) -> list[str]:
    return [
        str(python),
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-c",
        str(repo_root / "connector" / "pyproject.toml"),
        "--strict-markers",
        "-o",
        "addopts=",
        "-o",
        "xfail_strict=true",
        "--maxfail=1",
        "--junitxml",
        str(junit_path),
        *(item.pytest_node_id for item in CHECKPOINTS),
    ]


def pytest_environment(repo_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        environment.pop(key, None)
    environment.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": os.pathsep.join(
                str(repo_root / name) for name in ("connector", "gateway", "relay")
            ),
        }
    )
    return environment


def _case_outcome(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "FAILED"
    if case.find("error") is not None:
        return "ERROR"
    if case.find("skipped") is not None:
        return "SKIPPED"
    return "PASSED"


def parse_junit(path: Path, pytest_exit_code: int) -> MatrixResult:
    try:
        if path.lstat().st_size > 2 * 1024 * 1024:
            raise GateFailure("PYTEST_REPORT_INVALID")
        root = ET.parse(path).getroot()
    except GateFailure:
        raise
    except (OSError, ET.ParseError) as error:
        raise GateFailure("PYTEST_REPORT_INVALID") from error
    cases: list[CaseResult] = []
    for case in root.iter("testcase"):
        name = case.get("name", "")
        duration_text = case.get("time", "")
        try:
            duration = float(duration_text)
        except ValueError as error:
            raise GateFailure("PYTEST_REPORT_INVALID") from error
        if not name or not math.isfinite(duration) or duration < 0:
            raise GateFailure("PYTEST_REPORT_INVALID")
        cases.append(
            CaseResult(
                name=name,
                outcome=_case_outcome(case),
                duration_seconds=round(duration, 6),
            )
        )
    names = [case.name for case in cases]
    observed = set(names)
    missing = tuple(sorted(EXPECTED_JUNIT_NAMES - observed))
    unexpected = tuple(sorted(observed - EXPECTED_JUNIT_NAMES))
    duplicate = tuple(sorted(name for name in observed if names.count(name) != 1))
    return MatrixResult(
        pytest_exit_code=pytest_exit_code,
        cases=tuple(cases),
        passed=sum(case.outcome == "PASSED" for case in cases),
        failed=sum(case.outcome == "FAILED" for case in cases),
        errors=sum(case.outcome == "ERROR" for case in cases),
        skipped=sum(case.outcome == "SKIPPED" for case in cases),
        missing=missing,
        unexpected=unexpected,
        duplicate=duplicate,
    )


def require_passing_matrix(result: MatrixResult) -> None:
    if result.partial or len(result.cases) != len(CHECKPOINTS):
        raise GateFailure("MATRIX_PARTIAL", result)
    if result.skipped:
        raise GateFailure("MATRIX_SKIPPED", result)
    if result.failed or result.errors or result.pytest_exit_code != 0:
        raise GateFailure("MATRIX_FAILED", result)
    if result.passed != len(CHECKPOINTS):
        raise GateFailure("MATRIX_PARTIAL", result)


def run_matrix(context: Context, junit_path: Path) -> MatrixResult:
    command = build_pytest_command(context.python, context.repo_root, junit_path)
    try:
        process = subprocess.run(
            command,
            cwd=context.repo_root,
            env=pytest_environment(context.repo_root),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise GateFailure("MATRIX_TIMEOUT") from error
    except OSError as error:
        raise GateFailure("MATRIX_EXECUTION_ERROR") from error
    if not junit_path.exists():
        raise GateFailure("PYTEST_REPORT_MISSING")
    result = parse_junit(junit_path, process.returncode)
    require_passing_matrix(result)
    return result


def _checkpoint_results(
    result: MatrixResult | None,
) -> list[dict[str, object]]:
    by_name = {case.name: case for case in result.cases} if result else {}
    values: list[dict[str, object]] = []
    for checkpoint in CHECKPOINTS:
        case = by_name.get(checkpoint.junit_name)
        values.append(
            {
                "checkpoint": checkpoint.checkpoint,
                "publicBoundary": checkpoint.public_boundary,
                "pytestNodeId": checkpoint.pytest_node_id,
                "repetitions": REPETITIONS,
                "outcome": case.outcome if case else "MISSING",
                "durationSeconds": case.duration_seconds if case else None,
            }
        )
    return values


def _result_document(result: MatrixResult | None) -> dict[str, object]:
    if result is None:
        return {
            "pytestExitCode": None,
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "flakyRetries": 0,
            "partial": True,
        }
    return {
        "pytestExitCode": result.pytest_exit_code,
        "collected": len(result.cases),
        "passed": result.passed,
        "failed": result.failed,
        "errors": result.errors,
        "skipped": result.skipped,
        "flakyRetries": 0,
        "partial": result.partial or len(result.cases) != len(CHECKPOINTS),
    }


def manifest_document(
    context: Context,
    *,
    outcome: str,
    duration_seconds: float,
    result: MatrixResult | None,
    failure_code: str | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": SCHEMA,
        "createdAt": _utc_now(),
        "outcome": outcome,
        "durationSeconds": round(duration_seconds, 3),
        "source": {
            "revision": context.source.revision,
            "tree": context.source.tree,
            "crashTestSha256": context.source.crash_test_sha256,
            "worktreeCleanBefore": True,
            "worktreeCleanAfterVerified": outcome == "PASS",
        },
        "inputs": {
            "androidExactCrashManifest": {
                "sha256": context.android_manifest.sha256,
                "sizeBytes": context.android_manifest.size_bytes,
                "schema": ANDROID_SCHEMA,
                "outcome": "PASS",
            },
            "gateApk": {
                "sha256": context.gate_apk.sha256,
                "sizeBytes": context.gate_apk.size_bytes,
            },
            "testApk": {
                "sha256": context.test_apk.sha256,
                "sizeBytes": context.test_apk.size_bytes,
            },
        },
        "matrix": {
            "checkpointCount": len(CHECKPOINTS),
            "repetitionsPerCheckpoint": REPETITIONS,
            "expectedSigkills": len(CHECKPOINTS) * REPETITIONS,
            "observedPassingCheckpoints": (
                result.passed if result is not None else 0
            ),
            "observedPassingIterations": (
                result.passed * REPETITIONS if result is not None else 0
            ),
            "checkpoints": _checkpoint_results(result),
        },
        "results": _result_document(result),
        "publicBoundaryCoverage": [
            {
                "boundary": number,
                "name": name,
                "owner": owner,
                "checkpoint": checkpoint,
                "repetitions": REPETITIONS,
                "evidenceSha256": (
                    context.android_manifest.sha256 if owner == "android" else None
                ),
            }
            for number, name, owner, checkpoint in PUBLIC_BOUNDARIES
        ],
        "safeguards": {
            "pytestPluginAutoload": False,
            "retryPluginsLoaded": 0,
            "fixedTestNodeIds": len(CHECKPOINTS),
            "fixedRepetitions": REPETITIONS,
            "rawPytestOutputPersisted": False,
        },
    }
    if failure_code is not None:
        document["failure"] = {"code": failure_code}
    return document


def _same_artifact(before: Artifact, after: Artifact) -> bool:
    return before == after


def prepare_context(args: argparse.Namespace) -> Context:
    repo_root = Path(args.repo_root).resolve()
    python = Path(args.python)
    git = Path(args.git)
    if not python.is_absolute() or not python.is_file():
        raise ConfigError("Python executable is invalid")
    source = inspect_source(repo_root, git, args.source_revision)
    android_manifest = inspect_artifact(
        args.android_manifest,
        args.android_manifest_sha256,
        "Android exact-crash manifest",
        require_mode_0600=True,
        maximum_size=2 * 1024 * 1024,
    )
    gate_apk = inspect_artifact(
        args.gate_apk,
        args.gate_apk_sha256,
        "Gate APK",
        maximum_size=1024 * 1024 * 1024,
    )
    test_apk = inspect_artifact(
        args.test_apk,
        args.test_apk_sha256,
        "Gate test APK",
        maximum_size=1024 * 1024 * 1024,
    )
    android_document = validate_android_document(
        read_android_document(Path(args.android_manifest)),
        gate_apk.sha256,
        test_apk.sha256,
    )
    return Context(
        repo_root=repo_root,
        python=python,
        source=source,
        android_manifest=android_manifest,
        gate_apk=gate_apk,
        test_apk=test_apk,
        android_document=android_document,
    )


def verify_unchanged(context: Context, args: argparse.Namespace) -> None:
    source = inspect_source(
        context.repo_root,
        Path(args.git),
        context.source.revision,
    )
    if source != context.source:
        raise GateFailure("SOURCE_CHANGED")
    checks = (
        (
            context.android_manifest,
            inspect_artifact(
                args.android_manifest,
                context.android_manifest.sha256,
                "Android exact-crash manifest",
                require_mode_0600=True,
                maximum_size=2 * 1024 * 1024,
            ),
        ),
        (
            context.gate_apk,
            inspect_artifact(
                args.gate_apk,
                context.gate_apk.sha256,
                "Gate APK",
                maximum_size=1024 * 1024 * 1024,
            ),
        ),
        (
            context.test_apk,
            inspect_artifact(
                args.test_apk,
                context.test_apk.sha256,
                "Gate test APK",
                maximum_size=1024 * 1024 * 1024,
            ),
        ),
    )
    if not all(_same_artifact(before, after) for before, after in checks):
        raise GateFailure("INPUT_EVIDENCE_CHANGED")


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo-root", default=str(root))
    value.add_argument("--python", default=sys.executable)
    value.add_argument("--git", default="/usr/bin/git")
    value.add_argument("--source-revision", required=True)
    value.add_argument("--android-manifest", required=True)
    value.add_argument("--android-manifest-sha256", required=True)
    value.add_argument("--gate-apk", required=True)
    value.add_argument("--gate-apk-sha256", required=True)
    value.add_argument("--test-apk", required=True)
    value.add_argument("--test-apk-sha256", required=True)
    value.add_argument("--manifest", required=True)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = relay_gate.require_new_manifest_path(args.manifest)
        context = prepare_context(args)
    except (ConfigError, relay_gate.ConfigError) as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    started = time.monotonic()
    result: MatrixResult | None = None
    failure: GateFailure | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".server-sigkill-gate-",
            dir=manifest.parent,
        ) as temporary:
            result = run_matrix(context, Path(temporary) / "pytest.xml")
        verify_unchanged(context, args)
    except GateFailure as error:
        failure = error
        result = error.result or result
    except ConfigError:
        failure = GateFailure("INPUT_EVIDENCE_CHANGED", result)
    document = manifest_document(
        context,
        outcome="FAIL" if failure else "PASS",
        duration_seconds=time.monotonic() - started,
        result=result,
        failure_code=failure.code if failure else None,
    )
    try:
        written = relay_gate.write_manifest(str(manifest), document)
    except relay_gate.ConfigError as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"{document['outcome']} manifest={written}")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
