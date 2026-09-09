#!/usr/bin/env python3
"""Archive bounded PhoneBridge controller sessions from a dedicated Codex home."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Iterable

try:
    from cheby_gateway.bridge import BridgeThread, StdioCodexBridge
except ModuleNotFoundError:  # Repository-root invocation before package installation.
    from gateway.cheby_gateway.bridge import BridgeThread, StdioCodexBridge


UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def candidates(
    threads: Iterable[BridgeThread],
    *,
    after: str,
    marker: str,
) -> list[BridgeThread]:
    marker_lower = marker.casefold()
    return sorted(
        (
            thread
            for thread in threads
            if thread.source_kind == "exec"
            and thread.created_at is not None
            and thread.created_at >= after
            and marker_lower in thread.title.casefold()
        ),
        key=lambda thread: thread.raw_id,
    )


async def run(args: argparse.Namespace) -> int:
    bridge = StdioCodexBridge(
        command=("codex", "app-server", "--listen", "stdio://"),
        cwd=args.cwd,
    )
    await bridge.start()
    try:
        selected = candidates(
            await bridge.list_threads(archived=False),
            after=args.after,
            marker=args.title_marker,
        )
        evidence = {
            "after": args.after,
            "titleMarker": args.title_marker,
            "apply": args.apply,
            "count": len(selected),
            "threadIds": [thread.raw_id for thread in selected],
        }
        if not args.apply:
            print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
            return 0
        if not selected:
            raise RuntimeError("No bounded controller sessions matched; refusing an empty apply")
        for thread in selected:
            await bridge.set_thread_archived(thread.raw_id, True)
        remaining = {thread.raw_id for thread in await bridge.list_threads(archived=False)}
        archived = {thread.raw_id for thread in await bridge.list_threads(archived=True)}
        selected_ids = set(evidence["threadIds"])
        if selected_ids & remaining or not selected_ids <= archived:
            raise RuntimeError("Codex did not verify the complete archival set")
        evidence["verified"] = True
        print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
        return 0
    finally:
        await bridge.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--after", required=True)
    parser.add_argument("--title-marker", default="phonebridge")
    parser.add_argument("--cwd", default="/workspace")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not UTC_TIMESTAMP.fullmatch(args.after):
        parser.error("--after must be a canonical UTC timestamp ending in Z")
    if len(args.title_marker.strip()) < 6:
        parser.error("--title-marker must contain at least six non-space characters")
    if not args.cwd.startswith("/"):
        parser.error("--cwd must be absolute")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
