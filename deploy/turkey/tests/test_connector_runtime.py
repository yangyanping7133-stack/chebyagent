from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify_connector_runtime.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_connector_runtime",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_module()


def _container(
    networks,
    *,
    profile="production",
    relay_url=runtime.PRODUCTION_RELAY_URL,
    host_ports=True,
):
    requested = {"3438/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3448"}]}
    active = {"3438/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3448"}]}
    environment = [
        f"CHEBY_CONNECTOR_RUNTIME_PROFILE={profile}",
        f"CHEBY_CONNECTOR_RELAY_URL={relay_url}",
    ]
    if profile == "production":
        environment.append(f"CHEBY_CODEX_COMMAND={runtime.REVIEWED_CODEX_COMMAND}")
    return {
        "Config": {
            "User": "10002:10002",
            "Env": environment,
        },
        "HostConfig": {
            "PortBindings": requested if host_ports else {},
            "SecurityOpt": [
                "no-new-privileges:true",
                runtime.CODEX_SECCOMP_OPTION,
            ],
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
        },
        "NetworkSettings": {
            "Networks": {
                name: {"IPAddress": address}
                for name, address in networks.items()
            },
            "Ports": active if host_ports else {},
        },
    }


def test_production_runtime_contract_is_exact() -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    runtime.verify_network_membership(
        document,
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        },
    )
    runtime.verify_runtime_profile(
        document,
        profile="production",
        relay_url=runtime.PRODUCTION_RELAY_URL,
    )
    runtime.verify_codex_sandbox_host_contract(document)
    runtime.verify_phonebridge_binding(document)
    runtime.verify_routes(
        "default via 172.30.0.1 dev eth0\n",
        "172.31.61.3 dev eth1 src 172.31.61.4 uid 10002\n",
        profile="production",
    )
    runtime.verify_routes(
        "default via 172.30.0.1 dev eth0\n",
        "172.31.61.3 dev eth1 src 172.31.61.4 uid 0\n    cache\n",
        profile="production",
    )


@pytest.mark.parametrize(
    "networks",
    (
        {
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        },
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
            "openclaw": "172.27.0.9",
        },
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.3",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        },
    ),
)
def test_production_network_drift_is_rejected(networks) -> None:
    with pytest.raises(runtime.ConnectorRuntimeError, match="membership differs"):
        runtime.verify_network_membership(
            _container(networks),
            {
                runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
                runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
            },
        )


def test_production_requires_outbound_default_route() -> None:
    with pytest.raises(runtime.ConnectorRuntimeError, match="default route"):
        runtime.verify_routes(
            "default via 172.31.61.1 dev eth1\n",
            "172.31.61.3 dev eth1 src 172.31.61.4 uid 10002\n",
            profile="production",
        )


def test_production_requires_backend_source_for_relay() -> None:
    with pytest.raises(runtime.ConnectorRuntimeError, match="backend identity"):
        runtime.verify_routes(
            "default via 172.30.0.1 dev eth0\n",
            "172.31.61.3 via 172.30.0.1 dev eth0 src 172.30.0.2\n",
            profile="production",
        )


def test_runtime_rejects_code_mode_host_drift() -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    document["Config"]["Env"][-1] = (
        "CHEBY_CODEX_COMMAND=codex app-server --listen stdio://"
    )
    with pytest.raises(runtime.ConnectorRuntimeError, match="tool mode drifted"):
        runtime.verify_runtime_profile(
            document,
            profile="production",
            relay_url=runtime.PRODUCTION_RELAY_URL,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("SecurityOpt", ["no-new-privileges:true"]),
        ("CapDrop", []),
        ("ReadonlyRootfs", False),
    ),
)
def test_codex_sandbox_host_contract_rejects_drift(field, value) -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    document["HostConfig"][field] = value
    with pytest.raises(runtime.ConnectorRuntimeError):
        runtime.verify_codex_sandbox_host_contract(document)


def test_codex_sandbox_host_contract_rejects_root_user() -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    document["Config"]["User"] = "0:0"
    with pytest.raises(runtime.ConnectorRuntimeError, match="non-root"):
        runtime.verify_codex_sandbox_host_contract(document)


def test_codex_sandbox_host_contract_accepts_only_matching_inline_profile() -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    profile = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}
    document["HostConfig"]["SecurityOpt"] = [
        "no-new-privileges:true",
        "seccomp=" + runtime.json.dumps(profile, separators=(",", ":")),
    ]

    runtime.verify_codex_sandbox_host_contract(
        document,
        expected_seccomp_profile=profile,
    )

    with pytest.raises(runtime.ConnectorRuntimeError, match="profile drifted"):
        runtime.verify_codex_sandbox_host_contract(
            document,
            expected_seccomp_profile={**profile, "defaultAction": "SCMP_ACT_KILL"},
        )


