from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


MAX_CONTROL_FRAME_BYTES = 256 * 1024
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ASSET_BASE64_CHARS = ((MAX_ASSET_BYTES + 2) // 3) * 4
_RELAY_ID = re.compile(r"^(asst|node|dev|msg|req)_[A-Za-z0-9_-]{22}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _opaque(value: str, label: str, maximum: int = 128) -> str:
    if not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} is invalid")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _typed_relay_id(value: str, prefix: str) -> str:
    if not _RELAY_ID.fullmatch(value) or not value.startswith(prefix + "_"):
        raise ValueError(f"{prefix} identity is invalid")
    return value


class TextInput(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)


class ImageInput(StrictModel):
    type: Literal["image"]
    asset_ref: str = Field(alias="assetRef", min_length=32, max_length=128)


TurnInput = Annotated[Union[TextInput, ImageInput], Field(discriminator="type")]


class EmptyParams(StrictModel):
    pass


class ThreadsListParams(StrictModel):
    archived: bool = False


class ThreadsCreateParams(StrictModel):
    title: Optional[str] = Field(default=None, max_length=120)


class ThreadIdParams(StrictModel):
    thread_id: str = Field(alias="threadId", min_length=1, max_length=128)

    @field_validator("thread_id")
    @classmethod
    def valid_thread_id(cls, value: str) -> str:
        return _opaque(value, "threadId")


class ThreadsReadParams(ThreadIdParams):
    message_limit: Optional[int] = Field(default=None, alias="messageLimit", ge=1, le=100)
    message_cursor: Optional[str] = Field(
        default=None, alias="messageCursor", min_length=1, max_length=1024
    )


class ThreadsPatchParams(ThreadIdParams):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    archived: Optional[bool] = None

    @model_validator(mode="after")
    def require_change(self) -> "ThreadsPatchParams":
        if self.title is None and self.archived is None:
            raise ValueError("at least one patch field is required")
        return self


class ThreadsDeleteConfirmParams(ThreadIdParams):
    confirm_permanent_delete: Literal[True] = Field(alias="confirmPermanentDelete")
    impact_token: str = Field(alias="impactToken", min_length=32, max_length=512)


class TurnsStartParams(ThreadIdParams):
    client_message_id: str = Field(alias="clientMessageId", min_length=8, max_length=128)
    input: list[TurnInput] = Field(min_length=1, max_length=32)

    @field_validator("client_message_id")
    @classmethod
    def valid_client_message_id(cls, value: str) -> str:
        return _opaque(value, "clientMessageId")

    @model_validator(mode="after")
    def validate_images(self) -> "TurnsStartParams":
        refs = [part.asset_ref for part in self.input if isinstance(part, ImageInput)]
        if len(refs) > 4 or len(refs) != len(set(refs)):
            raise ValueError("image references are invalid")
        return self


class AssetsUploadParams(ThreadIdParams):
    client_message_id: str = Field(alias="clientMessageId", min_length=8, max_length=128)
    client_asset_id: str = Field(alias="clientAssetId", min_length=36, max_length=36)
    media_type: Literal["image/jpeg", "image/png"] = Field(alias="mediaType")
    body_base64: str = Field(alias="bodyBase64", min_length=4, max_length=MAX_ASSET_BASE64_CHARS)

    @field_validator("client_message_id")
    @classmethod
    def valid_client_message_id(cls, value: str) -> str:
        return _opaque(value, "clientMessageId")

    def decoded_body(self) -> bytes:
        try:
            value = base64.b64decode(self.body_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("bodyBase64 is invalid") from exc
        if not 1 <= len(value) <= MAX_ASSET_BYTES:
            raise ValueError("decoded asset size is invalid")
        if base64.b64encode(value).decode("ascii") != self.body_base64:
            raise ValueError("bodyBase64 is not canonical")
        return value


class TurnsInterruptParams(ThreadIdParams):
    turn_id: str = Field(alias="turnId", min_length=1, max_length=128)

    @field_validator("turn_id")
    @classmethod
    def valid_turn_id(cls, value: str) -> str:
        return _opaque(value, "turnId")


class ApprovalsDecideParams(StrictModel):
    approval_id: str = Field(alias="approvalId", min_length=1, max_length=128)
    decision: Literal["approve", "reject"]
    action_token: str = Field(alias="actionToken", min_length=16, max_length=512)

    @field_validator("approval_id")
    @classmethod
    def valid_approval_id(cls, value: str) -> str:
        return _opaque(value, "approvalId")


class EventsSubscribeParams(StrictModel):
    after_seq: int = Field(alias="afterSeq", ge=0)


class EventsAckParams(StrictModel):
    stream_id: str = Field(alias="streamId", min_length=1, max_length=128)
    seq: int = Field(ge=0)

    @field_validator("stream_id")
    @classmethod
    def valid_stream_id(cls, value: str) -> str:
        return _opaque(value, "streamId")


OPERATION_PARAMS: dict[str, type[StrictModel]] = {
    "server.info": EmptyParams,
    "capabilities.get": EmptyParams,
    "threads.list": ThreadsListParams,
    "threads.create": ThreadsCreateParams,
    "threads.read": ThreadsReadParams,
    "threads.resume": ThreadIdParams,
    "threads.patch": ThreadsPatchParams,
    "threads.delete.preview": ThreadIdParams,
    "threads.delete.confirm": ThreadsDeleteConfirmParams,
    "turns.start": TurnsStartParams,
    "assets.upload": AssetsUploadParams,
    "turns.interrupt": TurnsInterruptParams,
    "approvals.decide": ApprovalsDecideParams,
    "events.subscribe": EventsSubscribeParams,
    "events.ack": EventsAckParams,
}


class CommandPayload(StrictModel):
    kind: Literal["command"]
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    device_id: str = Field(alias="deviceId", min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    params: dict[str, Any]

    @field_validator("request_id", "device_id")
    @classmethod
    def valid_ids(cls, value: str, info) -> str:
        return _typed_relay_id(
            value, "req" if info.field_name == "request_id" else "dev"
        )

    @field_validator("operation")
    @classmethod
    def allowlisted_operation(cls, value: str) -> str:
        if value not in OPERATION_PARAMS:
            raise ValueError("operation is not allowlisted")
        return value

    def typed_params(self) -> StrictModel:
        return OPERATION_PARAMS[self.operation].model_validate(self.params)


class DeliveredMessage(StrictModel):
    v: Literal[1]
    type: Literal["delivery"]
    delivery_seq: int = Field(alias="deliverySeq", ge=1)
    message_id: str = Field(alias="messageId", min_length=1, max_length=128)
    payload: CommandPayload

    @field_validator("message_id")
    @classmethod
    def valid_message_id(cls, value: str) -> str:
        return _typed_relay_id(value, "msg")


class ReadyFrame(StrictModel):
    v: Literal[1]
    type: Literal["ready"]
    assistant_id: str = Field(alias="assistantId", min_length=1, max_length=128)
    principal_id: str = Field(alias="principalId", min_length=1, max_length=128)
    role: Literal["node"]
    ack_cursor: int = Field(alias="ackCursor", ge=0)
    next_delivery_seq: int = Field(alias="nextDeliverySeq", ge=1)
    node_status: Optional[str] = Field(
        default=None, alias="nodeStatus", min_length=1, max_length=32
    )

    @field_validator("assistant_id")
    @classmethod
    def valid_assistant_id(cls, value: str) -> str:
        return _typed_relay_id(value, "asst")

    @field_validator("principal_id")
    @classmethod
    def valid_principal_id(cls, value: str) -> str:
        return _typed_relay_id(value, "node")

    @model_validator(mode="after")
    def validate_node_ready(self) -> "ReadyFrame":
        if self.node_status is not None or self.next_delivery_seq < self.ack_cursor + 1:
            raise ValueError("Node ready state is invalid")
        return self


class AcceptedFrame(StrictModel):
    v: Literal[1]
    type: Literal["accepted"]
    message_id: str = Field(alias="messageId", min_length=1, max_length=128)
    delivery_seq: int = Field(alias="deliverySeq", ge=1)
    duplicate: bool
    queued: bool

    @field_validator("message_id")
    @classmethod
    def valid_message_id(cls, value: str) -> str:
        return _typed_relay_id(value, "msg")


class RelayAck(StrictModel):
    v: Literal[1] = 1
    type: Literal["ack"] = "ack"
    delivery_seq: int = Field(alias="deliverySeq", ge=0)


class AcknowledgedFrame(StrictModel):
    v: Literal[1]
    type: Literal["acknowledged"]
    delivery_seq: int = Field(alias="deliverySeq", ge=0)


class PingFrame(StrictModel):
    v: Literal[1] = 1
    type: Literal["ping"] = "ping"
    nonce: str = Field(min_length=22, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class PongFrame(StrictModel):
    v: Literal[1] = 1
    type: Literal["pong"] = "pong"
    nonce: str = Field(min_length=22, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class ErrorBody(StrictModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    message: str = Field(min_length=1, max_length=512)
    retryable: bool
    retry_after_seconds: Optional[int] = Field(
        default=None, alias="retryAfterSeconds", ge=1
    )


class ResponsePayload(StrictModel):
    kind: Literal["response"] = "response"
    request_id: str = Field(alias="requestId")
    device_id: str = Field(alias="deviceId")
    operation: str
    ok: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[ErrorBody] = None

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        return _typed_relay_id(value, "req")

    @field_validator("device_id")
    @classmethod
    def valid_device_id(cls, value: str) -> str:
        return _typed_relay_id(value, "dev")

    @field_validator("operation")
    @classmethod
    def valid_operation(cls, value: str) -> str:
        if value not in OPERATION_PARAMS:
            raise ValueError("operation is not allowlisted")
        return value

    @model_validator(mode="after")
    def exactly_one_result(self) -> "ResponsePayload":
        if self.ok != (self.result is not None and self.error is None):
            raise ValueError("response result/error shape is invalid")
        return self


class EventData(StrictModel):
    occurred_at: str = Field(alias="occurredAt", min_length=1, max_length=64)
    turn_id: Optional[str] = Field(default=None, alias="turnId")
    item_id: Optional[str] = Field(default=None, alias="itemId")
    payload: dict[str, Any]


class EventPayload(StrictModel):
    kind: Literal["event"] = "event"
    request_id: str = Field(alias="requestId")
    device_id: str = Field(alias="deviceId")
    stream_id: str = Field(alias="streamId", min_length=1, max_length=128)
    event_id: str = Field(alias="eventId", min_length=1, max_length=128)
    event_seq: int = Field(alias="eventSeq", ge=1)
    event_type: Literal[
        "thread.snapshot",
        "thread.updated",
        "thread.deleted",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.interrupted",
        "message.snapshot",
        "message.patch",
        "approval.requested",
        "approval.resolved",
        "approval.expired",
        "asset.unavailable",
        "sync.required",
        "error",
        "audit.action",
    ] = Field(alias="eventType")
    thread_id: Optional[str] = Field(
        default=None, alias="threadId", min_length=1, max_length=128
    )
    data: EventData

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        return _typed_relay_id(value, "req")

    @field_validator("device_id")
    @classmethod
    def valid_device_id(cls, value: str) -> str:
        return _typed_relay_id(value, "dev")

    @field_validator("stream_id", "event_id", "thread_id")
    @classmethod
    def valid_public_ids(cls, value: Optional[str], info) -> Optional[str]:
        return None if value is None else _opaque(value, info.field_name)


class OutboundMessage(StrictModel):
    v: Literal[1] = 1
    type: Literal["message"] = "message"
    message_id: str = Field(alias="messageId")
    payload: Union[ResponsePayload, EventPayload]

    @field_validator("message_id")
    @classmethod
    def valid_message_id(cls, value: str) -> str:
        return _typed_relay_id(value, "msg")


def wire_json(model: BaseModel) -> str:
    return model.model_dump_json(by_alias=True, exclude_none=True)


def parse_bounded_json(raw: str, *, max_depth: int = 32, max_nodes: int = 20_000) -> Any:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("frame is not JSON") from exc
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("frame structure exceeds its limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value
