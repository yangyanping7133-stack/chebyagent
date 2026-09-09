from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from ipaddress import ip_address, ip_network
from typing import Any, AsyncIterator, Literal, Optional, Type

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .config import RelaySettings
from .connections import ConnectionLimitError, ConnectionManager, RelayConnection
from .limits import SlidingWindowLimiter
from .models import (
    INBOUND_FRAME_ADAPTER,
    AssetUploadParams,
    ClientAck,
    ClientMessage,
    ClientPing,
    CommandBase,
    EventPayload,
    Operation,
    PairingExchangeRequest,
    PairingExchangeResponse,
    RefreshRequest,
    RefreshResponse,
    ResponsePayload,
)
from .pop import ProofError, proof_subject_for_spki, verify_proof
from .store import (
    AckError,
    AuthenticationError,
    BindingError,
    CorrelationError,
    EPHEMERAL_CONTINUITY_PURPOSE,
    IdempotencyConflict,
    PairingDenied,
    Principal,
    QueueLimitExceeded,
    RefreshDenied,
    RelayStore,
    Role,
    StoreError,
)


_BEARER = re.compile(r"^Bearer ([A-Za-z0-9_.-]{32,256})$")
_OFFLINE_QUEUE_OPERATIONS = {Operation.TURNS_START}
_OPERATION_TTL_SECONDS: dict[Operation, int] = {
    Operation.SERVER_INFO: 30,
    Operation.CAPABILITIES_GET: 30,
    Operation.THREADS_LIST: 30,
    Operation.THREADS_CREATE: 60,
    Operation.THREADS_READ: 30,
    Operation.THREADS_RESUME: 30,
    Operation.THREADS_PATCH: 30,
    Operation.THREADS_DELETE_PREVIEW: 15,
    Operation.THREADS_DELETE_CONFIRM: 15,
    Operation.TURNS_START: 300,
    Operation.ASSETS_UPLOAD: 120,
    Operation.TURNS_INTERRUPT: 15,
    Operation.APPROVALS_DECIDE: 15,
    Operation.EVENTS_SUBSCRIBE: 30,
    Operation.EVENTS_ACK: 30,
}


class PublicError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _error_frame(code: str, message: str, *, retryable: bool = False) -> dict:
    return {
        "v": 1,
        "type": "error",
        "code": code,
        "message": message,
        "retryable": retryable,
    }


def _bearer(value: Optional[str]) -> str:
    match = _BEARER.fullmatch(value or "")
    if match is None:
        raise AuthenticationError("authentication required")
    return match.group(1)


