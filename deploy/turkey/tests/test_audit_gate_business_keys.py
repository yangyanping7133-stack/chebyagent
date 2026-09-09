from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/turkey/audit_gate_business_keys.py"
SPEC = importlib.util.spec_from_file_location("audit_gate_business_keys", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)

RUN_TOKEN = "a" * 32
ASSISTANT_ID = "asst_gate_audit"
DEVICE_ID = "dev_gate_audit"
NODE_ID = "node_gate_audit"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_relay(path: Path, total_turns: int = 500) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE nodes(
          id TEXT PRIMARY KEY, assistant_id TEXT, active_generation INTEGER);
        CREATE TABLE devices(
          id TEXT PRIMARY KEY, assistant_id TEXT, revoked_at INTEGER);
        CREATE TABLE streams(
          assistant_id TEXT, recipient_role TEXT, ack_cursor INTEGER);
        CREATE TABLE requests(
          assistant_id TEXT, request_id TEXT, device_id TEXT,
          operation TEXT, message_id TEXT);
        CREATE TABLE deliveries(
          assistant_id TEXT, recipient_role TEXT, delivery_seq INTEGER,
          sender_role TEXT, sender_principal_id TEXT, message_id TEXT,
          payload_json TEXT, expired_at INTEGER,
          PRIMARY KEY(assistant_id, recipient_role, delivery_seq));
        CREATE TABLE message_idempotency(
          assistant_id TEXT, sender_role TEXT, message_id TEXT,
          recipient_role TEXT, delivery_seq INTEGER, state TEXT);
        """
    )
    connection.execute(
        "INSERT INTO nodes VALUES(?,?,?)",
        (NODE_ID, ASSISTANT_ID, 7),
    )
    connection.execute(
        "INSERT INTO devices VALUES(?,?,NULL)",
        (DEVICE_ID, ASSISTANT_ID),
    )
    connection.executemany(
        "INSERT INTO streams VALUES(?,?,?)",
        [
            (ASSISTANT_ID, "node", total_turns),
            (ASSISTANT_ID, "device", total_turns * 2),
        ],
    )
    for index in range(total_turns):
        number = index + 1
        thread_number = index % 8 + 1
        ordinal = index // 8 + 1
        thread_id = f"thread_public_{thread_number:02d}"
        client_id = f"client_message_{number:04d}"
        turn_id = f"turn_public_{number:04d}"
        request_id = f"req_gate_{number:04d}"
        command_message = f"msg_command_{number:04d}"
        response_message = f"msg_response_{number:04d}"
        terminal_message = f"msg_terminal_{number:04d}"
        marker = (
            f"GATE-{RUN_TOKEN}-G{number}-T{thread_number}-N{ordinal}"
        )
        command = {
            "kind": "command",
            "requestId": request_id,
            "deviceId": DEVICE_ID,
            "operation": "turns.start",
            "params": {
                "threadId": thread_id,
                "clientMessageId": client_id,
                "input": [{"type": "text", "text": marker}],
            },
        }
        response = {
            "kind": "response",
            "requestId": request_id,
            "deviceId": DEVICE_ID,
            "operation": "turns.start",
            "ok": True,
            "result": {
                "id": turn_id,
                "threadId": thread_id,
                "clientMessageId": client_id,
                "status": "completed",
            },
        }
        terminal = {
            "kind": "event",
            "requestId": "req_subscription_gate",
            "deviceId": DEVICE_ID,
            "streamId": "stream_gate",
            "eventId": f"event_terminal_{number:04d}",
            "eventSeq": number,
            "eventType": "turn.completed",
            "threadId": thread_id,
            "data": {
                "occurredAt": "2026-07-26T00:00:00Z",
                "turnId": turn_id,
                "payload": {"status": "completed"},
            },
        }
        connection.execute(
            "INSERT INTO requests VALUES(?,?,?,?,?)",
            (
                ASSISTANT_ID,
                request_id,
                DEVICE_ID,
                "turns.start",
                command_message,
            ),
        )
        connection.executemany(
            "INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,NULL)",
            [
                (
                    ASSISTANT_ID,
                    "node",
                    number,
                    "device",
                    DEVICE_ID,
                    command_message,
                    canonical(command),
                ),
                (
                    ASSISTANT_ID,
                    "device",
                    number,
                    "node",
                    NODE_ID,
                    response_message,
                    canonical(response),
                ),
                (
                    ASSISTANT_ID,
                    "device",
                    total_turns + number,
                    "node",
                    NODE_ID,
                    terminal_message,
                    canonical(terminal),
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO message_idempotency VALUES(?,?,?,?,?,?)",
            [
                (
                    ASSISTANT_ID,
                    "device",
                    command_message,
                    "node",
                    number,
                    "acked",
                ),
                (
                    ASSISTANT_ID,
                    "node",
                    response_message,
                    "device",
                    number,
                    "acked",
                ),
                (
                    ASSISTANT_ID,
                    "node",
                    terminal_message,
                    "device",
                    total_turns + number,
                    "acked",
                ),
            ],
        )
    connection.commit()
    connection.close()


def create_connector(path: Path, total_turns: int = 500) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE inbound_messages(
          message_id TEXT PRIMARY KEY, request_id TEXT, fingerprint TEXT,
          created_at INTEGER);
        CREATE TABLE requests(
          request_id TEXT PRIMARY KEY, fingerprint TEXT, operation TEXT,
          response_frame TEXT, response_message_id TEXT, created_at INTEGER);
        CREATE TABLE request_intents(
          request_id TEXT PRIMARY KEY, message_id TEXT, state TEXT);
        CREATE TABLE processed_deliveries(
          delivery_seq INTEGER PRIMARY KEY, request_id TEXT, created_at INTEGER);
        CREATE TABLE outbox(
          local_seq INTEGER PRIMARY KEY, message_id TEXT, frame_json TEXT);
        INSERT INTO meta VALUES('bound_device_id', 'dev_gate_audit');
        """
    )
    connection.execute(
        "INSERT INTO meta VALUES('ack_cursor', ?)",
        (str(total_turns),),
    )
    for index in range(total_turns):
        number = index + 1
        thread_number = index % 8 + 1
        thread_id = f"thread_public_{thread_number:02d}"
        client_id = f"client_message_{number:04d}"
        turn_id = f"turn_public_{number:04d}"
        request_id = f"req_gate_{number:04d}"
        response_message = f"msg_response_{number:04d}"
        frame = {
            "v": 1,
            "type": "message",
            "messageId": response_message,
            "payload": {
                "kind": "response",
                "requestId": request_id,
                "deviceId": DEVICE_ID,
                "operation": "turns.start",
                "ok": True,
                "result": {
                    "id": turn_id,
                    "threadId": thread_id,
                    "clientMessageId": client_id,
                    "status": "completed",
                },
            },
        }
        connection.execute(
            "INSERT INTO inbound_messages VALUES(?,?,?,?)",
            (f"msg_command_{number:04d}", request_id, "fingerprint", number),
        )
        connection.execute(
            "INSERT INTO requests VALUES(?,?,?,?,?,?)",
            (
                request_id,
                "fingerprint",
                "turns.start",
                canonical(frame),
                response_message,
                number,
            ),
        )
    connection.commit()
    connection.close()


