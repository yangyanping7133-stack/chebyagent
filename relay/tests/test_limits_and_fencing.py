from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cheby_relay.app import create_app
from cheby_relay.config import RelaySettings
from cheby_relay.limits import SlidingWindowLimiter
from cheby_relay.store import RelayStore

from .conftest import (
    RelayHarness,
    command,
    device_ws_headers,
    node_ws_headers,
    receive_disconnect,
    receive_type,
    response_for,
)


def _custom_harness(tmp_path, **changes):
    base = RelaySettings(db_path=str(tmp_path / "limits.sqlite3"))
    settings = replace(base, **changes)
    store = RelayStore(settings)
    client_context = TestClient(create_app(settings, store=store))
    client = client_context.__enter__()
    return RelayHarness(settings, store, client), client_context


def test_default_limits_cover_the_single_user_multi_thread_burst(tmp_path):
    settings = RelaySettings(db_path=str(tmp_path / "defaults.sqlite3"))

    assert settings.message_rate_limit == 600
    assert settings.control_frame_rate_limit >= settings.message_rate_limit
    assert settings.max_pending_messages < settings.message_rate_limit


def test_limiter_bounds_key_cardinality_and_evicts_idle_buckets(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("cheby_relay.limits.time.monotonic", lambda: clock[0])
    limiter = SlidingWindowLimiter(max_keys=2)

    async def exercise() -> None:
        assert await limiter.allow("one", limit=1, window_seconds=1)
        assert await limiter.allow("two", limit=1, window_seconds=1)
        assert not await limiter.allow("three", limit=1, window_seconds=1)
        assert limiter.tracked_key_count == 2

        clock[0] = 102.0
        assert await limiter.allow("three", limit=1, window_seconds=1)
        assert limiter.tracked_key_count == 1

    asyncio.run(exercise())


def test_rate_frame_queue_and_connection_limits(tmp_path):
    harness, context = _custom_harness(
        tmp_path,
        message_rate_limit=1,
        control_frame_rate_limit=1,
        message_rate_window_seconds=60,
        max_pending_messages=1,
    )
    try:
        bundle = harness.bootstrap()
        device = harness.enroll(bundle)
        headers = device_ws_headers(device)
        with harness.client.websocket_connect("/relay/v1/device", headers=headers) as socket:
            receive_type(socket, "ready")
            socket.send_json({"v": 1, "type": "ping", "nonce": "abcdefgh"})
            receive_type(socket, "pong")
            socket.send_json({"v": 1, "type": "ping", "nonce": "ijklmnop"})
            with pytest.raises(WebSocketDisconnect) as denied:
                socket.receive_json()
            assert denied.value.code == 4408

        first = command(
            device,
            "turns.start",
            {
                "threadId": "thread-1",
                "clientMessageId": "client-0001",
                "input": [{"type": "text", "text": "one"}],
            },
        )
        second = command(
            device,
            "turns.start",
            {
                "threadId": "thread-1",
                "clientMessageId": "client-0002",
                "input": [{"type": "text", "text": "two"}],
            },
        )
        # A fresh access token also gets a fresh rate-limit identity.
        refreshed = harness.enroll(bundle, key=device.key)
        with harness.client.websocket_connect(
            "/relay/v1/device", headers=device_ws_headers(refreshed)
        ) as socket:
            receive_type(socket, "ready")
            socket.send_json(first)
            receive_type(socket, "accepted")
            socket.send_json(second)
            with pytest.raises(WebSocketDisconnect) as denied:
                socket.receive_json()
            # Message-rate limiting is reached before queue handling in this setup.
            assert denied.value.code == 4408
    finally:
        context.__exit__(None, None, None)
        harness.store.close()


def test_control_frames_do_not_consume_business_message_quota(tmp_path):
    harness, context = _custom_harness(
        tmp_path,
        message_rate_limit=1,
        control_frame_rate_limit=10,
    )
    try:
        bundle = harness.bootstrap()
        device = harness.enroll(bundle)
        request = command(
            device,
            "turns.start",
            {
                "threadId": "thread-control-quota",
                "clientMessageId": "client-control-quota",
                "input": [{"type": "text", "text": "one"}],
            },
        )
        with harness.client.websocket_connect(
            "/relay/v1/device", headers=device_ws_headers(device)
        ) as socket:
            receive_type(socket, "ready")
            for nonce in ("abcdefgh", "ijklmnop", "qrstuvwx"):
                socket.send_json({"v": 1, "type": "ping", "nonce": nonce})
                assert receive_type(socket, "pong")["nonce"] == nonce

            socket.send_json(request)
            receive_type(socket, "accepted")
            socket.send_json(request)
            with pytest.raises(WebSocketDisconnect) as denied:
                socket.receive_json()
            assert denied.value.code == 4408
    finally:
        context.__exit__(None, None, None)
        harness.store.close()


def test_node_generation_fences_old_consumer(harness):
    bundle = harness.bootstrap()
    with harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(bundle)
    ) as old_node:
        receive_type(old_node, "ready")
        with harness.client.websocket_connect(
            "/relay/v1/node", headers=node_ws_headers(bundle)
        ) as new_node:
            receive_type(new_node, "ready")
            with pytest.raises(WebSocketDisconnect) as fenced:
                old_node.receive_json()
            assert fenced.value.code == 4403
            new_node.send_json({"v": 1, "type": "ping", "nonce": "abcdefgh"})
            assert receive_type(new_node, "pong")["nonce"] == "abcdefgh"


