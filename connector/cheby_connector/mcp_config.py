from __future__ import annotations

import os
import json
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Optional


MANAGED_BEGIN = "# BEGIN CHEBYCODEX MANAGED LOCAL MCP"
MANAGED_END = "# END CHEBYCODEX MANAGED LOCAL MCP"
SNIPPET_PATH = Path("/app/deploy/codex-mcp.connector.toml")
SERVER_NAMES = {"phonebridge", "offline_memory", "offline_skill"}
PERMISSION_PROFILE_NAME = "cheby_mobile"
MAX_CONFIG_BYTES = 1024 * 1024


def ensure_codex_mcp_config(
    codex_home: Optional[Path] = None,
    snippet_path: Path = SNIPPET_PATH,
) -> Path:
    home = codex_home or Path(os.environ.get("CODEX_HOME", "/home/cheby/.codex"))
    if not home.is_absolute() or home.is_symlink():
        raise RuntimeError("CODEX_HOME must be an absolute non-symlink directory")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home_metadata = home.lstat()
    if not stat.S_ISDIR(home_metadata.st_mode) or home_metadata.st_mode & 0o077:
        raise RuntimeError("CODEX_HOME permissions are unsafe")
    config = home / "config.toml"
    existing = _read_config(config)
    snippet = snippet_path.read_text(encoding="utf-8")
    snippet_value = tomllib.loads(snippet)
    snippet_servers = set(snippet_value.get("mcp_servers", {}))
    if snippet_servers != SERVER_NAMES:
        raise RuntimeError("reviewed MCP snippet has an unexpected server set")
    snippet_profiles = set(snippet_value.get("permissions", {}))
    if snippet_profiles != {PERMISSION_PROFILE_NAME}:
        raise RuntimeError("reviewed MCP snippet has an unexpected permission profile set")

    unmanaged = existing
    begin = existing.find(MANAGED_BEGIN)
    end = existing.find(MANAGED_END)
    if (begin < 0) != (end < 0) or (begin >= 0 and end < begin):
        raise RuntimeError("Codex config has a malformed managed MCP block")
    if begin >= 0:
        end += len(MANAGED_END)
        unmanaged = (existing[:begin].rstrip() + "\n" + existing[end:].lstrip()).strip()

    unmanaged_value = tomllib.loads(unmanaged) if unmanaged else {}
    conflicts = SERVER_NAMES.intersection(unmanaged_value.get("mcp_servers", {}))
    if conflicts:
        raise RuntimeError("Codex config already has unmanaged local MCP server names")
    if PERMISSION_PROFILE_NAME in unmanaged_value.get("permissions", {}):
        raise RuntimeError("Codex config already has the managed permission profile name")

    managed = f"{MANAGED_BEGIN}\n{snippet.rstrip()}\n{MANAGED_END}"
    rendered = f"{unmanaged.rstrip()}\n\n{managed}\n" if unmanaged.strip() else f"{managed}\n"
    value = tomllib.loads(rendered)
    if set(value.get("mcp_servers", {})).intersection(SERVER_NAMES) != SERVER_NAMES:
        raise RuntimeError("rendered Codex MCP config failed validation")
    rendered_profiles = value.get("permissions", {})
    if (
        not isinstance(rendered_profiles, dict)
        or rendered_profiles.get(PERMISSION_PROFILE_NAME)
        != snippet_value["permissions"][PERMISSION_PROFILE_NAME]
    ):
        raise RuntimeError("rendered Codex permission profile failed validation")
    if rendered == existing:
        return config
    _atomic_write(config, rendered.encode("utf-8"))
    return config


def validate_codex_mcp_registration(codex_home: Optional[Path] = None) -> None:
    home = codex_home or Path(os.environ.get("CODEX_HOME", "/home/cheby/.codex"))
    binary = os.environ.get("CHEBY_CODEX_BINARY", "codex")
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    try:
        result = subprocess.run(
            [binary, "mcp", "list", "--json"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            env=environment,
        )
        servers = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex rejected the managed MCP configuration") from error
    enabled = {
        item.get("name")
        for item in servers
        if isinstance(item, dict) and item.get("enabled") is True
    }
    if not SERVER_NAMES.issubset(enabled):
        raise RuntimeError("Codex did not enable every required local MCP server")


def _read_config(path: Path) -> str:
    if not path.exists():
        return ""
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_size > MAX_CONFIG_BYTES
    ):
        raise RuntimeError("Codex config permissions or size are unsafe")
    return path.read_text(encoding="utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_CONFIG_BYTES:
        raise RuntimeError("Codex config exceeds the size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.toml.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
