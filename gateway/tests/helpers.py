from __future__ import annotations

import base64
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from cheby_gateway.bridge import FakeCodexBridge
from cheby_gateway.config import GatewaySettings
from cheby_gateway.pop import P256_ORDER, PROOF_VERSION, canonical_request
from cheby_gateway.service import GatewayService
from cheby_gateway.store import GatewayStore


@dataclass
class TestContext:
    tempdir: tempfile.TemporaryDirectory
    settings: GatewaySettings
    store: GatewayStore
    bridge: FakeCodexBridge
    service: GatewayService
    device_private_key: ec.EllipticCurvePrivateKey
    device_public_key: str

    def close(self) -> None:
        self.store.close()
        self.tempdir.cleanup()


def make_context(
    event_retention: int = 100,
    token_ttl_seconds: int = 900,
    refresh_token_ttl_seconds: int = 2_592_000,
    approval_sweep_interval_seconds: float = 3600.0,
    **settings_overrides,
) -> TestContext:
    tempdir = tempfile.TemporaryDirectory(prefix="cheby-gateway-test-")
    db_path = str(Path(tempdir.name) / "gateway.sqlite3")
    settings_values = dict(
        db_path=db_path,
        pairing_secret="test-pairing-secret",
        bridge_mode="fake",
        codex_cwd="/workspace",
        token_ttl_seconds=token_ttl_seconds,
        refresh_token_ttl_seconds=refresh_token_ttl_seconds,
        event_retention=event_retention,
        approval_sweep_interval_seconds=approval_sweep_interval_seconds,
    )
    settings_values.update(settings_overrides)
    if settings_values.get("local_image_enabled") and "asset_staging_dir" not in settings_overrides:
        asset_dir = Path(tempdir.name) / "asset-staging"
        asset_dir.mkdir(mode=0o700)
        asset_dir.chmod(0o700)
        settings_values["asset_staging_dir"] = str(asset_dir)
    settings = GatewaySettings(**settings_values)
    store = GatewayStore(db_path, event_retention=event_retention)
    bridge = FakeCodexBridge(auto_events=False)
    service = GatewayService(settings, store, bridge)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return TestContext(
        tempdir,
        settings,
        store,
        bridge,
        service,
        private_key,
        base64.b64encode(public_der).decode("ascii"),
    )


def pair(context: TestContext):
    response = context.service.exchange_pairing(
        pairing_secret="test-pairing-secret",
        device_name="JUY-AL00",
        device_public_key=context.device_public_key,
    )
    return response, context.service.authenticate(response.access_token)


def proof_headers(
    private_key: ec.EllipticCurvePrivateKey,
    method: str,
    raw_target: bytes,
    body: bytes = b"",
    bearer_token: str = "",
    *,
    timestamp: Optional[int] = None,
    nonce: Optional[bytes] = None,
) -> dict[str, str]:
    signed_at = int(time.time()) if timestamp is None else int(timestamp)
    nonce_bytes = os.urandom(24) if nonce is None else nonce
    nonce_value = base64.b64encode(nonce_bytes).decode("ascii")
    canonical = canonical_request(
        method=method,
        raw_target=raw_target,
        timestamp=str(signed_at),
        nonce=nonce_value,
        body=body,
        bearer_token=bearer_token,
    )
    signature = private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(signature)
    if s_value > P256_ORDER // 2:
        s_value = P256_ORDER - s_value
    signature = encode_dss_signature(r_value, s_value)
    return {
        "X-Cheby-Signature-Version": PROOF_VERSION,
        "X-Cheby-Timestamp": str(signed_at),
        "X-Cheby-Nonce": nonce_value,
        "X-Cheby-Signature": base64.b64encode(signature).decode("ascii"),
    }


class DeviceProofAuth(httpx.Auth):
    """Sign the exact bytes encoded by httpx, including raw query ordering."""

    requires_request_body = True

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self.private_key = private_key

    def _sign(self, request: httpx.Request) -> httpx.Request:
        authorization = request.headers.get("authorization", "")
        bearer_token = ""
        if authorization.lower().startswith("bearer "):
            bearer_token = authorization[7:]
        request.headers.update(
            proof_headers(
                self.private_key,
                request.method,
                request.url.raw_path,
                request.content,
                bearer_token,
            )
        )
        return request

    def auth_flow(self, request: httpx.Request):
        yield self._sign(request)

    def __call__(self, request: httpx.Request) -> httpx.Request:
        return self._sign(request)


def install_proof_auth(client, context: TestContext) -> None:
    client.auth = DeviceProofAuth(context.device_private_key)
