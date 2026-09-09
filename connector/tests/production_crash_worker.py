from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from cheby_connector.config import ConnectorSettings
from cheby_connector.engine import ConnectorEngine
from cheby_connector.gateway import DirectGatewayBackend
from cheby_connector.gate_bridge import ScriptedGateCodexBridge
from cheby_connector.state import ConnectorState
from cheby_gateway.config import GatewaySettings
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore
from cheby_relay.config import RelaySettings
from cheby_relay.store import RelayStore


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)
    raise RuntimeError("SIGKILL returned")


async def _run(config: dict[str, Any]) -> None:
    boundary = str(config["boundary"])
    if boundary == "relay_command_committed":
        relay = RelayStore(RelaySettings(db_path=str(config["relayDb"])))
        principal = relay.authenticate(str(config["deviceAccessToken"]), "device")
        relay.enqueue(
            principal=principal,
            recipient_role="node",
            message_id=str(config["messageId"]),
            payload=dict(config["commandPayload"]),
            expires_at=None,
        )
        _kill()
    if boundary == "relay_response_committed":
        relay = RelayStore(RelaySettings(db_path=str(config["relayDb"])))
        principal = relay.authenticate(str(config["nodeToken"]), "node")
        relay.enqueue(
            principal=principal,
            recipient_role="device",
            message_id=str(config["responseMessageId"]),
            payload=dict(config["responsePayload"]),
            expires_at=None,
            node_generation=int(config["nodeGeneration"]),
        )
        _kill()

    gateway_settings = GatewaySettings(
        db_path=str(config["gatewayDb"]),
        pairing_secret=str(config["pairingSecret"]),
        bridge_mode="fake",
        asset_staging_dir=str(config["assetStagingDir"]),
        approval_sweep_interval_seconds=3600,
    )
    store = GatewayStore(gateway_settings.db_path)
    bridge = ScriptedGateCodexBridge(str(config["bridgeState"]))
    service = GatewayService(gateway_settings, store, bridge)
    await service.start()
    backend = DirectGatewayBackend(
        service=service,
        store=store,
        connector_name=str(config["connectorId"]),
        owns_store=True,
    )
    await backend.bind_device(str(config["deviceId"]))
    connector_settings = ConnectorSettings(
        relay_url="wss://relay.example/relay/v1/node",
        connector_id=str(config["connectorId"]),
        relay_token=str(config["relayToken"]),
        state_path=str(config["connectorDb"]),
        health_path=str(config["healthPath"]),
        request_timeout_seconds=2,
        idempotency_gc_interval_seconds=3600,
    )
    state = ConnectorState(connector_settings.state_path)
    engine = ConnectorEngine(connector_settings, state, backend)
    if boundary == "connector_outbox_removed":
        engine.mark_accepted(str(config["responseMessageId"]))
        _kill()
    request_id = str(config["requestId"])
    client_message_id = str(config["clientMessageId"])

    if boundary == "connector_execution_intent":
        original = state.mark_execution_started

        def kill_after_intent(candidate: str, fingerprint: str) -> None:
            original(candidate, fingerprint)
            if candidate == request_id:
                _kill()

        state.mark_execution_started = kill_after_intent  # type: ignore[method-assign]
    elif boundary == "gateway_turn_reservation":
        original = store.reserve_turn

        def kill_after_reservation(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            candidate = args[2] if len(args) >= 3 else kwargs.get("client_message_id")
            if candidate == client_message_id:
                _kill()
            return result

        store.reserve_turn = kill_after_reservation  # type: ignore[method-assign]
    elif boundary == "scripted_codex_accepted":
        original = bridge.start_turn

        async def kill_after_codex(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            candidate = args[1] if len(args) >= 2 else kwargs.get("client_message_id")
            if candidate == client_message_id:
                _kill()
            return result

        bridge.start_turn = kill_after_codex  # type: ignore[method-assign]
    elif boundary == "gateway_terminal_committed":
        original_start = bridge.start_turn

        async def hold_gateway_response_until_terminal(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            result = await original_start(*args, **kwargs)
            # The scripted bridge has durably accepted the Turn and scheduled
            # its production event path. Hold the turn/start response so the
            # Connector cannot commit its next boundary before terminal state.
            await asyncio.Event().wait()
            return result

        bridge.start_turn = hold_gateway_response_until_terminal  # type: ignore[method-assign]
        original = store.apply_thread_turn_events

        def kill_after_terminal(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            turn_id = kwargs.get("turn_id")
            turn_status = kwargs.get("turn_status")
            if turn_id is not None and turn_status in {
                "completed",
                "failed",
                "interrupted",
            }:
                row = store.turn_by_public_id(str(turn_id))
                if (
                    row is not None
                    and str(row["client_message_id"]) == client_message_id
                    and str(row["delivery_state"]) == "terminal"
                ):
                    _kill()
            return result

        store.apply_thread_turn_events = kill_after_terminal  # type: ignore[method-assign]
    elif boundary == "connector_response_committed":
        original = state.complete_delivery

        def kill_after_response(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if kwargs.get("request_id") == request_id:
                _kill()
            return result

        state.complete_delivery = kill_after_response  # type: ignore[method-assign]
    else:
        raise ValueError("unsupported crash boundary")

    await engine.handle_delivery(str(config["rawDelivery"]))
    if boundary == "gateway_terminal_committed":
        # Scripted Codex emits terminal lifecycle events on a separate task
        # after returning the accepted Turn. Keep this process alive until that
        # task reaches the transaction wrapper above (which SIGKILLs us).
        await asyncio.Event().wait()
    raise RuntimeError("checkpoint did not terminate the worker")


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(512 * 1024 + 1)
        if not raw or len(raw) > 512 * 1024:
            return 64
        config = json.loads(raw)
        if not isinstance(config, dict):
            return 64
        for field in (
            "gatewayDb",
            "bridgeState",
            "assetStagingDir",
            "connectorDb",
            "healthPath",
        ):
            if not Path(str(config.get(field, ""))).is_absolute():
                return 64
        if config.get("boundary") in {
            "relay_command_committed",
            "relay_response_committed",
        } and not Path(str(config.get("relayDb", ""))).is_absolute():
            return 64
        asyncio.run(_run(config))
    except BaseException:
        # Never print configuration, test secrets, or exception paths.
        return 70
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
