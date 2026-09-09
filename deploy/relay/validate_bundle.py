#!/usr/bin/env python3
"""Static, side-effect-free checks for the public Relay deployment bundle."""

from __future__ import annotations

import json
import re
from pathlib import Path


BUNDLE = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"relay deployment bundle: FAIL: {message}")


def logical_requirements(path: Path) -> list[str]:
    result: list[str] = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        current += (" " if current else "") + line.removesuffix("\\").strip()
        if not continued:
            result.append(current)
            current = ""
    require(not current, "requirements.lock has an incomplete continuation")
    return result


def docker_instructions(dockerfile: str) -> list[str]:
    require(
        re.search(
            r"^[ \t]*#[ \t]*escape[ \t]*=",
            dockerfile,
            re.IGNORECASE | re.MULTILINE,
        )
        is None,
        "custom Docker escape parser directives are forbidden",
    )
    instructions: list[str] = []
    parts: list[str] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not parts and (not line or line.startswith("#")):
            continue
        continued = line.endswith("\\")
        parts.append(line[:-1].rstrip() if continued else line)
        if not continued:
            instructions.append(" ".join(parts))
            parts = []
    require(not parts, "Dockerfile has an incomplete continuation")
    return instructions


def validate_runtime_command(dockerfile: str) -> None:
    directives = [
        instruction
        for instruction in docker_instructions(dockerfile)
        if re.match(r"^CMD(?:\s|$)", instruction, re.IGNORECASE)
    ]
    require(len(directives) == 1, "Dockerfile must contain exactly one CMD instruction")
    match = re.fullmatch(
        r"[ \t]*CMD[ \t]+(\[.*\])[ \t]*",
        directives[0],
        re.IGNORECASE,
    )
    require(match is not None, "Dockerfile CMD must use single-line JSON form")
    try:
        command = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "relay deployment bundle: FAIL: Dockerfile CMD must be valid JSON"
        ) from exc
    require(
        isinstance(command, list) and all(isinstance(value, str) for value in command),
        "Dockerfile CMD must be a JSON string array",
    )
    require(
        command.count("--ws-max-size") == 1,
        "Relay runtime must set --ws-max-size exactly once",
    )
    limit_index = command.index("--ws-max-size") + 1
    require(
        limit_index < len(command) and command[limit_index] == "12582913",
        "Relay transport message ceiling must be exactly one byte above the 12 MiB business limit",
    )


def main() -> None:
    import yaml

    dockerfile = (BUNDLE / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(
        (BUNDLE / "docker-compose.yml").read_text(encoding="utf-8")
    )
    requirements = logical_requirements(BUNDLE / "requirements.lock")

    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    require(from_lines, "Dockerfile has no base image")
    require(
        all("@sha256:" in image for image in from_lines),
        "every base image must be digest-pinned",
    )
    require("--require-hashes" in dockerfile, "pip must enforce dependency hashes")
    require("--workers\", \"1" in dockerfile, "Relay must run exactly one worker")
    validate_runtime_command(dockerfile)
    require("--no-access-log" in dockerfile, "access logging must remain disabled")
    require("--log-level\", \"critical" in dockerfile,
            "Relay must suppress stack-bearing server error logs")
    require("--no-proxy-headers" in dockerfile, "forwarded headers must not be trusted")
    require("--reload" not in dockerfile, "development reload is forbidden")
    require("debug=True" not in dockerfile, "debug mode is forbidden")

    packages = set()
    for requirement in requirements:
        require("==" in requirement, "every dependency must be exactly version-pinned")
        require(
            re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", requirement) is not None,
            "every dependency must include a SHA-256 hash",
        )
        require("http://" not in requirement and "https://" not in requirement,
                "direct dependency URLs are forbidden")
        packages.add(requirement.split("==", 1)[0].lower())
    require(
        {"cryptography", "fastapi", "pydantic", "uvicorn", "websockets"}.issubset(packages),
        "the production dependency set is incomplete",
    )

    services = compose.get("services", {})
    require(set(services) == {"relay", "relay-admin"}, "unexpected Compose service")
    relay = services["relay"]
    require("build" not in relay, "runtime Compose must consume a reviewed image")
    require(relay.get("pull_policy") == "never", "runtime must not pull a mutable image")
    require(relay.get("platform") == "linux/amd64", "dependency lock requires linux/amd64")
    require(relay.get("user") == "10001:10001", "Relay must run as the fixed non-root UID")
    require(relay.get("read_only") is True, "Relay root filesystem must be read-only")
    require(relay.get("cap_drop") == ["ALL"], "Relay must drop every capability")
    require(
        "no-new-privileges:true" in relay.get("security_opt", []),
        "Relay must set no-new-privileges",
    )
    require(
        all(str(port).startswith("127.0.0.1:") for port in relay.get("ports", [])),
        "Relay may bind only to host loopback behind TLS ingress",
    )
    require(
        relay.get("environment", {}).get("CHEBY_RELAY_DB_PATH") == "/data/relay.sqlite3",
        "Relay database must use the persistent data mount",
    )
    relay_environment = relay.get("environment", {})
    require(
        "CHEBY_RELAY_TRUSTED_PROXY_CIDRS" in relay_environment,
        "Relay must explicitly configure its immediate trusted proxy boundary",
    )
    require(
        relay_environment.get("CHEBY_RELAY_CLIENT_IP_HEADER")
        == "${CHEBY_RELAY_CLIENT_IP_HEADER:-x-relay-client-ip}",
        "Relay must use the dedicated sanitized client identity header",
    )
    env_text = "\n".join(
        f"{key}={value}" for key, value in relay.get("environment", {}).items()
    ).lower()
    require(
        not any(word in env_text for word in ("token=", "secret=", "password=")),
        "runtime secrets must not be placed in Compose environment values",
    )

    admin = services["relay-admin"]
    require(admin.get("profiles") == ["admin"], "admin service must be opt-in")
    require(admin.get("network_mode") == "none", "admin service must have no network")
    require(not admin.get("ports"), "admin service must not publish ports")
    require(admin.get("read_only") is True, "admin root filesystem must be read-only")

    ignore = (BUNDLE / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    for expected in (
        "!relay/cheby_relay/**",
        "!deploy/relay/Dockerfile",
        "!deploy/relay/requirements.lock",
    ):
        require(expected in ignore, f"Docker context allowlist is missing {expected}")

    print("relay deployment bundle: PASS")


if __name__ == "__main__":
    main()
