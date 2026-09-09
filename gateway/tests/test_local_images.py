from __future__ import annotations

import io
import json
import os
import asyncio
import sqlite3
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from cheby_gateway.api import create_app
from cheby_gateway.assets import _safe_limits
from cheby_gateway.bridge import BridgeDeliveryUnknown, BridgeEvent, BridgeNotAccepted

from .helpers import install_proof_auth, make_context


def image_bytes(kind: str = "PNG", *, metadata: bool = False) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA" if kind == "PNG" else "RGB", (16, 12), (10, 20, 30, 200))
    if kind == "PNG":
        info = PngImagePlugin.PngInfo()
        if metadata:
            info.add_text("comment", "private metadata")
        image.save(output, format="PNG", pnginfo=info)
    else:
        options = {"comment": b"private metadata" if metadata else b""}
        if metadata:
            exif = Image.Exif()
            exif[274] = 6
            exif[270] = "private EXIF"
            options.update(exif=exif, icc_profile=b"private ICC")
        image.save(output, format="JPEG", **options)
    return output.getvalue()


class LocalImageGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = make_context(local_image_enabled=True)
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
                "deviceName": "Image phone",
                "devicePublicKey": self.context.device_public_key,
            },
        ).json()
        self.headers = {"Authorization": "Bearer " + pairing["accessToken"]}
        created = self.client.post(
            "/v1/threads", headers=self.headers, json={"title": "Images"}
        )
        self.thread_id = created.json()["id"]

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.context.close()

    def upload(self, body: bytes, client_asset_id: str, message_id: str = "client-image-message-001"):
        return self.client.put(
            "/v1/threads/%s/turn-inputs/%s/images/%s"
            % (self.thread_id, message_id, client_asset_id),
            headers={**self.headers, "Content-Type": "application/octet-stream"},
            content=body,
        )

    def test_assets_expose_upload_only_and_no_public_download_route(self) -> None:
        image_routes = {
            (method, route.path)
            for route in self.app.routes
            for method in (getattr(route, "methods", None) or set())
            if "/images/" in getattr(route, "path", "")
        }
        self.assertEqual(
            {
                (
                    "PUT",
                    "/v1/threads/{thread_id}/turn-inputs/{client_message_id}/images/{client_asset_id}",
                )
            },
            image_routes,
        )
        self.assertTrue(
            all("asset-staging" not in getattr(route, "path", "") for route in self.app.routes)
        )
        path = (
            "/v1/threads/%s/turn-inputs/client-image-message-001/images/%s"
            % (self.thread_id, uuid.uuid4())
        )
        self.assertEqual(405, self.client.get(path, headers=self.headers).status_code)
        self.assertEqual(405, self.client.head(path, headers=self.headers).status_code)

    def test_capability_upload_idempotency_and_metadata_stripping(self) -> None:
        capability = self.client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(200, capability.status_code)
        self.assertEqual("no-store", capability.headers["cache-control"])
        self.assertEqual(
            ["image/jpeg", "image/png"],
            capability.json()["inputs"]["localImage"]["mediaTypes"],
        )
        self.assertEqual(
            10,
            capability.json()["inputs"]["localImage"]["maxImagesPerTurn"],
        )
        asset_id = str(uuid.uuid4())
        source = image_bytes(metadata=True)
        first = self.upload(source, asset_id)
        second = self.upload(source, asset_id)
        self.assertEqual(201, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json()["assetRef"], second.json()["assetRef"])
        row = self.context.store._conn.execute("SELECT * FROM image_assets").fetchone()
        final_path = os.path.join(self.context.settings.asset_staging_dir, row["storage_name"])
        with Image.open(final_path) as normalized:
            self.assertNotIn("comment", normalized.info)
            self.assertFalse(normalized.getexif())
        conflict = self.upload(image_bytes("JPEG"), asset_id)
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("ASSET_IDEMPOTENCY_CONFLICT", conflict.json()["error"]["code"])

        oriented = self.upload(image_bytes("JPEG", metadata=True), str(uuid.uuid4()))
        self.assertEqual(201, oriented.status_code)
        self.assertEqual((12, 16), (oriented.json()["width"], oriented.json()["height"]))
        oriented_row = self.context.store._conn.execute(
            "SELECT * FROM image_assets WHERE asset_ref = ?",
            (oriented.json()["assetRef"],),
        ).fetchone()
        oriented_path = os.path.join(
            self.context.settings.asset_staging_dir, oriented_row["storage_name"]
        )
        with Image.open(oriented_path) as normalized:
            self.assertFalse(normalized.getexif())
            self.assertNotIn("icc_profile", normalized.info)
            self.assertNotIn("comment", normalized.info)

    def test_rejects_unsupported_truncated_polyglot_and_wrong_content_type(self) -> None:
        cases = (
            (b"GIF89a" + b"x" * 20, "IMAGE_UNSUPPORTED"),
            (image_bytes("JPEG")[:-2], "IMAGE_UNSAFE"),
            (image_bytes("PNG") + b"<svg/>", "IMAGE_UNSAFE"),
        )
        for source, code in cases:
            response = self.upload(source, str(uuid.uuid4()))
            self.assertEqual(code, response.json()["error"]["code"])
        wrong = self.client.put(
            "/v1/threads/%s/turn-inputs/client-image-message-001/images/%s"
            % (self.thread_id, uuid.uuid4()),
            headers={**self.headers, "Content-Type": "image/png"},
            content=image_bytes(),
        )
        self.assertEqual(415, wrong.status_code)

    def test_upload_requires_one_canonical_length_and_rejects_transfer_encoding(self) -> None:
        path = (
            "/v1/threads/%s/turn-inputs/client-image-message-001/images/%s"
            % (self.thread_id, uuid.uuid4())
        )
        request = self.client.build_request(
            "PUT",
            path,
            headers={**self.headers, "Content-Type": "application/octet-stream"},
            content=b"x",
        )
        del request.headers["content-length"]
        missing = self.client.send(request)
        self.assertEqual(411, missing.status_code)

        request = self.client.build_request(
            "PUT",
            path,
            headers={**self.headers, "Content-Type": "application/octet-stream"},
            content=b"x",
        )
        request.headers = httpx.Headers(
            list(request.headers.multi_items()) + [("Content-Length", "1")]
        )
        duplicate = self.client.send(request)
        self.assertEqual(400, duplicate.status_code)

        request = self.client.build_request(
            "PUT",
            path,
            headers={**self.headers, "Content-Type": "application/octet-stream"},
            content=b"x",
        )
        del request.headers["content-length"]
        request.headers["Transfer-Encoding"] = "chunked"
        transfer = self.client.send(request)
        self.assertEqual(400, transfer.status_code)

    def test_turn_claim_maps_only_internal_local_image_and_public_summary(self) -> None:
        asset = self.upload(image_bytes(), str(uuid.uuid4())).json()
        request = {
            "clientMessageId": "client-image-message-001",
            "input": [
                {"type": "text", "text": "inspect"},
                {"type": "image", "assetRef": asset["assetRef"]},
            ],
        }
        started = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json=request,
        )
        self.assertEqual(202, started.status_code)
        raw_turn = next(iter(self.context.bridge.turns.values()))
        self.assertEqual("localImage", raw_turn["input"][1]["type"])
        self.assertEqual("auto", raw_turn["input"][1]["detail"])
        self.assertRegex(raw_turn["input"][1]["path"], r"^/asset-staging/img_[a-f0-9]{64}\.png$")
        public_json = "\n".join(
            str(row[0])
            for row in self.context.store._conn.execute(
                "SELECT payload_json FROM message_snapshots UNION ALL SELECT payload_json FROM event_outbox"
            ).fetchall()
        )
        self.assertIn("[图片 × 1]", public_json)
        self.assertNotIn(asset["assetRef"], public_json)
        self.assertNotIn("/asset-staging/", public_json)
        self.assertNotIn("img_", public_json)

        changed = dict(request)
        changed["input"] = list(reversed(request["input"]))
        conflict = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json=changed,
        )
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("TURN_IDEMPOTENCY_CONFLICT", conflict.json()["error"]["code"])

    def test_tamper_is_detected_and_terminal_event_cleans_file(self) -> None:
        asset = self.upload(image_bytes(), str(uuid.uuid4())).json()
        row = self.context.store._conn.execute("SELECT * FROM image_assets").fetchone()
        path = os.path.join(self.context.settings.asset_staging_dir, row["storage_name"])
        with open(path, "rb") as handle:
            original = handle.read()
        with open(path, "r+b") as handle:
            handle.write(bytes([original[0] ^ 1]))
        rejected = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-001",
                "input": [{"type": "image", "assetRef": asset["assetRef"]}],
            },
        )
        self.assertEqual("ASSET_UNAVAILABLE", rejected.json()["error"]["code"])
        event = self.context.store._conn.execute(
            "SELECT payload_json FROM event_outbox WHERE type = 'asset.unavailable'"
        ).fetchone()
        self.assertEqual(
            {"clientMessageId": "client-image-message-001", "reason": "storageValidation"},
            json.loads(event[0]),
        )

    def test_v11_schema_contains_assets_cleanup_and_fingerprint(self) -> None:
        self.assertEqual(12, self.context.store._conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in self.context.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("image_assets", tables)
        self.assertIn("image_asset_cleanup_queue", tables)
        turn_columns = {
            row[1] for row in self.context.store._conn.execute("PRAGMA table_info(turns)")
        }
        self.assertIn("request_fingerprint", turn_columns)

    def test_concurrent_duplicate_upload_converges_and_binding_is_enforced(self) -> None:
        source = image_bytes()
        asset_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.upload(source, asset_id), range(2)))
        self.assertEqual([200, 201], sorted(response.status_code for response in responses))
        self.assertEqual(1, self.context.store._conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])
        asset_ref = responses[0].json()["assetRef"]
        wrong_binding = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-other",
                "input": [{"type": "image", "assetRef": asset_ref}],
            },
        )
        self.assertEqual("ASSET_UNAVAILABLE", wrong_binding.json()["error"]["code"])

    def test_terminal_turn_queues_then_removes_claimed_asset(self) -> None:
        asset = self.upload(image_bytes(), str(uuid.uuid4())).json()
        started = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-001",
                "input": [{"type": "image", "assetRef": asset["assetRef"]}],
            },
        )
        self.assertEqual(202, started.status_code)
        row = self.context.store._conn.execute("SELECT * FROM image_assets").fetchone()
        path = os.path.join(self.context.settings.asset_staging_dir, row["storage_name"])
        raw_turn = next(iter(self.context.bridge.turns.values()))
        self.client_context.portal.call(
            self.context.bridge.emit_event,
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_turn["threadId"],
                    "turn": {"id": raw_turn["id"], "status": "completed", "items": []},
                },
            ),
        )
        self.assertFalse(os.path.exists(path))
        self.assertEqual(0, self.context.store._conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0])
        self.assertEqual(0, self.context.store._conn.execute("SELECT COUNT(*) FROM image_asset_cleanup_queue").fetchone()[0])
        retried = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-001",
                "input": [{"type": "image", "assetRef": asset["assetRef"]}],
            },
        )
        self.assertEqual(202, retried.status_code)
        self.assertEqual(started.json()["id"], retried.json()["id"])
        self.assertEqual(1, self.context.bridge.start_turn_calls)

    def test_claimed_hard_expiry_emits_only_small_unavailable_event(self) -> None:
        asset = self.upload(image_bytes(), str(uuid.uuid4())).json()
        started = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-001",
                "input": [{"type": "image", "assetRef": asset["assetRef"]}],
            },
        )
        self.assertEqual(202, started.status_code)
        self.context.store._conn.execute(
            "UPDATE image_assets SET hard_expires_at = ?",
            ("2000-01-01T00:00:00Z",),
        )
        self.assertEqual(1, self.context.store.queue_expired_image_assets())
        row = self.context.store._conn.execute(
            "SELECT payload_json FROM event_outbox WHERE type = 'asset.unavailable'"
        ).fetchone()
        payload = json.loads(row[0])
        self.assertEqual(
            {"clientMessageId": "client-image-message-001", "reason": "expired"},
            payload,
        )
        self.assertNotIn(asset["assetRef"], row[0])
        self.assertNotIn("/asset-staging/", row[0])
        self.context.service.assets.cleanup()

    def test_more_than_ten_images_is_rejected_before_store_or_bridge(self) -> None:
        response = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json={
                "clientMessageId": "client-image-message-001",
                "input": [
                    {"type": "image", "assetRef": "ast_" + str(index) * 32}
                    for index in range(1, 12)
                ],
            },
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual(0, self.context.bridge.start_turn_calls)
        self.assertEqual(0, self.context.store._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])

    def test_not_accepted_retries_same_image_but_unknown_delivery_never_retries(self) -> None:
        asset = self.upload(image_bytes(), str(uuid.uuid4())).json()
        request = {
            "clientMessageId": "client-image-message-001",
            "input": [{"type": "image", "assetRef": asset["assetRef"]}],
        }
        original = self.context.bridge.start_turn
        calls = 0

        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BridgeNotAccepted("not written")
            return await original(*args, **kwargs)

        self.context.bridge.start_turn = fail_once
        first = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json=request,
        )
        second = self.client.post(
            "/v1/threads/%s/turns" % self.thread_id,
            headers=self.headers,
            json=request,
        )
        self.assertEqual("TURN_NOT_ACCEPTED", first.json()["error"]["code"])
        self.assertEqual(202, second.status_code)
        self.assertEqual(2, calls)

        other_asset = self.upload(
            image_bytes("JPEG"),
            str(uuid.uuid4()),
            message_id="client-image-unknown-002",
        ).json()
        unknown_calls = 0

        async def delivery_unknown(*args, **kwargs):
            nonlocal unknown_calls
            unknown_calls += 1
            raise BridgeDeliveryUnknown("write outcome unknown")

        self.context.bridge.start_turn = delivery_unknown
        unknown_request = {
            "clientMessageId": "client-image-unknown-002",
            "input": [{"type": "image", "assetRef": other_asset["assetRef"]}],
        }
        # Complete the first fake turn so the Thread admits the next test turn.
        self.context.store.apply_thread_turn_events(
            next(iter(self.context.store._conn.execute("SELECT device_id FROM turns")))[0],
            self.thread_id,
            events=[],
            thread_status="idle",
            turn_id=second.json()["id"],
            turn_status="completed",
        )
        for _ in range(2):
            response = self.client.post(
                "/v1/threads/%s/turns" % self.thread_id,
                headers=self.headers,
                json=unknown_request,
            )
            self.assertEqual(
                "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                response.json()["error"]["code"],
            )
        self.assertEqual(1, unknown_calls)


