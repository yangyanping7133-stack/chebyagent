from __future__ import annotations

from datetime import timedelta
import importlib.util
import ipaddress
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


BUNDLE = Path(__file__).resolve().parents[1]
PUBLIC_IP = ipaddress.ip_address("192.0.0.9")


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BUNDLE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


certs = load("relay_edge_certificates", "certificate_manager.py")
monitor = load("relay_edge_monitor", "monitor_tls.py")
preflight = load("relay_edge_preflight", "preflight_host.py")
renderer = load("relay_edge_renderer", "render_nginx_config.py")


def _material(tmp_path: Path):
    ca = tmp_path / "ca"
    certs.create_ca(ca, PUBLIC_IP, 27461, valid_days=365)
    candidate = tmp_path / "candidate"
    manifest = certs.issue_candidate(
        ca, candidate, PUBLIC_IP, 27461, valid_hours=160
    )
    trust_document = certs.build_trust_bundle([ca / "ca.pem"], PUBLIC_IP, [27461])
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps(trust_document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    tls = tmp_path / "tls"
    tls.mkdir(mode=0o700)
    certs.install_trust_bundle(tls, trust_path, PUBLIC_IP, 27461)
    return ca, candidate, tls, manifest


def test_private_ca_leaf_activation_and_public_bundle_contract(tmp_path):
    ca, candidate, tls, issued = _material(tmp_path)
    activated = certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    assert activated == issued
    current = certs.read_release(tls, "current", PUBLIC_IP, 27461)
    assert current == issued

    ca_certificate = x509.load_pem_x509_certificate((ca / "ca.pem").read_bytes())
    leaf = x509.load_pem_x509_certificate((candidate / "leaf.pem").read_bytes())
    assert ca_certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value.ca
    assert leaf.issuer == ca_certificate.subject
    assert leaf.subject != leaf.issuer
    assert (
        leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value.get_values_for_type(x509.IPAddress)
        == [PUBLIC_IP]
    )
    assert (ca / "ca-key.pem").stat().st_mode & 0o077 == 0
    assert (candidate / "privkey.pem").stat().st_mode & 0o077 == 0


def test_activated_candidate_is_atomically_consumed(tmp_path):
    _, candidate, tls, issued = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)

    consumed = certs.consume_candidate(tls, candidate, PUBLIC_IP, 27461)

    assert consumed == issued
    assert not candidate.exists()
    assert not (candidate.parent / certs.CONSUMED_CANDIDATE_NAME).exists()
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


def test_candidate_consumer_rejects_content_mismatch_without_deleting(tmp_path):
    ca, first, tls, issued = _material(tmp_path)
    certs.activate_candidate(tls, first, PUBLIC_IP, 27461)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    candidate = staging / "candidate"
    replacement = certs.issue_candidate(
        ca,
        candidate,
        PUBLIC_IP,
        27461,
        valid_hours=160,
    )
    assert replacement != issued

    with pytest.raises(
        RuntimeError,
        match="does not match the current release",
    ):
        certs.consume_candidate(tls, candidate, PUBLIC_IP, 27461)

    assert candidate.is_dir()
    assert set(path.name for path in candidate.iterdir()) == set(
        certs.RELEASE_FILE_MODES
    )
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


def test_candidate_consumer_never_cleans_an_active_release_alias(tmp_path):
    _, candidate, tls, issued = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    current = (tls / "current").resolve(strict=True)

    with pytest.raises(RuntimeError, match="staging aliases active TLS state"):
        certs.consume_candidate(
            tls,
            current / "candidate",
            PUBLIC_IP,
            27461,
            resume_only=True,
        )

    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    (staging / certs.CONSUMED_CANDIDATE_NAME).symlink_to(current)
    with pytest.raises(RuntimeError):
        certs.consume_candidate(
            tls,
            staging / "candidate",
            PUBLIC_IP,
            27461,
            resume_only=True,
        )
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


@pytest.mark.parametrize("mutation", ("missing", "extra", "symlink", "hardlink"))
def test_candidate_consumer_rejects_unsafe_file_sets_without_deleting(
    tmp_path,
    mutation,
):
    _, candidate, tls, issued = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    current = (tls / "current").resolve(strict=True)
    leaf = candidate / "leaf.pem"
    if mutation == "missing":
        leaf.unlink()
    elif mutation == "extra":
        (candidate / "unexpected").write_bytes(b"unexpected")
    elif mutation == "symlink":
        leaf.unlink()
        leaf.symlink_to(current / "leaf.pem")
    else:
        leaf.unlink()
        leaf.hardlink_to(current / "leaf.pem")

    with pytest.raises(RuntimeError):
        certs.consume_candidate(tls, candidate, PUBLIC_IP, 27461)

    assert candidate.is_dir()
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


