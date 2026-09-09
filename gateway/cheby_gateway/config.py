from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from ipaddress import ip_network
from typing import Optional, Tuple


@dataclass(frozen=True)
class GatewaySettings:
    db_path: str
    pairing_secret: str
    bridge_mode: str
    codex_command: Tuple[str, ...] = (
        "python",
        "/app/deploy/codex_sandbox_launcher.py",
        "codex",
        "--ask-for-approval",
        "never",
        "app-server",
        "--disable",
        "code_mode_host",
        "--disable",
        "code_mode",
        "--listen",
        "stdio://",
    )
    codex_cwd: str = "/workspace"
    token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    event_retention: int = 10_000
    request_timeout_seconds: float = 30.0
    approval_sweep_interval_seconds: float = 1.0
    protocol_frame_limit_bytes: int = 64 * 1024 * 1024
    trusted_proxy_cidrs: Tuple[str, ...] = ()
    pairing_source_limit: int = 8
    pairing_global_limit: int = 40
    pairing_window_seconds: int = 60
    pairing_backoff_base_seconds: int = 2
    pairing_backoff_max_seconds: int = 60
    proof_clock_skew_seconds: int = 300
    refresh_source_limit: int = 20
    refresh_global_limit: int = 100
    refresh_device_limit: int = 10
    refresh_window_seconds: int = 60
    send_device_limit: int = 30
    send_window_seconds: int = 60
    approval_device_limit: int = 20
    approval_window_seconds: int = 60
    websocket_connect_global_limit: int = 200
    websocket_connect_source_limit: int = 30
    websocket_connect_device_limit: int = 20
    websocket_connect_window_seconds: int = 60
    websocket_message_device_limit: int = 120
    websocket_message_window_seconds: int = 60
    websocket_inbound_max_bytes: int = 16 * 1024
    websocket_event_max_bytes: int = 512 * 1024
    request_body_limit_bytes: int = 64 * 1024
    pairing_body_limit_bytes: int = 16 * 1024
    refresh_body_limit_bytes: int = 4 * 1024
    turn_body_limit_bytes: int = 256 * 1024
    thread_detail_default_messages: int = 20
    thread_detail_max_messages: int = 100
    thread_detail_max_bytes: int = 4 * 1024 * 1024
    delete_impact_ttl_seconds: int = 60
    provisional_thread_ttl_seconds: int = 3_600
    local_image_enabled: bool = False
    asset_staging_dir: str = "/asset-staging"
    codex_expected_version: str = "0.144.6"
    asset_sweep_interval_seconds: float = 60.0
    image_upload_device_limit: int = 10
    image_upload_window_seconds: int = 60

    def __post_init__(self) -> None:
        bounded = {
            "pairing_source_limit": self.pairing_source_limit,
            "pairing_global_limit": self.pairing_global_limit,
            "pairing_window_seconds": self.pairing_window_seconds,
            "pairing_backoff_base_seconds": self.pairing_backoff_base_seconds,
            "pairing_backoff_max_seconds": self.pairing_backoff_max_seconds,
            "proof_clock_skew_seconds": self.proof_clock_skew_seconds,
            "refresh_source_limit": self.refresh_source_limit,
            "refresh_global_limit": self.refresh_global_limit,
            "refresh_device_limit": self.refresh_device_limit,
            "refresh_window_seconds": self.refresh_window_seconds,
            "send_device_limit": self.send_device_limit,
            "send_window_seconds": self.send_window_seconds,
            "approval_device_limit": self.approval_device_limit,
            "approval_window_seconds": self.approval_window_seconds,
            "websocket_connect_global_limit": self.websocket_connect_global_limit,
            "websocket_connect_source_limit": self.websocket_connect_source_limit,
            "websocket_connect_device_limit": self.websocket_connect_device_limit,
            "websocket_connect_window_seconds": self.websocket_connect_window_seconds,
            "websocket_message_device_limit": self.websocket_message_device_limit,
            "websocket_message_window_seconds": self.websocket_message_window_seconds,
            "thread_detail_default_messages": self.thread_detail_default_messages,
            "thread_detail_max_messages": self.thread_detail_max_messages,
            "delete_impact_ttl_seconds": self.delete_impact_ttl_seconds,
            "provisional_thread_ttl_seconds": self.provisional_thread_ttl_seconds,
            "image_upload_device_limit": self.image_upload_device_limit,
            "image_upload_window_seconds": self.image_upload_window_seconds,
        }
        for name, value in bounded.items():
            if not 1 <= value <= 1_000_000:
                raise ValueError("%s must be between 1 and 1000000" % name)
        if self.pairing_backoff_base_seconds > self.pairing_backoff_max_seconds:
            raise ValueError(
                "pairing_backoff_base_seconds must not exceed "
                "pairing_backoff_max_seconds"
            )
        if self.delete_impact_ttl_seconds > 300:
            raise ValueError("delete_impact_ttl_seconds must not exceed 300")
        if self.provisional_thread_ttl_seconds > 86_400:
            raise ValueError("provisional_thread_ttl_seconds must not exceed 86400")
        if self.proof_clock_skew_seconds > 900:
            raise ValueError("proof_clock_skew_seconds must not exceed 900")
        if self.thread_detail_default_messages > self.thread_detail_max_messages:
            raise ValueError(
                "thread_detail_default_messages must not exceed thread_detail_max_messages"
            )
        if self.thread_detail_max_messages > 100:
            raise ValueError("thread_detail_max_messages must not exceed 100")
        byte_limits = {
            "websocket_event_max_bytes": self.websocket_event_max_bytes,
            "websocket_inbound_max_bytes": self.websocket_inbound_max_bytes,
            "request_body_limit_bytes": self.request_body_limit_bytes,
            "pairing_body_limit_bytes": self.pairing_body_limit_bytes,
            "refresh_body_limit_bytes": self.refresh_body_limit_bytes,
            "turn_body_limit_bytes": self.turn_body_limit_bytes,
            "thread_detail_max_bytes": self.thread_detail_max_bytes,
        }
        for name, value in byte_limits.items():
            if not 1024 <= value <= 64 * 1024 * 1024:
                raise ValueError("%s must be between 1024 and 67108864" % name)
        if self.websocket_event_max_bytes > 512 * 1024:
            raise ValueError("websocket_event_max_bytes must not exceed 524288")
        if self.websocket_inbound_max_bytes > 16 * 1024:
            raise ValueError("websocket_inbound_max_bytes must not exceed 16384")
        if self.request_body_limit_bytes > 64 * 1024:
            raise ValueError("request_body_limit_bytes must not exceed 65536")
        if self.pairing_body_limit_bytes > 16 * 1024:
            raise ValueError("pairing_body_limit_bytes must not exceed 16384")
        if self.refresh_body_limit_bytes > 4 * 1024:
            raise ValueError("refresh_body_limit_bytes must not exceed 4096")
        if self.turn_body_limit_bytes > 256 * 1024:
            raise ValueError("turn_body_limit_bytes must not exceed 262144")
        if self.thread_detail_max_bytes > 4 * 1024 * 1024:
            raise ValueError("thread_detail_max_bytes must not exceed 4194304")
        if not os.path.isabs(self.asset_staging_dir):
            raise ValueError("asset_staging_dir must be absolute")
        if self.asset_sweep_interval_seconds <= 0:
            raise ValueError("asset_sweep_interval_seconds must be positive")
        for value in self.trusted_proxy_cidrs:
            try:
                network = ip_network(value, strict=False)
            except ValueError as exc:
                raise ValueError("CHEBY_TRUSTED_PROXY_CIDRS contains invalid CIDR") from exc
            if network.prefixlen == 0:
                raise ValueError(
                    "CHEBY_TRUSTED_PROXY_CIDRS must not trust the open Internet"
                )

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "GatewaySettings":
        values = os.environ if environ is None else environ
        pairing_secret = values.get("CHEBY_PAIRING_SECRET", "")
        pairing_secret_file = values.get("CHEBY_PAIRING_SECRET_FILE", "")
        if not pairing_secret and pairing_secret_file:
            try:
                with open(pairing_secret_file, "r", encoding="utf-8") as handle:
                    pairing_secret = handle.read().strip()
            except OSError as exc:
                raise RuntimeError("CHEBY_PAIRING_SECRET_FILE cannot be read") from exc
        if not pairing_secret:
            raise RuntimeError("CHEBY_PAIRING_SECRET is required")

        command = tuple(
            shlex.split(
                values.get(
                    "CHEBY_CODEX_COMMAND",
                    "python /app/deploy/codex_sandbox_launcher.py codex --ask-for-approval never app-server --disable code_mode_host --disable code_mode --listen stdio://",
                )
            )
        )
        if not command:
            raise RuntimeError("CHEBY_CODEX_COMMAND must not be empty")

        raw_bridge_mode = values.get("CHEBY_BRIDGE_MODE", "").strip()
        if not raw_bridge_mode:
            raise RuntimeError("CHEBY_BRIDGE_MODE must be explicitly configured")
        bridge_mode = raw_bridge_mode.lower()
        if bridge_mode not in {"fake", "stdio"}:
            raise RuntimeError("CHEBY_BRIDGE_MODE must be fake or stdio")

        trusted_proxy_cidrs = tuple(
            part.strip()
            for part in values.get("CHEBY_TRUSTED_PROXY_CIDRS", "").split(",")
            if part.strip()
        )

        return cls(
            db_path=values.get("CHEBY_GATEWAY_DB", "/data/gateway.sqlite3"),
            pairing_secret=pairing_secret,
            bridge_mode=bridge_mode,
            codex_command=command,
            codex_cwd=values.get("CHEBY_CODEX_CWD", "/workspace"),
            token_ttl_seconds=int(values.get("CHEBY_TOKEN_TTL_SECONDS", "900")),
            refresh_token_ttl_seconds=int(
                values.get("CHEBY_REFRESH_TOKEN_TTL_SECONDS", "2592000")
            ),
            event_retention=int(values.get("CHEBY_EVENT_RETENTION", "10000")),
            request_timeout_seconds=float(
                values.get("CHEBY_CODEX_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            approval_sweep_interval_seconds=float(
                values.get("CHEBY_APPROVAL_SWEEP_INTERVAL_SECONDS", "1")
            ),
            protocol_frame_limit_bytes=max(
                1024,
                int(
                    values.get(
                        "CHEBY_CODEX_FRAME_LIMIT_BYTES",
                        str(64 * 1024 * 1024),
                    )
                ),
            ),
            trusted_proxy_cidrs=trusted_proxy_cidrs,
            pairing_source_limit=int(
                values.get("CHEBY_PAIRING_SOURCE_LIMIT", "8")
            ),
            pairing_global_limit=int(
                values.get("CHEBY_PAIRING_GLOBAL_LIMIT", "40")
            ),
            pairing_window_seconds=int(
                values.get("CHEBY_PAIRING_WINDOW_SECONDS", "60")
            ),
            pairing_backoff_base_seconds=int(
                values.get("CHEBY_PAIRING_BACKOFF_BASE_SECONDS", "2")
            ),
            pairing_backoff_max_seconds=int(
                values.get("CHEBY_PAIRING_BACKOFF_MAX_SECONDS", "60")
            ),
            proof_clock_skew_seconds=int(
                values.get("CHEBY_PROOF_CLOCK_SKEW_SECONDS", "300")
            ),
            refresh_source_limit=int(
                values.get("CHEBY_REFRESH_SOURCE_LIMIT", "20")
            ),
            refresh_global_limit=int(
                values.get("CHEBY_REFRESH_GLOBAL_LIMIT", "100")
            ),
            refresh_device_limit=int(
                values.get("CHEBY_REFRESH_DEVICE_LIMIT", "10")
            ),
            refresh_window_seconds=int(
                values.get("CHEBY_REFRESH_WINDOW_SECONDS", "60")
            ),
            send_device_limit=int(values.get("CHEBY_SEND_DEVICE_LIMIT", "30")),
            send_window_seconds=int(values.get("CHEBY_SEND_WINDOW_SECONDS", "60")),
            approval_device_limit=int(
                values.get("CHEBY_APPROVAL_DEVICE_LIMIT", "20")
            ),
            approval_window_seconds=int(
                values.get("CHEBY_APPROVAL_WINDOW_SECONDS", "60")
            ),
            websocket_connect_global_limit=int(
                values.get("CHEBY_WEBSOCKET_CONNECT_GLOBAL_LIMIT", "200")
            ),
            websocket_connect_source_limit=int(
                values.get("CHEBY_WEBSOCKET_CONNECT_SOURCE_LIMIT", "30")
            ),
            websocket_connect_device_limit=int(
                values.get("CHEBY_WEBSOCKET_CONNECT_DEVICE_LIMIT", "20")
            ),
            websocket_connect_window_seconds=int(
                values.get("CHEBY_WEBSOCKET_CONNECT_WINDOW_SECONDS", "60")
            ),
            websocket_message_device_limit=int(
                values.get("CHEBY_WEBSOCKET_MESSAGE_DEVICE_LIMIT", "120")
            ),
            websocket_message_window_seconds=int(
                values.get("CHEBY_WEBSOCKET_MESSAGE_WINDOW_SECONDS", "60")
            ),
            websocket_inbound_max_bytes=int(
                values.get("CHEBY_WEBSOCKET_INBOUND_MAX_BYTES", str(16 * 1024))
            ),
            websocket_event_max_bytes=int(
                values.get("CHEBY_WEBSOCKET_EVENT_MAX_BYTES", str(512 * 1024))
            ),
            request_body_limit_bytes=int(
                values.get("CHEBY_REQUEST_BODY_LIMIT_BYTES", str(64 * 1024))
            ),
            pairing_body_limit_bytes=int(
                values.get("CHEBY_PAIRING_BODY_LIMIT_BYTES", str(16 * 1024))
            ),
            refresh_body_limit_bytes=int(
                values.get("CHEBY_REFRESH_BODY_LIMIT_BYTES", str(4 * 1024))
            ),
            turn_body_limit_bytes=int(
                values.get("CHEBY_TURN_BODY_LIMIT_BYTES", str(256 * 1024))
            ),
            thread_detail_default_messages=int(
                values.get("CHEBY_THREAD_DETAIL_DEFAULT_MESSAGES", "20")
            ),
            thread_detail_max_messages=int(
                values.get("CHEBY_THREAD_DETAIL_MAX_MESSAGES", "100")
            ),
            thread_detail_max_bytes=int(
                values.get("CHEBY_THREAD_DETAIL_MAX_BYTES", str(4 * 1024 * 1024))
            ),
            delete_impact_ttl_seconds=int(
                values.get("CHEBY_DELETE_IMPACT_TTL_SECONDS", "60")
            ),
            provisional_thread_ttl_seconds=int(
                values.get("CHEBY_PROVISIONAL_THREAD_TTL_SECONDS", "3600")
            ),
            local_image_enabled=values.get(
                "CHEBY_LOCAL_IMAGE_ENABLED", "false"
            ).strip().lower() in {"1", "true", "yes", "on"},
            asset_staging_dir=values.get(
                "CHEBY_ASSET_STAGING_DIR", "/asset-staging"
            ),
            codex_expected_version=values.get(
                "CHEBY_CODEX_EXPECTED_VERSION", "0.144.6"
            ),
            asset_sweep_interval_seconds=float(
                values.get("CHEBY_ASSET_SWEEP_INTERVAL_SECONDS", "60")
            ),
            image_upload_device_limit=int(
                values.get("CHEBY_IMAGE_UPLOAD_DEVICE_LIMIT", "10")
            ),
            image_upload_window_seconds=int(
                values.get("CHEBY_IMAGE_UPLOAD_WINDOW_SECONDS", "60")
            ),
        )
