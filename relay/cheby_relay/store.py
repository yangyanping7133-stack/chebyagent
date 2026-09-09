from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .config import RelaySettings


Role = Literal["device", "node"]
EPHEMERAL_CONTINUITY_PURPOSE = "production_reload_continuity"
EPHEMERAL_CONTINUITY_DEVICE_NAME = (
    "Cheby Edge production reload continuity"
)
EPHEMERAL_CONTINUITY_MAX_AGE_SECONDS = 3600


class StoreError(RuntimeError):
    code = "STORE_ERROR"


class AuthenticationError(StoreError):
    code = "AUTH_REQUIRED"


class BindingError(StoreError):
    code = "BINDING_DENIED"


class PairingDenied(StoreError):
    code = "PAIRING_DENIED"


class RefreshDenied(StoreError):
    code = "REFRESH_DENIED"


class QueueLimitExceeded(StoreError):
    code = "QUEUE_FULL"


class IdempotencyConflict(StoreError):
    code = "IDEMPOTENCY_CONFLICT"


class AckError(StoreError):
    code = "INVALID_ACK"


class CorrelationError(StoreError):
    code = "CORRELATION_DENIED"


class EphemeralContinuityDenied(StoreError):
    code = "EPHEMERAL_CONTINUITY_DENIED"


@dataclass(frozen=True)
class Principal:
    credential_id: str
    assistant_id: str
    principal_id: str
    role: Role
    expires_at: Optional[int]
    assistant_purpose: str
    public_key_spki: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class BootstrapBundle:
    assistant_id: str
    node_id: str
    pairing_secret: str = field(repr=False)
    node_token: str = field(repr=False)


@dataclass(frozen=True)
class EphemeralContinuityBootstrap:
    assistant_id: str
    node_id: str
    pairing_secret: str = field(repr=False)


@dataclass(frozen=True)
class DeviceTokens:
    assistant_id: str
    device_id: str
    access_token: str = field(repr=False)
    access_expires_at: int
    refresh_token: str = field(repr=False)
    refresh_expires_at: int


@dataclass(frozen=True)
class Delivery:
    delivery_seq: int
    message_id: str
    payload: dict


@dataclass(frozen=True)
class EnqueueResult:
    delivery_seq: int
    state: Literal["new", "pending", "acked", "expired"]

    @property
    def duplicate(self) -> bool:
        return self.state != "new"

    @property
    def still_pending(self) -> bool:
        return self.state in {"new", "pending"}


@dataclass(frozen=True)
class RoutedDelivery:
    assistant_id: str
    delivery: Delivery


@dataclass(frozen=True)
class GCResult:
    expiration_notices: list[RoutedDelivery]
    expired_deliveries: int
    deleted_deliveries: int
    deleted_idempotency: int
    deleted_requests: int
    deleted_credentials: int
    deleted_nonces: int


