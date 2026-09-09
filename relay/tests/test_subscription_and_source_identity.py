from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from cheby_relay.app import _source, create_app
from cheby_relay.config import RelaySettings
from cheby_relay.store import CorrelationError, RelayStore

from .conftest import (
    RelayHarness,
    command,
    device_ws_headers,
    pairing_body,
    proof_headers,
    random_id,
    receive_disconnect,
    receive_type,
    response_for,
)


def test_active_event_subscription_survives_request_gc_and_handoff_is_explicit(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    device_principal = harness.store.authenticate(device.access_token, "device")
    node_principal = harness.store.authenticate(bundle.node_token, "node")
    generation = harness.store.activate_node_generation(bundle.node_id)
    first = command(device, "events.subscribe", {"afterSeq": 0})
    first_result = harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=first["messageId"],
        payload=first["payload"],
        expires_at=int(time.time()) + 30,
    )
    harness.store.acknowledge(
        principal=node_principal,
        seq=first_result.delivery_seq,
        node_generation=generation,
    )
    first_response = response_for(first)
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=first_response["messageId"],
        payload=first_response["payload"],
        expires_at=None,
        node_generation=generation,
    )
    with harness.store._lock, harness.store._db:
        harness.store._db.execute(
            "UPDATE requests SET last_activity_at=0 WHERE assistant_id=? AND request_id=?",
            (bundle.assistant_id, first["payload"]["requestId"]),
        )
    harness.store.run_gc()
    assert harness.store.request_binding(
        bundle.assistant_id, first["payload"]["requestId"]
    ) is not None

    event = {
        "kind": "event",
        "requestId": first["payload"]["requestId"],
        "deviceId": device.device_id,
        "streamId": "stream-1",
        "eventId": "event-0001",
        "eventSeq": 1,
        "eventType": "turn.started",
        "threadId": "thread-1",
        "data": {"occurredAt": "2026-07-20T12:00:00Z", "payload": {}},
    }
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=random_id("msg_"),
        payload=event,
        expires_at=None,
        node_generation=generation,
    )

    second = command(device, "events.subscribe", {"afterSeq": 1})
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=second["messageId"],
        payload=second["payload"],
        expires_at=int(time.time()) + 30,
    )
    # The replacement is provisional: the old request remains active until
    # Node durably reports successful replacement.
    assert bool(
        harness.store.request_binding(
            bundle.assistant_id, first["payload"]["requestId"]
        )["subscription_active"]
    )
    replacement_response = response_for(second)
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=replacement_response["messageId"],
        payload=replacement_response["payload"],
        expires_at=None,
        node_generation=generation,
    )
    assert not bool(
        harness.store.request_binding(
            bundle.assistant_id, first["payload"]["requestId"]
        )["subscription_active"]
    )
    # Losing the separate Node delivery ack after a successful response must
    # not let TTL expiry undo the confirmed replacement.
    harness.store.run_gc(now=int(time.time()) + 31)
    assert bool(
        harness.store.request_binding(
            bundle.assistant_id, second["payload"]["requestId"]
        )["subscription_confirmed"]
    )
    # A late old event is accepted during the bounded handoff grace instead of
    # wedging the Connector durable outbox.
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=random_id("msg_"),
        payload={**event, "eventId": "event-0002", "eventSeq": 2},
        expires_at=None,
        node_generation=generation,
    )
    with harness.store._lock, harness.store._db:
        harness.store._db.execute(
            """UPDATE requests SET last_activity_at=0, subscription_closed_at=0
               WHERE assistant_id=? AND request_id=?""",
            (bundle.assistant_id, first["payload"]["requestId"]),
        )
    harness.store.run_gc()
    assert harness.store.request_binding(
        bundle.assistant_id, first["payload"]["requestId"]
    ) is None
    assert harness.store.request_binding(
        bundle.assistant_id, second["payload"]["requestId"]
    ) is not None
    with pytest.raises(CorrelationError):
        harness.store.enqueue(
            principal=node_principal,
            recipient_role="device",
            message_id=random_id("msg_"),
            payload={**event, "eventId": "event-0003", "eventSeq": 3},
            expires_at=None,
            node_generation=generation,
        )


