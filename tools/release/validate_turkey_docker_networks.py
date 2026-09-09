#!/usr/bin/env python3
"""Bind Turkey host-route exceptions to the exact reviewed Docker bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, NamedTuple


EXPECTED_NAME = "chebycodex-turkey_backend"
EXPECTED_SUBNET = ipaddress.ip_network("172.31.42.0/28")
EXPECTED_LABELS = {
    "com.docker.compose.project": "chebycodex-turkey",
    "com.docker.compose.network": "backend",
}
NETWORK_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
INTERFACE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,15}")
BRIDGE_OPTION = "com.docker.network.bridge.name"


class NetworkBinding(NamedTuple):
    network_id: str
    bridge: str


def network_subnets(network: dict[str, Any]) -> set[ipaddress._BaseNetwork]:
    subnets: set[ipaddress._BaseNetwork] = set()
    for config in network.get("IPAM", {}).get("Config", []) or []:
        value = config.get("Subnet")
        if value:
            try:
                subnets.add(ipaddress.ip_network(value))
            except ValueError as error:
                raise RuntimeError("Docker network contains an invalid subnet") from error
    return subnets


def reviewed_binding(network: dict[str, Any]) -> NetworkBinding:
    network_id = str(network.get("Id", ""))
    if NETWORK_ID_PATTERN.fullmatch(network_id) is None:
        raise RuntimeError(f"Docker network {EXPECTED_NAME} has an invalid exact ID")
    if network.get("Driver") != "bridge" or network.get("Scope") != "local":
        raise RuntimeError(f"Docker network {EXPECTED_NAME} is not a local bridge")
    options = network.get("Options") or {}
    if not isinstance(options, dict):
        raise RuntimeError(f"Docker network {EXPECTED_NAME} options are malformed")
    bridge = (
        str(options[BRIDGE_OPTION])
        if BRIDGE_OPTION in options
        else f"br-{network_id[:12]}"
    )
    if INTERFACE_PATTERN.fullmatch(bridge) is None:
        raise RuntimeError(f"Docker network {EXPECTED_NAME} bridge name is invalid")
    return NetworkBinding(network_id=network_id, bridge=bridge)


def validate(networks: list[dict[str, Any]]) -> NetworkBinding | None:
    seen_expected = 0
    binding: NetworkBinding | None = None
    for network in networks:
        name = str(network.get("Name", "unknown"))
        subnets = network_subnets(network)
        if name == EXPECTED_NAME:
            seen_expected += 1
            if subnets != {EXPECTED_SUBNET}:
                raise RuntimeError(
                    f"Docker network {EXPECTED_NAME} does not have the exact reviewed subnet"
                )
            if network.get("Internal") is not True:
                raise RuntimeError(f"Docker network {EXPECTED_NAME} is not internal")
            labels = network.get("Labels") or {}
            if not isinstance(labels, dict):
                raise RuntimeError(f"Docker network {EXPECTED_NAME} labels are malformed")
            for key, value in EXPECTED_LABELS.items():
                if labels.get(key) != value:
                    raise RuntimeError(
                        f"Docker network {EXPECTED_NAME} lacks reviewed Compose label {key}"
                    )
            binding = reviewed_binding(network)
            continue
        for subnet in subnets:
            if subnet.overlaps(EXPECTED_SUBNET):
                raise RuntimeError(
                    f"Docker network {name} overlaps reviewed backend {EXPECTED_SUBNET}"
                )
    if seen_expected > 1:
        raise RuntimeError(f"multiple Docker networks claim the name {EXPECTED_NAME}")
    return binding


def route_destination(route: dict[str, Any], family: int) -> ipaddress._BaseNetwork | None:
    raw = route.get("dst")
    if raw in {None, "default"}:
        return None
    try:
        network = ipaddress.ip_network(str(raw), strict=False)
    except ValueError as error:
        raise RuntimeError("host route table contains an invalid destination") from error
    if network.version != family:
        raise RuntimeError("host route table family does not match its command")
    return network


def route_protocol(route: dict[str, Any]) -> str:
    values = {
        str(route[key])
        for key in ("protocol", "proto")
        if key in route and route[key] is not None
    }
    if len(values) != 1:
        return ""
    return next(iter(values))


def route_table(route: dict[str, Any]) -> str:
    value = route.get("table")
    return "main" if value is None else str(value)


def is_reviewed_backend_route(
    route: dict[str, Any], destination: ipaddress._BaseNetwork, binding: NetworkBinding
) -> bool:
    if str(route.get("dev", "")) != binding.bridge or route_protocol(route) != "kernel":
        return False
    table = route_table(route)
    route_type = str(route.get("type", "unicast"))
    scope = str(route.get("scope", ""))
    if table in {"main", "254"}:
        return (
            destination == EXPECTED_SUBNET
            and route_type == "unicast"
            and scope == "link"
        )
    if table not in {"local", "255"}:
        return False
    if not destination.subnet_of(EXPECTED_SUBNET) or destination.prefixlen != 32:
        return False
    return (route_type, scope) in {("local", "host"), ("broadcast", "link")}


def validate_host_routes(
    ipv4_routes: list[dict[str, Any]],
    ipv6_routes: list[dict[str, Any]],
    binding: NetworkBinding | None = None,
) -> None:
    for family, routes in ((4, ipv4_routes), (6, ipv6_routes)):
        if not isinstance(routes, list) or not all(isinstance(route, dict) for route in routes):
            raise RuntimeError("host route JSON must be a list of objects")
        for route in routes:
            destination = route_destination(route, family)
            if destination is None or not destination.overlaps(EXPECTED_SUBNET):
                continue
            if binding is not None and is_reviewed_backend_route(route, destination, binding):
                continue
            raise RuntimeError(
                f"host/VPN route {destination} is not the exact reviewed Docker backend route"
            )


def validate_bridge_link(
    document: list[dict[str, Any]], binding: NetworkBinding
) -> None:
    if len(document) != 1 or not isinstance(document[0], dict):
        raise RuntimeError("reviewed Docker bridge must resolve to exactly one host link")
    link = document[0]
    link_info = link.get("linkinfo") or {}
    if not isinstance(link_info, dict):
        raise RuntimeError("reviewed Docker bridge link metadata is malformed")
    info_kind = link_info.get("info_kind")
    if link.get("ifname") != binding.bridge or info_kind != "bridge":
        raise RuntimeError("reviewed Docker network device is not the exact Linux bridge")


def inspect_bridge_link(binding: NetworkBinding) -> None:
    try:
        document = json.loads(
            subprocess.check_output(
                ["ip", "-j", "link", "show", "dev", binding.bridge],
                text=True,
                stderr=subprocess.PIPE,
            )
        )
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
        raise RuntimeError("could not inspect reviewed Docker bridge interface") from error
    if not isinstance(document, list):
        raise RuntimeError("host link output must be a JSON object list")
    validate_bridge_link(document, binding)


def inspect_routes(family: int) -> list[dict[str, Any]]:
    if family == 6 and not Path("/proc/net/if_inet6").exists():
        return []
    command = ["ip", "-j", f"-{family}", "route", "show", "table", "all"]
    try:
        output = subprocess.check_output(command, text=True, stderr=subprocess.PIPE)
        document = json.loads(output)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not inspect IPv{family} host routes") from error
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise RuntimeError(f"IPv{family} route output must be a JSON object list")
    return document


def inspect_docker_networks() -> list[dict[str, Any]]:
    try:
        identifiers = subprocess.check_output(
            ["docker", "network", "ls", "-q"], text=True, stderr=subprocess.PIPE
        ).split()
        document = (
            json.loads(
                subprocess.check_output(
                    ["docker", "network", "inspect", *identifiers],
                    text=True,
                    stderr=subprocess.PIPE,
                )
            )
            if identifiers
            else []
        )
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
        raise RuntimeError("could not inspect Docker networks") from error
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise RuntimeError("Docker network inspect output must be a list of objects")
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inspect_json", nargs="?", default="-")
    parser.add_argument("--inspect-docker", action="store_true")
    parser.add_argument("--inspect-host-routes", action="store_true")
    args = parser.parse_args()
    if args.inspect_docker and args.inspect_host_routes:
        parser.error("choose one live inspection mode")
    if args.inspect_host_routes:
        binding = validate(inspect_docker_networks())
        if binding is not None:
            inspect_bridge_link(binding)
        validate_host_routes(inspect_routes(4), inspect_routes(6), binding)
        print("host/VPN route collision gate: PASS")
        return 0
    if args.inspect_docker:
        document = inspect_docker_networks()
    elif args.inspect_json == "-":
        document = json.load(sys.stdin)
    else:
        document = json.loads(Path(args.inspect_json).read_text(encoding="utf-8"))
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise RuntimeError("Docker network inspect output must be a list of objects")
    validate(document)
    print("Docker network collision gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
