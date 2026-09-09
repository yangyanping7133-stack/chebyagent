#!/usr/bin/env python3
"""Fail-closed postflight for the Relay Edge Docker network contract."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Mapping, Sequence
import urllib.error
import urllib.request


PRODUCTION_INGRESS_NETWORK = "chebycodex-relay-edge_edge-ingress"
PRODUCTION_BACKEND_NETWORK = "chebycodex-relay-edge_relay-backend"
PRODUCTION_LOOPBACK_NETWORK = "chebycodex-relay-edge_relay-loopback-publish"
GATE_INGRESS_NETWORK = "chebycodex-relay-edge_gate-edge-ingress"
GATE_BACKEND_NETWORK = "chebycodex-relay-edge_gate-backend"
GATE_LOOPBACK_NETWORK = "chebycodex-relay-edge_gate-relay-loopback-publish"
NO_MASQUERADE = {"com.docker.network.bridge.enable_ip_masquerade": "false"}
DOCKER_BRIDGE = re.compile(r"br-[0-9a-f]{12,64}\Z")
PROTECTED_HOST_PORTS = frozenset({18080, 18081, 27461, 27462})


class RuntimeNetworkError(RuntimeError):
    """A secret-free runtime network validation failure."""


def _run(command: Sequence[str]) -> str:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeNetworkError("runtime network command failed") from exc


def _container_id(
    compose_file: Path,
    service: str,
    *,
    include_gate: bool,
) -> str:
    command = ["docker", "compose", "--file", str(compose_file)]
    if include_gate:
        command.extend(("--profile", "gate"))
    command.extend(("ps", "--quiet", service))
    values = [line.strip() for line in _run(command).splitlines() if line.strip()]
    if len(values) != 1:
        raise RuntimeNetworkError(f"{service} must resolve to one running container")
    return values[0]


def _inspect_container(container_id: str) -> Mapping[str, object]:
    try:
        document = json.loads(_run(("docker", "inspect", container_id)))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeNetworkError("invalid Docker container inspection") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise RuntimeNetworkError("Docker container inspection is not singular")
    value = document[0]
    if not isinstance(value, dict):
        raise RuntimeNetworkError("Docker container inspection is malformed")
    return value


def _inspect_network(name: str) -> Mapping[str, object]:
    try:
        document = json.loads(_run(("docker", "network", "inspect", name)))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeNetworkError("invalid Docker network inspection") from exc
    if not isinstance(document, list) or len(document) != 1:
        raise RuntimeNetworkError("Docker network inspection is not singular")
    value = document[0]
    if not isinstance(value, dict):
        raise RuntimeNetworkError("Docker network inspection is malformed")
    return value


def verify_network(
    document: Mapping[str, object],
    *,
    name: str,
    subnet: str,
    internal: bool,
    only_container: tuple[str, str] | None = None,
    options: Mapping[str, str] | None = None,
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
        raise RuntimeNetworkError(f"network {name} differs from the reviewed contract")
    actual_options = document.get("Options")
    if options is not None and (
        not isinstance(actual_options, dict)
        or any(actual_options.get(key) != value for key, value in options.items())
    ):
        raise RuntimeNetworkError(f"network {name} differs from the reviewed contract")
    if only_container is not None:
        container_id, address = only_container
        containers = document.get("Containers")
        if not isinstance(containers, dict) or set(containers) != {container_id}:
            raise RuntimeNetworkError(
                f"network {name} must contain only its reviewed container"
            )
        endpoint = containers[container_id]
        endpoint_address = (
            endpoint.get("IPv4Address") if isinstance(endpoint, dict) else None
        )
        try:
            parsed_address = str(ipaddress.ip_interface(endpoint_address).ip)
        except (TypeError, ValueError) as exc:
            raise RuntimeNetworkError(
                f"network {name} has an invalid Edge address"
            ) from exc
        if parsed_address != address:
            raise RuntimeNetworkError(f"network {name} has the wrong Edge address")


def _network_addresses(
    document: Mapping[str, object],
) -> dict[str, str]:
    network_settings = document.get("NetworkSettings")
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings, dict)
        else None
    )
    if not isinstance(networks, dict):
        raise RuntimeNetworkError("container network inspection is missing")
    result: dict[str, str] = {}
    for name, value in networks.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise RuntimeNetworkError("container network inspection is malformed")
        address = value.get("IPAddress")
        if not isinstance(address, str) or not address:
            raise RuntimeNetworkError("container network address is missing")
        result[name] = address
    return result


def verify_container_networks(
    document: Mapping[str, object],
    expected: Mapping[str, str],
) -> None:
    if _network_addresses(document) != dict(expected):
        raise RuntimeNetworkError("container network membership differs from the contract")


def verify_port_binding(
    document: Mapping[str, object],
    *,
    bind_ip: str,
    host_port: int,
    container_port: int,
    label: str,
) -> None:
    expected = [{"HostIp": bind_ip, "HostPort": str(host_port)}]
    port_key = f"{container_port}/tcp"
    host_config = document.get("HostConfig")
    port_bindings = (
        host_config.get("PortBindings")
        if isinstance(host_config, dict)
        else None
    )
    network_settings = document.get("NetworkSettings")
    runtime_ports = (
        network_settings.get("Ports")
        if isinstance(network_settings, dict)
        else None
    )
    if not isinstance(port_bindings, dict) or port_bindings != {port_key: expected}:
        raise RuntimeNetworkError(
            f"{label} host port binding differs from the contract"
        )
    if not isinstance(runtime_ports, dict) or runtime_ports.get(port_key) != expected:
        raise RuntimeNetworkError(f"{label} runtime port publication is absent")
    if any(
        key != port_key and value
        for key, value in runtime_ports.items()
    ):
        raise RuntimeNetworkError(
            f"{label} has an unexpected published container port"
        )


def verify_edge_port_binding(
    document: Mapping[str, object],
    *,
    bind_ip: str,
    host_port: int,
) -> None:
    verify_port_binding(
        document,
        bind_ip=bind_ip,
        host_port=host_port,
        container_port=8443,
        label="Edge",
    )


def verify_relay_loopback_binding(
    document: Mapping[str, object],
    *,
    host_port: int,
) -> None:
    verify_port_binding(
        document,
        bind_ip="127.0.0.1",
        host_port=host_port,
        container_port=8080,
        label="Relay loopback",
    )


def verify_loopback_health(
    host_port: int,
    *,
    opener: object | None = None,
) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{host_port}/healthz",
        headers={"Host": "127.0.0.1"},
        method="GET",
    )
    proxy_free_opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )
    try:
        with proxy_free_opener.open(request, timeout=3) as response:
            status = response.status
            body = response.read(4097)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeNetworkError("Relay loopback health is unreachable") from exc
    if status != 200:
        raise RuntimeNetworkError("Relay loopback health returned a non-200 status")
    if len(body) > 4096:
        raise RuntimeNetworkError("Relay loopback health response is oversized")


def _container_pid(document: Mapping[str, object]) -> int:
    state = document.get("State")
    value = state.get("Pid") if isinstance(state, dict) else None
    if not isinstance(value, int) or value <= 0:
        raise RuntimeNetworkError("Edge network namespace is unavailable")
    return value


def verify_edge_routes(
    default_routes: str,
    relay_route: str,
    *,
    ingress_gateway: str,
    backend_source: str,
) -> None:
    defaults = [
        shlex.split(line)
        for line in default_routes.splitlines()
        if line.strip()
    ]
    if (
        len(defaults) != 1
        or not defaults[0]
        or defaults[0][0] != "default"
        or _option(defaults[0], "via") != ingress_gateway
    ):
        raise RuntimeNetworkError("Edge default route is not on its ingress bridge")
    route_lines = [
        shlex.split(line)
        for line in relay_route.splitlines()
        if line.strip()
    ]
    # `ip route get` emits its cache marker as an indented continuation on
    # current iproute2 releases. It carries no route identity, so accept that
    # one exact continuation while rejecting every other extra line.
    if len(route_lines) == 2 and route_lines[1] == ["cache"]:
        route_lines.pop()
    if len(route_lines) != 1 or _option(route_lines[0], "src") != backend_source:
        raise RuntimeNetworkError("Edge-to-Relay route does not use its backend identity")


def verify_edge_namespace_routes(
    document: Mapping[str, object],
    *,
    ingress_gateway: str,
    relay_address: str,
    backend_source: str,
) -> None:
    pid = str(_container_pid(document))
    default_routes = _run(
        ("nsenter", "--target", pid, "--net", "--", "ip", "-4", "route", "show", "default")
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
    verify_edge_routes(
        default_routes,
        relay_route,
        ingress_gateway=ingress_gateway,
        backend_source=backend_source,
    )


def _option(tokens: Sequence[str], name: str) -> str | None:
    try:
        index = tokens.index(name)
    except ValueError:
        return None
    return tokens[index + 1] if index + 1 < len(tokens) else None


def _parse_exact_dnat(
    line: str,
) -> tuple[tuple[str, int], tuple[str, int]] | None:
    tokens = shlex.split(line)
    if tokens[:2] != ["-A", "DOCKER"]:
        return None

    # iptables-save preserves the matches but does not promise a stable order;
    # Docker has emitted both `! -i ... -d ...` and `-d ... ! -i ...`.
    # Parse order-independently while retaining a strict, duplicate-free
    # allowlist so an extra source, comment or conntrack match cannot hide.
    values: dict[str, str] = {}
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "!":
            if (
                index + 2 >= len(tokens)
                or tokens[index + 1] != "-i"
                or "in_interface" in values
            ):
                return None
            values["in_interface"] = tokens[index + 2]
            index += 3
            continue
        fields = {
            "-p": "protocol",
            "-m": "module",
            "-d": "destination",
            "--dport": "destination_port",
            "-j": "jump",
            "--to-destination": "target",
        }
        field = fields.get(token)
        if field is None or field in values or index + 1 >= len(tokens):
            return None
        values[field] = tokens[index + 1]
        index += 2

    if (
        set(values)
        != {
            "in_interface",
            "protocol",
            "module",
            "destination",
            "destination_port",
            "jump",
            "target",
        }
        or DOCKER_BRIDGE.fullmatch(values["in_interface"]) is None
        or values["protocol"] != "tcp"
        or values["module"] != "tcp"
        or values["jump"] != "DNAT"
    ):
        return None
    destination = values["destination"]
    if not destination.endswith("/32"):
        return None
    try:
        bind_ip = str(ipaddress.IPv4Address(destination[:-3]))
        host_port = int(values["destination_port"])
        target_host, target_port_text = values["target"].rsplit(":", 1)
        target_ip = str(ipaddress.IPv4Address(target_host))
        target_port = int(target_port_text)
    except (ValueError, TypeError):
        return None
    if not (1 <= host_port <= 65535 and 1 <= target_port <= 65535):
        return None
    return (bind_ip, host_port), (target_ip, target_port)


def _loose_option_values(tokens: Sequence[str], *names: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token in names:
            values.append(tokens[index + 1])
    return values


def _rule_is_relevant(
    tokens: Sequence[str],
    *,
    expected: Mapping[tuple[str, int], tuple[str, int]],
) -> bool:
    if tokens[:2] != ["-A", "DOCKER"]:
        return False
    jumps = _loose_option_values(tokens, "-j", "--jump")
    if "DNAT" not in jumps:
        return False
    destinations = _loose_option_values(tokens, "-d", "--destination")
    port_values = _loose_option_values(
        tokens,
        "--dport",
        "--destination-port",
        "--dports",
    )
    targets = _loose_option_values(tokens, "--to-destination")
    protected_targets = {
        f"{address}:{port}" for address, port in expected.values()
    }
    if any(target in protected_targets for target in targets):
        return True
    if any(
        _port_spec_intersects(port_text, PROTECTED_HOST_PORTS)
        for port_text in port_values
    ):
        return True
    for destination in destinations:
        for port_text in port_values:
            try:
                destination_ip = str(
                    ipaddress.ip_network(destination, strict=False).network_address
                )
                port = int(port_text)
            except ValueError:
                return True
            if (destination_ip, port) in expected:
                return True
    return False


def _port_spec_intersects(
    value: str | None,
    protected: frozenset[int],
) -> bool:
    if value is None:
        return False
    for part in value.split(","):
        bounds = part.split(":", 1)
        try:
            start = int(bounds[0])
            end = int(bounds[-1])
        except ValueError:
            continue
        if start > end:
            start, end = end, start
        if any(start <= port <= end for port in protected):
            return True
    return False


def has_exact_dnat(
    iptables_save: str,
    *,
    bind_ip: str,
    host_port: int,
    ingress_ip: str,
    container_port: int = 8443,
) -> bool:
    expected = ((bind_ip, host_port), (ingress_ip, container_port))
    return any(
        _parse_exact_dnat(line) == expected
        for line in iptables_save.splitlines()
    )


def verify_exact_dnat_set(
    iptables_save: str,
    *,
    expected: Mapping[tuple[str, int], tuple[str, int]],
) -> None:
    relevant: list[tuple[tuple[str, int], tuple[str, int]]] = []
    for line in iptables_save.splitlines():
        tokens = shlex.split(line)
        if not _rule_is_relevant(tokens, expected=expected):
            continue
        parsed = _parse_exact_dnat(line)
        if parsed is None:
            raise RuntimeNetworkError(
                "Docker DNAT contains a restricted or drifted match"
            )
        relevant.append(parsed)
    if sorted(relevant, key=str) != sorted(expected.items(), key=str):
        raise RuntimeNetworkError(
            "Docker DNAT set differs from the exact publication contract"
        )


def _port(value: str, expected: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if parsed != expected:
        raise argparse.ArgumentTypeError(f"port must be exactly {expected}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path(__file__).resolve().parent / "docker-compose.yml",
    )
    parser.add_argument("--bind-ip", type=ipaddress.ip_address, required=True)
    parser.add_argument(
        "--production-port",
        type=lambda value: _port(value, 27461),
        default=27461,
    )
    parser.add_argument(
        "--gate-port",
        type=lambda value: _port(value, 27462),
        default=27462,
    )
    parser.add_argument(
        "--production-loopback-port",
        type=lambda value: _port(value, 18080),
        default=18080,
    )
    parser.add_argument(
        "--gate-loopback-port",
        type=lambda value: _port(value, 18081),
        default=18081,
    )
    parser.add_argument("--include-gate", action="store_true")
    args = parser.parse_args(argv)
    if args.bind_ip.version != 4:
        raise RuntimeNetworkError("bind address must be IPv4")
    compose_file = args.compose_file.resolve(strict=True)
    bind_ip = str(args.bind_ip)

    production_edge_id = _container_id(
        compose_file,
        "edge",
        include_gate=args.include_gate,
    )
    production_relay_id = _container_id(
        compose_file,
        "relay",
        include_gate=args.include_gate,
    )
    production_edge = _inspect_container(production_edge_id)
    production_relay = _inspect_container(production_relay_id)
    verify_container_networks(
        production_edge,
        {
            PRODUCTION_INGRESS_NETWORK: "172.31.60.2",
            PRODUCTION_BACKEND_NETWORK: "172.31.61.2",
        },
    )
    verify_container_networks(
        production_relay,
        {
            PRODUCTION_LOOPBACK_NETWORK: "172.31.64.2",
            PRODUCTION_BACKEND_NETWORK: "172.31.61.3",
        },
    )
    verify_edge_port_binding(
        production_edge,
        bind_ip=bind_ip,
        host_port=args.production_port,
    )
    verify_relay_loopback_binding(
        production_relay,
        host_port=args.production_loopback_port,
    )
    verify_network(
        _inspect_network(PRODUCTION_INGRESS_NETWORK),
        name=PRODUCTION_INGRESS_NETWORK,
        subnet="172.31.60.0/28",
        internal=False,
        only_container=(production_edge_id, "172.31.60.2"),
    )
    verify_network(
        _inspect_network(PRODUCTION_BACKEND_NETWORK),
        name=PRODUCTION_BACKEND_NETWORK,
        subnet="172.31.61.0/28",
        internal=True,
    )
    verify_network(
        _inspect_network(PRODUCTION_LOOPBACK_NETWORK),
        name=PRODUCTION_LOOPBACK_NETWORK,
        subnet="172.31.64.0/28",
        internal=False,
        only_container=(production_relay_id, "172.31.64.2"),
        options=NO_MASQUERADE,
    )
    verify_edge_namespace_routes(
        production_edge,
        ingress_gateway="172.31.60.1",
        relay_address="172.31.61.3",
        backend_source="172.31.61.2",
    )
    verify_loopback_health(args.production_loopback_port)

    expected_dnat = {
        (bind_ip, args.production_port): ("172.31.60.2", 8443),
        ("127.0.0.1", args.production_loopback_port): ("172.31.64.2", 8080),
    }
    if args.include_gate:
        gate_edge_id = _container_id(
            compose_file,
            "edge-gate",
            include_gate=True,
        )
        gate_relay_id = _container_id(
            compose_file,
            "relay-gate",
            include_gate=True,
        )
        gate_edge = _inspect_container(gate_edge_id)
        gate_relay = _inspect_container(gate_relay_id)
        verify_container_networks(
            gate_edge,
            {
                GATE_INGRESS_NETWORK: "172.31.63.2",
                GATE_BACKEND_NETWORK: "172.31.62.2",
            },
        )
        verify_container_networks(
            gate_relay,
            {
                GATE_LOOPBACK_NETWORK: "172.31.65.2",
                GATE_BACKEND_NETWORK: "172.31.62.3",
            },
        )
        verify_edge_port_binding(
            gate_edge,
            bind_ip=bind_ip,
            host_port=args.gate_port,
        )
        verify_relay_loopback_binding(
            gate_relay,
            host_port=args.gate_loopback_port,
        )
        verify_network(
            _inspect_network(GATE_INGRESS_NETWORK),
            name=GATE_INGRESS_NETWORK,
            subnet="172.31.63.0/28",
            internal=False,
            only_container=(gate_edge_id, "172.31.63.2"),
        )
        verify_network(
            _inspect_network(GATE_BACKEND_NETWORK),
            name=GATE_BACKEND_NETWORK,
            subnet="172.31.62.0/28",
            internal=True,
        )
        verify_network(
            _inspect_network(GATE_LOOPBACK_NETWORK),
            name=GATE_LOOPBACK_NETWORK,
            subnet="172.31.65.0/28",
            internal=False,
            only_container=(gate_relay_id, "172.31.65.2"),
            options=NO_MASQUERADE,
        )
        verify_edge_namespace_routes(
            gate_edge,
            ingress_gateway="172.31.63.1",
            relay_address="172.31.62.3",
            backend_source="172.31.62.2",
        )
        verify_loopback_health(args.gate_loopback_port)
        expected_dnat[(bind_ip, args.gate_port)] = ("172.31.63.2", 8443)
        expected_dnat[("127.0.0.1", args.gate_loopback_port)] = (
            "172.31.65.2",
            8080,
        )

    nat = _run(("iptables-save", "-t", "nat"))
    verify_exact_dnat_set(
        nat,
        expected=expected_dnat,
    )

    print("relay-edge runtime networks: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeNetworkError) as exc:
        raise SystemExit(f"relay-edge runtime networks: FAIL: {exc}") from None