class DisabledLocalImageCapabilityTests(unittest.TestCase):
    def test_worker_infinite_hard_limits_are_replaced_and_fail_closed(self) -> None:
        import resource

        with mock.patch.object(
            resource,
            "getrlimit",
            return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        ), mock.patch.object(resource, "setrlimit") as set_limit:
            _safe_limits()
        expected_soft = (8, 768 * 1024 * 1024, 16 * 1024 * 1024, 32)
        self.assertEqual(expected_soft, tuple(call.args[1][0] for call in set_limit.call_args_list))
        self.assertTrue(
            all(call.args[1][1] == resource.RLIM_INFINITY for call in set_limit.call_args_list)
        )

        with mock.patch.object(
            resource,
            "getrlimit",
            return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        ), mock.patch.object(
            resource, "setrlimit", side_effect=OSError("denied")
        ), mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(OSError):
                _safe_limits()

    def test_close_cancels_and_awaits_asset_sweeper(self) -> None:
        context = make_context(local_image_enabled=True)

        async def exercise() -> None:
            await context.service.start()
            asset_task = context.service._asset_sweeper_task
            self.assertIsNotNone(asset_task)
            self.assertFalse(asset_task.done())
            await context.service.close()
            self.assertTrue(asset_task.done())
            self.assertTrue(asset_task.cancelled())

        try:
            asyncio.run(exercise())
        finally:
            context.close()

    def test_disabled_capability_is_empty(self) -> None:
        context = make_context()
        app = create_app(settings=context.settings, store=context.store, bridge=context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            pairing = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "test-pairing-secret",
                    "deviceName": "No image phone",
                    "devicePublicKey": context.device_public_key,
                },
            ).json()
            response = client.get(
                "/v1/capabilities",
                headers={"Authorization": "Bearer " + pairing["accessToken"]},
            )
            self.assertEqual({}, response.json()["inputs"])
        context.close()

    def test_version_and_directory_gates_fail_closed(self) -> None:
        for mutate in ("version", "mode"):
            context = make_context(local_image_enabled=True)
            if mutate == "version":
                context.bridge._runtime_version = "0.144.5"
            else:
                os.chmod(context.settings.asset_staging_dir, 0o755)
            app = create_app(
                settings=context.settings,
                store=context.store,
                bridge=context.bridge,
            )
            with TestClient(app) as client:
                install_proof_auth(client, context)
                pairing = client.post(
                    "/v1/pairings/exchange",
                    json={
                        "pairingSecret": "test-pairing-secret",
                        "deviceName": "Gated phone",
                        "devicePublicKey": context.device_public_key,
                    },
                ).json()
                response = client.get(
                    "/v1/capabilities",
                    headers={"Authorization": "Bearer " + pairing["accessToken"]},
                )
                self.assertEqual({}, response.json()["inputs"])
            context.close()

    def test_restart_drains_cleanup_queue_parts_and_orphans(self) -> None:
        context = make_context(local_image_enabled=True)
        app = create_app(settings=context.settings, store=context.store, bridge=context.bridge)
        with TestClient(app) as client:
            install_proof_auth(client, context)
            pairing = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "test-pairing-secret",
                    "deviceName": "Restart phone",
                    "devicePublicKey": context.device_public_key,
                },
            ).json()
            headers = {"Authorization": "Bearer " + pairing["accessToken"]}
            thread_id = client.post(
                "/v1/threads", headers=headers, json={"title": "Restart"}
            ).json()["id"]
            response = client.put(
                "/v1/threads/%s/turn-inputs/client-restart-message/images/%s"
                % (thread_id, uuid.uuid4()),
                headers={**headers, "Content-Type": "application/octet-stream"},
                content=image_bytes(),
            )
            self.assertEqual(201, response.status_code)
        row = context.store._conn.execute("SELECT storage_name FROM image_assets").fetchone()
        queued_name = str(row["storage_name"])
        with context.store._transaction() as conn:
            conn.execute(
                "INSERT INTO image_asset_cleanup_queue VALUES (?, 'crashWindow', ?)",
                (queued_name, "2026-07-19T00:00:00Z"),
            )
            conn.execute("DELETE FROM image_assets")
        part_path = os.path.join(context.settings.asset_staging_dir, ".crash.part")
        orphan_name = "img_%s.png" % ("f" * 64)
        orphan_path = os.path.join(context.settings.asset_staging_dir, orphan_name)
        for path in (part_path, orphan_path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, b"orphan")
            os.close(fd)
        linked_part = os.path.join(
            context.settings.asset_staging_dir, ".normalized_crash.part"
        )
        linked_final = os.path.join(
            context.settings.asset_staging_dir, "img_%s.png" % ("e" * 64)
        )
        fd = os.open(linked_part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"interrupted publish")
        os.close(fd)
        os.link(linked_part, linked_final)

        restarted = create_app(
            settings=context.settings,
            store=context.store,
            bridge=context.bridge,
        )
        with TestClient(restarted):
            self.assertFalse(os.path.exists(os.path.join(context.settings.asset_staging_dir, queued_name)))
            self.assertFalse(os.path.exists(part_path))
            self.assertFalse(os.path.exists(orphan_path))
            self.assertFalse(os.path.exists(linked_part))
            self.assertFalse(os.path.exists(linked_final))
            self.assertEqual(
                0,
                context.store._conn.execute(
                    "SELECT COUNT(*) FROM image_asset_cleanup_queue"
                ).fetchone()[0],
            )
        context.close()
