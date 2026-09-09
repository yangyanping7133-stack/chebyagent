from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set


ALLOWED_CODEX_METHODS: Set[str] = {
    "thread/start",
    "thread/list",
    "thread/read",
    "thread/resume",
    "thread/name/set",
    "thread/archive",
    "thread/unarchive",
    "thread/delete",
    "turn/start",
    "turn/interrupt",
}

APPROVAL_REQUEST_METHODS: Set[str] = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}

# app-server uses newline-delimited JSON and can return a complete long thread
# as one line. Keep a deliberate upper bound while avoiding asyncio's ~64 KiB
# default StreamReader limit, which is too small for real Codex history.
MAX_PROTOCOL_FRAME_BYTES = 64 * 1024 * 1024
THREAD_LIST_PAGE_SIZE = 100
THREAD_LIST_MAX_PAGES = 1_000
CODEX_PERMISSION_PROFILE = "cheby_mobile"
THREAD_SOURCE_KINDS = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)


class BridgeError(RuntimeError):
    pass


class BridgeMethodDenied(BridgeError):
    pass


class BridgeUnavailable(BridgeError):
    pass


class BridgeNotAccepted(BridgeUnavailable):
    """The request was definitely not accepted and is safe to retry."""


class BridgeThreadNotFound(BridgeNotAccepted):
    """Codex explicitly reported that the requested Thread is not loaded."""


class BridgeDeliveryUnknown(BridgeUnavailable):
    """The request may have been accepted; automatic retry is unsafe."""


class BridgeFrameTooLarge(BridgeUnavailable):
    """app-server emitted one NDJSON frame above the configured hard cap."""


@dataclass(frozen=True)
class BridgeThread:
    raw_id: str
    title: str = "New conversation"
    preview: str = ""
    status: str = "idle"
    archived: Optional[bool] = None
    parent_raw_id: Optional[str] = None
    source_kind: str = "unknown"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    turns: Sequence["BridgeTurn"] = field(default_factory=tuple)


@dataclass(frozen=True)
class BridgeTurn:
    raw_id: str
    status: str = "inProgress"
    client_message_id: Optional[str] = None
    items: Sequence["BridgeItem"] = field(default_factory=tuple)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    @property
    def created_at(self) -> Optional[str]:
        return self.started_at

    @property
    def updated_at(self) -> Optional[str]:
        return self.completed_at or self.started_at


@dataclass(frozen=True)
class BridgeItem:
    """Lossless-enough Codex history item used by the public projection layer."""

    raw_id: str
    type: str
    status: Optional[str] = None
    content: Any = None
    text: Optional[str] = None
    client_message_id: Optional[str] = None
    phase: Optional[str] = None
    ordinal: int = 0
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    change_count: int = 0
    evidence_source: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class BridgeEvent:
    method: str
    params: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[Any] = None


BridgeEventHandler = Callable[[BridgeEvent], Awaitable[None]]


