from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import json
import os
import signal
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cheby_connector.config import ConnectorSettings
from cheby_connector.engine import ConnectorEngine
from cheby_connector.gateway import DirectGatewayBackend
from cheby_connector.gate_bridge import ScriptedGateCodexBridge
from cheby_connector.models import ThreadsCreateParams
from cheby_connector.state import ConnectorState
from cheby_gateway.config import GatewaySettings
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore
from cheby_relay.config import RelaySettings
from cheby_relay.store import RelayStore


class InjectedRestartCheckpoint(BaseException):
    """A synthetic stop used only by the fast deterministic reconstruction matrix."""


class ServerCrashBoundary(str, Enum):
    TRANSPORT_WRITE_UNCONFIRMED = "transport_write_unconfirmed"
    RELAY_COMMAND_COMMITTED = "relay_command_committed"
    RELAY_COMMAND_ACCEPTED_TRANSPORT = "relay_command_accepted_transport"
    CONNECTOR_EXECUTION_INTENT_COMMITTED = "connector_execution_intent_committed"
    GATEWAY_TURN_RESERVED = "gateway_turn_reserved"
    SCRIPTED_CODEX_TURN_PERSISTED = "scripted_codex_turn_persisted"
    RELAY_RESPONSE_COMMITTED = "relay_response_committed"
    GATEWAY_TERMINAL_COMMITTED = "gateway_terminal_committed"
    CONNECTOR_RESPONSE_OUTBOX_REMOVED = "connector_response_outbox_removed"


class ExactSigkillBoundary(str, Enum):
    RELAY_COMMAND_COMMITTED = "relay_command_committed"
    CONNECTOR_EXECUTION_INTENT = "connector_execution_intent"
    GATEWAY_TURN_RESERVATION = "gateway_turn_reservation"
    SCRIPTED_CODEX_ACCEPTED = "scripted_codex_accepted"
    GATEWAY_TERMINAL_COMMITTED = "gateway_terminal_committed"
    CONNECTOR_RESPONSE_COMMITTED = "connector_response_committed"
    RELAY_RESPONSE_COMMITTED = "relay_response_committed"
    CONNECTOR_OUTBOX_REMOVED = "connector_outbox_removed"


SIGKILL_BOUNDARIES = (
    ExactSigkillBoundary.CONNECTOR_EXECUTION_INTENT.value,
    ExactSigkillBoundary.GATEWAY_TURN_RESERVATION.value,
    ExactSigkillBoundary.SCRIPTED_CODEX_ACCEPTED.value,
    ExactSigkillBoundary.GATEWAY_TERMINAL_COMMITTED.value,
    ExactSigkillBoundary.CONNECTOR_RESPONSE_COMMITTED.value,
)


@dataclass(frozen=True)
class Command:
    message_id: str
    request_id: str
    client_message_id: str
    thread_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CrashPersistenceSnapshot:
    """Read-only view of the four durable owners before recovery can mutate them."""

    relay_command_deliveries: tuple[dict[str, Any], ...]
    relay_response_deliveries: tuple[dict[str, Any], ...]
    relay_command_idempotency: tuple[dict[str, Any], ...]
    relay_response_idempotency: tuple[dict[str, Any], ...]
    relay_requests: tuple[dict[str, Any], ...]
    relay_streams: tuple[dict[str, Any], ...]
    connector_intents: tuple[dict[str, Any], ...]
    connector_inbound: tuple[dict[str, Any], ...]
    connector_requests: tuple[dict[str, Any], ...]
    connector_processed: tuple[dict[str, Any], ...]
    connector_outbox: tuple[dict[str, Any], ...]
    connector_meta: tuple[dict[str, Any], ...]
    gateway_turns: tuple[dict[str, Any], ...]
    gateway_threads: tuple[dict[str, Any], ...]
    gateway_user_messages: tuple[dict[str, Any], ...]
    bridge_turns: tuple[dict[str, Any], ...]


def _relay_id(prefix: str, value: int) -> str:
    return f"{prefix}_{value:022d}"


def _frame(delivery: Any) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "delivery",
            "deliverySeq": delivery.delivery_seq,
            "messageId": delivery.message_id,
            "payload": delivery.payload,
        },
        separators=(",", ":"),
    )


