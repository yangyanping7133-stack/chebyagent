from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import TypeAdapter

from .bridge import (
    APPROVAL_REQUEST_METHODS,
    BridgeNotAccepted,
    BridgeThreadNotFound,
    BridgeError,
    BridgeEvent,
    BridgeItem,
    BridgeThread,
    BridgeTurn,
    CodexBridge,
)
from .assets import ImageAssetError, ImageAssetManager
from .config import GatewaySettings
from .models import (
    ApprovalDTO,
    ApprovalDecision,
    AuthRefreshResponse,
    DeleteImpactPreviewResponse,
    DeleteImpactThreadDTO,
    PairingExchangeResponse,
    ThreadDetailResponse,
    ThreadDTO,
    ThreadStatus,
    TurnDTO,
    UserHistoryMessageSnapshot,
)
from .projection import (
    ProjectionItem,
    bounded_text,
    patch_ops,
    project_live_work_panel,
    semantically_equal,
    unicode_safe_text,
)
from .pop import ProofError, load_p256_spki
from .store import (
    AssetBindingError,
    AssetUnavailableError,
    DeviceRecord,
    GatewayStore,
    ThreadLifecycleBlockedError,
    ThreadBusyError,
    TurnIdempotencyConflictError,
    TurnAmbiguousError,
    public_id,
    utc_now,
)


_GATEWAY_SERVICE_INSTANCES: "weakref.WeakSet[GatewayService]" = weakref.WeakSet()
_PUBLIC_MESSAGE_ADAPTER = TypeAdapter(Dict[str, Any])


def turn_request_fingerprint(input_parts: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            input_parts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _after_gateway_fork_in_child() -> None:
    for service in tuple(_GATEWAY_SERVICE_INSTANCES):
        service._drop_inherited_instance_lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_gateway_fork_in_child)


