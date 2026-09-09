from __future__ import annotations

import asyncio
import time

import pytest

from cheby_relay.app import PublicError, _bounded_body
from cheby_relay.config import RelaySettings
from cheby_relay.connections import ConnectionManager, RelayConnection
from cheby_relay.store import AuthenticationError, BindingError, RelayStore

from .conftest import (
    command,
    device_ws_headers,
    node_ws_headers,
    random_id,
    receive_type,
)
from .test_limits_and_fencing import _custom_harness


def test_gate_payload_retention_environment_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(
        "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS",
        "43200",
    )
    monkeypatch.setenv("CHEBY_RELAY_DB_PATH", str(tmp_path / "relay.sqlite3"))
    assert RelaySettings.from_env().acked_payload_retention_seconds == 43_200

    monkeypatch.setenv(
        "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS",
        "not-a-number",
    )
    with pytest.raises(ValueError, match="positive decimal integer"):
        RelaySettings.from_env()

    monkeypatch.setenv(
        "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS",
        "604801",
    )
    with pytest.raises(ValueError, match="retention"):
        RelaySettings.from_env()


def test_atomic_write_revalidation_rejects_stale_generation_and_revoked_credential(
    harness,
):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    device_principal = harness.store.authenticate(device.access_token, "device")
    node_principal = harness.store.authenticate(bundle.node_token, "node")
    request = command(device, "threads.list", {"archived": False})
    request_payload = request["payload"]
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=request["messageId"],
        payload=request_payload,
        expires_at=int(time.time()) + 30,
    )
    stale_generation = harness.store.activate_node_generation(bundle.node_id)
    current_generation = harness.store.activate_node_generation(bundle.node_id)
    response_payload = {
        "kind": "response",
        "requestId": request_payload["requestId"],
        "deviceId": device.device_id,
        "operation": "threads.list",
        "ok": True,
        "result": {},
    }
    with pytest.raises(BindingError):
        harness.store.enqueue(
            principal=node_principal,
            recipient_role="device",
            message_id=random_id("msg_"),
            payload=response_payload,
            expires_at=None,
            node_generation=stale_generation,
        )
    with pytest.raises(BindingError):
        harness.store.acknowledge(
            principal=node_principal,
            seq=1,
            node_generation=stale_generation,
        )
    assert (
        harness.store.acknowledge(
            principal=node_principal,
            seq=1,
            node_generation=current_generation,
        )
        == 1
    )
    harness.store.revoke_principal(device.device_id)
    with pytest.raises(AuthenticationError):
        harness.store.enqueue(
            principal=device_principal,
            recipient_role="node",
            message_id=random_id("msg_"),
            payload={
                **request_payload,
                "requestId": random_id("req_"),
            },
            expires_at=int(time.time()) + 30,
        )


def test_writer_failure_is_removed_from_online_presence(tmp_path):
    class FailingSocket:
        async def send_json(self, _: dict) -> None:
            raise RuntimeError("write failed")

        async def close(self, *, code: int, reason: str) -> None:
            del code, reason

    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "writer.sqlite3"))
        store = RelayStore(settings)
        try:
            bundle = store.bootstrap_assistant()
            principal = store.authenticate(bundle.node_token, "node")
            generation = store.activate_node_generation(bundle.node_id)
            manager = ConnectionManager(settings, store)
            connection = RelayConnection(
                FailingSocket(),
                principal,
                settings.outbound_queue_size,
                node_generation=generation,
            )
            await manager.register(connection)
            connection.start()
            assert await connection.enqueue({"v": 1, "type": "test"})
            for _ in range(20):
                if not await manager.is_online(bundle.assistant_id, "node"):
                    break
                await asyncio.sleep(0)
            assert connection.closed
            assert not await manager.is_online(bundle.assistant_id, "node")
        finally:
            store.close()

    asyncio.run(scenario())


