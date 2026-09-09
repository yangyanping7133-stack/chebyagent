from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path
import sys
from types import ModuleType

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "verify_runtime_networks.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_runtime_networks",
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
    runtime_binding=True,
    *,
    bind_ip="192.168.0.43",
    host_port=27461,
    container_port=8443,
):
    binding = [{"HostIp": bind_ip, "HostPort": str(host_port)}]
    port_key = f"{container_port}/tcp"
    return {
        "HostConfig": {"PortBindings": {port_key: binding}},
        "NetworkSettings": {
            "Networks": {
                name: {"IPAddress": address}
                for name, address in networks.items()
            },
            "Ports": {
                "80/tcp": None,
                port_key: binding if runtime_binding else None,
            },
        },
    }


def test_runtime_contract_requires_distinct_ingress_and_backend() -> None:
    document = _container(
        {
            runtime.PRODUCTION_INGRESS_NETWORK: "172.31.60.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.2",
        }
    )
    runtime.verify_container_networks(
        document,
        {
            runtime.PRODUCTION_INGRESS_NETWORK: "172.31.60.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.2",
        },
    )
    runtime.verify_edge_port_binding(
        document,
        bind_ip="192.168.0.43",
        host_port=27461,
    )


def test_hostconfig_without_runtime_port_is_rejected() -> None:
    document = _container(
        {
            runtime.PRODUCTION_INGRESS_NETWORK: "172.31.60.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.2",
        },
        runtime_binding=False,
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="runtime port publication is absent",
    ):
        runtime.verify_edge_port_binding(
            document,
            bind_ip="192.168.0.43",
            host_port=27461,
        )


def test_relay_attached_to_ingress_is_rejected() -> None:
    relay = _container(
        {
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.3",
            runtime.PRODUCTION_INGRESS_NETWORK: "172.31.60.3",
        }
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="membership differs",
    ):
        runtime.verify_container_networks(
            relay,
            {runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.3"},
        )


def test_relay_loopback_publication_is_exact() -> None:
    relay = _container(
        {
            runtime.PRODUCTION_LOOPBACK_NETWORK: "172.31.64.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.3",
        },
        bind_ip="127.0.0.1",
        host_port=18080,
        container_port=8080,
    )
    runtime.verify_container_networks(
        relay,
        {
            runtime.PRODUCTION_LOOPBACK_NETWORK: "172.31.64.2",
            runtime.PRODUCTION_BACKEND_NETWORK: "172.31.61.3",
        },
    )
    runtime.verify_relay_loopback_binding(relay, host_port=18080)


def test_exact_dnat_requires_bind_port_and_ingress_address() -> None:
    rules = (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp -d 192.168.0.43/32 "
        "--dport 27461 -j DNAT --to-destination 172.31.60.2:8443\n"
    )
    assert runtime.has_exact_dnat(
        rules,
        bind_ip="192.168.0.43",
        host_port=27461,
        ingress_ip="172.31.60.2",
    )
    assert not runtime.has_exact_dnat(
        rules,
        bind_ip="192.168.0.43",
        host_port=27462,
        ingress_ip="172.31.60.2",
    )
    assert not runtime.has_exact_dnat(
        rules,
        bind_ip="192.168.0.43",
        host_port=27461,
        ingress_ip="172.31.61.2",
    )
    runtime.verify_exact_dnat_set(
        rules,
        expected={
            ("192.168.0.43", 27461): ("172.31.60.2", 8443),
        },
    )


def test_exact_dnat_accepts_every_match_group_order_including_real_docker_order() -> None:
    groups = (
        ("!", "-i", "br-0123456789ab"),
        ("-p", "tcp"),
        ("-m", "tcp"),
        ("-d", "127.0.0.1/32"),
        ("--dport", "18080"),
        ("-j", "DNAT"),
        ("--to-destination", "172.31.64.2:8080"),
    )
    expected = {
        ("127.0.0.1", 18080): ("172.31.64.2", 8080),
    }
    for permutation in itertools.permutations(groups):
        flattened = [token for group in permutation for token in group]
        line = " ".join(("-A", "DOCKER", *flattened))
        runtime.verify_exact_dnat_set(line, expected=expected)


@pytest.mark.parametrize(
    "duplicate",
    (
        ("!", "-i", "br-fedcba987654"),
        ("-p", "tcp"),
        ("-m", "tcp"),
        ("-d", "127.0.0.1/32"),
        ("--dport", "18080"),
        ("-j", "DNAT"),
        ("--to-destination", "172.31.64.2:8080"),
    ),
)
def test_exact_dnat_rejects_every_duplicate_match(duplicate: tuple[str, ...]) -> None:
    groups = (
        ("-d", "127.0.0.1/32"),
        ("!", "-i", "br-0123456789ab"),
        ("-p", "tcp"),
        ("-m", "tcp"),
        ("--dport", "18080"),
        ("-j", "DNAT"),
        ("--to-destination", "172.31.64.2:8080"),
        duplicate,
    )
    line = " ".join(
        ("-A", "DOCKER", *(token for group in groups for token in group))
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="restricted or drifted match",
    ):
        runtime.verify_exact_dnat_set(
            line,
            expected={
                ("127.0.0.1", 18080): ("172.31.64.2", 8080),
            },
        )


def test_relevance_scan_cannot_hide_a_protected_port_behind_a_duplicate() -> None:
    rules = (
        "-A DOCKER -d 198.51.100.9/32 ! -i br-0123456789ab "
        "-p tcp -m tcp --dport 29999 --dport 18080 "
        "-j DNAT --to-destination 172.31.99.2:8080\n"
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="restricted or drifted match",
    ):
        runtime.verify_exact_dnat_set(
            rules,
            expected={
                ("127.0.0.1", 18080): ("172.31.64.2", 8080),
            },
        )


@pytest.mark.parametrize(
    "extra",
    (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 192.168.0.43/32 --dport 27461 "
        "-j DNAT --to-destination 172.31.60.3:8443\n",
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 192.168.0.43/32 --dport 29999 "
        "-j DNAT --to-destination 172.31.60.2:8443\n",
    ),
)
def test_parallel_dnat_mapping_is_rejected(extra: str) -> None:
    base = (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 192.168.0.43/32 --dport 27461 "
        "-j DNAT --to-destination 172.31.60.2:8443\n"
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="DNAT (set differs|contains a restricted)",
    ):
        runtime.verify_exact_dnat_set(
            base + extra,
            expected={
                ("192.168.0.43", 27461): ("172.31.60.2", 8443),
            },
        )


@pytest.mark.parametrize(
    "restricted",
    (
        "-s 198.51.100.7/32 ",
        "-m comment --comment drift ",
        "-m conntrack --ctstate NEW ",
    ),
)
def test_restricted_dnat_matches_are_rejected(restricted: str) -> None:
    rules = (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        f"{restricted}-d 192.168.0.43/32 --dport 27461 "
        "-j DNAT --to-destination 172.31.60.2:8443\n"
    )
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="restricted or drifted match",
    ):
        runtime.verify_exact_dnat_set(
            rules,
            expected={
                ("192.168.0.43", 27461): ("172.31.60.2", 8443),
            },
        )


