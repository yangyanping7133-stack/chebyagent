from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cheby_gateway.service import turn_request_fingerprint
from cheby_gateway.store import (
    GatewayStore,
    ThreadBusyError,
    TurnIdempotencyConflictError,
)


class GatewayMigrationTests(unittest.TestCase):
    def test_legacy_duplicate_active_turns_converge_before_unique_index(self) -> None:
        tempdir = tempfile.TemporaryDirectory(prefix="cheby-gateway-migration-")
        self.addCleanup(tempdir.cleanup)
        db_path = str(Path(tempdir.name) / "legacy.sqlite3")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_expires_at TEXT NOT NULL,
                stream_id TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE threads (
                public_id TEXT PRIMARY KEY,
                raw_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                last_turn_id TEXT,
                unread INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE turns (
                public_id TEXT PRIMARY KEY,
                raw_id TEXT UNIQUE,
                device_id TEXT NOT NULL,
                thread_public_id TEXT NOT NULL,
                client_message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(device_id, thread_public_id, client_message_id)
            );
            """
        )
        now = "2026-07-19T00:00:00Z"
        conn.execute(
            "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                "dev_legacy",
                "Legacy phone",
                "legacy-public-key",
                "legacy-token-hash",
                "2099-01-01T00:00:00Z",
                "stream_legacy",
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, '', ?, ?, 'idle', NULL, 0, 0)",
            ("thr_legacy", "raw-thread-legacy", "Legacy", now, now),
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, '', ?, ?, 'idle', NULL, 0, 0)",
            (
                "thr_legacy_pending",
                "raw-thread-legacy-pending",
                "Legacy pending",
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, '', ?, ?, 'idle', NULL, 0, 0)",
            (
                "thr_legacy_double_accepted",
                "raw-thread-legacy-double-accepted",
                "Legacy double accepted",
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?, NULL, ?, ?, ?, 'pending', ?, ?)",
            (
                "turn_pending_older",
                "dev_legacy",
                "thr_legacy",
                "client-pending",
                "2026-07-19T00:00:01Z",
                "2026-07-19T00:00:01Z",
            ),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, 'inProgress', ?, ?)",
            (
                "turn_double_accepted_one",
                "raw-turn-double-accepted-one",
                "dev_legacy",
                "thr_legacy_double_accepted",
                "client-double-accepted-one",
                "2026-07-19T00:00:04Z",
                "2026-07-19T00:00:04Z",
            ),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (
                "turn_double_accepted_two",
                "raw-turn-double-accepted-two",
                "dev_legacy",
                "thr_legacy_double_accepted",
                "client-double-accepted-two",
                "2026-07-19T00:00:05Z",
                "2026-07-19T00:00:05Z",
            ),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, 'inProgress', ?, ?)",
            (
                "turn_accepted_newer",
                "raw-turn-accepted",
                "dev_legacy",
                "thr_legacy",
                "client-accepted",
                "2026-07-19T00:00:02Z",
                "2026-07-19T00:00:02Z",
            ),
        )
        conn.execute(
            "INSERT INTO turns VALUES (?, NULL, ?, ?, ?, 'pending', ?, ?)",
            (
                "turn_legacy_pending_unknown",
                "dev_legacy",
                "thr_legacy_pending",
                "client-legacy-pending",
                "2026-07-19T00:00:03Z",
                "2026-07-19T00:00:03Z",
            ),
        )
        conn.commit()
        conn.close()

        store = GatewayStore(db_path)
        self.addCleanup(store.close)

        kept = store.turn_by_public_id("turn_accepted_newer")
        failed = store.turn_by_public_id("turn_pending_older")
        self.assertEqual("inProgress", kept["status"])
        self.assertEqual("accepted", kept["delivery_state"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("reconciledDuplicate", failed["delivery_state"])
        legacy_pending = store.turn_by_public_id("turn_legacy_pending_unknown")
        self.assertEqual("ambiguous", legacy_pending["status"])
        self.assertEqual("ambiguous", legacy_pending["delivery_state"])
        pending_thread = store.thread_by_public_id("thr_legacy_pending")
        self.assertEqual("failed", pending_thread.status.value)
        for turn_id in ("turn_double_accepted_one", "turn_double_accepted_two"):
            ambiguous = store.turn_by_public_id(turn_id)
            self.assertEqual("ambiguous", ambiguous["status"])
            self.assertEqual("ambiguous", ambiguous["delivery_state"])
            self.assertEqual(
                "legacyMultipleAcceptedTurns",
                ambiguous["recovery_reason"],
            )
        double_thread = store.thread_by_public_id("thr_legacy_double_accepted")
        self.assertEqual("failed", double_thread.status.value)
        self.assertEqual(
            1,
            store._conn.execute(
                "SELECT needs_resync FROM threads WHERE public_id = ?",
                ("thr_legacy_double_accepted",),
            ).fetchone()[0],
        )
        self.assertEqual(12, store._conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertIsNotNone(
            store._conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'request_proof_nonces'"
            ).fetchone()
        )
        thread_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(threads)").fetchall()
        }
        self.assertIn("parent_raw_id", thread_columns)
        self.assertIn("source_kind", thread_columns)
        self.assertIn("catalog_state", thread_columns)
        self.assertIn("catalog_deadline_epoch", thread_columns)
        self.assertIn("lifecycle_state", thread_columns)
        stream_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(streams)").fetchall()
        }
        self.assertIn("replay_floor_seq", stream_columns)
        projection_columns = {
            row["name"]
            for row in store._conn.execute(
                "PRAGMA table_info(message_projection_items)"
            ).fetchall()
        }
        self.assertIn("evidence_json", projection_columns)
        for text in ("legacy presumed same", "legacy definitely changed"):
            with self.assertRaises(TurnIdempotencyConflictError):
                store.reserve_turn(
                    "dev_legacy",
                    "thr_legacy",
                    "client-accepted",
                    request_fingerprint=turn_request_fingerprint(
                        [{"type": "text", "text": text}]
                    ),
                )
        self.assertIsNotNone(
            store._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'thread_delete_impacts'
                """
            ).fetchone()
        )
        self.assertIsNotNone(
            store._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'thread_tombstones'
                """
            ).fetchone()
        )
        with self.assertRaises(ThreadBusyError):
            store.reserve_turn(
                "dev_legacy",
                "thr_legacy",
                "client-third",
            )


if __name__ == "__main__":
    unittest.main()
