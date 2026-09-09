#!/usr/bin/env python3
"""Install the pinned Docker-default-plus-bubblewrap seccomp profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.request import urlopen


MOBY_DEFAULT_URL = (
    "https://raw.githubusercontent.com/moby/moby/"
    "v26.1.3/profiles/seccomp/default.json"
)
MOBY_DEFAULT_SHA256 = (
    "9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76"
)
CLONE_NEWUSER = 0x10000000
NARROW_RULES: tuple[dict[str, Any], ...] = (
    {
        "names": ["clone"],
        "action": "SCMP_ACT_ALLOW",
        "args": [
            {
                "index": 0,
                "value": CLONE_NEWUSER,
                "valueTwo": CLONE_NEWUSER,
                "op": "SCMP_CMP_MASKED_EQ",
            }
        ],
        "excludes": {"arches": ["s390", "s390x"]},
    },
    {
        "names": ["unshare", "mount", "umount2", "pivot_root"],
        "action": "SCMP_ACT_ALLOW",
    },
)


class SeccompProfileError(RuntimeError):
    pass


def _unconditional_allow(rule: object, names: set[str]) -> bool:
    if not isinstance(rule, dict) or rule.get("action") != "SCMP_ACT_ALLOW":
        return False
    rule_names = rule.get("names")
    return (
        isinstance(rule_names, list)
        and bool(names.intersection(rule_names))
        and not any(key in rule for key in ("args", "includes", "excludes"))
    )


def build_profile(
    source: bytes,
    *,
    expected_source_sha256: str = MOBY_DEFAULT_SHA256,
) -> bytes:
    if hashlib.sha256(source).hexdigest() != expected_source_sha256:
        raise SeccompProfileError("Moby default seccomp source hash differs")
    try:
        profile = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeccompProfileError("Moby default seccomp source is invalid") from exc
    if not isinstance(profile, dict) or profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise SeccompProfileError("Moby default seccomp policy is not fail-closed")
    syscalls = profile.get("syscalls")
    if not isinstance(syscalls, list):
        raise SeccompProfileError("Moby default seccomp syscall list is missing")
    widened = {"clone", "unshare", "mount", "umount2", "pivot_root"}
    if any(_unconditional_allow(rule, widened) for rule in syscalls):
        raise SeccompProfileError("Moby source already contains an unexpected broad rule")
    syscalls.extend(json.loads(json.dumps(rule)) for rule in NARROW_RULES)
    return (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_source(path: Path | None) -> bytes:
    if path is not None:
        return path.read_bytes()
    with urlopen(MOBY_DEFAULT_URL, timeout=30) as response:
        return response.read()


def install_profile(payload: bytes, output: Path) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_profile(_read_source(args.source))
    install_profile(payload, args.output)
    print(f"Codex bubblewrap seccomp profile: PASS ({hashlib.sha256(payload).hexdigest()})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SeccompProfileError) as exc:
        raise SystemExit(f"Codex bubblewrap seccomp profile: FAIL: {exc}") from None
