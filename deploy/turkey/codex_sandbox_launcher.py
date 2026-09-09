#!/usr/bin/env python3
"""Launch Codex app-server in its reviewed outer mount namespace."""

from __future__ import annotations

import os
import sys


BWRAP_COMMAND = (
    "/usr/bin/bwrap",
    "--die-with-parent",
    "--new-session",
    "--ro-bind",
    "/",
    "/",
    "--dev-bind",
    "/dev",
    "/dev",
    # The managed permission-profile sandbox creates a nested user namespace.
    # It must be able to update its own uid/gid maps through procfs. The
    # untrusted native-tool process receives a separately restricted /proc from
    # Codex, so this bind is visible only to the trusted app-server launcher.
    "--bind",
    "/proc",
    "/proc",
    "--bind",
    "/workspace",
    "/workspace",
    "--bind",
    "/home/cheby/.codex",
    "/home/cheby/.codex",
    "--bind",
    "/home/cheby/.cache",
    "/home/cheby/.cache",
    "--bind",
    "/home/cheby/.config",
    "/home/cheby/.config",
    "--tmpfs",
    "/run/secrets",
    "--tmpfs",
    "/data",
    "--tmpfs",
    "/tmp",
)


def command(argv: list[str]) -> tuple[str, ...]:
    expected = [
        "codex",
        "--ask-for-approval",
        "never",
        "app-server",
        "--disable",
        "code_mode_host",
        "--disable",
        "code_mode",
        "--listen",
        "stdio://",
    ]
    if argv != expected:
        raise ValueError("Codex sandbox launcher received an unreviewed command")
    return BWRAP_COMMAND + ("--chdir", "/workspace", "--") + tuple(argv)


def main() -> None:
    try:
        value = command(sys.argv[1:])
    except ValueError as error:
        raise SystemExit(str(error)) from None
    os.execv(value[0], value)


if __name__ == "__main__":
    main()