class GatewayError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        retryable: bool = False,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class _AsyncLifecycleGate:
    """Writer-preferring gate for catalog mutations and Turn submissions."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._waiting_writers == 0
            )
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer and self._readers == 0
                )
            except BaseException:
                self._waiting_writers -= 1
                self._condition.notify_all()
                raise
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class GatewayService:
    PUBLIC_TEXT_LIMIT = 65_536
    TRUNCATION_MARKER = "\n\n[truncated]"
    USER_MESSAGE_SORT_ORDER_BASE = 0
    ASSISTANT_MESSAGE_SORT_ORDER = 2_000_000_000
    APPROVAL_SUMMARY_BYTES = 1_024
    APPROVAL_REASON_BYTES = 2_048
    APPROVAL_TOTAL_BYTES = 3_072

    def __init__(
        self,
        settings: GatewaySettings,
        store: GatewayStore,
        bridge: CodexBridge,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bridge = bridge
        self._bridge_event_handler = self.handle_bridge_event
        self._bridge_handler_registered = False
        self._bridge_started = False
        self._approval_sweeper_task: Optional[asyncio.Task] = None
        self._asset_sweeper_task: Optional[asyncio.Task] = None
        self.assets = ImageAssetManager(settings.asset_staging_dir, store)
        self._thread_lifecycle_gate: Optional[_AsyncLifecycleGate] = None
        self._turn_submission_locks: weakref.WeakValueDictionary[
            str, asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._instance_lock_fd: Optional[int] = None
        self._instance_lock_pid: Optional[int] = None
        _GATEWAY_SERVICE_INSTANCES.add(self)

    async def start(self) -> None:
        self._acquire_instance_lock()
        try:
            self.store.ensure_pairing_grant(self.settings.pairing_secret)
            self.bridge.set_event_handler(self._bridge_event_handler)
            self._bridge_handler_registered = True
            await self.bridge.start()
            self._bridge_started = True
            await self.assets.prepare(
                self.settings.local_image_enabled,
                await self.bridge.runtime_version(),
                self.settings.codex_expected_version,
            )
            await self._recover_claimed_delete_impacts()
            self._repair_retained_approval_data()
            await self.reconcile_startup()
            self._approval_sweeper_task = asyncio.create_task(
                self._approval_sweeper_loop()
            )
            if self.assets.enabled:
                self._asset_sweeper_task = asyncio.create_task(
                    self._asset_sweeper_loop()
                )
        except BaseException:
            if self._bridge_handler_registered:
                with contextlib.suppress(Exception):
                    await self.bridge.close()
            self._bridge_started = False
            self.bridge.clear_event_handler(self._bridge_event_handler)
            self._bridge_handler_registered = False
            self.assets.close()
            self._release_instance_lock()
            raise

    async def close(self) -> None:
        if not self._owns_instance_lock():
            return
        approval_task = self._approval_sweeper_task
        asset_task = self._asset_sweeper_task
        self._approval_sweeper_task = None
        self._asset_sweeper_task = None
        if approval_task is not None:
            approval_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await approval_task
        if asset_task is not None:
            asset_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asset_task
        try:
            if self._bridge_started:
                await self.bridge.close()
        finally:
            self.assets.close()
            self._bridge_started = False
            self.bridge.clear_event_handler(self._bridge_event_handler)
            self._bridge_handler_registered = False
            self._release_instance_lock()

    def _acquire_instance_lock(self) -> None:
        if self._instance_lock_fd is not None:
            if self._instance_lock_pid == os.getpid():
                raise GatewayError(
                    "GATEWAY_ALREADY_STARTED",
                    "Gateway service is already running",
                    status_code=503,
                    retryable=False,
                )
            # A prefork child must not treat the parent's inherited descriptor
            # as ownership. Close only this process's reference, without an
            # explicit LOCK_UN that could release the parent's shared lock.
            with contextlib.suppress(OSError):
                os.close(self._instance_lock_fd)
            self._instance_lock_fd = None
            self._instance_lock_pid = None
        database_path = os.path.realpath(os.path.abspath(self.store.path))
        lock_path = database_path + ".service.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            lock_fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise GatewayError(
                "GATEWAY_INSTANCE_LOCK_FAILED",
                "Gateway could not establish exclusive database ownership",
                status_code=503,
                retryable=False,
            ) from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise GatewayError(
                "GATEWAY_INSTANCE_CONFLICT",
                "Another Gateway instance already owns this database",
                status_code=503,
                retryable=False,
            ) from exc
        except OSError as exc:
            os.close(lock_fd)
            raise GatewayError(
                "GATEWAY_INSTANCE_LOCK_FAILED",
                "Gateway could not establish exclusive database ownership",
                status_code=503,
                retryable=False,
            ) from exc
        self._instance_lock_fd = lock_fd
        self._instance_lock_pid = os.getpid()

    def _release_instance_lock(self) -> None:
        lock_fd = self._instance_lock_fd
        lock_pid = self._instance_lock_pid
        self._instance_lock_fd = None
        self._instance_lock_pid = None
        if lock_fd is None:
            return
        if lock_pid == os.getpid():
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(lock_fd)

    def _drop_inherited_instance_lock(self) -> None:
        lock_fd = self._instance_lock_fd
        self._instance_lock_fd = None
        self._instance_lock_pid = None
        self._approval_sweeper_task = None
        self._asset_sweeper_task = None
        self._thread_lifecycle_gate = None
        self._turn_submission_locks = weakref.WeakValueDictionary()
        self.bridge.clear_event_handler(self._bridge_event_handler)
        self._bridge_handler_registered = False
        self._bridge_started = False
        if lock_fd is not None:
            # Never issue LOCK_UN in the child: the inherited descriptor shares
            # the parent's open-file description and could release its lock.
            with contextlib.suppress(OSError):
                os.close(lock_fd)

    def _require_instance_lock(self) -> None:
        if not self._owns_instance_lock():
            raise GatewayError(
                "GATEWAY_INSTANCE_CONFLICT",
                "This Gateway instance does not own the database lifecycle lock",
                status_code=503,
                retryable=False,
            )

    def _owns_instance_lock(self) -> bool:
        return (
            self._instance_lock_fd is not None
            and self._instance_lock_pid == os.getpid()
        )

    def exchange_pairing(
        self,
        pairing_secret: str,
        device_name: str,
        device_public_key: str,
    ) -> PairingExchangeResponse:
        self._require_instance_lock()
        try:
            # Keep key validation at the service boundary as well as the HTTP
            # proof boundary. Direct callers must not be able to register an
            # opaque string as a device identity.
            load_p256_spki(device_public_key)
        except ProofError as exc:
            raise GatewayError(
                "PAIRING_DENIED",
                "Pairing credentials are not valid",
                status_code=401,
            ) from exc
        token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = self._expiry_after(now, self.settings.token_ttl_seconds)
        refresh_expires_at = self._expiry_after(
            now,
            self.settings.refresh_token_ttl_seconds,
        )
        device, _ = self.store.consume_pairing_grant_and_pair(
            expected_secret=self.settings.pairing_secret,
            presented_secret=pairing_secret,
            name=device_name,
            device_public_key=device_public_key,
            token=token,
            token_expires_at=expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
        )
        if device is None:
            raise GatewayError(
                "PAIRING_DENIED",
                "Pairing credentials are not valid or the grant was already used",
                status_code=401,
            )
        return PairingExchangeResponse(
            device_id=device.id,
            access_token=token,
            expires_at=expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            stream_id=device.stream_id,
        )

    def ensure_relay_device(self, device_id: str, name: str) -> DeviceRecord:
        """Return the private device projection for a Relay-authenticated peer."""

        self._require_instance_lock()
        return self.store.ensure_relay_device(device_id, name)

    def authenticate(self, token: str) -> DeviceRecord:
        device, outcome = self.store.authenticate_with_outcome(token)
        if outcome == "expired":
            raise GatewayError(
                "TOKEN_EXPIRED",
                "Access token has expired",
                status_code=401,
            )
        if outcome == "revoked":
            raise GatewayError(
                "DEVICE_REVOKED",
                "This device is no longer authorized",
                status_code=401,
            )
        if device is None or outcome != "valid":
            raise GatewayError(
                "AUTH_REQUIRED",
                "Authentication is required",
                status_code=401,
            )
        return device

    def refresh_access(
        self,
        device_id: str,
        refresh_token: str,
    ) -> AuthRefreshResponse:
        self._require_instance_lock()
        access_token = secrets.token_urlsafe(32)
        rotated_refresh_token = secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = self._expiry_after(now, self.settings.token_ttl_seconds)
        refresh_expires_at = self._expiry_after(
            now,
            self.settings.refresh_token_ttl_seconds,
        )
        device, outcome = self.store.rotate_refresh_token(
            expected_device_id=device_id,
            refresh_token=refresh_token,
            new_access_token=access_token,
            access_expires_at=expires_at,
            new_refresh_token=rotated_refresh_token,
            refresh_expires_at=refresh_expires_at,
            now=now.isoformat().replace("+00:00", "Z"),
        )
        if device is None or outcome != "rotated":
            raise GatewayError(
                "REFRESH_DENIED",
                "Refresh token is not valid",
                status_code=401,
            )
        return AuthRefreshResponse(
            device_id=device.id,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=rotated_refresh_token,
            refresh_expires_at=refresh_expires_at,
            stream_id=device.stream_id,
        )

    async def reconcile_startup(self) -> None:
        """Reconcile durable reservations before accepting mobile traffic."""
        device = self.store.active_device()
        for row in self.store.list_active_turns():
            turn_id = str(row["public_id"])
            thread_id = str(row["thread_public_id"])
            delivery_state = str(row["delivery_state"])
            raw_turn_id = row["raw_id"]
            if delivery_state == "reserved" and row["status"] == "pending":
                # dispatching was never persisted, so turn/start was never called.
                self.store.mark_turn_not_accepted(turn_id)
                continue
            if delivery_state == "dispatching" or raw_turn_id is None:
                self.store.mark_turn_ambiguous(
                    turn_id,
                    device_id=(device.id if device is not None else None),
                )
                continue

            raw_thread_id = self.store.thread_raw_id(thread_id)
            if raw_thread_id is None:
                self.store.mark_turn_ambiguous(
                    turn_id,
                    device_id=(device.id if device is not None else None),
                )
                continue
            try:
                raw_thread = await self.bridge.read_thread(raw_thread_id)
            except BridgeError:
                self.store.mark_turn_ambiguous(
                    turn_id,
                    device_id=(device.id if device is not None else None),
                )
                continue
            public_status = self._public_status(raw_thread.status)
            if public_status in {
                ThreadStatus.RUNNING,
                ThreadStatus.WAITING_APPROVAL,
                ThreadStatus.WAITING_USER,
            }:
                self.store.update_turn_status(
                    turn_id,
                    (
                        "waitingApproval"
                        if public_status == ThreadStatus.WAITING_APPROVAL
                        else (
                            "waitingUser"
                            if public_status == ThreadStatus.WAITING_USER
                            else "inProgress"
                        )
                    ),
                )
                self.store.clear_thread_resync(thread_id, public_status.value)
                continue

            # First project authoritative history in case only the live event
            # was lost. If the accepted turn is still non-terminal after Codex
            # itself reports the Thread idle, the old app-server execution no
            # longer exists. Close it explicitly as outcome-unknown: never
            # resubmit the accepted business key, but do release this Thread's
            # FIFO for a new client message.
            self._project_bridge_history(thread_id, raw_thread.turns)
            refreshed = self.store.turn_by_public_id(turn_id)
            if refreshed is not None and str(refreshed["delivery_state"]) == "terminal":
                self.store.clear_thread_resync(thread_id, public_status.value)
                continue
            terminal_message = self._operation_outcome_unknown_message(
                thread_id=thread_id,
                turn_id=turn_id,
                raw_turn_id=str(raw_turn_id),
            )
            self.store.mark_accepted_turn_outcome_unknown(
                turn_id,
                device_id=(device.id if device is not None else None),
                terminal_message=terminal_message,
                terminal_message_sort_order=self.ASSISTANT_MESSAGE_SORT_ORDER,
            )

    async def sweep_expired_approvals(self, now: Optional[str] = None) -> int:
        self._require_instance_lock()
        device = self.store.active_device()
        claimed = self.store.claim_expired_approvals(
            now=now,
            device_id=(device.id if device is not None else None),
        )
        for row in claimed:
            request_id = json.loads(row["raw_request_id"])
            try:
                await self.bridge.resolve_approval(request_id, "reject")
            except BridgeError:
                # The expiry is terminal and fail-closed. Delivery is attempted
                # exactly once and never retried after an uncertain outcome.
                pass
        return len(claimed)

    async def _approval_sweeper_loop(self) -> None:
        interval = max(0.05, self.settings.approval_sweep_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            await self.sweep_expired_approvals()

    async def _asset_sweeper_loop(self) -> None:
        interval = max(1.0, self.settings.asset_sweep_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self.assets.cleanup)

    def local_image_capability(self) -> Dict[str, Any]:
        if not self.assets.enabled:
            return {}
        return {
            "localImage": {
                "uploadVersion": 1,
                "mediaTypes": ["image/jpeg", "image/png"],
                "maxUploadBytes": 8 * 1024 * 1024,
                "maxPixels": 25_000_000,
                "maxEdgePixels": 12_000,
                "maxImagesPerTurn": 10,
            }
        }

    async def upload_image(
        self,
        device: DeviceRecord,
        thread_id: str,
        client_message_id: str,
        client_asset_id: str,
        body: bytes,
    ):
        self._require_instance_lock()
        try:
            return await self.assets.upload(
                device_id=device.id,
                thread_id=thread_id,
                client_message_id=client_message_id,
                client_asset_id=client_asset_id,
                body=body,
            )
        except KeyError as exc:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404) from exc
        except ThreadLifecycleBlockedError as exc:
            code = "THREAD_ARCHIVED" if exc.reason == "archived" else "THREAD_DELETE_PENDING"
            raise GatewayError(code, "Conversation does not accept new images", 409) from exc
        except ImageAssetError as exc:
            raise GatewayError(exc.code, exc.message, exc.status_code) from exc

    async def _recover_claimed_delete_impacts(self) -> None:
        for record in self.store.list_claimed_delete_impacts():
            device = self.store.device_by_id(record.device_id)
            if device is None:
                # The FK normally makes this impossible. Do not accept traffic
                # while a destructive claim has no stream authority.
                raise GatewayError(
                    "DELETE_RECOVERY_REQUIRED",
                    "Permanent delete recovery has no device authority",
                    status_code=503,
                    retryable=False,
                )
            await self._sync_thread_catalog(
                device,
                require_stable=True,
                stability_root_raw_id=record.root_raw_id,
            )
            # Reconciliation has now either tombstoned absent Threads or kept
            # the still-authoritative mappings. In both cases the unknown
            # dispatch is terminal and must never be sent again.
            self.store.complete_delete_impact(
                record.token_hash,
                record.affected_raw_ids,
            )

    @staticmethod
    def _expiry_after(now: datetime, seconds: int) -> str:
        return (now + timedelta(seconds=seconds)).isoformat().replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def _ambiguous_turn_error() -> GatewayError:
        return GatewayError(
            "TURN_DELIVERY_AMBIGUOUS_RESYNC_REQUIRED",
            "Codex may have accepted this turn; resync before continuing",
            status_code=409,
            retryable=False,
        )

    def _lifecycle_gate(self) -> _AsyncLifecycleGate:
        gate = self._thread_lifecycle_gate
        if gate is None:
            gate = _AsyncLifecycleGate()
            self._thread_lifecycle_gate = gate
        return gate

    def _lifecycle_lock(self):
        return self._lifecycle_gate().write()

    def _turn_submission_lock(self, thread_id: str) -> asyncio.Lock:
        lock = self._turn_submission_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_submission_locks[thread_id] = lock
        return lock

    async def sync_threads(
        self,
        device: DeviceRecord,
        archived: bool = False,
    ) -> List[ThreadDTO]:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            await self._sync_thread_catalog(device, require_stable=True)
            return self.store.list_threads(include_archived=archived)

    async def create_thread(
        self, device: DeviceRecord, title: Optional[str]
    ) -> ThreadDTO:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            try:
                raw = await self.bridge.start_thread(title=title)
            except BridgeError as exc:
                raise GatewayError(
                    "CODEX_UNAVAILABLE",
                    "Codex could not create the conversation",
                    status_code=503,
                    retryable=True,
                ) from exc
            return self.store.create_thread_with_snapshot_event(
                device.id,
                raw_id=raw.raw_id,
                title=raw.title,
                preview=raw.preview,
                status=self._public_status(raw.status).value,
                archived=bool(raw.archived),
                created_at=raw.created_at,
                updated_at=raw.updated_at,
                provisional_ttl_seconds=self.settings.provisional_thread_ttl_seconds,
            )

    async def read_thread(
        self,
        thread_id: str,
        device: Optional[DeviceRecord] = None,
        message_limit: Optional[int] = None,
        message_cursor: Optional[str] = None,
    ) -> ThreadDetailResponse:
        self._require_instance_lock()
        # A snapshot refresh is scoped to one Thread. Treating it as a global
        # lifecycle writer lets a selected-Thread refresh wait behind an active
        # Turn and, because the gate is writer-preferring, prevents a Turn on
        # every other Thread from entering. Serialize against submission only
        # for this Thread while allowing distinct Threads to keep progressing.
        async with self._turn_submission_lock(thread_id):
            async with self._lifecycle_gate().read():
                return await self._read_thread_locked(
                    thread_id,
                    device,
                    message_limit,
                    message_cursor,
                )

    async def _read_thread_locked(
        self,
        thread_id: str,
        device: Optional[DeviceRecord],
        message_limit: Optional[int],
        message_cursor: Optional[str],
    ) -> ThreadDetailResponse:
        raw_id = self._require_raw_thread_id(thread_id)
        try:
            raw = await self.bridge.read_thread(raw_id)
        except BridgeThreadNotFound as exc:
            cleanup_device = device or self.store.active_device()
            if cleanup_device is not None:
                self.store.delete_missing_provisional_thread(
                    cleanup_device.id,
                    thread_id,
                    reason="provisionalThreadNotLoaded",
                )
            raise GatewayError(
                "THREAD_NOT_FOUND",
                "Conversation was not found",
                status_code=404,
            ) from exc
        except BridgeError as exc:
            raise GatewayError(
                "THREAD_NOT_FOUND",
                "Conversation was not found",
                status_code=404,
            ) from exc
        try:
            thread = self._update_mapped_thread(thread_id, raw)
            history_reconciled = self._project_bridge_history(thread_id, raw.turns)
            if history_reconciled:
                self.store.clear_thread_resync(thread_id, thread.status.value)
        except ThreadLifecycleBlockedError as exc:
            raise GatewayError(
                "THREAD_NOT_FOUND",
                "Conversation was not found",
                status_code=404,
            ) from exc
        authenticated_device = device or self.store.active_device()
        if authenticated_device is None:
            raise GatewayError("AUTH_REQUIRED", "Authentication is required", 401)
        limit = (
            self.settings.thread_detail_default_messages
            if message_limit is None
            else max(
                1,
                min(message_limit, self.settings.thread_detail_max_messages),
            )
        )
        before_message = (
            None
            if message_cursor is None
            else self._decode_message_cursor(thread_id, message_cursor)
        )
        snapshot = self.store.read_thread_recovery_snapshot(
            authenticated_device.id,
            thread_id,
            message_limit=limit,
            before_message=before_message,
        )
        if snapshot is None:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        messages = list(snapshot.messages)
        message_keys = list(snapshot.message_keys)
        has_more_messages = snapshot.has_more_messages
        approvals = [
            self._approval_dto(
                row,
                action_token=self._approval_action_token(str(row["public_id"])),
            )
            for row in snapshot.approval_rows
        ]

        def build_response() -> ThreadDetailResponse:
            next_cursor = (
                self._encode_message_cursor(thread_id, message_keys[0])
                if has_more_messages and message_keys
                else None
            )
            return ThreadDetailResponse(
                thread=snapshot.thread,
                messages=messages,
                approvals=approvals,
                stream_id=snapshot.stream_id,
                cursor=snapshot.cursor,
                next_message_cursor=next_cursor,
                has_more_messages=has_more_messages,
            )

        response = build_response()
        response_size = len(response.model_dump_json(by_alias=True).encode("utf-8"))
        if response_size > self.settings.thread_detail_max_bytes and messages:
            # Serialize each message once, then select a suffix using exact JSON
            # component sizes. Re-serializing a shrinking multi-megabyte response
            # for every removed message is quadratic in the public page size.
            message_sizes = [
                len(_PUBLIC_MESSAGE_ADAPTER.dump_json(message))
                for message in messages
            ]
            remaining_message_bytes = sum(message_sizes)
            has_more_messages = True
            empty_page = ThreadDetailResponse(
                thread=snapshot.thread,
                messages=[],
                approvals=approvals,
                stream_id=snapshot.stream_id,
                cursor=snapshot.cursor,
                next_message_cursor="",
                has_more_messages=True,
            )
            empty_page_size = len(
                empty_page.model_dump_json(by_alias=True).encode("utf-8")
            )
            first = 0
            while first < len(messages) - 1:
                count = len(messages) - first
                cursor = self._encode_message_cursor(thread_id, message_keys[first])
                estimated_size = (
                    empty_page_size
                    + remaining_message_bytes
                    + max(0, count - 1)
                    + len(cursor.encode("ascii"))
                )
                if estimated_size <= self.settings.thread_detail_max_bytes:
                    break
                remaining_message_bytes -= message_sizes[first]
                first += 1
            if first:
                messages = messages[first:]
                message_keys = message_keys[first:]
            response = build_response()
            response_size = len(
                response.model_dump_json(by_alias=True).encode("utf-8")
            )
        if response_size > self.settings.thread_detail_max_bytes:
            raise GatewayError(
                "THREAD_DETAIL_TOO_LARGE",
                "Conversation detail exceeds the public response budget",
                status_code=409,
                retryable=False,
            )
        return response

    async def resume_thread(
        self,
        device: DeviceRecord,
        thread_id: str,
    ) -> ThreadDTO:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            raw_id = self._require_raw_thread_id(thread_id)
            try:
                await self.bridge.resume_thread(raw_id)
            except BridgeThreadNotFound as exc:
                self.store.delete_missing_provisional_thread(
                    device.id,
                    thread_id,
                    reason="provisionalThreadNotLoaded",
                )
                raise GatewayError(
                    "THREAD_NOT_FOUND",
                    "Conversation was not found",
                    status_code=404,
                ) from exc
            except BridgeError as exc:
                raise GatewayError(
                    "CODEX_UNAVAILABLE",
                    "Conversation could not be resumed",
                    status_code=503,
                    retryable=True,
                ) from exc
            await self._sync_thread_catalog(device, require_stable=True)
            thread = self.store.thread_by_public_id(thread_id)
            if thread is None:
                raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
            return thread

    async def patch_thread(
        self,
        device: DeviceRecord,
        thread_id: str,
        title: Optional[str],
        archived: Optional[bool],
    ) -> ThreadDTO:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            raw_id = self._require_raw_thread_id(thread_id)
            try:
                if title is not None:
                    await self.bridge.set_thread_name(raw_id, title)
                if archived is not None:
                    await self.bridge.set_thread_archived(raw_id, archived)
            except BridgeThreadNotFound as exc:
                self.store.delete_missing_provisional_thread(
                    device.id,
                    thread_id,
                    reason="provisionalThreadNotLoaded",
                )
                raise GatewayError(
                    "THREAD_NOT_FOUND",
                    "Conversation was not found",
                    status_code=404,
                ) from exc
            except BridgeError as exc:
                raise GatewayError(
                    "CODEX_UNAVAILABLE",
                    "Conversation could not be updated",
                    status_code=503,
                    retryable=True,
                ) from exc
            self.store.update_provisional_thread_metadata(
                device.id,
                thread_id,
                title=title,
                archived=archived,
            )
            await self._sync_thread_catalog(device, require_stable=True)
            thread = self.store.thread_by_public_id(thread_id)
            if thread is None:
                raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
            if archived is not None and (
                thread.status == ThreadStatus.ARCHIVED
            ) != archived:
                raise GatewayError(
                    "THREAD_STATE_NOT_RECONCILED",
                    "Conversation state did not reconcile",
                    status_code=409,
                    retryable=True,
                )
            return thread

    async def preview_delete_thread(
        self,
        device: DeviceRecord,
        thread_id: str,
    ) -> DeleteImpactPreviewResponse:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            root_raw_id = self._require_raw_thread_id(thread_id)
            catalog = await self._sync_thread_catalog(
                device,
                require_stable=True,
                stability_root_raw_id=root_raw_id,
            )
            impact = self._delete_impact(catalog, thread_id)
            self._require_archived_impact(impact)
            public_by_raw: Dict[str, ThreadDTO] = {}
            for raw in impact:
                mapped = self.store.thread_dto_by_raw_id(raw.raw_id)
                if mapped is None:
                    raise GatewayError(
                        "THREAD_CATALOG_INCONSISTENT",
                        "Conversation mapping did not reconcile",
                        status_code=409,
                        retryable=True,
                    )
                public_by_raw[raw.raw_id] = mapped
            token = secrets.token_urlsafe(48)
            expires_at = self._expiry_after(
                datetime.now(timezone.utc),
                self.settings.delete_impact_ttl_seconds,
            )
            affected_public_ids = [public_by_raw[raw.raw_id].id for raw in impact]
            try:
                self.store.create_delete_impact(
                    token=token,
                    device_id=device.id,
                    root_thread_public_id=thread_id,
                    root_raw_id=impact[0].raw_id,
                    affected_raw_ids=[raw.raw_id for raw in impact],
                    affected_public_ids=affected_public_ids,
                    expires_at=expires_at,
                )
            except ThreadLifecycleBlockedError as exc:
                raise GatewayError(
                    "DELETE_IMPACT_BUSY",
                    "Conversation lifecycle changed; refresh and try again",
                    status_code=409,
                ) from exc
            return DeleteImpactPreviewResponse(
                root_thread_id=thread_id,
                affected_threads=[
                    DeleteImpactThreadDTO(
                        id=public_by_raw[raw.raw_id].id,
                        title=public_by_raw[raw.raw_id].title,
                        status=public_by_raw[raw.raw_id].status,
                        archived=bool(raw.archived),
                    )
                    for raw in impact
                ],
                affected_count=len(impact),
                expires_at=expires_at,
                impact_token=token,
            )

    async def delete_thread(
        self,
        device: DeviceRecord,
        thread_id: str,
        impact_token: str,
    ) -> None:
        self._require_instance_lock()
        async with self._lifecycle_lock():
            token_state, token_record = self.store.inspect_delete_impact(
                impact_token,
                device.id,
                thread_id,
            )
            self._raise_for_delete_token_state(token_state)
            if token_record is None:
                raise GatewayError(
                    "DELETE_IMPACT_INVALID",
                    "Delete impact authorization is not valid",
                    status_code=409,
                )

            catalog = await self._sync_thread_catalog(
                device,
                require_stable=True,
                stability_root_raw_id=token_record.root_raw_id,
            )
            impact = self._delete_impact(catalog, thread_id)
            self._require_archived_impact(impact)
            public_by_raw = {
                str(row["raw_id"]): str(row["public_id"])
                for row in self.store.thread_catalog_rows()
            }
            affected_raw_ids = [raw.raw_id for raw in impact]
            affected_public_ids = [public_by_raw[raw_id] for raw_id in affected_raw_ids]
            claim_state, claimed = self.store.claim_delete_impact(
                impact_token,
                device.id,
                thread_id,
                affected_raw_ids,
                affected_public_ids,
            )
            self._raise_for_delete_token_state(claim_state)
            if claimed is None or claim_state != "claimed":
                raise GatewayError(
                    "DELETE_IMPACT_INVALID",
                    "Delete impact authorization is not valid",
                    status_code=409,
                )

            before_raw_ids = {raw.raw_id for raw in catalog}
            try:
                await self.bridge.delete_thread(claimed.root_raw_id)
            except BridgeThreadNotFound as exc:
                if self.store.thread_catalog_state(thread_id) == "provisional":
                    self.store.delete_threads_by_raw_ids(
                        device.id,
                        affected_raw_ids,
                        reason="provisionalThreadNotLoaded",
                    )
                    self.store.complete_delete_impact(
                        claimed.token_hash,
                        affected_raw_ids,
                    )
                    return
                reusable = self.store.release_delete_impact(claimed.token_hash)
                raise GatewayError(
                    "DELETE_NOT_ACCEPTED",
                    "Codex did not accept permanent delete",
                    status_code=503,
                    retryable=reusable,
                ) from exc
            except BridgeNotAccepted as exc:
                reusable = self.store.release_delete_impact(claimed.token_hash)
                raise GatewayError(
                    "DELETE_NOT_ACCEPTED",
                    "Codex did not accept permanent delete",
                    status_code=503,
                    retryable=reusable,
                ) from exc
            except BridgeError as exc:
                try:
                    await self._sync_thread_catalog(
                        device,
                        require_stable=True,
                        stability_root_raw_id=claimed.root_raw_id,
                    )
                except GatewayError:
                    # Keep the claim and pending-delete barrier durable. Startup
                    # recovery is the only safe authority after unknown delivery.
                    pass
                else:
                    self.store.complete_delete_impact(
                        claimed.token_hash,
                        affected_raw_ids,
                    )
                raise GatewayError(
                    "DELETE_DELIVERY_UNKNOWN",
                    "Permanent delete delivery is unknown; it will not be retried",
                    status_code=503,
                    retryable=False,
                ) from exc

            if self.store.thread_catalog_state(thread_id) == "provisional":
                self.store.delete_threads_by_raw_ids(
                    device.id,
                    affected_raw_ids,
                    reason="provisionalDeleteConfirmed",
                )
                self.store.complete_delete_impact(
                    claimed.token_hash,
                    affected_raw_ids,
                )
                return

            try:
                after_catalog = await self._sync_thread_catalog(
                    device,
                    require_stable=True,
                    stability_root_raw_id=claimed.root_raw_id,
                )
            except GatewayError:
                # A successful RPC followed by failed authority is not safe to
                # retry. Leave the durable claim for startup recovery.
                raise GatewayError(
                    "DELETE_RECONCILIATION_REQUIRED",
                    "Delete outcome requires an authoritative refresh",
                    status_code=503,
                    retryable=False,
                )

            self.store.complete_delete_impact(claimed.token_hash, affected_raw_ids)
            after_raw_ids = {raw.raw_id for raw in after_catalog}
            expected = set(affected_raw_ids)
            removed = before_raw_ids - after_raw_ids
            if removed != expected:
                if expected.issubset(removed):
                    raise GatewayError(
                        "DELETE_IMPACT_CHANGED_DURING_EXECUTION",
                        "The authoritative delete impact changed during execution",
                        status_code=409,
                    )
                raise GatewayError(
                    "DELETE_RECONCILIATION_REQUIRED",
                    "Delete outcome requires an authoritative refresh",
                    status_code=409,
                    retryable=False,
                )

    async def start_turn(
        self,
        device: DeviceRecord,
        thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> TurnDTO:
        self._require_instance_lock()
        try:
            self.store.preflight_turn_start(
                device.id,
                thread_id,
                client_message_id,
            )
        except KeyError as exc:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404) from exc
        except ThreadLifecycleBlockedError as exc:
            if exc.reason == "archived":
                raise GatewayError(
                    "THREAD_ARCHIVED",
                    "Unarchive the conversation before starting a turn",
                    status_code=409,
                ) from exc
            raise GatewayError(
                "THREAD_DELETE_PENDING",
                "Conversation deletion is in progress",
                status_code=409,
            ) from exc
        except ThreadBusyError as exc:
            raise GatewayError(
                "THREAD_BUSY",
                "Wait for the active turn in this conversation to finish",
                status_code=409,
                retryable=True,
                retry_after_seconds=2,
            ) from exc
        async with self._turn_submission_lock(thread_id):
            async with self._lifecycle_gate().read():
                return await self._start_turn_locked(
                    device,
                    thread_id,
                    client_message_id,
                    input_parts,
                )

    async def _start_turn_locked(
        self,
        device: DeviceRecord,
        thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> TurnDTO:
        raw_thread_id = self._require_raw_thread_id(thread_id)
        public_parts = [dict(part) for part in input_parts]
        request_fingerprint = turn_request_fingerprint(public_parts)
        asset_refs = tuple(
            str(part["assetRef"])
            for part in public_parts
            if part.get("type") == "image"
        )
        if asset_refs and not self.assets.enabled:
            raise GatewayError(
                "CAPABILITY_UNAVAILABLE",
                "Local image input is unavailable",
                status_code=503,
            )
        try:
            reservation = self.store.reserve_turn(
                device.id,
                thread_id,
                client_message_id,
                request_fingerprint=request_fingerprint,
                asset_refs=asset_refs,
            )
        except ThreadLifecycleBlockedError as exc:
            if exc.reason == "archived":
                raise GatewayError(
                    "THREAD_ARCHIVED",
                    "Unarchive the conversation before starting a turn",
                    status_code=409,
                ) from exc
            raise GatewayError(
                "THREAD_DELETE_PENDING",
                "Conversation deletion is in progress",
                status_code=409,
            ) from exc
        except ThreadBusyError as exc:
            raise GatewayError(
                "THREAD_BUSY",
                "Wait for the active turn in this conversation to finish",
                status_code=409,
                retryable=True,
                retry_after_seconds=2,
            ) from exc
        except TurnAmbiguousError as exc:
            raise self._ambiguous_turn_error() from exc
        except TurnIdempotencyConflictError as exc:
            raise GatewayError(
                "TURN_IDEMPOTENCY_CONFLICT",
                "The clientMessageId is already bound to different input",
                status_code=409,
            ) from exc
        except (AssetBindingError, AssetUnavailableError) as exc:
            self.store.append_event(
                device.id,
                "asset.unavailable",
                {"clientMessageId": client_message_id, "reason": "binding"},
                thread_id=thread_id,
            )
            raise GatewayError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409) from exc
        self._persist_user_message(
            thread_id,
            reservation.public_id,
            client_message_id,
            public_parts,
            reservation.created_at,
        )
        if not reservation.created:
            # Relay reconnects cancel only the in-flight delivery coroutine; the
            # process-local Gateway and its SQLite store stay alive. A replay can
            # therefore observe the exact reservation left by the cancelled
            # coroutine.
            #
            # `reserved/pending` proves mark_turn_dispatching never committed, so
            # Codex has not been called and the same business key may safely
            # continue below. `dispatching` is the uncertainty boundary: Codex may
            # already have accepted the request, so fence the Thread for an
            # authoritative resync instead of returning a false ACCEPTED result or
            # submitting a second Turn.
            if (
                reservation.delivery_state == "dispatching"
                or reservation.status == "dispatching"
            ):
                marked_ambiguous = self.store.mark_turn_ambiguous(
                    reservation.public_id,
                    device_id=device.id,
                    only_if_dispatching=True,
                )
                if marked_ambiguous is None:
                    accepted = self.store.turn_by_public_id(reservation.public_id)
                    if (
                        accepted is not None
                        and accepted["raw_id"] is not None
                        and str(accepted["delivery_state"]) in {"accepted", "terminal"}
                    ):
                        return TurnDTO(
                            id=reservation.public_id,
                            thread_id=thread_id,
                            status=str(accepted["status"]),
                            client_message_id=client_message_id,
                            created_at=str(accepted["created_at"]),
                        )
                raise self._ambiguous_turn_error()
            safe_reserved_replay = (
                reservation.delivery_state == "reserved"
                and reservation.status == "pending"
                and reservation.raw_id is None
            )
            if not safe_reserved_replay:
                accepted_delivery = reservation.delivery_state in {
                    "accepted",
                    "terminal",
                } and reservation.raw_id is not None
                if accepted_delivery:
                    return TurnDTO(
                        id=reservation.public_id,
                        thread_id=thread_id,
                        status=reservation.status,
                        client_message_id=client_message_id,
                        created_at=reservation.created_at,
                    )
                # No other existing state is proof that Codex accepted or
                # rejected the Turn. Fail closed instead of manufacturing an
                # `ok=true` response that Android would persist as ACCEPTED.
                self.store.mark_turn_ambiguous(
                    reservation.public_id,
                    device_id=device.id,
                )
                raise self._ambiguous_turn_error()
        self.store.update_user_message_state(
            thread_id, client_message_id, "queued"
        )
        records = self.store.image_assets_for_refs(asset_refs)
        if asset_refs:
            try:
                if len(records) != len(asset_refs) or any(
                    record.device_id != device.id
                    or record.thread_public_id != thread_id
                    or record.client_message_id != client_message_id
                    for record in records
                ):
                    raise ImageAssetError(
                        "ASSET_UNAVAILABLE", "Image asset is unavailable", 409
                    )
                self.assets.validate_records(records, require_claimed=True)
            except ImageAssetError as exc:
                self.store.mark_turn_not_accepted(reservation.public_id)
                self.store.append_event(
                    device.id,
                    "asset.unavailable",
                    {"clientMessageId": client_message_id, "reason": "storageValidation"},
                    thread_id=thread_id,
                    turn_id=reservation.public_id,
                )
                raise GatewayError(exc.code, exc.message, exc.status_code) from exc
        internal_parts: List[Dict[str, Any]] = []
        records_by_ref = {record.asset_ref: record for record in records}
        for part in public_parts:
            if part.get("type") == "image":
                record = records_by_ref.get(str(part.get("assetRef")))
                if record is None:
                    raise GatewayError("ASSET_UNAVAILABLE", "Image asset is unavailable", 409)
                internal_parts.append(
                    {
                        "type": "localImage",
                        "path": self.assets.internal_path(record),
                        "detail": "auto",
                    }
                )
            else:
                internal_parts.append(part)
        try:
            await self.bridge.resume_thread(raw_thread_id)
        except asyncio.CancelledError:
            # Connector request deadlines cancel this coroutine while the
            # process-local Gateway remains alive. Until dispatching is
            # durably marked, Codex has not received turn/start, so release the
            # reservation instead of leaving a permanent Thread lock.
            self.store.mark_turn_not_accepted(reservation.public_id)
            self.store.update_user_message_state(
                thread_id,
                client_message_id,
                "failed",
            )
            raise
        except BridgeThreadNotFound as exc:
            if self.store.delete_missing_provisional_thread(
                device.id,
                thread_id,
                reason="provisionalThreadNotLoaded",
            ):
                raise GatewayError(
                    "THREAD_NOT_FOUND",
                    "Conversation was not found",
                    status_code=404,
                ) from exc
            self.store.mark_turn_not_accepted(reservation.public_id)
            self.store.update_user_message_state(
                thread_id, client_message_id, "failed"
            )
            raise GatewayError(
                "THREAD_NOT_FOUND",
                "Conversation was not found",
                status_code=404,
            ) from exc
        except BridgeError as exc:
            # turn/start has not been attempted, so this reservation is
            # definitely safe to reuse with the same clientMessageId.
            self.store.mark_turn_not_accepted(reservation.public_id)
            self.store.update_user_message_state(
                thread_id, client_message_id, "failed"
            )
            raise GatewayError(
                "TURN_NOT_ACCEPTED",
                "Codex did not accept the turn; retry with the same clientMessageId",
                status_code=503,
                retryable=True,
            ) from exc
        if not self.store.mark_turn_dispatching(reservation.public_id):
            self.store.mark_turn_ambiguous(
                reservation.public_id,
                device_id=device.id,
            )
            raise self._ambiguous_turn_error()
        try:
            raw_turn = await self.bridge.start_turn(
                raw_thread_id,
                client_message_id,
                internal_parts,
            )
        except asyncio.CancelledError:
            # Once dispatching is committed, cancellation cannot distinguish
            # "Codex did not see it" from "Codex accepted but the response was
            # lost". Fence only the still-dispatching row; an early accepted or
            # terminal lifecycle event wins and must never be regressed.
            self.store.mark_turn_ambiguous(
                reservation.public_id,
                device_id=device.id,
                only_if_dispatching=True,
            )
            raise
        except BridgeNotAccepted as exc:
            self.store.mark_turn_not_accepted(reservation.public_id)
            self.store.update_user_message_state(
                thread_id, client_message_id, "failed"
            )
            raise GatewayError(
                "TURN_NOT_ACCEPTED",
                "Codex did not accept the turn; retry with the same clientMessageId",
                status_code=503,
                retryable=True,
            ) from exc
        except BridgeError as exc:
            marked_ambiguous = self.store.mark_turn_ambiguous(
                reservation.public_id,
                device_id=device.id,
                only_if_dispatching=True,
            )
            if marked_ambiguous is None:
                accepted = self.store.turn_by_public_id(reservation.public_id)
                if (
                    accepted is not None
                    and accepted["raw_id"] is not None
                    and str(accepted["delivery_state"]) in {"accepted", "terminal"}
                ):
                    return TurnDTO(
                        id=reservation.public_id,
                        thread_id=thread_id,
                        status=str(accepted["status"]),
                        client_message_id=client_message_id,
                        created_at=str(accepted["created_at"]),
                    )
            raise self._ambiguous_turn_error() from exc

        try:
            completed = self.store.complete_turn_reservation(
                reservation.public_id,
                raw_turn.raw_id,
                raw_turn.status,
                device_id=device.id,
                events=[{"type": "turn.started", "payload": {}}],
            )
        except TurnAmbiguousError as exc:
            self.store.mark_turn_ambiguous(
                reservation.public_id,
                device_id=device.id,
            )
            raise self._ambiguous_turn_error() from exc
        except KeyError as exc:
            # A thread/deleted notification can win after Codex accepted the
            # request but before its response reaches this coroutine. The
            # tombstone is authoritative: never recreate the local identity or
            # retry a turn that the remote side may already be executing.
            raise GatewayError(
                "TURN_ACCEPTED_THREAD_DELETED",
                "Codex accepted the turn after the conversation was deleted",
                status_code=409,
                retryable=False,
            ) from exc
        return TurnDTO(
            id=completed.public_id,
            thread_id=thread_id,
            status=completed.status,
            client_message_id=client_message_id,
            created_at=completed.created_at,
        )

    async def interrupt_turn(
        self,
        device: DeviceRecord,
        thread_id: str,
        turn_id: str,
    ) -> None:
        self._require_instance_lock()
        if not self.store.thread_accepts_events(thread_id):
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        raw_thread_id = self._require_raw_thread_id(thread_id)
        row = self.store.turn_by_public_id(turn_id)
        raw_turn_id = self.store.turn_raw_id(turn_id)
        if row is None or row["thread_public_id"] != thread_id or raw_turn_id is None:
            raise GatewayError("TURN_NOT_FOUND", "Turn was not found", 404)
        try:
            await self.bridge.interrupt_turn(raw_thread_id, raw_turn_id)
        except BridgeError as exc:
            raise GatewayError(
                "CODEX_UNAVAILABLE",
                "Turn could not be interrupted",
                status_code=503,
                retryable=True,
            ) from exc
        updated = self.store.apply_thread_turn_events(
            device.id,
            thread_id,
            events=[
                {
                    "type": "turn.interrupted",
                    "payload": {"status": "interrupted"},
                    "turn_id": turn_id,
                }
            ],
            thread_status=ThreadStatus.IDLE.value,
            turn_id=turn_id,
            turn_status="interrupted",
        )
        if updated is None:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        if self.assets.enabled:
            await asyncio.to_thread(self.assets.cleanup)

    async def resolve_approval(
        self,
        device: DeviceRecord,
        approval_id: str,
        action_token: str,
        decision: ApprovalDecision,
    ) -> ApprovalDTO:
        self._require_instance_lock()
        existing = self.store.approval_by_public_id(approval_id)
        resolved_payload: Dict[str, Any] = {}
        if existing is not None:
            resolved_payload = self._approval_dto(
                existing,
                action_token=None,
            ).model_copy(
                update={
                    "state": (
                        "approved"
                        if decision == ApprovalDecision.APPROVE
                        else "rejected"
                    )
                }
            ).model_dump(by_alias=True, exclude_none=True)
        row, outcome = self.store.resolve_approval_once(
            approval_id,
            action_token,
            decision.value,
            device_id=device.id,
            events=[
                {"type": "approval.resolved", "payload": resolved_payload},
                {
                    "type": "audit.action",
                    "payload": {
                        "action": "approval.decision",
                        "approvalId": approval_id,
                        "decision": decision.value,
                    },
                },
            ],
        )
        if outcome == "notFound":
            raise GatewayError("APPROVAL_NOT_FOUND", "Approval was not found", 404)
        if outcome == "invalidToken":
            raise GatewayError("APPROVAL_DENIED", "Approval token is not valid", 403)
        if outcome == "invalidDecision":
            raise GatewayError("APPROVAL_DECISION_INVALID", "Decision is not allowed", 400)
        if outcome == "expired":
            await self.sweep_expired_approvals()
            raise GatewayError("APPROVAL_EXPIRED", "Approval has expired", 410)
        if outcome == "alreadyResolved":
            raise GatewayError(
                "APPROVAL_ALREADY_RESOLVED",
                "Approval has already been resolved",
                409,
            )
        if row is None:
            raise GatewayError("APPROVAL_NOT_FOUND", "Approval was not found", 404)
        request_id = json.loads(row["raw_request_id"])
        try:
            await self.bridge.resolve_approval(request_id, decision.value)
        except BridgeError as exc:
            # The durable state remains terminal: never replay an approval decision
            # automatically because the side effect may already have been accepted.
            raise GatewayError(
                "APPROVAL_DELIVERY_UNKNOWN",
                "Approval state is final but delivery could not be confirmed",
                status_code=503,
                retryable=False,
            ) from exc
        approval = self._approval_dto(row, action_token=None)
        return approval

    async def handle_bridge_event(self, event: BridgeEvent) -> None:
        if not self._owns_instance_lock():
            return
        device = self.store.active_device()
        if device is None:
            return
        params = event.params
        raw_thread_id = params.get("threadId")
        if raw_thread_id is None and isinstance(params.get("thread"), dict):
            raw_thread_id = params["thread"].get("id")
        if not raw_thread_id:
            return
        raw_thread_id = str(raw_thread_id)
        if event.method == "thread/deleted":
            self.store.delete_threads_by_raw_ids(
                device.id,
                [raw_thread_id],
                reason="codexDeleteNotification",
            )
            if self.assets.enabled:
                await asyncio.to_thread(self.assets.cleanup)
            return

        thread_id = self.store.thread_public_id(raw_thread_id)
        if thread_id is None:
            if event.method in APPROVAL_REQUEST_METHODS and event.request_id is not None:
                with contextlib.suppress(BridgeError):
                    await self.bridge.resolve_approval(event.request_id, "reject")
            return
        if event.method in {"thread/archived", "thread/unarchived"}:
            self.store.apply_thread_turn_events(
                device.id,
                thread_id,
                events=[
                    {
                        "type": "thread.updated",
                        "payload": {
                            "status": (
                                ThreadStatus.ARCHIVED.value
                                if event.method == "thread/archived"
                                else ThreadStatus.IDLE.value
                            )
                        },
                    }
                ],
                thread_archived=(event.method == "thread/archived"),
            )
            return
        if event.method in APPROVAL_REQUEST_METHODS and event.request_id is not None:
            try:
                await self._handle_approval_event(device, event)
            except ThreadLifecycleBlockedError:
                with contextlib.suppress(BridgeError):
                    await self.bridge.resolve_approval(event.request_id, "reject")
            return
        if not self.store.thread_accepts_events(thread_id):
            return
        try:
            await self._handle_bridge_event_guarded(event)
        except ThreadLifecycleBlockedError:
            # A lifecycle transaction won the race after the lock-free reader
            # admitted this event. Store guards make the late write a no-op.
            return

    async def _handle_bridge_event_guarded(self, event: BridgeEvent) -> None:
        device = self.store.active_device()
        if device is None:
            return

        params = event.params
        raw_thread_id = params.get("threadId")
        if raw_thread_id is None and isinstance(params.get("thread"), dict):
            raw_thread_id = params["thread"].get("id")
        if not raw_thread_id:
            return
        thread_id = self.store.thread_public_id(str(raw_thread_id))
        if thread_id is None:
            return

        raw_turn_id = params.get("turnId")
        if raw_turn_id is None and isinstance(params.get("turn"), dict):
            raw_turn_id = params["turn"].get("id")
        turn_id = None
        bound_pending_turn = False
        if raw_turn_id:
            turn_id = self.store.turn_public_id(str(raw_turn_id))
            if turn_id is None and self._event_proves_turn_lifecycle(event.method):
                turn_value = params.get("turn")
                event_client_message_id = None
                if isinstance(turn_value, dict):
                    event_client_message_id = next(
                        (
                            str(turn_value[key])
                            for key in (
                                "clientId",
                                "clientUserMessageId",
                                "clientMessageId",
                            )
                            if turn_value.get(key)
                        ),
                        None,
                    )
                # A start notification is authoritative proof for the one
                # dispatching reservation. Other unknown lifecycle events need
                # the client id before they may claim it; otherwise a stale or
                # foreign terminal event could release the wrong Thread.
                if event.method == "turn/started" or event_client_message_id is not None:
                    turn_id = self.store.bind_pending_turn(
                        thread_id,
                        str(raw_turn_id),
                        event_client_message_id,
                        device_id=device.id,
                        emit_started_event=True,
                    )
                    bound_pending_turn = turn_id is not None
                if turn_id is None:
                    # Unknown lifecycle identities are not safe public Turns.
                    # They may be stale, foreign, or externally initiated; an
                    # authoritative history sync must establish them first.
                    turn_id = self.store.mapped_public_id(
                        "turn",
                        str(raw_turn_id),
                        thread_id,
                    )
                    if turn_id is None:
                        return
        turn_row = (
            self.store.turn_by_public_id(turn_id)
            if turn_id is not None
            else None
        )
        if (
            turn_row is not None
            and str(turn_row["thread_public_id"]) != thread_id
        ):
            return
        if event.method == "turn/completed" and turn_row is None:
            # Only a canonical Turn row may produce a terminal lifecycle event.
            # External or stale raw ids remain private until authoritative
            # history reconciliation establishes their public identity.
            return
        if (
            event.method == "turn/started"
            and turn_row is not None
            and (
                bound_pending_turn
                or str(turn_row["delivery_state"]) in {"accepted", "terminal"}
            )
        ):
            # The normal response path and the early-notification path each
            # commit acceptance and the canonical start event atomically. Any
            # later notification is only a transport duplicate.
            return
        if (
            event.method == "turn/completed"
            and turn_row is not None
            and str(turn_row["delivery_state"]) == "terminal"
        ):
            return

        raw_item_id = params.get("itemId")
        if raw_item_id is None and isinstance(params.get("item"), dict):
            raw_item_id = params["item"].get("id")
        item_id = None
        if raw_item_id:
            item_id = self.store.map_raw_id(
                "item", str(raw_item_id), "item", thread_id
            )

        if event.method == "item/agentMessage/delta" and turn_id and item_id:
            self._ingest_agent_delta(
                device,
                thread_id,
                turn_id,
                item_id,
                str(raw_turn_id),
                str(params.get("delta") or ""),
            )
            return

        if event.method == "item/completed" and turn_id and item_id:
            item = params.get("item")
            if isinstance(item, dict):
                self._ingest_projection_item(
                    device=device,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    raw_turn_id=str(raw_turn_id),
                    item_id=item_id,
                    item=item,
                )
                return

        if event.method == "item/started" and turn_id and item_id:
            item = params.get("item")
            if isinstance(item, dict):
                # Started Items are not authoritative text boundaries. Retain
                # only type/status so a partial secret cannot cross the public
                # stream before item/completed supplies the full redactable item.
                self._ingest_projection_item(
                    device=device,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    raw_turn_id=str(raw_turn_id),
                    item_id=item_id,
                    item={
                        "type": str(item.get("type") or "unknown"),
                        "status": str(item.get("status") or "inProgress"),
                    },
                )
                return

        if event.method == "turn/plan/updated" and turn_id:
            plan = params.get("plan") or []
            plan_raw_id = "panel-plan:%s" % raw_turn_id
            plan_item_id = self.store.map_raw_id(
                "item", plan_raw_id, "item", thread_id
            )
            self._ingest_projection_item(
                device=device,
                thread_id=thread_id,
                turn_id=turn_id,
                raw_turn_id=str(raw_turn_id),
                item_id=plan_item_id,
                item={
                    "type": "plan",
                    "status": "updated",
                    "text": "Plan updated with %d steps." % len(plan),
                },
            )
            return

        if (
            turn_id
            and item_id
            and event.method.startswith("item/")
            and any(
                marker in event.method
                for marker in ("commandExecution", "fileChange", "mcpToolCall", "dynamicTool")
            )
        ):
            item_type = event.method.split("/", 2)[1]
            self._ingest_projection_item(
                device=device,
                thread_id=thread_id,
                turn_id=turn_id,
                raw_turn_id=str(raw_turn_id),
                item_id=item_id,
                item={"type": item_type, "status": "inProgress"},
            )
            return

        event_type = self._event_type(event.method, params)
        event_record = {
            "type": event_type,
            "payload": self._safe_event_payload(event.method, params),
            "turn_id": turn_id,
            "item_id": item_id,
        }
        if event.method == "thread/status/changed":
            status = self._public_status(params.get("status"))
            self.store.apply_thread_turn_events(
                device.id,
                thread_id,
                events=[event_record],
                thread_status=status.value,
            )
            return
        elif event.method == "turn/started" and turn_id:
            self.store.apply_thread_turn_events(
                device.id,
                thread_id,
                events=[event_record],
                thread_status=ThreadStatus.RUNNING.value,
                last_turn_id=turn_id,
            )
            return
        elif event.method == "turn/completed" and turn_id:
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "completed")
            thread_status = (
                ThreadStatus.FAILED.value if status == "failed" else ThreadStatus.IDLE.value
            )
            authoritative_item_ids: List[str] = []
            for ordinal, item in enumerate(turn.get("items") or []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                if str(item.get("type") or "") == "userMessage":
                    continue
                completed_item_id = self.store.map_raw_id(
                    "item", str(item["id"]), "item", thread_id
                )
                authoritative_item_ids.append(completed_item_id)
                self._upsert_projection_item(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    raw_turn_id=str(raw_turn_id),
                    item_id=completed_item_id,
                    item=item,
                    ordinal=ordinal,
                )
            # Codex app-server 0.144.x emits live ``item/completed`` events with
            # the full answer, then a terminal ``turn/completed`` whose
            # ``items`` field can be an empty compatibility placeholder.  An
            # empty list is therefore not an authoritative instruction to
            # delete the already-ingested items.  A non-empty terminal list is
            # still authoritative and prunes synthetic/stale projections.
            if isinstance(turn.get("items"), list) and turn["items"]:
                self.store.prune_projection_items(turn_id, authoritative_item_ids)
            self._sync_live_work_panel(
                device=device,
                thread_id=thread_id,
                turn_id=turn_id,
                raw_turn_id=str(raw_turn_id),
                turn_status=status,
                emit_events=True,
                updated_at=self._timestamp_value(turn.get("completedAt")) or utc_now(),
            )
            self.store.apply_thread_turn_events(
                device.id,
                thread_id,
                events=[event_record],
                thread_status=thread_status,
                turn_id=turn_id,
                turn_status=status,
            )
            if self.assets.enabled:
                await asyncio.to_thread(self.assets.cleanup)
            return
        self.store.append_event(
            device.id,
            event_type,
            self._safe_event_payload(event.method, params),
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
        )

    async def _handle_approval_event(
        self, device: DeviceRecord, event: BridgeEvent
    ) -> None:
        params = event.params
        raw_thread_id = str(params.get("threadId") or "")
        raw_turn_id = str(params.get("turnId") or "")
        raw_item_id = str(params.get("itemId") or "")
        if not raw_thread_id or not raw_turn_id or not raw_item_id:
            return
        thread_id = self.store.thread_public_id(raw_thread_id)
        if thread_id is None:
            with contextlib.suppress(BridgeError):
                await self.bridge.resolve_approval(event.request_id, "reject")
            return
        turn_id = self.store.turn_public_id(raw_turn_id) or self.store.bind_pending_turn(
            thread_id,
            raw_turn_id,
            device_id=device.id,
            emit_started_event=True,
        )
        if turn_id is None:
            with contextlib.suppress(BridgeError):
                await self.bridge.resolve_approval(event.request_id, "reject")
            return
        turn_row = self.store.turn_by_public_id(turn_id)
        if (
            turn_row is None
            or str(turn_row["thread_public_id"]) != thread_id
            or str(turn_row["delivery_state"]) == "terminal"
        ):
            with contextlib.suppress(BridgeError):
                await self.bridge.resolve_approval(event.request_id, "reject")
            return
        item_id = self.store.map_raw_id("item", raw_item_id, "item", thread_id)
        kind = "command" if "commandExecution" in event.method else "fileChange"
        summary, reason = self._approval_public_fields(kind, params)
        approval_id = public_id("approval")
        action_token = self._approval_action_token(approval_id)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z")
        raw_request_id = json.dumps(
            event.request_id, separators=(",", ":"), sort_keys=True
        )
        approval_payload = ApprovalDTO(
            approval_id=approval_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            kind=kind,
            summary=summary,
            reason=reason,
            decisions=["approve", "reject"],
            state="pending",
            expires_at=expires_at,
            action_token=action_token,
        ).model_dump(by_alias=True, exclude_none=True)
        row, created = self.store.create_approval(
            approval_id=approval_id,
            raw_request_id=raw_request_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            kind=kind,
            summary=summary,
            reason=reason,
            decisions=["approve", "reject"],
            action_token=action_token,
            expires_at=expires_at,
            device_id=device.id,
            events=[{"type": "approval.requested", "payload": approval_payload}],
        )
        if not created:
            return

    def _ingest_agent_delta(
        self,
        device: DeviceRecord,
        thread_id: str,
        turn_id: str,
        item_id: str,
        raw_turn_id: str,
        delta: str,
    ) -> None:
        # A delta is not an authoritative content boundary. Publishing or even
        # persisting raw fragments can leak a secret split across notifications
        # before the full item can be redacted. It may only materialize the
        # content-free live panel; text arrives through item/completed.
        del delta
        existing = self.store.projection_item(item_id)
        if existing is None:
            self.store.upsert_projection_item(
                message_id=self._panel_ids(thread_id, raw_turn_id)[0],
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                item_type="agentMessage",
                status="inProgress",
                phase=None,
                text="",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        # Always verify materialization. If the process previously stopped after
        # the private ledger commit, an identical replay must repair the panel.
        self._sync_live_work_panel(
            device=device,
            thread_id=thread_id,
            turn_id=turn_id,
            raw_turn_id=raw_turn_id,
            turn_status=self._current_turn_status(turn_id),
            emit_events=True,
        )

    def _ingest_projection_item(
        self,
        *,
        device: DeviceRecord,
        thread_id: str,
        turn_id: str,
        raw_turn_id: str,
        item_id: str,
        item: Dict[str, Any],
    ) -> None:
        self._upsert_projection_item(
            thread_id=thread_id,
            turn_id=turn_id,
            raw_turn_id=raw_turn_id,
            item_id=item_id,
            item=item,
        )
        # The ledger write and the snapshot/event transaction are intentionally
        # replay-repairable: even a semantic duplicate must verify that the panel
        # exists and matches the ledger after a crash between the two commits.
        self._sync_live_work_panel(
            device=device,
            thread_id=thread_id,
            turn_id=turn_id,
            raw_turn_id=raw_turn_id,
            turn_status=self._current_turn_status(turn_id),
            emit_events=True,
        )

    def _upsert_projection_item(
        self,
        *,
        thread_id: str,
        turn_id: str,
        raw_turn_id: str,
        item_id: str,
        item: Dict[str, Any],
        ordinal: Optional[int] = None,
    ) -> bool:
        item_type = str(item.get("type") or "unknown")
        existing = self.store.projection_item(item_id)
        text = self._completed_item_text(item)
        if not text and existing is not None:
            text = str(existing["text"] or "")
        text, _ = self._bounded_public_text(text)
        phase = item.get("phase")
        if phase not in {"commentary", "final_answer"}:
            phase = None
        changes = item.get("changes")
        evidence = self._projection_evidence(item_type, item)
        return self.store.upsert_projection_item(
            message_id=self._panel_ids(thread_id, raw_turn_id)[0],
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            item_type=item_type,
            status=(str(item["status"]) if item.get("status") is not None else None),
            phase=phase,
            text=text,
            duration_ms=(
                int(item["durationMs"])
                if isinstance(item.get("durationMs"), int)
                else None
            ),
            exit_code=(
                int(item["exitCode"])
                if isinstance(item.get("exitCode"), int)
                else None
            ),
            change_count=(len(changes) if isinstance(changes, list) else 0),
            evidence=evidence,
            ordinal=ordinal,
            created_at=self._timestamp_value(item.get("createdAt")),
            updated_at=self._timestamp_value(item.get("updatedAt")) or utc_now(),
        )

    def _sync_live_work_panel(
        self,
        *,
        device: Optional[DeviceRecord],
        thread_id: str,
        turn_id: str,
        raw_turn_id: str,
        turn_status: str,
        emit_events: bool,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        sort_order: int = ASSISTANT_MESSAGE_SORT_ORDER,
    ) -> Dict[str, Any]:
        message_id, source_item_id = self._panel_ids(thread_id, raw_turn_id)
        existing = self.store.message_snapshot(message_id)
        now = updated_at or utc_now()
        born = (
            str(existing.get("createdAt"))
            if existing is not None
            else created_at or self._turn_started_at(turn_id) or now
        )
        items = [
            self._projection_item_from_row(row)
            for row in self.store.list_projection_items(turn_id)
        ]
        target = project_live_work_panel(
            message_id=message_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_item_id=source_item_id,
            turn_status=turn_status,
            items=items,
            created_at=born,
            updated_at=now,
            revision=(int(existing["revision"]) if existing else 0),
        )
        if existing is not None and semantically_equal(existing, target):
            self.store.save_message_snapshot(
                message_id,
                thread_id,
                turn_id,
                int(existing["revision"]),
                existing,
                sort_order=sort_order,
                replace_assistant_turn=True,
            )
            return existing
        events: List[Dict[str, Any]] = []
        if existing is None:
            target["revision"] = 0
            if emit_events:
                events.append(
                    {
                        "type": "message.snapshot",
                        "payload": target,
                        "item_id": source_item_id,
                    }
                )
        else:
            base_revision = int(existing["revision"])
            target["revision"] = base_revision + 1
            ops = patch_ops(existing, target)
            if not ops:
                return existing
            if emit_events:
                events.append(
                    {
                        "type": "message.patch",
                        "payload": {
                            "messageId": message_id,
                            "baseRevision": base_revision,
                            "nextRevision": base_revision + 1,
                            "ops": ops,
                        },
                        "item_id": source_item_id,
                    }
                )
        preview = str(target.get("fallback", {}).get("text") or "")[-240:]
        if emit_events:
            if device is None:
                raise RuntimeError("device is required when emitting projection events")
            self.store.save_message_snapshot_with_events(
                device.id,
                message_id,
                thread_id,
                turn_id,
                int(target["revision"]),
                target,
                events,
                sort_order=sort_order,
                thread_preview=preview,
                replace_assistant_turn=True,
            )
        else:
            self.store.save_message_snapshot(
                message_id,
                thread_id,
                turn_id,
                int(target["revision"]),
                target,
                sort_order=sort_order,
                replace_assistant_turn=True,
            )
        return target

    def _operation_outcome_unknown_message(
        self,
        *,
        thread_id: str,
        turn_id: str,
        raw_turn_id: str,
    ) -> Dict[str, Any]:
        message_id, source_item_id = self._panel_ids(thread_id, raw_turn_id)
        existing = self.store.message_snapshot(message_id)
        now = utc_now()
        items = [
            self._projection_item_from_row(row)
            for row in self.store.list_projection_items(turn_id)
        ]
        items.append(
            ProjectionItem(
                item_id="item_outcome_unknown_%s"
                % hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24],
                ordinal=max((item.ordinal for item in items), default=0) + 1,
                type="agentMessage",
                status="failed",
                phase="final_answer",
                text=(
                    "操作结果无法确认（OPERATION_OUTCOME_UNKNOWN）。"
                    "该消息已被 Codex 接收，不会自动重发；"
                    "请检查实际产物后再决定是否继续。"
                ),
                created_at=now,
                updated_at=now,
            )
        )
        return project_live_work_panel(
            message_id=message_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_item_id=source_item_id,
            turn_status="failed",
            items=items,
            created_at=(
                str(existing.get("createdAt"))
                if existing is not None
                else self._turn_started_at(turn_id) or now
            ),
            updated_at=now,
            revision=(int(existing["revision"]) + 1 if existing else 0),
        )

    @staticmethod
    def _completed_item_text(item: Dict[str, Any]) -> str:
        direct = item.get("text")
        if isinstance(direct, str):
            return direct
        message = item.get("message")
        if isinstance(message, str):
            return message
        if isinstance(message, dict) and isinstance(message.get("text"), str):
            return str(message["text"])
        content = item.get("content")
        if isinstance(content, list):
            return "".join(
                part
                if isinstance(part, str)
                else str(part.get("text") or "")
                for part in content
                if isinstance(part, (str, dict))
            )
        return ""

    @classmethod
    def _projection_item_from_row(
        cls,
        row: Dict[str, Any],
    ) -> ProjectionItem:
        return ProjectionItem(
            item_id=str(row["item_public_id"]),
            ordinal=int(row["ordinal"]),
            type=str(row["item_type"]),
            status=(str(row["status"]) if row["status"] is not None else None),
            phase=(str(row["phase"]) if row["phase"] is not None else None),
            text=str(row["text"] or ""),
            duration_ms=row["duration_ms"],
            exit_code=row["exit_code"],
            change_count=int(row["change_count"]),
            evidence=cls._projection_evidence_from_row(row),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _projection_evidence_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
        try:
            value = json.loads(str(row.get("evidence_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _projection_evidence(
        cls,
        item_type: str,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Build a bounded, redacted evidence object from explicit item fields.

        Arbitrary terminal output, command strings, and MCP result payloads are
        deliberately ignored. Test cards require a structured ``testEvidence``
        object or an explicit test item type; prose such as "12 tests passed"
        is never parsed.
        """

        normalized = item_type.lower().replace("-", "").replace("_", "")
        duration_ms = cls._nonnegative_evidence_count(
            item.get("durationMs"),
            maximum=86_400_000,
        )
        if item_type == "commandExecution":
            status = str(item.get("status") or "updated")
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                detail = "Command finished with exit code %d" % exit_code
            else:
                detail = "Command %s" % status
            return {
                "kind": "tool",
                "label": "Command",
                "detail": cls._safe_evidence_value(detail, "Command updated", 1_024),
                **({"durationMs": duration_ms} if duration_ms is not None else {}),
            }
        if "tool" in normalized or normalized in {
            "mcpservercall",
            "dynamiccall",
        }:
            candidate = next(
                (
                    item.get(key)
                    for key in ("toolName", "tool", "name", "title", "serverName", "server")
                    if isinstance(item.get(key), str) and str(item.get(key)).strip()
                ),
                None,
            )
            label = cls._safe_evidence_value(candidate, "Tool", 256)
            if any(
                marker in label
                for marker in ("[PATH]", "[INTERNAL_ID]", "[REDACTED]", "[INTERNAL_URL]")
            ):
                label = "Tool"
            status = str(item.get("status") or "updated")
            return {
                "kind": "tool",
                "label": label,
                "detail": cls._safe_evidence_value(
                    "Tool call %s" % status,
                    "Tool call updated",
                    1_024,
                ),
                **({"durationMs": duration_ms} if duration_ms is not None else {}),
            }
        if item_type == "fileChange":
            changes = item.get("changes")
            structured_changes = [
                change for change in changes or [] if isinstance(change, dict)
            ] if isinstance(changes, list) else []
            first = structured_changes[0] if structured_changes else {}
            file_source = next(
                (
                    value
                    for value in (
                        item.get("fileLabel"),
                        first.get("fileLabel"),
                        first.get("path"),
                    )
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
            file_label = cls._safe_file_label(
                file_source,
                (
                    "%d changed files" % len(structured_changes)
                    if len(structured_changes) > 1
                    else "Changed file"
                ),
            )
            additions = cls._explicit_change_total(
                item.get("additions"),
                structured_changes,
                "additions",
            )
            deletions = cls._explicit_change_total(
                item.get("deletions"),
                structured_changes,
                "deletions",
            )
            summary_source = item.get("summary") or first.get("summary")
            if isinstance(summary_source, str) and summary_source.strip():
                summary = cls._safe_evidence_value(
                    summary_source,
                    "File changes updated",
                    1_024,
                )
            elif additions is not None or deletions is not None:
                summary = "+%d -%d" % (additions or 0, deletions or 0)
            else:
                count = max(1, len(structured_changes))
                summary = "%d file change%s" % (count, "" if count == 1 else "s")
            evidence: Dict[str, Any] = {
                "kind": "diff",
                "fileLabel": file_label,
                "summary": summary,
            }
            if additions is not None:
                evidence["additions"] = additions
            if deletions is not None:
                evidence["deletions"] = deletions
            preview_source = item.get("preview") or first.get("preview")
            if isinstance(preview_source, str) and preview_source:
                preview, truncated = bounded_text(
                    cls._redact_sensitive(preview_source),
                    2_048,
                )
                evidence["preview"] = preview
                evidence["truncated"] = bool(
                    truncated
                    or item.get("truncated") is True
                    or first.get("truncated") is True
                )
            return evidence

        structured_test = item.get("testEvidence")
        explicit_test_type = normalized in {
            "test",
            "testresult",
            "testsummary",
            "testexecution",
        }
        if isinstance(structured_test, dict):
            source = structured_test
        elif explicit_test_type:
            source = item
        else:
            return {}
        counts = {
            key: cls._nonnegative_evidence_count(source.get(key))
            for key in ("passed", "failed", "skipped")
        }
        if not explicit_test_type and not any(value is not None for value in counts.values()):
            return {}
        title = cls._safe_evidence_value(source.get("title"), "Tests", 256)
        summary_source = source.get("summary")
        if isinstance(summary_source, str) and summary_source.strip():
            summary = cls._safe_evidence_value(
                summary_source,
                "Structured test evidence updated",
                1_024,
            )
        else:
            parts = [
                "%d %s" % (value, key)
                for key, value in counts.items()
                if value is not None
            ]
            summary = ", ".join(parts) or "Structured test evidence updated"
        evidence = {
            "kind": "test",
            "title": title,
            "summary": summary,
            "status": str(source.get("status") or item.get("status") or "completed"),
        }
        evidence.update(
            {key: value for key, value in counts.items() if value is not None}
        )
        test_duration = cls._nonnegative_evidence_count(
            source.get("durationMs", duration_ms),
            maximum=86_400_000,
        )
        if test_duration is not None:
            evidence["durationMs"] = test_duration
        return evidence

    @classmethod
    def _safe_evidence_value(
        cls,
        value: Any,
        fallback: str,
        limit: int,
    ) -> str:
        source = str(value) if isinstance(value, str) and value.strip() else fallback
        return bounded_text(cls._redact_sensitive(source.strip()), limit)[0]

    @classmethod
    def _safe_file_label(cls, value: Any, fallback: str) -> str:
        if not isinstance(value, str) or not value.strip():
            return fallback
        candidate = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        label = cls._safe_evidence_value(candidate, fallback, 256)
        if any(marker in label for marker in ("[PATH]", "[REDACTED]", "[INTERNAL_ID]")):
            return fallback
        return label

    @staticmethod
    def _nonnegative_evidence_count(
        value: Any,
        maximum: int = 1_000_000,
    ) -> Optional[int]:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > maximum
        ):
            return None
        return value

    @classmethod
    def _explicit_change_total(
        cls,
        direct: Any,
        changes: List[Dict[str, Any]],
        field: str,
    ) -> Optional[int]:
        direct_count = cls._nonnegative_evidence_count(direct)
        if direct_count is not None:
            return direct_count
        values = [
            cls._nonnegative_evidence_count(change.get(field))
            for change in changes
        ]
        if not values or any(value is None for value in values):
            return None
        total = sum(value for value in values if value is not None)
        return total if total <= 1_000_000 else None

    def _panel_ids(self, thread_id: str, raw_turn_id: str) -> Tuple[str, str]:
        stable_raw_key = "live-panel:%s" % raw_turn_id
        return (
            self.store.map_raw_id("message", stable_raw_key, "msg", thread_id),
            self.store.map_raw_id("item", stable_raw_key, "item", thread_id),
        )

    def _current_turn_status(self, turn_id: str) -> str:
        row = self.store.turn_by_public_id(turn_id)
        return str(row["status"] or "inProgress") if row is not None else "inProgress"

    def _turn_started_at(self, turn_id: str) -> Optional[str]:
        row = self.store.turn_by_public_id(turn_id)
        return str(row["created_at"]) if row is not None else None

    @staticmethod
    def _timestamp_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _persist_user_message(
        self,
        thread_id: str,
        turn_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
        created_at: str,
    ) -> Dict[str, Any]:
        suffix = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24]
        message_id = "msg_user_%s" % suffix
        item_id = "item_user_%s" % suffix
        text, truncated = self._bounded_public_text(
            self._input_parts_text(input_parts) or "User message"
        )
        snapshot = UserHistoryMessageSnapshot(
            message_id=message_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_item_id=item_id,
            state="queued",
            root_block_ids=["text"],
            blocks={
                "text": {
                    "type": "text",
                    "blockId": "text",
                    "text": text,
                    "fallbackText": text,
                    **({"truncated": True} if truncated else {}),
                }
            },
            fallback={"text": text},
            created_at=created_at,
            updated_at=created_at,
            client_message_id=client_message_id,
        ).model_dump(by_alias=True)
        return self.store.save_user_message_once(
            message_id,
            thread_id,
            turn_id,
            client_message_id,
            snapshot,
            sort_order=self.USER_MESSAGE_SORT_ORDER_BASE,
        )

    def _project_bridge_history(
        self,
        thread_id: str,
        turns: Any,
    ) -> bool:
        for turn_index, turn in enumerate(turns or ()):
            if not isinstance(turn, BridgeTurn):
                continue
            turn_id = self.store.turn_public_id(turn.raw_id)
            if turn_id is None and turn.client_message_id:
                turn_id = self.store.reconcile_ambiguous_turn_from_history(
                    thread_id,
                    turn.raw_id,
                    turn.client_message_id,
                    turn.status,
                )
            if turn_id is None:
                turn_id = self.store.map_raw_id(
                    "turn",
                    turn.raw_id,
                    "turn",
                    thread_id,
                )
            local_turn = self.store.turn_by_public_id(turn_id)
            if (
                local_turn is not None
                and str(local_turn["delivery_state"]) == "terminal"
                and str(local_turn["recovery_reason"]) == "operationOutcomeUnknown"
                and str(turn.status)
                in {"inProgress", "running", "waitingApproval", "waitingUser"}
            ):
                # A restarted app-server can replay the last active history item
                # even though that execution no longer exists. Preserve the
                # explicit local terminal rather than reverting the panel to
                # streaming on every Thread read.
                continue
            seen_client_ids: set[str] = set()
            projected_item_ids: List[str] = []
            user_items = [item for item in turn.items if item.type == "userMessage"]
            for item_index, item in enumerate(turn.items):
                if not isinstance(item, BridgeItem):
                    continue
                item_type = item.type
                item_id = self.store.map_raw_id(
                    "item",
                    item.raw_id,
                    "item",
                    thread_id,
                )
                created_at = (
                    item.created_at
                    or turn.created_at
                    or item.updated_at
                    or turn.updated_at
                    or utc_now()
                )
                text = self._bridge_item_text(item)
                if item_type == "userMessage":
                    candidate = item.client_message_id or (
                        turn.client_message_id
                        if len(user_items) == 1
                        else None
                    )
                    if not candidate or candidate in seen_client_ids:
                        identity = "%s:%s:%d" % (
                            turn.raw_id,
                            item.raw_id,
                            item.ordinal if item.ordinal is not None else item_index,
                        )
                        candidate = "history-%s" % hashlib.sha256(
                            identity.encode("utf-8")
                        ).hexdigest()[:24]
                    client_message_id = candidate
                    seen_client_ids.add(client_message_id)
                    existing_user = self.store.user_message_by_client_id(
                        thread_id,
                        client_message_id,
                    )
                    if existing_user is not None:
                        self.store.update_user_message_sort_order(
                            thread_id,
                            client_message_id,
                            item.ordinal if item.ordinal is not None else item_index,
                        )
                        if existing_user.get("state") != "completed":
                            self.store.update_user_message_state(
                                thread_id,
                                client_message_id,
                                "completed",
                            )
                        continue
                    message_id = self.store.map_raw_id(
                        "message",
                        item.raw_id,
                        "msg",
                        thread_id,
                    )
                    user_text, truncated = self._bounded_public_text(
                        text or "User message"
                    )
                    snapshot = UserHistoryMessageSnapshot(
                        message_id=message_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        source_item_id=item_id,
                        root_block_ids=["text"],
                        blocks={
                            "text": {
                                "type": "text",
                                "blockId": "text",
                                "text": user_text,
                                "fallbackText": user_text,
                                **({"truncated": True} if truncated else {}),
                            }
                        },
                        fallback={"text": user_text},
                        created_at=created_at,
                        updated_at=item.updated_at or turn.updated_at or created_at,
                        client_message_id=client_message_id,
                    ).model_dump(by_alias=True)
                    self.store.save_user_message_once(
                        message_id,
                        thread_id,
                        turn_id,
                        client_message_id,
                        snapshot,
                        sort_order=(
                            item.ordinal if item.ordinal is not None else item_index
                        ),
                    )
                    continue
                projected_item_ids.append(item_id)
                self.store.upsert_projection_item(
                    message_id=self._panel_ids(thread_id, turn.raw_id)[0],
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    item_type=item_type,
                    status=item.status,
                    phase=item.phase,
                    text=self._bounded_public_text(text)[0],
                    duration_ms=item.duration_ms,
                    exit_code=item.exit_code,
                    change_count=item.change_count,
                    evidence=self._projection_evidence(
                        item_type,
                        {
                            "type": item_type,
                            "status": item.status,
                            "durationMs": item.duration_ms,
                            **item.evidence_source,
                        },
                    ),
                    ordinal=item.ordinal if item.ordinal is not None else item_index,
                    created_at=item.created_at or turn.created_at,
                    updated_at=item.updated_at or turn.updated_at or created_at,
                )
            if str(turn.status) in {
                "completed",
                "failed",
                "interrupted",
                "cancelled",
                "canceled",
            }:
                self.store.prune_projection_items(turn_id, projected_item_ids)
            self._sync_live_work_panel(
                device=None,
                thread_id=thread_id,
                turn_id=turn_id,
                raw_turn_id=turn.raw_id,
                turn_status=turn.status,
                emit_events=False,
                created_at=turn.created_at,
                updated_at=turn.updated_at or turn.created_at or utc_now(),
                sort_order=self.ASSISTANT_MESSAGE_SORT_ORDER,
            )
        return not self.store.thread_has_ambiguous_turns(thread_id)

    @staticmethod
    def _event_proves_turn_lifecycle(method: str) -> bool:
        return method in {"turn/started", "turn/completed"} or method.startswith("item/")

    @staticmethod
    def _input_parts_text(input_parts: List[Dict[str, Any]]) -> str:
        text = "\n".join(
            str(part["text"])
            for part in input_parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        image_count = sum(
            1 for part in input_parts if isinstance(part, dict) and part.get("type") == "image"
        )
        marker = "[图片 × %d]" % image_count if image_count else ""
        return "\n".join(value for value in (text, marker) if value)

    @classmethod
    def _bridge_item_text(cls, item: BridgeItem) -> str:
        content = item.content
        text = item.text or ""
        if isinstance(content, str):
            text = text or content
        if isinstance(content, dict):
            if not text and isinstance(content.get("text"), str):
                text = str(content["text"])
            content = content.get("content")
        if isinstance(content, list):
            if not text:
                text = "".join(
                    part if isinstance(part, str) else str(part.get("text") or "")
                    for part in content
                    if isinstance(part, (str, dict))
                )
            image_count = sum(
                1
                for part in content
                if isinstance(part, dict) and part.get("type") in {"localImage", "image"}
            )
            marker = "[图片 × %d]" % image_count if image_count else ""
            return "\n".join(value for value in (text, marker) if value)
        return text

    def _approval_action_token(self, approval_id: str) -> str:
        digest = hmac.new(
            self.settings.pairing_secret.encode("utf-8"),
            ("approval:" + approval_id).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _encode_message_cursor(
        self,
        thread_id: str,
        key: Tuple[str, int, int, str],
    ) -> str:
        payload = json.dumps(
            [thread_id, key[0], key[1], key[2], key[3]],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(
            self.settings.pairing_secret.encode("utf-8"),
            b"message-page:" + payload,
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return encoded + "." + encoded_signature

    def _decode_message_cursor(
        self,
        thread_id: str,
        cursor: str,
    ) -> Tuple[str, int, int, str]:
        if len(cursor) > 1024 or cursor.count(".") != 1:
            raise GatewayError("MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400)
        encoded, encoded_signature = cursor.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", encoded_signature
        ):
            raise GatewayError("MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400)
        try:
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
            values = json.loads(payload.decode("ascii"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise GatewayError(
                "MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400
            ) from exc
        if (
            base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=") != encoded
            or base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
            != encoded_signature
        ):
            raise GatewayError("MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400)
        expected = hmac.new(
            self.settings.pairing_secret.encode("utf-8"),
            b"message-page:" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise GatewayError("MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400)
        if (
            not isinstance(values, list)
            or len(values) != 5
            or values[0] != thread_id
            or not isinstance(values[1], str)
            or not isinstance(values[2], int)
            or not isinstance(values[3], int)
            or not isinstance(values[4], str)
        ):
            raise GatewayError("MESSAGE_CURSOR_INVALID", "Message cursor is invalid", 400)
        return values[1], values[2], values[3], values[4]

    @classmethod
    def _bounded_public_text(cls, value: str) -> Tuple[str, bool]:
        return bounded_text(cls._redact_sensitive(value), cls.PUBLIC_TEXT_LIMIT)

    def _repair_retained_approval_data(self) -> None:
        repairs: List[Dict[str, Any]] = []
        for row in self.store.list_approvals():
            approval_id = str(row["public_id"])
            state = str(row["state"])
            action_token = (
                self._approval_action_token(approval_id)
                if state == "pending"
                else None
            )
            summary, reason = self._sanitize_approval_public_fields(
                str(row["summary"] or "Run an action"),
                str(
                    row["reason"]
                    or "Codex requires confirmation before this action."
                ),
            )
            payload = ApprovalDTO(
                approval_id=approval_id,
                thread_id=str(row["thread_public_id"]),
                turn_id=str(row["turn_public_id"]),
                item_id=str(row["item_public_id"]),
                kind=str(row["kind"]),
                summary=summary,
                reason=reason,
                decisions=json.loads(row["decisions_json"]),
                state=state,
                expires_at=str(row["expires_at"]),
                action_token=action_token,
            ).model_dump(by_alias=True, exclude_none=True)
            requested_payload = {
                **payload,
                # Historical request events must remain decodable as the
                # pending mutation that originally introduced the approval.
                # A later resolved/expired event carries the terminal state.
                "state": "pending",
            }
            repairs.append(
                {
                    "approval_id": approval_id,
                    "summary": summary,
                    "reason": reason,
                    "state": state,
                    "action_token": action_token,
                    "payload": payload,
                    "requested_payload": requested_payload,
                }
            )
        self.store.repair_approval_public_data(repairs)

    async def _fetch_thread_catalog(self) -> List[BridgeThread]:
        try:
            active = await self.bridge.list_threads(archived=False)
            archived = await self.bridge.list_threads(archived=True)
        except BridgeError as exc:
            raise GatewayError(
                "CODEX_UNAVAILABLE",
                "Codex is temporarily unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        catalog = list(active) + list(archived)
        raw_ids = [thread.raw_id for thread in catalog]
        if len(raw_ids) != len(set(raw_ids)):
            raise GatewayError(
                "THREAD_CATALOG_INCONSISTENT",
                "Codex returned an inconsistent conversation catalog",
                status_code=409,
                retryable=True,
            )
        if any(thread.archived is None for thread in catalog):
            raise GatewayError(
                "THREAD_CATALOG_INCONSISTENT",
                "Codex returned an incomplete conversation catalog",
                status_code=409,
                retryable=True,
            )
        return sorted(catalog, key=lambda thread: thread.raw_id)

    @staticmethod
    def _thread_catalog_fingerprint(
        catalog: List[BridgeThread],
        root_raw_id: Optional[str] = None,
    ) -> str:
        by_raw = {thread.raw_id: thread for thread in catalog}

        def belongs_to_root(thread: BridgeThread) -> bool:
            if root_raw_id is None:
                return True
            cursor: Optional[str] = thread.raw_id
            seen: set = set()
            while cursor is not None and cursor not in seen:
                if cursor == root_raw_id:
                    return True
                seen.add(cursor)
                parent = by_raw.get(cursor)
                cursor = parent.parent_raw_id if parent is not None else None
            return False

        values = [
            {
                "id": thread.raw_id,
                "parent": thread.parent_raw_id,
                "archived": thread.archived,
            }
            for thread in catalog
            if belongs_to_root(thread)
        ]
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def _sync_thread_catalog(
        self,
        device: DeviceRecord,
        require_stable: bool = False,
        stability_root_raw_id: Optional[str] = None,
    ) -> List[BridgeThread]:
        catalog = [
            thread
            for thread in await self._fetch_thread_catalog()
            if not self.store.is_thread_tombstoned(thread.raw_id)
        ]
        if require_stable:
            verified = [
                thread
                for thread in await self._fetch_thread_catalog()
                if not self.store.is_thread_tombstoned(thread.raw_id)
            ]
            if self._thread_catalog_fingerprint(
                catalog,
                stability_root_raw_id,
            ) != self._thread_catalog_fingerprint(
                verified,
                stability_root_raw_id,
            ):
                raise GatewayError(
                    "THREAD_CATALOG_CHANGED",
                    "Conversation catalog changed; refresh and try again",
                    status_code=409,
                    retryable=True,
                )
            catalog = verified
        self.store.reconcile_thread_catalog(
            device.id,
            [
                {
                    "raw_id": thread.raw_id,
                    "title": thread.title,
                    "preview": thread.preview,
                    "status": self._public_status(thread.status).value,
                    "archived": bool(thread.archived),
                    "parent_raw_id": thread.parent_raw_id,
                    "source_kind": thread.source_kind,
                    "created_at": thread.created_at,
                    "updated_at": thread.updated_at,
                }
                for thread in catalog
            ],
            scope_root_raw_id=stability_root_raw_id,
        )
        remote_raw_ids = {thread.raw_id for thread in catalog}
        provisional = [
            BridgeThread(
                raw_id=str(row["raw_id"]),
                title=str(row["title"]),
                preview=str(row["preview"]),
                status=str(row["status"]),
                archived=bool(row["archived"]),
                parent_raw_id=(
                    None
                    if row["parent_raw_id"] is None
                    else str(row["parent_raw_id"])
                ),
                source_kind=str(row["source_kind"] or "unknown"),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in self.store.provisional_thread_rows()
            if str(row["raw_id"]) not in remote_raw_ids
        ]
        return catalog + provisional

    def _delete_impact(
        self,
        catalog: List[BridgeThread],
        thread_id: str,
    ) -> List[BridgeThread]:
        root_raw_id = self._require_raw_thread_id(thread_id)
        by_raw = {thread.raw_id: thread for thread in catalog}
        if root_raw_id not in by_raw:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        children: Dict[str, List[str]] = {}
        for thread in catalog:
            if thread.parent_raw_id is not None:
                children.setdefault(thread.parent_raw_id, []).append(thread.raw_id)
        ordered_raw_ids: List[str] = []
        pending = [root_raw_id]
        while pending:
            current = pending.pop(0)
            if current in ordered_raw_ids:
                raise GatewayError(
                    "THREAD_TOPOLOGY_INVALID",
                    "Conversation descendant topology is invalid",
                    status_code=409,
                )
            ordered_raw_ids.append(current)
            pending.extend(sorted(children.get(current, [])))

        impacted = set(ordered_raw_ids)
        for raw_id in impacted:
            seen: set = set()
            cursor: Optional[str] = raw_id
            while cursor is not None and cursor in impacted:
                if cursor in seen:
                    raise GatewayError(
                        "THREAD_TOPOLOGY_INVALID",
                        "Conversation descendant topology is invalid",
                        status_code=409,
                    )
                seen.add(cursor)
                cursor = by_raw[cursor].parent_raw_id
        return [by_raw[raw_id] for raw_id in ordered_raw_ids]

    @staticmethod
    def _require_archived_impact(impact: List[BridgeThread]) -> None:
        if not impact or any(thread.archived is not True for thread in impact):
            raise GatewayError(
                "DELETE_REQUIRES_ARCHIVED",
                "Archive the conversation and all descendants before permanent delete",
                status_code=409,
            )

    @staticmethod
    def _raise_for_delete_token_state(state: str) -> None:
        if state in {"pending", "claimed"}:
            return
        if state == "expired":
            raise GatewayError(
                "DELETE_IMPACT_EXPIRED",
                "Delete impact authorization has expired",
                status_code=409,
            )
        if state == "used":
            raise GatewayError(
                "DELETE_IMPACT_ALREADY_USED",
                "Delete impact authorization was already used",
                status_code=409,
            )
        if state == "changed":
            raise GatewayError(
                "DELETE_IMPACT_CHANGED",
                "Conversation descendants changed; request a new preview",
                status_code=409,
            )
        if state == "archived":
            raise GatewayError(
                "DELETE_REQUIRES_ARCHIVED",
                "Archive the conversation and all descendants before permanent delete",
                status_code=409,
            )
        if state == "busy":
            raise GatewayError(
                "DELETE_IMPACT_BUSY",
                "A turn or delete operation is active in the affected conversations",
                status_code=409,
            )
        raise GatewayError(
            "DELETE_IMPACT_INVALID",
            "Delete impact authorization is not valid",
            status_code=409,
        )

    def _map_thread(self, raw: BridgeThread) -> ThreadDTO:
        existing_id = self.store.thread_public_id(raw.raw_id)
        if existing_id is not None:
            existing = self.store.thread_by_public_id(existing_id)
            preserve_archived_status = (
                raw.archived is None
                and existing is not None
                and existing.status == ThreadStatus.ARCHIVED
            )
            updated = self.store.update_thread(
                existing_id,
                title=raw.title,
                preview=raw.preview,
                status=(
                    None
                    if preserve_archived_status
                    else self._public_status(raw.status).value
                ),
                archived=raw.archived,
            )
            if updated is not None:
                return updated
        return self.store.get_or_create_thread(
            raw_id=raw.raw_id,
            title=raw.title,
            preview=raw.preview,
            status=self._public_status(raw.status).value,
            archived=bool(raw.archived),
            created_at=raw.created_at,
            updated_at=raw.updated_at,
        )

    def _update_mapped_thread(self, thread_id: str, raw: BridgeThread) -> ThreadDTO:
        existing = self.store.thread_by_public_id(thread_id)
        preserve_archived_status = (
            raw.archived is None
            and existing is not None
            and existing.status == ThreadStatus.ARCHIVED
        )
        thread = self.store.update_thread(
            thread_id,
            title=raw.title,
            preview=raw.preview,
            status=(
                None
                if preserve_archived_status
                else self._public_status(raw.status).value
            ),
            archived=raw.archived,
        )
        if thread is None:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        return thread

    def _require_raw_thread_id(self, thread_id: str) -> str:
        raw_id = self.store.thread_raw_id(thread_id)
        if raw_id is None:
            raise GatewayError("THREAD_NOT_FOUND", "Conversation was not found", 404)
        return raw_id

    @staticmethod
    def _public_status(value: Any) -> ThreadStatus:
        if isinstance(value, dict):
            flags = value.get("activeFlags") or []
            if "waitingOnApproval" in flags:
                return ThreadStatus.WAITING_APPROVAL
            if "waitingOnUserInput" in flags:
                return ThreadStatus.WAITING_USER
            value = value.get("type")
        normalized = str(value or "idle")
        return {
            "active": ThreadStatus.RUNNING,
            "running": ThreadStatus.RUNNING,
            "inProgress": ThreadStatus.RUNNING,
            "waitingApproval": ThreadStatus.WAITING_APPROVAL,
            "waitingUser": ThreadStatus.WAITING_USER,
            "waitingOnUser": ThreadStatus.WAITING_USER,
            "waitingOnUserInput": ThreadStatus.WAITING_USER,
            "systemError": ThreadStatus.FAILED,
            "failed": ThreadStatus.FAILED,
            "archived": ThreadStatus.ARCHIVED,
        }.get(normalized, ThreadStatus.IDLE)

    @staticmethod
    def _event_type(method: str, params: Dict[str, Any]) -> str:
        if method == "turn/completed":
            status = str((params.get("turn") or {}).get("status") or "completed")
            if status == "failed":
                return "turn.failed"
            if status == "interrupted":
                return "turn.interrupted"
            return "turn.completed"
        return {
            "thread/status/changed": "thread.updated",
            "thread/name/updated": "thread.updated",
            "thread/archived": "thread.updated",
            "thread/unarchived": "thread.updated",
            "thread/deleted": "thread.deleted",
            "turn/started": "turn.started",
            "item/started": "audit.action",
            "item/completed": "audit.action",
            "turn/diff/updated": "audit.action",
            "warning": "audit.action",
            "error": "error",
        }.get(method, "audit.action")

    @staticmethod
    def _safe_event_payload(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        # Only explicitly selected fields cross the public boundary. This prevents
        # raw Codex ids, paths, and debug structures from leaking to the phone.
        if method == "turn/completed":
            turn = params.get("turn") or {}
            return {"status": turn.get("status", "completed")}
        if method == "turn/started":
            return {}
        if method == "thread/status/changed":
            return {"status": GatewayService._public_status(params.get("status")).value}
        if method == "turn/diff/updated":
            return {
                "action": "turn.diff.updated",
                "fallbackText": "Codex changes updated",
            }
        if method == "error":
            return {
                "code": "CODEX_RUNTIME_ERROR",
                "message": "Codex reported a runtime issue",
                "retryable": False,
            }
        if method == "warning":
            return {
                "action": "codex.warning",
                "fallbackText": "Codex reported a runtime warning",
            }
        item = params.get("item")
        if isinstance(item, dict):
            return {
                "action": "item.updated",
                "itemType": str(item.get("type") or "unknown"),
                "status": str(item.get("status") or "completed"),
                "fallbackText": "Codex activity updated",
            }
        return {
            "action": "codex.event",
            "fallbackText": "Codex activity updated",
        }

    @classmethod
    def _approval_public_fields(
        cls,
        kind: str,
        params: Dict[str, Any],
    ) -> Tuple[str, str]:
        if kind == "command":
            command = params.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            summary_source = str(command) if command else "Run a command"
        else:
            changes = params.get("changes")
            summary_source = (
                json.dumps(changes, ensure_ascii=False, separators=(",", ":"))
                if changes is not None
                else "Apply file changes"
            )
        reason_source = str(
            params.get("reason")
            or "Codex requires confirmation before this action."
        )
        return cls._sanitize_approval_public_fields(summary_source, reason_source)

    @classmethod
    def _sanitize_approval_public_fields(
        cls,
        summary_source: str,
        reason_source: str,
    ) -> Tuple[str, str]:
        # Redact the complete fields before any truncation. Truncating first can
        # split a credential pattern and persist the unredacted prefix.
        summary_redacted = cls._redact_sensitive(summary_source)
        reason_redacted = cls._redact_sensitive(reason_source)
        summary, _ = bounded_text(summary_redacted, cls.APPROVAL_SUMMARY_BYTES)
        summary_bytes = len(summary.encode("utf-8"))
        reason_budget = min(
            cls.APPROVAL_REASON_BYTES,
            max(1, cls.APPROVAL_TOTAL_BYTES - summary_bytes),
        )
        reason, _ = bounded_text(reason_redacted, reason_budget)
        return summary, reason

    @staticmethod
    def _redact_sensitive(value: str) -> str:
        value = unicode_safe_text(value)
        value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", value)
        credential = re.compile(
            r"(?ix)\b(?P<name>api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"token|password|secret)\s*[:=]\s*(?:"
            r'"(?:\\.|[^"\\])*"|'
            r"'(?:\\.|[^'\\])*'|"
            r"[\"'][\s\S]*|"
            r"[^\s,;]+)"
        )
        value = credential.sub(lambda match: match.group("name") + "=[REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer [REDACTED]", value)
        value = re.sub(
            r"(?i)(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{10,})",
            "[REDACTED]",
            value,
        )
        value = re.sub(
            r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            "[REDACTED]",
            value,
        )
        value = re.sub(
            r"(?i)\braw-(?:thread|turn|item|message)-[A-Za-z0-9._:-]+",
            "[INTERNAL_ID]",
            value,
        )
        value = re.sub(
            r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            "[INTERNAL_ID]",
            value,
        )
        protected_urls: List[str] = []

        def protect_https_url(match: re.Match[str]) -> str:
            protected_urls.append(GatewayService._sanitize_public_url(match.group(0)))
            return "CHEBYHTTPSURL%dPLACEHOLDER" % (len(protected_urls) - 1)

        value = re.sub(
            r"(?i)https?://[^\s<>\"']+",
            protect_https_url,
            value,
        )
        value = GatewayService._redact_local_paths(value)
        for index, url in enumerate(protected_urls):
            value = value.replace("CHEBYHTTPSURL%dPLACEHOLDER" % index, url)
        return value

    @staticmethod
    def _sanitize_public_url(value: str) -> str:
        trailing = ""
        while value and value[-1] in ",.;:!?)]}":
            trailing = value[-1] + trailing
            value = value[:-1]
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            return "[URL]" + trailing
        internal = not host or host == "localhost" or host.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home.arpa")
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is None and "." not in host:
            internal = True
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            internal = True
        if internal or parsed.username is not None or parsed.password is not None:
            return "[INTERNAL_URL]" + trailing

        sensitive_name = re.compile(
            r"(?i)^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
            r"password|secret|authorization)$"
        )
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(sensitive_name.fullmatch(name) for name, _ in query):
            query = [
                (name, "[REDACTED]" if sensitive_name.fullmatch(name) else item)
                for name, item in query
            ]
            value = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
            )
        return value + trailing

    @staticmethod
    def _redact_local_paths(value: str) -> str:
        """Redact explicit local path tokens without treating URL slashes as paths."""

        def redact_candidate(match: re.Match[str]) -> str:
            candidate = match.group(0)
            # A filename extension followed by whitespace is an unambiguous end
            # for an unquoted path containing spaces. Preserve following prose.
            endpoint = re.search(r"\.[A-Za-z0-9]{1,16}(?=\s|$)", candidate)
            if endpoint is None:
                return "[PATH]"
            return "[PATH]" + candidate[endpoint.end() :]

        value = re.sub(
            r"\b[A-Za-z]:\\[^\r\n,;)\]}\"'`]+",
            redact_candidate,
            value,
        )
        return GatewayService._redact_posix_absolute_paths(value)

    @staticmethod
    def _redact_posix_absolute_paths(value: str) -> str:
        """Tokenize POSIX absolute paths with spaces and preserve prose boundaries.

        HTTP(S) URLs have already been replaced by slash-free placeholders.
        A path starts only at a token boundary and never at a slash embedded in
        ordinary text such as ``and/or`` or ``1/2``. Strong punctuation ends an
        unquoted path. For a filename followed by prose, the final extension is
        an unambiguous endpoint and its following punctuation is preserved.
        """

        start_pattern = re.compile(r"(?<![:\w])/(?![/\s])")
        strong_boundary = set("\r\n,;:)]}\"'`|!?")
        extension_pattern = re.compile(
            r"\.[A-Za-z0-9]{1,16}(?=$|\s|[,;:.)\]}|!?])"
        )
        output: List[str] = []
        cursor = 0
        while True:
            start_match = start_pattern.search(value, cursor)
            if start_match is None:
                output.append(value[cursor:])
                break
            start = start_match.start()
            output.append(value[cursor:start])
            candidate_end = start + 1
            while (
                candidate_end < len(value)
                and value[candidate_end] not in strong_boundary
            ):
                candidate_end += 1
            candidate = value[start:candidate_end]
            extension_matches = list(extension_pattern.finditer(candidate))
            if extension_matches:
                path_length = extension_matches[-1].end()
            else:
                path_length = len(candidate.rstrip())
            if path_length <= 1:
                output.append(value[start : start + 1])
                cursor = start + 1
                continue
            output.append("[PATH]")
            cursor = start + path_length
        return "".join(output)

    @staticmethod
    def _approval_dto(row: Any, action_token: Optional[str]) -> ApprovalDTO:
        return ApprovalDTO(
            approval_id=row["public_id"],
            thread_id=row["thread_public_id"],
            turn_id=row["turn_public_id"],
            item_id=row["item_public_id"],
            kind=row["kind"],
            summary=row["summary"],
            reason=(
                row["reason"]
                or "Codex requires confirmation before this action."
            ),
            decisions=json.loads(row["decisions_json"]),
            state=row["state"],
            expires_at=row["expires_at"],
            action_token=action_token,
        )
