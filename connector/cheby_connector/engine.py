from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from pydantic import ValidationError

from .config import ConnectorSettings
from .gateway import GatewayBackend
from .models import (
    AssetsUploadParams,
    DeliveredMessage,
    ErrorBody,
    EventData,
    EventPayload,
    EventsAckParams,
    EventsSubscribeParams,
    MAX_CONTROL_FRAME_BYTES,
    OutboundMessage,
    ResponsePayload,
    parse_bounded_json,
    wire_json,
)
from .state import ConnectorState, IdempotencyConflict, OutboxFull


class ProtocolViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    response_frame: str
    ack_cursor: int


class _KeyedLocks:
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, *keys: str) -> AsyncIterator[None]:
        names = sorted(set(keys))
        async with self._guard:
            locks: list[asyncio.Lock] = []
            for name in names:
                lock, users = self._locks.get(name, (asyncio.Lock(), 0))
                self._locks[name] = (lock, users + 1)
                locks.append(lock)
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            async with self._guard:
                for name in names:
                    lock, users = self._locks[name]
                    if users == 1 and not lock.locked():
                        del self._locks[name]
                    else:
                        self._locks[name] = (lock, users - 1)


class ConnectorEngine:
    def __init__(
        self,
        settings: ConnectorSettings,
        state: ConnectorState,
        gateway: GatewayBackend,
    ) -> None:
        self.settings = settings
        self.state = state
        self.gateway = gateway
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._keyed_locks = _KeyedLocks()
        self._binding_lock = asyncio.Lock()
        self._subscription: Optional[asyncio.Task] = None
        self._subscription_key: Optional[tuple[str, str, int]] = None
        self._subscription_lock = asyncio.Lock()
        self._event_last_queued: Optional[int] = None
        self._outbox_changed = asyncio.Event()
        self._gc_task = asyncio.create_task(
            self._gc_loop(), name="connector-idempotency-gc"
        )

    async def close(self) -> None:
        await self._stop_subscription()
        self._gc_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._gc_task
        await self.gateway.close()
        self.state.close()

    async def handle_delivery(self, raw: str) -> DeliveryResult:
        raw_size = len(raw.encode("utf-8"))
        if raw_size > self.settings.max_inbound_frame_bytes:
            raise ProtocolViolation("Relay frame exceeds the hard limit")
        try:
            delivery = DeliveredMessage.model_validate(parse_bounded_json(raw))
            params = delivery.payload.typed_params()
        except (ValidationError, ValueError) as exc:
            raise ProtocolViolation("Relay delivery schema is invalid") from exc
        if not isinstance(params, AssetsUploadParams) and raw_size > MAX_CONTROL_FRAME_BYTES:
            raise ProtocolViolation("Relay control command exceeds the semantic limit")
        if isinstance(params, AssetsUploadParams):
            try:
                params.decoded_body()
            except ValueError as exc:
                raise ProtocolViolation("Relay asset command is invalid") from exc
        await self._bind_device(delivery.payload.device_id)

        fingerprint = _fingerprint(delivery)
        response_message_id = _stable_message_id("response", delivery.payload.request_id)
        async with self._semaphore, self._keyed_locks.hold(
            "lane:" + _operation_lane(params),
            "message:" + delivery.message_id,
            "request:" + delivery.payload.request_id,
        ):
            try:
                cached = self.state.lookup_response(
                    delivery.message_id,
                    delivery.payload.request_id,
                    fingerprint,
                )
            except IdempotencyConflict as exc:
                raise ProtocolViolation("Relay idempotency identity was reused") from exc
            if cached is not None:
                cursor = self.state.record_replayed_delivery(
                    delivery_seq=delivery.delivery_seq,
                    message_id=delivery.message_id,
                    request_id=delivery.payload.request_id,
                    fingerprint=fingerprint,
                    response_message_id=response_message_id,
                    response_frame=cached,
                )
                self._outbox_changed.set()
                if isinstance(params, EventsSubscribeParams) and _response_succeeded(
                    cached
                ):
                    await self._replace_subscription(
                        request_id=delivery.payload.request_id,
                        device_id=delivery.payload.device_id,
                        after_seq=params.after_seq,
                    )
                return DeliveryResult(cached, cursor)

            try:
                intent_state = self.state.reserve_execution(
                    message_id=delivery.message_id,
                    request_id=delivery.payload.request_id,
                    fingerprint=fingerprint,
                    operation=delivery.payload.operation,
                    reserved_bytes=self.settings.max_outbound_frame_bytes,
                )
            except (IdempotencyConflict, OutboxFull) as exc:
                raise ProtocolViolation("Connector could not reserve durable response state") from exc
            # Generic mutations remain fail-closed after an interrupted execution.
            # turns.start is the one safe exception: Gateway durably keys it by
            # (device, thread, clientMessageId), fences dispatching/ambiguous Turns,
            # and therefore either returns the canonical Turn or requires resync
            # without calling Codex a second time.
            if intent_state == "executing" and delivery.payload.operation != "turns.start":
                response = self._error_response(
                    delivery,
                    response_message_id,
                    "OPERATION_OUTCOME_UNKNOWN",
                    "The interrupted local operation will not be replayed",
                    retryable=False,
                )
            else:
                if intent_state != "executing":
                    self.state.mark_execution_started(
                        delivery.payload.request_id, fingerprint
                    )
                response = await self._execute(delivery, params, response_message_id)
            frame = wire_json(response)
            if len(frame.encode("utf-8")) > self.settings.max_outbound_frame_bytes:
                response = self._error_response(
                    delivery,
                    response_message_id,
                    "RESPONSE_TOO_LARGE",
                    "The result exceeds the Relay response budget",
                    retryable=False,
                )
                frame = wire_json(response)
            try:
                cursor = self.state.complete_delivery(
                    delivery_seq=delivery.delivery_seq,
                    message_id=delivery.message_id,
                    request_id=delivery.payload.request_id,
                    fingerprint=fingerprint,
                    operation=delivery.payload.operation,
                    response_message_id=response_message_id,
                    response_frame=frame,
                )
            except (IdempotencyConflict, OutboxFull) as exc:
                raise ProtocolViolation("Connector durable state rejected the delivery") from exc
            self._outbox_changed.set()
            if isinstance(params, EventsSubscribeParams) and response.payload.ok:
                await self._replace_subscription(
                    request_id=delivery.payload.request_id,
                    device_id=delivery.payload.device_id,
                    after_seq=params.after_seq,
                )
            return DeliveryResult(frame, cursor)

    async def _bind_device(self, device_id: str) -> None:
        async with self._binding_lock:
            persisted = self.state.get_meta("bound_device_id")
            if persisted is not None and persisted != device_id:
                raise ProtocolViolation("Relay delivery device binding changed")
            if self.gateway.device_id is not None and self.gateway.device_id != device_id:
                raise ProtocolViolation("Gateway device binding changed")
            if self.gateway.device_id is None:
                try:
                    await self.gateway.bind_device(device_id)
                except ValueError as exc:
                    raise ProtocolViolation("Relay device binding was rejected") from exc
            if persisted is None:
                self.state.set_meta("bound_device_id", device_id)

    async def _execute(
        self,
        delivery: DeliveredMessage,
        params: object,
        response_message_id: str,
    ) -> OutboundMessage:
        try:
            result = await asyncio.wait_for(
                self._dispatch(delivery.payload.operation, params),
                timeout=self.settings.request_timeout_seconds,
            )
            return OutboundMessage(
                messageId=response_message_id,
                payload=ResponsePayload(
                    requestId=delivery.payload.request_id,
                    deviceId=delivery.payload.device_id,
                    operation=delivery.payload.operation,
                    ok=True,
                    result=result,
                ),
            )
        except asyncio.TimeoutError:
            return self._error_response(
                delivery,
                response_message_id,
                "OPERATION_OUTCOME_UNKNOWN",
                "The local operation deadline expired",
                retryable=False,
            )
        except ValidationError:
            return self._error_response(
                delivery,
                response_message_id,
                "INVALID_COMMAND",
                "Command parameters are invalid",
                retryable=False,
            )
        except ValueError:
            return self._error_response(
                delivery,
                response_message_id,
                "INVALID_COMMAND",
                "Command parameters are invalid",
                retryable=False,
            )
        except Exception as exc:
            try:
                from cheby_gateway.service import GatewayError
            except ImportError:  # Unit tests can use a backend without Gateway installed.
                GatewayError = ()  # type: ignore[assignment]
            if GatewayError and isinstance(exc, GatewayError):
                return self._error_response(
                    delivery,
                    response_message_id,
                    exc.code,
                    exc.message,
                    retryable=bool(exc.retryable),
                    retry_after_seconds=exc.retry_after_seconds,
                )
            return self._error_response(
                delivery,
                response_message_id,
                "CONNECTOR_INTERNAL_ERROR",
                "The local service could not complete the operation",
                retryable=False,
            )

    async def _dispatch(self, operation: str, params: object) -> dict[str, object]:
        if isinstance(params, EventsAckParams):
            if (
                self._event_last_queued is None
                or self.gateway.stream_id is None
                or params.stream_id != self.gateway.stream_id
                or params.seq > self._event_last_queued
            ):
                raise ValueError("event acknowledgement is outside the delivered cursor")
        return await self.gateway.dispatch(operation, params)

    @staticmethod
    def _error_response(
        delivery: DeliveredMessage,
        response_message_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: Optional[int] = None,
    ) -> OutboundMessage:
        return OutboundMessage(
            messageId=response_message_id,
            payload=ResponsePayload(
                requestId=delivery.payload.request_id,
                deviceId=delivery.payload.device_id,
                operation=delivery.payload.operation,
                ok=False,
                error=ErrorBody(
                    code=code,
                    message=message,
                    retryable=retryable,
                    retryAfterSeconds=retry_after_seconds,
                ),
            ),
        )

    async def _replace_subscription(
        self, *, request_id: str, device_id: str, after_seq: int
    ) -> None:
        key = (request_id, device_id, after_seq)
        async with self._subscription_lock:
            if (
                self._subscription_key == key
                and self._subscription is not None
                and not self._subscription.done()
            ):
                return
            await self._cancel_subscription_locked()
            self._event_last_queued = after_seq
            self._subscription_key = key
            self._subscription = asyncio.create_task(
                self._event_loop(request_id, device_id, after_seq),
                name="connector-event-pump",
            )
            self._subscription.add_done_callback(lambda _: self._outbox_changed.set())

    async def _stop_subscription(self) -> None:
        async with self._subscription_lock:
            await self._cancel_subscription_locked()

    async def _cancel_subscription_locked(self) -> None:
        task = self._subscription
        self._subscription = None
        self._subscription_key = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _event_loop(
        self, request_id: str, device_id: str, after_seq: int
    ) -> None:
        last_queued = after_seq
        while True:
            events = self.gateway.replay_events(last_queued)
            for event in events:
                payload = EventPayload(
                    requestId=request_id,
                    deviceId=device_id,
                    streamId=event.stream_id,
                    eventId=event.event_id,
                    eventSeq=event.seq,
                    eventType=event.type,
                    threadId=event.thread_id,
                    data=EventData(
                        occurredAt=event.occurred_at,
                        turnId=event.turn_id,
                        itemId=event.item_id,
                        payload=event.payload,
                    ),
                )
                message_id = _stable_message_id(
                    "event",
                    f"{request_id}:{event.stream_id}:{event.event_id}:{event.seq}",
                )
                frame = wire_json(
                    OutboundMessage(messageId=message_id, payload=payload)
                )
                if len(frame.encode("utf-8")) > self.settings.max_outbound_frame_bytes:
                    payload = payload.model_copy(
                        update={
                            "event_type": "sync.required",
                            "data": EventData(
                                occurredAt=event.occurred_at,
                                payload={"reason": "eventTooLarge"},
                            ),
                        }
                    )
                    frame = wire_json(
                        OutboundMessage(messageId=message_id, payload=payload)
                    )
                while True:
                    try:
                        self.state.enqueue(message_id, frame)
                        break
                    except OutboxFull:
                        await asyncio.sleep(0.25)
                self._outbox_changed.set()
                last_queued = event.seq
                self._event_last_queued = last_queued
            await asyncio.sleep(0.25)

    def pending_outbox(self) -> list[tuple[str, str]]:
        return self.state.pending_outbox()

    def mark_accepted(self, message_id: str) -> None:
        self.state.accept_outbox(message_id)
        self._outbox_changed.set()

    async def wait_for_outbox_change(self, sent_ids: set[str]) -> None:
        self._raise_if_background_failed()
        self._outbox_changed.clear()
        if any(message_id not in sent_ids for message_id, _ in self.pending_outbox()):
            return
        await self._outbox_changed.wait()
        self._raise_if_background_failed()

    def _raise_if_background_failed(self) -> None:
        if self._gc_task.done() and not self._gc_task.cancelled():
            error = self._gc_task.exception()
            if error is not None:
                raise ProtocolViolation("Connector maintenance stopped") from error
        task = self._subscription
        if task is None or not task.done() or task.cancelled():
            return
        error = task.exception()
        if error is not None:
            raise ProtocolViolation("Gateway event pump stopped") from error

    async def _gc_loop(self) -> None:
        while True:
            cutoff = int(time.time()) - self.settings.idempotency_retention_seconds
            self.state.gc_completed(
                cutoff_epoch=cutoff,
                batch_size=self.settings.idempotency_gc_batch_size,
            )
            await asyncio.sleep(self.settings.idempotency_gc_interval_seconds)

def _fingerprint(delivery: DeliveredMessage) -> str:
    canonical = json.dumps(
        delivery.payload.model_dump(by_alias=True, mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _operation_lane(params: object) -> str:
    thread_id = getattr(params, "thread_id", None)
    return "thread:" + str(thread_id) if thread_id else "global"


def _stable_message_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(
        ("CHEBY-CONNECTOR-1\0" + kind + "\0" + identity).encode("utf-8")
    ).digest()[:16]
    return "msg_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _response_succeeded(frame: str) -> bool:
    try:
        value = parse_bounded_json(frame)
    except ValueError:
        return False
    return (
        isinstance(value, dict)
        and isinstance(value.get("payload"), dict)
        and value["payload"].get("ok") is True
    )
