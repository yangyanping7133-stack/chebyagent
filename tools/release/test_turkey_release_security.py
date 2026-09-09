#!/usr/bin/env python3
"""Negative security tests for the Turkey release package."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import copy
import hashlib
import importlib.util
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
BUNDLE_ROOT = REPOSITORY_ROOT / "deploy" / "turkey"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def load_exporter():
    path = SCRIPT_DIR / "export_ip_certificate.py"
    specification = importlib.util.spec_from_file_location("turkey_cert_export", path)
    require(specification is not None and specification.loader is not None, "import failed")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_network_validator():
    path = SCRIPT_DIR / "validate_turkey_docker_networks.py"
    specification = importlib.util.spec_from_file_location("turkey_networks", path)
    require(specification is not None and specification.loader is not None, "import failed")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def load_release_module(name: str):
    path = SCRIPT_DIR / name
    specification = importlib.util.spec_from_file_location(
        "turkey_release_" + name.replace(".", "_"), path
    )
    require(specification is not None and specification.loader is not None, "import failed")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def require_runtime_error(callback, message: str) -> None:
    try:
        callback()
    except RuntimeError:
        return
    raise RuntimeError(message)


def require_value_error(callback, message: str) -> None:
    try:
        callback()
    except ValueError:
        return
    raise RuntimeError(message)


def render(output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "render_turkey_edge_config.py"),
            "--public-ip",
            "8.8.8.8",
            "--mode",
            "bootstrap",
            "--output",
            str(output),
            "--bundle-root",
            str(BUNDLE_ROOT),
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_renderer_rejects_special_outputs() -> None:
    with tempfile.TemporaryDirectory(prefix="cheby-render-negative-") as temporary_name:
        temporary = Path(temporary_name)
        victim = temporary / "victim"
        victim.write_text("do-not-overwrite", encoding="utf-8")
        link = temporary / "nginx.conf"
        link.symlink_to(victim)
        result = render(link)
        require(result.returncode != 0, "renderer accepted a final symlink")
        require(victim.read_text(encoding="utf-8") == "do-not-overwrite", "victim changed")

        link.unlink()
        fifo = temporary / "nginx.fifo"
        os.mkfifo(fifo, 0o600)
        result = render(fifo)
        require(result.returncode != 0, "renderer accepted a non-regular output")
        fifo.unlink()

        shared = temporary / "group-writable"
        shared.mkdir(mode=0o770)
        shared.chmod(0o770)
        result = render(shared / "nginx.conf")
        require(result.returncode != 0, "renderer accepted a group-writable parent")

        regular = temporary / "existing.conf"
        regular.write_text("old", encoding="utf-8")
        regular.chmod(0o600)
        result = render(regular)
        require(result.returncode == 0, f"renderer rejected safe output: {result.stderr}")
        mode = regular.lstat()
        require(stat.S_ISREG(mode.st_mode) and not regular.is_symlink(), "output type changed")
        require(stat.S_IMODE(mode.st_mode) == 0o600, "safe existing mode was not preserved")


def test_edge_probe_fixture_has_explicit_safe_address() -> None:
    fixture = load_release_module("edge_body_limit_upstream.py")
    original_bind = os.environ.get("CHEBY_PROBE_BIND")
    original_port = os.environ.get("CHEBY_PROBE_PORT")
    original_token = os.environ.get("CHEBY_PROBE_TOKEN")
    try:
        os.environ.pop("CHEBY_PROBE_BIND", None)
        os.environ.pop("CHEBY_PROBE_PORT", None)
        require(
            fixture.server_address() == ("0.0.0.0", 8080),
            "probe fixture default address drifted",
        )
        os.environ["CHEBY_PROBE_BIND"] = "127.0.0.1"
        os.environ["CHEBY_PROBE_PORT"] = "28081"
        require(
            fixture.server_address() == ("127.0.0.1", 28081),
            "probe fixture rejected the reviewed loopback port",
        )
        for bind, port in (
            ("192.0.2.1", "28081"),
            ("127.0.0.1", "80"),
            ("127.0.0.1", "028081"),
            ("127.0.0.1", "not-a-port"),
        ):
            os.environ["CHEBY_PROBE_BIND"] = bind
            os.environ["CHEBY_PROBE_PORT"] = port
            require_runtime_error(
                fixture.server_address,
                "probe fixture accepted an unreviewed bind address or port",
            )
        os.environ["CHEBY_PROBE_TOKEN"] = "a" * 64
        require(fixture.probe_token() == "a" * 64, "probe fixture rejected a valid token")
        for token in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
            os.environ["CHEBY_PROBE_TOKEN"] = token
            require_runtime_error(
                fixture.probe_token,
                "probe fixture accepted a malformed identity token",
            )
    finally:
        if original_bind is None:
            os.environ.pop("CHEBY_PROBE_BIND", None)
        else:
            os.environ["CHEBY_PROBE_BIND"] = original_bind
        if original_port is None:
            os.environ.pop("CHEBY_PROBE_PORT", None)
        else:
            os.environ["CHEBY_PROBE_PORT"] = original_port
        if original_token is None:
            os.environ.pop("CHEBY_PROBE_TOKEN", None)
        else:
            os.environ["CHEBY_PROBE_TOKEN"] = original_token


def test_edge_probe_config_rejects_active_drift_and_include_escape() -> None:
    validator = load_release_module("validate_edge_probe_config.py")
    production = (BUNDLE_ROOT / "edge" / "nginx.conf.template").read_text(
        encoding="utf-8"
    ).replace("__CHEBY_PUBLIC_IP__", "8.8.8.8")
    validator.validate_document(production, "production")
    probe = validator.rewrite_document(production, "a" * 64)
    validator.validate_document(probe, "probe")

    production_mutations = (
        production.replace(
            "listen 8080 default_server;",
            "listen 8080 default_server;\n        listen 0.0.0.0:28555;",
            1,
        ),
        production.replace(
            "server 172.31.42.3:8080 max_fails=2 fail_timeout=10s;",
            "server 172.31.42.3:8080 max_fails=2 fail_timeout=10s;\n"
            "        server 172.31.42.4:8080;",
            1,
        ),
        production.replace("http {", "http {\n    include /etc/nginx/conf.d/*.conf;", 1),
        production.replace(
            "upstream gateway_runtime {",
            "upstream gateway_runtime {\n        include /tmp/hidden-upstream.conf;",
            1,
        ),
    )
    for mutation in production_mutations:
        require_runtime_error(
            lambda mutation=mutation: validator.validate_document(mutation, "production"),
            "production Edge validator accepted an active listener/upstream/include escape",
        )

    probe_mutations = (
        probe.replace(
            "listen 127.0.0.1:28443 ssl default_server;",
            "listen 127.0.0.1:28443 ssl default_server;\n"
            "        listen [::1]:28443 ssl;",
            1,
        ),
        probe.replace(
            "server 127.0.0.1:28081 max_fails=2 fail_timeout=10s;",
            "server 127.0.0.1:28081 max_fails=2 fail_timeout=10s;\n"
            "        server 172.31.42.3:8080;",
            1,
        ),
        probe.replace("X-Cheby-Probe-Edge-Token", "X-Untrusted-Probe-Token", 1),
        probe.replace('"a' + "a" * 63 + '" always;', '"b' + "b" * 63 + '" always;', 1),
    )
    for mutation in probe_mutations:
        require_runtime_error(
            lambda mutation=mutation: validator.validate_document(mutation, "probe"),
            "probe Edge validator accepted residual network state or identity drift",
        )


def test_edge_probe_container_policy_rejects_mutations() -> None:
    validator = load_release_module("validate_edge_probe_script.py")
    probe_path = SCRIPT_DIR / "probe_turkey_edge_body_limits.sh"
    document = probe_path.read_text(encoding="utf-8")
    validator.validate_script(document)

    def remove_occurrence(source: str, marker: str, occurrence: int) -> str:
        position = -1
        for _ in range(occurrence + 1):
            position = source.find(marker, position + 1)
        require(position >= 0, f"missing Edge probe test marker: {marker}")
        return source[:position] + source[position + len(marker) :]

    for marker in (
        "--read-only",
        "--security-opt no-new-privileges:true",
        "--cap-drop ALL",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--tmpfs",
        "--network",
        "--no-healthcheck",
        "--interactive",
        "--noproxy '*'",
    ):
        for occurrence in range(document.count(marker)):
            mutation = remove_occurrence(document, marker, occurrence)
            require_runtime_error(
                lambda mutation=mutation: validator.validate_script(mutation),
                f"Edge probe policy accepted removal of {marker} occurrence {occurrence + 1}",
            )

    dangerous_mutations = (
        document.replace("docker run --rm", "docker run --privileged --rm", 1),
        document.replace("--cap-drop ALL", "--cap-drop ALL --cap-add SYS_ADMIN", 1),
        document.replace("--cap-drop ALL", "--cap-drop ALL --device /dev/kvm", 1),
        document.replace(
            "--cap-drop ALL", "--cap-drop ALL --device-cgroup-rule 'c 1:3 rwm'", 1
        ),
        document.replace("--network host", "--network host --pid host", 1),
        document.replace("--network host", "--network host --ipc=host", 1),
        document.replace("--network host", "--network host --uts host", 1),
        document.replace("--network host", "--network host --userns=host", 1),
        document.replace(
            "--security-opt no-new-privileges:true",
            "--security-opt no-new-privileges:true --security-opt seccomp=unconfined",
            1,
        ),
        document.replace(
            "--security-opt no-new-privileges:true",
            "--security-opt no-new-privileges:true --security-opt apparmor=unconfined",
            1,
        ),
        document.replace("--read-only", "--read-only --volume /var/run/docker.sock:/sock", 1),
        document.replace("--read-only", "--read-only --volume /etc:/host-etc:ro", 1),
        document.replace("--read-only", "--read-only --volume=/:/host:rw", 1),
        document.replace("--read-only", "--read-only -v/:/host:rw", 1),
        document.replace(
            "--read-only", '--read-only -v"$PWD:/host:rw"', 1
        ),
        document.replace("--read-only", "--read-only -v'$probe_directory/tls:/host:rw'", 1),
        document.replace("--read-only", '--read-only -v"$probe_directory/tls"', 1),
        document.replace("--read-only", "--read-only --mount type=bind,src=/,dst=/host", 1),
        document + "\niptables -F\n",
        document + "\nip6tables -F\n",
        document + "\nnft flush ruleset\n",
        document + "\nfirewall-cmd --reload\n",
        document + "\nufw disable\n",
        document + "\ndocker network create attacker\n",
        document + "\ndocker network connect attacker victim\n",
        document + "\ndocker network rm attacker\n",
        document.replace("--network host", "--network host --network-alias attacker", 1),
        document.replace("--network host", "--network host --net host", 1),
        document.replace("--network host", "--network host --net=host", 1),
        document.replace("--memory 128m", "--memory 128m -m 0", 1),
        document.replace("--memory 128m", "--memory 128m -m=0", 1),
        document.replace("--memory 128m", "--memory 128m -m0", 1),
    )
    for mutation in dangerous_mutations:
        require_runtime_error(
            lambda mutation=mutation: validator.validate_script(mutation),
            "Edge probe policy accepted a dangerous container/network/firewall mutation",
        )

    neutralization_mutations = (
        document.replace("--read-only", "--read-only --read-only=false", 1),
        document.replace(
            "--security-opt no-new-privileges:true",
            "--security-opt no-new-privileges:true --security-opt no-new-privileges:false",
            1,
        ),
        document.replace("--cap-drop ALL", "--cap-drop ALL --cap-drop NONE", 1),
        document.replace("--pids-limit 32", "--pids-limit 32 --pids-limit -1", 1),
        document.replace("--memory 128m", "--memory 128m --memory 0", 1),
        document.replace("--cpus 1", "--cpus 1 --cpus 0", 1),
        document.replace(
            "--tmpfs /tmp:size=16m,mode=1777",
            "--tmpfs /tmp:size=16m,mode=1777 --tmpfs /tmp:size=1g",
            1,
        ),
        document.replace("--network none", "--network none --network host", 1),
        document.replace(
            "--no-healthcheck", "--no-healthcheck --no-healthcheck=false", 1
        ),
        document.replace("docker run --rm", "docker run --rm --interactive", 1),
        document.replace("--interactive", "--interactive --interactive=false", 1),
        document.replace("--noproxy '*'", "--noproxy '*' --noproxy '*'", 1),
        document.replace("--noproxy '*'", "--noproxy '*' --noproxy ''", 1),
        document.replace(
            "--noproxy '*'", "--noproxy '*' --noproxy attacker.invalid", 1
        ),
        document.replace("--noproxy '*'", "--noproxy '*' --noproxy=", 1),
    )
    for mutation in neutralization_mutations:
        require_runtime_error(
            lambda mutation=mutation: validator.validate_script(mutation),
            "Edge probe policy accepted a duplicate or neutralized security constraint",
        )

    missing_client = document.replace('start_probe_client "$global_client_three"\n', "", 1)
    require_runtime_error(
        lambda: validator.validate_script(missing_client),
        "Edge probe policy accepted a missing long-lived client instance",
    )


def fake_lets_encrypt_chain() -> tuple[bytes, bytes]:
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name(
        [
            x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "Let's Encrypt"),
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "Fake Local Root"),
        ]
    )
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "8.8.8.8")]))
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=159))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("8.8.8.8"))]),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM)
    root_pem = root.public_bytes(serialization.Encoding.PEM)
    return leaf_pem, leaf_pem + root_pem


def test_fake_lets_encrypt_chain_is_rejected() -> None:
    exporter = load_exporter()
    leaf, fullchain = fake_lets_encrypt_chain()
    try:
        exporter.verify_trusted_chain(leaf, fullchain)
    except RuntimeError as error:
        require("system anchors" in str(error), f"unexpected fake-chain rejection: {error}")
    else:
        raise RuntimeError("self-signed O=Let's Encrypt chain was accepted")


def test_certificate_volume_rejects_symlinks() -> None:
    exporter = load_exporter()
    original_root = exporter.DESTINATION_ROOT
    with tempfile.TemporaryDirectory(prefix="cheby-cert-volume-negative-") as temporary_name:
        temporary = Path(temporary_name)
        destination = temporary / "tls"
        destination.mkdir(mode=0o700)
        victim = temporary / "victim"
        victim.mkdir(mode=0o700)
        root_link = temporary / "tls-link"
        root_link.symlink_to(victim, target_is_directory=True)
        exporter.DESTINATION_ROOT = root_link
        try:
            exporter.destination_root()
        except RuntimeError as error:
            require("real directory" in str(error), f"unexpected root-link error: {error}")
        else:
            raise RuntimeError("certificate publisher accepted a symlink /tls root")

        (destination / "releases").symlink_to(victim, target_is_directory=True)
        exporter.DESTINATION_ROOT = destination
        try:
            try:
                exporter.prepare_releases()
            except RuntimeError as error:
                require("real directory" in str(error), f"unexpected release-link error: {error}")
            else:
                raise RuntimeError("certificate publisher followed releases symlink")

            (destination / "releases").unlink()
            releases = destination / "releases"
            releases.mkdir(mode=0o711)
            (releases / ("a" * 32)).symlink_to(victim, target_is_directory=True)
            try:
                exporter.release_directories(releases)
            except RuntimeError as error:
                require("uncontrolled candidate" in str(error), f"unexpected candidate error: {error}")
            else:
                raise RuntimeError("certificate cleanup accepted a symlink candidate")

            (releases / ("a" * 32)).unlink()
            (releases / ("a" * 32)).mkdir(mode=0o500)
            escaped = destination / "current"
            escaped.symlink_to("../victim")
            try:
                exporter.release_target(escaped, releases)
            except RuntimeError as error:
                require("controlled relative link" in str(error), f"unexpected pointer error: {error}")
            else:
                raise RuntimeError("certificate publisher accepted an escaped current pointer")
        finally:
            exporter.DESTINATION_ROOT = original_root


def test_docker_network_impersonation_is_rejected() -> None:
    validator = load_network_validator()
    reviewed = {
        "Name": "chebycodex-turkey_backend",
        "Id": "1" * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Options": {"com.docker.network.bridge.name": "chebybr0"},
        "Labels": {
            "com.docker.compose.project": "chebycodex-turkey",
            "com.docker.compose.network": "backend",
        },
        "IPAM": {"Config": [{"Subnet": "172.31.42.0/28"}]},
    }
    binding = validator.validate([reviewed])
    require(
        binding is not None
        and binding.network_id == "1" * 64
        and binding.bridge == "chebybr0",
        "reviewed Docker network did not produce an exact route binding",
    )
    validator.validate_bridge_link(
        [{"ifname": "chebybr0", "linkinfo": {"info_kind": "bridge"}}],
        binding,
    )
    for fake_link in (
        [{"ifname": "tun0", "linkinfo": {"info_kind": "tun"}}],
        [{"ifname": "chebybr0", "linkinfo": {"info_kind": "tun"}}],
        [],
    ):
        require_runtime_error(
            lambda fake_link=fake_link: validator.validate_bridge_link(
                fake_link, binding
            ),
            "fake/TUN Docker backend interface was accepted",
        )
    default_bridge = copy.deepcopy(reviewed)
    default_bridge["Options"] = {}
    default_binding = validator.validate([default_bridge])
    require(
        default_binding is not None and default_binding.bridge == "br-" + "1" * 12,
        "Docker default bridge was not derived from the exact network ID",
    )
    for mutation in (
        {**reviewed, "Internal": False},
        {**reviewed, "Labels": {}},
        {**reviewed, "IPAM": {"Config": [{"Subnet": "172.31.42.0/27"}]}},
        {**reviewed, "Id": "not-an-exact-network-id"},
        {**reviewed, "Driver": "macvlan"},
        {**reviewed, "Scope": "swarm"},
        {
            **reviewed,
            "Options": {"com.docker.network.bridge.name": "bridge-name-is-too-long"},
        },
        {
            "Name": "attacker",
            "Internal": True,
            "Labels": {},
            "IPAM": {"Config": [{"Subnet": "172.31.42.8/29"}]},
        },
    ):
        try:
            validator.validate([mutation])
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Docker network impersonation/overlap was accepted")


def test_host_and_vpn_route_overlap_is_rejected() -> None:
    validator = load_network_validator()
    reviewed = {
        "Name": "chebycodex-turkey_backend",
        "Id": "2" * 64,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Options": {"com.docker.network.bridge.name": "chebybr0"},
        "Labels": {
            "com.docker.compose.project": "chebycodex-turkey",
            "com.docker.compose.network": "backend",
        },
        "IPAM": {"Config": [{"Subnet": "172.31.42.0/28"}]},
    }
    no_binding = validator.validate([])
    validator.validate_host_routes(
        [
            {"dst": "default", "gateway": "192.0.2.1"},
            {"dst": "10.0.0.0/8", "dev": "eth0"},
        ],
        [{"dst": "2001:db8::/32", "dev": "tun0"}],
        no_binding,
    )
    for destination in ("172.31.42.8/32", "172.31.0.0/16", "172.31.42.0/28"):
        require_runtime_error(
            lambda destination=destination: validator.validate_host_routes(
                [{"dst": destination, "dev": "tun0"}], [], no_binding
            ),
            f"first-deploy overlapping host/VPN route was accepted: {destination}",
        )

    binding = validator.validate([reviewed])
    exact_main = {
        "dst": "172.31.42.0/28",
        "dev": "chebybr0",
        "protocol": "kernel",
        "scope": "link",
        "table": "main",
    }
    exact_local = {
        "type": "local",
        "dst": "172.31.42.1/32",
        "dev": "chebybr0",
        "protocol": "kernel",
        "scope": "host",
        "table": "local",
    }
    exact_broadcast = {
        "type": "broadcast",
        "dst": "172.31.42.15/32",
        "dev": "chebybr0",
        "protocol": "kernel",
        "scope": "link",
        "table": 255,
    }
    validator.validate_host_routes(
        [exact_main, exact_local, exact_broadcast], [], binding
    )
    for mutation in (
        {**exact_main, "dev": "tun0"},
        {**exact_main, "dev": "br-attacker"},
        {**exact_main, "table": 100},
        {**exact_main, "protocol": "static"},
        {**exact_main, "scope": "global"},
        {**exact_main, "dst": "172.31.42.8/32"},
        {**exact_main, "type": "blackhole"},
        {**exact_local, "scope": "link"},
        {**exact_local, "dst": "172.31.0.0/16"},
    ):
        require_runtime_error(
            lambda mutation=mutation: validator.validate_host_routes(
                [mutation], [], binding
            ),
            "route not bound to the exact reviewed network/dev/table/proto/scope was accepted",
        )


def test_pairing_secret_requires_canonical_256_bits() -> None:
    validator = load_release_module("validate_pairing_secret.py")
    canonical = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=")
    validator.validate_secret_bytes(canonical)
    for weak in (
        b"password",
        b"a" * 42,
        b"a" * 44,
        b"0" * 64,
        canonical + b"\n",
        canonical[:-1] + b"=",
    ):
        require_runtime_error(
            lambda weak=weak: validator.validate_secret_bytes(weak),
            "weak or noncanonical pairing secret was accepted",
        )


def test_root_execution_tree_and_docker_endpoint_fail_closed() -> None:
    trust = load_release_module("validate_turkey_host_trust.py")
    with tempfile.TemporaryDirectory(prefix="cheby-root-trust-negative-") as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        script = temporary / "root-script.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o700)
        trust.require_trusted_component(script, os.getuid(), directory=False)
        script.chmod(0o720)
        require_runtime_error(
            lambda: trust.require_trusted_component(script, os.getuid(), directory=False),
            "group-writable root execution input was accepted",
        )
        script.chmod(0o700)
        link = temporary / "linked-script.sh"
        link.symlink_to(script)
        require_runtime_error(
            lambda: trust.require_trusted_component(link, os.getuid(), directory=False),
            "symlinked root execution input was accepted",
        )
        cache = temporary / "__pycache__"
        cache.mkdir(mode=0o700)
        require_runtime_error(
            lambda: trust.reject_python_bytecode(temporary),
            "production trust accepted a Python bytecode cache directory",
        )
        cache.rmdir()
        bytecode = temporary / "release.pyc"
        bytecode.write_bytes(b"not-bytecode")
        require_runtime_error(
            lambda: trust.reject_python_bytecode(temporary),
            "production trust accepted a Python bytecode file",
        )

    original_host = os.environ.get("DOCKER_HOST")
    os.environ["DOCKER_HOST"] = "tcp://attacker.invalid:2375"
    try:
        require_runtime_error(
            trust.validate_docker_socket,
            "remote/unreviewed Docker endpoint was accepted",
        )
    finally:
        if original_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = original_host


def test_tls_fingerprint_rejects_another_valid_leaf() -> None:
    monitor = load_release_module("monitor_ip_tls.py")
    first_leaf, _ = fake_lets_encrypt_chain()
    second_leaf, _ = fake_lets_encrypt_chain()
    first_der = x509.load_pem_x509_certificate(first_leaf).public_bytes(
        serialization.Encoding.DER
    )
    second_der = x509.load_pem_x509_certificate(second_leaf).public_bytes(
        serialization.Encoding.DER
    )
    expected = hashes.Hash(hashes.SHA256())
    expected.update(first_der)
    fingerprint = expected.finalize().hex()
    monitor.verify_expected_fingerprint(first_der, fingerprint)
    require_runtime_error(
        lambda: monitor.verify_expected_fingerprint(second_der, fingerprint),
        "a different valid leaf certificate satisfied fingerprint binding",
    )


def test_trivy_rfc3339_parser_is_strict_and_cross_version_safe() -> None:
    validator = load_release_module("validate_trivy_report.py")
    for fraction, expected_microsecond in (
        ("1", 100000),
        ("92", 920000),
        ("123", 123000),
        ("123456", 123456),
        ("123456789", 123456),
    ):
        parsed = validator.parse_rfc3339(
            f"2026-07-07T17:59:46.{fraction}Z"
        )
        require(
            parsed == datetime(
                2026,
                7,
                7,
                17,
                59,
                46,
                expected_microsecond,
                timezone.utc,
            ),
            f"RFC 3339 {len(fraction)}-digit secfrac parsed incorrectly",
        )
    for timestamp, expected_offset in (
        ("2026-07-07T17:59:46Z", timedelta(0)),
        ("2026-07-07T17:59:46.92+00:00", timedelta(0)),
        ("2026-07-07T17:59:46.92+03:00", timedelta(hours=3)),
        ("2026-07-07T17:59:46.92-05:30", -timedelta(hours=5, minutes=30)),
    ):
        parsed = validator.parse_rfc3339(timestamp)
        require(
            parsed.utcoffset() == expected_offset,
            f"RFC 3339 timezone offset changed for {timestamp}",
        )
    require(
        validator.parse_rfc3339("2026-07-07T17:59:46.92+03:00")
        .astimezone(timezone.utc)
        == datetime(2026, 7, 7, 14, 59, 46, 920000, timezone.utc),
        "RFC 3339 timezone conversion lost instant semantics",
    )
    for timestamp, expected_utc in (
        (
            "0001-01-01T23:59:00+23:59",
            datetime(1, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        ),
        (
            "9999-12-31T00:00:59-23:59",
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        ),
    ):
        require(
            validator.parse_rfc3339(timestamp).astimezone(timezone.utc)
            == expected_utc,
            f"representable RFC 3339 maximum offset boundary changed: {timestamp}",
        )
    require(
        validator.parse_rfc3339("2026-07-07T17:59:46.123456999Z")
        == datetime(2026, 7, 7, 17, 59, 46, 123456, timezone.utc),
        "RFC 3339 nanosecond truncation exceeded the documented 999 ns bound",
    )
    tolerance_now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    tolerance_boundary = validator.require_fresh_timestamp(
        "2026-07-07T12:05:00.000000999Z",
        now=tolerance_now,
        maximum_age_seconds=3600,
        future_tolerance_seconds=300,
    )
    require(
        tolerance_boundary
        == datetime(2026, 7, 7, 12, 5, 0, tzinfo=timezone.utc),
        "future tolerance boundary did not preserve bounded nanosecond truncation",
    )
    require_runtime_error(
        lambda: validator.require_fresh_timestamp(
            "2026-07-07T12:05:00.000001000Z",
            now=tolerance_now,
            maximum_age_seconds=3600,
            future_tolerance_seconds=300,
        ),
        "timestamp one microsecond beyond future tolerance was accepted",
    )
    for timestamp in (
        "2026-07-07T17:59:46.1234567890Z",
        "2026-07-07T17:59:46.Z",
        "2026-07-07T17:59:46,92Z",
        "2026-07-07 17:59:46.92Z",
        "2026-07-07T17:59:46.92z",
        "2026-07-07T17:59:46.92",
        "2026-07-07T17:59:46.92+3:00",
        "2026-07-07T17:59:46.92+0300",
        "2026-07-07T17:59:46.92+24:00",
        "2026-07-07T17:59:46.92+03:60",
        "2026-07-07T17:59:46.92-00:00",
        "2026-02-30T17:59:46.92Z",
        "2026-07-07T24:00:00Z",
        "2026-07-07T17:60:00Z",
        "2026-07-07T17:59:60Z",
        "2026-07-07T17:59:46.92Ztrailing",
        "2026-07-07T17:59:4٦.92Z",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ):
        require_value_error(
            lambda timestamp=timestamp: validator.parse_rfc3339(timestamp),
            f"malformed RFC 3339 timestamp was accepted: {timestamp!r}",
        )
    for value in (None, True, 1, b"2026-07-07T17:59:46Z"):
        require_value_error(
            lambda value=value: validator.parse_rfc3339(value),
            "non-string RFC 3339 timestamp was accepted",
        )


def test_trivy_missing_results_findings_and_stale_db_fail_closed() -> None:
    validator = load_release_module("validate_trivy_report.py")
    now = datetime.now(timezone.utc)
    clean_context_envelope = {
        "SchemaVersion": 2,
        "ArtifactName": "/scan",
        "ArtifactType": "filesystem",
        "CreatedAt": now.isoformat().replace("+00:00", "Z"),
        "ReportID": "019f7a2d-0095-78ff-ae17-b2fc9f0a309d",
        "Trivy": {"Version": "0.72.0"},
    }
    validator.validate_scan_report(
        clean_context_envelope,
        "exact-build-context",
        expected_artifact_name="/scan",
        expected_artifact_type="filesystem",
        now=now,
    )
    complete_context = copy.deepcopy(clean_context_envelope)
    complete_context["Results"] = [
        {"Target": "requirements.txt", "Class": "lang-pkgs", "Type": "pip"}
    ]
    validator.validate_scan_report(
        complete_context,
        "exact-build-context",
        expected_artifact_name="/scan",
        expected_artifact_type="filesystem",
        now=now,
    )
    valid_low_vulnerability = {
        "VulnerabilityID": "CVE-2026-0001",
        "PkgID": "example@1.0",
        "PkgName": "example",
        "PkgIdentifier": {"PURL": "pkg:pypi/example@1.0", "UID": "abc123"},
        "InstalledVersion": "1.0",
        "FixedVersion": "1.1",
        "Status": "fixed",
        "Layer": {"Digest": "sha256:" + "1" * 64, "DiffID": "sha256:" + "2" * 64},
        "SeveritySource": "nvd",
        "PrimaryURL": "https://example.invalid/CVE-2026-0001",
        "DataSource": {
            "ID": "alpine",
            "Name": "Alpine Secdb",
            "URL": "https://secdb.alpinelinux.org/",
        },
        "Fingerprint": "sha256:" + "3" * 64,
        "Title": "reviewed low-severity fixture",
        "Description": "fixture",
        "Severity": "LOW",
        "CweIDs": ["CWE-1"],
        "VendorSeverity": {"nvd": 1},
        "CVSS": {"nvd": {"V3Vector": "CVSS:3.1/test", "V3Score": 1.0}},
        "References": ["https://example.invalid/advisory"],
        "PublishedDate": "2026-01-01T00:00:00Z",
        "LastModifiedDate": "2026-01-02T00:00:00Z",
    }
    require(
        validator.validate_vulnerability(valid_low_vulnerability, "valid-low") == "LOW",
        "reviewed Trivy vulnerability shape did not validate",
    )
    low_report = copy.deepcopy(clean_context_envelope)
    low_report["Results"] = [
        {
            "Target": "requirements.txt",
            "Class": "lang-pkgs",
            "Type": "pip",
            "Packages": [],
            "Vulnerabilities": [valid_low_vulnerability],
            "MisconfSummary": None,
            "Misconfigurations": [],
            "Licenses": [],
            "CustomResources": [],
            "ExperimentalModifiedFindings": [],
        }
    ]
    validator.validate_scan_report(
        low_report,
        "valid-low",
        expected_artifact_name="/scan",
        expected_artifact_type="filesystem",
        now=now,
    )
    package_layer = {"DiffID": "sha256:" + "4" * 64}

    def observed_package(
        name: str,
        version: str,
        analyzed_by: str,
        *,
        package_id: str | None = None,
        **fields,
    ) -> dict[str, object]:
        package = {
            "Name": name,
            "Version": version,
            "Identifier": {
                "PURL": f"pkg:generic/{name}@{version}",
                "UID": hashlib.sha256(name.encode()).hexdigest()[:16],
            },
            "Layer": package_layer,
            "AnalyzedBy": analyzed_by,
            **fields,
        }
        if package_id is not None:
            package["ID"] = package_id
        return package

    alpine_package = observed_package(
        "busybox",
        "1.37.0-r0",
        "apk",
        package_id="busybox@1.37.0-r0",
        Arch="x86_64",
        Digest="sha1:fixture",
        DependsOn=["so:libc.musl-x86_64.so.1"],
        InstalledFiles=["/bin/busybox"],
        Licenses=["GPL-2.0-only"],
        Maintainer="Alpine Developers",
        SrcName="busybox",
        SrcVersion="1.37.0-r0",
    )
    node_package = observed_package(
        "example-node",
        "1.0.0",
        "node-pkg",
        package_id="example-node@1.0.0",
        FilePath="/app/package-lock.json",
        Licenses=["MIT"],
        DependsOn=["dependency@1.0.0"],
        Relationship="direct",
    )
    python_package = observed_package(
        "example-python",
        "2.0.0",
        "python-pkg",
        FilePath="/usr/lib/python/site-packages/example.dist-info/METADATA",
        Licenses=["Apache-2.0"],
        DependsOn=[],
        Relationship="root",
    )
    rust_package = observed_package(
        "example-rust",
        "3.0.0",
        "rustbinary",
        package_id="example-rust@3.0.0",
        FilePath="/usr/bin/example-rust",
        Licenses=["MIT"],
        Relationship="root",
    )
    go_package = observed_package(
        "example-go",
        "v4.0.0",
        "gobinary",
        package_id="example-go@v4.0.0",
        FilePath="/usr/bin/example-go",
        Relationship="root",
    )
    go_root_package = {
        "Name": "example.org/reviewed-root",
        "ID": "example.org/reviewed-root",
        "Identifier": {
            "PURL": "pkg:golang/example.org/reviewed-root",
            "UID": "reviewed-root-uid",
        },
        "Layer": package_layer,
        "AnalyzedBy": "gobinary",
        "DependsOn": ["example.org/reviewed-dependency@v1.0.0"],
        "Relationship": "root",
    }
    package_report = copy.deepcopy(clean_context_envelope)
    package_report["Results"] = [
        {
            "Target": "alpine",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Packages": [alpine_package],
        },
        {
            "Target": "package-lock.json",
            "Class": "lang-pkgs",
            "Type": "node-pkg",
            "Packages": [node_package],
        },
        {
            "Target": "python metadata",
            "Class": "lang-pkgs",
            "Type": "python-pkg",
            "Packages": [python_package],
        },
        {
            "Target": "rust binary one",
            "Class": "lang-pkgs",
            "Type": "rustbinary",
            "Packages": [rust_package] * 506,
        },
        {
            "Target": "rust binary two",
            "Class": "lang-pkgs",
            "Type": "rustbinary",
            "Packages": [copy.deepcopy(rust_package)] * 506,
        },
        {
            "Target": "go binary",
            "Class": "lang-pkgs",
            "Type": "gobinary",
            "Packages": [go_root_package, go_package],
        },
    ]
    validator.validate_scan_report(
        package_report,
        "observed-package-inventory",
        expected_artifact_name="/scan",
        expected_artifact_type="filesystem",
        now=now,
    )
    blocking_package_report = copy.deepcopy(package_report)
    blocking_package_finding = copy.deepcopy(valid_low_vulnerability)
    blocking_package_finding["Severity"] = "HIGH"
    blocking_package_report["Results"][0]["Vulnerabilities"] = [
        blocking_package_finding
    ]
    require_runtime_error(
        lambda: validator.validate_scan_report(
            blocking_package_report,
            "package-inventory-with-high",
            expected_artifact_name="/scan",
            expected_artifact_type="filesystem",
            now=now,
        ),
        "non-empty Trivy Packages bypassed a HIGH vulnerability verdict",
    )

    def require_invalid_package(
        package: object,
        *,
        result_class: str = "os-pkgs",
        result_type: str = "alpine",
    ) -> None:
        require_runtime_error(
            lambda: validator.validate_findings(
                [
                    {
                        "Target": "package-negative",
                        "Class": result_class,
                        "Type": result_type,
                        "Packages": [package],
                    }
                ],
                "package-negative",
            ),
            "malformed Trivy package inventory was accepted",
        )

    package_mutations = []
    unknown_package_field = copy.deepcopy(alpine_package)
    unknown_package_field["Unreviewed"] = True
    package_mutations.append(unknown_package_field)
    for field, value in (
        ("Name", 123),
        ("AnalyzedBy", ["apk"]),
        ("Identifier", []),
        ("Layer", []),
        ("Licenses", "MIT"),
        ("Licenses", [1]),
        ("DependsOn", [None]),
        ("InstalledFiles", [1]),
    ):
        mutation = copy.deepcopy(alpine_package)
        mutation[field] = value
        package_mutations.append(mutation)
    for field in ("Name", "Version", "ID", "Identifier", "Layer", "AnalyzedBy"):
        mutation = copy.deepcopy(alpine_package)
        mutation.pop(field)
        package_mutations.append(mutation)
    identifier_missing_uid = copy.deepcopy(alpine_package)
    identifier_missing_uid["Identifier"] = {
        "PURL": identifier_missing_uid["Identifier"]["PURL"]
    }
    package_mutations.append(identifier_missing_uid)
    identifier_with_bom_ref = copy.deepcopy(alpine_package)
    identifier_with_bom_ref["Identifier"]["BOMRef"] = "unreviewed-bom-ref"
    package_mutations.append(identifier_with_bom_ref)
    layer_missing_diff_id = copy.deepcopy(alpine_package)
    layer_missing_diff_id["Layer"] = {
        "Digest": "sha256:" + "5" * 64,
    }
    package_mutations.append(layer_missing_diff_id)
    layer_empty_diff_id = copy.deepcopy(alpine_package)
    layer_empty_diff_id["Layer"] = {"DiffID": ""}
    package_mutations.append(layer_empty_diff_id)
    layer_unknown_field = copy.deepcopy(alpine_package)
    layer_unknown_field["Layer"] = {
        "DiffID": layer_unknown_field["Layer"]["DiffID"],
        "CreatedBy": "unreviewed",
    }
    package_mutations.append(layer_unknown_field)
    oversized_package_string = copy.deepcopy(alpine_package)
    oversized_package_string["Name"] = "x" * (
        validator.MAX_PACKAGE_STRING_BYTES + 1
    )
    package_mutations.append(oversized_package_string)
    oversized_package_list = copy.deepcopy(alpine_package)
    oversized_package_list["DependsOn"] = ["dependency"] * (
        validator.PACKAGE_LIST_LIMITS["DependsOn"] + 1
    )
    package_mutations.append(oversized_package_list)
    oversized_nested_string = copy.deepcopy(alpine_package)
    oversized_nested_string["Licenses"] = [
        "x" * (validator.MAX_PACKAGE_STRING_BYTES + 1)
    ]
    package_mutations.append(oversized_nested_string)
    for package in package_mutations:
        require_invalid_package(package)

    os_with_language_field = copy.deepcopy(alpine_package)
    os_with_language_field["FilePath"] = "/unreviewed"
    require_invalid_package(os_with_language_field)
    language_with_os_field = copy.deepcopy(node_package)
    language_with_os_field["Arch"] = "amd64"
    require_invalid_package(
        language_with_os_field, result_class="lang-pkgs", result_type="node-pkg"
    )
    invalid_relationship = copy.deepcopy(node_package)
    invalid_relationship["Relationship"] = "transitive-unreviewed"
    require_invalid_package(
        invalid_relationship, result_class="lang-pkgs", result_type="node-pkg"
    )
    for field in ("DependsOn", "Relationship"):
        mutation = copy.deepcopy(go_root_package)
        mutation.pop(field)
        require_invalid_package(
            mutation, result_class="lang-pkgs", result_type="gobinary"
        )
    for field, value in (
        ("Relationship", "direct"),
        ("AnalyzedBy", "gomod"),
        ("ID", "example.org/different-root"),
        ("FilePath", "/unreviewed/versionless-root"),
    ):
        mutation = copy.deepcopy(go_root_package)
        mutation[field] = value
        require_invalid_package(
            mutation, result_class="lang-pkgs", result_type="gobinary"
        )
    versioned_root_purl = copy.deepcopy(go_root_package)
    versioned_root_purl["Identifier"]["PURL"] += "@v1.0.0"
    require_invalid_package(
        versioned_root_purl, result_class="lang-pkgs", result_type="gobinary"
    )
    for module_path in (
        "example.org",
        "example.org/_reviewed-root",
        "example.org/-reviewed-root",
        "example.org/~reviewed-root",
        "example.org/Reviewed_ROOT",
        "example.org/reviewed-root/v2",
        "example.org/reviewed-root/v10",
        "example.org/reviewed-root/v",
        "example.org/reviewed-root/v2beta",
        "gopkg.in",
        "gopkg.in/reviewed.v0",
        "gopkg.in/reviewed.v1",
        "gopkg.in/reviewed.v2",
        "gopkg.in/reviewed.v2-unstable",
    ):
        mutation = copy.deepcopy(go_root_package)
        mutation["Name"] = module_path
        mutation["ID"] = module_path
        mutation["Identifier"]["PURL"] = f"pkg:golang/{module_path}"
        validator.validate_package_inventory_item(
            mutation, "lang-pkgs", "gobinary", "valid-go-module-path"
        )
    for suffix in ("@v1.2.3", "?type=module", "#cmd"):
        mutation = copy.deepcopy(go_root_package)
        mutation["Name"] += suffix
        mutation["ID"] = mutation["Name"]
        mutation["Identifier"]["PURL"] = f"pkg:golang/{mutation['Name']}"
        require_invalid_package(
            mutation, result_class="lang-pkgs", result_type="gobinary"
        )
    noncanonical_module_path = copy.deepcopy(go_root_package)
    noncanonical_module_path["Name"] = "local/reviewed-root"
    noncanonical_module_path["ID"] = noncanonical_module_path["Name"]
    noncanonical_module_path["Identifier"]["PURL"] = (
        f"pkg:golang/{noncanonical_module_path['Name']}"
    )
    require_invalid_package(
        noncanonical_module_path,
        result_class="lang-pkgs",
        result_type="gobinary",
    )
    for module_path in (
        "example..org/reviewed-root",
        "-bad.example/reviewed-root",
        "Example.org/reviewed-root",
        "example_org/reviewed-root",
        "/example.org/reviewed-root",
        "example.org/reviewed-root/",
        "example.org//reviewed-root",
        "example.org/.reviewed-root",
        "example.org/reviewed-root.",
        "example.org/../reviewed-root",
        "example.org/%72eviewed-root",
        "example.org/reviewed root",
        "example.org/reviewed\\root",
        "example.org/reviewed\nroot",
        "example.org/reviewed\x00root",
        "example.org/reviewed@root",
        "example.org/reviewed?type=module",
        "example.org/reviewed#cmd",
        "example.org/CON",
        "example.org/EXAMPL~1",
        "example.org/reviewed-root/v0",
        "example.org/reviewed-root/v1",
        "example.org/reviewed-root/v01",
        "example.org/reviewed-root/v1.2",
        "gopkg.in/reviewed",
        "gopkg.in/reviewed.v01",
        "gopkg.in/reviewed.v0-unstable",
        "gopkg.in/reviewed.v1.2",
    ):
        mutation = copy.deepcopy(go_root_package)
        mutation["Name"] = module_path
        mutation["ID"] = module_path
        mutation["Identifier"]["PURL"] = f"pkg:golang/{module_path}"
        require_invalid_package(
            mutation, result_class="lang-pkgs", result_type="gobinary"
        )
    for dependencies in (
        [],
        "example.org/not-a-list",
        1,
        True,
        1.5,
        [go_root_package["ID"]],
    ):
        mutation = copy.deepcopy(go_root_package)
        mutation["DependsOn"] = dependencies
        require_invalid_package(
            mutation, result_class="lang-pkgs", result_type="gobinary"
        )
    ordinary_go_without_version = copy.deepcopy(go_package)
    ordinary_go_without_version.pop("Version")
    require_invalid_package(
        ordinary_go_without_version,
        result_class="lang-pkgs",
        result_type="gobinary",
    )
    second_go_root = copy.deepcopy(go_root_package)
    second_go_root["Name"] = "example.net/second-root"
    second_go_root["ID"] = second_go_root["Name"]
    second_go_root["Identifier"]["PURL"] = f"pkg:golang/{second_go_root['Name']}"
    require_runtime_error(
        lambda: validator.validate_findings(
            [
                {
                    "Target": "multiple-versionless-go-roots",
                    "Class": "lang-pkgs",
                    "Type": "gobinary",
                    "Packages": [go_root_package, second_go_root],
                }
            ],
            "multiple-versionless-go-roots",
        ),
        "multiple versionless roots were accepted in one gobinary result",
    )
    for package, result_type, required_fields in (
        (alpine_package, "alpine", ("Arch", "Digest")),
        (node_package, "node-pkg", ("FilePath", "ID", "Licenses")),
        (python_package, "python-pkg", ("FilePath", "Licenses")),
        (rust_package, "rustbinary", ("ID",)),
        (go_package, "gobinary", ("ID",)),
    ):
        for field in required_fields:
            mutation = copy.deepcopy(package)
            mutation.pop(field)
            require_invalid_package(
                mutation,
                result_class=("os-pkgs" if result_type == "alpine" else "lang-pkgs"),
                result_type=result_type,
            )
    require_runtime_error(
        lambda: validator.validate_findings(
            [
                {
                    "Target": "packages-non-list",
                    "Class": "os-pkgs",
                    "Type": "alpine",
                    "Packages": {},
                }
            ],
            "packages-non-list",
        ),
        "non-list Trivy Packages was accepted",
    )
    require_runtime_error(
        lambda: validator.validate_findings(
            [
                {
                    "Target": "packages-result-limit",
                    "Class": "lang-pkgs",
                    "Type": "gobinary",
                    "Packages": [go_package]
                    * (validator.MAX_PACKAGES_PER_RESULT + 1),
                }
            ],
            "packages-result-limit",
        ),
        "oversized per-result Trivy Packages was accepted",
    )
    report_limit_results = [
        {
            "Target": f"packages-report-limit-{index}",
            "Class": "lang-pkgs",
            "Type": "gobinary",
            "Packages": [go_package] * validator.MAX_PACKAGES_PER_RESULT,
        }
        for index in range(
            validator.MAX_PACKAGES_PER_REPORT // validator.MAX_PACKAGES_PER_RESULT
            + 1
        )
    ]
    require_runtime_error(
        lambda: validator.validate_findings(
            report_limit_results, "packages-report-limit"
        ),
        "oversized report-wide Trivy Packages was accepted",
    )
    original_nested_item_limit = validator.MAX_PACKAGE_NESTED_ITEMS_PER_REPORT
    try:
        validator.MAX_PACKAGE_NESTED_ITEMS_PER_REPORT = 2
        aggregate_nested_packages = []
        for dependencies in (
            ["dependency-a", "dependency-b"],
            ["dependency-c"],
        ):
            package = copy.deepcopy(go_package)
            package["DependsOn"] = dependencies
            aggregate_nested_packages.append(package)
        require_runtime_error(
            lambda: validator.validate_findings(
                [
                    {
                        "Target": "packages-nested-report-limit",
                        "Class": "lang-pkgs",
                        "Type": "gobinary",
                        "Packages": aggregate_nested_packages,
                    }
                ],
                "packages-nested-report-limit",
            ),
            "oversized report-wide nested Trivy package inventory was accepted",
        )
    finally:
        validator.MAX_PACKAGE_NESTED_ITEMS_PER_REPORT = original_nested_item_limit
    require_runtime_error(
        lambda: validator.validate_findings(
            [
                {
                    "Target": "secret-with-packages",
                    "Class": "secret",
                    "Packages": [copy.deepcopy(go_package)],
                }
            ],
            "secret-with-packages",
        ),
        "non-empty valid Trivy Packages was accepted on the secret class",
    )
    compact_clean = json.dumps(clean_context_envelope, separators=(",", ":"))
    compact_complete = json.dumps(complete_context, separators=(",", ":"))
    compact_low = json.dumps(low_report, separators=(",", ":"))
    compact_packages = json.dumps(package_report, separators=(",", ":"))
    duplicate_report_documents = (
        compact_clean.replace(
            '"SchemaVersion":2', '"SchemaVersion":1,"SchemaVersion":2', 1
        ),
        compact_complete.replace(
            '"Target":"requirements.txt"',
            '"Target":"wrong","Target":"requirements.txt"',
            1,
        ),
        compact_low.replace(
            '"Severity":"LOW"', '"Severity":"CRITICAL","Severity":"LOW"', 1
        ),
        compact_packages.replace(
            '"Name":"busybox"', '"Name":123,"Name":"busybox"', 1
        ),
    )
    with tempfile.TemporaryDirectory(prefix="cheby-duplicate-trivy-json-") as name:
        temporary = Path(name)
        for index, document in enumerate(duplicate_report_documents):
            validator.validate_scan_report(
                json.loads(document),
                "permissive-duplicate-control",
                expected_artifact_name="/scan",
                expected_artifact_type="filesystem",
                now=now,
            )
            report_path = temporary / f"duplicate-{index}.json"
            report_path.write_text(document, encoding="utf-8")
            require_runtime_error(
                lambda report_path=report_path: validator.load(report_path),
                "Trivy evidence parser accepted a duplicate object key",
            )
    valid_secret = {
        "RuleID": "fixture-secret",
        "Category": "Custom",
        "Severity": "LOW",
        "Title": "fixture secret",
        "StartLine": 1,
        "EndLine": 1,
        "Code": {
            "Lines": [
                {
                    "Number": 1,
                    "Content": "secret=********",
                    "IsCause": True,
                    "Annotation": "",
                    "Truncated": False,
                    "Highlighted": "secret=********",
                    "FirstCause": True,
                    "LastCause": True,
                }
            ]
        },
        "Match": "secret=********",
        "Offset": 0,
    }
    validator.validate_secret(valid_secret, "valid-secret-shape")
    invalid_nested_vulnerability = copy.deepcopy(valid_low_vulnerability)
    invalid_nested_vulnerability["DataSource"]["Unreviewed"] = "value"
    require_runtime_error(
        lambda: validator.validate_vulnerability(
            invalid_nested_vulnerability, "nested-vulnerability"
        ),
        "vulnerability with an unknown nested field was accepted",
    )
    invalid_nested_secret = copy.deepcopy(valid_secret)
    invalid_nested_secret["Code"]["Lines"][0]["Unreviewed"] = True
    require_runtime_error(
        lambda: validator.validate_secret(invalid_nested_secret, "nested-secret"),
        "secret with an unknown nested field was accepted",
    )
    for field in (
        "SchemaVersion",
        "ArtifactName",
        "ArtifactType",
        "CreatedAt",
        "ReportID",
        "Trivy",
    ):
        incomplete = dict(clean_context_envelope)
        incomplete.pop(field)
        require_runtime_error(
            lambda incomplete=incomplete: validator.validate_scan_report(
                incomplete,
                "exact-build-context",
                expected_artifact_name="/scan",
                expected_artifact_type="filesystem",
                now=now,
            ),
            f"context envelope missing {field} was accepted",
        )
    unsafe_reports = []
    for field, value in (
        ("SchemaVersion", True),
        ("ArtifactName", "/wrong"),
        ("ArtifactType", "container_image"),
        ("CreatedAt", (now - timedelta(hours=2)).isoformat()),
        ("CreatedAt", 123),
        ("ReportID", 123),
        ("Trivy", {"Version": "0.71.0"}),
        ("Trivy", {"Version": "0.72.0", "Server": {}}),
    ):
        mutation = copy.deepcopy(clean_context_envelope)
        mutation[field] = value
        unsafe_reports.append(mutation)
    unexpected = copy.deepcopy(clean_context_envelope)
    unexpected["Unreviewed"] = True
    unsafe_reports.append(unexpected)
    malformed_result = copy.deepcopy(clean_context_envelope)
    malformed_result["Results"] = [{"Target": "x", "Class": "secret", "Type": "secret", "Secrets": {}}]
    unsafe_reports.append(malformed_result)
    invalid_severity = copy.deepcopy(clean_context_envelope)
    invalid_severity["Results"] = [
        {
            "Target": "x",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Vulnerabilities": [
                {
                    **valid_low_vulnerability,
                    "Severity": 5,
                }
            ],
        }
    ]
    unsafe_reports.append(invalid_severity)
    blocking = copy.deepcopy(clean_context_envelope)
    blocking["Results"] = [
        {
            "Target": "x",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Vulnerabilities": [
                {
                    **valid_low_vulnerability,
                    "Severity": "CRITICAL",
                }
            ],
        }
    ]
    unsafe_reports.append(blocking)
    severity_only = copy.deepcopy(clean_context_envelope)
    severity_only["Results"] = [
        {
            "Target": "x",
            "Class": "os-pkgs",
            "Type": "alpine",
            "Vulnerabilities": [{"Severity": "LOW"}],
        }
    ]
    unsafe_reports.append(severity_only)
    for core_field in ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity"):
        for empty in (False, True):
            incomplete_finding = copy.deepcopy(valid_low_vulnerability)
            if empty:
                incomplete_finding[core_field] = ""
            else:
                incomplete_finding.pop(core_field)
            incomplete_report = copy.deepcopy(clean_context_envelope)
            incomplete_report["Results"] = [
                {
                    "Target": "x",
                    "Class": "os-pkgs",
                    "Type": "alpine",
                    "Vulnerabilities": [incomplete_finding],
                }
            ]
            unsafe_reports.append(incomplete_report)
    secret = copy.deepcopy(clean_context_envelope)
    secret["Results"] = [
        {
            "Target": "x",
            "Class": "secret",
            "Secrets": [valid_secret],
        }
    ]
    unsafe_reports.append(secret)
    for result in (
        {"Target": "x", "Class": "lang-pkgs", "Type": "pip", "Packages": [1]},
        {"Target": "x", "Class": "lang-pkgs", "Type": "pip", "MisconfSummary": 123},
        {
            "Target": "x",
            "Class": "lang-pkgs",
            "Type": "pip",
            "ExperimentalModifiedFindings": [1],
        },
        {"Target": "x", "Class": "config", "Type": "dockerfile"},
        {"Target": "x", "Class": "os-pkgs", "Type": "pip"},
        {"Target": "x", "Class": "lang-pkgs", "Type": "future-package-manager"},
        {"Target": "x", "Class": "secret", "Type": "secret", "Secrets": []},
    ):
        mutation = copy.deepcopy(clean_context_envelope)
        mutation["Results"] = [result]
        unsafe_reports.append(mutation)
    unknown_vulnerability_field = copy.deepcopy(low_report)
    unknown_vulnerability_field["Results"][0]["Vulnerabilities"][0]["Unreviewed"] = True
    unsafe_reports.append(unknown_vulnerability_field)
    for report in unsafe_reports:
        require_runtime_error(
            lambda report=report: validator.validate_scan_report(
                report,
                "negative",
                expected_artifact_name="/scan",
                expected_artifact_type="filesystem",
                now=now,
            ),
            "malformed, wrong-target, stale, or blocking Trivy result was accepted",
        )

    image_id = "sha256:" + "a" * 64
    clean_image = {
        **clean_context_envelope,
        "ArtifactName": "/evidence/runtime.tar",
        "ArtifactType": "container_image",
        "ArtifactID": image_id,
        "Metadata": {"ImageID": image_id},
        "Results": [{"Target": "runtime (alpine 3.23)", "Class": "os-pkgs", "Type": "alpine"}],
    }
    validator.validate_scan_report(
        clean_image,
        "immutable-runtime",
        expected_artifact_name="/evidence/runtime.tar",
        expected_artifact_type="container_image",
        expected_target=image_id,
        now=now,
    )
    for field, value in (
        ("ArtifactName", "/evidence/edge.tar"),
        ("ArtifactID", "sha256:" + "b" * 64),
        ("Metadata", {"ImageID": "sha256:" + "b" * 64}),
        ("Results", []),
        ("Results", None),
    ):
        mutation = copy.deepcopy(clean_image)
        mutation[field] = value
        require_runtime_error(
            lambda mutation=mutation: validator.validate_scan_report(
                mutation,
                "immutable-runtime",
                expected_artifact_name="/evidence/runtime.tar",
                expected_artifact_type="container_image",
                expected_target=image_id,
                now=now,
            ),
            "empty or wrong-target immutable image report was accepted",
        )

    database = {
        "Version": 2,
        "UpdatedAt": (now - timedelta(hours=1)).isoformat(),
        "NextUpdate": (now + timedelta(hours=5)).isoformat(),
        "DownloadedAt": (now - timedelta(minutes=30)).isoformat(),
    }
    validator.validate_version_report(
        {"Version": "0.72.0", "VulnerabilityDB": database},
        now=now,
    )
    validator.validate_version_report(
        {"Version": "0.72.0-cheby.1", "VulnerabilityDB": database},
        now=now,
        expected_scanner_version="0.72.0-cheby.1",
    )
    for version_report in (
        {"Version": "0.71.0", "VulnerabilityDB": database},
        {"Version": "0.72.0"},
        {
            "Version": "0.72.0",
            "VulnerabilityDB": {**database, "UpdatedAt": (now - timedelta(hours=49)).isoformat()},
        },
        {"Version": "0.72.0", "VulnerabilityDB": {**database, "Version": "2"}},
        {"Version": "0.72.0", "VulnerabilityDB": {**database, "DownloadedAt": 123}},
        {
            "Version": "0.72.0",
            "VulnerabilityDB": {
                **database,
                "DownloadedAt": (
                    now - timedelta(hours=1, microseconds=1)
                ).isoformat(),
            },
        },
        {"Version": "0.72.0", "VulnerabilityDB": {**database, "NextUpdate": (now - timedelta(hours=2)).isoformat()}},
        {"Version": "0.72.0", "VulnerabilityDB": database, "Unreviewed": {}},
    ):
        require_runtime_error(
            lambda version_report=version_report: validator.validate_version_report(
                version_report, now=now
            ),
            "wrong scanner, missing DB, or stale DB was accepted",
        )

    findings_lock_path = BUNDLE_ROOT / "trivy-official-findings.lock.json"
    findings_lock = json.loads(findings_lock_path.read_text(encoding="utf-8"))
    blocking_findings = []
    for vulnerability_id, package_name, installed, fixed, severity in findings_lock[
        "blockingFindings"
    ]:
        finding = copy.deepcopy(valid_low_vulnerability)
        finding.update(
            {
                "VulnerabilityID": vulnerability_id,
                "PkgName": package_name,
                "InstalledVersion": installed,
                "Severity": severity,
            }
        )
        if fixed is None:
            finding.pop("FixedVersion", None)
        else:
            finding["FixedVersion"] = fixed
        blocking_findings.append(finding)
    official_antiblindness = copy.deepcopy(clean_context_envelope)
    official_antiblindness.update(
        {
            "ArtifactName": "/evidence/official-trivy.tar",
            "ArtifactType": "container_image",
            "ArtifactID": image_id,
            "Metadata": {"ImageID": image_id},
            "Results": [
                {
                    "Target": "official-trivy",
                    "Class": "lang-pkgs",
                    "Type": "gobinary",
                    "Vulnerabilities": blocking_findings,
                }
            ],
        }
    )
    custom_antiblindness = copy.deepcopy(official_antiblindness)
    custom_antiblindness["Trivy"]["Version"] = "0.72.0-cheby.1"
    fingerprint = validator.validate_antiblindness_reports(
        official_antiblindness,
        custom_antiblindness,
        findings_lock_path,
        expected_artifact_name="/evidence/official-trivy.tar",
        expected_target=image_id,
        now=now,
    )
    require(
        fingerprint
        == "fb8a3de7c31a6f8f8775202a39a3761542eb9b7436d329a71c5dae1ae0d48bb3",
        "official Trivy anti-blindness tuple fingerprint drifted",
    )
    for mutation in (
        lambda report: report["Results"][0]["Vulnerabilities"].pop(),
        lambda report: report["Results"][0]["Vulnerabilities"][0].update(
            {"PkgName": "replacement-package"}
        ),
        lambda report: report["Trivy"].update({"Version": "0.72.0"}),
    ):
        candidate = copy.deepcopy(custom_antiblindness)
        mutation(candidate)
        require_runtime_error(
            lambda candidate=candidate: validator.validate_antiblindness_reports(
                official_antiblindness,
                candidate,
                findings_lock_path,
                expected_artifact_name="/evidence/official-trivy.tar",
                expected_target=image_id,
                now=now,
            ),
            "shrunk, replaced, or wrong-version anti-blindness report was accepted",
        )

    with tempfile.TemporaryDirectory(prefix="cheby-trivy-db-cache-") as cache_name:
        cache = Path(cache_name)
        cache.chmod(0o700)
        database_directory = cache / "db"
        database_directory.mkdir(mode=0o700)
        metadata_path = database_directory / "metadata.json"
        database_path = database_directory / "trivy.db"
        metadata_path.write_text('{"Version":2}\n', encoding="utf-8")
        database_path.write_bytes(b"reviewed-trivy-db-fixture")
        metadata_path.chmod(0o600)
        database_path.chmod(0o600)
        hashes = validator.validate_database_cache(cache, os.getuid())
        require(
            hashes
            == (
                hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
                hashlib.sha256(database_path.read_bytes()).hexdigest(),
            ),
            "Trivy DB cache hashes were not content-bound",
        )
        require(
            validator.MAX_TRIVY_DATABASE_BYTES == 1280 * 1024 * 1024,
            "Trivy DB byte ceiling differs from the reviewed 1.25 GiB bound",
        )
        with database_path.open("r+b") as database_handle:
            database_handle.truncate(validator.MAX_TRIVY_DATABASE_BYTES + 1)
        require_runtime_error(
            lambda: validator.validate_database_cache(cache, os.getuid()),
            "Trivy DB cache accepted a sparse file above its reviewed byte ceiling",
        )
        database_path.write_bytes(b"reviewed-trivy-db-fixture")
        database_path.chmod(0o600)
        extra = cache / "unreviewed"
        extra.write_text("x", encoding="utf-8")
        require_runtime_error(
            lambda: validator.validate_database_cache(cache, os.getuid()),
            "Trivy DB cache accepted an extra file",
        )
        extra.unlink()
        for index in range(validator.MAX_DATABASE_CACHE_ENTRIES + 2):
            overflow = cache / f"overflow-{index:02d}"
            overflow.write_text("x", encoding="utf-8")
            overflow.chmod(0o600)
        require_runtime_error(
            lambda: validator.validate_database_cache(cache, os.getuid()),
            "Trivy DB cache accepted an inventory beyond its preallocation cap",
        )
        for overflow in cache.glob("overflow-*"):
            overflow.unlink()
        metadata_path.chmod(0o620)
        require_runtime_error(
            lambda: validator.validate_database_cache(cache, os.getuid()),
            "Trivy DB cache accepted a group-writable file",
        )
        metadata_path.chmod(0o600)
        metadata_path.write_text('{"Version":1,"Version":2}\n', encoding="utf-8")
        require_runtime_error(
            lambda: validator.validate_database_cache(cache, os.getuid()),
            "Trivy DB cache accepted duplicate JSON metadata keys",
        )

    validator_source = (SCRIPT_DIR / "validate_trivy_report.py").read_text(
        encoding="utf-8"
    )
    require(
        "database_path.read_bytes()" not in validator_source
        and "metadata_path.read_bytes()" not in validator_source
        and 'path.rglob("*")' not in validator_source,
        "Trivy DB validation can still preallocate unbounded cache evidence",
    )
    require_runtime_error(
        lambda: validator.stream_sha256(
            io.BytesIO(b"growing-db"), maximum_bytes=3, expected_bytes=3
        ),
        "streaming DB hash did not enforce its reviewed byte bound",
    )
    with tempfile.TemporaryDirectory(prefix="cheby-trivy-json-evidence-") as name:
        temporary = Path(name)
        valid_evidence = temporary / "valid.json"
        valid_evidence.write_text("{}\n", encoding="utf-8")
        require(validator.load(valid_evidence) == {}, "bounded JSON evidence did not load")
        linked_evidence = temporary / "linked.json"
        linked_evidence.symlink_to(valid_evidence)
        require_runtime_error(
            lambda: validator.load(linked_evidence),
            "Trivy evidence loader followed a symlink",
        )
        oversized_evidence = temporary / "oversized.json"
        with oversized_evidence.open("wb") as handle:
            handle.truncate(validator.MAX_JSON_EVIDENCE_BYTES + 1)
        require_runtime_error(
            lambda: validator.load(oversized_evidence),
            "Trivy evidence loader accepted an oversized sparse file",
        )

    with tempfile.TemporaryDirectory(prefix="cheby-docker-archive-") as name:
        temporary = Path(name)
        layer_one = b"first-uncompressed-layer-tar-stream"
        layer_two = b"second-uncompressed-layer-tar-stream"
        diff_one = "sha256:" + hashlib.sha256(layer_one).hexdigest()
        diff_two = "sha256:" + hashlib.sha256(layer_two).hexdigest()
        no_layer_sources = object()

        def write_archive(
            archive_path: Path,
            *,
            config_document: object,
            manifest_layers: list[str],
            layer_files: dict[str, bytes],
            layer_sources: object = no_layer_sources,
        ) -> str:
            config = json.dumps(config_document, separators=(",", ":")).encode()
            expected_id = "sha256:" + hashlib.sha256(config).hexdigest()
            if layer_sources is no_layer_sources:
                manifest_entry = {
                    "Config": expected_id.removeprefix("sha256:") + ".json",
                    "RepoTags": None,
                    "Layers": manifest_layers,
                }
                members_to_write = {
                    "manifest.json": json.dumps([manifest_entry]).encode(),
                    expected_id.removeprefix("sha256:") + ".json": config,
                    **layer_files,
                }
            else:
                config_name = "blobs/sha256/" + expected_id.removeprefix("sha256:")
                rootfs = config_document.get("rootfs", {}) if isinstance(config_document, dict) else {}
                configured_diff_ids = rootfs.get("diff_ids", []) if isinstance(rootfs, dict) else []
                layer_descriptors = []
                for position, layer_name in enumerate(manifest_layers):
                    content = layer_files.get(layer_name, b"")
                    digest = (
                        configured_diff_ids[position]
                        if position < len(configured_diff_ids)
                        and isinstance(configured_diff_ids[position], str)
                        else "sha256:" + hashlib.sha256(content).hexdigest()
                    )
                    layer_descriptors.append(
                        {
                            "mediaType": validator.DOCKER_LAYER_MEDIA_TYPE,
                            "digest": digest,
                            "size": len(content),
                        }
                    )
                oci_manifest = json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": validator.OCI_MANIFEST_MEDIA_TYPE,
                        "config": {
                            "mediaType": validator.OCI_CONFIG_MEDIA_TYPE,
                            "digest": expected_id,
                            "size": len(config),
                        },
                        "layers": layer_descriptors,
                    },
                    separators=(",", ":"),
                ).encode()
                oci_manifest_digest = "sha256:" + hashlib.sha256(oci_manifest).hexdigest()
                oci_manifest_name = (
                    "blobs/sha256/" + oci_manifest_digest.removeprefix("sha256:")
                )
                index = json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": validator.OCI_INDEX_MEDIA_TYPE,
                        "manifests": [
                            {
                                "mediaType": validator.OCI_MANIFEST_MEDIA_TYPE,
                                "digest": oci_manifest_digest,
                                "size": len(oci_manifest),
                                "platform": {"architecture": "amd64", "os": "linux"},
                            }
                        ],
                    },
                    separators=(",", ":"),
                ).encode()
                manifest_entry = {
                    "Config": config_name,
                    "RepoTags": None,
                    "Layers": manifest_layers,
                    "LayerSources": layer_sources,
                }
                members_to_write = {
                    "manifest.json": json.dumps([manifest_entry]).encode(),
                    "oci-layout": json.dumps(
                        {"imageLayoutVersion": validator.OCI_LAYOUT_VERSION},
                        separators=(",", ":"),
                    ).encode(),
                    "index.json": index,
                    config_name: config,
                    oci_manifest_name: oci_manifest,
                    **layer_files,
                }
                parent = None
                for position in range(len(manifest_layers)):
                    history_id = hashlib.sha256(
                        f"reviewed-history-{position}".encode()
                    ).hexdigest()
                    history_document = {
                        "container_config": {},
                        "created": "2026-01-01T00:00:00Z",
                        "id": history_id,
                        "os": "linux",
                    }
                    if parent is not None:
                        history_document["parent"] = parent
                    if position == len(manifest_layers) - 1:
                        history_document["architecture"] = (
                            config_document.get("architecture", "amd64")
                            if isinstance(config_document, dict)
                            else "amd64"
                        )
                        history_document["config"] = {}
                    history = json.dumps(
                        history_document, separators=(",", ":")
                    ).encode()
                    history_name = "blobs/sha256/" + hashlib.sha256(history).hexdigest()
                    members_to_write[history_name] = history
                    parent = history_id
            with tarfile.open(archive_path, "w") as archive:
                if layer_sources is not no_layer_sources:
                    for directory in sorted(validator.HYBRID_REQUIRED_DIRECTORIES):
                        info = tarfile.TarInfo(directory)
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                for entry_name, content in members_to_write.items():
                    info = tarfile.TarInfo(entry_name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            archive_path.chmod(0o600)
            return expected_id

        def write_raw_archive(
            archive_path: Path,
            *,
            config: bytes,
            manifest: bytes,
            layer_files: dict[str, bytes],
        ) -> str:
            expected_id = "sha256:" + hashlib.sha256(config).hexdigest()
            with tarfile.open(archive_path, "w") as archive:
                for entry_name, content in (
                    ("manifest.json", manifest),
                    (expected_id.removeprefix("sha256:") + ".json", config),
                    *layer_files.items(),
                ):
                    info = tarfile.TarInfo(entry_name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            archive_path.chmod(0o600)
            return expected_id

        archive_path = temporary / "image.tar"
        base_config = {
            "architecture": "amd64",
            "config": {"Env": []},
            "created": "2026-01-01T00:00:00Z",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_one]},
        }
        expected_id = write_archive(
            archive_path,
            config_document=base_config,
            manifest_layers=["layer-one/layer.tar"],
            layer_files={"layer-one/layer.tar": layer_one},
        )
        validator.validate_image_archive(archive_path, expected_id, os.getuid())
        require_runtime_error(
            lambda: validator.validate_image_archive(
                archive_path, "sha256:" + "f" * 64, os.getuid()
            ),
            "Docker archive with the wrong immutable image ID was accepted",
        )

        bad_archives: list[tuple[Path, str, str]] = []
        blob_layer = "blobs/sha256/" + diff_one.removeprefix("sha256:")
        valid_layer_source = {
            "digest": diff_one,
            "mediaType": validator.DOCKER_LAYER_MEDIA_TYPE,
            "size": len(layer_one),
        }
        docker_26 = temporary / "docker-26.tar"
        docker_26_id = write_archive(
            docker_26,
            config_document=base_config,
            manifest_layers=[blob_layer],
            layer_files={blob_layer: layer_one},
            layer_sources={diff_one: valid_layer_source},
        )
        validator.validate_image_archive(docker_26, docker_26_id, os.getuid())

        blob_layer_two = "blobs/sha256/" + diff_two.removeprefix("sha256:")
        hybrid_config = {
            "architecture": "amd64",
            "config": {"Env": ["FIXTURE=1"]},
            "created": "2026-01-01T00:00:00Z",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_one, diff_two]},
        }
        hybrid_sources = {
            diff_one: valid_layer_source,
            diff_two: {
                "digest": diff_two,
                "mediaType": validator.DOCKER_LAYER_MEDIA_TYPE,
                "size": len(layer_two),
            },
        }
        hybrid_chain = temporary / "docker-26-chain.tar"
        hybrid_chain_id = write_archive(
            hybrid_chain,
            config_document=hybrid_config,
            manifest_layers=[blob_layer, blob_layer_two],
            layer_files={blob_layer: layer_one, blob_layer_two: layer_two},
            layer_sources=hybrid_sources,
        )
        validator.validate_image_archive(hybrid_chain, hybrid_chain_id, os.getuid())

        def read_regular_members(archive_path: Path) -> dict[str, bytes]:
            result: dict[str, bytes] = {}
            with tarfile.open(archive_path, "r:") as archive:
                for member in archive.getmembers():
                    if member.isreg():
                        handle = archive.extractfile(member)
                        require(handle is not None, "hybrid fixture member is unreadable")
                        result[member.name] = handle.read()
            return result

        def write_member_archive(
            archive_path: Path,
            members: dict[str, bytes],
            directories: set[str] | None = None,
        ) -> None:
            with tarfile.open(archive_path, "w") as archive:
                for directory in sorted(
                    validator.HYBRID_REQUIRED_DIRECTORIES
                    if directories is None
                    else directories
                ):
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                for entry_name, content in members.items():
                    info = tarfile.TarInfo(entry_name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            archive_path.chmod(0o600)

        hybrid_members = read_regular_members(hybrid_chain)

        def hybrid_manifest(members: dict[str, bytes]) -> dict[str, object]:
            document = json.loads(members["manifest.json"])
            require(
                isinstance(document, list)
                and len(document) == 1
                and isinstance(document[0], dict),
                "hybrid fixture manifest is malformed",
            )
            return document[0]

        def hybrid_index(members: dict[str, bytes]) -> dict[str, object]:
            document = json.loads(members["index.json"])
            require(isinstance(document, dict), "hybrid fixture index is malformed")
            return document

        def replace_oci_manifest(
            members: dict[str, bytes], document: dict[str, object]
        ) -> None:
            index_document = hybrid_index(members)
            descriptor = index_document["manifests"][0]
            old_name = "blobs/sha256/" + descriptor["digest"].removeprefix("sha256:")
            members.pop(old_name)
            content = json.dumps(document, separators=(",", ":")).encode()
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            new_name = "blobs/sha256/" + digest.removeprefix("sha256:")
            members[new_name] = content
            descriptor["digest"] = digest
            descriptor["size"] = len(content)
            members["index.json"] = json.dumps(
                index_document, separators=(",", ":")
            ).encode()

        def current_oci_manifest(members: dict[str, bytes]) -> dict[str, object]:
            index_document = hybrid_index(members)
            digest = index_document["manifests"][0]["digest"]
            document = json.loads(
                members["blobs/sha256/" + digest.removeprefix("sha256:")]
            )
            require(isinstance(document, dict), "hybrid OCI fixture is malformed")
            return document

        def add_hybrid_mutation(
            description: str,
            mutate,
        ) -> None:
            members = copy.deepcopy(hybrid_members)
            mutate(members)
            mutation_path = temporary / f"hybrid-{len(bad_archives)}.tar"
            write_member_archive(mutation_path, members)
            bad_archives.append((mutation_path, hybrid_chain_id, description))

        def edit_json_member(members, name, edit) -> None:
            document = json.loads(members[name])
            edit(document)
            members[name] = json.dumps(document, separators=(",", ":")).encode()

        def edit_oci_manifest(members, edit) -> None:
            document = current_oci_manifest(members)
            edit(document)
            replace_oci_manifest(members, document)

        def history_names(members: dict[str, bytes]) -> list[str]:
            manifest_document = hybrid_manifest(members)
            index_document = hybrid_index(members)
            oci_name = (
                "blobs/sha256/"
                + index_document["manifests"][0]["digest"].removeprefix("sha256:")
            )
            known = {
                manifest_document["Config"],
                oci_name,
                *manifest_document["Layers"],
            }
            return sorted(
                name
                for name in members
                if name.startswith("blobs/sha256/") and name not in known
            )

        def replace_history_document(members, history_name, document) -> None:
            members.pop(history_name)
            content = json.dumps(document, separators=(",", ":")).encode()
            replacement = "blobs/sha256/" + hashlib.sha256(content).hexdigest()
            members[replacement] = content

        add_hybrid_mutation(
            "hybrid wrong config path",
            lambda members: edit_json_member(
                members,
                "manifest.json",
                lambda document: document[0].__setitem__(
                    "Config", "blobs/sha256/" + "f" * 64
                ),
            ),
        )
        add_hybrid_mutation(
            "hybrid layer order drift",
            lambda members: edit_json_member(
                members,
                "manifest.json",
                lambda document: document[0]["Layers"].reverse(),
            ),
        )
        add_hybrid_mutation(
            "missing hybrid config blob",
            lambda members: members.pop(hybrid_manifest(members)["Config"]),
        )
        add_hybrid_mutation(
            "missing OCI layout", lambda members: members.pop("oci-layout")
        )
        add_hybrid_mutation(
            "wrong OCI layout version",
            lambda members: edit_json_member(
                members,
                "oci-layout",
                lambda document: document.__setitem__("imageLayoutVersion", "1.0.1"),
            ),
        )
        add_hybrid_mutation(
            "unknown OCI layout metadata",
            lambda members: edit_json_member(
                members,
                "oci-layout",
                lambda document: document.__setitem__("Unreviewed", True),
            ),
        )
        add_hybrid_mutation(
            "missing OCI index", lambda members: members.pop("index.json")
        )
        for description, field, value in (
            ("wrong OCI index schema", "schemaVersion", 1),
            ("wrong OCI index media type", "mediaType", "application/octet-stream"),
            ("empty OCI index manifests", "manifests", []),
        ):
            add_hybrid_mutation(
                description,
                lambda members, field=field, value=value: edit_json_member(
                    members,
                    "index.json",
                    lambda document, field=field, value=value: document.__setitem__(
                        field, value
                    ),
                ),
            )
        for description, field, value in (
            (
                "wrong OCI index descriptor media type",
                "mediaType",
                "application/octet-stream",
            ),
            ("wrong OCI index descriptor digest", "digest", "sha256:" + "f" * 64),
            ("wrong OCI index descriptor size", "size", 1),
        ):
            add_hybrid_mutation(
                description,
                lambda members, field=field, value=value: edit_json_member(
                    members,
                    "index.json",
                    lambda document, field=field, value=value: document["manifests"][
                        0
                    ].__setitem__(field, value),
                ),
            )
        add_hybrid_mutation(
            "OCI index platform differs from config",
            lambda members: edit_json_member(
                members,
                "index.json",
                lambda document: document["manifests"][0]["platform"].__setitem__(
                    "architecture", "arm64"
                ),
            ),
        )
        add_hybrid_mutation(
            "missing OCI manifest blob",
            lambda members: members.pop(
                "blobs/sha256/"
                + hybrid_index(members)["manifests"][0]["digest"].removeprefix(
                    "sha256:"
                )
            ),
        )

        for description, edit in (
            (
                "wrong OCI manifest schema",
                lambda document: document.__setitem__("schemaVersion", 1),
            ),
            (
                "wrong OCI manifest media type",
                lambda document: document.__setitem__(
                    "mediaType", "application/octet-stream"
                ),
            ),
            (
                "wrong OCI config descriptor digest",
                lambda document: document["config"].__setitem__(
                    "digest", "sha256:" + "f" * 64
                ),
            ),
            (
                "wrong OCI config descriptor size",
                lambda document: document["config"].__setitem__("size", 1),
            ),
            (
                "wrong OCI config descriptor media type",
                lambda document: document["config"].__setitem__(
                    "mediaType", "application/octet-stream"
                ),
            ),
            (
                "wrong OCI layer descriptor digest",
                lambda document: document["layers"][0].__setitem__(
                    "digest", diff_two
                ),
            ),
            (
                "wrong OCI layer descriptor size",
                lambda document: document["layers"][0].__setitem__("size", 1),
            ),
            (
                "wrong OCI layer descriptor media type",
                lambda document: document["layers"][0].__setitem__(
                    "mediaType", "application/octet-stream"
                ),
            ),
            (
                "wrong OCI layer descriptor order",
                lambda document: document["layers"].reverse(),
            ),
            (
                "missing OCI layer descriptor",
                lambda document: document["layers"].pop(),
            ),
            (
                "unknown OCI manifest metadata",
                lambda document: document.__setitem__("Unreviewed", True),
            ),
            (
                "unknown OCI layer descriptor metadata",
                lambda document: document["layers"][0].__setitem__(
                    "Unreviewed", True
                ),
            ),
        ):
            add_hybrid_mutation(
                description,
                lambda members, edit=edit: edit_oci_manifest(members, edit),
            )

        def remove_history(members) -> None:
            members.pop(history_names(members)[0])

        add_hybrid_mutation("missing history blob", remove_history)

        def add_extra_blob(members) -> None:
            content = b"unreviewed-extra-blob"
            members["blobs/sha256/" + hashlib.sha256(content).hexdigest()] = content

        add_hybrid_mutation("extra undeclared blob", add_extra_blob)
        add_hybrid_mutation(
            "unknown non-blob regular member",
            lambda members: members.__setitem__("UNREVIEWED.txt", b"unexpected"),
        )

        unknown_directory_path = temporary / "hybrid-unknown-directory.tar"
        write_member_archive(
            unknown_directory_path,
            copy.deepcopy(hybrid_members),
            directories=validator.HYBRID_REQUIRED_DIRECTORIES | {"unreviewed"},
        )
        bad_archives.append(
            (
                unknown_directory_path,
                hybrid_chain_id,
                "unknown hybrid directory member",
            )
        )

        missing_directory_path = temporary / "hybrid-missing-directory.tar"
        write_member_archive(
            missing_directory_path,
            copy.deepcopy(hybrid_members),
            directories={"blobs"},
        )
        bad_archives.append(
            (
                missing_directory_path,
                hybrid_chain_id,
                "missing required hybrid directory member",
            )
        )

        manifest_document = hybrid_manifest(hybrid_members)
        index_document = hybrid_index(hybrid_members)
        known_blob_names = {
            manifest_document["Config"],
            "blobs/sha256/"
            + index_document["manifests"][0]["digest"].removeprefix("sha256:"),
            *manifest_document["Layers"],
        }
        blob_infos: dict[str, tarfile.TarInfo] = {}
        for blob_name, content in hybrid_members.items():
            if blob_name.startswith("blobs/sha256/"):
                info = tarfile.TarInfo(blob_name)
                info.size = len(content)
                blob_infos[blob_name] = info
        oversized_history_infos = copy.deepcopy(blob_infos)
        oversized_history_name = next(
            name for name in oversized_history_infos if name not in known_blob_names
        )
        oversized_history_infos[oversized_history_name].size = (
            validator.MAX_DOCKER_HISTORY_BYTES + 1
        )
        require_runtime_error(
            lambda: validator.classify_hybrid_history_members(
                oversized_history_infos, known_blob_names, 2
            ),
            "oversized history metadata was not rejected before payload access",
        )
        oversized_unknown_infos = copy.deepcopy(blob_infos)
        unknown_info = tarfile.TarInfo("blobs/sha256/" + "f" * 64)
        unknown_info.size = validator.MAX_DOCKER_ARCHIVE_BYTES
        oversized_unknown_infos[unknown_info.name] = unknown_info
        require_runtime_error(
            lambda: validator.classify_hybrid_history_members(
                oversized_unknown_infos, known_blob_names, 2
            ),
            "oversized unknown blob was not rejected before payload access",
        )

        def mutate_history(members, edit, *, child: bool = False) -> None:
            candidates = history_names(members)
            selected_name = candidates[0]
            selected = json.loads(members[selected_name])
            if child:
                for candidate in candidates:
                    document = json.loads(members[candidate])
                    if "parent" in document:
                        selected_name = candidate
                        selected = document
                        break
            edit(selected)
            replace_history_document(members, selected_name, selected)

        for description, edit, child in (
            (
                "history unknown metadata",
                lambda document: document.__setitem__("Unreviewed", True),
                False,
            ),
            (
                "history noncanonical ID",
                lambda document: document.__setitem__("id", "not-a-history-id"),
                False,
            ),
            (
                "history wrong OS",
                lambda document: document.__setitem__("os", "windows"),
                False,
            ),
            (
                "history container config wrong type",
                lambda document: document.__setitem__("container_config", []),
                False,
            ),
            (
                "history created wrong type",
                lambda document: document.__setitem__("created", 123),
                False,
            ),
            (
                "history invalid parent reference",
                lambda document: document.__setitem__("parent", "f" * 64),
                True,
            ),
            (
                "history missing final config",
                lambda document: document.pop("config", None),
                True,
            ),
            (
                "history final created drift",
                lambda document: document.__setitem__(
                    "created", "2026-01-02T00:00:00Z"
                ),
                True,
            ),
            (
                "history final architecture drift",
                lambda document: document.__setitem__("architecture", "arm64"),
                True,
            ),
            (
                "history final config wrong type",
                lambda document: document.__setitem__("config", []),
                True,
            ),
        ):
            add_hybrid_mutation(
                description,
                lambda members, edit=edit, child=child: mutate_history(
                    members, edit, child=child
                ),
            )

        def duplicate_history_id(members) -> None:
            candidates = history_names(members)
            documents = {
                candidate: json.loads(members[candidate]) for candidate in candidates
            }
            root = next(document for document in documents.values() if "parent" not in document)
            child_name = next(
                candidate
                for candidate, document in documents.items()
                if "parent" in document
            )
            child = documents[child_name]
            child["id"] = root["id"]
            replace_history_document(members, child_name, child)

        add_hybrid_mutation("duplicate history IDs", duplicate_history_id)

        def cycle_history_chain(members) -> None:
            candidates = history_names(members)
            documents = {
                candidate: json.loads(members[candidate]) for candidate in candidates
            }
            root_name = next(
                candidate
                for candidate, document in documents.items()
                if "parent" not in document
            )
            leaf = next(
                document for document in documents.values() if "architecture" in document
            )
            root = documents[root_name]
            root["parent"] = leaf["id"]
            replace_history_document(members, root_name, root)

        add_hybrid_mutation("cyclic history chain", cycle_history_chain)

        def tamper_blob(members, name) -> None:
            members[name] = members[name] + b"tamper"

        add_hybrid_mutation(
            "config blob hash tamper",
            lambda members: tamper_blob(members, hybrid_manifest(members)["Config"]),
        )
        add_hybrid_mutation(
            "layer blob hash tamper",
            lambda members: tamper_blob(members, hybrid_manifest(members)["Layers"][0]),
        )
        add_hybrid_mutation(
            "OCI manifest blob hash tamper",
            lambda members: tamper_blob(
                members,
                "blobs/sha256/"
                + hybrid_index(members)["manifests"][0]["digest"].removeprefix(
                    "sha256:"
                ),
            ),
        )
        add_hybrid_mutation(
            "history blob hash tamper",
            lambda members: tamper_blob(members, history_names(members)[0]),
        )

        hybrid_entry = hybrid_manifest(hybrid_members)
        hybrid_config_json = json.dumps(hybrid_entry["Config"])
        hybrid_layers_json = json.dumps(
            hybrid_entry["Layers"], separators=(",", ":")
        )
        source_one_json = json.dumps(
            hybrid_entry["LayerSources"][diff_one], separators=(",", ":")
        )
        source_two_json = json.dumps(
            hybrid_entry["LayerSources"][diff_two], separators=(",", ":")
        )
        hybrid_sources_json = (
            "{"
            + json.dumps(diff_one)
            + ":"
            + source_one_json
            + ","
            + json.dumps(diff_two)
            + ":"
            + source_two_json
            + "}"
        )
        hybrid_manifest_prefix = (
            '[{"Config":'
            + hybrid_config_json
            + ',"RepoTags":null,"Layers":'
            + hybrid_layers_json
        )
        raw_hybrid_duplicate_manifests = (
            hybrid_manifest_prefix
            + ',"LayerSources":'
            + hybrid_sources_json
            + ',"LayerSources":'
            + hybrid_sources_json
            + "}]",
            '[{"Config":'
            + hybrid_config_json
            + ',"RepoTags":null,"Layers":[],"Layers":'
            + hybrid_layers_json
            + ',"LayerSources":'
            + hybrid_sources_json
            + "}]",
            hybrid_manifest_prefix
            + ',"LayerSources":{"'
            + diff_one
            + '":{"digest":"'
            + diff_two
            + '","mediaType":"'
            + validator.DOCKER_LAYER_MEDIA_TYPE
            + '","size":1},"'
            + diff_one
            + '":'
            + source_one_json
            + ',"'
            + diff_two
            + '":'
            + source_two_json
            + "}}]",
            hybrid_manifest_prefix
            + ',"LayerSources":{"'
            + diff_one
            + '":{"digest":"'
            + diff_two
            + '","digest":"'
            + diff_one
            + '","mediaType":"'
            + validator.DOCKER_LAYER_MEDIA_TYPE
            + '","size":'
            + str(len(layer_one))
            + '},"'
            + diff_two
            + '":'
            + source_two_json
            + "}}]",
            hybrid_manifest_prefix
            + ',"LayerSources":{"'
            + diff_one
            + '":{"digest":"'
            + diff_one
            + '","mediaType":"'
            + validator.DOCKER_LAYER_MEDIA_TYPE
            + '","size":0,"size":'
            + str(len(layer_one))
            + '},"'
            + diff_two
            + '":'
            + source_two_json
            + "}}]",
        )
        for document in raw_hybrid_duplicate_manifests:
            require(
                isinstance(json.loads(document), list),
                "raw hybrid duplicate-key fixture is malformed",
            )
            members = copy.deepcopy(hybrid_members)
            members["manifest.json"] = document.encode()
            mutation_path = temporary / f"hybrid-duplicate-{len(bad_archives)}.tar"
            write_member_archive(mutation_path, members)
            bad_archives.append(
                (mutation_path, hybrid_chain_id, "hybrid duplicate JSON key")
            )

        base_config_bytes = json.dumps(base_config, separators=(",", ":")).encode()
        base_config_id = "sha256:" + hashlib.sha256(base_config_bytes).hexdigest()
        config_name_json = json.dumps(
            base_config_id.removeprefix("sha256:") + ".json"
        )
        layers_json = json.dumps([blob_layer], separators=(",", ":"))
        source_key_json = json.dumps(diff_one)
        valid_source_json = json.dumps(valid_layer_source, separators=(",", ":"))
        source_map_json = "{" + source_key_json + ":" + valid_source_json + "}"
        manifest_prefix = (
            '[{"Config":'
            + config_name_json
            + ',"RepoTags":null,"Layers":'
            + layers_json
        )
        duplicate_manifest_documents = (
            (
                "duplicate top-level LayerSources",
                manifest_prefix
                + ',"LayerSources":'
                + source_map_json
                + ',"LayerSources":'
                + source_map_json
                + "}]",
            ),
            (
                "duplicate top-level Layers",
                '[{"Config":'
                + config_name_json
                + ',"RepoTags":null,"Layers":[],"Layers":'
                + layers_json
                + ',"LayerSources":'
                + source_map_json
                + "}]",
            ),
            (
                "duplicate layer-source digest key",
                manifest_prefix
                + ',"LayerSources":{"'
                + diff_one
                + '":{"digest":"'
                + diff_two
                + '","mediaType":"'
                + validator.DOCKER_LAYER_MEDIA_TYPE
                + '","size":1},"'
                + diff_one
                + '":'
                + valid_source_json
                + "}}]",
            ),
            (
                "duplicate layer-source entry digest",
                manifest_prefix
                + ',"LayerSources":{"'
                + diff_one
                + '":{"digest":"'
                + diff_two
                + '","digest":"'
                + diff_one
                + '","mediaType":"'
                + validator.DOCKER_LAYER_MEDIA_TYPE
                + '","size":'
                + str(len(layer_one))
                + "}}}]",
            ),
            (
                "duplicate layer-source entry size",
                manifest_prefix
                + ',"LayerSources":{"'
                + diff_one
                + '":{"digest":"'
                + diff_one
                + '","mediaType":"'
                + validator.DOCKER_LAYER_MEDIA_TYPE
                + '","size":0,"size":'
                + str(len(layer_one))
                + "}}}]",
            ),
        )
        for index, (description, document) in enumerate(duplicate_manifest_documents):
            require(isinstance(json.loads(document), list), "duplicate manifest fixture is invalid")
            duplicate_path = temporary / f"duplicate-manifest-{index}.tar"
            duplicate_id = write_raw_archive(
                duplicate_path,
                config=base_config_bytes,
                manifest=document.encode(),
                layer_files={blob_layer: layer_one},
            )
            bad_archives.append((duplicate_path, duplicate_id, description))

        valid_rootfs_json = json.dumps(
            {"type": "layers", "diff_ids": [diff_one]}, separators=(",", ":")
        )
        duplicate_config_documents = (
            (
                "duplicate config rootfs",
                '{"architecture":"amd64","rootfs":{"type":"not-layers",'
                '"diff_ids":[]},"rootfs":'
                + valid_rootfs_json
                + "}",
            ),
            (
                "duplicate config diff_ids",
                '{"architecture":"amd64","rootfs":{"type":"layers",'
                '"diff_ids":[],"diff_ids":["'
                + diff_one
                + '"]}}',
            ),
        )
        for index, (description, document) in enumerate(duplicate_config_documents):
            require(isinstance(json.loads(document), dict), "duplicate config fixture is invalid")
            config_bytes = document.encode()
            config_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            classic_manifest = json.dumps(
                [
                    {
                        "Config": config_id.removeprefix("sha256:") + ".json",
                        "RepoTags": None,
                        "Layers": ["layer-one/layer.tar"],
                    }
                ]
            ).encode()
            duplicate_path = temporary / f"duplicate-config-{index}.tar"
            duplicate_id = write_raw_archive(
                duplicate_path,
                config=config_bytes,
                manifest=classic_manifest,
                layer_files={"layer-one/layer.tar": layer_one},
            )
            bad_archives.append((duplicate_path, duplicate_id, description))

        layer_source_mutations: list[tuple[str, object]] = [
            ("missing layer source", {}),
            (
                "extra layer source",
                {
                    diff_one: valid_layer_source,
                    diff_two: {
                        "digest": diff_two,
                        "mediaType": validator.DOCKER_LAYER_MEDIA_TYPE,
                        "size": len(layer_two),
                    },
                },
            ),
            (
                "wrong layer-source digest",
                {diff_one: {**valid_layer_source, "digest": diff_two}},
            ),
            (
                "wrong layer-source media type",
                {diff_one: {**valid_layer_source, "mediaType": "application/octet-stream"}},
            ),
            (
                "wrong layer-source member size",
                {diff_one: {**valid_layer_source, "size": len(layer_one) + 1}},
            ),
            (
                "non-positive layer-source size",
                {diff_one: {**valid_layer_source, "size": 0}},
            ),
            (
                "oversized layer-source size",
                {
                    diff_one: {
                        **valid_layer_source,
                        "size": validator.MAX_DOCKER_LAYER_BYTES + 1,
                    }
                },
            ),
            (
                "boolean layer-source size",
                {diff_one: {**valid_layer_source, "size": True}},
            ),
            (
                "unknown layer-source field",
                {diff_one: {**valid_layer_source, "Unreviewed": True}},
            ),
            ("non-map layer sources", []),
            ("null layer sources", None),
        ]
        for index, (description, layer_sources) in enumerate(layer_source_mutations):
            mutation_path = temporary / f"layer-source-{index}.tar"
            mutation_id = write_archive(
                mutation_path,
                config_document=base_config,
                manifest_layers=[blob_layer],
                layer_files={blob_layer: layer_one},
                layer_sources=layer_sources,
            )
            bad_archives.append((mutation_path, mutation_id, description))

        wrong_hybrid_rootfs = temporary / "wrong-hybrid-rootfs-order.tar"
        wrong_hybrid_rootfs_id = write_archive(
            wrong_hybrid_rootfs,
            config_document={
                **hybrid_config,
                "rootfs": {"type": "layers", "diff_ids": [diff_two, diff_one]},
            },
            manifest_layers=[blob_layer, blob_layer_two],
            layer_files={blob_layer: layer_one, blob_layer_two: layer_two},
            layer_sources=hybrid_sources,
        )
        bad_archives.append(
            (
                wrong_hybrid_rootfs,
                wrong_hybrid_rootfs_id,
                "wrong hybrid rootfs diff-ID order",
            )
        )

        wrong_blob_path = temporary / "wrong-blob-path.tar"
        wrong_blob_name = "blobs/sha256/" + diff_two.removeprefix("sha256:")
        wrong_blob_id = write_archive(
            wrong_blob_path,
            config_document=base_config,
            manifest_layers=[wrong_blob_name],
            layer_files={wrong_blob_name: layer_one},
            layer_sources={diff_one: valid_layer_source},
        )
        bad_archives.append((wrong_blob_path, wrong_blob_id, "wrong diff-ID blob path"))

        tampered = temporary / "tampered.tar"
        tampered_id = write_archive(
            tampered,
            config_document=base_config,
            manifest_layers=["layer-one/layer.tar"],
            layer_files={"layer-one/layer.tar": b"tampered-layer"},
        )
        bad_archives.append((tampered, tampered_id, "tampered layer"))

        length_mismatch = temporary / "length-mismatch.tar"
        length_id = write_archive(
            length_mismatch,
            config_document={
                "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": []},
            },
            manifest_layers=["layer-one/layer.tar"],
            layer_files={"layer-one/layer.tar": layer_one},
        )
        bad_archives.append((length_mismatch, length_id, "empty diff-ID chain"))

        wrong_order = temporary / "wrong-order.tar"
        order_id = write_archive(
            wrong_order,
            config_document={
                "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": [diff_one, diff_two]},
            },
            manifest_layers=["layer-two/layer.tar", "layer-one/layer.tar"],
            layer_files={
                "layer-one/layer.tar": layer_one,
                "layer-two/layer.tar": layer_two,
            },
        )
        bad_archives.append((wrong_order, order_id, "wrong layer order"))

        malformed_config = temporary / "malformed-config.tar"
        malformed_id = write_archive(
            malformed_config,
            config_document={
                "architecture": "amd64",
                "rootfs": {"type": "not-layers", "diff_ids": [diff_one]},
            },
            manifest_layers=["layer-one/layer.tar"],
            layer_files={"layer-one/layer.tar": layer_one},
        )
        bad_archives.append((malformed_config, malformed_id, "malformed rootfs config"))

        extra_layer = temporary / "extra-layer.tar"
        extra_id = write_archive(
            extra_layer,
            config_document=base_config,
            manifest_layers=["layer-one/layer.tar"],
            layer_files={
                "layer-one/layer.tar": layer_one,
                "extra/layer.tar": layer_two,
            },
        )
        bad_archives.append((extra_layer, extra_id, "extra undeclared layer"))

        missing_layer = temporary / "missing-layer.tar"
        missing_id = write_archive(
            missing_layer,
            config_document=base_config,
            manifest_layers=["missing/layer.tar"],
            layer_files={},
        )
        bad_archives.append((missing_layer, missing_id, "missing declared layer"))

        for bad_archive, bad_id, description in bad_archives:
            require_runtime_error(
                lambda bad_archive=bad_archive, bad_id=bad_id: validator.validate_image_archive(
                    bad_archive, bad_id, os.getuid()
                ),
                f"Docker archive accepted {description}",
            )

        member_cap_archive = temporary / "member-cap.tar"
        with tarfile.open(member_cap_archive, mode="w") as archive:
            for index in range(validator.MAX_DOCKER_ARCHIVE_MEMBERS + 1):
                member = tarfile.TarInfo(f"member-{index:05d}")
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                archive.addfile(member)
        member_cap_archive.chmod(0o600)
        require_runtime_error(
            lambda: validator.validate_image_archive(
                member_cap_archive, "sha256:" + "f" * 64, os.getuid()
            ),
            "Docker archive member cap was enforced only after full retention",
        )
        validator_source = (SCRIPT_DIR / "validate_trivy_report.py").read_text(
            encoding="utf-8"
        )
        require(
            "archive.getmembers()" not in validator_source,
            "Docker archive still retains every member before applying its cap",
        )


def test_scanner_image_archive_is_minimal_and_hardened() -> None:
    validator = load_release_module("validate_trivy_report.py")
    ca_content = b"fixture-ca-bundle"
    binary_content = b"fixture-static-trivy-binary"

    def layer_bytes(
        directories: tuple[str, ...],
        file_path: str,
        content: bytes,
        mode: int,
        *,
        whiteout: str | None = None,
        extra_file: str | None = None,
    ) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as layer:
            for directory in directories:
                member = tarfile.TarInfo(directory)
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                member.uid = 0
                member.gid = 0
                layer.addfile(member)
            if whiteout is not None:
                member = tarfile.TarInfo(whiteout)
                member.mode = 0o000
                member.uid = 0
                member.gid = 0
                member.size = 0
                layer.addfile(member, io.BytesIO())
            member = tarfile.TarInfo(file_path)
            member.mode = mode
            member.uid = 0
            member.gid = 0
            member.size = len(content)
            layer.addfile(member, io.BytesIO(content))
            if extra_file is not None:
                extra = tarfile.TarInfo(extra_file)
                extra.mode = 0o644
                extra.uid = 0
                extra.gid = 0
                extra.size = 1
                layer.addfile(extra, io.BytesIO(b"x"))
        return stream.getvalue()

    def write_scanner_archive(
        path: Path,
        *,
        binary_name: str = "trivy",
        labels: dict[str, str] | None = None,
        runtime_mutation=None,
        ca: bytes = ca_content,
        extra_file: str | None = None,
        created: str | None = None,
    ) -> str:
        ca_layer = layer_bytes(
            ("etc", "etc/ssl", "etc/ssl/certs"),
            "etc/ssl/certs/ca-certificates.crt",
            ca,
            0o644,
        )
        binary_layer = layer_bytes(
            ("usr", "usr/local", "usr/local/bin"),
            f"usr/local/bin/{binary_name}",
            binary_content,
            0o755,
            whiteout="usr/.wh..wh..opq",
            extra_file=extra_file,
        )
        runtime_config = {
            "User": "65532:65532",
            "Entrypoint": [f"/usr/local/bin/{binary_name}"],
            "Env": copy.deepcopy(validator.MINIMAL_TOOL_ENV),
            "WorkingDir": validator.MINIMAL_TOOL_WORKING_DIR,
            "Labels": copy.deepcopy(
                validator.SCANNER_LABELS if labels is None else labels
            ),
        }
        if runtime_mutation is not None:
            runtime_mutation(runtime_config)
        config_document = {
            "architecture": "amd64",
            "os": "linux",
            "config": runtime_config,
            "rootfs": {
                "type": "layers",
                "diff_ids": [
                    "sha256:" + hashlib.sha256(ca_layer).hexdigest(),
                    "sha256:" + hashlib.sha256(binary_layer).hexdigest(),
                ],
            },
        }
        if created is not None:
            config_document["created"] = created
        config = json.dumps(config_document, separators=(",", ":")).encode()
        image_id = "sha256:" + hashlib.sha256(config).hexdigest()
        config_name = image_id.removeprefix("sha256:") + ".json"
        manifest = json.dumps(
            [
                {
                    "Config": config_name,
                    "RepoTags": None,
                    "Layers": ["one/layer.tar", "two/layer.tar"],
                }
            ],
            separators=(",", ":"),
        ).encode()
        with tarfile.open(path, mode="w") as archive:
            for name, content in (
                ("manifest.json", manifest),
                (config_name, config),
                ("one/layer.tar", ca_layer),
                ("two/layer.tar", binary_layer),
            ):
                member = tarfile.TarInfo(name)
                member.mode = 0o600
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        path.chmod(0o600)
        return image_id

    original_binary_hash = validator.SCANNER_BINARY_SHA256
    original_cosign_binary_hash = validator.COSIGN_BINARY_SHA256
    original_ca_hash = validator.SCANNER_CA_BUNDLE_SHA256
    validator.SCANNER_BINARY_SHA256 = hashlib.sha256(binary_content).hexdigest()
    validator.COSIGN_BINARY_SHA256 = hashlib.sha256(binary_content).hexdigest()
    validator.SCANNER_CA_BUNDLE_SHA256 = hashlib.sha256(ca_content).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="cheby-scanner-archive-") as name:
            temporary = Path(name)
            valid = temporary / "valid.tar"
            valid_id = write_scanner_archive(valid)
            validator.validate_scanner_image_archive(valid, valid_id, os.getuid())
            second = temporary / "second-valid.tar"
            second_id = write_scanner_archive(
                second, created="2026-07-19T00:00:00Z"
            )
            require(second_id != valid_id, "dynamic scanner image fixture ID did not vary")
            validator.validate_scanner_image_archive(second, second_id, os.getuid())
            cosign = temporary / "cosign-valid.tar"
            cosign_id = write_scanner_archive(
                cosign, binary_name="cosign", labels=validator.COSIGN_LABELS
            )
            validator.validate_cosign_image_archive(cosign, cosign_id, os.getuid())
            require_runtime_error(
                lambda: validator.validate_scanner_image_archive(
                    cosign, cosign_id, os.getuid()
                ),
                "hardened Cosign archive was accepted as the scanner image",
            )
            valid.chmod(0o400)
            validator.validate_scanner_image_archive(
                valid, valid_id, os.getuid(), expected_mode=0o400
            )
            require_runtime_error(
                lambda: validator.validate_scanner_image_archive(
                    valid, valid_id, os.getuid()
                ),
                "non-private archive mode was accepted without an explicit contract",
            )
            for index, options in enumerate(
                (
                    {
                        "runtime_mutation": lambda config: config.update(
                            {"Cmd": ["/bin/sh"]}
                        )
                    },
                    {"runtime_mutation": lambda config: config.update({"Env": ["X=1"]})},
                    {
                        "runtime_mutation": lambda config: config.pop("Env")
                    },
                    {
                        "runtime_mutation": lambda config: config.update(
                            {"WorkingDir": "/tmp"}
                        )
                    },
                    {
                        "runtime_mutation": lambda config: config.pop("WorkingDir")
                    },
                    {"extra_file": "usr/local/bin/unreviewed"},
                    {"ca": b"different-ca-bundle"},
                )
            ):
                candidate = temporary / f"invalid-{index}.tar"
                candidate_id = write_scanner_archive(candidate, **options)
                require_runtime_error(
                    lambda candidate=candidate, candidate_id=candidate_id: (
                        validator.validate_scanner_image_archive(
                            candidate, candidate_id, os.getuid()
                        )
                    ),
                    "scanner archive accepted runtime drift, an extra file, or wrong CA",
                )
    finally:
        validator.SCANNER_BINARY_SHA256 = original_binary_hash
        validator.COSIGN_BINARY_SHA256 = original_cosign_binary_hash
        validator.SCANNER_CA_BUNDLE_SHA256 = original_ca_hash


def valid_release_manifest() -> dict[str, object]:
    manifest = load_release_module("turkey_release_manifest.py")
    now = datetime.now(timezone.utc)
    image = "sha256:" + "a" * 64
    scanner_image = "sha256:" + "e" * 64
    verifier_image = "sha256:" + "7" * 64
    support = lambda letter, reference: {
        "id": "sha256:" + letter * 64,
        "reference": reference,
    }
    return {
        "schemaVersion": 3,
        "createdAt": now.isoformat(),
        "buildContextSha256": "f" * 64,
        "images": {
            "runtime": {"id": image, "reference": image},
            "edge": {
                "id": "sha256:" + "b" * 64,
                "reference": "sha256:" + "b" * 64,
            },
            "certbot": support("c", "certbot/certbot:v5.7.0@sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4"),
            "alpine": support("d", "alpine:3.23.5@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40"),
            "trivy": {"id": scanner_image, "reference": scanner_image},
        },
        "scanner": {
            **manifest.SCANNER_FIXED_PROVENANCE,
            "imageId": scanner_image,
        },
        "verifier": {
            **manifest.VERIFIER_FIXED_PROVENANCE,
            "imageId": verifier_image,
            "reference": verifier_image,
        },
        "scan": {
            "result": "pass",
            "scanners": ["secret", "vuln"],
            "scannerVersion": "0.72.0-cheby.1",
            "bootstrap": {
                "officialTrivyId": "sha256:" + "6" * 64,
                "officialTrivyReference": manifest.OFFICIAL_TRIVY_REFERENCE,
                "cosignId": verifier_image,
                "cosignReference": verifier_image,
                "officialCosignReference": manifest.OFFICIAL_COSIGN_REFERENCE,
                "certificateIdentityRegexp": manifest.COSIGN_IDENTITY_REGEXP,
                "certificateOidcIssuer": manifest.COSIGN_ISSUER,
                "signatureEvidenceSha256": "8" * 64,
                "customCosignScanResult": "pass",
                "officialCosignScanResult": "pass",
                "verified": True,
            },
            "database": {
                "schemaVersion": 2,
                "updatedAt": (now - timedelta(hours=1)).isoformat(),
                "downloadedAt": (now - timedelta(minutes=30)).isoformat(),
                "nextUpdate": (now + timedelta(hours=5)).isoformat(),
                "metadataSha256": "9" * 64,
                "trivyDbSha256": "0" * 64,
            },
            "antiBlindness": {
                "officialToCustom": "pass",
                "customToCustom": "pass",
                "customToOfficial": "exact-lock",
                "blockingFindingCount": 16,
                "secretCount": 0,
                "findingsLockSha256": manifest.FINDINGS_LOCK_SHA256,
                "officialFindingsSha256": manifest.OFFICIAL_FINDINGS_SHA256,
            },
        },
    }


def test_release_manifest_is_immutable_and_fail_closed() -> None:
    manifest = load_release_module("turkey_release_manifest.py")
    reviewed = valid_release_manifest()
    manifest.validate_document(reviewed)
    manifest.validate_running_release("", "", None)
    require_runtime_error(
        lambda: manifest.validate_running_release(
            reviewed["images"]["runtime"]["reference"], "", None
        ),
        "an unrecorded running application release was accepted",
    )
    manifest.validate_running_release(
        reviewed["images"]["runtime"]["reference"],
        reviewed["images"]["edge"]["reference"],
        reviewed,
    )
    require_runtime_error(
        lambda: manifest.validate_running_release("", "", reviewed),
        "an unexpectedly stopped recorded release was accepted outside recovery",
    )
    manifest.validate_running_release("", "", reviewed, allow_all_stopped=True)
    require_runtime_error(
        lambda: manifest.validate_running_release(
            reviewed["images"]["runtime"]["reference"], "", reviewed
        ),
        "a partially missing recorded release was accepted",
    )
    require_runtime_error(
        lambda: manifest.validate_running_release(
            "sha256:" + "9" * 64,
            reviewed["images"]["edge"]["reference"],
            reviewed,
        ),
        "running application drift from recorded release was accepted",
    )
    mutations = []
    mutable_runtime = copy.deepcopy(reviewed)
    mutable_runtime["images"]["runtime"]["reference"] = "local/runtime:latest"
    mutations.append(mutable_runtime)
    missing_secret_scan = copy.deepcopy(reviewed)
    missing_secret_scan["scan"]["scanners"] = ["vuln"]
    mutations.append(missing_secret_scan)
    stale_database = copy.deepcopy(reviewed)
    stale_database["scan"]["database"]["updatedAt"] = (
        datetime.now(timezone.utc) - timedelta(hours=49)
    ).isoformat()
    mutations.append(stale_database)
    mutable_scanner = copy.deepcopy(reviewed)
    mutable_scanner["images"]["trivy"]["reference"] = "local/scanner:latest"
    mutations.append(mutable_scanner)
    mismatched_scanner = copy.deepcopy(reviewed)
    mismatched_scanner["scanner"]["imageId"] = "sha256:" + "1" * 64
    mutations.append(mismatched_scanner)
    floating_cosign = copy.deepcopy(reviewed)
    floating_cosign["scan"]["bootstrap"]["cosignReference"] = (
        "ghcr.io/sigstore/cosign/cosign:v3.1.2"
    )
    mutations.append(floating_cosign)
    mismatched_verifier = copy.deepcopy(reviewed)
    mismatched_verifier["scan"]["bootstrap"]["cosignId"] = "sha256:" + "4" * 64
    mutations.append(mismatched_verifier)
    mutable_verifier = copy.deepcopy(reviewed)
    mutable_verifier["verifier"]["reference"] = "local/cosign:latest"
    mutations.append(mutable_verifier)
    changed_verifier_binary = copy.deepcopy(reviewed)
    changed_verifier_binary["verifier"]["binarySha256"] = "5" * 64
    mutations.append(changed_verifier_binary)
    floating_official_cosign = copy.deepcopy(reviewed)
    floating_official_cosign["scan"]["bootstrap"]["officialCosignReference"] = (
        "ghcr.io/sigstore/cosign/cosign:v3.1.2"
    )
    mutations.append(floating_official_cosign)
    unscanned_cosign = copy.deepcopy(reviewed)
    unscanned_cosign["scan"]["bootstrap"]["officialCosignScanResult"] = "blocked"
    mutations.append(unscanned_cosign)
    custom_unscanned_cosign = copy.deepcopy(reviewed)
    custom_unscanned_cosign["scan"]["bootstrap"]["customCosignScanResult"] = "blocked"
    mutations.append(custom_unscanned_cosign)
    shrunk_antiblindness = copy.deepcopy(reviewed)
    shrunk_antiblindness["scan"]["antiBlindness"]["blockingFindingCount"] = 15
    mutations.append(shrunk_antiblindness)
    extra_image = copy.deepcopy(reviewed)
    extra_image["images"]["unreviewed"] = {
        "id": "sha256:" + "9" * 64,
        "reference": "sha256:" + "9" * 64,
    }
    mutations.append(extra_image)
    for mutation in mutations:
        require_runtime_error(
            lambda mutation=mutation: manifest.validate_document(mutation),
            "unsafe release manifest mutation was accepted",
        )
    dynamic_scanner = copy.deepcopy(reviewed)
    dynamic_id = "sha256:" + "2" * 64
    dynamic_scanner["images"]["trivy"] = {
        "id": dynamic_id,
        "reference": dynamic_id,
    }
    dynamic_scanner["scanner"]["imageId"] = dynamic_id
    manifest.validate_document(dynamic_scanner)
    dynamic_verifier = copy.deepcopy(reviewed)
    dynamic_verifier_id = "sha256:" + "3" * 64
    dynamic_verifier["verifier"]["imageId"] = dynamic_verifier_id
    dynamic_verifier["verifier"]["reference"] = dynamic_verifier_id
    dynamic_verifier["scan"]["bootstrap"]["cosignId"] = dynamic_verifier_id
    dynamic_verifier["scan"]["bootstrap"]["cosignReference"] = dynamic_verifier_id
    manifest.validate_document(dynamic_verifier)

    with tempfile.TemporaryDirectory(prefix="cheby-bounded-release-json-") as name:
        temporary = Path(name)
        manifest_path = temporary / "release.json"
        encoded_manifest = json.dumps(reviewed, separators=(",", ":"))
        duplicate_manifest = encoded_manifest.replace(
            '"result":"pass"', '"result":"blocked","result":"pass"', 1
        )
        manifest_path.write_text(duplicate_manifest, encoding="utf-8")
        manifest_path.chmod(0o600)
        require_runtime_error(
            lambda: manifest.load_manifest(manifest_path, os.getuid()),
            "release manifest accepted a nested duplicate JSON key",
        )
        with manifest_path.open("wb") as handle:
            handle.truncate(manifest.MAX_RELEASE_JSON_BYTES + 1)
        require_runtime_error(
            lambda: manifest.load_manifest(manifest_path, os.getuid()),
            "release manifest accepted oversized JSON before parsing",
        )

        state_directory = temporary / "state"
        state_directory.mkdir(mode=0o700)
        state_path = state_directory / "state.json"
        state_document = {
            "schemaVersion": 1,
            "generation": 1,
            "current": reviewed,
            "previous": None,
        }
        encoded_state = json.dumps(state_document, separators=(",", ":"))
        state_path.write_text(
            encoded_state.replace(
                '"result":"pass"', '"result":"blocked","result":"pass"', 1
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        require_runtime_error(
            lambda: manifest.load_state(state_directory, os.getuid()),
            "release state accepted a nested duplicate JSON key",
        )
        with state_path.open("wb") as handle:
            handle.truncate(manifest.MAX_RELEASE_JSON_BYTES + 1)
        require_runtime_error(
            lambda: manifest.load_state(state_directory, os.getuid()),
            "release state accepted oversized JSON before parsing",
        )

        original = temporary / "identity.json"
        replacement = temporary / "replacement.json"
        displaced = temporary / "displaced.json"
        original.write_text("{}\n", encoding="utf-8")
        replacement.write_text("{}\n", encoding="utf-8")
        original.chmod(0o600)
        replacement.chmod(0o600)
        real_open = os.open

        def swap_before_open(path: Path, flags: int) -> int:
            path.rename(displaced)
            replacement.rename(path)
            return real_open(path, flags)

        require_runtime_error(
            lambda: manifest.read_bounded_json(
                original,
                expected_uid=os.getuid(),
                expected_mode=0o600,
                opener=swap_before_open,
            ),
            "bounded release JSON reader accepted an lstat/open identity race",
        )

        same_inode = temporary / "same-inode.json"
        same_inode.write_text('{"a":1}\n', encoding="utf-8")
        same_inode.chmod(0o600)
        baseline = same_inode.stat()

        def mutate_same_inode_before_open(path: Path, flags: int) -> int:
            path.write_text('{"b":2}\n', encoding="utf-8")
            os.utime(
                path,
                ns=(baseline.st_atime_ns, baseline.st_mtime_ns + 1_000_000_000),
            )
            return real_open(path, flags)

        require_runtime_error(
            lambda: manifest.read_bounded_json(
                same_inode,
                expected_uid=os.getuid(),
                expected_mode=0o600,
                opener=mutate_same_inode_before_open,
            ),
            "bounded release JSON reader accepted same-inode pre-open mutation",
        )

    with tempfile.TemporaryDirectory(prefix="cheby-manifest-state-") as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        prepared = temporary / "prepared" / "release-state"
        manifest.ensure_state_directory(
            prepared, os.getuid(), tree_root=temporary
        )
        require(
            prepared.is_dir() and stat.S_IMODE(prepared.stat().st_mode) == 0o700,
            "symlink-safe state preparation did not create a private directory",
        )
        victim_directory = temporary / "victim-directory"
        victim_directory.mkdir(mode=0o700)
        linked_directory = temporary / "linked-directory"
        linked_directory.symlink_to(victim_directory, target_is_directory=True)
        require_runtime_error(
            lambda: manifest.ensure_state_directory(
                linked_directory / "release-state",
                os.getuid(),
                tree_root=temporary,
            ),
            "state preparation followed a directory symlink",
        )
        candidate = temporary / "candidate.json"
        manifest.atomic_write(candidate, reviewed)
        state = temporary / "state"
        state.mkdir(mode=0o700)
        manifest.record_state(candidate, state, os.getuid(), temporary)
        first_state = manifest.load_state(state, os.getuid())
        require(
            first_state is not None
            and first_state["generation"] == 1
            and first_state["current"] == reviewed
            and first_state["previous"] is None,
            "first atomic release generation was not recorded",
        )
        next_release = copy.deepcopy(reviewed)
        next_release["createdAt"] = datetime.now(timezone.utc).isoformat()
        next_release["images"]["runtime"] = {
            "id": "sha256:" + "1" * 64,
            "reference": "sha256:" + "1" * 64,
        }
        manifest.atomic_write(candidate, next_release)
        manifest.record_state(candidate, state, os.getuid(), temporary)
        second_state = manifest.load_state(state, os.getuid())
        require(
            second_state is not None
            and second_state["generation"] == 2
            and second_state["current"] == next_release
            and second_state["previous"] == reviewed,
            "atomic release generation did not retain exact current/previous IDs",
        )


class FakeActivationBackend:
    def __init__(
        self,
        running: dict[str, str],
        deployments: list[object],
    ) -> None:
        self.running = dict(running)
        self.deployments = list(deployments)
        self.deploy_calls: list[dict[str, object]] = []
        self.validation_calls: list[tuple[dict[str, object], bool]] = []
        self.stopped = False

    def validate_release(self, release: dict[str, object], fresh: bool) -> None:
        self.validation_calls.append((release, fresh))

    def deploy(self, release: dict[str, object]) -> bool:
        self.deploy_calls.append(release)
        if not self.deployments:
            raise RuntimeError("unexpected mocked deployment")
        action = self.deployments.pop(0)
        if action == "expected":
            self.running = {
                name: str(release["images"][name]["reference"])
                for name in ("runtime", "edge")
            }
            return True
        if action == "noop":
            return True
        if action is False:
            return False
        if isinstance(action, dict):
            self.running = dict(action)
            return True
        if isinstance(action, BaseException):
            raise action
        raise RuntimeError("invalid mocked deployment action")

    def running_images(self) -> dict[str, str]:
        return dict(self.running)

    def stop(self) -> None:
        self.stopped = True
        self.running = {"runtime": "", "edge": ""}


def distinct_release(source: dict[str, object], runtime: str, edge: str) -> dict[str, object]:
    release = copy.deepcopy(source)
    release["createdAt"] = datetime.now(timezone.utc).isoformat()
    release["images"]["runtime"] = {"id": runtime, "reference": runtime}
    release["images"]["edge"] = {"id": edge, "reference": edge}
    return release


def release_running(release: dict[str, object]) -> dict[str, str]:
    return {
        name: str(release["images"][name]["reference"])
        for name in ("runtime", "edge")
    }


def test_activation_rejects_noop_wrong_actual_and_failed_rollback() -> None:
    activation = load_release_module("activate_turkey_release.py")
    previous = valid_release_manifest()
    candidate = distinct_release(
        previous, "sha256:" + "1" * 64, "sha256:" + "2" * 64
    )
    state_before = {
        "schemaVersion": 1,
        "generation": 1,
        "current": previous,
        "previous": None,
    }

    for first_action in (
        "noop",
        {
            "runtime": "sha256:" + "9" * 64,
            "edge": candidate["images"]["edge"]["reference"],
        },
    ):
        backend = FakeActivationBackend(
            release_running(previous), [first_action, "expected"]
        )
        require_runtime_error(
            lambda backend=backend: activation.activate(
                candidate,
                state_before,
                backend,
                lambda: (_ for _ in ()).throw(RuntimeError("record must not run")),
                lambda: state_before,
            ),
            "no-op or wrong-actual candidate Compose was accepted",
        )
        require(
            backend.running == release_running(previous)
            and backend.deploy_calls == [candidate, previous]
            and backend.validation_calls == [(candidate, True), (previous, False)],
            "candidate rejection did not verify and restore exact previous image IDs",
        )

    wrong_actual = {
        "runtime": "sha256:" + "9" * 64,
        "edge": candidate["images"]["edge"]["reference"],
    }
    failed_rollback = FakeActivationBackend(
        release_running(previous), [wrong_actual, "noop"]
    )
    require_runtime_error(
        lambda: activation.activate(
            candidate,
            state_before,
            failed_rollback,
            lambda: None,
            lambda: state_before,
        ),
        "no-op rollback after candidate drift was accepted",
    )
    require(
        failed_rollback.stopped and not any(failed_rollback.running.values()),
        "failed exact rollback did not stop both unverified services",
    )


def test_activation_state_transaction_and_explicit_stopped_recovery() -> None:
    activation = load_release_module("activate_turkey_release.py")
    manifest = load_release_module("turkey_release_manifest.py")
    previous = valid_release_manifest()
    candidate = distinct_release(
        previous, "sha256:" + "3" * 64, "sha256:" + "4" * 64
    )
    state_before = {
        "schemaVersion": 1,
        "generation": 1,
        "current": previous,
        "previous": None,
    }
    with tempfile.TemporaryDirectory(prefix="cheby-activation-transaction-") as name:
        temporary = Path(name)
        temporary.chmod(0o700)
        state_directory = temporary / "state"
        state_directory.mkdir(mode=0o700)
        manifest.atomic_write(state_directory / "state.json", state_before)
        candidate_path = temporary / "candidate.json"
        manifest.atomic_write(candidate_path, candidate)
        writer_calls = 0

        def fail_after_first_replace(path, document, mode):
            nonlocal writer_calls
            writer_calls += 1
            manifest.atomic_write(path, document, mode)
            if writer_calls == 1:
                raise RuntimeError("injected post-record failure")

        backend = FakeActivationBackend(
            release_running(previous), ["expected", "expected"]
        )
        require_runtime_error(
            lambda: activation.activate(
                candidate,
                state_before,
                backend,
                lambda: manifest.record_state(
                    candidate_path,
                    state_directory,
                    os.getuid(),
                    temporary,
                    writer=fail_after_first_replace,
                ),
                lambda: manifest.load_state(state_directory, os.getuid()),
            ),
            "post-record failure was accepted as a successful activation",
        )
        require(
            writer_calls == 2
            and manifest.load_state(state_directory, os.getuid()) == state_before
            and backend.running == release_running(previous),
            "post-record failure did not restore state snapshot and previous images",
        )

    stopped = {"runtime": "", "edge": ""}
    blocked = FakeActivationBackend(stopped, ["expected"])
    require_runtime_error(
        lambda: activation.activate(
            candidate,
            state_before,
            blocked,
            lambda: None,
            lambda: state_before,
        ),
        "normal activation accepted an unexpectedly fully stopped current release",
    )
    require(not blocked.deploy_calls, "blocked stopped-state activation changed services")

    state_box = {"value": state_before}
    recovery = FakeActivationBackend(stopped, ["expected"])

    def record_recovery() -> None:
        state_box["value"] = {
            "schemaVersion": 1,
            "generation": 2,
            "current": candidate,
            "previous": previous,
        }

    activation.activate(
        candidate,
        state_before,
        recovery,
        record_recovery,
        lambda: state_box["value"],
        allow_all_stopped_recovery=True,
    )
    require(
        recovery.running == release_running(candidate)
        and state_box["value"]["current"] == candidate,
        "explicit all-stopped recovery did not activate exact candidate IDs",
    )


def test_activation_lock_is_symlink_safe_and_cross_process_exclusive() -> None:
    activation = load_release_module("activate_turkey_release.py")
    with tempfile.TemporaryDirectory(prefix="cheby-activation-lock-") as name:
        temporary = Path(name)
        temporary.chmod(0o700)
        state_directory = temporary / "state"
        state_directory.mkdir(mode=0o700)
        command = [
            sys.executable,
            "-c",
            (
                "import os,sys; from pathlib import Path; "
                "from activate_turkey_release import activation_lock; "
                "lock=activation_lock(Path(sys.argv[1]),int(sys.argv[2]),Path(sys.argv[3])); "
                "lock.__enter__(); lock.__exit__(None,None,None)"
            ),
            str(state_directory),
            str(os.getuid()),
            str(temporary),
        ]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SCRIPT_DIR)
        environment["PYTHONPYCACHEPREFIX"] = str(temporary / "pycache")
        with activation.activation_lock(state_directory, os.getuid(), temporary):
            competing = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            require(
                competing.returncode != 0
                and "already running" in competing.stderr,
                "a real concurrent activation process acquired the transaction lock",
            )
        after_release = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        require(
            after_release.returncode == 0,
            f"activation lock remained unavailable after release: {after_release.stderr}",
        )

        linked_state = temporary / "linked-state"
        linked_state.mkdir(mode=0o700)
        victim = temporary / "victim"
        victim.write_text("do-not-lock", encoding="utf-8")
        (linked_state / "activation.lock").symlink_to(victim)
        require_runtime_error(
            lambda: activation.activation_lock(
                linked_state, os.getuid(), temporary
            ).__enter__(),
            "symlinked activation lock was accepted",
        )
        require(victim.read_text(encoding="utf-8") == "do-not-lock", "lock symlink victim changed")

        unsafe_parent = temporary / "unsafe"
        unsafe_parent.mkdir(mode=0o770)
        unsafe_parent.chmod(0o770)
        unsafe_state = unsafe_parent / "state"
        unsafe_state.mkdir(mode=0o700)
        require_runtime_error(
            lambda: activation.activation_lock(
                unsafe_state, os.getuid(), temporary
            ).__enter__(),
            "activation lock accepted a group-writable state parent",
        )


def test_materialized_build_context_is_exact_and_deterministic() -> None:
    context = load_release_module("prepare_turkey_build_context.py")
    with tempfile.TemporaryDirectory(prefix="cheby-context-exact-") as temporary_name:
        temporary = Path(temporary_name)
        first = temporary / "first"
        second = temporary / "second"
        first_digest = context.materialize(REPOSITORY_ROOT, first)
        second_digest = context.materialize(REPOSITORY_ROOT, second)
        require(first_digest == second_digest, "build-context digest is not deterministic")
        require(
            context.hash_materialized_context(first) == first_digest,
            "scan-time context rehash differs from build-time digest",
        )
        require(len(first_digest) == 64, "build-context digest is not SHA-256")
        actual = {
            candidate.relative_to(first).as_posix()
            for candidate in first.rglob("*")
            if candidate.is_file()
        }
        expected = {relative for relative, _ in context.source_files(REPOSITORY_ROOT)}
        require(actual == expected, "materialized build context differs from exact allowlist")
        for forbidden in ("gateway/tests", ".git", "tools/release", "deploy/gateway"):
            require(not (first / forbidden).exists(), f"forbidden context path entered: {forbidden}")


def test_compose_rejects_privilege_and_mount_escalation() -> None:
    validator = load_release_module("validate_turkey_compose.py")
    document = yaml.safe_load((BUNDLE_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    validator.validate_source(BUNDLE_ROOT / "docker-compose.yml")
    require(
        document["services"]["runtime"]["environment"]["CHEBY_LOCAL_IMAGE_ENABLED"]
        == "true",
        "production Compose does not explicitly enable local images",
    )
    mutations = []
    for service_name in document["services"]:
        cap_add = copy.deepcopy(document)
        cap_add["services"][service_name]["cap_add"] = ["SYS_ADMIN"]
        mutations.append(cap_add)
    for field, value in (
        ("security_opt", ["no-new-privileges:true", "seccomp=unconfined"]),
        ("privileged", True),
        ("devices", ["/dev/kvm:/dev/kvm"]),
        ("pid", "host"),
        ("ipc", "host"),
        ("network_mode", "host"),
    ):
        mutation = copy.deepcopy(document)
        mutation["services"]["runtime"][field] = value
        mutations.append(mutation)
    extra_mount = copy.deepcopy(document)
    extra_mount["services"]["runtime"]["volumes"].append("/:/host:rw")
    mutations.append(extra_mount)
    writable_config = copy.deepcopy(document)
    writable_config["services"]["edge"]["volumes"][0] = (
        "./generated/nginx.conf:/etc/nginx/nginx.conf:rw"
    )
    mutations.append(writable_config)
    relaxed_edge_tmpfs = copy.deepcopy(document)
    relaxed_edge_tmpfs["services"]["edge"]["tmpfs"] = [
        "/tmp:size=64m,uid=101,gid=101,mode=0700"
    ]
    mutations.append(relaxed_edge_tmpfs)
    mutable_image = copy.deepcopy(document)
    mutable_image["services"]["runtime"]["image"] = "attacker.invalid/runtime:latest"
    mutations.append(mutable_image)
    build_bypass = copy.deepcopy(document)
    build_bypass["services"]["runtime"]["build"] = {"context": "../.."}
    mutations.append(build_bypass)
    unreviewed_fields = (
        "uts",
        "userns_mode",
        "use_api_socket",
        "group_add",
        "sysctls",
        "cgroup",
        "cgroup_parent",
        "device_cgroup_rules",
        "runtime",
        "isolation",
        "env_file",
        "configs",
        "extra_hosts",
        "links",
        "x-unreviewed-service-field",
    )
    for field in unreviewed_fields:
        mutation = copy.deepcopy(document)
        mutation["services"]["runtime"][field] = {"attacker": "controlled"}
        mutations.append(mutation)
    pairing_override = copy.deepcopy(document)
    pairing_override["services"]["runtime"]["environment"]["CHEBY_PAIRING_SECRET"] = "stolen"
    mutations.append(pairing_override)
    preload = copy.deepcopy(document)
    preload["services"]["runtime"]["environment"]["LD_PRELOAD"] = "/workspace/evil.so"
    mutations.append(preload)
    for invalid_value in ("false", True):
        image_capability = copy.deepcopy(document)
        image_capability["services"]["runtime"]["environment"][
            "CHEBY_LOCAL_IMAGE_ENABLED"
        ] = invalid_value
        mutations.append(image_capability)
    missing_image_capability = copy.deepcopy(document)
    del missing_image_capability["services"]["runtime"]["environment"][
        "CHEBY_LOCAL_IMAGE_ENABLED"
    ]
    mutations.append(missing_image_capability)
    exporter_command = copy.deepcopy(document)
    exporter_command["services"]["cert-export"]["command"] = ["/workspace/evil.py"]
    mutations.append(exporter_command)
    external_egress = copy.deepcopy(document)
    external_egress["networks"]["codex-egress"] = {
        "external": True,
        "name": "openclaw",
    }
    mutations.append(external_egress)
    removed_resource = copy.deepcopy(document)
    del removed_resource["services"]["runtime"]["mem_limit"]
    mutations.append(removed_resource)
    relaxed_resource = copy.deepcopy(document)
    relaxed_resource["services"]["runtime"]["pids_limit"] = 8192
    mutations.append(relaxed_resource)
    nested_extra = copy.deepcopy(document)
    nested_extra["services"]["runtime"]["networks"]["backend"]["link_local_ips"] = [
        "169.254.1.1"
    ]
    mutations.append(nested_extra)

    with tempfile.TemporaryDirectory(prefix="cheby-compose-negative-") as temporary_name:
        fixture = Path(temporary_name) / "docker-compose.yml"
        for mutation in mutations:
            fixture.write_text(yaml.safe_dump(mutation, sort_keys=False), encoding="utf-8")
            require_runtime_error(
                lambda: validator.validate_source(fixture),
                "malicious Compose fixture passed the raw production gate",
            )

    release = valid_release_manifest()
    normalized_services = {
        "runtime": {"image": release["images"]["runtime"]["reference"]},
        "edge": {"image": release["images"]["edge"]["reference"]},
        "certbot": {"image": release["images"]["certbot"]["reference"]},
        "cert-export": {"image": release["images"]["certbot"]["reference"]},
    }
    validator.require_manifest_images(normalized_services, release)
    normalized_services["runtime"]["image"] = "sha256:" + "9" * 64
    require_runtime_error(
        lambda: validator.require_manifest_images(normalized_services, release),
        "normalized Compose accepted an unscanned immutable image ID",
    )
    normalized = copy.deepcopy(document["services"])
    normalized["runtime"]["command"] = None
    normalized["runtime"]["entrypoint"] = None
    normalized["edge"]["command"] = None
    normalized["edge"]["entrypoint"] = None
    normalized["runtime"]["image"] = release["images"]["runtime"]["reference"]
    normalized["edge"]["image"] = release["images"]["edge"]["reference"]
    normalized["runtime"]["mem_limit"] = 18 * 1024**3
    normalized["runtime"]["cpus"] = 8.0
    normalized["edge"]["mem_limit"] = 256 * 1024**2
    normalized["certbot"]["mem_limit"] = 256 * 1024**2
    normalized["cert-export"]["mem_limit"] = 128 * 1024**2
    normalized["certbot"]["networks"] = {"default": None}
    normalized["runtime"]["secrets"] = [
        {"source": "pairing_secret", "target": "/run/secrets/pairing_secret"}
    ]
    normalized["edge"]["depends_on"] = {
        "runtime": {"condition": "service_healthy", "required": True}
    }
    normalized["edge"]["ports"] = [
        {
            "mode": "ingress",
            "host_ip": "192.0.2.10",
            "target": 8080,
            "published": "80",
            "protocol": "tcp",
        },
        {
            "mode": "ingress",
            "host_ip": "192.0.2.10",
            "target": 8443,
            "published": "443",
            "protocol": "tcp",
        },
    ]
    normalized["cert-export"]["environment"]["CHEBY_PUBLIC_IP"] = "8.8.8.8"
    validator.require_service_field_allowlist(normalized, normalized=True)
    validator.require_exact_service_values(
        normalized,
        normalized=True,
        release=release,
        public_ip="8.8.8.8",
        bind_ip="192.0.2.10",
    )
    nginx_document = (BUNDLE_ROOT / "edge" / "nginx.conf.template").read_text(
        encoding="utf-8"
    )
    proxy_document = (
        BUNDLE_ROOT / "edge" / "includes" / "proxy-http.conf"
    ).read_text(encoding="utf-8")
    validator.require_edge_upload_budget(
        document["services"]["edge"], nginx_document, proxy_document
    )
    for nginx_mutation, proxy_mutation in (
        (
            nginx_document.replace(
                "limit_conn image_connection_source 1;",
                "limit_conn image_connection_source 2;",
            ),
            proxy_document,
        ),
        (
            nginx_document.replace(
                "limit_conn image_connection_global 2;",
                "limit_conn image_connection_global 3;",
            ),
            proxy_document,
        ),
        (
            nginx_document,
            proxy_document.replace("proxy_request_buffering on;", "proxy_request_buffering off;"),
        ),
    ):
        require_runtime_error(
            lambda nginx_mutation=nginx_mutation, proxy_mutation=proxy_mutation: (
                validator.require_edge_upload_budget(
                    document["services"]["edge"], nginx_mutation, proxy_mutation
                )
            ),
            "Edge upload buffer/concurrency budget mutation was accepted",
        )
    for field in unreviewed_fields:
        field_mutation = copy.deepcopy(normalized)
        field_mutation["runtime"][field] = {"attacker": "controlled"}
        require_runtime_error(
            lambda field_mutation=field_mutation: validator.require_service_field_allowlist(
                field_mutation, normalized=True
            ),
            f"normalized Compose accepted unreviewed service field {field}",
        )

    normalized_mutations = []
    pairing_override = copy.deepcopy(normalized)
    pairing_override["runtime"]["environment"]["CHEBY_PAIRING_SECRET"] = "stolen"
    normalized_mutations.append(pairing_override)
    preload = copy.deepcopy(normalized)
    preload["runtime"]["environment"]["LD_PRELOAD"] = "/workspace/evil.so"
    normalized_mutations.append(preload)
    for invalid_value in ("false", True):
        image_capability = copy.deepcopy(normalized)
        image_capability["runtime"]["environment"][
            "CHEBY_LOCAL_IMAGE_ENABLED"
        ] = invalid_value
        normalized_mutations.append(image_capability)
    missing_image_capability = copy.deepcopy(normalized)
    del missing_image_capability["runtime"]["environment"][
        "CHEBY_LOCAL_IMAGE_ENABLED"
    ]
    normalized_mutations.append(missing_image_capability)
    exporter_command = copy.deepcopy(normalized)
    exporter_command["cert-export"]["command"] = ["/workspace/evil.py"]
    normalized_mutations.append(exporter_command)
    relaxed_resource = copy.deepcopy(normalized)
    relaxed_resource["runtime"]["pids_limit"] = 8192
    normalized_mutations.append(relaxed_resource)
    nested_extra = copy.deepcopy(normalized)
    nested_extra["runtime"]["networks"]["backend"]["link_local_ips"] = [
        "169.254.1.1"
    ]
    normalized_mutations.append(nested_extra)
    for mutation in normalized_mutations:
        require_runtime_error(
            lambda mutation=mutation: validator.require_exact_service_values(
                mutation,
                normalized=True,
                release=release,
                public_ip="8.8.8.8",
                bind_ip="192.0.2.10",
            ),
            "unsafe normalized nested service mutation was accepted",
        )

    normalized_top = {
        "name": "chebycodex-turkey",
        "services": normalized,
        "networks": {
            "backend": {
                "name": "chebycodex-turkey_backend",
                "internal": True,
                "ipam": {"config": [{"subnet": "172.31.42.0/28"}]},
            },
            "codex-egress": {
                "name": "chebycodex-turkey_codex-egress",
                "ipam": {},
            },
            "default": {"name": "chebycodex-turkey_default", "ipam": {}},
        },
        "volumes": {
            name: {"name": f"chebycodex-turkey_{name}"}
            for name in validator.TOP_LEVEL_VOLUME_NAMES
        },
        "secrets": {
            "pairing_secret": {
                "name": "chebycodex-turkey_pairing_secret",
                "file": "/run/cheby/pairing_secret",
            }
        },
    }
    validator.require_exact_top_level(
        normalized_top, normalized=True, pairing_secret_path="/run/cheby/pairing_secret"
    )
    normalized_external = copy.deepcopy(normalized_top)
    normalized_external["networks"]["codex-egress"] = {
        "external": True,
        "name": "openclaw",
    }
    require_runtime_error(
        lambda: validator.require_exact_top_level(
            normalized_external,
            normalized=True,
            pairing_secret_path="/run/cheby/pairing_secret",
        ),
        "normalized external OpenClaw egress network was accepted",
    )


def test_missing_alert_hook_is_no_go() -> None:
    with tempfile.TemporaryDirectory(prefix="cheby-alert-negative-") as temporary_name:
        missing = Path(temporary_name) / "missing-hook"
        result = subprocess.run(
            [str(SCRIPT_DIR / "validate_cert_alert_hook.sh"), str(missing)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        require(result.returncode != 0, "missing external alert hook was accepted")


def test_scanner_supply_chain_lock_is_exact() -> None:
    validator = load_release_module("validate_turkey_scanner_lock.py")
    lock_path = BUNDLE_ROOT / "scanner.lock"
    document = lock_path.read_text(encoding="utf-8")
    digest = validator.validate_lock(lock_path)
    require(
        re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
        "scanner supply-chain lock did not produce a canonical digest",
    )
    require(
        "SCANNER_IMAGE_ID=" not in document,
        "builder-dependent scanner image ID was made a static supply-chain lock",
    )
    mutations = (
        document
        + "SCANNER_IMAGE_ID=sha256:"
        + "1" * 64
        + "\n",
        document.replace(
            "GOPROXY=https://proxy.golang.org",
            "GOPROXY=https://proxy.golang.org,direct",
        ),
        document.replace("GOPRIVATE=", "GOPRIVATE=internal.example"),
        document.replace("GOENV=off", "GOENV=/tmp/goenv"),
    )
    with tempfile.TemporaryDirectory(prefix="cheby-scanner-lock-") as temporary_name:
        temporary = Path(temporary_name)
        for index, mutation in enumerate(mutations):
            candidate = temporary / f"scanner-{index}.lock"
            candidate.write_text(mutation, encoding="utf-8")
            require_runtime_error(
                lambda candidate=candidate: validator.validate_lock(candidate),
                "mutated scanner supply-chain lock was accepted",
            )


def test_cosign_supply_chain_lock_is_exact() -> None:
    validator = load_release_module("validate_turkey_cosign_lock.py")
    lock_path = BUNDLE_ROOT / "cosign.lock"
    document = lock_path.read_text(encoding="utf-8")
    digest = validator.validate_lock(lock_path)
    require(
        re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
        "Cosign supply-chain lock did not produce a canonical digest",
    )
    require(
        "COSIGN_IMAGE_ID=" not in document,
        "builder-dependent Cosign image ID was made a static supply-chain lock",
    )
    mutations = (
        document + "COSIGN_IMAGE_ID=sha256:" + "1" * 64 + "\n",
        document.replace(
            "ghcr.io/sigstore/cosign/cosign:v3.1.2@sha256:"
            "d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849",
            "ghcr.io/sigstore/cosign/cosign:v3.1.2",
        ),
        document.replace(
            "GOPROXY=https://proxy.golang.org",
            "GOPROXY=https://proxy.golang.org,direct",
        ),
        document.replace("GOPRIVATE=", "GOPRIVATE=internal.example"),
        document.replace("GOENV=off", "GOENV=/tmp/goenv"),
        document.replace(
            "COSIGN_MATERIALIZED_GO_SUM_SHA256="
            "e05fc44fa4275f87b4b6c3c1d35945abd96e1ef27bb739b255090b508a0a7528",
            "COSIGN_MATERIALIZED_GO_SUM_SHA256=" + "0" * 64,
        ),
    )
    with tempfile.TemporaryDirectory(prefix="cheby-cosign-lock-") as temporary_name:
        temporary = Path(temporary_name)
        for index, mutation in enumerate(mutations):
            candidate = temporary / f"cosign-{index}.lock"
            candidate.write_text(mutation, encoding="utf-8")
            require_runtime_error(
                lambda candidate=candidate: validator.validate_lock(candidate),
                "mutated Cosign supply-chain lock was accepted",
            )


def shell_logical_commands(source: str) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    parts: list[str] = []
    start_line = 0
    for line_number, raw_line in enumerate(source.splitlines(), 1):
        stripped = raw_line.strip()
        if not parts:
            start_line = line_number
        continuation = stripped.endswith("\\")
        parts.append(stripped[:-1].rstrip() if continuation else stripped)
        if not continuation:
            commands.append((start_line, " ".join(parts)))
            parts = []
    if parts:
        commands.append((start_line, " ".join(parts)))
    return commands


def require_db_downloader_policy(source: str) -> None:
    try:
        start = source.index("db_download_status=0")
        end = source.index("database_hashes_before=", start)
    except ValueError as error:
        raise RuntimeError("bounded DB downloader block is absent") from error
    block = source[start:end]
    actual_commands = [
        command for _line, command in shell_logical_commands(block) if command
    ]
    expected_commands = [
        "db_download_status=0",
        "docker run --rm --network host --read-only --user 65532:65532 "
        "--security-opt no-new-privileges:true --cap-drop ALL --pids-limit 128 "
        "--memory 2g --cpus 1 --env HOME=/tmp "
        "--tmpfs /tmp:size=512m,uid=65532,gid=65532,mode=0700 "
        '--volume "$temporary_dir/cache:/cache:rw" "$scanner_id" '
        "--cache-dir /cache image --timeout 15m --download-db-only "
        "|| db_download_status=$?",
        'if [ "$db_download_status" -ne 0 ]; then',
        'echo "SCAN BLOCKED: vulnerability DB download failed '
        '(scanner exit $db_download_status)" >&2',
        "exit 4",
        "fi",
    ]
    if actual_commands != expected_commands:
        raise RuntimeError("scanner DB downloader sandbox or diagnostics drifted")


def require_bootstrap_execution_policy(source: str) -> None:
    commands = [
        (line, re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"$\1", command))
        for line, command in shell_logical_commands(source)
    ]
    official_trivy_reference = (
        "ghcr.io/aquasecurity/trivy:0.72.0@sha256:"
        "cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
    )
    official_cosign_reference = (
        "ghcr.io/sigstore/cosign/cosign:v3.1.2@sha256:"
        "d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849"
    )
    docker_word_pattern = re.compile(r"\bdocker\b")
    exact_image_save_pattern = re.compile(r"\bdocker\s+image\s+save\b")
    exact_pull_pattern = re.compile(r"\bdocker\s+(?:image\s+)?pull\b")
    custom_cosign_line = next(
        line
        for line, command in commands
        if 'scan_custom_archive cosign "$cosign_id"' in command
    )
    scanner_validation_line = next(
        line
        for line, command in commands
        if 'validate_trivy_report.py" scanner-image' in command
    )
    docker_commands = [item for item in commands if docker_word_pattern.search(item[1])]
    if any(
        re.search(r"\b(?:create|start)\b", command)
        for _line, command in docker_commands
    ):
        raise RuntimeError("unreviewed Docker create/start lifecycle is present")
    official_cosign_tokens = ("$official_cosign_reference", official_cosign_reference)
    if any(
        any(token in command for token in official_cosign_tokens)
        for _line, command in docker_commands
    ):
        raise RuntimeError("official Cosign has an executable invocation")
    for _line, command in docker_commands:
        if "$official_trivy_id" in command:
            raise RuntimeError("official Trivy ID bypasses the post-signature wrappers")
        if (
            "$official_trivy_reference" in command
            or official_trivy_reference in command
        ) and not (
            exact_pull_pattern.search(command)
            or ("$cosign_id" in command and "verify --timeout 3m" in command)
        ):
            raise RuntimeError("official Trivy reference has an unreviewed Docker use")

    cosign_docker_commands = [
        (line, command)
        for line, command in docker_commands
        if "$cosign_id" in command
    ]
    if (
        len(cosign_docker_commands) != 1
        or "verify --timeout 3m" not in cosign_docker_commands[0][1]
        or cosign_docker_commands[0][0] <= custom_cosign_line
    ):
        raise RuntimeError("hardened Cosign executes outside its reviewed first-use order")
    if any(
        "run_scanner_" in command and "$cosign_id" in command
        for _line, command in commands
    ):
        raise RuntimeError("hardened Cosign executes through an unreviewed wrapper")
    verify_line = cosign_docker_commands[0][0]

    scanner_docker_commands = [
        (line, command)
        for line, command in docker_commands
        if "$scanner_id" in command
        and not exact_image_save_pattern.search(command)
    ]
    if (
        len(scanner_docker_commands) != 1
        or "image --timeout 15m --download-db-only"
        not in scanner_docker_commands[0][1]
        or scanner_docker_commands[0][0] <= scanner_validation_line
    ):
        raise RuntimeError("custom scanner executes before exact archive validation")

    official_wrapper_runs = [
        (line, command)
        for line, command in commands
        if "run_scanner_" in command
        and ("$official_trivy_id" in command or "$official_trivy_reference" in command)
    ]
    if not official_wrapper_runs or any(
        line <= verify_line or "$official_trivy_reference" in command
        for line, command in official_wrapper_runs
    ):
        raise RuntimeError("official Trivy executes before signature verification")


def test_static_release_boundaries() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    expected_context = [
        "**",
        "!.dockerignore",
        "!gateway/",
        "!gateway/cheby_gateway/",
        "!gateway/cheby_gateway/**",
        "!deploy/",
        "!deploy/turkey/",
        "!deploy/turkey/Dockerfile.cosign",
        "!deploy/turkey/Dockerfile.edge",
        "!deploy/turkey/Dockerfile.checker",
        "!deploy/turkey/Dockerfile.runtime",
        "!deploy/turkey/Dockerfile.scanner",
        "!deploy/turkey/check-requirements.lock",
        "!deploy/turkey/cosign.lock",
        "!deploy/turkey/requirements.lock",
        "!deploy/turkey/scanner.lock",
        "!deploy/turkey/trivy-official-findings.lock.json",
        "!deploy/turkey/edge/",
        "!deploy/turkey/edge/includes/",
        "!deploy/turkey/edge/includes/**",
        "**/__pycache__/",
        "**/*.pyc",
        "**/*.pyo",
    ]
    require(dockerignore.splitlines() == expected_context, "Docker context allowlist drifted")

    runtime = (BUNDLE_ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    scanner_dockerfile = (BUNDLE_ROOT / "Dockerfile.scanner").read_text(
        encoding="utf-8"
    )
    scanner_lock = (BUNDLE_ROOT / "scanner.lock").read_text(encoding="utf-8")
    cosign_dockerfile = (BUNDLE_ROOT / "Dockerfile.cosign").read_text(
        encoding="utf-8"
    )
    cosign_lock = (BUNDLE_ROOT / "cosign.lock").read_text(encoding="utf-8")
    manifest_module = load_release_module("turkey_release_manifest.py")
    require(
        manifest_module.SCANNER_FIXED_PROVENANCE["dockerfileSha256"]
        == hashlib.sha256(scanner_dockerfile.encode()).hexdigest()
        and manifest_module.SCANNER_FIXED_PROVENANCE["lockSha256"]
        == hashlib.sha256(scanner_lock.encode()).hexdigest()
        and manifest_module.VERIFIER_FIXED_PROVENANCE["dockerfileSha256"]
        == hashlib.sha256(cosign_dockerfile.encode()).hexdigest()
        and manifest_module.VERIFIER_FIXED_PROVENANCE["lockSha256"]
        == hashlib.sha256(cosign_lock.encode()).hexdigest(),
        "manifest supply-chain hashes do not match the reviewed Dockerfiles/locks",
    )
    checker = (BUNDLE_ROOT / "Dockerfile.checker").read_text(encoding="utf-8")
    checker_lock = (BUNDLE_ROOT / "check-requirements.lock").read_text(encoding="utf-8")
    generic = (REPOSITORY_ROOT / "deploy" / "gateway" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    require("@sha256:" in checker.splitlines()[0], "checker base is not digest-pinned")
    require("--require-hashes" in checker, "checker dependencies are not hash enforced")
    require("pyyaml==6.0.3" in checker_lock, "checker PyYAML is not pinned")
    require("cryptography==49.0.0" in checker_lock, "checker cryptography is not pinned")
    require(
        checker_lock.count("--hash=sha256:") == 4,
        "checker lock is incomplete or has unexpected entries",
    )
    release_check = (SCRIPT_DIR / "check_turkey_bundle.sh").read_text(encoding="utf-8")
    require(
        release_check.count("docker build --platform linux/amd64 --network host") == 4,
        "runtime, Edge, scanner, and verifier builds must use reviewed host-network egress",
    )
    require(
        "docker build --pull --platform linux/amd64 --network host" in release_check,
        "checker build lacks the reviewed host-network DNS recovery",
    )
    scan_script = (SCRIPT_DIR / "scan_turkey_images.sh").read_text(encoding="utf-8")
    require_db_downloader_policy(scan_script)
    for mutation in (
        scan_script.replace("--memory 2g", "--memory 1g", 1),
        scan_script.replace("--memory 2g", "--memory 2g --memory 0", 1),
        scan_script.replace(
            "--cap-drop ALL", "--cap-drop ALL --cap-add SYS_ADMIN", 1
        ),
        scan_script.replace("|| db_download_status=$?", "|| db_download_status=0", 1),
        scan_script.replace(
            'vulnerability DB download failed (scanner exit $db_download_status)" >&2\n'
            "    exit 4",
            'vulnerability DB download failed (scanner exit $db_download_status)" >&2\n'
            "    exit 0",
            1,
        ),
    ):
        require_runtime_error(
            lambda mutation=mutation: require_db_downloader_policy(mutation),
            "weakened DB downloader resource or exit-status policy was accepted",
        )
    require(
        '"$scanner_id" --cache-dir /cache image --timeout 15m --download-db-only'
        in scan_script,
        "patched custom scanner does not materialize the DB before isolated scans",
    )
    require(
        '"$official_trivy_reference" --cache-dir /cache image --timeout 15m --download-db-only'
        not in scan_script,
        "vulnerable official ORAS path was reused for the networked DB download",
    )
    scanner_validation_position = scan_script.index(
        'validate_trivy_report.py" scanner-image'
    )
    database_download_position = scan_script.index(
        '"$scanner_id" --cache-dir /cache image --timeout 15m --download-db-only'
    )
    require(
        scanner_validation_position < database_download_position,
        "custom scanner executes before its rootfs and binary are validated",
    )
    require(
        scan_script.count("--network host") == 2,
        "only Cosign verification and the DB downloader may use host networking",
    )
    require(
        scan_script.count("--network none") == 3,
        "context, archive, and metadata scanners are not all network-isolated",
    )
    require(
        "--tmpfs /tmp:size=1g,uid=65532,gid=65532,mode=0700" in scan_script,
        "isolated scans cannot unpack the bounded 624 MiB runtime image",
    )
    require(
        scan_script.count("--skip-db-update --skip-java-db-update") >= 6,
        "an isolated scan path may attempt an unreviewed DB update",
    )
    require(
        scan_script.count("--offline-scan") == 7,
        "a network-none filesystem/image scan lacks explicit offline semantics",
    )
    require(
        scan_script.count("--timeout 15m") == 8
        and '"$cosign_id" verify --timeout 3m' in scan_script,
        "scanner or signature verification has no explicit reviewed time bound",
    )
    require(
        "/var/run/docker.sock:/var/run/docker.sock" not in scan_script,
        "a sandboxed verifier, downloader, or scanner receives the Docker socket",
    )
    require(
        "--tmpfs /tmp:size=512m,uid=65532,gid=65532,mode=0700" in scan_script,
        "scanner DB downloader lacks its proven bounded extraction workspace",
    )
    require(
        scan_script.count("--user 65532:65532") == 5
        and '"$temporary_dir/cache:/cache:rw"' in scan_script
        and scan_script.count('"$temporary_dir/cache:/cache:ro"') == 3,
        "scanner UID or ephemeral DB cache read/write boundary drifted",
    )
    for marker in (
        "docker image save --output",
        'chmod 0400 "$archive"',
        'chown 65532:65532 "$archive"',
        "--expected-owner-uid 65532 --expected-mode 0400",
        "scanner-image",
        "cosign-image",
        "compare-official-findings",
        "database_hashes_after",
        '"$database_hashes_after" = "$database_hashes_before"',
        "--expected-owner-uid 0",
    ):
        require(marker in scan_script, f"socket-free immutable scan chain lacks {marker}")
    for marker in (
        "ghcr.io/aquasecurity/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f",
        "ghcr.io/sigstore/cosign/cosign:v3.1.2@sha256:d91bc4e7e95e8d2f549c747a72dc174f90579e410a1695f57f686674f84ce849",
        "--certificate-identity-regexp",
        "--certificate-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "official-to-custom",
        "custom-to-custom",
        "custom-to-official",
    ):
        require(marker in scan_script, f"signed scanner cross-check lacks {marker}")
    require(
        "SCANNER_IMAGE_ID=" not in scanner_lock,
        "builder-dependent scanner image ID was incorrectly made a static lock",
    )
    require(
        "COSIGN_IMAGE_ID=" not in cosign_lock,
        "builder-dependent Cosign image ID was incorrectly made a static lock",
    )
    require_bootstrap_execution_policy(scan_script)
    for unsafe_insertion in (
        'docker run "$official_cosign_reference" version\n',
        'docker run "${official_cosign_reference}" version\n',
        'docker run "$official_trivy_id" version\n',
        'docker run "${official_trivy_id}" version\n',
        'docker run "$official_trivy_reference" version\n',
        'run_scanner_archive "$official_trivy_id" version\n',
        'docker run "$cosign_id" version\n',
        'docker run "${cosign_id}" version\n',
        'docker create "$official_cosign_reference" version\n',
        'docker start "$cosign_id"\n',
        'docker container run "${official_trivy_id}" version\n',
        'docker container run "${cosign_id}" version\n',
        'docker container create "${official_cosign_reference}" version\n',
        'docker container start "${cosign_id}"\n',
        'docker image pull "${official_cosign_reference}"\n',
        'docker --context default run "${official_cosign_reference}" version\n',
        'docker --context default run "${official_trivy_id}" version\n',
        'docker --context default run "${cosign_id}" version\n',
    ):
        mutated = scan_script.replace(
            'scan_custom_archive cosign "$cosign_id"',
            unsafe_insertion + 'scan_custom_archive cosign "$cosign_id"',
            1,
        )
        require_runtime_error(
            lambda mutated=mutated: require_bootstrap_execution_policy(mutated),
            "bootstrap invocation inventory accepted an early direct tool execution",
        )
    for marker in (
        "FROM golang:1.26.5-alpine3.24@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS builder",
        "FROM scratch",
        "GOENV=off",
        "GOWORK=off",
        "GOTOOLCHAIN=local",
        "GOPROXY=https://proxy.golang.org",
        "GOSUMDB=sum.golang.org",
        'GOPRIVATE=""',
        'GONOSUMDB=""',
        'GONOPROXY=""',
        'GOINSECURE=""',
        "go mod edit -require=oras.land/oras-go/v2@v2.6.2",
        "go mod tidy",
        "go mod download all",
        "go mod verify",
        "RUN --network=none CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOEXPERIMENT=jsonv2",
        "-trimpath -buildvcs=false -mod=readonly",
        "-buildid=",
        "30329a9bfa5c4b29e7f0b3552ae7430ef011b73963033e9b028e2003bcd08a39",
        "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "WORKDIR /",
        "USER 65532:65532",
        'ENTRYPOINT ["/usr/local/bin/trivy"]',
    ):
        require(marker in scanner_dockerfile, f"hardened scanner build lacks {marker}")
    require(
        scanner_dockerfile.count("COPY --from=builder") == 2
        and " apk " not in scanner_dockerfile
        and "apk add" not in scanner_dockerfile,
        "scratch scanner final filesystem is not restricted to binary and CA bundle",
    )
    for marker in (
        "FROM golang:1.26.5-alpine3.24@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS builder",
        "FROM scratch",
        "GOENV=off",
        "GOWORK=off",
        "GOTOOLCHAIN=local",
        "GOPROXY=https://proxy.golang.org",
        "GOSUMDB=sum.golang.org",
        'GOPRIVATE=""',
        'GONOSUMDB=""',
        'GONOPROXY=""',
        'GOINSECURE=""',
        "go mod download all",
        "go mod verify",
        "cfd81b60e95b440397f37e1db9cc0961fac72e32f7d3362dc4ed7432a6852f70",
        "e05fc44fa4275f87b4b6c3c1d35945abd96e1ef27bb739b255090b508a0a7528",
        "RUN --network=none CGO_ENABLED=0 GOOS=linux GOARCH=amd64",
        "-trimpath -buildvcs=false -mod=readonly",
        "-buildid=",
        "7ba7d877672635f2d7e537ca3d20e751c088d86265c0f8bc6698d0084899c0af",
        "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "WORKDIR /",
        "USER 65532:65532",
        'ENTRYPOINT ["/usr/local/bin/cosign"]',
    ):
        require(marker in cosign_dockerfile, f"hardened Cosign build lacks {marker}")
    require(
        cosign_dockerfile.count("COPY --from=builder") == 2
        and " apk " not in cosign_dockerfile
        and "apk add" not in cosign_dockerfile,
        "scratch Cosign final filesystem is not restricted to binary and CA bundle",
    )
    scanner_final_stage = scanner_dockerfile.split("FROM scratch", maxsplit=1)[1]
    cosign_final_stage = cosign_dockerfile.split("FROM scratch", maxsplit=1)[1]
    exact_tool_path = (
        "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    require(
        scanner_final_stage.count(exact_tool_path) == 1
        and cosign_final_stage.count(exact_tool_path) == 1
        and scanner_final_stage.count("WORKDIR /") == 1
        and cosign_final_stage.count("WORKDIR /") == 1,
        "minimal tool final-stage PATH or working directory is not exact",
    )
    require(
        scanner_dockerfile.count("RUN --network=none") == 1
        and cosign_dockerfile.count("RUN --network=none") == 1
        and scanner_dockerfile.index("go mod download all")
        < scanner_dockerfile.index("RUN --network=none")
        and cosign_dockerfile.index("go mod download all")
        < cosign_dockerfile.index("RUN --network=none"),
        "dependency materialization is not complete before the offline compile",
    )
    cosign_upstream_sum = (
        "cfd81b60e95b440397f37e1db9cc0961fac72e32f7d3362dc4ed7432a6852f70"
    )
    cosign_materialized_sum = (
        "e05fc44fa4275f87b4b6c3c1d35945abd96e1ef27bb739b255090b508a0a7528"
    )
    require(
        cosign_dockerfile.count(cosign_upstream_sum) == 1
        and cosign_dockerfile.count(cosign_materialized_sum) == 1
        and cosign_dockerfile.index(cosign_upstream_sum)
        < cosign_dockerfile.index("go mod download all")
        < cosign_dockerfile.index(cosign_materialized_sum)
        < cosign_dockerfile.index("RUN --network=none"),
        "Cosign upstream and materialized go.sum locks are absent or misordered",
    )
    edge_probe_call = release_check.split(
        '"$script_dir/probe_turkey_edge_body_limits.sh"', maxsplit=1
    )[1].split('if [ -z "$manifest_output" ]', maxsplit=1)[0]
    scanner_call = release_check.split(
        '"$script_dir/scan_turkey_images.sh"', maxsplit=1
    )[1].split("CHEBY_PUBLIC_IP=", maxsplit=1)[0]
    require(
        "--scanner-image" not in edge_probe_call
        and '--scanner-image "$scanner_image"' in scanner_call
        and '--cosign-image "$cosign_image"' in scanner_call,
        "custom scanner or verifier image was wired to the wrong release command",
    )
    alpine_base = (
        "python:3.12-alpine3.23@sha256:"
        "601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d"
    )
    require(runtime.count(alpine_base) == 2, "runtime build stages lack the scanned Alpine base")
    require(checker.startswith(f"FROM {alpine_base}\n"), "checker lacks the scanned Alpine base")
    require(
        "apk add --no-cache openssl=3.5.7-r0" in checker,
        "checker lacks its exact signed OpenSSL CLI package",
    )
    for marker in (
        "/sbin/apk add --no-cache",
        "bubblewrap=0.11.0-r2",
        "ca-certificates=20260611-r0",
        "git=2.52.0-r0",
        "openssh-client-default=10.2_p1-r0",
        "ripgrep=15.1.0-r0",
    ):
        require(marker in runtime, f"runtime Alpine package pin missing: {marker}")
    require("apt-get" not in runtime and "snapshot.debian" not in runtime, "Debian runtime returned")
    require(
        "/usr/sbin/addgroup -S -g 10002 cheby" in runtime
        and "/usr/sbin/adduser -S -D -u 10002 -G cheby" in runtime,
        "runtime user creation must not depend on the restricted runtime PATH",
    )
    require(
        (
            "&& install -d -o cheby -g cheby -m 0700 \\\n"
            "        /data \\\n"
            "        /asset-staging \\\n"
        )
        in runtime,
        "asset staging is not created as the runtime user's private 0700 directory",
    )
    require("COPY gateway /app/gateway" not in runtime, "runtime copies the full gateway tree")
    assets_source = (REPOSITORY_ROOT / "gateway" / "cheby_gateway" / "assets.py").read_text(
        encoding="utf-8"
    )
    pyproject = (REPOSITORY_ROOT / "gateway" / "pyproject.toml").read_text(encoding="utf-8")
    require('PIL.__version__ != "12.3.0"' in assets_source, "Pillow worker pin drifted")
    for marker in (
        'requires-python = ">=3.10"',
        '"fastapi==0.139.2"',
        '"Pillow==12.3.0"',
        '"uvicorn==0.51.0"',
    ):
        require(marker in pyproject, f"Gateway package metadata drifted: {marker}")
    require(
        "hashlib.file_digest(f, 'sha512')" in runtime
        and "e04ec49f30a0d0e9c1c42c989f027eaa767058761ed1849caf9b9c2966e7804f6"
        "ee3ed17ae95842eaab62a7f639791992bc3038d4ecfac9c05230bac1ce1e3bb" in runtime,
        "Codex tarball SHA-512 verification drifted",
    )
    edge = (BUNDLE_ROOT / "Dockerfile.edge").read_text(encoding="utf-8")
    for marker in (
        "/sbin/apk add --no-cache",
        "c-ares=1.34.8-r0",
        "curl=8.20.0-r0",
        "libcurl=8.20.0-r0",
        "libexpat=2.8.2-r0",
    ):
        require(marker in edge, f"Edge fixed package pin missing: {marker}")
    for dockerfile in (runtime, generic):
        for flag in (
            '"--ws-max-size", "16384"',
            '"--ws-max-queue", "4"',
            '"--ws-per-message-deflate", "false"',
        ):
            require(flag in dockerfile, f"Uvicorn security flag missing: {flag}")

    for name in ("bootstrap_ip_certificate.sh", "renew_ip_certificate.sh"):
        content = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        require("recover_turkey_tls.sh" in content, f"{name} lacks fail-closed recovery")
        require("expected_fingerprint" in content, f"{name} lacks certificate identity binding")
    for name in ("preflight_turkey_host.sh", "monitor_turkey_certificate.sh"):
        content = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        require("validate_cert_alert_hook.sh" in content, f"{name} permits a missing alert hook")
    recovery = (SCRIPT_DIR / "recover_turkey_tls.sh").read_text(encoding="utf-8")
    for marker in (
        "CHEBY_CERT_EXPORT_ACTION=rollback",
        "nginx -s reload",
        "deactivate",
        "--mode bootstrap",
        "stop edge",
        "--expected-sha256",
    ):
        require(marker in recovery, f"recovery path lacks {marker}")

    renew_unit = (
        BUNDLE_ROOT / "systemd" / "chebycodex-cert-renew.service"
    ).read_text(encoding="utf-8")
    require(
        "ReadWritePaths=/opt/chebycodex/deploy/turkey/generated" in renew_unit,
        "renewal sandbox cannot rewrite the generated Nginx configuration",
    )
    nginx = (BUNDLE_ROOT / "edge" / "nginx.conf.template").read_text(encoding="utf-8")
    bootstrap_nginx = (BUNDLE_ROOT / "edge" / "nginx.bootstrap.conf.template").read_text(
        encoding="utf-8"
    )
    for marker in (
        "client_body_temp_path /tmp/client_body;",
        "proxy_temp_path /tmp/proxy;",
        "fastcgi_temp_path /tmp/fastcgi;",
        "uwsgi_temp_path /tmp/uwsgi;",
        "scgi_temp_path /tmp/scgi;",
    ):
        require(marker in nginx, f"production Nginx read-only temp path missing: {marker}")
        require(marker in bootstrap_nginx, f"bootstrap Nginx read-only temp path missing: {marker}")
    require("client_max_body_size 256k;" in nginx, "turn ingress is not capped at 256 KiB")
    for marker in (
        "location ~ ^/v1/threads/[^/]+/turn-inputs/[^/]+/images/[^/]+$ {",
        "client_max_body_size 8m;",
        "limit_conn_zone $binary_remote_addr zone=image_connection_source:10m;",
        "limit_conn_zone $server_addr zone=image_connection_global:10m;",
        "limit_conn image_connection_source 1;",
        "limit_conn image_connection_global 2;",
        "limit_except PUT {",
    ):
        require(marker in nginx, f"exact image-upload Edge limit missing: {marker}")
    body_probe = (SCRIPT_DIR / "probe_turkey_edge_body_limits.sh").read_text(
        encoding="utf-8"
    )
    for marker in (
        "truncate -s 8388608",
        "truncate -s 8388609",
        'exact_status" = "204:$probe_token"',
        'over_status" = "413:$probe_token"',
        'near_miss_status" = "413:$probe_token"',
        "--limit-rate 1024k",
        'same_second_status" = "429:$probe_token"',
        'global_three_status" = "429:$probe_token"',
        "docker inspect --format '{{json .State}}'",
        "docker inspect --format '{{.Id}}:{{.State.Running}}'",
        "--connect-timeout 1 --max-time 2",
        "upstream_port=28081",
        "http_port=28080",
        "tls_port=28443",
        "listen 127.0.0.1:$http_port default_server;",
        "listen 127.0.0.1:$tls_port ssl default_server;",
        "--env CHEBY_PROBE_BIND=127.0.0.1",
        '--env "CHEBY_PROBE_PORT=$upstream_port"',
        '--env "CHEBY_PROBE_TOKEN=$probe_token"',
        "%header{x-cheby-probe-upstream-token}",
        "%header{x-cheby-probe-edge-token}",
        "require_probe_services_running",
        "--interface 127.0.0.2",
        '"$image_path-global-two" 127.0.0.3',
        '"$image_path-global-three" 127.0.0.4',
        'candidate.bind(("127.0.0.1", int(raw_port)))',
    ):
        require(marker in body_probe, f"real Edge image body probe lacks {marker}")
    require(
        body_probe.count("--network host") >= 6
        and "docker network create" not in body_probe
        and "docker network rm" not in body_probe
        and "--network-alias" not in body_probe,
        "Edge body probe still depends on a temporary/reused Docker bridge",
    )
    probe_upstream = (SCRIPT_DIR / "edge_body_limit_upstream.py").read_text(
        encoding="utf-8"
    )
    require(
        'os.environ.get("CHEBY_PROBE_PORT", "8080")' in probe_upstream
        and 'os.environ.get("CHEBY_PROBE_BIND", "0.0.0.0")' in probe_upstream,
        "Edge body probe fixture lacks explicit bind/port controls",
    )
    require(
        'self.send_header("X-Cheby-Probe-Upstream-Token", self.probe_token)' in probe_upstream
        and probe_upstream.count("self.send_probe_status(204)") == 1,
        "Edge body probe fixture identity is not restricted to direct readiness",
    )
    websocket_proxy = (
        BUNDLE_ROOT / "edge" / "includes" / "proxy-websocket.conf"
    ).read_text(encoding="utf-8")
    require(
        'proxy_set_header Sec-WebSocket-Extensions "";' in websocket_proxy
        and "proxy_hide_header Sec-WebSocket-Extensions;" in websocket_proxy,
        "Edge does not strip WebSocket compression negotiation",
    )
    preflight = (SCRIPT_DIR / "preflight_turkey_host.sh").read_text(encoding="utf-8")
    for marker in (
        "validate_turkey_docker_networks.py",
        "--inspect-host-routes",
        "validate_pairing_secret.py",
        "validate_turkey_root_execution",
        "Docker data disk needs at least 20 GiB free",
        "release checkout filesystem needs at least 2 GiB free",
    ):
        require(marker in preflight, f"preflight lacks {marker}")

    compose_validator = (SCRIPT_DIR / "validate_turkey_compose.py").read_text(
        encoding="utf-8"
    )
    require(
        compose_validator.count("18 * 1024**3") >= 2
        and "16 * 1024**3" not in compose_validator,
        "raw and normalized Compose memory floors differ",
    )
    compose = (BUNDLE_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    require("build:" not in compose, "production Compose still permits local builds")
    require("local/chebycodex" not in compose, "production Compose still uses mutable tags")
    require("cap_add:" not in compose, "production Compose adds Linux capabilities")
    for marker in ("CHEBY_RUNTIME_IMAGE", "CHEBY_EDGE_IMAGE", "pull_policy: never"):
        require(marker in compose, f"production Compose lacks immutable image control {marker}")

    scanner = (SCRIPT_DIR / "scan_turkey_images.sh").read_text(encoding="utf-8")
    for marker in (
        "--scanners vuln,secret",
        "exact-build-context",
        "validate_trivy_report.py",
        "turkey_release_manifest.py",
    ):
        require(marker in scanner, f"scanner gate lacks {marker}")
    activation_shell = (SCRIPT_DIR / "activate_turkey_release.sh").read_text(
        encoding="utf-8"
    )
    require(
        "verify_turkey_release_images" in activation_shell
        and "--allow-all-stopped-recovery" in activation_shell,
        "activation wrapper lacks immutable image/recovery controls",
    )
    require(
        "turkey_release_manifest.py\" prepare-state" in activation_shell
        and 'install -d -o root -g root -m 0700 "$state_directory"' not in activation_shell,
        "activation wrapper does not prepare the state directory symlink-safely",
    )
    activation = (SCRIPT_DIR / "activate_turkey_release.py").read_text(encoding="utf-8")
    for marker in (
        "with activation_lock(args.state_directory, args.expected_owner_uid):",
        "fcntl.LOCK_EX | fcntl.LOCK_NB",
        '"activation.lock"',
        "backend.validate_release(candidate, fresh=True)",
        '"--no-build"',
        '"--pull"',
        "validate_running_release(",
        "record_candidate()",
        "backend.validate_release(current, fresh=False)",
        "backend.stop()",
    ):
        require(marker in activation, f"immutable deploy/rollback path lacks {marker}")
    release_state = (SCRIPT_DIR / "turkey_release_manifest.py").read_text(
        encoding="utf-8"
    )
    require(
        'state_path = state_directory / "state.json"' in release_state
        and '"generation"' in release_state,
        "release state is not one atomic generation document",
    )

    common = (SCRIPT_DIR / "turkey_release_common.sh").read_text(encoding="utf-8")
    trust = (SCRIPT_DIR / "validate_turkey_host_trust.py").read_text(encoding="utf-8")
    context = (SCRIPT_DIR / "prepare_turkey_build_context.py").read_text(encoding="utf-8")
    require(
        "PYTHONPYCACHEPREFIX" in release_check
        and "PYTHONPYCACHEPREFIX" in scan_script
        and "PYTHONPYCACHEPREFIX" in common,
        "host/container Python bytecode is not redirected to private temporary storage",
    )
    require(
        "release source contains Python bytecode or __pycache__" in release_check,
        "release gate does not reject pre-existing Python bytecode before container build",
    )
    require(
        "reject_python_bytecode(repository_root)" in trust
        and 'candidate.name == "__pycache__"' in context
        and '{".pyc", ".pyo"}' in context,
        "production trust or build-context install does not reject Python bytecode",
    )

    for unit_name in (
        "chebycodex-cert-renew.service",
        "chebycodex-cert-monitor.service",
        "chebycodex-cert-alert@.service",
    ):
        unit = (BUNDLE_ROOT / "systemd" / unit_name).read_text(encoding="utf-8")
        require("validate_turkey_host_trust.py" in unit, f"{unit_name} lacks root trust gate")


def main() -> int:
    test_renderer_rejects_special_outputs()
    test_edge_probe_fixture_has_explicit_safe_address()
    test_edge_probe_config_rejects_active_drift_and_include_escape()
    test_edge_probe_container_policy_rejects_mutations()
    test_fake_lets_encrypt_chain_is_rejected()
    test_certificate_volume_rejects_symlinks()
    test_docker_network_impersonation_is_rejected()
    test_host_and_vpn_route_overlap_is_rejected()
    test_pairing_secret_requires_canonical_256_bits()
    test_root_execution_tree_and_docker_endpoint_fail_closed()
    test_tls_fingerprint_rejects_another_valid_leaf()
    test_trivy_rfc3339_parser_is_strict_and_cross_version_safe()
    test_trivy_missing_results_findings_and_stale_db_fail_closed()
    test_scanner_image_archive_is_minimal_and_hardened()
    test_release_manifest_is_immutable_and_fail_closed()
    test_activation_rejects_noop_wrong_actual_and_failed_rollback()
    test_activation_state_transaction_and_explicit_stopped_recovery()
    test_activation_lock_is_symlink_safe_and_cross_process_exclusive()
    test_materialized_build_context_is_exact_and_deterministic()
    test_compose_rejects_privilege_and_mount_escalation()
    test_missing_alert_hook_is_no_go()
    test_scanner_supply_chain_lock_is_exact()
    test_cosign_supply_chain_lock_is_exact()
    test_static_release_boundaries()
    print("Turkey release negative security tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
