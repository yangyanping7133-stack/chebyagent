#!/usr/bin/env python3
"""Real-device fault and eight-hour soak gates for the isolated Android stack.

The fault mode drives ten app process kills and ten causal Wi-Fi, Edge, Relay
and Connector interruptions. App kills use separate prepare/recover
instrumentation processes. Transport cycles are fault-held queued recovery:
the component is down and Android has observed offline before A1/A2/B1 are
enqueued as QUEUED. They are not NETWORK_SENT or ACCEPTED crash-boundary
evidence. Host actions are acknowledged through an app-private file only after
the state change is observed.

The soak mode runs 500 production AppViewModel Turns over eight Threads for at
least eight monotonic hours, then reuses the read-only four-layer business-key
audit. Neither mode launches an Activity or sends screen coordinates/text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import android_relay_gate as relay_gate


FAULT_TEST_CLASS = (
    "com.cheby.codex.mobile.gateway.RelayFaultGateInstrumentedTest"
)
APP_KILL_PREPARE_METHOD = "prepareAppKillRecoveryPhase"
APP_KILL_RECOVER_METHOD = "recoverAppKillRecoveryPhase"
TRANSPORT_METHOD = "recoverFaultHeldQueuedTurnsAfterCausalTransportFaults"
FAULT_PHASE_ARGUMENT = "cheby_relay_fault_phase"
FAULT_RUN_TOKEN_ARGUMENT = "cheby_relay_fault_run_token"
FAULT_ACTION_NONCE_ARGUMENT = "cheby_relay_fault_action_nonce"
FAULT_ITERATION_ARGUMENT = "cheby_relay_fault_iteration"
FAULT_REPETITIONS_ARGUMENT = "cheby_relay_fault_repetitions"
SOAK_DURATION_ARGUMENT = "cheby_relay_soak_min_duration_millis"
FAULT_ACK_FILE = "cheby-gate-fault-ack-v2"
REQUIRED_REPETITIONS = 10
REQUIRED_SOAK_THREADS = 8
REQUIRED_SOAK_TURNS = 500
REQUIRED_SOAK_MILLIS = 8 * 60 * 60 * 1000
REQUIRED_SOAK_SECONDS = REQUIRED_SOAK_MILLIS / 1000.0
REQUIRED_FAULT_CYCLES = REQUIRED_REPETITIONS * 5
REQUIRED_FAULT_TURNS = REQUIRED_FAULT_CYCLES * 5
REQUIRED_FAULT_THREADS = REQUIRED_FAULT_CYCLES * 2
EVIDENCE_STATUS_CODE = 2
STATUS_LINE = re.compile(
    r"^INSTRUMENTATION_STATUS: (cheby_gate_[a-z_]+)=(.{0,512})$"
)
HEX_32 = re.compile(r"^[a-f0-9]{32}$")
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
STARTED_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z$"
)
SAFE_PHASE = re.compile(r"^[a-z][a-z-]{0,31}$")
MAX_LINE_CHARS = 16 * 1024
MAX_STATE_OUTPUT_BYTES = 64 * 1024
REMOTE_COMPONENTS = ("edge", "relay", "connector")
COMPONENTS_WITHOUT_HEALTHCHECK = frozenset({"edge", "connector"})
RESTORE_HEALTH_TIMEOUT_SECONDS = 180.0
RESTORE_HEALTH_POLL_SECONDS = 0.5
EXPECTED_GATE_CONTAINERS = {
    "edge": "chebycodex-relay-edge-edge-gate-1",
    "relay": "chebycodex-relay-edge-relay-gate-1",
    "connector": "chebycodex-turkey-gate-connector-gate-connector-1",
}
REMOTE_FAULT_AUDIT_PROGRAM = (
    "/usr/local/sbin/chebycodex-audit-gate-business-keys"
)
REMOTE_FAULT_AUDIT_SCHEMA = "chebycodex.gate-fault-run-audit.v1"
ALL_COMPONENTS = frozenset((*REMOTE_COMPONENTS, "wifi", "app", "all"))
ALLOWED_EVENT_KEYS = frozenset(
    {
        "cheby_gate_checkpoint",
        "cheby_gate_action_nonce",
        "cheby_gate_component",
        "cheby_gate_iteration",
        "cheby_gate_phase_digest",
        "cheby_gate_turns",
        "cheby_gate_outbox_entries",
        "cheby_gate_duplicate_bubbles",
        "cheby_gate_production_queue",
        "cheby_gate_action_observed",
    }
)


@dataclass(frozen=True)
class GateEvent:
    checkpoint: str
    nonce: str
    component: str
    iteration: int
    turns: int
    outbox_entries: int
    duplicate_bubbles: int
    action_observed: bool
    phase_digest: str | None = None


@dataclass
class InstrumentationRun:
    events: list[GateEvent] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)
    instrumentation_code: int | None = None
    junit_ok: bool = False
    junit_failed: bool = False


@dataclass(frozen=True)
class ContainerState:
    started_at: str
    running: bool
    health: str | None


@dataclass(frozen=True)
class RestartEvidence:
    component: str
    iteration: int
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class StoppedContainer:
    component: str
    iteration: int
    container: str
    started_at_before: str


@dataclass
class FaultEvidence:
    app_prepare_runs: int = 0
    app_recovery_runs: int = 0
    app_force_stops: int = 0
    transport_instrumentation_runs: int = 0
    recovered: dict[str, int] = field(
        default_factory=lambda: {
            "app": 0,
            "wifi": 0,
            "edge": 0,
            "relay": 0,
            "connector": 0,
        }
    )
    wifi_disable_commands: int = 0
    wifi_enable_commands: int = 0
    wifi_restored: bool = False
    queued_business_keys: int = 0
    verified_turns: int = 0
    final_outbox_entries: int | None = None
    duplicate_bubbles: int | None = None
    phase_digests: list[str] = field(default_factory=list)
    restart_evidence: list[RestartEvidence] = field(default_factory=list)
    audited_cycles: int = 0
    permanent_accepted_entries: int | None = None
    remote_audit: dict[str, object] | None = None


def _parse_bool(value: str, label: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise relay_gate.GateError(f"{label} was malformed")


def _parse_nonnegative_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise relay_gate.GateError(f"{label} was malformed") from error
    if parsed < 0:
        raise relay_gate.GateError(f"{label} was malformed")
    return parsed


def parse_gate_event(values: Mapping[str, str]) -> GateEvent:
    required = ALLOWED_EVENT_KEYS - {"cheby_gate_phase_digest"}
    if not required.issubset(values) or not set(values).issubset(ALLOWED_EVENT_KEYS):
        raise relay_gate.GateError("Fault checkpoint evidence schema was invalid")
    nonce = values["cheby_gate_action_nonce"]
    component = values["cheby_gate_component"]
    checkpoint = values["cheby_gate_checkpoint"]
    if HEX_32.fullmatch(nonce) is None:
        raise relay_gate.GateError("Fault checkpoint nonce was invalid")
    if component not in ALL_COMPONENTS:
        raise relay_gate.GateError("Fault checkpoint component was invalid")
    if not checkpoint or len(checkpoint) > 64:
        raise relay_gate.GateError("Fault checkpoint name was invalid")
    iteration = _parse_nonnegative_int(
        values["cheby_gate_iteration"],
        "Fault checkpoint iteration",
    )
    if not 1 <= iteration <= REQUIRED_REPETITIONS:
        raise relay_gate.GateError("Fault checkpoint iteration was outside the gate")
    turns = _parse_nonnegative_int(
        values["cheby_gate_turns"],
        "Fault checkpoint Turn count",
    )
    outbox_entries = _parse_nonnegative_int(
        values["cheby_gate_outbox_entries"],
        "Fault checkpoint Outbox count",
    )
    duplicate_bubbles = _parse_nonnegative_int(
        values["cheby_gate_duplicate_bubbles"],
        "Fault checkpoint duplicate count",
    )
    if not _parse_bool(
        values["cheby_gate_production_queue"],
        "Fault checkpoint production queue assertion",
    ):
        raise relay_gate.GateError("Fault checkpoint bypassed the production queue")
    digest = values.get("cheby_gate_phase_digest")
    if digest is not None and SHA256_HEX.fullmatch(digest) is None:
        raise relay_gate.GateError("Fault checkpoint phase digest was invalid")
    return GateEvent(
        checkpoint=checkpoint,
        nonce=nonce,
        component=component,
        iteration=iteration,
        turns=turns,
        outbox_entries=outbox_entries,
        duplicate_bubbles=duplicate_bubbles,
        action_observed=_parse_bool(
            values["cheby_gate_action_observed"],
            "Fault checkpoint action assertion",
        ),
        phase_digest=digest,
    )


def _read_lines(stream: Iterable[str], output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def run_fault_instrumentation(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    event_handler: Callable[[GateEvent], None] | None = None,
    expected_host_kill_checkpoint: str | None = None,
) -> InstrumentationRun:
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
        raise relay_gate.GateError("Fault instrumentation could not start") from error
    assert process.stdout is not None
    output: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_lines,
        args=(process.stdout, output),
        name="android-fault-gate-output",
        daemon=True,
    )
    reader.start()
    evidence = InstrumentationRun()
    pending: dict[str, str] = {}
    deadline = time.monotonic() + timeout_seconds
    stream_ended = False
    expected_kill_seen = False
    try:
        while not stream_ended:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise relay_gate.GateError("Fault instrumentation timed out")
            try:
                line = output.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    stream_ended = True
                continue
            if line is None:
                stream_ended = True
                continue
            if len(line) > MAX_LINE_CHARS:
                raise relay_gate.GateError(
                    "Fault instrumentation emitted an oversized line"
                )
            stripped = line.rstrip("\r\n")
            match = STATUS_LINE.fullmatch(stripped)
            if match is not None:
                key, value = match.groups()
                if key not in ALLOWED_EVENT_KEYS:
                    raise relay_gate.GateError(
                        "Fault instrumentation emitted unknown Gate evidence"
                    )
                if key in pending or any(ord(character) < 0x20 for character in value):
                    raise relay_gate.GateError(
                        "Fault instrumentation evidence was malformed"
                    )
                pending[key] = value
                continue
            if stripped.startswith("INSTRUMENTATION_STATUS: cheby_gate_"):
                raise relay_gate.GateError(
                    "Fault instrumentation evidence line was malformed"
                )
            if stripped.startswith("INSTRUMENTATION_STATUS_CODE:"):
                try:
                    code = int(stripped.partition(":")[2].strip())
                except ValueError as error:
                    raise relay_gate.GateError(
                        "Fault instrumentation status code was malformed"
                    ) from error
                evidence.status_codes.append(code)
                if code == EVIDENCE_STATUS_CODE:
                    event = parse_gate_event(pending)
                    evidence.events.append(event)
                    pending = {}
                    if event_handler is not None:
                        if (
                            event.checkpoint == expected_host_kill_checkpoint
                            and process.poll() is not None
                        ):
                            raise relay_gate.GateError(
                                "App process exited before the host force-stop"
                            )
                        event_handler(event)
                    if event.checkpoint == expected_host_kill_checkpoint:
                        expected_kill_seen = True
                else:
                    pending = {}
                continue
            if stripped.startswith("INSTRUMENTATION_CODE:"):
                if evidence.instrumentation_code is not None:
                    raise relay_gate.GateError(
                        "Fault instrumentation emitted duplicate final codes"
                    )
                try:
                    evidence.instrumentation_code = int(
                        stripped.partition(":")[2].strip()
                    )
                except ValueError as error:
                    raise relay_gate.GateError(
                        "Fault instrumentation final code was malformed"
                    ) from error
            elif stripped.startswith("OK (") and stripped.endswith(")"):
                evidence.junit_ok = True
            elif stripped == "FAILURES!!!":
                evidence.junit_failed = True
        return_code = process.wait(
            timeout=max(1.0, deadline - time.monotonic())
        )
    except (relay_gate.GateError, subprocess.TimeoutExpired):
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
            raise relay_gate.GateError(
                "Fault instrumentation output reader did not stop"
            )
    if expected_host_kill_checkpoint is not None:
        validate_expected_host_kill(
            evidence,
            expected_host_kill_checkpoint,
            expected_kill_seen,
        )
        return evidence
    if return_code != 0:
        raise relay_gate.GateError("Fault instrumentation process failed")
    validate_fault_run(evidence)
    return evidence


def validate_expected_host_kill(
    evidence: InstrumentationRun,
    checkpoint: str,
    checkpoint_seen_while_alive: bool,
) -> None:
    if (
        not checkpoint_seen_while_alive
        or not evidence.events
        or evidence.events[-1].checkpoint != checkpoint
        or evidence.instrumentation_code == -1
        or evidence.junit_ok
        or evidence.junit_failed
        or not evidence.status_codes
        or evidence.status_codes[0] != 1
        or any(
            code not in {0, EVIDENCE_STATUS_CODE}
            for code in evidence.status_codes[1:]
        )
        or 0 in evidence.status_codes[1:-1]
        or len(evidence.events)
        != evidence.status_codes.count(EVIDENCE_STATUS_CODE)
    ):
        raise relay_gate.GateError(
            "App-kill prepare was not terminated from a live checkpoint"
        )


def validate_fault_run(evidence: InstrumentationRun) -> None:
    if (
        evidence.instrumentation_code != -1
        or not evidence.junit_ok
        or evidence.junit_failed
    ):
        raise relay_gate.GateError(
            "Fault instrumentation did not report a clean JUnit pass"
        )
    if (
        not evidence.status_codes
        or evidence.status_codes[0] != 1
        or evidence.status_codes[-1] != 0
        or any(
            code != EVIDENCE_STATUS_CODE
            for code in evidence.status_codes[1:-1]
        )
        or len(evidence.events)
        != evidence.status_codes.count(EVIDENCE_STATUS_CODE)
    ):
        raise relay_gate.GateError(
            "Fault instrumentation status sequence was incomplete or skipped"
        )


def _instrumentation_command(
    adb: Path,
    serial: str,
    test_method: str,
    arguments: Sequence[tuple[str, str]],
) -> list[str]:
    command = [
        *relay_gate.adb_prefix(adb, serial),
        "shell",
        "am",
        "instrument",
        "-w",
        "-r",
        "-e",
        "class",
        f"{FAULT_TEST_CLASS}#{test_method}",
    ]
    for key, value in arguments:
        command.extend(("-e", key, value))
    command.append(relay_gate.RUNNER_COMPONENT)
    return command


def build_app_kill_phase_command(
    adb: Path,
    serial: str,
    *,
    phase: str,
    run_token: str,
    action_nonce: str,
    iteration: int,
    expected_reply: str,
) -> list[str]:
    if phase not in {"app-kill-prepare", "app-kill-recover"}:
        raise relay_gate.ConfigError("Unknown app-kill phase")
    if HEX_32.fullmatch(run_token) is None or HEX_32.fullmatch(action_nonce) is None:
        raise relay_gate.ConfigError("App-kill identity was invalid")
    if not 1 <= iteration <= REQUIRED_REPETITIONS:
        raise relay_gate.ConfigError("App-kill iteration was invalid")
    method = (
        APP_KILL_PREPARE_METHOD
        if phase == "app-kill-prepare"
        else APP_KILL_RECOVER_METHOD
    )
    return _instrumentation_command(
        adb,
        serial,
        method,
        (
            (FAULT_PHASE_ARGUMENT, phase),
            (FAULT_RUN_TOKEN_ARGUMENT, run_token),
            (FAULT_ACTION_NONCE_ARGUMENT, action_nonce),
            (FAULT_ITERATION_ARGUMENT, str(iteration)),
            (
                "cheby_relay_expected_reply_b64url",
                relay_gate.encode_expected_reply(expected_reply),
            ),
        ),
    )


def build_transport_command(
    adb: Path,
    serial: str,
    *,
    run_token: str,
    expected_reply: str,
) -> list[str]:
    if HEX_32.fullmatch(run_token) is None:
        raise relay_gate.ConfigError("Fault run token was invalid")
    return _instrumentation_command(
        adb,
        serial,
        TRANSPORT_METHOD,
        (
            (FAULT_PHASE_ARGUMENT, "transport"),
            (FAULT_RUN_TOKEN_ARGUMENT, run_token),
            (FAULT_REPETITIONS_ARGUMENT, str(REQUIRED_REPETITIONS)),
            (
                "cheby_relay_expected_reply_b64url",
                relay_gate.encode_expected_reply(expected_reply),
            ),
        ),
    )


def build_soak_command(
    adb: Path,
    serial: str,
    expected_reply: str,
) -> list[str]:
    command = relay_gate.build_instrumentation_command(
        adb=adb,
        serial=serial,
        mode="load",
        expected_reply=expected_reply,
        threads=REQUIRED_SOAK_THREADS,
        turns=REQUIRED_SOAK_TURNS,
    )
    command[-1:-1] = [
        "-e",
        SOAK_DURATION_ARGUMENT,
        str(REQUIRED_SOAK_MILLIS),
    ]
    return command


def build_fault_ack_command(adb: Path, serial: str) -> list[str]:
    return [
        *relay_gate.adb_prefix(adb, serial),
        "exec-out",
        "run-as",
        relay_gate.TARGET_PACKAGE,
        "sh",
        "-c",
        f"umask 077; cat > files/{FAULT_ACK_FILE}",
    ]


def write_fault_ack(
    adb: Path,
    serial: str,
    event: GateEvent,
    observed_state: str,
) -> None:
    if observed_state not in {
        "stopped",
        "started",
        "disabled",
        "enabled",
        "outbox-held",
    }:
        raise relay_gate.GateError("Fault acknowledgement state was invalid")
    payload = (
        f"{event.nonce}|{event.component}|{event.iteration}|{observed_state}\n"
    ).encode("ascii")
    try:
        result = subprocess.run(
            build_fault_ack_command(adb, serial),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise relay_gate.GateError(
            "Fault acknowledgement did not complete"
        ) from error
    if result.returncode != 0 or len(result.stdout) > MAX_STATE_OUTPUT_BYTES:
        raise relay_gate.GateError("Fault acknowledgement failed")


def build_wifi_command(adb: Path, serial: str, enabled: bool) -> list[str]:
    return [
        *relay_gate.adb_prefix(adb, serial),
        "shell",
        "svc",
        "wifi",
        "enable" if enabled else "disable",
    ]


def _run_state_probe(command: Sequence[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise relay_gate.GateError("Device state probe did not complete") from error
    if len(result.stdout) > MAX_STATE_OUTPUT_BYTES:
        raise relay_gate.GateError("Device state probe output exceeded the limit")
    return result.returncode, result.stdout.decode("utf-8", errors="replace").strip()


def read_wifi_enabled(adb: Path, serial: str) -> bool:
    code, text = _run_state_probe(
        [*relay_gate.adb_prefix(adb, serial), "shell", "cmd", "wifi", "status"]
    )
    normalized = text.lower()
    if code == 0:
        if "wi-fi is enabled" in normalized or "wifi is enabled" in normalized:
            return True
        if "wi-fi is disabled" in normalized or "wifi is disabled" in normalized:
            return False
    code, text = _run_state_probe(
        [
            *relay_gate.adb_prefix(adb, serial),
            "shell",
            "settings",
            "get",
            "global",
            "wifi_on",
        ]
    )
    if code == 0 and text in {"0", "1"}:
        return text == "1"
    raise relay_gate.GateError("Device Wi-Fi state was not observable")


def await_wifi_state(
    adb: Path,
    serial: str,
    expected: bool,
    *,
    timeout_seconds: float = 60.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    pacing = threading.Event()
    while True:
        if read_wifi_enabled(adb, serial) is expected:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise relay_gate.GateError("Device Wi-Fi did not reach the requested state")
        pacing.wait(min(0.5, remaining))


def force_stop_and_verify(adb: Path, serial: str, package_name: str) -> None:
    relay_gate.run_bounded(
        [
            *relay_gate.adb_prefix(adb, serial),
            "shell",
            "am",
            "force-stop",
            package_name,
        ],
        timeout_seconds=30,
        label="Gate package force-stop",
    )
    code, output = _run_state_probe(
        [*relay_gate.adb_prefix(adb, serial), "shell", "pidof", package_name]
    )
    if output or code not in {0, 1}:
        raise relay_gate.GateError("Gate package remained alive after force-stop")


def build_container_inspect_command(
    ssh: Path,
    ssh_host: str,
    container: str,
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
        "--format={{json .State}}",
        container,
    ]


def build_container_restart_command(
    ssh: Path,
    ssh_host: str,
    container: str,
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
        container,
    ]


def build_container_stop_command(
    ssh: Path,
    ssh_host: str,
    container: str,
) -> list[str]:
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "docker",
        "stop",
        "--time",
        "20",
        container,
    ]


def build_container_start_command(
    ssh: Path,
    ssh_host: str,
    container: str,
) -> list[str]:
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "docker",
        "start",
        container,
    ]


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise relay_gate.GateError(
                "Container state contained duplicate JSON keys"
            )
        result[key] = value
    return result


def read_container_state(
    ssh: Path,
    ssh_host: str,
    container: str,
) -> ContainerState:
    output = relay_gate.run_bounded(
        build_container_inspect_command(ssh, ssh_host, container),
        timeout_seconds=30,
        label="Gate container state inspection",
    )
    try:
        document = json.loads(
            output.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise relay_gate.GateError("Gate container state was malformed") from error
    if not isinstance(document, dict):
        raise relay_gate.GateError("Gate container state was malformed")
    started_at = document.get("StartedAt")
    running = document.get("Running")
    health_value = document.get("Health")
    if (
        not isinstance(started_at, str)
        or STARTED_AT.fullmatch(started_at) is None
        or type(running) is not bool
    ):
        raise relay_gate.GateError("Gate container epoch was invalid")
    health: str | None = None
    if health_value is not None:
        if not isinstance(health_value, dict):
            raise relay_gate.GateError("Gate container health was invalid")
        status = health_value.get("Status")
        if status not in {"starting", "healthy", "unhealthy"}:
            raise relay_gate.GateError("Gate container health was invalid")
        health = str(status)
    return ContainerState(started_at=started_at, running=running, health=health)


def restart_container_causally(
    ssh: Path,
    ssh_host: str,
    container: str,
    component: str,
    iteration: int,
) -> RestartEvidence:
    before = read_container_state(ssh, ssh_host, container)
    output = relay_gate.run_bounded(
        build_container_restart_command(ssh, ssh_host, container),
        timeout_seconds=120,
        label="Gate container restart",
    )
    lines = output.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[-1].strip() != container:
        raise relay_gate.GateError("Gate container restart was not acknowledged")
    deadline = time.monotonic() + 180.0
    pacing = threading.Event()
    while True:
        after = read_container_state(ssh, ssh_host, container)
        ready = (
            after.started_at != before.started_at
            and container_state_is_ready(component, after)
        )
        if ready:
            return RestartEvidence(
                component=component,
                iteration=iteration,
                before_sha256=hashlib.sha256(
                    before.started_at.encode("ascii")
                ).hexdigest(),
                after_sha256=hashlib.sha256(
                    after.started_at.encode("ascii")
                ).hexdigest(),
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise relay_gate.GateError(
                "Gate container did not reach a new ready epoch"
            )
        pacing.wait(min(0.5, remaining))


def stop_container_causally(
    ssh: Path,
    ssh_host: str,
    container: str,
    component: str,
    iteration: int,
    register_intent: Callable[[StoppedContainer], None],
) -> StoppedContainer:
    before = read_container_state(ssh, ssh_host, container)
    if not before.running:
        raise relay_gate.GateError(
            "Gate fault component was not running before the down checkpoint"
        )
    intent = StoppedContainer(
        component=component,
        iteration=iteration,
        container=container,
        started_at_before=before.started_at,
    )
    # Register before issuing docker stop. A timeout or lost acknowledgement
    # after Docker applies the stop must still be recoverable in finally.
    register_intent(intent)
    output = relay_gate.run_bounded(
        build_container_stop_command(ssh, ssh_host, container),
        timeout_seconds=120,
        label="Gate container stop",
    )
    lines = output.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[-1].strip() != container:
        raise relay_gate.GateError("Gate container stop was not acknowledged")
    deadline = time.monotonic() + 120.0
    pacing = threading.Event()
    while True:
        current = read_container_state(ssh, ssh_host, container)
        if not current.running:
            return intent
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise relay_gate.GateError("Gate container did not stop")
        pacing.wait(min(0.5, remaining))


def container_state_is_ready(
    component: str,
    state: ContainerState,
) -> bool:
    return state.running and (
        state.health == "healthy"
        or (
            state.health is None
            and component in COMPONENTS_WITHOUT_HEALTHCHECK
        )
    )


def await_running_container_ready_or_stopped(
    ssh: Path,
    ssh_host: str,
    container: str,
    component: str,
    current: ContainerState,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> ContainerState:
    if timeout_seconds < 0 or poll_seconds < 0:
        raise relay_gate.GateError("Gate restoration health budget was invalid")
    if (
        current.running
        and current.health is None
        and component not in COMPONENTS_WITHOUT_HEALTHCHECK
    ):
        raise relay_gate.GateError(
            "Gate component lacks its required health check"
        )
    deadline = time.monotonic() + timeout_seconds
    pacing = threading.Event()
    while current.running and not container_state_is_ready(component, current):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise relay_gate.GateError(
                "Gate component did not become healthy during restoration"
            )
        pacing.wait(min(poll_seconds, remaining))
        current = read_container_state(ssh, ssh_host, container)
    return current


def restore_initially_running_gate_containers(
    ssh: Path,
    ssh_host: str,
    containers: Mapping[str, str],
    initial_states: Mapping[str, ContainerState],
    stop_intents: Mapping[tuple[str, int, str], StoppedContainer],
    *,
    health_timeout_seconds: float = RESTORE_HEALTH_TIMEOUT_SECONDS,
    health_poll_seconds: float = RESTORE_HEALTH_POLL_SECONDS,
) -> tuple[list[RestartEvidence], bool]:
    evidence: list[RestartEvidence] = []
    failed = False
    for component in REMOTE_COMPONENTS:
        container = containers[component]
        initial = initial_states.get(component)
        try:
            current = read_container_state(ssh, ssh_host, container)
        except relay_gate.GateError:
            failed = True
            continue
        if initial is None:
            failed = True
            continue
        if not initial.running:
            continue
        if current.running:
            try:
                current = await_running_container_ready_or_stopped(
                    ssh,
                    ssh_host,
                    container,
                    component,
                    current,
                    timeout_seconds=health_timeout_seconds,
                    poll_seconds=health_poll_seconds,
                )
            except relay_gate.GateError:
                failed = True
                continue
            if current.running:
                continue
        candidates = [
            intent
            for intent in stop_intents.values()
            if intent.component == component and intent.container == container
        ]
        restore_intent = candidates[-1] if candidates else StoppedContainer(
            component=component,
            iteration=0,
            container=container,
            started_at_before=initial.started_at,
        )
        try:
            evidence.append(
                start_container_causally(ssh, ssh_host, restore_intent)
            )
        except relay_gate.GateError:
            # A start acknowledgement can be lost after Docker has restored
            # the container. One final read-only probe distinguishes that
            # safe state from an actual restoration failure.
            try:
                restored = read_container_state(ssh, ssh_host, container)
            except relay_gate.GateError:
                failed = True
            else:
                try:
                    restored = await_running_container_ready_or_stopped(
                        ssh,
                        ssh_host,
                        container,
                        component,
                        restored,
                        timeout_seconds=health_timeout_seconds,
                        poll_seconds=health_poll_seconds,
                    )
                except relay_gate.GateError:
                    failed = True
                if not container_state_is_ready(component, restored):
                    failed = True
    return evidence, failed


def assert_container_stopped(
    ssh: Path,
    ssh_host: str,
    stopped: StoppedContainer,
) -> None:
    if read_container_state(ssh, ssh_host, stopped.container).running:
        raise relay_gate.GateError(
            "Gate component restarted before the Outbox checkpoint"
        )


def start_container_causally(
    ssh: Path,
    ssh_host: str,
    stopped: StoppedContainer,
) -> RestartEvidence:
    assert_container_stopped(ssh, ssh_host, stopped)
    output = relay_gate.run_bounded(
        build_container_start_command(ssh, ssh_host, stopped.container),
        timeout_seconds=120,
        label="Gate container start",
    )
    lines = output.decode("utf-8", errors="replace").splitlines()
    if not lines or lines[-1].strip() != stopped.container:
        raise relay_gate.GateError("Gate container start was not acknowledged")
    deadline = time.monotonic() + 180.0
    pacing = threading.Event()
    while True:
        after = read_container_state(ssh, ssh_host, stopped.container)
        ready = (
            after.started_at != stopped.started_at_before
            and container_state_is_ready(stopped.component, after)
        )
        if ready:
            return RestartEvidence(
                component=stopped.component,
                iteration=stopped.iteration,
                before_sha256=hashlib.sha256(
                    stopped.started_at_before.encode("ascii")
                ).hexdigest(),
                after_sha256=hashlib.sha256(
                    after.started_at.encode("ascii")
                ).hexdigest(),
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise relay_gate.GateError(
                "Gate container did not reach a new ready epoch"
            )
        pacing.wait(min(0.5, remaining))


def validate_app_prepare(
    run: InstrumentationRun,
    *,
    iteration: int,
    nonce: str,
) -> GateEvent:
    if len(run.events) != 2:
        raise relay_gate.GateError("App-kill prepare checkpoint count was invalid")
    down, event = run.events
    if (
        down.checkpoint != "app_prepare_offline_required"
        or down.component != "edge"
        or down.iteration != iteration
        or down.nonce != nonce
        or down.action_observed
        or down.turns != 0
        or down.outbox_entries != 0
        or down.duplicate_bubbles != 0
        or event.checkpoint != "app_kill_outbox_ready"
        or event.component != "app"
        or event.iteration != iteration
        or event.nonce != nonce
        or event.action_observed
        or event.phase_digest is None
        or event.turns != 0
        or event.outbox_entries != 3
        or event.duplicate_bubbles != 0
    ):
        raise relay_gate.GateError("App-kill prepare evidence did not match")
    return event


def validate_app_recovery(
    run: InstrumentationRun,
    *,
    iteration: int,
    nonce: str,
) -> GateEvent:
    if len(run.events) != 1:
        raise relay_gate.GateError("App-kill recovery checkpoint count was invalid")
    event = run.events[0]
    if (
        event.checkpoint != "app_kill_recovered"
        or event.component != "app"
        or event.iteration != iteration
        or event.nonce != nonce
        or not event.action_observed
        or event.phase_digest is None
        or event.turns != 5
        or event.outbox_entries != 0
        or event.duplicate_bubbles != 0
    ):
        raise relay_gate.GateError("App-kill recovery evidence did not match")
    return event


def validate_transport_sequence(run: InstrumentationRun) -> None:
    expected_events = REQUIRED_REPETITIONS * 4 * 4 + 1
    if len(run.events) != expected_events:
        raise relay_gate.GateError("Fault gate completion evidence count was invalid")
    index = 0
    for iteration in range(1, REQUIRED_REPETITIONS + 1):
        for component in (*REMOTE_COMPONENTS, "wifi"):
            down = run.events[index]
            queued = run.events[index + 1]
            up = run.events[index + 2]
            recovered = run.events[index + 3]
            if (
                down.checkpoint != "fault_down_required"
                or queued.checkpoint != "fault_outbox_ready"
                or up.checkpoint != "fault_up_required"
                or recovered.checkpoint != "fault_recovered"
                or any(
                    value.component != component or value.iteration != iteration
                    for value in (down, queued, up, recovered)
                )
                or len({down.nonce, queued.nonce, up.nonce, recovered.nonce}) != 1
                or down.action_observed
                or queued.action_observed
                or up.action_observed
                or not recovered.action_observed
                or down.turns != 0
                or queued.turns != 0
                or up.turns != 0
                or recovered.turns != 5
                or down.outbox_entries != 0
                or queued.outbox_entries != 3
                or up.outbox_entries != 3
                or recovered.outbox_entries != 0
                or any(
                    value.duplicate_bubbles != 0
                    for value in (down, queued, up, recovered)
                )
            ):
                raise relay_gate.GateError("Transport fault evidence was out of order")
            index += 4
    complete = run.events[index]
    if (
        complete.checkpoint != "fault_gate_complete"
        or complete.component != "all"
        or complete.iteration != REQUIRED_REPETITIONS
        or not complete.action_observed
        or complete.turns != REQUIRED_REPETITIONS * 4 * 5
        or complete.outbox_entries != 0
        or complete.duplicate_bubbles != 0
    ):
        raise relay_gate.GateError("Fault gate completion evidence was invalid")


def _phase_digest_aggregate(values: Sequence[str]) -> str | None:
    if not values:
        return None
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _sanitized_remote_audit(
    document: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": document["schema"],
        "outcome": document["outcome"],
        "counts": document["counts"],
        "digests": document["digests"],
    }


def build_fault_audit_command(
    ssh: Path,
    ssh_host: str,
    run_token: str,
) -> list[str]:
    if HEX_32.fullmatch(run_token) is None:
        raise relay_gate.GateError("Fault run token was invalid")
    return [
        str(ssh),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        ssh_host,
        "sudo",
        "-n",
        REMOTE_FAULT_AUDIT_PROGRAM,
        "--mode",
        "fault",
        "--run-token",
        run_token,
    ]


def _reject_duplicate_audit_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise relay_gate.GateError(
                "Remote fault audit emitted duplicate JSON keys"
            )
        result[key] = value
    return result


def read_fault_audit(
    plan: relay_gate.AuditPlan,
    run_token: str,
) -> dict[str, object]:
    output = relay_gate.run_bounded(
        build_fault_audit_command(plan.ssh, plan.ssh_host, run_token),
        timeout_seconds=120,
        label="Gate fault business-key audit",
    )
    try:
        document = json.loads(
            output.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_audit_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise relay_gate.GateError(
            "Remote fault audit output was malformed"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "outcome",
        "runToken",
        "counts",
        "digests",
    }:
        raise relay_gate.GateError("Remote fault audit schema was invalid")
    counts = document.get("counts")
    digests = document.get("digests")
    expected_counts = {
        "turns": REQUIRED_FAULT_TURNS,
        "threads": REQUIRED_FAULT_THREADS,
        "relayTuples": REQUIRED_FAULT_TURNS,
        "connectorTuples": REQUIRED_FAULT_TURNS,
        "gatewayTuples": REQUIRED_FAULT_TURNS,
        "fakeCodexTuples": REQUIRED_FAULT_TURNS,
        "bindingGenerationMappings": 1,
        "relevantConnectorIntents": 0,
        "relevantConnectorOutbox": 0,
        "relevantConnectorProcessedDeliveries": 0,
        "cycles": REQUIRED_FAULT_CYCLES,
        "businessKeys": REQUIRED_FAULT_TURNS,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }
    expected_digest_keys = {
        "tupleSha256",
        "relayTupleSha256",
        "connectorTupleSha256",
        "gatewayTupleSha256",
        "fakeCodexTupleSha256",
        "bindingGenerationMappingSha256",
    }
    if (
        document.get("schema") != REMOTE_FAULT_AUDIT_SCHEMA
        or document.get("outcome") != "PASS"
        or document.get("runToken") != run_token
        or not isinstance(counts, dict)
        or counts != expected_counts
        or any(type(counts.get(key)) is not int for key in expected_counts)
        or not isinstance(digests, dict)
        or set(digests) != expected_digest_keys
        or any(
            not isinstance(value, str)
            or SHA256_HEX.fullmatch(value) is None
            for value in digests.values()
        )
    ):
        raise relay_gate.GateError(
            "Remote fault audit did not prove the fixed run contract"
        )
    tuple_digests = {
        str(digests[key])
        for key in (
            "tupleSha256",
            "relayTupleSha256",
            "connectorTupleSha256",
            "gatewayTupleSha256",
            "fakeCodexTupleSha256",
        )
    }
    if len(tuple_digests) != 1:
        raise relay_gate.GateError(
            "Remote fault audit business-key digests diverged"
        )
    return document


def manifest_document(
    *,
    mode: str,
    outcome: str,
    gate_apk: relay_gate.Artifact,
    test_apk: relay_gate.Artifact,
    installed_gate_sha256: str | None,
    installed_test_sha256: str | None,
    duration_seconds: float,
    fault: FaultEvidence | None,
    soak: Mapping[str, object] | None,
    failure: str | None,
) -> dict[str, object]:
    if outcome == "PASS" and fault is not None and (
        fault.final_outbox_entries != 0
        or fault.duplicate_bubbles != 0
        or fault.permanent_accepted_entries != 0
    ):
        raise relay_gate.GateError(
            "Fault PASS lacked measured global Outbox/bubble convergence"
        )
    if outcome == "PASS" and soak is not None and (
        soak.get("outboxEntries") != 0
        or soak.get("duplicateBubbles") != 0
    ):
        raise relay_gate.GateError(
            "Soak PASS lacked measured global Outbox/bubble convergence"
        )
    fault_evidence: dict[str, object] | None = None
    if fault is not None:
        fault_evidence = {
            "appPrepareRuns": fault.app_prepare_runs,
            "appRecoveryRuns": fault.app_recovery_runs,
            "appForceStops": fault.app_force_stops,
            "transportInstrumentationRuns":
                fault.transport_instrumentation_runs,
            "recovered": dict(fault.recovered),
            "wifiDisableCommands": fault.wifi_disable_commands,
            "wifiEnableCommands": fault.wifi_enable_commands,
            "wifiRestored": fault.wifi_restored,
            "queuedCrossFaultBusinessKeys": fault.queued_business_keys,
            "verifiedTurns": fault.verified_turns,
            "finalOutboxEntries": fault.final_outbox_entries,
            "duplicateBubbles": fault.duplicate_bubbles,
            "auditedCycles": fault.audited_cycles,
            "permanentAcceptedEntries": fault.permanent_accepted_entries,
            "hostDatabaseAudit": fault.remote_audit,
            "phaseDigestAggregate":
                _phase_digest_aggregate(fault.phase_digests),
            "remoteRestartEpochs": [
                {
                    "component": value.component,
                    "iteration": value.iteration,
                    "beforeSha256": value.before_sha256,
                    "afterSha256": value.after_sha256,
                }
                for value in fault.restart_evidence
            ],
        }
    return {
        "schema": "chebycodex.android-fault-soak-gate.v1",
        "createdAt": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
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
            "faults": fault_evidence,
            "soak": soak,
        },
        "safeguards": {
            "activityLaunches": 0,
            "screenCoordinatesSent": 0,
            "screenTextInputsSent": 0,
            "wifiInfrastructureOnly": True,
            "rawInstrumentationStored": False,
        },
        "failure": failure,
    }


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("faults", "soak"), required=True)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--gate-apk", required=True)
    parser.add_argument("--test-apk", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-reply", default=relay_gate.GATE_REPLY_MARKER)
    parser.add_argument("--ssh", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument(
        "--edge-container",
        default="chebycodex-relay-edge-edge-gate-1",
    )
    parser.add_argument(
        "--relay-container",
        default="chebycodex-relay-edge-relay-gate-1",
    )
    parser.add_argument(
        "--connector-container",
        default="chebycodex-turkey-gate-connector-gate-connector-1",
    )
    parser.add_argument("--phase-timeout-seconds", type=float, default=1200.0)
    parser.add_argument(
        "--transport-timeout-seconds",
        type=float,
        default=21_600.0,
    )
    parser.add_argument(
        "--soak-timeout-seconds",
        type=float,
        default=36_000.0,
    )
    return parser


def validate_args(
    args: argparse.Namespace,
) -> tuple[
    Path,
    Path,
    relay_gate.Artifact,
    relay_gate.Artifact,
    relay_gate.AuditPlan,
    dict[str, str],
]:
    adb = relay_gate.require_regular_file(args.adb, "ADB", executable=True)
    ssh = relay_gate.require_regular_file(args.ssh, "SSH", executable=True)
    args.serial = relay_gate.validate_identifier(
        args.serial,
        relay_gate.SAFE_SERIAL,
        "Device serial",
    )
    args.ssh_host = relay_gate.validate_identifier(
        args.ssh_host,
        relay_gate.SAFE_REMOTE_NAME,
        "SSH host",
    )
    containers = {
        component: relay_gate.validate_identifier(
            getattr(args, f"{component}_container"),
            relay_gate.SAFE_CONTAINER_NAME,
            f"Gate {component} container",
        )
        for component in REMOTE_COMPONENTS
    }
    if containers != EXPECTED_GATE_CONTAINERS:
        raise relay_gate.ConfigError(
            "Fault gate container names must identify the isolated Gate stack"
        )
    gate_apk = relay_gate.inspect_apk(args.gate_apk, "Gate APK")
    test_apk = relay_gate.inspect_apk(args.test_apk, "Gate test APK")
    args.expected_reply = relay_gate.validate_expected_reply(args.expected_reply)
    if not 600.0 <= args.phase_timeout_seconds <= 3600.0:
        raise relay_gate.ConfigError("App-kill phase timeout is outside the gate")
    if not 7200.0 <= args.transport_timeout_seconds <= 43_200.0:
        raise relay_gate.ConfigError(
            "Transport fault timeout is outside the gate"
        )
    if not 32_400.0 <= args.soak_timeout_seconds <= 43_200.0:
        raise relay_gate.ConfigError("Soak timeout is outside the gate")
    return (
        adb,
        ssh,
        gate_apk,
        test_apk,
        relay_gate.AuditPlan(ssh=ssh, ssh_host=args.ssh_host),
        containers,
    )


def _install_gate_artifacts(
    adb: Path,
    serial: str,
    gate_apk: relay_gate.Artifact,
    test_apk: relay_gate.Artifact,
) -> tuple[str, str]:
    installed_gate = relay_gate.install_and_verify(
        adb,
        serial,
        gate_apk,
        relay_gate.TARGET_PACKAGE,
    )
    installed_test = relay_gate.install_and_verify(
        adb,
        serial,
        test_apk,
        relay_gate.TEST_PACKAGE,
    )
    force_stop_and_verify(adb, serial, relay_gate.TEST_PACKAGE)
    force_stop_and_verify(adb, serial, relay_gate.TARGET_PACKAGE)
    return installed_gate, installed_test


def execute_faults(
    args: argparse.Namespace,
    *,
    adb: Path,
    ssh: Path,
    gate_apk: relay_gate.Artifact,
    test_apk: relay_gate.Artifact,
    audit_plan: relay_gate.AuditPlan,
    containers: Mapping[str, str],
) -> tuple[dict[str, object], int]:
    started = time.monotonic()
    fault = FaultEvidence()
    installed_gate: str | None = None
    installed_test: str | None = None
    failure: str | None = None
    outcome = "FAIL"
    exit_code = 1
    wifi_initially_enabled = False
    wifi_touched = False
    initial_container_states: dict[str, ContainerState] = {}
    stopped_containers: dict[tuple[str, int, str], StoppedContainer] = {}
    try:
        installed_gate, installed_test = _install_gate_artifacts(
            adb,
            args.serial,
            gate_apk,
            test_apk,
        )
        wifi_initially_enabled = read_wifi_enabled(adb, args.serial)
        if not wifi_initially_enabled:
            raise relay_gate.GateError(
                "Fault gate requires Wi-Fi to be initially enabled"
            )
        for component in REMOTE_COMPONENTS:
            initial_container_states[component] = read_container_state(
                ssh,
                args.ssh_host,
                containers[component],
            )
        if any(
            not initial_container_states[component].running
            for component in REMOTE_COMPONENTS
        ):
            raise relay_gate.GateError(
                "Fault gate requires every isolated component initially running"
            )
        run_token = secrets.token_hex(16)
        for iteration in range(1, REQUIRED_REPETITIONS + 1):
            nonce = secrets.token_hex(16)

            def handle_app_prepare(event: GateEvent) -> None:
                key = ("edge", iteration, nonce)
                if event.checkpoint == "app_prepare_offline_required":
                    if (
                        event.component != "edge"
                        or event.iteration != iteration
                        or event.nonce != nonce
                        or event.outbox_entries != 0
                    ):
                        raise relay_gate.GateError(
                            "App-kill offline checkpoint was invalid"
                        )
                    stopped = stop_container_causally(
                        ssh,
                        args.ssh_host,
                        containers["edge"],
                        "edge",
                        iteration,
                        register_intent=lambda intent: stopped_containers.__setitem__(
                            key,
                            intent,
                        ),
                    )
                    write_fault_ack(
                        adb,
                        args.serial,
                        event,
                        "stopped",
                    )
                elif event.checkpoint == "app_kill_outbox_ready":
                    if (
                        event.component != "app"
                        or event.iteration != iteration
                        or event.nonce != nonce
                        or event.outbox_entries != 3
                        or event.phase_digest is None
                    ):
                        raise relay_gate.GateError(
                            "App-kill Outbox checkpoint was invalid"
                        )
                    stopped = stopped_containers.get(key)
                    if stopped is None:
                        raise relay_gate.GateError(
                            "App-kill checkpoint lacked a stopped Edge"
                        )
                    assert_container_stopped(ssh, args.ssh_host, stopped)
                    force_stop_and_verify(
                        adb,
                        args.serial,
                        relay_gate.TARGET_PACKAGE,
                    )
                    force_stop_and_verify(
                        adb,
                        args.serial,
                        relay_gate.TEST_PACKAGE,
                    )
                    fault.app_force_stops += 1
                    restarted = start_container_causally(
                        ssh,
                        args.ssh_host,
                        stopped,
                    )
                    fault.restart_evidence.append(restarted)
                    stopped_containers.pop(key, None)

            prepare = run_fault_instrumentation(
                build_app_kill_phase_command(
                    adb,
                    args.serial,
                    phase="app-kill-prepare",
                    run_token=run_token,
                    action_nonce=nonce,
                    iteration=iteration,
                    expected_reply=args.expected_reply,
                ),
                timeout_seconds=args.phase_timeout_seconds,
                event_handler=handle_app_prepare,
                expected_host_kill_checkpoint="app_kill_outbox_ready",
            )
            prepared = validate_app_prepare(
                prepare,
                iteration=iteration,
                nonce=nonce,
            )
            fault.app_prepare_runs += 1
            fault.queued_business_keys += prepared.outbox_entries
            recovered_run = run_fault_instrumentation(
                build_app_kill_phase_command(
                    adb,
                    args.serial,
                    phase="app-kill-recover",
                    run_token=run_token,
                    action_nonce=nonce,
                    iteration=iteration,
                    expected_reply=args.expected_reply,
                ),
                timeout_seconds=args.phase_timeout_seconds,
            )
            recovered = validate_app_recovery(
                recovered_run,
                iteration=iteration,
                nonce=nonce,
            )
            if recovered.phase_digest != prepared.phase_digest:
                raise relay_gate.GateError(
                    "App-kill durable phase digest changed after recovery"
                )
            fault.app_recovery_runs += 1
            fault.recovered["app"] += 1
            fault.verified_turns += recovered.turns
            fault.phase_digests.append(str(recovered.phase_digest))
            fault.duplicate_bubbles = (
                (fault.duplicate_bubbles or 0) + recovered.duplicate_bubbles
            )

        def handle_transport_event(event: GateEvent) -> None:
            nonlocal wifi_touched
            key = (event.component, event.iteration, event.nonce)
            if event.checkpoint == "fault_down_required":
                if event.component in REMOTE_COMPONENTS:
                    stopped = stop_container_causally(
                        ssh,
                        args.ssh_host,
                        containers[event.component],
                        event.component,
                        event.iteration,
                        register_intent=lambda intent: stopped_containers.__setitem__(
                            key,
                            intent,
                        ),
                    )
                    write_fault_ack(
                        adb,
                        args.serial,
                        event,
                        "stopped",
                    )
                elif event.component == "wifi":
                    if not read_wifi_enabled(adb, args.serial):
                        raise relay_gate.GateError(
                            "Wi-Fi fault did not start enabled"
                        )
                    relay_gate.run_bounded(
                        build_wifi_command(adb, args.serial, False),
                        timeout_seconds=30,
                        label="Device Wi-Fi disable",
                    )
                    wifi_touched = True
                    fault.wifi_disable_commands += 1
                    await_wifi_state(adb, args.serial, False)
                    write_fault_ack(adb, args.serial, event, "disabled")
            elif event.checkpoint == "fault_outbox_ready":
                if event.outbox_entries != 3:
                    raise relay_gate.GateError(
                        "Fault Outbox checkpoint did not hold three keys"
                    )
                if event.component in REMOTE_COMPONENTS:
                    stopped = stopped_containers.get(key)
                    if stopped is None:
                        raise relay_gate.GateError(
                            "Fault Outbox checkpoint lacked a stopped component"
                        )
                    assert_container_stopped(ssh, args.ssh_host, stopped)
                elif event.component == "wifi":
                    if read_wifi_enabled(adb, args.serial):
                        raise relay_gate.GateError(
                            "Wi-Fi recovered before the Outbox checkpoint"
                        )
                write_fault_ack(
                    adb,
                    args.serial,
                    event,
                    "outbox-held",
                )
            elif event.checkpoint == "fault_up_required":
                if event.component in REMOTE_COMPONENTS:
                    stopped = stopped_containers.get(key)
                    if stopped is None:
                        raise relay_gate.GateError(
                            "Fault up checkpoint lacked stopped state"
                        )
                    restarted = start_container_causally(
                        ssh,
                        args.ssh_host,
                        stopped,
                    )
                    fault.restart_evidence.append(restarted)
                    stopped_containers.pop(key, None)
                    write_fault_ack(
                        adb,
                        args.serial,
                        event,
                        "started",
                    )
                elif event.component == "wifi":
                    if read_wifi_enabled(adb, args.serial):
                        raise relay_gate.GateError(
                            "Wi-Fi up checkpoint started enabled"
                        )
                    relay_gate.run_bounded(
                        build_wifi_command(adb, args.serial, True),
                        timeout_seconds=30,
                        label="Device Wi-Fi enable",
                    )
                    fault.wifi_enable_commands += 1
                    await_wifi_state(adb, args.serial, True)
                    write_fault_ack(adb, args.serial, event, "enabled")

        transport = run_fault_instrumentation(
            build_transport_command(
                adb,
                args.serial,
                run_token=run_token,
                expected_reply=args.expected_reply,
            ),
            timeout_seconds=args.transport_timeout_seconds,
            event_handler=handle_transport_event,
        )
        fault.transport_instrumentation_runs = 1
        validate_transport_sequence(transport)
        for event in transport.events:
            if event.checkpoint == "fault_recovered":
                fault.recovered[event.component] += 1
                fault.queued_business_keys += 3
                fault.verified_turns += event.turns
        complete = transport.events[-1]
        exact = {
            "app": REQUIRED_REPETITIONS,
            "wifi": REQUIRED_REPETITIONS,
            "edge": REQUIRED_REPETITIONS,
            "relay": REQUIRED_REPETITIONS,
            "connector": REQUIRED_REPETITIONS,
        }
        if (
            fault.recovered != exact
            or fault.wifi_disable_commands != REQUIRED_REPETITIONS
            or fault.wifi_enable_commands != REQUIRED_REPETITIONS
            or len(fault.restart_evidence) != REQUIRED_REPETITIONS * 4
            or fault.queued_business_keys != REQUIRED_REPETITIONS * 5 * 3
            or fault.verified_turns != REQUIRED_REPETITIONS * 5 * 5
            or fault.duplicate_bubbles != 0
            or complete.outbox_entries != 0
            or complete.duplicate_bubbles != 0
        ):
            raise relay_gate.GateError("Fault gate did not complete exact actions")
        fault.wifi_restored = read_wifi_enabled(adb, args.serial)
        if not fault.wifi_restored:
            raise relay_gate.GateError("Fault gate did not restore Wi-Fi")
        fault.final_outbox_entries = complete.outbox_entries
        fault.duplicate_bubbles = (
            (fault.duplicate_bubbles or 0) + complete.duplicate_bubbles
        )
        audit = read_fault_audit(audit_plan, run_token)
        fault.remote_audit = _sanitized_remote_audit(audit)
        fault.audited_cycles = REQUIRED_FAULT_CYCLES
        # Every app and transport recovery checkpoint above asserted a
        # zero-entry production Outbox before the fixed remote audit ran.
        fault.permanent_accepted_entries = fault.final_outbox_entries
        outcome = "PASS"
        exit_code = 0
    except relay_gate.GateError as error:
        failure = str(error)
    finally:
        package_cleanup_failed = False
        if installed_gate is not None or installed_test is not None:
            for package_name in (
                relay_gate.TARGET_PACKAGE,
                relay_gate.TEST_PACKAGE,
            ):
                try:
                    force_stop_and_verify(adb, args.serial, package_name)
                except relay_gate.GateError:
                    package_cleanup_failed = True
        restored, restoration_failed = restore_initially_running_gate_containers(
            ssh,
            args.ssh_host,
            containers,
            initial_container_states,
            stopped_containers,
        )
        fault.restart_evidence.extend(restored)
        if restored and failure is None:
            outcome = "FAIL"
            exit_code = 1
            failure = (
                "Fault gate found and restored an unexpectedly stopped component"
            )
        if wifi_initially_enabled and (
            wifi_touched
            or not run_catching_wifi_enabled(adb, args.serial)
        ):
            try:
                relay_gate.run_bounded(
                    build_wifi_command(adb, args.serial, True),
                    timeout_seconds=30,
                    label="Device Wi-Fi recovery",
                )
                await_wifi_state(adb, args.serial, True)
                fault.wifi_restored = True
            except relay_gate.GateError:
                fault.wifi_restored = False
                if failure is None:
                    failure = "Fault gate could not restore device Wi-Fi"
                    outcome = "FAIL"
                    exit_code = 1
        if restoration_failed:
            outcome = "FAIL"
            exit_code = 1
            if failure is None:
                failure = "Fault gate could not restore a stopped component"
        if package_cleanup_failed:
            outcome = "FAIL"
            exit_code = 1
            if failure is None:
                failure = "Fault gate could not stop its isolated test packages"
    return (
        manifest_document(
            mode="faults",
            outcome=outcome,
            gate_apk=gate_apk,
            test_apk=test_apk,
            installed_gate_sha256=installed_gate,
            installed_test_sha256=installed_test,
            duration_seconds=time.monotonic() - started,
            fault=fault,
            soak=None,
            failure=failure,
        ),
        exit_code,
    )


def run_catching_wifi_enabled(adb: Path, serial: str) -> bool:
    try:
        return read_wifi_enabled(adb, serial)
    except relay_gate.GateError:
        return False


def execute_soak(
    args: argparse.Namespace,
    *,
    adb: Path,
    gate_apk: relay_gate.Artifact,
    test_apk: relay_gate.Artifact,
    audit_plan: relay_gate.AuditPlan,
) -> tuple[dict[str, object], int]:
    started = time.monotonic()
    installed_gate: str | None = None
    installed_test: str | None = None
    failure: str | None = None
    soak_evidence: dict[str, object] | None = None
    outcome = "FAIL"
    exit_code = 1
    try:
        installed_gate, installed_test = _install_gate_artifacts(
            adb,
            args.serial,
            gate_apk,
            test_apk,
        )
        instrumentation_started = time.monotonic()
        evidence = relay_gate.run_instrumentation(
            build_soak_command(adb, args.serial, args.expected_reply),
            mode="load",
            restart_plan=None,
            timeout_seconds=args.soak_timeout_seconds,
        )
        host_elapsed = time.monotonic() - instrumentation_started
        relay_gate.validate_instrumentation_evidence(
            evidence,
            "load",
            expected_threads=REQUIRED_SOAK_THREADS,
            expected_turns=REQUIRED_SOAK_TURNS,
        )
        android_elapsed = relay_gate.require_int(
            evidence.values,
            "cheby_gate_soak_elapsed_millis",
        )
        relay_gate.require_true(
            evidence.values,
            "cheby_gate_soak_monotonic_verified",
        )
        if (
            android_elapsed < REQUIRED_SOAK_MILLIS
            or host_elapsed < REQUIRED_SOAK_SECONDS
        ):
            raise relay_gate.GateError(
                "Soak gate did not span eight real monotonic hours"
            )
        run_token = relay_gate.require_pattern(
            evidence.values,
            "cheby_gate_run_token",
            relay_gate.RESTART_NONCE,
        )
        tuple_sha256 = relay_gate.require_pattern(
            evidence.values,
            "cheby_gate_tuple_sha256",
            relay_gate.SHA256_HEX,
        )
        audit = relay_gate.read_gate_audit(
            audit_plan,
            run_token,
            expected_threads=REQUIRED_SOAK_THREADS,
            expected_turns=REQUIRED_SOAK_TURNS,
            android_tuple_sha256=tuple_sha256,
        )
        soak_evidence = {
            "threads": REQUIRED_SOAK_THREADS,
            "turns": REQUIRED_SOAK_TURNS,
            "minimumDurationMillis": REQUIRED_SOAK_MILLIS,
            "androidElapsedMillis": android_elapsed,
            "hostElapsedSeconds": round(host_elapsed, 3),
            "outboxEntries": relay_gate.require_int(
                evidence.values,
                "cheby_gate_outbox_entries",
            ),
            "duplicateBubbles": relay_gate.require_int(
                evidence.values,
                "cheby_gate_duplicate_bubbles",
            ),
            "tupleSha256": tuple_sha256,
            "scopeGenerationSha256": relay_gate.require_pattern(
                evidence.values,
                "cheby_gate_scope_generation_sha256",
                relay_gate.SHA256_HEX,
            ),
            "hostDatabaseAudit": _sanitized_remote_audit(audit),
        }
        outcome = "PASS"
        exit_code = 0
    except relay_gate.GateError as error:
        failure = str(error)
    finally:
        package_cleanup_failed = False
        if installed_gate is not None or installed_test is not None:
            for package_name in (
                relay_gate.TARGET_PACKAGE,
                relay_gate.TEST_PACKAGE,
            ):
                try:
                    force_stop_and_verify(adb, args.serial, package_name)
                except relay_gate.GateError:
                    package_cleanup_failed = True
        if package_cleanup_failed:
            outcome = "FAIL"
            exit_code = 1
            if failure is None:
                failure = "Soak gate could not stop its isolated test packages"
    return (
        manifest_document(
            mode="soak",
            outcome=outcome,
            gate_apk=gate_apk,
            test_apk=test_apk,
            installed_gate_sha256=installed_gate,
            installed_test_sha256=installed_test,
            duration_seconds=time.monotonic() - started,
            fault=None,
            soak=soak_evidence,
            failure=failure,
        ),
        exit_code,
    )


def execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    (
        adb,
        ssh,
        gate_apk,
        test_apk,
        audit_plan,
        containers,
    ) = validate_args(args)
    if args.mode == "faults":
        return execute_faults(
            args,
            adb=adb,
            ssh=ssh,
            gate_apk=gate_apk,
            test_apk=test_apk,
            audit_plan=audit_plan,
            containers=containers,
        )
    return execute_soak(
        args,
        adb=adb,
        gate_apk=gate_apk,
        test_apk=test_apk,
        audit_plan=audit_plan,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        relay_gate.require_new_manifest_path(args.manifest)
        document, exit_code = execute(args)
        manifest = relay_gate.write_manifest(args.manifest, document)
    except relay_gate.ConfigError as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"{document['outcome']} manifest={manifest}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