def test_revoked_open_node_closes_before_next_delivery(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as device_ws, harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(bundle)
    ) as node_ws:
        receive_type(device_ws, "ready")
        receive_type(node_ws, "ready")
        receive_type(device_ws, "node.status")
        harness.store.rotate_node_credential(bundle.assistant_id, bundle.node_id)
        device_ws.send_json(command(device, "threads.list", {"archived": False}))
        offline = receive_type(device_ws, "error")
        assert offline["code"] == "NODE_OFFLINE"
        with pytest.raises(WebSocketDisconnect) as revoked:
            node_ws.receive_json()
        assert revoked.value.code == 4403


def test_revoked_open_device_closes_before_node_response_delivery(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    request = command(device, "threads.list", {"archived": False})
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as device_ws, harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(bundle)
    ) as node_ws:
        receive_type(device_ws, "ready")
        receive_type(node_ws, "ready")
        receive_type(device_ws, "node.status")
        device_ws.send_json(request)
        receive_type(device_ws, "accepted")
        receive_type(node_ws, "delivery")
        harness.store.revoke_principal(device.device_id)
        node_ws.send_json(response_for(request))
        receive_type(node_ws, "accepted")
        assert receive_disconnect(device_ws) == 4401


def test_persistent_queue_count_limit_closes_sender(tmp_path):
    harness, context = _custom_harness(tmp_path, max_pending_messages=1)
    try:
        bundle = harness.bootstrap()
        device = harness.enroll(bundle)
        first = command(
            device,
            "turns.start",
            {
                "threadId": "thread-1",
                "clientMessageId": "client-0001",
                "input": [{"type": "text", "text": "one"}],
            },
        )
        second = command(
            device,
            "turns.start",
            {
                "threadId": "thread-1",
                "clientMessageId": "client-0002",
                "input": [{"type": "text", "text": "two"}],
            },
        )
        with harness.client.websocket_connect(
            "/relay/v1/device", headers=device_ws_headers(device)
        ) as socket:
            receive_type(socket, "ready")
            socket.send_json(first)
            receive_type(socket, "accepted")
            socket.send_json(second)
            assert receive_disconnect(socket) == 4410
    finally:
        context.__exit__(None, None, None)
        harness.store.close()


def test_frame_and_per_principal_connection_limits(tmp_path):
    harness, context = _custom_harness(
        tmp_path,
        max_frame_bytes=1024,
        max_command_bytes=512,
    )
    try:
        bundle = harness.bootstrap()
        device = harness.enroll(bundle)
        with harness.client.websocket_connect(
            "/relay/v1/device", headers=device_ws_headers(device)
        ) as first:
            receive_type(first, "ready")
            with harness.client.websocket_connect(
                "/relay/v1/device", headers=device_ws_headers(device)
            ) as second:
                assert receive_disconnect(second) == 4408
            first.send_text("x" * 1025)
            assert receive_disconnect(first) == 4409
    finally:
        context.__exit__(None, None, None)
        harness.store.close()


def test_expired_open_device_closes_before_response_delivery(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    request = command(device, "threads.list", {"archived": False})
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as device_ws, harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(bundle)
    ) as node_ws:
        receive_type(device_ws, "ready")
        receive_type(node_ws, "ready")
        receive_type(device_ws, "node.status")
        device_ws.send_json(request)
        receive_type(device_ws, "accepted")
        receive_type(node_ws, "delivery")
        with harness.store._lock, harness.store._db:
            harness.store._db.execute(
                "UPDATE credentials SET expires_at=0 WHERE principal_id=? AND role='device'",
                (device.device_id,),
            )
        node_ws.send_json(response_for(request))
        receive_type(node_ws, "accepted")
        assert receive_disconnect(device_ws) == 4401
