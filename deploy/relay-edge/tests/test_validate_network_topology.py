from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_bundle.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "relay_edge_validate_bundle",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_module()


def _network(
    name: str,
    subnet: str,
    *,
    internal: bool,
    masquerade: bool | None = None,
):
    value = {
        "name": name,
        "driver": "bridge",
        "internal": internal,
        "enable_ipv6": False,
        "ipam": {"config": [{"subnet": subnet}]},
    }
    if masquerade is not None:
        value["driver_opts"] = {
            "com.docker.network.bridge.enable_ip_masquerade": (
                "true" if masquerade else "false"
            ),
        }
    if internal:
        value.pop("driver")
    return value


def _reviewed_compose():
    return {
        "services": {
            "edge": {
                "networks": {
                    "edge-ingress": {"ipv4_address": "172.31.60.2"},
                    "relay-backend": {"ipv4_address": "172.31.61.2"},
                },
            },
            "relay": {
                "networks": {
                    "relay-loopback-publish": {
                        "ipv4_address": "172.31.64.2",
                    },
                    "relay-backend": {"ipv4_address": "172.31.61.3"},
                },
            },
            "edge-gate": {
                "networks": {
                    "gate-edge-ingress": {"ipv4_address": "172.31.63.2"},
                    "gate-backend": {"ipv4_address": "172.31.62.2"},
                },
            },
            "relay-gate": {
                "networks": {
                    "gate-relay-loopback-publish": {
                        "ipv4_address": "172.31.65.2",
                    },
                    "gate-backend": {"ipv4_address": "172.31.62.3"},
                },
            },
        },
        "networks": {
            "edge-ingress": _network(
                "chebycodex-relay-edge_edge-ingress",
                "172.31.60.0/28",
                internal=False,
            ),
            "relay-backend": _network(
                "chebycodex-relay-edge_relay-backend",
                "172.31.61.0/28",
                internal=True,
            ),
            "relay-loopback-publish": _network(
                "chebycodex-relay-edge_relay-loopback-publish",
                "172.31.64.0/28",
                internal=False,
                masquerade=False,
            ),
            "gate-backend": _network(
                "chebycodex-relay-edge_gate-backend",
                "172.31.62.0/28",
                internal=True,
            ),
            "gate-relay-loopback-publish": _network(
                "chebycodex-relay-edge_gate-relay-loopback-publish",
                "172.31.65.0/28",
                internal=False,
                masquerade=False,
            ),
            "gate-edge-ingress": _network(
                "chebycodex-relay-edge_gate-edge-ingress",
                "172.31.63.0/28",
                internal=False,
            ),
        },
    }


def test_static_topology_accepts_only_six_reviewed_bridges() -> None:
    validator.validate_network_topology(_reviewed_compose())


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["services"]["relay"]["networks"].update(
            {"edge-ingress": {"ipv4_address": "172.31.60.3"}}
        ),
        lambda value: value["networks"]["relay-loopback-publish"][
            "driver_opts"
        ].update(
            {"com.docker.network.bridge.enable_ip_masquerade": "true"}
        ),
        lambda value: value["networks"]["gate-relay-loopback-publish"].update(
            {"enable_ipv6": True}
        ),
        lambda value: value["networks"].update(
            {
                "shared": _network(
                    "shared",
                    "172.31.66.0/28",
                    internal=False,
                ),
            }
        ),
    ),
)
def test_static_topology_rejects_publication_or_isolation_drift(mutate) -> None:
    compose = copy.deepcopy(_reviewed_compose())
    mutate(compose)
    with pytest.raises(SystemExit, match="relay-edge bundle: FAIL"):
        validator.validate_network_topology(compose)
