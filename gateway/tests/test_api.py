from __future__ import annotations

import unittest

from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient

from cheby_gateway.api import create_app

from .helpers import install_proof_auth, make_context


class GatewayAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = make_context(event_retention=3)
        self.app = create_app(
            settings=self.context.settings,
            store=self.context.store,
            bridge=self.context.bridge,
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        install_proof_auth(self.client, self.context)
        pairing = self.client.post(
            "/v1/pairings/exchange",
            json={
                "pairingSecret": "test-pairing-secret",
                "deviceName": "JUY-AL00",
                "devicePublicKey": self.context.device_public_key,
            },
        )
        self.assertEqual(200, pairing.status_code)
        body = pairing.json()
        self.token = body["accessToken"]
        self.device_id = body["deviceId"]
        self.refresh_token = body["refreshToken"]
        self.refresh_expires_at = body["refreshExpiresAt"]
        self.stream_id = body["streamId"]
        self.headers = {"Authorization": "Bearer %s" % self.token}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.context.close()

    def test_duplicate_send_and_generic_rpc_rejection(self) -> None:
        created = self.client.post(
            "/v1/threads",
            headers=self.headers,
            json={"title": "API idempotency"},
        )
        self.assertEqual(201, created.status_code)
        thread_id = created.json()["id"]
        request = {
            "clientMessageId": "client-message-api-123",
            "input": [{"type": "text", "text": "Run once"}],
        }

        first = self.client.post(
            "/v1/threads/%s/turns" % thread_id,
            headers=self.headers,
            json=request,
        )
        second = self.client.post(
            "/v1/threads/%s/turns" % thread_id,
            headers=self.headers,
            json=request,
        )

        self.assertEqual(202, first.status_code)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        busy = self.client.post(
            "/v1/threads/%s/turns" % thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-message-api-other",
                "input": [{"type": "text", "text": "Must not start"}],
            },
        )
        self.assertEqual(409, busy.status_code)
        self.assertEqual("THREAD_BUSY", busy.json()["error"]["code"])
        self.assertTrue(busy.json()["error"]["retryable"])
        self.assertEqual("2", busy.headers["retry-after"])
        rejected = self.client.post(
            "/v1/codex/rpc",
            headers=self.headers,
            json={"method": "fs/readFile", "params": {}},
        )
        self.assertEqual(404, rejected.status_code)

    def test_validation_error_does_not_echo_pairing_secret(self) -> None:
        secret = "short"
        response = self.client.post(
            "/v1/pairings/exchange",
            json={
                "pairingSecret": secret,
                "deviceName": "JUY-AL00",
                "devicePublicKey": self.context.device_public_key,
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertNotIn(secret, response.text)

        valid_but_wrong_secret = "wrong-pairing-secret-value"
        denied = self.client.post(
            "/v1/pairings/exchange",
            json={
                "pairingSecret": valid_but_wrong_secret,
                "deviceName": "JUY-AL00",
                "devicePublicKey": self.context.device_public_key,
            },
        )
        self.assertEqual(401, denied.status_code)
        self.assertNotIn(valid_but_wrong_secret, denied.text)

    def test_auth_error_does_not_echo_access_token(self) -> None:
        invalid_token = self.token + "-invalid"
        response = self.client.get(
            "/v1/threads",
            headers={"Authorization": "Bearer %s" % invalid_token},
        )

        self.assertEqual(401, response.status_code)
        self.assertNotIn(invalid_token, response.text)

    def test_permanent_delete_requires_body_token_and_never_reflects_it(self) -> None:
        created = self.client.post(
            "/v1/threads",
            headers=self.headers,
            json={"title": "Delete safely"},
        )
        self.assertEqual(201, created.status_code)
        thread_id = created.json()["id"]
        archived = self.client.patch(
            "/v1/threads/%s" % thread_id,
            headers=self.headers,
            json={"archived": True},
        )
        self.assertEqual(200, archived.status_code)

        preview = self.client.post(
            "/v1/threads/%s/delete-preview" % thread_id,
            headers=self.headers,
        )
        self.assertEqual(200, preview.status_code)
        self.assertEqual("no-store", preview.headers.get("cache-control"))
        impact_token = preview.json()["impactToken"]
        self.assertEqual(1, preview.json()["affectedCount"])
        self.assertNotIn("raw-thread", preview.text)

        legacy_query = self.client.delete(
            "/v1/threads/%s?confirmPermanentDelete=true&impactToken=%s"
            % (thread_id, impact_token),
            headers=self.headers,
        )
        self.assertEqual(422, legacy_query.status_code)
        self.assertNotIn(impact_token, legacy_query.text)

        invalid_confirmation = self.client.request(
            "DELETE",
            "/v1/threads/%s" % thread_id,
            headers=self.headers,
            json={
                "confirmPermanentDelete": False,
                "impactToken": impact_token,
            },
        )
        self.assertEqual(422, invalid_confirmation.status_code)
        self.assertNotIn(impact_token, invalid_confirmation.text)

        deleted = self.client.request(
            "DELETE",
            "/v1/threads/%s" % thread_id,
            headers=self.headers,
            json={
                "confirmPermanentDelete": True,
                "impactToken": impact_token,
            },
        )
        self.assertEqual(200, deleted.status_code)
        self.assertEqual({"deleted": True}, deleted.json())

    def test_refresh_rotates_tokens_and_replay_is_rejected(self) -> None:
        refreshed = self.client.post(
            "/v1/auth/refresh",
            json={"deviceId": self.device_id, "refreshToken": self.refresh_token},
        )

        self.assertEqual(200, refreshed.status_code)
        body = refreshed.json()
        self.assertEqual(self.stream_id, body["streamId"])
        self.assertIn("deviceId", body)
        self.assertIn("accessToken", body)
        self.assertIn("expiresAt", body)
        self.assertIn("refreshToken", body)
        self.assertIn("refreshExpiresAt", body)
        self.assertNotEqual(self.token, body["accessToken"])
        self.assertNotEqual(self.refresh_token, body["refreshToken"])

        replay = self.client.post(
            "/v1/auth/refresh",
            json={"deviceId": self.device_id, "refreshToken": self.refresh_token},
        )
        self.assertEqual(401, replay.status_code)
        self.assertEqual("REFRESH_DENIED", replay.json()["error"]["code"])
        self.assertNotIn(self.refresh_token, replay.text)
        old_access = self.client.get("/v1/threads", headers=self.headers)
        self.assertEqual(401, old_access.status_code)
        new_access = self.client.get(
            "/v1/threads",
            headers={"Authorization": "Bearer %s" % body["accessToken"]},
        )
        self.assertEqual(200, new_access.status_code)

        self.assertTrue(self.context.store.revoke_device(body["deviceId"]))
        revoked = self.client.post(
            "/v1/auth/refresh",
            json={"deviceId": self.device_id, "refreshToken": body["refreshToken"]},
        )
        self.assertEqual(401, revoked.status_code)
        self.assertEqual("REFRESH_DENIED", revoked.json()["error"]["code"])

    def test_websocket_replays_and_accepts_ack(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        first = self.context.store.append_event(
            device.id,
            "audit.action",
            {"action": "test.one"},
            thread_id="thr_test",
        )
        second = self.context.store.append_event(
            device.id,
            "audit.action",
            {"action": "test.two"},
            thread_id="thr_test",
        )
        replay = self.context.store.replay_events(
            device.id,
            self.stream_id,
            0,
        )

        with self.client.websocket_connect(
            "/v1/events?streamId=%s&afterSeq=0" % self.stream_id,
            headers=self.headers,
        ) as socket:
            received = [socket.receive_json() for _ in replay.events]
            self.assertEqual(
                [event.seq for event in replay.events],
                [event["seq"] for event in received],
            )
            socket.send_json(
                {
                    "type": "ack",
                    "streamId": self.stream_id,
                    "seq": second.seq,
                }
            )
        self.assertLess(first.seq, second.seq)

    def test_websocket_gap_emits_sync_required(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        for index in range(5):
            self.context.store.append_event(
                device.id,
                "audit.action",
                {"action": "overflow", "index": index},
                thread_id="thr_test",
            )

        with self.client.websocket_connect(
            "/v1/events?streamId=%s&afterSeq=0" % self.stream_id,
            headers=self.headers,
        ) as socket:
            message = socket.receive_json()
            self.assertEqual("sync.required", message["type"])
            self.assertEqual(self.stream_id, message["streamId"])

    def test_websocket_requires_matching_stream_id(self) -> None:
        with self.client.websocket_connect(
            "/v1/events?afterSeq=0",
            headers=self.headers,
        ) as missing_socket:
            with self.assertRaises(WebSocketDisconnect) as missing:
                missing_socket.receive_json()
        self.assertEqual(4400, missing.exception.code)

        with self.client.websocket_connect(
            "/v1/events?streamId=stream_stale&afterSeq=99",
            headers=self.headers,
        ) as socket:
            message = socket.receive_json()
            self.assertEqual("sync.required", message["type"])
            self.assertEqual("streamChanged", message["payload"]["reason"])
            self.assertEqual(self.stream_id, message["streamId"])
            self.assertNotIn("turnId", message)
            self.assertNotIn("itemId", message)
            with self.assertRaises(WebSocketDisconnect) as changed:
                socket.receive_json()
            self.assertEqual(4409, changed.exception.code)

    def _assert_websocket_unauthorized(self, token: str, device=None) -> None:
        device = device or self.context.store.active_device()
        self.assertIsNotNone(device)
        self.context.store.append_event(
            device.id,
            "audit.action",
            {"action": "must.not.replay.before.auth"},
            thread_id="thr_auth_probe",
        )
        before = self.context.store.replay_events(
            device.id,
            device.stream_id,
            0,
        ).current_seq
        with self.client.websocket_connect(
            "/v1/events?streamId=%s&afterSeq=0" % device.stream_id,
            headers={"Authorization": "Bearer %s" % token},
        ) as socket:
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(4401, closed.exception.code)
        self.assertNotIn(token, str(closed.exception))
        after = self.context.store.replay_events(
            device.id,
            device.stream_id,
            0,
        ).current_seq
        self.assertEqual(before, after)

    def test_websocket_invalid_token_accepts_then_closes_4401(self) -> None:
        self._assert_websocket_unauthorized(self.token + "-invalid")

    def test_websocket_expired_token_accepts_then_closes_4401(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        self.context.store._conn.execute(
            "UPDATE devices SET token_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", device.id),
        )
        self._assert_websocket_unauthorized(self.token)

    def test_websocket_revoked_token_accepts_then_closes_4401(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        self.assertTrue(self.context.store.revoke_device(device.id))
        self._assert_websocket_unauthorized(self.token, device=device)

    def test_open_websocket_revocation_closes_4401_before_new_event(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        cursor = self.context.store.replay_events(
            device.id,
            device.stream_id,
            0,
        ).current_seq
        with self.client.websocket_connect(
            "/v1/events?streamId=%s&afterSeq=%d" % (self.stream_id, cursor),
            headers=self.headers,
        ) as socket:
            self.assertTrue(self.context.store.revoke_device(device.id))
            self.context.store.append_event(
                device.id,
                "audit.action",
                {"action": "must.not.leak.after.revoke"},
                thread_id="thr_auth_probe",
            )
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(4401, closed.exception.code)

    def test_open_websocket_expiry_closes_4401_before_new_event(self) -> None:
        device = self.context.store.authenticate(self.token)
        self.assertIsNotNone(device)
        cursor = self.context.store.replay_events(
            device.id,
            device.stream_id,
            0,
        ).current_seq
        with self.client.websocket_connect(
            "/v1/events?streamId=%s&afterSeq=%d" % (self.stream_id, cursor),
            headers=self.headers,
        ) as socket:
            self.context.store._conn.execute(
                "UPDATE devices SET token_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", device.id),
            )
            self.context.store.append_event(
                device.id,
                "audit.action",
                {"action": "must.not.leak.after.expiry"},
                thread_id="thr_auth_probe",
            )
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(4401, closed.exception.code)


if __name__ == "__main__":
    unittest.main()
