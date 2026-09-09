from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


BUNDLE = Path(__file__).resolve().parents[1]
RECOVERY = BUNDLE / "recover_certificate.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir(mode=0o700)
    binary_directory.chmod(0o700)
    log = tmp_path / "docker.log"
    docker = binary_directory / "docker"
    docker.write_text(
        f"""#!{sys.executable}
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as output:
    output.write(" ".join(arguments) + "\\n")

scenario = os.environ["FAKE_DOCKER_SCENARIO"]
state_path = os.environ["FAKE_DOCKER_STATE"]

def fail_once(name):
    try:
        with open(state_path, encoding="ascii") as source:
            state = source.read()
    except FileNotFoundError:
        state = ""
    marker = name + "\\n"
    if marker not in state:
        with open(state_path, "a", encoding="ascii") as output:
            output.write(marker)
        return True
    return False

if "cert-tool" in arguments and "show" in arguments:
    print("a" * 64)
    raise SystemExit(0)
if "cert-tool" in arguments and "rollback" in arguments:
    raise SystemExit(
        1
        if scenario in {{"rollback-fail", "previous-missing", "previous-invalid"}}
        else 0
    )
if "cert-consumer" in arguments and "consume" in arguments:
    failed = scenario == "consume-fail" and "--resume-only" not in arguments
    raise SystemExit(1 if failed else 0)
if "ps" in arguments and "edge-gate" in arguments:
    raise SystemExit(0)
if "nginx" in arguments and "-t" in arguments:
    offline_bootstrap = "run" in arguments and "exec" not in arguments
    failed = scenario == "nginx-test-fail" or (
        scenario == "bootstrap-nginx-test-fail" and offline_bootstrap
    )
    raise SystemExit(1 if failed else 0)
if "nginx" in arguments and "-s" in arguments and "reload" in arguments:
    failed = scenario == "reload-fail" or (
        scenario == "rotation-reload-once" and fail_once("reload")
    )
    raise SystemExit(1 if failed else 0)
if "tls-monitor" in arguments:
    failed = scenario == "fingerprint-mismatch" or (
        scenario == "rotation-monitor-once" and fail_once("monitor")
    )
    raise SystemExit(1 if failed else 0)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    python = binary_directory / "python3"
    python.write_text(
        f"""#!{sys.executable}
import os
import sys

with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as output:
    output.write("python3 " + " ".join(sys.argv[1:]) + "\\n")
if (
    os.environ["FAKE_DOCKER_SCENARIO"] == "loopback-occupied"
    and "--require-free-loopback-port" in sys.argv
):
    raise SystemExit(1)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    python.chmod(0o700)
    return binary_directory, log


def _environment(
    tmp_path: Path,
    binary_directory: Path,
    log: Path,
    scenario: str,
) -> dict[str, str]:
    tls = tmp_path / "tls"
    staging = tmp_path / "staging"
    tls.mkdir(mode=0o700, exist_ok=True)
    staging.mkdir(mode=0o700, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binary_directory}:{environment['PATH']}",
            "FAKE_DOCKER_LOG": str(log),
            "FAKE_DOCKER_SCENARIO": scenario,
            "FAKE_DOCKER_STATE": str(tmp_path / "docker.state"),
            "CHEBY_PUBLIC_IP": "192.0.0.9",
            "CHEBY_BIND_IP": "192.168.0.43",
            "CHEBY_RELAY_PUBLIC_PORT": "27461",
            "CHEBY_RELAY_GATE_PORT": "27462",
            "CHEBY_RELAY_LOOPBACK_PORT": "18080",
            "CHEBY_RELAY_TLS_DIR": str(tls),
            "CHEBY_RELAY_CERT_STAGE_DIR": str(staging),
        }
    )
    return environment


