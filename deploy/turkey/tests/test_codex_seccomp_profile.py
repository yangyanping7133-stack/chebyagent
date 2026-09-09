from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "install_codex_seccomp_profile.py"
spec = importlib.util.spec_from_file_location("install_codex_seccomp_profile", MODULE_PATH)
assert spec is not None and spec.loader is not None
profile_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = profile_module
spec.loader.exec_module(profile_module)


def _source(extra_rule=None) -> bytes:
    document = {
        "defaultAction": "SCMP_ACT_ERRNO",
        "architectures": ["SCMP_ARCH_X86_64"],
        "syscalls": [{"names": ["read"], "action": "SCMP_ACT_ALLOW"}],
    }
    if extra_rule is not None:
        document["syscalls"].append(extra_rule)
    return json.dumps(document).encode()


def test_build_profile_preserves_default_and_adds_only_narrow_rules() -> None:
    source = _source()
    payload = profile_module.build_profile(
        source,
        expected_source_sha256=hashlib.sha256(source).hexdigest(),
    )
    document = json.loads(payload)
    assert document["syscalls"][0] == {
        "names": ["read"],
        "action": "SCMP_ACT_ALLOW",
    }
    assert document["syscalls"][-2:] == list(profile_module.NARROW_RULES)
    clone = document["syscalls"][-2]
    assert clone["args"] == [
        {
            "index": 0,
            "value": profile_module.CLONE_NEWUSER,
            "valueTwo": profile_module.CLONE_NEWUSER,
            "op": "SCMP_CMP_MASKED_EQ",
        }
    ]
    assert set(document["syscalls"][-1]["names"]) == {
        "unshare",
        "mount",
        "umount2",
        "pivot_root",
    }


def test_build_profile_rejects_source_hash_drift() -> None:
    with pytest.raises(profile_module.SeccompProfileError, match="hash differs"):
        profile_module.build_profile(_source(), expected_source_sha256="0" * 64)


def test_build_profile_rejects_existing_unconditional_namespace_rule() -> None:
    source = _source({"names": ["unshare"], "action": "SCMP_ACT_ALLOW"})
    with pytest.raises(profile_module.SeccompProfileError, match="broad rule"):
        profile_module.build_profile(
            source,
            expected_source_sha256=hashlib.sha256(source).hexdigest(),
        )


def test_install_profile_is_atomic_and_non_executable(tmp_path: Path) -> None:
    target = tmp_path / "private" / "seccomp.json"
    profile_module.install_profile(b"{}\n", target)
    assert target.read_bytes() == b"{}\n"
    assert target.stat().st_mode & 0o777 == 0o644
