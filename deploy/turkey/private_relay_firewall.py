#!/usr/bin/env python3
"""Install and verify the fixed-IP Relay firewall on the Turkey host.

The script is installed at the legacy systemd ExecStart path
``/usr/local/sbin/codex-security-lockdown.sh``.  The suffix is intentionally
preserved; the Python shebang is authoritative.

Policy updates are staged in the inactive A/B chains and made live by replacing
one dispatcher rule.  A failed verification restores the previous dispatch
targets.  The previous on-disk executable is retained next to the installed
copy, while the active executable and mode file are replaced atomically.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable, Sequence


MODE_GATE = "gate-enabled"
MODE_PRODUCTION = "production-only"
VALID_MODES = frozenset({MODE_GATE, MODE_PRODUCTION})

DEFAULT_DESTINATION = Path("/usr/local/sbin/codex-security-lockdown.sh")
DEFAULT_MODE_FILE = Path("/etc/chebycodex-firewall/mode")
DEFAULT_LOCK_FILE = Path("/run/lock/chebycodex-firewall.lock")

PRODUCTION_INGRESS_SUBNET = "172.31.60.0/28"
PRODUCTION_INGRESS_EDGE = "172.31.60.2/32"
PRODUCTION_BACKEND_SUBNET = "172.31.61.0/28"
PRODUCTION_BACKEND_EDGE = "172.31.61.2/32"
PRODUCTION_RELAY = "172.31.61.3/32"
PRODUCTION_CONNECTOR = "172.31.61.4/32"
GATE_BACKEND_SUBNET = "172.31.62.0/28"
GATE_BACKEND_EDGE = "172.31.62.2/32"
GATE_RELAY = "172.31.62.3/32"
GATE_CONNECTOR = "172.31.62.4/32"
GATE_INGRESS_SUBNET = "172.31.63.0/28"
GATE_INGRESS_EDGE = "172.31.63.2/32"
PRODUCTION_LOOPBACK_SUBNET = "172.31.64.0/28"
PRODUCTION_LOOPBACK_RELAY = "172.31.64.2/32"
GATE_LOOPBACK_SUBNET = "172.31.65.0/28"
GATE_LOOPBACK_RELAY = "172.31.65.2/32"

IPV6_DISABLE_PATHS = (
    Path("/proc/sys/net/ipv6/conf/all/disable_ipv6"),
    Path("/proc/sys/net/ipv6/conf/default/disable_ipv6"),
)

OPENCLAW_NETWORK_NAME = "openclaw-phone-controller_default"
OPENCLAW_NET_FALLBACK = "172.27.0.0/16"
CODEX_NETWORK_NAME = "codex-standalone-net"
CODEX_NET_FALLBACK = "172.28.0.0/16"
CHEBYCODEX_NETWORK_NAME = "chebycodex-turkey-connector_outbound"
CHEBYCODEX_NET_FALLBACK = "172.30.0.0/28"
CHEBYCODEX_ADDRESS = "172.30.0.2/32"
TURKEY_VPN_NETWORK_NAME = "turkey-vpn-net"
TURKEY_VPN_NET_FALLBACK = "172.24.0.0/16"

DNS_SERVERS = ("100.125.2.250/32", "100.125.2.251/32")
HSS_NET = "100.125.0.0/16"


class FirewallError(RuntimeError):
    """A generic, secret-free firewall failure."""


@dataclass(frozen=True)
class Rule:
    source: str = "0.0.0.0/0"
    destination: str = "0.0.0.0/0"
    protocol: str | None = None
    in_interface: str | None = None
    out_interface: str | None = None
    source_port: str | None = None
    destination_port: str | None = None
    original_destination_port: str | None = None
    states: frozenset[str] = frozenset()
    jump: str | None = None
    reject_with: str | None = None
    unknown: tuple[str, ...] = ()
    negated: bool = False

    def arguments(self) -> list[str]:
        args: list[str] = []
        if self.source not in ("0.0.0.0/0", "::/0"):
            args.extend(("-s", self.source))
        if self.destination not in ("0.0.0.0/0", "::/0"):
            args.extend(("-d", self.destination))
        if self.in_interface is not None:
            args.extend(("-i", self.in_interface))
        if self.out_interface is not None:
            args.extend(("-o", self.out_interface))
        if self.protocol is not None:
            args.extend(("-p", self.protocol))
        if self.source_port is not None:
            args.extend(("-m", "multiport" if "," in self.source_port else self.protocol or "tcp"))
            args.extend(("--sports" if "," in self.source_port else "--sport", self.source_port))
        if self.destination_port is not None:
            args.extend(("-m", "multiport" if "," in self.destination_port else self.protocol or "tcp"))
            args.extend(("--dports" if "," in self.destination_port else "--dport", self.destination_port))
        if self.original_destination_port is not None or self.states:
            args.extend(("-m", "conntrack"))
            if self.original_destination_port is not None:
                args.extend(("--ctorigdstport", self.original_destination_port))
            if self.states:
                args.extend(("--ctstate", ",".join(sorted(self.states))))
        if self.jump is not None:
            args.extend(("-j", self.jump))
        if self.reject_with is not None:
            args.extend(("--reject-with", self.reject_with))
        return args


@dataclass(frozen=True)
class NetworkContext:
    public_interface: str
    openclaw_net: str
    codex_net: str
    chebycodex_net: str
    turkey_vpn_net: str
    allow_turkey_vpn: bool


@dataclass(frozen=True)
class Slot:
    binary: str
    parent: str
    dispatcher: str
    chain_a: str
    chain_b: str
    rules: tuple[Rule, ...]


@dataclass
class SwitchedSlot:
    slot: Slot
    old_target: str | None
    new_target: str
    inserted_parent_jump: bool


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(command),
                check=check,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FirewallError("firewall command failed") from exc


def _network(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


def parse_rule(
    line: str,
    chain: str,
    *,
    default_network: str = "0.0.0.0/0",
) -> Rule | None:
    tokens = shlex.split(line)
    if len(tokens) < 2 or tokens[0] != "-A" or tokens[1] != chain:
        return None
    values: dict[str, object] = {
        "source": default_network,
        "destination": default_network,
        "states": frozenset(),
        "unknown": (),
        "negated": False,
    }
    unknown: list[str] = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "!":
            values["negated"] = True
            index += 1
            continue
        if token in ("-s", "--source", "-d", "--destination"):
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            key = "source" if token in ("-s", "--source") else "destination"
            values[key] = _network(tokens[index + 1])
            index += 2
            continue
        if token in ("-p", "--protocol", "-i", "--in-interface", "-o", "--out-interface"):
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            key = {
                "-p": "protocol",
                "--protocol": "protocol",
                "-i": "in_interface",
                "--in-interface": "in_interface",
                "-o": "out_interface",
                "--out-interface": "out_interface",
            }[token]
            values[key] = tokens[index + 1]
            index += 2
            continue
        if token in ("--sport", "--sports", "--source-port", "--source-ports"):
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["source_port"] = tokens[index + 1]
            index += 2
            continue
        if token in (
            "--dport",
            "--dports",
            "--destination-port",
            "--destination-ports",
        ):
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["destination_port"] = tokens[index + 1]
            index += 2
            continue
        if token == "--ctorigdstport":
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["original_destination_port"] = tokens[index + 1]
            index += 2
            continue
        if token == "--ctstate":
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["states"] = frozenset(
                item.upper() for item in tokens[index + 1].split(",")
            )
            index += 2
            continue
        if token in ("-j", "--jump"):
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["jump"] = tokens[index + 1]
            index += 2
            continue
        if token == "--reject-with":
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            values["reject_with"] = tokens[index + 1]
            index += 2
            continue
        if token == "-m":
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            index += 2
            continue
        if token == "--comment":
            if index + 1 >= len(tokens):
                raise FirewallError("invalid firewall rule")
            index += 2
            continue
        unknown.append(token)
        index += 1
    values["unknown"] = tuple(unknown)
    return Rule(**values)  # type: ignore[arg-type]


def parse_chain(
    output: str,
    chain: str,
    *,
    default_network: str = "0.0.0.0/0",
) -> tuple[Rule, ...]:
    parsed: list[Rule] = []
    for line in output.splitlines():
        rule = parse_rule(line, chain, default_network=default_network)
        if rule is not None:
            parsed.append(rule)
    return tuple(parsed)


def _accept(
    *,
    source: str = "0.0.0.0/0",
    destination: str = "0.0.0.0/0",
    protocol: str | None = None,
    in_interface: str | None = None,
    out_interface: str | None = None,
    destination_port: str | None = None,
    original_destination_port: str | None = None,
    states: Iterable[str] = (),
) -> Rule:
    return Rule(
        source=_network(source),
        destination=_network(destination),
        protocol=protocol,
        in_interface=in_interface,
        out_interface=out_interface,
        destination_port=destination_port,
        original_destination_port=original_destination_port,
        states=frozenset(state.upper() for state in states),
        jump="ACCEPT",
    )


def _drop(
    *,
    source: str = "0.0.0.0/0",
    destination: str = "0.0.0.0/0",
    protocol: str | None = None,
    in_interface: str | None = None,
    destination_port: str | None = None,
    original_destination_port: str | None = None,
    states: Iterable[str] = (),
) -> Rule:
    return Rule(
        source=_network(source),
        destination=_network(destination),
        protocol=protocol,
        in_interface=in_interface,
        destination_port=destination_port,
        original_destination_port=original_destination_port,
        states=frozenset(state.upper() for state in states),
        jump="DROP",
    )


def build_input_rules(context: NetworkContext) -> tuple[Rule, ...]:
    rules = [
        _accept(in_interface="lo"),
        _accept(states=("ESTABLISHED", "RELATED")),
        _drop(source=PRODUCTION_INGRESS_SUBNET),
        _drop(source=PRODUCTION_BACKEND_SUBNET),
        _drop(source=GATE_BACKEND_SUBNET),
        _drop(source=GATE_INGRESS_SUBNET),
        _drop(source=PRODUCTION_LOOPBACK_SUBNET),
        _drop(source=GATE_LOOPBACK_SUBNET),
        _accept(protocol="tcp", destination_port="10", states=("NEW",)),
        _accept(protocol="tcp", destination_port="3438", states=("NEW",)),
        _accept(protocol="tcp", destination_port="3448", states=("NEW",)),
    ]
    if context.allow_turkey_vpn:
        rules.extend(
            (
                _accept(protocol="tcp", destination_port="2096", states=("NEW",)),
                _accept(protocol="udp", destination_port="2096", states=("NEW",)),
                _accept(protocol="udp", destination_port="443", states=("NEW",)),
            )
        )
    rules.extend((_drop(protocol="tcp"), _drop(protocol="udp")))
    return tuple(rules)


def build_output_rules(_: NetworkContext) -> tuple[Rule, ...]:
    rules = [
        _accept(out_interface="lo"),
        _accept(states=("ESTABLISHED", "RELATED")),
    ]
    for dns in DNS_SERVERS:
        rules.extend(
            (
                _accept(
                    destination=dns,
                    protocol="udp",
                    destination_port="53",
                    states=("NEW",),
                ),
                _accept(
                    destination=dns,
                    protocol="tcp",
                    destination_port="53",
                    states=("NEW",),
                ),
            )
        )
    rules.extend(
        (
            _accept(
                destination=HSS_NET,
                protocol="tcp",
                destination_port="10180",
                states=("NEW",),
            ),
            Rule(
                protocol="tcp",
                destination_port="22,5901",
                states=frozenset({"NEW"}),
                jump="REJECT",
                reject_with="tcp-reset",
            ),
        )
    )
    return tuple(rules)


def build_docker_rules(
    context: NetworkContext,
    mode: str,
) -> tuple[Rule, ...]:
    rules = [
        _drop(
            protocol="tcp",
            in_interface=context.public_interface,
            original_destination_port="80",
            states=("NEW",),
        ),
        _drop(
            protocol="tcp",
            in_interface=context.public_interface,
            original_destination_port="443",
            states=("NEW",),
        ),
        _drop(source=context.chebycodex_net, destination=context.openclaw_net),
        _drop(source=context.openclaw_net, destination=context.chebycodex_net),
        _drop(source=context.codex_net, destination=context.openclaw_net),
        _drop(source=context.openclaw_net, destination=context.codex_net),
    ]
    if mode == MODE_PRODUCTION:
        # Fence already-established Gate WSS and backend flows during the
        # gate-enabled -> production-only transition.
        rules.extend(
            (
                _drop(source=GATE_INGRESS_SUBNET),
                _drop(destination=GATE_INGRESS_SUBNET),
                _drop(source=GATE_BACKEND_SUBNET),
                _drop(destination=GATE_BACKEND_SUBNET),
                _drop(source=GATE_LOOPBACK_SUBNET),
                _drop(destination=GATE_LOOPBACK_SUBNET),
            )
        )
    rules.extend(
        (
            _accept(states=("ESTABLISHED", "RELATED")),
            _accept(
                destination=PRODUCTION_INGRESS_EDGE,
                protocol="tcp",
                in_interface=context.public_interface,
                destination_port="8443",
                original_destination_port="27461",
                states=("NEW",),
            ),
        )
    )
    if mode == MODE_GATE:
        rules.append(
            _accept(
                destination=GATE_INGRESS_EDGE,
                protocol="tcp",
                in_interface=context.public_interface,
                destination_port="8443",
                original_destination_port="27462",
                states=("NEW",),
            )
        )
    rules.extend(
        (
            _accept(
                source=PRODUCTION_BACKEND_EDGE,
                destination=PRODUCTION_RELAY,
                protocol="tcp",
                destination_port="8080",
                states=("NEW",),
            ),
            _accept(
                source=PRODUCTION_CONNECTOR,
                destination=PRODUCTION_RELAY,
                protocol="tcp",
                destination_port="8080",
                states=("NEW",),
            ),
            _accept(
                destination=PRODUCTION_LOOPBACK_RELAY,
                protocol="tcp",
                in_interface="lo",
                destination_port="8080",
                original_destination_port="18080",
                states=("NEW",),
            ),
        )
    )
    if mode == MODE_GATE:
        rules.extend(
            (
                _accept(
                    source=GATE_BACKEND_EDGE,
                    destination=GATE_RELAY,
                    protocol="tcp",
                    destination_port="8080",
                    states=("NEW",),
                ),
                _accept(
                    source=GATE_CONNECTOR,
                    destination=GATE_RELAY,
                    protocol="tcp",
                    destination_port="8080",
                    states=("NEW",),
                ),
                _accept(
                    destination=GATE_LOOPBACK_RELAY,
                    protocol="tcp",
                    in_interface="lo",
                    destination_port="8080",
                    original_destination_port="18081",
                    states=("NEW",),
                ),
            )
        )
    private_subnets = [
        PRODUCTION_INGRESS_SUBNET,
        PRODUCTION_BACKEND_SUBNET,
        PRODUCTION_LOOPBACK_SUBNET,
    ]
    if mode == MODE_GATE:
        private_subnets.extend(
            (
                GATE_BACKEND_SUBNET,
                GATE_INGRESS_SUBNET,
                GATE_LOOPBACK_SUBNET,
            )
        )
    for subnet in private_subnets:
        rules.extend((_drop(source=subnet), _drop(destination=subnet)))
    rules.extend(
        (
            _accept(
                destination=context.openclaw_net,
                protocol="tcp",
                in_interface=context.public_interface,
                destination_port="18789",
                original_destination_port="3438",
                states=("NEW",),
            ),
            _accept(
                destination=context.openclaw_net,
                protocol="tcp",
                in_interface=context.public_interface,
                destination_port="3438",
                original_destination_port="3438",
                states=("NEW",),
            ),
            _accept(
                source=context.codex_net,
                destination=CHEBYCODEX_ADDRESS,
                protocol="tcp",
                destination_port="3438",
                original_destination_port="3438",
                states=("NEW",),
            ),
            _accept(
                destination=CHEBYCODEX_ADDRESS,
                protocol="tcp",
                in_interface=context.public_interface,
                destination_port="3438",
                original_destination_port="3448",
                states=("NEW",),
            ),
        )
    )
    for dns in DNS_SERVERS:
        rules.extend(
            (
                _accept(
                    source=context.chebycodex_net,
                    destination=dns,
                    protocol="udp",
                    destination_port="53",
                ),
                _accept(
                    source=context.chebycodex_net,
                    destination=dns,
                    protocol="tcp",
                    destination_port="53",
                ),
            )
        )
    rules.extend(
        (
            _accept(
                source=context.chebycodex_net,
                protocol="tcp",
                destination_port="443",
            ),
            _accept(source=context.openclaw_net),
            _accept(source=context.codex_net),
        )
    )
    if context.allow_turkey_vpn:
        rules.extend(
            (
                _accept(
                    destination=context.turkey_vpn_net,
                    protocol="tcp",
                    destination_port="2096",
                ),
                _accept(
                    destination=context.turkey_vpn_net,
                    protocol="udp",
                    destination_port="2096",
                ),
                _accept(source=context.turkey_vpn_net),
            )
        )
    rules.append(_drop())
    return tuple(rules)


def build_ipv6_input_rules() -> tuple[Rule, ...]:
    return (
        Rule(
            source="::/0",
            destination="::/0",
            in_interface="lo",
            jump="ACCEPT",
        ),
        Rule(
            source="::/0",
            destination="::/0",
            states=frozenset({"ESTABLISHED", "RELATED"}),
            jump="ACCEPT",
        ),
        Rule(
            source="::/0",
            destination="::/0",
            protocol="tcp",
            jump="DROP",
        ),
        Rule(
            source="::/0",
            destination="::/0",
            protocol="udp",
            jump="DROP",
        ),
    )


def build_ipv6_output_rules() -> tuple[Rule, ...]:
    return (
        Rule(
            source="::/0",
            destination="::/0",
            out_interface="lo",
            jump="ACCEPT",
        ),
        Rule(
            source="::/0",
            destination="::/0",
            states=frozenset({"ESTABLISHED", "RELATED"}),
            jump="ACCEPT",
        ),
        Rule(
            source="::/0",
            destination="::/0",
            protocol="tcp",
            destination_port="22,5901",
            states=frozenset({"NEW"}),
            jump="REJECT",
            reject_with="tcp-reset",
        ),
    )


def _ipv6_globally_disabled() -> bool:
    values: list[str] = []
    for path in IPV6_DISABLE_PATHS:
        try:
            values.append(path.read_text(encoding="ascii").strip())
        except OSError:
            return False
    return values == ["1", "1"]


def _include_ipv6_or_fail_closed() -> bool:
    if shutil.which("ip6tables") is not None:
        return True
    if _ipv6_globally_disabled():
        return False
    raise FirewallError(
        "ip6tables is required unless host IPv6 is globally disabled"
    )


def _run_iptables(
    runner: CommandRunner,
    binary: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return runner.run((binary, "-w", "5", *args), check=check)


def _chain_exists(runner: CommandRunner, binary: str, chain: str) -> bool:
    result = _run_iptables(runner, binary, "-S", chain, check=False)
    return result.returncode == 0


def _ensure_chain(runner: CommandRunner, binary: str, chain: str) -> None:
    if not _chain_exists(runner, binary, chain):
        _run_iptables(runner, binary, "-N", chain)


def _ensure_parent_chain(runner: CommandRunner, slot: Slot) -> None:
    if _chain_exists(runner, slot.binary, slot.parent):
        return
    if slot.binary == "iptables" and slot.parent == "DOCKER-USER":
        # Docker normally creates this user-owned chain when the daemon starts.
        # The reviewed boot unit deliberately runs first, so create only this
        # documented extension chain and install the complete policy before
        # Docker can restore any restart-policy container port mappings.
        _run_iptables(runner, slot.binary, "-N", slot.parent)
        return
    raise FirewallError("required firewall parent chain is absent")


def _chain_rules(
    runner: CommandRunner,
    binary: str,
    chain: str,
) -> tuple[Rule, ...]:
    output = _run_iptables(runner, binary, "-S", chain).stdout
    return parse_chain(
        output,
        chain,
        default_network="::/0" if binary == "ip6tables" else "0.0.0.0/0",
    )


def _populate_chain(
    runner: CommandRunner,
    slot: Slot,
    chain: str,
) -> None:
    _ensure_chain(runner, slot.binary, chain)
    _run_iptables(runner, slot.binary, "-F", chain)
    for rule in slot.rules:
        _run_iptables(
            runner,
            slot.binary,
            "-A",
            chain,
            *rule.arguments(),
        )


def _dispatcher_target(
    runner: CommandRunner,
    slot: Slot,
) -> str | None:
    if not _chain_exists(runner, slot.binary, slot.dispatcher):
        raise FirewallError("firewall dispatcher is absent")
    rules = _chain_rules(runner, slot.binary, slot.dispatcher)
    if not rules:
        return None
    if (
        len(rules) != 1
        or rules[0].jump not in (slot.chain_a, slot.chain_b)
        or rules[0]
        != Rule(
            source="::/0" if slot.binary == "ip6tables" else "0.0.0.0/0",
            destination="::/0"
            if slot.binary == "ip6tables"
            else "0.0.0.0/0",
            jump=rules[0].jump,
        )
    ):
        raise FirewallError("unexpected firewall dispatcher")
    return rules[0].jump


def _chain_has_first_jump(
    runner: CommandRunner,
    binary: str,
    parent: str,
    target: str,
) -> bool:
    if not _chain_exists(runner, binary, parent):
        return False
    rules = _chain_rules(runner, binary, parent)
    expected = Rule(
        source="::/0" if binary == "ip6tables" else "0.0.0.0/0",
        destination="::/0" if binary == "ip6tables" else "0.0.0.0/0",
        jump=target,
    )
    return bool(rules) and rules[0] == expected


def _parent_has_first_jump(
    runner: CommandRunner,
    slot: Slot,
) -> bool:
    return _chain_has_first_jump(
        runner,
        slot.binary,
        slot.parent,
        slot.dispatcher,
    )


def _switch_slot(
    runner: CommandRunner,
    slot: Slot,
    old_target: str | None,
    new_target: str,
) -> SwitchedSlot:
    if old_target is None:
        _run_iptables(runner, slot.binary, "-A", slot.dispatcher, "-j", new_target)
    else:
        _run_iptables(
            runner,
            slot.binary,
            "-R",
            slot.dispatcher,
            "1",
            "-j",
            new_target,
        )
    inserted = False
    if not _parent_has_first_jump(runner, slot):
        _run_iptables(
            runner,
            slot.binary,
            "-I",
            slot.parent,
            "1",
            "-j",
            slot.dispatcher,
        )
        inserted = True
    return SwitchedSlot(
        slot=slot,
        old_target=old_target,
        new_target=new_target,
        inserted_parent_jump=inserted,
    )


def _rollback_slot(runner: CommandRunner, switched: SwitchedSlot) -> None:
    slot = switched.slot
    try:
        if switched.old_target is None:
            _run_iptables(runner, slot.binary, "-D", slot.dispatcher, "1")
        else:
            _run_iptables(
                runner,
                slot.binary,
                "-R",
                slot.dispatcher,
                "1",
                "-j",
                switched.old_target,
            )
        if switched.inserted_parent_jump:
            _run_iptables(runner, slot.binary, "-D", slot.parent, "1")
    except FirewallError:
        # The newly selected policy is complete and ends in a fail-closed rule.
        # Do not risk a second, broader mutation while handling an error.
        pass


def _verify_slot(runner: CommandRunner, slot: Slot) -> None:
    if not _parent_has_first_jump(runner, slot):
        raise FirewallError("firewall dispatcher is not first")
    target = _dispatcher_target(runner, slot)
    if target is None:
        raise FirewallError("firewall dispatcher is empty")
    if _chain_rules(runner, slot.binary, target) != slot.rules:
        raise FirewallError("active firewall policy differs from reviewed policy")


def _port_spec_contains(port_spec: str, expected: int) -> bool:
    for component in port_spec.split(","):
        bounds = component.replace("-", ":").split(":")
        try:
            numbers = [int(value) for value in bounds]
        except ValueError as exc:
            raise FirewallError("unrecognized Docker NAT port") from exc
        if len(numbers) == 1 and numbers[0] == expected:
            return True
        if len(numbers) == 2 and numbers[0] <= expected <= numbers[1]:
            return True
        if len(numbers) not in (1, 2):
            raise FirewallError("unrecognized Docker NAT port")
    return False


def _verify_docker_nat_has_no_tcp_80_or_443(
    runner: CommandRunner,
) -> None:
    result = _run_iptables(
        runner,
        "iptables",
        "-t",
        "nat",
        "-S",
        "DOCKER",
        check=False,
    )
    if result.returncode != 0:
        raise FirewallError("Docker NAT chain is absent")
    for rule in parse_chain(result.stdout, "DOCKER"):
        if (
            rule.protocol == "tcp"
            and rule.jump == "DNAT"
            and rule.destination_port is not None
            and any(
                _port_spec_contains(rule.destination_port, port)
                for port in (80, 443)
            )
        ):
            raise FirewallError("Docker NAT publishes forbidden TCP 80/443")


def _remove_legacy_input_443(
    runner: CommandRunner,
) -> None:
    if not _chain_exists(runner, "iptables", "CODX_LOCKDOWN_IN"):
        return
    candidate_rules = _chain_rules(runner, "iptables", "CODX_LOCKDOWN_IN")
    for index in range(len(candidate_rules), 0, -1):
        rule = candidate_rules[index - 1]
        if (
            rule.jump == "ACCEPT"
            and rule.protocol == "tcp"
            and rule.destination_port == "443"
        ):
            _run_iptables(
                runner,
                "iptables",
                "-D",
                "CODX_LOCKDOWN_IN",
                str(index),
            )
def _resolve_docker_subnet(
    runner: CommandRunner,
    network_name: str,
    fallback: str,
) -> str:
    if shutil.which("docker") is None:
        return _network(fallback)
    result = runner.run(
        (
            "docker",
            "network",
            "inspect",
            "--format",
            "{{range .IPAM.Config}}{{if .Subnet}}{{.Subnet}}{{end}}{{end}}",
            network_name,
        ),
        check=False,
    )
    if result.returncode != 0:
        return _network(fallback)
    value = result.stdout.strip().splitlines()
    return _network(value[0]) if value and value[0] else _network(fallback)


def _resolve_public_interface(runner: CommandRunner) -> str:
    configured = os.environ.get("CHEBY_PUBLIC_INTERFACE", "").strip()
    if configured:
        if not configured.replace("-", "").replace("_", "").isalnum():
            raise FirewallError("invalid public interface")
        return configured
    result = runner.run(("ip", "-4", "route", "show", "default"), check=False)
    if result.returncode == 0:
        tokens = result.stdout.split()
        for index, token in enumerate(tokens[:-1]):
            if token == "dev":
                return tokens[index + 1]
    raise FirewallError("cannot resolve public interface")


def resolve_context(runner: CommandRunner) -> NetworkContext:
    allow_vpn = os.environ.get("ALLOW_TURKEY_VPN", "1") == "1"
    return NetworkContext(
        public_interface=_resolve_public_interface(runner),
        openclaw_net=_resolve_docker_subnet(
            runner, OPENCLAW_NETWORK_NAME, OPENCLAW_NET_FALLBACK
        ),
        codex_net=_resolve_docker_subnet(
            runner, CODEX_NETWORK_NAME, CODEX_NET_FALLBACK
        ),
        chebycodex_net=_resolve_docker_subnet(
            runner, CHEBYCODEX_NETWORK_NAME, CHEBYCODEX_NET_FALLBACK
        ),
        turkey_vpn_net=_resolve_docker_subnet(
            runner, TURKEY_VPN_NETWORK_NAME, TURKEY_VPN_NET_FALLBACK
        ),
        allow_turkey_vpn=allow_vpn,
    )


def build_slots(
    context: NetworkContext,
    mode: str,
    *,
    include_ipv6: bool,
) -> tuple[Slot, ...]:
    slots = [
        Slot(
            binary="iptables",
            parent="INPUT",
            dispatcher="CODX_IN_DISPATCH",
            chain_a="CODX_IN_A",
            chain_b="CODX_IN_B",
            rules=build_input_rules(context),
        ),
        Slot(
            binary="iptables",
            parent="OUTPUT",
            dispatcher="CODX_OUT_DISPATCH",
            chain_a="CODX_OUT_A",
            chain_b="CODX_OUT_B",
            rules=build_output_rules(context),
        ),
        Slot(
            binary="iptables",
            parent="DOCKER-USER",
            dispatcher="CODX_DOCKER_DISPATCH",
            chain_a="CODX_DOCKER_A",
            chain_b="CODX_DOCKER_B",
            rules=build_docker_rules(context, mode),
        ),
    ]
    if include_ipv6:
        slots.extend(
            (
                Slot(
                    binary="ip6tables",
                    parent="INPUT",
                    dispatcher="CODX6_IN_DISPATCH",
                    chain_a="CODX6_IN_A",
                    chain_b="CODX6_IN_B",
                    rules=build_ipv6_input_rules(),
                ),
                Slot(
                    binary="ip6tables",
                    parent="OUTPUT",
                    dispatcher="CODX6_OUT_DISPATCH",
                    chain_a="CODX6_OUT_A",
                    chain_b="CODX6_OUT_B",
                    rules=build_ipv6_output_rules(),
                ),
            )
        )
    return tuple(slots)


def verify_policy(
    runner: CommandRunner,
    context: NetworkContext,
    mode: str,
    *,
    require_docker_forward_hook: bool = True,
) -> None:
    include_ipv6 = _include_ipv6_or_fail_closed()
    for slot in build_slots(context, mode, include_ipv6=include_ipv6):
        _verify_slot(runner, slot)

    if require_docker_forward_hook and not _chain_has_first_jump(
        runner,
        "iptables",
        "FORWARD",
        "DOCKER-USER",
    ):
        raise FirewallError("Docker FORWARD hook is not first")
    if require_docker_forward_hook:
        _verify_docker_nat_has_no_tcp_80_or_443(runner)

    for chain in ("CODX_LOCKDOWN_IN", "CODX_IN_A", "CODX_IN_B"):
        if not _chain_exists(runner, "iptables", chain):
            continue
        for rule in _chain_rules(runner, "iptables", chain):
            if (
                rule.jump == "ACCEPT"
                and rule.protocol in (None, "tcp")
                and rule.destination_port in ("80", "443", "80,443", "443,80")
            ):
                raise FirewallError("TCP 80/443 host ingress is allowed")


def verify_pre_docker_policy(
    runner: CommandRunner,
    context: NetworkContext,
    mode: str,
) -> None:
    verify_policy(
        runner,
        context,
        mode,
        require_docker_forward_hook=False,
    )


def _mode_path(path: Path) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return MODE_PRODUCTION
    if value not in VALID_MODES:
        raise FirewallError("invalid persisted firewall mode")
    return value


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def apply_policy(
    runner: CommandRunner,
    context: NetworkContext,
    mode: str,
    mode_file: Path,
) -> None:
    if mode not in VALID_MODES:
        raise FirewallError("invalid firewall mode")
    include_ipv6 = _include_ipv6_or_fail_closed()
    slots = build_slots(context, mode, include_ipv6=include_ipv6)
    prepared: list[tuple[Slot, str | None, str]] = []
    switched: list[SwitchedSlot] = []

    # Removing the stale legacy 443 allow is monotonic and deliberately is not
    # rolled back to an insecure state.
    _remove_legacy_input_443(runner)

    try:
        for slot in slots:
            _ensure_parent_chain(runner, slot)
            _ensure_chain(runner, slot.binary, slot.dispatcher)
            old_target = _dispatcher_target(runner, slot)
            new_target = (
                slot.chain_b if old_target == slot.chain_a else slot.chain_a
            )
            _populate_chain(runner, slot, new_target)
            prepared.append((slot, old_target, new_target))

        for slot, old_target, new_target in prepared:
            switched.append(
                _switch_slot(runner, slot, old_target, new_target)
            )

        verify_pre_docker_policy(runner, context, mode)
        _atomic_write(mode_file, f"{mode}\n".encode("ascii"), 0o600)
    except Exception:
        for item in reversed(switched):
            _rollback_slot(runner, item)
        raise

def _install_self(
    source: Path,
    destination: Path,
    mode_file: Path,
    mode: str,
) -> None:
    source_bytes = source.read_bytes()
    if not source_bytes.startswith(b"#!/usr/bin/env python3\n"):
        raise FirewallError("invalid firewall installer source")

    previous = destination.with_name(f"{destination.name}.previous")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if destination.exists():
        _atomic_write(previous, destination.read_bytes(), 0o700)
    _atomic_write(destination, source_bytes, 0o700)

    try:
        runner = CommandRunner()
        context = resolve_context(runner)
        apply_policy(runner, context, mode, mode_file)
    except FirewallError:
        # The active updater rolls back dispatch targets itself.  Restore the
        # prior executable if possible, but never execute it automatically:
        # the prior Turkey script contained a known TCP 443 ingress allowance.
        if previous.exists():
            _atomic_write(destination, previous.read_bytes(), 0o700)
        raise FirewallError("installed firewall policy failed verification")

    if _mode_path(mode_file) != mode:
        raise FirewallError("firewall mode was not persisted")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install, atomically apply, or verify Turkey Relay firewall"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument(
        "--verify-pre-docker",
        action="store_true",
        dest="verify_pre_docker",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(f"--{MODE_GATE}", action="store_true", dest="gate")
    mode.add_argument(
        f"--{MODE_PRODUCTION}",
        action="store_true",
        dest="production",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mode-file",
        type=Path,
        default=DEFAULT_MODE_FILE,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_FILE,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _selected_mode(args: argparse.Namespace) -> str:
    if args.gate:
        return MODE_GATE
    if args.production:
        return MODE_PRODUCTION
    return _mode_path(args.mode_file)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    mode = _selected_mode(args)
    args.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with args.lock_file.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.install:
            _install_self(
                Path(__file__).resolve(),
                args.destination,
                args.mode_file,
                mode,
            )
        else:
            runner = CommandRunner()
            context = resolve_context(runner)
            if args.verify_pre_docker:
                verify_pre_docker_policy(runner, context, mode)
            elif args.verify:
                verify_policy(runner, context, mode)
            else:
                apply_policy(runner, context, mode, args.mode_file)
    print(f"Turkey private Relay firewall: PASS ({mode})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FirewallError as exc:
        print(f"Turkey private Relay firewall: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