def test_codex_seccomp_profile_requires_exact_hash(tmp_path: Path) -> None:
    profile = tmp_path / "seccomp.json"
    payload = b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[]}'
    profile.write_bytes(payload)
    assert runtime.verify_codex_seccomp_profile(
        profile,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    ) == {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}
    with pytest.raises(runtime.ConnectorRuntimeError, match="hash drifted"):
        runtime.verify_codex_seccomp_profile(
            profile,
            expected_sha256="0" * 64,
        )


def test_codex_bubblewrap_runtime_executes_bounded_live_probe(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    runtime.verify_codex_bubblewrap_runtime("connector-id")

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "docker",
        "exec",
        "connector-id",
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
    ]
    assert kwargs["timeout"] == 15.0
    assert kwargs["check"] is True


def test_codex_bubblewrap_runtime_fails_closed_on_timeout(monkeypatch) -> None:
    def time_out(command, **kwargs):
        raise runtime.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runtime.subprocess, "run", time_out)

    with pytest.raises(runtime.ConnectorRuntimeError, match="command failed"):
        runtime.verify_codex_bubblewrap_runtime("connector-id")


def test_codex_permission_profile_runtime_checks_usefulness_and_isolation(monkeypatch) -> None:
    calls = []
    cleanup = []

    def fake_checked_run(command, *, timeout_seconds=30.0):
        calls.append((list(command), timeout_seconds))
        return "4242\n" if len(calls) == 1 else ""

    def fake_cleanup(command, **kwargs):
        cleanup.append((command, kwargs))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(runtime, "_run", fake_checked_run)
    monkeypatch.setattr(runtime.subprocess, "run", fake_cleanup)

    runtime.verify_codex_permission_profile_runtime("connector-id")

    assert len(calls) == 3
    command, timeout = calls[1]
    assert command[:7] == [
        "docker",
        "exec",
        "connector-id",
        "timeout",
        "-s",
        "KILL",
        "15",
    ]
    assert command[7:7 + len(runtime.CODEX_OUTER_BWRAP_COMMAND)] == list(
        runtime.CODEX_OUTER_BWRAP_COMMAND
    )
    assert ("--bind", "/proc", "/proc") == tuple(
        command[
            command.index("/proc") - 1:command.index("/proc") + 2
        ]
    )
    codex_index = 7 + len(runtime.CODEX_OUTER_BWRAP_COMMAND)
    assert command[codex_index:codex_index + 7] == [
        "codex",
        "sandbox",
        "-P",
        "cheby_mobile",
        "-C",
        "/workspace",
        "/bin/sh",
    ]
    script = command[-1]
    assert "test ! -r /home/cheby/.codex/auth.json" in script
    assert "test ! -r /run/secrets/node_token" in script
    assert "test ! -r /proc/4242/status" in script
    assert "kill -0 4242" in script
    assert "printf ok" in script
    assert "socket.create_connection" in script
    assert timeout == 20.0
    assert calls[2][0] == ["docker", "exec", "connector-id", "kill", "-0", "4242"]
    assert cleanup[0][0] == ["docker", "exec", "connector-id", "kill", "4242"]


def test_production_rejects_unknown_route_continuation() -> None:
    with pytest.raises(runtime.ConnectorRuntimeError, match="backend identity"):
        runtime.verify_routes(
            "default via 172.30.0.1 dev eth0\n",
            "172.31.61.3 dev eth1 src 172.31.61.4 uid 0\n"
            "    cache\n"
            "unexpected continuation\n",
            profile="production",
        )


def test_gate_is_backend_only_and_portless() -> None:
    document = _container(
        {runtime.GATE_BACKEND_NETWORK: "172.31.62.4"},
        profile="gate",
        relay_url=runtime.GATE_RELAY_URL,
        host_ports=False,
    )
    runtime.verify_network_membership(
        document,
        {runtime.GATE_BACKEND_NETWORK: "172.31.62.4"},
    )
    runtime.verify_runtime_profile(
        document,
        profile="gate",
        relay_url=runtime.GATE_RELAY_URL,
    )
    runtime.verify_no_ports(document)
    runtime.verify_routes(
        "default via 172.31.62.1 dev eth0\n",
        "172.31.62.3 dev eth0 src 172.31.62.4 uid 10002\n",
        profile="gate",
    )
    runtime.verify_routes(
        "default via 172.31.62.1 dev eth0\n",
        "172.31.62.3 dev eth0 src 172.31.62.4 uid 0\n    cache\n",
        profile="gate",
    )


