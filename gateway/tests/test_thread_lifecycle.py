from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from cheby_gateway.bridge import (
    BridgeDeliveryUnknown,
    BridgeError,
    BridgeNotAccepted,
    BridgeEvent,
    FakeCodexBridge,
    BridgeThreadNotFound,
    StdioCodexBridge,
    THREAD_SOURCE_KINDS,
)
from cheby_gateway.service import GatewayError
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore

from .helpers import make_context, pair


def add_descendant(context, raw_id, parent_raw_id, title, archived=False):
    context.bridge.threads[raw_id] = {
        "id": raw_id,
        "title": title,
        "preview": title + " preview",
        "status": "archived" if archived else "idle",
        "archived": archived,
        "parentThreadId": parent_raw_id,
        "sourceKind": "subAgentThreadSpawn",
        "turns": [],
    }


async def terminate_process_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        await asyncio.to_thread(process.wait, 10)


def close_process_pipes(process) -> None:
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


class ThreadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context(delete_impact_ttl_seconds=60)
        await self.context.service.start()
        self.pairing, self.device = pair(self.context)
        self.root = await self.context.service.create_thread(self.device, "Root")
        self.root_raw_id = self.context.store.thread_raw_id(self.root.id)
        add_descendant(
            self.context,
            "raw-child",
            self.root_raw_id,
            "Child",
        )
        add_descendant(
            self.context,
            "raw-grandchild",
            "raw-child",
            "Grandchild",
        )

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.context.close()

    async def _archive_family(self):
        return await self.context.service.patch_thread(
            self.device,
            self.root.id,
            title=None,
            archived=True,
        )

    def _hide_thread_from_catalog(self, raw_id):
        original_list = self.context.bridge.list_threads

        async def filtered_list(archived=False):
            return [
                thread
                for thread in await original_list(archived)
                if thread.raw_id != raw_id
            ]

        self.context.bridge.list_threads = filtered_list
        return original_list

    async def test_provisional_thread_survives_catalog_refresh_and_rename(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Pending")
        raw_id = self.context.store.thread_raw_id(pending.id)
        original_list = self._hide_thread_from_catalog(raw_id)

        active = await self.context.service.sync_threads(self.device, archived=False)
        self.assertIn(pending.id, {thread.id for thread in active})
        self.assertEqual(
            "provisional",
            self.context.store.thread_catalog_state(pending.id),
        )

        renamed = await self.context.service.patch_thread(
            self.device,
            pending.id,
            title="Renamed before first turn",
            archived=None,
        )
        self.assertEqual("Renamed before first turn", renamed.title)
        self.assertEqual(
            "provisional",
            self.context.store.thread_catalog_state(pending.id),
        )

        self.context.bridge.list_threads = original_list
        await self.context.service.sync_threads(self.device, archived=False)
        row = self.context.store._conn.execute(
            "SELECT catalog_state, catalog_deadline_epoch FROM threads WHERE public_id = ?",
            (pending.id,),
        ).fetchone()
        self.assertEqual("visible", row["catalog_state"])
        self.assertIsNone(row["catalog_deadline_epoch"])

    async def test_provisional_thread_survives_service_reconstruction(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Reconstruct")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self._hide_thread_from_catalog(raw_id)

        await self.context.service.close()
        rebuilt = GatewayService(
            self.context.settings,
            self.context.store,
            self.context.bridge,
        )
        self.context.service = rebuilt
        await rebuilt.start()

        active = await rebuilt.sync_threads(self.device, archived=False)
        self.assertIn(pending.id, {thread.id for thread in active})
        self.assertEqual(
            "provisional",
            self.context.store.thread_catalog_state(pending.id),
        )

    async def test_expired_provisional_thread_is_cleaned_authoritatively(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Expired")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self._hide_thread_from_catalog(raw_id)
        self.context.store._conn.execute(
            "UPDATE threads SET catalog_deadline_epoch = 0 WHERE public_id = ?",
            (pending.id,),
        )

        active = await self.context.service.sync_threads(self.device, archived=False)

        self.assertNotIn(pending.id, {thread.id for thread in active})
        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_explicit_not_loaded_cleans_provisional_mapping(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Missing")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self.context.bridge.threads.pop(raw_id)

        with self.assertRaises(GatewayError) as missing:
            await self.context.service.read_thread(pending.id, self.device)

        self.assertEqual("THREAD_NOT_FOUND", missing.exception.code)
        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_provisional_thread_can_be_archived_and_deleted(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Disposable")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self._hide_thread_from_catalog(raw_id)

        archived = await self.context.service.patch_thread(
            self.device,
            pending.id,
            title=None,
            archived=True,
        )
        self.assertEqual("archived", archived.status.value)
        preview = await self.context.service.preview_delete_thread(
            self.device,
            pending.id,
        )
        self.assertEqual(1, preview.affected_count)

        await self.context.service.delete_thread(
            self.device,
            pending.id,
            preview.impact_token,
        )

        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_delete_explicit_not_loaded_consumes_provisional_impact(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Already gone")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self._hide_thread_from_catalog(raw_id)
        await self.context.service.patch_thread(
            self.device,
            pending.id,
            title=None,
            archived=True,
        )
        preview = await self.context.service.preview_delete_thread(
            self.device,
            pending.id,
        )
        self.context.bridge.threads.pop(raw_id)

        await self.context.service.delete_thread(
            self.device,
            pending.id,
            preview.impact_token,
        )

        state, _ = self.context.store.inspect_delete_impact(
            preview.impact_token,
            self.device.id,
            pending.id,
        )
        self.assertEqual("used", state)
        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_read_not_loaded_cannot_race_claimed_provisional_delete(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Delete race")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self._hide_thread_from_catalog(raw_id)
        await self.context.service.patch_thread(
            self.device,
            pending.id,
            title=None,
            archived=True,
        )
        preview = await self.context.service.preview_delete_thread(
            self.device,
            pending.id,
        )
        delete_started = asyncio.Event()
        allow_delete_result = asyncio.Event()

        async def delayed_not_loaded_delete(raw_thread_id):
            self.assertEqual(raw_id, raw_thread_id)
            delete_started.set()
            await allow_delete_result.wait()
            raise BridgeThreadNotFound("Codex thread is not loaded")

        self.context.bridge.delete_thread = delayed_not_loaded_delete
        delete_task = asyncio.create_task(
            self.context.service.delete_thread(
                self.device,
                pending.id,
                preview.impact_token,
            )
        )
        await delete_started.wait()
        self.context.bridge.threads.pop(raw_id)
        read_task = asyncio.create_task(
            self.context.service.read_thread(pending.id, self.device)
        )
        await asyncio.sleep(0)
        self.assertFalse(read_task.done())

        allow_delete_result.set()
        await delete_task
        with self.assertRaises(GatewayError) as missing:
            await read_task

        self.assertEqual("THREAD_NOT_FOUND", missing.exception.code)
        state, _ = self.context.store.inspect_delete_impact(
            preview.impact_token,
            self.device.id,
            pending.id,
        )
        self.assertEqual("used", state)
        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_turn_resume_explicit_not_loaded_cascades_reservation(self) -> None:
        pending = await self.context.service.create_thread(self.device, "Turn missing")
        raw_id = self.context.store.thread_raw_id(pending.id)
        self.context.bridge.threads.pop(raw_id)

        with self.assertRaises(GatewayError) as missing:
            await self.context.service.start_turn(
                self.device,
                pending.id,
                "client-provisional-missing",
                [{"type": "text", "text": "must not dispatch"}],
            )

        self.assertEqual("THREAD_NOT_FOUND", missing.exception.code)
        self.assertEqual(
            0,
            self.context.store._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
        )
        self.assertEqual(
            0,
            self.context.store._conn.execute(
                "SELECT COUNT(*) FROM message_snapshots"
            ).fetchone()[0],
        )
        self.assertIsNone(self.context.store.thread_by_public_id(pending.id))
        self.assertTrue(self.context.store.is_thread_tombstoned(raw_id))

    async def test_concurrent_create_and_refresh_keep_provisional_mapping(self) -> None:
        self.context.bridge.list_threads = lambda archived=False: asyncio.sleep(
            0, result=[]
        )

        pending, active = await asyncio.gather(
            self.context.service.create_thread(self.device, "Concurrent"),
            self.context.service.sync_threads(self.device, archived=False),
        )

        refreshed = await self.context.service.sync_threads(
            self.device,
            archived=False,
        )
        self.assertIn(
            pending.id,
            {thread.id for thread in active + refreshed},
        )
        self.assertEqual(
            "provisional",
            self.context.store.thread_catalog_state(pending.id),
        )

    async def test_active_archived_lists_and_unarchive_are_authoritative(self) -> None:
        active = await self.context.service.sync_threads(self.device, archived=False)
        self.assertEqual(
            {"Root", "Child", "Grandchild"},
            {thread.title for thread in active},
        )
        self.assertEqual([], await self.context.service.sync_threads(self.device, True))

        archived_root = await self._archive_family()
        self.assertEqual("archived", archived_root.status.value)
        self.assertEqual([], await self.context.service.sync_threads(self.device, False))
        archived = await self.context.service.sync_threads(self.device, True)
        self.assertEqual(
            {"Root", "Child", "Grandchild"},
            {thread.title for thread in archived},
        )

        restored = await self.context.service.patch_thread(
            self.device,
            self.root.id,
            title=None,
            archived=False,
        )
        self.assertEqual("idle", restored.status.value)
        self.assertEqual([], await self.context.service.sync_threads(self.device, True))
        self.assertEqual(
            {"Root", "Child", "Grandchild"},
            {
                thread.title
                for thread in await self.context.service.sync_threads(
                    self.device, False
                )
            },
        )

    async def test_delete_preview_requires_entire_impact_to_be_archived(self) -> None:
        with self.assertRaises(GatewayError) as raised:
            await self.context.service.preview_delete_thread(self.device, self.root.id)
        self.assertEqual("DELETE_REQUIRES_ARCHIVED", raised.exception.code)

        await self._archive_family()
        self.context.bridge.threads["raw-child"]["archived"] = False
        self.context.bridge.threads["raw-child"]["status"] = "idle"
        with self.assertRaises(GatewayError) as child_active:
            await self.context.service.preview_delete_thread(self.device, self.root.id)
        self.assertEqual("DELETE_REQUIRES_ARCHIVED", child_active.exception.code)

    async def test_read_response_without_archive_field_preserves_archive_state(self) -> None:
        await self._archive_family()
        original_read = self.context.bridge.read_thread

        async def read_without_archive(raw_thread_id):
            return replace(await original_read(raw_thread_id), archived=None)

        self.context.bridge.read_thread = read_without_archive
        detail = await self.context.service.read_thread(self.root.id, self.device)

        self.assertEqual("archived", detail.thread.status.value)
        self.assertEqual(
            "archived",
            self.context.store.thread_by_public_id(self.root.id).status.value,
        )

    async def test_catalog_change_between_stability_scans_fails_closed(self) -> None:
        await self._archive_family()
        original_list = self.context.bridge.list_threads
        calls = 0

        async def shifting_list(archived=False):
            nonlocal calls
            calls += 1
            if calls == 3:
                add_descendant(
                    self.context,
                    "raw-during-preview",
                    "raw-grandchild",
                    "During preview",
                    archived=True,
                )
            return await original_list(archived)

        self.context.bridge.list_threads = shifting_list
        with self.assertRaises(GatewayError) as changed:
            await self.context.service.preview_delete_thread(self.device, self.root.id)
        self.assertEqual("THREAD_CATALOG_CHANGED", changed.exception.code)
        self.assertEqual(
            0,
            self.context.store._conn.execute(
                "SELECT COUNT(*) FROM thread_delete_impacts"
            ).fetchone()[0],
        )

    async def test_preview_is_public_complete_and_delete_reconciles_cascade(self) -> None:
        await self._archive_family()
        child_public_id = self.context.store.thread_public_id("raw-child")
        self.context.store.map_raw_id(
            "item",
            "raw-private-item",
            "item",
            child_public_id,
        )

        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        serialized = json.dumps(preview.model_dump(by_alias=True))
        self.assertEqual(3, preview.affected_count)
        self.assertEqual(
            ["Root", "Child", "Grandchild"],
            [thread.title for thread in preview.affected_threads],
        )
        self.assertNotIn("raw-", serialized)
        affected_public_ids = {thread.id for thread in preview.affected_threads}
        before_delete_cursor = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        ).current_seq

        await self.context.service.delete_thread(
            self.device,
            self.root.id,
            preview.impact_token,
        )

        self.assertEqual({}, self.context.bridge.threads)
        self.assertEqual([], self.context.store.thread_catalog_rows())
        self.assertIsNone(
            self.context.store._conn.execute(
                "SELECT 1 FROM id_mappings WHERE raw_id = ?",
                ("raw-private-item",),
            ).fetchone()
        )
        replay = self.context.store.replay_events(
            self.device.id, self.device.stream_id, before_delete_cursor
        )
        deleted = [event for event in replay.events if event.type == "thread.deleted"]
        self.assertEqual(affected_public_ids, {event.thread_id for event in deleted})
        self.assertNotIn("raw-", json.dumps([event.model_dump() for event in deleted]))

        with self.assertRaises(GatewayError) as replayed:
            await self.context.service.delete_thread(
                self.device,
                self.root.id,
                preview.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_ALREADY_USED", replayed.exception.code)

    async def test_expired_wrong_device_and_descendant_change_fail_closed(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        wrong_device = replace(self.device, id="dev_other")
        with self.assertRaises(GatewayError) as wrong:
            await self.context.service.delete_thread(
                wrong_device,
                self.root.id,
                preview.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_INVALID", wrong.exception.code)

        self.context.store._conn.execute(
            "UPDATE thread_delete_impacts SET expires_at = ?",
            ("2000-01-01T00:00:00Z",),
        )
        with self.assertRaises(GatewayError) as expired:
            await self.context.service.delete_thread(
                self.device,
                self.root.id,
                preview.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_EXPIRED", expired.exception.code)
        self.assertIn(self.root_raw_id, self.context.bridge.threads)

        fresh = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        add_descendant(
            self.context,
            "raw-late-descendant",
            "raw-grandchild",
            "Late descendant",
            archived=True,
        )
        with self.assertRaises(GatewayError) as changed:
            await self.context.service.delete_thread(
                self.device,
                self.root.id,
                fresh.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_CHANGED", changed.exception.code)
        self.assertIn(self.root_raw_id, self.context.bridge.threads)

    async def test_token_expires_at_exact_boundary(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        boundary = "2099-01-01T00:00:00Z"
        self.context.store._conn.execute(
            "UPDATE thread_delete_impacts SET expires_at = ?",
            (boundary,),
        )
        state, _ = self.context.store.inspect_delete_impact(
            preview.impact_token,
            self.device.id,
            self.root.id,
            now=boundary,
        )
        self.assertEqual("expired", state)

    async def test_concurrent_replay_dispatches_delete_once(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_delete = self.context.bridge.delete_thread
        calls = 0

        async def blocked_delete(raw_thread_id):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            await original_delete(raw_thread_id)

        self.context.bridge.delete_thread = blocked_delete
        first = asyncio.create_task(
            self.context.service.delete_thread(
                self.device, self.root.id, preview.impact_token
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(
            self.context.service.delete_thread(
                self.device, self.root.id, preview.impact_token
            )
        )
        await asyncio.sleep(0)
        release.set()
        await first
        with self.assertRaises(GatewayError) as replayed:
            await second
        self.assertEqual("DELETE_IMPACT_ALREADY_USED", replayed.exception.code)
        self.assertEqual(1, calls)

    async def test_pending_token_and_single_use_survive_gateway_restart(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        db_path = self.context.settings.db_path
        await self.context.service.close()
        self.context.store.close()

        reopened_store = GatewayStore(db_path)
        reopened_service = GatewayService(
            self.context.settings,
            reopened_store,
            self.context.bridge,
        )
        self.context.store = reopened_store
        self.context.service = reopened_service
        await reopened_service.start()
        reopened_device = reopened_store.active_device()
        self.assertIsNotNone(reopened_device)

        await reopened_service.delete_thread(
            reopened_device,
            self.root.id,
            preview.impact_token,
        )
        with self.assertRaises(GatewayError) as replayed:
            await reopened_service.delete_thread(
                reopened_device,
                self.root.id,
                preview.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_ALREADY_USED", replayed.exception.code)

    async def test_new_preview_invalidates_previous_pending_token(self) -> None:
        await self._archive_family()
        old_preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        new_preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )

        with self.assertRaises(GatewayError) as stale:
            await self.context.service.delete_thread(
                self.device,
                self.root.id,
                old_preview.impact_token,
            )
        self.assertEqual("DELETE_IMPACT_CHANGED", stale.exception.code)
        await self.context.service.delete_thread(
            self.device,
            self.root.id,
            new_preview.impact_token,
        )

    async def test_delete_purges_history_and_every_cursor_is_gap_safe(self) -> None:
        await self.context.service.sync_threads(self.device, False)
        child_public_id = self.context.store.thread_public_id("raw-child")
        marker = "DELETED-HISTORY-MARKER-7F2C"
        marker_event = self.context.store.append_event(
            self.device.id,
            "audit.action",
            {"action": "test.marker", "marker": marker},
            thread_id=child_public_id,
        )
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        cursor_before_delete = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        ).current_seq

        await self.context.service.delete_thread(
            self.device, self.root.id, preview.impact_token
        )

        current = self.context.store.replay_events(
            self.device.id, self.device.stream_id, cursor_before_delete
        ).current_seq
        stored_payloads = self.context.store._conn.execute(
            "SELECT payload_json FROM event_outbox"
        ).fetchall()
        self.assertNotIn(marker, json.dumps([row[0] for row in stored_payloads]))
        self.assertLess(marker_event.seq, cursor_before_delete)
        for after_seq in range(0, current + 2):
            replay = self.context.store.replay_events(
                self.device.id,
                self.device.stream_id,
                after_seq,
            )
            self.assertNotIn(
                marker,
                json.dumps([event.model_dump() for event in replay.events]),
            )
        old_cursor = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            marker_event.seq,
        )
        self.assertTrue(old_cursor.sync_required)
        current_cursor = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            cursor_before_delete,
        )
        self.assertFalse(current_cursor.sync_required)
        self.assertEqual(
            3,
            len([event for event in current_cursor.events if event.type == "thread.deleted"]),
        )

    async def test_pending_delete_rejects_concurrent_turn_before_dispatch(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_delete = self.context.bridge.delete_thread

        async def blocked_delete(raw_thread_id):
            entered.set()
            await release.wait()
            await original_delete(raw_thread_id)

        self.context.bridge.delete_thread = blocked_delete
        deletion = asyncio.create_task(
            self.context.service.delete_thread(
                self.device, self.root.id, preview.impact_token
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        with self.assertRaises(GatewayError) as blocked:
            await self.context.service.start_turn(
                self.device,
                self.root.id,
                "client-during-delete",
                [{"type": "text", "text": "Must not dispatch"}],
            )
        self.assertEqual("THREAD_DELETE_PENDING", blocked.exception.code)
        self.assertEqual(0, self.context.bridge.start_turn_calls)
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": self.root_raw_id,
                    "turn": {"id": "raw-during-delete", "status": "inProgress"},
                },
            )
        )
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="thread/unarchived",
                params={"threadId": self.root_raw_id},
            )
        )
        self.assertIsNone(self.context.store.turn_public_id("raw-during-delete"))
        self.assertEqual(
            "pendingDelete",
            self.context.store._conn.execute(
                "SELECT lifecycle_state FROM threads WHERE public_id = ?",
                (self.root.id,),
            ).fetchone()[0],
        )
        release.set()
        await deletion

    async def test_second_service_cannot_patch_while_delete_is_pending(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device,
            self.root.id,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_delete = self.context.bridge.delete_thread

        async def blocked_delete(raw_thread_id):
            entered.set()
            await release.wait()
            await original_delete(raw_thread_id)

        self.context.bridge.delete_thread = blocked_delete
        deletion = asyncio.create_task(
            self.context.service.delete_thread(
                self.device,
                self.root.id,
                preview.impact_token,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        second_store = GatewayStore(self.context.settings.db_path)
        second_bridge = FakeCodexBridge(auto_events=False)
        second_service = GatewayService(
            self.context.settings,
            second_store,
            second_bridge,
        )
        try:
            with self.assertRaises(GatewayError) as startup:
                await second_service.start()
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", startup.exception.code)
            self.assertFalse(startup.exception.retryable)
            self.assertFalse(second_bridge.connected)

            with self.assertRaises(GatewayError) as patch:
                await second_service.patch_thread(
                    self.device,
                    self.root.id,
                    title=None,
                    archived=False,
                )
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", patch.exception.code)
            await second_service.handle_bridge_event(
                BridgeEvent(
                    method="thread/unarchived",
                    params={"threadId": self.root_raw_id},
                )
            )
            row = self.context.store._conn.execute(
                """
                SELECT archived, lifecycle_state FROM threads
                WHERE public_id = ?
                """,
                (self.root.id,),
            ).fetchone()
            self.assertEqual(1, row["archived"])
            self.assertEqual("pendingDelete", row["lifecycle_state"])
        finally:
            release.set()
            await deletion
            await second_service.close()
            second_store.close()

    async def test_nonowner_construction_cannot_hijack_owner_bridge_handler(self) -> None:
        second_store = GatewayStore(self.context.settings.db_path)
        second_service = GatewayService(
            self.context.settings,
            second_store,
            self.context.bridge,
        )
        try:
            with self.assertRaises(GatewayError) as startup:
                await second_service.start()
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", startup.exception.code)

            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="thread/archived",
                    params={"threadId": self.root_raw_id},
                )
            )
            self.assertEqual(
                "archived",
                self.context.store.thread_by_public_id(self.root.id).status.value,
            )

            await second_service.close()
            self.assertTrue(self.context.bridge.connected)
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="thread/unarchived",
                    params={"threadId": self.root_raw_id},
                )
            )
            self.assertEqual(
                "idle",
                self.context.store.thread_by_public_id(self.root.id).status.value,
            )
        finally:
            await second_service.close()
            second_store.close()

    async def test_nonowner_cannot_pair_or_rotate_owner_refresh_token(self) -> None:
        second_store = GatewayStore(self.context.settings.db_path)
        second_service = GatewayService(
            self.context.settings,
            second_store,
            FakeCodexBridge(auto_events=False),
        )
        try:
            with self.assertRaises(GatewayError) as pairing:
                second_service.exchange_pairing(
                    pairing_secret="test-pairing-secret",
                    device_name="Non-owner",
                    device_public_key="non-owner-public-key",
                )
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", pairing.exception.code)

            with self.assertRaises(GatewayError) as refresh:
                second_service.refresh_access(
                    self.pairing.device_id,
                    self.pairing.refresh_token,
                )
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", refresh.exception.code)

            rotated = self.context.service.refresh_access(
                self.pairing.device_id,
                self.pairing.refresh_token,
            )
            self.assertEqual(self.device.id, rotated.device_id)
        finally:
            await second_service.close()
            second_store.close()

    async def test_read_race_with_delete_notification_cannot_recreate_thread(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        original_read = self.context.bridge.read_thread

        async def blocked_read(raw_thread_id):
            entered.set()
            await release.wait()
            return await original_read(raw_thread_id)

        self.context.bridge.read_thread = blocked_read
        reading = asyncio.create_task(
            self.context.service.read_thread(self.root.id, self.device)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="thread/deleted",
                params={"threadId": self.root_raw_id},
            )
        )
        release.set()
        with self.assertRaises(GatewayError) as raised:
            await reading
        self.assertEqual("THREAD_NOT_FOUND", raised.exception.code)
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))

    async def test_interrupt_race_with_delete_notification_cannot_recreate_thread(self) -> None:
        turn = await self.context.service.start_turn(
            self.device,
            self.root.id,
            "client-interrupt-delete-race",
            [{"type": "text", "text": "race"}],
        )
        raw_turn_id = self.context.store.turn_raw_id(turn.id)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_interrupt = self.context.bridge.interrupt_turn

        async def blocked_interrupt(raw_thread_id, current_raw_turn_id):
            entered.set()
            await release.wait()
            await original_interrupt(raw_thread_id, current_raw_turn_id)

        self.context.bridge.interrupt_turn = blocked_interrupt
        interrupting = asyncio.create_task(
            self.context.service.interrupt_turn(self.device, self.root.id, turn.id)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="thread/deleted",
                params={"threadId": self.root_raw_id},
            )
        )
        release.set()
        with self.assertRaises(GatewayError) as raised:
            await interrupting
        self.assertEqual("THREAD_NOT_FOUND", raised.exception.code)
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))
        self.assertIsNone(self.context.store.turn_public_id(raw_turn_id))

    async def test_turn_success_response_after_delete_notification_is_terminal(self) -> None:
        original_start = self.context.bridge.start_turn
        original_delete = self.context.bridge.delete_thread
        accepted_raw_turn_id = None

        async def notification_before_success_response(
            raw_thread_id,
            client_message_id,
            input_parts,
        ):
            nonlocal accepted_raw_turn_id
            raw_turn = await original_start(
                raw_thread_id,
                client_message_id,
                input_parts,
            )
            accepted_raw_turn_id = raw_turn.raw_id
            await original_delete(raw_thread_id)
            await self.context.bridge.emit_event(
                BridgeEvent(
                    method="thread/deleted",
                    params={"threadId": raw_thread_id},
                )
            )
            return raw_turn

        self.context.bridge.start_turn = notification_before_success_response
        with self.assertRaises(GatewayError) as raised:
            await self.context.service.start_turn(
                self.device,
                self.root.id,
                "client-response-after-delete",
                [{"type": "text", "text": "accepted before delete"}],
            )
        self.assertEqual("TURN_ACCEPTED_THREAD_DELETED", raised.exception.code)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(1, self.context.bridge.start_turn_calls)
        self.assertIsNotNone(accepted_raw_turn_id)
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))
        self.assertIsNone(
            self.context.store.turn_public_id(str(accepted_raw_turn_id))
        )

        with self.assertRaises(GatewayError) as replayed:
            await self.context.service.start_turn(
                self.device,
                self.root.id,
                "client-response-after-delete",
                [{"type": "text", "text": "must not retry"}],
            )
        self.assertEqual("THREAD_NOT_FOUND", replayed.exception.code)
        self.assertEqual(1, self.context.bridge.start_turn_calls)

    async def test_cross_cwd_descendant_is_included_without_cwd_filter(self) -> None:
        self.context.bridge.threads["raw-grandchild"]["cwd"] = "/other/worktree"
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        self.assertEqual(
            {"Root", "Child", "Grandchild"},
            {thread.title for thread in preview.affected_threads},
        )

    async def test_delete_notifications_and_late_events_never_resurrect_identity(self) -> None:
        await self.context.service.sync_threads(self.device, False)
        root_public_id = self.root.id
        child_public_id = self.context.store.thread_public_id("raw-child")
        cursor = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        ).current_seq

        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="thread/deleted",
                params={"threadId": self.root_raw_id},
            )
        )
        first_current = self.context.store.replay_events(
            self.device.id, self.device.stream_id, cursor
        ).current_seq
        for event in (
            BridgeEvent(method="thread/deleted", params={"threadId": self.root_raw_id}),
            BridgeEvent(method="thread/archived", params={"threadId": self.root_raw_id}),
            BridgeEvent(method="thread/unarchived", params={"threadId": self.root_raw_id}),
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": self.root_raw_id,
                    "turn": {"id": "raw-late-turn", "status": "inProgress"},
                },
            ),
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": self.root_raw_id,
                    "turnId": "raw-late-turn",
                    "item": {
                        "id": "raw-late-item",
                        "type": "agentMessage",
                        "text": "must disappear",
                    },
                },
            ),
        ):
            await self.context.service.handle_bridge_event(event)

        await self.context.service.handle_bridge_event(
            BridgeEvent(method="thread/deleted", params={"threadId": "raw-unknown"})
        )
        await self.context.service.handle_bridge_event(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": "raw-unknown",
                    "turn": {"id": "raw-unknown-turn", "status": "inProgress"},
                },
            )
        )

        self.assertIsNone(self.context.store.thread_by_public_id(root_public_id))
        self.assertIsNone(self.context.store.thread_by_public_id(child_public_id))
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))
        self.assertIsNone(self.context.store.turn_public_id("raw-late-turn"))
        self.assertEqual(
            first_current,
            self.context.store.replay_events(
                self.device.id, self.device.stream_id, cursor
            ).current_seq,
        )
        tombstones = self.context.store._conn.execute(
            "SELECT raw_id, COUNT(*) FROM thread_tombstones GROUP BY raw_id"
        ).fetchall()
        self.assertEqual(1, dict(tombstones)[self.root_raw_id])
        self.assertEqual(1, dict(tombstones)["raw-child"])
        self.assertEqual(1, dict(tombstones)["raw-unknown"])
        active = await self.context.service.sync_threads(self.device, False)
        self.assertNotIn(root_public_id, {thread.id for thread in active})
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))

    async def test_claimed_remote_success_is_recovered_after_process_crash(self) -> None:
        await self._archive_family()
        marker = "CLAIM-CRASH-MARKER"
        self.context.store.append_event(
            self.device.id,
            "audit.action",
            {"marker": marker},
            thread_id=self.root.id,
        )
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        state, record = self.context.store.inspect_delete_impact(
            preview.impact_token, self.device.id, self.root.id
        )
        self.assertEqual("pending", state)
        claim_state, claimed = self.context.store.claim_delete_impact(
            preview.impact_token,
            self.device.id,
            self.root.id,
            record.affected_raw_ids,
            record.affected_public_ids,
        )
        self.assertEqual("claimed", claim_state)
        await self.context.bridge.delete_thread(record.root_raw_id)

        await self._restart_context_store_and_service()

        self.assertEqual([], self.context.store.thread_catalog_rows())
        self.assertEqual(
            "consumed",
            self.context.store._conn.execute(
                "SELECT state FROM thread_delete_impacts WHERE token_hash = ?",
                (claimed.token_hash,),
            ).fetchone()[0],
        )
        self.assertNotIn(
            marker,
            json.dumps(
                [
                    row[0]
                    for row in self.context.store._conn.execute(
                        "SELECT payload_json FROM event_outbox"
                    ).fetchall()
                ]
            ),
        )

    async def test_remote_success_then_reconcile_failure_recovers_without_retry(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        original_delete = self.context.bridge.delete_thread
        original_list = self.context.bridge.list_threads
        delete_calls = 0
        list_calls = 0

        async def counted_delete(raw_thread_id):
            nonlocal delete_calls
            delete_calls += 1
            await original_delete(raw_thread_id)

        async def fail_post_delete_reconcile(archived=False):
            nonlocal list_calls
            list_calls += 1
            if list_calls > 4:
                raise BridgeError("injected post-delete catalog failure")
            return await original_list(archived)

        self.context.bridge.delete_thread = counted_delete
        self.context.bridge.list_threads = fail_post_delete_reconcile
        with self.assertRaises(GatewayError) as raised:
            await self.context.service.delete_thread(
                self.device,
                self.root.id,
                preview.impact_token,
            )
        self.assertEqual("DELETE_RECONCILIATION_REQUIRED", raised.exception.code)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(1, delete_calls)
        self.assertEqual(
            "claimed",
            self.context.store._conn.execute(
                "SELECT state FROM thread_delete_impacts"
            ).fetchone()[0],
        )
        self.assertEqual(
            {"pendingDelete"},
            {
                row[0]
                for row in self.context.store._conn.execute(
                    "SELECT lifecycle_state FROM threads"
                ).fetchall()
            },
        )

        self.context.bridge.list_threads = original_list
        await self._restart_context_store_and_service()

        self.assertEqual(1, delete_calls)
        self.assertEqual([], self.context.store.thread_catalog_rows())
        self.assertEqual(
            "consumed",
            self.context.store._conn.execute(
                "SELECT state FROM thread_delete_impacts"
            ).fetchone()[0],
        )

    async def test_claimed_without_remote_delete_is_consumed_and_repreviewable(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        _, record = self.context.store.inspect_delete_impact(
            preview.impact_token, self.device.id, self.root.id
        )
        state, claimed = self.context.store.claim_delete_impact(
            preview.impact_token,
            self.device.id,
            self.root.id,
            record.affected_raw_ids,
            record.affected_public_ids,
        )
        self.assertEqual("claimed", state)

        await self._restart_context_store_and_service()

        self.assertEqual(
            "consumed",
            self.context.store._conn.execute(
                "SELECT state FROM thread_delete_impacts WHERE token_hash = ?",
                (claimed.token_hash,),
            ).fetchone()[0],
        )
        new_preview = await self.context.service.preview_delete_thread(
            self.context.store.active_device(), self.root.id
        )
        self.assertEqual(3, new_preview.affected_count)

    async def test_not_accepted_releases_same_token_but_unknown_never_retries(self) -> None:
        await self._archive_family()
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        original_delete = self.context.bridge.delete_thread
        calls = 0

        async def not_accepted(raw_thread_id):
            nonlocal calls
            calls += 1
            raise BridgeNotAccepted("not accepted")

        self.context.bridge.delete_thread = not_accepted
        with self.assertRaises(GatewayError) as rejected:
            await self.context.service.delete_thread(
                self.device, self.root.id, preview.impact_token
            )
        self.assertEqual("DELETE_NOT_ACCEPTED", rejected.exception.code)
        self.assertTrue(rejected.exception.retryable)
        self.assertEqual(
            "pending",
            self.context.store.inspect_delete_impact(
                preview.impact_token, self.device.id, self.root.id
            )[0],
        )

        self.context.bridge.delete_thread = original_delete
        await self.context.service.delete_thread(
            self.device, self.root.id, preview.impact_token
        )
        self.assertEqual(1, calls)

        second_root = await self.context.service.create_thread(self.device, "Unknown")
        await self.context.service.patch_thread(
            self.device, second_root.id, title=None, archived=True
        )
        second_preview = await self.context.service.preview_delete_thread(
            self.device, second_root.id
        )

        async def delivery_unknown(raw_thread_id):
            raise BridgeDeliveryUnknown("unknown")

        self.context.bridge.delete_thread = delivery_unknown
        with self.assertRaises(GatewayError) as unknown:
            await self.context.service.delete_thread(
                self.device, second_root.id, second_preview.impact_token
            )
        self.assertEqual("DELETE_DELIVERY_UNKNOWN", unknown.exception.code)
        self.assertFalse(unknown.exception.retryable)
        with self.assertRaises(GatewayError) as replayed:
            await self.context.service.delete_thread(
                self.device, second_root.id, second_preview.impact_token
            )
        self.assertEqual("DELETE_IMPACT_ALREADY_USED", replayed.exception.code)

    async def test_unrelated_metadata_churn_does_not_invalidate_preview(self) -> None:
        unrelated = await self.context.service.create_thread(self.device, "Unrelated")
        await self._archive_family()
        original_list = self.context.bridge.list_threads
        calls = 0
        unrelated_raw = self.context.store.thread_raw_id(unrelated.id)

        async def metadata_churn(archived=False):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.context.bridge.threads[unrelated_raw]["title"] = "Renamed elsewhere"
                self.context.bridge.threads[unrelated_raw]["updatedAt"] = (
                    "2099-01-01T00:00:00Z"
                )
            return await original_list(archived)

        self.context.bridge.list_threads = metadata_churn
        preview = await self.context.service.preview_delete_thread(
            self.device, self.root.id
        )
        self.assertEqual(3, preview.affected_count)

    async def test_scoped_stability_scan_does_not_delete_partition_drift(self) -> None:
        unrelated = await self.context.service.create_thread(self.device, "Unrelated")
        unrelated_raw_id = self.context.store.thread_raw_id(unrelated.id)
        marker = self.context.store.append_event(
            self.device.id,
            "audit.action",
            {"marker": "UNRELATED-PARTITION-DRIFT"},
            thread_id=unrelated.id,
        )
        await self._archive_family()
        original_list = self.context.bridge.list_threads
        calls = 0

        async def missing_from_verified_partition(archived=False):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.context.bridge.threads[unrelated_raw_id]["archived"] = True
                self.context.bridge.threads[unrelated_raw_id]["status"] = "archived"
            elif calls == 4:
                self.context.bridge.threads[unrelated_raw_id]["archived"] = False
                self.context.bridge.threads[unrelated_raw_id]["status"] = "idle"
            return await original_list(archived)

        self.context.bridge.list_threads = missing_from_verified_partition
        preview = await self.context.service.preview_delete_thread(
            self.device,
            self.root.id,
        )
        self.assertEqual(3, preview.affected_count)
        self.assertEqual(
            unrelated.id,
            self.context.store.thread_public_id(unrelated_raw_id),
        )
        self.assertFalse(self.context.store.is_thread_tombstoned(unrelated_raw_id))
        self.assertIsNotNone(
            self.context.store._conn.execute(
                "SELECT 1 FROM event_outbox WHERE event_id = ?",
                (marker.event_id,),
            ).fetchone()
        )

        self.context.bridge.list_threads = original_list
        active = await self.context.service.sync_threads(self.device, archived=False)
        self.assertIn(unrelated.id, {thread.id for thread in active})
        self.assertEqual(
            unrelated.id,
            self.context.store.thread_public_id(unrelated_raw_id),
        )
        self.assertFalse(self.context.store.is_thread_tombstoned(unrelated_raw_id))

    async def test_scoped_reconcile_keeps_orphan_child_and_unrelated_state(self) -> None:
        unrelated = await self.context.service.create_thread(self.device, "Unrelated")
        await self.context.service.sync_threads(self.device, archived=False)
        unrelated_raw_id = self.context.store.thread_raw_id(unrelated.id)
        child_id = self.context.store.thread_public_id("raw-child")
        grandchild_id = self.context.store.thread_public_id("raw-grandchild")
        marker = self.context.store.append_event(
            self.device.id,
            "audit.action",
            {"marker": "UNRELATED-ORPHAN-SCOPE"},
            thread_id=unrelated.id,
        )

        self.context.bridge.threads.pop(self.root_raw_id)
        catalog = await self.context.service._sync_thread_catalog(
            self.device,
            require_stable=True,
            stability_root_raw_id=self.root_raw_id,
        )

        self.assertNotIn(self.root_raw_id, {thread.raw_id for thread in catalog})
        self.assertIn("raw-child", {thread.raw_id for thread in catalog})
        self.assertIsNone(self.context.store.thread_public_id(self.root_raw_id))
        self.assertTrue(self.context.store.is_thread_tombstoned(self.root_raw_id))
        self.assertEqual(child_id, self.context.store.thread_public_id("raw-child"))
        self.assertEqual(
            grandchild_id,
            self.context.store.thread_public_id("raw-grandchild"),
        )
        self.assertFalse(self.context.store.is_thread_tombstoned("raw-child"))
        self.assertEqual(
            unrelated.id,
            self.context.store.thread_public_id(unrelated_raw_id),
        )
        self.assertFalse(self.context.store.is_thread_tombstoned(unrelated_raw_id))
        self.assertIsNotNone(
            self.context.store._conn.execute(
                "SELECT 1 FROM event_outbox WHERE event_id = ?",
                (marker.event_id,),
            ).fetchone()
        )
        replay = self.context.store.replay_events(
            self.device.id,
            self.device.stream_id,
            marker.seq - 1,
        )
        self.assertFalse(replay.sync_required)
        self.assertIn(marker.event_id, {event.event_id for event in replay.events})

        active = await self.context.service.sync_threads(self.device, archived=False)
        self.assertEqual(
            {unrelated.id, child_id, grandchild_id},
            {thread.id for thread in active},
        )

    async def _restart_context_store_and_service(self) -> None:
        db_path = self.context.settings.db_path
        await self.context.service.close()
        self.context.store.close()
        reopened_store = GatewayStore(db_path)
        reopened_service = GatewayService(
            self.context.settings,
            reopened_store,
            self.context.bridge,
        )
        self.context.store = reopened_store
        self.context.service = reopened_service
        await reopened_service.start()

    async def test_external_delete_removes_stale_mapping_authoritatively(self) -> None:
        await self.context.service.sync_threads(self.device, False)
        child_public_id = self.context.store.thread_public_id("raw-child")
        before_delete_cursor = self.context.store.replay_events(
            self.device.id, self.device.stream_id, 0
        ).current_seq
        self.context.bridge.threads.pop("raw-child")
        self.context.bridge.threads.pop("raw-grandchild")

        active = await self.context.service.sync_threads(self.device, False)

        self.assertEqual([self.root.id], [thread.id for thread in active])
        self.assertIsNone(self.context.store.thread_by_public_id(child_public_id))
        replay = self.context.store.replay_events(
            self.device.id, self.device.stream_id, before_delete_cursor
        )
        self.assertIn(
            child_public_id,
            [event.thread_id for event in replay.events if event.type == "thread.deleted"],
        )


class GatewayInstanceLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_crash_releases_exclusive_database_owner_lock(self) -> None:
        context = make_context()
        gateway_root = str(Path(__file__).resolve().parents[1])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (
                gateway_root,
                environment.get("PYTHONPATH", ""),
            )
            if value
        )
        child_program = """
import asyncio
import sys
from cheby_gateway.bridge import FakeCodexBridge
from cheby_gateway.config import GatewaySettings
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore

async def main():
    settings = GatewaySettings(
        db_path=sys.argv[1],
        pairing_secret="test-pairing-secret",
        bridge_mode="fake",
    )
    store = GatewayStore(settings.db_path)
    service = GatewayService(settings, store, FakeCodexBridge(auto_events=False))
    await service.start()
    print("READY", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", child_program, context.settings.db_path],
            cwd=gateway_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        parent_started = False
        try:
            self.assertIsNotNone(process.stdout)
            ready = await asyncio.wait_for(
                asyncio.to_thread(process.stdout.readline),
                timeout=10,
            )
            if ready.strip() != "READY":
                await terminate_process_group(process)
                _, stderr = await asyncio.to_thread(process.communicate)
                self.fail("child Gateway did not acquire lock: %s" % stderr)

            with self.assertRaises(GatewayError) as conflict:
                await context.service.start()
            self.assertEqual("GATEWAY_INSTANCE_CONFLICT", conflict.exception.code)

            await terminate_process_group(process)
            await context.service.start()
            parent_started = True
            self.assertTrue(context.bridge.connected)
        finally:
            await terminate_process_group(process)
            close_process_pipes(process)
            if parent_started:
                await context.service.close()
            context.close()

    async def test_idle_prefork_child_does_not_retain_dead_owner_lock(self) -> None:
        context = make_context()
        gateway_root = str(Path(__file__).resolve().parents[1])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (
                gateway_root,
                environment.get("PYTHONPATH", ""),
            )
            if value
        )
        owner_program = """
import asyncio
import os
import sys
import time
from cheby_gateway.bridge import FakeCodexBridge
from cheby_gateway.config import GatewaySettings
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore

async def main():
    settings = GatewaySettings(
        db_path=sys.argv[1],
        pairing_secret="test-pairing-secret",
        bridge_mode="fake",
    )
    store = GatewayStore(settings.db_path)
    service = GatewayService(settings, store, FakeCodexBridge(auto_events=False))
    await service.start()
    child_pid = os.fork()
    if child_pid == 0:
        print("CHILD_READY %d" % os.getpid(), flush=True)
        while True:
            time.sleep(1)
    print("OWNER_READY %d" % child_pid, flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", owner_program, context.settings.db_path],
            cwd=gateway_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        idle_child_pid = None
        replacement_started = False
        try:
            self.assertIsNotNone(process.stdout)
            readiness = set()
            for _ in range(2):
                line = await asyncio.wait_for(
                    asyncio.to_thread(process.stdout.readline),
                    timeout=10,
                )
                readiness.add(line.strip())
            owner_line = next(
                (line for line in readiness if line.startswith("OWNER_READY ")),
                None,
            )
            child_line = next(
                (line for line in readiness if line.startswith("CHILD_READY ")),
                None,
            )
            if owner_line is None or child_line is None:
                await terminate_process_group(process)
                _, stderr = await asyncio.to_thread(process.communicate)
                self.fail("prefork owner did not become ready: %s" % stderr)
            idle_child_pid = int(owner_line.split()[1])
            self.assertEqual(idle_child_pid, int(child_line.split()[1]))

            process.kill()
            await asyncio.to_thread(process.wait, 10)
            os.kill(idle_child_pid, 0)

            await context.service.start()
            replacement_started = True
            self.assertTrue(context.bridge.connected)
        finally:
            await terminate_process_group(process)
            close_process_pipes(process)
            if replacement_started:
                await context.service.close()
            context.close()

    async def test_prefork_readiness_timeout_terminates_pipe_holder(self) -> None:
        child_program = """
import os
import time

if os.fork() == 0:
    print("CHILD_HOLDS_PIPE", flush=True)
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", child_program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.assertIsNotNone(process.stdout)
        try:
            child_ready = await asyncio.wait_for(
                asyncio.to_thread(process.stdout.readline),
                timeout=5,
            )
            self.assertEqual("CHILD_HOLDS_PIPE", child_ready.strip())
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.to_thread(process.stdout.readline),
                    timeout=0.05,
                )
        finally:
            await asyncio.wait_for(terminate_process_group(process), timeout=5)
            await asyncio.wait_for(
                asyncio.to_thread(close_process_pipes, process),
                timeout=5,
            )
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)


class ThreadPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_start_is_headless_with_managed_permissions(self) -> None:
        bridge = StdioCodexBridge(command=("unused",), cwd="/workspace")
        calls = []

        async def request_method(method, params):
            calls.append((method, dict(params)))
            return {
                "thread": {
                    "id": "raw-started",
                    "name": "Started",
                    "source": "appServer",
                }
            }

        bridge.request_method = request_method
        await bridge.start_thread()

        self.assertEqual("thread/start", calls[0][0])
        self.assertEqual("cheby_mobile", calls[0][1]["permissions"])
        self.assertEqual("never", calls[0][1]["approvalPolicy"])
        self.assertNotIn("sandbox", calls[0][1])
        self.assertNotIn("approvalsReviewer", calls[0][1])

    async def test_thread_resume_is_headless_with_managed_permissions(self) -> None:
        bridge = StdioCodexBridge(command=("unused",), cwd="/workspace")
        calls = []

        async def request_method(method, params):
            calls.append((method, dict(params)))
            return {
                "thread": {
                    "id": "raw-resumed",
                    "name": "Resumed",
                    "source": "appServer",
                }
            }

        bridge.request_method = request_method
        await bridge.resume_thread("raw-resumed")

        self.assertEqual("thread/resume", calls[0][0])
        self.assertEqual("cheby_mobile", calls[0][1]["permissions"])
        self.assertEqual("never", calls[0][1]["approvalPolicy"])
        self.assertNotIn("sandbox", calls[0][1])
        self.assertNotIn("approvalsReviewer", calls[0][1])

    async def test_turn_start_is_headless_with_managed_permissions(self) -> None:
        bridge = StdioCodexBridge(command=("unused",), cwd="/workspace")
        calls = []

        async def request_method(method, params):
            calls.append((method, dict(params)))
            return {"turn": {"id": "raw-turn", "status": "inProgress"}}

        bridge.request_method = request_method
        await bridge.start_turn(
            "raw-thread",
            "client-message",
            [{"type": "text", "text": "hello"}],
        )

        self.assertEqual("turn/start", calls[0][0])
        self.assertEqual("never", calls[0][1]["approvalPolicy"])
        self.assertNotIn("approvalsReviewer", calls[0][1])
        self.assertEqual("cheby_mobile", calls[0][1]["permissions"])
        self.assertNotIn("sandboxPolicy", calls[0][1])

    async def test_all_pages_and_subagent_sources_are_requested(self) -> None:
        bridge = StdioCodexBridge(command=("unused",), cwd="/workspace")
        calls = []

        async def request_method(method, params):
            calls.append((method, dict(params)))
            if "cursor" not in params:
                return {
                    "data": [
                        {
                            "id": "root",
                            "name": "Root",
                            "source": "appServer",
                        }
                    ],
                    "nextCursor": "cursor-2",
                }
            return {
                "data": [
                    {
                        "id": "child",
                        "name": "Child",
                        "parentThreadId": "root",
                        "source": {
                            "subAgent": {
                                "thread_spawn": {
                                    "depth": 1,
                                    "parent_thread_id": "root",
                                }
                            }
                        },
                    }
                ],
                "nextCursor": None,
            }

        bridge.request_method = request_method
        threads = await bridge.list_threads(archived=True)

        self.assertEqual(["root", "child"], [thread.raw_id for thread in threads])
        self.assertTrue(all(thread.archived is True for thread in threads))
        self.assertEqual("root", threads[1].parent_raw_id)
        self.assertEqual("subAgentThreadSpawn", threads[1].source_kind)
        self.assertEqual(list(THREAD_SOURCE_KINDS), calls[0][1]["sourceKinds"])
        self.assertNotIn("cwd", calls[0][1])
        self.assertNotIn("cursor", calls[0][1])
        self.assertEqual("cursor-2", calls[1][1]["cursor"])

    async def test_repeated_pagination_cursor_fails_closed(self) -> None:
        bridge = StdioCodexBridge(command=("unused",), cwd="/workspace")

        async def request_method(method, params):
            return {"data": [], "nextCursor": "same-cursor"}

        bridge.request_method = request_method
        with self.assertRaises(BridgeError):
            await bridge.list_threads()


if __name__ == "__main__":
    unittest.main()
