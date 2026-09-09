from __future__ import annotations

import base64
import re
import uuid
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class WireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )


ASSISTANT_PATTERN = r"^asst_[A-Za-z0-9_-]{22}$"
NODE_PATTERN = r"^node_[A-Za-z0-9_-]{22}$"
DEVICE_PATTERN = r"^dev_[A-Za-z0-9_-]{22}$"
MESSAGE_PATTERN = r"^msg_[A-Za-z0-9_-]{22}$"
REQUEST_PATTERN = r"^req_[A-Za-z0-9_-]{22}$"

AssistantId = Annotated[str, Field(pattern=ASSISTANT_PATTERN)]
NodeId = Annotated[str, Field(pattern=NODE_PATTERN)]
DeviceId = Annotated[str, Field(pattern=DEVICE_PATTERN)]
MessageId = Annotated[str, Field(pattern=MESSAGE_PATTERN)]
RequestId = Annotated[str, Field(pattern=REQUEST_PATTERN)]


def _opaque_text(value: str) -> str:
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise ValueError("opaque identifiers cannot contain whitespace or controls")
    if len(value.encode("utf-8")) > 1024:
        raise ValueError("opaque identifier exceeds UTF-8 byte limit")
    return value


def _short_opaque_text(value: str) -> str:
    value = _opaque_text(value)
    if len(value.encode("utf-8")) > 128:
        raise ValueError("opaque identifier exceeds UTF-8 byte limit")
    return value


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_opaque_text),
]
ShortOpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(_short_opaque_text),
]


class Operation(str, Enum):
    SERVER_INFO = "server.info"
    CAPABILITIES_GET = "capabilities.get"
    THREADS_LIST = "threads.list"
    THREADS_CREATE = "threads.create"
    THREADS_READ = "threads.read"
    THREADS_RESUME = "threads.resume"
    THREADS_PATCH = "threads.patch"
    THREADS_DELETE_PREVIEW = "threads.delete.preview"
    THREADS_DELETE_CONFIRM = "threads.delete.confirm"
    TURNS_START = "turns.start"
    ASSETS_UPLOAD = "assets.upload"
    TURNS_INTERRUPT = "turns.interrupt"
    APPROVALS_DECIDE = "approvals.decide"
    EVENTS_SUBSCRIBE = "events.subscribe"
    EVENTS_ACK = "events.ack"


class EmptyParams(WireModel):
    pass


class ThreadsListParams(WireModel):
    archived: bool = False


class ThreadsCreateParams(WireModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)


class ThreadParams(WireModel):
    thread_id: OpaqueId


