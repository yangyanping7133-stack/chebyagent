from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from .conftest import (
    command,
    device_ws_headers,
    node_ws_headers,
    random_id,
    receive_disconnect,
    receive_type,
    response_for,
)


def test_wrong_role_missing_pop_origin_and_query_are_rejected(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)

    with harness.client.websocket_connect(
        "/relay/v1/device", headers=node_ws_headers(bundle)
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403

    with harness.client.websocket_connect(
        "/relay/v1/node", headers={"authorization": "Bearer " + device.access_token}
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403

    with harness.client.websocket_connect(
        "/relay/v1/device",
        headers={"authorization": "Bearer " + device.access_token},
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4401

    with harness.client.websocket_connect(
        "/relay/v1/node", headers={**node_ws_headers(bundle), "origin": "https://evil.example"}
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403

    with harness.client.websocket_connect(
        "/relay/v1/node?token=leak", headers=node_ws_headers(bundle)
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4400


def test_offline_policy_duplicate_reconnect_replay_ack_and_node_status(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    headers = device_ws_headers(device)
    message_id = random_id("msg_")
    request_id = random_id("req_")
    queued = command(
        device,
        "turns.start",
        {
            "threadId": "thread-1",
            "clientMessageId": "client-message-1",
            "input": [{"type": "text", "text": "hello"}],
        },
        message_id=message_id,
        request_id=request_id,
    )
    read = command(device, "threads.read", {"threadId": "thread-1"})

    with harness.client.websocket_connect("/relay/v1/device", headers=headers) as device_ws:
        ready = receive_type(device_ws, "ready")
        assert ready["nodeStatus"] == "offline"
        device_ws.send_json(read)
        offline = receive_type(device_ws, "error")
        assert offline["code"] == "NODE_OFFLINE"

        device_ws.send_json(queued)
        accepted = receive_type(device_ws, "accepted")
        assert accepted == {
            "v": 1,
            "type": "accepted",
            "messageId": message_id,
            "deliverySeq": 1,
            "duplicate": False,
            "queued": True,
        }
        device_ws.send_json(queued)
        duplicate = receive_type(device_ws, "accepted")
        assert duplicate["duplicate"] is True
        assert duplicate["deliverySeq"] == 1

        with harness.client.websocket_connect(
            "/relay/v1/node", headers=node_ws_headers(bundle)
        ) as node_ws:
            assert receive_type(node_ws, "ready")["ackCursor"] == 0
            delivery = receive_type(node_ws, "delivery")
            assert delivery["messageId"] == message_id
            assert delivery["payload"]["requestId"] == request_id
            status = receive_type(device_ws, "node.status")
            assert status["status"] == "online"

        status = receive_type(device_ws, "node.status")
        assert status["status"] == "offline"

        with harness.client.websocket_connect(
            "/relay/v1/node", headers=node_ws_headers(bundle)
        ) as replay_ws:
            receive_type(replay_ws, "ready")
            replay = receive_type(replay_ws, "delivery")
            assert replay == delivery
            replay_ws.send_json({"v": 1, "type": "ack", "deliverySeq": 1})
            assert receive_type(replay_ws, "acknowledged")["deliverySeq"] == 1

        with harness.client.websocket_connect(
            "/relay/v1/node", headers=node_ws_headers(bundle)
        ) as resumed_ws:
            ready = receive_type(resumed_ws, "ready")
            assert ready["ackCursor"] == 1
            resumed_ws.send_json({"v": 1, "type": "ping", "nonce": "abcdefgh"})
            assert receive_type(resumed_ws, "pong")["nonce"] == "abcdefgh"


def test_response_correlation_and_cross_assistant_isolation(harness):
    first_bundle = harness.bootstrap()
    first_device = harness.enroll(first_bundle)
    second_bundle = harness.bootstrap()
    second_device = harness.enroll(second_bundle)
    request = command(first_device, "threads.list", {"archived": False})

    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(first_device)
    ) as device_ws, harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(first_bundle)
    ) as first_node:
        receive_type(device_ws, "ready")
        receive_type(first_node, "ready")
        receive_type(device_ws, "node.status")
        device_ws.send_json(request)
        receive_type(device_ws, "accepted")
        receive_type(first_node, "delivery")

        response = response_for(request)
        first_node.send_json(response)
        receive_type(first_node, "accepted")
        delivered = receive_type(device_ws, "delivery")
        assert delivered["payload"]["requestId"] == request["payload"]["requestId"]

    with harness.client.websocket_connect(
        "/relay/v1/node", headers=node_ws_headers(second_bundle)
    ) as second_node:
        receive_type(second_node, "ready")
        second_node.send_json(response_for(request))
        with pytest.raises(WebSocketDisconnect) as denied:
            second_node.receive_json()
        assert denied.value.code == 4403

    forged = command(
        first_device,
        "threads.list",
        {"archived": False},
    )
    forged["payload"]["deviceId"] = second_device.device_id
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(first_device)
    ) as socket:
        receive_type(socket, "ready")
        socket.send_json(forged)
        assert receive_disconnect(socket) == 4403


def test_strict_schema_forbidden_gateway_credentials_and_idempotency_conflict(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as socket:
        receive_type(socket, "ready")
        invalid = command(device, "threads.list", {"archived": False})
        invalid["payload"]["params"]["path"] = "/v1/threads"
        socket.send_json(invalid)
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4400

    message_id = random_id("msg_")
    first = command(
        device,
        "turns.start",
        {
            "threadId": "thread-1",
            "clientMessageId": "client-0001",
            "input": [{"type": "text", "text": "one"}],
        },
        message_id=message_id,
    )
    changed = json.loads(json.dumps(first))
    changed["payload"]["params"]["input"][0]["text"] = "two"
    with harness.client.websocket_connect(
        "/relay/v1/device", headers=device_ws_headers(device)
    ) as socket:
        receive_type(socket, "ready")
        socket.send_json(first)
        receive_type(socket, "accepted")
        socket.send_json(changed)
        conflict = receive_type(socket, "error")
        assert conflict["code"] == "IDEMPOTENCY_CONFLICT"


def test_duplicate_after_target_ack_is_accepted_without_redelivery(harness):
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
        delivery = receive_type(node_ws, "delivery")
        node_ws.send_json(
            {"v": 1, "type": "ack", "deliverySeq": delivery["deliverySeq"]}
        )
        receive_type(node_ws, "acknowledged")

        device_ws.send_json(request)
        duplicate = receive_type(device_ws, "accepted")
        assert duplicate["duplicate"] is True
        node_ws.send_json({"v": 1, "type": "ping", "nonce": "abcdefgh"})
        assert node_ws.receive_json() == {
            "v": 1,
            "type": "pong",
            "nonce": "abcdefgh",
        }


def test_replay_gate_keeps_live_delivery_sequence_strictly_increasing(harness):
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
    ) as device_ws:
        receive_type(device_ws, "ready")
        device_ws.send_json(first)
        assert receive_type(device_ws, "accepted")["deliverySeq"] == 1
        node_context = harness.client.websocket_connect(
            "/relay/v1/node", headers=node_ws_headers(bundle)
        )
        with node_context as node_ws:
            # Send immediately after the handshake, while server-side ready/replay
            # and live routing may otherwise race.
            device_ws.send_json(second)
            receive_type(node_ws, "ready")
            deliveries = [
                receive_type(node_ws, "delivery"),
                receive_type(node_ws, "delivery"),
            ]
            assert [frame["deliverySeq"] for frame in deliveries] == [1, 2]
