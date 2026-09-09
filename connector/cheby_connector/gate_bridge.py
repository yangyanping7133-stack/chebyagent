from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from cheby_gateway.bridge import (
    BridgeError,
    BridgeEvent,
    BridgeThread,
    BridgeTurn,
    FakeCodexBridge,
)


MAX_GATE_STATE_BYTES = 8 * 1024 * 1024
GATE_REPLY_PREFIX = "CHEBY_GATE_REPLY_V1_b71f3d69f2f14e25a4c71c5db6632f8e"
GATE_CONCURRENCY_PREFIX = "Gate-Concurrency"
LOAD_FIRST_TURN_MARKER = re.compile(
    r"^GATE-([a-f0-9]{32})-G[1-9][0-9]*-T[1-9][0-9]*-N1$"
)


class ScriptedGateCodexBridge(FakeCodexBridge):
    """Deterministic, durable fake used only by the isolated 27462 gate."""

    def __init__(
        self,
        state_path: str,
        *,
        load_barrier_timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(auto_events=False)
        self._state_path = Path(state_path)
        if not self._state_path.is_absolute():
            raise ValueError("Gate script state path must be absolute")
        if not 0 < load_barrier_timeout_seconds <= 120:
            raise ValueError("Gate load barrier timeout is invalid")
        self._thread_counter = 0
        self._turn_counter = 0
        self._load_barrier_timeout_seconds = load_barrier_timeout_seconds
        self._load_barriers: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    async def start_thread(self, title: Optional[str] = None) -> BridgeThread:
        self._thread_counter += 1
        raw_id = f"gate-thread-{self._thread_counter:08d}"
        value = {
            "id": raw_id,
            "title": title or "New conversation",
            "preview": "",
            "status": "idle",
            "archived": False,
            "turns": [],
        }
        self.threads[raw_id] = value
        self._persist_state()
        return self._thread_value(value)

    async def set_thread_name(self, raw_thread_id: str, title: str) -> None:
        await super().set_thread_name(raw_thread_id, title)
        self._persist_state()

    async def set_thread_archived(
        self, raw_thread_id: str, archived: bool
    ) -> None:
        await super().set_thread_archived(raw_thread_id, archived)
        self._persist_state()

    async def delete_thread(self, raw_thread_id: str) -> None:
        affected = set(self._descendant_raw_ids(raw_thread_id))
        await super().delete_thread(raw_thread_id)
        self.turns = {
            turn_id: turn
            for turn_id, turn in self.turns.items()
            if turn.get("threadId") not in affected
        }
        self._persist_state()

    async def start_turn(
        self,
        raw_thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> BridgeTurn:
        thread = self.threads.get(raw_thread_id)
        if thread is None:
            raise BridgeError("thread not found")

        self.start_turn_calls += 1
        current_text = _input_text(input_parts)
        concurrency_proof = await self._await_load_concurrency_barrier(
            raw_thread_id,
            current_text,
        )
        self._turn_counter += 1
        sequence = self._turn_counter
        raw_turn_id = f"gate-turn-{sequence:08d}"
        raw_user_item_id = f"gate-user-{sequence:08d}"
        raw_agent_item_id = f"gate-agent-{sequence:08d}"
        remembered_text = _last_user_text(thread)
        reply = _scripted_reply(current_text, remembered_text)
        if concurrency_proof is not None:
            reply = f"{reply}\n{concurrency_proof}"
        turn = {
            "id": raw_turn_id,
            "threadId": raw_thread_id,
            "status": "completed",
            "clientMessageId": client_message_id,
            "input": input_parts,
            "items": [
                {
                    "id": raw_user_item_id,
                    "type": "userMessage",
                    "status": "completed",
                    "clientId": client_message_id,
                    "content": input_parts,
                },
                {
                    "id": raw_agent_item_id,
                    "type": "agentMessage",
                    "status": "completed",
                    "text": reply,
                },
            ],
        }
        self.turns[raw_turn_id] = turn
        thread.setdefault("turns", []).append(turn)
        thread["preview"] = reply
        thread["status"] = "idle"
        self._persist_state()
        asyncio.create_task(
            self._emit_scripted_turn(
                raw_thread_id,
                raw_turn_id,
                raw_agent_item_id,
                reply,
            )
        )
        return BridgeTurn(raw_id=raw_turn_id, status="inProgress")

    async def _await_load_concurrency_barrier(
        self,
        raw_thread_id: str,
        current_text: str,
    ) -> Optional[str]:
        marker = LOAD_FIRST_TURN_MARKER.fullmatch(current_text)
        if marker is None:
            return None
        run_token = marker.group(1)
        barrier = self._load_barriers.setdefault(
            run_token,
            {
                "threads": set(),
                "released": asyncio.Event(),
            },
        )
        threads = barrier["threads"]
        released = barrier["released"]
        if not isinstance(threads, set) or not isinstance(released, asyncio.Event):
            raise BridgeError("load gate concurrency state is invalid")
        threads.add(raw_thread_id)
        if len(threads) >= 2:
            released.set()
        try:
            await asyncio.wait_for(
                released.wait(),
                timeout=self._load_barrier_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise BridgeError(
                "load gate cross-thread submission barrier was not satisfied"
            ) from error
        return f"{GATE_CONCURRENCY_PREFIX}: {run_token}:2"

    async def interrupt_turn(self, raw_thread_id: str, raw_turn_id: str) -> None:
        await super().interrupt_turn(raw_thread_id, raw_turn_id)
        self._persist_state()

    async def _emit_scripted_turn(
        self,
        raw_thread_id: str,
        raw_turn_id: str,
        raw_item_id: str,
        reply: str,
    ) -> None:
        await asyncio.sleep(0)
        await self._emit(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": raw_turn_id,
                        "status": "inProgress",
                        "items": [],
                    },
                },
            )
        )
        await self._emit(
            BridgeEvent(
                method="item/agentMessage/delta",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "itemId": raw_item_id,
                    "delta": reply,
                },
            )
        )
        await self._emit(
            BridgeEvent(
                method="item/completed",
                params={
                    "threadId": raw_thread_id,
                    "turnId": raw_turn_id,
                    "item": {
                        "id": raw_item_id,
                        "type": "agentMessage",
                        "text": reply,
                    },
                },
            )
        )
        await self._emit(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {
                        "id": raw_turn_id,
                        "status": "completed",
                        "items": [],
                    },
                },
            )
        )

    def _load_state(self) -> None:
        try:
            descriptor = os.open(
                self._state_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError("Gate script state cannot be read") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size > MAX_GATE_STATE_BYTES
            ):
                raise RuntimeError("Gate script state is unsafe")
            chunks = bytearray()
            while len(chunks) <= MAX_GATE_STATE_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        MAX_GATE_STATE_BYTES + 1 - len(chunks),
                    ),
                )
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_GATE_STATE_BYTES:
            raise RuntimeError("Gate script state is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Gate script state is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {"v", "threadCounter", "turnCounter", "threads", "turns"}
            or value.get("v") != 1
            or type(value.get("threadCounter")) is not int
            or type(value.get("turnCounter")) is not int
            or not isinstance(value.get("threads"), dict)
            or not isinstance(value.get("turns"), dict)
        ):
            raise RuntimeError("Gate script state has an invalid schema")
        if value["threadCounter"] < 0 or value["turnCounter"] < 0:
            raise RuntimeError("Gate script counters are invalid")
        self._thread_counter = value["threadCounter"]
        self._turn_counter = value["turnCounter"]
        self.threads = value["threads"]
        self.turns = value["turns"]

    def _persist_state(self) -> None:
        parent = self._state_path.parent
        if not parent.is_dir():
            raise RuntimeError("Gate script state directory does not exist")
        payload = json.dumps(
            {
                "v": 1,
                "threadCounter": self._thread_counter,
                "turnCounter": self._turn_counter,
                "threads": self.threads,
                "turns": self.turns,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAX_GATE_STATE_BYTES:
            raise RuntimeError("Gate script state is too large")
        try:
            current = os.lstat(self._state_path)
        except FileNotFoundError:
            pass
        else:
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_mode & 0o077
                or current.st_uid != os.geteuid()
                or current.st_nlink != 1
            ):
                raise RuntimeError("Gate script state is unsafe")
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short Gate state write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self._state_path)
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _input_text(input_parts: List[Dict[str, Any]]) -> str:
    text = "\n".join(
        part["text"]
        for part in input_parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()
    return text[:4096] or "[non-text input]"


def _last_user_text(thread: Dict[str, Any]) -> str:
    for turn in reversed(thread.get("turns") or []):
        if not isinstance(turn, dict):
            continue
        parts = turn.get("input")
        if isinstance(parts, list):
            return _input_text(parts)
    return ""


def _scripted_reply(current_text: str, remembered_text: str) -> str:
    if remembered_text:
        return (
            f"{GATE_REPLY_PREFIX}\n"
            f"Remembered: {remembered_text}\n"
            f"Current: {current_text}"
        )
    return f"{GATE_REPLY_PREFIX}\nReceived: {current_text}"
