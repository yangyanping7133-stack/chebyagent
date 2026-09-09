from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Sequence

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "private_relay_firewall.py"
SYSTEMD_DIR = MODULE_PATH.parent / "systemd"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "private_relay_firewall",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


firewall = _load_module()


def _context():
    return firewall.NetworkContext(
        public_interface="eth0",
        openclaw_net="172.27.0.0/16",
        codex_net="172.28.0.0/16",
        chebycodex_net="172.30.0.0/28",
        turkey_vpn_net="172.24.0.0/16",
        allow_turkey_vpn=True,
    )


def _index(rules, expected) -> int:
    return rules.index(expected)


def test_input_policy_closes_tcp_80_and_443_but_preserves_existing_services() -> None:
    rules = firewall.build_input_rules(_context())
    accepted_tcp_ports = {
        rule.destination_port
        for rule in rules
        if rule.jump == "ACCEPT" and rule.protocol == "tcp"
    }

    assert accepted_tcp_ports == {"10", "3438", "3448", "2096"}
    assert "80" not in accepted_tcp_ports
    assert "443" not in accepted_tcp_ports
    vpn_udp_443 = firewall._accept(
        protocol="udp",
        destination_port="443",
        states=("NEW",),
    )
    assert vpn_udp_443 in rules
    assert _index(rules, vpn_udp_443) < _index(
        rules, firewall._drop(protocol="udp")
    )
    for subnet in (
        firewall.PRODUCTION_INGRESS_SUBNET,
        firewall.PRODUCTION_BACKEND_SUBNET,
        firewall.GATE_BACKEND_SUBNET,
        firewall.GATE_INGRESS_SUBNET,
        firewall.PRODUCTION_LOOPBACK_SUBNET,
        firewall.GATE_LOOPBACK_SUBNET,
    ):
        assert firewall._drop(source=subnet) in rules
    assert rules[-2:] == (
        firewall._drop(protocol="tcp"),
        firewall._drop(protocol="udp"),
    )


