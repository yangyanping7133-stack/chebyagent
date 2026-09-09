#!/usr/bin/env python3
"""Read real Codex threads and report aggregate evidence without content or IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import shlex
import sys

from cheby_gateway.bridge import StdioCodexBridge


def mib(raw: int) -> float:
    # Linux reports KiB; macOS reports bytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(raw / divisor, 2)


async def probe(args: argparse.Namespace) -> None:
    bridge = StdioCodexBridge(
        command=tuple(shlex.split(args.command)),
        cwd=args.cwd,
        request_timeout_seconds=args.timeout,
        protocol_frame_limit_bytes=args.frame_limit_bytes,
    )
    await bridge.start()
    threads = []
    try:
        raw_ids = list(args.thread_id or [])
        if not raw_ids:
            candidates = await bridge.list_threads(archived=False)
            if len(candidates) < args.count:
                candidates += await bridge.list_threads(archived=True)
            raw_ids = list(dict.fromkeys(thread.raw_id for thread in candidates))[
                : args.count
            ]
        if len(raw_ids) < args.count:
            raise RuntimeError(
                "requested %d real Threads but only %d were available"
                % (args.count, len(raw_ids))
            )
        threads = [await bridge.read_thread(raw_id) for raw_id in raw_ids]
    finally:
        await bridge.close()
    result = {
        "threadCount": len(threads),
        "turnCount": sum(len(thread.turns) for thread in threads),
        "itemCount": sum(
            len(turn.items) for thread in threads for turn in thread.turns
        ),
        "gatewayPeakRssMiB": mib(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "codexChildPeakRssMiB": mib(
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        ),
        "frameLimitBytes": args.frame_limit_bytes,
        "contentOrRawIdsEmitted": False,
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", action="append")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--cwd", required=True)
    parser.add_argument(
        "--command",
        default="codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--frame-limit-bytes", type=int, default=64 * 1024 * 1024)
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
