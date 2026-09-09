from __future__ import annotations

import importlib.util
from pathlib import Path
import struct

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex_bwrap_wrapper.py"
SPEC = importlib.util.spec_from_file_location("codex_bwrap_wrapper", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


def test_filter_is_arch_checked_and_denies_every_signal_syscall() -> None:
    payload = wrapper.signal_filter()
    assert len(payload) % 8 == 0
    instructions = [
        struct.unpack("=HBBI", payload[index:index + 8])
        for index in range(0, len(payload), 8)
    ]
    assert instructions[:4] == [
        (wrapper.BPF_LD_W_ABS, 0, 0, 4),
        (wrapper.BPF_JMP_JEQ_K, 1, 0, wrapper.AUDIT_ARCH_X86_64),
        (wrapper.BPF_RET_K, 0, 0, wrapper.SECCOMP_RET_KILL_PROCESS),
        (wrapper.BPF_LD_W_ABS, 0, 0, 0),
    ]
    compared = {
        value
        for code, _jump_true, _jump_false, value in instructions
        if code == wrapper.BPF_JMP_JEQ_K and value != wrapper.AUDIT_ARCH_X86_64
    }
    assert compared == set(wrapper.DENIED_SYSCALLS)
    assert instructions[-1] == (wrapper.BPF_RET_K, 0, 0, wrapper.SECCOMP_RET_ALLOW)


def test_command_adds_filter_before_reviewed_bwrap_arguments() -> None:
    assert wrapper.command(["--ro-bind", "/", "/", "true"], 9) == (
        "/usr/bin/bwrap",
        "--add-seccomp-fd",
        "9",
        "--ro-bind",
        "/",
        "/",
        "true",
    )
    with pytest.raises(ValueError, match="stdio"):
        wrapper.command(["true"], 2)
