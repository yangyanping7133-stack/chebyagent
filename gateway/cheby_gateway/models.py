from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class APIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ThreadStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_USER = "waitingUser"
    WAITING_APPROVAL = "waitingApproval"
    FAILED = "failed"
    ARCHIVED = "archived"


class ThreadDTO(APIModel):
    id: str
    title: str
    preview: str = ""
    created_at: str
    updated_at: str
    status: ThreadStatus
    last_turn_id: Optional[str] = None
    unread: bool = False


class ThreadListResponse(APIModel):
    data: List[ThreadDTO]


class DeleteImpactThreadDTO(APIModel):
    id: str
    title: str
    status: ThreadStatus
    archived: bool


class DeleteImpactPreviewResponse(APIModel):
    root_thread_id: str
    affected_threads: List[DeleteImpactThreadDTO]
    affected_count: int = Field(ge=1)
    expires_at: str
    impact_token: str = Field(repr=False)


class DeleteThreadRequest(APIModel):
    confirm_permanent_delete: Literal[True]
    impact_token: str = Field(min_length=32, max_length=512, repr=False)


class CreateThreadRequest(APIModel):
    title: Optional[str] = Field(default=None, max_length=120)


class PatchThreadRequest(APIModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    archived: Optional[bool] = None

    @model_validator(mode="after")
    def require_change(self) -> "PatchThreadRequest":
        if self.title is None and self.archived is None:
            raise ValueError("at least one change is required")
        return self


class TextInputPart(APIModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000)


class ImageInputPart(APIModel):
    type: Literal["image"]
    asset_ref: str = Field(min_length=32, max_length=128, repr=False)


InputPart = Annotated[
    Union[TextInputPart, ImageInputPart],
    Field(discriminator="type"),
]


class StartTurnRequest(APIModel):
    client_message_id: str = Field(min_length=8, max_length=128)
    input: List[InputPart] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_images(self) -> "StartTurnRequest":
        refs = [part.asset_ref for part in self.input if isinstance(part, ImageInputPart)]
        if len(refs) > 10:
            raise ValueError("at most ten images are allowed")
        if len(refs) != len(set(refs)):
            raise ValueError("image references must be unique")
        return self


class TurnDTO(APIModel):
    id: str
    thread_id: str
    status: str
    client_message_id: str
    created_at: str


class PairingExchangeRequest(APIModel):
    pairing_secret: str = Field(min_length=8, max_length=512, repr=False)
    device_name: str = Field(min_length=1, max_length=80)
    device_public_key: str = Field(min_length=16, max_length=8192, repr=False)


class PairingExchangeResponse(APIModel):
    device_id: str
    access_token: str = Field(repr=False)
    expires_at: str
    refresh_token: str = Field(repr=False)
    refresh_expires_at: str
    stream_id: str


class AuthRefreshRequest(APIModel):
    device_id: str = Field(min_length=8, max_length=128)
    refresh_token: str = Field(min_length=16, max_length=512, repr=False)


class AuthRefreshResponse(APIModel):
    device_id: str
    access_token: str = Field(repr=False)
    expires_at: str
    refresh_token: str = Field(repr=False)
    refresh_expires_at: str
    stream_id: str


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalDecisionRequest(APIModel):
    decision: ApprovalDecision
    action_token: str = Field(min_length=16, max_length=512, repr=False)


class ApprovalDTO(APIModel):
    approval_id: str
    thread_id: str
    turn_id: str
    item_id: str
    kind: str
    summary: str
    reason: str
    decisions: List[str]
    state: str
    expires_at: str
    action_token: Optional[str] = Field(default=None, repr=False)


class EventEnvelope(APIModel):
    v: Literal[1] = 1
    stream_id: str
    event_id: str
    seq: int
    occurred_at: str
    type: Literal[
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
    thread_id: str
    turn_id: Optional[str] = None
    item_id: Optional[str] = None
    payload: Dict[str, Any]


class EventAck(APIModel):
    type: Literal["ack"]
    stream_id: str
    seq: int = Field(ge=0)


class ErrorDTO(APIModel):
    code: str
    message: str
    retryable: bool = False


class ThreadDetailResponse(APIModel):
    thread: ThreadDTO
    messages: List[Dict[str, Any]]
    approvals: List[ApprovalDTO]
    stream_id: str
    cursor: int = Field(ge=0)
    next_message_cursor: Optional[str] = None
    has_more_messages: bool = False


class ServerInfoResponse(APIModel):
    protocol_version: Literal[1] = 1
    bridge_mode: str
    codex_connected: bool


class LocalImageCapability(APIModel):
    upload_version: Literal[1] = 1
    media_types: List[Literal["image/jpeg", "image/png"]]
    max_upload_bytes: Literal[8_388_608] = 8_388_608
    max_pixels: Literal[25_000_000] = 25_000_000
    max_edge_pixels: Literal[12_000] = 12_000
    max_images_per_turn: Literal[10] = 10


class CapabilitiesResponse(APIModel):
    v: Literal[1] = 1
    inputs: Dict[str, LocalImageCapability]


class ImageUploadResponse(APIModel):
    asset_ref: str = Field(min_length=32, max_length=128, repr=False)
    media_type: Literal["image/jpeg", "image/png"]
    width: int = Field(ge=1, le=12_000)
    height: int = Field(ge=1, le=12_000)
    byte_count: int = Field(ge=1, le=8_388_608)
    expires_at: str


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"


class ReplayResult(APIModel):
    sync_required: bool
    stream_id: str
    current_seq: int
    events: List[EventEnvelope]


class DeleteResponse(APIModel):
    deleted: bool


class InterruptResponse(APIModel):
    interrupted: bool


class ApprovalDecisionResponse(APIModel):
    approval: ApprovalDTO


class RichMessageSnapshot(APIModel):
    schema_name: Literal["cheby.rich-message/1.0"] = Field(
        default="cheby.rich-message/1.0",
        alias="schema",
    )
    message_id: str
    thread_id: str
    turn_id: str
    source_item_id: str
    role: Literal["assistant"]
    state: Literal[
        "queued",
        "streaming",
        "waitingInput",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
    ]
    revision: int
    root_block_ids: List[str]
    blocks: Dict[str, Dict[str, Any]]
    fallback: Dict[str, str]
    created_at: str
    updated_at: str

    @field_validator("revision")
    @classmethod
    def non_negative_revision(cls, value: int) -> int:
        if value < 0:
            raise ValueError("revision must be non-negative")
        return value


class UserHistoryMessageSnapshot(APIModel):
    """GET-only user history projection; never emitted as a delta event."""

    schema_name: Literal["cheby.rich-message/1.0"] = Field(
        default="cheby.rich-message/1.0",
        alias="schema",
    )
    message_id: str
    thread_id: str
    turn_id: str
    source_item_id: str
    role: Literal["user"] = "user"
    state: Literal["queued", "completed", "failed"] = "completed"
    revision: Literal[0] = 0
    root_block_ids: List[str]
    blocks: Dict[str, Dict[str, Any]]
    fallback: Dict[str, str]
    created_at: str
    updated_at: str
    client_message_id: str
