from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from cheby_relay.config import RelaySettings
from cheby_relay.connections import (
    ConnectionLimitError,
    ConnectionManager,
    RelayConnection,
)
from cheby_relay.store import RelayStore


@dataclass
class ControlledSocket:
    fail_writes: bool = False
    block_writes: bool = False
    sent: list[dict] = field(default_factory=list)
    close_calls: list[tuple[int, str]] = field(default_factory=list)
    write_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_write: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_json(self, frame: dict) -> None:
        self.write_started.set()
        if self.fail_writes:
            raise RuntimeError("controlled write failure")
        if self.block_writes:
            await self.release_write.wait()
        self.sent.append(frame)

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


def _node_connection(store: RelayStore, socket: ControlledSocket, *, queue_size: int = 8):
    bundle = store.bootstrap_assistant()
    principal = store.authenticate(bundle.node_token, "node")
    generation = store.activate_node_generation(bundle.node_id)
    return RelayConnection(
        socket,  # type: ignore[arg-type]
        principal,
        queue_size,
        node_generation=generation,
    )


def _pending_writer_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith("cheby-relay-writer:")
    ]


def test_normal_close_is_idempotent_and_awaits_writer(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "normal.sqlite3"))
        store = RelayStore(settings)
        try:
            socket = ControlledSocket()
            connection = _node_connection(store, socket)
            connection.start()
            assert await connection.enqueue({"type": "test"})
            await asyncio.wait_for(socket.write_started.wait(), timeout=1)
            await connection.close(1000, "NORMAL")
            await connection.close(1000, "NORMAL")
            assert connection.writer_task is not None
            assert connection.writer_task.done()
            assert socket.close_calls == [(1000, "NORMAL")]
            assert _pending_writer_tasks() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_peer_disconnect_awaits_writer_without_sending_second_close(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "peer.sqlite3"))
        store = RelayStore(settings)
        try:
            socket = ControlledSocket(block_writes=True)
            connection = _node_connection(store, socket)
            connection.start()
            assert await connection.enqueue({"type": "test"})
            await asyncio.wait_for(socket.write_started.wait(), timeout=1)

            await connection.close(
                1000,
                "PEER_DISCONNECTED",
                websocket_already_closed=True,
            )
            await connection.close(1000, "CONNECTION_CLOSED")

            assert connection.writer_task is not None
            assert connection.writer_task.done()
            assert socket.close_calls == []
            assert _pending_writer_tasks() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_writer_failure_is_owned_closed_and_unregistered(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "failure.sqlite3"))
        store = RelayStore(settings)
        try:
            manager = ConnectionManager(settings, store)
            socket = ControlledSocket(fail_writes=True)
            connection = _node_connection(store, socket)
            await manager.register(connection)
            connection.start()
            assert await connection.enqueue({"type": "test"})
            assert connection.writer_task is not None
            await asyncio.wait_for(
                asyncio.shield(connection.writer_task), timeout=1
            )
            assert connection.closed
            assert socket.close_calls == [(1011, "WRITE_FAILED")]
            assert await manager.connection_count() == 0
            assert _pending_writer_tasks() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_slow_consumer_closes_socket_and_cancels_blocked_writer(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "slow.sqlite3"))
        store = RelayStore(settings)
        try:
            socket = ControlledSocket(block_writes=True)
            connection = _node_connection(store, socket, queue_size=1)
            connection.start()
            assert await connection.enqueue({"sequence": 1})
            await asyncio.wait_for(socket.write_started.wait(), timeout=1)
            assert await connection.enqueue({"sequence": 2})
            assert not await connection.enqueue({"sequence": 3})
            assert connection.writer_task is not None
            assert connection.writer_task.done()
            assert socket.close_calls == [(4410, "SLOW_CONSUMER")]
            assert _pending_writer_tasks() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_node_replacement_awaits_stale_writer_before_returning(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "replacement.sqlite3"))
        store = RelayStore(settings)
        try:
            manager = ConnectionManager(settings, store)
            bundle = store.bootstrap_assistant()
            principal = store.authenticate(bundle.node_token, "node")
            first = RelayConnection(
                ControlledSocket(),  # type: ignore[arg-type]
                principal,
                8,
                node_generation=store.activate_node_generation(bundle.node_id),
            )
            await manager.register(first)
            first.start()
            second = RelayConnection(
                ControlledSocket(),  # type: ignore[arg-type]
                principal,
                8,
                node_generation=store.activate_node_generation(bundle.node_id),
            )
            await manager.register(second)
            second.start()
            assert first.closed
            assert first.writer_task is not None and first.writer_task.done()
            assert await manager.connection_count() == 1
            assert await manager.is_online(bundle.assistant_id, "node")
            await manager.shutdown()
            assert second.writer_task is not None and second.writer_task.done()
            assert _pending_writer_tasks() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_manager_shutdown_closes_every_connection_and_rejects_registration(tmp_path):
    async def scenario() -> None:
        settings = RelaySettings(db_path=str(tmp_path / "shutdown.sqlite3"))
        store = RelayStore(settings)
        try:
            manager = ConnectionManager(settings, store)
            first = _node_connection(store, ControlledSocket())
            second = _node_connection(store, ControlledSocket())
            for connection in (first, second):
                await manager.register(connection)
                connection.start()
            await manager.shutdown()
            assert await manager.connection_count() == 0
            assert all(
                connection.writer_task is not None
                and connection.writer_task.done()
                and connection.closed
                for connection in (first, second)
            )
            assert _pending_writer_tasks() == []

            rejected = _node_connection(store, ControlledSocket())
            with pytest.raises(ConnectionLimitError):
                await manager.register(rejected)
        finally:
            store.close()

    asyncio.run(scenario())
