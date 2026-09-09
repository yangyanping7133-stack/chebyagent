from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from cheby_relay.admin import bootstrap as relay_admin_bootstrap
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

import edge_gate


def pairing_envelope(secret: str = "pair_" + "s" * 43) -> bytes:
    document = {
        "v": 1,
        "assistantId": "asst_" + "A" * 22,
        "pairingSecret": secret,
    }
    raw = json.dumps(document, separators=(",", ":")).encode("ascii")
    return b"CXC1." + base64.urlsafe_b64encode(raw).rstrip(b"=") + b"\n"


def ca_certificate(common_name: str) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )


def test_target_requires_an_explicit_edge_port() -> None:
    parser = edge_gate.create_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--ip",
                "203.0.113.7",
                "--ca-cert",
                "/tmp/ca.pem",
                "--boundary-only",
            ]
        )
    with pytest.raises(edge_gate.GateError, match="27461 or 27462"):
        edge_gate.parse_target("203.0.113.7", 443, 5)
    assert edge_gate.parse_target("203.0.113.7", 27461, 5).authority.endswith(
        ":27461"
    )
    assert edge_gate.parse_target("203.0.113.7", 27462, 5).authority.endswith(
        ":27462"
    )
    with pytest.raises(edge_gate.GateError, match="at most 60"):
        edge_gate.parse_target("203.0.113.7", 27462, 61)


def test_full_gate_requires_explicit_60_second_network_timeout() -> None:
    bootstrap = edge_gate.PairingBootstrap(
        "asst_" + "A" * 22,
        "pair_" + "s" * 43,
    )
    edge_gate.require_full_gate_timeout(None, 10)
    edge_gate.require_full_gate_timeout(bootstrap, 60)
    for timeout_seconds in (10, 59):
        with pytest.raises(
            edge_gate.GateError,
            match="explicit --timeout 60",
        ):
            edge_gate.require_full_gate_timeout(bootstrap, timeout_seconds)


