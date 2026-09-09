from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from cheby_connector.config import ConnectorSettings
from cheby_connector.engine import (
    ConnectorEngine,
    ProtocolViolation,
    _KeyedLocks,
    _operation_lane,
)
from cheby_connector.gateway import DirectGatewayBackend
from cheby_connector.health import run_health_watchdog
from cheby_connector.models import AssetsUploadParams, DeliveredMessage
from cheby_connector.relay import RelayConnector, _LaneSequencer, _SingleInFlight
from cheby_connector.state import ConnectorState, OutboxFull


NODE_ID = "node_" + "N" * 22
DEVICE_ID = "dev_" + "D" * 22
OTHER_DEVICE_ID = "dev_" + "E" * 22
MESSAGE_1 = "msg_" + "A" * 22
MESSAGE_2 = "msg_" + "B" * 22
MESSAGE_3 = "msg_" + "C" * 22
REQUEST_1 = "req_" + "R" * 22
REQUEST_2 = "req_" + "S" * 22
REQUEST_3 = "req_" + "T" * 22
MESSAGE_4 = "msg_" + "F" * 22
REQUEST_4 = "req_" + "U" * 22
MESSAGE_5 = "msg_" + "G" * 22
REQUEST_5 = "req_" + "V" * 22
MESSAGE_6 = "msg_" + "H" * 22
REQUEST_6 = "req_" + "W" * 22


def settings(tmp_path: Path, **updates: Any) -> ConnectorSettings:
    values = dict(
        relay_url="wss://relay.example/relay/v1/node",
        connector_id=NODE_ID,
        relay_token="node-secret-for-test",
        state_path=str(tmp_path / "state" / "connector.sqlite3"),
        request_timeout_seconds=0.2,
    )
    values.update(updates)
    return ConnectorSettings(**values)


def delivery(
    *,
    seq: int = 1,
    message_id: str = MESSAGE_1,
    request_id: str = REQUEST_1,
    device_id: str = DEVICE_ID,
    operation: str = "threads.create",
    params: Optional[dict[str, Any]] = None,
) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": "delivery",
            "deliverySeq": seq,
            "messageId": message_id,
            "payload": {
                "kind": "command",
                "requestId": request_id,
                "deviceId": device_id,
                "operation": operation,
                "params": {"title": "New"} if params is None else params,
            },
        },
        separators=(",", ":"),
    )


@dataclass
class FakeEvent:
    stream_id: str
    event_id: str
    seq: int
    occurred_at: str
    type: str
    thread_id: str
    turn_id: Optional[str]
    item_id: Optional[str]
    payload: dict[str, Any]