async def _bounded_body(request: Request, maximum: int) -> bytes:
    raw_headers = request.scope.get("headers", [])
    content_lengths = [
        value for name, value in raw_headers if name.lower() == b"content-length"
    ]
    if any(name.lower() == b"transfer-encoding" for name, _ in raw_headers):
        raise PublicError("INVALID_REQUEST", "Request framing is invalid", 400)
    if len(content_lengths) > 1:
        raise PublicError("INVALID_REQUEST", "Request framing is invalid", 400)
    declared: Optional[int] = None
    if content_lengths:
        try:
            raw = content_lengths[0].decode("ascii")
            if not raw.isdecimal() or str(int(raw)) != raw:
                raise ValueError
            declared = int(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicError("INVALID_REQUEST", "Request framing is invalid", 400) from exc
        if declared > maximum:
            raise PublicError("REQUEST_TOO_LARGE", "Request body is too large", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise PublicError("REQUEST_TOO_LARGE", "Request body is too large", 413)
        body.extend(chunk)
    if declared is not None and declared != len(body):
        raise PublicError("INVALID_REQUEST", "Request framing is invalid", 400)
    return bytes(body)


def _parse_body(body: bytes, model: Type[BaseModel]) -> BaseModel:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_invalid_constant,
        )
        return model.model_validate(value)
    except (UnicodeDecodeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise PublicError("INVALID_REQUEST", "Request validation failed", 422) from exc


def _raw_target(scope: dict) -> bytes:
    raw_path = scope.get("raw_path") or scope["path"].encode("ascii")
    query = scope.get("query_string", b"")
    return raw_path + (b"?" + query if query else b"")


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_frame(raw: str, settings: RelaySettings):
    encoded = raw.encode("utf-8")
    if len(encoded) > settings.max_frame_bytes:
        raise PublicError("FRAME_TOO_LARGE", "Frame is too large", 4409)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_invalid_constant,
        )
    except (ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise PublicError("INVALID_FRAME", "Frame validation failed", 4400) from exc
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > settings.max_json_nodes or depth > settings.max_json_depth:
            raise PublicError("INVALID_FRAME", "Frame validation failed", 4400)
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    try:
        frame = INBOUND_FRAME_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise PublicError("INVALID_FRAME", "Frame validation failed", 4400) from exc
    if isinstance(frame, ClientMessage):
        is_asset = isinstance(getattr(frame.payload, "params", None), AssetUploadParams)
        if not is_asset and len(encoded) > settings.max_command_bytes:
            raise PublicError("MESSAGE_TOO_LARGE", "Message is too large", 4409)
    return frame


def _source(scope: dict, settings: RelaySettings) -> str:
    client = scope.get("client")
    peer_text = "unknown" if client is None else str(client[0])
    try:
        peer = ip_address(peer_text)
    except ValueError:
        return peer_text
    trusted = any(
        peer in ip_network(value, strict=False)
        for value in settings.trusted_proxy_cidrs
    )
    if not trusted:
        return str(peer)
    header_name = settings.client_ip_header.encode("ascii")
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == header_name
    ]
    if len(values) != 1:
        raise ValueError("trusted proxy client identity header is missing or repeated")
    try:
        raw_value = values[0].decode("ascii")
        if raw_value != raw_value.strip() or "," in raw_value:
            raise ValueError
        return str(ip_address(raw_value))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("trusted proxy client identity header is invalid") from exc


def _has_exactly_one_header(scope: dict, name: str) -> bool:
    encoded = name.lower().encode("ascii")
    return sum(1 for key, _ in scope.get("headers", []) if key.lower() == encoded) == 1


def _proof_headers_are_singular(scope: dict, *, include_authorization: bool) -> bool:
    names = [
        "x-cheby-signature-version",
        "x-cheby-timestamp",
        "x-cheby-nonce",
        "x-cheby-signature",
    ]
    if include_authorization:
        names.append("authorization")
    return all(_has_exactly_one_header(scope, name) for name in names)


def create_app(
    settings: Optional[RelaySettings] = None,
    *,
    store: Optional[RelayStore] = None,
) -> FastAPI:
    resolved_settings = settings or RelaySettings.from_env()
    resolved_store = store or RelayStore(resolved_settings)
    owns_store = store is None
    manager = ConnectionManager(resolved_settings, resolved_store)
    public_limiter = SlidingWindowLimiter(
        max_keys=resolved_settings.rate_limiter_max_keys
    )
    connect_limiter = SlidingWindowLimiter(
        max_keys=resolved_settings.rate_limiter_max_keys
    )
    socket_limiter = SlidingWindowLimiter(
        max_keys=resolved_settings.rate_limiter_max_keys
    )

    async def gc_loop() -> None:
        while True:
            await asyncio.sleep(resolved_settings.gc_interval_seconds)
            async with manager.delivery_lock("gc", "device"):
                result = resolved_store.run_gc()
                for routed in result.expiration_notices:
                    delivery = routed.delivery
                    await manager.send_role(
                        routed.assistant_id,
                        "device",
                        {
                            "v": 1,
                            "type": "delivery",
                            "deliverySeq": delivery.delivery_seq,
                            "messageId": delivery.message_id,
                            "payload": delivery.payload,
                        },
                    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        sweep_task = asyncio.create_task(gc_loop())
        try:
            yield
        finally:
            sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await sweep_task
            await manager.shutdown()
            if owns_store:
                resolved_store.close()

    app = FastAPI(
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(resolved_settings.allowed_hosts),
    )
    app.state.relay_store = resolved_store
    app.state.connection_manager = manager
    app.state.public_rate_limiter = public_limiter

    @app.exception_handler(PublicError)
    async def public_error_handler(_: Request, exc: PublicError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "retryable": False},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "Request validation failed",
                "retryable": False,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/relay/v1/pairings/exchange")
    async def pairing_exchange(request: Request) -> JSONResponse:
        if request.scope.get("query_string"):
            raise PublicError("INVALID_REQUEST", "Query parameters are not allowed", 400)
        if request.headers.get("content-type") != "application/json":
            raise PublicError("INVALID_REQUEST", "Content-Type must be application/json", 415)
        body = await _bounded_body(request, resolved_settings.pairing_body_limit_bytes)
        parsed = _parse_body(body, PairingExchangeRequest)
        assert isinstance(parsed, PairingExchangeRequest)
        try:
            source = _source(request.scope, resolved_settings)
        except ValueError as exc:
            raise PublicError(
                "SOURCE_IDENTITY_INVALID", "Client source identity is invalid", 400
            ) from exc
        global_allowed = await public_limiter.allow(
            "pair:global",
            limit=resolved_settings.pairing_rate_limit * 100,
            window_seconds=resolved_settings.public_rate_window_seconds,
        )
        if not global_allowed:
            raise PublicError("RATE_LIMITED", "Too many requests", 429)
        allowed = await public_limiter.allow(
            f"pair:{source}:{parsed.assistant_id}",
            limit=resolved_settings.pairing_rate_limit,
            window_seconds=resolved_settings.public_rate_window_seconds,
        )
        if not allowed:
            raise PublicError("RATE_LIMITED", "Too many requests", 429)
        try:
            if not _proof_headers_are_singular(
                request.scope, include_authorization=False
            ):
                raise ProofError("missing or repeated proof header")
            subject = proof_subject_for_spki(parsed.device_public_key)
            proof = verify_proof(
                public_key_spki=parsed.device_public_key,
                headers=request.headers,
                method="POST",
                raw_target=_raw_target(request.scope),
                body=body,
                bearer_token="",
                now_epoch=int(time.time()),
                allowed_skew_seconds=resolved_settings.proof_clock_skew_seconds,
            )
            resolved_store.claim_proof_nonce(subject, proof.nonce)
            tokens = resolved_store.exchange_pairing(
                assistant_id=parsed.assistant_id,
                pairing_secret=parsed.pairing_secret,
                device_name=parsed.device_name,
                public_key_spki=parsed.device_public_key,
            )
        except (ProofError, PairingDenied, AuthenticationError) as exc:
            raise PublicError(
                "PAIRING_DENIED", "Pairing credentials are not valid", 401
            ) from exc
        response = PairingExchangeResponse(
            assistant_id=tokens.assistant_id,
            device_id=tokens.device_id,
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at,
        )
        return JSONResponse(
            status_code=200,
            content=response.model_dump(by_alias=True),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/relay/v1/auth/refresh")
    async def auth_refresh(request: Request) -> JSONResponse:
        if request.scope.get("query_string"):
            raise PublicError("INVALID_REQUEST", "Query parameters are not allowed", 400)
        if request.headers.get("content-type") != "application/json":
            raise PublicError("INVALID_REQUEST", "Content-Type must be application/json", 415)
        body = await _bounded_body(request, resolved_settings.refresh_body_limit_bytes)
        parsed = _parse_body(body, RefreshRequest)
        assert isinstance(parsed, RefreshRequest)
        try:
            source = _source(request.scope, resolved_settings)
        except ValueError as exc:
            raise PublicError(
                "SOURCE_IDENTITY_INVALID", "Client source identity is invalid", 400
            ) from exc
        global_allowed = await public_limiter.allow(
            "refresh:global",
            limit=resolved_settings.refresh_rate_limit * 100,
            window_seconds=resolved_settings.public_rate_window_seconds,
        )
        if not global_allowed:
            raise PublicError("RATE_LIMITED", "Too many requests", 429)
        allowed = await public_limiter.allow(
            f"refresh:{source}:{parsed.device_id}",
            limit=resolved_settings.refresh_rate_limit,
            window_seconds=resolved_settings.public_rate_window_seconds,
        )
        if not allowed:
            raise PublicError("RATE_LIMITED", "Too many requests", 429)
        public_key = resolved_store.device_public_key(parsed.device_id)
        if public_key is None:
            raise PublicError("REFRESH_DENIED", "Refresh credentials are not valid", 401)
        try:
            if not _proof_headers_are_singular(
                request.scope, include_authorization=False
            ):
                raise ProofError("missing or repeated proof header")
            proof = verify_proof(
                public_key_spki=public_key,
                headers=request.headers,
                method="POST",
                raw_target=_raw_target(request.scope),
                body=body,
                bearer_token="",
                now_epoch=int(time.time()),
                allowed_skew_seconds=resolved_settings.proof_clock_skew_seconds,
            )
            resolved_store.claim_proof_nonce("device:" + parsed.device_id, proof.nonce)
            tokens = resolved_store.refresh_device(
                device_id=parsed.device_id, refresh_token=parsed.refresh_token
            )
        except (ProofError, RefreshDenied, AuthenticationError) as exc:
            raise PublicError(
                "REFRESH_DENIED", "Refresh credentials are not valid", 401
            ) from exc
        response = RefreshResponse(
            device_id=tokens.device_id,
            access_token=tokens.access_token,
            access_expires_at=tokens.access_expires_at,
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at,
        )
        return JSONResponse(
            status_code=200,
            content=response.model_dump(by_alias=True),
            headers={"Cache-Control": "no-store"},
        )

    async def authenticate_socket(websocket: WebSocket, expected_role: Role) -> tuple[Principal, str]:
        token = _bearer(websocket.headers.get("authorization"))
        try:
            principal = resolved_store.authenticate(token, expected_role)
        except AuthenticationError as exc:
            other: Role = "node" if expected_role == "device" else "device"
            try:
                resolved_store.authenticate(token, other)
            except StoreError:
                raise exc
            raise BindingError("wrong credential role") from exc
        return principal, token

    async def socket_endpoint(websocket: WebSocket, role: Role) -> None:
        await websocket.accept()
        if websocket.headers.get("origin") is not None:
            await websocket.close(code=4403, reason="ORIGIN_NOT_ALLOWED")
            return
        if websocket.scope.get("query_string"):
            await websocket.close(code=4400, reason="QUERY_NOT_ALLOWED")
            return
        try:
            if not _has_exactly_one_header(websocket.scope, "authorization"):
                raise AuthenticationError("authentication required")
            principal, token = await authenticate_socket(websocket, role)
            if (
                role == "node"
                and principal.assistant_purpose
                == EPHEMERAL_CONTINUITY_PURPOSE
            ):
                raise BindingError("continuity Node is disabled")
            if not await connect_limiter.allow(
                f"connect:{role}:{principal.credential_id}",
                limit=resolved_settings.connection_rate_limit,
                window_seconds=resolved_settings.connection_rate_window_seconds,
            ):
                await websocket.close(code=4408, reason="RATE_LIMITED")
                return
            if role == "device":
                if not _proof_headers_are_singular(
                    websocket.scope, include_authorization=True
                ):
                    raise ProofError("missing or repeated proof header")
                assert principal.public_key_spki is not None
                proof = verify_proof(
                    public_key_spki=principal.public_key_spki,
                    headers=websocket.headers,
                    method="GET",
                    raw_target=_raw_target(websocket.scope),
                    body=b"",
                    bearer_token=token,
                    now_epoch=int(time.time()),
                    allowed_skew_seconds=resolved_settings.proof_clock_skew_seconds,
                )
                resolved_store.claim_proof_nonce(
                    "device:" + principal.principal_id, proof.nonce
                )
        except BindingError:
            await websocket.close(code=4403, reason="BINDING_DENIED")
            return
        except (AuthenticationError, ProofError):
            await websocket.close(code=4401, reason="AUTH_REQUIRED")
            return

        generation = (
            resolved_store.activate_node_generation(principal.principal_id)
            if role == "node"
            else None
        )
        connection = RelayConnection(
            websocket,
            principal,
            resolved_settings.outbound_queue_size,
            node_generation=generation,
        )
        try:
            async with manager.delivery_lock(principal.assistant_id, role):
                try:
                    await manager.register(connection)
                except ConnectionLimitError:
                    await websocket.close(code=4408, reason="CONNECTION_LIMIT")
                    return
                connection.start()
                ack_cursor, next_seq = resolved_store.stream_state(
                    principal.assistant_id, role
                )
                ready = {
                    "v": 1,
                    "type": "ready",
                    "assistantId": principal.assistant_id,
                    "principalId": principal.principal_id,
                    "role": role,
                    "ackCursor": ack_cursor,
                    "nextDeliverySeq": next_seq,
                }
                if role == "device":
                    ready["nodeStatus"] = (
                        "online"
                        if await manager.is_online(principal.assistant_id, "node")
                        else "offline"
                    )
                if not await manager.send_connection(connection, ready):
                    return
                if (
                    principal.assistant_purpose
                    != EPHEMERAL_CONTINUITY_PURPOSE
                ):
                    for delivery in resolved_store.pending_deliveries(
                        principal.assistant_id, role
                    ):
                        delivered = await manager.send_connection(
                            connection,
                            {
                                "v": 1,
                                "type": "delivery",
                                "deliverySeq": delivery.delivery_seq,
                                "messageId": delivery.message_id,
                                "payload": delivery.payload,
                            },
                        )
                        if not delivered:
                            return
            if role == "node":
                await manager.send_role(
                    principal.assistant_id,
                    "device",
                    {"v": 1, "type": "node.status", "status": "online"},
                )
            await socket_receive_loop(connection, socket_limiter)
        finally:
            became_offline = await manager.unregister(connection)
            await connection.close(1000, "CONNECTION_CLOSED")
            if became_offline:
                await manager.send_role(
                    principal.assistant_id,
                    "device",
                    {"v": 1, "type": "node.status", "status": "offline"},
                )

    async def socket_receive_loop(
        connection: RelayConnection, limiter: SlidingWindowLimiter
    ) -> None:
        principal = connection.principal
        while True:
            try:
                received = await connection.websocket.receive()
            except WebSocketDisconnect:
                await connection.close(
                    1000,
                    "PEER_DISCONNECTED",
                    websocket_already_closed=True,
                )
                return
            if received.get("type") == "websocket.disconnect":
                await connection.close(
                    1000,
                    "PEER_DISCONNECTED",
                    websocket_already_closed=True,
                )
                return
            if not manager.connection_valid(connection):
                code = 4401 if principal.role == "device" else 4403
                await connection.close(code, "CREDENTIAL_EXPIRED_OR_REVOKED")
                return
            raw = received.get("text")
            if raw is None:
                await connection.close(4400, "TEXT_FRAMES_ONLY")
                return
            try:
                frame = _parse_frame(raw, resolved_settings)
            except PublicError as exc:
                await connection.close(exc.status_code, exc.code)
                return
            is_message = isinstance(frame, ClientMessage)
            allowed = await limiter.allow(
                ("message:" if is_message else "control:")
                + principal.credential_id,
                limit=(
                    resolved_settings.message_rate_limit
                    if is_message
                    else resolved_settings.control_frame_rate_limit
                ),
                window_seconds=(
                    resolved_settings.message_rate_window_seconds
                    if is_message
                    else resolved_settings.control_frame_rate_window_seconds
                ),
            )
            if not allowed:
                await connection.close(4408, "RATE_LIMITED")
                return
            if isinstance(frame, ClientPing):
                await manager.send_connection(
                    connection,
                    {"v": 1, "type": "pong", "nonce": frame.nonce},
                )
                continue
            if (
                principal.assistant_purpose
                == EPHEMERAL_CONTINUITY_PURPOSE
            ):
                await connection.close(4403, "BINDING_DENIED")
                return
            if isinstance(frame, ClientAck):
                async with manager.delivery_lock(
                    principal.assistant_id, principal.role
                ):
                    try:
                        cursor = resolved_store.acknowledge(
                            principal=principal,
                            seq=frame.delivery_seq,
                            node_generation=connection.node_generation,
                        )
                    except AckError:
                        await connection.close(4400, "INVALID_ACK")
                        return
                    except AuthenticationError:
                        code = 4401 if principal.role == "device" else 4403
                        await connection.close(code, "CREDENTIAL_EXPIRED_OR_REVOKED")
                        return
                    except BindingError:
                        await connection.close(4403, "BINDING_DENIED")
                        return
                await manager.send_connection(
                    connection,
                    {"v": 1, "type": "acknowledged", "deliverySeq": cursor},
                )
                continue
            assert isinstance(frame, ClientMessage)
            payload = frame.payload
            if principal.role == "device":
                if not isinstance(payload, CommandBase) or payload.device_id != principal.principal_id:
                    await connection.close(4403, "BINDING_DENIED")
                    return
                recipient_role: Role = "node"
                target_online = False
                expires_at = int(time.time()) + _OPERATION_TTL_SECONDS[payload.operation]
            else:
                if not isinstance(payload, (ResponsePayload, EventPayload)):
                    await connection.close(4403, "ROLE_DENIED")
                    return
                recipient_role = "device"
                target_online = await manager.is_online(principal.assistant_id, "device")
                expires_at = None
            payload_dict = payload.model_dump(by_alias=True, mode="json", exclude_none=True)
            async with manager.delivery_lock(principal.assistant_id, recipient_role):
                target_online = await manager.is_online(
                    principal.assistant_id, recipient_role
                )
                if (
                    principal.role == "device"
                    and not target_online
                    and payload.operation not in _OFFLINE_QUEUE_OPERATIONS
                ):
                    await manager.send_role(
                        principal.assistant_id,
                        "device",
                        {"v": 1, "type": "node.status", "status": "offline"},
                    )
                    await manager.send_connection(
                        connection,
                        _error_frame(
                            "NODE_OFFLINE",
                            "The dedicated Node is offline",
                            retryable=True,
                        ),
                    )
                    continue
                try:
                    result = resolved_store.enqueue(
                        principal=principal,
                        recipient_role=recipient_role,
                        message_id=frame.message_id,
                        payload=payload_dict,
                        expires_at=expires_at,
                        node_generation=connection.node_generation,
                    )
                except QueueLimitExceeded:
                    await connection.close(4410, "QUEUE_FULL")
                    return
                except (CorrelationError, BindingError):
                    await connection.close(4403, "BINDING_DENIED")
                    return
                except AuthenticationError:
                    code = 4401 if principal.role == "device" else 4403
                    await connection.close(code, "CREDENTIAL_EXPIRED_OR_REVOKED")
                    return
                except IdempotencyConflict:
                    await manager.send_connection(
                        connection,
                        _error_frame(
                            "IDEMPOTENCY_CONFLICT",
                            "The idempotency identifier was reused",
                        ),
                    )
                    continue
                if result.state == "expired":
                    await manager.send_connection(
                        connection,
                        _error_frame(
                            "DELIVERY_EXPIRED",
                            "The prior command expired before Node delivery",
                            retryable=True,
                        ),
                    )
                    continue
                accepted = {
                    "v": 1,
                    "type": "accepted",
                    "messageId": frame.message_id,
                    "deliverySeq": result.delivery_seq,
                    "duplicate": result.duplicate,
                    "queued": not target_online,
                }
                if not await manager.send_connection(connection, accepted):
                    return
                if target_online and (not result.duplicate or result.still_pending):
                    await manager.send_role(
                        principal.assistant_id,
                        recipient_role,
                        {
                            "v": 1,
                            "type": "delivery",
                            "deliverySeq": result.delivery_seq,
                            "messageId": frame.message_id,
                            "payload": payload_dict,
                        },
                    )

    @app.websocket("/relay/v1/device")
    async def device_socket(websocket: WebSocket) -> None:
        await socket_endpoint(websocket, "device")

    @app.websocket("/relay/v1/node")
    async def node_socket(websocket: WebSocket) -> None:
        await socket_endpoint(websocket, "node")

    return app