def test_gate_policy_allows_only_exact_edge_and_relay_flows_before_isolation() -> None:
    context = _context()
    rules = firewall.build_docker_rules(context, firewall.MODE_GATE)
    production_ingress = firewall._accept(
        destination=firewall.PRODUCTION_INGRESS_EDGE,
        protocol="tcp",
        in_interface="eth0",
        destination_port="8443",
        original_destination_port="27461",
        states=("NEW",),
    )
    gate_ingress = firewall._accept(
        destination=firewall.GATE_INGRESS_EDGE,
        protocol="tcp",
        in_interface="eth0",
        destination_port="8443",
        original_destination_port="27462",
        states=("NEW",),
    )
    production_edge = firewall._accept(
        source=firewall.PRODUCTION_BACKEND_EDGE,
        destination=firewall.PRODUCTION_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    )
    gate_edge = firewall._accept(
        source=firewall.GATE_BACKEND_EDGE,
        destination=firewall.GATE_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    )
    production_connector = firewall._accept(
        source=firewall.PRODUCTION_CONNECTOR,
        destination=firewall.PRODUCTION_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    )
    gate_connector = firewall._accept(
        source=firewall.GATE_CONNECTOR,
        destination=firewall.GATE_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    )
    production_source_drop = firewall._drop(
        source=firewall.PRODUCTION_BACKEND_SUBNET
    )
    gate_source_drop = firewall._drop(source=firewall.GATE_BACKEND_SUBNET)
    legacy_openclaw_accept = firewall._accept(
        destination=context.openclaw_net,
        protocol="tcp",
        in_interface="eth0",
        destination_port="3438",
        original_destination_port="3438",
        states=("NEW",),
    )

    assert rules[:2] == (
        firewall._drop(
            protocol="tcp",
            in_interface="eth0",
            original_destination_port="80",
            states=("NEW",),
        ),
        firewall._drop(
            protocol="tcp",
            in_interface="eth0",
            original_destination_port="443",
            states=("NEW",),
        ),
    )
    assert rules[2:6] == (
        firewall._drop(
            source=context.chebycodex_net,
            destination=context.openclaw_net,
        ),
        firewall._drop(
            source=context.openclaw_net,
            destination=context.chebycodex_net,
        ),
        firewall._drop(
            source=context.codex_net,
            destination=context.openclaw_net,
        ),
        firewall._drop(
            source=context.openclaw_net,
            destination=context.codex_net,
        ),
    )
    established = firewall._accept(states=("ESTABLISHED", "RELATED"))
    assert _index(rules, rules[0]) < _index(rules, established)
    assert _index(rules, rules[1]) < _index(rules, established)
    assert _index(rules, rules[2]) < _index(rules, established)
    assert _index(rules, rules[3]) < _index(rules, established)
    assert production_ingress in rules
    assert gate_ingress in rules
    assert production_edge in rules
    assert gate_edge in rules
    assert production_connector in rules
    assert gate_connector in rules
    assert firewall._accept(
        destination=firewall.PRODUCTION_LOOPBACK_RELAY,
        protocol="tcp",
        in_interface="lo",
        destination_port="8080",
        original_destination_port="18080",
        states=("NEW",),
    ) in rules
    assert firewall._accept(
        destination=firewall.GATE_LOOPBACK_RELAY,
        protocol="tcp",
        in_interface="lo",
        destination_port="8080",
        original_destination_port="18081",
        states=("NEW",),
    ) in rules
    assert firewall._accept(
        destination=firewall.PRODUCTION_INGRESS_EDGE,
        protocol="tcp",
        in_interface="eth0",
        destination_port="8443",
        original_destination_port="27462",
        states=("NEW",),
    ) not in rules
    assert firewall._accept(
        destination=firewall.GATE_INGRESS_EDGE,
        protocol="tcp",
        in_interface="eth0",
        destination_port="8443",
        original_destination_port="27461",
        states=("NEW",),
    ) not in rules
    assert firewall._accept(
        source=firewall.PRODUCTION_INGRESS_EDGE,
        destination=firewall.PRODUCTION_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    ) not in rules
    assert firewall._accept(
        source=firewall.GATE_INGRESS_EDGE,
        destination=firewall.GATE_RELAY,
        protocol="tcp",
        destination_port="8080",
        states=("NEW",),
    ) not in rules
    assert _index(rules, production_connector) < _index(
        rules, production_source_drop
    )
    assert _index(rules, gate_connector) < _index(rules, gate_source_drop)
    assert _index(rules, production_ingress) < _index(
        rules,
        firewall._drop(destination=firewall.PRODUCTION_INGRESS_SUBNET),
    )
    assert _index(rules, gate_ingress) < _index(
        rules,
        firewall._drop(destination=firewall.GATE_INGRESS_SUBNET),
    )
    assert _index(rules, production_edge) < _index(
        rules,
        firewall._drop(source=firewall.PRODUCTION_BACKEND_SUBNET),
    )
    assert _index(rules, gate_edge) < _index(
        rules,
        firewall._drop(source=firewall.GATE_BACKEND_SUBNET),
    )
    assert _index(rules, gate_source_drop) < _index(
        rules, legacy_openclaw_accept
    )
    for subnet in (
        firewall.PRODUCTION_INGRESS_SUBNET,
        firewall.PRODUCTION_BACKEND_SUBNET,
        firewall.GATE_BACKEND_SUBNET,
        firewall.GATE_INGRESS_SUBNET,
        firewall.PRODUCTION_LOOPBACK_SUBNET,
        firewall.GATE_LOOPBACK_SUBNET,
    ):
        assert _index(rules, firewall._drop(source=subnet)) > _index(
            rules, established
        )
        assert _index(rules, firewall._drop(destination=subnet)) > _index(
            rules, established
        )
    assert rules[-1] == firewall._drop()


