from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import ConnectorSettings
from .engine import ConnectorEngine, ProtocolViolation, _operation_lane
from .models import (
    AcknowledgedFrame,
    AcceptedFrame,
    DeliveredMessage,
    PingFrame,
    PongFrame,
    ReadyFrame,
    RelayAck,
    parse_bounded_json,
    wire_json,
)


LOGGER = logging.getLogger("cheby_connector")


@dataclass(frozen=True)
class _ScheduledDelivery:
    raw: str
    lane: str
    predecessor: asyncio.Future[None] | None
    completion: asyncio.Future[None]


class _LaneSequencer:
    """Register FIFO dependencies in WebSocket receive order."""

    def __init__(self) -> None:
        self._tails: dict[str, asyncio.Future[None]] = {}

    def register(self, raw: str, lane: str) -> _ScheduledDelivery:
        completion = asyncio.get_running_loop().create_future()
        completion.add_done_callback(_consume_future_failure)
        scheduled = _ScheduledDelivery(
            raw=raw,
            lane=lane,
            predecessor=self._tails.get(lane),
            completion=completion,
        )
        self._tails[lane] = completion
        return scheduled

    def complete(
        self,
        delivery: _ScheduledDelivery,
        failure: BaseException | None = None,
    ) -> None:
        if not delivery.completion.done():
            if isinstance(failure, asyncio.CancelledError):
                delivery.completion.cancel()
            elif failure is not None:
                delivery.completion.set_exception(failure)
            else:
                delivery.completion.set_result(None)
        if self._tails.get(delivery.lane) is delivery.completion:
            self._tails.pop(delivery.lane, None)


def _consume_future_failure(future: asyncio.Future[None]) -> None:
    if not future.cancelled():
        future.exception()


def _failure_category(error: Exception) -> str:
    if isinstance(error, ConnectionClosed):
        received = error.rcvd
        code = 1006 if received is None else received.code
        return f"websocket_close_{code}"
    if isinstance(error, ProtocolViolation):
        return "protocol_violation"
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "transport"
    return "internal"


