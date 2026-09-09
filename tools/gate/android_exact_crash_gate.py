#!/usr/bin/env python3
"""Fixed Android process-death gate for five exact delivery boundaries.

Every boundary runs exactly 25 times. ``prepare`` must remain alive after
publishing an app-private checkpoint; the host verifies its PID and exact
Outbox snapshot, force-stops the Gate package, and starts an independent
``recover`` instrumentation process. No Activity, coordinate, click, or text
input command is issued.
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
from typing import Any, Iterable, Mapping, Sequence

import android_relay_gate as relay_gate


TARGET_PACKAGE = "com.cheby.codex.mobile.gate"
TEST_PACKAGE = "com.cheby.codex.mobile.gate.test"
RUNNER = f"{TEST_PACKAGE}/androidx.test.runner.AndroidJUnitRunner"
TEST_CLASS = (
    "com.cheby.codex.mobile.gateway."
    "DeliveryExactCrashGateInstrumentedTest"
)
PREPARE_METHOD = "prepareAtExactBoundaryAndRequireHostProcessDeath"
RECOVER_METHOD = "recoverAfterHostProcessDeathAndProveQueueConvergence"
BOUNDARIES = ("ENQUEUE", "NETWORK", "ACCEPTED", "ANDROID", "CLEANED")
REPETITIONS = 25
TURNS_PER_CYCLE = 3
THREADS_PER_CYCLE = 2
EXPECTED_CYCLES = len(BOUNDARIES) * REPETITIONS
EXPECTED_TURNS = EXPECTED_CYCLES * TURNS_PER_CYCLE
EXPECTED_THREADS = EXPECTED_CYCLES * THREADS_PER_CYCLE
CHECKPOINT_FILE = "cheby-exact-crash-checkpoint-v1.json"
RECOVERY_FILE = "cheby-exact-crash-recovery-v1.json"
PLAN_FILE = "cheby-exact-crash-plan-v1.json"
REMOTE_AUDIT = "/usr/local/sbin/chebycodex-audit-gate-business-keys"
REMOTE_SCHEMA = "chebycodex.gate-exact-crash-audit.v1"
CHECKPOINT_SCHEMA = "chebycodex.android-exact-crash-checkpoint.v1"
RECOVERY_SCHEMA = "chebycodex.android-exact-crash-recovery.v1"
RUN_TOKEN = re.compile(r"^[a-f0-9]{32}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
PUBLIC_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
SAFE_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SAFE_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
STATUS_LINE = re.compile(
    r"^INSTRUMENTATION_STATUS: (cheby_exact_[a-z_]+)=(.{0,512})$"
)
ALLOWED_STATUS_KEYS = frozenset(
    {
        "cheby_exact_checkpoint",
        "cheby_exact_boundary",
        "cheby_exact_run_token",
        "cheby_exact_iteration",
        "cheby_exact_turns",
        "cheby_exact_threads",
        "cheby_exact_outbox_entries",
        "cheby_exact_duplicate_bubbles",
        "cheby_exact_same_thread_fifo",
        "cheby_exact_other_thread_independent",
        "cheby_exact_tuple_sha256",
        "cheby_exact_business_key_sha256",
    }
)
PRIVATE_JSON_LIMIT = 128 * 1024
INSTRUMENTATION_OUTPUT_LIMIT = 128 * 1024
PREPARE_TIMEOUT_SECONDS = 300.0
RECOVERY_TIMEOUT_SECONDS = 15 * 60.0
PROCESS_EXIT_TIMEOUT_SECONDS = 30.0


class GateError(RuntimeError):
    """Sanitized failure safe to persist in a public manifest."""


@dataclass(frozen=True, order=True)
class BusinessKey:
    scope: str
    generation: int
    thread_id: str
    client_message_id: str


@dataclass(frozen=True, order=True)
class BusinessTuple:
    thread_id: str
    client_message_id: str
    turn_id: str


@dataclass(frozen=True)
class Checkpoint:
    boundary: str
    iteration: int
    nonce: str
    pid: int
    target: BusinessKey
    target_state: str
    turn_id: str | None
    pending: tuple[BusinessKey, BusinessKey]
    business_key_sha256: str


@dataclass(frozen=True)
class Recovery:
    tuples: tuple[BusinessTuple, ...]
    keys: tuple[BusinessKey, ...]
    tuple_sha256: str
    business_key_sha256: str
    outbox_entries: int
    duplicate_bubbles: int


@dataclass
class InstrumentationOutput:
    values: dict[str, str] = field(default_factory=dict)
    status_codes: list[int] = field(default_factory=list)
    final_code: int | None = None
    junit_ok: bool = False
    junit_failed: bool = False
    byte_count: int = 0


def observed_host_counts(
    per_boundary: Mapping[str, int],
    checkpoint_digests: Sequence[str],
    recoveries: Sequence[Recovery],
    tuples: Sequence[BusinessTuple],
    keys: Sequence[BusinessKey],
) -> dict[str, int]:
    """Derive host evidence counts only from completed, validated observations."""

    return {
        "cycles": sum(per_boundary.values()),
        "hostForceStops": len(checkpoint_digests),
        "abnormalPrepareTerminations": len(checkpoint_digests),
        "independentRecoveryProcesses": len(recoveries),
        "turns": len(tuples),
        "threads": len({key.thread_id for key in keys}),
    }


def reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("Private evidence contains duplicate JSON keys")
        result[key] = value
    return result


def strict_json(raw: bytes, label: str) -> dict[str, Any]:
    if not raw or len(raw) > PRIVATE_JSON_LIMIT:
        raise GateError(f"{label} size is outside the gate")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GateError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise GateError(f"{label} has an invalid shape")
    return value


def require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise GateError(f"{label} schema is invalid")


def require_string(
    value: Mapping[str, Any],
    key: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise GateError("Private evidence contains an invalid string")
    if pattern is not None and pattern.fullmatch(item) is None:
        raise GateError("Private evidence contains an invalid identifier")
    return item


def parse_key(value: Any, *, allow_turn: bool) -> tuple[BusinessKey, str, str | None]:
    if not isinstance(value, dict):
        raise GateError("Checkpoint business key is malformed")
    expected = {
        "scope",
        "generation",
        "threadId",
        "clientMessageId",
        "state",
    }
    if allow_turn and "turnId" in value:
        expected.add("turnId")
    require_exact_keys(value, expected, "Checkpoint business key")
    generation = value.get("generation")
    if type(generation) is not int or generation < 0:
        raise GateError("Checkpoint generation is invalid")
    key = BusinessKey(
        require_string(value, "scope", PUBLIC_ID),
        generation,
        require_string(value, "threadId", PUBLIC_ID),
        require_string(value, "clientMessageId", PUBLIC_ID),
    )
    state = require_string(value, "state")
    turn_id = (
        require_string(value, "turnId", PUBLIC_ID)
        if "turnId" in value
        else None
    )
    return key, state, turn_id


def digest_business_keys(values: Iterable[BusinessKey]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        for field in (
            value.scope,
            str(value.generation),
            value.thread_id,
            value.client_message_id,
        ):
            digest.update(field.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def digest_tuples(values: Iterable[BusinessTuple]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        for field in (
            value.thread_id,
            value.client_message_id,
            value.turn_id,
        ):
            digest.update(field.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def parse_checkpoint(
    document: Mapping[str, Any],
    *,
    run_token: str,
    boundary: str,
    iteration: int,
    nonce: str,
) -> Checkpoint:
    require_exact_keys(
        document,
        {
            "schema",
            "runToken",
            "boundary",
            "iteration",
            "nonce",
            "pid",
            "target",
            "pending",
            "businessKeySha256",
            "normalJunitMustNotReturn",
        },
        "Exact crash checkpoint",
    )
    if (
        document.get("schema") != CHECKPOINT_SCHEMA
        or document.get("runToken") != run_token
        or document.get("boundary") != boundary
        or document.get("iteration") != iteration
        or document.get("nonce") != nonce
        or document.get("normalJunitMustNotReturn") is not True
    ):
        raise GateError("Exact crash checkpoint identity changed")
    pid = document.get("pid")
    if type(pid) is not int or pid <= 0:
        raise GateError("Exact crash checkpoint PID is invalid")
    target, target_state, turn_id = parse_key(
        document.get("target"),
        allow_turn=True,
    )
    expected_state = {
        "ENQUEUE": "QUEUED",
        "NETWORK": "QUEUED",
        "ACCEPTED": "QUEUED",
        "ANDROID": "ACCEPTED",
        "CLEANED": "ABSENT",
    }[boundary]
    if target_state != expected_state:
        raise GateError("Target Outbox state does not match the exact boundary")
    if (boundary in {"ANDROID", "CLEANED"}) != (turn_id is not None):
        raise GateError("Target Turn identity does not match the exact boundary")
    pending_raw = document.get("pending")
    if not isinstance(pending_raw, list) or len(pending_raw) != 2:
        raise GateError("Exact crash pending rows are incomplete")
    pending: list[BusinessKey] = []
    for raw in pending_raw:
        key, state, pending_turn = parse_key(raw, allow_turn=False)
        if state != "QUEUED" or pending_turn is not None:
            raise GateError("Exact crash pending row is not QUEUED")
        pending.append(key)
    if (
        target.scope != pending[0].scope
        or target.scope != pending[1].scope
        or target.generation != pending[0].generation
        or target.generation != pending[1].generation
        or target.thread_id != pending[0].thread_id
        or target.thread_id == pending[1].thread_id
        or len({target.client_message_id, *(item.client_message_id for item in pending)}) != 3
    ):
        raise GateError("Exact crash pending business keys are not isolated")
    expected_digest = digest_business_keys([target, *pending])
    digest = require_string(document, "businessKeySha256", SHA256)
    if digest != expected_digest:
        raise GateError("Exact crash business-key digest is inconsistent")
    return Checkpoint(
        boundary,
        iteration,
        nonce,
        pid,
        target,
        target_state,
        turn_id,
        (pending[0], pending[1]),
        digest,
    )


def parse_recovery(
    document: Mapping[str, Any],
    checkpoint: Checkpoint,
    run_token: str,
) -> Recovery:
    require_exact_keys(
        document,
        {
            "schema",
            "runToken",
            "boundary",
            "iteration",
            "nonce",
            "killedPid",
            "messages",
            "tupleSha256",
            "businessKeySha256",
            "outboxEntries",
            "duplicateBubbles",
            "sameThreadFifo",
            "otherThreadIndependent",
        },
        "Exact crash recovery",
    )
    if (
        document.get("schema") != RECOVERY_SCHEMA
        or document.get("runToken") != run_token
        or document.get("boundary") != checkpoint.boundary
        or document.get("iteration") != checkpoint.iteration
        or document.get("nonce") != checkpoint.nonce
        or document.get("killedPid") != checkpoint.pid
        or document.get("outboxEntries") != 0
        or document.get("duplicateBubbles") != 0
        or document.get("sameThreadFifo") is not True
        or document.get("otherThreadIndependent") is not True
    ):
        raise GateError("Exact crash recovery invariants are incomplete")
    raw_messages = document.get("messages")
    if not isinstance(raw_messages, list) or len(raw_messages) != 3:
        raise GateError("Exact crash recovery Turn set is incomplete")
    by_label: dict[str, tuple[BusinessKey, BusinessTuple]] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise GateError("Exact crash recovery message is malformed")
        require_exact_keys(
            raw,
            {
                "label",
                "scope",
                "generation",
                "threadId",
                "clientMessageId",
                "turnId",
                "turnCount",
            },
            "Exact crash recovery message",
        )
        label = require_string(raw, "label")
        if label not in {"A1", "A2", "B1"} or label in by_label:
            raise GateError("Exact crash recovery label set is invalid")
        generation = raw.get("generation")
        if type(generation) is not int or generation < 0:
            raise GateError("Exact crash recovery generation is invalid")
        key = BusinessKey(
            require_string(raw, "scope", PUBLIC_ID),
            generation,
            require_string(raw, "threadId", PUBLIC_ID),
            require_string(raw, "clientMessageId", PUBLIC_ID),
        )
        business_tuple = BusinessTuple(
            key.thread_id,
            key.client_message_id,
            require_string(raw, "turnId", PUBLIC_ID),
        )
        if raw.get("turnCount") != 1:
            raise GateError("Exact crash recovery count is not one")
        by_label[label] = (key, business_tuple)
    expected_keys = {
        "A1": checkpoint.target,
        "A2": checkpoint.pending[0],
        "B1": checkpoint.pending[1],
    }
    if any(by_label[label][0] != key for label, key in expected_keys.items()):
        raise GateError("Exact crash recovery changed a business key")
    tuples = tuple(value[1] for value in by_label.values())
    keys = tuple(value[0] for value in by_label.values())
    if len(set(tuples)) != 3 or len({value.turn_id for value in tuples}) != 3:
        raise GateError("Exact crash recovery contains duplicate Turns")
    tuple_digest = require_string(document, "tupleSha256", SHA256)
    key_digest = require_string(document, "businessKeySha256", SHA256)
    if tuple_digest != digest_tuples(tuples) or key_digest != digest_business_keys(keys):
        raise GateError("Exact crash recovery digest is inconsistent")
    return Recovery(
        tuples,
        keys,
        tuple_digest,
        key_digest,
        int(document["outboxEntries"]),
        int(document["duplicateBubbles"]),
    )


def adb_prefix(adb: Path, serial: str) -> list[str]:
    return [str(adb), "-s", serial]


def instrumentation_command(
    adb: Path,
    serial: str,
    *,
    phase: str,
    run_token: str,
    boundary: str,
    iteration: int,
    nonce: str,
    expected_reply: str,
) -> list[str]:
    method = PREPARE_METHOD if phase == "prepare" else RECOVER_METHOD
    encoded_reply = base64.urlsafe_b64encode(
        expected_reply.encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    arguments = {
        "cheby_exact_crash_gate": "true",
        "cheby_exact_crash_phase": phase,
        "cheby_exact_crash_run_token": run_token,
        "cheby_exact_crash_boundary": boundary,
        "cheby_exact_crash_iteration": str(iteration),
        "cheby_exact_crash_nonce": nonce,
        "cheby_relay_expected_reply_b64url": encoded_reply,
        "class": f"{TEST_CLASS}#{method}",
    }
    command = [*adb_prefix(adb, serial), "shell", "am", "instrument", "-w", "-r"]
    for key, value in arguments.items():
        command.extend(("-e", key, value))
    command.append(RUNNER)
    return command


def private_file_command(
    adb: Path,
    serial: str,
    filename: str,
) -> list[str]:
    if filename not in {PLAN_FILE, CHECKPOINT_FILE, RECOVERY_FILE}:
        raise GateError("Private evidence file is not allowlisted")
    return [
        *adb_prefix(adb, serial),
        "exec-out",
        "run-as",
        TARGET_PACKAGE,
        "cat",
        f"files/{filename}",
    ]


def read_private_file(
    adb: Path,
    serial: str,
    filename: str,
) -> bytes | None:
    try:
        result = subprocess.run(
            private_file_command(adb, serial, filename),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("Private evidence read did not complete") from error
    if result.returncode != 0:
        return None
    if len(result.stdout) > PRIVATE_JSON_LIMIT:
        raise GateError("Private evidence exceeded the approved limit")
    return result.stdout


def delete_private_files(adb: Path, serial: str) -> None:
    command = [
        *adb_prefix(adb, serial),
        "shell",
        "run-as",
        TARGET_PACKAGE,
        "rm",
        "-f",
        *(f"files/{name}" for name in (PLAN_FILE, CHECKPOINT_FILE, RECOVERY_FILE)),
    ]
    relay_gate.run_bounded(command, 30, "Exact crash evidence cleanup")


def read_pid(adb: Path, serial: str) -> int | None:
    try:
        result = subprocess.run(
            [*adb_prefix(adb, serial), "shell", "pidof", TARGET_PACKAGE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("Gate PID inspection did not complete") from error
    if result.returncode != 0 or not result.stdout.strip():
        return None
    values = result.stdout.decode("ascii", errors="strict").split()
    if len(values) != 1 or not values[0].isdigit():
        raise GateError("Gate PID inspection was ambiguous")
    return int(values[0])


def force_stop(adb: Path, serial: str) -> None:
    for package in (TARGET_PACKAGE, TEST_PACKAGE):
        relay_gate.run_bounded(
            [*adb_prefix(adb, serial), "shell", "am", "force-stop", package],
            30,
            "Exact crash host force-stop",
        )


def parse_output_line(line: str, evidence: InstrumentationOutput) -> None:
    encoded = line.encode("utf-8", errors="replace")
    evidence.byte_count += len(encoded)
    if evidence.byte_count > INSTRUMENTATION_OUTPUT_LIMIT:
        raise GateError("Instrumentation output exceeded the approved limit")
    stripped = line.rstrip("\r\n")
    match = STATUS_LINE.fullmatch(stripped)
    if match is not None:
        key, value = match.groups()
        if key not in ALLOWED_STATUS_KEYS or key in evidence.values:
            raise GateError("Instrumentation evidence schema is invalid")
        if any(ord(character) < 0x20 for character in value):
            raise GateError("Instrumentation evidence is malformed")
        evidence.values[key] = value
    elif stripped.startswith("INSTRUMENTATION_STATUS_CODE:"):
        try:
            evidence.status_codes.append(int(stripped.partition(":")[2].strip()))
        except ValueError as error:
            raise GateError("Instrumentation status code is invalid") from error
    elif stripped.startswith("INSTRUMENTATION_CODE:"):
        if evidence.final_code is not None:
            raise GateError("Instrumentation emitted duplicate final codes")
        try:
            evidence.final_code = int(stripped.partition(":")[2].strip())
        except ValueError as error:
            raise GateError("Instrumentation final code is invalid") from error
    elif stripped.startswith("OK (") and stripped.endswith(")"):
        evidence.junit_ok = True
    elif stripped == "FAILURES!!!":
        evidence.junit_failed = True


def output_reader(
    stream: Iterable[str],
    lines: queue.Queue[str | None],
) -> None:
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(None)


def start_instrumentation(
    command: Sequence[str],
) -> tuple[subprocess.Popen[str], queue.Queue[str | None], threading.Thread]:
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
        target=output_reader,
        args=(process.stdout, lines),
        name="android-exact-crash-output",
        daemon=True,
    )
    reader.start()
    return process, lines, reader


def consume_available_output(
    lines: queue.Queue[str | None],
    evidence: InstrumentationOutput,
) -> bool:
    ended = False
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            return ended
        if line is None:
            ended = True
        else:
            parse_output_line(line, evidence)


def validate_live_prepare_checkpoint(
    evidence: InstrumentationOutput,
    *,
    process_returncode: int | None,
    live_pid: int | None,
    checkpoint: Checkpoint,
) -> None:
    if process_returncode is not None:
        raise GateError("Prepare ended normally before host process death")
    if evidence.junit_ok or evidence.final_code is not None:
        raise GateError("Prepare reported a normal JUnit completion")
    if live_pid != checkpoint.pid:
        raise GateError("Checkpoint PID did not identify the live Gate process")


def validate_killed_prepare(
    evidence: InstrumentationOutput,
    *,
    live_pid_after: int | None,
) -> None:
    if live_pid_after is not None:
        raise GateError("Gate process survived host force-stop")
    if evidence.junit_ok or evidence.final_code == -1:
        raise GateError("Force-stopped prepare was misreported as a JUnit pass")


def run_prepare_and_force_stop(
    adb: Path,
    serial: str,
    command: Sequence[str],
    *,
    run_token: str,
    boundary: str,
    iteration: int,
    nonce: str,
) -> Checkpoint:
    process, lines, reader = start_instrumentation(command)
    evidence = InstrumentationOutput()
    deadline = time.monotonic() + PREPARE_TIMEOUT_SECONDS
    checkpoint: Checkpoint | None = None
    try:
        while time.monotonic() < deadline:
            consume_available_output(lines, evidence)
            if process.poll() is not None:
                raise GateError("Prepare ended normally before host process death")
            raw = read_private_file(adb, serial, CHECKPOINT_FILE)
            if raw is not None:
                checkpoint = parse_checkpoint(
                    strict_json(raw, "Exact crash checkpoint"),
                    run_token=run_token,
                    boundary=boundary,
                    iteration=iteration,
                    nonce=nonce,
                )
                break
            time.sleep(0.1)
        if checkpoint is None:
            raise GateError("Prepare did not reach its exact checkpoint")
        validate_live_prepare_checkpoint(
            evidence,
            process_returncode=process.poll(),
            live_pid=read_pid(adb, serial),
            checkpoint=checkpoint,
        )
        force_stop(adb, serial)
        exit_deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
        while process.poll() is None and time.monotonic() < exit_deadline:
            consume_available_output(lines, evidence)
            time.sleep(0.05)
        if process.poll() is None:
            raise GateError("Host force-stop did not terminate instrumentation")
        reader.join(timeout=5)
        consume_available_output(lines, evidence)
        if reader.is_alive():
            raise GateError("Prepare output reader did not stop")
        validate_killed_prepare(
            evidence,
            live_pid_after=read_pid(adb, serial),
        )
        return checkpoint
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=5)


def validate_recovery_output(
    evidence: InstrumentationOutput,
    checkpoint: Checkpoint,
    run_token: str,
) -> None:
    expected = {
        "cheby_exact_checkpoint": "exact_crash_recovered",
        "cheby_exact_boundary": checkpoint.boundary,
        "cheby_exact_run_token": run_token,
        "cheby_exact_iteration": str(checkpoint.iteration),
        "cheby_exact_turns": "3",
        "cheby_exact_threads": "2",
        "cheby_exact_outbox_entries": "0",
        "cheby_exact_duplicate_bubbles": "0",
        "cheby_exact_same_thread_fifo": "true",
        "cheby_exact_other_thread_independent": "true",
    }
    if any(evidence.values.get(key) != value for key, value in expected.items()):
        raise GateError("Recovery instrumentation evidence is incomplete")
    for key in (
        "cheby_exact_tuple_sha256",
        "cheby_exact_business_key_sha256",
    ):
        if SHA256.fullmatch(evidence.values.get(key, "")) is None:
            raise GateError("Recovery instrumentation digest is invalid")
    if (
        evidence.final_code != -1
        or not evidence.junit_ok
        or evidence.junit_failed
        or evidence.status_codes != [1, 2, 0]
    ):
        raise GateError("Recovery instrumentation did not report a clean pass")


def run_recovery(
    command: Sequence[str],
    adb: Path,
    serial: str,
    checkpoint: Checkpoint,
    run_token: str,
) -> Recovery:
    process, lines, reader = start_instrumentation(command)
    evidence = InstrumentationOutput()
    deadline = time.monotonic() + RECOVERY_TIMEOUT_SECONDS
    ended = False
    try:
        while not ended:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateError("Recovery instrumentation timed out")
            try:
                line = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    ended = True
                continue
            if line is None:
                ended = True
            else:
                parse_output_line(line, evidence)
        return_code = process.wait(timeout=max(1, deadline - time.monotonic()))
        reader.join(timeout=5)
        if return_code != 0 or reader.is_alive():
            raise GateError("Recovery instrumentation process failed")
        validate_recovery_output(evidence, checkpoint, run_token)
        raw = read_private_file(adb, serial, RECOVERY_FILE)
        if raw is None:
            raise GateError("Recovery private evidence is missing")
        recovery = parse_recovery(
            strict_json(raw, "Exact crash recovery"),
            checkpoint,
            run_token,
        )
        if (
            evidence.values["cheby_exact_tuple_sha256"]
            != recovery.tuple_sha256
            or evidence.values["cheby_exact_business_key_sha256"]
            != recovery.business_key_sha256
        ):
            raise GateError("Recovery status and private evidence diverged")
        return recovery
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=5)


def read_remote_audit(
    ssh: Path,
    ssh_host: str,
    run_token: str,
    android_tuple_sha256: str,
) -> dict[str, Any]:
    output = relay_gate.run_bounded(
        [
            str(ssh),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            ssh_host,
            "sudo",
            "-n",
            REMOTE_AUDIT,
            "--mode",
            "exact-crash",
            "--run-token",
            run_token,
        ],
        120,
        "Exact crash read-only business-key audit",
    )
    document = strict_json(output, "Exact crash remote audit")
    require_exact_keys(
        document,
        {"schema", "outcome", "runToken", "counts", "digests"},
        "Exact crash remote audit",
    )
    expected_counts = {
        "turns": EXPECTED_TURNS,
        "threads": EXPECTED_THREADS,
        "relayTuples": EXPECTED_TURNS,
        "connectorTuples": EXPECTED_TURNS,
        "gatewayTuples": EXPECTED_TURNS,
        "fakeCodexTuples": EXPECTED_TURNS,
        "bindingGenerationMappings": 1,
        "relevantConnectorIntents": 0,
        "relevantConnectorOutbox": 0,
        "relevantConnectorProcessedDeliveries": 0,
        "cycles": EXPECTED_CYCLES,
        "businessKeys": EXPECTED_TURNS,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }
    counts = document.get("counts")
    digests = document.get("digests")
    if (
        document.get("schema") != REMOTE_SCHEMA
        or document.get("outcome") != "PASS"
        or document.get("runToken") != run_token
        or counts != expected_counts
        or not isinstance(digests, dict)
    ):
        raise GateError("Exact crash remote audit contract is incomplete")
    expected_digest_keys = {
        "tupleSha256",
        "relayTupleSha256",
        "connectorTupleSha256",
        "gatewayTupleSha256",
        "fakeCodexTupleSha256",
        "bindingGenerationMappingSha256",
    }
    if set(digests) != expected_digest_keys or any(
        not isinstance(value, str) or SHA256.fullmatch(value) is None
        for value in digests.values()
    ):
        raise GateError("Exact crash remote audit digests are invalid")
    tuple_values = {
        digests[key]
        for key in (
            "tupleSha256",
            "relayTupleSha256",
            "connectorTupleSha256",
            "gatewayTupleSha256",
            "fakeCodexTupleSha256",
        )
    }
    if tuple_values != {android_tuple_sha256}:
        raise GateError("Android and server exact-crash tuple digests diverged")
    return {
        "schema": document["schema"],
        "outcome": document["outcome"],
        "counts": counts,
        "digests": digests,
    }


def write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise GateError("Manifest path is invalid")
    try:
        relay_gate.write_manifest(str(path), dict(document))
    except relay_gate.ConfigError as error:
        raise GateError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--adb", required=True)
    value.add_argument("--serial", required=True)
    value.add_argument("--gate-apk", required=True)
    value.add_argument("--test-apk", required=True)
    value.add_argument("--ssh", default="/usr/bin/ssh")
    value.add_argument("--ssh-host", default="remote-213")
    value.add_argument("--expected-reply", default=relay_gate.GATE_REPLY_MARKER)
    value.add_argument("--manifest", required=True)
    return value


def execute(args: argparse.Namespace) -> dict[str, Any]:
    adb = relay_gate.require_regular_file(args.adb, "adb", executable=True)
    ssh = relay_gate.require_regular_file(args.ssh, "ssh", executable=True)
    if SAFE_SERIAL.fullmatch(args.serial) is None:
        raise GateError("Android serial is invalid")
    if SAFE_REMOTE.fullmatch(args.ssh_host) is None:
        raise GateError("SSH host is invalid")
    expected_reply = relay_gate.validate_expected_reply(args.expected_reply)
    gate_apk = relay_gate.inspect_apk(args.gate_apk, "Gate APK")
    test_apk = relay_gate.inspect_apk(args.test_apk, "Gate test APK")
    installed_gate = relay_gate.install_and_verify(
        adb, args.serial, gate_apk, TARGET_PACKAGE
    )
    installed_test = relay_gate.install_and_verify(
        adb, args.serial, test_apk, TEST_PACKAGE
    )
    force_stop(adb, args.serial)
    run_token = secrets.token_hex(16)
    tuples: list[BusinessTuple] = []
    keys: list[BusinessKey] = []
    checkpoint_digests: list[str] = []
    recoveries: list[Recovery] = []
    verified_outbox_counts: list[int] = []
    verified_duplicate_counts: list[int] = []
    per_boundary = {boundary: 0 for boundary in BOUNDARIES}
    for boundary in BOUNDARIES:
        for iteration in range(1, REPETITIONS + 1):
            delete_private_files(adb, args.serial)
            nonce = secrets.token_hex(16)
            checkpoint = run_prepare_and_force_stop(
                adb,
                args.serial,
                instrumentation_command(
                    adb,
                    args.serial,
                    phase="prepare",
                    run_token=run_token,
                    boundary=boundary,
                    iteration=iteration,
                    nonce=nonce,
                    expected_reply=expected_reply,
                ),
                run_token=run_token,
                boundary=boundary,
                iteration=iteration,
                nonce=nonce,
            )
            recovery = run_recovery(
                instrumentation_command(
                    adb,
                    args.serial,
                    phase="recover",
                    run_token=run_token,
                    boundary=boundary,
                    iteration=iteration,
                    nonce=nonce,
                    expected_reply=expected_reply,
                ),
                adb,
                args.serial,
                checkpoint,
                run_token,
            )
            tuples.extend(recovery.tuples)
            keys.extend(recovery.keys)
            checkpoint_digests.append(checkpoint.business_key_sha256)
            recoveries.append(recovery)
            verified_outbox_counts.append(recovery.outbox_entries)
            verified_duplicate_counts.append(recovery.duplicate_bubbles)
            per_boundary[boundary] += 1
    observed = observed_host_counts(
        per_boundary,
        checkpoint_digests,
        recoveries,
        tuples,
        keys,
    )
    if (
        per_boundary != {boundary: REPETITIONS for boundary in BOUNDARIES}
        or observed["cycles"] != EXPECTED_CYCLES
        or observed["hostForceStops"] != EXPECTED_CYCLES
        or observed["abnormalPrepareTerminations"] != EXPECTED_CYCLES
        or observed["independentRecoveryProcesses"] != EXPECTED_CYCLES
        or observed["turns"] != EXPECTED_TURNS
        or observed["threads"] != EXPECTED_THREADS
        or len(set(tuples)) != EXPECTED_TURNS
        or len(keys) != EXPECTED_TURNS
        or len(set(keys)) != EXPECTED_TURNS
        or verified_outbox_counts != [0] * EXPECTED_CYCLES
        or verified_duplicate_counts != [0] * EXPECTED_CYCLES
    ):
        raise GateError("Exact crash host matrix did not converge")
    android_tuple_digest = digest_tuples(tuples)
    android_key_digest = digest_business_keys(keys)
    remote_audit = read_remote_audit(
        ssh,
        args.ssh_host,
        run_token,
        android_tuple_digest,
    )
    aggregate = hashlib.sha256()
    for value in checkpoint_digests:
        aggregate.update(value.encode("ascii"))
        aggregate.update(b"\0")
    return {
        "schema": "chebycodex.android-exact-crash-gate.v1",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome": "PASS",
        "runToken": run_token,
        "artifacts": {
            "gateApk": {
                "sha256": gate_apk.sha256,
                "installedSha256": installed_gate,
            },
            "testApk": {
                "sha256": test_apk.sha256,
                "installedSha256": installed_test,
            },
        },
        "evidence": {
            "boundaries": per_boundary,
            "hostForceStops": observed["hostForceStops"],
            "abnormalPrepareTerminations": observed[
                "abnormalPrepareTerminations"
            ],
            "independentRecoveryProcesses": observed[
                "independentRecoveryProcesses"
            ],
            "turns": observed["turns"],
            "threads": observed["threads"],
            "maxTurnCountPerBusinessKey": remote_audit["counts"][
                "maxTurnCountPerBusinessKey"
            ],
            "maxEffectCountPerBusinessKey": remote_audit["counts"][
                "maxEffectCountPerBusinessKey"
            ],
            "duplicateBubbles": sum(verified_duplicate_counts),
            "finalOutboxEntries": sum(verified_outbox_counts),
            "outboxZeroRecoveryProcesses": sum(
                value == 0 for value in verified_outbox_counts
            ),
            "androidTupleSha256": android_tuple_digest,
            "androidBusinessKeySha256": android_key_digest,
            "checkpointAggregateSha256": aggregate.hexdigest(),
            "hostDatabaseAudit": remote_audit,
        },
        "safeguards": {
            "activityLaunches": 0,
            "screenCoordinatesSent": 0,
            "screenClicksSent": 0,
            "screenTextInputsSent": 0,
            "arbitraryRemoteCommands": 0,
            "fixedRepetitions": REPETITIONS,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    manifest = Path(args.manifest)
    try:
        if not manifest.is_absolute() or not manifest.parent.is_dir():
            raise GateError("Manifest path is invalid")
        relay_gate.require_new_manifest_path(str(manifest))
    except (GateError, relay_gate.ConfigError) as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        document = execute(args)
    except (GateError, relay_gate.GateError, OSError, ValueError) as error:
        document = {
            "schema": "chebycodex.android-exact-crash-gate.v1",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "outcome": "FAIL",
            "durationSeconds": round(time.monotonic() - started, 3),
            "failure": str(error),
        }
        try:
            write_manifest(manifest, document)
        except (GateError, relay_gate.GateError) as manifest_error:
            print(f"CONFIG_ERROR: {manifest_error}", file=sys.stderr)
            return 2
        return 1
    document["durationSeconds"] = round(time.monotonic() - started, 3)
    try:
        write_manifest(manifest, document)
    except GateError as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