def test_production_only_removes_gate_accepts_but_keeps_gate_fail_closed() -> None:
    context = _context()
    rules = firewall.build_docker_rules(
        context,
        firewall.MODE_PRODUCTION,
    )

    assert not any(
        rule.jump == "ACCEPT"
        and (
            rule.source in {
                firewall.GATE_BACKEND_EDGE,
                firewall.GATE_INGRESS_EDGE,
                firewall.GATE_CONNECTOR,
                firewall.GATE_LOOPBACK_RELAY,
            }
            or rule.destination in {
                firewall.GATE_BACKEND_EDGE,
                firewall.GATE_INGRESS_EDGE,
                firewall.GATE_RELAY,
                firewall.GATE_LOOPBACK_RELAY,
            }
        )
        for rule in rules
    )
    established = firewall._accept(states=("ESTABLISHED", "RELATED"))
    for subnet in (
        firewall.GATE_BACKEND_SUBNET,
        firewall.GATE_INGRESS_SUBNET,
        firewall.GATE_LOOPBACK_SUBNET,
    ):
        source_drop = firewall._drop(source=subnet)
        destination_drop = firewall._drop(destination=subnet)
        assert source_drop in rules
        assert destination_drop in rules
        assert _index(rules, source_drop) < _index(rules, established)
        assert _index(rules, destination_drop) < _index(rules, established)
    assert firewall._accept(source=context.openclaw_net) in rules
    assert firewall._accept(source=context.codex_net) in rules
    assert firewall._accept(
        source=context.codex_net,
        destination=firewall.CHEBYCODEX_ADDRESS,
        protocol="tcp",
        destination_port="3438",
        original_destination_port="3438",
        states=("NEW",),
    ) in rules
    assert firewall._accept(
        destination=firewall.CHEBYCODEX_ADDRESS,
        protocol="tcp",
        in_interface="eth0",
        destination_port="3438",
        original_destination_port="3448",
        states=("NEW",),
    ) in rules
    assert firewall._accept(source=context.turkey_vpn_net) in rules
    assert rules[-1] == firewall._drop()


def test_rule_parser_normalizes_modules_networks_and_state_order() -> None:
    parsed = firewall.parse_rule(
        "-A CODX -s 172.31.60.1 -d 172.31.60.2 "
        "-i eth0 -p tcp -m tcp --dport 8443 -m conntrack "
        "--ctorigdstport 27461 "
        "--ctstate RELATED,ESTABLISHED -j ACCEPT",
        "CODX",
    )
    assert parsed == firewall.Rule(
        source="172.31.60.1/32",
        destination="172.31.60.2/32",
        protocol="tcp",
        in_interface="eth0",
        destination_port="8443",
        original_destination_port="27461",
        states=frozenset({"ESTABLISHED", "RELATED"}),
        jump="ACCEPT",
    )