@pytest.mark.parametrize(
    "crash_point",
    (
        "after-rename",
        "after-rename-fsync",
        "after-unlink:ca.pem",
        "after-unlink:fullchain.pem",
        "after-unlink:leaf.pem",
        "after-unlink:privkey.pem",
        "after-unlink:release.json",
        "after-rmdir",
    ),
)
def test_candidate_consumer_recovers_every_cleanup_crash_boundary(
    tmp_path,
    crash_point,
):
    _, candidate, tls, issued = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)

    def crash(label):
        if label == crash_point:
            raise RuntimeError("simulated consumer crash")

    with pytest.raises(RuntimeError, match="simulated consumer crash"):
        certs.consume_candidate(
            tls,
            candidate,
            PUBLIC_IP,
            27461,
            _fault_hook=crash,
        )

    certs.consume_candidate(
        tls,
        candidate,
        PUBLIC_IP,
        27461,
        resume_only=True,
    )
    assert not candidate.exists()
    assert not (candidate.parent / certs.CONSUMED_CANDIDATE_NAME).exists()
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


def test_activation_resumes_an_identical_durable_orphan_release(tmp_path):
    _, candidate, tls, issued = _material(tmp_path)
    releases = tls / certs.RELEASES_DIRECTORY
    releases.mkdir(mode=0o700)
    orphan = releases / issued["leafCertificateSha256"]
    shutil.copytree(candidate, orphan)

    activated = certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)

    assert activated == issued
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == issued


def test_current_next_ca_overlap_and_rollback(tmp_path):
    ca_one = tmp_path / "ca-one"
    ca_two = tmp_path / "ca-two"
    certs.create_ca(ca_one, PUBLIC_IP, 27461, valid_days=365)
    certs.create_ca(ca_two, PUBLIC_IP, 27461, valid_days=365)
    trust = certs.build_trust_bundle(
        [ca_one / "ca.pem", ca_two / "ca.pem"], PUBLIC_IP, [27461]
    )
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    tls = tmp_path / "tls"
    tls.mkdir(mode=0o700)
    certs.install_trust_bundle(tls, trust_path, PUBLIC_IP, 27461)

    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = certs.issue_candidate(
        ca_one, first, PUBLIC_IP, 27461, valid_hours=160
    )
    second_manifest = certs.issue_candidate(
        ca_two, second, PUBLIC_IP, 27461, valid_hours=160
    )
    certs.activate_candidate(tls, first, PUBLIC_IP, 27461)
    certs.activate_candidate(tls, second, PUBLIC_IP, 27461)
    restored = certs.rollback(tls, PUBLIC_IP, 27461)
    assert restored == first_manifest
    assert (
        certs.read_release(tls, "previous", PUBLIC_IP, 27461)
        == second_manifest
    )

    next_only = certs.build_trust_bundle(
        [ca_two / "ca.pem"], PUBLIC_IP, [27461]
    )
    next_only_path = tmp_path / "next-only.json"
    next_only_path.write_text(json.dumps(next_only), encoding="utf-8")
    with pytest.raises(RuntimeError, match="distrust the active"):
        certs.install_trust_bundle(
            tls, next_only_path, PUBLIC_IP, 27461
        )


def test_missing_previous_release_fails_without_changing_current(tmp_path):
    _, candidate, tls, current = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)

    with pytest.raises(RuntimeError, match="previous"):
        certs.rollback(tls, PUBLIC_IP, 27461)

    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == current


def test_invalid_previous_release_fails_without_changing_current(tmp_path):
    ca, first, tls, _ = _material(tmp_path)
    second = tmp_path / "second"
    second_manifest = certs.issue_candidate(
        ca,
        second,
        PUBLIC_IP,
        27461,
        valid_hours=160,
    )
    certs.activate_candidate(tls, first, PUBLIC_IP, 27461)
    certs.activate_candidate(tls, second, PUBLIC_IP, 27461)
    previous_release = (tls / "previous").resolve(strict=True)
    previous_manifest = previous_release / "release.json"
    previous_manifest.chmod(0o600)
    previous_manifest.write_text("{}\n", encoding="ascii")
    previous_manifest.chmod(0o444)

    with pytest.raises(RuntimeError, match="manifest"):
        certs.rollback(tls, PUBLIC_IP, 27461)

    assert (
        certs.read_release(tls, "current", PUBLIC_IP, 27461)
        == second_manifest
    )


