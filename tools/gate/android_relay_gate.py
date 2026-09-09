#!/usr/bin/env python3
"""Host-side Android Relay gate with restart-aware, sanitized evidence.

The runner installs only the supplied Gate APKs, force-stops their packages and
runs instrumentation. It never launches an Activity, sends coordinates/text
input, or changes Wi-Fi. In headless-restart mode it restarts the isolated Gate
Connector only after the Android test emits its first-Turn checkpoint.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


TARGET_PACKAGE = "com.cheby.codex.mobile.gate"
TEST_PACKAGE = "com.cheby.codex.mobile.gate.test"
RUNNER_COMPONENT = f"{TEST_PACKAGE}/androidx.test.runner.AndroidJUnitRunner"
HEADLESS_CLASS = (
    "com.cheby.codex.mobile.gateway.RelayHeadlessE2EInstrumentedTest"
    "#pairedPrivateSessionSupportsThreadsTurnsAndReconstruction"
)
LOAD_CLASS = (
    "com.cheby.codex.mobile.gateway.RelayLoadGateInstrumentedTest"
    "#pairedGateSupportsProductionQueueLoadAndRecovery"
)
RESTART_CHECKPOINT = "connector_restart_required"
HEADLESS_COMPLETE_CHECKPOINT = "headless_gate_complete"
LOAD_COMPLETE_CHECKPOINT = "load_gate_complete"
EVIDENCE_STATUS_CODE = 2
RESTART_ACK_FILE = "cheby-gate-restart-ack-v1"
RESTART_NONCE = re.compile(r"^[a-f0-9]{32}$")
CONNECTOR_EPOCH = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z$")
SAFE_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SAFE_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
EXPECTED_GATE_CONNECTOR_CONTAINERS = frozenset(
    {"chebycodex-turkey-gate-connector-gate-connector-1"}
)
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
STATUS_LINE = re.compile(
    r"^INSTRUMENTATION_STATUS: (cheby_gate_[a-z0-9_]+)=(.{0,512})$"
)
REMOTE_AUDIT_PROGRAM = "/usr/local/sbin/chebycodex-audit-gate-business-keys"
REMOTE_AUDIT_SCHEMA = "chebycodex.gate-business-key-audit.v1"
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_APK_BYTES = 256 * 1024 * 1024
MAX_LINE_CHARS = 16 * 1024
GATE_REPLY_MARKER = "CHEBY_GATE_REPLY_V1_b71f3d69f2f14e25a4c71c5db6632f8e"
ALLOWED_EVIDENCE_KEYS = frozenset(
    {
        "cheby_gate_checkpoint",
        "cheby_gate_restart_observed",
        "cheby_gate_restart_causal_ack",
        "cheby_gate_restart_nonce",
        "cheby_gate_remembered_marker_verified",
        "cheby_gate_threads",
        "cheby_gate_turns",
        "cheby_gate_business_keys",
        "cheby_gate_outbox_entries",
        "cheby_gate_duplicate_bubbles",
        "cheby_gate_first_last_verified",
        "cheby_gate_max_concurrent",
        "cheby_gate_server_barrier_threads",
        "cheby_gate_production_queue",
        "cheby_gate_run_token",
        "cheby_gate_tuple_sha256",
        "cheby_gate_scope_generation_sha256",
        "cheby_gate_scope_generation_pairs",
        "cheby_gate_scope_generation_observations",
        "cheby_gate_soak_elapsed_millis",
        "cheby_gate_soak_monotonic_verified",
        "cheby_gate_failure_stage",
        "cheby_gate_selected_thread_match",
        "cheby_gate_conversation_present",
        "cheby_gate_conversation_thread_match",
        "cheby_gate_can_send",
        "cheby_gate_conversation_recovering",
        "cheby_gate_conversation_resync",
        "cheby_gate_connection_state",
        "cheby_gate_gateway_kind",
        "cheby_gate_outbox_count",
        "cheby_gate_threads_visible",
    }
)


class GateError(RuntimeError):
    """Sanitized operational failure safe to include in a manifest."""


class ConfigError(GateError):
    """Invalid local operator input."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str
    size_bytes: int


@dataclass
class InstrumentationEvidence:
    checkpoints: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    status_codes: list[int] = field(default_factory=list)
    protocol_transcript: list[dict[str, object]] = field(default_factory=list)
    instrumentation_code: int | None = None
    junit_ok: bool = False
    junit_failed: bool = False
    restart_executed: bool = False
    restart_succeeded: bool = False
    restart_nonce: str | None = None
    restart_ack_written: bool = False
    connector_epoch_before_sha256: str | None = None
    connector_epoch_after_sha256: str | None = None
    remote_audit: dict[str, object] | None = None