def _response_message_id(request_id: str) -> str:
    digest = hashlib.sha256(
        ("CHEBY-CONNECTOR-1\0response\0" + request_id).encode("utf-8")
    ).digest()[:16]
    return "msg_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _command_fingerprint(command: Command) -> str:
    canonical = json.dumps(
        command.payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _turn_fingerprint(command: Command) -> str:
    canonical = json.dumps(
        command.payload["params"]["input"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_only_rows(
    path: Path,
    query: str,
    params: tuple[Any, ...] = (),
) -> tuple[dict[str, Any], ...]:
    """Read a killed process' SQLite state without running migrations/recovery."""

    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        return tuple(dict(row) for row in connection.execute(query, params).fetchall())
    finally:
        connection.close()


def _one(
    rows: tuple[dict[str, Any], ...],
    label: str,
) -> dict[str, Any]:
    assert len(rows) == 1, f"{label} must contain exactly one durable row"
    return rows[0]


class ProductionCrashHarness:
    """Drive the production stores through fast reconstruction and SIGKILL gates."""

    def __init__(
        self,
        root: Path,
        *,
        request_timeout_seconds: float = 2,
    ) -> None:
        self.root = root
        self.relay_settings = RelaySettings(db_path=str(root / "relay.sqlite3"))
        self.relay = RelayStore(self.relay_settings)
        self.bootstrap = self.relay.bootstrap_assistant()
        key = ec.generate_private_key(ec.SECP256R1())
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.device_tokens = self.relay.exchange_pairing(
            assistant_id=self.bootstrap.assistant_id,
            pairing_secret=self.bootstrap.pairing_secret,
            device_name="JUY-AL00",
            public_key_spki=base64.b64encode(public_der).decode("ascii"),
        )
        self.node_generation = self.relay.activate_node_generation(
            self.bootstrap.node_id
        )
        self.device_principal = self.relay.authenticate(
            self.device_tokens.access_token, "device"
        )
        self.node_principal = self.relay.authenticate(
            self.bootstrap.node_token, "node"
        )
        self.gateway_path = root / "gateway.sqlite3"
        self.connector_path = root / "connector.sqlite3"
        self.bridge_path = root / "gate-script-state.json"
        self.gateway_settings = GatewaySettings(
            db_path=str(self.gateway_path),
            pairing_secret="test-pairing-secret",
            bridge_mode="fake",
            asset_staging_dir=str(root / "assets"),
            approval_sweep_interval_seconds=3600,
        )
        self.connector_settings = ConnectorSettings(
            relay_url="wss://relay.example/relay/v1/node",
            connector_id=self.bootstrap.node_id,
            relay_token=self.bootstrap.node_token,
            state_path=str(self.connector_path),
            health_path=str(root / "connector.health"),
            request_timeout_seconds=request_timeout_seconds,
            idempotency_gc_interval_seconds=3600,
        )
        self.store: GatewayStore
        self.bridge: ScriptedGateCodexBridge
        self.service: GatewayService
        self.backend: DirectGatewayBackend
        self.state: ConnectorState
        self.engine: ConnectorEngine
        self._terminal_events: dict[str, asyncio.Event] = {}
        self._stack_open = False

    async def start(self) -> None:
        await self._open_stack()

    async def close(self) -> None:
        if self._stack_open:
            await self.engine.close()
            self._stack_open = False
        self.relay.close()

    async def _open_stack(self) -> None:
        self.store = GatewayStore(str(self.gateway_path))
        self.bridge = ScriptedGateCodexBridge(str(self.bridge_path))
        self.service = GatewayService(
            self.gateway_settings,
            self.store,
            self.bridge,
        )
        self._install_terminal_observer()
        await self.service.start()
        self.backend = DirectGatewayBackend(
            service=self.service,
            store=self.store,
            connector_name=self.bootstrap.node_id,
            owns_store=True,
        )
        await self.backend.bind_device(self.device_tokens.device_id)
        self.state = ConnectorState(str(self.connector_path))
        self.engine = ConnectorEngine(
            self.connector_settings,
            self.state,
            self.backend,
        )
        self._stack_open = True

    async def stop_stack(self) -> None:
        if not self._stack_open:
            return
        await self.engine.close()
        self._stack_open = False

    async def restart_stack(self) -> None:
        await self.stop_stack()
        await self._open_stack()

    def snapshot_after_sigkill(self, command: Command) -> CrashPersistenceSnapshot:
        """Snapshot killed-process files before any production object is reopened."""

        assistant_id = self.bootstrap.assistant_id
        response_message_id = _response_message_id(command.request_id)
        relay_path = Path(self.relay_settings.db_path)
        relay_command_deliveries = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, recipient_role, delivery_seq, sender_role,
                   sender_principal_id, message_id, payload_json, fingerprint,
                   expired_at
            FROM deliveries
            WHERE assistant_id = ? AND recipient_role = 'node'
                AND message_id = ?
            """,
            (assistant_id, command.message_id),
        )
        relay_response_deliveries = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, recipient_role, delivery_seq, sender_role,
                   sender_principal_id, message_id, payload_json, fingerprint,
                   expired_at
            FROM deliveries
            WHERE assistant_id = ? AND recipient_role = 'device'
                AND message_id = ?
            """,
            (assistant_id, response_message_id),
        )
        relay_command_idempotency = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, sender_role, message_id, fingerprint,
                   recipient_role, delivery_seq, state
            FROM message_idempotency
            WHERE assistant_id = ? AND sender_role = 'device'
                AND message_id = ?
            """,
            (assistant_id, command.message_id),
        )
        relay_response_idempotency = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, sender_role, message_id, fingerprint,
                   recipient_role, delivery_seq, state
            FROM message_idempotency
            WHERE assistant_id = ? AND sender_role = 'node'
                AND message_id = ?
            """,
            (assistant_id, response_message_id),
        )
        relay_requests = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, request_id, device_id, operation, message_id
            FROM requests
            WHERE assistant_id = ? AND request_id = ?
            """,
            (assistant_id, command.request_id),
        )
        relay_streams = _read_only_rows(
            relay_path,
            """
            SELECT assistant_id, recipient_role, next_seq, ack_cursor
            FROM streams
            WHERE assistant_id = ?
            ORDER BY recipient_role
            """,
            (assistant_id,),
        )

        connector_intents = _read_only_rows(
            self.connector_path,
            """
            SELECT request_id, message_id, fingerprint, operation, state
            FROM request_intents WHERE request_id = ?
            """,
            (command.request_id,),
        )
        connector_inbound = _read_only_rows(
            self.connector_path,
            """
            SELECT message_id, request_id, fingerprint
            FROM inbound_messages WHERE message_id = ?
            """,
            (command.message_id,),
        )
        connector_requests = _read_only_rows(
            self.connector_path,
            """
            SELECT request_id, fingerprint, operation, response_frame,
                   response_message_id
            FROM requests WHERE request_id = ?
            """,
            (command.request_id,),
        )
        connector_processed = _read_only_rows(
            self.connector_path,
            """
            SELECT delivery_seq, request_id
            FROM processed_deliveries WHERE request_id = ?
            """,
            (command.request_id,),
        )
        connector_outbox = _read_only_rows(
            self.connector_path,
            """
            SELECT local_seq, message_id, frame_json
            FROM outbox WHERE message_id = ?
            """,
            (response_message_id,),
        )
        connector_meta = _read_only_rows(
            self.connector_path,
            """
            SELECT key, value FROM meta
            WHERE key IN ('ack_cursor', 'bound_device_id')
            ORDER BY key
            """,
        )

        gateway_turns = _read_only_rows(
            self.gateway_path,
            """
            SELECT public_id, raw_id, device_id, thread_public_id,
                   client_message_id, status, delivery_state,
                   request_fingerprint
            FROM turns
            WHERE device_id = ? AND thread_public_id = ?
                AND client_message_id = ?
            """,
            (
                self.device_tokens.device_id,
                command.thread_id,
                command.client_message_id,
            ),
        )
        gateway_threads = _read_only_rows(
            self.gateway_path,
            """
            SELECT public_id, raw_id FROM threads WHERE public_id = ?
            """,
            (command.thread_id,),
        )
        gateway_user_messages = _read_only_rows(
            self.gateway_path,
            """
            SELECT message_id, thread_public_id, turn_public_id,
                   client_message_id, role, payload_json
            FROM message_snapshots
            WHERE thread_public_id = ? AND client_message_id = ?
                AND role = 'user'
            """,
            (command.thread_id, command.client_message_id),
        )

        bridge_state = json.loads(self.bridge_path.read_text(encoding="utf-8"))
        bridge_turns = tuple(
            dict(turn)
            for turn in bridge_state.get("turns", {}).values()
            if turn.get("clientMessageId") == command.client_message_id
        )
        return CrashPersistenceSnapshot(
            relay_command_deliveries=relay_command_deliveries,
            relay_response_deliveries=relay_response_deliveries,
            relay_command_idempotency=relay_command_idempotency,
            relay_response_idempotency=relay_response_idempotency,
            relay_requests=relay_requests,
            relay_streams=relay_streams,
            connector_intents=connector_intents,
            connector_inbound=connector_inbound,
            connector_requests=connector_requests,
            connector_processed=connector_processed,
            connector_outbox=connector_outbox,
            connector_meta=connector_meta,
            gateway_turns=gateway_turns,
            gateway_threads=gateway_threads,
            gateway_user_messages=gateway_user_messages,
            bridge_turns=bridge_turns,
        )

    def assert_exact_sigkill_snapshot(
        self,
        snapshot: CrashPersistenceSnapshot,
        *,
        boundary: ExactSigkillBoundary,
        command: Command,
        expected_delivery_seq: int,
    ) -> None:
        """Prove this commit happened and the next durable commit did not."""

        stage = list(ExactSigkillBoundary).index(boundary) + 1
        response_message_id = _response_message_id(command.request_id)
        expected_command_fingerprint = _command_fingerprint(command)

        relay_delivery = _one(
            snapshot.relay_command_deliveries,
            "Relay command delivery",
        )
        relay_payload = json.loads(str(relay_delivery["payload_json"]))
        assert relay_payload == command.payload
        assert int(relay_delivery["delivery_seq"]) == expected_delivery_seq
        assert relay_delivery["sender_role"] == "device"
        assert relay_delivery["recipient_role"] == "node"
        assert relay_delivery["sender_principal_id"] == self.device_tokens.device_id
        assert bytes(relay_delivery["fingerprint"]).hex() == expected_command_fingerprint

        relay_command_key = _one(
            snapshot.relay_command_idempotency,
            "Relay command idempotency",
        )
        assert int(relay_command_key["delivery_seq"]) == expected_delivery_seq
        assert relay_command_key["recipient_role"] == "node"
        assert bytes(relay_command_key["fingerprint"]).hex() == expected_command_fingerprint
        assert relay_command_key["state"] == (
            "acked"
            if boundary == ExactSigkillBoundary.CONNECTOR_OUTBOX_REMOVED
            else "pending"
        )
        relay_request = _one(snapshot.relay_requests, "Relay request correlation")
        assert relay_request == {
            "assistant_id": self.bootstrap.assistant_id,
            "request_id": command.request_id,
            "device_id": self.device_tokens.device_id,
            "operation": "turns.start",
            "message_id": command.message_id,
        }
        streams = {
            str(row["recipient_role"]): row for row in snapshot.relay_streams
        }
        assert set(streams) == {"device", "node"}
        assert int(streams["node"]["ack_cursor"]) == (
            expected_delivery_seq
            if boundary == ExactSigkillBoundary.CONNECTOR_OUTBOX_REMOVED
            else expected_delivery_seq - 1
        )

        expected_identity = {
            "deviceId": self.device_tokens.device_id,
            "threadId": command.thread_id,
            "clientMessageId": command.client_message_id,
        }
        assert relay_payload["deviceId"] == expected_identity["deviceId"]
        assert relay_payload["requestId"] == command.request_id
        assert relay_payload["operation"] == "turns.start"
        assert relay_payload["params"]["threadId"] == expected_identity["threadId"]
        assert (
            relay_payload["params"]["clientMessageId"]
            == expected_identity["clientMessageId"]
        )

        connector_started = stage >= 2
        gateway_reserved = stage >= 3
        codex_persisted = stage >= 4
        connector_completed = stage >= 6
        relay_response_committed = stage >= 7
        connector_outbox_removed = stage >= 8
        connector_meta = {
            str(row["key"]): str(row["value"]) for row in snapshot.connector_meta
        }
        assert int(connector_meta["ack_cursor"]) == (
            expected_delivery_seq if connector_completed else expected_delivery_seq - 1
        )
        if "bound_device_id" in connector_meta:
            assert connector_meta["bound_device_id"] == self.device_tokens.device_id

        if connector_started and not connector_completed:
            connector_intent = _one(
                snapshot.connector_intents,
                "Connector execution intent",
            )
            assert connector_intent == {
                "request_id": command.request_id,
                "message_id": command.message_id,
                "fingerprint": expected_command_fingerprint,
                "operation": "turns.start",
                "state": "executing",
            }
        else:
            assert snapshot.connector_intents == ()

        gateway_thread = _one(snapshot.gateway_threads, "Gateway Thread mapping")
        assert gateway_thread["public_id"] == command.thread_id
        raw_thread_id = str(gateway_thread["raw_id"])

        gateway_turn: dict[str, Any] | None = None
        if gateway_reserved:
            gateway_turn = _one(snapshot.gateway_turns, "Gateway Turn")
            assert gateway_turn["device_id"] == expected_identity["deviceId"]
            assert gateway_turn["thread_public_id"] == expected_identity["threadId"]
            assert (
                gateway_turn["client_message_id"]
                == expected_identity["clientMessageId"]
            )
            assert gateway_turn["request_fingerprint"] == _turn_fingerprint(command)
        else:
            assert snapshot.gateway_turns == ()

        if boundary == ExactSigkillBoundary.GATEWAY_TURN_RESERVATION:
            assert gateway_turn is not None
            assert (
                gateway_turn["status"],
                gateway_turn["delivery_state"],
                gateway_turn["raw_id"],
            ) == ("pending", "reserved", None)
            assert snapshot.gateway_user_messages == ()
        elif gateway_reserved:
            user_message = _one(
                snapshot.gateway_user_messages,
                "Gateway canonical user message",
            )
            assert user_message["thread_public_id"] == command.thread_id
            assert user_message["client_message_id"] == command.client_message_id
            assert user_message["turn_public_id"] == gateway_turn["public_id"]

        bridge_turn: dict[str, Any] | None = None
        if codex_persisted:
            bridge_turn = _one(snapshot.bridge_turns, "scripted Codex Turn")
            assert bridge_turn["threadId"] == raw_thread_id
            assert bridge_turn["clientMessageId"] == command.client_message_id
            assert bridge_turn["input"] == command.payload["params"]["input"]
        else:
            assert snapshot.bridge_turns == ()

        if boundary == ExactSigkillBoundary.SCRIPTED_CODEX_ACCEPTED:
            assert gateway_turn is not None
            assert (
                gateway_turn["status"],
                gateway_turn["delivery_state"],
                gateway_turn["raw_id"],
            ) == ("dispatching", "dispatching", None)
        elif boundary == ExactSigkillBoundary.GATEWAY_TERMINAL_COMMITTED:
            assert gateway_turn is not None
            assert bridge_turn is not None
            assert gateway_turn["status"] == "completed"
            assert gateway_turn["delivery_state"] == "terminal"
            assert gateway_turn["raw_id"] == bridge_turn["id"]

        if connector_completed:
            connector_inbound = _one(
                snapshot.connector_inbound,
                "Connector inbound identity",
            )
            assert connector_inbound == {
                "message_id": command.message_id,
                "request_id": command.request_id,
                "fingerprint": expected_command_fingerprint,
            }
            connector_request = _one(
                snapshot.connector_requests,
                "Connector response",
            )
            assert connector_request["request_id"] == command.request_id
            assert connector_request["fingerprint"] == expected_command_fingerprint
            assert connector_request["operation"] == "turns.start"
            assert connector_request["response_message_id"] == response_message_id
            connector_frame = json.loads(str(connector_request["response_frame"]))
            assert connector_frame["messageId"] == response_message_id
            connector_payload = connector_frame["payload"]
            assert connector_payload["requestId"] == command.request_id
            assert connector_payload["deviceId"] == expected_identity["deviceId"]
            assert connector_payload["operation"] == "turns.start"
            assert connector_payload["ok"] is True
            assert connector_payload["result"]["threadId"] == command.thread_id
            assert (
                connector_payload["result"]["clientMessageId"]
                == command.client_message_id
            )
            assert gateway_turn is not None
            assert bridge_turn is not None
            assert connector_payload["result"]["id"] == gateway_turn["public_id"]
            assert gateway_turn["raw_id"] == bridge_turn["id"]
            assert snapshot.connector_processed == ()
            if connector_outbox_removed:
                assert snapshot.connector_outbox == ()
            else:
                connector_outbox = _one(
                    snapshot.connector_outbox,
                    "Connector response outbox",
                )
                assert connector_outbox["message_id"] == response_message_id
                assert json.loads(str(connector_outbox["frame_json"])) == connector_frame
        else:
            assert snapshot.connector_inbound == ()
            assert snapshot.connector_requests == ()
            assert snapshot.connector_processed == ()
            assert snapshot.connector_outbox == ()

        if relay_response_committed:
            relay_response = _one(
                snapshot.relay_response_deliveries,
                "Relay response delivery",
            )
            relay_response_key = _one(
                snapshot.relay_response_idempotency,
                "Relay response idempotency",
            )
            assert relay_response["message_id"] == response_message_id
            assert relay_response["sender_role"] == "node"
            assert relay_response["recipient_role"] == "device"
            assert (
                relay_response["sender_principal_id"]
                == self.bootstrap.node_id
            )
            assert int(relay_response_key["delivery_seq"]) == int(
                relay_response["delivery_seq"]
            )
            response_delivery_seq = int(relay_response["delivery_seq"])
            assert int(streams["device"]["next_seq"]) == response_delivery_seq + 1
            assert int(streams["device"]["ack_cursor"]) == (
                response_delivery_seq
                if connector_outbox_removed
                else response_delivery_seq - 1
            )
            assert bytes(relay_response_key["fingerprint"]) == bytes(
                relay_response["fingerprint"]
            )
            relay_response_payload = json.loads(str(relay_response["payload_json"]))
            connector_frame = json.loads(
                str(_one(snapshot.connector_requests, "Connector response")["response_frame"])
            )
            assert relay_response_payload == connector_frame["payload"]
            assert relay_response_payload["result"]["threadId"] == command.thread_id
            assert (
                relay_response_payload["result"]["clientMessageId"]
                == command.client_message_id
            )
            assert relay_response_key["state"] == (
                "acked" if connector_outbox_removed else "pending"
            )
        else:
            assert snapshot.relay_response_deliveries == ()
            assert snapshot.relay_response_idempotency == ()

    async def sigkill_at(
        self,
        boundary: str,
        delivery: Any,
        command: Command,
    ) -> None:
        await self.stop_stack()
        worker = Path(__file__).with_name("production_crash_worker.py")
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repository / component)
            for component in ("connector", "gateway", "relay")
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        config = {
            "boundary": boundary,
            "gatewayDb": str(self.gateway_path),
            "bridgeState": str(self.bridge_path),
            "assetStagingDir": str(self.root / "assets"),
            "connectorDb": str(self.connector_path),
            "healthPath": str(self.root / "connector.health"),
            "pairingSecret": self.gateway_settings.pairing_secret,
            "connectorId": self.bootstrap.node_id,
            "relayToken": self.bootstrap.node_token,
            "deviceId": self.device_tokens.device_id,
            "requestId": command.request_id,
            "clientMessageId": command.client_message_id,
            "rawDelivery": _frame(delivery),
        }
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(config, separators=(",", ":")).encode("utf-8")
                ),
                timeout=5,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise AssertionError("crash worker did not reach its exact checkpoint")
        assert process.returncode == -signal.SIGKILL
        assert stdout == b""
        assert stderr == b""
        snapshot = self.snapshot_after_sigkill(command)
        self.assert_exact_sigkill_snapshot(
            snapshot,
            boundary=ExactSigkillBoundary(boundary),
            command=command,
            expected_delivery_seq=int(delivery.delivery_seq),
        )
        await self._open_stack()

    async def sigkill_connector_outbox_removal(
        self,
        response_message_id: str,
        command: Command,
        expected_delivery_seq: int,
    ) -> None:
        assert response_message_id == _response_message_id(command.request_id)
        await self.stop_stack()
        worker = Path(__file__).with_name("production_crash_worker.py")
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repository / component)
            for component in ("connector", "gateway", "relay")
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        config = {
            "boundary": "connector_outbox_removed",
            "gatewayDb": str(self.gateway_path),
            "bridgeState": str(self.bridge_path),
            "assetStagingDir": str(self.root / "assets"),
            "connectorDb": str(self.connector_path),
            "healthPath": str(self.root / "connector.health"),
            "pairingSecret": self.gateway_settings.pairing_secret,
            "connectorId": self.bootstrap.node_id,
            "relayToken": self.bootstrap.node_token,
            "deviceId": self.device_tokens.device_id,
            "responseMessageId": response_message_id,
        }
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(config, separators=(",", ":")).encode("utf-8")
                ),
                timeout=5,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise AssertionError(
                "Connector outbox crash worker missed its checkpoint"
            )
        assert process.returncode == -signal.SIGKILL
        assert stdout == b""
        assert stderr == b""
        snapshot = self.snapshot_after_sigkill(command)
        self.assert_exact_sigkill_snapshot(
            snapshot,
            boundary=ExactSigkillBoundary.CONNECTOR_OUTBOX_REMOVED,
            command=command,
            expected_delivery_seq=expected_delivery_seq,
        )
        await self._open_stack()

    async def sigkill_relay_command_commit(self, command: Command) -> None:
        worker = Path(__file__).with_name("production_crash_worker.py")
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repository / component)
            for component in ("connector", "gateway", "relay")
        )
        config = {
            "boundary": "relay_command_committed",
            "relayDb": str(self.root / "relay.sqlite3"),
            "gatewayDb": str(self.gateway_path),
            "bridgeState": str(self.bridge_path),
            "assetStagingDir": str(self.root / "assets"),
            "connectorDb": str(self.connector_path),
            "healthPath": str(self.root / "connector.health"),
            "deviceAccessToken": self.device_tokens.access_token,
            "messageId": command.message_id,
            "commandPayload": command.payload,
        }
        _ack_cursor, expected_delivery_seq = self.relay.stream_state(
            self.bootstrap.assistant_id,
            "node",
        )
        self.relay.close()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(config, separators=(",", ":")).encode("utf-8")
                ),
                timeout=5,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            stdout = b""
            stderr = b""
            raise AssertionError("Relay crash worker did not reach its exact checkpoint")
        assert process.returncode == -signal.SIGKILL
        assert stdout == b""
        assert stderr == b""
        snapshot = self.snapshot_after_sigkill(command)
        self.assert_exact_sigkill_snapshot(
            snapshot,
            boundary=ExactSigkillBoundary.RELAY_COMMAND_COMMITTED,
            command=command,
            expected_delivery_seq=expected_delivery_seq,
        )
        self.relay = RelayStore(self.relay_settings)
        self.device_principal = self.relay.authenticate(
            self.device_tokens.access_token,
            "device",
        )
        self.node_principal = self.relay.authenticate(
            self.bootstrap.node_token,
            "node",
        )

    async def sigkill_relay_response_commit(
        self,
        result: Any,
        command: Command,
        expected_delivery_seq: int,
    ) -> None:
        worker = Path(__file__).with_name("production_crash_worker.py")
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repository / component)
            for component in ("connector", "gateway", "relay")
        )
        response = json.loads(result.response_frame)
        config = {
            "boundary": "relay_response_committed",
            "relayDb": str(self.root / "relay.sqlite3"),
            "gatewayDb": str(self.gateway_path),
            "bridgeState": str(self.bridge_path),
            "assetStagingDir": str(self.root / "assets"),
            "connectorDb": str(self.connector_path),
            "healthPath": str(self.root / "connector.health"),
            "nodeToken": self.bootstrap.node_token,
            "nodeGeneration": self.node_generation,
            "responseMessageId": str(response["messageId"]),
            "responsePayload": response["payload"],
        }
        _ack_cursor, expected_response_seq = self.relay.stream_state(
            self.bootstrap.assistant_id,
            "device",
        )
        self.relay.close()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(config, separators=(",", ":")).encode("utf-8")
                ),
                timeout=5,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            stdout = b""
            stderr = b""
            raise AssertionError("Relay response crash worker missed its checkpoint")
        assert process.returncode == -signal.SIGKILL
        assert stdout == b""
        assert stderr == b""
        snapshot = self.snapshot_after_sigkill(command)
        self.assert_exact_sigkill_snapshot(
            snapshot,
            boundary=ExactSigkillBoundary.RELAY_RESPONSE_COMMITTED,
            command=command,
            expected_delivery_seq=expected_delivery_seq,
        )
        relay_response = _one(
            snapshot.relay_response_deliveries,
            "Relay response delivery",
        )
        assert int(relay_response["delivery_seq"]) == expected_response_seq
        self.relay = RelayStore(self.relay_settings)
        self.device_principal = self.relay.authenticate(
            self.device_tokens.access_token,
            "device",
        )
        self.node_principal = self.relay.authenticate(
            self.bootstrap.node_token,
            "node",
        )

    def restart_relay(self) -> None:
        self.relay.close()
        self.relay = RelayStore(self.relay_settings)
        self.device_principal = self.relay.authenticate(
            self.device_tokens.access_token, "device"
        )
        self.node_principal = self.relay.authenticate(
            self.bootstrap.node_token, "node"
        )
        assert self.relay.is_current_node_generation(
            self.bootstrap.node_id,
            self.node_generation,
        )

    async def new_thread(self, title: str) -> str:
        result = await self.backend.dispatch(
            "threads.create",
            ThreadsCreateParams(title=title),
        )
        return str(result["id"])

    def command(
        self,
        *,
        ordinal: int,
        thread_id: str,
        client_message_id: str,
        text: str,
    ) -> Command:
        request_id = _relay_id("req", ordinal)
        message_id = _relay_id("msg", ordinal)
        return Command(
            message_id=message_id,
            request_id=request_id,
            client_message_id=client_message_id,
            thread_id=thread_id,
            payload={
                "kind": "command",
                "requestId": request_id,
                "deviceId": self.device_tokens.device_id,
                "operation": "turns.start",
                "params": {
                    "threadId": thread_id,
                    "clientMessageId": client_message_id,
                    "input": [{"type": "text", "text": text}],
                },
            },
        )

    def submit(self, command: Command) -> Any:
        result = self.relay.enqueue(
            principal=self.device_principal,
            recipient_role="node",
            message_id=command.message_id,
            payload=command.payload,
            expires_at=None,
        )
        pending = {
            item.message_id: item
            for item in self.relay.pending_deliveries(
                self.bootstrap.assistant_id,
                "node",
            )
        }
        assert command.message_id in pending
        assert result.delivery_seq == pending[command.message_id].delivery_seq
        return pending[command.message_id]

    async def process(self, delivery: Any) -> tuple[Any, dict[str, Any]]:
        result = await self.engine.handle_delivery(_frame(delivery))
        body = json.loads(result.response_frame)
        return result, body

    def commit_response(
        self,
        result: Any,
        *,
        clean_connector_outbox: bool,
    ) -> dict[str, Any]:
        value = json.loads(result.response_frame)
        target_message_id = str(value["messageId"])
        pending_before_enqueue = self.relay.pending_deliveries(
            self.bootstrap.assistant_id,
            "device",
        )
        assert len(pending_before_enqueue) <= 1
        if pending_before_enqueue:
            pending_replay = pending_before_enqueue[0]
            assert pending_replay.message_id == target_message_id
            assert pending_replay.payload == value["payload"]
        accepted = self.relay.enqueue(
            principal=self.node_principal,
            recipient_role="device",
            message_id=target_message_id,
            payload=value["payload"],
            expires_at=None,
            node_generation=self.node_generation,
        )
        assert accepted.state in {"new", "pending"}
        # A cumulative ACK can hide an older orphan. Before consuming anything,
        # prove Android would see exactly this one response at this exact seq.
        pending_device = self.relay.pending_deliveries(
            self.bootstrap.assistant_id,
            "device",
        )
        assert len(pending_device) == 1
        pending_response = pending_device[0]
        assert pending_response.message_id == target_message_id
        assert pending_response.delivery_seq == accepted.delivery_seq
        assert pending_response.payload == value["payload"]
        device_ack_cursor, device_next_seq = self.relay.stream_state(
            self.bootstrap.assistant_id,
            "device",
        )
        assert accepted.delivery_seq == device_ack_cursor + 1
        assert device_next_seq == accepted.delivery_seq + 1

        acknowledged = self.relay.acknowledge(
            principal=self.device_principal,
            seq=accepted.delivery_seq,
        )
        assert acknowledged == accepted.delivery_seq
        assert self.relay.pending_deliveries(
            self.bootstrap.assistant_id,
            "device",
        ) == []
        self.relay.acknowledge(
            principal=self.node_principal,
            seq=result.ack_cursor,
            node_generation=self.node_generation,
        )
        if clean_connector_outbox:
            self.engine.mark_accepted(str(value["messageId"]))
        return value

    async def await_terminal(self, client_message_id: str) -> bool:
        row = self.store._conn.execute(
            "SELECT delivery_state FROM turns WHERE client_message_id = ?",
            (client_message_id,),
        ).fetchone()
        if row is not None and str(row["delivery_state"]) == "terminal":
            return True
        event = self._terminal_events.setdefault(
            client_message_id,
            asyncio.Event(),
        )
        try:
            await asyncio.wait_for(event.wait(), timeout=1)
        except asyncio.TimeoutError:
            return False
        return True

    def _install_terminal_observer(self) -> None:
        original = self.store.apply_thread_turn_events

        def observed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            turn_id = kwargs.get("turn_id")
            turn_status = kwargs.get("turn_status")
            if turn_id is not None and turn_status in {
                "completed",
                "failed",
                "interrupted",
            }:
                row = self.store.turn_by_public_id(str(turn_id))
                if row is not None:
                    self._terminal_events.setdefault(
                        str(row["client_message_id"]),
                        asyncio.Event(),
                    ).set()
            return result

        self.store.apply_thread_turn_events = observed  # type: ignore[method-assign]

    def matching_turn_count(self, client_message_id: str) -> int:
        return int(
            self.store._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()[0]
        )

    def bridge_turn_count(self, client_message_id: str) -> int:
        state = json.loads(self.bridge_path.read_text(encoding="utf-8"))
        return sum(
            1
            for turn in state.get("turns", {}).values()
            if turn.get("clientMessageId") == client_message_id
        )

    def assert_no_connector_orphans(self) -> None:
        assert self.state._conn.execute(
            "SELECT COUNT(*) FROM request_intents"
        ).fetchone()[0] == 0
        assert self.state._conn.execute(
            "SELECT COUNT(*) FROM processed_deliveries"
        ).fetchone()[0] == 0
        assert self.state._conn.execute(
            "SELECT COUNT(*) FROM outbox"
        ).fetchone()[0] == 0
        assert self.store._conn.execute(
            """
            SELECT COUNT(*) FROM turns
            WHERE delivery_state IN ('reserved', 'dispatching')
            """
        ).fetchone()[0] == 0
        assert self.relay.pending_deliveries(
            self.bootstrap.assistant_id,
            "device",
        ) == []
        assert self.relay.pending_deliveries(
            self.bootstrap.assistant_id,
            "node",
        ) == []


def _crash_after_sync(
    target: Any,
    method_name: str,
    matches: Callable[[tuple[Any, ...], dict[str, Any]], bool],
) -> None:
    original = getattr(target, method_name)
    fired = False

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        result = original(*args, **kwargs)
        if not fired and matches(args, kwargs):
            fired = True
            raise InjectedRestartCheckpoint(method_name)
        return result

    setattr(target, method_name, wrapped)


def _crash_after_async(
    target: Any,
    method_name: str,
    matches: Callable[[tuple[Any, ...], dict[str, Any]], bool],
) -> None:
    original = getattr(target, method_name)
    fired = False

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        result = await original(*args, **kwargs)
        if not fired and matches(args, kwargs):
            fired = True
            raise InjectedRestartCheckpoint(method_name)
        return result

    setattr(target, method_name, wrapped)


@pytest.mark.asyncio
async def test_commit_response_refuses_to_cumulatively_ack_a_hidden_delivery(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "cumulative-ack-fence")
    await harness.start()
    try:
        thread_a = await harness.new_thread("ACK fence A")
        thread_b = await harness.new_thread("ACK fence B")
        first = harness.command(
            ordinal=90_900_001,
            thread_id=thread_a,
            client_message_id="ack-fence-A1",
            text="first",
        )
        second = harness.command(
            ordinal=90_900_002,
            thread_id=thread_b,
            client_message_id="ack-fence-B1",
            text="second",
        )
        first_delivery = harness.submit(first)
        second_delivery = harness.submit(second)
        first_result, first_body = await harness.process(first_delivery)
        second_result, second_body = await harness.process(second_delivery)
        assert first_body["payload"]["ok"] is True
        assert second_body["payload"]["ok"] is True

        first_frame = json.loads(first_result.response_frame)
        first_relay_result = harness.relay.enqueue(
            principal=harness.node_principal,
            recipient_role="device",
            message_id=str(first_frame["messageId"]),
            payload=first_frame["payload"],
            expires_at=None,
            node_generation=harness.node_generation,
        )
        device_cursor_before, _next_seq = harness.relay.stream_state(
            harness.bootstrap.assistant_id,
            "device",
        )

        with pytest.raises(AssertionError):
            harness.commit_response(
                second_result,
                clean_connector_outbox=True,
            )

        device_cursor_after, _next_seq = harness.relay.stream_state(
            harness.bootstrap.assistant_id,
            "device",
        )
        assert device_cursor_after == device_cursor_before
        pending = harness.relay.pending_deliveries(
            harness.bootstrap.assistant_id,
            "device",
        )
        assert [
            (item.delivery_seq, item.message_id)
            for item in pending
        ] == [(first_relay_result.delivery_seq, str(first_frame["messageId"]))]
        second_message_id = str(json.loads(second_result.response_frame)["messageId"])
        assert harness.relay._db.execute(
            """
            SELECT COUNT(*) FROM deliveries
            WHERE assistant_id = ? AND recipient_role = 'device'
                AND message_id = ?
            """,
            (harness.bootstrap.assistant_id, second_message_id),
        ).fetchone()[0] == 0
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_live_reconnect_continues_an_exact_reserved_turn_without_gateway_restart(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "live-reserved-reconnect")
    await harness.start()
    original_reserve = harness.store.reserve_turn
    try:
        thread_id = await harness.new_thread("Live reserved reconnect")
        command = harness.command(
            ordinal=91_000_001,
            thread_id=thread_id,
            client_message_id="live-reserved-reconnect-A1",
            text="live-reserved-reconnect",
        )
        delivery = harness.submit(command)
        cancelled = False

        def cancel_after_reservation(*args: Any, **kwargs: Any) -> Any:
            nonlocal cancelled
            result = original_reserve(*args, **kwargs)
            candidate = args[2] if len(args) >= 3 else kwargs.get("client_message_id")
            if (
                not cancelled
                and candidate == command.client_message_id
                and result.created
            ):
                cancelled = True
                raise asyncio.CancelledError()
            return result

        harness.store.reserve_turn = cancel_after_reservation  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await harness.process(delivery)
        assert cancelled
        row = harness.store.turn_by_client_message(
            harness.device_tokens.device_id,
            thread_id,
            command.client_message_id,
        )
        assert row is not None
        assert (row["status"], row["delivery_state"], row["raw_id"]) == (
            "pending",
            "reserved",
            None,
        )
        assert harness.bridge_turn_count(command.client_message_id) == 0

        # Relay reconnect re-delivers on the same process-local Gateway. This is
        # the production path that an orderly Gateway restart cannot represent.
        harness.store.reserve_turn = original_reserve  # type: ignore[method-assign]
        result, body = await harness.process(delivery)
        assert body["payload"]["ok"] is True
        assert body["payload"]["result"]["threadId"] == thread_id
        assert (
            body["payload"]["result"]["clientMessageId"]
            == command.client_message_id
        )
        harness.commit_response(result, clean_connector_outbox=True)
        assert await harness.await_terminal(command.client_message_id)
        assert harness.matching_turn_count(command.client_message_id) == 1
        assert harness.bridge_turn_count(command.client_message_id) == 1
        harness.assert_no_connector_orphans()
    finally:
        if harness._stack_open:
            harness.store.reserve_turn = original_reserve  # type: ignore[method-assign]
        await harness.close()


@pytest.mark.asyncio
async def test_live_reconnect_fences_dispatching_without_gateway_restart(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "live-dispatching-reconnect")
    await harness.start()
    original_dispatching = harness.store.mark_turn_dispatching
    try:
        thread_id = await harness.new_thread("Live dispatching reconnect")
        command = harness.command(
            ordinal=92_000_001,
            thread_id=thread_id,
            client_message_id="live-dispatching-reconnect-A1",
            text="live-dispatching-reconnect",
        )
        delivery = harness.submit(command)
        cancelled = False

        def cancel_after_dispatching(turn_id: str) -> bool:
            nonlocal cancelled
            result = original_dispatching(turn_id)
            if not cancelled and result:
                cancelled = True
                raise asyncio.CancelledError()
            return result

        harness.store.mark_turn_dispatching = cancel_after_dispatching  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await harness.process(delivery)
        assert cancelled
        row = harness.store.turn_by_client_message(
            harness.device_tokens.device_id,
            thread_id,
            command.client_message_id,
        )
        assert row is not None
        assert (row["status"], row["delivery_state"], row["raw_id"]) == (
            "dispatching",
            "dispatching",
            None,
        )
        assert harness.bridge_turn_count(command.client_message_id) == 0

        harness.store.mark_turn_dispatching = original_dispatching  # type: ignore[method-assign]
        result, body = await harness.process(delivery)
        assert body["payload"]["ok"] is False
        assert (
            body["payload"]["error"]["code"]
            == "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED"
        )
        harness.commit_response(result, clean_connector_outbox=True)
        row = harness.store.turn_by_client_message(
            harness.device_tokens.device_id,
            thread_id,
            command.client_message_id,
        )
        assert row is not None
        assert row["delivery_state"] == "ambiguous"
        assert harness.store.thread_has_ambiguous_turns(thread_id)
        assert harness.matching_turn_count(command.client_message_id) == 1
        assert harness.bridge_turn_count(command.client_message_id) == 0
        harness.assert_no_connector_orphans()
    finally:
        if harness._stack_open:
            harness.store.mark_turn_dispatching = original_dispatching  # type: ignore[method-assign]
        await harness.close()


@pytest.mark.asyncio
async def test_real_connector_timeout_releases_pre_dispatch_reservation(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(
        tmp_path / "request-timeout-reserved",
        request_timeout_seconds=0.2,
    )
    await harness.start()
    original_resume = harness.bridge.resume_thread
    try:
        thread_id = await harness.new_thread("Timeout before dispatch")
        first = harness.command(
            ordinal=92_100_001,
            thread_id=thread_id,
            client_message_id="timeout-before-dispatch-A1",
            text="timeout-before-dispatch",
        )
        first_delivery = harness.submit(first)

        async def block_resume(_raw_thread_id: str) -> None:
            await asyncio.Event().wait()

        harness.bridge.resume_thread = block_resume  # type: ignore[method-assign]
        result, body = await harness.process(first_delivery)
        assert body["payload"]["ok"] is False
        assert body["payload"]["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
        harness.commit_response(result, clean_connector_outbox=True)
        row = harness.store.turn_by_client_message(
            harness.device_tokens.device_id,
            thread_id,
            first.client_message_id,
        )
        assert row is not None
        assert (row["status"], row["delivery_state"], row["raw_id"]) == (
            "retryable",
            "notAccepted",
            None,
        )
        assert not harness.store.thread_has_ambiguous_turns(thread_id)
        assert harness.bridge_turn_count(first.client_message_id) == 0

        harness.bridge.resume_thread = original_resume  # type: ignore[method-assign]
        second = harness.command(
            ordinal=92_100_002,
            thread_id=thread_id,
            client_message_id="timeout-before-dispatch-A2",
            text="thread-remains-usable",
        )
        second_delivery = harness.submit(second)
        second_result, second_body = await harness.process(second_delivery)
        assert second_body["payload"]["ok"] is True
        harness.commit_response(second_result, clean_connector_outbox=True)
        assert await harness.await_terminal(second.client_message_id)
        assert harness.bridge_turn_count(second.client_message_id) == 1
        harness.assert_no_connector_orphans()
    finally:
        if harness._stack_open:
            harness.bridge.resume_thread = original_resume  # type: ignore[method-assign]
        await harness.close()


@pytest.mark.asyncio
async def test_real_connector_timeout_fences_post_dispatch_reservation(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(
        tmp_path / "request-timeout-dispatching",
        request_timeout_seconds=0.2,
    )
    await harness.start()
    original_start = harness.bridge.start_turn
    try:
        thread_id = await harness.new_thread("Timeout after dispatch")
        first = harness.command(
            ordinal=92_200_001,
            thread_id=thread_id,
            client_message_id="timeout-after-dispatch-A1",
            text="timeout-after-dispatch",
        )
        first_delivery = harness.submit(first)

        async def block_start(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.Event().wait()

        harness.bridge.start_turn = block_start  # type: ignore[method-assign]
        result, body = await harness.process(first_delivery)
        assert body["payload"]["ok"] is False
        assert body["payload"]["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
        harness.commit_response(result, clean_connector_outbox=True)
        row = harness.store.turn_by_client_message(
            harness.device_tokens.device_id,
            thread_id,
            first.client_message_id,
        )
        assert row is not None
        assert (row["status"], row["delivery_state"], row["raw_id"]) == (
            "ambiguous",
            "ambiguous",
            None,
        )
        assert harness.store.thread_has_ambiguous_turns(thread_id)
        assert harness.bridge_turn_count(first.client_message_id) == 0

        harness.bridge.start_turn = original_start  # type: ignore[method-assign]
        second = harness.command(
            ordinal=92_200_002,
            thread_id=thread_id,
            client_message_id="timeout-after-dispatch-A2",
            text="must-not-bypass-ambiguity",
        )
        second_delivery = harness.submit(second)
        second_result, second_body = await harness.process(second_delivery)
        assert second_body["payload"]["ok"] is False
        assert (
            second_body["payload"]["error"]["code"]
            == "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED"
        )
        harness.commit_response(second_result, clean_connector_outbox=True)
        assert harness.matching_turn_count(second.client_message_id) == 0
        assert harness.bridge_turn_count(second.client_message_id) == 0
        harness.assert_no_connector_orphans()
    finally:
        if harness._stack_open:
            harness.bridge.start_turn = original_start  # type: ignore[method-assign]
        await harness.close()


@pytest.mark.asyncio
async def test_relay_command_commit_is_sigkilled_and_recovered_twenty_five_times(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "relay-command-commit-sigkill")
    await harness.start()
    try:
        completed_iterations = 0
        for iteration in builtins.range(25):
            run_token = hashlib.sha256(
                f"sigkill:relay-command:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            thread_a = await harness.new_thread(f"Relay crash A {run_token}")
            thread_b = await harness.new_thread(f"Relay crash B {run_token}")
            ordinal = 93_000_000 + iteration * 10
            a1 = harness.command(
                ordinal=ordinal + 1,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A1",
                text=f"{run_token}:A1",
            )
            a2 = harness.command(
                ordinal=ordinal + 2,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A2",
                text=f"{run_token}:A2",
            )
            b1 = harness.command(
                ordinal=ordinal + 3,
                thread_id=thread_b,
                client_message_id=f"{run_token}-B1",
                text=f"{run_token}:B1",
            )

            await harness.sigkill_relay_command_commit(a1)
            # The mobile retry uses the exact same Relay messageId. It must
            # resolve to the one row committed immediately before SIGKILL.
            a1_delivery = harness.submit(a1)
            a2_delivery = harness.submit(a2)
            b1_delivery = harness.submit(b1)
            assert (
                harness.relay._db.execute(
                    """
                    SELECT COUNT(*) FROM deliveries
                    WHERE assistant_id = ? AND recipient_role = 'node'
                        AND message_id = ?
                    """,
                    (harness.bootstrap.assistant_id, a1.message_id),
                ).fetchone()[0]
                == 1
            )

            b_result, b_body = await harness.process(b1_delivery)
            assert b_body["payload"]["ok"] is True
            harness.commit_response(b_result, clean_connector_outbox=True)
            assert await harness.await_terminal(b1.client_message_id)

            a1_result, a1_body = await harness.process(a1_delivery)
            assert a1_body["payload"]["ok"] is True
            harness.commit_response(a1_result, clean_connector_outbox=True)
            assert await harness.await_terminal(a1.client_message_id)

            a2_result, a2_body = await harness.process(a2_delivery)
            assert a2_body["payload"]["ok"] is True
            harness.commit_response(a2_result, clean_connector_outbox=True)
            assert await harness.await_terminal(a2.client_message_id)

            assert harness.matching_turn_count(a1.client_message_id) == 1
            assert harness.matching_turn_count(a2.client_message_id) == 1
            assert harness.matching_turn_count(b1.client_message_id) == 1
            assert harness.bridge_turn_count(a1.client_message_id) == 1
            assert harness.bridge_turn_count(a2.client_message_id) == 1
            assert harness.bridge_turn_count(b1.client_message_id) == 1
            harness.assert_no_connector_orphans()
            completed_iterations += 1
        assert completed_iterations == 25
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_relay_response_commit_is_sigkilled_and_replayed_twenty_five_times(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "relay-response-commit-sigkill")
    await harness.start()
    try:
        completed_iterations = 0
        for iteration in builtins.range(25):
            run_token = hashlib.sha256(
                f"sigkill:relay-response:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            thread_a = await harness.new_thread(f"Relay response A {run_token}")
            thread_b = await harness.new_thread(f"Relay response B {run_token}")
            ordinal = 94_000_000 + iteration * 10
            a1 = harness.command(
                ordinal=ordinal + 1,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A1",
                text=f"{run_token}:A1",
            )
            a2 = harness.command(
                ordinal=ordinal + 2,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A2",
                text=f"{run_token}:A2",
            )
            b1 = harness.command(
                ordinal=ordinal + 3,
                thread_id=thread_b,
                client_message_id=f"{run_token}-B1",
                text=f"{run_token}:B1",
            )
            a1_delivery = harness.submit(a1)
            a2_delivery = harness.submit(a2)
            b1_delivery = harness.submit(b1)

            b_result, b_body = await harness.process(b1_delivery)
            assert b_body["payload"]["ok"] is True
            harness.commit_response(b_result, clean_connector_outbox=True)
            assert await harness.await_terminal(b1.client_message_id)

            a1_result, a1_body = await harness.process(a1_delivery)
            assert a1_body["payload"]["ok"] is True
            await harness.sigkill_relay_response_commit(
                a1_result,
                a1,
                int(a1_delivery.delivery_seq),
            )
            response = json.loads(a1_result.response_frame)
            pending_device = harness.relay.pending_deliveries(
                harness.bootstrap.assistant_id,
                "device",
            )
            assert [
                delivery.message_id for delivery in pending_device
            ] == [response["messageId"]]
            # Connector reconnect repeats the stable response messageId. Relay
            # returns the committed row; Android consumes it once.
            harness.commit_response(a1_result, clean_connector_outbox=True)
            assert await harness.await_terminal(a1.client_message_id)

            a2_result, a2_body = await harness.process(a2_delivery)
            assert a2_body["payload"]["ok"] is True
            harness.commit_response(a2_result, clean_connector_outbox=True)
            assert await harness.await_terminal(a2.client_message_id)

            assert harness.matching_turn_count(a1.client_message_id) == 1
            assert harness.matching_turn_count(a2.client_message_id) == 1
            assert harness.matching_turn_count(b1.client_message_id) == 1
            assert harness.bridge_turn_count(a1.client_message_id) == 1
            assert harness.bridge_turn_count(a2.client_message_id) == 1
            assert harness.bridge_turn_count(b1.client_message_id) == 1
            harness.assert_no_connector_orphans()
            completed_iterations += 1
        assert completed_iterations == 25
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_connector_outbox_removal_is_sigkilled_and_durable_twenty_five_times(
    tmp_path: Path,
) -> None:
    harness = ProductionCrashHarness(tmp_path / "connector-outbox-remove-sigkill")
    await harness.start()
    try:
        completed_iterations = 0
        for iteration in builtins.range(25):
            run_token = hashlib.sha256(
                f"sigkill:connector-outbox:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            thread_a = await harness.new_thread(f"Connector outbox A {run_token}")
            thread_b = await harness.new_thread(f"Connector outbox B {run_token}")
            ordinal = 95_000_000 + iteration * 10
            a1 = harness.command(
                ordinal=ordinal + 1,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A1",
                text=f"{run_token}:A1",
            )
            a2 = harness.command(
                ordinal=ordinal + 2,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A2",
                text=f"{run_token}:A2",
            )
            b1 = harness.command(
                ordinal=ordinal + 3,
                thread_id=thread_b,
                client_message_id=f"{run_token}-B1",
                text=f"{run_token}:B1",
            )
            a1_delivery = harness.submit(a1)
            a2_delivery = harness.submit(a2)
            b1_delivery = harness.submit(b1)

            b_result, b_body = await harness.process(b1_delivery)
            assert b_body["payload"]["ok"] is True
            harness.commit_response(b_result, clean_connector_outbox=True)
            assert await harness.await_terminal(b1.client_message_id)

            a1_result, a1_body = await harness.process(a1_delivery)
            assert a1_body["payload"]["ok"] is True
            response = harness.commit_response(
                a1_result,
                clean_connector_outbox=False,
            )
            response_message_id = str(response["messageId"])
            assert [
                message_id
                for message_id, _frame_value in harness.engine.pending_outbox()
            ] == [response_message_id]
            await harness.sigkill_connector_outbox_removal(
                response_message_id,
                a1,
                int(a1_delivery.delivery_seq),
            )
            assert harness.engine.pending_outbox() == []
            assert await harness.await_terminal(a1.client_message_id)

            a2_result, a2_body = await harness.process(a2_delivery)
            assert a2_body["payload"]["ok"] is True
            harness.commit_response(a2_result, clean_connector_outbox=True)
            assert await harness.await_terminal(a2.client_message_id)

            assert harness.matching_turn_count(a1.client_message_id) == 1
            assert harness.matching_turn_count(a2.client_message_id) == 1
            assert harness.matching_turn_count(b1.client_message_id) == 1
            assert harness.bridge_turn_count(a1.client_message_id) == 1
            assert harness.bridge_turn_count(a2.client_message_id) == 1
            assert harness.bridge_turn_count(b1.client_message_id) == 1
            harness.assert_no_connector_orphans()
            completed_iterations += 1
        assert completed_iterations == 25
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", tuple(ServerCrashBoundary))
async def test_production_store_reconstruction_matrix_runs_twenty_five_times(
    tmp_path: Path,
    boundary: ServerCrashBoundary,
) -> None:
    harness = ProductionCrashHarness(tmp_path / boundary.value)
    await harness.start()
    try:
        for iteration in builtins.range(25):
            run_token = hashlib.sha256(
                f"{boundary.value}:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            thread_a = await harness.new_thread(f"A-{run_token}")
            thread_b = await harness.new_thread(f"B-{run_token}")
            base = (list(ServerCrashBoundary).index(boundary) + 1) * 1_000_000
            ordinal = base + iteration * 10
            a1 = harness.command(
                ordinal=ordinal + 1,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A1",
                text=f"{run_token}:A1",
            )
            a2 = harness.command(
                ordinal=ordinal + 2,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A2",
                text=f"{run_token}:A2",
            )
            b1 = harness.command(
                ordinal=ordinal + 3,
                thread_id=thread_b,
                client_message_id=f"{run_token}-B1",
                text=f"{run_token}:B1",
            )

            if boundary == ServerCrashBoundary.TRANSPORT_WRITE_UNCONFIRMED:
                # The byte write has no durable server mutation. Recovery repeats
                # the same messageId into RelayStore's production idempotency key.
                harness.restart_relay()

            if boundary == ServerCrashBoundary.RELAY_COMMAND_COMMITTED:
                _crash_after_sync(
                    harness.relay,
                    "enqueue",
                    lambda _args, kwargs: kwargs.get("message_id") == a1.message_id,
                )

            try:
                a1_delivery = harness.submit(a1)
            except InjectedRestartCheckpoint:
                harness.restart_relay()
                a1_delivery = harness.submit(a1)

            if boundary == ServerCrashBoundary.RELAY_COMMAND_ACCEPTED_TRANSPORT:
                # The accepted frame is transport-only; the committed Relay row
                # is the recovery authority.
                harness.restart_relay()
                a1_delivery = harness.submit(a1)

            a2_delivery = harness.submit(a2)
            b1_delivery = harness.submit(b1)

            if boundary == ServerCrashBoundary.CONNECTOR_EXECUTION_INTENT_COMMITTED:
                _crash_after_sync(
                    harness.state,
                    "mark_execution_started",
                    lambda args, _kwargs: args[0] == a1.request_id,
                )
            elif boundary == ServerCrashBoundary.GATEWAY_TURN_RESERVED:
                _crash_after_sync(
                    harness.store,
                    "reserve_turn",
                    lambda args, _kwargs: args[2] == a1.client_message_id,
                )
            elif boundary == ServerCrashBoundary.SCRIPTED_CODEX_TURN_PERSISTED:
                _crash_after_async(
                    harness.bridge,
                    "start_turn",
                    lambda args, _kwargs: args[1] == a1.client_message_id,
                )

            crashed = boundary in {
                ServerCrashBoundary.CONNECTOR_EXECUTION_INTENT_COMMITTED,
                ServerCrashBoundary.GATEWAY_TURN_RESERVED,
                ServerCrashBoundary.SCRIPTED_CODEX_TURN_PERSISTED,
            }
            if crashed:
                with pytest.raises(InjectedRestartCheckpoint):
                    await harness.process(a1_delivery)
                await harness.restart_stack()

            # Delivery sequence three is processed while one/two are missing.
            # The production cursor stays behind the gap, but Thread B completes.
            b_result, b_body = await harness.process(b1_delivery)
            assert b_body["payload"]["ok"] is True
            harness.commit_response(b_result, clean_connector_outbox=True)
            assert await harness.await_terminal(b1.client_message_id)
            assert harness.bridge_turn_count(b1.client_message_id) == 1

            a_result, a_body = await harness.process(a1_delivery)
            harness.commit_response(
                a_result,
                clean_connector_outbox=boundary
                not in {
                    ServerCrashBoundary.RELAY_RESPONSE_COMMITTED,
                    ServerCrashBoundary.CONNECTOR_RESPONSE_OUTBOX_REMOVED,
                },
            )

            if boundary == ServerCrashBoundary.RELAY_RESPONSE_COMMITTED:
                # This synthetic reconstruction has already consumed the Relay
                # delivery. Reopening may rebuild the exact Connector response,
                # but must not manufacture a second Android delivery.
                await harness.restart_stack()
                replay_result, replay_body = await harness.process(a1_delivery)
                assert replay_body == a_body
                replay_id = str(json.loads(replay_result.response_frame)["messageId"])
                assert [
                    message_id
                    for message_id, _frame_value in harness.engine.pending_outbox()
                ] == [replay_id]
                harness.engine.mark_accepted(replay_id)
            elif boundary == ServerCrashBoundary.GATEWAY_TERMINAL_COMMITTED:
                assert await harness.await_terminal(a1.client_message_id)
                await harness.restart_stack()
            elif boundary == ServerCrashBoundary.CONNECTOR_RESPONSE_OUTBOX_REMOVED:
                response_id = str(json.loads(a_result.response_frame)["messageId"])
                harness.engine.mark_accepted(response_id)
                await harness.restart_stack()

            assert harness.matching_turn_count(a1.client_message_id) == 1
            assert harness.bridge_turn_count(a1.client_message_id) == 1
            assert a_body["payload"]["ok"] or (
                a_body["payload"]["error"]["code"]
                == "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED"
            )

            a1_row = harness.store._conn.execute(
                """
                SELECT delivery_state FROM turns
                WHERE thread_public_id = ? AND client_message_id = ?
                """,
                (thread_a, a1.client_message_id),
            ).fetchone()
            assert a1_row is not None
            if str(a1_row["delivery_state"]) != "ambiguous":
                assert await harness.await_terminal(a1.client_message_id)

            a2_result, a2_body = await harness.process(a2_delivery)
            harness.commit_response(a2_result, clean_connector_outbox=True)
            if str(a1_row["delivery_state"]) == "ambiguous":
                assert a2_body["payload"]["ok"] is False
                assert a2_body["payload"]["error"]["code"] in {
                    "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                    "THREAD_BUSY",
                }
                assert harness.bridge_turn_count(a2.client_message_id) == 0
            else:
                assert a2_body["payload"]["ok"] is True
                assert await harness.await_terminal(a2.client_message_id)
                assert harness.bridge_turn_count(a2.client_message_id) == 1

            # Four durable owners agree on the exact client business identity.
            relay_row = harness.relay._db.execute(
                """
                SELECT payload_json FROM deliveries
                WHERE assistant_id = ? AND recipient_role = 'node'
                    AND message_id = ?
                """,
                (harness.bootstrap.assistant_id, a1.message_id),
            ).fetchone()
            assert relay_row is not None
            relay_payload = json.loads(relay_row["payload_json"])
            connector_response = harness.state._conn.execute(
                "SELECT response_frame FROM requests WHERE request_id = ?",
                (a1.request_id,),
            ).fetchone()
            gateway_turn = harness.store._conn.execute(
                """
                SELECT public_id, raw_id, thread_public_id, client_message_id,
                       status, delivery_state
                FROM turns WHERE client_message_id = ?
                """,
                (a1.client_message_id,),
            ).fetchone()
            bridge_state = json.loads(harness.bridge_path.read_text(encoding="utf-8"))
            bridge_turn_id, bridge_turn = next(
                (turn_id, turn)
                for turn_id, turn in bridge_state["turns"].items()
                if turn["clientMessageId"] == a1.client_message_id
            )
            assert relay_payload["deviceId"] == harness.device_tokens.device_id
            assert relay_payload["params"]["threadId"] == thread_a
            assert relay_payload["params"]["clientMessageId"] == a1.client_message_id
            assert connector_response is not None
            connector_payload = json.loads(
                connector_response["response_frame"]
            )["payload"]
            assert connector_payload["requestId"] == a1.request_id
            assert connector_payload["deviceId"] == harness.device_tokens.device_id
            assert connector_payload["operation"] == "turns.start"
            assert gateway_turn["thread_public_id"] == thread_a
            assert gateway_turn["client_message_id"] == a1.client_message_id
            raw_thread_a = harness.store.thread_raw_id(thread_a)
            assert raw_thread_a is not None
            assert bridge_turn_id == bridge_turn["id"]
            assert bridge_turn["threadId"] == raw_thread_a
            assert bridge_turn["clientMessageId"] == a1.client_message_id
            if connector_payload["ok"]:
                result_payload = connector_payload["result"]
                assert result_payload["id"] == gateway_turn["public_id"]
                assert result_payload["threadId"] == thread_a
                assert (
                    result_payload["clientMessageId"]
                    == a1.client_message_id
                )
                assert gateway_turn["raw_id"] == bridge_turn_id
            else:
                assert (
                    connector_payload["error"]["code"]
                    == "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED"
                )
                assert gateway_turn["delivery_state"] == "ambiguous"
            assert harness.relay.is_current_node_generation(
                harness.bootstrap.node_id,
                harness.node_generation,
            )
            relay_states = harness.relay._db.execute(
                """
                SELECT state FROM message_idempotency
                WHERE assistant_id = ? AND sender_role = 'device'
                    AND message_id IN (?, ?, ?)
                ORDER BY message_id
                """,
                (
                    harness.bootstrap.assistant_id,
                    a1.message_id,
                    a2.message_id,
                    b1.message_id,
                ),
            ).fetchall()
            assert len(relay_states) == 3
            assert {str(row["state"]) for row in relay_states} == {"acked"}
            harness.assert_no_connector_orphans()
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", SIGKILL_BOUNDARIES)
async def test_production_subprocess_is_sigkilled_at_committed_checkpoint_25_times(
    tmp_path: Path,
    boundary: str,
) -> None:
    harness = ProductionCrashHarness(tmp_path / boundary)
    await harness.start()
    try:
        completed_iterations = 0
        for iteration in builtins.range(25):
            run_token = hashlib.sha256(
                f"sigkill:{boundary}:{iteration}".encode("ascii")
            ).hexdigest()[:32]
            thread_a = await harness.new_thread(f"SIGKILL-A-{run_token}")
            thread_b = await harness.new_thread(f"SIGKILL-B-{run_token}")
            base = (SIGKILL_BOUNDARIES.index(boundary) + 20) * 1_000_000
            ordinal = base + iteration * 10
            a1 = harness.command(
                ordinal=ordinal + 1,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A1",
                text=f"{run_token}:A1",
            )
            a2 = harness.command(
                ordinal=ordinal + 2,
                thread_id=thread_a,
                client_message_id=f"{run_token}-A2",
                text=f"{run_token}:A2",
            )
            b1 = harness.command(
                ordinal=ordinal + 3,
                thread_id=thread_b,
                client_message_id=f"{run_token}-B1",
                text=f"{run_token}:B1",
            )
            a1_delivery = harness.submit(a1)
            a2_delivery = harness.submit(a2)
            b1_delivery = harness.submit(b1)

            await harness.sigkill_at(boundary, a1_delivery, a1)

            # B1 crosses the missing delivery cursor and completes before A1
            # recovery. A2 has not reached Codex.
            b_result, b_body = await harness.process(b1_delivery)
            assert b_body["payload"]["ok"] is True
            harness.commit_response(b_result, clean_connector_outbox=True)
            assert await harness.await_terminal(b1.client_message_id)
            assert harness.bridge_turn_count(b1.client_message_id) == 1
            assert harness.bridge_turn_count(a2.client_message_id) == 0

            a_result, a_body = await harness.process(a1_delivery)
            harness.commit_response(a_result, clean_connector_outbox=True)
            assert harness.matching_turn_count(a1.client_message_id) == 1
            assert harness.bridge_turn_count(a1.client_message_id) == 1
            assert a_body["payload"]["ok"] or (
                a_body["payload"]["error"]["code"]
                == "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED"
            )
            a1_row = harness.store._conn.execute(
                """
                SELECT delivery_state FROM turns
                WHERE thread_public_id = ? AND client_message_id = ?
                """,
                (thread_a, a1.client_message_id),
            ).fetchone()
            assert a1_row is not None
            if str(a1_row["delivery_state"]) != "ambiguous":
                assert await harness.await_terminal(a1.client_message_id)

            a2_result, a2_body = await harness.process(a2_delivery)
            harness.commit_response(a2_result, clean_connector_outbox=True)
            if str(a1_row["delivery_state"]) == "ambiguous":
                assert a2_body["payload"]["ok"] is False
                assert a2_body["payload"]["error"]["code"] in {
                    "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                    "THREAD_BUSY",
                }
                assert harness.bridge_turn_count(a2.client_message_id) == 0
            else:
                assert a2_body["payload"]["ok"] is True
                assert await harness.await_terminal(a2.client_message_id)
                assert harness.bridge_turn_count(a2.client_message_id) == 1

            bridge_state = json.loads(harness.bridge_path.read_text(encoding="utf-8"))
            raw_thread_a = harness.store.thread_raw_id(thread_a)
            assert raw_thread_a is not None
            a_client_ids = [
                str(turn["clientMessageId"])
                for turn in bridge_state["threads"][raw_thread_a]["turns"]
                if str(turn.get("clientMessageId", "")).startswith(run_token)
            ]
            expected_a_ids = [a1.client_message_id]
            if a2_body["payload"]["ok"]:
                expected_a_ids.append(a2.client_message_id)
            assert a_client_ids == expected_a_ids
            harness.assert_no_connector_orphans()
            completed_iterations += 1
        assert completed_iterations == 25
    finally:
        await harness.close()
