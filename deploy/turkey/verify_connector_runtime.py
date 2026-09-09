#!/usr/bin/env python3
"""Fail-closed Docker runtime postflight for production and Gate Connector."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
from typing import Mapping, Sequence


PRODUCTION_OUTBOUND_NETWORK = "chebycodex-turkey-connector_outbound"
PRODUCTION_BACKEND_NETWORK = "chebycodex-relay-edge_relay-backend"
GATE_BACKEND_NETWORK = "chebycodex-relay-edge_gate-backend"
PRODUCTION_RELAY_URL = "ws://172.31.61.3:8080/relay/v1/node"
GATE_RELAY_URL = "ws://172.31.62.3:8080/relay/v1/node"
REVIEWED_CODEX_COMMAND = (
    "python /app/deploy/codex_sandbox_launcher.py codex --ask-for-approval never "
    "app-server --disable code_mode_host --disable code_mode --listen stdio://"
)
CODEX_SECCOMP_OPTION = (
    "seccomp=/etc/chebycodex-connector/codex-bwrap-seccomp.json"
)
CODEX_SECCOMP_PATH = Path(
    "/etc/chebycodex-connector/codex-bwrap-seccomp.json"
)
CODEX_SECCOMP_SHA256 = (
    "0d2734a72fb880d1779003a5c00f293a48524e6eb4089c195eaad78aaf809a55"
)
CODEX_PERMISSION_PROFILE = "cheby_mobile"
CODEX_OUTER_BWRAP_COMMAND = (
    "/usr/bin/bwrap",
    "--die-with-parent",
    "--new-session",
    "--ro-bind",
    "/",
    "/",
    "--dev-bind",
    "/dev",
    "/dev",
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
    "--chdir",
    "/workspace",
    "--",
)


class ConnectorRuntimeError(RuntimeError):
    """A secret-free Connector runtime validation failure."""


def _run(command: Sequence[str], *, timeout_seconds: float = 30.0) -> str:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConnectorRuntimeError("Connector runtime command failed") from exc


def _container_id(
    compose_file: Path,
    service: str,
    env_files: Sequence[Path],
) -> str:
    command = ["docker", "compose"]
    for env_file in env_files:
        command.extend(("--env-file", str(env_file)))
    command.extend(("--file", str(compose_file), "ps", "--quiet", service))
    values = [line.strip() for line in _run(command).splitlines() if line.strip()]
    if len(values) != 1:
        raise ConnectorRuntimeError(
            f"{service} must resolve to one running container"
        )
    return values[0]


def _inspect_container(container_id: str) -> Mapping[str, object]:
    return _inspect_document(
        ("docker", "inspect", container_id),
        "Docker container inspection",
    )


def _inspect_network(name: str) -> Mapping[str, object]:
    return _inspect_document(
        ("docker", "network", "inspect", name),
        "Docker network inspection",
    )


def _inspect_document(
    command: Sequence[str],
    label: str,
) -> Mapping[str, object]:
    try:
        document = json.loads(_run(command))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConnectorRuntimeError(f"invalid {label}") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise ConnectorRuntimeError(f"{label} is not singular")
    value = document[0]
    if not isinstance(value, dict):
        raise ConnectorRuntimeError(f"{label} is malformed")
    return value


def _network_addresses(document: Mapping[str, object]) -> dict[str, str]:
    settings = document.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, dict) else None
    if not isinstance(networks, dict):
        raise ConnectorRuntimeError("Connector network inspection is missing")
    result: dict[str, str] = {}
    for name, endpoint in networks.items():
        address = endpoint.get("IPAddress") if isinstance(endpoint, dict) else None
        if not isinstance(name, str) or not isinstance(address, str) or not address:
            raise ConnectorRuntimeError("Connector network inspection is malformed")
        result[name] = address
    return result


def verify_network_membership(
    document: Mapping[str, object],
    expected: Mapping[str, str],
) -> None:
    if _network_addresses(document) != dict(expected):
        raise ConnectorRuntimeError(
            "Connector network membership differs from the reviewed profile"
        )


def verify_network_contract(
    document: Mapping[str, object],
    *,
    name: str,
    subnet: str,
    internal: bool,
    only_container: tuple[str, str] | None = None,
    exact_container_addresses: frozenset[str] | None = None,
) -> None:
    ipam = document.get("IPAM")
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    actual_subnets = {
        value.get("Subnet")
        for value in configs
        if isinstance(value, dict) and isinstance(value.get("Subnet"), str)
    } if isinstance(configs, list) else set()
    if (
        document.get("Name") != name
        or document.get("Driver") != "bridge"
        or document.get("Internal") is not internal
        or document.get("EnableIPv6") is not False
        or actual_subnets != {subnet}
    ):
        raise ConnectorRuntimeError(
            f"network {name} differs from the reviewed contract"
        )
    if only_container is None:
        if exact_container_addresses is None:
            return
    containers = document.get("Containers")
    if not isinstance(containers, dict):
        raise ConnectorRuntimeError(
            f"network {name} container membership is malformed"
        )
    if exact_container_addresses is not None:
        addresses: set[str] = set()
        for endpoint in containers.values():
            address = (
                endpoint.get("IPv4Address")
                if isinstance(endpoint, dict)
                else None
            )
            try:
                parsed = str(ipaddress.ip_interface(address).ip)
            except (TypeError, ValueError) as exc:
                raise ConnectorRuntimeError(
                    f"network {name} has an invalid container address"
                ) from exc
            if parsed in addresses:
                raise ConnectorRuntimeError(
                    f"network {name} contains a duplicate container address"
                )
            addresses.add(parsed)
        if addresses != set(exact_container_addresses):
            raise ConnectorRuntimeError(
                f"network {name} container membership differs from the contract"
            )
    if only_container is None:
        return
    container_id, expected_address = only_container
    if set(containers) != {container_id}:
        raise ConnectorRuntimeError(
            f"network {name} must contain only production Connector"
        )
    endpoint = containers[container_id]
    address = endpoint.get("IPv4Address") if isinstance(endpoint, dict) else None
    try:
        parsed = str(ipaddress.ip_interface(address).ip)
    except (TypeError, ValueError) as exc:
        raise ConnectorRuntimeError(
            f"network {name} has an invalid Connector address"
        ) from exc
    if parsed != expected_address:
        raise ConnectorRuntimeError(
            f"network {name} has the wrong Connector address"
        )


def _environment(document: Mapping[str, object]) -> list[str]:
    config = document.get("Config")
    values = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ConnectorRuntimeError("Connector environment inspection is malformed")
    return values


def _environment_value(document: Mapping[str, object], name: str) -> str:
    prefix = f"{name}="
    values = [
        value.removeprefix(prefix)
        for value in _environment(document)
        if value.startswith(prefix)
    ]
    if len(values) != 1:
        raise ConnectorRuntimeError(
            f"Connector {name} must occur exactly once"
        )
    return values[0]


def _environment_values(document: Mapping[str, object], name: str) -> list[str]:
    prefix = f"{name}="
    return [
        value.removeprefix(prefix)
        for value in _environment(document)
        if value.startswith(prefix)
    ]


def verify_runtime_profile(
    document: Mapping[str, object],
    *,
    profile: str,
    relay_url: str,
) -> None:
    if profile not in {"production", "gate"}:
        raise ConnectorRuntimeError("Connector runtime profile is unsupported")
    codex_commands = _environment_values(document, "CHEBY_CODEX_COMMAND")
    codex_mode_matches = (
        codex_commands == [REVIEWED_CODEX_COMMAND]
        if profile == "production"
        else not codex_commands
    )
    if (
        _environment_value(document, "CHEBY_CONNECTOR_RUNTIME_PROFILE") != profile
        or _environment_value(document, "CHEBY_CONNECTOR_RELAY_URL") != relay_url
        or not codex_mode_matches
    ):
        raise ConnectorRuntimeError(
            "Connector runtime profile, private Relay URL, or Codex tool mode drifted"
        )


def verify_codex_sandbox_host_contract(
    document: Mapping[str, object],
    *,
    expected_seccomp_profile: Mapping[str, object] | None = None,
) -> None:
    """Prove the outer container permits Codex's inner bubblewrap sandbox."""
    host = document.get("HostConfig")
    if not isinstance(host, dict):
        raise ConnectorRuntimeError("Connector HostConfig inspection is missing")
    security_options = host.get("SecurityOpt")
    if not isinstance(security_options, list) or not all(
        isinstance(value, str) for value in security_options
    ):
        raise ConnectorRuntimeError(
            "Connector security options cannot support the reviewed Codex sandbox"
        )
    seccomp_options = [
        value.removeprefix("seccomp=")
        for value in security_options
        if value.startswith("seccomp=")
    ]
    if (
        security_options.count("no-new-privileges:true") != 1
        or len(seccomp_options) != 1
        or len(security_options) != 2
    ):
        raise ConnectorRuntimeError(
            "Connector security options cannot support the reviewed Codex sandbox"
        )
    if expected_seccomp_profile is None:
        if seccomp_options[0] != CODEX_SECCOMP_PATH.as_posix():
            raise ConnectorRuntimeError("Connector seccomp option drifted")
    else:
        try:
            runtime_profile = json.loads(seccomp_options[0])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ConnectorRuntimeError("Connector runtime seccomp is malformed") from exc
        if runtime_profile != dict(expected_seccomp_profile):
            raise ConnectorRuntimeError("Connector runtime seccomp profile drifted")
    if host.get("CapDrop") != ["ALL"] or host.get("ReadonlyRootfs") is not True:
        raise ConnectorRuntimeError(
            "Connector capability or read-only-root contract drifted"
        )
    config = document.get("Config")
    if not isinstance(config, dict) or config.get("User") != "10002:10002":
        raise ConnectorRuntimeError("Connector must run as the reviewed non-root user")


