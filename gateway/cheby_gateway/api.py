from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Sequence

from fastapi import (
    Depends,
    FastAPI,
    Header,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .bridge import FakeCodexBridge, StdioCodexBridge
from .config import GatewaySettings
from .models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    AuthRefreshRequest,
    AuthRefreshResponse,
    CapabilitiesResponse,
    CreateThreadRequest,
    DeleteResponse,
    DeleteImpactPreviewResponse,
    DeleteThreadRequest,
    ErrorDTO,
    EventAck,
    HealthResponse,
    ImageUploadResponse,
    InterruptResponse,
    LocalImageCapability,
    PairingExchangeRequest,
    PairingExchangeResponse,
    PatchThreadRequest,
    ServerInfoResponse,
    StartTurnRequest,
    ThreadDTO,
    ThreadDetailResponse,
    ThreadListResponse,
    TurnDTO,
)
from .pop import (
    ProofError,
    canonical_target,
    proof_subject_for_spki,
    verify_proof,
)
from .service import GatewayError, GatewayService
from .store import DeviceRecord, GatewayStore, RateLimitDecision


SYNC_THREAD_ID = "thr_gateway_sync"


class BoundedRequestBodyMiddleware:
    _IMAGE_UPLOAD = re.compile(
        r"^/v1/threads/[^/]+/turn-inputs/[^/]+/images/[^/]+$"
    )
    def __init__(self, app, settings: GatewaySettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        image_upload = method == "PUT" and bool(self._IMAGE_UPLOAD.fullmatch(path))
        limit = self._limit_for(path, method)
        content_lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await self._reject(send, 400, "INVALID_REQUEST")
            return
        transfer_encodings = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"transfer-encoding"
        ]
        if (
            len(transfer_encodings) > 1
            or (content_lengths and transfer_encodings)
            or (image_upload and transfer_encodings)
        ):
            await self._reject(send, 400, "INVALID_REQUEST")
            return
        declared = None
        if image_upload and len(content_lengths) != 1:
            await self._reject(send, 411, "CONTENT_LENGTH_REQUIRED")
            return
        if content_lengths:
            try:
                declared_text = content_lengths[0].decode("ascii")
            except (UnicodeDecodeError, ValueError):
                await self._reject(send, 400, "INVALID_REQUEST")
                return
            if (
                not declared_text.isdecimal()
                or len(declared_text) > 20
                or (len(declared_text) > 1 and declared_text.startswith("0"))
            ):
                await self._reject(send, 400, "INVALID_REQUEST")
                return
            declared = int(declared_text)
            if image_upload and declared < 1:
                await self._reject(send, 400, "INVALID_REQUEST")
                return
            if declared > limit:
                await self._reject(send, 413, "REQUEST_TOO_LARGE")
                return

        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                await self._reject(send, 400, "INVALID_REQUEST")
                return
            chunk = message.get("body", b"")
            chunks.extend(chunk)
            if len(chunks) > limit:
                await self._reject(send, 413, "REQUEST_TOO_LARGE")
                return
            if not message.get("more_body", False):
                break
        if image_upload and declared != len(chunks):
            await self._reject(send, 400, "INVALID_REQUEST")
            return

        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(chunks), "more_body": False}

        await self.app(scope, bounded_receive, send)

    def _limit_for(self, path: str, method: str) -> int:
        if method == "PUT" and self._IMAGE_UPLOAD.fullmatch(path):
            return 8 * 1024 * 1024
        if path == "/v1/pairings/exchange":
            return self.settings.pairing_body_limit_bytes
        if path == "/v1/auth/refresh":
            return self.settings.refresh_body_limit_bytes
        if method == "POST" and path.endswith("/turns"):
            return self.settings.turn_body_limit_bytes
        return self.settings.request_body_limit_bytes

    @staticmethod
    async def _reject(send, status: int, code: str) -> None:
        payload = json.dumps(
            {
                "error": {
                    "code": code,
                    "message": (
                        "Request body exceeds the allowed size"
                        if status == 413
                        else "Content-Length is required"
                        if status == 411
                        else "Request validation failed"
                    ),
                    "retryable": False,
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def create_app(
    settings: Optional[GatewaySettings] = None,
    store: Optional[GatewayStore] = None,
    bridge=None,
) -> FastAPI:
    resolved_settings = settings or GatewaySettings.from_env()
    resolved_store = store or GatewayStore(
        resolved_settings.db_path,
        event_retention=resolved_settings.event_retention,
    )
    resolved_bridge = bridge
    if resolved_bridge is None:
        if resolved_settings.bridge_mode == "stdio":
            resolved_bridge = StdioCodexBridge(
                command=resolved_settings.codex_command,
                cwd=resolved_settings.codex_cwd,
                request_timeout_seconds=resolved_settings.request_timeout_seconds,
                protocol_frame_limit_bytes=resolved_settings.protocol_frame_limit_bytes,
            )
        elif resolved_settings.bridge_mode == "fake":
            resolved_bridge = FakeCodexBridge()
        else:
            raise RuntimeError("unsupported Bridge mode")
    service = GatewayService(resolved_settings, resolved_store, resolved_bridge)
    trusted_proxy_networks = tuple(
        ipaddress.ip_network(value, strict=False)
        for value in resolved_settings.trusted_proxy_cidrs
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.close()
            if store is None:
                resolved_store.close()

    app = FastAPI(
        title="ChebyCodex Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(BoundedRequestBodyMiddleware, settings=resolved_settings)
    app.state.gateway_service = service
    app.state.gateway_store = resolved_store
    app.state.gateway_bridge = resolved_bridge

    @app.middleware("http")
    async def public_auth_rate_limit(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/v1/pairings/exchange":
            source = _client_source(request, trusted_proxy_networks)
            request.state.rate_limit_source = source
            decision = resolved_store.consume_rate_limits(
                [
                    (
                        "pairing.source",
                        source,
                        resolved_settings.pairing_source_limit,
                        resolved_settings.pairing_window_seconds,
                    ),
                    (
                        "pairing.global",
                        "all",
                        resolved_settings.pairing_global_limit,
                        resolved_settings.pairing_window_seconds,
                    ),
                ]
            )
            if not decision.allowed:
                return _rate_limited_response(decision)
        elif request.method == "POST" and request.url.path == "/v1/auth/refresh":
            source = _client_source(request, trusted_proxy_networks)
            request.state.rate_limit_source = source
            decision = resolved_store.consume_rate_limits(
                [
                    (
                        "refresh.source",
                        source,
                        resolved_settings.refresh_source_limit,
                        resolved_settings.refresh_window_seconds,
                    ),
                    (
                        "refresh.global",
                        "all",
                        resolved_settings.refresh_global_limit,
                        resolved_settings.refresh_window_seconds,
                    ),
                ]
            )
            if not decision.allowed:
                return _rate_limited_response(decision)
        return await call_next(request)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        body = ErrorDTO(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
        ).model_dump(by_alias=True)
        headers = None
        if exc.retry_after_seconds is not None:
            headers = {"Retry-After": str(max(1, exc.retry_after_seconds))}
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": body},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default validation response can echo rejected input. Pairing
        # secrets and action tokens must never be reflected in debug JSON.
        body = ErrorDTO(
            code="INVALID_REQUEST",
            message="Request validation failed",
            retryable=False,
        ).model_dump(by_alias=True)
        return JSONResponse(status_code=422, content={"error": body})

    async def verify_request_proof(
        request: Request,
        *,
        public_key: str,
        subject: str,
        bearer_token: str,
        error_code: str,
        error_message: str,
    ) -> None:
        try:
            proof = verify_proof(
                public_key_spki=public_key,
                headers=request.headers,
                method=request.method,
                raw_target=canonical_target(
                    request.scope.get("raw_path", request.url.path.encode("ascii")),
                    request.scope.get("query_string", b""),
                ),
                body=await request.body(),
                bearer_token=bearer_token,
                now_epoch=int(time.time()),
                allowed_skew_seconds=resolved_settings.proof_clock_skew_seconds,
            )
            claimed = resolved_store.claim_request_proof_nonce(
                subject,
                proof.nonce,
                proof.timestamp,
                int(time.time()),
                resolved_settings.proof_clock_skew_seconds * 2 + 1,
            )
            if not claimed:
                raise ProofError("proof nonce was already used")
        except (ProofError, UnicodeEncodeError):
            raise GatewayError(
                error_code,
                error_message,
                status_code=401,
                retryable=False,
            )

    async def authenticated_device(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> DeviceRecord:
        token = _bearer_token(authorization)
        device = service.authenticate(token)
        await verify_request_proof(
            request,
            public_key=device.public_key,
            subject="device:" + device.id,
            bearer_token=token,
            error_code="AUTH_REQUIRED",
            error_message="Authentication is required",
        )
        return device

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/v1/pairings/exchange",
        response_model=PairingExchangeResponse,
        response_model_by_alias=True,
    )
    async def pairing_exchange(
        request: Request,
        body: PairingExchangeRequest,
    ) -> PairingExchangeResponse:
        source = getattr(
            request.state,
            "rate_limit_source",
            _client_source(request, trusted_proxy_networks),
        )
        try:
            subject = proof_subject_for_spki(body.device_public_key)
            await verify_request_proof(
                request,
                public_key=body.device_public_key,
                subject=subject,
                bearer_token="",
                error_code="PAIRING_DENIED",
                error_message="Pairing credentials are not valid",
            )
            response = service.exchange_pairing(
                pairing_secret=body.pairing_secret,
                device_name=body.device_name,
                device_public_key=body.device_public_key,
            )
        except ProofError as exc:
            denied = GatewayError(
                "PAIRING_DENIED",
                "Pairing credentials are not valid",
                status_code=401,
            )
            resolved_store.penalize_rate_limit(
                "pairing.source",
                source,
                resolved_settings.pairing_backoff_base_seconds,
                resolved_settings.pairing_backoff_max_seconds,
                resolved_settings.pairing_window_seconds,
            )
            raise denied from exc
        except GatewayError as exc:
            if exc.code == "PAIRING_DENIED":
                resolved_store.penalize_rate_limit(
                    "pairing.source",
                    source,
                    resolved_settings.pairing_backoff_base_seconds,
                    resolved_settings.pairing_backoff_max_seconds,
                    resolved_settings.pairing_window_seconds,
                )
            raise
        resolved_store.clear_rate_limit_penalty("pairing.source", source)
        return response

    @app.post(
        "/v1/auth/refresh",
        response_model=AuthRefreshResponse,
        response_model_by_alias=True,
    )
    async def auth_refresh(
        request: Request,
        body: AuthRefreshRequest,
    ) -> AuthRefreshResponse:
        device = resolved_store.device_by_id(body.device_id)
        if device is None:
            raise GatewayError(
                "REFRESH_DENIED",
                "Refresh credentials are not valid",
                status_code=401,
            )
        await verify_request_proof(
            request,
            public_key=device.public_key,
            subject="device:" + device.id,
            bearer_token="",
            error_code="REFRESH_DENIED",
            error_message="Refresh credentials are not valid",
        )
        _enforce_rate_limit(
            resolved_store.consume_rate_limit(
                "refresh.device",
                device.id,
                resolved_settings.refresh_device_limit,
                resolved_settings.refresh_window_seconds,
            )
        )
        return service.refresh_access(body.device_id, body.refresh_token)

    @app.get(
        "/v1/server",
        response_model=ServerInfoResponse,
        response_model_by_alias=True,
    )
    async def server_info(
        _: DeviceRecord = Depends(authenticated_device),
    ) -> ServerInfoResponse:
        return ServerInfoResponse(
            bridge_mode=resolved_settings.bridge_mode,
            codex_connected=resolved_bridge.connected,
        )

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        response_model_by_alias=True,
    )
    async def capabilities(
        response: Response,
        _: DeviceRecord = Depends(authenticated_device),
    ) -> CapabilitiesResponse:
        response.headers["Cache-Control"] = "no-store"
        inputs = service.local_image_capability()
        return CapabilitiesResponse(
            inputs={
                key: LocalImageCapability.model_validate(value)
                for key, value in inputs.items()
            }
        )

    @app.get(
        "/v1/threads",
        response_model=ThreadListResponse,
        response_model_by_alias=True,
    )
    async def list_threads(
        archived: bool = Query(default=False),
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ThreadListResponse:
        return ThreadListResponse(
            data=await service.sync_threads(device, archived=archived)
        )

    @app.post(
        "/v1/threads",
        response_model=ThreadDTO,
        response_model_by_alias=True,
        status_code=201,
    )
    async def create_thread(
        body: CreateThreadRequest,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ThreadDTO:
        return await service.create_thread(device, body.title)

    @app.get(
        "/v1/threads/{thread_id}",
        response_model=ThreadDetailResponse,
        response_model_by_alias=True,
    )
    async def read_thread(
        thread_id: str,
        message_limit: Optional[int] = Query(
            default=None,
            alias="messageLimit",
            ge=1,
            le=100,
        ),
        message_cursor: Optional[str] = Query(
            default=None,
            alias="messageCursor",
            min_length=1,
            max_length=1024,
        ),
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ThreadDetailResponse:
        return await service.read_thread(
            thread_id,
            device,
            message_limit=message_limit,
            message_cursor=message_cursor,
        )

    @app.post(
        "/v1/threads/{thread_id}/resume",
        response_model=ThreadDTO,
        response_model_by_alias=True,
    )
    async def resume_thread(
        thread_id: str,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ThreadDTO:
        return await service.resume_thread(device, thread_id)

    @app.patch(
        "/v1/threads/{thread_id}",
        response_model=ThreadDTO,
        response_model_by_alias=True,
    )
    async def patch_thread(
        thread_id: str,
        body: PatchThreadRequest,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ThreadDTO:
        return await service.patch_thread(
            device,
            thread_id,
            title=body.title,
            archived=body.archived,
        )

    @app.post(
        "/v1/threads/{thread_id}/delete-preview",
        response_model=DeleteImpactPreviewResponse,
        response_model_by_alias=True,
    )
    async def preview_delete_thread(
        thread_id: str,
        response: Response,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> DeleteImpactPreviewResponse:
        response.headers["Cache-Control"] = "no-store"
        return await service.preview_delete_thread(device, thread_id)

    @app.delete(
        "/v1/threads/{thread_id}",
        response_model=DeleteResponse,
        response_model_by_alias=True,
    )
    async def delete_thread(
        thread_id: str,
        body: DeleteThreadRequest,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> DeleteResponse:
        await service.delete_thread(device, thread_id, body.impact_token)
        return DeleteResponse(deleted=True)

    @app.post(
        "/v1/threads/{thread_id}/turns",
        response_model=TurnDTO,
        response_model_by_alias=True,
        status_code=202,
    )
    async def start_turn(
        thread_id: str,
        body: StartTurnRequest,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> TurnDTO:
        _enforce_rate_limit(
            resolved_store.consume_rate_limit(
                "send.device",
                device.id,
                resolved_settings.send_device_limit,
                resolved_settings.send_window_seconds,
            )
        )
        parts = [part.model_dump(by_alias=True) for part in body.input]
        return await service.start_turn(
            device,
            thread_id,
            body.client_message_id,
            parts,
        )

    @app.put(
        "/v1/threads/{thread_id}/turn-inputs/{client_message_id}/images/{client_asset_id}",
        response_model=ImageUploadResponse,
        response_model_by_alias=True,
    )
    async def upload_turn_image(
        thread_id: str,
        client_message_id: str,
        client_asset_id: str,
        request: Request,
        response: Response,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ImageUploadResponse:
        if request.headers.get("content-type") != "application/octet-stream":
            raise GatewayError(
                "INVALID_CONTENT_TYPE",
                "Content-Type must be application/octet-stream",
                status_code=415,
            )
        if not 8 <= len(client_message_id) <= 128:
            raise GatewayError("INVALID_REQUEST", "Request validation failed", 422)
        try:
            parsed_asset_id = uuid.UUID(client_asset_id)
        except ValueError as exc:
            raise GatewayError("INVALID_REQUEST", "Request validation failed", 422) from exc
        if str(parsed_asset_id) != client_asset_id:
            raise GatewayError("INVALID_REQUEST", "Request validation failed", 422)
        _enforce_rate_limit(
            resolved_store.consume_rate_limit(
                "image-upload.device",
                device.id,
                resolved_settings.image_upload_device_limit,
                resolved_settings.image_upload_window_seconds,
            )
        )
        result = await service.upload_image(
            device,
            thread_id,
            client_message_id,
            client_asset_id,
            await request.body(),
        )
        response.status_code = 201 if result.created else 200
        response.headers["Cache-Control"] = "no-store"
        record = result.record
        return ImageUploadResponse(
            asset_ref=record.asset_ref,
            media_type=record.media_type,
            width=record.width,
            height=record.height,
            byte_count=record.byte_count,
            expires_at=record.expires_at,
        )

    @app.post(
        "/v1/threads/{thread_id}/turns/{turn_id}/interrupt",
        response_model=InterruptResponse,
        response_model_by_alias=True,
    )
    async def interrupt_turn(
        thread_id: str,
        turn_id: str,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> InterruptResponse:
        await service.interrupt_turn(device, thread_id, turn_id)
        return InterruptResponse(interrupted=True)

    @app.post(
        "/v1/approvals/{approval_id}/decision",
        response_model=ApprovalDecisionResponse,
        response_model_by_alias=True,
    )
    async def approval_decision(
        approval_id: str,
        body: ApprovalDecisionRequest,
        device: DeviceRecord = Depends(authenticated_device),
    ) -> ApprovalDecisionResponse:
        _enforce_rate_limit(
            resolved_store.consume_rate_limit(
                "approval.device",
                device.id,
                resolved_settings.approval_device_limit,
                resolved_settings.approval_window_seconds,
            )
        )
        approval = await service.resolve_approval(
            device,
            approval_id,
            body.action_token,
            body.decision,
        )
        return ApprovalDecisionResponse(approval=approval)

    @app.websocket("/v1/events")
    async def events_socket(websocket: WebSocket) -> None:
        # Accept once before application-level authentication so Starlette and
        # Uvicorn preserve our 44xx WebSocket close codes. Closing before
        # accept is an HTTP handshake rejection (typically 403), which hides
        # TOKEN_EXPIRED/refresh semantics from mobile clients.
        await websocket.accept()
        source = _client_source(websocket, trusted_proxy_networks)
        decision = resolved_store.consume_rate_limits(
            [
                (
                    "websocket.connect.source",
                    source,
                    resolved_settings.websocket_connect_source_limit,
                    resolved_settings.websocket_connect_window_seconds,
                ),
                (
                    "websocket.connect.global",
                    "all",
                    resolved_settings.websocket_connect_global_limit,
                    resolved_settings.websocket_connect_window_seconds,
                ),
            ]
        )
        if not decision.allowed:
            await _close_websocket_rate_limited(websocket, decision)
            return
        try:
            token = _bearer_token(websocket.headers.get("authorization"))
            device = service.authenticate(token)
            proof = verify_proof(
                public_key_spki=device.public_key,
                headers=websocket.headers,
                method="GET",
                raw_target=canonical_target(
                    websocket.scope.get("raw_path", b"/v1/events"),
                    websocket.scope.get("query_string", b""),
                ),
                body=b"",
                bearer_token=token,
                now_epoch=int(time.time()),
                allowed_skew_seconds=resolved_settings.proof_clock_skew_seconds,
            )
            if not resolved_store.claim_request_proof_nonce(
                "device:" + device.id,
                proof.nonce,
                proof.timestamp,
                int(time.time()),
                resolved_settings.proof_clock_skew_seconds * 2 + 1,
            ):
                raise ProofError("proof nonce was already used")
        except (GatewayError, ProofError, UnicodeEncodeError):
            await websocket.close(code=4401)
            return
        decision = resolved_store.consume_rate_limit(
            "websocket.connect.device",
            device.id,
            resolved_settings.websocket_connect_device_limit,
            resolved_settings.websocket_connect_window_seconds,
        )
        if not decision.allowed:
            await _close_websocket_rate_limited(websocket, decision)
            return
        try:
            after_seq = int(websocket.query_params.get("afterSeq", "0"))
            requested_stream_id = websocket.query_params.get("streamId")
            if after_seq < 0:
                raise ValueError
            if not requested_stream_id:
                raise ValueError
        except ValueError:
            await websocket.close(code=4400)
            return

        async def socket_auth_valid() -> bool:
            try:
                current = service.authenticate(token)
            except GatewayError:
                await websocket.close(code=4401)
                return False
            if current.id != device.id or current.stream_id != device.stream_id:
                await websocket.close(code=4401)
                return False
            return True

        async def send_small_event(event) -> bool:
            payload = json.dumps(
                event.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(payload.encode("utf-8")) > resolved_settings.websocket_event_max_bytes:
                await websocket.close(code=1011)
                return False
            await websocket.send_text(payload)
            return True

        async def send_replay_event(event) -> tuple[int, bool]:
            payload = json.dumps(
                event.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(payload.encode("utf-8")) <= resolved_settings.websocket_event_max_bytes:
                await websocket.send_text(payload)
                return event.seq, True
            # Never put an oversized frame on the wire. The replacement event is
            # appended after the oversized event so its sequence safely advances
            # the client beyond the skipped backlog.
            sync_event = resolved_store.append_event(
                device.id,
                "sync.required",
                {"reason": "eventTooLarge"},
                thread_id=SYNC_THREAD_ID,
            )
            return sync_event.seq, await send_small_event(sync_event)

        # Recheck immediately before replay so a revoke racing the handshake
        # cannot expose even one queued event.
        if not await socket_auth_valid():
            return

        replay = resolved_store.replay_events(
            device.id,
            requested_stream_id,
            after_seq,
        )
        if requested_stream_id != device.stream_id:
            sync_event = resolved_store.append_event(
                device.id,
                "sync.required",
                {"reason": "streamChanged"},
                thread_id=SYNC_THREAD_ID,
            )
            if not await send_small_event(sync_event):
                return
            await websocket.close(code=4409)
            return
        last_sent = after_seq
        if replay.sync_required:
            sync_event = resolved_store.append_event(
                device.id,
                "sync.required",
                {"reason": "eventGap"},
                thread_id=SYNC_THREAD_ID,
            )
            if not await send_small_event(sync_event):
                return
            last_sent = sync_event.seq
        else:
            for event in replay.events:
                last_sent, sent = await send_replay_event(event)
                if not sent:
                    return
                if last_sent != event.seq:
                    break

        try:
            while True:
                # This loop also serves as an idle-connection auth heartbeat.
                # It is intentionally per polling cycle, not per streamed delta.
                if not await socket_auth_valid():
                    return
                replay = resolved_store.replay_events(
                    device.id,
                    requested_stream_id,
                    last_sent,
                )
                if replay.sync_required:
                    sync_event = resolved_store.append_event(
                        device.id,
                        "sync.required",
                        {"reason": "eventGap"},
                        thread_id=SYNC_THREAD_ID,
                    )
                    if not await send_small_event(sync_event):
                        return
                    last_sent = sync_event.seq
                else:
                    for event in replay.events:
                        last_sent, sent = await send_replay_event(event)
                        if not sent:
                            return
                        if last_sent != event.seq:
                            break
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=0.25
                    )
                except asyncio.TimeoutError:
                    continue
                decision = resolved_store.consume_rate_limit(
                    "websocket.message.device",
                    device.id,
                    resolved_settings.websocket_message_device_limit,
                    resolved_settings.websocket_message_window_seconds,
                )
                if not decision.allowed:
                    await _close_websocket_rate_limited(websocket, decision)
                    return
                if (
                    len(raw.encode("utf-8"))
                    > resolved_settings.websocket_inbound_max_bytes
                ):
                    await websocket.close(code=4400)
                    return
                if not await socket_auth_valid():
                    return
                try:
                    ack = EventAck.model_validate(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    await websocket.close(code=4400)
                    return
                if ack.seq > last_sent or not resolved_store.acknowledge(
                    device.id, ack.stream_id, ack.seq
                ):
                    await websocket.close(code=4400)
                    return
        except WebSocketDisconnect:
            return

    return app


def _bearer_token(value: Optional[str]) -> str:
    if value is None:
        raise GatewayError("AUTH_REQUIRED", "Authentication is required", 401)
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        raise GatewayError("AUTH_REQUIRED", "Authentication is required", 401)
    return token


def _enforce_rate_limit(decision: RateLimitDecision) -> None:
    if decision.allowed:
        return
    raise GatewayError(
        "RATE_LIMITED",
        "Too many requests; retry later",
        status_code=429,
        retryable=True,
        retry_after_seconds=decision.retry_after_seconds,
    )


def _rate_limited_response(decision: RateLimitDecision) -> JSONResponse:
    body = ErrorDTO(
        code="RATE_LIMITED",
        message="Too many requests; retry later",
        retryable=True,
    ).model_dump(by_alias=True)
    return JSONResponse(
        status_code=429,
        content={"error": body},
        headers={"Retry-After": str(max(1, decision.retry_after_seconds))},
    )


async def _close_websocket_rate_limited(
    websocket: WebSocket,
    decision: RateLimitDecision,
) -> None:
    await websocket.close(
        code=4429,
        reason="retry-after=%d" % max(1, decision.retry_after_seconds),
    )


def _client_source(
    connection: Request | WebSocket,
    trusted_proxy_networks: Sequence[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ],
) -> str:
    """Resolve a rate-limit source without trusting public X-Forwarded-For."""

    peer = connection.client.host if connection.client is not None else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in network for network in trusted_proxy_networks):
        return str(peer_ip)

    forwarded = connection.headers.get("x-forwarded-for")
    if not forwarded:
        return str(peer_ip)
    try:
        chain = [
            ipaddress.ip_address(part.strip())
            for part in forwarded.split(",")
            if part.strip()
        ]
    except ValueError:
        return str(peer_ip)
    if not chain:
        return str(peer_ip)

    # Walk from the trusted boundary inwards. This resists a client-supplied
    # leftmost value when the edge appends instead of replacing the header.
    for candidate in reversed(chain):
        if not any(candidate in network for network in trusted_proxy_networks):
            return str(candidate)
    return str(chain[0])