@dataclass(frozen=True)
class RestartPlan:
    restart_command: tuple[str, ...]
    epoch_command: tuple[str, ...]
    adb: Path
    serial: str


@dataclass(frozen=True)
class AuditPlan:
    ssh: Path
    ssh_host: str


def require_regular_file(path_text: str, label: str, executable: bool = False) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        raise ConfigError(f"{label} path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigError(f"{label} file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ConfigError(f"{label} must be a regular non-symlink file")
    if executable and not os.access(path, os.X_OK):
        raise ConfigError(f"{label} is not executable")
    return path


def inspect_apk(path_text: str, label: str) -> Artifact:
    path = require_regular_file(path_text, label)
    size = path.stat().st_size
    if size <= 0 or size > MAX_APK_BYTES:
        raise ConfigError(f"{label} size is outside the approved range")
    return Artifact(path=path, sha256=sha256_file(path), size_bytes=size)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if value.startswith("-") or pattern.fullmatch(value) is None:
        raise ConfigError(f"{label} is invalid")
    return value


def validate_expected_reply(value: str) -> str:
    if value != GATE_REPLY_MARKER:
        raise ConfigError("Expected reply marker must match the frozen Gate contract")
    return value


def encode_expected_reply(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def adb_prefix(adb: Path, serial: str) -> list[str]:
    return [str(adb), "-s", serial]


def build_install_command(adb: Path, serial: str, apk: Path) -> list[str]:
    return [*adb_prefix(adb, serial), "install", "-r", "-t", str(apk)]


def build_force_stop_commands(adb: Path, serial: str) -> list[list[str]]:
    return [
        [*adb_prefix(adb, serial), "shell", "am", "force-stop", TEST_PACKAGE],
        [*adb_prefix(adb, serial), "shell", "am", "force-stop", TARGET_PACKAGE],
    ]


def build_instrumentation_command(
    adb: Path,
    serial: str,
    mode: str,
    expected_reply: str,
    threads: int,
    turns: int,
) -> list[str]:
    encoded_reply = encode_expected_reply(expected_reply)
    if mode == "headless-restart":
        test_class = HEADLESS_CLASS
        arguments = [
            "-e",
            "cheby_relay_e2e",
            "true",
            "-e",
            "cheby_relay_expected_reply_b64url",
            encoded_reply,
        ]
    elif mode in {"load", "quick-demo"}:
        test_class = LOAD_CLASS
        arguments = [
            "-e",
            "cheby_relay_load_gate",
            "true",
            "-e",
            "cheby_relay_load_threads",
            str(threads),
            "-e",
            "cheby_relay_load_turns",
            str(turns),
            "-e",
            "cheby_relay_expected_reply_b64url",
            encoded_reply,
        ]
    else:
        raise ConfigError("Unknown Android Relay gate mode")
    return [
        *adb_prefix(adb, serial),
        "shell",
        "am",
        "instrument",
        "-w",
        "-r",
        "-e",
        "class",
        test_class,
        *arguments,
        RUNNER_COMPONENT,
    ]


def build_restart_command(
    ssh: Path,
    ssh_host: str,
    connector_container: str,
) -> list[str]:
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "docker",
        "restart",
        "--time",
        "20",
        connector_container,
    ]


def build_epoch_command(
    ssh: Path,
    ssh_host: str,
    connector_container: str,
) -> list[str]:
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "docker",
        "inspect",
        "--format={{.State.StartedAt}}",
        connector_container,
    ]


def build_gate_audit_command(
    ssh: Path,
    ssh_host: str,
    run_token: str,
    mode: str = "load",
) -> list[str]:
    if RESTART_NONCE.fullmatch(run_token) is None:
        raise GateError("Android Gate run token was invalid")
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "sudo",
        "-n",
        REMOTE_AUDIT_PROGRAM,
        "--mode",
        mode,
        "--run-token",
        run_token,
    ]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("Remote Gate audit emitted duplicate JSON keys")
        result[key] = value
    return result


