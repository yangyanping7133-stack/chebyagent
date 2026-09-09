from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from cheby_gateway.service import GatewayError

from .helpers import make_context, pair


class EventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _started_context(self, **settings):
        context = make_context(**settings)
        await context.service.start()
        self.addCleanup(context.close)
        self.addAsyncCleanup(context.service.close)
        return context

    async def test_replay_is_ordered_and_ack_is_monotonic(self) -> None:
        context = await self._started_context(event_retention=10)
        _, device = pair(context)

        first = context.store.append_event(
            device.id,
            "audit.action",
            {"action": "test.one"},
            thread_id="thr_test",
        )
        second = context.store.append_event(
            device.id,
            "audit.action",
            {"action": "test.two"},
            thread_id="thr_test",
        )
        replay = context.store.replay_events(
            device.id,
            device.stream_id,
            first.seq - 1,
        )

        self.assertFalse(replay.sync_required)
        self.assertEqual([first.seq, second.seq], [event.seq for event in replay.events])
        self.assertTrue(
            context.store.acknowledge(device.id, device.stream_id, second.seq)
        )
        self.assertTrue(
            context.store.acknowledge(device.id, device.stream_id, first.seq)
        )
        self.assertFalse(
            context.store.acknowledge(device.id, device.stream_id, second.seq + 10)
        )

    async def test_replay_gap_requires_snapshot_sync(self) -> None:
        context = await self._started_context(event_retention=2)
        _, device = pair(context)
        for index in range(4):
            context.store.append_event(
                device.id,
                "audit.action",
                {"action": "test.event", "index": index},
                thread_id="thr_test",
            )

        replay = context.store.replay_events(device.id, device.stream_id, 0)

        self.assertTrue(replay.sync_required)
        self.assertEqual([], replay.events)
        self.assertEqual(4, replay.current_seq)

    async def test_wrong_stream_never_replays_old_cursor(self) -> None:
        context = await self._started_context()
        _, device = pair(context)
        context.store.append_event(
            device.id,
            "audit.action",
            {"action": "test.event"},
            thread_id="thr_test",
        )

        replay = context.store.replay_events(device.id, "stream_stale", 0)

        self.assertTrue(replay.sync_required)
        self.assertEqual(device.stream_id, replay.stream_id)
        self.assertEqual([], replay.events)

    async def test_public_event_type_and_thread_are_centrally_enforced(self) -> None:
        context = await self._started_context()
        _, device = pair(context)

        with self.assertRaises(ValueError):
            context.store.append_event(
                device.id,
                "codex.event",
                {},
                thread_id="thr_test",
            )
        with self.assertRaises(ValueError):
            context.store.append_event(device.id, "audit.action", {})

    async def test_plain_access_token_is_not_persisted(self) -> None:
        context = await self._started_context()
        response, _ = pair(context)

        database_bytes = (context.tempdir.name + "/gateway.sqlite3")
        with open(database_bytes, "rb") as handle:
            persisted = handle.read()

        self.assertNotIn(response.access_token.encode("utf-8"), persisted)
        self.assertNotIn(response.refresh_token.encode("utf-8"), persisted)

    async def test_relay_device_is_idempotent_and_keeps_one_stream(self) -> None:
        context = await self._started_context()
        device_id = "dev_AAAAAAAAAAAAAAAAAAAAAA"

        first = context.service.ensure_relay_device(device_id, "JUY-AL00")
        context.store.append_event(
            first.id,
            "audit.action",
            {"action": "relay.connected"},
            thread_id="thr_test",
        )
        second = context.service.ensure_relay_device(device_id, "Primary phone")

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.stream_id, second.stream_id)
        self.assertEqual("Primary phone", second.name)
        replay = context.store.replay_events(second.id, second.stream_id, 0)
        self.assertFalse(replay.sync_required)
        self.assertEqual(1, len(replay.events))

    async def test_relay_device_revokes_legacy_direct_device(self) -> None:
        context = await self._started_context()
        response, direct = pair(context)

        relay = context.service.ensure_relay_device(
            "dev_BBBBBBBBBBBBBBBBBBBBBB",
            "JUY-AL00",
        )

        self.assertNotEqual(direct.id, relay.id)
        with self.assertRaises(GatewayError) as revoked:
            context.service.authenticate(response.access_token)
        self.assertEqual("DEVICE_REVOKED", revoked.exception.code)
        self.assertEqual(relay.id, context.store.active_device().id)

    async def test_relay_device_rejects_untrusted_identity_fields(self) -> None:
        context = await self._started_context()

        for device_id in (
            "dev_short",
            "dev_AAAAAAAAAAAAAAAAAAAAA!",
            "node_AAAAAAAAAAAAAAAAAAAAAA",
        ):
            with self.subTest(device_id=device_id):
                with self.assertRaises(ValueError):
                    context.service.ensure_relay_device(device_id, "JUY-AL00")
        for name in ("", "phone\nspoof", "x" * 121):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    context.service.ensure_relay_device(
                        "dev_CCCCCCCCCCCCCCCCCCCCCC",
                        name,
                    )

    async def test_switch_snapshot_is_complete_at_its_global_cursor(self) -> None:
        context = await self._started_context()
        _, device = pair(context)
        thread_a = context.store.get_or_create_thread("raw-thread-a", title="A")
        thread_b = context.store.get_or_create_thread("raw-thread-b", title="B")
        event_a = context.store.append_event(
            device.id,
            "audit.action",
            {"action": "subscribed.a"},
            thread_id=thread_a.id,
        )
        barrier = threading.Barrier(2)
        written = {}

        def acknowledge_a():
            barrier.wait(timeout=2)
            return context.store.acknowledge(
                device.id,
                device.stream_id,
                event_a.seq,
            )

        def publish_b():
            barrier.wait(timeout=2)
            payload = {
                "schema": "cheby.rich-message/1.0",
                "messageId": "msg_switch_b",
                "threadId": thread_b.id,
                "turnId": "turn_switch_b",
                "sourceItemId": "item_switch_b",
                "role": "user",
                "state": "completed",
                "revision": 0,
                "rootBlockIds": ["text"],
                "blocks": {
                    "text": {
                        "type": "text",
                        "blockId": "text",
                        "text": "B arrived while A was active",
                        "fallbackText": "B arrived while A was active",
                    }
                },
                "fallback": {"text": "B arrived while A was active"},
                "createdAt": "2026-07-19T00:00:00Z",
                "updatedAt": "2026-07-19T00:00:00Z",
                "clientMessageId": "client-switch-b",
            }
            context.store.save_user_message_once(
                "msg_switch_b",
                thread_b.id,
                "turn_switch_b",
                "client-switch-b",
                payload,
            )
            written["event"] = context.store.append_event(
                device.id,
                "message.snapshot",
                payload,
                thread_id=thread_b.id,
                turn_id="turn_switch_b",
                item_id="item_switch_b",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            ack_future = executor.submit(acknowledge_a)
            publish_future = executor.submit(publish_b)
            self.assertTrue(ack_future.result(timeout=3))
            publish_future.result(timeout=3)

        snapshot = context.store.read_thread_recovery_snapshot(
            device.id,
            thread_b.id,
        )
        replay = context.store.replay_events(
            device.id,
            device.stream_id,
            snapshot.cursor,
        )
        self.assertEqual(device.stream_id, snapshot.stream_id)
        self.assertEqual(written["event"].seq, snapshot.cursor)
        self.assertEqual("msg_switch_b", snapshot.messages[0]["messageId"])
        self.assertFalse(replay.sync_required)
        self.assertEqual([], replay.events)

    async def test_snapshot_cannot_interleave_message_save_and_event_cursor(self) -> None:
        context = await self._started_context()
        _, device = pair(context)
        thread = context.store.get_or_create_thread(
            "raw-thread-atomic",
            title="Atomic",
        )
        payload = {
            "schema": "cheby.rich-message/1.0",
            "messageId": "msg_atomic",
            "threadId": thread.id,
            "turnId": "turn_atomic",
            "sourceItemId": "item_atomic",
            "role": "assistant",
            "state": "completed",
            "revision": 0,
            "rootBlockIds": ["text"],
            "blocks": {
                "text": {
                    "type": "text",
                    "blockId": "text",
                    "text": "atomic",
                    "fallbackText": "atomic",
                }
            },
            "fallback": {"text": "atomic"},
            "createdAt": "2026-07-19T00:00:00Z",
            "updatedAt": "2026-07-19T00:00:00Z",
        }
        before_append = threading.Event()
        release_append = threading.Event()
        snapshot_started = threading.Event()
        original_append = context.store._append_event_in_transaction

        def blocked_append(*args, **kwargs):
            before_append.set()
            if not release_append.wait(timeout=3):
                raise TimeoutError("test did not release event append")
            return original_append(*args, **kwargs)

        context.store._append_event_in_transaction = blocked_append

        def write_projection():
            return context.store.save_message_snapshot_with_events(
                device.id,
                "msg_atomic",
                thread.id,
                "turn_atomic",
                0,
                payload,
                [
                    {
                        "type": "message.snapshot",
                        "payload": payload,
                        "item_id": "item_atomic",
                    }
                ],
            )

        def read_projection():
            snapshot_started.set()
            return context.store.read_thread_recovery_snapshot(
                device.id,
                thread.id,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                write_future = executor.submit(write_projection)
                self.assertTrue(before_append.wait(timeout=2))
                read_future = executor.submit(read_projection)
                self.assertTrue(snapshot_started.wait(timeout=2))
                self.assertFalse(read_future.done())
                release_append.set()
                events = write_future.result(timeout=3)
                snapshot = read_future.result(timeout=3)
        finally:
            release_append.set()
            context.store._append_event_in_transaction = original_append

        self.assertEqual(events[0].seq, snapshot.cursor)
        self.assertEqual("msg_atomic", snapshot.messages[0]["messageId"])


if __name__ == "__main__":
    unittest.main()
