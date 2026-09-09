from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from fastapi.testclient import TestClient

from cheby_relay.app import create_app
from cheby_relay.config import RelaySettings
from cheby_relay.pop import P256_ORDER, canonical_request
from cheby_relay.store import BootstrapBundle, RelayStore


@dataclass
class EnrolledDevice:
    assistant_id: str
    device_id: str
    access_token: str
    refresh_token: str
    key: ec.EllipticCurvePrivateKey


@dataclass
class RelayHarness:
    settings: RelaySettings
    store: RelayStore
    client: TestClient

    def bootstrap(self) -> BootstrapBundle:
        return self.store.bootstrap_assistant()

    def enroll(
        self,
        bundle: BootstrapBundle,
        *,
        key: ec.EllipticCurvePrivateKey | None = None,
    ) -> EnrolledDevice:
        private_key = key or ec.generate_private_key(ec.SECP256R1())
        body = pairing_body(bundle, private_key)
        response = self.client.post(
            "/relay/v1/pairings/exchange",
            content=body,
            headers={
                "content-type": "application/json",
                **proof_headers(
                    private_key,
                    method="POST",
                    target="/relay/v1/pairings/exchange",
                    body=body,
                ),
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        return EnrolledDevice(
            assistant_id=data["assistantId"],
            device_id=data["deviceId"],
            access_token=data["accessToken"],
            refresh_token=data["refreshToken"],
            key=private_key,
        )


@pytest.fixture
def harness(tmp_path) -> RelayHarness:
    settings = RelaySettings(db_path=str(tmp_path / "relay.sqlite3"))
    store = RelayStore(settings)
    app = create_app(settings, store=store)
    with TestClient(app) as client:
        yield RelayHarness(settings, store, client)
    store.close()


def pairing_body(
    bundle: BootstrapBundle, key: ec.EllipticCurvePrivateKey
) -> bytes:
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return json.dumps(
        {
            "assistantId": bundle.assistant_id,
            "pairingSecret": bundle.pairing_secret,
            "deviceName": "JUY-AL00",
            "devicePublicKey": base64.b64encode(spki).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def proof_headers(
    key: ec.EllipticCurvePrivateKey,
    *,
    method: str,
    target: str,
    body: bytes = b"",
    bearer: str = "",
    nonce: bytes | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    nonce_value = base64.b64encode(nonce or secrets.token_bytes(16)).decode("ascii")
    timestamp_value = str(int(time.time()) if timestamp is None else timestamp)
    canonical = canonical_request(
        method=method,
        raw_target=target.encode("ascii"),
        timestamp=timestamp_value,
        nonce=nonce_value,
        body=body,
        bearer_token=bearer,
    )
    signature = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(signature)
    if s_value > P256_ORDER // 2:
        s_value = P256_ORDER - s_value
    signature = encode_dss_signature(r_value, s_value)
    return {
        "x-cheby-signature-version": "1",
        "x-cheby-timestamp": timestamp_value,
        "x-cheby-nonce": nonce_value,
        "x-cheby-signature": base64.b64encode(signature).decode("ascii"),
    }


def device_ws_headers(device: EnrolledDevice, *, target: str = "/relay/v1/device") -> dict[str, str]:
    return {
        "authorization": "Bearer " + device.access_token,
        **proof_headers(
            device.key,
            method="GET",
            target=target,
            bearer=device.access_token,
        ),
    }


def node_ws_headers(bundle: BootstrapBundle) -> dict[str, str]:
    return {"authorization": "Bearer " + bundle.node_token}


def random_id(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(16)


def command(
    device: EnrolledDevice,
    operation: str,
    params: dict,
    *,
    message_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    return {
        "v": 1,
        "type": "message",
        "messageId": message_id or random_id("msg_"),
        "payload": {
            "kind": "command",
            "requestId": request_id or random_id("req_"),
            "deviceId": device.device_id,
            "operation": operation,
            "params": params,
        },
    }


def response_for(command_frame: dict, *, message_id: str | None = None) -> dict:
    payload = command_frame["payload"]
    return {
        "v": 1,
        "type": "message",
        "messageId": message_id or random_id("msg_"),
        "payload": {
            "kind": "response",
            "requestId": payload["requestId"],
            "deviceId": payload["deviceId"],
            "operation": payload["operation"],
            "ok": True,
            "result": {},
        },
    }


def receive_type(socket, expected: str, *, maximum: int = 10) -> dict:
    for _ in range(maximum):
        frame = socket.receive_json()
        if frame.get("type") == expected:
            return frame
    raise AssertionError(f"did not receive frame type {expected}")


def receive_disconnect(socket, *, maximum: int = 10) -> int:
    from starlette.websockets import WebSocketDisconnect

    for _ in range(maximum):
        try:
            socket.receive_json()
        except WebSocketDisconnect as exc:
            return exc.code
    raise AssertionError("socket did not close")
