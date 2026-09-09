from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket

from .config import RelaySettings
from .store import Principal, RelayStore, Role


class ConnectionLimitError(RuntimeError):
    pass


@dataclass(eq=False)
class RelayConnection:
    websocket: WebSocket
    principal: Principal
    queue_size: int
    node_generation: Optional[int] = None
    queue: asyncio.Queue[Optional[dict]] = field(init=False)
    writer_task: Optional[asyncio.Task] = field(default=None, init=False)
    closed: bool = field(default=False, init=False)
    _close_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _websocket_close_started: bool = field(default=False, init=False, repr=False)
    _websocket_closed: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    failure_callback: Optional[Callable[["RelayConnection"], Awaitable[None]]] = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)

    def start(self) -> None:
        if self.closed:
            raise RuntimeError("cannot start a closed Relay connection")
        if self.writer_task is not None:
            raise RuntimeError("Relay connection writer is already started")
        self.writer_task = asyncio.create_task(
            self._writer(),
            name=(
                "cheby-relay-writer:"
                f"{self.principal.role}:{self.principal.principal_id}"
            ),
        )

    async def _writer(self) -> None:
        try:
            while True:
                frame = await self.queue.get()
                if frame is None:
                    return
                await self.websocket.send_json(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            callback = self.failure_callback
            if callback is None:
                await self.close(1011, "WRITE_FAILED")
                return
            try:
                # Failure handling is part of the owned writer task. Never
                # detach it: unregister/offline publication must finish before
                # the task can be considered complete.
                await callback(self)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A manager callback must not leave the WebSocket open if its
                # own cleanup fails.
                await self.close(1011, "WRITE_FAILED")

    async def enqueue(self, frame: dict) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            await self.close(4410, "SLOW_CONSUMER")
            return False

    async def close(
        self,
        code: int,
        reason: str,
        *,
        websocket_already_closed: bool = False,
    ) -> None:
        current = asyncio.current_task()
        async with self._close_lock:
            first_close = not self.closed
            self.closed = True
            writer = self.writer_task
            cancel_writer = (
                first_close
                and writer is not None
                and writer is not current
                and not writer.done()
            )
            if cancel_writer:
                writer.cancel()
            owns_websocket_close = not self._websocket_close_started
            if owns_websocket_close:
                self._websocket_close_started = True

        # Await the writer outside the close lock. A writer failure invokes the
        # manager callback, which calls close() from that same writer task.
        # Holding the lock while awaiting it would deadlock that path.
        if writer is not None and writer is not current:
            with suppress(asyncio.CancelledError):
                await writer

        if owns_websocket_close and websocket_already_closed:
            # A peer disconnect is already the terminal WebSocket event. Do
            # not send a second close frame: TestClient and some ASGI servers
            # can wait indefinitely for a peer that has already gone away.
            # The writer is still cancelled and awaited above.
            self._websocket_closed.set()
        elif owns_websocket_close:
            try:
                await asyncio.wait_for(
                    self.websocket.close(code=code, reason=reason),
                    timeout=5.0,
                )
            except Exception:
                pass
            finally:
                self._websocket_closed.set()
        else:
            await self._websocket_closed.wait()


class ConnectionManager:
    def __init__(self, settings: RelaySettings, store: RelayStore) -> None:
        self.settings = settings
        self.store = store
        self._by_stream: dict[tuple[str, Role], set[RelayConnection]] = {}
        self._lock: Optional[asyncio.Lock] = None
        # One routing critical section keeps SQLite sequence assignment, replay,
        # live delivery, ack, credential changes observed by writes, and GC
        # notice emission ordered in the single-worker v1 process.
        self._routing_lock: Optional[asyncio.Lock] = None
        self._shutting_down = False

    def _manager_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def delivery_lock(self, assistant_id: str, role: Role) -> asyncio.Lock:
        del assistant_id, role
        if self._routing_lock is None:
            self._routing_lock = asyncio.Lock()
        return self._routing_lock

    async def register(self, connection: RelayConnection) -> None:
        replaced: list[RelayConnection] = []
        key = (connection.principal.assistant_id, connection.principal.role)
        async with self._manager_lock():
            if self._shutting_down:
                raise ConnectionLimitError("connection manager is shutting down")
            total = sum(len(value) for value in self._by_stream.values())
            stream = self._by_stream.setdefault(key, set())
            same_principal = [
                existing
                for existing in stream
                if existing.principal.principal_id == connection.principal.principal_id
            ]
            if connection.principal.role == "node":
                for existing in same_principal:
                    stream.discard(existing)
                    replaced.append(existing)
                total -= len(replaced)
            elif len(same_principal) >= self.settings.max_connections_per_principal:
                raise ConnectionLimitError("principal connection limit")
            if total >= self.settings.max_connections_total:
                raise ConnectionLimitError("global connection limit")
            stream.add(connection)
            connection.failure_callback = self._writer_failed
        for existing in replaced:
            await existing.close(4403, "STALE_NODE_GENERATION")

    async def _writer_failed(self, connection: RelayConnection) -> None:
        became_offline = await self.unregister(connection)
        await connection.close(1011, "WRITE_FAILED")
        if became_offline:
            await self.send_role(
                connection.principal.assistant_id,
                "device",
                {"v": 1, "type": "node.status", "status": "offline"},
            )

    async def shutdown(self) -> None:
        async with self._manager_lock():
            self._shutting_down = True
            connections = [
                connection
                for stream in self._by_stream.values()
                for connection in stream
            ]
            self._by_stream.clear()
        if connections:
            await asyncio.gather(
                *(
                    connection.close(1001, "SERVER_SHUTDOWN")
                    for connection in connections
                )
            )

    async def connection_count(self) -> int:
        async with self._manager_lock():
            return sum(len(value) for value in self._by_stream.values())

    async def unregister(self, connection: RelayConnection) -> bool:
        key = (connection.principal.assistant_id, connection.principal.role)
        async with self._manager_lock():
            stream = self._by_stream.get(key)
            if stream is None or connection not in stream:
                return False
            stream.discard(connection)
            if not stream:
                self._by_stream.pop(key, None)
                return connection.principal.role == "node"
            return False

    async def is_online(self, assistant_id: str, role: Role) -> bool:
        async with self._manager_lock():
            targets = list(self._by_stream.get((assistant_id, role), ()))
        online = False
        for connection in targets:
            if self.connection_valid(connection):
                online = True
            else:
                code = 4401 if role == "device" else 4403
                await self.unregister(connection)
                await connection.close(code, "CREDENTIAL_EXPIRED_OR_REVOKED")
        return online

    def connection_valid(self, connection: RelayConnection) -> bool:
        if connection.closed:
            return False
        if connection.writer_task is not None and connection.writer_task.done():
            return False
        if not self.store.principal_still_valid(connection.principal):
            return False
        if connection.principal.role == "node":
            return (
                connection.node_generation is not None
                and self.store.is_current_node_generation(
                    connection.principal.principal_id, connection.node_generation
                )
            )
        return True

    async def send_connection(self, connection: RelayConnection, frame: dict) -> bool:
        if not self.connection_valid(connection):
            code = 4401 if connection.principal.role == "device" else 4403
            await self.unregister(connection)
            await connection.close(code, "CREDENTIAL_EXPIRED_OR_REVOKED")
            return False
        return await connection.enqueue(frame)

    async def send_role(self, assistant_id: str, role: Role, frame: dict) -> int:
        async with self._manager_lock():
            targets = list(self._by_stream.get((assistant_id, role), ()))
        sent = 0
        for connection in targets:
            if await self.send_connection(connection, frame):
                sent += 1
        return sent