class RelayConnector:
    def __init__(self, settings: ConnectorSettings, engine: ConnectorEngine) -> None:
        self.settings = settings
        self.engine = engine
        self._rng = secrets.SystemRandom()
        self._reached_ready = False
        self._ready_since: float | None = None

    async def run_forever(self) -> None:
        delay_ceiling = self.settings.reconnect_base_seconds
        while True:
            self._reached_ready = False
            self._ready_since = None
            try:
                await self._run_connection()
                delay_ceiling = self.settings.reconnect_base_seconds
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "relay connection unavailable category=%s",
                    _failure_category(error),
                )
            if self._ready_was_stable(asyncio.get_running_loop().time()):
                delay_ceiling = self.settings.reconnect_base_seconds
            delay = self._rng.uniform(0.0, delay_ceiling)
            await asyncio.sleep(delay)
            delay_ceiling = min(
                self.settings.reconnect_max_seconds,
                max(self.settings.reconnect_base_seconds, delay_ceiling * 2),
            )
    async def _run_connection(self) -> None:
        async with connect(
            self.settings.relay_url,
            additional_headers={
                "Authorization": "Bearer " + self.settings.relay_token,
            },
            open_timeout=self.settings.connect_timeout_seconds,
            close_timeout=5,
            max_size=self.settings.max_inbound_frame_bytes,
            max_queue=4,
            compression=None,
            ping_interval=None,
        ) as websocket:
            ready_raw = await asyncio.wait_for(
                websocket.recv(), timeout=self.settings.connect_timeout_seconds
            )
            ready = self._parse_ready(ready_raw)
            if ready.principal_id != self.settings.connector_id:
                raise ProtocolViolation("Relay principal binding is invalid")
            local_cursor = self.engine.state.ack_cursor
            if ready.ack_cursor > local_cursor:
                raise ProtocolViolation("Relay cursor is ahead of durable local state")
            self._reached_ready = True
            self._ready_since = asyncio.get_running_loop().time()

            send_lock = asyncio.Lock()
            heartbeat = _Heartbeat()
            in_flight = _SingleInFlight()
            if local_cursor > ready.ack_cursor:
                async with send_lock:
                    await websocket.send(
                        wire_json(RelayAck(deliverySeq=local_cursor))
                    )

            deliveries: asyncio.Queue[_ScheduledDelivery] = asyncio.Queue(
                maxsize=self.settings.max_concurrency * 4
            )
            sequencer = _LaneSequencer()
            tasks = [
                asyncio.create_task(
                    self._sender(websocket, send_lock, in_flight)
                ),
                asyncio.create_task(
                    self._receiver(
                        websocket,
                        send_lock,
                        heartbeat,
                        deliveries,
                        in_flight,
                        sequencer,
                    )
                ),
                asyncio.create_task(self._heartbeat(websocket, send_lock, heartbeat)),
            ]
            tasks.extend(
                asyncio.create_task(
                    self._delivery_worker(
                        deliveries,
                        websocket,
                        send_lock,
                        sequencer,
                    ),
                    name="connector-delivery-%d" % index,
                )
                for index in range(self.settings.max_concurrency)
            )
            try:
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in done:
                    error = task.exception()
                    if error is not None:
                        raise error
                raise ConnectionError("Relay connection task stopped")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _ready_was_stable(self, now: float) -> bool:
        return (
            self._reached_ready
            and self._ready_since is not None
            and now - self._ready_since >= self.settings.ping_interval_seconds
        )

    async def _sender(
        self,
        websocket: Any,
        send_lock: asyncio.Lock,
        in_flight: "_SingleInFlight",
    ) -> None:
        while True:
            pending = self.engine.pending_outbox()
            if not pending:
                await self.engine.wait_for_outbox_change(set())
                continue
            message_id, frame = pending[0]
            if len(frame.encode("utf-8")) > self.settings.max_outbound_frame_bytes:
                raise ProtocolViolation("Durable outbound frame exceeds its limit")
            accepted = await in_flight.send(
                websocket, send_lock, message_id, frame
            )
            try:
                await asyncio.wait_for(
                    accepted.wait(), timeout=self.settings.connect_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise ConnectionError("Relay acceptance deadline expired") from exc

    async def _receiver(
        self,
        websocket: Any,
        send_lock: asyncio.Lock,
        heartbeat: "_Heartbeat",
        deliveries: asyncio.Queue[_ScheduledDelivery],
        in_flight: "_SingleInFlight",
        sequencer: _LaneSequencer,
    ) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                await websocket.close(code=1003, reason="text-frames-only")
                raise ProtocolViolation("Relay sent a binary frame")
            if len(raw.encode("utf-8")) > self.settings.max_inbound_frame_bytes:
                await websocket.close(code=1009, reason="frame-too-large")
                raise ProtocolViolation("Relay frame exceeds the hard limit")
            frame_type = self._frame_type(raw)
            if frame_type == "accepted":
                try:
                    accepted = AcceptedFrame.model_validate(parse_bounded_json(raw))
                except (ValidationError, ValueError) as exc:
                    raise ProtocolViolation("Relay accepted frame is invalid") from exc
                await in_flight.accept(
                    accepted.message_id,
                    lambda: self.engine.mark_accepted(accepted.message_id),
                )
            elif frame_type == "delivery":
                # Validate once here so malformed deliveries fail the connection
                # before consuming a concurrency slot.
                try:
                    delivery = DeliveredMessage.model_validate(parse_bounded_json(raw))
                    params = delivery.payload.typed_params()
                except (ValidationError, ValueError) as exc:
                    raise ProtocolViolation("Relay delivery frame is invalid") from exc
                scheduled = sequencer.register(raw, _operation_lane(params))
                try:
                    await deliveries.put(scheduled)
                except BaseException as error:
                    sequencer.complete(scheduled, error)
                    raise
            elif frame_type == "acknowledged":
                try:
                    acknowledged = AcknowledgedFrame.model_validate(
                        parse_bounded_json(raw)
                    )
                except (ValidationError, ValueError) as exc:
                    raise ProtocolViolation("Relay acknowledgement frame is invalid") from exc
                if acknowledged.delivery_seq > self.engine.state.ack_cursor:
                    raise ProtocolViolation("Relay acknowledged an unsaved delivery cursor")
            elif frame_type == "ping":
                try:
                    ping = PingFrame.model_validate(parse_bounded_json(raw))
                except (ValidationError, ValueError) as exc:
                    raise ProtocolViolation("Relay ping frame is invalid") from exc
                async with send_lock:
                    await websocket.send(wire_json(PongFrame(nonce=ping.nonce)))
            elif frame_type == "pong":
                try:
                    pong = PongFrame.model_validate(parse_bounded_json(raw))
                except (ValidationError, ValueError) as exc:
                    raise ProtocolViolation("Relay pong frame is invalid") from exc
                heartbeat.accept(pong.nonce)
            else:
                raise ProtocolViolation("Relay frame type is not allowed")
        raise ConnectionError("Relay connection closed")

    async def _delivery_worker(
        self,
        deliveries: asyncio.Queue[_ScheduledDelivery],
        websocket: Any,
        send_lock: asyncio.Lock,
        sequencer: _LaneSequencer,
    ) -> None:
        while True:
            scheduled = await deliveries.get()
            failure: BaseException | None = None
            try:
                if scheduled.predecessor is not None:
                    await asyncio.shield(scheduled.predecessor)
                await self._handle_delivery(scheduled.raw, websocket, send_lock)
            except BaseException as error:
                failure = error
                raise
            finally:
                sequencer.complete(scheduled, failure)
                deliveries.task_done()

    async def _handle_delivery(
        self, raw: str, websocket: Any, send_lock: asyncio.Lock
    ) -> None:
        result = await self.engine.handle_delivery(raw)
        # The response is already durable in the outbox. A cumulative delivery
        # ack is sent only after that transaction commits.
        async with send_lock:
            await websocket.send(wire_json(RelayAck(deliverySeq=result.ack_cursor)))

    async def _heartbeat(
        self, websocket: Any, send_lock: asyncio.Lock, heartbeat: "_Heartbeat"
    ) -> None:
        while True:
            await asyncio.sleep(self.settings.ping_interval_seconds)
            nonce = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
            waiter = heartbeat.expect(nonce)
            async with send_lock:
                await websocket.send(wire_json(PingFrame(nonce=nonce)))
            try:
                await asyncio.wait_for(
                    waiter, timeout=self.settings.pong_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise ConnectionError("Relay heartbeat deadline expired") from exc

    @staticmethod
    def _frame_type(raw: str) -> str:
        try:
            value = parse_bounded_json(raw)
        except ValueError as exc:
            raise ProtocolViolation("Relay frame is not JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ProtocolViolation("Relay frame envelope is invalid")
        return value["type"]

    @staticmethod
    def _parse_ready(raw: object) -> ReadyFrame:
        if not isinstance(raw, str):
            raise ProtocolViolation("Relay ready frame must be text")
        try:
            return ReadyFrame.model_validate(parse_bounded_json(raw))
        except (ValidationError, ValueError) as exc:
            raise ProtocolViolation("Relay ready frame is invalid") from exc


class _Heartbeat:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[None]] = {}

    def expect(self, nonce: str) -> asyncio.Future[None]:
        future = asyncio.get_running_loop().create_future()
        self._pending[nonce] = future
        return future

    def accept(self, nonce: str) -> None:
        future = self._pending.pop(nonce, None)
        if future is None:
            raise ProtocolViolation("Relay pong nonce was not pending")
        if not future.done():
            future.set_result(None)


class _SingleInFlight:
    """Bind each Relay acceptance to exactly one message sent on this socket."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._message_id: str | None = None
        self._sent = False
        self._accepted: asyncio.Event | None = None

    async def send(
        self,
        websocket: Any,
        send_lock: asyncio.Lock,
        message_id: str,
        frame: str,
    ) -> asyncio.Event:
        async with self._lock:
            if self._message_id is not None:
                raise ProtocolViolation("Relay outbound window is already occupied")
            accepted = asyncio.Event()
            self._message_id = message_id
            self._accepted = accepted
            self._sent = False
            try:
                async with send_lock:
                    await websocket.send(frame)
            except BaseException:
                self._message_id = None
                self._accepted = None
                raise
            self._sent = True
            return accepted

    async def accept(
        self, message_id: str, persist: Callable[[], None]
    ) -> None:
        async with self._lock:
            if (
                self._message_id is None
                or not self._sent
                or self._message_id != message_id
                or self._accepted is None
            ):
                raise ProtocolViolation(
                    "Relay accepted a message outside the connection send window"
                )
            persist()
            accepted = self._accepted
            self._message_id = None
            self._accepted = None
            self._sent = False
            accepted.set()
