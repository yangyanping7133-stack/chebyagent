#!/usr/bin/env python3
"""Validate security invariants in normalized `docker compose config` JSON."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
from typing import Any

from turkey_release_manifest import EXPECTED_SUPPORT_REFERENCES, load_manifest


IMMUTABLE_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
PROJECT_NAME = "chebycodex-turkey"
RUNTIME_IMAGE_SOURCE = "${CHEBY_RUNTIME_IMAGE:?set the scanned immutable runtime image ID}"
EDGE_IMAGE_SOURCE = "${CHEBY_EDGE_IMAGE:?set the scanned immutable Edge image ID}"
PUBLIC_IP_SOURCE = "${CHEBY_PUBLIC_IP:?set the dedicated public IPv4 address}"
PAIRING_SECRET_SOURCE = (
    "${CHEBY_PAIRING_SECRET_PATH:?set an absolute path to the pairing secret file}"
)
RUNTIME_ENVIRONMENT = {
    "HOME": "/home/cheby",
    "CODEX_HOME": "/home/cheby/.codex",
    "CHEBY_GATEWAY_DB": "/data/gateway.sqlite3",
    "CHEBY_BRIDGE_MODE": "stdio",
    "CHEBY_LOCAL_IMAGE_ENABLED": "true",
    "CHEBY_CODEX_COMMAND": "codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
    "CHEBY_CODEX_CWD": "/workspace",
    "CHEBY_CODEX_FRAME_LIMIT_BYTES": "67108864",
    "CHEBY_PAIRING_SECRET_FILE": "/run/secrets/pairing_secret",
    "CHEBY_TRUSTED_PROXY_CIDRS": "172.31.42.2/32",
    "CHEBY_PAIRING_SOURCE_LIMIT": "8",
    "CHEBY_PAIRING_GLOBAL_LIMIT": "40",
    "CHEBY_PAIRING_WINDOW_SECONDS": "60",
    "CHEBY_PAIRING_BACKOFF_BASE_SECONDS": "2",
    "CHEBY_PAIRING_BACKOFF_MAX_SECONDS": "60",
    "CHEBY_SEND_DEVICE_LIMIT": "30",
    "CHEBY_SEND_WINDOW_SECONDS": "60",
    "CHEBY_APPROVAL_DEVICE_LIMIT": "20",
    "CHEBY_APPROVAL_WINDOW_SECONDS": "60",
    "CHEBY_WEBSOCKET_CONNECT_GLOBAL_LIMIT": "200",
    "CHEBY_WEBSOCKET_CONNECT_SOURCE_LIMIT": "30",
    "CHEBY_WEBSOCKET_CONNECT_DEVICE_LIMIT": "20",
    "CHEBY_WEBSOCKET_CONNECT_WINDOW_SECONDS": "60",
    "CHEBY_WEBSOCKET_MESSAGE_DEVICE_LIMIT": "120",
    "CHEBY_WEBSOCKET_MESSAGE_WINDOW_SECONDS": "60",
}
EDGE_ENVIRONMENT = {"NGINX_ENTRYPOINT_QUIET_LOGS": "1"}
CERT_EXPORT_STATIC_ENVIRONMENT = {"CHEBY_CERT_MIN_REMAINING_HOURS": "24"}
EDGE_UPLOAD_TMPFS = "/tmp:size=32m,uid=101,gid=101,mode=0700"
IMAGE_UPLOAD_LOCATION = (
    "location ~ ^/v1/threads/[^/]+/turn-inputs/[^/]+/images/[^/]+$ {"
)
RAW_TMPFS = {
    "runtime": [
        "/tmp:size=64m,mode=1777",
        "/home/cheby/.cache:size=64m,uid=10002,gid=10002,mode=0700",
        "/home/cheby/.config:size=16m,uid=10002,gid=10002,mode=0700",
    ],
    "edge": [EDGE_UPLOAD_TMPFS],
    "certbot": ["/tmp:size=32m,mode=1777"],
    "cert-export": ["/tmp:size=16m,mode=1777"],
}
RAW_VOLUMES = {
    "runtime": {
        "gateway-data:/data:rw",
        "codex-home:/home/cheby/.codex:rw",
        "workspace-data:/workspace:rw",
        "asset-staging:/asset-staging:rw",
    },
    "edge": {
        "./generated/nginx.conf:/etc/nginx/nginx.conf:ro",
        "acme-webroot:/var/www/acme:ro",
        "tls-live:/tls:ro",
    },
    "certbot": {
        "letsencrypt-state:/etc/letsencrypt:rw",
        "certbot-work:/var/lib/letsencrypt:rw",
        "certbot-logs:/var/log/letsencrypt:rw",
        "acme-webroot:/var/www/acme:rw",
    },
    "cert-export": {
        "letsencrypt-state:/etc/letsencrypt:ro",
        "tls-live:/tls:rw",
        "../../tools/release/export_ip_certificate.py:/opt/release/export_ip_certificate.py:ro",
    },
}
TOP_LEVEL_VOLUME_NAMES = {
    "gateway-data",
    "codex-home",
    "workspace-data",
    "asset-staging",
    "acme-webroot",
    "letsencrypt-state",
    "certbot-work",
    "certbot-logs",
    "tls-live",
}
NAMED_VOLUME_TARGETS = {
    name: {
        tuple(part for part in volume.split(":"))
        for volume in volumes
        if not volume.startswith(".")
    }
    for name, volumes in RAW_VOLUMES.items()
}
RAW_SERVICE_FIELDS = {
    "runtime": {
        "platform", "image", "pull_policy", "restart", "init", "read_only",
        "user", "environment", "expose", "networks", "volumes", "tmpfs",
        "secrets", "security_opt", "cap_drop", "pids_limit", "mem_limit",
        "cpus", "stop_grace_period",
    },
    "edge": {
        "platform", "image", "pull_policy", "restart", "read_only", "user",
        "environment", "depends_on", "ports", "networks", "volumes", "tmpfs",
        "security_opt", "cap_drop", "pids_limit", "mem_limit", "cpus",
        "stop_grace_period",
    },
    "certbot": {
        "platform", "image", "profiles", "read_only", "user", "entrypoint",
        "command", "volumes", "tmpfs", "security_opt", "cap_drop",
        "pids_limit", "mem_limit", "cpus",
    },
    "cert-export": {
        "platform", "image", "profiles", "read_only", "user", "entrypoint",
        "command", "environment", "network_mode", "volumes", "tmpfs",
        "security_opt", "cap_drop", "pids_limit", "mem_limit", "cpus",
    },
}
NORMALIZED_SERVICE_FIELDS = {
    name: fields
    | ({"networks"} if name == "certbot" else set())
    | ({"command", "entrypoint"} if name in {"runtime", "edge"} else set())
    for name, fields in RAW_SERVICE_FIELDS.items()
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def nginx_block(document: str, opening: str) -> str:
    require(document.count(opening) == 1, "exact image-upload location is not unique")
    start = document.index(opening)
    brace = document.index("{", start)
    depth = 0
    for position in range(brace, len(document)):
        if document[position] == "{":
            depth += 1
        elif document[position] == "}":
            depth -= 1
            if depth == 0:
                return document[start : position + 1]
    raise RuntimeError("exact image-upload location is not balanced")


def require_edge_upload_budget(
    edge: dict[str, Any],
    nginx_document: str | None = None,
    proxy_document: str | None = None,
) -> None:
    require(edge.get("tmpfs") == [EDGE_UPLOAD_TMPFS], "Edge upload tmpfs budget drifted")
    repository_root = Path(__file__).resolve().parents[2]
    if nginx_document is None:
        nginx_document = (
            repository_root / "deploy/turkey/edge/nginx.conf.template"
        ).read_text(encoding="utf-8")
    if proxy_document is None:
        proxy_document = (
            repository_root / "deploy/turkey/edge/includes/proxy-http.conf"
        ).read_text(encoding="utf-8")
    for zone in (
        "limit_conn_zone $binary_remote_addr zone=image_connection_source:10m;",
        "limit_conn_zone $server_addr zone=image_connection_global:10m;",
    ):
        require(nginx_document.count(zone) == 1, f"Edge upload zone drifted: {zone}")
    location = nginx_block(nginx_document, IMAGE_UPLOAD_LOCATION)
    for directive in (
        "client_max_body_size 8m;",
        "limit_conn image_connection_source 1;",
        "limit_conn image_connection_global 2;",
        "include /etc/nginx/includes/proxy-http.conf;",
    ):
        require(location.count(directive) == 1, f"Edge upload directive drifted: {directive}")
    require(
        proxy_document.count("proxy_request_buffering on;") == 1
        and "proxy_request_buffering off;" not in proxy_document,
        "Edge image uploads must remain request-buffered",
    )


def require_service_field_allowlist(
    services: dict[str, dict[str, Any]], normalized: bool
) -> None:
    allowed_by_service = (
        NORMALIZED_SERVICE_FIELDS if normalized else RAW_SERVICE_FIELDS
    )
    require(
        set(services) == set(allowed_by_service),
        "Compose service set is not exact",
    )
    for name, service in services.items():
        require(isinstance(service, dict), f"{name} service must be a mapping")
        actual = set(service)
        expected = allowed_by_service[name]
        unknown = actual - expected
        missing = expected - actual
        require(
            not unknown and not missing,
            f"{name} top-level fields are not exact; unknown={sorted(unknown)}, missing={sorted(missing)}",
        )


def memory_bytes(value: object) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if text and text[-1:] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def duration_seconds(value: object) -> int:
    if isinstance(value, int):
        if value >= 1_000_000_000:
            require(value % 1_000_000_000 == 0, "duration is not whole seconds")
            return value // 1_000_000_000
        return value
    match = re.fullmatch(r"(\d+)s", str(value))
    require(match is not None, "duration must be an exact whole-second value")
    return int(match.group(1))


def require_exact_service_values(
    services: dict[str, dict[str, Any]],
    normalized: bool,
    release: dict[str, Any] | None = None,
    public_ip: str | None = None,
    bind_ip: str | None = None,
) -> None:
    runtime = services["runtime"]
    edge = services["edge"]
    certbot = services["certbot"]
    cert_export = services["cert-export"]

    require(runtime.get("environment") == RUNTIME_ENVIRONMENT, "runtime environment must be exact")
    require(edge.get("environment") == EDGE_ENVIRONMENT, "Edge environment must be exact")
    expected_export_environment = {
        **CERT_EXPORT_STATIC_ENVIRONMENT,
        "CHEBY_PUBLIC_IP": str(public_ip) if normalized else PUBLIC_IP_SOURCE,
    }
    require(
        cert_export.get("environment") == expected_export_environment,
        "certificate exporter environment must be exact",
    )
    require("environment" not in certbot, "Certbot environment must remain empty")

    for name, expected_user in {
        "runtime": "10002:10002",
        "edge": "101:101",
        "certbot": "0:0",
        "cert-export": "0:0",
    }.items():
        service = services[name]
        require(service.get("platform") == "linux/amd64", f"{name} platform drifted")
        require(service.get("read_only") is True, f"{name} read_only must be true")
        require(service.get("user") == expected_user, f"{name} user drifted")
        require(service.get("security_opt") == ["no-new-privileges:true"], f"{name} security options drifted")
        require(service.get("cap_drop") == ["ALL"], f"{name} capability set drifted")
        require(service.get("tmpfs") == RAW_TMPFS[name], f"{name} tmpfs set/value drifted")
    require_edge_upload_budget(edge)

    require(runtime.get("restart") == "unless-stopped", "runtime restart policy drifted")
    require(edge.get("restart") == "unless-stopped", "Edge restart policy drifted")
    require(runtime.get("init") is True, "runtime init must remain enabled")
    require(runtime.get("pull_policy") == "never", "runtime pull policy drifted")
    require(edge.get("pull_policy") == "never", "Edge pull policy drifted")

    require(certbot.get("profiles") == ["maintenance"], "Certbot profile set drifted")
    require(cert_export.get("profiles") == ["maintenance"], "exporter profile set drifted")
    require(certbot.get("entrypoint") == ["certbot"], "Certbot entrypoint drifted")
    require(certbot.get("command") == ["--version"], "Certbot command drifted")
    require(cert_export.get("entrypoint") == ["python"], "exporter entrypoint drifted")
    require(
        cert_export.get("command") == ["/opt/release/export_ip_certificate.py"],
        "exporter command drifted",
    )
    certbot_image = EXPECTED_SUPPORT_REFERENCES["certbot"]
    require(certbot.get("image") == certbot_image, "Certbot image digest drifted")
    require(cert_export.get("image") == certbot_image, "exporter image digest drifted")
    if normalized:
        require(release is not None, "normalized release manifest is required")
        for name in ("runtime", "edge"):
            require(services[name].get("command") is None, f"{name} normalized command drifted")
            require(
                services[name].get("entrypoint") is None,
                f"{name} normalized entrypoint drifted",
            )
        require(
            runtime.get("image") == release["images"]["runtime"]["reference"],
            "runtime normalized image drifted",
        )
        require(
            edge.get("image") == release["images"]["edge"]["reference"],
            "Edge normalized image drifted",
        )
    else:
        require(runtime.get("image") == RUNTIME_IMAGE_SOURCE, "runtime image source drifted")
        require(edge.get("image") == EDGE_IMAGE_SOURCE, "Edge image source drifted")

    require(runtime.get("expose") == ["8080"], "runtime expose set drifted")
    raw_ports = {
        "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:80:8080/tcp",
        "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:443:8443/tcp",
    }
    if normalized:
        ports = edge.get("ports")
        require(isinstance(ports, list) and len(ports) == 2, "Edge normalized ports must be exact")
        normalized_ports: set[tuple[str, int, int, str, str]] = set()
        for port in ports:
            require(
                isinstance(port, dict)
                and set(port) == {"mode", "host_ip", "target", "published", "protocol"},
                "Edge normalized port fields must be exact",
            )
            normalized_ports.add(
                (
                    str(port["host_ip"]),
                    int(port["published"]),
                    int(port["target"]),
                    str(port["protocol"]),
                    str(port["mode"]),
                )
            )
        require(
            normalized_ports
            == {
                (str(bind_ip), 80, 8080, "tcp", "ingress"),
                (str(bind_ip), 443, 8443, "tcp", "ingress"),
            },
            "Edge normalized port values drifted",
        )
    else:
        ports = edge.get("ports")
        require(
            isinstance(ports, list) and len(ports) == 2 and set(ports) == raw_ports,
            "Edge raw ports must be exact",
        )

    runtime_networks = runtime.get("networks")
    edge_networks = edge.get("networks")
    require(
        isinstance(runtime_networks, dict)
        and set(runtime_networks) == {"backend", "codex-egress"}
        and runtime_networks["backend"] == {"ipv4_address": "172.31.42.3"}
        and runtime_networks["codex-egress"] in ({}, None),
        "runtime network attachments must be exact",
    )
    require(
        isinstance(edge_networks, dict)
        and edge_networks == {"backend": {"ipv4_address": "172.31.42.2"}},
        "Edge network attachment must be exact",
    )
    if normalized:
        require(
            certbot.get("networks") == {"default": None}
            or certbot.get("networks") == {"default": {}},
            "Certbot normalized default network must be exact",
        )
        require(
            edge.get("depends_on")
            == {"runtime": {"condition": "service_healthy", "required": True}},
            "Edge normalized dependency must be exact",
        )
        runtime_secrets = runtime.get("secrets")
        require(
            isinstance(runtime_secrets, list)
            and len(runtime_secrets) == 1
            and isinstance(runtime_secrets[0], dict)
            and runtime_secrets[0]
            == {"source": "pairing_secret", "target": "/run/secrets/pairing_secret"},
            "runtime normalized secret mount must be exact",
        )
    else:
        require(
            edge.get("depends_on") == {"runtime": {"condition": "service_healthy"}},
            "Edge dependency must be exact",
        )
        require(runtime.get("secrets") == ["pairing_secret"], "runtime secret mount drifted")

    raw_resources = {
        "runtime": (4096, "${CHEBY_RUNTIME_MEMORY:-18g}", "${CHEBY_RUNTIME_CPUS:-8.0}", 45),
        "edge": (128, "256m", 0.5, 30),
        "certbot": (128, "256m", 0.5, None),
        "cert-export": (64, "128m", 0.25, None),
    }
    normalized_resources = {
        "runtime": (4096, 18 * 1024**3, 8.0, 45),
        "edge": (128, 256 * 1024**2, 0.5, 30),
        "certbot": (128, 256 * 1024**2, 0.5, None),
        "cert-export": (64, 128 * 1024**2, 0.25, None),
    }
    resources = normalized_resources if normalized else raw_resources
    for name, (pids_limit, memory, cpus, grace_seconds) in resources.items():
        service = services[name]
        require(service.get("pids_limit") == pids_limit, f"{name} PID limit drifted")
        if normalized:
            require(memory_bytes(service.get("mem_limit")) == memory, f"{name} memory limit drifted")
            require(float(service.get("cpus")) == cpus, f"{name} CPU limit drifted")
        else:
            require(service.get("mem_limit") == memory, f"{name} memory limit drifted")
            require(service.get("cpus") == cpus, f"{name} CPU limit drifted")
        if grace_seconds is not None:
            require(
                duration_seconds(service.get("stop_grace_period")) == grace_seconds,
                f"{name} stop grace period drifted",
            )


def require_exact_top_level(
    document: dict[str, Any], normalized: bool, pairing_secret_path: str | None = None
) -> None:
    require(
        set(document) == {"name", "services", "networks", "volumes", "secrets"},
        "Compose top-level fields must be exact",
    )
    require(document.get("name") == PROJECT_NAME, "Compose project name drifted")
    if normalized:
        networks = {
            "backend": {
                "name": f"{PROJECT_NAME}_backend",
                "internal": True,
                "ipam": {"config": [{"subnet": "172.31.42.0/28"}]},
            },
            "codex-egress": {"name": f"{PROJECT_NAME}_codex-egress", "ipam": {}},
            "default": {"name": f"{PROJECT_NAME}_default", "ipam": {}},
        }
        volumes = {
            name: {"name": f"{PROJECT_NAME}_{name}"}
            for name in TOP_LEVEL_VOLUME_NAMES
        }
        require(pairing_secret_path is not None, "normalized pairing secret path is required")
        require(Path(pairing_secret_path).is_absolute(), "pairing secret path must be absolute")
        secrets = {
            "pairing_secret": {
                "name": f"{PROJECT_NAME}_pairing_secret",
                "file": pairing_secret_path,
            }
        }
    else:
        networks = {
            "backend": {
                "internal": True,
                "ipam": {"config": [{"subnet": "172.31.42.0/28"}]},
            },
            "codex-egress": {},
        }
        volumes = {name: {} for name in TOP_LEVEL_VOLUME_NAMES}
        secrets = {"pairing_secret": {"file": PAIRING_SECRET_SOURCE}}
    require(document.get("networks") == networks, "top-level network definitions must be exact")
    require(document.get("volumes") == volumes, "top-level volume definitions must be exact")
    require(document.get("secrets") == secrets, "top-level secret definition must be exact")


def compose_default(value: object, variable: str) -> str:
    match = re.fullmatch(rf"\$\{{{re.escape(variable)}:-([^}}]+)}}", str(value))
    require(match is not None, f"{variable} must have an explicit Compose default")
    return match.group(1)


def require_hardened(
    service: dict[str, Any], name: str, user: str, network_mode: str | None = None
) -> None:
    require(service.get("read_only") is True, f"{name} root filesystem must be read-only")
    require(service.get("user") == user, f"{name} must use fixed user {user}")
    require(service.get("cap_drop") == ["ALL"], f"{name} must drop exactly all capabilities")
    require("cap_add" not in service, f"{name} must not declare cap_add")
    require(
        service.get("security_opt") == ["no-new-privileges:true"],
        f"{name} security options must be exactly no-new-privileges",
    )
    require("privileged" not in service, f"{name} must not declare privileged mode")
    for field in ("devices", "volumes_from", "pid", "ipc"):
        require(field not in service, f"{name} must not declare {field}")
    require(
        service.get("network_mode") == network_mode,
        f"{name} network_mode must be {network_mode!r}",
    )
    require(service.get("platform") == "linux/amd64", f"{name} platform must be linux/amd64")


def require_raw_volumes(service: dict[str, Any], name: str) -> None:
    volumes = service.get("volumes", [])
    require(
        isinstance(volumes, list) and set(volumes) == RAW_VOLUMES[name] and len(volumes) == len(RAW_VOLUMES[name]),
        f"{name} volume set or access mode drifted",
    )


def require_normalized_volumes(service: dict[str, Any], name: str) -> None:
    volumes = service.get("volumes", [])
    require(isinstance(volumes, list), f"{name} normalized volumes must be a list")
    require(
        len(volumes) == len(RAW_VOLUMES[name]),
        f"{name} normalized volume count drifted",
    )
    named_actual: set[tuple[str, str, str]] = set()
    bind_targets: set[tuple[str, bool]] = set()
    for volume in volumes:
        require(isinstance(volume, dict), f"{name} normalized volume is malformed")
        require(
            not (set(volume) - {"type", "source", "target", "read_only", "volume", "bind"}),
            f"{name} normalized volume has an unreviewed nested field",
        )
        volume_type = volume.get("type")
        target = str(volume.get("target", ""))
        read_only = bool(volume.get("read_only", False))
        source = str(volume.get("source", ""))
        if volume_type == "volume":
            require(
                source in TOP_LEVEL_VOLUME_NAMES,
                f"{name} normalized named-volume source drifted",
            )
            require(volume.get("volume", {}) == {}, f"{name} volume options drifted")
            require("bind" not in volume, f"{name} volume contains bind options")
            named_actual.add((source, target, "ro" if read_only else "rw"))
        elif volume_type == "bind":
            require(Path(source).is_absolute(), f"{name} bind source must normalize absolutely")
            require("volume" not in volume, f"{name} bind contains volume options")
            require(
                volume.get("bind", {"create_host_path": True})
                == {"create_host_path": True},
                f"{name} bind options drifted",
            )
            bind_targets.add((target, read_only))
            if target == "/etc/nginx/nginx.conf":
                require(
                    source
                    == str(
                        Path(__file__).resolve().parents[2]
                        / "deploy/turkey/generated/nginx.conf"
                    ),
                    "Edge config bind source drifted",
                )
            elif target == "/opt/release/export_ip_certificate.py":
                require(
                    source
                    == str(
                        Path(__file__).resolve().parents[2]
                        / "tools/release/export_ip_certificate.py"
                    ),
                    "exporter bind source drifted",
                )
            else:
                raise RuntimeError(f"{name} has an unreviewed host bind mount")
        else:
            raise RuntimeError(f"{name} has an unreviewed mount type")
    require(named_actual == NAMED_VOLUME_TARGETS[name], f"{name} named volume set drifted")
    expected_binds = {
        "runtime": set(),
        "edge": {("/etc/nginx/nginx.conf", True)},
        "certbot": set(),
        "cert-export": {("/opt/release/export_ip_certificate.py", True)},
    }[name]
    require(bind_targets == expected_binds, f"{name} bind mount set drifted")


def require_manifest_images(
    services: dict[str, dict[str, Any]], release: dict[str, Any]
) -> None:
    for service_name in ("runtime", "edge"):
        image = str(services[service_name].get("image", ""))
        require(
            IMMUTABLE_IMAGE_ID.fullmatch(image) is not None,
            f"{service_name} image is not an immutable ID",
        )
        require(
            image == release["images"][service_name]["reference"],
            f"{service_name} image does not match scanned release manifest",
        )
    certbot_image = release["images"]["certbot"]["reference"]
    require(
        services["certbot"].get("image") == certbot_image
        and services["cert-export"].get("image") == certbot_image,
        "certificate service images do not match the scanned release manifest",
    )


def validate_source(path: Path) -> None:
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("PyYAML is required for the raw Compose security gate") from error

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(document, dict), "Compose source must be a mapping")
    require_exact_top_level(document, normalized=False)
    services = document.get("services", {})
    require(isinstance(services, dict), "Compose services must be a mapping")
    require_service_field_allowlist(services, normalized=False)
    require_exact_service_values(services, normalized=False)
    runtime = services["runtime"]
    edge = services["edge"]
    certbot = services["certbot"]
    cert_export = services["cert-export"]

    for name, service in services.items():
        require("build" not in service, f"{name} must not have a production build directive")
        require_raw_volumes(service, name)

    require(not runtime.get("ports"), "runtime must not publish a host port")
    require(runtime.get("expose") == ["8080"], "runtime may expose only private port 8080")
    require_hardened(runtime, "runtime", "10002:10002")
    require(
        runtime.get("image") == "${CHEBY_RUNTIME_IMAGE:?set the scanned immutable runtime image ID}",
        "runtime image must come only from the scanned release manifest",
    )
    require(runtime.get("pull_policy") == "never", "runtime pull policy must be never")
    require(runtime.get("init") is True, "runtime must use an init process")
    environment = runtime.get("environment", {})
    require(environment.get("CHEBY_BRIDGE_MODE") == "stdio", "production bridge must use stdio")
    require(
        environment.get("CHEBY_LOCAL_IMAGE_ENABLED") == "true",
        "production local-image capability must be explicitly enabled",
    )
    require(
        environment.get("CHEBY_CODEX_COMMAND")
        == "codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
        "Codex app-server must only listen on stdio",
    )
    require(
        environment.get("CHEBY_TRUSTED_PROXY_CIDRS") == "172.31.42.2/32",
        "runtime may trust only the fixed Edge address",
    )
    require(
        memory_bytes(compose_default(runtime.get("mem_limit"), "CHEBY_RUNTIME_MEMORY"))
        >= 18 * 1024**3,
        "combined Gateway+Codex default must be at least 18 GiB",
    )
    require(
        float(compose_default(runtime.get("cpus"), "CHEBY_RUNTIME_CPUS")) >= 8.0,
        "combined Gateway+Codex default must be at least 8 CPUs",
    )
    require(int(runtime.get("pids_limit", 0)) >= 4096, "runtime PID limit is too small")
    require(
        set(runtime.get("networks", {})) == {"backend", "codex-egress"},
        "runtime must use only backend and Codex egress networks",
    )
    require(
        runtime["networks"]["backend"].get("ipv4_address") == "172.31.42.3",
        "runtime backend address must remain fixed",
    )

    require_hardened(edge, "edge", "101:101")
    require(
        edge.get("image") == "${CHEBY_EDGE_IMAGE:?set the scanned immutable Edge image ID}",
        "Edge image must come only from the scanned release manifest",
    )
    require(edge.get("pull_policy") == "never", "Edge pull policy must be never")
    expected_ports = {
        "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:80:8080/tcp",
        "${CHEBY_BIND_IP:?set the ECS local IPv4 mapped to the EIP}:443:8443/tcp",
    }
    require(set(edge.get("ports", [])) == expected_ports, "Edge must bind only local-IP TCP 80/443")
    require(
        set(edge.get("networks", {})) == {"backend"},
        "Edge must attach only to the internal backend",
    )
    require(
        edge["networks"]["backend"].get("ipv4_address") == "172.31.42.2",
        "Edge backend address must remain fixed",
    )

    for name, service in services.items():
        if name != "edge":
            require(not service.get("ports"), f"{name} must not publish host ports")

    image_pattern = re.compile(
        r"^certbot/certbot:v(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}$"
    )
    certbot_image = str(certbot.get("image", ""))
    match = image_pattern.fullmatch(certbot_image)
    require(match is not None, "Certbot image must be official, versioned, and digest-pinned")
    require(
        certbot_image == EXPECTED_SUPPORT_REFERENCES["certbot"],
        "Certbot image must equal the reviewed release digest",
    )
    require(
        tuple(int(part) for part in match.groups()) >= (5, 4, 0),
        "Certbot must be at least 5.4.0 for IP webroot",
    )
    require(cert_export.get("image") == certbot_image, "exporter must use the pinned Certbot image")
    require(cert_export.get("network_mode") == "none", "certificate exporter must have no network")
    require("networks" not in certbot, "Certbot must use only its isolated default network")
    require("networks" not in cert_export, "certificate exporter must not declare networks")
    require_hardened(certbot, "certbot", "0:0")
    require_hardened(cert_export, "certificate exporter", "0:0", network_mode="none")

    networks = document.get("networks", {})
    require(set(networks) == {"backend", "codex-egress"}, "top-level network set is not exact")
    require(networks.get("backend", {}).get("internal") is True, "backend must be internal")
    subnet = networks.get("backend", {}).get("ipam", {}).get("config", [{}])[0].get("subnet")
    require(subnet == "172.31.42.0/28", "backend subnet must remain the reviewed /28")

    required_volumes = {
        "gateway-data",
        "codex-home",
        "workspace-data",
        "asset-staging",
        "acme-webroot",
        "letsencrypt-state",
        "certbot-work",
        "certbot-logs",
        "tls-live",
    }
    require(required_volumes == set(document.get("volumes", {})), "persistent volume set is not exact")
    require(set(document.get("secrets", {})) == {"pairing_secret"}, "secret set is not exact")
    require(runtime.get("secrets") == ["pairing_secret"], "runtime secret mount is not exact")
    for name in ("edge", "certbot", "cert-export"):
        require("secrets" not in services[name], f"{name} must not mount Compose secrets")
    print("raw Compose security invariants: PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--compose-json", type=Path)
    source.add_argument("--compose-yaml", type=Path)
    parser.add_argument("--public-ip")
    parser.add_argument("--bind-ip")
    parser.add_argument("--pairing-secret-path")
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--allow-stale-manifest", action="store_true")
    args = parser.parse_args()

    if args.compose_yaml is not None:
        validate_source(args.compose_yaml)
        return 0

    if (
        not args.public_ip
        or not args.bind_ip
        or not args.pairing_secret_path
        or args.release_manifest is None
    ):
        parser.error(
            "--public-ip, --bind-ip, --pairing-secret-path, and --release-manifest are required with --compose-json"
        )

    public_ip = ipaddress.ip_address(args.public_ip)
    bind_ip = ipaddress.ip_address(args.bind_ip)
    require(public_ip.version == 4 and public_ip.is_global, "public IP is not global IPv4")
    require(bind_ip.version == 4, "bind IP is not IPv4")
    require(not bind_ip.is_unspecified, "bind IP must not be 0.0.0.0")

    document = json.loads(args.compose_json.read_text(encoding="utf-8"))
    require(isinstance(document, dict), "normalized Compose must be a mapping")
    require_exact_top_level(
        document, normalized=True, pairing_secret_path=args.pairing_secret_path
    )
    services = document["services"]
    require(isinstance(services, dict), "normalized Compose services must be a mapping")
    require_service_field_allowlist(services, normalized=True)
    runtime = services["runtime"]
    edge = services["edge"]
    certbot = services["certbot"]
    cert_export = services["cert-export"]
    release = load_manifest(
        args.release_manifest,
        maximum_db_age_hours=None if args.allow_stale_manifest else 48,
    )
    require_manifest_images(services, release)
    require_exact_service_values(
        services,
        normalized=True,
        release=release,
        public_ip=str(public_ip),
        bind_ip=str(bind_ip),
    )

    for name, service in services.items():
        require("build" not in service, f"{name} must not have a normalized build directive")
        require_normalized_volumes(service, name)

    require(not runtime.get("ports"), "runtime must not publish a host port")
    require_hardened(runtime, "runtime", "10002:10002")
    require(runtime.get("pull_policy") == "never", "runtime pull policy must be never")
    require(
        memory_bytes(runtime.get("mem_limit", 0)) >= 18 * 1024**3,
        "combined Gateway+Codex runtime must have at least 18 GiB",
    )
    require(float(runtime.get("cpus", 0)) >= 8.0, "runtime must have at least 8 CPUs")
    require(
        runtime["environment"].get("CHEBY_BRIDGE_MODE") == "stdio",
        "production bridge must use stdio",
    )
    require(
        runtime["environment"].get("CHEBY_LOCAL_IMAGE_ENABLED") == "true",
        "production local-image capability must be explicitly enabled",
    )
    require(
        runtime["environment"].get("CHEBY_CODEX_COMMAND")
        == "codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
        "Codex app-server must only listen on stdio",
    )
    require(
        runtime["environment"].get("CHEBY_TRUSTED_PROXY_CIDRS") == "172.31.42.2/32",
        "runtime may trust only the fixed Edge address",
    )

    ports = edge.get("ports", [])
    require(len(ports) == 2, "Edge must publish exactly two ports")
    normalized = {
        (str(port.get("host_ip")), int(port["published"]), int(port["target"]))
        for port in ports
    }
    require(
        normalized == {(str(bind_ip), 80, 8080), (str(bind_ip), 443, 8443)},
        "Edge must bind only local-EIP ports 80 and 443",
    )
    require_hardened(edge, "Edge", "101:101")
    require(edge.get("pull_policy") == "never", "Edge pull policy must be never")
    require(document["networks"]["backend"].get("internal") is True, "backend must be internal")

    require_hardened(certbot, "certbot", "0:0")
    require_hardened(cert_export, "certificate exporter", "0:0", network_mode="none")
    require(set(certbot.get("networks", {})) == {"default"}, "Certbot normalized network drifted")
    require(not cert_export.get("networks"), "certificate exporter normalized network drifted")

    image = certbot.get("image", "")
    require("@sha256:" in image, "Certbot image must be digest-pinned")
    require(image.startswith("certbot/certbot:v"), "Certbot must use the official image")
    tag = image.split("@", 1)[0]
    version = tuple(int(part) for part in tag.rsplit("v", 1)[1].split("."))
    require(version >= (5, 4, 0), "Certbot must be at least 5.4.0 for IP webroot")

    print("normalized Compose security invariants: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