@pytest.mark.parametrize(
    "drift",
    (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 198.51.100.9/32 --dport 27461 "
        "-j DNAT --to-destination 172.31.99.2:8443\n",
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "--dport 27461 -j DNAT --to-destination 172.31.99.2:8443\n",
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 127.0.0.2/32 --dport 18081 "
        "-j DNAT --to-destination 172.31.99.2:8080\n",
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-m multiport --dports 18080,29999 "
        "-j DNAT --to-destination 172.31.99.2:8080\n",
    ),
)
def test_any_protected_host_port_dnat_drift_is_rejected(drift: str) -> None:
    with pytest.raises(
        runtime.RuntimeNetworkError,
        match="DNAT (set differs|contains a restricted)",
    ):
        runtime.verify_exact_dnat_set(
            drift,
            expected={
                ("192.168.0.43", 27461): ("172.31.60.2", 8443),
                ("127.0.0.1", 18080): ("172.31.64.2", 8080),
            },
        )


def test_edge_and_loopback_dnat_must_both_be_singular() -> None:
    rules = (
        "-A DOCKER ! -i br-0123456789ab -p tcp -m tcp "
        "-d 192.168.0.43/32 --dport 27461 "
        "-j DNAT --to-destination 172.31.60.2:8443\n"
        "-A DOCKER ! -i br-fedcba987654 -p tcp -m tcp "
        "-d 127.0.0.1/32 --dport 18080 "
        "-j DNAT --to-destination 172.31.64.2:8080\n"
    )
    runtime.verify_exact_dnat_set(
        rules,
        expected={
            ("192.168.0.43", 27461): ("172.31.60.2", 8443),
            ("127.0.0.1", 18080): ("172.31.64.2", 8080),
        },
    )
    with pytest.raises(runtime.RuntimeNetworkError, match="DNAT set differs"):
        runtime.verify_exact_dnat_set(
            rules.splitlines(keepends=True)[0],
            expected={
                ("192.168.0.43", 27461): ("172.31.60.2", 8443),
                ("127.0.0.1", 18080): ("172.31.64.2", 8080),
            },
        )