def _run_recovery(
    tmp_path: Path,
    scenario: str,
) -> tuple[subprocess.CompletedProcess, str]:
    binary_directory, log = _fake_docker(tmp_path)
    environment = _environment(tmp_path, binary_directory, log, scenario)
    completed = subprocess.run(
        ["/bin/sh", str(RECOVERY), "simulated certificate failure"],
        cwd=BUNDLE,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return completed, log.read_text(encoding="utf-8")


def _run_script(
    tmp_path: Path,
    scenario: str,
    script_name: str,
) -> tuple[subprocess.CompletedProcess, str]:
    binary_directory, log = _fake_docker(tmp_path)
    environment = _environment(tmp_path, binary_directory, log, scenario)
    if script_name == "rotate_certificate.sh":
        candidate = Path(environment["CHEBY_RELAY_CERT_STAGE_DIR"]) / "candidate"
        candidate.mkdir(mode=0o700)
        (candidate / "release.json").write_text("{}\n", encoding="ascii")
    completed = subprocess.run(
        ["/bin/sh", str(BUNDLE / script_name)],
        cwd=BUNDLE,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return completed, log.read_text(encoding="utf-8")


def _has_command(log: str, *parts: str) -> bool:
    return any(
        all(part in shlex.split(line) for part in parts)
        for line in log.splitlines()
    )


@pytest.mark.parametrize(
    ("scenario", "trigger"),
    (
        ("rollback-fail", ("cert-tool", "rollback")),
        ("previous-missing", ("cert-tool", "rollback")),
        ("previous-invalid", ("cert-tool", "rollback")),
        ("nginx-test-fail", ("nginx", "-t")),
        ("reload-fail", ("nginx", "-s", "reload")),
        ("fingerprint-mismatch", ("tls-monitor", "--port", "27461")),
    ),
)
def test_recovery_failures_deactivate_current_and_stop_both_edges(
    tmp_path: Path,
    scenario: str,
    trigger: tuple[str, ...],
) -> None:
    completed, log = _run_recovery(tmp_path, scenario)
    assert completed.returncode == 1
    assert _has_command(log, "cert-tool", "rollback")
    assert _has_command(log, *trigger)
    assert _has_command(log, "cert-tool", "deactivate")
    assert _has_command(log, "stop", "edge")
    assert _has_command(log, "stop", "edge-gate")
    assert "failed closed" in completed.stderr


def test_verified_previous_reloads_without_deactivation_or_stop(
    tmp_path: Path,
) -> None:
    completed, log = _run_recovery(tmp_path, "verified-previous")
    assert completed.returncode == 1
    assert _has_command(log, "cert-tool", "rollback")
    assert _has_command(log, "nginx", "-t")
    assert _has_command(log, "nginx", "-s", "reload")
    assert _has_command(log, "tls-monitor", "--port", "27461")
    assert not _has_command(log, "cert-tool", "deactivate")
    assert not _has_command(log, "stop", "edge")
    assert "restored and publicly verified" in completed.stderr


def test_bootstrap_nginx_validation_failure_invokes_verified_rollback(
    tmp_path: Path,
) -> None:
    completed, log = _run_script(
        tmp_path,
        "bootstrap-nginx-test-fail",
        "bootstrap_edge.sh",
    )
    assert completed.returncode == 1
    assert _has_command(log, "run", "edge", "nginx", "-t")
    assert _has_command(log, "cert-tool", "rollback")
    assert not _has_command(log, "up", "relay", "edge")
    assert not _has_command(log, "cert-tool", "deactivate")


def test_bootstrap_refuses_activation_when_production_loopback_is_occupied(
    tmp_path: Path,
) -> None:
    completed, log = _run_script(
        tmp_path,
        "loopback-occupied",
        "bootstrap_edge.sh",
    )
    assert completed.returncode == 1
    assert _has_command(
        log,
        "python3",
        "--require-free-loopback-port",
        "18080",
    )
    assert not _has_command(log, "up", "relay", "edge")
    assert not _has_command(log, "cert-tool", "activate")


@pytest.mark.parametrize(
    ("scenario", "trigger"),
    (
        ("rotation-reload-once", ("nginx", "-s", "reload")),
        ("rotation-monitor-once", ("tls-monitor", "--port", "27461")),
    ),
)
def test_rotation_post_activation_failure_invokes_verified_rollback(
    tmp_path: Path,
    scenario: str,
    trigger: tuple[str, ...],
) -> None:
    completed, log = _run_script(
        tmp_path,
        scenario,
        "rotate_certificate.sh",
    )
    assert completed.returncode == 1
    assert _has_command(log, "cert-tool", "activate")
    assert _has_command(log, *trigger)
    assert _has_command(log, "cert-tool", "rollback")
    assert not _has_command(log, "cert-tool", "deactivate")
    assert not _has_command(log, "stop", "edge")


def test_rotation_consumes_only_after_reload_and_public_monitor(tmp_path) -> None:
    completed, log = _run_script(
        tmp_path,
        "rotation-success",
        "rotate_certificate.sh",
    )
    assert completed.returncode == 0
    commands = [shlex.split(line) for line in log.splitlines()]
    resume = next(
        index
        for index, command in enumerate(commands)
        if "cert-consumer" in command
        and "consume" in command
        and "--resume-only" in command
    )
    activate = next(
        index
        for index, command in enumerate(commands)
        if "cert-tool" in command and "activate" in command
    )
    reload = next(
        index
        for index, command in enumerate(commands)
        if "nginx" in command and "-s" in command and "reload" in command
    )
    monitor = next(
        index
        for index, command in enumerate(commands)
        if "tls-monitor" in command
    )
    consume = next(
        index
        for index, command in enumerate(commands)
        if "cert-consumer" in command
        and "consume" in command
        and "--resume-only" not in command
    )
    assert resume < activate < reload < monitor < consume


def test_bootstrap_consumes_only_after_public_monitor(tmp_path) -> None:
    completed, log = _run_script(tmp_path, "success", "bootstrap_edge.sh")
    assert completed.returncode == 0
    commands = [shlex.split(line) for line in log.splitlines()]
    activate = next(
        index
        for index, command in enumerate(commands)
        if "cert-tool" in command and "activate" in command
    )
    monitor = next(
        index for index, command in enumerate(commands) if "tls-monitor" in command
    )
    consume = next(
        index
        for index, command in enumerate(commands)
        if "cert-consumer" in command and "consume" in command
    )
    assert activate < monitor < consume


def test_bootstrap_consume_failure_keeps_verified_edge_running(tmp_path) -> None:
    completed, log = _run_script(tmp_path, "consume-fail", "bootstrap_edge.sh")
    assert completed.returncode == 1
    assert _has_command(log, "tls-monitor", "--port", "27461")
    assert _has_command(log, "cert-consumer", "consume")
    assert not _has_command(log, "cert-tool", "rollback")
    assert not _has_command(log, "stop", "edge")


def test_consume_failure_does_not_rollback_or_stop_healthy_edge(tmp_path) -> None:
    completed, log = _run_script(
        tmp_path,
        "consume-fail",
        "rotate_certificate.sh",
    )
    assert completed.returncode == 1
    assert _has_command(log, "cert-tool", "activate")
    assert _has_command(log, "nginx", "-s", "reload")
    assert _has_command(log, "tls-monitor", "--port", "27461")
    assert _has_command(log, "cert-consumer", "consume")
    assert not _has_command(log, "cert-tool", "rollback")
    assert not _has_command(log, "cert-tool", "deactivate")
    assert not _has_command(log, "stop", "edge")