class CodexBridge(ABC):
    def __init__(self) -> None:
        self._event_handler: Optional[BridgeEventHandler] = None

    def set_event_handler(self, handler: BridgeEventHandler) -> None:
        self._event_handler = handler

    def clear_event_handler(self, handler: BridgeEventHandler) -> None:
        if self._event_handler is handler:
            self._event_handler = None

    async def _emit(self, event: BridgeEvent) -> None:
        if self._event_handler is not None:
            await self._event_handler(event)

    @property
    @abstractmethod
    def connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_threads(self, archived: bool = False) -> List[BridgeThread]:
        raise NotImplementedError

    @abstractmethod
    async def start_thread(self, title: Optional[str] = None) -> BridgeThread:
        raise NotImplementedError

    @abstractmethod
    async def read_thread(self, raw_thread_id: str) -> BridgeThread:
        raise NotImplementedError

    @abstractmethod
    async def resume_thread(self, raw_thread_id: str) -> BridgeThread:
        raise NotImplementedError

    @abstractmethod
    async def set_thread_name(self, raw_thread_id: str, title: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def set_thread_archived(self, raw_thread_id: str, archived: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_thread(self, raw_thread_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def start_turn(
        self,
        raw_thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> BridgeTurn:
        raise NotImplementedError

    @abstractmethod
    async def interrupt_turn(self, raw_thread_id: str, raw_turn_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resolve_approval(self, request_id: Any, decision: str) -> None:
        raise NotImplementedError

    async def request_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        raise BridgeMethodDenied("generic Codex RPC is not exposed")

    async def runtime_version(self) -> Optional[str]:
        return None


class FakeCodexBridge(CodexBridge):
    def __init__(
        self,
        auto_events: bool = True,
        runtime_version: Optional[str] = "0.144.6",
    ) -> None:
        super().__init__()
        self._connected = False
        self.auto_events = auto_events
        self.threads: Dict[str, Dict[str, Any]] = {}
        self.turns: Dict[str, Dict[str, Any]] = {}
        self.start_turn_calls = 0
        self.approval_responses: Dict[str, str] = {}
        self._runtime_version = runtime_version

    async def runtime_version(self) -> Optional[str]:
        return self._runtime_version

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def list_threads(self, archived: bool = False) -> List[BridgeThread]:
        return [
            self._thread_value(value)
            for value in self.threads.values()
            if bool(value.get("archived", False)) == archived
        ]

    async def start_thread(self, title: Optional[str] = None) -> BridgeThread:
        raw_id = "raw-thread-%s" % uuid.uuid4().hex
        value = {
            "id": raw_id,
            "title": title or "New conversation",
            "preview": "",
            "status": "idle",
            "archived": False,
            "turns": [],
        }
        self.threads[raw_id] = value
        return self._thread_value(value)

    async def read_thread(self, raw_thread_id: str) -> BridgeThread:
        value = self.threads.get(raw_thread_id)
        if value is None:
            raise BridgeThreadNotFound("Codex thread is not loaded")
        return self._thread_value(value)

    async def resume_thread(self, raw_thread_id: str) -> BridgeThread:
        return await self.read_thread(raw_thread_id)

    async def set_thread_name(self, raw_thread_id: str, title: str) -> None:
        value = self.threads.get(raw_thread_id)
        if value is None:
            raise BridgeThreadNotFound("Codex thread is not loaded")
        value["title"] = title

    async def set_thread_archived(self, raw_thread_id: str, archived: bool) -> None:
        value = self.threads.get(raw_thread_id)
        if value is None:
            raise BridgeThreadNotFound("Codex thread is not loaded")
        affected = self._descendant_raw_ids(raw_thread_id)
        for affected_raw_id in affected:
            affected_value = self.threads[affected_raw_id]
            affected_value["archived"] = archived
            affected_value["status"] = "archived" if archived else "idle"

    async def delete_thread(self, raw_thread_id: str) -> None:
        if raw_thread_id not in self.threads:
            raise BridgeThreadNotFound("Codex thread is not loaded")
        for affected_raw_id in self._descendant_raw_ids(raw_thread_id):
            self.threads.pop(affected_raw_id, None)

    def _descendant_raw_ids(self, raw_thread_id: str) -> List[str]:
        affected: List[str] = []
        pending = [raw_thread_id]
        while pending:
            current = pending.pop(0)
            if current in affected or current not in self.threads:
                continue
            affected.append(current)
            pending.extend(
                candidate_id
                for candidate_id, value in self.threads.items()
                if value.get("parentThreadId") == current
            )
        return affected

    async def start_turn(
        self,
        raw_thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> BridgeTurn:
        if raw_thread_id not in self.threads:
            raise BridgeError("thread not found")
        self.start_turn_calls += 1
        raw_turn_id = "raw-turn-%s" % uuid.uuid4().hex
        raw_item_id = "raw-item-%s" % uuid.uuid4().hex
        self.turns[raw_turn_id] = {
            "id": raw_turn_id,
            "threadId": raw_thread_id,
            "status": "inProgress",
            "clientMessageId": client_message_id,
            "input": input_parts,
            "items": [
                {
                    "id": "raw-user-item-%s" % uuid.uuid4().hex,
                    "type": "userMessage",
                    "status": "completed",
                    "clientId": client_message_id,
                    "content": input_parts,
                }
            ],
        }
        self.threads[raw_thread_id].setdefault("turns", []).append(
            self.turns[raw_turn_id]
        )
        self.threads[raw_thread_id]["status"] = "active"
        if self.auto_events:
            asyncio.create_task(
                self._emit_fake_turn(raw_thread_id, raw_turn_id, raw_item_id)
            )
        return BridgeTurn(raw_id=raw_turn_id, status="inProgress")

    async def _emit_fake_turn(
        self,
        raw_thread_id: str,
        raw_turn_id: str,
        raw_item_id: str,
    ) -> None:
        await asyncio.sleep(0)
        await self._emit(
            BridgeEvent(
                method="turn/started",
                params={
                    "threadId": raw_thread_id,
                    "turn": {"id": raw_turn_id, "status": "inProgress", "items": []},
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
                    "delta": "Fake Codex response",
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
                        "text": "Fake Codex response",
                    },
                },
            )
        )
        self.turns[raw_turn_id]["status"] = "completed"
        self.threads[raw_thread_id]["status"] = "idle"
        await self._emit(
            BridgeEvent(
                method="turn/completed",
                params={
                    "threadId": raw_thread_id,
                    "turn": {"id": raw_turn_id, "status": "completed", "items": []},
                },
            )
        )

    async def interrupt_turn(self, raw_thread_id: str, raw_turn_id: str) -> None:
        turn = self.turns.get(raw_turn_id)
        if turn is None or turn["threadId"] != raw_thread_id:
            raise BridgeError("turn not found")
        turn["status"] = "interrupted"
        self.threads[raw_thread_id]["status"] = "idle"

    async def resolve_approval(self, request_id: Any, decision: str) -> None:
        key = json.dumps(request_id, separators=(",", ":"), sort_keys=True)
        if key in self.approval_responses:
            raise BridgeError("approval already resolved")
        self.approval_responses[key] = decision

    async def emit_event(self, event: BridgeEvent) -> None:
        await self._emit(event)

    @staticmethod
    def _thread_value(value: Dict[str, Any]) -> BridgeThread:
        return BridgeThread(
            raw_id=value["id"],
            title=value.get("title") or "New conversation",
            preview=value.get("preview") or "",
            status=value.get("status") or "idle",
            archived=bool(value.get("archived", False)),
            parent_raw_id=(
                str(value["parentThreadId"])
                if value.get("parentThreadId")
                else None
            ),
            source_kind=str(value.get("sourceKind") or "unknown"),
            created_at=value.get("createdAt"),
            updated_at=value.get("updatedAt"),
            turns=tuple(
                StdioCodexBridge._parse_turn(turn)
                for turn in (value.get("turns") or [])
                if isinstance(turn, dict)
            ),
        )


class StdioCodexBridge(CodexBridge):
    def __init__(
        self,
        command: Sequence[str],
        cwd: str,
        request_timeout_seconds: float = 30.0,
        protocol_frame_limit_bytes: int = MAX_PROTOCOL_FRAME_BYTES,
    ) -> None:
        super().__init__()
        self.command = tuple(command)
        self.cwd = cwd
        self.request_timeout_seconds = request_timeout_seconds
        self.protocol_frame_limit_bytes = max(1024, protocol_frame_limit_bytes)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._request_counter = 0
        self._reader_failed = False

    @property
    def connected(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and not self._reader_failed
        )

    async def runtime_version(self) -> Optional[str]:
        if not self.command:
            return None
        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.command[0],
                "--version",
                cwd=self.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            if process is not None:
                with contextlib.suppress(Exception):
                    process.kill()
                    await process.wait()
            return None
        if process.returncode != 0 or len(stdout) > 4096:
            return None
        match = re.search(rb"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)", stdout)
        return match.group(1).decode("ascii") if match else None

    async def start(self) -> None:
        if self.connected:
            return
        self._reader_failed = False
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.protocol_frame_limit_bytes,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._request_unchecked(
            "initialize",
            {
                "clientInfo": {
                    "name": "chebycodex_mobile_gateway",
                    "title": "ChebyCodex Mobile Gateway",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._write_message({"method": "initialized", "params": {}})

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionError):
                    pass
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None
        ]
        self._reader_task = None
        self._stderr_task = None
        for task in tasks:
            if task is not None:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            # Python 3.9 on macOS may otherwise defer pipe transport cleanup
            # until after the per-test event loop has closed.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeDeliveryUnknown("Codex bridge closed"))
        self._pending.clear()

    async def request_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method not in ALLOWED_CODEX_METHODS:
            raise BridgeMethodDenied("Codex method is not allowlisted")
        return await self._request_unchecked(method, params)

    async def list_threads(self, archived: bool = False) -> List[BridgeThread]:
        threads: List[BridgeThread] = []
        seen_raw_ids: Set[str] = set()
        cursor: Optional[str] = None
        seen_cursors: Set[str] = set()
        for _ in range(THREAD_LIST_MAX_PAGES):
            params: Dict[str, Any] = {
                "limit": THREAD_LIST_PAGE_SIZE,
                "sourceKinds": list(THREAD_SOURCE_KINDS),
                "archived": archived,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await self.request_method("thread/list", params)
            data = response.get("data")
            if not isinstance(data, list):
                raise BridgeError("Codex returned an invalid thread page")
            for value in data:
                if not isinstance(value, dict):
                    raise BridgeError("Codex returned an invalid thread entry")
                parsed = self._parse_thread(value, archived=archived)
                if parsed.raw_id in seen_raw_ids:
                    raise BridgeError("Codex repeated a thread across pages")
                seen_raw_ids.add(parsed.raw_id)
                threads.append(parsed)
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return threads
            if not isinstance(next_cursor, str) or not next_cursor:
                raise BridgeError("Codex returned an invalid thread cursor")
            if next_cursor in seen_cursors:
                raise BridgeError("Codex repeated a thread cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise BridgeError("Codex thread pagination exceeded its safety bound")

    async def start_thread(self, title: Optional[str] = None) -> BridgeThread:
        response = await self.request_method(
            "thread/start",
            {
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "permissions": CODEX_PERMISSION_PROFILE,
                "serviceName": "chebycodex_mobile_gateway",
            },
        )
        thread = self._parse_thread(response.get("thread", {}), archived=False)
        if title:
            await self.set_thread_name(thread.raw_id, title)
            thread = BridgeThread(
                raw_id=thread.raw_id,
                title=title,
                preview=thread.preview,
                status=thread.status,
                archived=thread.archived,
                parent_raw_id=thread.parent_raw_id,
                source_kind=thread.source_kind,
                created_at=thread.created_at,
                updated_at=thread.updated_at,
                turns=thread.turns,
            )
        return thread

    async def read_thread(self, raw_thread_id: str) -> BridgeThread:
        response = await self.request_method(
            "thread/read", {"threadId": raw_thread_id, "includeTurns": True}
        )
        return self._parse_thread(response.get("thread", {}))

    async def resume_thread(self, raw_thread_id: str) -> BridgeThread:
        response = await self.request_method(
            "thread/resume",
            {
                "threadId": raw_thread_id,
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "permissions": CODEX_PERMISSION_PROFILE,
            },
        )
        return self._parse_thread(response.get("thread", {}))

    async def set_thread_name(self, raw_thread_id: str, title: str) -> None:
        await self.request_method(
            "thread/name/set", {"threadId": raw_thread_id, "name": title}
        )

    async def set_thread_archived(self, raw_thread_id: str, archived: bool) -> None:
        method = "thread/archive" if archived else "thread/unarchive"
        await self.request_method(method, {"threadId": raw_thread_id})

    async def delete_thread(self, raw_thread_id: str) -> None:
        await self.request_method("thread/delete", {"threadId": raw_thread_id})

    async def start_turn(
        self,
        raw_thread_id: str,
        client_message_id: str,
        input_parts: List[Dict[str, Any]],
    ) -> BridgeTurn:
        response = await self.request_method(
            "turn/start",
            {
                "threadId": raw_thread_id,
                "clientUserMessageId": client_message_id,
                "input": input_parts,
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "permissions": CODEX_PERMISSION_PROFILE,
            },
        )
        turn = response.get("turn") or {}
        raw_id = turn.get("id")
        if not raw_id:
            raise BridgeDeliveryUnknown("Codex returned a turn without an id")
        return BridgeTurn(raw_id=str(raw_id), status=str(turn.get("status", "inProgress")))

    async def interrupt_turn(self, raw_thread_id: str, raw_turn_id: str) -> None:
        await self.request_method(
            "turn/interrupt", {"threadId": raw_thread_id, "turnId": raw_turn_id}
        )

    async def resolve_approval(self, request_id: Any, decision: str) -> None:
        codex_decision = "accept" if decision == "approve" else "decline"
        await self._write_message(
            {"id": request_id, "result": {"decision": codex_decision}}
        )

    async def _request_unchecked(
        self, method: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not self.connected:
            raise BridgeNotAccepted("Codex app-server is not connected")
        self._request_counter += 1
        request_id = "gw-%d" % self._request_counter
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(
                {"id": request_id, "method": method, "params": params}
            )
        except BridgeNotAccepted:
            self._pending.pop(request_id, None)
            raise
        except BridgeError as exc:
            self._pending.pop(request_id, None)
            raise BridgeDeliveryUnknown("Codex request delivery is unknown") from exc
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            self._pending.pop(request_id, None)
            raise BridgeDeliveryUnknown("Codex request delivery is unknown") from exc
        try:
            result = await asyncio.wait_for(
                future, timeout=self.request_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise BridgeDeliveryUnknown("Codex request timed out") from exc
        if not isinstance(result, dict):
            raise BridgeError("Codex returned an invalid response")
        return result

    async def _write_message(self, message: Dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise BridgeNotAccepted("Codex app-server is not connected")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise BridgeDeliveryUnknown(
                    "Codex request delivery is unknown"
                ) from exc

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        terminal_error: Optional[BridgeError] = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                request_id = message.get("id")
                if request_id is not None and (
                    "result" in message or "error" in message
                ):
                    future = self._pending.pop(str(request_id), None)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message.get("error")
                        error_message = (
                            str(error.get("message") or "")
                            if isinstance(error, dict)
                            else ""
                        )
                        if error_message.lower().startswith(
                            ("thread not loaded:", "thread not found:")
                        ):
                            future.set_exception(
                                BridgeThreadNotFound("Codex thread is not loaded")
                            )
                        else:
                            future.set_exception(
                                BridgeNotAccepted("Codex request failed")
                            )
                    else:
                        future.set_result(message.get("result") or {})
                    continue
                method = message.get("method")
                params = message.get("params") or {}
                if not isinstance(method, str) or not isinstance(params, dict):
                    continue
                if request_id is not None and method not in APPROVAL_REQUEST_METHODS:
                    await self._write_message(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": "Client request is not supported",
                            },
                        }
                    )
                    continue
                await self._emit(
                    BridgeEvent(method=method, params=params, request_id=request_id)
                )
        except ValueError:
            terminal_error = BridgeFrameTooLarge(
                "Codex protocol frame exceeded the configured hard cap"
            )
        finally:
            self._reader_failed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        terminal_error
                        or BridgeDeliveryUnknown("Codex app-server exited")
                    )
            self._pending.clear()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.readline():
            # Raw app-server diagnostics may contain paths or prompt fragments.
            # They are deliberately consumed without forwarding to application logs.
            pass

    @staticmethod
    def _parse_thread(
        value: Dict[str, Any],
        archived: Optional[bool] = None,
    ) -> BridgeThread:
        raw_id = value.get("id")
        if not raw_id:
            raise BridgeError("Codex returned a thread without an id")
        status_value = value.get("status", "idle")
        if isinstance(status_value, dict):
            status_type = str(status_value.get("type", "idle"))
            flags = status_value.get("activeFlags") or []
            if "waitingOnApproval" in flags:
                status_value = "waitingApproval"
            elif "waitingOnUserInput" in flags:
                status_value = "waitingUser"
            elif status_type == "active":
                status_value = "running"
            elif status_type == "systemError":
                status_value = "failed"
            else:
                status_value = "idle"
        title = value.get("name") or value.get("preview") or "New conversation"
        return BridgeThread(
            raw_id=str(raw_id),
            title=str(title),
            preview=str(value.get("preview") or ""),
            status=str(status_value),
            archived=(
                archived
                if archived is not None
                else (
                    bool(value["archived"])
                    if "archived" in value
                    else None
                )
            ),
            parent_raw_id=(
                str(value["parentThreadId"])
                if value.get("parentThreadId")
                else None
            ),
            source_kind=StdioCodexBridge._source_kind(value.get("source")),
            created_at=_timestamp(value.get("createdAt")),
            updated_at=_timestamp(value.get("updatedAt")),
            turns=tuple(
                StdioCodexBridge._parse_turn(turn)
                for turn in (value.get("turns") or [])
                if isinstance(turn, dict)
            ),
        )

    @staticmethod
    def _source_kind(value: Any) -> str:
        if isinstance(value, str):
            return value if value in THREAD_SOURCE_KINDS else "unknown"
        if not isinstance(value, dict) or "subAgent" not in value:
            return "unknown"
        sub_agent = value.get("subAgent")
        if sub_agent == "review":
            return "subAgentReview"
        if sub_agent == "compact":
            return "subAgentCompact"
        if isinstance(sub_agent, dict) and "thread_spawn" in sub_agent:
            return "subAgentThreadSpawn"
        return "subAgentOther"

    @staticmethod
    def _parse_turn(value: Dict[str, Any]) -> BridgeTurn:
        raw_id = value.get("id")
        if not raw_id:
            raise BridgeError("Codex returned a turn without an id")
        return BridgeTurn(
            raw_id=str(raw_id),
            status=str(value.get("status") or "inProgress"),
            client_message_id=(
                str(value.get("clientId") or value.get("clientUserMessageId") or value.get("clientMessageId"))
                if value.get("clientId") or value.get("clientUserMessageId") or value.get("clientMessageId")
                else None
            ),
            items=tuple(
                StdioCodexBridge._parse_item(item, ordinal)
                for ordinal, item in enumerate(value.get("items") or [])
                if isinstance(item, dict)
            ),
            started_at=_timestamp(value.get("startedAt") or value.get("createdAt")),
            completed_at=_timestamp(value.get("completedAt") or value.get("updatedAt")),
        )

    @staticmethod
    def _parse_item(value: Dict[str, Any], ordinal: int = 0) -> BridgeItem:
        raw_id = value.get("id")
        if not raw_id:
            raise BridgeError("Codex returned a history item without an id")
        return BridgeItem(
            raw_id=str(raw_id),
            type=str(value.get("type") or "unknown"),
            status=(str(value["status"]) if value.get("status") is not None else None),
            content=value.get("content"),
            text=(str(value["text"]) if isinstance(value.get("text"), str) else None),
            client_message_id=(
                str(value.get("clientId") or value.get("clientUserMessageId") or value.get("clientMessageId"))
                if value.get("clientId") or value.get("clientUserMessageId") or value.get("clientMessageId")
                else None
            ),
            phase=(
                str(value["phase"])
                if value.get("phase") in {"commentary", "final_answer"}
                else None
            ),
            ordinal=ordinal,
            duration_ms=(
                int(value["durationMs"])
                if isinstance(value.get("durationMs"), int)
                else None
            ),
            exit_code=(
                int(value["exitCode"])
                if isinstance(value.get("exitCode"), int)
                else None
            ),
            change_count=(
                len(value.get("changes") or [])
                if isinstance(value.get("changes"), list)
                else 0
            ),
            evidence_source=_projection_evidence_source(value),
            created_at=_timestamp(value.get("createdAt")),
            updated_at=_timestamp(value.get("updatedAt")),
        )


def _projection_evidence_source(value: Dict[str, Any]) -> Dict[str, Any]:
    """Retain only explicitly structured evidence fields for later redaction.

    Command strings, aggregate output, arbitrary tool results, and debug
    payloads are intentionally excluded. The service applies the final public
    redaction and bounds before any value is persisted.
    """

    source: Dict[str, Any] = {}
    for key in (
        "toolName",
        "serverName",
        "tool",
        "server",
        "name",
        "title",
        "durationMs",
        "additions",
        "deletions",
        "fileLabel",
        "summary",
        "preview",
        "truncated",
        "passed",
        "failed",
        "skipped",
    ):
        if key in value:
            source[key] = value[key]
    changes = value.get("changes")
    if isinstance(changes, list):
        source["changes"] = [
            {
                key: change[key]
                for key in (
                    "path",
                    "fileLabel",
                    "additions",
                    "deletions",
                    "summary",
                    "preview",
                    "truncated",
                )
                if key in change
            }
            for change in changes[:64]
            if isinstance(change, dict)
        ]
    structured_tests = value.get("testEvidence")
    if isinstance(structured_tests, dict):
        source["testEvidence"] = {
            key: structured_tests[key]
            for key in (
                "title",
                "summary",
                "passed",
                "failed",
                "skipped",
                "durationMs",
                "status",
            )
            if key in structured_tests
        }
    return source


def _timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, OverflowError):
        return None