def test_failed_or_expired_subscription_replacement_preserves_prior_stream(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    device_principal = harness.store.authenticate(device.access_token, "device")
    node_principal = harness.store.authenticate(bundle.node_token, "node")
    generation = harness.store.activate_node_generation(bundle.node_id)

    first = command(device, "events.subscribe", {"afterSeq": 0})
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=first["messageId"],
        payload=first["payload"],
        expires_at=int(time.time()) + 30,
    )
    first_response = response_for(first)
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=first_response["messageId"],
        payload=first_response["payload"],
        expires_at=None,
        node_generation=generation,
    )
    failed = command(device, "events.subscribe", {"afterSeq": 1})
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=failed["messageId"],
        payload=failed["payload"],
        expires_at=int(time.time()) + 30,
    )
    failed_response = {
        **response_for(failed)["payload"],
        "ok": False,
        "error": {"code": "SUBSCRIBE_FAILED", "message": "failed", "retryable": True},
    }
    failed_response.pop("result")
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=random_id("msg_"),
        payload=failed_response,
        expires_at=None,
        node_generation=generation,
    )
    first_binding = harness.store.request_binding(
        bundle.assistant_id, first["payload"]["requestId"]
    )
    failed_binding = harness.store.request_binding(
        bundle.assistant_id, failed["payload"]["requestId"]
    )
    assert bool(first_binding["subscription_active"])
    assert not bool(failed_binding["subscription_active"])

    expiring = command(device, "events.subscribe", {"afterSeq": 1})
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=expiring["messageId"],
        payload=expiring["payload"],
        expires_at=1,
    )
    harness.store.run_gc(now=int(time.time()))
    assert bool(
        harness.store.request_binding(
            bundle.assistant_id, first["payload"]["requestId"]
        )["subscription_active"]
    )
    assert not bool(
        harness.store.request_binding(
            bundle.assistant_id, expiring["payload"]["requestId"]
        )["subscription_active"]
    )


def test_confirmed_subscribe_payload_gc_does_not_require_cumulative_node_ack(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    device_principal = harness.store.authenticate(device.access_token, "device")
    node_principal = harness.store.authenticate(bundle.node_token, "node")
    generation = harness.store.activate_node_generation(bundle.node_id)
    subscribe = command(device, "events.subscribe", {"afterSeq": 0})
    harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=subscribe["messageId"],
        payload=subscribe["payload"],
        expires_at=int(time.time()) + 30,
    )
    response = response_for(subscribe)
    harness.store.enqueue(
        principal=node_principal,
        recipient_role="device",
        message_id=response["messageId"],
        payload=response["payload"],
        expires_at=None,
        node_generation=generation,
    )
    with harness.store._lock, harness.store._db:
        harness.store._db.execute(
            "UPDATE message_idempotency SET retained_until=0 WHERE message_id=?",
            (subscribe["messageId"],),
        )
    # Idempotency evidence must outlive the payload if a bounded sweep cannot
    # reclaim that payload yet; otherwise a later sweep loses its ack proof.
    harness.store.run_gc()
    with harness.store._lock:
        assert harness.store._db.execute(
            "SELECT 1 FROM deliveries WHERE message_id=?",
            (subscribe["messageId"],),
        ).fetchone()
        retained = harness.store._db.execute(
            "SELECT state FROM message_idempotency WHERE message_id=?",
            (subscribe["messageId"],),
        ).fetchone()
        assert retained["state"] == "acked"
    with harness.store._lock, harness.store._db:
        harness.store._db.execute(
            "UPDATE deliveries SET created_at=0 WHERE message_id=?",
            (subscribe["messageId"],),
        )
    result = harness.store.run_gc()
    assert result.deleted_deliveries == 1
    with harness.store._lock:
        delivery = harness.store._db.execute(
            "SELECT 1 FROM deliveries WHERE message_id=?",
            (subscribe["messageId"],),
        ).fetchone()
        idempotency = harness.store._db.execute(
            "SELECT state FROM message_idempotency WHERE message_id=?",
            (subscribe["messageId"],),
        ).fetchone()
    assert delivery is None
    assert idempotency is None
    assert harness.store.pending_deliveries(bundle.assistant_id, "node") == []