class FakeRunner:
    def __init__(self) -> None:
        self.chains: dict[tuple[str, str], list[list[str]]] = {
            ("iptables", "INPUT"): [["-j", "CODX_LOCKDOWN_IN"]],
            ("iptables", "OUTPUT"): [["-j", "CODX_LOCKDOWN_OUT"]],
            ("iptables", "FORWARD"): [["-j", "DOCKER-USER"]],
            ("iptables", "DOCKER-USER"): [["-j", "DROP"]],
            ("iptables:nat", "DOCKER"): [],
            ("iptables", "CODX_LOCKDOWN_IN"): [
                ["-i", "lo", "-j", "ACCEPT"],
                [
                    "-p",
                    "tcp",
                    "-m",
                    "tcp",
                    "--dport",
                    "443",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "NEW",
                    "-j",
                    "ACCEPT",
                ],
                ["-p", "tcp", "-j", "DROP"],
            ],
            ("iptables", "CODX_LOCKDOWN_OUT"): [
                ["-o", "lo", "-j", "ACCEPT"]
            ],
        }
        self.fail_predicate = None
        self.failed = False

    def _completed(
        self,
        command: Sequence[str],
        returncode: int = 0,
        stdout: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout,
            "",
        )

    def run(
        self,
        command: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = list(command)
        if (
            self.fail_predicate is not None
            and not self.failed
            and self.fail_predicate(command)
        ):
            self.failed = True
            raise firewall.FirewallError("injected failure")
        binary = command[0]
        if binary not in ("iptables", "ip6tables"):
            raise AssertionError(command)
        assert command[1:3] == ["-w", "5"]
        args = command[3:]
        if args[:2] == ["-t", "nat"]:
            assert args[2] == "-S"
            key = (f"{binary}:nat", args[3])
            if key not in self.chains:
                return self._completed(command, returncode=1)
            output = [f"-N {args[3]}"]
            output.extend(
                f"-A {args[3]} " + " ".join(rule)
                for rule in self.chains[key]
            )
            return self._completed(command, stdout="\n".join(output) + "\n")
        action = args[0]

        if action == "-S":
            key = (binary, args[1])
            if key not in self.chains:
                return self._completed(command, returncode=1)
            output = [f"-N {args[1]}"]
            output.extend(
                f"-A {args[1]} " + " ".join(rule)
                for rule in self.chains[key]
            )
            return self._completed(command, stdout="\n".join(output) + "\n")

        if action == "-N":
            key = (binary, args[1])
            if key in self.chains:
                if check:
                    raise firewall.FirewallError("chain exists")
                return self._completed(command, returncode=1)
            self.chains[key] = []
            return self._completed(command)

        key = (binary, args[1])
        if key not in self.chains:
            if check:
                raise firewall.FirewallError("chain absent")
            return self._completed(command, returncode=1)
        rules = self.chains[key]

        if action == "-F":
            rules.clear()
            return self._completed(command)
        if action == "-A":
            rules.append(args[2:])
            return self._completed(command)
        if action == "-I":
            position = int(args[2]) - 1
            rules.insert(position, args[3:])
            return self._completed(command)
        if action == "-R":
            position = int(args[2]) - 1
            if position >= len(rules):
                raise firewall.FirewallError("replace position absent")
            rules[position] = args[3:]
            return self._completed(command)
        if action == "-C":
            candidate = args[2:]
            return self._completed(
                command,
                returncode=0 if candidate in rules else 1,
            )
        if action == "-D":
            if len(args) == 3 and args[2].isdigit():
                position = int(args[2]) - 1
                if position >= len(rules):
                    raise firewall.FirewallError("delete position absent")
                rules.pop(position)
            else:
                candidate = args[2:]
                try:
                    rules.remove(candidate)
                except ValueError:
                    if check:
                        raise firewall.FirewallError("delete rule absent")
                    return self._completed(command, returncode=1)
            return self._completed(command)
        raise AssertionError(command)


@pytest.fixture
def ipv4_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        firewall.shutil,
        "which",
        lambda binary: None if binary == "ip6tables" else f"/usr/bin/{binary}",
    )
    monkeypatch.setattr(firewall, "_ipv6_globally_disabled", lambda: True)


def _active_targets(fake: FakeRunner) -> dict[str, str]:
    result = {}
    for dispatcher in (
        "CODX_IN_DISPATCH",
        "CODX_OUT_DISPATCH",
        "CODX_DOCKER_DISPATCH",
    ):
        result[dispatcher] = fake.chains[("iptables", dispatcher)][0][1]
    return result


def test_apply_is_idempotent_removes_legacy_443_and_persists_mode(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    mode_file = tmp_path / "mode"

    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_GATE,
        mode_file,
    )
    first_targets = _active_targets(fake)
    firewall.verify_policy(fake, _context(), firewall.MODE_GATE)

    assert fake.chains[("iptables", "INPUT")][0] == [
        "-j",
        "CODX_IN_DISPATCH",
    ]
    assert not any(
        "443" in rule
        for rule in fake.chains[("iptables", "CODX_LOCKDOWN_IN")]
    )
    assert mode_file.read_text(encoding="ascii") == "gate-enabled\n"

    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_GATE,
        mode_file,
    )
    firewall.verify_policy(fake, _context(), firewall.MODE_GATE)
    assert _active_targets(fake) != first_targets
    assert fake.chains[("iptables", "INPUT")].count(
        ["-j", "CODX_IN_DISPATCH"]
    ) == 1
    assert fake.chains[("iptables", "DOCKER-USER")][0] == [
        "-j",
        "CODX_DOCKER_DISPATCH",
    ]


