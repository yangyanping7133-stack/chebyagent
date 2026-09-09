#!/usr/bin/env python3
"""Add a deny-signal seccomp filter to every Codex native-tool sandbox."""

from __future__ import annotations

import errno
import os
import struct
import sys


REAL_BWRAP = "/usr/bin/bwrap"
AUDIT_ARCH_X86_64 = 0xC000003E
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

# x86_64 signal-delivery syscalls. The app-server remains outside this filter,
# so it can still interrupt and time out its own sandboxed children.
DENIED_SYSCALLS = (62, 129, 200, 234, 297, 424)

BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_RET_K = 0x06


def instruction(code: int, jump_true: int, jump_false: int, value: int) -> bytes:
    return struct.pack("=HBBI", code, jump_true, jump_false, value)


def signal_filter() -> bytes:
    result = bytearray()
    # Reject execution under an unexpected syscall ABI instead of allowing a
    # syscall-number collision to bypass the deny list.
    result += instruction(BPF_LD_W_ABS, 0, 0, 4)  # seccomp_data.arch
    result += instruction(BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH_X86_64)
    result += instruction(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS)
    result += instruction(BPF_LD_W_ABS, 0, 0, 0)  # seccomp_data.nr
    for syscall_number in DENIED_SYSCALLS:
        result += instruction(BPF_JMP_JEQ_K, 0, 1, syscall_number)
        result += instruction(BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM)
    result += instruction(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW)
    return bytes(result)


def command(argv: list[str], filter_fd: int) -> tuple[str, ...]:
    if filter_fd < 3:
        raise ValueError("seccomp filter descriptor must not replace stdio")
    return (
        REAL_BWRAP,
        "--add-seccomp-fd",
        str(filter_fd),
        *argv,
    )


def main() -> None:
    if sys.argv[1:] in (["--help"], ["--version"]):
        os.execv(REAL_BWRAP, (REAL_BWRAP, *sys.argv[1:]))
    descriptor = os.memfd_create("chebycodex-no-signals", flags=0)
    os.write(descriptor, signal_filter())
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    value = command(sys.argv[1:], descriptor)
    os.execv(value[0], value)


if __name__ == "__main__":
    main()
