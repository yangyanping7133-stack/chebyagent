from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from cheby_connector.config import (
    GATE_PRIVATE_RELAY_URL,
    PRODUCTION_PRIVATE_RELAY_URL,
    ConnectorSettings,
)
from cheby_connector.gate_bridge import (
    GATE_CONCURRENCY_PREFIX,
    GATE_REPLY_PREFIX,
    ScriptedGateCodexBridge,
)
from cheby_connector.gateway import DirectGatewayBackend
from cheby_connector.models import (
    ThreadsCreateParams,
    ThreadsReadParams,
    TurnsStartParams,
)
from cheby_gateway.bridge import BridgeError
from cheby_gateway.config import GatewaySettings


NODE_ID = "node_" + "N" * 22
DEVICE_ID = "dev_" + "D" * 22


def _settings(tmp_path: Path, **updates: object) -> ConnectorSettings:
    values: dict[str, object] = {
        "relay_url": PRODUCTION_PRIVATE_RELAY_URL,
        "connector_id": NODE_ID,
        "relay_token": "private-test-token",
        "state_path": str(tmp_path / "connector.sqlite3"),
        "health_path": str(tmp_path / "connector.health"),
    }
    values.update(updates)
    return ConnectorSettings(**values)  # type: ignore[arg-type]


def test_private_relay_config_is_an_exact_fail_closed_allowlist(
    tmp_path: Path,
) -> None:
    assert _settings(tmp_path).relay_url == PRODUCTION_PRIVATE_RELAY_URL
    assert (
        _settings(
            tmp_path,
            relay_url=GATE_PRIVATE_RELAY_URL,
            runtime_profile="gate",
        ).runtime_profile
        == "gate"
    )

    for invalid in (
        "ws://172.31.61.4:8080/relay/v1/node",
        "ws://172.31.61.3:18080/relay/v1/node",
        "ws://172.31.61.3:8080/relay/v1/device",
        "ws://172.31.61.3:8080/relay/v1/node?token=secret",
        "ws://relay-backend:8080/relay/v1/node",
    ):
        with pytest.raises(ValueError):
            _settings(tmp_path, relay_url=invalid)

    with pytest.raises(ValueError):
        _settings(
            tmp_path,
            relay_url=PRODUCTION_PRIVATE_RELAY_URL,
            runtime_profile="gate",
        )
    with pytest.raises(ValueError):
        _settings(tmp_path, relay_url=GATE_PRIVATE_RELAY_URL)


def test_gate_compose_has_no_codex_phonebridge_or_openclaw_runtime_inputs() -> None:
    compose_path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "turkey"
        / "docker-compose.connector-gate.yml"
    )
    source = compose_path.read_text(encoding="utf-8")
    for forbidden in (
        "CODEX_HOME",
        "CHEBY_CODEX_",
        "PHONEBRIDGE",
        "OPENCLAW",
        "3437",
        "3438",
        "3448",
    ):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_scripted_gate_reply_remembers_marker_and_survives_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "gate-script-state.json"
    bridge = ScriptedGateCodexBridge(str(state_path))
    events = []
    terminal = asyncio.Event()

    async def receive(event) -> None:
        events.append(event)
        if event.method == "turn/completed":
            terminal.set()

    bridge.set_event_handler(receive)
    await bridge.start()
    thread = await bridge.start_thread("Gate")
    first = await bridge.start_turn(
        thread.raw_id,
        "client-message-0001",
        [{"type": "text", "text": "MARKER-ALPHA"}],
    )
    await asyncio.wait_for(terminal.wait(), timeout=1)

    assert first.raw_id == "gate-turn-00000001"
    assert [event.method for event in events] == [
        "turn/started",
        "item/agentMessage/delta",
        "item/completed",
        "turn/completed",
    ]
    assert GATE_REPLY_PREFIX in events[1].params["delta"]
    assert "MARKER-ALPHA" in events[1].params["delta"]
    assert state_path.stat().st_mode & 0o077 == 0
    await bridge.close()

    restarted = ScriptedGateCodexBridge(str(state_path))
    remembered_events = []
    remembered_terminal = asyncio.Event()

    async def receive_remembered(event) -> None:
        remembered_events.append(event)
        if event.method == "turn/completed":
            remembered_terminal.set()

    restarted.set_event_handler(receive_remembered)
    await restarted.start()
    loaded = await restarted.read_thread(thread.raw_id)
    assert loaded.turns[0].raw_id == first.raw_id
    second = await restarted.start_turn(
        thread.raw_id,
        "client-message-0002",
        [{"type": "text", "text": "MARKER-BETA"}],
    )
    await asyncio.wait_for(remembered_terminal.wait(), timeout=1)
    reply = remembered_events[1].params["delta"]
    assert second.raw_id == "gate-turn-00000002"
    assert "Remembered: MARKER-ALPHA" in reply
    assert "Current: MARKER-BETA" in reply
    await restarted.close()


