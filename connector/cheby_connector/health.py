from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path


async def run_health_watchdog(path: str, interval_seconds: float) -> None:
    """Refresh a local liveness file from the main asyncio event loop."""

    health_path = Path(path)
    health_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not health_path.parent.is_dir():
        raise RuntimeError("Connector health parent is not a directory")
    while True:
        _atomic_refresh(health_path)
        await asyncio.sleep(interval_seconds)


def _atomic_refresh(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix="." + path.name + ".",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        payload = f"{os.getpid()} {time.time_ns()}\n".encode("ascii")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