def test_candidate_from_unlisted_ca_is_rejected_without_changing_current(tmp_path):
    _, candidate, tls, first = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    rogue_ca = tmp_path / "rogue-ca"
    rogue_candidate = tmp_path / "rogue-candidate"
    certs.create_ca(rogue_ca, PUBLIC_IP, 27461, valid_days=365)
    certs.issue_candidate(
        rogue_ca, rogue_candidate, PUBLIC_IP, 27461, valid_hours=160
    )
    with pytest.raises(RuntimeError, match="absent from the APK trust bundle"):
        certs.activate_candidate(tls, rogue_candidate, PUBLIC_IP, 27461)
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == first


def test_candidate_with_wrong_private_key_is_rejected_without_changing_current(
    tmp_path,
):
    _, candidate, tls, current = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    rejected = tmp_path / "wrong-key"
    shutil.copytree(candidate, rejected)
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    (rejected / "privkey.pem").chmod(0o600)
    (rejected / "privkey.pem").write_bytes(
        wrong_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (rejected / "privkey.pem").chmod(0o400)

    with pytest.raises(RuntimeError, match="private key do not match"):
        certs.activate_candidate(tls, rejected, PUBLIC_IP, 27461)
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == current


def test_candidate_with_wrong_ip_san_is_rejected_without_changing_current(
    tmp_path,
):
    ca, candidate, tls, current = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    rejected = tmp_path / "wrong-san"
    certs.issue_candidate(
        ca,
        rejected,
        ipaddress.ip_address("203.0.113.8"),
        27461,
        valid_hours=160,
    )

    with pytest.raises(RuntimeError, match="IP SAN"):
        certs.activate_candidate(tls, rejected, PUBLIC_IP, 27461)
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == current


def test_candidate_with_wrong_chain_is_rejected_without_changing_current(
    tmp_path,
):
    _, candidate, tls, current = _material(tmp_path)
    certs.activate_candidate(tls, candidate, PUBLIC_IP, 27461)
    rejected = tmp_path / "wrong-chain"
    shutil.copytree(candidate, rejected)
    leaf = (rejected / "leaf.pem").read_bytes()
    ca = (rejected / "ca.pem").read_bytes()
    (rejected / "fullchain.pem").chmod(0o600)
    (rejected / "fullchain.pem").write_bytes(ca + leaf)
    (rejected / "fullchain.pem").chmod(0o444)

    with pytest.raises(RuntimeError, match="full chain order"):
        certs.activate_candidate(tls, rejected, PUBLIC_IP, 27461)
    assert certs.read_release(tls, "current", PUBLIC_IP, 27461) == current


def test_monitor_requires_leaf_ca_ip_and_both_exact_digests(tmp_path):
    ca, candidate, _, manifest = _material(tmp_path)
    ca_certificate = x509.load_pem_x509_certificate((ca / "ca.pem").read_bytes())
    leaf = x509.load_pem_x509_certificate((candidate / "leaf.pem").read_bytes())
    der = leaf.public_bytes(serialization.Encoding.DER)
    verified = monitor.verify_peer_certificate(
        der,
        ca_certificate=ca_certificate,
        public_ip=PUBLIC_IP,
        expected_certificate_sha256=manifest["leafCertificateSha256"],
        expected_spki_sha256=manifest["leafSpkiSha256"],
        expected_ca_sha256=manifest["caCertificateSha256"],
        minimum_hours=48,
    )
    assert verified.serial_number == leaf.serial_number

    with pytest.raises(RuntimeError, match="fingerprint"):
        monitor.verify_peer_certificate(
            der,
            ca_certificate=ca_certificate,
            public_ip=PUBLIC_IP,
            expected_certificate_sha256="0" * 64,
            expected_spki_sha256=manifest["leafSpkiSha256"],
            expected_ca_sha256=manifest["caCertificateSha256"],
            minimum_hours=48,
        )
    with pytest.raises(RuntimeError, match="safety floor"):
        monitor.verify_peer_certificate(
            der,
            ca_certificate=ca_certificate,
            public_ip=PUBLIC_IP,
            expected_certificate_sha256=manifest["leafCertificateSha256"],
            expected_spki_sha256=manifest["leafSpkiSha256"],
            expected_ca_sha256=manifest["caCertificateSha256"],
            minimum_hours=48,
            now=leaf.not_valid_after_utc - timedelta(hours=47),
        )


def test_renderer_is_exact_authority_and_has_no_public_standard_port():
    production = renderer.render(
        PUBLIC_IP, 27461, ipaddress.ip_address("172.31.61.3")
    )
    gate = renderer.render(
        PUBLIC_IP, 27462, ipaddress.ip_address("172.31.62.3")
    )
    assert '$http_host != "192.0.0.9:27461"' in production
    assert '$http_host != "192.0.0.9:27462"' in gate
    assert "172.31.61.3:8080" in production
    assert "172.31.62.3:8080" in gate
    assert "listen 80;" not in production
    assert "listen 443" not in production
    for forged_header_variable in (
        "$http_forwarded",
        "$http_x_forwarded_for",
        "$http_x_forwarded_host",
        "$http_x_forwarded_port",
        "$http_x_forwarded_proto",
        "$http_x_real_ip",
        "$http_cf_connecting_ip",
        "$http_x_relay_client_ip",
    ):
        assert f'if ({forged_header_variable} != "")' in production
        assert f'if ({forged_header_variable} != "")' in gate


def test_renderer_output_is_readable_by_non_root_edge_user(tmp_path):
    output = tmp_path / "nginx.conf"
    renderer._atomic_write(output, "events {}\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_host_preflight_accepts_deploy_time_identity_and_rejects_unsafe_values():
    preflight._validate_fixed_identity(
        ipaddress.ip_address("192.168.0.43"),
        ipaddress.ip_address("192.0.0.8"),
        27461,
        27462,
    )
    with pytest.raises(RuntimeError, match="public identity"):
        preflight._validate_fixed_identity(
            ipaddress.ip_address("192.168.0.43"),
            ipaddress.ip_address("192.168.0.44"),
            27461,
            27462,
        )
    with pytest.raises(RuntimeError, match="bind identity"):
        preflight._validate_fixed_identity(
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("192.0.0.8"),
            27461,
            27462,
        )
    with pytest.raises(RuntimeError, match="ports"):
        preflight._validate_fixed_identity(
            ipaddress.ip_address("192.168.0.43"),
            ipaddress.ip_address("192.0.0.8"),
            443,
            27462,
        )


def test_host_preflight_activation_rejects_occupied_production_loopback(
    monkeypatch,
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight_host.py",
            "--bind-ip",
            "192.168.0.43",
            "--public-ip",
            "192.0.0.9",
            "--production-port",
            "27461",
            "--gate-port",
            "27462",
            "--require-free-loopback-port",
            "18080",
        ],
    )
    monkeypatch.setattr(
        preflight,
        "_local_addresses",
        lambda: {ipaddress.ip_address("192.168.0.43")},
    )
    monkeypatch.setattr(preflight, "_listening_ports", lambda: set())
    monkeypatch.setattr(preflight, "_docker_published_ports", lambda: {18080})

    with pytest.raises(
        RuntimeError,
        match="production Relay loopback activation port 18080 is already occupied",
    ):
        preflight.main()


def test_host_preflight_read_only_inventory_allows_live_old_loopback(
    monkeypatch,
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight_host.py",
            "--bind-ip",
            "192.168.0.43",
            "--public-ip",
            "192.0.0.9",
            "--production-port",
            "27461",
            "--gate-port",
            "27462",
        ],
    )
    monkeypatch.setattr(
        preflight,
        "_local_addresses",
        lambda: {ipaddress.ip_address("192.168.0.43")},
    )
    monkeypatch.setattr(preflight, "_listening_ports", lambda: {18080})
    monkeypatch.setattr(preflight, "_docker_published_ports", lambda: set())

    assert preflight.main() == 0