class ThreadsReadParams(ThreadParams):
    message_limit: Optional[int] = Field(default=None, ge=1, le=100)
    message_cursor: Optional[str] = Field(default=None, min_length=1, max_length=1024)

    @field_validator("message_cursor")
    @classmethod
    def safe_cursor(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _opaque_text(value)


class ThreadsPatchParams(ThreadParams):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    archived: Optional[bool] = None

    @model_validator(mode="after")
    def require_change(self) -> "ThreadsPatchParams":
        if self.title is None and self.archived is None:
            raise ValueError("at least one change is required")
        return self


class ThreadsDeleteConfirmParams(ThreadParams):
    confirm_permanent_delete: Literal[True]
    impact_token: str = Field(min_length=32, max_length=512, repr=False)

    @field_validator("impact_token")
    @classmethod
    def safe_impact_token(cls, value: str) -> str:
        return _opaque_text(value)


class TextInputPart(WireModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)


class ImageInputPart(WireModel):
    type: Literal["image"]
    asset_ref: str = Field(min_length=32, max_length=128)


InputPart = Annotated[Union[TextInputPart, ImageInputPart], Field(discriminator="type")]


class TurnsStartParams(ThreadParams):
    client_message_id: str = Field(min_length=8, max_length=128)
    input: List[InputPart] = Field(min_length=1, max_length=32)

    @field_validator("client_message_id")
    @classmethod
    def safe_client_message_id(cls, value: str) -> str:
        return _short_opaque_text(value)

    @model_validator(mode="after")
    def bound_images(self) -> "TurnsStartParams":
        images = [part.asset_ref for part in self.input if isinstance(part, ImageInputPart)]
        if len(images) > 4 or len(images) != len(set(images)):
            raise ValueError("image inputs must be unique and no more than four")
        return self


class AssetUploadParams(ThreadParams):
    client_message_id: str = Field(min_length=8, max_length=128)
    client_asset_id: str = Field(min_length=36, max_length=36)
    media_type: Literal["image/jpeg", "image/png"]
    body_base64: str = Field(min_length=4, max_length=11_184_812, repr=False)

    @field_validator("client_message_id")
    @classmethod
    def safe_client_message_id(cls, value: str) -> str:
        return _short_opaque_text(value)

    @field_validator("client_asset_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("clientAssetId must be a canonical UUID") from exc
        if str(parsed) != value:
            raise ValueError("clientAssetId must be a canonical UUID")
        return value

    @field_validator("body_base64")
    @classmethod
    def canonical_bounded_base64(cls, value: str) -> str:
        try:
            encoded = value.encode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("bodyBase64 must be canonical base64") from exc
        if len(decoded) > 8 * 1024 * 1024 or base64.b64encode(decoded) != encoded:
            raise ValueError("bodyBase64 exceeds limit or is not canonical")
        return value


class TurnsInterruptParams(ThreadParams):
    turn_id: OpaqueId


class ApprovalsDecideParams(WireModel):
    approval_id: OpaqueId
    decision: Literal["approve", "reject"]
    action_token: str = Field(min_length=16, max_length=512, repr=False)

    @field_validator("action_token")
    @classmethod
    def safe_action_token(cls, value: str) -> str:
        return _opaque_text(value)


class EventsSubscribeParams(WireModel):
    after_seq: int = Field(ge=0)


class EventsAckParams(WireModel):
    stream_id: ShortOpaqueId
    seq: int = Field(ge=0)


class CommandBase(WireModel):
    kind: Literal["command"]
    request_id: RequestId
    device_id: DeviceId


class ServerInfoCommand(CommandBase):
    operation: Literal[Operation.SERVER_INFO]
    params: EmptyParams


class CapabilitiesGetCommand(CommandBase):
    operation: Literal[Operation.CAPABILITIES_GET]
    params: EmptyParams


class ThreadsListCommand(CommandBase):
    operation: Literal[Operation.THREADS_LIST]
    params: ThreadsListParams


class ThreadsCreateCommand(CommandBase):
    operation: Literal[Operation.THREADS_CREATE]
    params: ThreadsCreateParams


class ThreadsReadCommand(CommandBase):
    operation: Literal[Operation.THREADS_READ]
    params: ThreadsReadParams


class ThreadsResumeCommand(CommandBase):
    operation: Literal[Operation.THREADS_RESUME]
    params: ThreadParams


class ThreadsPatchCommand(CommandBase):
    operation: Literal[Operation.THREADS_PATCH]
    params: ThreadsPatchParams


class ThreadsDeletePreviewCommand(CommandBase):
    operation: Literal[Operation.THREADS_DELETE_PREVIEW]
    params: ThreadParams


class ThreadsDeleteConfirmCommand(CommandBase):
    operation: Literal[Operation.THREADS_DELETE_CONFIRM]
    params: ThreadsDeleteConfirmParams


class TurnsStartCommand(CommandBase):
    operation: Literal[Operation.TURNS_START]
    params: TurnsStartParams


class AssetsUploadCommand(CommandBase):
    operation: Literal[Operation.ASSETS_UPLOAD]
    params: AssetUploadParams


class TurnsInterruptCommand(CommandBase):
    operation: Literal[Operation.TURNS_INTERRUPT]
    params: TurnsInterruptParams


class ApprovalsDecideCommand(CommandBase):
    operation: Literal[Operation.APPROVALS_DECIDE]
    params: ApprovalsDecideParams


class EventsSubscribeCommand(CommandBase):
    operation: Literal[Operation.EVENTS_SUBSCRIBE]
    params: EventsSubscribeParams


class EventsAckCommand(CommandBase):
    operation: Literal[Operation.EVENTS_ACK]
    params: EventsAckParams


CommandPayload = Annotated[
    Union[
        ServerInfoCommand,
        CapabilitiesGetCommand,
        ThreadsListCommand,
        ThreadsCreateCommand,
        ThreadsReadCommand,
        ThreadsResumeCommand,
        ThreadsPatchCommand,
        ThreadsDeletePreviewCommand,
        ThreadsDeleteConfirmCommand,
        TurnsStartCommand,
        AssetsUploadCommand,
        TurnsInterruptCommand,
        ApprovalsDecideCommand,
        EventsSubscribeCommand,
        EventsAckCommand,
    ],
    Field(discriminator="operation"),
]


class OperationError(WireModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    message: str = Field(min_length=1, max_length=256)
    retryable: bool = False


_FORBIDDEN_KEYS = {
    "authorization",
    "bearer",
    "accesstoken",
    "refreshtoken",
    "pairingsecret",
    "devicepublickey",
    "xchebysignatureversion",
    "xchebytimestamp",
    "xchebynonce",
    "xchebysignature",
}


def _reject_transport_credentials(value: JsonValue) -> JsonValue:
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = re.sub(r"[-_.]", "", key).lower()
                if normalized in _FORBIDDEN_KEYS:
                    raise ValueError("Gateway credentials and PoP are forbidden on Relay")
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return value


class ResponsePayload(WireModel):
    kind: Literal["response"]
    request_id: RequestId
    device_id: DeviceId
    operation: Annotated[Operation, Field(strict=False)]
    ok: bool
    result: Optional[JsonValue] = None
    error: Optional[OperationError] = None

    @field_validator("result")
    @classmethod
    def no_transport_credentials(cls, value: Optional[JsonValue]) -> Optional[JsonValue]:
        return None if value is None else _reject_transport_credentials(value)

    @model_validator(mode="after")
    def exactly_one_result(self) -> "ResponsePayload":
        if self.ok != (self.error is None):
            raise ValueError("successful responses cannot contain error")
        if self.ok and self.result is None:
            raise ValueError("successful responses require result")
        if not self.ok and self.error is None:
            raise ValueError("failed responses require error")
        if not self.ok and self.result is not None:
            raise ValueError("failed responses cannot contain result")
        return self


EventType = Literal[
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
]


class EventData(WireModel):
    occurred_at: str = Field(min_length=20, max_length=40)
    turn_id: Optional[OpaqueId] = None
    item_id: Optional[OpaqueId] = None
    payload: JsonValue

    @field_validator("payload")
    @classmethod
    def no_transport_credentials(cls, value: JsonValue) -> JsonValue:
        return _reject_transport_credentials(value)


class EventPayload(WireModel):
    kind: Literal["event"]
    request_id: RequestId
    device_id: DeviceId
    stream_id: ShortOpaqueId
    event_id: Annotated[
        str, Field(min_length=8, max_length=128), AfterValidator(_short_opaque_text)
    ]
    event_seq: int = Field(ge=0)
    event_type: EventType
    thread_id: Optional[OpaqueId] = None
    data: EventData


RelayPayload = Annotated[
    Union[CommandPayload, ResponsePayload, EventPayload],
    Field(discriminator="kind"),
]


class ClientMessage(WireModel):
    v: Literal[1]
    type: Literal["message"]
    message_id: MessageId
    payload: RelayPayload


class ClientAck(WireModel):
    v: Literal[1]
    type: Literal["ack"]
    delivery_seq: int = Field(ge=0)


class ClientPing(WireModel):
    v: Literal[1]
    type: Literal["ping"]
    nonce: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


InboundFrame = Annotated[
    Union[ClientMessage, ClientAck, ClientPing], Field(discriminator="type")
]
INBOUND_FRAME_ADAPTER = TypeAdapter(InboundFrame)


class PairingExchangeRequest(WireModel):
    assistant_id: AssistantId
    pairing_secret: str = Field(min_length=32, max_length=128, repr=False)
    device_name: str = Field(min_length=1, max_length=80)
    device_public_key: str = Field(min_length=16, max_length=8192, repr=False)

    @field_validator("device_name")
    @classmethod
    def safe_device_name(cls, value: str) -> str:
        if any(ord(character) < 0x20 for character in value):
            raise ValueError("deviceName contains control characters")
        if len(value.encode("utf-8")) > 80:
            raise ValueError("deviceName exceeds UTF-8 byte limit")
        return value


class PairingExchangeResponse(WireModel):
    assistant_id: AssistantId
    device_id: DeviceId
    access_token: str = Field(min_length=32, max_length=256, repr=False)
    access_expires_at: int
    refresh_token: str = Field(min_length=32, max_length=256, repr=False)
    refresh_expires_at: int


class RefreshRequest(WireModel):
    device_id: DeviceId
    refresh_token: str = Field(min_length=32, max_length=256, repr=False)


class RefreshResponse(WireModel):
    device_id: DeviceId
    access_token: str = Field(min_length=32, max_length=256, repr=False)
    access_expires_at: int
    refresh_token: str = Field(min_length=32, max_length=256, repr=False)
    refresh_expires_at: int


def canonical_json_bytes(model: BaseModel) -> bytes:
    return model.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
