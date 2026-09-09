from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MAX_BLOCK_TEXT = 65_536
MAX_PUBLIC_MESSAGE_BYTES = 256 * 1024
TRUNCATION_MARKER = "\n\n[truncated]"
COMPACT_BLOCK_FALLBACK_BYTES = 1_024
COMPACT_MESSAGE_FALLBACK_BYTES = 4_096
MAX_EVIDENCE_BLOCKS = 48
MAX_EVIDENCE_LABEL_BYTES = 256
MAX_EVIDENCE_DETAIL_BYTES = 1_024
MAX_DIFF_PREVIEW_BYTES = 2_048


@dataclass(frozen=True)
class ProjectionItem:
    """Private, normalized input for the public one-panel-per-Turn projection."""

    item_id: str
    ordinal: int
    type: str
    status: Optional[str] = None
    phase: Optional[str] = None
    text: str = ""
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    change_count: int = 0
    evidence: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def bounded_text(value: str, limit: int = MAX_BLOCK_TEXT) -> Tuple[str, bool]:
    value = unicode_safe_text(value)
    encoded = value.encode("utf-8")
    if len(value) <= limit and len(encoded) <= limit:
        return value, False
    marker = TRUNCATION_MARKER.encode("utf-8")
    clipped = encoded[: max(0, limit - len(marker))]
    while clipped:
        try:
            prefix = clipped.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    else:
        prefix = ""
    return prefix + TRUNCATION_MARKER, True


def unicode_safe_text(value: str) -> str:
    """Replace lone UTF-16 surrogates before UTF-8 or JSON serialization."""

    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in value
    )


def empty_live_work_panel(
    *,
    message_id: str,
    thread_id: str,
    turn_id: str,
    source_item_id: str,
    created_at: str,
) -> Dict[str, Any]:
    status = _status_block("inProgress", 0)
    return {
        "schema": "cheby.rich-message/1.0",
        "messageId": message_id,
        "threadId": thread_id,
        "turnId": turn_id,
        "sourceItemId": source_item_id,
        "role": "assistant",
        "state": "streaming",
        "revision": 0,
        "rootBlockIds": ["status"],
        "blocks": {"status": status},
        "fallback": {"text": "Codex is working"},
        "createdAt": created_at,
        "updatedAt": created_at,
    }


def project_live_work_panel(
    *,
    message_id: str,
    thread_id: str,
    turn_id: str,
    source_item_id: str,
    turn_status: str,
    items: Sequence[ProjectionItem],
    created_at: str,
    updated_at: str,
    revision: int = 0,
) -> Dict[str, Any]:
    ordered = sorted(items, key=lambda item: (item.ordinal, item.item_id))
    agent_items = [item for item in ordered if item.type == "agentMessage"]
    terminal = turn_status in {
        "completed",
        "failed",
        "interrupted",
        "cancelled",
        "canceled",
    }
    promoted_item_id: Optional[str] = None
    if terminal and agent_items and not any(
        item.phase == "final_answer" for item in agent_items
    ):
        # 0.145 explicitly documents phase=None as a compatibility case. On a
        # terminal Turn the last unknown-phase message is the safest final answer.
        unknown = [item for item in agent_items if item.phase is None]
        if unknown:
            promoted_item_id = unknown[-1].item_id

    activity_lines: List[str] = []
    answers: List[str] = []
    for item in ordered:
        text = item.text.strip()
        if item.type == "agentMessage":
            if not text:
                continue
            if item.phase == "final_answer" or item.item_id == promoted_item_id:
                answers.append(text)
            else:
                activity_lines.append(text)
            continue
        safe_line = _safe_activity_line(item)
        if safe_line:
            activity_lines.append(safe_line)

    activity_text, activity_truncated = bounded_text("\n\n".join(activity_lines))
    answer_text, answer_truncated = bounded_text("\n\n".join(answers))
    activity_truncated = activity_truncated or any(
        value.endswith(TRUNCATION_MARKER) for value in activity_lines
    )
    answer_truncated = answer_truncated or any(
        value.endswith(TRUNCATION_MARKER) for value in answers
    )

    counts = _counts(ordered)
    blocks: Dict[str, Dict[str, Any]] = {
        "status": _status_block(turn_status, len(ordered)),
    }
    roots = ["status"]
    if activity_text:
        blocks["activity"] = {
            "type": "text",
            "blockId": "activity",
            "text": activity_text,
            "markdown": True,
            "fallbackText": activity_text,
            **({"truncated": True} if activity_truncated else {}),
        }
        roots.append("activity")
    evidence_blocks: List[Tuple[str, Dict[str, Any]]] = []
    for item in ordered:
        block = _structured_evidence_block(item)
        if block is None:
            continue
        evidence_blocks.append((str(block["blockId"]), block))
    # Android accepts at most 64 root blocks. Retain the newest structured
    # evidence while reserving room for status, activity, metrics, and answer.
    for block_id, block in evidence_blocks[-MAX_EVIDENCE_BLOCKS:]:
        blocks[block_id] = block
        roots.append(block_id)
    if any(counts.values()):
        metric_items = []
        for label, value in (
            ("Updates", counts["updates"]),
            ("Commands", counts["commands"]),
            ("File changes", counts["file_changes"]),
            ("Tools", counts["tools"]),
        ):
            if value:
                metric_items.append({"label": label, "value": str(value)})
        blocks["metrics"] = {
            "type": "metrics",
            "blockId": "metrics",
            "items": metric_items,
            "fallbackText": "; ".join(
                "%s %s" % (item["label"], item["value"])
                for item in metric_items
            ),
        }
        roots.append("metrics")
    if answer_text:
        blocks["answer"] = {
            "type": "text",
            "blockId": "answer",
            "text": answer_text,
            "markdown": True,
            "fallbackText": answer_text,
            **({"truncated": True} if answer_truncated else {}),
        }
        roots.append("answer")

    fallback = answer_text or activity_text or blocks["status"]["fallbackText"]
    snapshot = {
        "schema": "cheby.rich-message/1.0",
        "messageId": message_id,
        "threadId": thread_id,
        "turnId": turn_id,
        "sourceItemId": source_item_id,
        "role": "assistant",
        "state": _message_state(turn_status),
        "revision": revision,
        "rootBlockIds": roots,
        "blocks": blocks,
        "fallback": {"text": bounded_text(fallback)[0]},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }
    return _fit_public_message_budget(snapshot)


