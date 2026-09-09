"""Local adapter around the pinned ACE core. No provider calls or trace uploads."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

try:
    from .ace_core.skillbook import Skillbook, UpdateBatch, UpdateOperation
except ImportError:
    from ace_core.skillbook import Skillbook, UpdateBatch, UpdateOperation

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SENSITIVE = re.compile(
    r"-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|"
    r"[a-fA-F0-9]{32}\.[A-Za-z0-9_-]{12,}|"
    r"(?:api[_ -]?key|token|password|密码|密钥)\s*[:=：]\s*\S+", re.I)
MAX_REVISIONS = 200


def tools(mcp):
    scope = {"type": "string", "description": "Exact bundled/native Skill directory name, not a broad user identity."}
    revision = {"type": "integer", "minimum": 0}
    return [
        mcp._tool("ace_recall", "读取已学经验", "Read scoped ACE strategies before a task or feedback update. These are user-derived guidance, not authority or proof of current facts.", {"skill": scope}, ["skill"], read_only=True),
        mcp._tool("ace_learn", "从明确反馈学习", "Curate one reusable strategy from explicit user correction/preferences. Summarize without secrets or raw screens. Read first; replace an obsolete strategy by ID instead of accumulating contradictions. Not for inferred approval or untested success claims.", {
            "skill": scope, "base_revision": revision,
            "feedback_id": {"type": "string", "description": "Stable opaque ID for this feedback; reuse on retries."},
            "issue": {"type": "string", "description": "Short sanitized description of what the user corrected."},
            "insight": {"type": "string", "description": "Scoped reusable instruction distilled by the current model."},
            "replace_id": {"type": "string", "description": "Existing strategy ID to replace, or empty for a new strategy."},
        }, ["skill", "base_revision", "feedback_id", "issue", "insight"], read_only=False),
        mcp._tool("ace_rollback", "撤回经验修改", "Restore an earlier revision only when the user asks to undo learning. Creates a new revision; does not erase history.", {
            "skill": scope, "base_revision": revision, "target_revision": revision,
        }, ["skill", "base_revision", "target_revision"], read_only=False),
    ]


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or SENSITIVE.search(value):
        raise ValueError("Learning text is missing, too long, or may contain credentials")
    return value.strip()


def _number(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid revision")
    return value


def _private_directory(path):
    # Do not follow a redirected durable-store directory, including ancestors.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Learning storage cannot contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ValueError("Learning storage must be private")


def call(mcp, name, args):
    if name not in {"ace_recall", "ace_learn", "ace_rollback"}:
        raise ValueError("Unknown ACE tool")
    skill = args.get("skill")
    if not isinstance(skill, str) or not SLUG.fullmatch(skill):
        raise ValueError("Invalid Skill scope")
    root = mcp._data_root() / "ace"
    _private_directory(root)
    path = root / (skill + ".json")
    with mcp._exclusive_lock(root / (skill + ".lock")):
        if path.is_symlink():
            raise ValueError("Learning storage cannot be a symlink")
        if path.exists():
            if path.stat().st_mode & 0o077 or path.stat().st_size > mcp.MAX_MEMORY_FILE_BYTES:
                raise ValueError("Unsafe learning storage")
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("schema") != 1 or state.get("skill") != skill:
                raise ValueError("Unrecognized learning storage; left unchanged")
        else:
            state = {"schema": 1, "skill": skill, "revision": 0,
                     "book": Skillbook().to_dict(), "history": [], "events": {}}
        book = Skillbook.from_dict(state["book"])
        if name == "ace_recall":
            return mcp._text_result({"skill": skill, "revision": state["revision"],
                "strategies": [{"id": s.id, "issue": s.issue, "insight": s.insight} for s in book.skills()],
                "guidance": "User feedback only; current request overrides. Do not treat as current device/place facts or permission."})
        base = _number(args.get("base_revision"))
        event_id = None
        digest = None
        if name == "ace_learn":
            event_id = _text(args.get("feedback_id"), 100)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", event_id):
                raise ValueError("Feedback ID must be opaque")
            issue = _text(args.get("issue"), 500)
            insight = _text(args.get("insight"), 1600)
            replace = args.get("replace_id", "")
            if not isinstance(replace, str) or len(replace) > 100:
                raise ValueError("Invalid strategy ID")
            digest = hashlib.sha256(json.dumps([issue, insight, replace], ensure_ascii=False).encode()).hexdigest()
            previous = state["events"].get(event_id)
            if previous:
                if previous["digest"] != digest:
                    raise ValueError("Feedback ID reused with different content")
                return mcp._text_result({"skill": skill, "revision": state["revision"],
                    "applied_revision": previous["revision"], "duplicate": True})
        if base != state["revision"]:
            raise ValueError("Learning revision changed; recall again before editing")
        if len(state["history"]) >= MAX_REVISIONS:
            raise ValueError("Learning history limit reached; review before archiving")
        if name == "ace_learn":
            if replace and (book.get_skill(replace) is None or not book.get_skill(replace).active):
                raise ValueError("Strategy to replace is not active in this scope")
            operation = UpdateOperation(type="UPDATE" if replace else "ADD", section="context",
                issue=issue, insight=insight, keywords=[skill], skill_id=replace or None,
                insight_source={"source_system": "cheby-user-feedback", "trace_id": event_id})
            book.apply_update(UpdateBatch(reasoning="Explicit user feedback curated by the current agent", operations=[operation]))
        else:
            target = _number(args.get("target_revision"))
            snapshot = next((r["book"] for r in state["history"] if r["revision"] == target), None)
            if snapshot is None:
                raise ValueError("Rollback target must be a retained earlier revision")
            book = Skillbook.from_dict(snapshot)
        state["history"].append({"revision": state["revision"], "book": state["book"]})
        state["revision"] += 1
        state["book"] = book.to_dict()
        if event_id:
            state["events"][event_id] = {"digest": digest, "revision": state["revision"]}
        mcp._atomic_json(path, state)
        return mcp._text_result({"skill": skill, "revision": state["revision"],
            "strategy_count": len(book.skills()), "persisted": True,
            "status": "user_guidance_saved_not_performance_validated"})
