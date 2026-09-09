#!/usr/bin/env python3
"""Static, side-effect-free checks for the outbound Connector bundle."""

from __future__ import annotations

import re
import hashlib
import tomllib
from pathlib import Path

import yaml


BUNDLE = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"Turkey Connector bundle: FAIL: {message}")


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
    require(not current, "dependency lock has an incomplete continuation")
    return result


def main() -> None:
    dockerfile = (BUNDLE / "Dockerfile.connector").read_text(encoding="utf-8")
    runner = (BUNDLE / "connector_runner.py").read_text(encoding="utf-8")
    main_source = (BUNDLE.parent.parent / "connector/cheby_connector/main.py").read_text(encoding="utf-8")
    config_source = (BUNDLE.parent.parent / "connector/cheby_connector/config.py").read_text(encoding="utf-8")
    gate_bridge_source = (BUNDLE.parent.parent / "connector/cheby_connector/gate_bridge.py").read_text(encoding="utf-8")
    phonebridge_runtime = (BUNDLE.parent.parent / "connector/cheby_connector/phonebridge_runtime.py").read_text(encoding="utf-8")
    codex_launcher = (BUNDLE / "codex_sandbox_launcher.py").read_text(encoding="utf-8")
    codex_bwrap_wrapper = (BUNDLE / "codex_bwrap_wrapper.py").read_text(encoding="utf-8")
    compose = yaml.safe_load(
        (BUNDLE / "docker-compose.connector.yml").read_text(encoding="utf-8")
    )
    gate_compose = yaml.safe_load(
        (BUNDLE / "docker-compose.connector-gate.yml").read_text(encoding="utf-8")
    )
    relay_edge_compose = yaml.safe_load(
        (BUNDLE.parent / "relay-edge" / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
    )
    installer = (BUNDLE / "install_node_bootstrap.py").read_text(encoding="utf-8")
    seccomp_installer = (BUNDLE / "install_codex_seccomp_profile.py").read_text(
        encoding="utf-8"
    )
    firewall = (BUNDLE / "private_relay_firewall.py").read_text(
        encoding="utf-8"
    )
    firewall_unit = (BUNDLE / "systemd/chebycodex-firewall.service").read_text(
        encoding="utf-8"
    )
    docker_firewall_drop_in = (
        BUNDLE / "systemd/docker.service.d/10-chebycodex-firewall.conf"
    ).read_text(encoding="utf-8")
    requirements = logical_requirements(BUNDLE / "connector-requirements.lock")
    mcp_config = (BUNDLE / "codex-mcp.connector.toml").read_text(encoding="utf-8")
    mcp_value = tomllib.loads(mcp_config)
    phonebridge_source = BUNDLE.parent.parent / "connector/phonebridge/phonebridge.mjs"
    phonebridge_hash = (BUNDLE.parent.parent / "connector/phonebridge/phonebridge.sha256").read_text(encoding="utf-8").split()[0]

    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    require(len(from_lines) == 2, "Connector Dockerfile must have two pinned stages")
    require(
        all("@sha256:" in image for image in from_lines),
        "every base image must be digest-pinned",
    )
    require("--require-hashes" in dockerfile, "pip must enforce dependency hashes")
    require("connector_runner.py" in dockerfile, "hardened Connector runner is required")
    require(
        "COPY --chown=0:0 --chmod=0555 deploy/turkey/codex_sandbox_launcher.py" in dockerfile,
        "reviewed Codex secret-isolation launcher is required",
    )
    require(
        '"--ro-bind"' in codex_launcher
        and '"--dev-bind"' in codex_launcher
        and re.search(r'"--bind",\s*"/proc",\s*"/proc"', codex_launcher)
        and '"/run/secrets"' in codex_launcher
        and '"--ask-for-approval"' in codex_launcher,
        "Codex launcher must isolate secrets while staying headless",
    )
    require(
        "COPY --chown=0:0 --chmod=0555 deploy/turkey/codex_bwrap_wrapper.py /usr/local/bin/bwrap"
        in dockerfile
        and "--add-seccomp-fd" in codex_bwrap_wrapper
        and "DENIED_SYSCALLS = (62, 129, 200, 234, 297, 424)"
        in codex_bwrap_wrapper
        and "AUDIT_ARCH_X86_64" in codex_bwrap_wrapper,
        "Codex native tools must use the reviewed deny-signal sandbox wrapper",
    )
    require("except Exception:" in runner and "import traceback" not in runner
            and "exc_info" not in runner,
            "Connector runner must emit only a generic startup failure")
    require(
        "os.umask(0o077)" in runner
        and runner.index("os.umask(0o077)") < runner.index(
            "from cheby_connector.main import main"
        ),
        "Connector runner must set a private umask before application imports",
    )
    require("codex app-server" not in dockerfile, "Codex must be spawned by the typed adapter")
    require(re.search(r"^EXPOSE\s", dockerfile, re.MULTILINE) is None,
            "Connector image must not declare a listener")
    require("uvicorn" not in dockerfile.lower(), "Connector image must not start Gateway HTTP")
    require("nodejs=24.17.0-r0" in dockerfile,
            "PhoneBridge Node runtime must be exactly pinned")
    require("COPY connector/phonebridge/phonebridge.mjs /opt/phonebridge/phonebridge.mjs" in dockerfile
            and "sha256sum -c phonebridge.sha256" in dockerfile,
            "reviewed PhoneBridge source must be copied and hash-verified inside the image")
    require(hashlib.sha256(phonebridge_source.read_bytes()).hexdigest() == phonebridge_hash,
            "vendored PhoneBridge source does not match its reviewed SHA-256")
    require(phonebridge_hash in phonebridge_runtime,
            "PhoneBridge runtime source gate must pin the reviewed SHA-256")
    require("COPY --chown=0:0 --chmod=0444 deploy/turkey/codex-mcp.connector.toml /app/deploy/codex-mcp.connector.toml" in dockerfile,
            "reviewed MCP config must be installed root-owned and world-readable in the production image")
    require("ensure_codex_mcp_config()" in main_source
            and "validate_codex_mcp_registration()" in main_source,
            "Connector must install and ask the real Codex CLI to validate MCP registration")
    require('settings.runtime_profile == "production"' in main_source,
            "production-only MCP and PhoneBridge startup guard is required")
    require("/data/connector.health" in dockerfile and "st_mtime<20" in dockerfile
            and "os.kill(pid,0)" in dockerfile,
            "healthcheck must verify a fresh main-loop watchdog file")
    require("os.kill(1, 0)" not in dockerfile,
            "PID-only healthchecks cannot detect a stalled event loop")
    require(
        "9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76"
        in seccomp_installer
        and '"v26.1.3/profiles/seccomp/default.json"' in seccomp_installer
        and '"value": CLONE_NEWUSER' in seccomp_installer
        and '["unshare", "mount", "umount2", "pivot_root"]'
        in seccomp_installer,
        "Codex seccomp installer must pin the matching Moby default and narrow rules",
    )

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
        {"cryptography", "pillow", "pydantic", "websockets"}.issubset(packages),
        "the Connector dependency set is incomplete",
    )
    require("fastapi" not in packages and "uvicorn" not in packages,
            "the outbound Connector must not install the public Gateway server stack")
    require(
        "CHEBY_PHONEBRIDGE_BROKER_SOCKET" in mcp_config
        and "CHEBY_PHONEBRIDGE_TOKEN_FILE" not in mcp_config
        and "CHEBY_PHONEBRIDGE_URL" not in mcp_config,
        "Codex MCP config must expose only the allowlisted broker socket",
    )
    require(
        set(mcp_value.get("permissions", {})) == {"cheby_mobile"}
        and mcp_value["permissions"]["cheby_mobile"]["filesystem"]
        == {
            ":minimal": "read",
            "/opt/codex": "read",
            "/usr/local/bin/bwrap": "read",
            ":workspace_roots": {".": "write"},
        }
        and mcp_value["permissions"]["cheby_mobile"]["network"]
        == {"enabled": True},
        "Codex must use the reviewed no-approval credential-isolating profile",
    )

    services = compose.get("services", {})
    require(set(services) == {"connector"}, "outbound Compose must have one service")
    connector = services["connector"]
    require("build" not in connector, "runtime Compose must consume a reviewed image")
    require(connector.get("pull_policy") == "never", "runtime must not pull a mutable image")
    require(connector.get("user") == "10002:10002", "Connector must run non-root")
    require(connector.get("read_only") is True, "Connector root filesystem must be read-only")
    require(connector.get("cap_drop") == ["ALL"], "Connector must drop every capability")
    require(
        connector.get("security_opt")
        == [
            "no-new-privileges:true",
            "seccomp=/etc/chebycodex-connector/codex-bwrap-seccomp.json",
        ],
        "Connector must preserve no-new-privileges and permit the Codex user namespace",
    )
    require(
        connector.get("ports") == ["0.0.0.0:3448:3438/tcp"]
        and "expose" not in connector,
        "Connector may publish only IPv4 host 3448 to mTLS PhoneBridge port 3438",
    )
    require(connector.get("network_mode") != "host", "host networking is forbidden")

    environment = connector.get("environment", {})
    require(
        environment.get("CHEBY_CONNECTOR_RELAY_TOKEN_FILE")
        == "/run/secrets/node_token",
        "Node credential must be loaded from the read-only file",
    )
    require("CHEBY_CONNECTOR_RELAY_TOKEN" not in environment,
            "Node credential must not be supplied through the environment")
    require(environment.get("CHEBY_BRIDGE_MODE") == "stdio",
            "Connector must own Codex through the stdio adapter")
    require(
        environment.get("CHEBY_CODEX_COMMAND")
        == "python /app/deploy/codex_sandbox_launcher.py codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
        "Codex must remain an isolated in-process stdio child using native tools",
    )
    require(environment.get("CHEBY_CONNECTOR_MAX_INBOUND_FRAME_BYTES") == "12582912",
            "Connector inbound frame ceiling must be 12 MiB")
    require(environment.get("CHEBY_CONNECTOR_HEALTH_PATH") == "/data/connector.health",
            "Connector health path must match the image healthcheck")
    require(environment.get("CHEBY_CONNECTOR_HEALTH_INTERVAL_SECONDS") == "5",
            "Connector watchdog must refresh within the image health window")
    require(environment.get("CHEBY_CONNECTOR_IDEMPOTENCY_RETENTION_SECONDS") == "2592000",
            "Connector idempotency retention must be explicit")
    require(environment.get("CHEBY_LOCAL_MCP_DATA_ROOT") == "/home/cheby/.codex/local-mcp",
            "local MCP data must stay inside the private persistent Codex home")
    require(environment.get("CHEBY_PHONEBRIDGE_ENABLED") == "true",
            "PhoneBridge must be supervised inside the Connector container")
    require(environment.get("CHEBY_PHONEBRIDGE_URL") == "http://127.0.0.1:3437",
            "PhoneBridge MCP must use only its same-container loopback endpoint")
    require(environment.get("CHEBY_PHONEBRIDGE_TOKEN_FILE") == "/run/secrets/phonebridge_local_token",
            "PhoneBridge must load its credential from the read-only file")
    require(
        environment.get("CHEBY_PHONEBRIDGE_BROKER_SOCKET")
        == "/home/cheby/.codex/runtime/phonebridge-broker.sock",
        "PhoneBridge MCP must use the allowlisted Connector-owned broker",
    )
    require(environment.get("PHONEBRIDGE_ENV_FILE") == "/run/secrets/phonebridge.env",
            "PhoneBridge runtime must load its private reviewed env file")
    require(environment.get("PHONEBRIDGE_LOCAL_TOKEN_FILE") == "/run/secrets/phonebridge_local_token",
            "PhoneBridge must authenticate the local controller with a file credential")
    require(environment.get("PHONEBRIDGE_LOCAL_TOKEN_CONTROLLER") == "chebycodex",
            "PhoneBridge credential identity must be server-assigned to ChebyCodex")
    require(environment.get("PHONEBRIDGE_MUTATION_CONTROLLER") == "chebycodex",
            "PhoneBridge mutation controller must match its authenticated credential identity")
    require("PHONEBRIDGE_LOCAL_TOKEN" not in environment,
            "PhoneBridge credential must never be supplied through the environment")

    require(
        connector.get("networks", {})
        == {
            "outbound": {"ipv4_address": "172.30.0.2"},
            "relay-backend": {"ipv4_address": "172.31.61.4"},
        },
        "production Connector must use fixed outbound and private Relay identities",
    )
    require(
        environment.get("CHEBY_CONNECTOR_RUNTIME_PROFILE") == "production",
        "production Connector runtime profile must be explicit",
    )
    require(
        environment.get("CHEBY_CONNECTOR_RELAY_URL")
        == "ws://172.31.61.3:8080/relay/v1/node",
        "production Connector must hard-pin the fixed private Relay endpoint",
    )

    mounts = connector.get("volumes", [])
    mount_by_target = {
        item.get("target"): item for item in mounts if isinstance(item, dict)
    }
    for target in (
        "/run/secrets/node_token",
        "/run/secrets/gateway_internal_secret",
        "/run/secrets/phonebridge_local_token",
        "/run/secrets/phonebridge.env",
        "/run/secrets/phonebridge-certs",
    ):
        mount = mount_by_target.get(target)
        require(mount is not None, f"missing private mount {target}")
        require(mount.get("read_only") is True, f"{target} must be read-only")
        require(
            mount.get("bind", {}).get("create_host_path") is False,
            f"{target} must fail closed when its host file is missing",
        )

    for env_name in (".env.connector.example", ".env.connector-gate.example"):
        public_env = (BUNDLE / env_name).read_text(encoding="utf-8")
        for line in public_env.splitlines():
            key, separator, value = line.partition("=")
            if separator and re.search(
                r"TOKEN|SECRET|PASSWORD", key, re.IGNORECASE
            ):
                require(
                    key.endswith("_PATH") or not value,
                    f"{env_name} must not contain a secret value",
                )

    require(
        mcp_config.count('command = "/usr/local/bin/python"') == 3
        and mcp_config.count(
            'args = ["-I", "/app/connector/cheby_connector/local_mcp.py",'
        ) == 3
        and mcp_config.count('cwd = "/app/connector"') == 3
        and "PYTHONPATH" not in mcp_config,
        "Codex MCP config must use an isolated absolute in-image Python entrypoint",
    )
    require(set(re.findall(r"^\[mcp_servers\.([^.\]]+)\]$", mcp_config, re.MULTILINE))
            == {"phonebridge", "offline_memory", "offline_skill"},
            "Codex MCP config must define exactly PhoneBridge, memory, and skill")
    def mcp_server_block(name: str) -> str:
        match = re.search(
            rf"^\[mcp_servers\.{re.escape(name)}\]\s*$"
            rf"(?P<body>.*?)(?=^\[|\Z)",
            mcp_config,
            re.MULTILINE | re.DOTALL,
        )
        require(match is not None, f"missing MCP server block: {name}")
        return match.group("body")

    require(
        all(
            'default_tools_approval_mode = "approve"'
            in mcp_server_block(name)
            for name in ("phonebridge", "offline_memory", "offline_skill")
        )
        and not re.search(r"^\[mcp_servers\.[^.]+\.tools\.", mcp_config, re.MULTILINE),
        "headless single-user MCP servers must run without an unavailable approval UI",
    )
    require("PHONEBRIDGE_LOCAL_TOKEN" not in mcp_config,
            "Codex MCP config must not contain a PhoneBridge token value")
    legacy = (BUNDLE / "README.md").read_text(encoding="utf-8")
    require("SUPERSEDED" in legacy, "legacy Edge bundle must be marked superseded")
    networks = compose.get("networks", {})
    outbound = networks.get("outbound", {})
    require(outbound.get("name") == "chebycodex-turkey-connector_outbound"
            and outbound.get("driver") == "bridge"
            and outbound.get("internal") is False
            and outbound.get("enable_ipv6") is False
            and outbound.get("ipam", {}).get("config") == [{"subnet": "172.30.0.0/28"}],
            "Connector network must have the reviewed dedicated subnet for firewall policy")
    relay_backend = networks.get("relay-backend", {})
    require(
        relay_backend
        == {
            "name": "chebycodex-relay-edge_relay-backend",
            "external": True,
        },
        "production Connector must join the existing internal Relay backend",
    )

    gate_services = gate_compose.get("services", {})
    require(
        set(gate_services) == {"gate-connector"},
        "Gate Compose must contain exactly one Connector service",
    )
    gate = gate_services["gate-connector"]
    require("build" not in gate and gate.get("pull_policy") == "never",
            "Gate must consume a reviewed immutable image")
    require(gate.get("user") == "10002:10002" and gate.get("read_only") is True,
            "Gate must run non-root with a read-only root filesystem")
    require(gate.get("cap_drop") == ["ALL"]
            and "no-new-privileges:true" in gate.get("security_opt", []),
            "Gate must be capability-free and set no-new-privileges")
    require("ports" not in gate and "expose" not in gate
            and gate.get("network_mode") != "host",
            "Gate must publish no port and cannot use host networking")
    require(
        gate.get("networks")
        == {"gate-backend": {"ipv4_address": "172.31.62.4"}},
        "Gate must join only its fixed internal backend identity",
    )
    require(
        gate_compose.get("networks", {}).get("gate-backend")
        == {
            "name": "chebycodex-relay-edge_gate-backend",
            "external": True,
        },
        "Gate must use the existing isolated Relay Gate backend",
    )
    edge_services = relay_edge_compose.get("services", {})
    edge_networks = relay_edge_compose.get("networks", {})
    require(
        edge_services.get("relay", {}).get("networks", {}).get(
            "relay-backend", {}
        ).get("ipv4_address")
        == "172.31.61.3"
        and edge_services.get("relay-gate", {}).get("networks", {}).get(
            "gate-backend", {}
        ).get("ipv4_address")
        == "172.31.62.3",
        "Connector private Relay IP contract must match relay-edge",
    )
    require(
        edge_networks.get("relay-backend", {}).get("name")
        == "chebycodex-relay-edge_relay-backend"
        and edge_networks.get("relay-backend", {}).get("internal") is True
        and edge_networks.get("gate-backend", {}).get("name")
        == "chebycodex-relay-edge_gate-backend"
        and edge_networks.get("gate-backend", {}).get("internal") is True,
        "Connector external networks must resolve to internal relay-edge backends",
    )
    require(
        edge_networks.get("edge-ingress")
        == {
            "name": "chebycodex-relay-edge_edge-ingress",
            "driver": "bridge",
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.60.0/28"}]},
        }
        and edge_networks.get("gate-edge-ingress")
        == {
            "name": "chebycodex-relay-edge_gate-edge-ingress",
            "driver": "bridge",
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.63.0/28"}]},
        },
        "production and Gate Edge ingress bridges must be fixed and L2-separated",
    )
    require(
        edge_networks.get("relay-loopback-publish")
        == {
            "name": "chebycodex-relay-edge_relay-loopback-publish",
            "driver": "bridge",
            "driver_opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.64.0/28"}]},
        }
        and edge_networks.get("gate-relay-loopback-publish")
        == {
            "name": "chebycodex-relay-edge_gate-relay-loopback-publish",
            "driver": "bridge",
            "driver_opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.65.0/28"}]},
        },
        "Relay loopback publication bridges must be fixed and non-masquerading",
    )
    require(
        edge_services.get("edge", {}).get("networks")
        == {
            "edge-ingress": {"ipv4_address": "172.31.60.2"},
            "relay-backend": {"ipv4_address": "172.31.61.2"},
        }
        and edge_services.get("edge-gate", {}).get("networks")
        == {
            "gate-edge-ingress": {"ipv4_address": "172.31.63.2"},
            "gate-backend": {"ipv4_address": "172.31.62.2"},
        },
        "each Edge must join only its own ingress and backend networks",
    )
    require(
        edge_services.get("relay", {}).get("networks")
        == {
            "relay-loopback-publish": {"ipv4_address": "172.31.64.2"},
            "relay-backend": {"ipv4_address": "172.31.61.3"},
        }
        and edge_services.get("relay-gate", {}).get("networks")
        == {
            "gate-relay-loopback-publish": {"ipv4_address": "172.31.65.2"},
            "gate-backend": {"ipv4_address": "172.31.62.3"},
        },
        "each Relay must join only its backend and loopback publication bridge",
    )
    gate_environment = gate.get("environment", {})
    require(
        gate_environment.get("CHEBY_CONNECTOR_RELAY_URL")
        == "ws://172.31.62.3:8080/relay/v1/node",
        "Gate must use the fixed private Gate Relay endpoint",
    )
    require(
        gate_environment.get("CHEBY_CONNECTOR_RUNTIME_PROFILE") == "gate"
        and gate_environment.get("CHEBY_BRIDGE_MODE") == "fake",
        "Gate must use only the scripted fake runtime",
    )
    require(
        gate_environment.get("CHEBY_GATEWAY_DB")
        == "/data/gate-gateway.sqlite3"
        and gate_environment.get("CHEBY_CONNECTOR_STATE")
        == "/data/gate-connector.sqlite3"
        and gate_environment.get("CHEBY_GATE_SCRIPT_STATE")
        == "/data/gate-script-state.json",
        "Gate transport, Gateway, and script state must be independent",
    )
    require(
        gate_environment.get("CHEBY_CONNECTOR_HEALTH_PATH")
        == "/data/connector.health",
        "Gate watchdog path must match the immutable image healthcheck",
    )
    for forbidden in (
        "CODEX_HOME",
        "CHEBY_CODEX_COMMAND",
        "CHEBY_CODEX_CWD",
        "CHEBY_CODEX_EXPECTED_VERSION",
        "CHEBY_LOCAL_MCP_DATA_ROOT",
        "CHEBY_PHONEBRIDGE_ENABLED",
        "CHEBY_PHONEBRIDGE_URL",
        "PHONEBRIDGE_ENV_FILE",
    ):
        require(
            forbidden not in gate_environment,
            f"Gate must not configure production runtime input {forbidden}",
        )
    gate_mounts = {
        item.get("target"): item
        for item in gate.get("volumes", [])
        if isinstance(item, dict)
    }
    require(
        set(gate_mounts)
        == {
            "/data",
            "/run/secrets/node_token",
            "/run/secrets/gateway_internal_secret",
        },
        "Gate may mount only its independent data and credentials",
    )
    for target in (
        "/run/secrets/node_token",
        "/run/secrets/gateway_internal_secret",
    ):
        require(
            gate_mounts[target].get("read_only") is True
            and gate_mounts[target].get("bind", {}).get("create_host_path") is False,
            f"Gate credential {target} must fail closed as a read-only mount",
        )
    require(
        "ScriptedGateCodexBridge" in gate_bridge_source
        and "Remembered:" in gate_bridge_source
        and "Current:" in gate_bridge_source,
        "Gate bridge must provide deterministic marker memory replies",
    )
    require(
        "PRIVATE_RELAY_NODE_URLS" in installer
        and "--relay-url" in installer
        and "private_relay_node_url" in installer,
        "bootstrap installer must support an allowlisted private Relay override",
    )
    for endpoint in (
        "ws://172.31.61.3:8080/relay/v1/node",
        "ws://172.31.62.3:8080/relay/v1/node",
    ):
        require(
            endpoint in config_source and endpoint in installer,
            f"private Relay endpoint {endpoint} must be consistently allowlisted",
        )
    for marker in (
        "CODX_DOCKER_DISPATCH",
        "CODX_IN_DISPATCH",
        "MODE_GATE = \"gate-enabled\"",
        "MODE_PRODUCTION = \"production-only\"",
        "_ensure_parent_chain",
        'slot.binary == "iptables" and slot.parent == "DOCKER-USER"',
        "_remove_legacy_input_443",
        "in_interface=context.public_interface",
        "destination_port=\"8443\"",
        "original_destination_port=\"27461\"",
        "original_destination_port=\"27462\"",
        "PRODUCTION_INGRESS_SUBNET = \"172.31.60.0/28\"",
        "PRODUCTION_INGRESS_EDGE = \"172.31.60.2/32\"",
        "PRODUCTION_BACKEND_EDGE = \"172.31.61.2/32\"",
        "GATE_BACKEND_EDGE = \"172.31.62.2/32\"",
        "GATE_INGRESS_SUBNET = \"172.31.63.0/28\"",
        "GATE_INGRESS_EDGE = \"172.31.63.2/32\"",
        "PRODUCTION_LOOPBACK_SUBNET = \"172.31.64.0/28\"",
        "PRODUCTION_LOOPBACK_RELAY = \"172.31.64.2/32\"",
        "GATE_LOOPBACK_SUBNET = \"172.31.65.0/28\"",
        "GATE_LOOPBACK_RELAY = \"172.31.65.2/32\"",
        'original_destination_port="18080"',
        'original_destination_port="18081"',
        "source=PRODUCTION_CONNECTOR",
        "source=GATE_CONNECTOR",
        "rules.append(_drop())",
        "_include_ipv6_or_fail_closed",
        "_verify_docker_nat_has_no_tcp_80_or_443",
        "--install",
        "--verify",
        "--verify-pre-docker",
        'require_docker_forward_hook: bool = True',
        '"FORWARD",',
        '"DOCKER-USER",',
    ):
        require(
            marker in firewall,
            f"private Relay firewall is missing reviewed marker {marker}",
        )
    require(
        "CHEBY_MCP_HTTPS_PORT" not in firewall,
        "private Relay firewall must not retain the legacy TCP 443 input allow",
    )
    require(
        firewall_unit
        == """[Unit]
Description=ChebyCodex fail-closed host firewall
Wants=network-online.target
After=network-online.target
Before=docker.service

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ConfigurationDirectory=chebycodex-firewall
ConfigurationDirectoryMode=0700
ReadWritePaths=/etc/chebycodex-firewall
ExecStart=/usr/local/sbin/codex-security-lockdown.sh --apply
ExecStartPost=/usr/local/sbin/codex-security-lockdown.sh --verify-pre-docker
RemainAfterExit=yes
TimeoutStartSec=60
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
""",
        "firewall systemd unit must apply and verify before Docker starts",
    )
    require(
        docker_firewall_drop_in
        == """[Unit]
Requires=chebycodex-firewall.service
After=chebycodex-firewall.service

[Service]
ExecStartPost=/usr/local/sbin/codex-security-lockdown.sh --verify
""",
        "Docker must require the firewall and verify its live FORWARD hook",
    )

    ignore = (BUNDLE / "Dockerfile.connector.dockerignore").read_text(
        encoding="utf-8"
    )
    for expected in (
        "!connector/cheby_connector/**",
        "!connector/phonebridge/**",
        "!gateway/cheby_gateway/**",
        "!deploy/turkey/Dockerfile.connector",
        "!deploy/turkey/connector-requirements.lock",
        "!deploy/turkey/connector_runner.py",
        "!deploy/turkey/codex-mcp.connector.toml",
    ):
        require(expected in ignore, f"Docker context allowlist is missing {expected}")

    print("Turkey Connector bundle: PASS")


if __name__ == "__main__":
    main()
