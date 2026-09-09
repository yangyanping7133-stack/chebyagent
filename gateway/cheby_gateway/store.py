from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import EventEnvelope, ReplayResult, ThreadDTO, ThreadStatus


EVENT_TYPES = {
    "thread.snapshot",
    "thread.updated",
    "thread.deleted",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "message.snapshot",
    "message.patch",
    "approval.requested",
    "approval.resolved",
    "approval.expired",
    "asset.unavailable",
    "sync.required",
    "error",
    "audit.action",
}

RELAY_INTERNAL_PUBLIC_KEY = "relay-device:v1"
RELAY_DEVICE_PREFIX = "dev_"
RELAY_DEVICE_SUFFIX_LENGTH = 22
RELAY_ID_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def public_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeviceRecord:
    id: str
    name: str
    public_key: str
    stream_id: str
    token_expires_at: str


@dataclass(frozen=True)
class TurnReservation:
    public_id: str
    raw_id: Optional[str]
    thread_public_id: str
    client_message_id: str
    status: str
    delivery_state: str
    attempt_count: int
    created_at: str
    created: bool


@dataclass(frozen=True)
class ImageAssetRecord:
    asset_ref: str
    device_id: str
    thread_public_id: str
    client_message_id: str
    client_asset_id: str
    turn_public_id: Optional[str]
    storage_name: str
    media_type: str
    width: int
    height: int
    byte_count: int
    source_sha256: str
    normalized_sha256: str
    file_device: int
    file_inode: int
    state: str
    expires_at: str
    hard_expires_at: str
    created_at: str


@dataclass(frozen=True)
class ThreadRecoverySnapshot:
    thread: ThreadDTO
    messages: Tuple[Dict[str, Any], ...]
    message_keys: Tuple[Tuple[str, int, int, str], ...]
    has_more_messages: bool
    approval_rows: Tuple[sqlite3.Row, ...]
    stream_id: str
    cursor: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class DeleteImpactRecord:
    token_hash: str
    device_id: str
    root_thread_public_id: str
    root_raw_id: str
    affected_raw_ids: Tuple[str, ...]
    affected_public_ids: Tuple[str, ...]
    expires_at: str
    state: str


@dataclass(frozen=True)
class _RateLimitState:
    scope: str
    subject_hash: str
    window_seconds: int
    limit: int
    window_started_at: float
    request_count: int
    failure_count: int
    blocked_until: float
    exists: bool


class ThreadBusyError(RuntimeError):
    pass


class TurnIdempotencyConflictError(RuntimeError):
    pass


class AssetBindingError(RuntimeError):
    pass


class AssetUnavailableError(RuntimeError):
    pass


class AssetQuotaError(RuntimeError):
    pass


class TurnAmbiguousError(RuntimeError):
    pass


class ThreadLifecycleBlockedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GatewayStore:
    RATE_LIMIT_BUCKETS_PER_SCOPE = 4096

    def __init__(self, path: str, event_retention: int = 10_000) -> None:
        self.path = path
        self.event_retention = max(1, event_retention)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_expires_at TEXT NOT NULL,
                refresh_token_hash TEXT UNIQUE,
                refresh_expires_at TEXT,
                stream_id TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pairing_grants (
                secret_hash TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                scope TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                window_started_at REAL NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                blocked_until REAL NOT NULL DEFAULT 0,
                window_seconds INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(scope, subject_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS threads (
                public_id TEXT PRIMARY KEY,
                raw_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                last_turn_id TEXT,
                unread INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                needs_resync INTEGER NOT NULL DEFAULT 0,
                parent_raw_id TEXT,
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                catalog_state TEXT NOT NULL DEFAULT 'visible',
                catalog_deadline_epoch INTEGER,
                lifecycle_state TEXT NOT NULL DEFAULT 'active'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS turns (
                public_id TEXT PRIMARY KEY,
                raw_id TEXT UNIQUE,
                device_id TEXT NOT NULL REFERENCES devices(id),
                thread_public_id TEXT NOT NULL REFERENCES threads(public_id) ON DELETE CASCADE,
                client_message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'reserved',
                recovery_reason TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(device_id, thread_public_id, client_message_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS id_mappings (
                kind TEXT NOT NULL,
                raw_id TEXT NOT NULL,
                public_id TEXT NOT NULL UNIQUE,
                thread_public_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(kind, raw_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS approvals (
                public_id TEXT PRIMARY KEY,
                raw_request_id TEXT NOT NULL UNIQUE,
                thread_public_id TEXT NOT NULL REFERENCES threads(public_id) ON DELETE CASCADE,
                turn_public_id TEXT NOT NULL,
                item_public_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                reason TEXT,
                decisions_json TEXT NOT NULL,
                state TEXT NOT NULL,
                action_token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decision TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS message_snapshots (
                message_id TEXT PRIMARY KEY,
                thread_public_id TEXT NOT NULL REFERENCES threads(public_id) ON DELETE CASCADE,
                turn_public_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'assistant',
                client_message_id TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS message_projection_items (
                item_public_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                thread_public_id TEXT NOT NULL REFERENCES threads(public_id) ON DELETE CASCADE,
                turn_public_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                status TEXT,
                phase TEXT,
                text TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER,
                exit_code INTEGER,
                change_count INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS streams (
                device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                stream_id TEXT NOT NULL UNIQUE,
                next_seq INTEGER NOT NULL DEFAULT 1,
                acked_seq INTEGER NOT NULL DEFAULT 0,
                replay_floor_seq INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS thread_delete_impacts (
                token_hash TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                root_thread_public_id TEXT NOT NULL,
                root_raw_id TEXT NOT NULL,
                affected_raw_ids_json TEXT NOT NULL,
                affected_public_ids_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS thread_tombstones (
                raw_id TEXT PRIMARY KEY,
                public_id TEXT UNIQUE,
                deleted_at TEXT NOT NULL,
                reason TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS request_proof_nonces (
                subject_hash TEXT NOT NULL,
                nonce_hash TEXT NOT NULL,
                signed_at INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(subject_hash, nonce_hash)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS event_outbox (
                stream_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL,
                type TEXT NOT NULL,
                thread_public_id TEXT,
                turn_public_id TEXT,
                item_public_id TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(stream_id, seq)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS image_assets (
                asset_ref TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                thread_public_id TEXT NOT NULL REFERENCES threads(public_id) ON DELETE CASCADE,
                client_message_id TEXT NOT NULL,
                client_asset_id TEXT NOT NULL,
                turn_public_id TEXT REFERENCES turns(public_id) ON DELETE CASCADE,
                storage_name TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                byte_count INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                normalized_sha256 TEXT NOT NULL,
                file_device INTEGER NOT NULL,
                file_inode INTEGER NOT NULL,
                state TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                hard_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                UNIQUE(device_id, thread_public_id, client_message_id, client_asset_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS image_asset_cleanup_queue (
                storage_name TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                queued_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_event_device_seq ON event_outbox(device_id, seq)",
            "CREATE INDEX IF NOT EXISTS idx_messages_thread ON message_snapshots(thread_public_id, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_projection_turn ON message_projection_items(turn_public_id, ordinal)",
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_updated ON rate_limit_buckets(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_delete_impact_root ON thread_delete_impacts(root_raw_id, state)",
            "CREATE INDEX IF NOT EXISTS idx_proof_nonce_signed_at ON request_proof_nonces(signed_at)",
            "CREATE INDEX IF NOT EXISTS idx_image_asset_owner ON image_assets(device_id, state, expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_image_asset_turn ON image_assets(turn_public_id)",
        ]
        with self._transaction() as conn:
            for statement in statements:
                conn.execute(statement)
            existing_turn_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(turns)").fetchall()
            }
            had_delivery_state = "delivery_state" in existing_turn_columns
            self._ensure_column(
                conn,
                "devices",
                "refresh_token_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "devices",
                "refresh_expires_at",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "threads",
                "needs_resync",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "threads", "parent_raw_id", "TEXT")
            self._ensure_column(
                conn,
                "threads",
                "source_kind",
                "TEXT NOT NULL DEFAULT 'unknown'",
            )
            self._ensure_column(
                conn,
                "threads",
                "catalog_state",
                "TEXT NOT NULL DEFAULT 'visible'",
            )
            self._ensure_column(
                conn,
                "threads",
                "catalog_deadline_epoch",
                "INTEGER",
            )
            self._ensure_column(
                conn,
                "threads",
                "lifecycle_state",
                "TEXT NOT NULL DEFAULT 'active'",
            )
            self._ensure_column(
                conn,
                "streams",
                "replay_floor_seq",
                "INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_thread_parent_raw ON threads(parent_raw_id)"
            )
            self._ensure_column(
                conn,
                "turns",
                "delivery_state",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            self._ensure_column(
                conn,
                "turns",
                "attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(conn, "turns", "recovery_reason", "TEXT")
            self._ensure_column(conn, "turns", "request_fingerprint", "TEXT")
            self._ensure_column(
                conn,
                "message_snapshots",
                "role",
                "TEXT NOT NULL DEFAULT 'assistant'",
            )
            self._ensure_column(
                conn,
                "message_snapshots",
                "client_message_id",
                "TEXT",
            )
            self._ensure_column(conn, "message_snapshots", "created_at", "TEXT")
            self._ensure_column(
                conn,
                "message_snapshots",
                "sort_order",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                "message_projection_items",
                "evidence_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                "rate_limit_buckets",
                "window_seconds",
                "INTEGER NOT NULL DEFAULT 60",
            )
            self._ensure_column(
                conn,
                "rate_limit_buckets",
                "expires_at",
                "REAL NOT NULL DEFAULT 0",
            )
            # The pre-expiry schema was never released with a persisted window
            # width. Its only production default was 60 seconds. Preserve an
            # active/default window and any longer backoff, without carrying
            # the old seven-day updated_at TTL forward.
            conn.execute(
                """
                UPDATE rate_limit_buckets
                SET expires_at = MAX(
                    blocked_until,
                    window_started_at + window_seconds,
                    updated_at + window_seconds
                )
                WHERE expires_at <= 0
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rate_limit_expiry
                ON rate_limit_buckets(scope, expires_at)
                """
            )
            conn.execute(
                "UPDATE message_snapshots SET created_at = updated_at WHERE created_at IS NULL"
            )
            if had_delivery_state:
                conn.execute(
                    """
                    UPDATE turns
                    SET delivery_state = CASE
                        WHEN raw_id IS NOT NULL AND status IN
                            ('inProgress', 'running', 'waitingApproval', 'waitingUser') THEN 'accepted'
                        WHEN status = 'pending' THEN 'reserved'
                        WHEN status = 'failed' THEN 'ambiguous'
                        ELSE 'terminal'
                    END
                    WHERE delivery_state IS NULL OR delivery_state = 'legacy'
                    """
                )
            else:
                # Before delivery_state existed, `pending` covered both the
                # pre-write and post-write/pre-response windows. It is unsafe
                # to infer that Codex never accepted such a legacy request.
                conn.execute(
                    """
                    UPDATE turns
                    SET delivery_state = CASE
                        WHEN raw_id IS NOT NULL AND status IN
                            ('inProgress', 'running', 'waitingApproval', 'waitingUser') THEN 'accepted'
                        WHEN status IN ('pending', 'failed') THEN 'ambiguous'
                        ELSE 'terminal'
                    END
                    """
                )
            conn.execute("DROP INDEX IF EXISTS idx_one_active_turn_per_thread")
            self._repair_duplicate_active_turns(conn)
            if not had_delivery_state:
                conn.execute(
                    """
                    UPDATE threads
                    SET status = ?, needs_resync = 1, updated_at = ?
                    WHERE public_id IN (
                        SELECT DISTINCT thread_public_id FROM turns
                        WHERE delivery_state = 'ambiguous'
                    )
                    """,
                    (ThreadStatus.FAILED.value, utc_now()),
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX idx_one_active_turn_per_thread
                ON turns(thread_public_id)
                WHERE status IN (
                    'pending', 'dispatching', 'inProgress',
                    'running', 'waitingApproval', 'waitingUser'
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_message_client_id
                ON message_snapshots(thread_public_id, client_message_id)
                WHERE role = 'user' AND client_message_id IS NOT NULL
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_refresh_token ON devices(refresh_token_hash)"
            )
            conn.execute("PRAGMA user_version = 12")

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        }
        if column not in columns:
            conn.execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration)
            )

    @staticmethod
    def _repair_duplicate_active_turns(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT public_id, raw_id, thread_public_id, status,
                   delivery_state, created_at
            FROM turns
            WHERE status IN (
                'pending', 'dispatching', 'inProgress',
                'running', 'waitingApproval', 'waitingUser'
            )
            ORDER BY thread_public_id ASC,
                CASE
                    WHEN raw_id IS NOT NULL THEN 0
                    WHEN status = 'pending' THEN 1
                    ELSE 2
                END ASC,
                created_at ASC,
                public_id ASC
            """
        ).fetchall()
        grouped: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["thread_public_id"]), []).append(row)
        now = utc_now()
        for thread_id, candidates in grouped.items():
            accepted = [candidate for candidate in candidates if candidate["raw_id"]]
            if len(accepted) > 1:
                for candidate in candidates:
                    conn.execute(
                        """
                        UPDATE turns
                        SET status = 'ambiguous', delivery_state = 'ambiguous',
                            recovery_reason = 'legacyMultipleAcceptedTurns',
                            updated_at = ?
                        WHERE public_id = ?
                        """,
                        (now, candidate["public_id"]),
                    )
                conn.execute(
                    """
                    UPDATE threads
                    SET status = ?, needs_resync = 1, updated_at = ?
                    WHERE public_id = ?
                    """,
                    (ThreadStatus.FAILED.value, now, thread_id),
                )
                continue
            winner = candidates[0]
            for duplicate in candidates[1:]:
                conn.execute(
                    """
                    UPDATE turns
                    SET status = 'failed', delivery_state = 'reconciledDuplicate',
                        recovery_reason = 'legacyDuplicateReservation',
                        updated_at = ?
                    WHERE public_id = ?
                    """,
                    (now, duplicate["public_id"]),
                )
            if winner["raw_id"] is not None:
                conn.execute(
                    "UPDATE turns SET delivery_state = 'accepted' WHERE public_id = ?",
                    (winner["public_id"],),
                )
            elif (
                winner["status"] == "pending"
                and winner["delivery_state"] == "reserved"
            ):
                conn.execute(
                    "UPDATE turns SET delivery_state = 'reserved' WHERE public_id = ?",
                    (winner["public_id"],),
                )
            else:
                conn.execute(
                    """
                    UPDATE turns
                    SET status = 'ambiguous', delivery_state = 'ambiguous',
                        recovery_reason = 'legacyDeliveryStateUnknown',
                        updated_at = ?
                    WHERE public_id = ?
                    """,
                    (now, winner["public_id"]),
                )
                conn.execute(
                    """
                    UPDATE threads
                    SET status = ?, needs_resync = 1, updated_at = ?
                    WHERE public_id = ?
                    """,
                    (ThreadStatus.FAILED.value, now, thread_id),
                )

    def pair_device(
        self,
        name: str,
        device_public_key: str,
        token: str,
        token_expires_at: str,
        refresh_token: str,
        refresh_expires_at: str,
    ) -> DeviceRecord:
        now = utc_now()
        device_id = public_id("dev")
        stream_id = public_id("stream")
        with self._transaction() as conn:
            conn.execute("UPDATE devices SET active = 0, updated_at = ?", (now,))
            conn.execute(
                """
                INSERT INTO devices(
                    id, name, public_key, token_hash, token_expires_at,
                    refresh_token_hash, refresh_expires_at,
                    stream_id, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    device_id,
                    name,
                    device_public_key,
                    secret_hash(token),
                    token_expires_at,
                    secret_hash(refresh_token),
                    refresh_expires_at,
                    stream_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO streams(device_id, stream_id, next_seq, acked_seq, updated_at) VALUES (?, ?, 1, 0, ?)",
                (device_id, stream_id, now),
            )
        return DeviceRecord(
            id=device_id,
            name=name,
            public_key=device_public_key,
            stream_id=stream_id,
            token_expires_at=token_expires_at,
        )

    def ensure_relay_device(self, device_id: str, name: str) -> DeviceRecord:
        """Materialize the Relay-authenticated device used by the Connector.

        This record is intentionally impossible to authenticate through the
        legacy bearer-token API: only a random verifier with no retained
        preimage is stored, and its expiry is in the past. The public Relay is
        the authentication boundary while the private Connector uses this row
        solely for Gateway ownership, idempotency, and event-stream state.
        """

        suffix = (
            device_id[len(RELAY_DEVICE_PREFIX) :]
            if device_id.startswith(RELAY_DEVICE_PREFIX)
            else ""
        )
        if (
            len(suffix) != RELAY_DEVICE_SUFFIX_LENGTH
            or any(character not in RELAY_ID_ALPHABET for character in suffix)
        ):
            raise ValueError("relay device id is invalid")
        normalized_name = name.strip()
        if (
            not normalized_name
            or len(normalized_name) > 120
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized_name)
        ):
            raise ValueError("relay device name is invalid")

        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is not None and row["public_key"] != RELAY_INTERNAL_PUBLIC_KEY:
                raise ValueError("relay device id collides with another identity")

            conn.execute(
                "UPDATE devices SET active = 0, updated_at = ? WHERE active = 1 AND id != ?",
                (now, device_id),
            )
            if row is None:
                stream_id = public_id("stream")
                # The random token preimage is deliberately discarded. This
                # row can never authenticate on the legacy HTTP/WSS surface.
                unusable_token_hash = secret_hash(secrets.token_urlsafe(48))
                conn.execute(
                    """
                    INSERT INTO devices(
                        id, name, public_key, token_hash, token_expires_at,
                        refresh_token_hash, refresh_expires_at,
                        stream_id, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 1, ?, ?)
                    """,
                    (
                        device_id,
                        normalized_name,
                        RELAY_INTERNAL_PUBLIC_KEY,
                        unusable_token_hash,
                        "1970-01-01T00:00:00Z",
                        stream_id,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO streams(device_id, stream_id, next_seq, acked_seq, updated_at)
                    VALUES (?, ?, 1, 0, ?)
                    """,
                    (device_id, stream_id, now),
                )
            else:
                stream_id = str(row["stream_id"])
                conn.execute(
                    "UPDATE devices SET name = ?, active = 1, updated_at = ? WHERE id = ?",
                    (normalized_name, now, device_id),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO streams(
                        device_id, stream_id, next_seq, acked_seq, updated_at
                    ) VALUES (?, ?, 1, 0, ?)
                    """,
                    (device_id, stream_id, now),
                )

        return DeviceRecord(
            id=device_id,
            name=normalized_name,
            public_key=RELAY_INTERNAL_PUBLIC_KEY,
            stream_id=stream_id,
            token_expires_at="1970-01-01T00:00:00Z",
        )

    def ensure_pairing_grant(self, pairing_secret: str) -> None:
        pairing_secret_hash = secret_hash(pairing_secret)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO pairing_grants(
                    secret_hash, state, failed_attempts, max_attempts, created_at
                ) VALUES (?, 'active', 0, ?, ?)
                """,
                # The legacy attempt fields remain for schema compatibility.
                # Public probes are throttled in rate_limit_buckets and never
                # permanently mutate or lock the one-time pairing grant.
                (pairing_secret_hash, 1, utc_now()),
            )
            # Earlier builds permanently marked a grant `locked` after a few
            # public probes. Migrate only that legacy state back to active;
            # consumed grants remain consumed and one-time.
            conn.execute(
                """
                UPDATE pairing_grants
                SET state = 'active', failed_attempts = 0
                WHERE secret_hash = ? AND state = 'locked'
                """,
                (pairing_secret_hash,),
            )

    def consume_rate_limit(
        self,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
        now_epoch: Optional[float] = None,
    ) -> RateLimitDecision:
        return self.consume_rate_limits(
            [(scope, subject, limit, window_seconds)],
            now_epoch=now_epoch,
        )

    def consume_rate_limits(
        self,
        rules: List[Tuple[str, str, int, int]],
        now_epoch: Optional[float] = None,
    ) -> RateLimitDecision:
        """Atomically consume a group of fixed-window allowances.

        Subjects are irreversibly hashed before persistence. SQLite's
        BEGIN IMMEDIATE transaction makes the decision consistent across
        Gateway processes that share this database file. The group is all or
        nothing: a blocked global bucket cannot create an unbounded number of
        source rows, and a blocked source cannot drain the global allowance.
        """

        if not rules:
            raise ValueError("at least one rate limit rule is required")
        for _, _, limit, window_seconds in rules:
            if limit < 1 or window_seconds < 1:
                raise ValueError("rate limit and window must be positive")
        current = time.time() if now_epoch is None else float(now_epoch)
        with self._transaction() as conn:
            self._prune_expired_rate_limits(
                conn,
                {scope for scope, _, _, _ in rules},
                current,
            )
            states: List[_RateLimitState] = []
            for scope, subject, limit, window_seconds in rules:
                subject_digest = self._rate_limit_subject_hash(scope, subject)
                row = conn.execute(
                    """
                    SELECT window_started_at, request_count, failure_count,
                           blocked_until
                    FROM rate_limit_buckets
                    WHERE scope = ? AND subject_hash = ?
                    """,
                    (scope, subject_digest),
                ).fetchone()
                if row is None:
                    window_started_at = current
                    request_count = 0
                    failure_count = 0
                    blocked_until = 0.0
                    exists = False
                else:
                    window_started_at = float(row["window_started_at"])
                    request_count = int(row["request_count"])
                    failure_count = int(row["failure_count"])
                    blocked_until = float(row["blocked_until"])
                    exists = True
                    if (
                        current >= window_started_at + window_seconds
                        and blocked_until <= current
                    ):
                        window_started_at = current
                        request_count = 0
                        failure_count = 0
                        blocked_until = 0.0
                states.append(
                    _RateLimitState(
                        scope=scope,
                        subject_hash=subject_digest,
                        window_seconds=window_seconds,
                        limit=limit,
                        window_started_at=window_started_at,
                        request_count=request_count,
                        failure_count=failure_count,
                        blocked_until=blocked_until,
                        exists=exists,
                    )
                )

            new_per_scope: Dict[str, int] = {}
            for state in states:
                if state.blocked_until > current:
                    return RateLimitDecision(
                        allowed=False,
                        retry_after_seconds=max(
                            1, math.ceil(state.blocked_until - current)
                        ),
                    )
                if state.request_count >= state.limit:
                    retry_at = state.window_started_at + state.window_seconds
                    return RateLimitDecision(
                        allowed=False,
                        retry_after_seconds=max(1, math.ceil(retry_at - current)),
                    )
                if not state.exists:
                    bucket_count = conn.execute(
                        """
                        SELECT COUNT(*) FROM rate_limit_buckets WHERE scope = ?
                        """,
                        (state.scope,),
                    ).fetchone()[0]
                    pending = new_per_scope.get(state.scope, 0)
                    if (
                        bucket_count + pending
                        >= self.RATE_LIMIT_BUCKETS_PER_SCOPE
                    ):
                        return RateLimitDecision(
                            allowed=False,
                            retry_after_seconds=state.window_seconds,
                        )
                    new_per_scope[state.scope] = pending + 1

            for state in states:
                expires_at = max(
                    state.window_started_at + state.window_seconds,
                    state.blocked_until,
                )
                conn.execute(
                    """
                    INSERT INTO rate_limit_buckets(
                        scope, subject_hash, window_started_at, request_count,
                        failure_count, blocked_until, window_seconds,
                        expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, subject_hash) DO UPDATE SET
                        window_started_at = excluded.window_started_at,
                        request_count = excluded.request_count,
                        failure_count = excluded.failure_count,
                        blocked_until = excluded.blocked_until,
                        window_seconds = excluded.window_seconds,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        state.scope,
                        state.subject_hash,
                        state.window_started_at,
                        state.request_count + 1,
                        state.failure_count,
                        state.blocked_until,
                        state.window_seconds,
                        expires_at,
                        current,
                    ),
                )
            return RateLimitDecision(allowed=True)

    def penalize_rate_limit(
        self,
        scope: str,
        subject: str,
        base_delay_seconds: int,
        max_delay_seconds: int,
        window_seconds: int,
        now_epoch: Optional[float] = None,
    ) -> int:
        """Apply bounded exponential backoff without touching a grant."""

        if min(base_delay_seconds, max_delay_seconds, window_seconds) < 1:
            raise ValueError("backoff values must be positive")
        if base_delay_seconds > max_delay_seconds:
            raise ValueError("base backoff must not exceed maximum backoff")
        current = time.time() if now_epoch is None else float(now_epoch)
        subject_digest = self._rate_limit_subject_hash(scope, subject)
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT window_started_at, request_count, failure_count,
                       blocked_until
                FROM rate_limit_buckets
                WHERE scope = ? AND subject_hash = ?
                """,
                (scope, subject_digest),
            ).fetchone()
            if row is None or (
                current >= float(row["window_started_at"]) + window_seconds
                and float(row["blocked_until"]) <= current
            ):
                window_started_at = current
                request_count = 0
                failures = 1
            else:
                window_started_at = float(row["window_started_at"])
                request_count = int(row["request_count"])
                failures = int(row["failure_count"]) + 1
            exponent = min(failures - 1, 30)
            delay = min(max_delay_seconds, base_delay_seconds * (2**exponent))
            blocked_until = current + delay
            expires_at = max(
                window_started_at + window_seconds,
                blocked_until,
            )
            conn.execute(
                """
                INSERT INTO rate_limit_buckets(
                    scope, subject_hash, window_started_at, request_count,
                    failure_count, blocked_until, window_seconds,
                    expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, subject_hash) DO UPDATE SET
                    window_started_at = excluded.window_started_at,
                    request_count = excluded.request_count,
                    failure_count = excluded.failure_count,
                    blocked_until = excluded.blocked_until,
                    window_seconds = excluded.window_seconds,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    scope,
                    subject_digest,
                    window_started_at,
                    request_count,
                    failures,
                    blocked_until,
                    window_seconds,
                    expires_at,
                    current,
                ),
            )
            return delay

    def clear_rate_limit_penalty(self, scope: str, subject: str) -> None:
        subject_digest = self._rate_limit_subject_hash(scope, subject)
        current = time.time()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE rate_limit_buckets SET
                    failure_count = 0,
                    blocked_until = 0,
                    expires_at = MAX(
                        window_started_at + window_seconds,
                        ?
                    ),
                    updated_at = ?
                WHERE scope = ? AND subject_hash = ?
                """,
                (current, current, scope, subject_digest),
            )

    @staticmethod
    def _rate_limit_subject_hash(scope: str, subject: str) -> str:
        return secret_hash("rate-limit\0%s\0%s" % (scope, subject))

    @staticmethod
    def _prune_expired_rate_limits(
        conn: sqlite3.Connection,
        scopes: set[str],
        current: float,
    ) -> None:
        for scope in sorted(scopes):
            conn.execute(
                """
                DELETE FROM rate_limit_buckets
                WHERE scope = ? AND expires_at <= ? AND blocked_until <= ?
                """,
                (scope, current, current),
            )

    def consume_pairing_grant_and_pair(
        self,
        expected_secret: str,
        presented_secret: str,
        name: str,
        device_public_key: str,
        token: str,
        token_expires_at: str,
        refresh_token: str,
        refresh_expires_at: str,
    ) -> Tuple[Optional[DeviceRecord], str]:
        """Atomically consume a one-time grant and activate exactly one device."""

        now = utc_now()
        expected_hash = secret_hash(expected_secret)
        presented_hash = secret_hash(presented_secret)
        with self._transaction() as conn:
            grant = conn.execute(
                "SELECT * FROM pairing_grants WHERE secret_hash = ?",
                (expected_hash,),
            ).fetchone()
            if grant is None or grant["state"] != "active":
                return None, "denied"
            if not secrets.compare_digest(expected_hash, presented_hash):
                return None, "denied"
            consumed = conn.execute(
                """
                UPDATE pairing_grants
                SET state = 'consumed', consumed_at = ?
                WHERE secret_hash = ? AND state = 'active'
                """,
                (now, expected_hash),
            )
            if consumed.rowcount != 1:
                return None, "denied"
            device_id = public_id("dev")
            stream_id = public_id("stream")
            conn.execute("UPDATE devices SET active = 0, updated_at = ?", (now,))
            conn.execute(
                """
                INSERT INTO devices(
                    id, name, public_key, token_hash, token_expires_at,
                    refresh_token_hash, refresh_expires_at,
                    stream_id, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    device_id,
                    name,
                    device_public_key,
                    secret_hash(token),
                    token_expires_at,
                    secret_hash(refresh_token),
                    refresh_expires_at,
                    stream_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO streams(device_id, stream_id, next_seq, acked_seq, updated_at)
                VALUES (?, ?, 1, 0, ?)
                """,
                (device_id, stream_id, now),
            )
            return (
                DeviceRecord(
                    id=device_id,
                    name=name,
                    public_key=device_public_key,
                    stream_id=stream_id,
                    token_expires_at=token_expires_at,
                ),
                "paired",
            )

    def authenticate(self, token: str, now: Optional[str] = None) -> Optional[DeviceRecord]:
        device, outcome = self.authenticate_with_outcome(token, now=now)
        return device if outcome == "valid" else None

    def authenticate_with_outcome(
        self,
        token: str,
        now: Optional[str] = None,
    ) -> Tuple[Optional[DeviceRecord], str]:
        current = utc_now() if now is None else now
        row = self._conn.execute(
            """
            SELECT id, name, public_key, stream_id, token_expires_at, active
            FROM devices
            WHERE token_hash = ?
            """,
            (secret_hash(token),),
        ).fetchone()
        if row is None:
            return None, "invalid"
        device = DeviceRecord(
            id=row["id"],
            name=row["name"],
            public_key=row["public_key"],
            stream_id=row["stream_id"],
            token_expires_at=row["token_expires_at"],
        )
        if not bool(row["active"]):
            return device, "revoked"
        if row["token_expires_at"] <= current:
            return device, "expired"
        return device, "valid"

    def claim_request_proof_nonce(
        self,
        subject: str,
        nonce: bytes,
        signed_at: int,
        now_epoch: int,
        retention_seconds: int,
    ) -> bool:
        subject_digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        nonce_digest = hashlib.sha256(nonce).hexdigest()
        cutoff = int(now_epoch) - max(1, int(retention_seconds))
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM request_proof_nonces WHERE signed_at < ?",
                (cutoff,),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO request_proof_nonces(
                        subject_hash, nonce_hash, signed_at, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (subject_digest, nonce_digest, int(signed_at), utc_now()),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def rotate_refresh_token(
        self,
        expected_device_id: str,
        refresh_token: str,
        new_access_token: str,
        access_expires_at: str,
        new_refresh_token: str,
        refresh_expires_at: str,
        now: Optional[str] = None,
    ) -> Tuple[Optional[DeviceRecord], str]:
        current = utc_now() if now is None else now
        old_hash = secret_hash(refresh_token)
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT id, name, public_key, stream_id, token_expires_at,
                       refresh_expires_at, active
                FROM devices WHERE id = ? AND refresh_token_hash = ?
                """,
                (expected_device_id, old_hash),
            ).fetchone()
            if row is None:
                return None, "invalid"
            device = DeviceRecord(
                id=row["id"],
                name=row["name"],
                public_key=row["public_key"],
                stream_id=row["stream_id"],
                token_expires_at=row["token_expires_at"],
            )
            if not bool(row["active"]):
                return device, "revoked"
            if not row["refresh_expires_at"] or row["refresh_expires_at"] <= current:
                return device, "expired"
            cursor = conn.execute(
                """
                UPDATE devices
                SET token_hash = ?, token_expires_at = ?,
                    refresh_token_hash = ?, refresh_expires_at = ?, updated_at = ?
                WHERE id = ? AND refresh_token_hash = ? AND active = 1
                """,
                (
                    secret_hash(new_access_token),
                    access_expires_at,
                    secret_hash(new_refresh_token),
                    refresh_expires_at,
                    current,
                    row["id"],
                    old_hash,
                ),
            )
            if cursor.rowcount != 1:
                return None, "invalid"
            rotated = DeviceRecord(
                id=row["id"],
                name=row["name"],
                public_key=row["public_key"],
                stream_id=row["stream_id"],
                token_expires_at=access_expires_at,
            )
            return rotated, "rotated"

    def revoke_device(self, device_id: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE devices SET active = 0, updated_at = ? WHERE id = ? AND active = 1",
                (utc_now(), device_id),
            )
        return cursor.rowcount == 1

    def active_device(self) -> Optional[DeviceRecord]:
        row = self._conn.execute(
            """
            SELECT id, name, public_key, stream_id, token_expires_at
            FROM devices WHERE active = 1 ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            id=row["id"],
            name=row["name"],
            public_key=row["public_key"],
            stream_id=row["stream_id"],
            token_expires_at=row["token_expires_at"],
        )

    def device_by_id(self, device_id: str) -> Optional[DeviceRecord]:
        row = self._conn.execute(
            """
            SELECT id, name, public_key, stream_id, token_expires_at
            FROM devices WHERE id = ?
            """,
            (device_id,),
        ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            public_key=str(row["public_key"]),
            stream_id=str(row["stream_id"]),
            token_expires_at=str(row["token_expires_at"]),
        )

    def get_or_create_thread(
        self,
        raw_id: str,
        title: str = "New conversation",
        preview: str = "",
        status: str = ThreadStatus.IDLE.value,
        archived: bool = False,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> ThreadDTO:
        existing = self._conn.execute(
            "SELECT * FROM threads WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        if existing is not None:
            return self._thread_from_row(existing)

        now = utc_now()
        public_thread_id = public_id("thr")
        created = created_at or now
        updated = updated_at or created
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM thread_tombstones WHERE raw_id = ?", (raw_id,)
            ).fetchone() is not None:
                raise ThreadLifecycleBlockedError("deleted")
            try:
                conn.execute(
                    """
                    INSERT INTO threads(
                        public_id, raw_id, title, preview, created_at, updated_at,
                        status, archived
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        public_thread_id,
                        raw_id,
                        title or "New conversation",
                        preview,
                        created,
                        updated,
                        ThreadStatus.ARCHIVED.value if archived else status,
                        int(archived),
                    ),
                )
            except sqlite3.IntegrityError:
                pass
        row = self._conn.execute(
            "SELECT * FROM threads WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("thread mapping was not created")
        return self._thread_from_row(row)

    def create_thread_with_snapshot_event(
        self,
        device_id: str,
        raw_id: str,
        title: str,
        preview: str,
        status: str,
        archived: bool,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        provisional_ttl_seconds: int = 3_600,
    ) -> ThreadDTO:
        now = utc_now()
        provisional_deadline = int(time.time()) + max(
            1, min(int(provisional_ttl_seconds), 86_400)
        )
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM thread_tombstones WHERE raw_id = ?", (raw_id,)
            ).fetchone() is not None:
                raise ThreadLifecycleBlockedError("deleted")
            row = conn.execute(
                "SELECT * FROM threads WHERE raw_id = ?", (raw_id,)
            ).fetchone()
            if row is None:
                thread_id = public_id("thr")
                created = created_at or now
                updated = updated_at or created
                conn.execute(
                    """
                    INSERT INTO threads(
                        public_id, raw_id, title, preview, created_at, updated_at,
                        status, archived, catalog_state, catalog_deadline_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?)
                    """,
                    (
                        thread_id,
                        raw_id,
                        title or "New conversation",
                        preview,
                        created,
                        updated,
                        ThreadStatus.ARCHIVED.value if archived else status,
                        int(archived),
                        provisional_deadline,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM threads WHERE public_id = ?", (thread_id,)
                ).fetchone()
            if row is None:
                raise RuntimeError("thread mapping was not created")
            thread = self._thread_from_row(row)
            self._append_event_in_transaction(
                conn,
                device_id,
                "thread.snapshot",
                thread.model_dump(by_alias=True),
                thread.id,
            )
            return thread

    def thread_by_public_id(self, thread_id: str) -> Optional[ThreadDTO]:
        row = self._conn.execute(
            "SELECT * FROM threads WHERE public_id = ?", (thread_id,)
        ).fetchone()
        return None if row is None else self._thread_from_row(row)

    def thread_raw_id(self, thread_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT raw_id FROM threads WHERE public_id = ?", (thread_id,)
        ).fetchone()
        return None if row is None else str(row["raw_id"])

    def thread_public_id(self, raw_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT public_id FROM threads WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        return None if row is None else str(row["public_id"])

    def list_threads(self, include_archived: bool = False) -> List[ThreadDTO]:
        rows = self._conn.execute(
            """
            SELECT * FROM threads
            WHERE archived = ?
            ORDER BY updated_at DESC
            """,
            (int(include_archived),),
        ).fetchall()
        return [self._thread_from_row(row) for row in rows]

    def reconcile_thread_catalog(
        self,
        device_id: str,
        catalog: List[Dict[str, Any]],
        scope_root_raw_id: Optional[str] = None,
    ) -> List[ThreadDTO]:
        """Reconcile local Thread authority from one complete Codex catalog scan.

        The caller must provide both active and archived partitions. Mapping
        cleanup and every public snapshot/update/delete event are committed in
        the same SQLite writer transaction. A scoped stability scan may mutate
        only the union of the remote and existing root closure; a transiently
        missing unrelated partition entry must never become authoritative.
        """

        input_raw_ids = [str(record["raw_id"]) for record in catalog]
        if len(input_raw_ids) != len(set(input_raw_ids)):
            raise ValueError("authoritative thread catalog contains duplicates")
        now = utc_now()
        now_epoch = int(time.time())
        with self._transaction() as conn:
            tombstoned_raw_ids = {
                str(row["raw_id"])
                for row in conn.execute(
                    "SELECT raw_id FROM thread_tombstones"
                ).fetchall()
            }
            catalog = [
                record
                for record in catalog
                if str(record["raw_id"]) not in tombstoned_raw_ids
            ]
            existing_rows = conn.execute("SELECT * FROM threads").fetchall()
            existing_by_raw = {str(row["raw_id"]): row for row in existing_rows}
            remote_by_raw = {
                str(record["raw_id"]): record for record in catalog
            }
            if scope_root_raw_id is None:
                records_to_upsert = catalog
                rows_authorized_for_delete = existing_rows
            else:
                remote_scope = self._raw_closure_ids(
                    catalog,
                    scope_root_raw_id,
                )
                local_scope = self._raw_closure_ids(
                    existing_rows,
                    scope_root_raw_id,
                )
                scoped_raw_ids = remote_scope | local_scope
                records_to_upsert = [
                    record
                    for record in catalog
                    if str(record["raw_id"]) in scoped_raw_ids
                ]
                rows_authorized_for_delete = [
                    row
                    for row in existing_rows
                    if str(row["raw_id"]) in local_scope
                ]

            for record in records_to_upsert:
                raw_id = str(record["raw_id"])
                archived = bool(record["archived"])
                status = (
                    ThreadStatus.ARCHIVED.value
                    if archived
                    else str(record.get("status") or ThreadStatus.IDLE.value)
                )
                title = str(record.get("title") or "New conversation")
                preview = str(record.get("preview") or "")
                parent_raw_id = record.get("parent_raw_id")
                if parent_raw_id is not None:
                    parent_raw_id = str(parent_raw_id)
                source_kind = str(record.get("source_kind") or "unknown")
                row = existing_by_raw.get(raw_id)
                if row is None:
                    thread_id = public_id("thr")
                    created_at = str(record.get("created_at") or now)
                    updated_at = str(record.get("updated_at") or created_at)
                    conn.execute(
                        """
                        INSERT INTO threads(
                            public_id, raw_id, title, preview, created_at,
                            updated_at, status, archived, parent_raw_id,
                            source_kind
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            thread_id,
                            raw_id,
                            title,
                            preview,
                            created_at,
                            updated_at,
                            status,
                            int(archived),
                            parent_raw_id,
                            source_kind,
                        ),
                    )
                    inserted = conn.execute(
                        "SELECT * FROM threads WHERE public_id = ?", (thread_id,)
                    ).fetchone()
                    if inserted is None:
                        raise RuntimeError("thread mapping was not created")
                    thread = self._thread_from_row(inserted)
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        "thread.snapshot",
                        thread.model_dump(by_alias=True),
                        thread.id,
                    )
                    continue

                public_changed = any(
                    (
                        str(row["title"]) != title,
                        str(row["preview"]) != preview,
                        str(row["status"]) != status,
                        bool(row["archived"]) != archived,
                    )
                )
                topology_changed = (
                    row["parent_raw_id"] != parent_raw_id
                    or str(row["source_kind"] or "unknown") != source_kind
                )
                catalog_changed = str(row["catalog_state"]) != "visible"
                updated_at = str(record.get("updated_at") or row["updated_at"])
                if (
                    public_changed
                    or topology_changed
                    or catalog_changed
                    or updated_at != row["updated_at"]
                ):
                    conn.execute(
                        """
                        UPDATE threads
                        SET title = ?, preview = ?, updated_at = ?, status = ?,
                            archived = ?, parent_raw_id = ?, source_kind = ?,
                            catalog_state = 'visible', catalog_deadline_epoch = NULL
                        WHERE public_id = ?
                        """,
                        (
                            title,
                            preview,
                            updated_at,
                            status,
                            int(archived),
                            parent_raw_id,
                            source_kind,
                            row["public_id"],
                        ),
                    )
                if public_changed:
                    changed = conn.execute(
                        "SELECT * FROM threads WHERE public_id = ?",
                        (row["public_id"],),
                    ).fetchone()
                    if changed is None:
                        raise RuntimeError("thread mapping disappeared")
                    thread = self._thread_from_row(changed)
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        "thread.updated",
                        thread.model_dump(by_alias=True),
                        thread.id,
                    )

            stale_rows = [
                row
                for row in rows_authorized_for_delete
                if str(row["raw_id"]) not in remote_by_raw
                and not (
                    str(row["catalog_state"]) == "provisional"
                    and row["catalog_deadline_epoch"] is not None
                    and int(row["catalog_deadline_epoch"]) > now_epoch
                )
            ]
            self._delete_thread_rows_in_transaction(
                conn,
                device_id,
                stale_rows,
                reason="authoritativeCatalogMissing",
            )

            rows = conn.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC, public_id ASC"
            ).fetchall()
            return [self._thread_from_row(row) for row in rows]

    @staticmethod
    def _raw_closure_ids(
        records: Iterable[Any],
        root_raw_id: str,
    ) -> set:
        parent_by_raw: Dict[str, Optional[str]] = {}
        for record in records:
            raw_id = str(record["raw_id"])
            parent = record["parent_raw_id"]
            parent_by_raw[raw_id] = None if parent is None else str(parent)

        closure = set()
        for raw_id in parent_by_raw:
            cursor: Optional[str] = raw_id
            seen = set()
            while cursor is not None and cursor not in seen:
                if cursor == root_raw_id:
                    closure.add(raw_id)
                    break
                seen.add(cursor)
                cursor = parent_by_raw.get(cursor)
        return closure

    def delete_threads_by_raw_ids(
        self,
        device_id: str,
        root_raw_ids: Iterable[str],
        reason: str,
    ) -> Tuple[str, ...]:
        roots = tuple(sorted(set(str(value) for value in root_raw_ids if value)))
        if not roots:
            return ()
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM threads").fetchall()
            by_raw = {str(row["raw_id"]): row for row in rows}
            children: Dict[str, List[str]] = {}
            for row in rows:
                if row["parent_raw_id"] is not None:
                    children.setdefault(str(row["parent_raw_id"]), []).append(
                        str(row["raw_id"])
                    )
            affected_raw_ids: List[str] = []
            pending = list(roots)
            while pending:
                raw_id = pending.pop(0)
                if raw_id in affected_raw_ids:
                    continue
                affected_raw_ids.append(raw_id)
                pending.extend(sorted(children.get(raw_id, [])))
            affected_rows = [
                by_raw[raw_id] for raw_id in affected_raw_ids if raw_id in by_raw
            ]
            deleted_public_ids = self._delete_thread_rows_in_transaction(
                conn,
                device_id,
                affected_rows,
                reason=reason,
            )
            now = utc_now()
            for raw_id in affected_raw_ids:
                if raw_id in by_raw:
                    continue
                conn.execute(
                    """
                    INSERT INTO thread_tombstones(raw_id, public_id, deleted_at, reason)
                    VALUES (?, NULL, ?, ?)
                    ON CONFLICT(raw_id) DO NOTHING
                    """,
                    (raw_id, now, reason),
                )
            return deleted_public_ids

    def _delete_thread_rows_in_transaction(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        rows: Iterable[sqlite3.Row],
        reason: str,
    ) -> Tuple[str, ...]:
        unique = {
            str(row["public_id"]): row
            for row in rows
            if row is not None
        }
        if not unique:
            return ()
        now = utc_now()
        thread_ids = tuple(sorted(unique))
        placeholders = ",".join("?" for _ in thread_ids)
        self._queue_asset_rows_in_transaction(
            conn,
            "threadDeleted",
            "thread_public_id IN (%s)" % placeholders,
            thread_ids,
        )
        purged_streams = conn.execute(
            """
            SELECT stream_id, MAX(seq) AS max_seq
            FROM event_outbox
            WHERE thread_public_id IN (%s)
            GROUP BY stream_id
            """ % placeholders,
            thread_ids,
        ).fetchall()
        conn.execute(
            "DELETE FROM event_outbox WHERE thread_public_id IN (%s)"
            % placeholders,
            thread_ids,
        )
        # Selective history purge can create internal sequence holes. Persist a
        # replay floor per stream so every cursor before the last removed event
        # is forced through snapshot sync, while unrelated retained events stay
        # intact and remain replayable from a sufficiently current cursor.
        for stream in purged_streams:
            conn.execute(
                """
                UPDATE streams
                SET replay_floor_seq = MAX(replay_floor_seq, ?), updated_at = ?
                WHERE stream_id = ?
                """,
                (int(stream["max_seq"]), now, str(stream["stream_id"])),
            )
        for thread_id in sorted(unique):
            row = unique[thread_id]
            conn.execute(
                """
                INSERT INTO thread_tombstones(raw_id, public_id, deleted_at, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(raw_id) DO UPDATE SET
                    public_id = COALESCE(thread_tombstones.public_id, excluded.public_id)
                """,
                (str(row["raw_id"]), thread_id, now, reason),
            )
            conn.execute(
                "DELETE FROM id_mappings WHERE thread_public_id = ?",
                (thread_id,),
            )
            conn.execute("DELETE FROM threads WHERE public_id = ?", (thread_id,))
        for thread_id in sorted(unique):
            self._append_event_in_transaction(
                conn,
                device_id,
                "thread.deleted",
                {"threadId": thread_id},
                thread_id,
            )
        return tuple(sorted(unique))

    def is_thread_tombstoned(self, raw_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM thread_tombstones WHERE raw_id = ?", (raw_id,)
        ).fetchone() is not None

    def thread_accepts_events(self, thread_id: str) -> bool:
        row = self._conn.execute(
            "SELECT archived, lifecycle_state FROM threads WHERE public_id = ?",
            (thread_id,),
        ).fetchone()
        return bool(
            row is not None
            and not bool(row["archived"])
            and str(row["lifecycle_state"]) == "active"
        )

    @staticmethod
    def _require_thread_mutable(
        conn: sqlite3.Connection,
        thread_id: str,
        allow_archived: bool = True,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT archived, lifecycle_state FROM threads WHERE public_id = ?
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ThreadLifecycleBlockedError("deleted")
        if str(row["lifecycle_state"]) != "active":
            raise ThreadLifecycleBlockedError("pendingDelete")
        if not allow_archived and bool(row["archived"]):
            raise ThreadLifecycleBlockedError("archived")
        return row

    def thread_catalog_rows(self) -> List[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM threads ORDER BY public_id ASC"
        ).fetchall()

    def provisional_thread_rows(self) -> List[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM threads
            WHERE catalog_state = 'provisional'
            ORDER BY raw_id ASC
            """
        ).fetchall()

    def thread_catalog_state(self, thread_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT catalog_state FROM threads WHERE public_id = ?",
            (thread_id,),
        ).fetchone()
        return None if row is None else str(row["catalog_state"])

    def update_provisional_thread_metadata(
        self,
        device_id: str,
        thread_id: str,
        *,
        title: Optional[str],
        archived: Optional[bool],
    ) -> Optional[ThreadDTO]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE public_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None or str(row["catalog_state"]) != "provisional":
                return None
            next_title = str(row["title"]) if title is None else title
            next_archived = bool(row["archived"]) if archived is None else archived
            next_status = str(row["status"])
            if archived is not None:
                next_status = (
                    ThreadStatus.ARCHIVED.value
                    if archived
                    else ThreadStatus.IDLE.value
                )
            public_changed = any(
                (
                    next_title != str(row["title"]),
                    next_archived != bool(row["archived"]),
                    next_status != str(row["status"]),
                )
            )
            if public_changed:
                conn.execute(
                    """
                    UPDATE threads
                    SET title = ?, archived = ?, status = ?, updated_at = ?
                    WHERE public_id = ? AND catalog_state = 'provisional'
                    """,
                    (
                        next_title,
                        int(next_archived),
                        next_status,
                        utc_now(),
                        thread_id,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM threads WHERE public_id = ?",
                (thread_id,),
            ).fetchone()
            if updated is None:
                return None
            thread = self._thread_from_row(updated)
            if public_changed:
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "thread.updated",
                    thread.model_dump(by_alias=True),
                    thread.id,
                )
            return thread

    def delete_missing_provisional_thread(
        self,
        device_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM threads
                WHERE public_id = ? AND catalog_state = 'provisional'
                """,
                (thread_id,),
            ).fetchone()
            if row is None:
                return False
            self._delete_thread_rows_in_transaction(
                conn,
                device_id,
                (row,),
                reason=reason,
            )
            return True

    def thread_dto_by_raw_id(self, raw_id: str) -> Optional[ThreadDTO]:
        row = self._conn.execute(
            "SELECT * FROM threads WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        return None if row is None else self._thread_from_row(row)

    def create_delete_impact(
        self,
        token: str,
        device_id: str,
        root_thread_public_id: str,
        root_raw_id: str,
        affected_raw_ids: Iterable[str],
        affected_public_ids: Iterable[str],
        expires_at: str,
    ) -> None:
        raw_ids = tuple(sorted(set(str(value) for value in affected_raw_ids)))
        public_ids = tuple(sorted(set(str(value) for value in affected_public_ids)))
        if not raw_ids or not public_ids:
            raise ValueError("delete impact cannot be empty")
        now = utc_now()
        with self._transaction() as conn:
            placeholders = ",".join("?" for _ in public_ids)
            thread_rows = conn.execute(
                """
                SELECT archived, lifecycle_state FROM threads
                WHERE public_id IN (%s)
                """ % placeholders,
                public_ids,
            ).fetchall()
            if len(thread_rows) != len(public_ids):
                raise ThreadLifecycleBlockedError("deleted")
            if any(
                str(row["lifecycle_state"]) != "active" for row in thread_rows
            ):
                raise ThreadLifecycleBlockedError("pendingDelete")
            if any(not bool(row["archived"]) for row in thread_rows):
                raise ThreadLifecycleBlockedError("active")
            conn.execute(
                """
                UPDATE thread_delete_impacts
                SET state = 'expired', completed_at = ?
                WHERE state = 'pending' AND expires_at <= ?
                """,
                (now, now),
            )
            # Only the newest preview for a device/root pair remains usable.
            # This bounds pending authorizations and makes repeated previews
            # invalidate stale UI confirmation sheets.
            conn.execute(
                """
                UPDATE thread_delete_impacts
                SET state = 'invalidated', completed_at = ?
                WHERE state = 'pending' AND device_id = ?
                    AND root_thread_public_id = ?
                """,
                (now, device_id, root_thread_public_id),
            )
            # Retain a bounded terminal audit tail without allowing previews
            # to grow the database indefinitely.
            conn.execute(
                """
                DELETE FROM thread_delete_impacts
                WHERE token_hash IN (
                    SELECT token_hash FROM thread_delete_impacts
                    WHERE state != 'pending'
                    ORDER BY created_at DESC, token_hash DESC
                    LIMIT -1 OFFSET 1024
                )
                """
            )
            conn.execute(
                """
                INSERT INTO thread_delete_impacts(
                    token_hash, device_id, root_thread_public_id, root_raw_id,
                    affected_raw_ids_json, affected_public_ids_json, expires_at,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    secret_hash(token),
                    device_id,
                    root_thread_public_id,
                    root_raw_id,
                    json.dumps(raw_ids, separators=(",", ":")),
                    json.dumps(public_ids, separators=(",", ":")),
                    expires_at,
                    now,
                ),
            )

    def inspect_delete_impact(
        self,
        token: str,
        device_id: str,
        root_thread_public_id: str,
        now: Optional[str] = None,
    ) -> Tuple[str, Optional[DeleteImpactRecord]]:
        checked_at = now or utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM thread_delete_impacts WHERE token_hash = ?",
                (secret_hash(token),),
            ).fetchone()
            if row is None:
                return "invalid", None
            if (
                str(row["device_id"]) != device_id
                or str(row["root_thread_public_id"]) != root_thread_public_id
            ):
                return "invalid", None
            if str(row["state"]) != "pending":
                state = str(row["state"])
                outcome = (
                    "expired"
                    if state == "expired"
                    else "changed"
                    if state == "invalidated"
                    else "used"
                )
                return outcome, self._delete_impact_from_row(row)
            if str(row["expires_at"]) <= checked_at:
                conn.execute(
                    """
                    UPDATE thread_delete_impacts
                    SET state = 'expired', completed_at = ?
                    WHERE token_hash = ? AND state = 'pending'
                    """,
                    (checked_at, row["token_hash"]),
                )
                expired = conn.execute(
                    "SELECT * FROM thread_delete_impacts WHERE token_hash = ?",
                    (row["token_hash"],),
                ).fetchone()
                return "expired", self._delete_impact_from_row(expired or row)
            return "pending", self._delete_impact_from_row(row)

    def claim_delete_impact(
        self,
        token: str,
        device_id: str,
        root_thread_public_id: str,
        affected_raw_ids: Iterable[str],
        affected_public_ids: Iterable[str],
        now: Optional[str] = None,
    ) -> Tuple[str, Optional[DeleteImpactRecord]]:
        checked_at = now or utc_now()
        expected_raw = tuple(sorted(set(str(value) for value in affected_raw_ids)))
        expected_public = tuple(
            sorted(set(str(value) for value in affected_public_ids))
        )
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM thread_delete_impacts WHERE token_hash = ?",
                (secret_hash(token),),
            ).fetchone()
            if row is None:
                return "invalid", None
            record = self._delete_impact_from_row(row)
            if (
                record.device_id != device_id
                or record.root_thread_public_id != root_thread_public_id
            ):
                return "invalid", None
            if record.state != "pending":
                outcome = (
                    "expired"
                    if record.state == "expired"
                    else "changed"
                    if record.state == "invalidated"
                    else "used"
                )
                return outcome, record
            if record.expires_at <= checked_at:
                conn.execute(
                    """
                    UPDATE thread_delete_impacts
                    SET state = 'expired', completed_at = ?
                    WHERE token_hash = ? AND state = 'pending'
                    """,
                    (checked_at, record.token_hash),
                )
                return "expired", record
            if (
                record.affected_raw_ids != expected_raw
                or record.affected_public_ids != expected_public
            ):
                conn.execute(
                    """
                    UPDATE thread_delete_impacts
                    SET state = 'invalidated', completed_at = ?
                    WHERE token_hash = ? AND state = 'pending'
                    """,
                    (checked_at, record.token_hash),
                )
                return "changed", record
            if not expected_public:
                return "changed", record
            placeholders = ",".join("?" for _ in expected_public)
            thread_rows = conn.execute(
                """
                SELECT public_id, archived, lifecycle_state
                FROM threads WHERE public_id IN (%s)
                """ % placeholders,
                expected_public,
            ).fetchall()
            if len(thread_rows) != len(expected_public):
                return "changed", record
            if any(not bool(value["archived"]) for value in thread_rows):
                return "archived", record
            if any(
                str(value["lifecycle_state"]) != "active"
                for value in thread_rows
            ):
                return "busy", record
            active_turn = conn.execute(
                """
                SELECT 1 FROM turns
                WHERE thread_public_id IN (%s) AND status IN (
                    'pending', 'dispatching', 'inProgress', 'running',
                    'waitingApproval', 'waitingUser'
                ) LIMIT 1
                """ % placeholders,
                expected_public,
            ).fetchone()
            if active_turn is not None:
                return "busy", record
            cursor = conn.execute(
                """
                UPDATE thread_delete_impacts
                SET state = 'claimed', claimed_at = ?
                WHERE token_hash = ? AND state = 'pending'
                """,
                (checked_at, record.token_hash),
            )
            if cursor.rowcount != 1:
                return "used", record
            conn.execute(
                """
                UPDATE threads SET lifecycle_state = 'pendingDelete'
                WHERE public_id IN (%s) AND lifecycle_state = 'active'
                """ % placeholders,
                expected_public,
            )
            claimed = conn.execute(
                "SELECT * FROM thread_delete_impacts WHERE token_hash = ?",
                (record.token_hash,),
            ).fetchone()
            return "claimed", self._delete_impact_from_row(claimed or row)

    def complete_delete_impact(
        self,
        token_hash: str,
        affected_raw_ids: Iterable[str],
    ) -> None:
        now = utc_now()
        affected = tuple(sorted(set(str(value) for value in affected_raw_ids)))
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE thread_delete_impacts
                SET state = 'consumed', completed_at = ?
                WHERE token_hash = ? AND state = 'claimed'
                """,
                (now, token_hash),
            )
            if affected:
                placeholders = ",".join("?" for _ in affected)
                conn.execute(
                    """
                    UPDATE thread_delete_impacts
                    SET state = 'invalidated', completed_at = ?
                    WHERE state = 'pending' AND root_raw_id IN (%s)
                    """ % placeholders,
                    (now,) + affected,
                )
            record = conn.execute(
                """
                SELECT affected_public_ids_json FROM thread_delete_impacts
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if record is not None:
                public_ids = tuple(json.loads(record["affected_public_ids_json"]))
                if public_ids:
                    placeholders = ",".join("?" for _ in public_ids)
                    conn.execute(
                        """
                        UPDATE threads SET lifecycle_state = 'active'
                        WHERE public_id IN (%s)
                        """ % placeholders,
                        public_ids,
                    )

    def release_delete_impact(self, token_hash: str) -> bool:
        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM thread_delete_impacts WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None or str(row["state"]) != "claimed":
                return False
            public_ids = tuple(json.loads(row["affected_public_ids_json"]))
            next_state = "pending" if str(row["expires_at"]) > now else "expired"
            conn.execute(
                """
                UPDATE thread_delete_impacts
                SET state = ?, claimed_at = NULL,
                    completed_at = CASE WHEN ? = 'expired' THEN ? ELSE NULL END
                WHERE token_hash = ? AND state = 'claimed'
                """,
                (next_state, next_state, now, token_hash),
            )
            if public_ids:
                placeholders = ",".join("?" for _ in public_ids)
                conn.execute(
                    """
                    UPDATE threads SET lifecycle_state = 'active'
                    WHERE public_id IN (%s)
                    """ % placeholders,
                    public_ids,
                )
            return next_state == "pending"

    def list_claimed_delete_impacts(self) -> List[DeleteImpactRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM thread_delete_impacts
            WHERE state = 'claimed' ORDER BY claimed_at ASC, token_hash ASC
            """
        ).fetchall()
        return [self._delete_impact_from_row(row) for row in rows]

    @staticmethod
    def _delete_impact_from_row(row: sqlite3.Row) -> DeleteImpactRecord:
        return DeleteImpactRecord(
            token_hash=str(row["token_hash"]),
            device_id=str(row["device_id"]),
            root_thread_public_id=str(row["root_thread_public_id"]),
            root_raw_id=str(row["root_raw_id"]),
            affected_raw_ids=tuple(json.loads(row["affected_raw_ids_json"])),
            affected_public_ids=tuple(json.loads(row["affected_public_ids_json"])),
            expires_at=str(row["expires_at"]),
            state=str(row["state"]),
        )

    def update_thread(
        self,
        thread_id: str,
        title: Optional[str] = None,
        preview: Optional[str] = None,
        status: Optional[str] = None,
        archived: Optional[bool] = None,
        last_turn_id: Optional[str] = None,
    ) -> Optional[ThreadDTO]:
        changes: List[str] = ["updated_at = ?"]
        values: List[Any] = [utc_now()]
        if title is not None:
            changes.append("title = ?")
            values.append(title)
        if preview is not None:
            changes.append("preview = ?")
            values.append(preview)
        if archived is not None:
            changes.append("archived = ?")
            values.append(int(archived))
            changes.append("status = ?")
            values.append(
                ThreadStatus.ARCHIVED.value if archived else ThreadStatus.IDLE.value
            )
        elif status is not None:
            changes.append("status = ?")
            values.append(status)
        if last_turn_id is not None:
            changes.append("last_turn_id = ?")
            values.append(last_turn_id)
        values.append(thread_id)
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=True)
            cursor = conn.execute(
                "UPDATE threads SET %s WHERE public_id = ?" % ", ".join(changes),
                values,
            )
        if cursor.rowcount == 0:
            return None
        return self.thread_by_public_id(thread_id)

    def apply_thread_turn_events(
        self,
        device_id: str,
        thread_id: str,
        events: List[Dict[str, Any]],
        thread_status: Optional[str] = None,
        thread_title: Optional[str] = None,
        thread_archived: Optional[bool] = None,
        last_turn_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_status: Optional[str] = None,
        delete_thread: bool = False,
    ) -> Optional[ThreadDTO]:
        """Commit public state and all events describing it in one transaction."""

        now = utc_now()
        with self._transaction() as conn:
            try:
                self._require_thread_mutable(conn, thread_id, allow_archived=True)
            except ThreadLifecycleBlockedError:
                return None
            if turn_id is not None and turn_status is not None:
                delivery_state = (
                    "accepted"
                    if turn_status
                    in {"inProgress", "running", "waitingApproval", "waitingUser"}
                    else "terminal"
                )
                conn.execute(
                    """
                    UPDATE turns SET status = ?, delivery_state = ?, updated_at = ?
                    WHERE public_id = ?
                    """,
                    (turn_status, delivery_state, now, turn_id),
                )
                if delivery_state == "terminal":
                    self._queue_asset_rows_in_transaction(
                        conn,
                        "turnTerminal",
                        "turn_public_id = ?",
                        (turn_id,),
                    )
            changes = ["updated_at = ?"]
            values: List[Any] = [now]
            if thread_title is not None:
                changes.append("title = ?")
                values.append(thread_title)
            if thread_archived is not None:
                changes.extend(["archived = ?", "status = ?"])
                values.extend(
                    [
                        int(thread_archived),
                        (
                            ThreadStatus.ARCHIVED.value
                            if thread_archived
                            else ThreadStatus.IDLE.value
                        ),
                    ]
                )
            elif thread_status is not None:
                changes.append("status = ?")
                values.append(thread_status)
            if last_turn_id is not None:
                changes.append("last_turn_id = ?")
                values.append(last_turn_id)
            values.append(thread_id)
            conn.execute(
                "UPDATE threads SET %s WHERE public_id = ?" % ", ".join(changes),
                values,
            )
            for event in events:
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    str(event["type"]),
                    event.get("payload") or {},
                    thread_id,
                    event.get("turn_id", turn_id),
                    event.get("item_id"),
                )
            if delete_thread:
                self._queue_asset_rows_in_transaction(
                    conn,
                    "threadDeleted",
                    "thread_public_id = ?",
                    (thread_id,),
                )
                conn.execute("DELETE FROM threads WHERE public_id = ?", (thread_id,))
                return None
            row = conn.execute(
                "SELECT * FROM threads WHERE public_id = ?", (thread_id,)
            ).fetchone()
            return None if row is None else self._thread_from_row(row)

    def delete_thread_mapping(self, thread_id: str) -> bool:
        with self._transaction() as conn:
            self._queue_asset_rows_in_transaction(
                conn,
                "threadDeleted",
                "thread_public_id = ?",
                (thread_id,),
            )
            cursor = conn.execute(
                "DELETE FROM threads WHERE public_id = ?", (thread_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _image_asset_from_row(row: sqlite3.Row) -> ImageAssetRecord:
        return ImageAssetRecord(
            asset_ref=str(row["asset_ref"]),
            device_id=str(row["device_id"]),
            thread_public_id=str(row["thread_public_id"]),
            client_message_id=str(row["client_message_id"]),
            client_asset_id=str(row["client_asset_id"]),
            turn_public_id=(
                None if row["turn_public_id"] is None else str(row["turn_public_id"])
            ),
            storage_name=str(row["storage_name"]),
            media_type=str(row["media_type"]),
            width=int(row["width"]),
            height=int(row["height"]),
            byte_count=int(row["byte_count"]),
            source_sha256=str(row["source_sha256"]),
            normalized_sha256=str(row["normalized_sha256"]),
            file_device=int(row["file_device"]),
            file_inode=int(row["file_inode"]),
            state=str(row["state"]),
            expires_at=str(row["expires_at"]),
            hard_expires_at=str(row["hard_expires_at"]),
            created_at=str(row["created_at"]),
        )

    def register_image_asset(
        self,
        *,
        asset_ref: str,
        device_id: str,
        thread_id: str,
        client_message_id: str,
        client_asset_id: str,
        storage_name: str,
        media_type: str,
        width: int,
        height: int,
        byte_count: int,
        source_sha256: str,
        normalized_sha256: str,
        file_device: int,
        file_inode: int,
        expires_at: str,
        hard_expires_at: str,
        max_assets: int = 32,
        max_bytes: int = 128 * 1024 * 1024,
    ) -> Tuple[ImageAssetRecord, bool]:
        now = utc_now()
        with self._transaction() as conn:
            thread = conn.execute(
                "SELECT archived, lifecycle_state FROM threads WHERE public_id = ?",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            if str(thread["lifecycle_state"]) != "active":
                raise ThreadLifecycleBlockedError("pendingDelete")
            if bool(thread["archived"]):
                raise ThreadLifecycleBlockedError("archived")
            existing = conn.execute(
                """
                SELECT * FROM image_assets
                WHERE device_id = ? AND thread_public_id = ?
                    AND client_message_id = ? AND client_asset_id = ?
                """,
                (device_id, thread_id, client_message_id, client_asset_id),
            ).fetchone()
            if existing is not None:
                if str(existing["source_sha256"]) != source_sha256:
                    raise AssetBindingError(client_asset_id)
                return self._image_asset_from_row(existing), False
            quota = conn.execute(
                """
                SELECT COUNT(*) AS asset_count, COALESCE(SUM(byte_count), 0) AS total_bytes
                FROM image_assets WHERE device_id = ?
                """,
                (device_id,),
            ).fetchone()
            if int(quota["asset_count"]) >= max_assets or (
                int(quota["total_bytes"]) + byte_count > max_bytes
            ):
                raise AssetQuotaError(device_id)
            conn.execute(
                """
                INSERT INTO image_assets(
                    asset_ref, device_id, thread_public_id, client_message_id,
                    client_asset_id, turn_public_id, storage_name, media_type,
                    width, height, byte_count, source_sha256, normalized_sha256,
                    file_device, file_inode, state, expires_at, hard_expires_at,
                    created_at, claimed_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'ready', ?, ?, ?, NULL)
                """,
                (
                    asset_ref, device_id, thread_id, client_message_id,
                    client_asset_id, storage_name, media_type, width, height,
                    byte_count, source_sha256, normalized_sha256, file_device,
                    file_inode, expires_at, hard_expires_at, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM image_assets WHERE asset_ref = ?", (asset_ref,)
            ).fetchone()
            if row is None:
                raise RuntimeError("image asset was not created")
            return self._image_asset_from_row(row), True

    def image_assets_for_refs(self, refs: Iterable[str]) -> Tuple[ImageAssetRecord, ...]:
        values = tuple(refs)
        if not values:
            return ()
        placeholders = ",".join("?" for _ in values)
        rows = self._conn.execute(
            "SELECT * FROM image_assets WHERE asset_ref IN (%s)" % placeholders,
            values,
        ).fetchall()
        by_ref = {str(row["asset_ref"]): self._image_asset_from_row(row) for row in rows}
        return tuple(by_ref[value] for value in values if value in by_ref)

    @staticmethod
    def _claimable_assets_in_transaction(
        conn: sqlite3.Connection,
        device_id: str,
        thread_id: str,
        client_message_id: str,
        refs: Tuple[str, ...],
        now: str,
    ) -> Tuple[sqlite3.Row, ...]:
        if not refs:
            return ()
        if len(refs) > 4 or len(refs) != len(set(refs)):
            raise AssetBindingError("invalid asset set")
        placeholders = ",".join("?" for _ in refs)
        rows = conn.execute(
            "SELECT * FROM image_assets WHERE asset_ref IN (%s)" % placeholders,
            refs,
        ).fetchall()
        by_ref = {str(row["asset_ref"]): row for row in rows}
        if len(by_ref) != len(refs):
            raise AssetUnavailableError("asset was not found")
        ordered = tuple(by_ref[value] for value in refs)
        for row in ordered:
            if (
                str(row["device_id"]) != device_id
                or str(row["thread_public_id"]) != thread_id
                or str(row["client_message_id"]) != client_message_id
                or str(row["state"]) != "ready"
                or str(row["expires_at"]) <= now
            ):
                raise AssetUnavailableError("asset binding is unavailable")
        return ordered

    @staticmethod
    def _queue_asset_rows_in_transaction(
        conn: sqlite3.Connection,
        reason: str,
        predicate: str,
        values: Tuple[Any, ...],
    ) -> None:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO image_asset_cleanup_queue(storage_name, reason, queued_at)
            SELECT storage_name, ?, ? FROM image_assets WHERE %s
            ON CONFLICT(storage_name) DO NOTHING
            """ % predicate,
            (reason, now, *values),
        )
        conn.execute("DELETE FROM image_assets WHERE %s" % predicate, values)

    def queue_expired_image_assets(self, now: Optional[str] = None) -> int:
        cutoff = now or utc_now()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT storage_name, device_id, thread_public_id,
                    client_message_id, turn_public_id, state, hard_expires_at
                FROM image_assets
                WHERE (state = 'ready' AND expires_at <= ?)
                   OR hard_expires_at <= ?
                """,
                (cutoff, cutoff),
            ).fetchall()
            for row in rows:
                if str(row["state"]) != "claimed" or str(row["hard_expires_at"]) > cutoff:
                    continue
                self._append_event_in_transaction(
                    conn,
                    str(row["device_id"]),
                    "asset.unavailable",
                    {
                        "clientMessageId": str(row["client_message_id"]),
                        "reason": "expired",
                    },
                    str(row["thread_public_id"]),
                    (
                        None
                        if row["turn_public_id"] is None
                        else str(row["turn_public_id"])
                    ),
                )
            self._queue_asset_rows_in_transaction(
                conn,
                "expired",
                "(state = 'ready' AND expires_at <= ?) OR hard_expires_at <= ?",
                (cutoff, cutoff),
            )
            return len(rows)

    def image_cleanup_queue(self) -> Tuple[str, ...]:
        return tuple(
            str(row["storage_name"])
            for row in self._conn.execute(
                "SELECT storage_name FROM image_asset_cleanup_queue ORDER BY queued_at"
            ).fetchall()
        )

    def complete_image_cleanup(self, storage_name: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM image_asset_cleanup_queue WHERE storage_name = ?",
                (storage_name,),
            )

    def referenced_image_storage_names(self) -> Tuple[str, ...]:
        return tuple(
            str(row["storage_name"])
            for row in self._conn.execute("SELECT storage_name FROM image_assets").fetchall()
        )

    def reserve_turn(
        self,
        device_id: str,
        thread_id: str,
        client_message_id: str,
        request_fingerprint: Optional[str] = None,
        asset_refs: Iterable[str] = (),
    ) -> TurnReservation:
        now = utc_now()
        requested_refs = tuple(asset_refs)
        with self._transaction() as conn:
            thread = conn.execute(
                """
                SELECT status, needs_resync, archived, lifecycle_state
                FROM threads WHERE public_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise KeyError(thread_id)
            if str(thread["lifecycle_state"]) != "active":
                raise ThreadLifecycleBlockedError("pendingDelete")
            if bool(thread["archived"]):
                raise ThreadLifecycleBlockedError("archived")
            existing = conn.execute(
                """
                SELECT * FROM turns
                WHERE device_id = ? AND thread_public_id = ? AND client_message_id = ?
                """,
                (device_id, thread_id, client_message_id),
            ).fetchone()
            if existing is not None:
                if existing["delivery_state"] == "ambiguous" or existing["status"] == "ambiguous":
                    raise TurnAmbiguousError(existing["public_id"])
                existing_fingerprint = existing["request_fingerprint"]
                if (
                    request_fingerprint is not None
                    and (
                        existing_fingerprint is None
                        or str(existing_fingerprint) != request_fingerprint
                    )
                ):
                    raise TurnIdempotencyConflictError(client_message_id)
                if (
                    existing["status"] == "retryable"
                    and existing["delivery_state"] == "notAccepted"
                ):
                    if bool(thread["needs_resync"]):
                        raise TurnAmbiguousError(existing["public_id"])
                    try:
                        conn.execute(
                            """
                            UPDATE turns
                            SET raw_id = NULL, status = 'pending',
                                delivery_state = 'reserved',
                                attempt_count = attempt_count + 1,
                                updated_at = ?
                            WHERE public_id = ? AND status = 'retryable'
                                AND delivery_state = 'notAccepted'
                            """,
                            (now, existing["public_id"]),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ThreadBusyError(thread_id) from exc
                    retried = conn.execute(
                        "SELECT * FROM turns WHERE public_id = ?",
                        (existing["public_id"],),
                    ).fetchone()
                    if retried is None:
                        raise RuntimeError("retryable turn disappeared")
                    return self._turn_reservation(retried, created=True)
                return self._turn_reservation(existing, created=False)

            if bool(thread["needs_resync"]):
                raise TurnAmbiguousError(thread_id)
            if thread["status"] in {
                ThreadStatus.RUNNING.value,
                ThreadStatus.WAITING_APPROVAL.value,
                ThreadStatus.WAITING_USER.value,
            }:
                raise ThreadBusyError(thread_id)

            turn_id = public_id("turn")
            asset_rows = self._claimable_assets_in_transaction(
                conn,
                device_id,
                thread_id,
                client_message_id,
                requested_refs,
                now,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO turns(
                        public_id, raw_id, device_id, thread_public_id,
                        client_message_id, status, delivery_state,
                        attempt_count, request_fingerprint, created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, ?, 'pending', 'reserved', 1, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        device_id,
                        thread_id,
                        client_message_id,
                        request_fingerprint,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The idempotency lookup above handles retries of the same
                # clientMessageId. The partial unique index makes a distinct
                # concurrent reservation for this thread fail atomically.
                raise ThreadBusyError(thread_id) from exc
            if asset_rows:
                placeholders = ",".join("?" for _ in asset_rows)
                conn.execute(
                    """
                    UPDATE image_assets
                    SET state = 'claimed', turn_public_id = ?, claimed_at = ?
                    WHERE asset_ref IN (%s)
                    """ % placeholders,
                    (turn_id, now, *(str(row["asset_ref"]) for row in asset_rows)),
                )
            row = conn.execute(
                "SELECT * FROM turns WHERE public_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("turn reservation was not created")
        return self._turn_reservation(row, created=True)

    def preflight_turn_start(
        self,
        device_id: str,
        thread_id: str,
        client_message_id: str,
    ) -> None:
        """Lock-free fast rejection before the async lifecycle mutex.

        The mutating reserve_turn call repeats every check transactionally;
        this preflight only prevents a distinct sender from waiting behind a
        long Codex RPC that has already reserved the Thread.
        """

        row = self._conn.execute(
            """
            SELECT archived, lifecycle_state FROM threads WHERE public_id = ?
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            raise KeyError(thread_id)
        if str(row["lifecycle_state"]) != "active":
            raise ThreadLifecycleBlockedError("pendingDelete")
        if bool(row["archived"]):
            raise ThreadLifecycleBlockedError("archived")
        active = self._conn.execute(
            """
            SELECT device_id, client_message_id FROM turns
            WHERE thread_public_id = ? AND status IN (
                'pending', 'dispatching', 'inProgress', 'running',
                'waitingApproval', 'waitingUser'
            ) LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if active is not None and not (
            str(active["device_id"]) == device_id
            and str(active["client_message_id"]) == client_message_id
        ):
            raise ThreadBusyError(thread_id)

    def complete_turn_reservation(
        self,
        turn_id: str,
        raw_id: str,
        status: str = "inProgress",
        device_id: Optional[str] = None,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> TurnReservation:
        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE public_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            existing_raw_id = (
                None if row["raw_id"] is None else str(row["raw_id"])
            )
            if existing_raw_id is not None:
                if existing_raw_id != raw_id:
                    raise TurnAmbiguousError(turn_id)
                if str(row["delivery_state"]) in {"accepted", "terminal"}:
                    # A lifecycle notification may commit acceptance (and even
                    # completion) before the turn/start response is delivered.
                    # The response confirms the same identity but must not emit
                    # a second start event or regress a terminal Turn.
                    return self._turn_reservation(row, created=False)
            if (
                existing_raw_id is not None
                or str(row["status"]) != "dispatching"
                or str(row["delivery_state"]) != "dispatching"
            ):
                raise TurnAmbiguousError(turn_id)
            try:
                cursor = conn.execute(
                    """
                    UPDATE turns
                    SET raw_id = ?, status = ?, delivery_state = 'accepted',
                        updated_at = ?
                    WHERE public_id = ? AND raw_id IS NULL
                        AND status = 'dispatching'
                        AND delivery_state = 'dispatching'
                    """,
                    (raw_id, status, now, turn_id),
                )
            except sqlite3.IntegrityError as exc:
                raise TurnAmbiguousError(turn_id) from exc
            if cursor.rowcount != 1:
                raise TurnAmbiguousError(turn_id)
            row = conn.execute(
                "SELECT * FROM turns WHERE public_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if device_id is not None:
                user_message = conn.execute(
                    """
                    SELECT message_id, payload_json FROM message_snapshots
                    WHERE thread_public_id = ? AND role = 'user'
                        AND client_message_id = ?
                    """,
                    (row["thread_public_id"], row["client_message_id"]),
                ).fetchone()
                if user_message is not None:
                    user_payload = json.loads(user_message["payload_json"])
                    user_payload["state"] = "completed"
                    user_payload["updatedAt"] = now
                    conn.execute(
                        """
                        UPDATE message_snapshots
                        SET payload_json = ?, updated_at = ?
                        WHERE message_id = ?
                        """,
                        (
                            json.dumps(
                                user_payload,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            now,
                            user_message["message_id"],
                        ),
                    )
                conn.execute(
                    """
                    UPDATE threads SET status = ?, last_turn_id = ?, updated_at = ?
                    WHERE public_id = ?
                    """,
                    (
                        ThreadStatus.RUNNING.value,
                        turn_id,
                        now,
                        row["thread_public_id"],
                    ),
                )
                for event in events or []:
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        str(event["type"]),
                        event.get("payload") or {},
                        str(row["thread_public_id"]),
                        turn_id,
                        event.get("item_id"),
                    )
        return self._turn_reservation(row, created=False)

    def bind_pending_turn(
        self,
        thread_id: str,
        raw_id: str,
        client_message_id: Optional[str] = None,
        *,
        device_id: Optional[str] = None,
        emit_started_event: bool = False,
    ) -> Optional[str]:
        """Bind an early turn event to the one outstanding local reservation.

        app-server normally responds to turn/start before turn/started, but the
        gateway does not rely on cross-channel scheduling for identity safety.
        """
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=False)
            existing = conn.execute(
                """
                SELECT public_id, thread_public_id FROM turns WHERE raw_id = ?
                """,
                (raw_id,),
            ).fetchone()
            if existing is not None:
                return (
                    str(existing["public_id"])
                    if str(existing["thread_public_id"]) == thread_id
                    else None
                )
            pending = conn.execute(
                """
                SELECT public_id, client_message_id FROM turns
                WHERE thread_public_id = ? AND raw_id IS NULL
                    AND (
                        (
                            status = 'dispatching' AND delivery_state = 'dispatching'
                            AND (? IS NULL OR client_message_id = ?)
                        )
                        OR (
                            status = 'ambiguous' AND delivery_state = 'ambiguous'
                            AND ? IS NOT NULL AND client_message_id = ?
                        )
                    )
                ORDER BY created_at ASC LIMIT 1
                """,
                (
                    thread_id,
                    client_message_id,
                    client_message_id,
                    client_message_id,
                    client_message_id,
                ),
            ).fetchone()
            if pending is None:
                return None
            now = utc_now()
            cursor = conn.execute(
                """
                UPDATE turns
                SET raw_id = ?, status = 'inProgress',
                    delivery_state = 'accepted', updated_at = ?
                WHERE public_id = ? AND raw_id IS NULL
                    AND (
                        (
                            status = 'dispatching' AND delivery_state = 'dispatching'
                            AND (? IS NULL OR client_message_id = ?)
                        )
                        OR (
                            status = 'ambiguous' AND delivery_state = 'ambiguous'
                            AND ? IS NOT NULL AND client_message_id = ?
                        )
                    )
                """,
                (
                    raw_id,
                    now,
                    pending["public_id"],
                    client_message_id,
                    client_message_id,
                    client_message_id,
                    client_message_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            user_message = conn.execute(
                """
                SELECT message_id, payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, pending["client_message_id"]),
            ).fetchone()
            if user_message is not None:
                payload = json.loads(user_message["payload_json"])
                payload["state"] = "completed"
                payload["updatedAt"] = now
                conn.execute(
                    "UPDATE message_snapshots SET payload_json = ?, updated_at = ? WHERE message_id = ?",
                    (
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                        now,
                        user_message["message_id"],
                    ),
                )
            remaining_ambiguous = conn.execute(
                """
                SELECT 1 FROM turns
                WHERE thread_public_id = ? AND public_id != ?
                    AND (status = 'ambiguous' OR delivery_state = 'ambiguous')
                LIMIT 1
                """,
                (thread_id, pending["public_id"]),
            ).fetchone()
            conn.execute(
                """
                UPDATE threads
                SET status = ?, needs_resync = ?, last_turn_id = ?, updated_at = ?
                WHERE public_id = ?
                """,
                (
                    ThreadStatus.RUNNING.value,
                    int(remaining_ambiguous is not None),
                    pending["public_id"],
                    now,
                    thread_id,
                ),
            )
            if emit_started_event:
                if device_id is None:
                    raise ValueError("device_id is required for a start event")
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "turn.started",
                    {},
                    thread_id,
                    str(pending["public_id"]),
                )
            return str(pending["public_id"])

    def mark_turn_dispatching(self, turn_id: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE turns
                SET status = 'dispatching', delivery_state = 'dispatching',
                    updated_at = ?
                WHERE public_id = ? AND status = 'pending'
                    AND delivery_state = 'reserved'
                """,
                (utc_now(), turn_id),
            )
        return cursor.rowcount == 1

    def mark_turn_not_accepted(self, turn_id: str) -> None:
        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT thread_public_id, client_message_id FROM turns WHERE public_id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                UPDATE turns
                SET status = 'retryable', delivery_state = 'notAccepted',
                    raw_id = NULL, updated_at = ?
                WHERE public_id = ?
                """,
                (now, turn_id),
            )
            conn.execute(
                """
                UPDATE threads
                SET status = ?, needs_resync = 0, updated_at = ?
                WHERE public_id = ?
                """,
                (ThreadStatus.IDLE.value, now, row["thread_public_id"]),
            )

    def mark_turn_ambiguous(
        self,
        turn_id: str,
        device_id: Optional[str] = None,
        only_if_dispatching: bool = False,
    ) -> Optional[str]:
        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT thread_public_id, client_message_id FROM turns WHERE public_id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return None
            thread_id = str(row["thread_public_id"])
            if only_if_dispatching:
                cursor = conn.execute(
                    """
                    UPDATE turns
                    SET status = 'ambiguous', delivery_state = 'ambiguous',
                        updated_at = ?
                    WHERE public_id = ? AND raw_id IS NULL
                        AND status = 'dispatching'
                        AND delivery_state = 'dispatching'
                    """,
                    (now, turn_id),
                )
                if cursor.rowcount != 1:
                    return None
            else:
                conn.execute(
                    """
                    UPDATE turns
                    SET status = 'ambiguous', delivery_state = 'ambiguous',
                        updated_at = ?
                    WHERE public_id = ?
                    """,
                    (now, turn_id),
                )
            conn.execute(
                """
                UPDATE threads
                SET status = ?, needs_resync = 1, last_turn_id = ?, updated_at = ?
                WHERE public_id = ?
                """,
                (ThreadStatus.FAILED.value, turn_id, now, thread_id),
            )
            user_message = conn.execute(
                """
                SELECT message_id, payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, row["client_message_id"]),
            ).fetchone()
            if user_message is not None:
                payload = json.loads(user_message["payload_json"])
                payload["state"] = "failed"
                payload["updatedAt"] = now
                conn.execute(
                    "UPDATE message_snapshots SET payload_json = ?, updated_at = ? WHERE message_id = ?",
                    (
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                        now,
                        user_message["message_id"],
                    ),
                )
            if device_id is not None:
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "error",
                    {
                        "code": "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
                        "message": "Codex may have accepted this turn; resync is required",
                        "retryable": False,
                    },
                    thread_id,
                    turn_id,
                )
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "sync.required",
                    {"reason": "turnDeliveryAmbiguous"},
                    thread_id,
                    turn_id,
                )
            return thread_id

    def mark_accepted_turn_outcome_unknown(
        self,
        turn_id: str,
        device_id: Optional[str],
        terminal_message: Optional[Dict[str, Any]] = None,
        terminal_message_sort_order: int = 0,
    ) -> bool:
        """Terminate an accepted orphan without ever making it retryable."""
        now = utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT thread_public_id, client_message_id FROM turns
                WHERE public_id = ? AND delivery_state = 'accepted'
                    AND status IN (
                        'inProgress', 'running', 'waitingApproval', 'waitingUser'
                    )
                """,
                (turn_id,),
            ).fetchone()
            if row is None:
                return False
            thread_id = str(row["thread_public_id"])
            cursor = conn.execute(
                """
                UPDATE turns
                SET status = 'failed', delivery_state = 'terminal',
                    recovery_reason = 'operationOutcomeUnknown', updated_at = ?
                WHERE public_id = ? AND delivery_state = 'accepted'
                    AND status IN (
                        'inProgress', 'running', 'waitingApproval', 'waitingUser'
                    )
                """,
                (now, turn_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                UPDATE threads
                SET status = ?, needs_resync = 0, last_turn_id = ?, updated_at = ?
                WHERE public_id = ?
                """,
                (ThreadStatus.IDLE.value, turn_id, now, thread_id),
            )
            user_message = conn.execute(
                """
                SELECT message_id, payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, row["client_message_id"]),
            ).fetchone()
            if user_message is not None:
                payload = json.loads(user_message["payload_json"])
                payload["state"] = "failed"
                payload["updatedAt"] = now
                conn.execute(
                    """
                    UPDATE message_snapshots
                    SET payload_json = ?, updated_at = ? WHERE message_id = ?
                    """,
                    (
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                        now,
                        user_message["message_id"],
                    ),
                )
            pending_approvals = conn.execute(
                """
                SELECT public_id, item_public_id FROM approvals
                WHERE turn_public_id = ? AND state = 'pending'
                ORDER BY created_at ASC, public_id ASC
                """,
                (turn_id,),
            ).fetchall()
            conn.execute(
                """
                UPDATE approvals
                SET state = 'expired', decision = 'reject', resolved_at = ?
                WHERE turn_public_id = ? AND state = 'pending'
                """,
                (now, turn_id),
            )
            self._queue_asset_rows_in_transaction(
                conn,
                "turnTerminal",
                "turn_public_id = ?",
                (turn_id,),
            )
            if device_id is not None:
                for approval in pending_approvals:
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        "approval.expired",
                        {
                            "approvalId": str(approval["public_id"]),
                            "state": "expired",
                            "reason": "turnOutcomeUnknown",
                        },
                        thread_id,
                        turn_id,
                        str(approval["item_public_id"]),
                    )
                if terminal_message is not None:
                    message_id = str(terminal_message["messageId"])
                    self._delete_other_assistant_snapshots_in_transaction(
                        conn,
                        turn_id,
                        message_id,
                    )
                    self._save_message_snapshot_in_transaction(
                        conn,
                        message_id,
                        thread_id,
                        turn_id,
                        int(terminal_message["revision"]),
                        "assistant",
                        None,
                        terminal_message,
                        str(terminal_message["createdAt"]),
                        terminal_message_sort_order,
                    )
                    conn.execute(
                        "UPDATE threads SET preview = ?, updated_at = ? WHERE public_id = ?",
                        (
                            str(terminal_message.get("fallback", {}).get("text") or "")[-240:],
                            now,
                            thread_id,
                        ),
                    )
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        "message.snapshot",
                        terminal_message,
                        thread_id,
                        turn_id,
                        str(terminal_message["sourceItemId"]),
                    )
                error = {
                    "code": "OPERATION_OUTCOME_UNKNOWN",
                    "message": "Codex stopped before the accepted turn outcome was confirmed",
                    "retryable": False,
                }
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "turn.failed",
                    {"status": "failed", "error": error},
                    thread_id,
                    turn_id,
                )
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    "error",
                    error,
                    thread_id,
                    turn_id,
                )
            return True

    def reconcile_ambiguous_turn_from_history(
        self,
        thread_id: str,
        raw_id: str,
        client_message_id: str,
        status: str,
    ) -> Optional[str]:
        """Bind authoritative Codex history to one exact ambiguous client turn."""

        now = utc_now()
        terminal = status in {
            "completed", "failed", "interrupted", "cancelled", "canceled"
        }
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=False)
            existing = conn.execute(
                "SELECT public_id FROM turns WHERE raw_id = ?", (raw_id,)
            ).fetchone()
            if existing is not None:
                return str(existing["public_id"])
            candidate = conn.execute(
                """
                SELECT public_id FROM turns
                WHERE thread_public_id = ? AND client_message_id = ?
                    AND raw_id IS NULL AND status = 'ambiguous'
                    AND delivery_state = 'ambiguous'
                """,
                (thread_id, client_message_id),
            ).fetchone()
            if candidate is None:
                return None
            cursor = conn.execute(
                """
                UPDATE turns SET raw_id = ?, status = ?, delivery_state = ?, updated_at = ?
                WHERE public_id = ? AND raw_id IS NULL
                    AND status = 'ambiguous' AND delivery_state = 'ambiguous'
                """,
                (
                    raw_id,
                    status,
                    "terminal" if terminal else "accepted",
                    now,
                    candidate["public_id"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            if terminal:
                self._queue_asset_rows_in_transaction(
                    conn,
                    "turnTerminal",
                    "turn_public_id = ?",
                    (candidate["public_id"],),
                )
            user_message = conn.execute(
                """
                SELECT message_id, payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, client_message_id),
            ).fetchone()
            if user_message is not None:
                payload = json.loads(user_message["payload_json"])
                payload["state"] = "completed"
                payload["updatedAt"] = now
                conn.execute(
                    "UPDATE message_snapshots SET payload_json = ?, updated_at = ? WHERE message_id = ?",
                    (
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                        now,
                        user_message["message_id"],
                    ),
                )
            return str(candidate["public_id"])

    def thread_has_ambiguous_turns(self, thread_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM turns
            WHERE thread_public_id = ?
                AND (status = 'ambiguous' OR delivery_state = 'ambiguous')
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        return row is not None

    def clear_thread_resync(self, thread_id: str, status: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE threads SET needs_resync = 0, status = ?, updated_at = ?
                WHERE public_id = ?
                """,
                (status, utc_now(), thread_id),
            )

    def list_active_turns(self) -> List[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM turns
            WHERE status IN (
                'pending', 'dispatching', 'inProgress',
                'running', 'waitingApproval', 'waitingUser'
            )
            ORDER BY thread_public_id ASC, created_at ASC, public_id ASC
            """
        ).fetchall()

    def fail_turn_reservation(self, turn_id: str) -> None:
        self.mark_turn_ambiguous(turn_id)

    def update_turn_status(self, turn_id: str, status: str) -> None:
        delivery_state = (
            "accepted"
            if status in {"inProgress", "running", "waitingApproval", "waitingUser"}
            else "terminal"
        )
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE turns SET status = ?, delivery_state = ?, updated_at = ?
                WHERE public_id = ?
                """,
                (status, delivery_state, utc_now(), turn_id),
            )
            if delivery_state == "terminal":
                self._queue_asset_rows_in_transaction(
                    conn,
                    "turnTerminal",
                    "turn_public_id = ?",
                    (turn_id,),
                )

    def turn_by_public_id(self, turn_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM turns WHERE public_id = ?", (turn_id,)
        ).fetchone()

    def turn_by_client_message(
        self,
        device_id: str,
        thread_id: str,
        client_message_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM turns
            WHERE device_id = ? AND thread_public_id = ? AND client_message_id = ?
            """,
            (device_id, thread_id, client_message_id),
        ).fetchone()

    def turn_public_id(self, raw_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT public_id FROM turns WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        return None if row is None else str(row["public_id"])

    def turn_raw_id(self, public_id_value: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT raw_id FROM turns WHERE public_id = ?", (public_id_value,)
        ).fetchone()
        if row is None or row["raw_id"] is None:
            return None
        return str(row["raw_id"])

    def map_raw_id(
        self,
        kind: str,
        raw_id: str,
        prefix: str,
        thread_public_id: Optional[str] = None,
    ) -> str:
        mapped = public_id(prefix)
        with self._transaction() as conn:
            if thread_public_id is not None:
                self._require_thread_mutable(
                    conn, thread_public_id, allow_archived=True
                )
            existing = conn.execute(
                "SELECT public_id FROM id_mappings WHERE kind = ? AND raw_id = ?",
                (kind, raw_id),
            ).fetchone()
            if existing is not None:
                return str(existing["public_id"])
            try:
                conn.execute(
                    """
                    INSERT INTO id_mappings(kind, raw_id, public_id, thread_public_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (kind, raw_id, mapped, thread_public_id, utc_now()),
                )
            except sqlite3.IntegrityError:
                pass
        row = self._conn.execute(
            "SELECT public_id FROM id_mappings WHERE kind = ? AND raw_id = ?",
            (kind, raw_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("id mapping was not created")
        return str(row["public_id"])

    def mapped_public_id(
        self,
        kind: str,
        raw_id: str,
        thread_public_id: Optional[str] = None,
    ) -> Optional[str]:
        """Return an existing history mapping without creating new identity."""

        row = self._conn.execute(
            """
            SELECT public_id, thread_public_id
            FROM id_mappings WHERE kind = ? AND raw_id = ?
            """,
            (kind, raw_id),
        ).fetchone()
        if row is None:
            return None
        if (
            thread_public_id is not None
            and row["thread_public_id"] is not None
            and str(row["thread_public_id"]) != thread_public_id
        ):
            return None
        return str(row["public_id"])

    def create_approval(
        self,
        approval_id: str,
        raw_request_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        kind: str,
        summary: str,
        reason: Optional[str],
        decisions: List[str],
        action_token: str,
        expires_at: str,
        device_id: Optional[str] = None,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[sqlite3.Row, bool]:
        now = utc_now()
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=True)
            existing = conn.execute(
                "SELECT * FROM approvals WHERE raw_request_id = ?", (raw_request_id,)
            ).fetchone()
            if existing is not None:
                return existing, False
            conn.execute(
                """
                INSERT INTO approvals(
                    public_id, raw_request_id, thread_public_id, turn_public_id,
                    item_public_id, kind, summary, reason, decisions_json, state,
                    action_token_hash, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    approval_id,
                    raw_request_id,
                    thread_id,
                    turn_id,
                    item_id,
                    kind,
                    summary,
                    reason,
                    json.dumps(decisions, separators=(",", ":")),
                    secret_hash(action_token),
                    expires_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE public_id = ?", (approval_id,)
            ).fetchone()
            if device_id is not None:
                conn.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE public_id = ?",
                    (ThreadStatus.WAITING_APPROVAL.value, now, thread_id),
                )
                for event in events or []:
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        str(event["type"]),
                        event.get("payload") or {},
                        thread_id,
                        turn_id,
                        item_id,
                    )
        if row is None:
            raise RuntimeError("approval was not created")
        return row, True

    def list_approvals(self) -> List[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals ORDER BY created_at, public_id"
        ).fetchall()

    def repair_approval_public_data(
        self,
        repairs: List[Dict[str, Any]],
    ) -> Tuple[int, int]:
        by_id = {str(repair["approval_id"]): repair for repair in repairs}
        repaired: Dict[str, Dict[str, Any]] = {}
        removed_events = 0
        with self._transaction() as conn:
            for approval_id, repair in by_id.items():
                action_token = repair.get("action_token")
                action_token_hash = secret_hash(
                    str(action_token)
                    if action_token is not None
                    else secrets.token_urlsafe(48)
                )
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET summary = ?, reason = ?, action_token_hash = ?
                    WHERE public_id = ?
                    """,
                    (
                        str(repair["summary"]),
                        str(repair["reason"]),
                        action_token_hash,
                        approval_id,
                    ),
                )
                if cursor.rowcount == 1:
                    repaired[approval_id] = repair

            event_rows = conn.execute(
                """
                SELECT stream_id, seq, type, payload_json FROM event_outbox
                WHERE type IN (
                    'approval.requested', 'approval.resolved', 'approval.expired'
                )
                """
            ).fetchall()
            removed_floor_by_stream: Dict[str, int] = {}
            for event_row in event_rows:
                try:
                    existing_payload = json.loads(event_row["payload_json"])
                except (TypeError, ValueError):
                    existing_payload = None
                approval_id = (
                    str(existing_payload.get("approvalId") or "")
                    if isinstance(existing_payload, dict)
                    else ""
                )
                repair = repaired.get(approval_id)
                event_type = str(event_row["type"])
                state = str(repair.get("state") or "") if repair is not None else ""
                replacement: Optional[Dict[str, Any]] = None
                if repair is not None and event_type == "approval.requested":
                    replacement = repair["requested_payload"]
                elif (
                    repair is not None
                    and event_type == "approval.resolved"
                    and state in {"approved", "rejected"}
                ):
                    replacement = repair["payload"]
                elif (
                    repair is not None
                    and event_type == "approval.expired"
                    and state == "expired"
                ):
                    replacement = {"approvalId": approval_id}

                if replacement is not None:
                    conn.execute(
                        """
                        UPDATE event_outbox SET payload_json = ?
                        WHERE stream_id = ? AND seq = ?
                        """,
                        (
                            json.dumps(
                                replacement,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            event_row["stream_id"],
                            event_row["seq"],
                        ),
                    )
                    continue

                conn.execute(
                    "DELETE FROM event_outbox WHERE stream_id = ? AND seq = ?",
                    (event_row["stream_id"], event_row["seq"]),
                )
                removed_events += 1
                stream_id = str(event_row["stream_id"])
                removed_floor_by_stream[stream_id] = max(
                    removed_floor_by_stream.get(stream_id, 0),
                    int(event_row["seq"]),
                )

            for stream_id, replay_floor in removed_floor_by_stream.items():
                conn.execute(
                    """
                    UPDATE streams
                    SET replay_floor_seq = MAX(replay_floor_seq, ?), updated_at = ?
                    WHERE stream_id = ?
                    """,
                    (replay_floor, utc_now(), stream_id),
                )
        return len(repaired), removed_events

    def approval_by_public_id(self, approval_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM approvals WHERE public_id = ?", (approval_id,)
        ).fetchone()

    def resolve_approval_once(
        self,
        approval_id: str,
        action_token: str,
        decision: str,
        now: Optional[str] = None,
        device_id: Optional[str] = None,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[sqlite3.Row], str]:
        current = utc_now() if now is None else now
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE public_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                return None, "notFound"
            if row["state"] != "pending":
                return row, "alreadyResolved"
            turn = conn.execute(
                "SELECT status, delivery_state FROM turns WHERE public_id = ?",
                (row["turn_public_id"],),
            ).fetchone()
            if (
                turn is None
                or str(turn["delivery_state"]) != "accepted"
                or str(turn["status"])
                not in {"inProgress", "running", "waitingApproval", "waitingUser"}
            ):
                conn.execute(
                    """
                    UPDATE approvals
                    SET state = 'expired', decision = 'reject', resolved_at = ?
                    WHERE public_id = ? AND state = 'pending'
                    """,
                    (current, approval_id),
                )
                resolved = conn.execute(
                    "SELECT * FROM approvals WHERE public_id = ?",
                    (approval_id,),
                ).fetchone()
                return resolved, "alreadyResolved"
            if row["expires_at"] <= current:
                return row, "expired"
            if not secrets.compare_digest(
                row["action_token_hash"], secret_hash(action_token)
            ):
                return row, "invalidToken"
            if decision not in json.loads(row["decisions_json"]):
                return row, "invalidDecision"
            final_state = "approved" if decision == "approve" else "rejected"
            conn.execute(
                """
                UPDATE approvals
                SET state = ?, decision = ?, resolved_at = ?
                WHERE public_id = ? AND state = 'pending'
                """,
                (final_state, decision, current, approval_id),
            )
            resolved = conn.execute(
                "SELECT * FROM approvals WHERE public_id = ?", (approval_id,)
            ).fetchone()
            if device_id is not None:
                conn.execute(
                    "UPDATE threads SET status = ?, updated_at = ? WHERE public_id = ?",
                    (
                        ThreadStatus.RUNNING.value,
                        current,
                        row["thread_public_id"],
                    ),
                )
                for event in events or []:
                    self._append_event_in_transaction(
                        conn,
                        device_id,
                        str(event["type"]),
                        event.get("payload") or {},
                        str(row["thread_public_id"]),
                        str(row["turn_public_id"]),
                        str(row["item_public_id"]),
                    )
            return resolved, "resolved"

    def claim_expired_approvals(
        self,
        now: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        current = utc_now() if now is None else now
        with self._transaction() as conn:
            due = conn.execute(
                """
                SELECT public_id FROM approvals
                WHERE state = 'pending' AND expires_at <= ?
                ORDER BY expires_at ASC, public_id ASC
                """,
                (current,),
            ).fetchall()
            claimed: List[sqlite3.Row] = []
            for record in due:
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET state = 'expired', decision = 'reject', resolved_at = ?
                    WHERE public_id = ? AND state = 'pending'
                    """,
                    (current, record["public_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                row = conn.execute(
                    "SELECT * FROM approvals WHERE public_id = ?",
                    (record["public_id"],),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE threads SET status = ?, updated_at = ? WHERE public_id = ?",
                        (
                            ThreadStatus.RUNNING.value,
                            current,
                            row["thread_public_id"],
                        ),
                    )
                    if device_id is not None:
                        self._append_event_in_transaction(
                            conn,
                            device_id,
                            "approval.expired",
                            {"approvalId": str(row["public_id"])},
                            str(row["thread_public_id"]),
                            str(row["turn_public_id"]),
                            str(row["item_public_id"]),
                        )
                    claimed.append(row)
            return claimed

    def upsert_projection_item(
        self,
        *,
        message_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        item_type: str,
        status: Optional[str],
        phase: Optional[str],
        text: str,
        duration_ms: Optional[int] = None,
        exit_code: Optional[int] = None,
        change_count: int = 0,
        evidence: Optional[Dict[str, Any]] = None,
        ordinal: Optional[int] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> bool:
        """Persist private fold input and report whether its semantics changed."""

        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=True)
            existing = conn.execute(
                "SELECT * FROM message_projection_items WHERE item_public_id = ?",
                (item_id,),
            ).fetchone()
            if existing is None:
                if ordinal is None:
                    row = conn.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), -1) + 1 AS next_ordinal
                        FROM message_projection_items WHERE turn_public_id = ?
                        """,
                        (turn_id,),
                    ).fetchone()
                    ordinal = int(row["next_ordinal"])
            else:
                ordinal = int(existing["ordinal"]) if ordinal is None else ordinal
                if created_at is None:
                    created_at = existing["created_at"]
            assert ordinal is not None
            evidence_json = json.dumps(
                evidence or {},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            values = (
                message_id,
                thread_id,
                turn_id,
                ordinal,
                item_type,
                status,
                phase,
                text,
                duration_ms,
                exit_code,
                max(0, change_count),
                evidence_json,
                created_at,
            )
            changed = existing is None or values != (
                existing["message_id"],
                existing["thread_public_id"],
                existing["turn_public_id"],
                int(existing["ordinal"]),
                existing["item_type"],
                existing["status"],
                existing["phase"],
                existing["text"],
                existing["duration_ms"],
                existing["exit_code"],
                int(existing["change_count"]),
                existing["evidence_json"],
                existing["created_at"],
            )
            if not changed:
                return False
            conn.execute(
                """
                INSERT INTO message_projection_items(
                    item_public_id, message_id, thread_public_id, turn_public_id,
                    ordinal, item_type, status, phase, text, duration_ms,
                    exit_code, change_count, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_public_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    thread_public_id = excluded.thread_public_id,
                    turn_public_id = excluded.turn_public_id,
                    ordinal = excluded.ordinal,
                    item_type = excluded.item_type,
                    status = excluded.status,
                    phase = excluded.phase,
                    text = excluded.text,
                    duration_ms = excluded.duration_ms,
                    exit_code = excluded.exit_code,
                    change_count = excluded.change_count,
                    evidence_json = excluded.evidence_json,
                    created_at = COALESCE(message_projection_items.created_at, excluded.created_at),
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    *values,
                    updated_at or utc_now(),
                ),
            )
            return True

    def list_projection_items(self, turn_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM message_projection_items
            WHERE turn_public_id = ?
            ORDER BY ordinal ASC, item_public_id ASC
            """,
            (turn_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def projection_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM message_projection_items WHERE item_public_id = ?",
            (item_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def prune_projection_items(self, turn_id: str, keep_item_ids: List[str]) -> None:
        with self._transaction() as conn:
            if keep_item_ids:
                placeholders = ",".join("?" for _ in keep_item_ids)
                conn.execute(
                    """
                    DELETE FROM message_projection_items
                    WHERE turn_public_id = ? AND item_public_id NOT IN (%s)
                    """ % placeholders,
                    (turn_id, *keep_item_ids),
                )
            else:
                conn.execute(
                    "DELETE FROM message_projection_items WHERE turn_public_id = ?",
                    (turn_id,),
                )

    def save_message_snapshot(
        self,
        message_id: str,
        thread_id: str,
        turn_id: str,
        revision: int,
        payload: Dict[str, Any],
        sort_order: int = 0,
        replace_assistant_turn: bool = False,
    ) -> None:
        role = str(payload.get("role") or "assistant")
        client_message_id = payload.get("clientMessageId")
        created_at = str(payload.get("createdAt") or utc_now())
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=True)
            if replace_assistant_turn and role == "assistant":
                self._delete_other_assistant_snapshots_in_transaction(
                    conn, turn_id, message_id
                )
            self._save_message_snapshot_in_transaction(
                conn,
                message_id,
                thread_id,
                turn_id,
                revision,
                role,
                client_message_id,
                payload,
                created_at,
                sort_order,
            )

    def save_message_snapshot_with_events(
        self,
        device_id: str,
        message_id: str,
        thread_id: str,
        turn_id: str,
        revision: int,
        payload: Dict[str, Any],
        events: List[Dict[str, Any]],
        sort_order: int = 0,
        thread_preview: Optional[str] = None,
        replace_assistant_turn: bool = False,
    ) -> List[EventEnvelope]:
        role = str(payload.get("role") or "assistant")
        client_message_id = payload.get("clientMessageId")
        created_at = str(payload.get("createdAt") or utc_now())
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=True)
            if replace_assistant_turn and role == "assistant":
                self._delete_other_assistant_snapshots_in_transaction(
                    conn, turn_id, message_id
                )
            self._save_message_snapshot_in_transaction(
                conn,
                message_id,
                thread_id,
                turn_id,
                revision,
                role,
                client_message_id,
                payload,
                created_at,
                sort_order,
            )
            if thread_preview is not None:
                conn.execute(
                    "UPDATE threads SET preview = ?, updated_at = ? WHERE public_id = ?",
                    (thread_preview, utc_now(), thread_id),
                )
            return [
                self._append_event_in_transaction(
                    conn,
                    device_id,
                    str(event["type"]),
                    event.get("payload") or {},
                    thread_id=str(event.get("thread_id") or thread_id),
                    turn_id=event.get("turn_id", turn_id),
                    item_id=event.get("item_id"),
                )
                for event in events
            ]

    @staticmethod
    def _delete_other_assistant_snapshots_in_transaction(
        conn: sqlite3.Connection,
        turn_id: str,
        message_id: str,
    ) -> None:
        conn.execute(
            """
            DELETE FROM message_snapshots
            WHERE turn_public_id = ? AND role = 'assistant' AND message_id != ?
            """,
            (turn_id, message_id),
        )

    @staticmethod
    def _save_message_snapshot_in_transaction(
        conn: sqlite3.Connection,
        message_id: str,
        thread_id: str,
        turn_id: str,
        revision: int,
        role: str,
        client_message_id: Optional[str],
        payload: Dict[str, Any],
        created_at: str,
        sort_order: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO message_snapshots(
                message_id, thread_public_id, turn_public_id,
                revision, role, client_message_id, payload_json,
                created_at, sort_order, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                revision = excluded.revision,
                role = excluded.role,
                client_message_id = excluded.client_message_id,
                payload_json = excluded.payload_json,
                sort_order = excluded.sort_order,
                updated_at = excluded.updated_at
            """,
            (
                message_id,
                thread_id,
                turn_id,
                revision,
                role,
                client_message_id,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                created_at,
                sort_order,
                utc_now(),
            ),
        )

    def save_user_message_once(
        self,
        message_id: str,
        thread_id: str,
        turn_id: str,
        client_message_id: str,
        payload: Dict[str, Any],
        sort_order: int = 0,
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._transaction() as conn:
            self._require_thread_mutable(conn, thread_id, allow_archived=False)
            conn.execute(
                """
                INSERT OR IGNORE INTO message_snapshots(
                    message_id, thread_public_id, turn_public_id,
                    revision, role, client_message_id, payload_json,
                    created_at, sort_order, updated_at
                ) VALUES (?, ?, ?, 0, 'user', ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    thread_id,
                    turn_id,
                    client_message_id,
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    str(payload.get("createdAt") or now),
                    sort_order,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, client_message_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("user message snapshot was not persisted")
        return json.loads(row["payload_json"])

    def user_message_by_client_id(
        self,
        thread_id: str,
        client_message_id: str,
    ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT payload_json FROM message_snapshots
            WHERE thread_public_id = ? AND role = 'user'
                AND client_message_id = ?
            """,
            (thread_id, client_message_id),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def update_user_message_sort_order(
        self,
        thread_id: str,
        client_message_id: str,
        sort_order: int,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE message_snapshots SET sort_order = ?
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (sort_order, thread_id, client_message_id),
            )

    def update_user_message_state(
        self,
        thread_id: str,
        client_message_id: str,
        state: str,
    ) -> Optional[Dict[str, Any]]:
        if state not in {"queued", "completed", "failed"}:
            raise ValueError("unsupported user message state")
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT message_id, payload_json FROM message_snapshots
                WHERE thread_public_id = ? AND role = 'user'
                    AND client_message_id = ?
                """,
                (thread_id, client_message_id),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            payload["state"] = state
            payload["updatedAt"] = utc_now()
            conn.execute(
                """
                UPDATE message_snapshots SET payload_json = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    utc_now(),
                    row["message_id"],
                ),
            )
            return payload

    def message_snapshot(self, message_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT payload_json FROM message_snapshots WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def list_message_snapshots(self, thread_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT current.payload_json FROM message_snapshots AS current
            WHERE current.thread_public_id = ?
            ORDER BY
                (
                    SELECT MIN(COALESCE(peer.created_at, peer.updated_at))
                    FROM message_snapshots AS peer
                    WHERE peer.turn_public_id = current.turn_public_id
                ) ASC,
                (
                    SELECT MIN(peer.rowid)
                    FROM message_snapshots AS peer
                    WHERE peer.turn_public_id = current.turn_public_id
                ) ASC,
                current.sort_order ASC,
                current.message_id ASC
            """,
            (thread_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def assistant_snapshot_count(self, turn_id: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count FROM message_snapshots
            WHERE turn_public_id = ? AND role = 'assistant'
            """,
            (turn_id,),
        ).fetchone()
        return int(row["count"])

    def read_thread_recovery_snapshot(
        self,
        device_id: str,
        thread_id: str,
        message_limit: int = 20,
        before_message: Optional[Tuple[str, int, int, str]] = None,
        now: Optional[str] = None,
    ) -> Optional[ThreadRecoverySnapshot]:
        """Read state and its stream cursor from one SQLite snapshot."""

        current = utc_now() if now is None else now
        with self._transaction() as conn:
            thread_row = conn.execute(
                "SELECT * FROM threads WHERE public_id = ?",
                (thread_id,),
            ).fetchone()
            if thread_row is None:
                return None
            limit = max(1, min(100, int(message_limit)))
            cursor = before_message
            cursor_clause = ""
            cursor_values: Tuple[Any, ...] = ()
            if cursor is not None:
                cursor_clause = """
                    AND (
                        page_time < ?
                        OR (page_time = ? AND turn_order < ?)
                        OR (page_time = ? AND turn_order = ? AND sort_order < ?)
                        OR (
                            page_time = ? AND turn_order = ?
                            AND sort_order = ? AND message_id < ?
                        )
                    )
                """
                cursor_values = (
                    cursor[0],
                    cursor[0], cursor[1],
                    cursor[0], cursor[1], cursor[2],
                    cursor[0], cursor[1], cursor[2], cursor[3],
                )
            message_rows = conn.execute(
                """
                WITH ordered AS (
                    SELECT
                        current.message_id,
                        current.payload_json,
                        current.sort_order,
                        COALESCE(
                            (
                                SELECT MIN(COALESCE(peer.created_at, peer.updated_at))
                                FROM message_snapshots AS peer
                                WHERE peer.turn_public_id = current.turn_public_id
                            ),
                            current.updated_at
                        ) AS page_time,
                        (
                            SELECT MIN(peer.rowid)
                            FROM message_snapshots AS peer
                            WHERE peer.turn_public_id = current.turn_public_id
                        ) AS turn_order
                    FROM message_snapshots AS current
                    WHERE current.thread_public_id = ?
                )
                SELECT * FROM ordered
                WHERE 1 = 1
                %s
                ORDER BY page_time DESC, turn_order DESC,
                    sort_order DESC, message_id DESC
                LIMIT ?
                """ % cursor_clause,
                (thread_id,) + cursor_values + (limit + 1,),
            ).fetchall()
            has_more_messages = len(message_rows) > limit
            selected_message_rows = list(reversed(message_rows[:limit]))
            approval_rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE thread_public_id = ? AND state = 'pending'
                    AND expires_at > ?
                ORDER BY created_at ASC, public_id ASC
                """,
                (thread_id, current),
            ).fetchall()
            stream = conn.execute(
                "SELECT stream_id, next_seq FROM streams WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if stream is None:
                raise KeyError("device stream not found")
            return ThreadRecoverySnapshot(
                thread=self._thread_from_row(thread_row),
                messages=tuple(
                    json.loads(message_row["payload_json"])
                    for message_row in selected_message_rows
                ),
                message_keys=tuple(
                    (
                        str(message_row["page_time"]),
                        int(message_row["turn_order"]),
                        int(message_row["sort_order"]),
                        str(message_row["message_id"]),
                    )
                    for message_row in selected_message_rows
                ),
                has_more_messages=has_more_messages,
                approval_rows=tuple(approval_rows),
                stream_id=str(stream["stream_id"]),
                cursor=int(stream["next_seq"]) - 1,
            )

    def append_event(
        self,
        device_id: str,
        event_type: str,
        payload: Dict[str, Any],
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> EventEnvelope:
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported public event type")
        if not thread_id:
            raise ValueError("public events require thread_id")
        with self._transaction() as conn:
            return self._append_event_in_transaction(
                conn,
                device_id,
                event_type,
                payload,
                thread_id,
                turn_id,
                item_id,
            )

    def _append_event_in_transaction(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        event_type: str,
        payload: Dict[str, Any],
        thread_id: str,
        turn_id: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> EventEnvelope:
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported public event type")
        if not thread_id:
            raise ValueError("public events require thread_id")
        now = utc_now()
        event_id = public_id("evt")
        stream = conn.execute(
            "SELECT stream_id, next_seq FROM streams WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if stream is None:
            raise KeyError("device stream not found")
        stream_id = str(stream["stream_id"])
        seq = int(stream["next_seq"])
        conn.execute(
            "UPDATE streams SET next_seq = ?, updated_at = ? WHERE device_id = ?",
            (seq + 1, now, device_id),
        )
        conn.execute(
            """
            INSERT INTO event_outbox(
                stream_id, seq, event_id, device_id, occurred_at, type,
                thread_public_id, turn_public_id, item_public_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stream_id,
                seq,
                event_id,
                device_id,
                now,
                event_type,
                thread_id,
                turn_id,
                item_id,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        cutoff = seq - self.event_retention
        if cutoff > 0:
            conn.execute(
                "DELETE FROM event_outbox WHERE stream_id = ? AND seq <= ?",
                (stream_id, cutoff),
            )
        return EventEnvelope(
            stream_id=stream_id,
            event_id=event_id,
            seq=seq,
            occurred_at=now,
            type=event_type,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            payload=payload,
        )

    def replay_events(
        self,
        device_id: str,
        stream_id: str,
        after_seq: int,
    ) -> ReplayResult:
        stream = self._conn.execute(
            """
            SELECT stream_id, next_seq, replay_floor_seq
            FROM streams WHERE device_id = ?
            """,
            (device_id,),
        ).fetchone()
        if stream is None:
            raise KeyError("device stream not found")
        current_stream_id = str(stream["stream_id"])
        current_seq = int(stream["next_seq"]) - 1
        if stream_id != current_stream_id:
            return ReplayResult(
                sync_required=True,
                stream_id=current_stream_id,
                current_seq=current_seq,
                events=[],
            )
        bounds = self._conn.execute(
            "SELECT MIN(seq) AS min_seq, MAX(seq) AS max_seq FROM event_outbox WHERE stream_id = ?",
            (current_stream_id,),
        ).fetchone()
        min_seq = None if bounds is None else bounds["min_seq"]
        sync_required = (
            after_seq > current_seq
            or after_seq < int(stream["replay_floor_seq"])
            or (min_seq is not None and after_seq < int(min_seq) - 1)
        )
        if sync_required:
            return ReplayResult(
                sync_required=True,
                stream_id=current_stream_id,
                current_seq=current_seq,
                events=[],
            )
        rows = self._conn.execute(
            """
            SELECT * FROM event_outbox
            WHERE stream_id = ? AND seq > ? ORDER BY seq ASC
            """,
            (current_stream_id, after_seq),
        ).fetchall()
        return ReplayResult(
            sync_required=False,
            stream_id=current_stream_id,
            current_seq=current_seq,
            events=[self._event_from_row(row) for row in rows],
        )

    def acknowledge(self, device_id: str, stream_id: str, seq: int) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT next_seq, acked_seq FROM streams WHERE device_id = ? AND stream_id = ?",
                (device_id, stream_id),
            ).fetchone()
            if row is None or seq >= int(row["next_seq"]):
                return False
            if seq <= int(row["acked_seq"]):
                return True
            conn.execute(
                "UPDATE streams SET acked_seq = ?, updated_at = ? WHERE device_id = ?",
                (seq, utc_now(), device_id),
            )
            return True

    @staticmethod
    def _thread_from_row(row: sqlite3.Row) -> ThreadDTO:
        return ThreadDTO(
            id=row["public_id"],
            title=row["title"],
            preview=row["preview"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=ThreadStatus(row["status"]),
            last_turn_id=row["last_turn_id"],
            unread=bool(row["unread"]),
        )

    @staticmethod
    def _turn_reservation(row: sqlite3.Row, created: bool) -> TurnReservation:
        return TurnReservation(
            public_id=row["public_id"],
            raw_id=row["raw_id"],
            thread_public_id=row["thread_public_id"],
            client_message_id=row["client_message_id"],
            status=row["status"],
            delivery_state=row["delivery_state"],
            attempt_count=int(row["attempt_count"]),
            created_at=row["created_at"],
            created=created,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventEnvelope:
        return EventEnvelope(
            stream_id=row["stream_id"],
            event_id=row["event_id"],
            seq=row["seq"],
            occurred_at=row["occurred_at"],
            type=row["type"],
            thread_id=row["thread_public_id"],
            turn_id=row["turn_public_id"],
            item_id=row["item_public_id"],
            payload=json.loads(row["payload_json"]),
        )
