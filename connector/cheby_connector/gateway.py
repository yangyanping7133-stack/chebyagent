from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .models import (
    ApprovalsDecideParams,
    AssetsUploadParams,
    EmptyParams,
    EventsAckParams,
    EventsSubscribeParams,
    ThreadIdParams,
    ThreadsCreateParams,
    ThreadsDeleteConfirmParams,
    ThreadsListParams,
    ThreadsPatchParams,
    ThreadsReadParams,
    TurnsInterruptParams,
    TurnsStartParams,
)


class GatewayBackend(Protocol):
    device_id: Optional[str]
    stream_id: Optional[str]

    async def bind_device(self, device_id: str) -> None: ...

    async def dispatch(self, operation: str, params: object) -> dict[str, Any]: ...

    def replay_events(self, after_seq: int) -> list[Any]: ...

    async def close(self) -> None: ...


@dataclass
class DirectGatewayBackend:
    """Typed in-process boundary; no reverse proxy or local network listener."""

    service: Any
    store: Any
    connector_name: str
    device: Any = None
    owns_store: bool = False

    @property
    def device_id(self) -> Optional[str]:
        return None if self.device is None else str(self.device.id)

    @property
    def stream_id(self) -> Optional[str]:
        return None if self.device is None else str(self.device.stream_id)

    @classmethod
    async def from_env(
        cls,
        connector_name: str,
        *,
        runtime_profile: str = "production",
        gate_script_state_path: str = "/data/gate-script-state.json",
    ) -> "DirectGatewayBackend":
        from cheby_gateway.bridge import FakeCodexBridge, StdioCodexBridge
        from cheby_gateway.config import GatewaySettings
        from cheby_gateway.service import GatewayService
        from cheby_gateway.store import GatewayStore

        settings = GatewaySettings.from_env()
        store = GatewayStore(settings.db_path, event_retention=settings.event_retention)
        service = None
        try:
            if runtime_profile not in {"production", "gate"}:
                raise RuntimeError("unsupported Connector runtime profile")
            if runtime_profile == "gate":
                if settings.bridge_mode != "fake":
                    raise RuntimeError(
                        "Gate runtime requires the scripted fake bridge"
                    )
                from .gate_bridge import ScriptedGateCodexBridge

                bridge = ScriptedGateCodexBridge(gate_script_state_path)
            elif settings.bridge_mode == "stdio":
                bridge = StdioCodexBridge(
                    command=settings.codex_command,
                    cwd=settings.codex_cwd,
                    request_timeout_seconds=settings.request_timeout_seconds,
                    protocol_frame_limit_bytes=settings.protocol_frame_limit_bytes,
                )
            elif settings.bridge_mode == "fake":
                bridge = FakeCodexBridge()
            else:  # GatewaySettings rejects this; keep the boundary fail-closed.
                raise RuntimeError("unsupported Gateway bridge mode")
            service = GatewayService(settings, store, bridge)
            await service.start()
        except BaseException:
            if service is not None:
                with contextlib.suppress(Exception):
                    await service.close()
            store.close()
            raise
        return cls(
            service=service,
            store=store,
            connector_name=connector_name,
            owns_store=True,
        )

    async def bind_device(self, device_id: str) -> None:
        if self.device is not None and self.device.id != device_id:
            raise ValueError("Connector is already bound to another Relay device")
        self.device = self.service.ensure_relay_device(device_id, self.connector_name)

    async def close(self) -> None:
        await self.service.close()
        if self.owns_store:
            self.store.close()

    async def dispatch(self, operation: str, params: object) -> dict[str, Any]:
        from cheby_gateway.models import ApprovalDecision

        if self.device is None:
            raise RuntimeError("Connector has no Relay device binding")

        if operation == "server.info":
            _expect(params, EmptyParams)
            return {
                "protocolVersion": 1,
                "bridgeMode": self.service.settings.bridge_mode,
                "codexConnected": bool(self.service.bridge.connected),
            }
        if operation == "capabilities.get":
            _expect(params, EmptyParams)
            return {"v": 1, "inputs": self.service.local_image_capability()}
        if operation == "threads.list":
            params = _expect(params, ThreadsListParams)
            threads = await self.service.sync_threads(
                self.device, archived=params.archived
            )
            return {"data": [_dump(item) for item in threads]}
        if operation == "threads.create":
            params = _expect(params, ThreadsCreateParams)
            return _dump(await self.service.create_thread(self.device, params.title))
        if operation == "threads.read":
            params = _expect(params, ThreadsReadParams)
            return _dump(
                await self.service.read_thread(
                    params.thread_id,
                    self.device,
                    message_limit=params.message_limit,
                    message_cursor=params.message_cursor,
                )
            )
        if operation == "threads.resume":
            params = _expect(params, ThreadIdParams)
            return _dump(
                await self.service.resume_thread(self.device, params.thread_id)
            )
        if operation == "threads.patch":
            params = _expect(params, ThreadsPatchParams)
            return _dump(
                await self.service.patch_thread(
                    self.device,
                    params.thread_id,
                    title=params.title,
                    archived=params.archived,
                )
            )
        if operation == "threads.delete.preview":
            params = _expect(params, ThreadIdParams)
            return _dump(
                await self.service.preview_delete_thread(
                    self.device, params.thread_id
                )
            )
        if operation == "threads.delete.confirm":
            params = _expect(params, ThreadsDeleteConfirmParams)
            await self.service.delete_thread(
                self.device, params.thread_id, params.impact_token
            )
            return {"deleted": True}
        if operation == "turns.start":
            params = _expect(params, TurnsStartParams)
            parts = [item.model_dump(by_alias=True) for item in params.input]
            return _dump(
                await self.service.start_turn(
                    self.device,
                    params.thread_id,
                    params.client_message_id,
                    parts,
                )
            )
        if operation == "assets.upload":
            params = _expect(params, AssetsUploadParams)
            body = params.decoded_body()
            _validate_media_claim(params.media_type, body)
            result = await self.service.upload_image(
                self.device,
                params.thread_id,
                params.client_message_id,
                params.client_asset_id,
                body,
            )
            record = result.record
            return {
                "assetRef": record.asset_ref,
                "mediaType": record.media_type,
                "width": record.width,
                "height": record.height,
                "byteCount": record.byte_count,
                "expiresAt": record.expires_at,
                "created": bool(result.created),
            }
        if operation == "turns.interrupt":
            params = _expect(params, TurnsInterruptParams)
            await self.service.interrupt_turn(
                self.device, params.thread_id, params.turn_id
            )
            return {"interrupted": True}
        if operation == "approvals.decide":
            params = _expect(params, ApprovalsDecideParams)
            approval = await self.service.resolve_approval(
                self.device,
                params.approval_id,
                params.action_token,
                ApprovalDecision(params.decision),
            )
            return {"approval": _dump(approval)}
        if operation == "events.subscribe":
            params = _expect(params, EventsSubscribeParams)
            replay = self.store.replay_events(
                self.device.id, self.device.stream_id, params.after_seq
            )
            return {
                "streamId": self.device.stream_id,
                "currentSeq": replay.current_seq,
                "syncRequired": replay.sync_required,
            }
        if operation == "events.ack":
            params = _expect(params, EventsAckParams)
            if params.stream_id != self.device.stream_id or not self.store.acknowledge(
                self.device.id, params.stream_id, params.seq
            ):
                from cheby_gateway.service import GatewayError

                raise GatewayError(
                    "EVENT_ACK_INVALID",
                    "Event acknowledgement is invalid",
                    status_code=409,
                )
            return {"acknowledged": True}
        raise RuntimeError("operation dispatch table is incomplete")

    def replay_events(self, after_seq: int) -> list[Any]:
        if self.device is None:
            raise RuntimeError("Connector has no Relay device binding")
        replay = self.store.replay_events(
            self.device.id, self.device.stream_id, after_seq
        )
        if not replay.sync_required:
            return list(replay.events)
        event = self.store.append_event(
            self.device.id,
            "sync.required",
            {"reason": "eventGap"},
            thread_id="thr_gateway_sync",
        )
        return [event]


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


def _expect(value: object, expected: type[Any]) -> Any:
    if not isinstance(value, expected):
        raise ValueError("operation parameter type is invalid")
    return value


def _validate_media_claim(media_type: str, body: bytes) -> None:
    detected = None
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif body.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    if detected != media_type:
        raise ValueError("mediaType does not match the decoded asset")