def patch_ops(previous: Dict[str, Any], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    previous_blocks = previous.get("blocks") or {}
    current_blocks = current.get("blocks") or {}
    for block_id in current.get("rootBlockIds") or []:
        if previous_blocks.get(block_id) != current_blocks.get(block_id):
            ops.append(
                {
                    "op": "block.put",
                    "blockId": block_id,
                    "value": current_blocks[block_id],
                }
            )
    for block_id in previous_blocks:
        if block_id not in current_blocks:
            ops.append({"op": "block.remove", "blockId": block_id})
    if previous.get("rootBlockIds") != current.get("rootBlockIds"):
        ops.append({"op": "root.set", "value": current.get("rootBlockIds") or []})
    if previous.get("state") != current.get("state"):
        ops.append({"op": "message.state.set", "value": current.get("state")})
    return ops


def semantically_equal(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    ignored = {"revision", "updatedAt"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def serialized_public_message_bytes(snapshot: Dict[str, Any]) -> int:
    """Use the conservative, human-readable JSON representation for the cap."""

    return len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))


def _fit_public_message_budget(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministically degrade duplicated text before primary panel content.

    A text block carries both ``text`` and ``fallbackText`` and the message also
    carries a top-level fallback. Two individually legal 64 KiB blocks can
    therefore exceed the 256 KiB public-message budget through duplication or
    JSON escaping.  Fallbacks remain readable, metrics remain intact, and any
    primary text reduced here is explicitly marked as truncated.
    """

    if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
        return snapshot

    blocks = snapshot.get("blocks") or {}
    for block_id in ("activity", "answer"):
        block = blocks.get(block_id)
        if not isinstance(block, dict):
            continue
        fallback_text = block.get("fallbackText")
        if isinstance(fallback_text, str):
            block["fallbackText"] = _truncate_with_marker(
                fallback_text,
                COMPACT_BLOCK_FALLBACK_BYTES,
            )
    if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
        return snapshot

    fallback = snapshot.get("fallback")
    if isinstance(fallback, dict) and isinstance(fallback.get("text"), str):
        fallback["text"] = _truncate_with_marker(
            fallback["text"],
            COMPACT_MESSAGE_FALLBACK_BYTES,
        )
    if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
        return snapshot

    # Preserve the final answer preferentially. Activity is useful, but it is
    # the first primary field to yield when JSON escaping consumes the envelope.
    for block_id in ("activity", "answer"):
        if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
            break
        _fit_block_text(snapshot, block_id)

    # Generated public IDs and timestamps are bounded, so legal projection
    # input reaches the cap above. These deterministic fallbacks keep the
    # projector non-throwing if a future optional field grows unexpectedly.
    for block_id in ("activity", "answer"):
        if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
            break
        blocks.pop(block_id, None)
        roots = snapshot.get("rootBlockIds") or []
        snapshot["rootBlockIds"] = [value for value in roots if value != block_id]
    # Structured evidence fields are tightly bounded, but a future combination
    # of many optional blocks must still fail readable and within the envelope.
    # Prefer the newest evidence and remove the oldest blocks first.
    for block_id in list(snapshot.get("rootBlockIds") or []):
        if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
            break
        block = blocks.get(block_id)
        if not isinstance(block, dict) or block.get("type") not in {
            "tool",
            "diff",
            "test",
        }:
            continue
        blocks.pop(block_id, None)
        snapshot["rootBlockIds"] = [
            value
            for value in snapshot.get("rootBlockIds") or []
            if value != block_id
        ]
    if serialized_public_message_bytes(snapshot) > MAX_PUBLIC_MESSAGE_BYTES:
        snapshot["fallback"] = {"text": blocks["status"]["fallbackText"]}
    return snapshot


def _fit_block_text(snapshot: Dict[str, Any], block_id: str) -> None:
    block = (snapshot.get("blocks") or {}).get(block_id)
    if not isinstance(block, dict) or not isinstance(block.get("text"), str):
        return
    original = block["text"]
    block["truncated"] = True
    marker_bytes = len(TRUNCATION_MARKER.encode("utf-8"))
    low = marker_bytes
    high = max(marker_bytes, len(original.encode("utf-8")))
    block["text"] = TRUNCATION_MARKER
    if serialized_public_message_bytes(snapshot) > MAX_PUBLIC_MESSAGE_BYTES:
        return
    best = block["text"]
    while low <= high:
        middle = (low + high) // 2
        candidate = _truncate_with_marker(original, middle, force=True)
        block["text"] = candidate
        if serialized_public_message_bytes(snapshot) <= MAX_PUBLIC_MESSAGE_BYTES:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    block["text"] = best


def _truncate_with_marker(value: str, limit: int, *, force: bool = False) -> str:
    encoded = value.encode("utf-8")
    if not force and len(encoded) <= limit:
        return value
    source = (
        value[: -len(TRUNCATION_MARKER)]
        if value.endswith(TRUNCATION_MARKER)
        else value
    )
    marker = TRUNCATION_MARKER.encode("utf-8")
    clipped = source.encode("utf-8")[: max(0, limit - len(marker))]
    while clipped:
        try:
            prefix = clipped.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    else:
        prefix = ""
    return prefix + TRUNCATION_MARKER


def _safe_activity_line(item: ProjectionItem) -> Optional[str]:
    status = (item.status or "updated").lower()
    if item.type == "commandExecution":
        if item.exit_code is not None:
            return "Command finished with exit code %d." % item.exit_code
        return "Command %s." % _safe_status(status)
    if item.type == "fileChange":
        count = max(0, item.change_count)
        return "File changes %s%s." % (
            _safe_status(status),
            " (%d)" % count if count else "",
        )
    if _is_tool(item.type):
        return "Tool call %s." % _safe_status(status)
    if item.type not in {"agentMessage", "userMessage", "reasoning", "plan"}:
        return "Codex activity updated."
    return None


def _structured_evidence_block(
    item: ProjectionItem,
) -> Optional[Dict[str, Any]]:
    """Project only allowlisted structured evidence into typed public blocks.

    Arbitrary command output and terminal text never enter this path. The
    service supplies a small, already-redacted evidence object derived from
    explicit Codex item fields; these bounds are a second public-boundary gate.
    """

    evidence = item.evidence or {}
    kind = str(evidence.get("kind") or "")
    block_id = _evidence_block_id(item, kind)
    state = _evidence_state(
        str(evidence["status"]) if evidence.get("status") is not None else item.status
    )
    duration_label = _duration_label(
        evidence.get("durationMs", item.duration_ms)
    )
    if kind == "tool":
        label = _evidence_text(evidence.get("label"), "Tool")
        detail = _evidence_text(
            evidence.get("detail"),
            _safe_status(item.status or "updated").capitalize(),
            MAX_EVIDENCE_DETAIL_BYTES,
        )
        block: Dict[str, Any] = {
            "type": "tool",
            "blockId": block_id,
            "label": label,
            "detail": detail,
            "state": state,
            "fallbackText": "%s: %s" % (label, detail),
        }
        if duration_label is not None:
            block["durationLabel"] = duration_label
        return block
    if kind == "diff":
        file_label = _evidence_text(
            evidence.get("fileLabel"),
            "Changed files",
        )
        summary = _evidence_text(
            evidence.get("summary"),
            "File changes %s" % _safe_status(item.status or "updated"),
            MAX_EVIDENCE_DETAIL_BYTES,
        )
        block = {
            "type": "diff",
            "blockId": block_id,
            "fileLabel": file_label,
            "summary": summary,
            "fallbackText": "%s: %s" % (file_label, summary),
        }
        preview = evidence.get("preview")
        if isinstance(preview, str) and preview:
            block["preview"] = _evidence_text(
                preview,
                "",
                MAX_DIFF_PREVIEW_BYTES,
            )
            if evidence.get("truncated") is True:
                block["truncated"] = True
        for field in ("additions", "deletions"):
            value = evidence.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                block[field] = value
        return block
    if kind == "test":
        title = _evidence_text(evidence.get("title"), "Tests")
        summary = _evidence_text(
            evidence.get("summary"),
            "Structured test evidence updated",
            MAX_EVIDENCE_DETAIL_BYTES,
        )
        block = {
            "type": "test",
            "blockId": block_id,
            "title": title,
            "summary": summary,
            "state": state,
            "fallbackText": "%s: %s" % (title, summary),
        }
        for field in ("passed", "failed", "skipped"):
            value = evidence.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                block[field] = value
        if duration_label is not None:
            block["durationLabel"] = duration_label
        return block
    return None


def _evidence_block_id(item: ProjectionItem, kind: str) -> str:
    digest = hashlib.sha256(
        ("CHEBY-EVIDENCE-1\0" + item.item_id + "\0" + kind).encode("utf-8")
    ).hexdigest()[:16]
    return "evidence-%s-%s" % (kind or "unknown", digest)


def _evidence_text(
    value: Any,
    fallback: str,
    limit: int = MAX_EVIDENCE_LABEL_BYTES,
) -> str:
    candidate = value if isinstance(value, str) and value.strip() else fallback
    return bounded_text(str(candidate).strip(), limit)[0]


def _evidence_state(status: Optional[str]) -> str:
    normalized = str(status or "pending").lower().replace("_", "").replace("-", "")
    return {
        "inprogress": "running",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "declined": "failed",
        "interrupted": "failed",
        "cancelled": "failed",
        "canceled": "failed",
    }.get(normalized, "pending")


def _duration_label(value: Any) -> Optional[str]:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    if value < 1_000:
        return "%d ms" % value
    seconds = value / 1_000
    return ("%.1f s" % seconds).replace(".0 s", " s")


def _counts(items: Iterable[ProjectionItem]) -> Dict[str, int]:
    counts = {"updates": 0, "commands": 0, "file_changes": 0, "tools": 0}
    for item in items:
        if item.type == "agentMessage" and item.text.strip():
            counts["updates"] += 1
        elif item.type == "commandExecution":
            counts["commands"] += 1
        elif item.type == "fileChange":
            counts["file_changes"] += max(1, item.change_count)
        elif _is_tool(item.type):
            counts["tools"] += 1
    return counts


def _is_tool(item_type: str) -> bool:
    normalized = item_type.lower()
    return "tool" in normalized or normalized in {"mcpToolCall", "dynamicToolCall"}


def _safe_status(status: str) -> str:
    return {
        "inprogress": "is running",
        "running": "is running",
        "completed": "completed",
        "failed": "failed",
        "declined": "was declined",
        "interrupted": "was interrupted",
    }.get(status.replace("_", "").replace("-", ""), "updated")


def _status_block(turn_status: str, item_count: int) -> Dict[str, Any]:
    normalized = (turn_status or "inProgress").lower()
    terminal = normalized in {"completed", "failed", "interrupted", "cancelled", "canceled"}
    label, tone = {
        "completed": ("Codex completed the task", "success"),
        "failed": ("Codex could not complete the task", "danger"),
        "interrupted": ("Codex was interrupted", "warning"),
        "cancelled": ("Codex was cancelled", "warning"),
        "canceled": ("Codex was cancelled", "warning"),
    }.get(normalized, ("Codex is working", "info"))
    detail = "%d activity update%s" % (item_count, "" if item_count == 1 else "s")
    block = {
        "type": "status",
        "blockId": "status",
        "label": label,
        "detail": detail,
        "tone": tone,
        "fallbackText": "%s. %s." % (label, detail),
    }
    if terminal:
        block["progress"] = 1.0
    return block


def _message_state(turn_status: str) -> str:
    return {
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "waitingUser": "waitingInput",
        "waitingOnUser": "waitingInput",
    }.get(turn_status, "streaming")
