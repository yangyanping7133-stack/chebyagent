from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cheby_gateway.api import BoundedRequestBodyMiddleware, create_app
from cheby_gateway.pop import P256_ORDER, ProofError, verify_proof
from cheby_gateway.store import GatewayStore

from .helpers import DeviceProofAuth, install_proof_auth, make_context, proof_headers


class ProofPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = make_context()
        self.addCleanup(self.context.close)
        self.now = int(time.time())

    @staticmethod
    def _lower(headers: dict[str, str]) -> dict[str, str]:
        return {key.lower(): value for key, value in headers.items()}

    def _verify(self, headers, **overrides):
        values = {
            "public_key_spki": self.context.device_public_key,
            "headers": self._lower(headers),
            "method": "GET",
            "raw_target": b"/v1/threads?archived=false&order=newest",
            "body": b"",
            "bearer_token": "access-token",
            "now_epoch": self.now,
            "allowed_skew_seconds": 300,
        }
        values.update(overrides)
        return verify_proof(**values)

    def test_canonical_covers_raw_query_order_body_and_bearer(self) -> None:
        target = b"/v1/threads?archived=false&order=newest"
        headers = proof_headers(
            self.context.device_private_key,
            "GET",
            target,
            bearer_token="access-token",
            timestamp=self.now,
        )
        self._verify(headers)
        for change in (
            {"raw_target": b"/v1/threads?order=newest&archived=false"},
            {"method": "POST"},
            {"body": b"{}"},
            {"bearer_token": "other-token"},
        ):
            with self.assertRaises(ProofError):
                self._verify(headers, **change)

    def test_clock_wrong_key_curve_and_encoding_fail_closed(self) -> None:
        target = b"/v1/threads?archived=false&order=newest"
        headers = proof_headers(
            self.context.device_private_key,
            "GET",
            target,
            bearer_token="access-token",
            timestamp=self.now - 301,
        )
        with self.assertRaises(ProofError):
            self._verify(headers)

        oversized_timestamp = dict(headers)
        oversized_timestamp["X-Cheby-Timestamp"] = "9" * 10_000
        with self.assertRaises(ProofError):
            self._verify(oversized_timestamp)

        wrong_key = ec.generate_private_key(ec.SECP256R1())
        wrong_headers = proof_headers(
            wrong_key,
            "GET",
            target,
            bearer_token="access-token",
            timestamp=self.now,
        )
        with self.assertRaises(ProofError):
            self._verify(wrong_headers)

        p384 = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with self.assertRaises(ProofError):
            self._verify(
                proof_headers(
                    self.context.device_private_key,
                    "GET",
                    target,
                    bearer_token="access-token",
                    timestamp=self.now,
                ),
                public_key_spki=base64.b64encode(p384).decode("ascii"),
            )

        malformed = proof_headers(
            self.context.device_private_key,
            "GET",
            target,
            bearer_token="access-token",
            timestamp=self.now,
        )
        malformed["X-Cheby-Nonce"] += "="
        with self.assertRaises(ProofError):
            self._verify(malformed)

    def test_high_s_ecdsa_signature_is_rejected(self) -> None:
        target = b"/v1/threads?archived=false&order=newest"
        headers = proof_headers(
            self.context.device_private_key,
            "GET",
            target,
            bearer_token="access-token",
            timestamp=self.now,
        )
        signature = base64.b64decode(headers["X-Cheby-Signature"], validate=True)
        r_value, s_value = decode_dss_signature(signature)
        high_s = encode_dss_signature(r_value, P256_ORDER - s_value)
        headers["X-Cheby-Signature"] = base64.b64encode(high_s).decode("ascii")
        with self.assertRaises(ProofError):
            self._verify(headers)

    def test_nonce_claim_is_atomic_and_survives_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="cheby-proof-nonce-")
        self.addCleanup(tempdir.cleanup)
        path = str(Path(tempdir.name) / "gateway.sqlite3")
        store = GatewayStore(path)
        barrier = threading.Barrier(12)

        def claim(_: int) -> bool:
            barrier.wait(timeout=3)
            return store.claim_request_proof_nonce(
                "device:dev-proof",
                b"same-nonce-1234567890",
                1000,
                1000,
                601,
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(claim, range(12)))
        self.assertEqual(1, sum(results))
        store.close()

        reopened = GatewayStore(path)
        self.addCleanup(reopened.close)
        self.assertFalse(
            reopened.claim_request_proof_nonce(
                "device:dev-proof",
                b"same-nonce-1234567890",
                1000,
                1001,
                601,
            )
        )


class ProofAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = make_context(event_retention=20)
        self.app = create_app(
            self.context.settings,
            self.context.store,
            self.context.bridge,
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        install_proof_auth(self.client, self.context)
        response = self.client.post(
            "/v1/pairings/exchange",
            json={
                "pairingSecret": "test-pairing-secret",
                "deviceName": "Proof phone",
                "devicePublicKey": self.context.device_public_key,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        self.pairing = response.json()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.context.close()

    def test_wrong_key_and_bearer_binding_share_nonsensitive_auth_error(self) -> None:
        token = self.pairing["accessToken"]
        target = b"/v1/threads?archived=false"
        wrong_key = ec.generate_private_key(ec.SECP256R1())
        wrong_key_headers = {
            "Authorization": "Bearer " + token,
            **proof_headers(
                wrong_key,
                "GET",
                target,
                bearer_token=token,
            ),
        }
        self.client.auth = None
        wrong_key_response = self.client.get(
            "/v1/threads?archived=false",
            headers=wrong_key_headers,
        )

        bearer_mismatch_headers = {
            "Authorization": "Bearer " + token,
            **proof_headers(
                self.context.device_private_key,
                "GET",
                target,
                bearer_token="different-token",
            ),
        }
        bearer_mismatch = self.client.get(
            "/v1/threads?archived=false",
            headers=bearer_mismatch_headers,
        )
        expected = {
            "error": {
                "code": "AUTH_REQUIRED",
                "message": "Authentication is required",
                "retryable": False,
            }
        }
        self.assertEqual(
            (401, expected),
            (wrong_key_response.status_code, wrong_key_response.json()),
        )
        self.assertEqual((401, expected), (bearer_mismatch.status_code, bearer_mismatch.json()))
        self.assertNotIn(token, wrong_key_response.text + bearer_mismatch.text)

    def test_pairing_key_must_match_request_signer(self) -> None:
        context = make_context()
        app = create_app(context.settings, context.store, context.bridge)
        try:
            with TestClient(app) as client:
                wrong_key = ec.generate_private_key(ec.SECP256R1())
                client.auth = DeviceProofAuth(wrong_key)
                denied = client.post(
                    "/v1/pairings/exchange",
                    json={
                        "pairingSecret": "test-pairing-secret",
                        "deviceName": "Wrong signer",
                        "devicePublicKey": context.device_public_key,
                    },
                )
                self.assertEqual(401, denied.status_code)
                self.assertEqual("PAIRING_DENIED", denied.json()["error"]["code"])
                self.assertNotIn(context.device_public_key, denied.text)
        finally:
            context.close()

    def test_raw_query_order_is_signed_and_nonce_replay_is_rejected(self) -> None:
        token = self.pairing["accessToken"]
        target = b"/v1/threads?archived=false"
        headers = {
            "Authorization": "Bearer " + token,
            **proof_headers(
                self.context.device_private_key,
                "GET",
                target,
                bearer_token=token,
            ),
        }
        self.client.auth = None
        first = self.client.get("/v1/threads?archived=false", headers=headers)
        replay = self.client.get("/v1/threads?archived=false", headers=headers)
        tampered = self.client.get("/v1/threads?archived=true", headers=headers)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(401, replay.status_code)
        self.assertEqual(401, tampered.status_code)

    def test_refresh_is_device_bound_and_old_token_is_single_use(self) -> None:
        old_refresh = self.pairing["refreshToken"]
        wrong_device = self.client.post(
            "/v1/auth/refresh",
            json={
                "deviceId": "dev_unknown_device",
                "refreshToken": old_refresh,
            },
        )
        self.assertEqual(401, wrong_device.status_code)
        self.assertEqual("REFRESH_DENIED", wrong_device.json()["error"]["code"])

        valid = self.client.post(
            "/v1/auth/refresh",
            json={
                "deviceId": self.pairing["deviceId"],
                "refreshToken": old_refresh,
            },
        )
        self.assertEqual(200, valid.status_code, valid.text)
        replay = self.client.post(
            "/v1/auth/refresh",
            json={
                "deviceId": self.pairing["deviceId"],
                "refreshToken": old_refresh,
            },
        )
        self.assertEqual(401, replay.status_code)
        self.assertNotIn(old_refresh, replay.text + wrong_device.text)

    def test_websocket_requires_valid_pop_and_rejects_replayed_handshake(self) -> None:
        token = self.pairing["accessToken"]
        path = "/v1/events?streamId=%s&afterSeq=0" % self.pairing["streamId"]
        raw_target = path.encode("ascii")
        headers = {
            "Authorization": "Bearer " + token,
            **proof_headers(
                self.context.device_private_key,
                "GET",
                raw_target,
                bearer_token=token,
            ),
        }
        self.client.auth = None
        with self.client.websocket_connect(path, headers=headers) as socket:
            socket.close()
        with self.client.websocket_connect(path, headers=headers) as replayed:
            with self.assertRaises(WebSocketDisconnect) as replay_close:
                replayed.receive_json()
        self.assertEqual(4401, replay_close.exception.code)

        unsigned = {"Authorization": "Bearer " + token}
        with self.client.websocket_connect(path, headers=unsigned) as socket:
            with self.assertRaises(WebSocketDisconnect) as close:
                socket.receive_json()
        self.assertEqual(4401, close.exception.code)

    def test_oversized_event_becomes_small_sync_required_frame(self) -> None:
        device = self.context.store.active_device()
        oversized = self.context.store.append_event(
            device.id,
            "audit.action",
            {"action": "oversized", "fallbackText": "x" * (512 * 1024)},
            thread_id="thr_oversized",
        )
        path = "/v1/events?streamId=%s&afterSeq=%d" % (
            self.pairing["streamId"],
            oversized.seq - 1,
        )
        with self.client.websocket_connect(
            path,
            headers={"Authorization": "Bearer " + self.pairing["accessToken"]},
        ) as socket:
            message = socket.receive_json()
            self.assertEqual("sync.required", message["type"])
            self.assertEqual("eventTooLarge", message["payload"]["reason"])
            self.assertGreater(message["seq"], oversized.seq)
            self.assertLess(len(json.dumps(message).encode("utf-8")), 512 * 1024)
        later = self.context.store.append_event(
            device.id,
            "audit.action",
            {"action": "after.oversized"},
            thread_id="thr_after_oversized",
        )
        resume_path = "/v1/events?streamId=%s&afterSeq=%d" % (
            self.pairing["streamId"],
            message["seq"],
        )
        with self.client.websocket_connect(
            resume_path,
            headers={"Authorization": "Bearer " + self.pairing["accessToken"]},
        ) as socket:
            resumed = socket.receive_json()
            self.assertEqual(later.seq, resumed["seq"])
            self.assertEqual("after.oversized", resumed["payload"]["action"])

    def test_oversized_inbound_websocket_text_is_closed(self) -> None:
        path = "/v1/events?streamId=%s&afterSeq=0" % self.pairing["streamId"]
        with self.client.websocket_connect(
            path,
            headers={"Authorization": "Bearer " + self.pairing["accessToken"]},
        ) as socket:
            socket.send_text("x" * (16 * 1024 + 1))
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(4400, closed.exception.code)

    def test_oversized_unauthenticated_get_is_rejected_before_auth(self) -> None:
        self.client.auth = None
        response = self.client.request(
            "GET",
            "/v1/threads",
            content=b"x" * (self.context.settings.request_body_limit_bytes + 1),
        )
        self.assertEqual(413, response.status_code)
        self.assertEqual("REQUEST_TOO_LARGE", response.json()["error"]["code"])

    def test_message_limit_above_public_max_is_rejected(self) -> None:
        response = self.client.get(
            "/v1/threads/thr_unknown?messageLimit=101",
            headers={"Authorization": "Bearer " + self.pairing["accessToken"]},
        )
        self.assertEqual(422, response.status_code)


class BodyLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_is_stopped_before_downstream(self) -> None:
        context = make_context(request_body_limit_bytes=1024)
        self.addCleanup(context.close)
        downstream_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True

        middleware = BoundedRequestBodyMiddleware(downstream, context.settings)
        chunks = iter(
            [
                {"type": "http.request", "body": b"a" * 800, "more_body": True},
                {"type": "http.request", "body": b"b" * 800, "more_body": False},
            ]
        )
        sent = []

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/threads",
                "headers": [(b"transfer-encoding", b"chunked")],
            },
            receive,
            send,
        )
        self.assertFalse(downstream_called)
        self.assertEqual(413, sent[0]["status"])

    async def test_endpoint_specific_limits_cover_public_write_routes(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        middleware = BoundedRequestBodyMiddleware(lambda *_: None, context.settings)
        self.assertEqual(
            context.settings.pairing_body_limit_bytes,
            middleware._limit_for("/v1/pairings/exchange", "POST"),
        )
        self.assertEqual(
            context.settings.refresh_body_limit_bytes,
            middleware._limit_for("/v1/auth/refresh", "POST"),
        )
        self.assertEqual(
            context.settings.turn_body_limit_bytes,
            middleware._limit_for("/v1/threads/thr/turns", "POST"),
        )
        self.assertEqual(
            context.settings.request_body_limit_bytes,
            middleware._limit_for("/v1/approvals/apr/decision", "POST"),
        )

    async def test_oversized_and_ambiguous_content_length_are_rejected_early(self) -> None:
        context = make_context(request_body_limit_bytes=1024)
        self.addCleanup(context.close)
        downstream_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True

        async def never_receive():
            raise AssertionError("body must not be read after declared overflow")

        for headers, expected in (
            ([(b"content-length", b"1025")], 413),
            ([(b"content-length", b"01")], 400),
            ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
            (
                [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
                400,
            ),
        ):
            with self.subTest(headers=headers):
                sent = []

                async def send(message):
                    sent.append(message)

                await BoundedRequestBodyMiddleware(
                    downstream,
                    context.settings,
                )(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/v1/threads",
                        "headers": headers,
                    },
                    never_receive,
                    send,
                )
                self.assertEqual(expected, sent[0]["status"])
        self.assertFalse(downstream_called)


if __name__ == "__main__":
    unittest.main()