def test_cold_boot_installs_policy_before_docker_adds_forward_jump(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    del fake.chains[("iptables", "DOCKER-USER")]
    fake.chains[("iptables", "FORWARD")] = []
    mode_file = tmp_path / "mode"

    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
        mode_file,
    )

    assert fake.chains[("iptables", "FORWARD")] == []
    assert fake.chains[("iptables", "DOCKER-USER")][0] == [
        "-j",
        "CODX_DOCKER_DISPATCH",
    ]
    active_chain = _active_targets(fake)["CODX_DOCKER_DISPATCH"]
    assert fake.chains[("iptables", active_chain)]
    assert fake.chains[("iptables", active_chain)][-1] == ["-j", "DROP"]
    firewall.verify_pre_docker_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
    )

    # This models Docker's later initialization. The FORWARD hook becomes
    # reachable only after the complete fail-closed DOCKER-USER policy exists.
    fake.run(
        (
            "iptables",
            "-w",
            "5",
            "-I",
            "FORWARD",
            "1",
            "-j",
            "DOCKER-USER",
        )
    )

    assert fake.chains[("iptables", "FORWARD")][0] == [
        "-j",
        "DOCKER-USER",
    ]
    assert fake.chains[("iptables", "DOCKER-USER")][0] == [
        "-j",
        "CODX_DOCKER_DISPATCH",
    ]
    firewall.verify_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
    )


def test_full_verify_rejects_missing_or_nonfirst_docker_forward_hook(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
        tmp_path / "mode",
    )

    fake.chains[("iptables", "FORWARD")] = []
    firewall.verify_pre_docker_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
    )
    with pytest.raises(
        firewall.FirewallError,
        match="Docker FORWARD hook is not first",
    ):
        firewall.verify_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
        )

    fake.chains[("iptables", "FORWARD")] = [
        ["-j", "ACCEPT"],
        ["-j", "DOCKER-USER"],
    ]
    with pytest.raises(
        firewall.FirewallError,
        match="Docker FORWARD hook is not first",
    ):
        firewall.verify_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
        )

    fake.chains[("iptables", "FORWARD")] = [["-j", "DOCKER-USER"]]
    fake.chains[("iptables", "DOCKER-USER")].insert(0, ["-j", "ACCEPT"])
    with pytest.raises(
        firewall.FirewallError,
        match="firewall dispatcher is not first",
    ):
        firewall.verify_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
        )


def test_full_verify_rejects_forbidden_docker_nat_publication(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
        tmp_path / "mode",
    )
    fake.chains[("iptables:nat", "DOCKER")] = [
        [
            "-p",
            "tcp",
            "-m",
            "tcp",
            "--dport",
            "443",
            "-j",
            "DNAT",
            "--to-destination",
            "172.27.0.2:3438",
        ]
    ]

    with pytest.raises(
        firewall.FirewallError,
        match="Docker NAT publishes forbidden TCP 80/443",
    ):
        firewall.verify_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
        )


def test_missing_ip6tables_requires_proven_global_ipv6_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        firewall.shutil,
        "which",
        lambda binary: None if binary == "ip6tables" else f"/usr/bin/{binary}",
    )
    monkeypatch.setattr(
        firewall,
        "_ipv6_globally_disabled",
        lambda: False,
    )

    with pytest.raises(
        firewall.FirewallError,
        match="ip6tables is required",
    ):
        firewall.apply_policy(
            FakeRunner(),
            _context(),
            firewall.MODE_PRODUCTION,
            tmp_path / "mode",
        )