def test_route_contract_requires_ingress_default_and_backend_relay_source() -> None:
    runtime.verify_edge_routes(
        "default via 172.31.60.1 dev eth0\n",
        "172.31.61.3 dev eth1 src 172.31.61.2 uid 101\n",
        ingress_gateway="172.31.60.1",
        backend_source="172.31.61.2",
    )
    runtime.verify_edge_routes(
        "default via 172.31.60.1 dev eth0\n",
        "172.31.61.3 dev eth1 src 172.31.61.2 uid 0\n    cache\n",
        ingress_gateway="172.31.60.1",
        backend_source="172.31.61.2",
    )
    with pytest.raises(runtime.RuntimeNetworkError, match="default route"):
        runtime.verify_edge_routes(
            "default via 172.31.61.1 dev eth1\n",
            "172.31.61.3 dev eth1 src 172.31.61.2 uid 101\n",
            ingress_gateway="172.31.60.1",
            backend_source="172.31.61.2",
        )
    with pytest.raises(runtime.RuntimeNetworkError, match="backend identity"):
        runtime.verify_edge_routes(
            "default via 172.31.60.1 dev eth0\n",
            "172.31.61.3 dev eth1 src 172.31.61.2 uid 0\n"
            "    cache\n"
            "unexpected continuation\n",
            ingress_gateway="172.31.60.1",
            backend_source="172.31.61.2",
        )


def test_ingress_network_rejects_a_second_container() -> None:
    network = {
        "Name": runtime.PRODUCTION_INGRESS_NETWORK,
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "IPAM": {"Config": [{"Subnet": "172.31.60.0/28"}]},
        "Containers": {
            "edge": {"IPv4Address": "172.31.60.2/28"},
            "rogue": {"IPv4Address": "172.31.60.3/28"},
        },
    }
    with pytest.raises(runtime.RuntimeNetworkError, match="only its reviewed container"):
        runtime.verify_network(
            network,
            name=runtime.PRODUCTION_INGRESS_NETWORK,
            subnet="172.31.60.0/28",
            internal=False,
            only_container=("edge", "172.31.60.2"),
        )


def test_loopback_publication_requires_no_masquerade() -> None:
    network = {
        "Name": runtime.PRODUCTION_LOOPBACK_NETWORK,
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "Options": {"com.docker.network.bridge.enable_ip_masquerade": "true"},
        "IPAM": {"Config": [{"Subnet": "172.31.64.0/28"}]},
        "Containers": {
            "relay": {"IPv4Address": "172.31.64.2/28"},
        },
    }
    with pytest.raises(runtime.RuntimeNetworkError, match="reviewed contract"):
        runtime.verify_network(
            network,
            name=runtime.PRODUCTION_LOOPBACK_NETWORK,
            subnet="172.31.64.0/28",
            internal=False,
            only_container=("relay", "172.31.64.2"),
            options=runtime.NO_MASQUERADE,
        )


def test_loopback_publication_allows_unrelated_docker_default_options() -> None:
    network = {
        "Name": runtime.PRODUCTION_LOOPBACK_NETWORK,
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": False,
        "Options": {
            "com.docker.network.bridge.enable_ip_masquerade": "false",
            "com.docker.network.enable_ipv4": "true",
        },
        "IPAM": {"Config": [{"Subnet": "172.31.64.0/28"}]},
        "Containers": {
            "relay": {"IPv4Address": "172.31.64.2/28"},
        },
    }
    runtime.verify_network(
        network,
        name=runtime.PRODUCTION_LOOPBACK_NETWORK,
        subnet="172.31.64.0/28",
        internal=False,
        only_container=("relay", "172.31.64.2"),
        options=runtime.NO_MASQUERADE,
    )


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class _Opener:
    def __init__(self, response: _Response):
        self.response = response

    def open(self, request, timeout: int):
        assert request.full_url == "http://127.0.0.1:18080/healthz"
        assert timeout == 3
        return self.response


def test_loopback_health_rejects_oversized_response() -> None:
    with pytest.raises(runtime.RuntimeNetworkError, match="oversized"):
        runtime.verify_loopback_health(
            18080,
            opener=_Opener(_Response(b"x" * 4097)),
        )
    runtime.verify_loopback_health(
        18080,
        opener=_Opener(_Response(b'{"ok":true}')),
    )


def test_ingress_network_rejects_ipv6_enablement() -> None:
    network = {
        "Name": runtime.PRODUCTION_INGRESS_NETWORK,
        "Driver": "bridge",
        "Internal": False,
        "EnableIPv6": True,
        "IPAM": {"Config": [{"Subnet": "172.31.60.0/28"}]},
        "Containers": {
            "edge": {"IPv4Address": "172.31.60.2/28"},
        },
    }
    with pytest.raises(runtime.RuntimeNetworkError, match="reviewed contract"):
        runtime.verify_network(
            network,
            name=runtime.PRODUCTION_INGRESS_NETWORK,
            subnet="172.31.60.0/28",
            internal=False,
            only_container=("edge", "172.31.60.2"),
        )