def _random_id(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(16)


def _salted_verifier(secret: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(
        secret.encode("utf-8")
    )


def _new_verifier(secret: str) -> tuple[bytes, bytes]:
    salt = secrets.token_bytes(16)
    return salt, _salted_verifier(secret, salt)


def _matches(secret: str, salt: bytes, verifier: bytes) -> bool:
    try:
        candidate = _salted_verifier(secret, salt)
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(candidate, verifier)


def _credential_token(credential_id: str) -> str:
    return "rly1_" + credential_id.removeprefix("cred_") + "." + secrets.token_urlsafe(32)


def _credential_id_from_token(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 2 or not parts[0].startswith("rly1_"):
        raise AuthenticationError("invalid credential")
    public = parts[0][5:]
    if len(public) != 22 or len(parts[1]) != 43:
        raise AuthenticationError("invalid credential")
    return "cred_" + public


def _token_secret(token: str) -> str:
    try:
        return token.split(".", 1)[1]
    except IndexError as exc:
        raise AuthenticationError("invalid credential") from exc


class RelayStore:
    def __init__(self, settings: RelaySettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        db_path = Path(settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = db_path.exists()
        self._db = sqlite3.connect(settings.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        if not existed:
            os.chmod(settings.db_path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS assistants (
                    id TEXT PRIMARY KEY,
                    pairing_salt BLOB NOT NULL,
                    pairing_verifier BLOB NOT NULL,
                    pairing_consumed_at INTEGER,
                    pairing_retry_until INTEGER,
                    pairing_device_id TEXT,
                    created_at INTEGER NOT NULL,
                    purpose TEXT NOT NULL DEFAULT 'standard'
                        CHECK(purpose IN (
                            'standard',
                            'production_reload_continuity'
                        ))
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    assistant_id TEXT NOT NULL UNIQUE REFERENCES assistants(id),
                    active_generation INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    assistant_id TEXT NOT NULL UNIQUE REFERENCES assistants(id),
                    name TEXT NOT NULL,
                    public_key_spki TEXT NOT NULL,
                    refresh_salt BLOB NOT NULL,
                    refresh_verifier BLOB NOT NULL,
                    refresh_expires_at INTEGER NOT NULL,
                    previous_refresh_salt BLOB,
                    previous_refresh_verifier BLOB,
                    previous_refresh_valid_until INTEGER,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY,
                    assistant_id TEXT NOT NULL REFERENCES assistants(id),
                    principal_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('device','node')),
                    token_salt BLOB NOT NULL,
                    token_verifier BLOB NOT NULL,
                    expires_at INTEGER,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS credentials_principal
                    ON credentials(principal_id, role, revoked_at);
                CREATE TABLE IF NOT EXISTS proof_nonces (
                    subject TEXT NOT NULL,
                    nonce_hash BLOB NOT NULL,
                    claimed_at INTEGER NOT NULL,
                    PRIMARY KEY(subject, nonce_hash)
                );
                CREATE TABLE IF NOT EXISTS streams (
                    assistant_id TEXT NOT NULL,
                    recipient_role TEXT NOT NULL CHECK(recipient_role IN ('device','node')),
                    next_seq INTEGER NOT NULL DEFAULT 1,
                    ack_cursor INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(assistant_id, recipient_role)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    assistant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_activity_at INTEGER NOT NULL,
                    subscription_active INTEGER NOT NULL DEFAULT 0,
                    subscription_confirmed INTEGER NOT NULL DEFAULT 0,
                    subscription_closed_at INTEGER,
                    PRIMARY KEY(assistant_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    assistant_id TEXT NOT NULL,
                    recipient_role TEXT NOT NULL CHECK(recipient_role IN ('device','node')),
                    delivery_seq INTEGER NOT NULL,
                    sender_role TEXT NOT NULL CHECK(sender_role IN ('device','node','relay')),
                    sender_principal_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fingerprint BLOB NOT NULL,
                    byte_count INTEGER NOT NULL,
                    expires_at INTEGER,
                    expired_at INTEGER,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(assistant_id, recipient_role, delivery_seq),
                    UNIQUE(assistant_id, sender_role, message_id)
                );
                CREATE TABLE IF NOT EXISTS message_idempotency (
                    assistant_id TEXT NOT NULL,
                    sender_role TEXT NOT NULL CHECK(sender_role IN ('device','node')),
                    message_id TEXT NOT NULL,
                    fingerprint BLOB NOT NULL,
                    recipient_role TEXT NOT NULL CHECK(recipient_role IN ('device','node')),
                    delivery_seq INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','acked','expired')),
                    expires_at INTEGER,
                    retained_until INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(assistant_id, sender_role, message_id)
                );
                CREATE INDEX IF NOT EXISTS message_idempotency_retention
                    ON message_idempotency(retained_until);
                CREATE TABLE IF NOT EXISTS expiration_notices (
                    assistant_id TEXT NOT NULL,
                    source_recipient_role TEXT NOT NULL,
                    source_delivery_seq INTEGER NOT NULL,
                    notice_delivery_seq INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(assistant_id, source_recipient_role, source_delivery_seq)
                );
                """
            )
            assistant_columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(assistants)")
            }
            if "purpose" not in assistant_columns:
                self._db.execute(
                    """ALTER TABLE assistants ADD COLUMN purpose TEXT NOT NULL
                       DEFAULT 'standard' CHECK(purpose IN (
                           'standard','production_reload_continuity'
                       ))"""
                )
            request_columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(requests)")
            }
            if "last_activity_at" not in request_columns:
                self._db.execute(
                    "ALTER TABLE requests ADD COLUMN last_activity_at INTEGER NOT NULL DEFAULT 0"
                )
                self._db.execute(
                    "UPDATE requests SET last_activity_at=created_at WHERE last_activity_at=0"
                )
            if "subscription_active" not in request_columns:
                self._db.execute(
                    "ALTER TABLE requests ADD COLUMN subscription_active INTEGER NOT NULL DEFAULT 0"
                )
                self._db.execute(
                    """UPDATE requests SET subscription_active=1
                       WHERE operation='events.subscribe'"""
                )
            if "subscription_confirmed" not in request_columns:
                self._db.execute(
                    """ALTER TABLE requests ADD COLUMN subscription_confirmed
                       INTEGER NOT NULL DEFAULT 0"""
                )
                self._db.execute(
                    """UPDATE requests SET subscription_confirmed=1
                       WHERE operation='events.subscribe' AND subscription_active=1"""
                )
            if "subscription_closed_at" not in request_columns:
                self._db.execute(
                    "ALTER TABLE requests ADD COLUMN subscription_closed_at INTEGER"
                )

    def bootstrap_assistant(self) -> BootstrapBundle:
        now = int(time.time())
        assistant_id = _random_id("asst_")
        node_id = _random_id("node_")
        pairing_secret = "pair_" + secrets.token_urlsafe(32)
        pairing_salt, pairing_verifier = _new_verifier(pairing_secret)
        credential_id = _random_id("cred_")
        node_token = _credential_token(credential_id)
        token_salt, token_verifier = _new_verifier(_token_secret(node_token))
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO assistants(
                       id,pairing_salt,pairing_verifier,pairing_consumed_at,
                       pairing_retry_until,pairing_device_id,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    assistant_id,
                    pairing_salt,
                    pairing_verifier,
                    None,
                    None,
                    None,
                    now,
                ),
            )
            self._db.execute(
                "INSERT INTO nodes(id, assistant_id, created_at) VALUES (?,?,?)",
                (node_id, assistant_id, now),
            )
            self._db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    credential_id,
                    assistant_id,
                    node_id,
                    "node",
                    token_salt,
                    token_verifier,
                    None,
                    None,
                    now,
                ),
            )
            for role in ("device", "node"):
                self._db.execute(
                    "INSERT INTO streams(assistant_id,recipient_role) VALUES (?,?)",
                    (assistant_id, role),
                )
        return BootstrapBundle(assistant_id, node_id, pairing_secret, node_token)

    @staticmethod
    def prepare_ephemeral_continuity_bootstrap() -> EphemeralContinuityBootstrap:
        return EphemeralContinuityBootstrap(
            _random_id("asst_"),
            _random_id("node_"),
            "pair_" + secrets.token_urlsafe(32),
        )

    def bootstrap_ephemeral_continuity(
        self,
        prepared: Optional[EphemeralContinuityBootstrap] = None,
    ) -> EphemeralContinuityBootstrap:
        now = int(time.time())
        bundle = (
            self.prepare_ephemeral_continuity_bootstrap()
            if prepared is None
            else prepared
        )
        if (
            not bundle.assistant_id.startswith("asst_")
            or len(bundle.assistant_id) != 27
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in bundle.assistant_id[5:]
            )
            or not bundle.node_id.startswith("node_")
            or len(bundle.node_id) != 27
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in bundle.node_id[5:]
            )
            or not bundle.pairing_secret.startswith("pair_")
            or len(bundle.pairing_secret.encode("utf-8")) < 32
            or len(bundle.pairing_secret.encode("utf-8")) > 128
            or any(
                character.isspace() or ord(character) < 0x20
                for character in bundle.pairing_secret
            )
        ):
            raise EphemeralContinuityDenied(
                "ephemeral continuity bootstrap is invalid"
            )
        pairing_salt, pairing_verifier = _new_verifier(
            bundle.pairing_secret
        )
        credential_id = _random_id("cred_")
        token_salt = secrets.token_bytes(16)
        token_verifier = secrets.token_bytes(32)
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO assistants(
                       id,pairing_salt,pairing_verifier,pairing_consumed_at,
                       pairing_retry_until,pairing_device_id,created_at,purpose
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    bundle.assistant_id,
                    pairing_salt,
                    pairing_verifier,
                    None,
                    None,
                    None,
                    now,
                    EPHEMERAL_CONTINUITY_PURPOSE,
                ),
            )
            self._db.execute(
                "INSERT INTO nodes(id, assistant_id, created_at) VALUES (?,?,?)",
                (bundle.node_id, bundle.assistant_id, now),
            )
            self._db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    credential_id,
                    bundle.assistant_id,
                    bundle.node_id,
                    "node",
                    token_salt,
                    token_verifier,
                    None,
                    now,
                    now,
                ),
            )
            for role in ("device", "node"):
                self._db.execute(
                    "INSERT INTO streams(assistant_id,recipient_role) VALUES (?,?)",
                    (bundle.assistant_id, role),
                )
        return bundle

    def exchange_pairing(
        self,
        *,
        assistant_id: str,
        pairing_secret: str,
        device_name: str,
        public_key_spki: str,
        now: Optional[int] = None,
        retry_seconds: int = 120,
    ) -> DeviceTokens:
        current = int(time.time()) if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                assistant = self._db.execute(
                    "SELECT * FROM assistants WHERE id=?", (assistant_id,)
                ).fetchone()
                if assistant is None or not _matches(
                    pairing_secret,
                    assistant["pairing_salt"],
                    assistant["pairing_verifier"],
                ):
                    raise PairingDenied("pairing denied")
                if assistant["purpose"] == EPHEMERAL_CONTINUITY_PURPOSE and (
                    device_name != EPHEMERAL_CONTINUITY_DEVICE_NAME
                    or current < int(assistant["created_at"])
                    or current
                    > int(assistant["created_at"])
                    + EPHEMERAL_CONTINUITY_MAX_AGE_SECONDS
                ):
                    raise PairingDenied("pairing denied")
                device = self._db.execute(
                    "SELECT * FROM devices WHERE assistant_id=?", (assistant_id,)
                ).fetchone()
                if assistant["pairing_consumed_at"] is not None:
                    if (
                        device is None
                        or device["revoked_at"] is not None
                        or device["public_key_spki"] != public_key_spki
                        or (
                            assistant["purpose"] == EPHEMERAL_CONTINUITY_PURPOSE
                            and current > int(assistant["pairing_retry_until"] or 0)
                        )
                    ):
                        raise PairingDenied("pairing denied")
                    device_id = device["id"]
                else:
                    if device is not None:
                        raise PairingDenied("pairing denied")
                    device_id = _random_id("dev_")
                    refresh_placeholder_salt, refresh_placeholder = _new_verifier(
                        secrets.token_urlsafe(32)
                    )
                    self._db.execute(
                        """INSERT INTO devices(
                            id,assistant_id,name,public_key_spki,
                            refresh_salt,refresh_verifier,refresh_expires_at,created_at
                        ) VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            device_id,
                            assistant_id,
                            device_name,
                            public_key_spki,
                            refresh_placeholder_salt,
                            refresh_placeholder,
                            current,
                            current,
                        ),
                    )
                    self._db.execute(
                        """UPDATE assistants SET pairing_consumed_at=?,
                           pairing_retry_until=?, pairing_device_id=? WHERE id=?""",
                        (current, current + retry_seconds, device_id, assistant_id),
                    )
                tokens = self._rotate_device_tokens_locked(
                    assistant_id=assistant_id,
                    device_id=device_id,
                    now=current,
                    preserve_current_refresh_as_previous=False,
                )
                self._db.commit()
                return tokens
            except Exception:
                self._db.rollback()
                raise

    def refresh_device(
        self,
        *,
        device_id: str,
        refresh_token: str,
        now: Optional[int] = None,
    ) -> DeviceTokens:
        current = int(time.time()) if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                device = self._db.execute(
                    "SELECT * FROM devices WHERE id=?", (device_id,)
                ).fetchone()
                if device is None or device["revoked_at"] is not None:
                    raise RefreshDenied("refresh denied")
                current_match = (
                    current <= int(device["refresh_expires_at"])
                    and _matches(
                        refresh_token,
                        device["refresh_salt"],
                        device["refresh_verifier"],
                    )
                )
                previous_match = (
                    device["previous_refresh_salt"] is not None
                    and current <= int(device["previous_refresh_valid_until"] or 0)
                    and _matches(
                        refresh_token,
                        device["previous_refresh_salt"],
                        device["previous_refresh_verifier"],
                    )
                )
                if not current_match and not previous_match:
                    raise RefreshDenied("refresh denied")
                refreshed_until = current + self.settings.refresh_token_ttl_seconds
                # Refresh credentials are sender-constrained by the registered device key at the
                # HTTP boundary. Keep the accepted refresh secret stable and rotate only the
                # short-lived access credential. A server commit followed by any number of lost
                # responses can then be retried until the refresh expiry instead of permanently
                # locking out the one phone.
                #
                # ``previous_match`` upgrades a token left behind by the pre-v05 rotating scheme:
                # promote the caller's recoverable token back to current and remove the unknown
                # token whose response was lost.
                if previous_match:
                    self._db.execute(
                        """UPDATE devices SET refresh_salt=previous_refresh_salt,
                           refresh_verifier=previous_refresh_verifier,
                           refresh_expires_at=?,
                           previous_refresh_salt=NULL,
                           previous_refresh_verifier=NULL,
                           previous_refresh_valid_until=NULL WHERE id=?""",
                        (refreshed_until, device_id),
                    )
                else:
                    self._db.execute(
                        """UPDATE devices SET refresh_expires_at=?,
                           previous_refresh_salt=NULL,
                           previous_refresh_verifier=NULL,
                           previous_refresh_valid_until=NULL WHERE id=?""",
                        (refreshed_until, device_id),
                    )
                tokens = self._issue_device_access_locked(
                    assistant_id=device["assistant_id"],
                    device_id=device_id,
                    now=current,
                    refresh_token=refresh_token,
                    refresh_expires_at=refreshed_until,
                )
                self._db.commit()
                return tokens
            except Exception:
                self._db.rollback()
                raise

    def _rotate_device_tokens_locked(
        self,
        *,
        assistant_id: str,
        device_id: str,
        now: int,
        preserve_current_refresh_as_previous: bool,
        retry_grace_seconds: int = 120,
    ) -> DeviceTokens:
        device = self._db.execute(
            "SELECT * FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        if device is None:
            raise RefreshDenied("refresh denied")
        refresh_token = "rrf_" + secrets.token_urlsafe(32)
        refresh_salt, refresh_verifier = _new_verifier(refresh_token)
        refresh_expires = now + self.settings.refresh_token_ttl_seconds
        if preserve_current_refresh_as_previous:
            previous = (
                device["refresh_salt"],
                device["refresh_verifier"],
                now + retry_grace_seconds,
            )
        else:
            previous = (None, None, None)
        self._db.execute(
            """UPDATE devices SET refresh_salt=?, refresh_verifier=?,
               refresh_expires_at=?, previous_refresh_salt=?,
               previous_refresh_verifier=?, previous_refresh_valid_until=?
               WHERE id=?""",
            (
                refresh_salt,
                refresh_verifier,
                refresh_expires,
                previous[0],
                previous[1],
                previous[2],
                device_id,
            ),
        )
        return self._issue_device_access_locked(
            assistant_id=assistant_id,
            device_id=device_id,
            now=now,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires,
        )

    def _issue_device_access_locked(
        self,
        *,
        assistant_id: str,
        device_id: str,
        now: int,
        refresh_token: str,
        refresh_expires_at: int,
    ) -> DeviceTokens:
        self._db.execute(
            "UPDATE credentials SET revoked_at=? WHERE principal_id=? AND role='device' AND revoked_at IS NULL",
            (now, device_id),
        )
        credential_id = _random_id("cred_")
        access_token = _credential_token(credential_id)
        access_salt, access_verifier = _new_verifier(_token_secret(access_token))
        access_expires = now + self.settings.access_token_ttl_seconds
        self._db.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
            (
                credential_id,
                assistant_id,
                device_id,
                "device",
                access_salt,
                access_verifier,
                access_expires,
                None,
                now,
            ),
        )
        return DeviceTokens(
            assistant_id,
            device_id,
            access_token,
            access_expires,
            refresh_token,
            refresh_expires_at,
        )

    def authenticate(self, token: str, expected_role: Role) -> Principal:
        credential_id = _credential_id_from_token(token)
        with self._lock:
            row = self._db.execute(
                """SELECT credentials.*,assistants.purpose AS assistant_purpose
                   FROM credentials JOIN assistants
                     ON assistants.id=credentials.assistant_id
                   WHERE credentials.id=?""",
                (credential_id,),
            ).fetchone()
            now = int(time.time())
            if (
                row is None
                or row["role"] != expected_role
                or row["revoked_at"] is not None
                or (row["expires_at"] is not None and now >= int(row["expires_at"]))
                or not _matches(
                    _token_secret(token), row["token_salt"], row["token_verifier"]
                )
            ):
                raise AuthenticationError("authentication required")
            if expected_role == "device":
                bound = self._db.execute(
                    "SELECT * FROM devices WHERE id=? AND assistant_id=? AND revoked_at IS NULL",
                    (row["principal_id"], row["assistant_id"]),
                ).fetchone()
                public_key = None if bound is None else bound["public_key_spki"]
            else:
                bound = self._db.execute(
                    "SELECT * FROM nodes WHERE id=? AND assistant_id=?",
                    (row["principal_id"], row["assistant_id"]),
                ).fetchone()
                public_key = None
            if bound is None:
                raise BindingError("binding denied")
            return Principal(
                credential_id=row["id"],
                assistant_id=row["assistant_id"],
                principal_id=row["principal_id"],
                role=expected_role,
                expires_at=row["expires_at"],
                assistant_purpose=str(row["assistant_purpose"]),
                public_key_spki=public_key,
            )

    def principal_still_valid(self, principal: Principal) -> bool:
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                """SELECT credentials.role,credentials.principal_id,
                          credentials.assistant_id,credentials.expires_at,
                          credentials.revoked_at,
                          assistants.purpose AS assistant_purpose
                   FROM credentials JOIN assistants
                     ON assistants.id=credentials.assistant_id
                   WHERE credentials.id=?""",
                (principal.credential_id,),
            ).fetchone()
            if (
                row is None
                or row["role"] != principal.role
                or row["principal_id"] != principal.principal_id
                or row["assistant_id"] != principal.assistant_id
                or row["assistant_purpose"] != principal.assistant_purpose
                or row["revoked_at"] is not None
                or (row["expires_at"] is not None and now >= int(row["expires_at"]))
            ):
                return False
            if principal.role == "device":
                binding = self._db.execute(
                    "SELECT 1 FROM devices WHERE id=? AND assistant_id=? AND revoked_at IS NULL",
                    (principal.principal_id, principal.assistant_id),
                ).fetchone()
            else:
                binding = self._db.execute(
                    "SELECT 1 FROM nodes WHERE id=? AND assistant_id=?",
                    (principal.principal_id, principal.assistant_id),
                ).fetchone()
            return binding is not None

    def _assert_principal_active_locked(
        self,
        principal: Principal,
        *,
        node_generation: Optional[int],
        now: int,
    ) -> None:
        row = self._db.execute(
            """SELECT credentials.role,credentials.principal_id,
                      credentials.assistant_id,credentials.expires_at,
                      credentials.revoked_at,
                      assistants.purpose AS assistant_purpose
               FROM credentials JOIN assistants
                 ON assistants.id=credentials.assistant_id
               WHERE credentials.id=?""",
            (principal.credential_id,),
        ).fetchone()
        if (
            row is None
            or row["role"] != principal.role
            or row["principal_id"] != principal.principal_id
            or row["assistant_id"] != principal.assistant_id
            or row["assistant_purpose"] != principal.assistant_purpose
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and now >= int(row["expires_at"]))
        ):
            raise AuthenticationError("credential expired or revoked")
        if principal.role == "device":
            binding = self._db.execute(
                "SELECT 1 FROM devices WHERE id=? AND assistant_id=? AND revoked_at IS NULL",
                (principal.principal_id, principal.assistant_id),
            ).fetchone()
            if binding is None:
                raise BindingError("binding denied")
        else:
            binding = self._db.execute(
                "SELECT active_generation FROM nodes WHERE id=? AND assistant_id=?",
                (principal.principal_id, principal.assistant_id),
            ).fetchone()
            if (
                binding is None
                or node_generation is None
                or int(binding["active_generation"]) != node_generation
            ):
                raise BindingError("stale node generation")

    @staticmethod
    def _assert_data_plane_allowed(principal: Principal) -> None:
        if principal.assistant_purpose == EPHEMERAL_CONTINUITY_PURPOSE:
            raise BindingError("continuity identity has no data plane")

    def device_public_key(self, device_id: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT public_key_spki FROM devices WHERE id=? AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            return None if row is None else str(row[0])

    def rotate_node_credential(self, assistant_id: str, node_id: str) -> str:
        now = int(time.time())
        credential_id = _random_id("cred_")
        token = _credential_token(credential_id)
        salt, verifier = _new_verifier(_token_secret(token))
        with self._lock, self._db:
            binding = self._db.execute(
                """SELECT assistants.purpose FROM nodes JOIN assistants
                     ON assistants.id=nodes.assistant_id
                   WHERE nodes.id=? AND nodes.assistant_id=?""",
                (node_id, assistant_id),
            ).fetchone()
            if (
                binding is None
                or binding["purpose"] == EPHEMERAL_CONTINUITY_PURPOSE
            ):
                raise BindingError("binding denied")
            self._db.execute(
                "UPDATE credentials SET revoked_at=? WHERE principal_id=? AND role='node' AND revoked_at IS NULL",
                (now, node_id),
            )
            self._db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    credential_id,
                    assistant_id,
                    node_id,
                    "node",
                    salt,
                    verifier,
                    None,
                    None,
                    now,
                ),
            )
        return token

    def revoke_principal(self, principal_id: str) -> None:
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                "UPDATE credentials SET revoked_at=? WHERE principal_id=? AND revoked_at IS NULL",
                (now, principal_id),
            )
            self._db.execute(
                "UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (now, principal_id),
            )
            self._db.execute(
                """UPDATE requests SET subscription_active=0,
                   subscription_confirmed=0, subscription_closed_at=?,
                   last_activity_at=?
                   WHERE device_id=? AND operation='events.subscribe'
                   AND subscription_active=1""",
                (now, now, principal_id),
            )

    def revoke_ephemeral_continuity_assistant(
        self,
        *,
        assistant_id: str,
        node_id: str,
        now: Optional[int] = None,
    ) -> Optional[str]:
        """Fail-closed revocation for a production TLS continuity identity.

        The command deliberately cannot target an established phone binding. It
        accepts only an otherwise unused continuity-purpose assistant whose Node
        credential has already been revoked and whose optional Device uses the
        fixed continuity-only name. An exact identity pair that is wholly absent
        is an idempotent recovery success; every partial or mismatched identity
        fails closed.
        """

        current = int(time.time()) if now is None else now
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                assistant = self._db.execute(
                    "SELECT * FROM assistants WHERE id=?",
                    (assistant_id,),
                ).fetchone()
                node = self._db.execute(
                    "SELECT * FROM nodes WHERE id=?",
                    (node_id,),
                ).fetchone()
                if assistant is None and node is None:
                    # The bootstrap may fail before its first INSERT after the
                    # recovery metadata is durable.  Replaying that exact
                    # metadata is a safe, idempotent fence only while neither
                    # public identity exists.
                    self._db.commit()
                    return None
                if (
                    assistant is None
                    or node is None
                    or str(node["assistant_id"]) != assistant_id
                    or assistant["purpose"] != EPHEMERAL_CONTINUITY_PURPOSE
                    or int(node["active_generation"]) != 0
                ):
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )
                bound_nodes = self._db.execute(
                    "SELECT id FROM nodes WHERE assistant_id=? ORDER BY id",
                    (assistant_id,),
                ).fetchall()
                if [str(row["id"]) for row in bound_nodes] != [node_id]:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )

                devices = self._db.execute(
                    "SELECT * FROM devices WHERE assistant_id=?",
                    (assistant_id,),
                ).fetchall()
                if len(devices) > 1:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )
                device = devices[0] if devices else None
                device_id = None if device is None else str(device["id"])
                if (
                    device is None
                    and assistant["pairing_device_id"] is not None
                ) or (
                    device is not None
                    and (
                        str(device["name"])
                        != EPHEMERAL_CONTINUITY_DEVICE_NAME
                        or assistant["pairing_device_id"] != device_id
                    )
                ):
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )

                stream_rows = self._db.execute(
                    """SELECT recipient_role,next_seq,ack_cursor FROM streams
                       WHERE assistant_id=? ORDER BY recipient_role""",
                    (assistant_id,),
                ).fetchall()
                if [
                    (
                        str(row["recipient_role"]),
                        int(row["next_seq"]),
                        int(row["ack_cursor"]),
                    )
                    for row in stream_rows
                ] != [("device", 1, 0), ("node", 1, 0)]:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )

                for table in (
                    "requests",
                    "deliveries",
                    "message_idempotency",
                    "expiration_notices",
                ):
                    if self._db.execute(
                        f"SELECT 1 FROM {table} WHERE assistant_id=? LIMIT 1",
                        (assistant_id,),
                    ).fetchone() is not None:
                        raise EphemeralContinuityDenied(
                            "ephemeral continuity binding denied"
                        )

                credentials = self._db.execute(
                    """SELECT principal_id,role,revoked_at,created_at
                       FROM credentials WHERE assistant_id=?""",
                    (assistant_id,),
                ).fetchall()
                if not credentials:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )
                node_credentials = 0
                device_credentials = 0
                active_device_credentials = 0
                for credential in credentials:
                    role = str(credential["role"])
                    principal_id = str(credential["principal_id"])
                    if (
                        (
                            role == "node"
                            and principal_id != node_id
                        )
                        or (
                            role == "device"
                            and principal_id != device_id
                        )
                        or role not in {"node", "device"}
                    ):
                        raise EphemeralContinuityDenied(
                            "ephemeral continuity binding denied"
                        )
                    if role == "node":
                        node_credentials += 1
                        if (
                            credential["revoked_at"] is None
                            or int(credential["revoked_at"])
                            != int(credential["created_at"])
                        ):
                            raise EphemeralContinuityDenied(
                                "ephemeral continuity Node must be fenced first"
                            )
                    else:
                        device_credentials += 1
                        if credential["revoked_at"] is None:
                            active_device_credentials += 1
                if (
                    node_credentials != 1
                    or (
                        device is None
                        and device_credentials != 0
                    )
                    or (
                        device is not None
                        and device_credentials < 1
                    )
                    or active_device_credentials > 1
                ):
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity binding denied"
                    )

                self._db.execute(
                    """UPDATE credentials SET revoked_at=?
                       WHERE assistant_id=? AND revoked_at IS NULL""",
                    (current, assistant_id),
                )
                self._db.execute(
                    """UPDATE devices SET revoked_at=?
                       WHERE assistant_id=? AND revoked_at IS NULL""",
                    (current, assistant_id),
                )
                self._db.execute(
                    """UPDATE assistants SET pairing_consumed_at=?,
                       pairing_retry_until=?, pairing_device_id=?
                       WHERE id=?""",
                    (current, current - 1, device_id, assistant_id),
                )
                self._db.commit()
                return device_id
            except Exception:
                self._db.rollback()
                raise

    def purge_ephemeral_continuity_assistant(
        self,
        *,
        assistant_id: str,
        node_id: str,
        now: Optional[int] = None,
    ) -> None:
        """Delete only an exactly matched, revoked continuity-only assistant."""

        current = int(time.time()) if now is None else now
        device_id = self.revoke_ephemeral_continuity_assistant(
            assistant_id=assistant_id,
            node_id=node_id,
            now=current,
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                assistant = self._db.execute(
                    "SELECT purpose FROM assistants WHERE id=?",
                    (assistant_id,),
                ).fetchone()
                node = self._db.execute(
                    "SELECT assistant_id FROM nodes WHERE id=?",
                    (node_id,),
                ).fetchone()
                if assistant is None and node is None:
                    self._db.commit()
                    return
                if (
                    assistant is None
                    or node is None
                    or str(node["assistant_id"]) != assistant_id
                    or assistant["purpose"] != EPHEMERAL_CONTINUITY_PURPOSE
                ):
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity cleanup denied"
                    )
                active_credential = self._db.execute(
                    """SELECT 1 FROM credentials
                       WHERE assistant_id=? AND revoked_at IS NULL LIMIT 1""",
                    (assistant_id,),
                ).fetchone()
                active_device = self._db.execute(
                    """SELECT 1 FROM devices
                       WHERE assistant_id=? AND revoked_at IS NULL LIMIT 1""",
                    (assistant_id,),
                ).fetchone()
                if active_credential is not None or active_device is not None:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity revocation is incomplete"
                    )
                device = self._db.execute(
                    """SELECT public_key_spki FROM devices
                       WHERE assistant_id=?""",
                    (assistant_id,),
                ).fetchone()
                subjects: list[str] = []
                if device_id is not None:
                    subjects.append("device:" + device_id)
                if device is not None:
                    try:
                        spki_der = base64.b64decode(
                            str(device["public_key_spki"]),
                            validate=True,
                        )
                    except ValueError as error:
                        raise EphemeralContinuityDenied(
                            "ephemeral continuity public key is invalid"
                        ) from error
                    subjects.append(
                        "spki:" + hashlib.sha256(spki_der).hexdigest()
                    )
                for subject in subjects:
                    self._db.execute(
                        "DELETE FROM proof_nonces WHERE subject=?",
                        (subject,),
                    )
                self._db.execute(
                    "DELETE FROM credentials WHERE assistant_id=?",
                    (assistant_id,),
                )
                self._db.execute(
                    "DELETE FROM devices WHERE assistant_id=?",
                    (assistant_id,),
                )
                self._db.execute(
                    "DELETE FROM streams WHERE assistant_id=?",
                    (assistant_id,),
                )
                self._db.execute(
                    "DELETE FROM nodes WHERE assistant_id=?",
                    (assistant_id,),
                )
                deleted = self._db.execute(
                    "DELETE FROM assistants WHERE id=?",
                    (assistant_id,),
                )
                if deleted.rowcount != 1:
                    raise EphemeralContinuityDenied(
                        "ephemeral continuity cleanup denied"
                    )
                for table in (
                    "assistants",
                    "nodes",
                    "devices",
                    "credentials",
                    "streams",
                ):
                    if self._db.execute(
                        f"SELECT 1 FROM {table} WHERE "
                        + (
                            "id=?"
                            if table == "assistants"
                            else "assistant_id=?"
                        )
                        + " LIMIT 1",
                        (assistant_id,),
                    ).fetchone() is not None:
                        raise EphemeralContinuityDenied(
                            "ephemeral continuity cleanup is incomplete"
                        )
                for subject in subjects:
                    if self._db.execute(
                        "SELECT 1 FROM proof_nonces WHERE subject=? LIMIT 1",
                        (subject,),
                    ).fetchone() is not None:
                        raise EphemeralContinuityDenied(
                            "ephemeral continuity cleanup is incomplete"
                        )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def claim_proof_nonce(self, subject: str, nonce: bytes, now: Optional[int] = None) -> None:
        current = int(time.time()) if now is None else now
        nonce_hash = hashlib.sha256(nonce).digest()
        cutoff = current - self.settings.proof_nonce_retention_seconds
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """DELETE FROM proof_nonces WHERE rowid IN (
                        SELECT rowid FROM proof_nonces WHERE claimed_at<? LIMIT ?
                    )""",
                    (cutoff, self.settings.gc_batch_size),
                )
                self._db.execute(
                    "INSERT INTO proof_nonces VALUES (?,?,?)",
                    (subject, nonce_hash, current),
                )
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                self._db.rollback()
                raise AuthenticationError("proof replay denied") from exc
            except Exception:
                self._db.rollback()
                raise

    def activate_node_generation(self, node_id: str) -> int:
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT nodes.active_generation,assistants.purpose
                   FROM nodes JOIN assistants
                     ON assistants.id=nodes.assistant_id
                   WHERE nodes.id=?""",
                (node_id,),
            ).fetchone()
            if (
                row is None
                or row["purpose"] == EPHEMERAL_CONTINUITY_PURPOSE
            ):
                raise BindingError("binding denied")
            generation = int(row["active_generation"]) + 1
            self._db.execute(
                "UPDATE nodes SET active_generation=? WHERE id=?",
                (generation, node_id),
            )
            return generation

    def is_current_node_generation(self, node_id: str, generation: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT active_generation FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            return row is not None and int(row[0]) == generation

    def device_belongs(self, assistant_id: str, device_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM devices WHERE id=? AND assistant_id=? AND revoked_at IS NULL",
                (device_id, assistant_id),
            ).fetchone()
            return row is not None

    def request_binding(self, assistant_id: str, request_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM requests WHERE assistant_id=? AND request_id=?",
                (assistant_id, request_id),
            ).fetchone()

    def enqueue(
        self,
        *,
        principal: Principal,
        recipient_role: Role,
        message_id: str,
        payload: dict,
        expires_at: Optional[int],
        node_generation: Optional[int] = None,
    ) -> EnqueueResult:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_bytes = payload_json.encode("utf-8")
        fingerprint = hashlib.sha256(payload_bytes).digest()
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_principal_active_locked(
                    principal, node_generation=node_generation, now=now
                )
                self._assert_data_plane_allowed(principal)
                duplicate = self._db.execute(
                    """SELECT delivery_seq,fingerprint,recipient_role,state,expires_at
                       FROM message_idempotency
                       WHERE assistant_id=? AND sender_role=? AND message_id=?""",
                    (principal.assistant_id, principal.role, message_id),
                ).fetchone()
                if duplicate is not None:
                    if not hmac.compare_digest(duplicate["fingerprint"], fingerprint):
                        raise IdempotencyConflict("message id reused")
                    state = str(duplicate["state"])
                    if (
                        state == "pending"
                        and duplicate["expires_at"] is not None
                        and int(duplicate["expires_at"]) <= now
                    ):
                        delivery = self._db.execute(
                            """SELECT * FROM deliveries WHERE assistant_id=?
                               AND recipient_role=? AND delivery_seq=?""",
                            (
                                principal.assistant_id,
                                duplicate["recipient_role"],
                                duplicate["delivery_seq"],
                            ),
                        ).fetchone()
                        if delivery is not None:
                            self._expire_delivery_locked(delivery, now)
                        state = "expired"
                    self._db.commit()
                    return EnqueueResult(int(duplicate["delivery_seq"]), state)

                if principal.role == "device":
                    request_id = payload["requestId"]
                    request = self._db.execute(
                        "SELECT * FROM requests WHERE assistant_id=? AND request_id=?",
                        (principal.assistant_id, request_id),
                    ).fetchone()
                    if request is not None:
                        raise IdempotencyConflict("request id reused")
                else:
                    self._validate_node_correlation_locked(principal, payload, now)

                stream = self._db.execute(
                    "SELECT * FROM streams WHERE assistant_id=? AND recipient_role=?",
                    (principal.assistant_id, recipient_role),
                ).fetchone()
                if stream is None:
                    raise BindingError("stream binding denied")
                pending = self._db.execute(
                    """SELECT COUNT(*) AS count, COALESCE(SUM(byte_count),0) AS bytes
                       FROM deliveries WHERE assistant_id=? AND recipient_role=?
                       AND delivery_seq>? AND expired_at IS NULL
                       AND (expires_at IS NULL OR expires_at>?)""",
                    (
                        principal.assistant_id,
                        recipient_role,
                        stream["ack_cursor"],
                        now,
                    ),
                ).fetchone()
                if (
                    int(pending["count"]) >= self.settings.max_pending_messages
                    or int(pending["bytes"]) + len(payload_bytes)
                    > self.settings.max_pending_bytes
                ):
                    raise QueueLimitExceeded("queue full")
                seq = int(stream["next_seq"])
                self._db.execute(
                    "UPDATE streams SET next_seq=? WHERE assistant_id=? AND recipient_role=?",
                    (seq + 1, principal.assistant_id, recipient_role),
                )
                self._db.execute(
                    """INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        principal.assistant_id,
                        recipient_role,
                        seq,
                        principal.role,
                        principal.principal_id,
                        message_id,
                        payload_json,
                        fingerprint,
                        len(payload_bytes),
                        expires_at,
                        None,
                        now,
                    ),
                )
                self._db.execute(
                    """INSERT INTO message_idempotency VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        principal.assistant_id,
                        principal.role,
                        message_id,
                        fingerprint,
                        recipient_role,
                        seq,
                        "pending",
                        expires_at,
                        now + self.settings.idempotency_retention_seconds,
                        now,
                    ),
                )
                if principal.role == "device":
                    self._db.execute(
                        """INSERT INTO requests(
                           assistant_id,request_id,device_id,operation,message_id,
                           created_at,last_activity_at,subscription_active,
                           subscription_confirmed,subscription_closed_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            principal.assistant_id,
                            payload["requestId"],
                            principal.principal_id,
                            payload["operation"],
                            message_id,
                            now,
                            now,
                            1 if payload["operation"] == "events.subscribe" else 0,
                            0,
                            None,
                        ),
                    )
                self._db.commit()
                return EnqueueResult(seq, "new")
            except Exception:
                self._db.rollback()
                raise

    def _validate_node_correlation_locked(
        self, principal: Principal, payload: dict, now: int
    ) -> None:
        device_id = payload["deviceId"]
        request_id = payload["requestId"]
        device = self._db.execute(
            "SELECT 1 FROM devices WHERE id=? AND assistant_id=?",
            (device_id, principal.assistant_id),
        ).fetchone()
        request = self._db.execute(
            "SELECT * FROM requests WHERE assistant_id=? AND request_id=?",
            (principal.assistant_id, request_id),
        ).fetchone()
        if device is None or request is None or request["device_id"] != device_id:
            raise CorrelationError("response correlation denied")
        if payload["kind"] == "response" and payload["operation"] != request["operation"]:
            raise CorrelationError("response operation mismatch")
        if payload["kind"] == "response" and request["operation"] == "events.subscribe":
            # An authenticated correlated response proves that Connector
            # durably processed this command, even if its transport ack is
            # lost immediately afterward. Do not later expire the command and
            # undo the confirmed subscription state.
            self._db.execute(
                """UPDATE message_idempotency SET state='acked', retained_until=?
                   WHERE assistant_id=? AND sender_role='device' AND message_id=?
                   AND state='pending'""",
                (
                    now + self.settings.idempotency_retention_seconds,
                    principal.assistant_id,
                    request["message_id"],
                ),
            )
            if payload["ok"]:
                # The Connector persists and sends its response after every old
                # event already in its outbox. Only this explicit success makes
                # it safe to retire the previous correlation request.
                self._db.execute(
                    """UPDATE requests SET subscription_active=0,
                       subscription_confirmed=0, subscription_closed_at=?,
                       last_activity_at=?
                       WHERE assistant_id=? AND device_id=?
                       AND operation='events.subscribe' AND request_id!=?
                       AND subscription_active=1""",
                    (
                        now,
                        now,
                        principal.assistant_id,
                        device_id,
                        request_id,
                    ),
                )
                self._db.execute(
                    """UPDATE requests SET subscription_active=1,
                       subscription_confirmed=1, subscription_closed_at=NULL,
                       last_activity_at=?
                       WHERE assistant_id=? AND request_id=?""",
                    (now, principal.assistant_id, request_id),
                )
            else:
                self._db.execute(
                    """UPDATE requests SET subscription_active=0,
                       subscription_confirmed=0, subscription_closed_at=?,
                       last_activity_at=?
                       WHERE assistant_id=? AND request_id=?""",
                    (now, now, principal.assistant_id, request_id),
                )
        if payload["kind"] == "event":
            if request["operation"] != "events.subscribe":
                raise CorrelationError("event subscription mismatch")
            if not bool(request["subscription_active"]):
                closed_at = int(request["subscription_closed_at"] or 0)
                if now > (
                    closed_at + self.settings.subscription_handoff_grace_seconds
                ):
                    raise CorrelationError("event subscription is no longer active")
            self._db.execute(
                """UPDATE requests SET last_activity_at=?
                   WHERE assistant_id=? AND request_id=?""",
                (now, principal.assistant_id, request_id),
            )

    def _expire_delivery_locked(
        self, row: sqlite3.Row, now: int
    ) -> Optional[RoutedDelivery]:
        if row["expired_at"] is not None:
            return None
        self._db.execute(
            """UPDATE deliveries SET expired_at=? WHERE assistant_id=?
               AND recipient_role=? AND delivery_seq=? AND expired_at IS NULL""",
            (now, row["assistant_id"], row["recipient_role"], row["delivery_seq"]),
        )
        self._db.execute(
            """UPDATE message_idempotency SET state='expired', retained_until=?
               WHERE assistant_id=? AND sender_role=? AND message_id=?""",
            (
                now + self.settings.idempotency_retention_seconds,
                row["assistant_id"],
                row["sender_role"],
                row["message_id"],
            ),
        )
        if row["recipient_role"] != "node" or row["sender_role"] != "device":
            return None
        existing = self._db.execute(
            """SELECT 1 FROM expiration_notices WHERE assistant_id=?
               AND source_recipient_role=? AND source_delivery_seq=?""",
            (row["assistant_id"], row["recipient_role"], row["delivery_seq"]),
        ).fetchone()
        if existing is not None:
            return None
        source_payload = json.loads(row["payload_json"])
        if source_payload.get("kind") != "command":
            return None
        if source_payload.get("operation") == "events.subscribe":
            # A provisional replacement never displaced the prior confirmed
            # subscription. Expiry retires only the failed candidate.
            self._db.execute(
                """UPDATE requests SET subscription_active=0,
                   subscription_confirmed=0, subscription_closed_at=?,
                   last_activity_at=? WHERE assistant_id=? AND request_id=?
                   AND subscription_confirmed=0""",
                (
                    now,
                    now,
                    row["assistant_id"],
                    source_payload["requestId"],
                ),
            )
        notice_payload = {
            "kind": "response",
            "requestId": source_payload["requestId"],
            "deviceId": source_payload["deviceId"],
            "operation": source_payload["operation"],
            "ok": False,
            "error": {
                "code": "DELIVERY_EXPIRED",
                "message": "Command expired before Node delivery",
                "retryable": True,
            },
        }
        notice_json = json.dumps(
            notice_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        notice_bytes = notice_json.encode("utf-8")
        message_id = _random_id("msg_")
        stream = self._db.execute(
            "SELECT next_seq FROM streams WHERE assistant_id=? AND recipient_role='device'",
            (row["assistant_id"],),
        ).fetchone()
        if stream is None:
            raise BindingError("device stream binding denied")
        notice_seq = int(stream["next_seq"])
        self._db.execute(
            "UPDATE streams SET next_seq=? WHERE assistant_id=? AND recipient_role='device'",
            (notice_seq + 1, row["assistant_id"]),
        )
        self._db.execute(
            "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["assistant_id"],
                "device",
                notice_seq,
                "relay",
                "relay",
                message_id,
                notice_json,
                hashlib.sha256(notice_bytes).digest(),
                len(notice_bytes),
                None,
                None,
                now,
            ),
        )
        self._db.execute(
            "INSERT INTO expiration_notices VALUES (?,?,?,?,?,?)",
            (
                row["assistant_id"],
                row["recipient_role"],
                row["delivery_seq"],
                notice_seq,
                message_id,
                now,
            ),
        )
        return RoutedDelivery(
            row["assistant_id"],
            Delivery(notice_seq, message_id, notice_payload),
        )

    def pending_deliveries(self, assistant_id: str, recipient_role: Role) -> list[Delivery]:
        now = int(time.time())
        with self._lock:
            stream = self._db.execute(
                "SELECT ack_cursor FROM streams WHERE assistant_id=? AND recipient_role=?",
                (assistant_id, recipient_role),
            ).fetchone()
            if stream is None:
                raise BindingError("stream binding denied")
            rows = self._db.execute(
                """SELECT delivery_seq,message_id,payload_json FROM deliveries
                   WHERE assistant_id=? AND recipient_role=? AND delivery_seq>?
                   AND expired_at IS NULL AND (expires_at IS NULL OR expires_at>?)
                   ORDER BY delivery_seq""",
                (assistant_id, recipient_role, stream["ack_cursor"], now),
            ).fetchall()
        return [
            Delivery(int(row["delivery_seq"]), row["message_id"], json.loads(row["payload_json"]))
            for row in rows
        ]

    def stream_state(self, assistant_id: str, recipient_role: Role) -> tuple[int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT ack_cursor,next_seq FROM streams WHERE assistant_id=? AND recipient_role=?",
                (assistant_id, recipient_role),
            ).fetchone()
            if row is None:
                raise BindingError("stream binding denied")
            return int(row["ack_cursor"]), int(row["next_seq"])

    def acknowledge(
        self,
        *,
        principal: Principal,
        seq: int,
        node_generation: Optional[int] = None,
    ) -> int:
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._assert_principal_active_locked(
                    principal, node_generation=node_generation, now=now
                )
                self._assert_data_plane_allowed(principal)
                row = self._db.execute(
                    "SELECT ack_cursor,next_seq FROM streams WHERE assistant_id=? AND recipient_role=?",
                    (principal.assistant_id, principal.role),
                ).fetchone()
                if row is None or seq >= int(row["next_seq"]):
                    raise AckError("ack is outside the delivery stream")
                cursor = max(int(row["ack_cursor"]), seq)
                self._db.execute(
                    "UPDATE streams SET ack_cursor=? WHERE assistant_id=? AND recipient_role=?",
                    (cursor, principal.assistant_id, principal.role),
                )
                self._db.execute(
                    """UPDATE message_idempotency SET state='acked',
                       retained_until=? WHERE assistant_id=? AND recipient_role=?
                       AND delivery_seq<=? AND state='pending'""",
                    (
                        now + self.settings.idempotency_retention_seconds,
                        principal.assistant_id,
                        principal.role,
                        cursor,
                    ),
                )
                self._db.commit()
                return cursor
            except Exception:
                self._db.rollback()
                raise

    def run_gc(self, *, now: Optional[int] = None) -> GCResult:
        current = int(time.time()) if now is None else now
        batch = self.settings.gc_batch_size
        notices: list[RoutedDelivery] = []
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                due = self._db.execute(
                    """SELECT d.* FROM deliveries d
                       JOIN message_idempotency m
                       ON m.assistant_id=d.assistant_id
                       AND m.sender_role=d.sender_role
                       AND m.message_id=d.message_id
                       WHERE m.state='pending' AND d.expired_at IS NULL
                       AND d.expires_at IS NOT NULL AND d.expires_at<=?
                       ORDER BY d.expires_at,d.delivery_seq LIMIT ?""",
                    (current, batch),
                ).fetchall()
                for row in due:
                    notice = self._expire_delivery_locked(row, current)
                    if notice is not None:
                        notices.append(notice)

                acked_cutoff = current - self.settings.acked_payload_retention_seconds
                deleted_deliveries = self._db.execute(
                    """DELETE FROM deliveries WHERE rowid IN (
                        SELECT d.rowid FROM deliveries d JOIN streams s
                        ON s.assistant_id=d.assistant_id
                        AND s.recipient_role=d.recipient_role
                        WHERE d.created_at<=? AND (
                            d.delivery_seq<=s.ack_cursor OR EXISTS (
                                SELECT 1 FROM message_idempotency m
                                WHERE m.assistant_id=d.assistant_id
                                AND m.sender_role=d.sender_role
                                AND m.message_id=d.message_id
                                AND m.state='acked'
                            )
                        )
                        LIMIT ?
                    )""",
                    (acked_cutoff, batch),
                ).rowcount
                remaining = max(0, batch - deleted_deliveries)
                if remaining:
                    expired_cutoff = (
                        current - self.settings.expired_payload_retention_seconds
                    )
                    deleted_deliveries += self._db.execute(
                        """DELETE FROM deliveries WHERE rowid IN (
                            SELECT rowid FROM deliveries WHERE expired_at IS NOT NULL
                            AND expired_at<=? LIMIT ?
                        )""",
                        (expired_cutoff, remaining),
                    ).rowcount

                deleted_idempotency = self._db.execute(
                    """DELETE FROM message_idempotency WHERE rowid IN (
                        SELECT m.rowid FROM message_idempotency m
                        WHERE m.state!='pending' AND m.retained_until<=?
                        AND NOT EXISTS (
                            SELECT 1 FROM deliveries d
                            WHERE d.assistant_id=m.assistant_id
                            AND d.sender_role=m.sender_role
                            AND d.message_id=m.message_id
                        ) LIMIT ?
                    )""",
                    (current, batch),
                ).rowcount
                request_cutoff = current - self.settings.request_retention_seconds
                deleted_requests = self._db.execute(
                    """DELETE FROM requests WHERE rowid IN (
                        SELECT r.rowid FROM requests r WHERE r.last_activity_at<=?
                        AND NOT (
                            r.operation='events.subscribe'
                            AND r.subscription_active=1
                            AND r.subscription_confirmed=1
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM message_idempotency m
                            WHERE m.assistant_id=r.assistant_id
                            AND m.sender_role='device' AND m.message_id=r.message_id
                            AND m.state='pending'
                        ) LIMIT ?
                    )""",
                    (request_cutoff, batch),
                ).rowcount
                credential_cutoff = (
                    current - self.settings.credential_retention_seconds
                )
                deleted_credentials = self._db.execute(
                    """DELETE FROM credentials WHERE rowid IN (
                        SELECT credentials.rowid FROM credentials
                        JOIN assistants
                          ON assistants.id=credentials.assistant_id
                        WHERE assistants.purpose='standard' AND (
                            (credentials.revoked_at IS NOT NULL
                             AND credentials.revoked_at<=?) OR
                            (credentials.expires_at IS NOT NULL
                             AND credentials.expires_at<=?)
                        )
                        LIMIT ?
                    )""",
                    (credential_cutoff, credential_cutoff, batch),
                ).rowcount
                nonce_cutoff = current - self.settings.proof_nonce_retention_seconds
                deleted_nonces = self._db.execute(
                    """DELETE FROM proof_nonces WHERE rowid IN (
                        SELECT rowid FROM proof_nonces WHERE claimed_at<=? LIMIT ?
                    )""",
                    (nonce_cutoff, batch),
                ).rowcount
                self._db.commit()
                return GCResult(
                    expiration_notices=notices,
                    expired_deliveries=len(due),
                    deleted_deliveries=deleted_deliveries,
                    deleted_idempotency=deleted_idempotency,
                    deleted_requests=deleted_requests,
                    deleted_credentials=deleted_credentials,
                    deleted_nonces=deleted_nonces,
                )
            except Exception:
                self._db.rollback()
                raise

    def raw_secret_columns(self) -> list[str]:
        """Test/audit helper: schema must expose verifiers, never raw credential columns."""
        with self._lock:
            names: list[str] = []
            for table in ("assistants", "devices", "credentials"):
                for row in self._db.execute(f"PRAGMA table_info({table})"):
                    name = str(row["name"])
                    if name in {"token", "secret", "refresh_token", "pairing_secret"}:
                        names.append(f"{table}.{name}")
            return names
