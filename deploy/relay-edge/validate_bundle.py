#!/usr/bin/env python3
"""Static, side-effect-free gate for the isolated Relay Edge bundle."""

from __future__ import annotations

import importlib.util
import ipaddress
from pathlib import Path
import re
import sys


BUNDLE = Path(__file__).resolve().parent
REPOSITORY = BUNDLE.parents[1]
PUBLIC_IP = ipaddress.ip_address("192.0.0.9")
sys.dont_write_bytecode = True


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"relay-edge bundle: FAIL: {message}")


def load_certificate_manager():
    spec = importlib.util.spec_from_file_location(
        "relay_edge_certificate_manager", BUNDLE / "certificate_manager.py"
    )
    require(spec is not None and spec.loader is not None, "cannot load certificate manager")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_network_topology(compose: dict[str, object]) -> None:
    services = compose.get("services", {})
    networks = compose.get("networks", {})
    require(
        isinstance(services, dict) and isinstance(networks, dict),
        "Compose network topology is malformed",
    )
    require(
        services["edge"].get("networks")
        == {
            "edge-ingress": {"ipv4_address": "172.31.60.2"},
            "relay-backend": {"ipv4_address": "172.31.61.2"},
        },
        "production Edge must join only its dedicated ingress and backend identities",
    )
    require(
        services["edge-gate"].get("networks")
        == {
            "gate-edge-ingress": {"ipv4_address": "172.31.63.2"},
            "gate-backend": {"ipv4_address": "172.31.62.2"},
        },
        "Gate Edge must join only its dedicated ingress and backend identities",
    )
    require(
        services["relay"].get("networks")
        == {
            "relay-loopback-publish": {"ipv4_address": "172.31.64.2"},
            "relay-backend": {"ipv4_address": "172.31.61.3"},
        }
        and services["relay-gate"].get("networks")
        == {
            "gate-relay-loopback-publish": {"ipv4_address": "172.31.65.2"},
            "gate-backend": {"ipv4_address": "172.31.62.3"},
        },
        "Relay services must join only their backend and loopback publication bridges",
    )
    expected_networks = {
        "edge-ingress": {
            "name": "chebycodex-relay-edge_edge-ingress",
            "driver": "bridge",
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.60.0/28"}]},
        },
        "relay-backend": {
            "name": "chebycodex-relay-edge_relay-backend",
            "internal": True,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.61.0/28"}]},
        },
        "relay-loopback-publish": {
            "name": "chebycodex-relay-edge_relay-loopback-publish",
            "driver": "bridge",
            "driver_opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.64.0/28"}]},
        },
        "gate-backend": {
            "name": "chebycodex-relay-edge_gate-backend",
            "internal": True,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.62.0/28"}]},
        },
        "gate-relay-loopback-publish": {
            "name": "chebycodex-relay-edge_gate-relay-loopback-publish",
            "driver": "bridge",
            "driver_opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.65.0/28"}]},
        },
        "gate-edge-ingress": {
            "name": "chebycodex-relay-edge_gate-edge-ingress",
            "driver": "bridge",
            "internal": False,
            "enable_ipv6": False,
            "ipam": {"config": [{"subnet": "172.31.63.0/28"}]},
        },
    }
    require(
        networks == expected_networks,
        "Compose network set differs from the six reviewed fixed bridges",
    )