@pytest.mark.asyncio
async def test_load_first_turns_require_two_distinct_threads_at_server_barrier(
    tmp_path: Path,
) -> None:
    bridge = ScriptedGateCodexBridge(
        str(tmp_path / "gate-script-state.json"),
        load_barrier_timeout_seconds=1,
    )
    await bridge.start()
    first_thread = await bridge.start_thread("Gate A")
    second_thread = await bridge.start_thread("Gate B")
    run_token = "a" * 32
    first = asyncio.create_task(
        bridge.start_turn(
            first_thread.raw_id,
            "client-message-0001",
            [{"type": "text", "text": f"GATE-{run_token}-G1-T1-N1"}],
        )
    )
    await asyncio.sleep(0)
    assert not first.done()
    second = asyncio.create_task(
        bridge.start_turn(
            second_thread.raw_id,
            "client-message-0002",
            [{"type": "text", "text": f"GATE-{run_token}-G2-T2-N1"}],
        )
    )
    first_turn, second_turn = await asyncio.gather(first, second)
    first_reply = bridge.turns[first_turn.raw_id]["items"][1]["text"]
    second_reply = bridge.turns[second_turn.raw_id]["items"][1]["text"]
    expected_proof = f"{GATE_CONCURRENCY_PREFIX}: {run_token}:2"
    assert expected_proof in first_reply.splitlines()
    assert expected_proof in second_reply.splitlines()
    await bridge.close()


@pytest.mark.asyncio
async def test_load_barrier_rejects_a_serialized_single_thread_submission(
    tmp_path: Path,
) -> None:
    bridge = ScriptedGateCodexBridge(
        str(tmp_path / "gate-script-state.json"),
        load_barrier_timeout_seconds=0.01,
    )
    await bridge.start()
    thread = await bridge.start_thread("Gate A")
    with pytest.raises(
        BridgeError,
        match="cross-thread submission barrier was not satisfied",
    ):
        await bridge.start_turn(
            thread.raw_id,
            "client-message-0001",
            [{"type": "text", "text": f"GATE-{'b' * 32}-G1-T1-N1"}],
        )
    assert bridge.turns == {}
    await bridge.close()


@pytest.mark.asyncio
async def test_gate_backend_rejects_stdio_before_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = GatewaySettings(
        db_path=str(tmp_path / "gate-gateway.sqlite3"),
        pairing_secret="pair_" + "P" * 43,
        bridge_mode="stdio",
    )
    monkeypatch.setattr(
        GatewaySettings,
        "from_env",
        classmethod(lambda cls, environ=None: settings),
    )
    with pytest.raises(RuntimeError, match="scripted fake bridge"):
        await DirectGatewayBackend.from_env(
            NODE_ID,
            runtime_profile="gate",
            gate_script_state_path=str(tmp_path / "gate-script-state.json"),
        )