def test_cli_requires_an_explicit_non_skipping_mode() -> None:
    parser = edge_gate.create_parser()
    common = [
        "--ip",
        "203.0.113.7",
        "--port",
        "27461",
        "--ca-cert",
        "/tmp/ca.pem",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(common)
    with pytest.raises(SystemExit):
        parser.parse_args(
            [*common, "--boundary-only", "--pairing-bootstrap", "/tmp/pairing"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                *common,
                "--pairing-bootstrap",
                "/tmp/pairing",
                "--reload-continuity-bootstrap",
                "/tmp/reload-pairing",
            ]
        )
    boundary = parser.parse_args([*common, "--boundary-only"])
    assert boundary.boundary_only
    assert boundary.timeout == 10


def test_reload_continuity_cli_requires_all_coordination_inputs(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        edge_gate,
        "load_ca_certificate",
        lambda _: (object(), "test-ca"),
    )
    monkeypatch.setattr(
        edge_gate,
        "read_pairing_bootstrap",
        lambda _: edge_gate.PairingBootstrap(
            "asst_" + "A" * 22,
            "pair_" + "s" * 43,
        ),
    )
    result = edge_gate.main(
        [
            "--ip",
            "203.0.113.7",
            "--port",
            "27462",
            "--ca-cert",
            "/tmp/ca.pem",
            "--reload-continuity-bootstrap",
            "/tmp/pairing",
        ]
    )
    assert result == 2
    output = capsys.readouterr()
    assert (
        "requires expected leaf, ready signal, reloaded signal, and manifest"
        in output.err
    )
    assert "pair_" not in output.err


def test_ca_loader_accepts_current_plus_next_without_silent_truncation(
    tmp_path: Path,
) -> None:
    current = ca_certificate("current")
    next_authority = ca_certificate("next")
    current_pem = current.public_bytes(edge_gate.serialization.Encoding.PEM)
    next_pem = next_authority.public_bytes(edge_gate.serialization.Encoding.PEM)
    bundle = tmp_path / "current-next.pem"
    bundle.write_bytes(current_pem + next_pem)

    loaded, normalized = edge_gate.load_ca_certificate(str(bundle))
    assert loaded.fingerprint(hashes.SHA256()) == current.fingerprint(
        hashes.SHA256()
    )
    assert normalized.count("-----BEGIN CERTIFICATE-----") == 2
    edge_gate.create_tls_context(normalized)

    bundle.write_bytes(current_pem + current_pem)
    with pytest.raises(edge_gate.GateError, match="CA certificate is invalid"):
        edge_gate.load_ca_certificate(str(bundle))

    third_pem = ca_certificate("third").public_bytes(
        edge_gate.serialization.Encoding.PEM
    )
    bundle.write_bytes(current_pem + next_pem + third_pem)
    with pytest.raises(edge_gate.GateError, match="CA certificate is invalid"):
        edge_gate.load_ca_certificate(str(bundle))


def test_each_forwarding_header_is_probed_in_its_own_request(monkeypatch) -> None:
    target = edge_gate.parse_target("203.0.113.7", 27461, 5)
    observed: list[tuple[str, str]] = []

    def fake_https_request(
        actual_target,
        context,
        method,
        raw_target,
        *,
        headers=(),
        body=b"",
        host_values=None,
    ):
        assert actual_target == target
        assert method == "GET"
        assert raw_target == "/healthz"
        assert body == b""
        assert host_values is None
        assert len(headers) == 1
        observed.append(headers[0])
        return edge_gate.HttpResponse(400, (), b"")

    monkeypatch.setattr(edge_gate, "https_request", fake_https_request)
    edge_gate.check_forged_forwarding_headers(target, object())
    assert observed == list(edge_gate.FORGED_FORWARDING_HEADERS)


def test_boundary_only_gate_has_zero_skips(monkeypatch) -> None:
    target = edge_gate.parse_target("203.0.113.7", 27461, 5)

    monkeypatch.setattr(edge_gate, "create_tls_context", lambda _: object())
    monkeypatch.setattr(edge_gate, "verify_tls_identity", lambda *_: None)
    monkeypatch.setattr(edge_gate, "check_health", lambda *_: None)
    monkeypatch.setattr(
        edge_gate,
        "expect_websocket_http_rejection",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        edge_gate,
        "expect_websocket_close",
        lambda *_args, **_kwargs: None,
    )

    def fake_https_request(
        _target,
        _context,
        _method,
        raw_target,
        *,
        headers=(),
        body=b"",
        host_values=None,
    ):
        assert body == b""
        if raw_target == "/relay/v1/unknown":
            return edge_gate.HttpResponse(404, (), b"")
        if headers:
            assert len(headers) == 1
            assert headers[0] in edge_gate.FORGED_FORWARDING_HEADERS
            return edge_gate.HttpResponse(400, (), b"")
        assert host_values is not None
        return edge_gate.HttpResponse(400 if len(host_values) > 1 else 421, (), b"")

    monkeypatch.setattr(edge_gate, "https_request", fake_https_request)
    recorder = edge_gate.run_gate(target, "test-ca", None)
    assert all(result.state == "PASS" for result in recorder.results)
    assert not any(result.state == "SKIP" for result in recorder.results)
    assert "each forged forwarding header rejected" in {
        result.name for result in recorder.results
    }


def test_raw_request_preserves_explicit_and_repeated_host() -> None:
    target = edge_gate.parse_target("203.0.113.7", 27462, 5)
    request = edge_gate.build_raw_request(
        "GET",
        "/healthz",
        headers=[
            ("Host", target.authority),
            ("Host", target.authority),
            ("Connection", "close"),
        ],
    )
    assert request.startswith(b"GET /healthz HTTP/1.1\r\n")
    assert request.count(b"Host: 203.0.113.7:27462\r\n") == 2
    assert b":443" not in request


def test_raw_request_rejects_header_and_target_injection() -> None:
    with pytest.raises(edge_gate.GateError):
        edge_gate.build_raw_request(
            "GET",
            "/healthz\r\nInjected: yes",
            headers=[("Host", "203.0.113.7:27462")],
        )
    with pytest.raises(edge_gate.GateError):
        edge_gate.build_raw_request(
            "GET",
            "/healthz",
            headers=[("Host", "203.0.113.7:27462\r\nInjected: yes")],
        )


def test_pairing_envelope_is_strict_and_secret_is_not_in_repr() -> None:
    secret = "pair_" + "x" * 43
    parsed = edge_gate.parse_pairing_envelope(pairing_envelope(secret))
    assert parsed.assistant_id == "asst_" + "A" * 22
    assert parsed.pairing_secret == secret
    assert secret not in repr(parsed)

    padded = pairing_envelope(secret).rstrip() + b"="
    with pytest.raises(edge_gate.GateError):
        edge_gate.parse_pairing_envelope(padded)

    decoded = base64.urlsafe_b64decode(
        pairing_envelope(secret)[5:].strip() + b"=="
    )
    with_extra = json.loads(decoded)
    with_extra["relayOrigin"] = "https://example.invalid"
    extra_raw = json.dumps(with_extra, separators=(",", ":")).encode("ascii")
    extra = b"CXC1." + base64.urlsafe_b64encode(extra_raw).rstrip(b"=")
    with pytest.raises(edge_gate.GateError):
        edge_gate.parse_pairing_envelope(extra)


def test_relay_admin_explicit_gate_output_is_accepted_by_edge_gate(tmp_path) -> None:
    device = tmp_path / "device.cxc1"
    node = tmp_path / "node.json"
    relay_admin_bootstrap(
        database=str(tmp_path / "relay.sqlite3"),
        relay_origin="https://203.0.113.20:27462",
        device_output=str(device),
        node_output=str(node),
        device_format="cxc1",
    )

    parsed = edge_gate.read_pairing_bootstrap(str(device))
    assert parsed.assistant_id == json.loads(node.read_text())["assistantId"]


def test_pairing_file_requires_exact_private_mode(tmp_path: Path) -> None:
    path = tmp_path / "gate-pairing.cxc1"
    path.write_bytes(pairing_envelope())
    path.chmod(0o644)
    with pytest.raises(edge_gate.GateError, match="0600"):
        edge_gate.read_pairing_bootstrap(str(path))
    path.chmod(0o600)
    parsed = edge_gate.read_pairing_bootstrap(str(path))
    assert parsed.assistant_id.startswith("asst_")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pairing_file_rejects_relative_path_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(edge_gate.GateError, match="absolute"):
        edge_gate.read_pairing_bootstrap("gate-pairing.cxc1")
    target = tmp_path / "target.cxc1"
    target.write_bytes(pairing_envelope())
    target.chmod(0o600)
    link = tmp_path / "link.cxc1"
    link.symlink_to(target)
    if hasattr(os, "O_NOFOLLOW"):
        with pytest.raises(edge_gate.GateError, match="opened safely"):
            edge_gate.read_pairing_bootstrap(str(link))


def test_reload_signals_require_private_directory_and_exact_mode(
    tmp_path: Path,
) -> None:
    private = tmp_path / "signals"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    ready = private / "ready"
    edge_gate.write_private_signal(str(ready), b"READY\n")
    assert ready.read_bytes() == b"READY\n"
    assert stat.S_IMODE(ready.stat().st_mode) == 0o600

    completed = private / "reloaded"
    completed.write_bytes(b"RELOADED\n")
    completed.chmod(0o644)
    with pytest.raises(edge_gate.GateError, match="0600"):
        edge_gate.read_private_signal_if_present(
            str(completed),
            b"RELOADED\n",
        )
    completed.chmod(0o600)
    assert edge_gate.read_private_signal_if_present(
        str(completed),
        b"RELOADED\n",
    )

    public = tmp_path / "public-signals"
    public.mkdir(mode=0o755)
    with pytest.raises(edge_gate.GateError, match="directory must be private"):
        edge_gate.write_private_signal(str(public / "ready"), b"READY\n")


class FakeTlsSocket:
    def __init__(self, leaf_der: bytes) -> None:
        self.leaf_der = leaf_der

    def getpeercert(self, *, binary_form: bool) -> bytes:
        assert binary_form
        return self.leaf_der


class FakeReloadWebSocket:
    def __init__(
        self,
        device: edge_gate.EnrolledDevice,
        initial_leaf_der: bytes,
    ) -> None:
        self.device = device
        self.connection = FakeTlsSocket(initial_leaf_der)
        self.ready_pending = True
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.revoke_on_next_receive = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def send_frame(self, opcode: int, payload: bytes) -> None:
        assert opcode == 0x1
        self.sent.append(json.loads(payload))

    def receive_json(self):
        if self.revoke_on_next_receive:
            self.revoke_on_next_receive = False
            raise edge_gate.WebSocketClosed(4401)
        if self.ready_pending:
            self.ready_pending = False
            return {
                "v": 1,
                "type": "ready",
                "assistantId": self.device.assistant_id,
                "principalId": self.device.device_id,
                "role": "device",
                "ackCursor": 0,
                "nextDeliverySeq": 1,
                "nodeStatus": "online",
            }
        ping = self.sent[-1]
        return {"v": 1, "type": "pong", "nonce": ping["nonce"]}


def test_reload_wait_uses_virtual_time_heartbeat_without_sleep(
    monkeypatch,
    tmp_path: Path,
) -> None:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    reloaded = signal_dir / "reloaded"
    device = edge_gate.EnrolledDevice(
        assistant_id="asst_" + "A" * 22,
        device_id="dev_" + "D" * 22,
        access_token="access_" + "a" * 40,
        key=ec.generate_private_key(ec.SECP256R1()),
    )
    websocket = FakeReloadWebSocket(device, b"old-leaf")
    websocket.ready_pending = False
    clock = {"value": 100.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["value"] += seconds
        reloaded.write_bytes(b"RELOADED\n")
        reloaded.chmod(0o600)

    monkeypatch.setattr(edge_gate.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(edge_gate.time, "sleep", fake_sleep)
    edge_gate.wait_for_reload_signal(str(reloaded), websocket, 60)
    assert sleeps == [edge_gate.RELOAD_HEARTBEAT_SECONDS]
    assert [frame["type"] for frame in websocket.sent] == ["ping"]


def _exercise_reload_gate(
    monkeypatch,
    tmp_path: Path,
    *,
    new_connection_leaf_sha256: str | None = None,
    port: int = 27462,
):
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    ready = signal_dir / "ready"
    reloaded = signal_dir / "reloaded"
    cleanup_ready = signal_dir / "cleanup-ready"
    revoked = signal_dir / "revoked"
    manifest = signal_dir / "manifest.json"
    initial_leaf = b"initial-leaf-for-test"
    expected_new = hashlib.sha256(b"rotated-leaf-for-test").hexdigest()
    device = edge_gate.EnrolledDevice(
        assistant_id="asst_" + "A" * 22,
        device_id="dev_" + "D" * 22,
        access_token="access_" + "a" * 40,
        key=ec.generate_private_key(ec.SECP256R1()),
    )
    websocket = FakeReloadWebSocket(device, initial_leaf)
    real_write_signal = edge_gate.write_private_signal

    def signal_reload(path_value: str, content: bytes) -> None:
        real_write_signal(path_value, content)
        if content == b"READY\n":
            reloaded.write_bytes(b"RELOADED\n")
            reloaded.chmod(0o600)
        elif content == b"REVOKE\n":
            websocket.revoke_on_next_receive = True
            revoked.write_bytes(b"REVOKED\n")
            revoked.chmod(0o600)

    monkeypatch.setattr(edge_gate, "create_tls_context", lambda _: object())
    monkeypatch.setattr(
        edge_gate,
        "enroll_device",
        lambda *_args, **_kwargs: device,
    )
    monkeypatch.setattr(edge_gate, "open_websocket", lambda *_args, **_kwargs: websocket)
    monkeypatch.setattr(edge_gate, "write_private_signal", signal_reload)
    monkeypatch.setattr(
        edge_gate,
        "served_leaf_sha256",
        lambda *_: new_connection_leaf_sha256 or expected_new,
    )
    target = edge_gate.parse_target("203.0.113.7", port, 5)
    bootstrap = edge_gate.PairingBootstrap(
        device.assistant_id,
        "pair_" + "s" * 43,
    )
    recorder = edge_gate.run_reload_continuity_gate(
        target,
        "test-ca",
        bootstrap,
        expected_new_leaf_sha256=expected_new,
        ready_signal=str(ready),
        reloaded_signal=str(reloaded),
        wait_timeout_seconds=5,
        manifest_path=str(manifest),
        cleanup_ready_signal=(
            str(cleanup_ready) if port == 27461 else None
        ),
        revoked_signal=str(revoked) if port == 27461 else None,
    )
    return recorder, websocket, ready, manifest, cleanup_ready


def test_reload_continuity_keeps_same_authenticated_socket_and_checks_new_leaf(
    monkeypatch, tmp_path: Path
) -> None:
    recorder, websocket, ready, manifest, _ = _exercise_reload_gate(
        monkeypatch, tmp_path
    )
    assert [(result.name, result.state) for result in recorder.results] == [
        (
            "authenticated WSS survives reload and new TLS receives expected leaf",
            "PASS",
        )
    ]
    assert ready.read_bytes() == b"READY\n"
    assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    assert websocket.closed
    assert [frame["type"] for frame in websocket.sent] == ["ping", "ping"]
    assert websocket.sent[0]["nonce"] != websocket.sent[1]["nonce"]
    evidence = json.loads(manifest.read_text())
    assert evidence["outcome"] == "PASS"
    assert evidence["target"]["port"] == 27462
    assert evidence["identityScope"] == "isolated-gate"
    assert evidence["evidence"]["newConnectionExpectedLeaf"] is True
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_reload_continuity_rejects_a_new_connection_with_wrong_leaf(
    monkeypatch, tmp_path: Path
) -> None:
    recorder, _, _, manifest, _ = _exercise_reload_gate(
        monkeypatch,
        tmp_path,
        new_connection_leaf_sha256="f" * 64,
    )
    assert recorder.failed
    assert recorder.results[0].detail == (
        "new TLS connection did not receive the expected rotated leaf"
    )
    assert json.loads(manifest.read_text())["outcome"] == "FAIL"


def test_reload_continuity_rejects_stale_completion_signal(
    tmp_path: Path,
) -> None:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    reloaded = signal_dir / "reloaded"
    reloaded.write_bytes(b"RELOADED\n")
    reloaded.chmod(0o600)
    target = edge_gate.parse_target("203.0.113.7", 27462, 5)
    bootstrap = edge_gate.PairingBootstrap(
        "asst_" + "A" * 22,
        "pair_" + "s" * 43,
    )
    with pytest.raises(edge_gate.GateError, match="must not exist"):
        edge_gate.run_reload_continuity_gate(
            target,
            "test-ca",
            bootstrap,
            expected_new_leaf_sha256="a" * 64,
            ready_signal=str(signal_dir / "ready"),
            reloaded_signal=str(reloaded),
            wait_timeout_seconds=5,
            manifest_path=str(signal_dir / "manifest.json"),
        )


def test_production_reload_continuity_requires_revocation_coordination(
    tmp_path: Path,
) -> None:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    target = edge_gate.parse_target("203.0.113.7", 27461, 5)
    bootstrap = edge_gate.PairingBootstrap(
        "asst_" + "A" * 22,
        "pair_" + "s" * 43,
    )
    with pytest.raises(
        edge_gate.GateError,
        match="requires cleanup-ready and revoked signals",
    ):
        edge_gate.run_reload_continuity_gate(
            target,
            "test-ca",
            bootstrap,
            expected_new_leaf_sha256="a" * 64,
            ready_signal=str(signal_dir / "ready"),
            reloaded_signal=str(signal_dir / "reloaded"),
            wait_timeout_seconds=5,
            manifest_path=str(signal_dir / "manifest.json"),
        )


def test_gate_reload_continuity_rejects_partial_production_cleanup_signals(
    tmp_path: Path,
) -> None:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    target = edge_gate.parse_target("203.0.113.7", 27462, 5)
    bootstrap = edge_gate.PairingBootstrap(
        "asst_" + "A" * 22,
        "pair_" + "s" * 43,
    )
    with pytest.raises(
        edge_gate.GateError,
        match="only on port 27461",
    ):
        edge_gate.run_reload_continuity_gate(
            target,
            "test-ca",
            bootstrap,
            expected_new_leaf_sha256="a" * 64,
            ready_signal=str(signal_dir / "ready"),
            reloaded_signal=str(signal_dir / "reloaded"),
            wait_timeout_seconds=5,
            manifest_path=str(signal_dir / "manifest.json"),
            cleanup_ready_signal=str(signal_dir / "cleanup-ready"),
        )


def test_reload_continuity_rejects_colliding_artifact_paths(
    tmp_path: Path,
) -> None:
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir(mode=0o700)
    signal_dir.chmod(0o700)
    collision = signal_dir / "same"
    with pytest.raises(edge_gate.GateError, match="must be distinct"):
        edge_gate.run_reload_continuity_gate(
            edge_gate.parse_target("203.0.113.7", 27462, 5),
            "test-ca",
            edge_gate.PairingBootstrap(
                "asst_" + "A" * 22,
                "pair_" + "s" * 43,
            ),
            expected_new_leaf_sha256="a" * 64,
            ready_signal=str(collision),
            reloaded_signal=str(collision),
            wait_timeout_seconds=5,
            manifest_path=str(signal_dir / "manifest.json"),
        )


def test_production_reload_continuity_proves_revocation_and_new_leaf(
    monkeypatch,
    tmp_path: Path,
) -> None:
    recorder, websocket, _, manifest, cleanup_ready = _exercise_reload_gate(
        monkeypatch,
        tmp_path,
        port=27461,
    )
    assert not recorder.failed
    assert cleanup_ready.read_bytes() == b"REVOKE\n"
    assert [frame["type"] for frame in websocket.sent] == [
        "ping",
        "ping",
        "ping",
    ]
    document = json.loads(manifest.read_text())
    assert document["target"] == {"ip": "203.0.113.7", "port": 27461}
    assert document["identityScope"] == "ephemeral-production"
    assert document["evidence"] == recorder.evidence
    assert document["evidence"]["existingAuthenticatedWss"] is True
    assert document["evidence"]["postReloadPing"] is True
    assert document["evidence"]["newConnectionExpectedLeaf"] is True
    assert document["evidence"]["ephemeralIdentityRevoked"] is True
    rendered = manifest.read_text()
    assert "pair_" not in rendered
    assert "access_" not in rendered
    assert "asst_" not in rendered
    assert "dev_" not in rendered


def test_revocation_proof_fails_if_existing_socket_stays_usable() -> None:
    device = edge_gate.EnrolledDevice(
        assistant_id="asst_" + "A" * 22,
        device_id="dev_" + "D" * 22,
        access_token="access_" + "a" * 40,
        key=ec.generate_private_key(ec.SECP256R1()),
    )
    websocket = FakeReloadWebSocket(device, b"old-leaf")
    websocket.ready_pending = False
    with pytest.raises(
        edge_gate.GateError,
        match="remained usable",
    ):
        edge_gate.require_existing_connection_revoked(websocket)


def test_continuity_manifest_is_private_and_never_overwritten(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    manifest = private / "manifest.json"
    edge_gate.write_immutable_manifest(
        str(manifest),
        {"schema": "test", "outcome": "PASS"},
    )
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    original = manifest.read_bytes()
    with pytest.raises(edge_gate.GateError, match="already exists"):
        edge_gate.write_immutable_manifest(
            str(manifest),
            {"schema": "test", "outcome": "FAIL"},
        )
    assert manifest.read_bytes() == original


def test_continuity_manifest_does_not_remove_commit_racer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    manifest = private / "manifest.json"
    real_link = os.link
    real_open = os.open

    def racing_link(
        source,
        destination,
        *,
        src_dir_fd,
        dst_dir_fd,
        follow_symlinks,
    ):
        descriptor = real_open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(b'{"outcome":"RACER"}\n')
        return real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(edge_gate.os, "link", racing_link)
    with pytest.raises(edge_gate.GateError, match="already exists"):
        edge_gate.write_immutable_manifest(
            str(manifest),
            {"schema": "test", "outcome": "PASS"},
        )
    assert json.loads(manifest.read_text()) == {"outcome": "RACER"}
    assert list(private.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    ("failure_kind", "failure_call"),
    (
        ("stat", 2),
        ("fsync", 2),
        ("fsync", 3),
    ),
)
def test_continuity_manifest_publish_failure_never_leaves_pass(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
    failure_call: int,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    manifest = private / "manifest.json"
    real_stat = os.stat
    real_fsync = os.fsync
    matching_calls = 0
    injected = False

    def failing_stat(path, *args, **kwargs):
        nonlocal matching_calls, injected
        if (
            path == manifest.name
            and kwargs.get("dir_fd") is not None
            and not kwargs.get("follow_symlinks", True)
        ):
            matching_calls += 1
            if matching_calls == failure_call and not injected:
                injected = True
                raise OSError("injected published stat failure")
        return real_stat(path, *args, **kwargs)

    def failing_fsync(descriptor):
        nonlocal matching_calls, injected
        matching_calls += 1
        if matching_calls == failure_call and not injected:
            injected = True
            raise OSError("injected directory fsync failure")
        return real_fsync(descriptor)

    if failure_kind == "stat":
        monkeypatch.setattr(edge_gate.os, "stat", failing_stat)
    else:
        monkeypatch.setattr(edge_gate.os, "fsync", failing_fsync)

    with pytest.raises(edge_gate.GateError, match="persisted safely"):
        edge_gate.write_immutable_manifest(
            str(manifest),
            {"schema": "test", "outcome": "PASS"},
        )
    assert injected
    assert not manifest.exists()
    assert list(private.glob(".*.tmp")) == []

    edge_gate.write_immutable_manifest(
        str(manifest),
        {"schema": "test", "outcome": "FAIL"},
    )
    assert json.loads(manifest.read_text()) == {
        "outcome": "FAIL",
        "schema": "test",
    }


def test_continuity_manifest_invalid_published_inode_never_leaves_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    manifest = private / "manifest.json"
    real_stat = os.stat
    matching_calls = 0

    def mismatched_stat(path, *args, **kwargs):
        nonlocal matching_calls
        result = real_stat(path, *args, **kwargs)
        if (
            path == manifest.name
            and kwargs.get("dir_fd") is not None
            and not kwargs.get("follow_symlinks", True)
        ):
            matching_calls += 1
            if matching_calls == 1:
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino + 1,
                    st_nlink=result.st_nlink,
                )
        return result

    monkeypatch.setattr(edge_gate.os, "stat", mismatched_stat)
    with pytest.raises(edge_gate.GateError, match="published file is invalid"):
        edge_gate.write_immutable_manifest(
            str(manifest),
            {"schema": "test", "outcome": "PASS"},
        )
    assert not manifest.exists()
    assert list(private.glob(".*.tmp")) == []

    edge_gate.write_immutable_manifest(
        str(manifest),
        {"schema": "test", "outcome": "FAIL"},
    )
    assert json.loads(manifest.read_text()) == {
        "outcome": "FAIL",
        "schema": "test",
    }


def test_device_proof_is_low_s_and_verifies() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    headers = dict(
        edge_gate.proof_headers(
            key,
            method="GET",
            raw_target="/relay/v1/device",
            bearer_token="access-token-for-test-only",
        )
    )
    canonical = edge_gate.proof_canonical_request(
        method="GET",
        raw_target="/relay/v1/device",
        timestamp=headers["X-Cheby-Timestamp"],
        nonce=headers["X-Cheby-Nonce"],
        body=b"",
        bearer_token="access-token-for-test-only",
    )
    signature = base64.b64decode(headers["X-Cheby-Signature"], validate=True)
    _, s_value = decode_dss_signature(signature)
    assert s_value <= edge_gate.P256_ORDER // 2
    key.public_key().verify(signature, canonical, ec.ECDSA(hashes.SHA256()))


def test_oversized_message_payload_is_exactly_requested_size() -> None:
    payload = edge_gate._oversized_ping(edge_gate.MAX_FRAME_BYTES + 1)
    assert len(payload) == 12 * 1024 * 1024 + 1


class FakeOversizeWebSocket:
    def __init__(self, device: edge_gate.EnrolledDevice, close_code: int) -> None:
        self.device = device
        self.close_code = close_code
        self.sent_length = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def receive_json(self):
        return {
            "v": 1,
            "type": "ready",
            "assistantId": self.device.assistant_id,
            "principalId": self.device.device_id,
            "role": "device",
        }

    def send_frame(self, opcode: int, payload: bytes) -> None:
        assert opcode == 0x1
        self.sent_length = len(payload)
        raise edge_gate.WebSocketSendInterrupted("WebSocket send failed")

    def receive_frame(self):
        raise edge_gate.WebSocketClosed(self.close_code)


def oversize_device() -> edge_gate.EnrolledDevice:
    return edge_gate.EnrolledDevice(
        assistant_id="asst_" + "A" * 22,
        device_id="dev_" + "D" * 22,
        access_token="access_" + "a" * 40,
        key=ec.generate_private_key(ec.SECP256R1()),
    )


def test_websocket_send_wraps_peer_interruption() -> None:
    class InterruptedSocket:
        def sendall(self, _frame: bytes) -> None:
            raise OSError("peer closed")

    connection = edge_gate.WebSocketConnection(InterruptedSocket(), b"")
    with pytest.raises(
        edge_gate.WebSocketSendInterrupted,
        match="WebSocket send failed",
    ):
        connection.send_frame(0x1, b"payload")


def test_server_close_is_acknowledged_before_connection_is_marked_closed() -> None:
    class CloseSocket:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, frame: bytes) -> None:
            self.sent += frame

        def close(self) -> None:
            pass

    socket = CloseSocket()
    close_payload = struct.pack("!H", 4409)
    connection = edge_gate.WebSocketConnection(
        socket,
        bytes([0x88, len(close_payload)]) + close_payload,
    )

    with pytest.raises(edge_gate.WebSocketClosed) as closed:
        connection.receive_frame()

    assert closed.value.code == 4409
    assert connection.closed
    assert socket.sent[0] == 0x88
    masked_length = socket.sent[1]
    assert masked_length == 0x80 | len(close_payload)
    mask = socket.sent[2:6]
    masked_payload = socket.sent[6:]
    acknowledged = bytes(
        value ^ mask[index % 4] for index, value in enumerate(masked_payload)
    )
    assert acknowledged == close_payload


def test_oversized_frame_accepts_interrupted_send_only_with_4409(
    monkeypatch,
) -> None:
    device = oversize_device()
    application_websocket = FakeOversizeWebSocket(device, 4409)
    transport_websocket = FakeOversizeWebSocket(device, 1009)
    websockets = iter((application_websocket, transport_websocket))
    proof_nonces: list[str] = []

    def open_fake(*_args, **kwargs):
        proof_nonces.append(dict(kwargs["headers"])["X-Cheby-Nonce"])
        return next(websockets)

    monkeypatch.setattr(
        edge_gate,
        "open_websocket",
        open_fake,
    )

    edge_gate.check_authenticated_and_oversize(
        edge_gate.parse_target("203.0.113.7", 27462, 5),
        object(),
        device,
    )

    assert application_websocket.sent_length == edge_gate.MAX_FRAME_BYTES + 1
    assert transport_websocket.sent_length == edge_gate.MAX_FRAME_BYTES + 2
    assert len(proof_nonces) == 2
    assert proof_nonces[0] != proof_nonces[1]


def test_oversized_frame_interrupted_send_rejects_wrong_close(
    monkeypatch,
) -> None:
    device = oversize_device()
    websocket = FakeOversizeWebSocket(device, 1009)
    monkeypatch.setattr(
        edge_gate,
        "open_websocket",
        lambda *_args, **_kwargs: websocket,
    )

    with pytest.raises(edge_gate.GateError, match="wrong close code"):
        edge_gate.check_authenticated_and_oversize(
            edge_gate.parse_target("203.0.113.7", 27462, 5),
            object(),
            device,
        )


def test_transport_boundary_rejects_wrong_close(monkeypatch) -> None:
    device = oversize_device()
    websockets = iter(
        (
            FakeOversizeWebSocket(device, 4409),
            FakeOversizeWebSocket(device, 4409),
        )
    )
    monkeypatch.setattr(
        edge_gate,
        "open_websocket",
        lambda *_args, **_kwargs: next(websockets),
    )

    with pytest.raises(edge_gate.GateError, match="wrong close code"):
        edge_gate.check_authenticated_and_oversize(
            edge_gate.parse_target("203.0.113.7", 27462, 5),
            object(),
            device,
        )


def test_oversized_frame_interrupted_send_without_close_frame_fails(
    monkeypatch,
) -> None:
    device = oversize_device()

    class NoCloseFrame(FakeOversizeWebSocket):
        def receive_frame(self):
            raise edge_gate.GateError("WebSocket closed without a close frame")

    websocket = NoCloseFrame(device, 4409)
    monkeypatch.setattr(
        edge_gate,
        "open_websocket",
        lambda *_args, **_kwargs: websocket,
    )

    with pytest.raises(edge_gate.GateError, match="without a close frame"):
        edge_gate.check_authenticated_and_oversize(
            edge_gate.parse_target("203.0.113.7", 27462, 5),
            object(),
            device,
        )


def test_oversized_frame_does_not_swallow_other_send_failures(
    monkeypatch,
) -> None:
    device = oversize_device()

    class OtherSendFailure(FakeOversizeWebSocket):
        def send_frame(self, opcode: int, payload: bytes) -> None:
            raise edge_gate.GateError("different send failure")

    websocket = OtherSendFailure(device, 4409)
    monkeypatch.setattr(
        edge_gate,
        "open_websocket",
        lambda *_args, **_kwargs: websocket,
    )

    with pytest.raises(edge_gate.GateError, match="different send failure"):
        edge_gate.check_authenticated_and_oversize(
            edge_gate.parse_target("203.0.113.7", 27462, 5),
            object(),
            device,
        )


def test_recorder_never_renders_arbitrary_exception_text() -> None:
    credential = "cred-test-secret-must-not-appear"
    recorder = edge_gate.Recorder()

    def fail() -> None:
        raise RuntimeError(credential)

    assert recorder.check("safe check", fail) is False
    rendered = "\n".join(
        f"{result.name}:{result.detail}" for result in recorder.results
    )
    assert credential not in rendered
    assert "RuntimeError" in rendered


def test_pairing_is_never_allowed_on_production_port() -> None:
    target = edge_gate.parse_target("203.0.113.7", 27461, 5)
    bootstrap = edge_gate.parse_pairing_envelope(pairing_envelope())
    with pytest.raises(edge_gate.GateError, match="Gate port 27462"):
        edge_gate.enroll_device(target, object(), bootstrap)  # type: ignore[arg-type]


def test_gate_enrollment_constructs_verified_pop_in_memory(monkeypatch) -> None:
    target = edge_gate.parse_target("203.0.113.7", 27462, 5)
    bootstrap = edge_gate.parse_pairing_envelope(pairing_envelope())
    observed: dict[str, object] = {}

    def fake_https_request(
        actual_target,
        context,
        method,
        raw_target,
        *,
        headers=(),
        body=b"",
        host_values=None,
    ):
        assert actual_target == target
        assert method == "POST"
        assert raw_target == "/relay/v1/pairings/exchange"
        document = json.loads(body)
        assert document["assistantId"] == bootstrap.assistant_id
        assert document["pairingSecret"] == bootstrap.pairing_secret
        public_key = edge_gate.serialization.load_der_public_key(
            base64.b64decode(document["devicePublicKey"], validate=True)
        )
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        header_map = dict(headers)
        canonical = edge_gate.proof_canonical_request(
            method=method,
            raw_target=raw_target,
            timestamp=header_map["X-Cheby-Timestamp"],
            nonce=header_map["X-Cheby-Nonce"],
            body=body,
            bearer_token="",
        )
        signature = base64.b64decode(
            header_map["X-Cheby-Signature"], validate=True
        )
        public_key.verify(signature, canonical, ec.ECDSA(hashes.SHA256()))
        observed["verified"] = True
        response_body = json.dumps(
            {
                "assistantId": bootstrap.assistant_id,
                "deviceId": "dev_" + "D" * 22,
                "accessToken": "access_" + "a" * 40,
                "accessExpiresAt": 2_000_000_000,
                "refreshToken": "refresh_" + "r" * 40,
                "refreshExpiresAt": 2_000_000_100,
            },
            separators=(",", ":"),
        ).encode("ascii")
        return edge_gate.HttpResponse(
            200, (("Cache-Control", "no-store"),), response_body
        )

    monkeypatch.setattr(edge_gate, "https_request", fake_https_request)
    device = edge_gate.enroll_device(target, object(), bootstrap)  # type: ignore[arg-type]
    assert observed == {"verified": True}
    assert device.device_id == "dev_" + "D" * 22
    assert bootstrap.pairing_secret not in repr(device)
    assert device.access_token not in repr(device)