def test_expired_offline_command_emits_persistent_failure_and_retry_is_not_accepted(
    harness,
):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    queued = command(
        device,
        "turns.start",
        {
            "threadId": "thread-1",
            "clientMessageId": "client-expiry-1",
            "input": [{"type": "text", "text": "hello"}],
        },
    )
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as socket:
        receive_type(socket, "ready")
        socket.send_json(queued)
        assert receive_type(socket, "accepted")["queued"] is True

    result = harness.store.run_gc(now=int(time.time()) + 301)
    assert result.expired_deliveries == 1
    assert len(result.expiration_notices) == 1

    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as socket:
        receive_type(socket, "ready")
        notice = receive_type(socket, "delivery")
        assert notice["payload"]["error"] == {
            "code": "DELIVERY_EXPIRED",
            "message": "Command expired before Node delivery",
            "retryable": True,
        }
        socket.send_json(queued)
        terminal = receive_type(socket, "error")
        assert terminal["code"] == "DELIVERY_EXPIRED"
        assert terminal["retryable"] is True


def test_bounded_body_stops_consuming_as_soon_as_stream_exceeds_limit():
    class StreamingRequest:
        scope = {"headers": []}

        def __init__(self) -> None:
            self.consumed = 0

        async def stream(self):
            for chunk in (b"1234", b"56", b"must-not-be-consumed"):
                self.consumed += 1
                yield chunk

    async def scenario() -> None:
        request = StreamingRequest()
        with pytest.raises(PublicError) as denied:
            await _bounded_body(request, 5)  # type: ignore[arg-type]
        assert denied.value.status_code == 413
        assert request.consumed == 2

    asyncio.run(scenario())


def test_batched_gc_drops_large_payload_rows_before_idempotency_fingerprints(tmp_path):
    harness, context = _custom_harness(
        tmp_path,
        gc_batch_size=2,
        acked_payload_retention_seconds=1,
        expired_payload_retention_seconds=1,
        idempotency_retention_seconds=100,
        request_retention_seconds=1,
        credential_retention_seconds=1,
    )
    try:
        bundle = harness.bootstrap()
        device = harness.enroll(bundle)
        device_principal = harness.store.authenticate(device.access_token, "device")
        node_principal = harness.store.authenticate(bundle.node_token, "node")
        generation = harness.store.activate_node_generation(bundle.node_id)
        frames = [
            command(device, "threads.list", {"archived": False}) for _ in range(3)
        ]
        for frame in frames:
            harness.store.enqueue(
                principal=device_principal,
                recipient_role="node",
                message_id=frame["messageId"],
                payload=frame["payload"],
                expires_at=int(time.time()) + 300,
            )
        harness.store.acknowledge(
            principal=node_principal,
            seq=3,
            node_generation=generation,
        )
        base = int(time.time())
        for index in range(3):
            harness.store.claim_proof_nonce(
                f"gc-subject-{index}", bytes([index + 1]) * 16, now=base
            )
        for _ in range(3):
            harness.store.rotate_node_credential(bundle.assistant_id, bundle.node_id)

        first_gc = harness.store.run_gc(now=base + 2)
        assert first_gc.deleted_deliveries == 2
        assert first_gc.deleted_idempotency == 0
        with harness.store._lock:
            delivery_count = harness.store._db.execute(
                "SELECT COUNT(*) FROM deliveries"
            ).fetchone()[0]
            fingerprint_count = harness.store._db.execute(
                "SELECT COUNT(*) FROM message_idempotency"
            ).fetchone()[0]
        assert delivery_count == 1
        assert fingerprint_count == 3
        duplicate = harness.store.enqueue(
            principal=device_principal,
            recipient_role="node",
            message_id=frames[0]["messageId"],
            payload=frames[0]["payload"],
            expires_at=base + 300,
        )
        assert duplicate.state == "acked"

        second_gc = harness.store.run_gc(now=base + 901)
        assert second_gc.deleted_deliveries == 1
        assert 0 < second_gc.deleted_idempotency <= 2
        assert 0 < second_gc.deleted_credentials <= 2
        assert 0 < second_gc.deleted_nonces <= 2
        assert second_gc.deleted_requests <= 2
    finally:
        context.__exit__(None, None, None)
        harness.store.close()