class FakeBackend:
    def __init__(self, calls: Optional[list[tuple[str, object]]] = None) -> None:
        self.device_id: Optional[str] = None
        self.stream_id: Optional[str] = None
        self.calls = [] if calls is None else calls
        self.delay = 0.0
        self.result: dict[str, Any] = {"id": "thr_public", "title": "New"}
        self.events: list[FakeEvent] = []
        self.closed = False

    async def bind_device(self, device_id: str) -> None:
        if self.device_id is not None and self.device_id != device_id:
            raise ValueError("different device")
        self.device_id = device_id
        self.stream_id = "stream_public"

    async def dispatch(self, operation: str, params: object) -> dict[str, Any]:
        self.calls.append((operation, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        if operation == "events.subscribe":
            return {"streamId": self.stream_id, "currentSeq": 0, "syncRequired": False}
        return self.result

    def replay_events(self, after_seq: int) -> list[FakeEvent]:
        return [event for event in self.events if event.seq > after_seq]

    async def close(self) -> None:
        self.closed = True


def test_config_requires_secure_or_literal_loopback_node_identity(tmp_path: Path) -> None:
    valid = settings(tmp_path)
    assert valid.relay_url.startswith("wss://")
    assert settings(
        tmp_path,
        relay_url="ws://127.0.0.1:18080/relay/v1/node",
    ).relay_url.startswith("ws://127.0.0.1")
    assert settings(
        tmp_path,
        relay_url="ws://[::1]:18080/relay/v1/node",
    ).relay_url.startswith("ws://[::1]")
    with pytest.raises(ValueError):
        settings(tmp_path, relay_url="ws://relay.example/relay/v1/node")
    with pytest.raises(ValueError):
        settings(tmp_path, relay_url="ws://localhost:18080/relay/v1/node")
    with pytest.raises(ValueError):
        settings(tmp_path, relay_url="wss://relay.example/relay/v1/node?token=x")
    with pytest.raises(ValueError):
        settings(tmp_path, connector_id="connector")
    with pytest.raises(ValueError):
        settings(tmp_path, health_path="relative.health")
    with pytest.raises(ValueError):
        settings(tmp_path, health_interval_seconds=10.1)
    assert settings(tmp_path, max_concurrency=8).max_concurrency == 8
    with pytest.raises(ValueError):
        settings(tmp_path, max_concurrency=0)
    with pytest.raises(ValueError):
        settings(tmp_path, max_concurrency=9)


def test_reconnect_stability_starts_only_after_valid_ready(tmp_path: Path) -> None:
    config = settings(tmp_path, ping_interval_seconds=20.0)
    connector = RelayConnector(config, object())  # type: ignore[arg-type]
    connector._reached_ready = True
    connector._ready_since = 25.0

    # A 25-second DNS/TLS handshake followed by a 100ms READY connection is
    # still short-lived and must not reset exponential backoff.
    assert not connector._ready_was_stable(25.1)
    assert connector._ready_was_stable(45.0)


def test_token_file_must_be_private_regular_file(tmp_path: Path) -> None:
    token_file = tmp_path / "node-token"
    token_file.write_text("private-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    configured = ConnectorSettings.from_env(
        {
            "CHEBY_CONNECTOR_RELAY_URL": "wss://relay.example/relay/v1/node",
            "CHEBY_CONNECTOR_ID": NODE_ID,
            "CHEBY_CONNECTOR_RELAY_TOKEN_FILE": str(token_file),
            "CHEBY_CONNECTOR_STATE": str(tmp_path / "connector.sqlite3"),
        }
    )
    assert configured.relay_token == "private-token"
    token_file.chmod(0o644)
    with pytest.raises(RuntimeError):
        ConnectorSettings.from_env(
            {
                "CHEBY_CONNECTOR_RELAY_URL": "wss://relay.example/relay/v1/node",
                "CHEBY_CONNECTOR_ID": NODE_ID,
                "CHEBY_CONNECTOR_RELAY_TOKEN_FILE": str(token_file),
                "CHEBY_CONNECTOR_STATE": str(tmp_path / "connector.sqlite3"),
            }
        )


def test_operation_schema_rejects_arbitrary_path_headers_and_fields() -> None:
    raw = delivery(
        operation="threads.read",
        params={"threadId": "thr_public", "path": "/admin", "headers": {"x": "y"}},
    )
    command = DeliveredMessage.model_validate_json(raw)
    with pytest.raises(ValidationError):
        command.payload.typed_params()
    value = json.loads(raw)
    value["payload"]["operation"] = "http.request"
    with pytest.raises(ValidationError):
        DeliveredMessage.model_validate(value)


def test_asset_base64_is_strict_canonical_and_bounded() -> None:
    valid = AssetsUploadParams.model_validate(
        {
            "threadId": "thr_public",
            "clientMessageId": "client-0001",
            "clientAssetId": "123e4567-e89b-12d3-a456-426614174000",
            "mediaType": "image/png",
            "bodyBase64": base64.b64encode(b"\x89PNG\r\n\x1a\nbody").decode(),
        }
    )
    assert valid.decoded_body().startswith(b"\x89PNG")
    invalid = valid.model_copy(update={"body_base64": valid.body_base64 + "=="})
    with pytest.raises(ValueError):
        invalid.decoded_body()


@pytest.mark.asyncio
async def test_ack_loss_replay_returns_exact_response_without_reexecution(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    calls: list[tuple[str, object]] = []
    backend = FakeBackend(calls)
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, backend)
    raw = delivery()

    first = await engine.handle_delivery(raw)
    assert first.ack_cursor == 1
    assert len(calls) == 1
    first_frame = first.response_frame
    await engine.close()

    reopened_state = ConnectorState(config.state_path)
    replay_backend = FakeBackend(calls)
    await replay_backend.bind_device(DEVICE_ID)
    replay_engine = ConnectorEngine(config, reopened_state, replay_backend)
    replay = await replay_engine.handle_delivery(raw)
    assert replay.response_frame == first_frame
    assert replay.ack_cursor == 1
    assert len(calls) == 1
    await replay_engine.close()


@pytest.mark.asyncio
async def test_request_and_message_id_reuse_fail_closed(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    await engine.handle_delivery(delivery())

    with pytest.raises(ProtocolViolation):
        await engine.handle_delivery(
            delivery(message_id=MESSAGE_2, params={"title": "Different"})
        )
    with pytest.raises(ProtocolViolation):
        await engine.handle_delivery(
            delivery(request_id=REQUEST_2, params={"title": "Different"})
        )
    assert len(backend.calls) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_concurrent_duplicate_executes_once(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    backend.delay = 0.05
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    first, second = await asyncio.gather(
        engine.handle_delivery(delivery()),
        engine.handle_delivery(delivery(seq=2)),
    )
    assert first.response_frame == second.response_frame
    assert len(backend.calls) == 1
    assert max(first.ack_cursor, second.ack_cursor) == 2
    await engine.close()


@pytest.mark.asyncio
async def test_slow_delivery_executes_before_next_sequence(tmp_path: Path) -> None:
    config = settings(tmp_path)
    order: list[str] = []
    slow_started = asyncio.Event()

    class OrderedBackend(FakeBackend):
        async def dispatch(self, operation: str, params: object) -> dict[str, Any]:
            title = getattr(params, "title")
            order.append("start:" + title)
            if title == "Slow":
                slow_started.set()
                await asyncio.sleep(0.05)
            order.append("end:" + title)
            return {"title": title}

    backend = OrderedBackend()
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    slow = asyncio.create_task(
        engine.handle_delivery(delivery(params={"title": "Slow"}))
    )
    await slow_started.wait()
    fast = asyncio.create_task(
        engine.handle_delivery(
            delivery(
                seq=2,
                message_id=MESSAGE_2,
                request_id=REQUEST_2,
                params={"title": "Fast"},
            )
        )
    )
    await asyncio.gather(slow, fast)
    assert order == ["start:Slow", "end:Slow", "start:Fast", "end:Fast"]
    await engine.close()


@pytest.mark.asyncio
async def test_cumulative_ack_advances_only_after_contiguous_delivery(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    second = await engine.handle_delivery(
        delivery(seq=2, message_id=MESSAGE_2, request_id=REQUEST_2)
    )
    assert second.ack_cursor == 0
    first = await engine.handle_delivery(delivery(seq=1))
    assert first.ack_cursor == 2
    await engine.close()


@pytest.mark.asyncio
async def test_delivery_workers_parallelize_threads_preserve_fifo_and_ack_gap(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, max_concurrency=3)
    alpha_first_entered = asyncio.Event()
    release_alpha_first = asyncio.Event()
    alpha_second_entered = asyncio.Event()
    beta_entered = asyncio.Event()
    order: list[str] = []

    class ParallelBackend(FakeBackend):
        async def dispatch(self, operation: str, params: object) -> dict[str, Any]:
            assert operation == "turns.start"
            client_message_id = str(getattr(params, "client_message_id"))
            order.append(client_message_id)
            if client_message_id == "client-alpha-first":
                alpha_first_entered.set()
                await release_alpha_first.wait()
            elif client_message_id == "client-alpha-second":
                alpha_second_entered.set()
            elif client_message_id == "client-beta-first":
                beta_entered.set()
            return {
                "id": "turn_" + client_message_id,
                "threadId": str(getattr(params, "thread_id")),
                "clientMessageId": client_message_id,
                "status": "inProgress",
                "createdAt": "2026-07-26T00:00:00Z",
            }

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, frame: str) -> None:
            self.sent.append(frame)

    backend = ParallelBackend()
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, backend)
    connector = RelayConnector(config, engine)
    websocket = WebSocket()
    send_lock = asyncio.Lock()
    sequencer = _LaneSequencer()
    deliveries: asyncio.Queue = asyncio.Queue()
    workers = [
        asyncio.create_task(
            connector._delivery_worker(
                deliveries,
                websocket,
                send_lock,
                sequencer,
            )
        )
        for _ in range(config.max_concurrency)
    ]

    def schedule(raw: str) -> None:
        parsed = DeliveredMessage.model_validate_json(raw)
        params = parsed.payload.typed_params()
        deliveries.put_nowait(
            sequencer.register(raw, _operation_lane(params))
        )

    schedule(
        delivery(
            seq=1,
            operation="turns.start",
            params={
                "threadId": "thr_alpha",
                "clientMessageId": "client-alpha-first",
                "input": [{"type": "text", "text": "first"}],
            },
        )
    )
    schedule(
        delivery(
            seq=2,
            message_id=MESSAGE_2,
            request_id=REQUEST_2,
            operation="turns.start",
            params={
                "threadId": "thr_alpha",
                "clientMessageId": "client-alpha-second",
                "input": [{"type": "text", "text": "second"}],
            },
        )
    )
    schedule(
        delivery(
            seq=3,
            message_id=MESSAGE_6,
            request_id=REQUEST_6,
            operation="turns.start",
            params={
                "threadId": "thr_beta",
                "clientMessageId": "client-beta-first",
                "input": [{"type": "text", "text": "parallel"}],
            },
        )
    )

    try:
        await alpha_first_entered.wait()
        for _ in range(30):
            if beta_entered.is_set() and websocket.sent:
                break
            await asyncio.sleep(0)
        assert beta_entered.is_set()
        assert not alpha_second_entered.is_set()
        assert state.ack_cursor == 0
        assert [json.loads(frame)["deliverySeq"] for frame in websocket.sent] == [0]

        release_alpha_first.set()
        await deliveries.join()
        assert alpha_second_entered.is_set()
        assert order == [
            "client-alpha-first",
            "client-beta-first",
            "client-alpha-second",
        ]
        assert state.ack_cursor == 3
        assert [json.loads(frame)["deliverySeq"] for frame in websocket.sent] == [
            0,
            1,
            3,
        ]
    finally:
        release_alpha_first.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_delivery_failure_stops_all_same_thread_successors(
    tmp_path: Path,
) -> None:
    handled: list[str] = []

    class FailingConnector(RelayConnector):
        async def _handle_delivery(
            self,
            raw: str,
            websocket: Any,
            send_lock: asyncio.Lock,
        ) -> None:
            parsed = DeliveredMessage.model_validate_json(raw)
            params = parsed.payload.typed_params()
            client_message_id = str(getattr(params, "client_message_id"))
            handled.append(client_message_id)
            if client_message_id == "client-alpha-first":
                raise RuntimeError("deterministic backend failure")
            raise AssertionError("same-Thread successor reached the backend")

    config = settings(tmp_path, max_concurrency=3)
    backend = FakeBackend()
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, backend)
    connector = FailingConnector(config, engine)
    sequencer = _LaneSequencer()
    deliveries: asyncio.Queue = asyncio.Queue()
    workers = [
        asyncio.create_task(
            connector._delivery_worker(
                deliveries,
                object(),
                asyncio.Lock(),
                sequencer,
            )
        )
        for _ in range(config.max_concurrency)
    ]

    for index, (message_id, request_id) in enumerate(
        (
            (MESSAGE_1, REQUEST_1),
            (MESSAGE_2, REQUEST_2),
            (MESSAGE_3, REQUEST_3),
        ),
        start=1,
    ):
        raw = delivery(
            seq=index,
            message_id=message_id,
            request_id=request_id,
            operation="turns.start",
            params={
                "threadId": "thr_alpha",
                "clientMessageId": f"client-alpha-{'first' if index == 1 else index}",
                "input": [{"type": "text", "text": str(index)}],
            },
        )
        params = DeliveredMessage.model_validate_json(raw).payload.typed_params()
        deliveries.put_nowait(sequencer.register(raw, _operation_lane(params)))

    try:
        await asyncio.wait_for(deliveries.join(), timeout=1)
        results = await asyncio.gather(*workers, return_exceptions=True)
        assert handled == ["client-alpha-first"]
        assert all(
            isinstance(result, RuntimeError)
            and str(result) == "deterministic backend failure"
            for result in results
        ), results
        assert not sequencer._tails
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_keyed_lock_cancellation_releases_partially_acquired_locks() -> None:
    locks = _KeyedLocks()
    release_b = asyncio.Event()
    b_entered = asyncio.Event()

    async def hold_b() -> None:
        async with locks.hold("b"):
            b_entered.set()
            await release_b.wait()

    async def hold_a_then_wait_for_b() -> None:
        async with locks.hold("a", "b"):
            raise AssertionError("blocked keyed lock unexpectedly acquired")

    blocker = asyncio.create_task(hold_b())
    await b_entered.wait()
    waiter = asyncio.create_task(hold_a_then_wait_for_b())
    for _ in range(30):
        lock_a = locks._locks.get("a")
        if lock_a is not None and lock_a[0].locked():
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("waiter did not acquire the first keyed lock")

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async def acquire_a() -> None:
        async with locks.hold("a"):
            return

    await asyncio.wait_for(acquire_a(), timeout=1)
    release_b.set()
    await blocker
    assert not locks._locks


@pytest.mark.asyncio
async def test_first_device_binds_durably_and_switch_fails_closed(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, backend)
    await engine.handle_delivery(delivery())
    assert state.get_meta("bound_device_id") == DEVICE_ID
    with pytest.raises(ProtocolViolation):
        await engine.handle_delivery(
            delivery(
                seq=2,
                message_id=MESSAGE_2,
                request_id=REQUEST_2,
                device_id=OTHER_DEVICE_ID,
            )
        )
    await engine.close()


@pytest.mark.asyncio
async def test_timeout_is_durable_nonretryable_and_not_reexecuted(tmp_path: Path) -> None:
    config = settings(tmp_path, request_timeout_seconds=0.01)
    backend = FakeBackend()
    backend.delay = 0.1
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    raw = delivery()
    first = await engine.handle_delivery(raw)
    body = json.loads(first.response_frame)
    assert body["payload"]["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
    assert body["payload"]["error"]["retryable"] is False
    replay = await engine.handle_delivery(raw)
    assert replay.response_frame == first.response_frame
    assert len(backend.calls) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_crash_after_execution_intent_never_replays_side_effect(tmp_path: Path) -> None:
    config = settings(tmp_path)
    raw = delivery()
    parsed = DeliveredMessage.model_validate_json(raw)
    from cheby_connector.engine import _fingerprint

    fingerprint = _fingerprint(parsed)
    state = ConnectorState(config.state_path)
    state.set_meta("bound_device_id", DEVICE_ID)
    assert state.reserve_execution(
        message_id=MESSAGE_1,
        request_id=REQUEST_1,
        fingerprint=fingerprint,
        operation="threads.create",
        reserved_bytes=config.max_outbound_frame_bytes,
    ) == "reserved"
    state.mark_execution_started(REQUEST_1, fingerprint)
    state.close()  # Simulated process death before an exact response was persisted.

    calls: list[tuple[str, object]] = []
    reopened = ConnectorState(config.state_path)
    backend = FakeBackend(calls)
    await backend.bind_device(DEVICE_ID)
    engine = ConnectorEngine(config, reopened, backend)
    result = await engine.handle_delivery(raw)
    body = json.loads(result.response_frame)
    assert body["payload"]["error"]["code"] == "OPERATION_OUTCOME_UNKNOWN"
    assert calls == []
    assert result.ack_cursor == 1
    await engine.close()


@pytest.mark.asyncio
async def test_crash_after_turn_execution_intent_reenters_gateway_idempotency(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    raw = delivery(
        operation="turns.start",
        params={
            "threadId": "thr_public",
            "clientMessageId": "client-message-recovery-001",
            "input": [{"type": "text", "text": "recover exactly once"}],
        },
    )
    parsed = DeliveredMessage.model_validate_json(raw)
    from cheby_connector.engine import _fingerprint

    fingerprint = _fingerprint(parsed)
    state = ConnectorState(config.state_path)
    state.set_meta("bound_device_id", DEVICE_ID)
    assert state.reserve_execution(
        message_id=MESSAGE_1,
        request_id=REQUEST_1,
        fingerprint=fingerprint,
        operation="turns.start",
        reserved_bytes=config.max_outbound_frame_bytes,
    ) == "reserved"
    state.mark_execution_started(REQUEST_1, fingerprint)
    state.close()

    calls: list[tuple[str, object]] = []
    reopened = ConnectorState(config.state_path)
    backend = FakeBackend(calls)
    await backend.bind_device(DEVICE_ID)
    engine = ConnectorEngine(config, reopened, backend)
    result = await engine.handle_delivery(raw)
    body = json.loads(result.response_frame)["payload"]

    assert body["ok"] is True
    assert [operation for operation, _ in calls] == ["turns.start"]
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM request_intents WHERE request_id = ?",
        (REQUEST_1,),
    ).fetchone()[0] == 0
    await engine.close()


@pytest.mark.asyncio
async def test_event_subscription_queues_strict_event_envelope(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    backend.events.append(
        FakeEvent(
            stream_id="stream_public",
            event_id="evt_public",
            seq=1,
            occurred_at="2026-07-20T12:00:00Z",
            type="message.patch",
            thread_id="thr_public",
            turn_id="turn_public",
            item_id=None,
            payload={"messageId": "msg_public", "ops": []},
        )
    )
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    await engine.handle_delivery(
        delivery(operation="events.subscribe", params={"afterSeq": 0})
    )
    await asyncio.sleep(0.02)
    frames = [json.loads(frame) for _, frame in engine.pending_outbox()]
    event = next(frame for frame in frames if frame["payload"]["kind"] == "event")
    assert event["payload"]["eventSeq"] == 1
    assert event["payload"]["data"]["turnId"] == "turn_public"

    ahead = await engine.handle_delivery(
        delivery(
            seq=2,
            message_id=MESSAGE_2,
            request_id=REQUEST_2,
            operation="events.ack",
            params={"streamId": "stream_public", "seq": 2},
        )
    )
    assert json.loads(ahead.response_frame)["payload"]["error"]["code"] == "INVALID_COMMAND"
    valid = await engine.handle_delivery(
        delivery(
            seq=3,
            message_id=MESSAGE_3,
            request_id=REQUEST_3,
            operation="events.ack",
            params={"streamId": "stream_public", "seq": 1},
        )
    )
    assert json.loads(valid.response_frame)["payload"]["ok"] is True
    await engine.close()


@pytest.mark.asyncio
async def test_fast_resubscribe_preserves_unaccepted_old_event_correlation(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    backend = FakeBackend()
    backend.events.append(
        FakeEvent(
            stream_id="stream_public",
            event_id="evt_public",
            seq=1,
            occurred_at="2026-07-20T12:00:00Z",
            type="message.patch",
            thread_id="thr_public",
            turn_id="turn_public",
            item_id=None,
            payload={"messageId": "msg_public", "ops": []},
        )
    )
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    await engine.handle_delivery(
        delivery(operation="events.subscribe", params={"afterSeq": 0})
    )
    await asyncio.sleep(0.02)
    old_task = engine._subscription

    await engine.handle_delivery(
        delivery(
            seq=2,
            message_id=MESSAGE_2,
            request_id=REQUEST_2,
            operation="events.subscribe",
            params={"afterSeq": 0},
        )
    )
    await asyncio.sleep(0.02)
    assert old_task is not None and old_task.done()
    events = [
        json.loads(frame)
        for _, frame in engine.pending_outbox()
        if json.loads(frame)["payload"]["kind"] == "event"
    ]
    assert {event["payload"]["requestId"] for event in events} == {
        REQUEST_1,
        REQUEST_2,
    }
    assert len({event["messageId"] for event in events}) == 2
    await engine.close()


@pytest.mark.asyncio
async def test_relay_acceptance_is_connection_local_and_strictly_ordered() -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, frame: str) -> None:
            self.sent.append(frame)

    websocket = WebSocket()
    in_flight = _SingleInFlight()
    persisted: list[str] = []
    with pytest.raises(ProtocolViolation):
        await in_flight.accept(MESSAGE_1, lambda: persisted.append(MESSAGE_1))

    accepted = await in_flight.send(
        websocket, asyncio.Lock(), MESSAGE_1, "first"
    )
    with pytest.raises(ProtocolViolation):
        await in_flight.accept(MESSAGE_2, lambda: persisted.append(MESSAGE_2))
    assert persisted == []
    assert not accepted.is_set()

    await in_flight.accept(MESSAGE_1, lambda: persisted.append(MESSAGE_1))
    assert accepted.is_set()
    second = await in_flight.send(
        websocket, asyncio.Lock(), MESSAGE_2, "second"
    )
    await in_flight.accept(MESSAGE_2, lambda: persisted.append(MESSAGE_2))
    assert second.is_set()
    assert persisted == [MESSAGE_1, MESSAGE_2]
    assert websocket.sent == ["first", "second"]


@pytest.mark.asyncio
async def test_health_watchdog_refreshes_without_relay_activity(tmp_path: Path) -> None:
    health_path = tmp_path / "health" / "connector.health"
    task = asyncio.create_task(run_health_watchdog(str(health_path), 0.01))
    try:
        for _ in range(20):
            if health_path.exists():
                break
            await asyncio.sleep(0.005)
        first = health_path.stat().st_mtime_ns
        assert health_path.read_text(encoding="ascii").startswith(str(os.getpid()) + " ")
        await asyncio.sleep(0.03)
        assert health_path.stat().st_mtime_ns > first
        assert list(health_path.parent.glob(".connector.health.*.tmp")) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_gc_removes_only_expired_inactive_dedupe_in_batches(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, FakeBackend())
    first = await engine.handle_delivery(delivery())
    second = await engine.handle_delivery(
        delivery(seq=2, message_id=MESSAGE_2, request_id=REQUEST_2)
    )
    engine.mark_accepted(json.loads(first.response_frame)["messageId"])
    engine.mark_accepted(json.loads(second.response_frame)["messageId"])
    state._conn.execute("UPDATE requests SET created_at = ?", (1,))
    state._conn.execute("UPDATE inbound_messages SET created_at = ?", (1,))

    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=1) == 1
    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=1) == 1
    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=1) == 0
    await engine.close()


@pytest.mark.asyncio
async def test_gc_preserves_active_outbox_and_executing_intent(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    state = ConnectorState(config.state_path)
    engine = ConnectorEngine(config, state, FakeBackend())
    result = await engine.handle_delivery(delivery())
    response_message_id = json.loads(result.response_frame)["messageId"]
    state._conn.execute("UPDATE requests SET created_at = ?", (1,))
    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=10) == 0

    engine.mark_accepted(response_message_id)
    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=10) == 1

    parsed = DeliveredMessage.model_validate_json(
        delivery(
            seq=3,
            message_id=MESSAGE_4,
            request_id=REQUEST_4,
            params={"title": "Executing"},
        )
    )
    from cheby_connector.engine import _fingerprint

    fingerprint = _fingerprint(parsed)
    state.reserve_execution(
        message_id=MESSAGE_4,
        request_id=REQUEST_4,
        fingerprint=fingerprint,
        operation="threads.create",
        reserved_bytes=config.max_outbound_frame_bytes,
    )
    state.mark_execution_started(REQUEST_4, fingerprint)
    assert state.gc_completed(cutoff_epoch=int(time.time()), batch_size=10) == 0
    intent = state._conn.execute(
        "SELECT state FROM request_intents WHERE request_id = ?", (REQUEST_4,)
    ).fetchone()
    assert intent is not None and intent["state"] == "executing"
    await engine.close()


