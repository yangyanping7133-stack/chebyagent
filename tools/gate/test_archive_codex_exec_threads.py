from __future__ import annotations

from tools.gate.archive_codex_exec_threads import BridgeThread, candidates


def thread(raw_id: str, *, source: str, created: str, title: str) -> BridgeThread:
    return BridgeThread(
        raw_id=raw_id,
        source_kind=source,
        created_at=created,
        title=title,
    )


def test_candidates_require_exec_cutoff_and_phonebridge_marker() -> None:
    selected = candidates(
        [
            thread("match", source="exec", created="2026-08-07T11:00:00Z", title="PhoneBridge gate"),
            thread("old", source="exec", created="2026-08-07T10:00:00Z", title="PhoneBridge gate"),
            thread("user", source="vscode", created="2026-08-07T11:00:00Z", title="PhoneBridge gate"),
            thread("other", source="exec", created="2026-08-07T11:00:00Z", title="User task"),
        ],
        after="2026-08-07T10:50:00Z",
        marker="phonebridge",
    )

    assert [item.raw_id for item in selected] == ["match"]
