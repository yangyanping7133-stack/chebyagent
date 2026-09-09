#!/usr/bin/env python3
"""Check that the deployment dependency lock is complete and hash-pinned."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


ENTRY = re.compile(
    r"^([a-z0-9][a-z0-9-]*)==([^\s\\]+)\s+\\\n\s+--hash=sha256:([0-9a-f]{64})$"
)


def requirements(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        values[name.replace("_", "-").lower()] = version
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--direct", type=Path, required=True)
    args = parser.parse_args()

    text = args.lock.read_text(encoding="utf-8")
    logical_entries = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current.append(line)
        if not line.rstrip().endswith("\\"):
            logical_entries.append("\n".join(current))
            current = []
    if current:
        raise RuntimeError("unterminated requirement entry")

    locked: dict[str, str] = {}
    for entry in logical_entries:
        match = ENTRY.fullmatch(entry)
        if not match:
            raise RuntimeError(f"invalid or unhashed lock entry: {entry.splitlines()[0]}")
        name, version, _ = match.groups()
        normalized = name.replace("_", "-").lower()
        if normalized in locked:
            raise RuntimeError(f"duplicate locked dependency: {normalized}")
        locked[normalized] = version

    direct = requirements(args.direct)
    for name, version in direct.items():
        if locked.get(name) != version:
            raise RuntimeError(f"direct dependency mismatch for {name}")
    if len(locked) < len(direct) + 5:
        raise RuntimeError("lock does not appear to include transitive dependencies")
    print(f"hashed Python dependency lock: PASS ({len(locked)} packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
