#!/usr/bin/env python3
"""Fail-closed host checks for the NAT-mapped Relay Edge listener."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess


EXPECTED_PRODUCTION_LOOPBACK_PORT = 18080
DOCKER_PORT_KEY = re.compile(r"([1-9][0-9]{0,4})/(tcp|udp)")


def _validate_fixed_identity(
    bind_ip: ipaddress.IPv4Address,
    public_ip: ipaddress.IPv4Address,
    production_port: int,
    gate_port: int,
) -> None:
    if not public_ip.is_global:
        raise RuntimeError("Relay Edge public identity must be a globally routable IPv4 address")
    if bind_ip.is_loopback or bind_ip.is_multicast or bind_ip.is_unspecified:
        raise RuntimeError("Relay Edge bind identity must be a usable local IPv4 address")
    if bind_ip == public_ip:
        raise RuntimeError("NAT-mapped EIP must not be used as the Docker bind address")
    if production_port != 27461 or gate_port != 27462:
        raise RuntimeError("Relay Edge ports must remain exactly 27461 and 27462")


def _listening_ports() -> set[int]:
    result: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(path, encoding="ascii").read().splitlines()[1:]
        except OSError as error:
            raise RuntimeError(f"cannot inspect host listeners: {path}") from error
        for line in lines:
            fields = line.split()
            if len(fields) >= 4 and fields[3] == "0A":
                result.add(int(fields[1].rsplit(":", 1)[1], 16))
    return result


def _docker_published_ports() -> set[int]:
    try:
        listed = subprocess.run(
            ["docker", "container", "ls", "--quiet", "--no-trunc"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_ids = listed.stdout.split()
        if not container_ids:
            return set()
        inspected = subprocess.run(
            ["docker", "container", "inspect", *container_ids],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        containers = json.loads(inspected.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise RuntimeError("cannot inspect active Docker port publications") from error
    if not isinstance(containers, list):
        raise RuntimeError("Docker port publication inspection returned invalid data")
    result: set[int] = set()
    for container in containers:
        try:
            publications = container["NetworkSettings"]["Ports"]
        except (KeyError, TypeError) as error:
            raise RuntimeError(
                "Docker port publication inspection returned invalid data"
            ) from error
        if not isinstance(publications, dict):
            raise RuntimeError(
                "Docker port publication inspection returned invalid data"
            )
        for container_port, bindings in publications.items():
            if not isinstance(container_port, str):
                raise RuntimeError(
                    "Docker port publication inspection returned invalid data"
                )
            match = DOCKER_PORT_KEY.fullmatch(container_port)
            if match is None or int(match.group(1)) > 65535:
                raise RuntimeError(
                    "Docker port publication inspection returned invalid data"
                )
            if match.group(2) == "udp":
                continue
            if bindings is None:
                continue
            if not isinstance(bindings, list):
                raise RuntimeError(
                    "Docker port publication inspection returned invalid data"
                )
            for binding in bindings:
                try:
                    port = int(binding["HostPort"])
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        "Docker port publication inspection returned invalid data"
                    ) from error
                if not 1 <= port <= 65535:
                    raise RuntimeError(
                        "Docker port publication inspection returned invalid data"
                    )
                result.add(port)
    return result


def _local_addresses() -> set[ipaddress.IPv4Address]:
    try:
        completed = subprocess.run(
            ["ip", "-json", "address", "show"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        interfaces = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise RuntimeError("cannot enumerate local addresses with iproute2") from error
    result: set[ipaddress.IPv4Address] = set()
    for interface in interfaces:
        for address in interface.get("addr_info", []):
            if address.get("family") == "inet":
                parsed = ipaddress.ip_address(address.get("local", ""))
                if isinstance(parsed, ipaddress.IPv4Address):
                    result.add(parsed)
    return result


def _require_port_free(
    occupied_ports: set[int],
    port: int,
    *,
    purpose: str,
) -> None:
    if port in occupied_ports:
        raise RuntimeError(f"{purpose} port {port} is already occupied")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind-ip", required=True)
    parser.add_argument("--public-ip", required=True)
    parser.add_argument("--production-port", required=True, type=int)
    parser.add_argument("--gate-port", required=True, type=int)
    parser.add_argument(
        "--require-free-loopback-port",
        type=int,
        help=(
            "activation-only guard; require the stopped production Relay's "
            "loopback port to be free"
        ),
    )
    args = parser.parse_args()
    bind_ip = ipaddress.ip_address(args.bind_ip)
    public_ip = ipaddress.ip_address(args.public_ip)
    if not isinstance(bind_ip, ipaddress.IPv4Address) or bind_ip.is_loopback:
        raise RuntimeError("bind IP must be a non-loopback local IPv4 address")
    if not isinstance(public_ip, ipaddress.IPv4Address) or not public_ip.is_global:
        raise RuntimeError("public IP must be a globally routable IPv4 address")
    if bind_ip == public_ip:
        raise RuntimeError("NAT-mapped EIP must not be used as the Docker bind address")
    _validate_fixed_identity(
        bind_ip,
        public_ip,
        args.production_port,
        args.gate_port,
    )
    if bind_ip not in _local_addresses():
        raise RuntimeError("configured bind IP is not assigned to this host")
    occupied_ports = _listening_ports() | _docker_published_ports()
    forbidden = occupied_ports.intersection({80, 443})
    if forbidden:
        raise RuntimeError(
            "forbidden TCP 80/443 listener or Docker publication exists on this host"
        )
    occupied = occupied_ports.intersection({args.production_port, args.gate_port})
    if occupied:
        raise RuntimeError(
            "Relay Edge target listener or Docker publication is already occupied"
        )
    if args.require_free_loopback_port is not None:
        if args.require_free_loopback_port != EXPECTED_PRODUCTION_LOOPBACK_PORT:
            raise RuntimeError(
                "production Relay loopback port must remain exactly "
                f"{EXPECTED_PRODUCTION_LOOPBACK_PORT}"
            )
        _require_port_free(
            occupied_ports,
            args.require_free_loopback_port,
            purpose="production Relay loopback activation",
        )
    print(
        "Relay Edge host preflight passed; "
        f"local_bind={bind_ip} public_identity={public_ip}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