def _mock_active_docker_publications(monkeypatch, publications):
    responses = iter(
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="container-one\n",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "NetworkSettings": {
                                "Ports": publications,
                            }
                        }
                    ]
                ),
            ),
        )
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: next(responses),
    )


def test_host_preflight_reads_only_active_tcp_docker_publications(monkeypatch):
    _mock_active_docker_publications(
        monkeypatch,
        {
            "8080/tcp": [
                {
                    "HostIp": "127.0.0.1",
                    "HostPort": "18080",
                }
            ],
            "9000/tcp": None,
            "443/udp": [
                {
                    "HostIp": "0.0.0.0",
                    "HostPort": "443",
                }
            ],
        },
    )

    assert preflight._docker_published_ports() == {18080}


def test_host_preflight_allows_udp_443_docker_publication(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight_host.py",
            "--bind-ip",
            "192.168.0.43",
            "--public-ip",
            "192.0.0.9",
            "--production-port",
            "27461",
            "--gate-port",
            "27462",
        ],
    )
    monkeypatch.setattr(
        preflight,
        "_local_addresses",
        lambda: {ipaddress.ip_address("192.168.0.43")},
    )
    monkeypatch.setattr(preflight, "_listening_ports", lambda: set())
    _mock_active_docker_publications(
        monkeypatch,
        {
            "443/udp": [
                {
                    "HostIp": "0.0.0.0",
                    "HostPort": "443",
                }
            ]
        },
    )

    assert preflight.main() == 0


