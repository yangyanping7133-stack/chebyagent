#!/usr/bin/env python3
"""Read-only cross-layer audit for one isolated Android Gate run.

Production invocation requires a fixed ``quick-demo``, ``load`` or ``fault`` mode and accepts
only a public 32-hex run token. Database and scripted-Codex paths are fixed so
the host runner cannot turn this utility into an arbitrary file reader. A
tightly constrained /tmp fixture root exists only for deterministic CLI tests
when CHEBY_GATE_AUDIT_TEST_MODE=1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import quote


RUN_TOKEN = re.compile(r"^[a-f0-9]{32}$")
PUBLIC_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
LOAD_MARKER = re.compile(
    r"^GATE-([a-f0-9]{32})-G([1-9][0-9]*)-T([1-9][0-9]*)-N([1-9][0-9]*)$"
)
FAULT_MARKER = re.compile(
    r"^FAULT-([a-f0-9]{32})-(APP|EDGE|RELAY|CONNECTOR|WIFI)-"
    r"(10|[1-9])-(A1|A2|A3|B1|B2)$"
)
EXACT_CRASH_MARKER = re.compile(
    r"^EXACT-([a-f0-9]{32})-(ENQUEUE|NETWORK|ACCEPTED|ANDROID|CLEANED)-"
    r"(2[0-5]|1[0-9]|[1-9])-(A1|A2|B1)$"
)
RAW_GATE_TURN = re.compile(r"^gate-turn-([0-9]{8})$")
EXPECTED_TURNS = 500
EXPECTED_THREADS = 8
QUICK_DEMO_EXPECTED_TURNS = 16
FAULT_COMPONENTS = ("APP", "EDGE", "RELAY", "CONNECTOR", "WIFI")
FAULT_LABELS = ("A1", "A2", "A3", "B1", "B2")
FAULT_REPETITIONS = 10
FAULT_EXPECTED_CYCLES = len(FAULT_COMPONENTS) * FAULT_REPETITIONS
FAULT_EXPECTED_TURNS = FAULT_EXPECTED_CYCLES * len(FAULT_LABELS)
FAULT_EXPECTED_THREADS = FAULT_EXPECTED_CYCLES * 2
EXACT_CRASH_BOUNDARIES = (
    "ENQUEUE",
    "NETWORK",
    "ACCEPTED",
    "ANDROID",
    "CLEANED",
)
EXACT_CRASH_LABELS = ("A1", "A2", "B1")
EXACT_CRASH_REPETITIONS = 25
EXACT_CRASH_EXPECTED_CYCLES = (
    len(EXACT_CRASH_BOUNDARIES) * EXACT_CRASH_REPETITIONS
)
EXACT_CRASH_EXPECTED_TURNS = (
    EXACT_CRASH_EXPECTED_CYCLES * len(EXACT_CRASH_LABELS)
)
EXACT_CRASH_EXPECTED_THREADS = EXACT_CRASH_EXPECTED_CYCLES * 2
MAX_SQLITE_BYTES = 512 * 1024 * 1024
MAX_GATE_STATE_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_NODES = 100_000
MAX_RELAY_SCAN_ROWS = 100_000
MAX_RELAY_SCAN_BYTES = 256 * 1024 * 1024
MAX_OUTBOX_ROWS = 20_000

DEFAULT_RELAY_DB = Path(
    "/var/lib/chebycodex-relay-gate/data/relay.sqlite3"
)
DEFAULT_CONNECTOR_DB = Path(
    "/var/lib/chebycodex-gate-connector/data/gate-connector.sqlite3"
)
DEFAULT_GATEWAY_DB = Path(
    "/var/lib/chebycodex-gate-connector/data/gate-gateway.sqlite3"
)
DEFAULT_FAKE_STATE = Path(
    "/var/lib/chebycodex-gate-connector/data/gate-script-state.json"
)


class AuditFailure(RuntimeError):
    """A sanitized invariant failure; never include identifiers or raw rows."""


@dataclass(frozen=True)
class AuditContract:
    name: str
    schema: str
    marker_prefix: str
    marker_pattern: re.Pattern[str]
    expected_turns: int
    expected_threads: int


LOAD_CONTRACT = AuditContract(
    name="load",
    schema="chebycodex.gate-business-key-audit.v1",
    marker_prefix="GATE",
    marker_pattern=LOAD_MARKER,
    expected_turns=EXPECTED_TURNS,
    expected_threads=EXPECTED_THREADS,
)
QUICK_DEMO_CONTRACT = AuditContract(
    name="quick-demo",
    schema="chebycodex.gate-business-key-audit.v1",
    marker_prefix="GATE",
    marker_pattern=LOAD_MARKER,
    expected_turns=QUICK_DEMO_EXPECTED_TURNS,
    expected_threads=EXPECTED_THREADS,
)
FAULT_CONTRACT = AuditContract(
    name="fault",
    schema="chebycodex.gate-fault-run-audit.v1",
    marker_prefix="FAULT",
    marker_pattern=FAULT_MARKER,
    expected_turns=FAULT_EXPECTED_TURNS,
    expected_threads=FAULT_EXPECTED_THREADS,
)
EXACT_CRASH_CONTRACT = AuditContract(
    name="exact-crash",
    schema="chebycodex.gate-exact-crash-audit.v1",
    marker_prefix="EXACT",
    marker_pattern=EXACT_CRASH_MARKER,
    expected_turns=EXACT_CRASH_EXPECTED_TURNS,
    expected_threads=EXACT_CRASH_EXPECTED_THREADS,
)


@dataclass(frozen=True, order=True)
class BusinessTuple:
    thread_id: str
    client_message_id: str
    turn_id: str

    def __post_init__(self) -> None:
        for value in (self.thread_id, self.client_message_id, self.turn_id):
            if PUBLIC_ID.fullmatch(value) is None or "\x00" in value:
                raise AuditFailure("business tuple contains an invalid public identifier")


@dataclass(frozen=True)
class AuditPaths:
    relay_db: Path
    connector_db: Path
    gateway_db: Path
    fake_state: Path
    relay_uid: int
    connector_uid: int


@dataclass(frozen=True)
class RelayCommand:
    request_id: str
    message_id: str
    assistant_id: str
    device_id: str
    delivery_seq: int
    thread_id: str
    client_message_id: str
    marker: str


@dataclass(frozen=True)
class RelayAudit:
    tuples: frozenset[BusinessTuple]
    commands: Mapping[str, RelayCommand]
    markers: Mapping[BusinessTuple, str]
    delivery_sequences: Mapping[BusinessTuple, int]
    response_message_ids: Mapping[str, str]
    relevant_message_ids: frozenset[str]
    max_node_delivery_seq: int
    assistant_id: str
    device_id: str
    node_id: str
    active_generation: int


@dataclass(frozen=True)
class GatewayAudit:
    tuples: frozenset[BusinessTuple]
    raw_turn_to_public: Mapping[str, BusinessTuple]
    raw_thread_to_public: Mapping[str, str]
    device_id: str


@dataclass(frozen=True)
class FakeAudit:
    tuples: frozenset[BusinessTuple]
    markers: Mapping[BusinessTuple, str]
    effect_sequences: Mapping[BusinessTuple, int]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditFailure("JSON contains a duplicate object key")
        result[key] = value
    return result


def strict_json(text: str, *, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as exc:
        raise AuditFailure("JSON is not valid UTF-8") from exc
    if not encoded or len(encoded) > maximum_bytes:
        raise AuditFailure("JSON size is outside the audit limit")
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise AuditFailure("JSON is malformed") from exc
    stack = [value]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AuditFailure("JSON structure exceeds the audit limit")
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise AuditFailure("JSON contains a non-finite number")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise AuditFailure("JSON contains an unsupported value")
    return value


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuditFailure(f"{label} has an invalid object shape")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuditFailure(f"{label} is missing")
    return value


def _validate_regular_private_file(
    path: Path,
    *,
    maximum_bytes: int,
    expected_uid: int | None,
    allow_empty: bool = False,
) -> os.stat_result:
    if not path.is_absolute():
        raise AuditFailure("audit path is not absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuditFailure("audit input is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
        or (metadata.st_size <= 0 and not allow_empty)
        or metadata.st_size > maximum_bytes
        or (expected_uid is not None and metadata.st_uid != expected_uid)
    ):
        raise AuditFailure("audit input file safety check failed")
    return metadata


def _validate_sqlite_family(path: Path, expected_uid: int) -> None:
    _validate_regular_private_file(
        path,
        maximum_bytes=MAX_SQLITE_BYTES,
        expected_uid=expected_uid,
    )
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _validate_regular_private_file(
                sidecar,
                maximum_bytes=MAX_SQLITE_BYTES,
                expected_uid=expected_uid,
                allow_empty=True,
            )


def _require_tables(
    connection: sqlite3.Connection,
    required: Mapping[str, frozenset[str]],
) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {str(row["name"]) for row in rows}
    if not set(required).issubset(tables):
        raise AuditFailure("SQLite schema is missing a required table")
    for table, columns in required.items():
        actual = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns.issubset(actual):
            raise AuditFailure("SQLite schema is missing a required column")


@contextmanager
def readonly_database(
    path: Path,
    *,
    expected_uid: int | None,
    required_tables: Mapping[str, frozenset[str]],
) -> Iterator[sqlite3.Connection]:
    if expected_uid is None:
        raise AuditFailure("SQLite owner policy is missing")
    _validate_sqlite_family(path, expected_uid)
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
    except sqlite3.Error as exc:
        raise AuditFailure("SQLite read-only connection failed") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise AuditFailure("SQLite quick check failed")
        _require_tables(connection, required_tables)
        yield connection
        connection.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise AuditFailure("SQLite read-only snapshot failed") from exc
    finally:
        connection.close()


def tuple_digest(values: frozenset[BusinessTuple] | set[BusinessTuple]) -> str:
    """SHA-256 of sorted UTF-8 triples, each field terminated by NUL."""

    digest = hashlib.sha256()
    for value in sorted(values):
        for field in (
            value.thread_id,
            value.client_message_id,
            value.turn_id,
        ):
            digest.update(field.encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


def _marker_from_input(
    params: dict[str, Any],
    run_token: str,
    contract: AuditContract = LOAD_CONTRACT,
) -> str | None:
    inputs = params.get("input")
    if not isinstance(inputs, list):
        return None
    texts = [
        part.get("text")
        for part in inputs
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    if len(texts) != 1:
        return None
    match = contract.marker_pattern.fullmatch(texts[0])
    if match is None or match.group(1) != run_token:
        return None
    return texts[0]


def _scan_relay_node_deliveries(
    relay: sqlite3.Connection,
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    total_bytes = 0
    cursor = relay.execute(
        """
        SELECT assistant_id, recipient_role, delivery_seq, sender_role,
               sender_principal_id, message_id, payload_json, expired_at
        FROM deliveries
        WHERE sender_role='node' AND recipient_role='device'
        ORDER BY assistant_id, delivery_seq
        """
    )
    while True:
        batch = cursor.fetchmany(512)
        if not batch:
            break
        rows.extend(batch)
        if len(rows) > MAX_RELAY_SCAN_ROWS:
            raise AuditFailure("Relay delivery scan exceeds the row limit")
        total_bytes += sum(len(str(row["payload_json"]).encode("utf-8")) for row in batch)
        if total_bytes > MAX_RELAY_SCAN_BYTES:
            raise AuditFailure("Relay delivery scan exceeds the byte limit")
    return rows


def audit_relay(
    relay: sqlite3.Connection,
    run_token: str,
    contract: AuditContract = LOAD_CONTRACT,
) -> RelayAudit:
    marker_fragment = f"{contract.marker_prefix}-{run_token}-"
    command_rows = relay.execute(
        """
        SELECT assistant_id, recipient_role, delivery_seq, sender_role,
               sender_principal_id, message_id, payload_json, expired_at
        FROM deliveries
        WHERE sender_role='device' AND recipient_role='node'
          AND instr(payload_json, ?) > 0
        ORDER BY assistant_id, delivery_seq
        """,
        (marker_fragment,),
    ).fetchall()
    commands: dict[str, RelayCommand] = {}
    for row in command_rows:
        payload = _require_dict(
            strict_json(str(row["payload_json"])),
            "Relay command payload",
        )
        params = payload.get("params")
        marker = (
            _marker_from_input(params, run_token, contract)
            if isinstance(params, dict)
            else None
        )
        if (
            payload.get("kind") != "command"
            or payload.get("operation") != "turns.start"
            or not isinstance(params, dict)
            or marker is None
            or row["sender_principal_id"] != payload.get("deviceId")
        ):
            raise AuditFailure("Relay contains a malformed run command")
        request_id = _require_string(payload.get("requestId"), "Relay request id")
        if request_id in commands or row["expired_at"] is not None:
            raise AuditFailure("Relay contains a duplicate or expired Gate command")
        command = RelayCommand(
            request_id=request_id,
            message_id=_require_string(row["message_id"], "Relay message id"),
            assistant_id=_require_string(row["assistant_id"], "Relay assistant id"),
            device_id=_require_string(payload.get("deviceId"), "Relay device id"),
            delivery_seq=int(row["delivery_seq"]),
            thread_id=_require_string(params.get("threadId"), "Relay thread id"),
            client_message_id=_require_string(
                params.get("clientMessageId"),
                "Relay client message id",
            ),
            marker=marker,
        )
        commands[request_id] = command
    if len(commands) != contract.expected_turns:
        raise AuditFailure("Relay run command count is not exact")
    if (
        len({command.thread_id for command in commands.values()})
        != contract.expected_threads
    ):
        raise AuditFailure("Relay run Thread count is not exact")
    if (
        len({command.client_message_id for command in commands.values()})
        != contract.expected_turns
    ):
        raise AuditFailure("Relay Gate client message ids are not unique")
    assistant_ids = {command.assistant_id for command in commands.values()}
    device_ids = {command.device_id for command in commands.values()}
    if len(assistant_ids) != 1 or len(device_ids) != 1:
        raise AuditFailure("Relay Gate commands span multiple authenticated bindings")
    assistant_id = next(iter(assistant_ids))
    device_id = next(iter(device_ids))

    request_rows = relay.execute(
        """
        SELECT assistant_id, request_id, device_id, operation, message_id
        FROM requests WHERE operation='turns.start'
        """
    ).fetchall()
    requests = {
        str(row["request_id"]): row
        for row in request_rows
        if str(row["request_id"]) in commands
    }
    if set(requests) != set(commands):
        raise AuditFailure("Relay request ledger is incomplete")
    for request_id, command in commands.items():
        row = requests[request_id]
        if (
            row["assistant_id"] != command.assistant_id
            or row["device_id"] != command.device_id
            or row["message_id"] != command.message_id
        ):
            raise AuditFailure("Relay request ledger correlation failed")

    node_rows = _scan_relay_node_deliveries(relay)
    response_by_request: dict[str, tuple[sqlite3.Row, dict[str, Any]]] = {}
    for row in node_rows:
        payload = _require_dict(
            strict_json(str(row["payload_json"])),
            "Relay node payload",
        )
        request_id = payload.get("requestId")
        if payload.get("kind") == "response" and request_id in commands:
            if request_id in response_by_request:
                raise AuditFailure("Relay contains duplicate Gate responses")
            response_by_request[str(request_id)] = (row, payload)
    if set(response_by_request) != set(commands):
        raise AuditFailure("Relay response set is incomplete")

    tuples: set[BusinessTuple] = set()
    response_message_ids: dict[str, str] = {}
    relevant_message_ids = {command.message_id for command in commands.values()}
    expected_idempotency = {
        command.message_id: (
            command.assistant_id,
            "device",
            "node",
            command.delivery_seq,
        )
        for command in commands.values()
    }
    max_node_delivery_seq = 0
    response_tuple_by_turn: dict[tuple[str, str], BusinessTuple] = {}
    markers: dict[BusinessTuple, str] = {}
    delivery_sequences: dict[BusinessTuple, int] = {}
    for request_id, command in commands.items():
        row, payload = response_by_request[request_id]
        result = payload.get("result")
        if (
            row["expired_at"] is not None
            or payload.get("operation") != "turns.start"
            or payload.get("deviceId") != command.device_id
            or payload.get("ok") is not True
            or not isinstance(result, dict)
            or result.get("threadId") != command.thread_id
            or result.get("clientMessageId") != command.client_message_id
            or result.get("status") not in {"inProgress", "running", "completed"}
        ):
            raise AuditFailure("Relay successful response correlation failed")
        value = BusinessTuple(
            command.thread_id,
            command.client_message_id,
            _require_string(result.get("id"), "Relay public turn id"),
        )
        if value in tuples:
            raise AuditFailure("Relay tuple set contains a duplicate")
        tuples.add(value)
        markers[value] = command.marker
        delivery_sequences[value] = command.delivery_seq
        response_tuple_by_turn[(value.thread_id, value.turn_id)] = value
        response_message_ids[request_id] = _require_string(
            row["message_id"],
            "Relay response message id",
        )
        relevant_message_ids.add(response_message_ids[request_id])
        max_node_delivery_seq = max(max_node_delivery_seq, int(row["delivery_seq"]))

    actual_terminals: dict[BusinessTuple, sqlite3.Row] = {}
    for row in node_rows:
        payload = _require_dict(
            strict_json(str(row["payload_json"])),
            "Relay node payload",
        )
        if payload.get("kind") != "event" or payload.get("eventType") != "turn.completed":
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        key = (payload.get("threadId"), data.get("turnId"))
        value = response_tuple_by_turn.get(key)
        if value is None:
            continue
        if value in actual_terminals or row["expired_at"] is not None:
            raise AuditFailure("Relay terminal event is duplicated or expired")
        actual_terminals[value] = row
        relevant_message_ids.add(
            _require_string(row["message_id"], "Relay terminal message id")
        )
        max_node_delivery_seq = max(max_node_delivery_seq, int(row["delivery_seq"]))
    if set(actual_terminals) != tuples:
        raise AuditFailure("Relay terminal event set is incomplete")

    relevant_turns = {(value.thread_id, value.turn_id) for value in tuples}
    related_node_principals: set[str] = set()
    for row in node_rows:
        payload = _require_dict(
            strict_json(str(row["payload_json"])),
            "Relay node payload",
        )
        data = payload.get("data")
        relates_to_run = payload.get("requestId") in commands or (
            isinstance(data, dict)
            and (payload.get("threadId"), data.get("turnId")) in relevant_turns
        )
        if not relates_to_run:
            continue
        if (
            row["expired_at"] is not None
            or row["assistant_id"] != assistant_id
        ):
            raise AuditFailure("Relay retains an expired run delivery")
        message_id = _require_string(
            row["message_id"],
            "Relay relevant message id",
        )
        relevant_message_ids.add(message_id)
        idempotency_identity = (
            str(row["assistant_id"]),
            "node",
            "device",
            int(row["delivery_seq"]),
        )
        existing_identity = expected_idempotency.get(message_id)
        if (
            existing_identity is not None
            and existing_identity != idempotency_identity
        ):
            raise AuditFailure("Relay reuses a message id across deliveries")
        expected_idempotency[message_id] = idempotency_identity
        related_node_principals.add(
            _require_string(
                row["sender_principal_id"],
                "Relay node principal id",
            )
        )
        max_node_delivery_seq = max(max_node_delivery_seq, int(row["delivery_seq"]))

    idempotency_rows = _rows_for_values(
        relay,
        """
        SELECT assistant_id, sender_role, message_id, recipient_role,
               delivery_seq, state
        FROM message_idempotency WHERE message_id IN
        """,
        sorted(relevant_message_ids),
    )
    actual_idempotency = {
        str(row["message_id"]): (
            str(row["assistant_id"]),
            str(row["sender_role"]),
            str(row["recipient_role"]),
            int(row["delivery_seq"]),
        )
        for row in idempotency_rows
    }
    if (
        len(idempotency_rows) != len(relevant_message_ids)
        or len(actual_idempotency) != len(idempotency_rows)
        or actual_idempotency != expected_idempotency
    ):
        raise AuditFailure("Relay idempotency ledger is incomplete")
    if any(str(row["state"]) != "acked" for row in idempotency_rows):
        raise AuditFailure("Relay relevant delivery is not durably acknowledged")

    binding = relay.execute(
        """
        SELECT n.id AS node_id, n.active_generation, d.id AS device_id
        FROM nodes n JOIN devices d ON d.assistant_id=n.assistant_id
        WHERE n.assistant_id=? AND d.revoked_at IS NULL
        """,
        (assistant_id,),
    ).fetchall()
    if (
        len(binding) != 1
        or str(binding[0]["device_id"]) != device_id
        or int(binding[0]["active_generation"]) <= 0
        or related_node_principals != {str(binding[0]["node_id"])}
    ):
        raise AuditFailure("Relay authenticated binding/generation mapping is not unique")
    stream = relay.execute(
        """
        SELECT ack_cursor FROM streams
        WHERE assistant_id=? AND recipient_role='device'
        """,
        (assistant_id,),
    ).fetchone()
    if stream is None or int(stream["ack_cursor"]) < max_node_delivery_seq:
        raise AuditFailure("Relay device acknowledgement cursor is incomplete")
    node_stream = relay.execute(
        """
        SELECT ack_cursor FROM streams
        WHERE assistant_id=? AND recipient_role='node'
        """,
        (assistant_id,),
    ).fetchone()
    if node_stream is None or int(node_stream["ack_cursor"]) < max(
        command.delivery_seq for command in commands.values()
    ):
        raise AuditFailure("Relay node acknowledgement cursor is incomplete")
    return RelayAudit(
        tuples=frozenset(tuples),
        commands=commands,
        markers=markers,
        delivery_sequences=delivery_sequences,
        response_message_ids=response_message_ids,
        relevant_message_ids=frozenset(relevant_message_ids),
        max_node_delivery_seq=max_node_delivery_seq,
        assistant_id=assistant_id,
        device_id=device_id,
        node_id=str(binding[0]["node_id"]),
        active_generation=int(binding[0]["active_generation"]),
    )


def _rows_for_values(
    connection: sqlite3.Connection,
    sql_prefix: str,
    values: Sequence[str],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    unique_values = list(dict.fromkeys(values))
    for offset in range(0, len(unique_values), 400):
        chunk = unique_values[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(connection.execute(sql_prefix + f" ({placeholders})", chunk).fetchall())
    return rows


def audit_connector(
    connector: sqlite3.Connection,
    relay_audit: RelayAudit,
    contract: AuditContract = LOAD_CONTRACT,
) -> frozenset[BusinessTuple]:
    request_ids = sorted(relay_audit.commands)
    inbound_rows = _rows_for_values(
        connector,
        "SELECT message_id, request_id FROM inbound_messages WHERE request_id IN",
        request_ids,
    )
    if len(inbound_rows) != contract.expected_turns:
        raise AuditFailure("Connector inbound ledger count is incomplete")
    inbound = {str(row["request_id"]): str(row["message_id"]) for row in inbound_rows}
    if set(inbound) != set(request_ids):
        raise AuditFailure("Connector inbound request set is incomplete")
    for request_id, message_id in inbound.items():
        if relay_audit.commands[request_id].message_id != message_id:
            raise AuditFailure("Connector inbound message correlation failed")

    request_rows = _rows_for_values(
        connector,
        """
        SELECT request_id, operation, response_frame, response_message_id
        FROM requests WHERE request_id IN
        """,
        request_ids,
    )
    if len(request_rows) != contract.expected_turns:
        raise AuditFailure("Connector response ledger count is incomplete")
    tuples: set[BusinessTuple] = set()
    for row in request_rows:
        request_id = str(row["request_id"])
        command = relay_audit.commands.get(request_id)
        if command is None or row["operation"] != "turns.start":
            raise AuditFailure("Connector response operation correlation failed")
        frame = _require_dict(
            strict_json(str(row["response_frame"])),
            "Connector response frame",
        )
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise AuditFailure("Connector response payload is missing")
        result = payload.get("result")
        if (
            frame.get("v") != 1
            or frame.get("type") != "message"
            or frame.get("messageId") != row["response_message_id"]
            or row["response_message_id"] != relay_audit.response_message_ids[request_id]
            or payload.get("kind") != "response"
            or payload.get("requestId") != request_id
            or payload.get("deviceId") != command.device_id
            or payload.get("operation") != "turns.start"
            or payload.get("ok") is not True
            or not isinstance(result, dict)
            or result.get("threadId") != command.thread_id
            or result.get("clientMessageId") != command.client_message_id
        ):
            raise AuditFailure("Connector response frame correlation failed")
        tuples.add(
            BusinessTuple(
                command.thread_id,
                command.client_message_id,
                _require_string(result.get("id"), "Connector public turn id"),
            )
        )
    if len(tuples) != contract.expected_turns:
        raise AuditFailure("Connector tuple set contains a duplicate")

    intent_rows = _rows_for_values(
        connector,
        "SELECT request_id FROM request_intents WHERE request_id IN",
        request_ids,
    )
    if intent_rows:
        raise AuditFailure("Connector retains a relevant execution intent")
    processed_rows = _rows_for_values(
        connector,
        """
        SELECT delivery_seq, request_id FROM processed_deliveries
        WHERE request_id IN
        """,
        request_ids,
    )
    # ConnectorState._advance_cursor removes every contiguous processed row at
    # or below the durable ack_cursor. After a converged run, retaining one of
    # these rows is an orphan; the cursor below is the durable completion proof.
    if processed_rows:
        raise AuditFailure(
            "Connector retains a relevant processed-delivery row"
        )

    outbox_rows = connector.execute(
        "SELECT message_id, frame_json FROM outbox ORDER BY local_seq LIMIT ?",
        (MAX_OUTBOX_ROWS + 1,),
    ).fetchall()
    if len(outbox_rows) > MAX_OUTBOX_ROWS:
        raise AuditFailure("Connector outbox exceeds the audit row limit")
    relevant_turns = {value.turn_id for value in tuples}
    for row in outbox_rows:
        frame = _require_dict(
            strict_json(str(row["frame_json"])),
            "Connector outbox frame",
        )
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise AuditFailure("Connector outbox payload is malformed")
        data = payload.get("data")
        if (
            payload.get("requestId") in relay_audit.commands
            or (
                isinstance(data, dict)
                and data.get("turnId") in relevant_turns
            )
        ):
            raise AuditFailure("Connector retains a relevant outbox frame")
    bound = connector.execute(
        "SELECT value FROM meta WHERE key='bound_device_id'"
    ).fetchall()
    cursor = connector.execute(
        "SELECT value FROM meta WHERE key='ack_cursor'"
    ).fetchone()
    if (
        len(bound) != 1
        or str(bound[0]["value"]) != relay_audit.device_id
        or cursor is None
        or int(cursor["value"]) < max(
            command.delivery_seq for command in relay_audit.commands.values()
        )
    ):
        raise AuditFailure("Connector authenticated binding/cursor mapping failed")
    return frozenset(tuples)


def audit_gateway(
    gateway: sqlite3.Connection,
    relay_audit: RelayAudit,
    contract: AuditContract = LOAD_CONTRACT,
) -> GatewayAudit:
    client_ids = sorted(
        command.client_message_id for command in relay_audit.commands.values()
    )
    rows = _rows_for_values(
        gateway,
        """
        SELECT public_id, raw_id, device_id, thread_public_id,
               client_message_id, status, delivery_state, attempt_count
        FROM turns WHERE client_message_id IN
        """,
        client_ids,
    )
    if len(rows) != contract.expected_turns:
        raise AuditFailure("Gateway Turn ledger count is incomplete")
    tuples: set[BusinessTuple] = set()
    raw_turn_to_public: dict[str, BusinessTuple] = {}
    device_ids: set[str] = set()
    for row in rows:
        if (
            row["status"] != "completed"
            or row["delivery_state"] != "terminal"
            or row["raw_id"] is None
            or int(row["attempt_count"]) != 1
        ):
            raise AuditFailure("Gateway Turn is not an exact terminal delivery")
        value = BusinessTuple(
            str(row["thread_public_id"]),
            str(row["client_message_id"]),
            str(row["public_id"]),
        )
        tuples.add(value)
        raw_id = str(row["raw_id"])
        if raw_id in raw_turn_to_public:
            raise AuditFailure("Gateway raw Turn mapping is duplicated")
        raw_turn_to_public[raw_id] = value
        device_ids.add(str(row["device_id"]))
    if (
        len(tuples) != contract.expected_turns
        or device_ids != {relay_audit.device_id}
    ):
        raise AuditFailure("Gateway tuple/device set is not unique")

    mappings = _rows_for_values(
        gateway,
        """
        SELECT kind, raw_id, public_id, thread_public_id
        FROM id_mappings WHERE raw_id IN
        """,
        sorted(raw_turn_to_public),
    )
    turn_mappings = [row for row in mappings if row["kind"] == "turn"]
    # Reserved Turns are canonically bound in the turns ledger itself. The
    # generic id_mappings table is populated only for identities learned from
    # history, so relevant rows there are optional rather than a second copy
    # of the authoritative mapping. Any optional row must still agree exactly.
    for row in turn_mappings:
        value = raw_turn_to_public.get(str(row["raw_id"]))
        if (
            value is None
            or row["public_id"] != value.turn_id
            or row["thread_public_id"] != value.thread_id
        ):
            raise AuditFailure("Gateway public Turn id mapping is inconsistent")

    message_rows = _rows_for_values(
        gateway,
        """
        SELECT message_id, thread_public_id, turn_public_id, role,
               client_message_id, payload_json
        FROM message_snapshots WHERE turn_public_id IN
        """,
        sorted(value.turn_id for value in tuples),
    )
    by_turn: dict[str, list[sqlite3.Row]] = {}
    for row in message_rows:
        by_turn.setdefault(str(row["turn_public_id"]), []).append(row)
    for value in tuples:
        messages = by_turn.get(value.turn_id, [])
        users = [row for row in messages if row["role"] == "user"]
        assistants = [row for row in messages if row["role"] == "assistant"]
        if len(users) != 1 or len(assistants) != 1:
            raise AuditFailure("Gateway canonical message snapshot count is not exact")
        for row in (users[0], assistants[0]):
            payload = _require_dict(
                strict_json(str(row["payload_json"])),
                "Gateway message snapshot",
            )
            if (
                row["thread_public_id"] != value.thread_id
                or payload.get("threadId") != value.thread_id
                or payload.get("turnId") != value.turn_id
                or payload.get("state") != "completed"
            ):
                raise AuditFailure("Gateway canonical message snapshot is inconsistent")
        user_payload = _require_dict(
            strict_json(str(users[0]["payload_json"])),
            "Gateway canonical user message snapshot",
        )
        if (
            users[0]["client_message_id"] != value.client_message_id
            or user_payload.get("clientMessageId") != value.client_message_id
        ):
            raise AuditFailure("Gateway canonical user message lost its client id")

    thread_rows = _rows_for_values(
        gateway,
        "SELECT public_id, raw_id FROM threads WHERE public_id IN",
        sorted(value.thread_id for value in tuples),
    )
    if len(thread_rows) != contract.expected_threads:
        raise AuditFailure("Gateway public Thread mapping count is not exact")
    raw_thread_to_public = {
        str(row["raw_id"]): str(row["public_id"])
        for row in thread_rows
        if row["raw_id"] is not None
    }
    if len(raw_thread_to_public) != contract.expected_threads:
        raise AuditFailure("Gateway raw Thread mappings are incomplete")
    return GatewayAudit(
        tuples=frozenset(tuples),
        raw_turn_to_public=raw_turn_to_public,
        raw_thread_to_public=raw_thread_to_public,
        device_id=next(iter(device_ids)),
    )


def _read_private_json(
    path: Path,
    *,
    expected_uid: int | None,
) -> dict[str, Any]:
    _validate_regular_private_file(
        path,
        maximum_bytes=MAX_GATE_STATE_BYTES,
        expected_uid=expected_uid,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditFailure("Gate scripted state cannot be opened") from exc
    try:
        chunks = bytearray()
        while len(chunks) <= MAX_GATE_STATE_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_GATE_STATE_BYTES + 1 - len(chunks)),
            )
            if not chunk:
                break
            chunks.extend(chunk)
    finally:
        os.close(descriptor)
    if len(chunks) > MAX_GATE_STATE_BYTES:
        raise AuditFailure("Gate scripted state exceeds the audit limit")
    try:
        text = bytes(chunks).decode("utf-8")
    except UnicodeError as exc:
        raise AuditFailure("Gate scripted state is not valid UTF-8") from exc
    return _require_dict(
        strict_json(text, maximum_bytes=MAX_GATE_STATE_BYTES),
        "Gate scripted state",
    )


def audit_fake_state(
    state: dict[str, Any],
    run_token: str,
    gateway_audit: GatewayAudit,
    contract: AuditContract = LOAD_CONTRACT,
) -> FakeAudit:
    if (
        set(state) != {"v", "threadCounter", "turnCounter", "threads", "turns"}
        or state.get("v") != 1
        or type(state.get("threadCounter")) is not int
        or type(state.get("turnCounter")) is not int
        or not isinstance(state.get("threads"), dict)
        or not isinstance(state.get("turns"), dict)
    ):
        raise AuditFailure("Gate scripted state has an invalid root schema")
    threads = state["threads"]
    turns = state["turns"]
    tuples: set[BusinessTuple] = set()
    matching_raw_turns: set[str] = set()
    matching_raw_threads: set[str] = set()
    markers: dict[BusinessTuple, str] = {}
    effect_sequences: dict[BusinessTuple, int] = {}
    for raw_turn_id, raw_value in turns.items():
        if not isinstance(raw_turn_id, str) or not isinstance(raw_value, dict):
            raise AuditFailure("Gate scripted Turn state is malformed")
        parts = raw_value.get("input")
        params = {"input": parts}
        marker = _marker_from_input(params, run_token, contract)
        if marker is None:
            continue
        if (
            raw_value.get("id") != raw_turn_id
            or raw_value.get("status") != "completed"
            or not isinstance(raw_value.get("threadId"), str)
            or not isinstance(raw_value.get("clientMessageId"), str)
            or not isinstance(raw_value.get("items"), list)
        ):
            raise AuditFailure("Gate scripted Turn is not a completed canonical Turn")
        raw_turn_match = RAW_GATE_TURN.fullmatch(raw_turn_id)
        if raw_turn_match is None or int(raw_turn_match.group(1)) <= 0:
            raise AuditFailure("Gate scripted Turn effect sequence is invalid")
        effect_sequence = int(raw_turn_match.group(1))
        gateway_value = gateway_audit.raw_turn_to_public.get(raw_turn_id)
        public_thread = gateway_audit.raw_thread_to_public.get(raw_value["threadId"])
        if (
            gateway_value is None
            or public_thread is None
            or gateway_value.thread_id != public_thread
            or gateway_value.client_message_id != raw_value["clientMessageId"]
        ):
            raise AuditFailure("Gate scripted Turn public mapping is inconsistent")
        items = raw_value["items"]
        users = [
            item
            for item in items
            if isinstance(item, dict) and item.get("type") == "userMessage"
        ]
        assistants = [
            item
            for item in items
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        if len(users) != 1 or len(assistants) != 1:
            raise AuditFailure("Gate scripted Turn item count is not exact")
        if (
            users[0].get("clientId") != raw_value["clientMessageId"]
            or users[0].get("id") != f"gate-user-{effect_sequence:08d}"
            or assistants[0].get("id") != f"gate-agent-{effect_sequence:08d}"
        ):
            raise AuditFailure("Gate scripted effect identity is inconsistent")
        if gateway_value in tuples:
            raise AuditFailure("Gate scripted business key has duplicate effects")
        tuples.add(gateway_value)
        markers[gateway_value] = marker
        effect_sequences[gateway_value] = effect_sequence
        matching_raw_turns.add(raw_turn_id)
        matching_raw_threads.add(raw_value["threadId"])
    if (
        len(tuples) != contract.expected_turns
        or len(matching_raw_turns) != contract.expected_turns
        or len(matching_raw_threads) != contract.expected_threads
    ):
        raise AuditFailure("Gate scripted Turn/Thread count is incomplete")
    for raw_thread_id in matching_raw_threads:
        thread = threads.get(raw_thread_id)
        if not isinstance(thread, dict) or thread.get("id") != raw_thread_id:
            raise AuditFailure("Gate scripted Thread state is missing")
        embedded = thread.get("turns")
        if not isinstance(embedded, list):
            raise AuditFailure("Gate scripted Thread Turn list is malformed")
        embedded_ids = [
            item.get("id")
            for item in embedded
            if isinstance(item, dict) and item.get("id") in matching_raw_turns
        ]
        expected_ids = {
            raw_turn_id
            for raw_turn_id in matching_raw_turns
            if turns[raw_turn_id].get("threadId") == raw_thread_id
        }
        if len(embedded_ids) != len(set(embedded_ids)) or set(embedded_ids) != expected_ids:
            raise AuditFailure("Gate scripted Thread contains missing or duplicate Turns")
        matching_embedded_ids = [
            raw_id
            for raw_id in embedded_ids
            if raw_id in matching_raw_turns
        ]
        embedded_sequences = [
            int(require_match.group(1))
            for raw_id in matching_embedded_ids
            if (require_match := RAW_GATE_TURN.fullmatch(str(raw_id))) is not None
        ]
        if (
            len(embedded_sequences) != len(matching_embedded_ids)
            or embedded_sequences != sorted(embedded_sequences)
        ):
            raise AuditFailure("Gate scripted Thread effect order is inconsistent")
    if len(set(effect_sequences.values())) != contract.expected_turns:
        raise AuditFailure("Gate scripted effect sequence is duplicated")
    if effect_sequences and state["turnCounter"] < max(effect_sequences.values()):
        raise AuditFailure("Gate scripted effect counter is behind persisted effects")
    return FakeAudit(
        tuples=frozenset(tuples),
        markers=markers,
        effect_sequences=effect_sequences,
    )


RELAY_TABLES = {
    "nodes": frozenset({"id", "assistant_id", "active_generation"}),
    "devices": frozenset({"id", "assistant_id", "revoked_at"}),
    "streams": frozenset({"assistant_id", "recipient_role", "ack_cursor"}),
    "requests": frozenset(
        {"assistant_id", "request_id", "device_id", "operation", "message_id"}
    ),
    "deliveries": frozenset(
        {
            "assistant_id",
            "recipient_role",
            "delivery_seq",
            "sender_role",
            "sender_principal_id",
            "message_id",
            "payload_json",
            "expired_at",
        }
    ),
    "message_idempotency": frozenset(
        {
            "assistant_id",
            "sender_role",
            "message_id",
            "recipient_role",
            "delivery_seq",
            "state",
        }
    ),
}
CONNECTOR_TABLES = {
    "meta": frozenset({"key", "value"}),
    "inbound_messages": frozenset({"message_id", "request_id"}),
    "requests": frozenset(
        {"request_id", "operation", "response_frame", "response_message_id"}
    ),
    "request_intents": frozenset({"request_id", "message_id", "state"}),
    "processed_deliveries": frozenset({"delivery_seq", "request_id"}),
    "outbox": frozenset({"local_seq", "message_id", "frame_json"}),
}
GATEWAY_TABLES = {
    "threads": frozenset({"public_id", "raw_id"}),
    "turns": frozenset(
        {
            "public_id",
            "raw_id",
            "device_id",
            "thread_public_id",
            "client_message_id",
            "status",
            "delivery_state",
            "attempt_count",
        }
    ),
    "id_mappings": frozenset(
        {"kind", "raw_id", "public_id", "thread_public_id"}
    ),
    "message_snapshots": frozenset(
        {
            "message_id",
            "thread_public_id",
            "turn_public_id",
            "role",
            "client_message_id",
            "payload_json",
        }
    ),
}


def binding_mapping_digest(relay_audit: RelayAudit) -> str:
    digest = hashlib.sha256()
    for value in (
        relay_audit.assistant_id,
        relay_audit.device_id,
        relay_audit.node_id,
        str(relay_audit.active_generation),
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def audit_fault_matrix(
    relay_audit: RelayAudit,
    fake_audit: FakeAudit,
    run_token: str,
) -> dict[str, int]:
    """Validate every fixed fault cycle without emitting raw business keys."""

    expected_markers = {
        f"FAULT-{run_token}-{component}-{iteration}-{label}"
        for component in FAULT_COMPONENTS
        for iteration in range(1, FAULT_REPETITIONS + 1)
        for label in FAULT_LABELS
    }
    if (
        len(relay_audit.markers) != FAULT_EXPECTED_TURNS
        or len(fake_audit.markers) != FAULT_EXPECTED_TURNS
        or set(relay_audit.markers.values()) != expected_markers
        or set(fake_audit.markers.values()) != expected_markers
        or relay_audit.markers != fake_audit.markers
    ):
        raise AuditFailure("fault marker matrix is incomplete or inconsistent")

    by_cycle: dict[tuple[str, int], dict[str, BusinessTuple]] = {}
    for business_tuple, marker in fake_audit.markers.items():
        match = FAULT_MARKER.fullmatch(marker)
        if match is None or match.group(1) != run_token:
            raise AuditFailure("fault marker matrix contains an invalid marker")
        component = match.group(2)
        iteration = int(match.group(3))
        label = match.group(4)
        labels = by_cycle.setdefault((component, iteration), {})
        if label in labels:
            raise AuditFailure("fault cycle contains a duplicate business effect")
        labels[label] = business_tuple

    expected_cycles = {
        (component, iteration)
        for component in FAULT_COMPONENTS
        for iteration in range(1, FAULT_REPETITIONS + 1)
    }
    if set(by_cycle) != expected_cycles:
        raise AuditFailure("fault cycle set is incomplete")

    cycle_threads: set[str] = set()
    for labels in by_cycle.values():
        if set(labels) != set(FAULT_LABELS):
            raise AuditFailure("fault cycle label set is incomplete")
        thread_a = {labels[label].thread_id for label in ("A1", "A2", "A3")}
        thread_b = {labels[label].thread_id for label in ("B1", "B2")}
        if len(thread_a) != 1 or len(thread_b) != 1 or thread_a == thread_b:
            raise AuditFailure("fault cycle Thread isolation is invalid")
        cycle_threads.update(thread_a)
        cycle_threads.update(thread_b)
        sequence_a = [
            relay_audit.delivery_sequences[labels[label]]
            for label in ("A1", "A2", "A3")
        ]
        sequence_b = [
            relay_audit.delivery_sequences[labels[label]]
            for label in ("B1", "B2")
        ]
        if sequence_a != sorted(sequence_a) or len(set(sequence_a)) != 3:
            raise AuditFailure("fault cycle same-Thread FIFO is invalid")
        if sequence_b != sorted(sequence_b) or len(set(sequence_b)) != 2:
            raise AuditFailure("fault cycle same-Thread FIFO is invalid")
        effect_sequence_a = [
            fake_audit.effect_sequences[labels[label]]
            for label in ("A1", "A2", "A3")
        ]
        effect_sequence_b = [
            fake_audit.effect_sequences[labels[label]]
            for label in ("B1", "B2")
        ]
        if (
            effect_sequence_a != sorted(effect_sequence_a)
            or len(set(effect_sequence_a)) != 3
            or effect_sequence_b != sorted(effect_sequence_b)
            or len(set(effect_sequence_b)) != 2
        ):
            raise AuditFailure("fault cycle fake-Codex effect FIFO is invalid")
    if len(cycle_threads) != FAULT_EXPECTED_THREADS:
        raise AuditFailure("fault cycles reused a Thread across fault boundaries")

    return {
        "cycles": FAULT_EXPECTED_CYCLES,
        "businessKeys": FAULT_EXPECTED_TURNS,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }


def audit_exact_crash_matrix(
    relay_audit: RelayAudit,
    fake_audit: FakeAudit,
    run_token: str,
) -> dict[str, int]:
    """Validate the fixed five-boundary x 25 Android process-death matrix."""

    expected_markers = {
        f"EXACT-{run_token}-{boundary}-{iteration}-{label}"
        for boundary in EXACT_CRASH_BOUNDARIES
        for iteration in range(1, EXACT_CRASH_REPETITIONS + 1)
        for label in EXACT_CRASH_LABELS
    }
    if (
        len(relay_audit.markers) != EXACT_CRASH_EXPECTED_TURNS
        or len(fake_audit.markers) != EXACT_CRASH_EXPECTED_TURNS
        or set(relay_audit.markers.values()) != expected_markers
        or set(fake_audit.markers.values()) != expected_markers
        or relay_audit.markers != fake_audit.markers
    ):
        raise AuditFailure("exact-crash marker matrix is incomplete or inconsistent")

    by_cycle: dict[tuple[str, int], dict[str, BusinessTuple]] = {}
    for business_tuple, marker in fake_audit.markers.items():
        match = EXACT_CRASH_MARKER.fullmatch(marker)
        if match is None or match.group(1) != run_token:
            raise AuditFailure("exact-crash marker matrix contains an invalid marker")
        boundary = match.group(2)
        iteration = int(match.group(3))
        label = match.group(4)
        labels = by_cycle.setdefault((boundary, iteration), {})
        if label in labels:
            raise AuditFailure("exact-crash cycle contains a duplicate business effect")
        labels[label] = business_tuple

    expected_cycles = {
        (boundary, iteration)
        for boundary in EXACT_CRASH_BOUNDARIES
        for iteration in range(1, EXACT_CRASH_REPETITIONS + 1)
    }
    if set(by_cycle) != expected_cycles:
        raise AuditFailure("exact-crash cycle set is incomplete")

    cycle_threads: set[str] = set()
    for labels in by_cycle.values():
        if set(labels) != set(EXACT_CRASH_LABELS):
            raise AuditFailure("exact-crash cycle label set is incomplete")
        a1, a2, b1 = (labels[label] for label in EXACT_CRASH_LABELS)
        if (
            a1.thread_id != a2.thread_id
            or b1.thread_id == a1.thread_id
        ):
            raise AuditFailure("exact-crash Thread isolation is invalid")
        cycle_threads.update((a1.thread_id, b1.thread_id))
        if not (
            relay_audit.delivery_sequences[a1]
            < relay_audit.delivery_sequences[a2]
        ):
            raise AuditFailure(
                "exact-crash recovery violated same-Thread FIFO"
            )
        if not (
            fake_audit.effect_sequences[a1]
            < fake_audit.effect_sequences[a2]
        ):
            raise AuditFailure(
                "exact-crash fake-Codex effects violated same-Thread FIFO"
            )
    if len(cycle_threads) != EXACT_CRASH_EXPECTED_THREADS:
        raise AuditFailure("exact-crash cycles reused a Thread")

    return {
        "cycles": EXACT_CRASH_EXPECTED_CYCLES,
        "businessKeys": EXACT_CRASH_EXPECTED_TURNS,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }


@contextmanager
def private_process_mask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def perform_audit(
    paths: AuditPaths,
    run_token: str,
    contract: AuditContract = LOAD_CONTRACT,
) -> dict[str, Any]:
    if RUN_TOKEN.fullmatch(run_token) is None:
        raise AuditFailure("run token must be exactly 32 lowercase hex characters")
    if contract not in {
        LOAD_CONTRACT,
        QUICK_DEMO_CONTRACT,
        FAULT_CONTRACT,
        EXACT_CRASH_CONTRACT,
    }:
        raise AuditFailure("audit contract is not one of the fixed Gate modes")
    with private_process_mask(), ExitStack() as stack:
        relay = stack.enter_context(
            readonly_database(
                paths.relay_db,
                expected_uid=paths.relay_uid,
                required_tables=RELAY_TABLES,
            )
        )
        connector = stack.enter_context(
            readonly_database(
                paths.connector_db,
                expected_uid=paths.connector_uid,
                required_tables=CONNECTOR_TABLES,
            )
        )
        gateway = stack.enter_context(
            readonly_database(
                paths.gateway_db,
                expected_uid=paths.connector_uid,
                required_tables=GATEWAY_TABLES,
            )
        )
        fake_state = _read_private_json(
            paths.fake_state,
            expected_uid=paths.connector_uid,
        )
        relay_audit = audit_relay(relay, run_token, contract)
        connector_tuples = audit_connector(connector, relay_audit, contract)
        gateway_audit = audit_gateway(gateway, relay_audit, contract)
        fake_audit = audit_fake_state(
            fake_state,
            run_token,
            gateway_audit,
            contract,
        )

    layer_sets = {
        "relay": relay_audit.tuples,
        "connector": connector_tuples,
        "gateway": gateway_audit.tuples,
        "fakeCodex": fake_audit.tuples,
    }
    if any(values != relay_audit.tuples for values in layer_sets.values()):
        raise AuditFailure("cross-layer business tuple sets do not match")
    digests = {name: tuple_digest(values) for name, values in layer_sets.items()}
    if len(set(digests.values())) != 1:
        raise AuditFailure("cross-layer business tuple digests do not match")
    counts = {
        "turns": contract.expected_turns,
        "threads": contract.expected_threads,
        "relayTuples": len(relay_audit.tuples),
        "connectorTuples": len(connector_tuples),
        "gatewayTuples": len(gateway_audit.tuples),
        "fakeCodexTuples": len(fake_audit.tuples),
        "bindingGenerationMappings": 1,
        "relevantConnectorIntents": 0,
        "relevantConnectorOutbox": 0,
        "relevantConnectorProcessedDeliveries": 0,
    }
    if contract == FAULT_CONTRACT:
        counts.update(audit_fault_matrix(relay_audit, fake_audit, run_token))
    elif contract == EXACT_CRASH_CONTRACT:
        counts.update(audit_exact_crash_matrix(relay_audit, fake_audit, run_token))
    return {
        "schema": contract.schema,
        "outcome": "PASS",
        "runToken": run_token,
        "counts": counts,
        "digests": {
            "tupleSha256": digests["relay"],
            "relayTupleSha256": digests["relay"],
            "connectorTupleSha256": digests["connector"],
            "gatewayTupleSha256": digests["gateway"],
            "fakeCodexTupleSha256": digests["fakeCodex"],
            "bindingGenerationMappingSha256": binding_mapping_digest(relay_audit),
        },
    }


def perform_fault_audit(paths: AuditPaths, run_token: str) -> dict[str, Any]:
    return perform_audit(paths, run_token, FAULT_CONTRACT)


def perform_exact_crash_audit(
    paths: AuditPaths,
    run_token: str,
) -> dict[str, Any]:
    return perform_audit(paths, run_token, EXACT_CRASH_CONTRACT)


def resolve_paths(test_root: str | None) -> AuditPaths:
    if test_root is None:
        return AuditPaths(
            relay_db=DEFAULT_RELAY_DB,
            connector_db=DEFAULT_CONNECTOR_DB,
            gateway_db=DEFAULT_GATEWAY_DB,
            fake_state=DEFAULT_FAKE_STATE,
            relay_uid=10001,
            connector_uid=10002,
        )
    if os.environ.get("CHEBY_GATE_AUDIT_TEST_MODE") != "1":
        raise AuditFailure("test fixture paths are disabled")
    root = Path(test_root)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise AuditFailure("test fixture root is unavailable") from exc
    tmp = Path("/tmp").resolve()
    if (
        not resolved.is_absolute()
        or resolved == tmp
        or tmp not in resolved.parents
        or root.is_symlink()
    ):
        raise AuditFailure("test fixture root must be a real child of /tmp")
    metadata = resolved.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise AuditFailure("test fixture root safety check failed")
    return AuditPaths(
        relay_db=resolved / "relay.sqlite3",
        connector_db=resolved / "gate-connector.sqlite3",
        gateway_db=resolved / "gate-gateway.sqlite3",
        fake_state=resolved / "gate-script-state.json",
        relay_uid=os.geteuid(),
        connector_uid=os.geteuid(),
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("quick-demo", "load", "fault", "exact-crash"),
        required=True,
    )
    parser.add_argument("--run-token", required=True)
    parser.add_argument("--test-fixture-root", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        paths = resolve_paths(args.test_fixture_root)
        document = {
            "quick-demo": lambda: perform_audit(
                paths,
                args.run_token,
                QUICK_DEMO_CONTRACT,
            ),
            "load": lambda: perform_audit(paths, args.run_token),
            "fault": lambda: perform_fault_audit(paths, args.run_token),
            "exact-crash": lambda: perform_exact_crash_audit(
                paths,
                args.run_token,
            ),
        }[args.mode]()
    except AuditFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