def verify_codex_seccomp_profile(
    path: Path,
    *,
    expected_sha256: str = CODEX_SECCOMP_SHA256,
) -> Mapping[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ConnectorRuntimeError("Codex seccomp profile is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ConnectorRuntimeError("Codex seccomp profile hash drifted")
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConnectorRuntimeError("Codex seccomp profile is malformed") from exc
    if not isinstance(document, dict):
        raise ConnectorRuntimeError("Codex seccomp profile is malformed")
    return document


def verify_codex_bubblewrap_runtime(container_id: str) -> None:
    """Execute a minimum live bubblewrap capability probe with an inner timeout."""
    _run(
        (
            "docker",
            "exec",
            container_id,
            "timeout",
            "-s",
            "KILL",
            "10",
            "bwrap",
            "--unshare-user",
            "--uid",
            "10002",
            "--gid",
            "10002",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "true",
        ),
        timeout_seconds=15.0,
    )


def verify_codex_permission_profile_runtime(container_id: str) -> None:
    """Prove native tools are useful but cannot read or signal service state."""
    target = _run(
        (
            "docker",
            "exec",
            container_id,
            "/bin/sh",
            "-c",
            "/bin/sleep 30 >/dev/null 2>&1 & echo $!",
        ),
        timeout_seconds=5.0,
    ).strip()
    if not target.isdigit() or int(target) <= 1:
        raise ConnectorRuntimeError("signal-isolation target did not start")
    script = (
        "set -eu; "
        "test ! -r /home/cheby/.codex/auth.json; "
        "test ! -r /home/cheby/.codex/config.toml; "
        "test ! -r /run/secrets/node_token; "
        "test ! -r /data/connector.sqlite3; "
        f"test ! -r /proc/{target}/status; "
        f"if kill -0 {target} 2>/dev/null; then exit 70; fi; "
        "probe=/workspace/.cheby-permission-runtime-probe; "
        "trap 'rm -f \"$probe\"' EXIT; "
        "printf ok > \"$probe\"; "
        "test \"$(cat \"$probe\")\" = ok; "
        "/usr/local/bin/python -c 'import socket; "
        "s=socket.create_connection((\"172.31.61.3\",8080),5); s.close()'"
    )
    try:
        _run(
            (
                "docker",
                "exec",
                container_id,
                "timeout",
                "-s",
                "KILL",
                "15",
                *CODEX_OUTER_BWRAP_COMMAND,
                "codex",
                "sandbox",
                "-P",
                CODEX_PERMISSION_PROFILE,
                "-C",
                "/workspace",
                "/bin/sh",
                "-c",
                script,
            ),
            timeout_seconds=20.0,
        )
        _run(
            ("docker", "exec", container_id, "kill", "-0", target),
            timeout_seconds=5.0,
        )
    finally:
        subprocess.run(
            ["docker", "exec", container_id, "kill", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )


def verify_phonebridge_binding(document: Mapping[str, object]) -> None:
    host_config = document.get("HostConfig")
    port_bindings = (
        host_config.get("PortBindings")
        if isinstance(host_config, dict)
        else None
    )
    if not isinstance(port_bindings, dict) or set(port_bindings) != {"3438/tcp"}:
        raise ConnectorRuntimeError("PhoneBridge HostConfig mapping drifted")
    requested = port_bindings["3438/tcp"]
    if (
        not isinstance(requested, list)
        or len(requested) != 1
        or not isinstance(requested[0], dict)
        or requested[0].get("HostPort") != "3448"
        or requested[0].get("HostIp") != "0.0.0.0"
    ):
        raise ConnectorRuntimeError(
            "PhoneBridge requested mapping is not exact IPv4 3448:3438"
        )

    settings = document.get("NetworkSettings")
    runtime_ports = settings.get("Ports") if isinstance(settings, dict) else None
    if not isinstance(runtime_ports, dict) or set(runtime_ports) != {"3438/tcp"}:
        raise ConnectorRuntimeError("PhoneBridge runtime mapping drifted")
    active = runtime_ports["3438/tcp"]
    if active != [{"HostIp": "0.0.0.0", "HostPort": "3448"}]:
        raise ConnectorRuntimeError(
            "PhoneBridge active mapping is not the exact IPv4 3448:3438 contract"
        )


def verify_no_ports(document: Mapping[str, object]) -> None:
    host_config = document.get("HostConfig")
    port_bindings = (
        host_config.get("PortBindings")
        if isinstance(host_config, dict)
        else None
    )
    settings = document.get("NetworkSettings")
    runtime_ports = settings.get("Ports") if isinstance(settings, dict) else None
    if port_bindings not in ({}, None) or runtime_ports not in ({}, None):
        raise ConnectorRuntimeError("Gate Connector must publish and expose no port")


def _container_pid(document: Mapping[str, object]) -> int:
    state = document.get("State")
    value = state.get("Pid") if isinstance(state, dict) else None
    if not isinstance(value, int) or value <= 0:
        raise ConnectorRuntimeError("Connector network namespace is unavailable")
    return value


def _option(tokens: Sequence[str], name: str) -> str | None:
    try:
        index = tokens.index(name)
    except ValueError:
        return None
    return tokens[index + 1] if index + 1 < len(tokens) else None


def verify_routes(
    default_routes: str,
    relay_route: str,
    *,
    profile: str,
) -> None:
    relay_lines = [
        shlex.split(line) for line in relay_route.splitlines() if line.strip()
    ]
    # Current iproute2 prints an indented cache marker after the selected
    # route. Accept only that exact continuation; any other extra output stays
    # fail-closed.
    if len(relay_lines) == 2 and relay_lines[1] == ["cache"]:
        relay_lines.pop()
    expected_source = "172.31.61.4" if profile == "production" else "172.31.62.4"
    if len(relay_lines) != 1 or _option(relay_lines[0], "src") != expected_source:
        raise ConnectorRuntimeError(
            "Connector-to-Relay route does not use its backend identity"
        )

    defaults = [
        shlex.split(line) for line in default_routes.splitlines() if line.strip()
    ]
    if profile == "production":
        if (
            len(defaults) != 1
            or not defaults[0]
            or defaults[0][0] != "default"
            or _option(defaults[0], "via") != "172.30.0.1"
        ):
            raise ConnectorRuntimeError(
                "production Connector default route is not outbound"
            )
    elif any(_option(tokens, "via") != "172.31.62.1" for tokens in defaults):
        raise ConnectorRuntimeError(
            "Gate Connector has a default route outside its internal backend"
        )


def verify_namespace_routes(
    document: Mapping[str, object],
    *,
    profile: str,
) -> None:
    pid = str(_container_pid(document))
    relay_address = "172.31.61.3" if profile == "production" else "172.31.62.3"
    defaults = _run(
        (
            "nsenter",
            "--target",
            pid,
            "--net",
            "--",
            "ip",
            "-4",
            "route",
            "show",
            "default",
        )
    )
    relay_route = _run(
        (
            "nsenter",
            "--target",
            pid,
            "--net",
            "--",
            "ip",
            "-4",
            "route",
            "get",
            relay_address,
        )
    )
    verify_routes(defaults, relay_route, profile=profile)


def _default_compose(profile: str) -> Path:
    name = (
        "docker-compose.connector.yml"
        if profile == "production"
        else "docker-compose.connector-gate.yml"
    )
    return Path(__file__).resolve().parent / name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("production", "gate"),
        required=True,
    )
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--env-file", action="append", type=Path, default=[])
    parser.add_argument(
        "--seccomp-profile",
        type=Path,
        default=CODEX_SECCOMP_PATH,
    )
    args = parser.parse_args(argv)

    compose_file = (args.compose_file or _default_compose(args.profile)).resolve(
        strict=True
    )
    env_files = [path.resolve(strict=True) for path in args.env_file]
    service = "connector" if args.profile == "production" else "gate-connector"
    container_id = _container_id(compose_file, service, env_files)
    container = _inspect_container(container_id)

    if args.profile == "production":
        seccomp_profile = verify_codex_seccomp_profile(args.seccomp_profile)
        verify_network_membership(
            container,
            {
                PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
                PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
            },
        )
        verify_network_contract(
            _inspect_network(PRODUCTION_OUTBOUND_NETWORK),
            name=PRODUCTION_OUTBOUND_NETWORK,
            subnet="172.30.0.0/28",
            internal=False,
            only_container=(container_id, "172.30.0.2"),
        )
        verify_network_contract(
            _inspect_network(PRODUCTION_BACKEND_NETWORK),
            name=PRODUCTION_BACKEND_NETWORK,
            subnet="172.31.61.0/28",
            internal=True,
            exact_container_addresses=frozenset(
                {"172.31.61.2", "172.31.61.3", "172.31.61.4"}
            ),
        )
        verify_runtime_profile(
            container,
            profile="production",
            relay_url=PRODUCTION_RELAY_URL,
        )
        verify_codex_sandbox_host_contract(
            container,
            expected_seccomp_profile=seccomp_profile,
        )
        verify_codex_bubblewrap_runtime(container_id)
        verify_codex_permission_profile_runtime(container_id)
        verify_phonebridge_binding(container)
    else:
        verify_network_membership(
            container,
            {GATE_BACKEND_NETWORK: "172.31.62.4"},
        )
        verify_network_contract(
            _inspect_network(GATE_BACKEND_NETWORK),
            name=GATE_BACKEND_NETWORK,
            subnet="172.31.62.0/28",
            internal=True,
            exact_container_addresses=frozenset(
                {"172.31.62.2", "172.31.62.3", "172.31.62.4"}
            ),
        )
        verify_runtime_profile(
            container,
            profile="gate",
            relay_url=GATE_RELAY_URL,
        )
        verify_no_ports(container)

    verify_namespace_routes(container, profile=args.profile)
    print(f"{args.profile} Connector runtime: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ConnectorRuntimeError) as exc:
        raise SystemExit(f"Connector runtime: FAIL: {exc}") from None