def main() -> None:
    import yaml

    compose_text = (BUNDLE / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    services = compose.get("services", {})
    require(
        set(services)
        == {
            "relay",
            "edge",
            "relay-gate",
            "edge-gate",
            "relay-admin",
            "cert-tool",
            "cert-consumer",
            "tls-monitor",
        },
        "unexpected Compose service set",
    )
    require("certbot" not in compose_text.lower(), "public-CA tooling is forbidden")
    require(
        not re.search(r'["\'](?:[^"\']*:)?(?:80|443):', compose_text),
        "TCP 80/443 must never be host-published",
    )
    require(
        services["edge"]["ports"]
        == [
            "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:"
            "${CHEBY_RELAY_PUBLIC_PORT:?set 27461}:8443/tcp"
        ],
        "production Edge port mapping drifted",
    )
    require(
        services["edge-gate"]["ports"]
        == [
            "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:"
            "${CHEBY_RELAY_GATE_PORT:?set 27462}:8443/tcp"
        ],
        "Gate Edge port mapping drifted",
    )
    require(
        services["edge-gate"].get("profiles") == ["gate"]
        and services["relay-gate"].get("profiles") == ["gate"],
        "Gate services must remain opt-in",
    )
    require(
        services["relay"]["ports"]
        == [
            "127.0.0.1:${CHEBY_RELAY_LOOPBACK_PORT:"
            "?set the Connector loopback port}:8080/tcp"
        ],
        "production Relay must retain loopback-only publication",
    )
    require(
        services["relay"]["environment"]["CHEBY_RELAY_TRUSTED_PROXY_CIDRS"]
        == "172.31.61.2/32",
        "production Relay must trust only its exact Edge address",
    )
    require(
        services["relay"]["environment"]["CHEBY_RELAY_ALLOWED_HOSTS"]
        == (
            "${CHEBY_PUBLIC_IP:?set the fixed public IPv4 address},"
            "${CHEBY_RELAY_LEGACY_HOST:-},172.31.61.3,localhost,127.0.0.1"
        ),
        "production Relay Host allowlist must cover only fixed-IP, migration, "
        "internal Connector, and loopback identities",
    )
    require(
        services["relay-gate"]["environment"]["CHEBY_RELAY_TRUSTED_PROXY_CIDRS"]
        == "172.31.62.2/32",
        "Gate Relay must trust only its exact Edge address",
    )
    require(
        services["relay-gate"]["environment"]["CHEBY_RELAY_ALLOWED_HOSTS"]
        == (
            "${CHEBY_PUBLIC_IP:?set the fixed public IPv4 address},"
            "172.31.62.3,localhost,127.0.0.1"
        ),
        "Gate Relay Host allowlist must cover only fixed-IP, internal Connector, "
        "and loopback identities",
    )
    require(
        services["relay-gate"]["environment"].get(
            "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS"
        )
        == "43200",
        "Gate Relay must retain acknowledged payloads through the 8-hour audit",
    )
    require(
        "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS"
        not in services["relay"]["environment"],
        "production Relay must retain its normal bounded payload lifetime",
    )
    validate_network_topology(compose)
    for name in (
        "relay",
        "edge",
        "relay-gate",
        "edge-gate",
        "relay-admin",
        "cert-tool",
        "cert-consumer",
    ):
        service = services[name]
        require(service.get("read_only") is True, f"{name} filesystem must be read-only")
        require(service.get("cap_drop") == ["ALL"], f"{name} must drop all capabilities")
        require(
            "no-new-privileges:true" in service.get("security_opt", []),
            f"{name} must set no-new-privileges",
        )
    require(
        services["cert-tool"].get("network_mode") == "none"
        and services["cert-consumer"].get("network_mode") == "none",
        "certificate importer and consumer must have no network",
    )
    require(
        services["tls-monitor"].get("network_mode") == "host",
        "public monitor must test the actual host-published endpoint",
    )
    require(
        services["cert-tool"].get("user") == "101:101"
        and services["cert-consumer"].get("user") == "101:101"
        and services["tls-monitor"].get("user") == "101:101"
        and services["edge"].get("user") == "101:101",
        "TLS importer, consumer, monitor, and Edge must share only the isolated TLS UID",
    )
    consumer_mounts = {
        volume["target"]: volume
        for volume in services["cert-consumer"].get("volumes", [])
        if isinstance(volume, dict) and isinstance(volume.get("target"), str)
    }
    require(
        consumer_mounts.get("/tls", {}).get("read_only") is True
        and consumer_mounts.get("/staging", {}).get("read_only") is False,
        "certificate consumer must mount only TLS read-only and staging writable",
    )
    importer_mounts = {
        volume["target"]: volume
        for volume in services["cert-tool"].get("volumes", [])
        if isinstance(volume, dict) and isinstance(volume.get("target"), str)
    }
    require(
        importer_mounts.get("/staging", {}).get("read_only") is True,
        "certificate importer must retain read-only staging",
    )
    runtime_verifier = (BUNDLE / "verify_runtime_networks.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "NetworkSettings",
        "PortBindings",
        "EnableIPv6",
        "iptables-save",
        "PRODUCTION_INGRESS_NETWORK",
        "PRODUCTION_LOOPBACK_NETWORK",
        "GATE_INGRESS_NETWORK",
        "GATE_LOOPBACK_NETWORK",
        "runtime port publication is absent",
        "verify_relay_loopback_binding",
        "Relay loopback health is unreachable",
        "restricted or drifted match",
    ):
        require(
            marker in runtime_verifier,
            f"runtime Docker network verifier is missing: {marker}",
        )
    connector_runtime_verifier = (
        REPOSITORY / "deploy/turkey/verify_connector_runtime.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "PRODUCTION_OUTBOUND_NETWORK",
        "GATE_BACKEND_NETWORK",
        "verify_phonebridge_binding",
        "verify_no_ports",
        "verify_namespace_routes",
        "CHEBY_CONNECTOR_RELAY_URL",
    ):
        require(
            marker in connector_runtime_verifier,
            f"Connector runtime verifier is missing: {marker}",
        )

    dockerfile = (BUNDLE / "Dockerfile.edge").read_text(encoding="utf-8")
    require(
        all(
            "@sha256:" in image
            for image in re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
        ),
        "Edge base image must be digest-pinned",
    )
    require("USER 101:101" in dockerfile, "Edge image must run as non-root UID 101")
    package_block = re.search(
        r"RUN /sbin/apk add --no-cache --upgrade \\\n(?P<body>.*?)\n\s*&& rm",
        dockerfile,
        re.DOTALL,
    )
    require(package_block is not None, "Edge patched package block is missing")
    package_pins = set(
        re.findall(
            r"^\s*([a-z0-9+_.-]+=[^\s\\]+)\s*\\$",
            package_block.group("body"),
            re.MULTILINE,
        )
    )
    require(
        package_pins
        == {
            "c-ares=1.34.8-r0",
            "curl=8.20.0-r0",
            "libcurl=8.20.0-r0",
            "libexpat=2.8.2-r0",
        },
        "Edge patched package pins drifted",
    )

    template = (BUNDLE / "edge/nginx.conf.template").read_text(encoding="utf-8")
    require(
        re.search(r"\blisten\s+(?:[^; ]+:)?(?:80|443)\b", template) is None,
        "Nginx template contains a forbidden TCP 80/443 listener",
    )
    for route in (
        "location = /healthz",
        "location = /relay/v1/pairings/exchange",
        "location = /relay/v1/auth/refresh",
        "location = /relay/v1/device",
        "location = /relay/v1/node",
    ):
        require(route in template, f"Nginx route allowlist is missing {route}")
    require(
        'if ($http_host != "__CHEBY_PUBLIC_AUTHORITY__")' in template,
        "exact IP:port Host validation is missing",
    )
    require('$is_args != ""' in template, "query strings must fail closed")
    require('$http_origin != ""' in template, "browser Origin must fail closed")
    for forwarded_variable in (
        "$http_forwarded",
        "$http_x_forwarded_for",
        "$http_x_forwarded_host",
        "$http_x_forwarded_port",
        "$http_x_forwarded_proto",
        "$http_x_real_ip",
        "$http_cf_connecting_ip",
        "$http_x_relay_client_ip",
    ):
        require(
            f'if ({forwarded_variable} != "")' in template,
            f"caller-supplied forwarding metadata is not rejected: {forwarded_variable}",
        )
    node_block = template.split("location = /relay/v1/node", 1)[1].split("}", 1)[0]
    require("return 404" in node_block, "public Node WebSocket must remain closed")
    require(
        template.rstrip().endswith("}"),
        "Nginx template is incomplete",
    )

    for include_name in ("proxy-http.conf", "proxy-websocket.conf"):
        include = (BUNDLE / "edge/includes" / include_name).read_text(encoding="utf-8")
        for header in (
            "Forwarded",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Forwarded-Port",
            "X-Forwarded-Proto",
            "X-Real-IP",
            "CF-Connecting-IP",
        ):
            require(
                f'proxy_set_header {header} "";' in include,
                f"{include_name} does not strip {header}",
            )
        require(
            "proxy_set_header X-Relay-Client-IP $remote_addr;" in include,
            f"{include_name} does not inject the dedicated source header",
        )
        require(
            "proxy_set_header Authorization" not in include,
            f"{include_name} must preserve duplicate Authorization evidence",
        )

    env = (BUNDLE / ".env.example").read_text(encoding="utf-8")
    require("CHEBY_PUBLIC_IP=203.0.113.10" in env, "public IP example drifted")
    require("CHEBY_BIND_IP=10.0.0.10" in env, "bind IP example drifted")
    require("CHEBY_RELAY_PUBLIC_PORT=27461" in env, "production port drifted")
    require("CHEBY_RELAY_GATE_PORT=27462" in env, "Gate port drifted")
    require(
        "CHEBY_RELAY_DATA_DIR=/srv/chebycodex-r18/relay-data" in env,
        "production Relay must reuse the exact active R18 SQLite bind",
    )
    require(
        "CHEBY_RELAY_LEGACY_HOST="
        "chemicals-submission-capital-indianapolis.trycloudflare.com" in env,
        "migration-only legacy Relay Host drifted",
    )

    for name in (
        "bootstrap_edge.sh",
        "rotate_certificate.sh",
        "monitor_certificate.sh",
        "recover_certificate.sh",
        "relay_edge_common.sh",
    ):
        script = (BUNDLE / name).read_text(encoding="utf-8")
        require(":443" not in script and "--port 443" not in script, f"{name} defaults to 443")
    common = (BUNDLE / "relay_edge_common.sh").read_text(encoding="utf-8")
    rotate = (BUNDLE / "rotate_certificate.sh").read_text(encoding="utf-8")
    bootstrap = (BUNDLE / "bootstrap_edge.sh").read_text(encoding="utf-8")
    require(
        '[ "$CHEBY_PUBLIC_IP" = ' not in common,
        "public IP must remain a validated deploy-time input",
    )
    require(
        '[ "$CHEBY_BIND_IP" = ' not in common,
        "bind IP must remain a validated deploy-time input",
    )
    require(
        '""|chemicals-submission-capital-indianapolis.trycloudflare.com)' in common,
        "runtime legacy Host allowlist must reject migration identity drift",
    )
    require(
        "| wc -l" not in common and "gate_container_ids=" in common,
        "Gate running-state checks must not hide Docker Compose failures in a pipeline",
    )
    require(
        "stop_status=0" in common and 'return "$stop_status"' in common,
        "Edge stop failures must propagate to the fail-closed systemd job",
    )
    require(
        '--port "$monitored_port"' in common,
        "TLS monitor port must always be explicit",
    )
    require(
        'nginx -t \\\n        || return "$?"' in common
        and 'nginx -s reload \\\n        || return "$?"' in common
        and 'monitor_port "$CHEBY_RELAY_PUBLIC_PORT" || return "$?"' in common
        and 'monitor_port "$CHEBY_RELAY_GATE_PORT" || return "$?"' in common,
        "Nginx validation, reload, and public monitor failures must propagate",
    )
    require(
        "cert_consumer()" in common
        and "--no-deps cert-consumer" in common,
        "certificate consumption must use the isolated no-network service",
    )
    resume_consume = rotate.index("--resume-only")
    activate_candidate = rotate.index("cert_tool activate")
    public_monitor = rotate.index("monitor_running_edges", activate_candidate)
    final_consume = rotate.rindex("cert_consumer consume")
    require(
        resume_consume < activate_candidate < public_monitor < final_consume,
        "certificate candidate consumption order must be recover, activate, "
        "public verify, consume",
    )
    require(
        rotate.index("recover_certificate.sh") < final_consume
        and "Certificate activated but its staging candidate could not be consumed"
        in rotate,
        "post-verification consume failure must not invoke certificate rollback",
    )
    bootstrap_consume = bootstrap.index("cert_consumer consume")
    require(
        bootstrap.index("monitor_port") < bootstrap_consume,
        "bootstrap must consume certificate staging only after public verification",
    )
    require(
        "Certificate activated but its staging candidate could not be consumed" in bootstrap,
        "bootstrap certificate consume failure must be explicit",
    )
    rotate_timer = (BUNDLE / "systemd/chebycodex-relay-cert-rotate.timer").read_text()
    monitor_timer = (BUNDLE / "systemd/chebycodex-relay-cert-monitor.timer").read_text()
    rotate_service = (
        BUNDLE / "systemd/chebycodex-relay-cert-rotate.service"
    ).read_text()
    monitor_service = (
        BUNDLE / "systemd/chebycodex-relay-cert-monitor.service"
    ).read_text()
    require("OnUnitActiveSec=12h" in rotate_timer, "rotation check must run every 12 hours")
    require("OnUnitActiveSec=1h" in monitor_timer, "monitor must run hourly")
    lock_command = (
        "/usr/bin/flock --exclusive --timeout 900 "
        "/run/lock/chebycodex-relay-cert.lock"
    )
    require(
        lock_command in rotate_service and lock_command in monitor_service,
        "certificate rotation and monitoring must share one bounded host lock",
    )
    require(
        "EnvironmentFile=/etc/chebycodex-relay-edge/relay-edge.env"
        in rotate_service,
        "one-shot certificate rotation must load the explicit Relay Edge environment",
    )
    for unit_name, unit in (
        ("rotation", rotate_service),
        ("monitor", monitor_service),
    ):
        for directive in (
            "User=root",
            "Group=root",
            "UMask=0077",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "ReadWritePaths=/run/lock",
            "TimeoutStartSec=20min",
        ):
            require(
                directive in unit,
                f"certificate {unit_name} unit is missing hardening: {directive}",
            )

    private_markers = ("PRIVATE KEY", "ca-key.pem")
    for path in BUNDLE.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        if path.name in {"certificate_manager.py", "validate_bundle.py"}:
            continue
        content = path.read_bytes()
        require(
            b"-----BEGIN " + b"PRIVATE KEY-----" not in content,
            f"private key material is committed in {path.relative_to(BUNDLE)}",
        )

    manager = load_certificate_manager()
    fixture_root = BUNDLE / "tests/fixtures"
    for name, port in (
        ("relay-trust-bundle.production.json", 27461),
        ("relay-trust-bundle.gate.json", 27462),
    ):
        content = (fixture_root / name).read_bytes()
        document, authorities = manager._parse_trust_bundle(content, PUBLIC_IP, port)
        require(document["servicePorts"] == [port], f"{name} authorizes the wrong port")
        require(len(authorities) == 1, f"{name} must carry one test CA")

    runbook = (
        REPOSITORY / "docs/deployment/TURKEY_EDGE_RUNBOOK.md"
    ).read_text(encoding="utf-8")
    for required in (
        "192.0.0.8:27461",
        "Gate APK -- TLS/WSS :27462",
        "TCP `80` | Closed",
        "TCP `443` | Closed",
        "never run concurrently against the same SQLite database",
        "Keep the Quick Tunnel process and hostname unchanged",
        "Do not describe the staged offline leaf",
        "verify_runtime_networks.py",
        "docker stop --time 30 chebycodex-r18-relay",
        "docker start chebycodex-r18-relay",
        "loopback_listeners=$(ss -H -ltn 'sport = :18080') || {",
        'if [ -n "$loopback_listeners" ]; then',
        'CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR"',
        "CHEBY_RELAY_BOOTSTRAP_DIR=/etc/chebycodex-relay/gate-bootstrap",
        "--relay-origin https://192.0.0.8:27462",
        "for gate_role in edge-full reload-continuity android-e2e",
        "public-identities.json",
        "revoke --database /data/relay.sqlite3",
        "/android-e2e/node-bootstrap.json",
        "--pairing-bootstrap /absolute/private/edge-full/device.cxc1 \\\n  --timeout 60",
        "--reload-continuity-bootstrap /absolute/private/reload-continuity/device.cxc1",
        "bootstrap-ephemeral-continuity",
        "revoke-ephemeral-continuity",
        "purge-ephemeral-continuity",
        "--ip 192.0.0.8 --port 27461",
        "--cleanup-ready-signal",
        "--revoked-signal",
        "production-continuity.json",
        "certificate_failure_gate.py",
        "negative-certificate.json",
        "/absolute/private/android-e2e/device.cxc1",
    ):
        require(required in runbook, f"fixed-IP runbook is missing: {required}")
    for obsolete in (
        "Android -- TLS/WSS --> dedicated EIP TCP 443",
        "inbound public TCP 80 for HTTP-01 validation",
        "tools/release/bootstrap_ip_certificate.sh",
        "--file deploy/relay/docker-compose.yml stop relay",
        "--file deploy/relay/docker-compose.yml up -d relay",
        "After Gate evidence:",
    ):
        require(obsolete not in runbook, f"obsolete TCP 80/443 runbook flow remains: {obsolete}")
    require(
        runbook.count(
            "postflight must also find the exact active "
            "`NetworkSettings.Ports` publication"
        )
        == 1,
        "fixed-IP runbook contains a duplicated runtime postflight requirement",
    )
    compose_env = (
        "--env-file /etc/chebycodex-relay-edge/relay-edge.env"
    )
    gate_connector_operator_env = (
        "--env-file /etc/chebycodex-gate-connector/operator.env"
    )
    gate_connector_node_env = (
        "--env-file /etc/chebycodex-gate-connector/node.env"
    )
    rollback = runbook.split(
        "If activation fails before phone migration", maxsplit=1
    )[1].split("After the migration APK authenticates", maxsplit=1)[0]
    restart_old_relay = "docker start chebycodex-r18-relay"
    require(
        rollback.count(compose_env) == 2
        and "stop edge relay" in rollback
        and "new_stack_running=$(docker compose" in rollback
        and 'if [ -n "$new_stack_running" ]; then' in rollback
        and 'test -z "$(docker compose' not in rollback
        and rollback.index("stop edge relay") < rollback.index(restart_old_relay),
        "rollback must stop the new Relay stack before restarting the old container",
    )
    gate_start = runbook.split("Start Gate only after production is healthy:", maxsplit=1)[
        1
    ].split("Gate has a separate Relay database", maxsplit=1)[0]
    require(
        gate_start.count("docker compose") == 6
        and gate_start.count(compose_env) == 4
        and gate_start.count(gate_connector_operator_env) == 3
        and gate_start.count(gate_connector_node_env) == 3
        and "gate_stack_running=$(docker compose" in gate_start
        and gate_start.count('if [ -n "$gate_stack_running" ]; then') == 1
        and "gate_data_entry=$(find" in gate_start
        and 'if [ -n "$gate_data_entry" ]; then' in gate_start
        and 'test -z "$(docker compose' not in gate_start
        and "for gate_role in edge-full reload-continuity android-e2e" in gate_start
        and gate_start.count(
            'CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR"'
        )
        == 2
        and "Gate role assistant identities are not distinct" in gate_start
        and "revoke --database /data/relay.sqlite3" in gate_start
        and gate_start.index("revoke --database /data/relay.sqlite3")
        < gate_start.index("--profile gate up --detach relay-gate edge-gate")
        and gate_start.index("install_node_bootstrap.py")
        < gate_start.index("--profile gate up --detach relay-gate edge-gate")
        and gate_start.count("node-bootstrap.json") >= 6
        and "--profile gate up --detach relay-gate edge-gate" in gate_start,
        "Gate must create, validate, fence, and install three isolated roles before startup",
    )
    require(
        "--file deploy/turkey/docker-compose.connector-gate.yml" in gate_start
        and "config --quiet" in gate_start
        and "up --detach gate-connector" in gate_start
        and gate_start.index("--profile gate up --detach relay-gate edge-gate")
        < gate_start.index("config --quiet")
        < gate_start.index("up --detach gate-connector")
        and "verify_connector_runtime.py" in gate_start
        and "--profile gate" in gate_start,
        "Gate Connector must render, start, and pass runtime verification after Gate Relay",
    )
    gate_stop = runbook.split(
        "After Full Edge, reload-continuity, and Android evidence have all passed",
        maxsplit=1,
    )[1].split(
        "## Final release gates", maxsplit=1
    )[0]
    device_cleanup = gate_stop.split(
        "On the Mac, remove the two test-only packages", maxsplit=1
    )[1].split("On Turkey, immediately fence Gate traffic", maxsplit=1)[0]
    require(
        gate_stop.count("docker compose") == 5
        and gate_stop.count(compose_env) == 3
        and gate_stop.count(gate_connector_operator_env) == 2
        and gate_stop.count(gate_connector_node_env) == 2
        and "stop gate-connector" in gate_stop
        and "gate_connector_running=$(docker compose" in gate_stop
        and "ps --status running --quiet gate-connector" in gate_stop
        and 'if [ -n "$gate_connector_running" ]; then' in gate_stop
        and "--profile gate stop edge-gate relay-gate" in gate_stop
        and "gate_stack_running=$(docker compose" in gate_stop
        and "ps --status running --quiet relay-gate edge-gate" in gate_stop
        and 'if [ -n "$gate_stack_running" ]; then' in gate_stop
        and '["roles"]["android-e2e"]["nodeId"]' in gate_stop
        and "revoke --database /data/relay.sqlite3" in gate_stop
        and "/etc/chebycodex-gate-connector/node.env" in gate_stop
        and "/etc/chebycodex-gate-connector/secrets/node_token" in gate_stop
        and "/etc/chebycodex-gate-connector/secrets/gateway_internal_secret"
        in gate_stop
        and 'find "$gate_connector_data" -xdev -mindepth 1 -delete'
        in gate_stop
        and "Gate Connector credential or data residue remains" in gate_stop,
        "Gate teardown must stop all services, revoke Android, and remove Connector state",
    )
    require(
        "gate_relay_data=/var/lib/chebycodex-relay-gate/data" in gate_stop
        and "gate_bootstrap=/etc/chebycodex-relay/gate-bootstrap" in gate_stop
        and 'realpath -e -- "$gate_relay_data"' in gate_stop
        and 'realpath -e -- "$gate_bootstrap"' in gate_stop
        and 'find "$gate_relay_data" -xdev -mindepth 1 -delete' in gate_stop
        and 'find "$gate_bootstrap" -xdev -mindepth 1 -delete' in gate_stop
        and '"10001:10001:700"' in gate_stop
        and "empty-state preflight would fail" in gate_stop,
        "Gate teardown must empty and verify the exact Relay DB and bootstrap roots",
    )
    require(
        'com.cheby.codex.mobile.gate.test' in device_cleanup
        and 'com.cheby.codex.mobile.gate' in device_cleanup
        and 'shell am force-stop "$gate_package"' in device_cleanup
        and 'uninstall "$gate_package"' in device_cleanup
        and 'shell pm path "$gate_package"' in device_cleanup
        and 'shell pidof "$gate_package"' in device_cleanup
        and not any(
            forbidden in device_cleanup.lower()
            for forbidden in (
                " shell input ",
                " input tap ",
                " input text ",
                " keyevent ",
                " uiautomator ",
                " monkey ",
                " am start ",
            )
        ),
        "final Gate APK teardown must remove and verify test state without screen operations",
    )
    require(
        gate_stop.count(
            "/usr/local/sbin/codex-security-lockdown.sh"
        )
        == 2
        and "--production-only" in gate_stop
        and "--verify --production-only" in gate_stop
        and "close the Huawei Cloud TCP `27462` rule" in gate_stop,
        "Gate teardown must close 27462 through the host and cloud boundaries",
    )
    require(
        gate_stop.index("stop gate-connector")
        < gate_stop.index("ps --status running --quiet gate-connector")
        < gate_stop.index("--profile gate stop edge-gate relay-gate")
        < gate_stop.index("ps --status running --quiet relay-gate edge-gate")
        < gate_stop.index('["roles"]["android-e2e"]["nodeId"]')
        < gate_stop.index("revoke --database /data/relay.sqlite3")
        < gate_stop.index(
            "rm -f -- \\\n  /etc/chebycodex-gate-connector/node.env"
        )
        < gate_stop.index('find "$gate_connector_data" -xdev')
        < gate_stop.index('find "$gate_relay_data" -xdev')
        < gate_stop.index('find "$gate_bootstrap" -xdev')
        < gate_stop.index("On the Mac, remove the two test-only packages")
        < gate_stop.index(
            "/usr/local/sbin/codex-security-lockdown.sh --production-only"
        ),
        "Gate teardown order must preserve audit, revocation, and cleanup safety",
    )
    full_gate_index = runbook.index(
        "--pairing-bootstrap /absolute/private/edge-full/device.cxc1"
    )
    temporary_rotate_service_index = runbook.index(
        "deploy/relay-edge/systemd/chebycodex-relay-cert-rotate.service"
    )
    reload_gate_index = runbook.index(
        "--reload-continuity-bootstrap "
        "/absolute/private/reload-continuity/device.cxc1"
    )
    production_reload_gate_index = runbook.index(
        '--reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1"'
    )
    android_gate_index = runbook.index(
        "Only after reload continuity passes, provision the Gate APK"
    )
    gate_stop_index = runbook.index("--profile gate stop edge-gate relay-gate")
    timer_enable_index = runbook.index("sudo systemctl enable --now")
    require(
        full_gate_index
        < temporary_rotate_service_index
        < reload_gate_index
        < production_reload_gate_index
        < android_gate_index
        < gate_stop_index
        < timer_enable_index,
        "Full, reload, Android, teardown, and timer activation order drifted",
    )
    gate_data_override = 'CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR"'
    gate_bootstrap_override = (
        "CHEBY_RELAY_BOOTSTRAP_DIR=/etc/chebycodex-relay/gate-bootstrap"
    )
    gate_admin = "--profile admin run --rm --no-deps relay-admin"
    require(
        runbook.index(gate_data_override)
        < runbook.index(gate_bootstrap_override)
        < runbook.index(gate_admin),
        "Gate relay-admin must receive both isolated invocation-scoped mounts",
    )

    edge_readme = (BUNDLE / "README.md").read_text(encoding="utf-8")
    readme_gate = edge_readme.split("## Gate and recovery", maxsplit=1)[1].split(
        "The Quick Tunnel may be removed", maxsplit=1
    )[0]
    require(
        "/etc/chebycodex-relay/gate-bootstrap" in edge_readme
        and readme_gate.count("docker compose") == readme_gate.count(compose_env)
        and 'CHEBY_RELAY_DATA_DIR="$CHEBY_RELAY_GATE_DATA_DIR"' in readme_gate
        and "--profile admin run --rm --no-deps relay-admin" in readme_gate
        and "for gate_role in edge-full reload-continuity android-e2e" in readme_gate
        and "public `assistantId`" in readme_gate
        and "`relay-admin revoke`" in readme_gate
        and "`android-e2e/node-bootstrap.json`" in readme_gate
        and "unknown outcome" in readme_gate
        and "--profile gate up --detach relay-gate edge-gate" in readme_gate
        and "--profile gate stop edge-gate relay-gate" in readme_gate
        and 'if [ -n "$gate_stack_running" ]; then' in readme_gate
        and 'if [ -n "$gate_data_entry" ]; then' in readme_gate
        and "This stop is not the complete teardown" in readme_gate
        and "Gate Relay\nDB/WAL/SHM directory" in readme_gate
        and "`public-identities.json`" in readme_gate
        and "verified production-only mode" in readme_gate
        and "next-run preflight passes" in readme_gate
        and 'test -z "$(docker compose' not in readme_gate,
        "Relay Edge README must preserve isolated Gate bootstrap and explicit Compose env",
    )
    require(
        "The fixed order is Full Edge with `edge-full`, reload continuity with"
        in readme_gate
        and readme_gate.index("The fixed order is Full Edge")
        < readme_gate.index("--profile gate stop edge-gate relay-gate"),
        "Relay Edge README must keep reload continuity before final Gate teardown",
    )

    connector_gate_docs = (
        REPOSITORY / "docs/deployment/TURKEY_CONNECTOR_RUNBOOK.md",
        REPOSITORY / "deploy/turkey/README_CONNECTOR.md",
    )
    fixed_android_bootstrap = (
        "/etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json"
    )
    for connector_gate_doc in connector_gate_docs:
        connector_gate_text = connector_gate_doc.read_text(encoding="utf-8")
        require(
            connector_gate_text.count(fixed_android_bootstrap) >= 3
            and "/root/gate-node-bootstrap.json" not in connector_gate_text
            and "Never install" in connector_gate_text
            and "`edge-full`" in connector_gate_text
            and "`reload-continuity`" in connector_gate_text
            and connector_gate_text.count(gate_connector_operator_env) >= 2
            and connector_gate_text.count(gate_connector_node_env) >= 2
            and "config --quiet" in connector_gate_text
            and "up --detach gate-connector" in connector_gate_text
            and "rm -f --" in connector_gate_text
            and "test ! -e" in connector_gate_text
            and "Gate Relay DB/WAL/SHM" in connector_gate_text
            and "production-only" in connector_gate_text,
            f"{connector_gate_doc.name} must use only the fixed android-e2e bootstrap",
        )

    gate_readme = (REPOSITORY / "tools/gate/README.md").read_text(encoding="utf-8")
    for marker in (
        "`edge-full`, `reload-continuity`, and `android-e2e`",
        "--pairing-bootstrap /absolute/private/edge-full/device.cxc1 \\\n  --timeout 60",
        "CXC1_FILE=/absolute/private/android-e2e/device.cxc1",
        "--reload-continuity-bootstrap "
        "/absolute/private/reload-continuity/device.cxc1",
        '--reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1"',
        "--cleanup-ready-signal",
        "--revoked-signal",
        "production-continuity.json",
        "certificate_failure_gate.py",
        "15 zero-skip tests",
        "The execution order is fixed: Full Edge first, reload continuity second",
        "Run this section only after the reload-continuity",
        "outcome is unknown",
        "Never retry",
        "`chebycodex-turkey-gate-connector-gate-connector-1`",
        "every other `--connector-container` value is",
        "## Final isolated-state teardown",
        "`com.cheby.codex.mobile.gate.test`",
        "verified production-only firewall",
    ):
        require(marker in gate_readme, f"Gate credential lifecycle is missing: {marker}")
    for stale_pairing_path in (
        "/bootstrap/gate-device.cxc1",
        "/absolute/path/to/gate-device.cxc1",
    ):
        require(
            stale_pairing_path not in runbook
            and stale_pairing_path not in edge_readme
            and stale_pairing_path not in gate_readme,
            f"shared one-use Gate pairing path remains: {stale_pairing_path}",
        )

    bootstrap = (BUNDLE / "bootstrap_edge.sh").read_text(encoding="utf-8")
    require(
        '--require-free-loopback-port "$CHEBY_RELAY_LOOPBACK_PORT"' in bootstrap,
        "Edge activation must require the production Relay loopback port to be free",
    )
    preflight_source = (BUNDLE / "preflight_host.py").read_text(encoding="utf-8")
    require(
        "_docker_published_ports()" in preflight_source
        and '"--require-free-loopback-port"' in preflight_source,
        "activation preflight must inspect Docker publications and host listeners",
    )

    gate_source = (REPOSITORY / "tools/gate/edge_gate.py").read_text(
        encoding="utf-8"
    )
    store_source = (
        REPOSITORY / "relay/cheby_relay/store.py"
    ).read_text(encoding="utf-8")
    continuity_device_name = "Cheby Edge production reload continuity"
    require(
        continuity_device_name in gate_source
        and continuity_device_name in store_source,
        "production continuity Device identity drifted across gate and Relay",
    )
    require(
        "FORGED_FORWARDING_HEADERS" in gate_source
        and "each forged forwarding header rejected" in gate_source,
        "public gate must probe every forwarding header independently",
    )
    require(
        "recorder.skip(" not in gate_source,
        "public gate must not manufacture skipped release checks",
    )
    for continuity_marker in (
        "run_reload_continuity_gate",
        "--reload-continuity-bootstrap",
        "--expected-new-leaf-sha256",
        "--manifest",
        "--ready-signal",
        "--reloaded-signal",
        "--cleanup-ready-signal",
        "--revoked-signal",
        "PRODUCTION_CONTINUITY_DEVICE_NAME",
        "ephemeralIdentityRevoked",
        "authenticated WSS survives reload and new TLS receives expected leaf",
    ):
        require(
            continuity_marker in gate_source,
            f"certificate reload continuity gate is missing: {continuity_marker}",
        )
    for continuity_runbook_marker in (
        "--reload-continuity-bootstrap",
        "--expected-new-leaf-sha256",
        "--manifest",
        "--cleanup-ready-signal",
        "--revoked-signal",
        "READY",
        "RELOADED",
        "REVOKE",
        "REVOKED",
    ):
        require(
            continuity_runbook_marker in runbook,
            "fixed-IP runbook is missing certificate reload coordination: "
            f"{continuity_runbook_marker}",
        )

    negative_gate = (
        REPOSITORY / "tools/gate/certificate_failure_gate.py"
    ).read_text(encoding="utf-8")
    for negative_marker in (
        "candidate-rejections",
        "previous-release-validation",
        "recovery-fail-closed",
        "rotation-post-activation-rollback",
        "expectedTests",
        "sourceSha256",
        "write_immutable_manifest",
    ):
        require(
            negative_marker in negative_gate,
            f"certificate negative gate is missing: {negative_marker}",
        )

    print("relay-edge deployment bundle: PASS")


if __name__ == "__main__":
    main()
