from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


PRODUCTION_PRIVATE_RELAY_URL = "ws://172.31.61.3:8080/relay/v1/node"
GATE_PRIVATE_RELAY_URL = "ws://172.31.62.3:8080/relay/v1/node"
LOCAL_RELAY_URLS = frozenset(
    {
        "ws://127.0.0.1:18080/relay/v1/node",
        "ws://[::1]:18080/relay/v1/node",
    }
)


@dataclass(frozen=True)
class ConnectorSettings:
    relay_url: str
    connector_id: str
    relay_token: str
    state_path: str = "/data/connector.sqlite3"
    health_path: str = "/data/connector.health"
    health_interval_seconds: float = 5.0
    idempotency_retention_seconds: int = 30 * 24 * 60 * 60
    idempotency_gc_interval_seconds: float = 60 * 60
    idempotency_gc_batch_size: int = 256
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 35.0
    ping_interval_seconds: float = 20.0
    pong_timeout_seconds: float = 10.0
    reconnect_base_seconds: float = 0.5
    reconnect_max_seconds: float = 30.0
    max_inbound_frame_bytes: int = 12 * 1024 * 1024
    max_outbound_frame_bytes: int = 256 * 1024
    max_concurrency: int = 4
    runtime_profile: str = "production"
    gate_script_state_path: str = "/data/gate-script-state.json"

    def __post_init__(self) -> None:
        relay = urlsplit(self.relay_url)
        secure_transport = relay.scheme == "wss"
        approved_cleartext_transport = self.relay_url in (
            LOCAL_RELAY_URLS
            | {PRODUCTION_PRIVATE_RELAY_URL, GATE_PRIVATE_RELAY_URL}
        )
        if (
            not (secure_transport or approved_cleartext_transport)
            or not relay.hostname
            or relay.username is not None
            or relay.password is not None
            or relay.query
            or relay.fragment
            or relay.path != "/relay/v1/node"
        ):
            raise ValueError(
                "relay_url must be a credential-free WSS endpoint or an explicitly approved private Relay WS endpoint"
            )
        if self.runtime_profile not in {"production", "gate"}:
            raise ValueError("runtime_profile must be production or gate")
        if (
            self.runtime_profile == "gate"
            and self.relay_url != GATE_PRIVATE_RELAY_URL
        ):
            raise ValueError("gate runtime must use the fixed Gate Relay endpoint")
        if (
            self.runtime_profile == "production"
            and self.relay_url == GATE_PRIVATE_RELAY_URL
        ):
            raise ValueError("production runtime cannot use the Gate Relay endpoint")
        connector_suffix = self.connector_id.removeprefix("node_")
        if (
            not self.connector_id.startswith("node_")
            or len(connector_suffix) != 22
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in connector_suffix
            )
        ):
            raise ValueError("connector_id must be a Relay Node id")
        if not self.relay_token or len(self.relay_token.encode("utf-8")) > 4096:
            raise ValueError("relay_token is required")
        if not Path(self.state_path).is_absolute():
            raise ValueError("state_path must be absolute")
        if not Path(self.health_path).is_absolute():
            raise ValueError("health_path must be absolute")
        if not Path(self.gate_script_state_path).is_absolute():
            raise ValueError("gate_script_state_path must be absolute")
        for name in (
            "connect_timeout_seconds",
            "request_timeout_seconds",
            "ping_interval_seconds",
            "pong_timeout_seconds",
            "reconnect_base_seconds",
            "reconnect_max_seconds",
            "idempotency_gc_interval_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.reconnect_base_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect base must not exceed maximum")
        if not 1024 <= self.max_inbound_frame_bytes <= 12 * 1024 * 1024:
            raise ValueError("max inbound frame must be between 1 KiB and 12 MiB")
        if not 1024 <= self.max_outbound_frame_bytes <= 256 * 1024:
            raise ValueError("max outbound frame must be between 1 KiB and 256 KiB")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("Connector concurrency must be between one and eight")
        if not 0 < self.health_interval_seconds <= 10:
            raise ValueError("health interval must be no more than 10 seconds")
        if self.idempotency_retention_seconds < 7 * 24 * 60 * 60:
            raise ValueError("idempotency retention must be at least seven days")
        if not 1 <= self.idempotency_gc_batch_size <= 1000:
            raise ValueError("idempotency GC batch must be between 1 and 1000")

    @classmethod
    def from_env(cls, environ: Optional[dict[str, str]] = None) -> "ConnectorSettings":
        values = os.environ if environ is None else environ
        token = values.get("CHEBY_CONNECTOR_RELAY_TOKEN", "")
        token_file = values.get("CHEBY_CONNECTOR_RELAY_TOKEN_FILE", "")
        if token and token_file:
            raise RuntimeError("configure relay token or relay token file, not both")
        if token_file:
            token_path = Path(token_file)
            token = _read_secret_file(token_path)
        return cls(
            relay_url=values.get("CHEBY_CONNECTOR_RELAY_URL", ""),
            connector_id=values.get("CHEBY_CONNECTOR_ID", ""),
            relay_token=token,
            state_path=values.get(
                "CHEBY_CONNECTOR_STATE", "/data/connector.sqlite3"
            ),
            health_path=values.get(
                "CHEBY_CONNECTOR_HEALTH_PATH", "/data/connector.health"
            ),
            health_interval_seconds=float(
                values.get("CHEBY_CONNECTOR_HEALTH_INTERVAL_SECONDS", "5")
            ),
            idempotency_retention_seconds=int(
                values.get(
                    "CHEBY_CONNECTOR_IDEMPOTENCY_RETENTION_SECONDS",
                    str(30 * 24 * 60 * 60),
                )
            ),
            idempotency_gc_interval_seconds=float(
                values.get("CHEBY_CONNECTOR_IDEMPOTENCY_GC_INTERVAL_SECONDS", "3600")
            ),
            idempotency_gc_batch_size=int(
                values.get("CHEBY_CONNECTOR_IDEMPOTENCY_GC_BATCH_SIZE", "256")
            ),
            connect_timeout_seconds=float(
                values.get("CHEBY_CONNECTOR_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            request_timeout_seconds=float(
                values.get("CHEBY_CONNECTOR_REQUEST_TIMEOUT_SECONDS", "35")
            ),
            ping_interval_seconds=float(
                values.get("CHEBY_CONNECTOR_PING_INTERVAL_SECONDS", "20")
            ),
            pong_timeout_seconds=float(
                values.get("CHEBY_CONNECTOR_PONG_TIMEOUT_SECONDS", "10")
            ),
            reconnect_base_seconds=float(
                values.get("CHEBY_CONNECTOR_RECONNECT_BASE_SECONDS", "0.5")
            ),
            reconnect_max_seconds=float(
                values.get("CHEBY_CONNECTOR_RECONNECT_MAX_SECONDS", "30")
            ),
            max_inbound_frame_bytes=int(
                values.get(
                    "CHEBY_CONNECTOR_MAX_INBOUND_FRAME_BYTES", str(12 * 1024 * 1024)
                )
            ),
            max_outbound_frame_bytes=int(
                values.get("CHEBY_CONNECTOR_MAX_OUTBOUND_FRAME_BYTES", str(256 * 1024))
            ),
            max_concurrency=int(values.get("CHEBY_CONNECTOR_MAX_CONCURRENCY", "4")),
            runtime_profile=values.get(
                "CHEBY_CONNECTOR_RUNTIME_PROFILE", "production"
            ).strip(),
            gate_script_state_path=values.get(
                "CHEBY_GATE_SCRIPT_STATE", "/data/gate-script-state.json"
            ),
        )


def _read_secret_file(path: Path) -> str:
    if not path.is_absolute():
        raise RuntimeError("relay token file must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("relay token file cannot be read") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > 4096
        ):
            raise RuntimeError("relay token file permissions or size are unsafe")
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            raise RuntimeError("relay token file is too large")
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("relay token file is not UTF-8") from exc
    finally:
        os.close(descriptor)