def test_ipv6_ingress_policy_is_fail_closed() -> None:
    rules = firewall.build_ipv6_input_rules()

    assert rules[-2:] == (
        firewall.Rule(
            source="::/0",
            destination="::/0",
            protocol="tcp",
            jump="DROP",
        ),
        firewall.Rule(
            source="::/0",
            destination="::/0",
            protocol="udp",
            jump="DROP",
        ),
    )


def test_systemd_boot_order_has_no_cycle_and_preserves_mode_write(
) -> None:
    firewall_unit = (
        SYSTEMD_DIR / "chebycodex-firewall.service"
    ).read_text(encoding="utf-8")
    docker_drop_in = (
        SYSTEMD_DIR / "docker.service.d/10-chebycodex-firewall.conf"
    ).read_text(encoding="utf-8")

    assert "Before=docker.service" in firewall_unit
    assert "After=docker.service" not in firewall_unit
    assert "Requires=docker.service" not in firewall_unit
    assert (
        "ExecStartPost=/usr/local/sbin/codex-security-lockdown.sh "
        "--verify-pre-docker"
    ) in firewall_unit
    assert "ProtectSystem=full" in firewall_unit
    assert "ReadWritePaths=/etc/chebycodex-firewall" in firewall_unit
    assert "Requires=chebycodex-firewall.service" in docker_drop_in
    assert "After=chebycodex-firewall.service" in docker_drop_in
    assert (
        "ExecStartPost=/usr/local/sbin/codex-security-lockdown.sh --verify"
    ) in docker_drop_in


def test_cold_boot_does_not_create_missing_system_parent_chain(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    del fake.chains[("iptables", "INPUT")]

    with pytest.raises(
        firewall.FirewallError,
        match="required firewall parent chain is absent",
    ):
        firewall.apply_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
            tmp_path / "mode",
        )

    assert ("iptables", "INPUT") not in fake.chains


def test_failed_partial_mode_switch_rolls_back_all_dispatchers(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    mode_file = tmp_path / "mode"
    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_GATE,
        mode_file,
    )
    previous_targets = _active_targets(fake)

    fake.fail_predicate = lambda command: (
        command[0] == "iptables"
        and "-R" in command
        and "CODX_OUT_DISPATCH" in command
    )
    with pytest.raises(firewall.FirewallError, match="injected failure"):
        firewall.apply_policy(
            fake,
            _context(),
            firewall.MODE_PRODUCTION,
            mode_file,
        )

    assert _active_targets(fake) == previous_targets
    assert mode_file.read_text(encoding="ascii") == "gate-enabled\n"
    firewall.verify_policy(fake, _context(), firewall.MODE_GATE)


def test_production_only_switch_removes_gate_from_active_policy(
    tmp_path: Path,
    ipv4_only: None,
) -> None:
    fake = FakeRunner()
    mode_file = tmp_path / "mode"
    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_GATE,
        mode_file,
    )

    firewall.apply_policy(
        fake,
        _context(),
        firewall.MODE_PRODUCTION,
        mode_file,
    )
    firewall.verify_policy(fake, _context(), firewall.MODE_PRODUCTION)

    active_chain = _active_targets(fake)["CODX_DOCKER_DISPATCH"]
    active = firewall.parse_chain(
        "\n".join(
            f"-A {active_chain} " + " ".join(rule)
            for rule in fake.chains[("iptables", active_chain)]
        ),
        active_chain,
    )
    assert not any(
        rule.jump == "ACCEPT"
        and (
            rule.source.startswith("172.31.62.")
            or rule.destination.startswith("172.31.62.")
            or rule.source.startswith("172.31.63.")
            or rule.destination.startswith("172.31.63.")
            or rule.source.startswith("172.31.65.")
            or rule.destination.startswith("172.31.65.")
        )
        for rule in active
    )
    assert mode_file.read_text(encoding="ascii") == "production-only\n"