def test_gate_rejects_codex_command() -> None:
    document = _container(
        {runtime.GATE_BACKEND_NETWORK: "172.31.62.4"},
        profile="gate",
        relay_url=runtime.GATE_RELAY_URL,
        host_ports=False,
    )
    document["Config"]["Env"].append(
        f"CHEBY_CODEX_COMMAND={runtime.REVIEWED_CODEX_COMMAND}"
    )
    with pytest.raises(runtime.ConnectorRuntimeError, match="Codex tool mode"):
        runtime.verify_runtime_profile(
            document,
            profile="gate",
            relay_url=runtime.GATE_RELAY_URL,
        )


def test_gate_rejects_any_second_network() -> None:
    document = _container(
        {
            runtime.GATE_BACKEND_NETWORK: "172.31.62.4",
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
        },
        profile="gate",
        relay_url=runtime.GATE_RELAY_URL,
        host_ports=False,
    )
    with pytest.raises(runtime.ConnectorRuntimeError, match="membership differs"):
        runtime.verify_network_membership(
            document,
            {runtime.GATE_BACKEND_NETWORK: "172.31.62.4"},
        )


def test_gate_rejects_port_binding_even_without_active_runtime_port() -> None:
    document = _container(
        {runtime.GATE_BACKEND_NETWORK: "172.31.62.4"},
        profile="gate",
        relay_url=runtime.GATE_RELAY_URL,
        host_ports=False,
    )
    document["HostConfig"]["PortBindings"] = {
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}],
    }
    with pytest.raises(runtime.ConnectorRuntimeError, match="no port"):
        runtime.verify_no_ports(document)


@pytest.mark.parametrize(
    "active",
    (
        [{"HostIp": "0.0.0.0", "HostPort": "3449"}],
        [{"HostIp": "::", "HostPort": "3448"}],
        [
            {"HostIp": "0.0.0.0", "HostPort": "3448"},
            {"HostIp": "::", "HostPort": "3448"},
        ],
    ),
)
def test_phonebridge_mapping_drift_is_rejected(active) -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    document["NetworkSettings"]["Ports"]["3438/tcp"] = active
    with pytest.raises(runtime.ConnectorRuntimeError, match="active mapping"):
        runtime.verify_phonebridge_binding(document)


def test_duplicate_runtime_url_is_rejected_without_disclosing_it() -> None:
    document = _container(
        {
            runtime.PRODUCTION_OUTBOUND_NETWORK: "172.30.0.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.4",
        }
    )
    document["Config"]["Env"].append(
        f"CHEBY_CONNECTOR_RELAY_URL={runtime.PRODUCTION_RELAY_URL}"
    )
    with pytest.raises(
        runtime.ConnectorRuntimeError,
        match="must occur exactly once",
    ):
        runtime.verify_runtime_profile(
            document,
            profile="production",
            relay_url=runtime.PRODUCTION_RELAY_URL,
        )


def test_outbound_network_must_be_non_internal_and_single_tenant() -> None:
    network = {
        "Name": runtime.PRODUCTION_OUTBOUND_NETWORK,
        "Driver": "bridge",
        "Internal": True,
        "EnableIPv6": False,
        "IPAM": {"Config": [{"Subnet": "172.30.0.0/28"}]},
        "Containers": {
            "connector": {"IPv4Address": "172.30.0.2/28"},
        },
    }
    with pytest.raises(runtime.ConnectorRuntimeError, match="reviewed contract"):
        runtime.verify_network_contract(
            network,
            name=runtime.PRODUCTION_OUTBOUND_NETWORK,
            subnet="172.30.0.0/28",
            internal=False,
            only_container=("connector", "172.30.0.2"),
        )


def test_backend_network_requires_exact_edge_relay_connector_membership() -> None:
    network = {
        "Name": runtime.PRODUCTION_BACKEND_NETWORK,
        "Driver": "bridge",
        "Internal": True,
        "EnableIPv6": False,
        "IPAM": {"Config": [{"Subnet": "172.31.61.0/28"}]},
        "Containers": {
            "edge": {"IPv4Address": "172.31.61.2/28"},
            "relay": {"IPv4Address": "172.31.61.3/28"},
            "connector": {"IPv4Address": "172.31.61.4/28"},
        },
    }
    expected = frozenset({"172.31.61.2", "172.31.61.3", "172.31.61.4"})
    runtime.verify_network_contract(
        network,
        name=runtime.PRODUCTION_BACKEND_NETWORK,
        subnet="172.31.61.0/28",
        internal=True,
        exact_container_addresses=expected,
    )

    network["Containers"]["rogue"] = {"IPv4Address": "172.31.61.5/28"}
    with pytest.raises(
        runtime.ConnectorRuntimeError,
        match="membership differs",
    ):
        runtime.verify_network_contract(
            network,
            name=runtime.PRODUCTION_BACKEND_NETWORK,
            subnet="172.31.61.0/28",
            internal=True,
            exact_container_addresses=expected,
        )
