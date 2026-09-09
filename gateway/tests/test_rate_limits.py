from __future__ import annotations

import ipaddress
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from cheby_gateway.api import _client_source, create_app
from cheby_gateway.store import GatewayStore

from .helpers import install_proof_auth, make_context


class RateLimitStoreTests(unittest.TestCase):
    def test_fixed_window_is_durable_and_resets_deterministically(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="cheby-rate-limit-")
        self.addCleanup(tempdir.cleanup)
        path = str(Path(tempdir.name) / "gateway.sqlite3")
        store = GatewayStore(path)
        self.assertTrue(store.consume_rate_limit("send.device", "dev-1", 2, 60, 100).allowed)
        self.assertTrue(store.consume_rate_limit("send.device", "dev-1", 2, 60, 101).allowed)
        store.close()

        reopened = GatewayStore(path)
        self.addCleanup(reopened.close)
        blocked = reopened.consume_rate_limit("send.device", "dev-1", 2, 60, 102)
        self.assertFalse(blocked.allowed)
        self.assertEqual(58, blocked.retry_after_seconds)
        self.assertTrue(
            reopened.consume_rate_limit("send.device", "dev-1", 2, 60, 160).allowed
        )

    def test_limit_is_atomic_under_concurrency(self) -> None:
        context = make_context()
        self.addCleanup(context.close)

        def attempt(_: int) -> bool:
            return context.store.consume_rate_limit(
                "send.device", "dev-concurrent", 5, 60, 100
            ).allowed

        with ThreadPoolExecutor(max_workers=12) as executor:
            allowed = list(executor.map(attempt, range(24)))
        self.assertEqual(5, sum(allowed))

    def test_concurrent_new_subjects_after_expiry_respect_hard_cap(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        scope = "websocket.connect.source"
        with patch.object(GatewayStore, "RATE_LIMIT_BUCKETS_PER_SCOPE", 3):
            for index in range(3):
                self.assertTrue(
                    context.store.consume_rate_limit(
                        scope, "old-%d" % index, 10, 60, 100
                    ).allowed
                )

            def replace(index: int) -> bool:
                return context.store.consume_rate_limit(
                    scope, "new-%d" % index, 10, 60, 160
                ).allowed

            with ThreadPoolExecutor(max_workers=12) as executor:
                allowed = list(executor.map(replace, range(12)))
        self.assertEqual(3, sum(allowed))
        subjects = context.store._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_buckets WHERE scope = ?",
            (scope,),
        ).fetchone()[0]
        self.assertEqual(3, subjects)

    def test_group_limit_bounds_subject_cardinality_and_prunes_stale_rows(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        for source in ("198.51.100.1", "198.51.100.2"):
            decision = context.store.consume_rate_limits(
                [
                    ("pairing.source", source, 10, 60),
                    ("pairing.global", "all", 2, 60),
                ],
                now_epoch=100,
            )
            self.assertTrue(decision.allowed)
        blocked = context.store.consume_rate_limits(
            [
                ("pairing.source", "198.51.100.3", 10, 60),
                ("pairing.global", "all", 2, 60),
            ],
            now_epoch=100,
        )
        self.assertFalse(blocked.allowed)
        source_rows = context.store._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_buckets WHERE scope = ?",
            ("pairing.source",),
        ).fetchone()[0]
        self.assertEqual(2, source_rows)

        context.store.consume_rate_limits(
            [
                ("pairing.source", "198.51.100.fresh", 10, 60),
                ("pairing.global", "all", 2, 60),
            ],
            now_epoch=8 * 24 * 60 * 60,
        )
        remaining = context.store._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_buckets"
        ).fetchone()[0]
        self.assertEqual(2, remaining)

    def test_hard_subject_cap_rejects_novel_buckets_without_growing_db(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        with patch.object(GatewayStore, "RATE_LIMIT_BUCKETS_PER_SCOPE", 2):
            self.assertTrue(
                context.store.consume_rate_limit(
                    "websocket.connect.source", "198.51.100.1", 10, 60, 100
                ).allowed
            )
            self.assertTrue(
                context.store.consume_rate_limit(
                    "websocket.connect.source", "198.51.100.2", 10, 60, 100
                ).allowed
            )
            blocked = context.store.consume_rate_limit(
                "websocket.connect.source", "198.51.100.3", 10, 60, 100
            )
        self.assertFalse(blocked.allowed)
        count = context.store._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_buckets WHERE scope = ?",
            ("websocket.connect.source",),
        ).fetchone()[0]
        self.assertEqual(2, count)

    def test_expired_bucket_releases_cap_without_evicting_active_bucket(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        scope = "websocket.connect.source"
        with patch.object(GatewayStore, "RATE_LIMIT_BUCKETS_PER_SCOPE", 1):
            self.assertTrue(
                context.store.consume_rate_limit(
                    scope, "198.51.100.1", 10, 60, 100
                ).allowed
            )
            active_denial = context.store.consume_rate_limit(
                scope, "198.51.100.2", 10, 60, 159
            )
            self.assertFalse(active_denial.allowed)
            self.assertTrue(
                context.store.consume_rate_limit(
                    scope, "198.51.100.2", 10, 60, 160
                ).allowed
            )
        count = context.store._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_buckets WHERE scope = ?",
            (scope,),
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_backoff_bucket_is_not_pruned_when_window_expires(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        scope = "pairing.source"
        with patch.object(GatewayStore, "RATE_LIMIT_BUCKETS_PER_SCOPE", 1):
            self.assertTrue(
                context.store.consume_rate_limit(
                    scope, "198.51.100.1", 10, 60, 100
                ).allowed
            )
            context.store.penalize_rate_limit(
                scope,
                "198.51.100.1",
                base_delay_seconds=100,
                max_delay_seconds=100,
                window_seconds=60,
                now_epoch=150,
            )
            same_subject_backoff = context.store.consume_rate_limit(
                scope, "198.51.100.1", 10, 60, 200
            )
            self.assertFalse(same_subject_backoff.allowed)
            self.assertEqual(50, same_subject_backoff.retry_after_seconds)
            blocked_by_live_backoff = context.store.consume_rate_limit(
                scope, "198.51.100.2", 10, 60, 200
            )
            self.assertFalse(blocked_by_live_backoff.allowed)
            row = context.store._conn.execute(
                """
                SELECT blocked_until, expires_at FROM rate_limit_buckets
                WHERE scope = ?
                """,
                (scope,),
            ).fetchone()
            self.assertEqual((250, 250), (row["blocked_until"], row["expires_at"]))
            self.assertTrue(
                context.store.consume_rate_limit(
                    scope, "198.51.100.2", 10, 60, 250
                ).allowed
            )

    def test_default_websocket_limits_do_not_fill_cap_after_21_minutes(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        settings = context.settings
        for attempt in range(5_000):
            source = "198.51.%d.%d" % (attempt // 256, attempt % 256)
            decision = context.store.consume_rate_limits(
                [
                    (
                        "websocket.connect.source",
                        source,
                        settings.websocket_connect_source_limit,
                        settings.websocket_connect_window_seconds,
                    ),
                    (
                        "websocket.connect.global",
                        "all",
                        settings.websocket_connect_global_limit,
                        settings.websocket_connect_window_seconds,
                    ),
                ],
                now_epoch=attempt * 0.3,
            )
            self.assertTrue(decision.allowed, attempt)
        source_count = context.store._conn.execute(
            """
            SELECT COUNT(*) FROM rate_limit_buckets
            WHERE scope = 'websocket.connect.source'
            """
        ).fetchone()[0]
        self.assertLessEqual(source_count, settings.websocket_connect_global_limit)

    def test_pre_expiry_schema_migrates_to_bounded_dynamic_expiry(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="cheby-rate-migration-")
        self.addCleanup(tempdir.cleanup)
        path = str(Path(tempdir.name) / "gateway.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE rate_limit_buckets (
                scope TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                window_started_at REAL NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                blocked_until REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(scope, subject_hash)
            );
            INSERT INTO rate_limit_buckets VALUES
                ('pairing.source', 'legacy-active', 100, 1, 0, 0, 110),
                ('pairing.source', 'legacy-backoff', 100, 1, 2, 200, 120);
            """
        )
        conn.commit()
        conn.close()

        store = GatewayStore(path)
        self.addCleanup(store.close)
        rows = store._conn.execute(
            """
            SELECT subject_hash, window_seconds, expires_at
            FROM rate_limit_buckets ORDER BY subject_hash
            """
        ).fetchall()
        self.assertEqual(
            [
                ("legacy-active", 60, 170),
                ("legacy-backoff", 60, 200),
            ],
            [tuple(row) for row in rows],
        )
        self.assertEqual(12, store._conn.execute("PRAGMA user_version").fetchone()[0])

    def test_pairing_backoff_escalates_without_mutating_grant(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        context.store.ensure_pairing_grant(context.settings.pairing_secret)
        self.assertEqual(
            2,
            context.store.penalize_rate_limit(
                "pairing.source", "198.51.100.8", 2, 16, 60, now_epoch=100
            ),
        )
        blocked = context.store.consume_rate_limit(
            "pairing.source", "198.51.100.8", 10, 60, now_epoch=101
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(1, blocked.retry_after_seconds)
        self.assertEqual(
            4,
            context.store.penalize_rate_limit(
                "pairing.source", "198.51.100.8", 2, 16, 60, now_epoch=102
            ),
        )
        grant = context.store._conn.execute(
            "SELECT state, failed_attempts FROM pairing_grants"
        ).fetchone()
        self.assertEqual("active", grant["state"])
        self.assertEqual(0, grant["failed_attempts"])

    def test_legacy_locked_grant_is_reopened_but_consumed_grant_is_not(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        context.store.ensure_pairing_grant(context.settings.pairing_secret)
        context.store._conn.execute(
            "UPDATE pairing_grants SET state = 'locked', failed_attempts = 5"
        )
        context.store.ensure_pairing_grant(context.settings.pairing_secret)
        reopened = context.store._conn.execute(
            "SELECT state, failed_attempts FROM pairing_grants"
        ).fetchone()
        self.assertEqual(("active", 0), (reopened["state"], reopened["failed_attempts"]))

        context.store._conn.execute(
            "UPDATE pairing_grants SET state = 'consumed', consumed_at = ?",
            ("2026-07-19T00:00:00Z",),
        )
        context.store.ensure_pairing_grant(context.settings.pairing_secret)
        consumed = context.store._conn.execute(
            "SELECT state FROM pairing_grants"
        ).fetchone()[0]
        self.assertEqual("consumed", consumed)

    def test_rate_limit_subject_is_hashed_at_rest(self) -> None:
        context = make_context()
        self.addCleanup(context.close)
        source = "198.51.100.99"
        context.store.consume_rate_limit("pairing.source", source, 5, 60, 100)
        persisted = Path(context.settings.db_path).read_bytes()
        self.assertNotIn(source.encode("utf-8"), persisted)


class RateLimitAPITests(unittest.TestCase):
    @staticmethod
    def _pair(client: TestClient, context):
        response = client.post(
            "/v1/pairings/exchange",
            json={
                "pairingSecret": "test-pairing-secret",
                "deviceName": "JUY-AL00",
                "devicePublicKey": context.device_public_key,
            },
        )
        if response.status_code != 200:
            raise AssertionError(response.text)
        body = response.json()
        return body, {"Authorization": "Bearer %s" % body["accessToken"]}

    def test_wrong_pairing_code_backs_off_but_does_not_lock_grant(self) -> None:
        context = make_context(
            pairing_source_limit=10,
            pairing_global_limit=20,
            pairing_backoff_base_seconds=30,
            pairing_backoff_max_seconds=30,
        )
        self.addCleanup(context.close)
        app = create_app(context.settings, context.store, context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            denied = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "wrong-pairing-secret-value",
                    "deviceName": "Probe",
                    "devicePublicKey": context.device_public_key,
                },
            )
            self.assertEqual(401, denied.status_code)
            grant = context.store._conn.execute(
                "SELECT state, failed_attempts FROM pairing_grants"
            ).fetchone()
            self.assertEqual(("active", 0), (grant["state"], grant["failed_attempts"]))

            backed_off = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "test-pairing-secret",
                    "deviceName": "JUY-AL00",
                    "devicePublicKey": context.device_public_key,
                },
            )
            self._assert_rate_limited(backed_off)
            context.store.clear_rate_limit_penalty("pairing.source", "testclient")
            paired = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "test-pairing-secret",
                    "deviceName": "JUY-AL00",
                    "devicePublicKey": context.device_public_key,
                },
            )
            self.assertEqual(200, paired.status_code)

    def test_invalid_pairing_bodies_hit_global_limit_with_stable_429(self) -> None:
        context = make_context(pairing_source_limit=10, pairing_global_limit=2)
        self.addCleanup(context.close)
        app = create_app(context.settings, context.store, context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            self.assertEqual(422, client.post("/v1/pairings/exchange", json={}).status_code)
            self.assertEqual(422, client.post("/v1/pairings/exchange", json={}).status_code)
            limited = client.post("/v1/pairings/exchange", json={})
        self._assert_rate_limited(limited)

    def test_refresh_source_global_and_device_limits_are_persistent(self) -> None:
        context = make_context(
            refresh_source_limit=10,
            refresh_global_limit=2,
            refresh_device_limit=1,
        )
        self.addCleanup(context.close)
        app = create_app(context.settings, context.store, context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            pairing, _ = self._pair(client, context)
            self.assertEqual(422, client.post("/v1/auth/refresh", json={}).status_code)
            self.assertEqual(422, client.post("/v1/auth/refresh", json={}).status_code)
            self._assert_rate_limited(client.post("/v1/auth/refresh", json={}))

        # A restart sees the same durable global bucket.
        reopened = GatewayStore(context.settings.db_path)
        self.addCleanup(reopened.close)
        blocked = reopened.consume_rate_limit(
            "refresh.global",
            "all",
            context.settings.refresh_global_limit,
            context.settings.refresh_window_seconds,
        )
        self.assertFalse(blocked.allowed)

        # Device limiting is independently enforced after a valid device proof.
        device_context = make_context(
            refresh_source_limit=10,
            refresh_global_limit=10,
            refresh_device_limit=1,
        )
        self.addCleanup(device_context.close)
        device_app = create_app(
            device_context.settings,
            device_context.store,
            device_context.bridge,
        )
        with TestClient(device_app) as client:
            install_proof_auth(client, device_context)
            pairing, _ = self._pair(client, device_context)
            first = client.post(
                "/v1/auth/refresh",
                json={
                    "deviceId": pairing["deviceId"],
                    "refreshToken": pairing["refreshToken"],
                },
            )
            self.assertEqual(200, first.status_code, first.text)
            second = client.post(
                "/v1/auth/refresh",
                json={
                    "deviceId": pairing["deviceId"],
                    "refreshToken": first.json()["refreshToken"],
                },
            )
            self._assert_rate_limited(second)

    def test_untrusted_forwarded_for_cannot_rotate_source_identity(self) -> None:
        trusted_scope = {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "server": ("gateway", 443),
            "client": ("127.0.0.1", 50000),
            "headers": [(b"x-forwarded-for", b"198.51.100.20, 10.0.0.9")],
        }
        untrusted_scope = dict(trusted_scope)
        untrusted_scope["client"] = ("203.0.113.9", 50000)
        networks = (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("10.0.0.0/8"))
        self.assertEqual(
            "198.51.100.20", _client_source(Request(trusted_scope), networks)
        )
        self.assertEqual(
            "203.0.113.9", _client_source(Request(untrusted_scope), networks)
        )

    def test_send_and_approval_limits_are_per_device_and_retryable(self) -> None:
        context = make_context(send_device_limit=1, approval_device_limit=1)
        self.addCleanup(context.close)
        app = create_app(context.settings, context.store, context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            _, headers = self._pair(client, context)
            created = client.post("/v1/threads", headers=headers, json={})
            thread_id = created.json()["id"]
            first = client.post(
                "/v1/threads/%s/turns" % thread_id,
                headers=headers,
                json={
                    "clientMessageId": "client-rate-limit-one",
                    "input": [{"type": "text", "text": "first"}],
                },
            )
            self.assertEqual(202, first.status_code)
            send_limited = client.post(
                "/v1/threads/%s/turns" % thread_id,
                headers=headers,
                json={
                    "clientMessageId": "client-rate-limit-two",
                    "input": [{"type": "text", "text": "second"}],
                },
            )
            self._assert_rate_limited(send_limited)

            approval_body = {
                "decision": "approve",
                "actionToken": "action-token-valid-length",
            }
            first_missing = client.post(
                "/v1/approvals/approval-missing/decision",
                headers=headers,
                json=approval_body,
            )
            self.assertEqual(404, first_missing.status_code)
            approval_limited = client.post(
                "/v1/approvals/approval-missing/decision",
                headers=headers,
                json=approval_body,
            )
            self._assert_rate_limited(approval_limited)

    def test_websocket_connection_and_message_limits_close_with_retry_hint(self) -> None:
        context = make_context(
            websocket_connect_source_limit=10,
            websocket_connect_global_limit=20,
            websocket_connect_device_limit=2,
            websocket_message_device_limit=1,
        )
        self.addCleanup(context.close)
        app = create_app(context.settings, context.store, context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            pairing, headers = self._pair(client, context)
            device = context.store.active_device()
            event = context.store.append_event(
                device.id,
                "audit.action",
                {"action": "rate.limit.test"},
                thread_id="thr_rate_limit",
            )
            path = "/v1/events?streamId=%s&afterSeq=0" % pairing["streamId"]
            with client.websocket_connect(path, headers=headers) as socket:
                self.assertEqual(event.seq, socket.receive_json()["seq"])
                ack = {
                    "type": "ack",
                    "streamId": pairing["streamId"],
                    "seq": event.seq,
                }
                socket.send_json(ack)
                socket.send_json(ack)
                with self.assertRaises(WebSocketDisconnect) as message_limited:
                    socket.receive_json()
            self.assertEqual(4429, message_limited.exception.code)
            self.assertRegex(message_limited.exception.reason, r"^retry-after=\d+$")

            # The first connection consumed one of two device connection slots.
            with client.websocket_connect(path, headers=headers) as second:
                second.close()
            with client.websocket_connect(path, headers=headers) as third:
                with self.assertRaises(WebSocketDisconnect) as connection_limited:
                    third.receive_json()
            self.assertEqual(4429, connection_limited.exception.code)
            self.assertRegex(connection_limited.exception.reason, r"^retry-after=\d+$")

    @staticmethod
    def _assert_rate_limited(response) -> None:
        if response.status_code != 429:
            raise AssertionError(response.text)
        retry_after = response.headers.get("Retry-After")
        if retry_after is None or int(retry_after) < 1:
            raise AssertionError("missing Retry-After")
        error = response.json()["error"]
        if error != {
            "code": "RATE_LIMITED",
            "message": "Too many requests; retry later",
            "retryable": True,
        }:
            raise AssertionError(error)


if __name__ == "__main__":
    unittest.main()