def test_outbox_has_count_and_byte_limits(tmp_path: Path) -> None:
    state = ConnectorState(
        str(tmp_path / "state.sqlite3"), outbox_max_count=1, outbox_max_bytes=20
    )
    state.enqueue("msg_a", "1234567890")
    with pytest.raises(OutboxFull):
        state.enqueue("msg_b", "x")
    state.close()


def test_outbox_preserves_insertion_order_not_message_id_order(tmp_path: Path) -> None:
    state = ConnectorState(str(tmp_path / "state.sqlite3"))
    state.enqueue("msg_z", "first")
    state.enqueue("msg_a", "second")
    assert state.pending_outbox() == [("msg_z", "first"), ("msg_a", "second")]
    state.close()


@pytest.mark.asyncio
async def test_direct_gateway_service_integration_and_relay_device_binding(
    tmp_path: Path,
) -> None:
    from cheby_gateway.bridge import FakeCodexBridge
    from cheby_gateway.config import GatewaySettings
    from cheby_gateway.service import GatewayService
    from cheby_gateway.store import GatewayStore

    gateway_settings = GatewaySettings(
        db_path=str(tmp_path / "gateway.sqlite3"),
        pairing_secret="test-pairing-secret",
        bridge_mode="fake",
        asset_staging_dir=str(tmp_path / "assets"),
    )
    store = GatewayStore(gateway_settings.db_path)
    legacy = store.pair_device(
        "legacy",
        "legacy-key",
        "legacy-access",
        "2999-01-01T00:00:00Z",
        "legacy-refresh",
        "2999-01-01T00:00:00Z",
    )
    service = GatewayService(gateway_settings, store, FakeCodexBridge())
    await service.start()
    backend = DirectGatewayBackend(
        service=service,
        store=store,
        connector_name=NODE_ID,
        owns_store=True,
    )
    await backend.bind_device(DEVICE_ID)
    assert backend.device_id == DEVICE_ID
    assert store.authenticate_with_outcome("legacy-access")[1] == "revoked"

    from cheby_connector.models import EmptyParams, ThreadsCreateParams, ThreadsListParams

    info = await backend.dispatch("server.info", EmptyParams())
    assert info["codexConnected"] is True
    created = await backend.dispatch(
        "threads.create", ThreadsCreateParams(title="Relay thread")
    )
    listed = await backend.dispatch("threads.list", ThreadsListParams(archived=False))
    assert created["id"] in {item["id"] for item in listed["data"]}
    assert (await backend.dispatch("server.info", EmptyParams()))["protocolVersion"] == 1
    assert store.device_by_id(legacy.id) is not None
    await backend.close()