def test_unconfirmed_subscribe_is_not_exempt_from_request_retention(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    device_principal = harness.store.authenticate(device.access_token, "device")
    node_principal = harness.store.authenticate(bundle.node_token, "node")
    generation = harness.store.activate_node_generation(bundle.node_id)
    subscribe = command(device, "events.subscribe", {"afterSeq": 0})
    result = harness.store.enqueue(
        principal=device_principal,
        recipient_role="node",
        message_id=subscribe["messageId"],
        payload=subscribe["payload"],
        expires_at=int(time.time()) + 30,
    )
    # Node transport-acks but never emits the operation response. This leaves
    # active=1/confirmed=0 and must not create a permanent GC exemption.
    harness.store.acknowledge(
        principal=node_principal,
        seq=result.delivery_seq,
        node_generation=generation,
    )
    with harness.store._lock, harness.store._db:
        harness.store._db.execute(
            "UPDATE requests SET last_activity_at=0 WHERE request_id=?",
            (subscribe["payload"]["requestId"],),
        )
    gc_result = harness.store.run_gc()
    assert gc_result.deleted_requests == 1
    assert harness.store.request_binding(
        bundle.assistant_id, subscribe["payload"]["requestId"]
    ) is None


def test_trusted_proxy_source_header_is_exact_and_untrusted_spoof_is_ignored(tmp_path):
    settings = RelaySettings(
        db_path=str(tmp_path / "source.sqlite3"),
        trusted_proxy_cidrs=("127.0.0.0/8",),
        client_ip_header="x-relay-client-ip",
    )
    trusted_scope = {
        "client": ("127.0.0.1", 12345),
        "headers": [(b"x-relay-client-ip", b"198.51.100.10")],
    }
    assert _source(trusted_scope, settings) == "198.51.100.10"
    with pytest.raises(ValueError):
        _source({"client": ("127.0.0.1", 1), "headers": []}, settings)
    with pytest.raises(ValueError):
        _source(
            {
                "client": ("127.0.0.1", 1),
                "headers": [
                    (b"x-relay-client-ip", b"198.51.100.10"),
                    (b"x-relay-client-ip", b"198.51.100.11"),
                ],
            },
            settings,
        )
    spoofed = {
        "client": ("203.0.113.9", 1),
        "headers": [(b"x-relay-client-ip", b"198.51.100.10")],
    }
    assert _source(spoofed, settings) == "203.0.113.9"


def test_pairing_rate_bucket_isolated_by_assistant_behind_same_trusted_proxy(tmp_path):
    settings = RelaySettings(
        db_path=str(tmp_path / "rate.sqlite3"),
        pairing_rate_limit=1,
        trusted_proxy_cidrs=("127.0.0.0/8",),
        client_ip_header="x-relay-client-ip",
    )
    store = RelayStore(settings)
    app = create_app(settings, store=store)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            bundles = [store.bootstrap_assistant(), store.bootstrap_assistant()]
            for bundle in bundles:
                from cryptography.hazmat.primitives.asymmetric import ec

                key = ec.generate_private_key(ec.SECP256R1())
                body = pairing_body(bundle, key)
                response = client.post(
                    "/relay/v1/pairings/exchange",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-relay-client-ip": "198.51.100.20",
                        **proof_headers(
                            key,
                            method="POST",
                            target="/relay/v1/pairings/exchange",
                            body=body,
                        ),
                    },
                )
                assert response.status_code == 200, response.text
    finally:
        store.close()


def test_public_global_guard_rejects_before_attacker_scoped_key_allocation(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ec

    settings = RelaySettings(
        db_path=str(tmp_path / "global-rate.sqlite3"),
        pairing_rate_limit=1,
        rate_limiter_max_keys=110,
    )
    store = RelayStore(settings)
    app = create_app(settings, store=store)
    try:
        with TestClient(app) as client:
            bundle = store.bootstrap_assistant()
            key = ec.generate_private_key(ec.SECP256R1())
            template = json.loads(pairing_body(bundle, key))
            for _ in range(100):
                template["assistantId"] = random_id("asst_")
                response = client.post(
                    "/relay/v1/pairings/exchange",
                    content=json.dumps(template, separators=(",", ":")),
                    headers={"content-type": "application/json"},
                )
                assert response.status_code == 401
            limiter = app.state.public_rate_limiter
            assert limiter.tracked_key_count == 101
            for _ in range(20):
                template["assistantId"] = random_id("asst_")
                response = client.post(
                    "/relay/v1/pairings/exchange",
                    content=json.dumps(template, separators=(",", ":")),
                    headers={"content-type": "application/json"},
                )
                assert response.status_code == 429
            assert limiter.tracked_key_count == 101
    finally:
        store.close()


def test_websocket_connection_rate_isolated_by_authenticated_credential(tmp_path):
    settings = RelaySettings(
        db_path=str(tmp_path / "connection-rate.sqlite3"),
        connection_rate_limit=1,
    )
    store = RelayStore(settings)
    app = create_app(settings, store=store)
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            first = RelayHarness(settings, store, client).enroll(
                store.bootstrap_assistant()
            )
            second = RelayHarness(settings, store, client).enroll(
                store.bootstrap_assistant()
            )
            with client.websocket_connect(
                "/relay/v1/device", headers=device_ws_headers(first)
            ) as socket:
                receive_type(socket, "ready")
            with client.websocket_connect(
                "/relay/v1/device", headers=device_ws_headers(first)
            ) as socket:
                assert receive_disconnect(socket) == 4408
            with client.websocket_connect(
                "/relay/v1/device", headers=device_ws_headers(second)
            ) as socket:
                receive_type(socket, "ready")
    finally:
        store.close()


def test_proxy_configuration_requires_both_trust_boundary_and_header(tmp_path):
    with pytest.raises(ValueError):
        RelaySettings(
            db_path=str(tmp_path / "bad.sqlite3"),
            trusted_proxy_cidrs=("127.0.0.0/8",),
        )
    with pytest.raises(ValueError):
        RelaySettings(
            db_path=str(tmp_path / "bad2.sqlite3"),
            trusted_proxy_cidrs=("127.0.0.0/8",),
            client_ip_header="x-forwarded-for",
        )
    with pytest.raises(ValueError):
        RelaySettings(
            db_path=str(tmp_path / "bad3.sqlite3"),
            trusted_proxy_cidrs=("0.0.0.0/0",),
            client_ip_header="x-relay-client-ip",
        )
