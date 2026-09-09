from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "codex_sandbox_launcher.py"
SPEC = importlib.util.spec_from_file_location("codex_sandbox_launcher", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


REVIEWED = [
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


def test_launcher_isolates_secrets_data_and_supports_inner_profile_sandbox() -> None:
    command = launcher.command(REVIEWED)
    assert command[0] == "/usr/bin/bwrap"
    assert ("--ro-bind", "/", "/") == command[command.index("--ro-bind"):command.index("--ro-bind") + 3]
    assert "--dev-bind" in command
    proc_index = command.index("/proc")
    assert command[proc_index - 1:proc_index + 2] == ("--bind", "/proc", "/proc")
    assert command.count("/run/secrets") == 1
    assert command.count("/data") == 1
    assert command[-len(REVIEWED):] == tuple(REVIEWED)


def test_launcher_rejects_command_drift() -> None:
    with pytest.raises(ValueError, match="unreviewed"):
        launcher.command(["codex", "app-server"])