def read_gate_audit(
    plan: AuditPlan,
    run_token: str,
    *,
    expected_threads: int,
    expected_turns: int,
    android_tuple_sha256: str,
    mode: str = "load",
) -> dict[str, object]:
    output = run_bounded(
        build_gate_audit_command(plan.ssh, plan.ssh_host, run_token, mode),
        timeout_seconds=120,
        label="Gate business-key audit",
    )
    try:
        text = output.decode("utf-8", errors="strict")
        document = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GateError("Remote Gate audit output was malformed") from error
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "outcome",
        "runToken",
        "counts",
        "digests",
    }:
        raise GateError("Remote Gate audit schema was invalid")
    counts = document.get("counts")
    digests = document.get("digests")
    if not isinstance(counts, dict) or set(counts) != {
        "turns",
        "threads",
        "relayTuples",
        "connectorTuples",
        "gatewayTuples",
        "fakeCodexTuples",
        "bindingGenerationMappings",
        "relevantConnectorIntents",
        "relevantConnectorOutbox",
        "relevantConnectorProcessedDeliveries",
    }:
        raise GateError("Remote Gate audit count schema was invalid")
    if not isinstance(digests, dict) or set(digests) != {
        "tupleSha256",
        "relayTupleSha256",
        "connectorTupleSha256",
        "gatewayTupleSha256",
        "fakeCodexTupleSha256",
        "bindingGenerationMappingSha256",
    }:
        raise GateError("Remote Gate audit digest schema was invalid")
    if (
        document.get("schema") != REMOTE_AUDIT_SCHEMA
        or document.get("outcome") != "PASS"
        or document.get("runToken") != run_token
    ):
        raise GateError("Remote Gate audit did not identify this load run")
    exact_counts = {
        "turns": expected_turns,
        "threads": expected_threads,
        "relayTuples": expected_turns,
        "connectorTuples": expected_turns,
        "gatewayTuples": expected_turns,
        "fakeCodexTuples": expected_turns,
        "bindingGenerationMappings": 1,
        "relevantConnectorIntents": 0,
        "relevantConnectorOutbox": 0,
        "relevantConnectorProcessedDeliveries": 0,
    }
    if any(type(counts.get(key)) is not int for key in exact_counts):
        raise GateError("Remote Gate audit count type was invalid")
    if counts != exact_counts:
        raise GateError("Remote Gate audit counts did not match the load gate")
    digest_values = list(digests.values())
    if any(
        not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None
        for value in digest_values
    ):
        raise GateError("Remote Gate audit digest was invalid")
    tuple_digests = [
        str(digests[key])
        for key in (
            "tupleSha256",
            "relayTupleSha256",
            "connectorTupleSha256",
            "gatewayTupleSha256",
            "fakeCodexTupleSha256",
        )
    ]
    if (
        len(set(tuple_digests)) != 1
        or tuple_digests[0] != android_tuple_sha256
    ):
        raise GateError("Android and host business-key digests did not match")
    return document


def build_restart_ack_command(adb: Path, serial: str) -> list[str]:
    return [
        *adb_prefix(adb, serial),
        "exec-out",
        "run-as",
        TARGET_PACKAGE,
        "sh",
        "-c",
        f"umask 077; cat > files/{RESTART_ACK_FILE}",
    ]


def run_bounded(command: Sequence[str], timeout_seconds: float, label: str) -> bytes:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError(f"{label} did not complete") from error
    if len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise GateError(f"{label} output exceeded the approved limit")
    if result.returncode != 0:
        raise GateError(f"{label} failed")
    return result.stdout


def read_connector_epoch(command: Sequence[str]) -> tuple[str, str]:
    output = run_bounded(
        command,
        timeout_seconds=30,
        label="Gate Connector epoch inspection",
    )
    lines = output.decode("utf-8", errors="replace").splitlines()
    if len(lines) != 1 or CONNECTOR_EPOCH.fullmatch(lines[0].strip()) is None:
        raise GateError("Gate Connector epoch was invalid")
    epoch = lines[0].strip()
    return epoch, hashlib.sha256(epoch.encode("ascii")).hexdigest()


