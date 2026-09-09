from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_network


def _positive_int_environment(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{name} must be a positive decimal integer")
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive decimal integer")
    return value


@dataclass(frozen=True)
class RelaySettings:
    db_path: str
    allowed_hosts: tuple[str, ...] = ("localhost", "testserver")
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    proof_clock_skew_seconds: int = 300
    proof_nonce_retention_seconds: int = 900
    max_frame_bytes: int = 12 * 1024 * 1024
    max_command_bytes: int = 256 * 1024
    max_asset_bytes: int = 8 * 1024 * 1024
    max_json_depth: int = 32
    max_json_nodes: int = 20_000
    max_connections_total: int = 200
    max_connections_per_principal: int = 1
    connection_rate_limit: int = 30
    connection_rate_window_seconds: int = 60
    # One user action can fan out into several structured Gateway responses and
    # events. Keep the boundary finite, but leave enough headroom for a short
    # multi-Thread burst without forcing both dedicated peers into reconnect
    # storms.
    message_rate_limit: int = 600
    message_rate_window_seconds: int = 60
    control_frame_rate_limit: int = 600
    control_frame_rate_window_seconds: int = 60
    max_pending_messages: int = 128
    max_pending_bytes: int = 64 * 1024 * 1024
    outbound_queue_size: int = 128
    pairing_body_limit_bytes: int = 16 * 1024
    refresh_body_limit_bytes: int = 4 * 1024
    pairing_rate_limit: int = 8
    refresh_rate_limit: int = 20
    public_rate_window_seconds: int = 60
    rate_limiter_max_keys: int = 4096
    gc_interval_seconds: float = 60.0
    gc_batch_size: int = 200
    acked_payload_retention_seconds: int = 3_600
    expired_payload_retention_seconds: int = 60
    idempotency_retention_seconds: int = 604_800
    request_retention_seconds: int = 2_592_000
    credential_retention_seconds: int = 604_800
    subscription_handoff_grace_seconds: int = 300
    trusted_proxy_cidrs: tuple[str, ...] = ()
    client_ip_header: str = ""

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ValueError("db_path is required")
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        positive = {
            "access_token_ttl_seconds": self.access_token_ttl_seconds,
            "refresh_token_ttl_seconds": self.refresh_token_ttl_seconds,
            "proof_clock_skew_seconds": self.proof_clock_skew_seconds,
            "proof_nonce_retention_seconds": self.proof_nonce_retention_seconds,
            "max_frame_bytes": self.max_frame_bytes,
            "max_command_bytes": self.max_command_bytes,
            "max_asset_bytes": self.max_asset_bytes,
            "max_json_depth": self.max_json_depth,
            "max_json_nodes": self.max_json_nodes,
            "max_connections_total": self.max_connections_total,
            "max_connections_per_principal": self.max_connections_per_principal,
            "connection_rate_limit": self.connection_rate_limit,
            "connection_rate_window_seconds": self.connection_rate_window_seconds,
            "message_rate_limit": self.message_rate_limit,
            "message_rate_window_seconds": self.message_rate_window_seconds,
            "control_frame_rate_limit": self.control_frame_rate_limit,
            "control_frame_rate_window_seconds": self.control_frame_rate_window_seconds,
            "max_pending_messages": self.max_pending_messages,
            "max_pending_bytes": self.max_pending_bytes,
            "outbound_queue_size": self.outbound_queue_size,
            "pairing_body_limit_bytes": self.pairing_body_limit_bytes,
            "refresh_body_limit_bytes": self.refresh_body_limit_bytes,
            "pairing_rate_limit": self.pairing_rate_limit,
            "refresh_rate_limit": self.refresh_rate_limit,
            "public_rate_window_seconds": self.public_rate_window_seconds,
            "rate_limiter_max_keys": self.rate_limiter_max_keys,
            "gc_batch_size": self.gc_batch_size,
            "acked_payload_retention_seconds": self.acked_payload_retention_seconds,
            "expired_payload_retention_seconds": self.expired_payload_retention_seconds,
            "idempotency_retention_seconds": self.idempotency_retention_seconds,
            "request_retention_seconds": self.request_retention_seconds,
            "credential_retention_seconds": self.credential_retention_seconds,
            "subscription_handoff_grace_seconds": self.subscription_handoff_grace_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.gc_interval_seconds <= 0:
            raise ValueError("gc_interval_seconds must be positive")
        if self.gc_batch_size > 10_000:
            raise ValueError("gc_batch_size must not exceed 10000")
        if self.expired_payload_retention_seconds > self.idempotency_retention_seconds:
            raise ValueError("expired payload retention must not exceed idempotency retention")
        if self.acked_payload_retention_seconds > self.idempotency_retention_seconds:
            raise ValueError("acked payload retention must not exceed idempotency retention")
        if bool(self.trusted_proxy_cidrs) != bool(self.client_ip_header):
            raise ValueError(
                "trusted_proxy_cidrs and client_ip_header must be configured together"
            )
        if self.client_ip_header and self.client_ip_header not in {
            "x-relay-client-ip",
            "cf-connecting-ip",
        }:
            raise ValueError(
                "client_ip_header must be x-relay-client-ip or cf-connecting-ip"
            )
        for value in self.trusted_proxy_cidrs:
            try:
                network = ip_network(value, strict=False)
            except ValueError as exc:
                raise ValueError("trusted_proxy_cidrs contains an invalid CIDR") from exc
            if network.prefixlen == 0:
                raise ValueError("trusted_proxy_cidrs must not trust the open Internet")
        if self.proof_clock_skew_seconds > 900:
            raise ValueError("proof clock skew must not exceed 900 seconds")
        if self.proof_nonce_retention_seconds < 2 * self.proof_clock_skew_seconds:
            raise ValueError("proof nonce retention must cover the full proof window")
        if self.max_asset_bytes > 8 * 1024 * 1024:
            raise ValueError("asset limit must not exceed 8 MiB")
        if self.max_command_bytes > self.max_frame_bytes:
            raise ValueError("command limit must not exceed frame limit")
        if self.max_pending_bytes < self.max_frame_bytes:
            raise ValueError("pending byte limit must hold at least one frame")

    @classmethod
    def from_env(cls) -> "RelaySettings":
        hosts = tuple(
            value.strip()
            for value in os.environ.get("CHEBY_RELAY_ALLOWED_HOSTS", "localhost").split(",")
            if value.strip()
        )
        return cls(
            db_path=os.environ.get("CHEBY_RELAY_DB_PATH", "/data/relay.sqlite3"),
            allowed_hosts=hosts,
            trusted_proxy_cidrs=tuple(
                value.strip()
                for value in os.environ.get(
                    "CHEBY_RELAY_TRUSTED_PROXY_CIDRS", ""
                ).split(",")
                if value.strip()
            ),
            client_ip_header=os.environ.get(
                "CHEBY_RELAY_CLIENT_IP_HEADER", ""
            ).strip().lower(),
            acked_payload_retention_seconds=_positive_int_environment(
                "CHEBY_RELAY_ACKED_PAYLOAD_RETENTION_SECONDS",
                cls.acked_payload_retention_seconds,
            ),
        )
