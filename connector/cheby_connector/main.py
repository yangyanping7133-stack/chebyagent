from __future__ import annotations

import asyncio
import logging

from .config import ConnectorSettings
from .engine import ConnectorEngine
from .gateway import DirectGatewayBackend
from .health import run_health_watchdog
from .mcp_config import ensure_codex_mcp_config, validate_codex_mcp_registration
from .phonebridge_runtime import (
    start_phonebridge,
    start_phonebridge_broker,
    stop_phonebridge,
    stop_phonebridge_broker,
)
from .relay import RelayConnector
from .state import ConnectorState


async def _run() -> None:
    settings = ConnectorSettings.from_env()
    if settings.runtime_profile == "production":
        ensure_codex_mcp_config()
        validate_codex_mcp_registration()
    phonebridge = None
    phonebridge_broker = None
    engine = None
    tasks: list[asyncio.Task[object]] = []
    try:
        if settings.runtime_profile == "production":
            phonebridge = await start_phonebridge()
            phonebridge_broker = await start_phonebridge_broker()
        state = ConnectorState(settings.state_path)
        gateway = await DirectGatewayBackend.from_env(
            settings.connector_id,
            runtime_profile=settings.runtime_profile,
            gate_script_state_path=settings.gate_script_state_path,
        )
        persisted_device = state.get_meta("bound_device_id")
        if persisted_device is not None:
            await gateway.bind_device(persisted_device)
        engine = ConnectorEngine(settings, state, gateway)
        connector = RelayConnector(settings, engine)
        tasks = [
            asyncio.create_task(
                run_health_watchdog(settings.health_path, settings.health_interval_seconds),
                name="connector-health-watchdog",
            ),
            asyncio.create_task(connector.run_forever(), name="connector-relay"),
        ]
        if phonebridge is not None:
            tasks.append(
                asyncio.create_task(phonebridge.wait(), name="connector-phonebridge")
            )
        done, _ = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stop_phonebridge(phonebridge)
        await stop_phonebridge_broker(phonebridge_broker)
        if engine is not None:
            await engine.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