def test_host_preflight_rejects_tcp_443_docker_publication(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight_host.py",
            "--bind-ip",
            "192.168.0.43",
            "--public-ip",
            "192.0.0.9",
            "--production-port",
            "27461",
            "--gate-port",
            "27462",
        ],
    )
    monkeypatch.setattr(
        preflight,
        "_local_addresses",
        lambda: {ipaddress.ip_address("192.168.0.43")},
    )
    monkeypatch.setattr(preflight, "_listening_ports", lambda: set())
    _mock_active_docker_publications(
        monkeypatch,
        {
            "443/tcp": [
                {
                    "HostIp": "0.0.0.0",
                    "HostPort": "443",
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="forbidden TCP 80/443"):
        preflight.main()


def test_host_preflight_rejects_malformed_docker_port_key(monkeypatch):
    _mock_active_docker_publications(
        monkeypatch,
        {
            "443": [
                {
                    "HostIp": "0.0.0.0",
                    "HostPort": "443",
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="invalid data"):
        preflight._docker_published_ports()


def test_transition_runbook_uses_explicit_compose_env_and_fail_closed_state_checks():
    repository = BUNDLE.parents[1]
    runbook = (
        repository / "docs/deployment/TURKEY_EDGE_RUNBOOK.md"
    ).read_text(encoding="utf-8")
    compose_env = "--env-file /etc/chebycodex-relay-edge/relay-edge.env"
    gate_connector_operator_env = (
        "--env-file /etc/chebycodex-gate-connector/operator.env"
    )
    gate_connector_node_env = (
        "--env-file /etc/chebycodex-gate-connector/node.env"
    )

    rollback = runbook.split(
        "If activation fails before phone migration", maxsplit=1
    )[1].split("After the migration APK authenticates", maxsplit=1)[0]
    assert rollback.count(compose_env) == 2
    assert "new_stack_running=$(docker compose" in rollback
    assert 'if [ -n "$new_stack_running" ]; then' in rollback
    assert "docker start chebycodex-r18-relay" in rollback
    assert rollback.index("stop edge relay") < rollback.index(
        "docker start chebycodex-r18-relay"
    )

    gate_start = runbook.split(
        "Start Gate only after production is healthy:", maxsplit=1
    )[1].split("Gate has a separate Relay database", maxsplit=1)[0]
    assert gate_start.count("docker compose") == 6
    assert gate_start.count(compose_env) == 4
    assert gate_start.count(gate_connector_operator_env) == 3
    assert gate_start.count(gate_connector_node_env) == 3
    assert "gate_stack_running=$(docker compose" in gate_start
    assert 'if [ -n "$gate_stack_running" ]; then' in gate_start
    assert "gate_data_entry=$(find" in gate_start
    assert 'if [ -n "$gate_data_entry" ]; then' in gate_start
    assert "for gate_role in edge-full reload-continuity android-e2e" in gate_start
    assert "Gate role assistant identities are not distinct" in gate_start
    assert "revoke --database /data/relay.sqlite3" in gate_start
    assert gate_start.index("revoke --database /data/relay.sqlite3") < gate_start.index(
        "--profile gate up --detach relay-gate edge-gate"
    )
    assert gate_start.index("install_node_bootstrap.py") < gate_start.index(
        "--profile gate up --detach relay-gate edge-gate"
    )
    assert "--profile gate up --detach relay-gate edge-gate" in gate_start
    assert "--file deploy/turkey/docker-compose.connector-gate.yml" in gate_start
    assert gate_start.index(
        "--profile gate up --detach relay-gate edge-gate"
    ) < gate_start.index("config --quiet") < gate_start.index(
        "up --detach gate-connector"
    )
    assert "verify_connector_runtime.py" in gate_start

    gate_stop = runbook.split(
        "After Full Edge, reload-continuity, and Android evidence have all passed",
        maxsplit=1,
    )[1].split(
        "## Final release gates", maxsplit=1
    )[0]
    device_cleanup = gate_stop.split(
        "On the Mac, remove the two test-only packages", maxsplit=1
    )[1].split("On Turkey, immediately fence Gate traffic", maxsplit=1)[0]
    assert gate_stop.count("docker compose") == 5
    assert gate_stop.count(compose_env) == 3
    assert gate_stop.count(gate_connector_operator_env) == 2
    assert gate_stop.count(gate_connector_node_env) == 2
    assert "stop gate-connector" in gate_stop
    assert "gate_connector_running=$(docker compose" in gate_stop
    assert "ps --status running --quiet gate-connector" in gate_stop
    assert 'if [ -n "$gate_connector_running" ]; then' in gate_stop
    assert "--profile gate stop edge-gate relay-gate" in gate_stop
    assert "gate_stack_running=$(docker compose" in gate_stop
    assert "ps --status running --quiet relay-gate edge-gate" in gate_stop
    assert 'if [ -n "$gate_stack_running" ]; then' in gate_stop
    assert '["roles"]["android-e2e"]["nodeId"]' in gate_stop
    assert "revoke --database /data/relay.sqlite3" in gate_stop
    assert "/etc/chebycodex-gate-connector/node.env" in gate_stop
    assert "/etc/chebycodex-gate-connector/secrets/node_token" in gate_stop
    assert (
        "/etc/chebycodex-gate-connector/secrets/gateway_internal_secret"
        in gate_stop
    )
    assert 'find "$gate_connector_data" -xdev -mindepth 1 -delete' in gate_stop
    assert "Gate Connector credential or data residue remains" in gate_stop
    assert "gate_relay_data=/var/lib/chebycodex-relay-gate/data" in gate_stop
    assert "gate_bootstrap=/etc/chebycodex-relay/gate-bootstrap" in gate_stop
    assert 'realpath -e -- "$gate_relay_data"' in gate_stop
    assert 'realpath -e -- "$gate_bootstrap"' in gate_stop
    assert 'find "$gate_relay_data" -xdev -mindepth 1 -delete' in gate_stop
    assert 'find "$gate_bootstrap" -xdev -mindepth 1 -delete' in gate_stop
    assert '"10001:10001:700"' in gate_stop
    assert "empty-state preflight would fail" in gate_stop
    assert "com.cheby.codex.mobile.gate.test" in device_cleanup
    assert "com.cheby.codex.mobile.gate" in device_cleanup
    assert 'shell am force-stop "$gate_package"' in device_cleanup
    assert 'uninstall "$gate_package"' in device_cleanup
    assert 'shell pm path "$gate_package"' in device_cleanup
    assert 'shell pidof "$gate_package"' in device_cleanup
    for forbidden in (
        " shell input ",
        " input tap ",
        " input text ",
        " keyevent ",
        " uiautomator ",
        " monkey ",
        " am start ",
    ):
        assert forbidden not in device_cleanup.lower()
    assert gate_stop.count("/usr/local/sbin/codex-security-lockdown.sh") == 2
    assert "--production-only" in gate_stop
    assert "--verify --production-only" in gate_stop
    assert "close the Huawei Cloud TCP `27462` rule" in gate_stop
    assert gate_stop.index("stop gate-connector") < gate_stop.index(
        "ps --status running --quiet gate-connector"
    ) < gate_stop.index("--profile gate stop edge-gate relay-gate") < gate_stop.index(
        "ps --status running --quiet relay-gate edge-gate"
    ) < gate_stop.index('["roles"]["android-e2e"]["nodeId"]') < gate_stop.index(
        "revoke --database /data/relay.sqlite3"
    ) < gate_stop.index(
        "rm -f -- \\\n  /etc/chebycodex-gate-connector/node.env"
    ) < gate_stop.index(
        'find "$gate_connector_data" -xdev'
    ) < gate_stop.index(
        'find "$gate_relay_data" -xdev'
    ) < gate_stop.index(
        'find "$gate_bootstrap" -xdev'
    ) < gate_stop.index(
        "On the Mac, remove the two test-only packages"
    ) < gate_stop.index(
        "/usr/local/sbin/codex-security-lockdown.sh --production-only"
    )

    full_gate = runbook.index(
        "--pairing-bootstrap /absolute/private/edge-full/device.cxc1 \\\n"
        "  --timeout 60"
    )
    reload_gate = runbook.index(
        "--reload-continuity-bootstrap "
        "/absolute/private/reload-continuity/device.cxc1"
    )
    production_reload_gate = runbook.index(
        '--reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1"'
    )
    android_gate = runbook.index(
        "Only after reload continuity passes, provision the Gate APK"
    )
    teardown = runbook.index("--profile gate stop edge-gate relay-gate")
    timers = runbook.index("sudo systemctl enable --now")
    assert (
        full_gate
        < reload_gate
        < production_reload_gate
        < android_gate
        < teardown
        < timers
    )
    certificate_section = runbook.split(
        "## Certificate rotation, recovery, and monitoring",
        maxsplit=1,
    )[1].split(
        "Only after reload continuity passes",
        maxsplit=1,
    )[0]
    for marker in (
        "bootstrap-ephemeral-continuity",
        "revoke-ephemeral-continuity",
        "purge-ephemeral-continuity",
        "--ip 192.0.0.8 --port 27461",
        "--cleanup-ready-signal",
        "--revoked-signal",
        "production-continuity.json",
        "ephemeralIdentityRevoked",
        "certificate_failure_gate.py",
        "15/15 zero-skip tests",
    ):
        assert marker in certificate_section
    assert certificate_section.index(
        "bootstrap-ephemeral-continuity"
    ) < certificate_section.index(
        '--reload-continuity-bootstrap "$SIGNAL_DIR/production-device.cxc1"'
    ) < certificate_section.index(
        "revoke-ephemeral-continuity"
    ) < certificate_section.index(
        "purge-ephemeral-continuity"
    )

    for section in (rollback, gate_start, gate_stop):
        assert 'test -z "$(docker compose' not in section


def test_connector_gate_docs_use_only_fixed_android_role_bootstrap():
    repository = BUNDLE.parents[1]
    docs = (
        repository / "docs/deployment/TURKEY_CONNECTOR_RUNBOOK.md",
        repository / "deploy/turkey/README_CONNECTOR.md",
    )
    fixed_android_bootstrap = (
        "/etc/chebycodex-relay/gate-bootstrap/android-e2e/node-bootstrap.json"
    )
    operator_env = "--env-file /etc/chebycodex-gate-connector/operator.env"
    node_env = "--env-file /etc/chebycodex-gate-connector/node.env"

    for path in docs:
        content = path.read_text(encoding="utf-8")
        assert content.count(fixed_android_bootstrap) >= 3
        assert "/root/gate-node-bootstrap.json" not in content
        assert "Never install" in content
        assert "`edge-full`" in content
        assert "`reload-continuity`" in content
        assert content.count(operator_env) >= 2
        assert content.count(node_env) >= 2
        assert "config --quiet" in content
        assert "up --detach gate-connector" in content
        assert "rm -f --" in content
        assert "test ! -e" in content
        assert "Gate Relay DB/WAL/SHM" in content
        assert "production-only" in content


def test_committed_public_fixtures_are_ca_only_and_port_isolated():
    fixture_dir = BUNDLE / "tests/fixtures"
    for filename, port in (
        ("relay-trust-bundle.production.json", 27461),
        ("relay-trust-bundle.gate.json", 27462),
    ):
        document, authorities = certs._parse_trust_bundle(
            (fixture_dir / filename).read_bytes(), PUBLIC_IP, port
        )
        assert document["servicePorts"] == [port]
        assert len(authorities) == 1
        authority = next(iter(authorities.values()))
        constraints = authority.extensions.get_extension_for_class(
            x509.BasicConstraints
        )
        usage = authority.extensions.get_extension_for_class(x509.KeyUsage)
        assert constraints.value.ca and constraints.value.path_length == 0
        assert usage.value.key_cert_sign