@pytest.mark.asyncio
async def test_gate_backend_projects_scripted_turns_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_settings = GatewaySettings(
        db_path=str(tmp_path / "gate-gateway.sqlite3"),
        pairing_secret="pair_" + "P" * 43,
        bridge_mode="fake",
        local_image_enabled=False,
        asset_staging_dir=str(tmp_path / "unused-assets"),
    )
    monkeypatch.setattr(
        GatewaySettings,
        "from_env",
        classmethod(lambda cls, environ=None: gateway_settings),
    )
    script_state = str(tmp_path / "gate-script-state.json")
    backend = await DirectGatewayBackend.from_env(
        NODE_ID,
        runtime_profile="gate",
        gate_script_state_path=script_state,
    )
    await backend.bind_device(DEVICE_ID)
    created = await backend.dispatch(
        "threads.create",
        ThreadsCreateParams(title="Gate durable thread"),
    )
    thread_id = created["id"]
    await backend.dispatch(
        "turns.start",
        TurnsStartParams.model_validate(
            {
                "threadId": thread_id,
                "clientMessageId": "client-message-0001",
                "input": [{"type": "text", "text": "MARKER-GAMMA"}],
            }
        ),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first_read = await backend.dispatch(
        "threads.read",
        ThreadsReadParams.model_validate({"threadId": thread_id}),
    )
    assert GATE_REPLY_PREFIX in json.dumps(first_read)
    assert "MARKER-GAMMA" in json.dumps(first_read)
    await backend.close()

    restarted = await DirectGatewayBackend.from_env(
        NODE_ID,
        runtime_profile="gate",
        gate_script_state_path=script_state,
    )
    await restarted.bind_device(DEVICE_ID)
    recovered = await restarted.dispatch(
        "threads.read",
        ThreadsReadParams.model_validate({"threadId": thread_id}),
    )
    assert "MARKER-GAMMA" in json.dumps(recovered)
    await restarted.dispatch(
        "turns.start",
        TurnsStartParams.model_validate(
            {
                "threadId": thread_id,
                "clientMessageId": "client-message-0002",
                "input": [{"type": "text", "text": "MARKER-DELTA"}],
            }
        ),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    continued = await restarted.dispatch(
        "threads.read",
        ThreadsReadParams.model_validate({"threadId": thread_id}),
    )
    continued_json = json.dumps(continued)
    assert "Remembered: MARKER-GAMMA" in continued_json
    assert "Current: MARKER-DELTA" in continued_json
    await restarted.close()


@pytest.mark.asyncio
async def test_gate_main_skips_mcp_phonebridge_and_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cheby_connector import main as connector_main

    configured = _settings(
        tmp_path,
        relay_url=GATE_PRIVATE_RELAY_URL,
        runtime_profile="gate",
        gate_script_state_path=str(tmp_path / "gate-script-state.json"),
    )
    monkeypatch.setattr(
        connector_main.ConnectorSettings,
        "from_env",
        classmethod(lambda cls, environ=None: configured),
    )

    def forbidden() -> None:
        raise AssertionError("production setup ran in Gate profile")

    async def forbidden_async():
        raise AssertionError("PhoneBridge ran in Gate profile")

    monkeypatch.setattr(connector_main, "ensure_codex_mcp_config", forbidden)
    monkeypatch.setattr(connector_main, "validate_codex_mcp_registration", forbidden)
    monkeypatch.setattr(connector_main, "start_phonebridge", forbidden_async)

    class Backend:
        device_id = None
        stream_id = None

        async def bind_device(self, device_id: str) -> None:
            raise AssertionError("unexpected persisted Gate device")

        async def dispatch(self, operation: str, params: object) -> dict:
            return {}

        def replay_events(self, after_seq: int) -> list:
            return []

        async def close(self) -> None:
            return None

    backend = Backend()
    captured = {}

    async def backend_from_env(
        connector_name: str,
        *,
        runtime_profile: str,
        gate_script_state_path: str,
    ):
        captured.update(
            connector_name=connector_name,
            runtime_profile=runtime_profile,
            gate_script_state_path=gate_script_state_path,
        )
        return backend

    monkeypatch.setattr(
        connector_main.DirectGatewayBackend,
        "from_env",
        backend_from_env,
    )

    class Relay:
        def __init__(self, settings, engine) -> None:
            pass

        async def run_forever(self) -> None:
            return None

    async def one_health_tick(path: str, interval: float) -> None:
        return None

    monkeypatch.setattr(connector_main, "RelayConnector", Relay)
    monkeypatch.setattr(connector_main, "run_health_watchdog", one_health_tick)

    await connector_main._run()

    assert captured == {
        "connector_name": NODE_ID,
        "runtime_profile": "gate",
        "gate_script_state_path": str(tmp_path / "gate-script-state.json"),
    }


def test_gate_state_rejects_symlink_and_corrupt_content(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "gate-link.json"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="cannot be read"):
        ScriptedGateCodexBridge(str(link))

    corrupt = tmp_path / "gate-corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    corrupt.chmod(0o600)
    with pytest.raises(RuntimeError, match="invalid"):
        ScriptedGateCodexBridge(str(corrupt))


def _installer_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "turkey"
        / "install_node_bootstrap.py"
    )
    spec = importlib.util.spec_from_file_location("install_node_bootstrap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_private_override_is_allowlisted_and_never_printed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = _installer_module()
    monkeypatch.setattr(installer.os, "fchown", lambda descriptor, uid, gid: None)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    bootstrap = private / "node-bootstrap.json"
    token = "rly1_" + "T" * 22 + "." + "S" * 43
    bootstrap.write_text(
        json.dumps(
            {
                "v": 1,
                "relayOrigin": "https://public.example:27462",
                "assistantId": "asst_" + "A" * 22,
                "nodeId": NODE_ID,
                "nodeToken": token,
            }
        ),
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    args = argparse.Namespace(
        bootstrap=str(bootstrap),
        env_output=str(private / "node.env"),
        token_output=str(private / "node_token"),
        gateway_secret_output=str(private / "gateway_secret"),
        relay_url=GATE_PRIVATE_RELAY_URL,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )

    installer.install(args)

    output = capsys.readouterr().out
    assert token not in output
    assert GATE_PRIVATE_RELAY_URL not in output
    assert (
        f"CHEBY_CONNECTOR_RELAY_URL={GATE_PRIVATE_RELAY_URL}"
        in (private / "node.env").read_text(encoding="utf-8")
    )
    with pytest.raises(ValueError, match="approved fixed endpoint"):
        installer.private_relay_node_url(
            "ws://172.31.62.4:8080/relay/v1/node"
        )