@pytest.mark.asyncio
async def test_turn_contract_ack_loss_busy_wait_and_public_lifecycle_transcript(
    tmp_path: Path,
) -> None:
    from cheby_gateway.bridge import BridgeEvent, FakeCodexBridge
    from cheby_gateway.config import GatewaySettings
    from cheby_gateway.service import GatewayService
    from cheby_gateway.store import GatewayStore
    from cheby_connector.models import ThreadsCreateParams

    gateway_settings = GatewaySettings(
        db_path=str(tmp_path / "gateway-turns.sqlite3"),
        pairing_secret="test-pairing-secret",
        bridge_mode="fake",
        asset_staging_dir=str(tmp_path / "assets-turns"),
    )
    store = GatewayStore(gateway_settings.db_path)
    bridge = FakeCodexBridge(auto_events=False)
    service = GatewayService(gateway_settings, store, bridge)
    await service.start()
    backend = DirectGatewayBackend(
        service=service,
        store=store,
        connector_name=NODE_ID,
        owns_store=True,
    )
    await backend.bind_device(DEVICE_ID)
    alpha = await backend.dispatch(
        "threads.create",
        ThreadsCreateParams(title="Alpha"),
    )
    beta = await backend.dispatch(
        "threads.create",
        ThreadsCreateParams(title="Beta"),
    )

    config = settings(
        tmp_path,
        state_path=str(tmp_path / "connector-turns.sqlite3"),
        request_timeout_seconds=1.0,
    )
    engine = ConnectorEngine(config, ConnectorState(config.state_path), backend)
    alpha_raw = delivery(
        operation="turns.start",
        params={
            "threadId": alpha["id"],
            "clientMessageId": "client-alpha-first",
            "input": [{"type": "text", "text": "Hold Alpha"}],
        },
    )
    alpha_result = await engine.handle_delivery(alpha_raw)
    alpha_body = json.loads(alpha_result.response_frame)["payload"]
    assert alpha_body["ok"] is True
    assert set(alpha_body["result"]) == {
        "id",
        "threadId",
        "status",
        "clientMessageId",
        "createdAt",
    }
    assert alpha_body["result"]["threadId"] == alpha["id"]
    assert alpha_body["result"]["clientMessageId"] == "client-alpha-first"
    assert alpha_body["result"]["id"].startswith("turn_")

    # Losing the delivery acknowledgement and replaying the same Relay command
    # returns the exact durable result without a second Codex turn.
    replay = await engine.handle_delivery(
        delivery(
            seq=2,
            operation="turns.start",
            params={
                "threadId": alpha["id"],
                "clientMessageId": "client-alpha-first",
                "input": [{"type": "text", "text": "Hold Alpha"}],
            },
        )
    )
    assert replay.response_frame == alpha_result.response_frame
    assert bridge.start_turn_calls == 1

    beta_result = await engine.handle_delivery(
        delivery(
            seq=3,
            message_id=MESSAGE_2,
            request_id=REQUEST_2,
            operation="turns.start",
            params={
                "threadId": beta["id"],
                "clientMessageId": "client-beta-first",
                "input": [{"type": "text", "text": "Hold Beta"}],
            },
        )
    )
    assert json.loads(beta_result.response_frame)["payload"]["ok"] is True
    active = store._conn.execute(
        """
        SELECT thread_public_id, COUNT(*) AS count FROM turns
        WHERE status IN ('pending', 'dispatching', 'inProgress', 'running')
        GROUP BY thread_public_id
        """
    ).fetchall()
    assert {str(row["thread_public_id"]): int(row["count"]) for row in active} == {
        alpha["id"]: 1,
        beta["id"]: 1,
    }

    busy = await engine.handle_delivery(
        delivery(
            seq=4,
            message_id=MESSAGE_3,
            request_id=REQUEST_3,
            operation="turns.start",
            params={
                "threadId": alpha["id"],
                "clientMessageId": "client-alpha-second",
                "input": [{"type": "text", "text": "Wait for Alpha"}],
            },
        )
    )
    busy_error = json.loads(busy.response_frame)["payload"]["error"]
    assert busy_error == {
        "code": "THREAD_BUSY",
        "message": "Wait for the active turn in this conversation to finish",
        "retryable": True,
        "retryAfterSeconds": 2,
    }
    assert bridge.start_turn_calls == 2

    await engine.handle_delivery(
        delivery(
            seq=5,
            message_id=MESSAGE_4,
            request_id=REQUEST_4,
            operation="events.subscribe",
            params={"afterSeq": 0},
        )
    )
    await asyncio.sleep(0)
    events = [
        json.loads(frame)["payload"]
        for _, frame in engine.pending_outbox()
        if json.loads(frame)["payload"]["kind"] == "event"
    ]
    started = [event for event in events if event["eventType"] == "turn.started"]
    assert {
        (
            event["threadId"],
            event["data"]["turnId"],
        )
        for event in started
    } == {
        (alpha["id"], alpha_body["result"]["id"]),
        (
            beta["id"],
            json.loads(beta_result.response_frame)["payload"]["result"]["id"],
        ),
    }
    transcript = json.dumps(started)
    assert "raw-thread-" not in transcript
    assert "raw-turn-" not in transcript

    alpha_turn_id = alpha_body["result"]["id"]
    await service.handle_bridge_event(
        BridgeEvent(
            method="turn/completed",
            params={
                "threadId": store.thread_raw_id(alpha["id"]),
                "turn": {
                    "id": store.turn_raw_id(alpha_turn_id),
                    "status": "completed",
                    "items": [],
                },
            },
        )
    )
    retried = await engine.handle_delivery(
        delivery(
            seq=6,
            message_id=MESSAGE_5,
            request_id=REQUEST_5,
            operation="turns.start",
            params={
                "threadId": alpha["id"],
                "clientMessageId": "client-alpha-second",
                "input": [{"type": "text", "text": "Wait for Alpha"}],
            },
        )
    )
    retried_body = json.loads(retried.response_frame)["payload"]
    assert retried_body["ok"] is True
    assert retried_body["result"]["clientMessageId"] == "client-alpha-second"
    assert bridge.start_turn_calls == 3
    # The original busy request remains exactly replayable; it cannot mutate
    # into a success and accidentally execute the same command a second time.
    busy_replay = await engine.handle_delivery(
        delivery(
            seq=7,
            message_id=MESSAGE_3,
            request_id=REQUEST_3,
            operation="turns.start",
            params={
                "threadId": alpha["id"],
                "clientMessageId": "client-alpha-second",
                "input": [{"type": "text", "text": "Wait for Alpha"}],
            },
        )
    )
    assert busy_replay.response_frame == busy.response_frame
    assert bridge.start_turn_calls == 3
    await engine.close()
