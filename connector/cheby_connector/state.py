from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


class IdempotencyConflict(RuntimeError):
    pass


class OutboxFull(RuntimeError):
    pass


class ConnectorState:
    """Durable command deduplication, delivery cursor, and outbound replay."""

    def __init__(
        self,
        path: str,
        *,
        outbox_max_count: int = 2048,
        outbox_max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        state_path = Path(path)
        if state_path.is_symlink():
            raise RuntimeError("Connector state path must not be a symlink")
        if not state_path.parent.exists():
            state_path.parent.mkdir(mode=0o700, parents=True)
        if not state_path.parent.is_dir():
            raise RuntimeError("Connector state parent is not a directory")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.outbox_max_count = outbox_max_count
        self.outbox_max_bytes = outbox_max_bytes
        self._migrate()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO meta(key, value) VALUES ('ack_cursor', '0');

                CREATE TABLE IF NOT EXISTS inbound_messages(
                    message_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    response_frame TEXT NOT NULL,
                    response_message_id TEXT,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_intents(
                    request_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved', 'executing')),
                    reserved_bytes INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_deliveries(
                    delivery_seq INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox(
                    local_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    frame_json TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(outbox)").fetchall()
            }
            if "local_seq" not in columns:
                self._conn.executescript(
                    """
                    ALTER TABLE outbox RENAME TO outbox_legacy;
                    CREATE TABLE outbox(
                        local_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT NOT NULL UNIQUE,
                        frame_json TEXT NOT NULL,
                        byte_count INTEGER NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    INSERT INTO outbox(message_id, frame_json, byte_count, created_at)
                    SELECT message_id, frame_json, byte_count, created_at
                    FROM outbox_legacy ORDER BY created_at, rowid;
                    DROP TABLE outbox_legacy;
                    """
                )
            request_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(requests)"
                ).fetchall()
            }
            if "response_message_id" not in request_columns:
                self._conn.execute(
                    "ALTER TABLE requests ADD COLUMN response_message_id TEXT"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS requests_created_at_idx "
                "ON requests(created_at)"
            )

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @property
    def ack_cursor(self) -> int:
        return int(self.get_meta("ack_cursor") or "0")

    def lookup_response(
        self, message_id: str, request_id: str, fingerprint: str
    ) -> Optional[str]:
        message = self._conn.execute(
            "SELECT request_id, fingerprint FROM inbound_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if message is not None and (
            str(message["request_id"]) != request_id
            or str(message["fingerprint"]) != fingerprint
        ):
            raise IdempotencyConflict("messageId was reused")
        request = self._conn.execute(
            "SELECT fingerprint, response_frame FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if request is None:
            return None
        if str(request["fingerprint"]) != fingerprint:
            raise IdempotencyConflict("requestId was reused")
        return str(request["response_frame"])

    def reserve_execution(
        self,
        *,
        message_id: str,
        request_id: str,
        fingerprint: str,
        operation: str,
        reserved_bytes: int,
    ) -> str:
        now = int(time.time())
        with self.transaction() as conn:
            self._assert_ids_available(conn, message_id, request_id, fingerprint)
            message_intent = conn.execute(
                "SELECT request_id, fingerprint FROM request_intents WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if message_intent is not None and (
                str(message_intent["request_id"]) != request_id
                or str(message_intent["fingerprint"]) != fingerprint
            ):
                raise IdempotencyConflict("messageId was reused")
            intent = conn.execute(
                "SELECT fingerprint, state FROM request_intents WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if intent is not None:
                if str(intent["fingerprint"]) != fingerprint:
                    raise IdempotencyConflict("requestId was reused")
                return str(intent["state"])
            usage = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM outbox) +
                    (SELECT COUNT(*) FROM request_intents) AS count,
                    COALESCE((SELECT SUM(byte_count) FROM outbox), 0) +
                    COALESCE((SELECT SUM(reserved_bytes) FROM request_intents), 0) AS bytes
                """
            ).fetchone()
            if (
                int(usage["count"]) >= self.outbox_max_count
                or int(usage["bytes"]) + reserved_bytes > self.outbox_max_bytes
            ):
                raise OutboxFull("outbox limit reached")
            conn.execute(
                "INSERT INTO request_intents VALUES (?, ?, ?, ?, 'reserved', ?, ?)",
                (
                    request_id,
                    message_id,
                    fingerprint,
                    operation,
                    reserved_bytes,
                    now,
                ),
            )
            return "reserved"

    def mark_execution_started(self, request_id: str, fingerprint: str) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE request_intents SET state = 'executing'
                WHERE request_id = ? AND fingerprint = ? AND state = 'reserved'
                """,
                (request_id, fingerprint),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT fingerprint, state FROM request_intents WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is None or str(row["fingerprint"]) != fingerprint:
                    raise IdempotencyConflict("request execution intent changed")
                if str(row["state"]) != "executing":
                    raise IdempotencyConflict("request execution state is invalid")

    def complete_delivery(
        self,
        *,
        delivery_seq: int,
        message_id: str,
        request_id: str,
        fingerprint: str,
        operation: str,
        response_message_id: str,
        response_frame: str,
    ) -> int:
        encoded_bytes = len(response_frame.encode("utf-8"))
        now = int(time.time())
        with self.transaction() as conn:
            self._assert_ids_available(conn, message_id, request_id, fingerprint)
            conn.execute(
                "INSERT OR IGNORE INTO inbound_messages VALUES (?, ?, ?, ?)",
                (message_id, request_id, fingerprint, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO requests(
                    request_id, fingerprint, operation, response_frame,
                    response_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    fingerprint,
                    operation,
                    response_frame,
                    response_message_id,
                    now,
                ),
            )
            self._put_outbox(
                conn,
                response_message_id,
                response_frame,
                encoded_bytes,
                now,
                consume_request_id=request_id,
            )
            conn.execute(
                "INSERT OR IGNORE INTO processed_deliveries VALUES (?, ?, ?)",
                (delivery_seq, request_id, now),
            )
            conn.execute("DELETE FROM request_intents WHERE request_id = ?", (request_id,))
            cursor = self._advance_cursor(conn)
        return cursor

    def record_replayed_delivery(
        self,
        *,
        delivery_seq: int,
        message_id: str,
        request_id: str,
        fingerprint: str,
        response_message_id: str,
        response_frame: str,
    ) -> int:
        now = int(time.time())
        with self.transaction() as conn:
            self._assert_ids_available(conn, message_id, request_id, fingerprint)
            conn.execute(
                "INSERT OR IGNORE INTO inbound_messages VALUES (?, ?, ?, ?)",
                (message_id, request_id, fingerprint, now),
            )
            conn.execute(
                """
                UPDATE requests SET response_message_id = ?
                WHERE request_id = ? AND response_message_id IS NULL
                """,
                (response_message_id, request_id),
            )
            self._put_outbox(
                conn,
                response_message_id,
                response_frame,
                len(response_frame.encode("utf-8")),
                now,
            )
            conn.execute(
                "INSERT OR IGNORE INTO processed_deliveries VALUES (?, ?, ?)",
                (delivery_seq, request_id, now),
            )
            return self._advance_cursor(conn)

    @staticmethod
    def _assert_ids_available(
        conn: sqlite3.Connection,
        message_id: str,
        request_id: str,
        fingerprint: str,
    ) -> None:
        message = conn.execute(
            "SELECT request_id, fingerprint FROM inbound_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if message is not None and (
            str(message["request_id"]) != request_id
            or str(message["fingerprint"]) != fingerprint
        ):
            raise IdempotencyConflict("messageId was reused")
        request = conn.execute(
            "SELECT fingerprint FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if request is not None and str(request["fingerprint"]) != fingerprint:
            raise IdempotencyConflict("requestId was reused")

    def enqueue(self, message_id: str, frame_json: str) -> None:
        now = int(time.time())
        with self.transaction() as conn:
            self._put_outbox(
                conn,
                message_id,
                frame_json,
                len(frame_json.encode("utf-8")),
                now,
            )

    def _put_outbox(
        self,
        conn: sqlite3.Connection,
        message_id: str,
        frame_json: str,
        byte_count: int,
        now: int,
        consume_request_id: Optional[str] = None,
    ) -> None:
        existing = conn.execute(
            "SELECT frame_json FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["frame_json"]) != frame_json:
                raise IdempotencyConflict("outbound messageId was reused")
            return
        reservation = None
        if consume_request_id is not None:
            reservation = conn.execute(
                "SELECT reserved_bytes FROM request_intents WHERE request_id = ?",
                (consume_request_id,),
            ).fetchone()
            if reservation is None:
                raise IdempotencyConflict("request execution reservation is missing")
        usage = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM outbox) +
                (SELECT COUNT(*) FROM request_intents) AS count,
                COALESCE((SELECT SUM(byte_count) FROM outbox), 0) +
                COALESCE((SELECT SUM(reserved_bytes) FROM request_intents), 0) AS bytes
            """
        ).fetchone()
        reservation_count = 1 if reservation is not None else 0
        reservation_bytes = (
            int(reservation["reserved_bytes"]) if reservation is not None else 0
        )
        if (
            int(usage["count"]) - reservation_count >= self.outbox_max_count
            or int(usage["bytes"]) - reservation_bytes + byte_count
            > self.outbox_max_bytes
        ):
            raise OutboxFull("outbox limit reached")
        conn.execute(
            """
            INSERT INTO outbox(message_id, frame_json, byte_count, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (message_id, frame_json, byte_count, now),
        )

    @staticmethod
    def _advance_cursor(conn: sqlite3.Connection) -> int:
        cursor = int(
            conn.execute("SELECT value FROM meta WHERE key = 'ack_cursor'").fetchone()[0]
        )
        while conn.execute(
            "SELECT 1 FROM processed_deliveries WHERE delivery_seq = ?", (cursor + 1,)
        ).fetchone() is not None:
            cursor += 1
        conn.execute("UPDATE meta SET value = ? WHERE key = 'ack_cursor'", (str(cursor),))
        conn.execute("DELETE FROM processed_deliveries WHERE delivery_seq <= ?", (cursor,))
        return cursor

    def pending_outbox(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT message_id, frame_json FROM outbox ORDER BY local_seq"
        ).fetchall()
        return [(str(row["message_id"]), str(row["frame_json"])) for row in rows]

    def accept_outbox(self, message_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM outbox WHERE message_id = ?", (message_id,))

    def gc_completed(self, *, cutoff_epoch: int, batch_size: int) -> int:
        """Delete expired dedupe rows only after all active durable work is clear."""

        if not 1 <= batch_size <= 1000:
            raise ValueError("GC batch size must be between 1 and 1000")
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT r.request_id
                FROM requests AS r
                WHERE r.created_at < ?
                  AND r.response_message_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM request_intents AS i
                      WHERE i.request_id = r.request_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM processed_deliveries AS p
                      WHERE p.request_id = r.request_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM outbox AS o
                      WHERE o.message_id = r.response_message_id
                  )
                ORDER BY r.created_at, r.request_id
                LIMIT ?
                """,
                (cutoff_epoch, batch_size),
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in rows]
            if not request_ids:
                return 0
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(
                f"DELETE FROM inbound_messages WHERE request_id IN ({placeholders})",
                request_ids,
            )
            conn.execute(
                f"DELETE FROM requests WHERE request_id IN ({placeholders})",
                request_ids,
            )
            return len(request_ids)