def create_gateway(path: Path, total_turns: int = 500) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE threads(public_id TEXT PRIMARY KEY, raw_id TEXT);
        CREATE TABLE turns(
          public_id TEXT PRIMARY KEY, raw_id TEXT, device_id TEXT,
          thread_public_id TEXT, client_message_id TEXT, status TEXT,
          delivery_state TEXT, attempt_count INTEGER);
        CREATE TABLE id_mappings(
          kind TEXT, raw_id TEXT, public_id TEXT, thread_public_id TEXT);
        CREATE TABLE message_snapshots(
          message_id TEXT PRIMARY KEY, thread_public_id TEXT,
          turn_public_id TEXT, role TEXT, client_message_id TEXT,
          payload_json TEXT);
        """
    )
    for thread_number in range(1, 9):
        connection.execute(
            "INSERT INTO threads VALUES(?,?)",
            (
                f"thread_public_{thread_number:02d}",
                f"gate-thread-{thread_number:08d}",
            ),
        )
    for index in range(total_turns):
        number = index + 1
        thread_number = index % 8 + 1
        thread_id = f"thread_public_{thread_number:02d}"
        raw_turn = f"gate-turn-{number:08d}"
        client_id = f"client_message_{number:04d}"
        turn_id = f"turn_public_{number:04d}"
        connection.execute(
            "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?)",
            (
                turn_id,
                raw_turn,
                DEVICE_ID,
                thread_id,
                client_id,
                "completed",
                "terminal",
                1,
            ),
        )
        connection.execute(
            "INSERT INTO id_mappings VALUES('turn',?,?,?)",
            (raw_turn, turn_id, thread_id),
        )
        for role in ("user", "assistant"):
            payload = {
                "id": f"message_{role}_{number:04d}",
                "threadId": thread_id,
                "turnId": turn_id,
                "role": role,
                "state": "completed",
            }
            if role == "user":
                payload["clientMessageId"] = client_id
            connection.execute(
                "INSERT INTO message_snapshots VALUES(?,?,?,?,?,?)",
                (
                    payload["id"],
                    thread_id,
                    turn_id,
                    role,
                    client_id if role == "user" else None,
                    canonical(payload),
                ),
            )
    connection.commit()
    connection.close()


def create_fake_state(path: Path, total_turns: int = 500) -> None:
    threads: dict[str, dict[str, object]] = {}
    turns: dict[str, dict[str, object]] = {}
    for thread_number in range(1, 9):
        raw_thread = f"gate-thread-{thread_number:08d}"
        threads[raw_thread] = {
            "id": raw_thread,
            "title": "Gate load",
            "preview": "",
            "status": "idle",
            "archived": True,
            "turns": [],
        }
    for index in range(total_turns):
        number = index + 1
        thread_number = index % 8 + 1
        ordinal = index // 8 + 1
        raw_thread = f"gate-thread-{thread_number:08d}"
        raw_turn = f"gate-turn-{number:08d}"
        client_id = f"client_message_{number:04d}"
        marker = (
            f"GATE-{RUN_TOKEN}-G{number}-T{thread_number}-N{ordinal}"
        )
        turn = {
            "id": raw_turn,
            "threadId": raw_thread,
            "status": "completed",
            "clientMessageId": client_id,
            "input": [{"type": "text", "text": marker}],
            "items": [
                {
                    "id": f"gate-user-{number:08d}",
                    "type": "userMessage",
                    "status": "completed",
                    "clientId": client_id,
                    "content": [{"type": "text", "text": marker}],
                },
                {
                    "id": f"gate-agent-{number:08d}",
                    "type": "agentMessage",
                    "status": "completed",
                    "text": "Fake Codex response",
                },
            ],
        }
        turns[raw_turn] = turn
        threads[raw_thread]["turns"].append(turn)
    path.write_text(
        canonical(
            {
                "v": 1,
                "threadCounter": 8,
                "turnCounter": total_turns,
                "threads": threads,
                "turns": turns,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    # CLI test paths are deliberately restricted to a private child of /tmp.
    root = Path("/tmp") / f"cheby-gate-audit-test-{os.getpid()}-{tmp_path.name}"
    root.mkdir(mode=0o700)
    create_relay(root / "relay.sqlite3")
    create_connector(root / "gate-connector.sqlite3")
    create_gateway(root / "gate-gateway.sqlite3")
    create_fake_state(root / "gate-script-state.json")
    for path in root.iterdir():
        path.chmod(0o600)
    yield root
    for path in root.iterdir():
        path.unlink()
    root.rmdir()


@pytest.fixture
def quick_fixture_root(tmp_path: Path) -> Path:
    root = Path("/tmp") / f"cheby-quick-audit-test-{os.getpid()}-{tmp_path.name}"
    root.mkdir(mode=0o700)
    create_relay(root / "relay.sqlite3", 16)
    create_connector(root / "gate-connector.sqlite3", 16)
    create_gateway(root / "gate-gateway.sqlite3", 16)
    create_fake_state(root / "gate-script-state.json", 16)
    for path in root.iterdir():
        path.chmod(0o600)
    yield root
    for path in root.iterdir():
        path.unlink()
    root.rmdir()


def audit_paths(root: Path) -> audit.AuditPaths:
    uid = os.geteuid()
    return audit.AuditPaths(
        relay_db=root / "relay.sqlite3",
        connector_db=root / "gate-connector.sqlite3",
        gateway_db=root / "gate-gateway.sqlite3",
        fake_state=root / "gate-script-state.json",
        relay_uid=uid,
        connector_uid=uid,
    )


def convert_fixture_to_fault_run(root: Path) -> None:
    assignments: list[tuple[str, int, str, int]] = []
    cycle = 0
    for component in audit.FAULT_COMPONENTS:
        for iteration in range(1, audit.FAULT_REPETITIONS + 1):
            cycle += 1
            for label in ("A1", "A2", "B1", "A3", "B2"):
                assignments.append((component, iteration, label, cycle))
    assert len(assignments) == audit.FAULT_EXPECTED_TURNS

    relay = sqlite3.connect(root / "relay.sqlite3")
    connector = sqlite3.connect(root / "gate-connector.sqlite3")
    gateway = sqlite3.connect(root / "gate-gateway.sqlite3")
    for number, (component, iteration, label, cycle_number) in enumerate(
        assignments,
        start=1,
    ):
        side = label[0].lower()
        public_thread = f"fault_thread_{cycle_number:03d}_{side}"
        raw_thread = f"gate-fault-thread-{cycle_number:03d}-{side}"
        marker = f"FAULT-{RUN_TOKEN}-{component}-{iteration}-{label}"

        command_row = relay.execute(
            """
            SELECT payload_json FROM deliveries
            WHERE assistant_id=? AND recipient_role='node' AND delivery_seq=?
            """,
            (ASSISTANT_ID, number),
        ).fetchone()
        assert command_row is not None
        command = json.loads(command_row[0])
        command["params"]["threadId"] = public_thread
        command["params"]["input"][0]["text"] = marker
        relay.execute(
            """
            UPDATE deliveries SET payload_json=?
            WHERE assistant_id=? AND recipient_role='node' AND delivery_seq=?
            """,
            (canonical(command), ASSISTANT_ID, number),
        )

        response_row = relay.execute(
            """
            SELECT payload_json FROM deliveries
            WHERE assistant_id=? AND recipient_role='device' AND delivery_seq=?
            """,
            (ASSISTANT_ID, number),
        ).fetchone()
        assert response_row is not None
        response = json.loads(response_row[0])
        response["result"]["threadId"] = public_thread
        relay.execute(
            """
            UPDATE deliveries SET payload_json=?
            WHERE assistant_id=? AND recipient_role='device' AND delivery_seq=?
            """,
            (canonical(response), ASSISTANT_ID, number),
        )

        terminal_sequence = 500 + number
        terminal_row = relay.execute(
            """
            SELECT payload_json FROM deliveries
            WHERE assistant_id=? AND recipient_role='device' AND delivery_seq=?
            """,
            (ASSISTANT_ID, terminal_sequence),
        ).fetchone()
        assert terminal_row is not None
        terminal = json.loads(terminal_row[0])
        terminal["threadId"] = public_thread
        relay.execute(
            """
            UPDATE deliveries SET payload_json=?
            WHERE assistant_id=? AND recipient_role='device' AND delivery_seq=?
            """,
            (canonical(terminal), ASSISTANT_ID, terminal_sequence),
        )

        request_id = f"req_gate_{number:04d}"
        connector_row = connector.execute(
            "SELECT response_frame FROM requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert connector_row is not None
        connector_frame = json.loads(connector_row[0])
        connector_frame["payload"]["result"]["threadId"] = public_thread
        connector.execute(
            "UPDATE requests SET response_frame=? WHERE request_id=?",
            (canonical(connector_frame), request_id),
        )

        raw_turn = f"gate-turn-{number:08d}"
        public_turn = f"turn_public_{number:04d}"
        gateway.execute(
            "UPDATE turns SET thread_public_id=? WHERE raw_id=?",
            (public_thread, raw_turn),
        )
        gateway.execute(
            "UPDATE id_mappings SET thread_public_id=? WHERE raw_id=?",
            (public_thread, raw_turn),
        )
        message_rows = gateway.execute(
            """
            SELECT message_id, payload_json FROM message_snapshots
            WHERE turn_public_id=?
            """,
            (public_turn,),
        ).fetchall()
        assert len(message_rows) == 2
        for message_id, payload_json in message_rows:
            payload = json.loads(payload_json)
            payload["threadId"] = public_thread
            gateway.execute(
                """
                UPDATE message_snapshots
                SET thread_public_id=?, payload_json=?
                WHERE message_id=?
                """,
                (public_thread, canonical(payload), message_id),
            )
        gateway.execute(
            "INSERT OR IGNORE INTO threads VALUES(?,?)",
            (public_thread, raw_thread),
        )
    relay.commit()
    connector.commit()
    gateway.commit()
    relay.close()
    connector.close()
    gateway.close()

    state_path = root / "gate-script-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for number, (component, iteration, label, cycle_number) in enumerate(
        assignments,
        start=1,
    ):
        side = label[0].lower()
        raw_thread = f"gate-fault-thread-{cycle_number:03d}-{side}"
        marker = f"FAULT-{RUN_TOKEN}-{component}-{iteration}-{label}"
        raw_turn = f"gate-turn-{number:08d}"
        turn = state["turns"][raw_turn]
        turn["threadId"] = raw_thread
        turn["input"][0]["text"] = marker
        turn["items"][0]["content"][0]["text"] = marker
        state["threads"].setdefault(
            raw_thread,
            {
                "id": raw_thread,
                "title": "Gate fault",
                "preview": "",
                "status": "idle",
                "archived": True,
                "turns": [],
            },
        )
    for thread in state["threads"].values():
        thread["turns"] = []
    for turn in state["turns"].values():
        state["threads"][turn["threadId"]]["turns"].append(turn)
    state["threadCounter"] = len(state["threads"])
    state_path.write_text(canonical(state), encoding="utf-8")
    state_path.chmod(0o600)


def test_positive_read_only_cross_layer_audit(fixture_root: Path) -> None:
    source_names = {
        "relay.sqlite3",
        "gate-connector.sqlite3",
        "gate-gateway.sqlite3",
        "gate-script-state.json",
    }
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in fixture_root.iterdir()
        if path.name in source_names
    }
    document = audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)
    assert document["outcome"] == "PASS"
    assert document["counts"]["turns"] == 500
    assert document["counts"]["threads"] == 8
    assert document["counts"]["bindingGenerationMappings"] == 1
    tuple_digests = {
        document["digests"][key]
        for key in (
            "tupleSha256",
            "relayTupleSha256",
            "connectorTupleSha256",
            "gatewayTupleSha256",
            "fakeCodexTupleSha256",
        )
    }
    assert len(tuple_digests) == 1
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in fixture_root.iterdir()
        if path.name in source_names
    }
    assert after == before
    for path in fixture_root.iterdir():
        if path.name.endswith(("-wal", "-shm")):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_quick_demo_has_a_distinct_fixed_sixteen_turn_remote_contract(
    quick_fixture_root: Path,
) -> None:
    document = audit.perform_audit(
        audit_paths(quick_fixture_root),
        RUN_TOKEN,
        audit.QUICK_DEMO_CONTRACT,
    )
    assert document["outcome"] == "PASS"
    assert document["counts"]["turns"] == 16
    assert document["counts"]["threads"] == 8


def test_gateway_turn_ledger_does_not_require_redundant_history_mappings(
    quick_fixture_root: Path,
) -> None:
    connection = sqlite3.connect(quick_fixture_root / "gate-gateway.sqlite3")
    connection.execute("DELETE FROM id_mappings WHERE kind='turn'")
    connection.commit()
    connection.close()

    document = audit.perform_audit(
        audit_paths(quick_fixture_root),
        RUN_TOKEN,
        audit.QUICK_DEMO_CONTRACT,
    )

    assert document["outcome"] == "PASS"
    assert document["counts"]["gatewayTuples"] == 16


def test_gateway_rejects_a_conflicting_optional_history_mapping(
    quick_fixture_root: Path,
) -> None:
    connection = sqlite3.connect(quick_fixture_root / "gate-gateway.sqlite3")
    connection.execute(
        "UPDATE id_mappings SET public_id=? WHERE kind='turn' AND raw_id=?",
        ("turn_public_conflict", "gate-turn-00000001"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(audit.AuditFailure, match="mapping is inconsistent"):
        audit.perform_audit(
            audit_paths(quick_fixture_root),
            RUN_TOKEN,
            audit.QUICK_DEMO_CONTRACT,
        )


def test_positive_fault_matrix_audit_is_read_only(fixture_root: Path) -> None:
    convert_fixture_to_fault_run(fixture_root)
    source_names = {
        "relay.sqlite3",
        "gate-connector.sqlite3",
        "gate-gateway.sqlite3",
        "gate-script-state.json",
    }
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in fixture_root.iterdir()
        if path.name in source_names
    }
    document = audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)
    assert document["schema"] == "chebycodex.gate-fault-run-audit.v1"
    assert document["counts"] == {
        "turns": 250,
        "threads": 100,
        "relayTuples": 250,
        "connectorTuples": 250,
        "gatewayTuples": 250,
        "fakeCodexTuples": 250,
        "bindingGenerationMappings": 1,
        "relevantConnectorIntents": 0,
        "relevantConnectorOutbox": 0,
        "relevantConnectorProcessedDeliveries": 0,
        "cycles": 50,
        "businessKeys": 250,
        "maxTurnCountPerBusinessKey": 1,
        "maxEffectCountPerBusinessKey": 1,
        "orphanDeliveries": 0,
        "orphanOutbox": 0,
        "orphanIntents": 0,
    }
    assert len(
        {
            document["digests"][key]
            for key in (
                "tupleSha256",
                "relayTupleSha256",
                "connectorTupleSha256",
                "gatewayTupleSha256",
                "fakeCodexTupleSha256",
            )
        }
    ) == 1
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in fixture_root.iterdir()
        if path.name in source_names
    }
    assert after == before


def test_fault_matrix_rejects_cross_layer_marker_change(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    path = fixture_root / "gate-script-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    turn = state["turns"]["gate-turn-00000001"]
    changed = f"FAULT-{RUN_TOKEN}-APP-1-A2"
    turn["input"][0]["text"] = changed
    turn["items"][0]["content"][0]["text"] = changed
    path.write_text(canonical(state), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(audit.AuditFailure, match="marker matrix"):
        audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fault_audit_rejects_gateway_reexecution(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    path = fixture_root / "gate-gateway.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE turns SET attempt_count=2
        WHERE client_message_id='client_message_0001'
        """
    )
    connection.commit()
    connection.close()
    with pytest.raises(audit.AuditFailure, match="exact terminal delivery"):
        audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fault_audit_rejects_orphan_connector_intent(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    path = fixture_root / "gate-connector.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO request_intents VALUES(
          'req_gate_0001', 'msg_command_0001', 'executing')
        """
    )
    connection.commit()
    connection.close()
    with pytest.raises(audit.AuditFailure, match="execution intent"):
        audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fault_matrix_rejects_same_thread_fifo_reordering(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    path = fixture_root / "relay.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        UPDATE deliveries SET delivery_seq=10001
        WHERE assistant_id=? AND recipient_role='node' AND delivery_seq=1
        """,
        (ASSISTANT_ID,),
    )
    connection.execute(
        """
        UPDATE deliveries SET delivery_seq=1
        WHERE assistant_id=? AND recipient_role='node' AND delivery_seq=2
        """,
        (ASSISTANT_ID,),
    )
    connection.execute(
        """
        UPDATE deliveries SET delivery_seq=2
        WHERE assistant_id=? AND recipient_role='node' AND delivery_seq=10001
        """,
        (ASSISTANT_ID,),
    )
    connection.execute(
        """
        UPDATE message_idempotency
        SET delivery_seq=CASE message_id
          WHEN 'msg_command_0001' THEN 2
          WHEN 'msg_command_0002' THEN 1
        END
        WHERE message_id IN ('msg_command_0001', 'msg_command_0002')
        """
    )
    connection.commit()
    connection.close()
    with pytest.raises(audit.AuditFailure, match="FIFO"):
        audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fault_matrix_rejects_fake_codex_effect_reordering(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    gateway_path = fixture_root / "gate-gateway.sqlite3"
    connection = sqlite3.connect(gateway_path)
    for table in ("turns", "id_mappings"):
        connection.execute(
            f"UPDATE {table} SET raw_id='gate-turn-99999999' "
            "WHERE raw_id='gate-turn-00000001'"
        )
        connection.execute(
            f"UPDATE {table} SET raw_id='gate-turn-00000001' "
            "WHERE raw_id='gate-turn-00000002'"
        )
        connection.execute(
            f"UPDATE {table} SET raw_id='gate-turn-00000002' "
            "WHERE raw_id='gate-turn-99999999'"
        )
    connection.commit()
    connection.close()

    state_path = fixture_root / "gate-script-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    first = state["turns"]["gate-turn-00000001"]
    second = state["turns"]["gate-turn-00000002"]
    first_body = dict(second)
    second_body = dict(first)
    first_body["id"] = "gate-turn-00000001"
    second_body["id"] = "gate-turn-00000002"
    first_body["items"] = [dict(item) for item in second["items"]]
    second_body["items"] = [dict(item) for item in first["items"]]
    first_body["items"][0]["id"] = "gate-user-00000001"
    first_body["items"][1]["id"] = "gate-agent-00000001"
    second_body["items"][0]["id"] = "gate-user-00000002"
    second_body["items"][1]["id"] = "gate-agent-00000002"
    state["turns"]["gate-turn-00000001"] = first_body
    state["turns"]["gate-turn-00000002"] = second_body
    for thread in state["threads"].values():
        thread["turns"] = []
    for turn in state["turns"].values():
        state["threads"][turn["threadId"]]["turns"].append(turn)
    state_path.write_text(canonical(state), encoding="utf-8")
    state_path.chmod(0o600)
    with pytest.raises(audit.AuditFailure, match="effect FIFO"):
        audit.perform_fault_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fault_cli_has_fixed_contract_and_no_raw_ids(
    fixture_root: Path,
) -> None:
    convert_fixture_to_fault_run(fixture_root)
    environment = dict(os.environ)
    environment["CHEBY_GATE_AUDIT_TEST_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "fault",
            "--run-token",
            RUN_TOKEN,
            "--test-fixture-root",
            str(fixture_root),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["counts"]["cycles"] == 50
    assert "fault_thread" not in result.stdout
    assert "client_message" not in result.stdout
    assert "FAULT-" not in result.stdout


def insert_extra_gateway_user_snapshot(
    fixture_root: Path,
    client_message_id: str | None,
) -> None:
    path = fixture_root / "gate-gateway.sqlite3"
    connection = sqlite3.connect(path)
    payload = {
        "id": "message_user_extra_0001",
        "threadId": "thread_public_01",
        "turnId": "turn_public_0001",
        "role": "user",
        "state": "completed",
        "clientMessageId": client_message_id,
    }
    connection.execute(
        "INSERT INTO message_snapshots VALUES(?,?,?,?,?,?)",
        (
            payload["id"],
            payload["threadId"],
            payload["turnId"],
            payload["role"],
            client_message_id,
            canonical(payload),
        ),
    )
    connection.commit()
    connection.close()


def test_gateway_rejects_extra_user_snapshot_with_wrong_client_id(
    fixture_root: Path,
) -> None:
    insert_extra_gateway_user_snapshot(fixture_root, "client_message_wrong")
    with pytest.raises(audit.AuditFailure, match="snapshot count is not exact"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_gateway_rejects_extra_user_snapshot_with_null_client_id(
    fixture_root: Path,
) -> None:
    insert_extra_gateway_user_snapshot(fixture_root, None)
    with pytest.raises(audit.AuditFailure, match="snapshot count is not exact"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_gateway_rejects_user_payload_client_identity_mismatch(
    fixture_root: Path,
) -> None:
    path = fixture_root / "gate-gateway.sqlite3"
    connection = sqlite3.connect(path)
    row = connection.execute(
        """
        SELECT payload_json FROM message_snapshots
        WHERE message_id='message_user_0001'
        """
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    payload["clientMessageId"] = "client_message_wrong"
    connection.execute(
        """
        UPDATE message_snapshots SET payload_json=?
        WHERE message_id='message_user_0001'
        """,
        (canonical(payload),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(audit.AuditFailure, match="lost its client id"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def append_extra_fake_user_item(
    fixture_root: Path,
    client_message_id: str | None,
) -> None:
    path = fixture_root / "gate-script-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    turn = state["turns"]["gate-turn-00000001"]
    turn["items"].append(
        {
            "id": "gate-user-extra-00000001",
            "type": "userMessage",
            "status": "completed",
            "clientId": client_message_id,
            "content": [{"type": "text", "text": "unexpected"}],
        }
    )
    path.write_text(canonical(state), encoding="utf-8")
    path.chmod(0o600)


def test_fake_state_rejects_extra_user_item_with_wrong_client_id(
    fixture_root: Path,
) -> None:
    append_extra_fake_user_item(fixture_root, "client_message_wrong")
    with pytest.raises(audit.AuditFailure, match="item count is not exact"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fake_state_rejects_extra_user_item_with_null_client_id(
    fixture_root: Path,
) -> None:
    append_extra_fake_user_item(fixture_root, None)
    with pytest.raises(audit.AuditFailure, match="item count is not exact"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_fake_state_rejects_user_client_identity_mismatch(
    fixture_root: Path,
) -> None:
    path = fixture_root / "gate-script-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["turns"]["gate-turn-00000001"]["items"][0]["clientId"] = (
        "client_message_wrong"
    )
    path.write_text(canonical(state), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(audit.AuditFailure, match="effect identity"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


@pytest.mark.parametrize(
    ("database", "statement", "message"),
    [
        (
            "relay.sqlite3",
            "DELETE FROM message_idempotency WHERE message_id='msg_terminal_0001'",
            "idempotency",
        ),
        (
            "gate-connector.sqlite3",
            """
            INSERT INTO request_intents VALUES(
              'req_gate_0001', 'msg_command_0001', 'executing')
            """,
            "execution intent",
        ),
        (
            "gate-connector.sqlite3",
            """
            INSERT INTO processed_deliveries VALUES(
              1, 'req_gate_0001', 1)
            """,
            "processed-delivery",
        ),
        (
            "gate-gateway.sqlite3",
            """
            UPDATE turns SET public_id='turn_wrong'
            WHERE client_message_id='client_message_0001'
            """,
            "mapping",
        ),
    ],
)
def test_negative_database_corruption_is_detected(
    fixture_root: Path,
    database: str,
    statement: str,
    message: str,
) -> None:
    path = fixture_root / database
    path.chmod(0o600)
    connection = sqlite3.connect(path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    with pytest.raises(audit.AuditFailure, match=message):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_negative_fake_turn_duplicate_is_detected(fixture_root: Path) -> None:
    path = fixture_root / "gate-script-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    first = state["turns"]["gate-turn-00000001"]
    state["threads"][first["threadId"]]["turns"].append(first)
    path.write_text(canonical(state), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(audit.AuditFailure, match="duplicate"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_cli_test_root_is_strict_and_output_has_no_raw_ids(
    fixture_root: Path,
) -> None:
    environment = dict(os.environ)
    environment["CHEBY_GATE_AUDIT_TEST_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "load",
            "--run-token",
            RUN_TOKEN,
            "--test-fixture-root",
            str(fixture_root),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["outcome"] == "PASS"
    assert "thread_public" not in result.stdout
    assert "client_message" not in result.stdout
    assert "turn_public" not in result.stdout
    assert "GATE-" not in result.stdout
    assert stat.S_IMODE(fixture_root.stat().st_mode) == 0o700


def test_cli_rejects_arbitrary_paths_and_bad_token(fixture_root: Path) -> None:
    without_test_mode = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "load",
            "--run-token",
            RUN_TOKEN,
            "--test-fixture-root",
            str(fixture_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert without_test_mode.returncode == 1
    missing_mode = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-token",
            RUN_TOKEN,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert missing_mode.returncode == 2
    with pytest.raises(audit.AuditFailure, match="32 lowercase hex"):
        audit.perform_audit(audit_paths(fixture_root), "not-a-token")


def test_world_readable_sqlite_sidecar_fails_closed(fixture_root: Path) -> None:
    sidecar = fixture_root / "gate-gateway.sqlite3-wal"
    sidecar.write_bytes(b"unsafe")
    sidecar.chmod(0o644)
    with pytest.raises(audit.AuditFailure, match="file safety"):
        audit.perform_audit(audit_paths(fixture_root), RUN_TOKEN)


def test_connector_runner_sets_umask_before_application_import() -> None:
    source = (ROOT / "deploy/turkey/connector_runner.py").read_text(encoding="utf-8")
    assert source.index("os.umask(0o077)") < source.index(
        "from cheby_connector.main import main"
    )
