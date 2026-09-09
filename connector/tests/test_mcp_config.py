from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from unittest.mock import Mock, patch

from cheby_connector.mcp_config import (
    MANAGED_BEGIN,
    MANAGED_END,
    PERMISSION_PROFILE_NAME,
    SERVER_NAMES,
    ensure_codex_mcp_config,
    validate_codex_mcp_registration,
)


SNIPPET = """
[permissions.cheby_mobile.filesystem]
":minimal" = "read"
"/opt/codex" = "read"
"/usr/local/bin/bwrap" = "read"
[permissions.cheby_mobile.filesystem.":workspace_roots"]
"." = "write"
[permissions.cheby_mobile.network]
enabled = true
[mcp_servers.phonebridge]
command = "python"
[mcp_servers.offline_memory]
command = "python"
[mcp_servers.offline_skill]
command = "python"
""".strip() + "\n"


def test_deployed_mcp_config_has_self_contained_python_runtime() -> None:
    snippet = Path(__file__).parents[2] / "deploy/turkey/codex-mcp.connector.toml"
    parsed = tomllib.loads(snippet.read_text(encoding="utf-8"))
    servers = parsed["mcp_servers"]

    assert set(servers) == SERVER_NAMES
    assert set(parsed["permissions"]) == {PERMISSION_PROFILE_NAME}
    profile = parsed["permissions"][PERMISSION_PROFILE_NAME]
    assert profile["filesystem"][":minimal"] == "read"
    assert profile["filesystem"]["/opt/codex"] == "read"
    assert profile["filesystem"]["/usr/local/bin/bwrap"] == "read"
    assert profile["filesystem"][":workspace_roots"] == {".": "write"}
    assert profile["network"] == {"enabled": True}
    for server in servers.values():
        assert server["command"] == "/usr/local/bin/python"
        assert server["args"][:2] == [
            "-I",
            "/app/connector/cheby_connector/local_mcp.py",
        ]
        assert server["cwd"] == "/app/connector"
        assert "PYTHONPATH" not in server["env"]

    assert all(
        server["default_tools_approval_mode"] == "approve"
        for server in servers.values()
    )
    assert "tools" not in servers["phonebridge"]
    phone_environment = servers["phonebridge"]["env"]
    assert phone_environment["CHEBY_PHONEBRIDGE_BROKER_SOCKET"].endswith("phonebridge-broker.sock")
    assert "CHEBY_PHONEBRIDGE_TOKEN_FILE" not in phone_environment
    assert "CHEBY_PHONEBRIDGE_URL" not in phone_environment


def test_isolated_mcp_entrypoint_ignores_workspace_import_hijack(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "hijacked"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    (workspace / "sitecustomize.py").write_text(payload, encoding="utf-8")
    fake_package = workspace / "cheby_connector"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    (fake_package / "local_mcp.py").write_text(payload, encoding="utf-8")

    entrypoint = (
        Path(__file__).parents[1] / "cheby_connector" / "local_mcp.py"
    ).resolve()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(workspace)
    completed = subprocess.run(
        [sys.executable, "-I", str(entrypoint), "memory"],
        cwd=workspace,
        env=environment,
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )

    response = json.loads(completed.stdout)
    assert response["result"]["serverInfo"]["name"] == "chebycodex-offline-memory"
    assert not marker.exists()


def test_managed_mcp_config_preserves_base_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_text(
        'model = "gpt-5.6-sol"\n'
        'model_reasoning_effort = "xhigh"\n'
        '[permissions.personal.network]\n'
        'enabled = false\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    snippet = tmp_path / "snippet.toml"
    snippet.write_text(SNIPPET, encoding="utf-8")

    output = ensure_codex_mcp_config(home, snippet)
    first = output.read_bytes()
    parsed = tomllib.loads(first.decode())
    assert parsed["model_reasoning_effort"] == "xhigh"
    assert set(parsed["mcp_servers"]) == SERVER_NAMES
    assert set(parsed["permissions"]) == {PERMISSION_PROFILE_NAME, "personal"}
    assert parsed["permissions"]["personal"]["network"] == {"enabled": False}
    assert first.decode().count(MANAGED_BEGIN) == 1
    assert first.decode().count(MANAGED_END) == 1
    assert os.stat(output).st_mode & 0o777 == 0o600

    ensure_codex_mcp_config(home, snippet)
    assert output.read_bytes() == first


def test_managed_mcp_config_rejects_unmanaged_name_conflict(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_text('[mcp_servers.phonebridge]\ncommand = "other"\n', encoding="utf-8")
    config.chmod(0o600)
    snippet = tmp_path / "snippet.toml"
    snippet.write_text(SNIPPET, encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmanaged"):
        ensure_codex_mcp_config(home, snippet)


def test_managed_mcp_config_rejects_permission_profile_conflict(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_text(
        '[permissions.cheby_mobile.network]\nenabled = false\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    snippet = tmp_path / "snippet.toml"
    snippet.write_text(SNIPPET, encoding="utf-8")
    with pytest.raises(RuntimeError, match="permission profile"):
        ensure_codex_mcp_config(home, snippet)


def test_managed_mcp_config_rejects_malformed_marker(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_text(f"{MANAGED_BEGIN}\n", encoding="utf-8")
    config.chmod(0o600)
    snippet = tmp_path / "snippet.toml"
    snippet.write_text(SNIPPET, encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed"):
        ensure_codex_mcp_config(home, snippet)


def test_real_codex_registration_gate_requires_all_servers(tmp_path: Path) -> None:
    complete = [{"name": name, "enabled": True} for name in SERVER_NAMES]
    with patch("cheby_connector.mcp_config.subprocess.run", return_value=Mock(stdout=__import__("json").dumps(complete))):
        validate_codex_mcp_registration(tmp_path)

    incomplete = [{"name": "phonebridge", "enabled": True}]
    with patch("cheby_connector.mcp_config.subprocess.run", return_value=Mock(stdout=__import__("json").dumps(incomplete))):
        with pytest.raises(RuntimeError, match="every required"):
            validate_codex_mcp_registration(tmp_path)