def write_restart_acknowledgement(
    adb: Path,
    serial: str,
    nonce: str,
) -> None:
    if RESTART_NONCE.fullmatch(nonce) is None:
        raise GateError("Android restart nonce was invalid")
    command = build_restart_ack_command(adb, serial)
    try:
        result = subprocess.run(
            command,
            input=f"{nonce}\n".encode("ascii"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("Android restart acknowledgement did not complete") from error
    if result.returncode != 0 or len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise GateError("Android restart acknowledgement failed")


def install_and_verify(
    adb: Path,
    serial: str,
    artifact: Artifact,
    package_name: str,
) -> str:
    output = run_bounded(
        build_install_command(adb, serial, artifact.path),
        timeout_seconds=180,
        label=f"{package_name} install",
    )
    if b"Success" not in output:
        raise GateError(f"{package_name} install was not acknowledged")
    installed_path = installed_base_apk_path(adb, serial, package_name)
    installed_sha256 = hash_installed_apk(adb, serial, installed_path)
    if installed_sha256 != artifact.sha256:
        raise GateError(f"{package_name} installed APK hash did not match")
    return installed_sha256


def installed_base_apk_path(adb: Path, serial: str, package_name: str) -> str:
    output = run_bounded(
        [*adb_prefix(adb, serial), "shell", "pm", "path", package_name],
        timeout_seconds=30,
        label=f"{package_name} path lookup",
    )
    lines = output.decode("utf-8", errors="strict").splitlines()
    candidates = [line.removeprefix("package:") for line in lines if line.startswith("package:")]
    base = [value for value in candidates if value.endswith("/base.apk")]
    if len(base) != 1 or not base[0].startswith("/data/app/") or "\x00" in base[0]:
        raise GateError(f"{package_name} installed APK path was invalid")
    return base[0]


def hash_installed_apk(adb: Path, serial: str, installed_path: str) -> str:
    command = [*adb_prefix(adb, serial), "exec-out", "cat", installed_path]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise GateError("Installed APK verification could not start") from error
    assert process.stdout is not None
    digest = hashlib.sha256()
    total = 0
    try:
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
            total += len(chunk)
            if total > MAX_APK_BYTES:
                process.kill()
                raise GateError("Installed APK exceeded the approved size")
            digest.update(chunk)
        return_code = process.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        process.kill()
        process.wait()
        raise GateError("Installed APK verification did not complete") from error
    finally:
        process.stdout.close()
    if return_code != 0 or total <= 0:
        raise GateError("Installed APK verification failed")
    return digest.hexdigest()


def parse_instrumentation_line(line: str, evidence: InstrumentationEvidence) -> None:
    if len(line) > MAX_LINE_CHARS:
        raise GateError("Instrumentation emitted an oversized line")
    stripped = line.rstrip("\r\n")
    match = STATUS_LINE.fullmatch(stripped)
    if match is not None and match.group(1) in ALLOWED_EVIDENCE_KEYS:
        key, value = match.groups()
        if any(ord(character) < 0x20 for character in value):
            raise GateError("Instrumentation evidence was malformed")
        if key == "cheby_gate_checkpoint":
            evidence.checkpoints.append(value)
        elif key == "cheby_gate_restart_nonce":
            if RESTART_NONCE.fullmatch(value) is None:
                raise GateError("Instrumentation restart nonce was malformed")
            if evidence.restart_nonce is not None and evidence.restart_nonce != value:
                raise GateError("Instrumentation changed its restart nonce")
            evidence.restart_nonce = value
        else:
            evidence.values[key] = value
        return
    if stripped.startswith("INSTRUMENTATION_CODE:"):
        try:
            code = int(stripped.partition(":")[2].strip())
        except ValueError as error:
            raise GateError("Instrumentation result code was malformed") from error
        if evidence.instrumentation_code is not None:
            raise GateError("Instrumentation emitted duplicate final codes")
        evidence.instrumentation_code = code
        evidence.protocol_transcript.append({"kind": "final", "code": code})
    elif stripped.startswith("INSTRUMENTATION_STATUS_CODE:"):
        try:
            code = int(stripped.partition(":")[2].strip())
        except ValueError as error:
            raise GateError("Instrumentation status code was malformed") from error
        evidence.status_codes.append(code)
        evidence.protocol_transcript.append({"kind": "status", "code": code})
    elif stripped.startswith("OK (") and stripped.endswith(")"):
        evidence.junit_ok = True
    elif stripped == "FAILURES!!!":
        evidence.junit_failed = True


def _read_lines(stream: Iterable[str], output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def restart_checkpoint_ready(evidence: InstrumentationEvidence) -> bool:
    return (
        RESTART_CHECKPOINT in evidence.checkpoints
        and evidence.restart_nonce is not None
        and EVIDENCE_STATUS_CODE in evidence.status_codes
    )


def run_instrumentation(
    command: Sequence[str],
    mode: str,
    restart_plan: RestartPlan | None,
    timeout_seconds: float,
    evidence: InstrumentationEvidence | None = None,
) -> InstrumentationEvidence:
    evidence = evidence or InstrumentationEvidence()
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise GateError("Instrumentation could not start") from error
    assert process.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_lines,
        args=(process.stdout, lines),
        name="android-relay-gate-output",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    stream_ended = False
    try:
        while not stream_ended:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("Instrumentation timed out")
            try:
                line = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    stream_ended = True
                continue
            if line is None:
                stream_ended = True
                continue
            parse_instrumentation_line(line, evidence)
            if (
                mode == "headless-restart"
                and restart_checkpoint_ready(evidence)
                and not evidence.restart_executed
            ):
                if restart_plan is None:
                    raise GateError("Connector restart plan is missing")
                before_epoch, before_hash = read_connector_epoch(
                    restart_plan.epoch_command
                )
                evidence.connector_epoch_before_sha256 = before_hash
                evidence.restart_executed = True
                restart_output = run_bounded(
                    restart_plan.restart_command,
                    timeout_seconds=120,
                    label="Gate Connector restart",
                )
                restart_lines = restart_output.decode("utf-8", errors="replace").splitlines()
                if (
                    not restart_lines
                    or restart_lines[-1].strip()
                    != restart_plan.restart_command[-1]
                ):
                    raise GateError("Gate Connector restart was not acknowledged")
                after_epoch, after_hash = read_connector_epoch(
                    restart_plan.epoch_command
                )
                if after_epoch == before_epoch:
                    raise GateError("Gate Connector start epoch did not change")
                evidence.connector_epoch_after_sha256 = after_hash
                nonce = evidence.restart_nonce
                if nonce is None:
                    raise GateError("Android restart nonce was missing")
                write_restart_acknowledgement(
                    restart_plan.adb,
                    restart_plan.serial,
                    nonce,
                )
                evidence.restart_ack_written = True
                evidence.restart_succeeded = True
        return_code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except (GateError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        reader.join(timeout=5.0)
        if reader.is_alive():
            raise GateError("Instrumentation output reader did not stop")
    if return_code != 0:
        raise GateError("Instrumentation process failed")
    validate_instrumentation_evidence(evidence, mode)
    return evidence


def require_int(values: dict[str, str], key: str) -> int:
    raw = values.get(key)
    try:
        value = int(raw) if raw is not None else None
    except ValueError as error:
        raise GateError("Instrumentation evidence contained an invalid count") from error
    if value is None or value < 0:
        raise GateError("Instrumentation evidence omitted a required count")
    return value


def require_true(values: dict[str, str], key: str) -> None:
    if values.get(key) != "true":
        raise GateError("Instrumentation evidence omitted a required assertion")


def require_pattern(
    values: dict[str, str],
    key: str,
    pattern: re.Pattern[str],
) -> str:
    value = values.get(key)
    if value is None or pattern.fullmatch(value) is None:
        raise GateError("Instrumentation evidence omitted a required digest identity")
    return value


def validate_instrumentation_evidence(
    evidence: InstrumentationEvidence,
    mode: str,
    expected_threads: int | None = None,
    expected_turns: int | None = None,
) -> None:
    if (
        evidence.instrumentation_code != -1
        or not evidence.junit_ok
        or evidence.junit_failed
    ):
        raise GateError("Instrumentation did not report a clean JUnit pass")
    if not evidence.status_codes or any(
        code not in {0, 1, EVIDENCE_STATUS_CODE}
        for code in evidence.status_codes
    ):
        raise GateError("Instrumentation emitted a failed, skipped or unknown status")
    if (
        evidence.status_codes[0] != 1
        or evidence.status_codes[-1] != 0
        or any(
            code != EVIDENCE_STATUS_CODE
            for code in evidence.status_codes[1:-1]
        )
    ):
        raise GateError("Instrumentation status sequence was incomplete or out of order")
    expected_transcript_size = len(evidence.status_codes) + 1
    if (
        len(evidence.protocol_transcript) != expected_transcript_size
        or evidence.protocol_transcript[-1] != {"kind": "final", "code": -1}
        or any(
            item.get("kind") != "status"
            for item in evidence.protocol_transcript[:-1]
        )
    ):
        raise GateError("Instrumentation protocol transcript was incomplete")
    if mode == "headless-restart":
        if evidence.checkpoints != [
            RESTART_CHECKPOINT,
            HEADLESS_COMPLETE_CHECKPOINT,
        ]:
            raise GateError("Android restart checkpoints were incomplete or out of order")
        if evidence.status_codes.count(EVIDENCE_STATUS_CODE) != 2:
            raise GateError("Android restart evidence status count was invalid")
        if not evidence.restart_executed or not evidence.restart_succeeded:
            raise GateError("Gate Connector restart was not proven")
        if (
            not evidence.restart_ack_written
            or evidence.connector_epoch_before_sha256 is None
            or evidence.connector_epoch_after_sha256 is None
            or evidence.connector_epoch_before_sha256
            == evidence.connector_epoch_after_sha256
        ):
            raise GateError("Gate Connector restart lacked a causal epoch handshake")
        require_true(evidence.values, "cheby_gate_restart_observed")
        require_true(evidence.values, "cheby_gate_restart_causal_ack")
        require_true(evidence.values, "cheby_gate_remembered_marker_verified")
        if require_int(evidence.values, "cheby_gate_threads") != 2:
            raise GateError("Headless Thread evidence count was invalid")
        if require_int(evidence.values, "cheby_gate_turns") != 2:
            raise GateError("Headless Turn evidence count was invalid")
    elif mode in {"load", "quick-demo"}:
        if evidence.checkpoints != [LOAD_COMPLETE_CHECKPOINT]:
            raise GateError("Android load checkpoint was incomplete or duplicated")
        if evidence.status_codes.count(EVIDENCE_STATUS_CODE) != 1:
            raise GateError("Android load evidence status count was invalid")
        threads = require_int(evidence.values, "cheby_gate_threads")
        turns = require_int(evidence.values, "cheby_gate_turns")
        if expected_threads is not None and threads != expected_threads:
            raise GateError("Load Thread evidence count did not match")
        if expected_turns is not None and turns != expected_turns:
            raise GateError("Load Turn evidence count did not match")
        if require_int(evidence.values, "cheby_gate_business_keys") != turns:
            raise GateError("Load business-key evidence was incomplete")
        if require_int(evidence.values, "cheby_gate_outbox_entries") != 0:
            raise GateError("Load outbox did not converge")
        if require_int(evidence.values, "cheby_gate_duplicate_bubbles") != 0:
            raise GateError("Load evidence contains duplicate bubbles")
        require_true(evidence.values, "cheby_gate_first_last_verified")
        require_true(evidence.values, "cheby_gate_production_queue")
        if require_int(evidence.values, "cheby_gate_server_barrier_threads") < 2:
            raise GateError("Load evidence did not prove the server concurrency barrier")
        require_pattern(
            evidence.values,
            "cheby_gate_run_token",
            RESTART_NONCE,
        )
        require_pattern(
            evidence.values,
            "cheby_gate_tuple_sha256",
            SHA256_HEX,
        )
        require_pattern(
            evidence.values,
            "cheby_gate_scope_generation_sha256",
            SHA256_HEX,
        )
        if require_int(evidence.values, "cheby_gate_scope_generation_pairs") != 1:
            raise GateError("Android binding scope/generation was not stable")
        if (
            require_int(
                evidence.values,
                "cheby_gate_scope_generation_observations",
            )
            != turns
        ):
            raise GateError("Android scope/generation evidence count was incomplete")
    else:
        raise ConfigError("Unknown Android Relay gate mode")


def manifest_document(
    mode: str,
    outcome: str,
    gate_apk: Artifact,
    test_apk: Artifact,
    installed_gate_sha256: str | None,
    installed_test_sha256: str | None,
    evidence: InstrumentationEvidence | None,
    failure: str | None,
    duration_seconds: float,
) -> dict[str, object]:
    safe_evidence = evidence or InstrumentationEvidence()
    return {
        "schema": "chebycodex.android-relay-gate.v1",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "outcome": outcome,
        "durationSeconds": round(duration_seconds, 3),
        "artifacts": {
            "gateApk": {
                "sha256": gate_apk.sha256,
                "sizeBytes": gate_apk.size_bytes,
                "installedSha256": installed_gate_sha256,
            },
            "testApk": {
                "sha256": test_apk.sha256,
                "sizeBytes": test_apk.size_bytes,
                "installedSha256": installed_test_sha256,
            },
        },
        "evidence": {
            "checkpoints": safe_evidence.checkpoints,
            "values": safe_evidence.values,
            "statusCodes": safe_evidence.status_codes,
            "protocolTranscript": safe_evidence.protocol_transcript,
            "instrumentationCode": safe_evidence.instrumentation_code,
            "junitPass": safe_evidence.junit_ok and not safe_evidence.junit_failed,
            "connectorRestartExecuted": safe_evidence.restart_executed,
            "connectorRestartSucceeded": safe_evidence.restart_succeeded,
            "restartAcknowledgementWritten": safe_evidence.restart_ack_written,
            "connectorEpochBeforeSha256": safe_evidence.connector_epoch_before_sha256,
            "connectorEpochAfterSha256": safe_evidence.connector_epoch_after_sha256,
            "hostDatabaseAudit": safe_evidence.remote_audit,
        },
        "safeguards": {
            "activityLaunches": 0,
            "screenCoordinatesSent": 0,
            "screenTextInputsSent": 0,
            "wifiCommandsSent": 0,
        },
        "failure": failure,
    }


def require_new_manifest_path(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute() or not path.name:
        raise ConfigError("Manifest path must be absolute")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or path.parent.is_symlink()
            or parent.st_mode & 0o077
            or (
                hasattr(os, "getuid")
                and parent.st_uid != os.getuid()
            )
        ):
            raise ConfigError(
                "Manifest parent must be a private owned directory"
            )
        path.lstat()
    except FileNotFoundError:
        return path
    except ConfigError:
        raise
    except OSError as error:
        raise ConfigError("Manifest path is unavailable") from error
    else:
        raise ConfigError("Manifest path already exists")


def write_manifest(path_text: str, document: dict[str, object]) -> Path:
    path = require_new_manifest_path(path_text)
    payload = json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    directory_descriptor = -1
    descriptor = -1
    temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    temporary_device = -1
    temporary_inode = -1
    published_created = False
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ConfigError("Manifest path already exists")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            os.fchmod(output.fileno(), 0o600)
            metadata = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise ConfigError("Manifest temporary file is invalid")
            temporary_device = metadata.st_dev
            temporary_inode = metadata.st_ino
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ConfigError("Manifest path already exists") from error
        published_created = True
        published = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_dev != temporary_device
            or published.st_ino != temporary_inode
            or published.st_nlink != 2
        ):
            raise ConfigError("Manifest published file is invalid")
        os.fsync(directory_descriptor)
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_created = False
        os.fsync(directory_descriptor)
        published_created = False
    except ConfigError:
        raise
    except OSError as error:
        raise ConfigError("Manifest could not be written safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            cleanup_changed_directory = False
            if published_created:
                published_descriptor = -1
                try:
                    published_descriptor = os.open(
                        path.name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_descriptor,
                    )
                    published = os.fstat(published_descriptor)
                    if (
                        stat.S_ISREG(published.st_mode)
                        and published.st_dev == temporary_device
                        and published.st_ino == temporary_inode
                    ):
                        os.unlink(path.name, dir_fd=directory_descriptor)
                        cleanup_changed_directory = True
                except FileNotFoundError:
                    pass
                except OSError:
                    # Preserve the original fail-closed exception. A successful
                    # cleanup is verified by the absence regression tests.
                    pass
                finally:
                    if published_descriptor >= 0:
                        os.close(published_descriptor)
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                    cleanup_changed_directory = True
                except OSError:
                    pass
            if cleanup_changed_directory:
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)
    return path


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("headless-restart", "quick-demo", "load"),
        required=True,
    )
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--gate-apk", required=True)
    parser.add_argument("--test-apk", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-reply", default=GATE_REPLY_MARKER)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--turns", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--ssh")
    parser.add_argument("--ssh-host")
    parser.add_argument(
        "--connector-container",
        default="chebycodex-turkey-gate-connector-gate-connector-1",
    )
    return parser


def validate_args(
    args: argparse.Namespace,
) -> tuple[
    Path,
    Artifact,
    Artifact,
    RestartPlan | None,
    AuditPlan | None,
]:
    adb = require_regular_file(args.adb, "ADB", executable=True)
    serial = validate_identifier(args.serial, SAFE_SERIAL, "Device serial")
    args.serial = serial
    gate_apk = inspect_apk(args.gate_apk, "Gate APK")
    test_apk = inspect_apk(args.test_apk, "Gate test APK")
    args.expected_reply = validate_expected_reply(args.expected_reply)
    if not 8 <= args.threads <= 32:
        raise ConfigError("Thread count must be in 8..32")
    if not args.threads <= args.turns <= 2000:
        raise ConfigError("Turn count must be between the Thread count and 2000")
    if args.mode == "load" and (args.threads != 8 or args.turns != 500):
        raise ConfigError("Load release gate requires exactly 8 Threads and 500 Turns")
    if args.mode == "quick-demo" and (args.threads != 8 or args.turns != 16):
        raise ConfigError("Quick demo gate requires exactly 8 Threads and 16 Turns")
    default_timeout = {
        "headless-restart": 1800.0,
        "quick-demo": 480.0,
        "load": 7200.0,
    }[args.mode]
    args.timeout_seconds = args.timeout_seconds or default_timeout
    minimum_timeout, maximum_timeout = {
        "headless-restart": (1500.0, 10_800.0),
        "quick-demo": (120.0, 480.0),
        "load": (6000.0, 10_800.0),
    }[args.mode]
    if not minimum_timeout <= args.timeout_seconds <= maximum_timeout:
        raise ConfigError("Instrumentation timeout is outside the approved range")
    if not args.ssh or not args.ssh_host:
        raise ConfigError("Android Relay gate requires SSH and an SSH host")
    ssh = require_regular_file(args.ssh, "SSH", executable=True)
    ssh_host = validate_identifier(args.ssh_host, SAFE_REMOTE_NAME, "SSH host")
    container = validate_identifier(
        args.connector_container,
        SAFE_CONTAINER_NAME,
        "Gate Connector container",
    )
    if container not in EXPECTED_GATE_CONNECTOR_CONTAINERS:
        raise ConfigError(
            "Connector restart target must identify the isolated Gate Connector"
        )
    restart_plan: RestartPlan | None = None
    audit_plan: AuditPlan | None = None
    if args.mode == "headless-restart":
        restart_plan = RestartPlan(
            restart_command=tuple(build_restart_command(ssh, ssh_host, container)),
            epoch_command=tuple(build_epoch_command(ssh, ssh_host, container)),
            adb=adb,
            serial=serial,
        )
    elif args.mode in {"load", "quick-demo"}:
        audit_plan = AuditPlan(ssh=ssh, ssh_host=ssh_host)
    return adb, gate_apk, test_apk, restart_plan, audit_plan


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    started = time.monotonic()
    adb, gate_apk, test_apk, restart_plan, audit_plan = validate_args(args)
    installed_gate_sha256: str | None = None
    installed_test_sha256: str | None = None
    evidence: InstrumentationEvidence | None = None
    failure: str | None = None
    outcome = "FAIL"
    exit_code = 1
    try:
        installed_gate_sha256 = install_and_verify(
            adb,
            args.serial,
            gate_apk,
            TARGET_PACKAGE,
        )
        installed_test_sha256 = install_and_verify(
            adb,
            args.serial,
            test_apk,
            TEST_PACKAGE,
        )
        for command in build_force_stop_commands(adb, args.serial):
            run_bounded(command, timeout_seconds=30, label="Gate package force-stop")
        instrumentation = build_instrumentation_command(
            adb=adb,
            serial=args.serial,
            mode=args.mode,
            expected_reply=args.expected_reply,
            threads=args.threads,
            turns=args.turns,
        )
        evidence = InstrumentationEvidence()
        run_instrumentation(
            command=instrumentation,
            mode=args.mode,
            restart_plan=restart_plan,
            timeout_seconds=args.timeout_seconds,
            evidence=evidence,
        )
        validate_instrumentation_evidence(
            evidence,
            args.mode,
            expected_threads=args.threads if args.mode in {"load", "quick-demo"} else None,
            expected_turns=args.turns if args.mode in {"load", "quick-demo"} else None,
        )
        if args.mode in {"load", "quick-demo"}:
            if audit_plan is None:
                raise GateError("Gate business-key audit plan is missing")
            evidence.remote_audit = read_gate_audit(
                audit_plan,
                require_pattern(
                    evidence.values,
                    "cheby_gate_run_token",
                    RESTART_NONCE,
                ),
                expected_threads=args.threads,
                expected_turns=args.turns,
                android_tuple_sha256=require_pattern(
                    evidence.values,
                    "cheby_gate_tuple_sha256",
                    SHA256_HEX,
                ),
                mode=args.mode,
            )
        outcome = "PASS"
        exit_code = 0
    except GateError as error:
        failure = str(error)
    document = manifest_document(
        mode=args.mode,
        outcome=outcome,
        gate_apk=gate_apk,
        test_apk=test_apk,
        installed_gate_sha256=installed_gate_sha256,
        installed_test_sha256=installed_test_sha256,
        evidence=evidence,
        failure=failure,
        duration_seconds=time.monotonic() - started,
    )
    return document, exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        require_new_manifest_path(args.manifest)
        document, exit_code = execute(args)
        manifest = write_manifest(args.manifest, document)
    except ConfigError as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"{document['outcome']} manifest={manifest}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
