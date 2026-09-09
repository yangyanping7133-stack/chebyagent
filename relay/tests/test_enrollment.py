from __future__ import annotations

import json
import base64
import time

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cheby_relay.store import PairingDenied, RefreshDenied
from .conftest import pairing_body, proof_headers


def public_spki(key: ec.EllipticCurvePrivateKey) -> str:
    return base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")


def test_pairing_requires_pop_and_lost_response_retry_is_same_key_only(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    body = pairing_body(bundle, key)
    path = "/relay/v1/pairings/exchange"

    denied = harness.client.post(path, content=body, headers={"content-type": "application/json"})
    assert denied.status_code == 401
    assert denied.json()["code"] == "PAIRING_DENIED"

    first = harness.client.post(
        path,
        content=body,
        headers={"content-type": "application/json", **proof_headers(key, method="POST", target=path, body=body)},
    )
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"

    retry = harness.client.post(
        path,
        content=body,
        headers={"content-type": "application/json", **proof_headers(key, method="POST", target=path, body=body)},
    )
    assert retry.status_code == 200
    assert retry.json()["deviceId"] == first.json()["deviceId"]
    assert retry.json()["accessToken"] != first.json()["accessToken"]
    assert harness.store.authenticate(retry.json()["accessToken"], "device")

    other_key = ec.generate_private_key(ec.SECP256R1())
    other_body = pairing_body(bundle, other_key)
    wrong_key = harness.client.post(
        path,
        content=other_body,
        headers={
            "content-type": "application/json",
            **proof_headers(other_key, method="POST", target=path, body=other_body),
        },
    )
    assert wrong_key.status_code == 401
    assert wrong_key.json()["code"] == "PAIRING_DENIED"


def test_standard_access_key_secret_key_remain_bound_to_the_same_phone_key(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    spki = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")

    first = harness.store.exchange_pairing(
        assistant_id=bundle.assistant_id,
        pairing_secret=bundle.pairing_secret,
        device_name="JUY-AL00",
        public_key_spki=spki,
        now=100,
        retry_seconds=1,
    )
    later = harness.store.exchange_pairing(
        assistant_id=bundle.assistant_id,
        pairing_secret=bundle.pairing_secret,
        device_name="JUY-AL00",
        public_key_spki=spki,
        now=10_000,
        retry_seconds=1,
    )
    assert later.device_id == first.device_id
    assert later.access_token != first.access_token

    other_key = ec.generate_private_key(ec.SECP256R1())
    other_spki = base64.b64encode(
        other_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    try:
        harness.store.exchange_pairing(
            assistant_id=bundle.assistant_id,
            pairing_secret=bundle.pairing_secret,
            device_name="other-phone",
            public_key_spki=other_spki,
            now=10_001,
        )
    except Exception as error:
        assert getattr(error, "code", None) == "PAIRING_DENIED"
    else:
        raise AssertionError("AK/SK was accepted for a different phone key")


def test_refresh_lost_response_retry_is_unbounded_and_old_access_fails_closed(harness):
    bundle = harness.bootstrap()
    device = harness.enroll(bundle)
    path = "/relay/v1/auth/refresh"
    body = json.dumps(
        {"deviceId": device.device_id, "refreshToken": device.refresh_token},
        separators=(",", ":"),
    ).encode()

    first = harness.client.post(
        path,
        content=body,
        headers={"content-type": "application/json", **proof_headers(device.key, method="POST", target=path, body=body)},
    )
    assert first.status_code == 200
    first_data = first.json()

    retry = harness.client.post(
        path,
        content=body,
        headers={"content-type": "application/json", **proof_headers(device.key, method="POST", target=path, body=body)},
    )
    assert retry.status_code == 200
    assert retry.json()["accessToken"] != first_data["accessToken"]
    assert first_data["refreshToken"] == device.refresh_token
    assert retry.json()["refreshToken"] == device.refresh_token
    assert retry.json()["refreshExpiresAt"] >= first_data["refreshExpiresAt"]
    assert retry.headers["cache-control"] == "no-store"

    for old_access in (device.access_token, first_data["accessToken"]):
        try:
            harness.store.authenticate(old_access, "device")
        except Exception as exc:
            assert getattr(exc, "code", None) == "AUTH_REQUIRED"
        else:
            raise AssertionError("old access token remained valid")


def test_refresh_survives_repeated_lost_responses_beyond_legacy_grace(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    device = harness.store.exchange_pairing(
        assistant_id=bundle.assistant_id,
        pairing_secret=bundle.pairing_secret,
        device_name="Android",
        public_key_spki=public_spki(key),
        now=now,
    )

    attempts = [
        harness.store.refresh_device(
            device_id=device.device_id,
            refresh_token=device.refresh_token,
            now=attempt_at,
        )
        for attempt_at in (now + 1, now + 122, now + 10_000)
    ]

    assert all(item.refresh_token == device.refresh_token for item in attempts)
    expected_expiries = [
        attempt_at + harness.settings.refresh_token_ttl_seconds
        for attempt_at in (now + 1, now + 122, now + 10_000)
    ]
    assert [item.refresh_expires_at for item in attempts] == expected_expiries
    assert len({item.access_token for item in attempts}) == len(attempts)
    assert harness.store.authenticate(attempts[-1].access_token, "device")
    for stale in (device.access_token, attempts[0].access_token, attempts[1].access_token):
        with pytest.raises(Exception) as rejected:
            harness.store.authenticate(stale, "device")
        assert getattr(rejected.value, "code", None) == "AUTH_REQUIRED"


def test_legacy_previous_refresh_is_promoted_to_stable_current_token(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    device = harness.store.exchange_pairing(
        assistant_id=bundle.assistant_id,
        pairing_secret=bundle.pairing_secret,
        device_name="Android",
        public_key_spki=public_spki(key),
        now=now,
    )
    with harness.store._lock, harness.store._db:
        legacy_rotated = harness.store._rotate_device_tokens_locked(
            assistant_id=bundle.assistant_id,
            device_id=device.device_id,
            now=now + 1,
            preserve_current_refresh_as_previous=True,
            retry_grace_seconds=120,
        )
    assert legacy_rotated.refresh_token != device.refresh_token

    recovered = harness.store.refresh_device(
        device_id=device.device_id,
        refresh_token=device.refresh_token,
        now=now + 2,
    )
    after_old_grace = harness.store.refresh_device(
        device_id=device.device_id,
        refresh_token=device.refresh_token,
        now=now + 1_000,
    )

    assert recovered.refresh_token == device.refresh_token
    assert after_old_grace.refresh_token == device.refresh_token
    assert recovered.refresh_expires_at == now + 2 + harness.settings.refresh_token_ttl_seconds
    assert harness.store.authenticate(after_old_grace.access_token, "device")


def test_active_stable_refresh_slides_expiry_but_idle_device_still_expires(harness):
    now = int(time.time())
    ttl = harness.settings.refresh_token_ttl_seconds
    active_bundle = harness.bootstrap()
    active_key = ec.generate_private_key(ec.SECP256R1())
    active = harness.store.exchange_pairing(
        assistant_id=active_bundle.assistant_id,
        pairing_secret=active_bundle.pairing_secret,
        device_name="active-phone",
        public_key_spki=public_spki(active_key),
        now=now,
    )

    first = harness.store.refresh_device(
        device_id=active.device_id,
        refresh_token=active.refresh_token,
        now=now + ttl - 1,
    )
    second = harness.store.refresh_device(
        device_id=active.device_id,
        refresh_token=active.refresh_token,
        now=now + (2 * ttl) - 2,
    )

    assert first.refresh_expires_at == now + (2 * ttl) - 1
    assert second.refresh_expires_at == now + (3 * ttl) - 2
    assert second.refresh_token == active.refresh_token

    idle_bundle = harness.bootstrap()
    idle_key = ec.generate_private_key(ec.SECP256R1())
    idle = harness.store.exchange_pairing(
        assistant_id=idle_bundle.assistant_id,
        pairing_secret=idle_bundle.pairing_secret,
        device_name="idle-phone",
        public_key_spki=public_spki(idle_key),
        now=now,
    )
    with pytest.raises(RefreshDenied):
        harness.store.refresh_device(
            device_id=idle.device_id,
            refresh_token=idle.refresh_token,
            now=now + ttl + 1,
        )


def test_proof_nonce_replay_and_extra_fields_fail_closed(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    body = pairing_body(bundle, key)
    headers = proof_headers(
        key,
        method="POST",
        target="/relay/v1/pairings/exchange",
        body=body,
        nonce=b"a" * 16,
    )
    first = harness.client.post(
        "/relay/v1/pairings/exchange",
        content=body,
        headers={"content-type": "application/json", **headers},
    )
    assert first.status_code == 200
    replay = harness.client.post(
        "/relay/v1/pairings/exchange",
        content=body,
        headers={"content-type": "application/json", **headers},
    )
    assert replay.status_code == 401

    invalid = json.loads(body)
    invalid["authorization"] = "forbidden"
    invalid_body = json.dumps(invalid, separators=(",", ":")).encode()
    rejected = harness.client.post(
        "/relay/v1/pairings/exchange",
        content=invalid_body,
        headers={
            "content-type": "application/json",
            **proof_headers(key, method="POST", target="/relay/v1/pairings/exchange", body=invalid_body),
        },
    )
    assert rejected.status_code == 422


def test_revoked_device_cannot_reissue_tokens_with_same_standard_ak_sk(harness):
    bundle = harness.bootstrap()
    key = ec.generate_private_key(ec.SECP256R1())
    first = harness.store.exchange_pairing(
        assistant_id=bundle.assistant_id,
        pairing_secret=bundle.pairing_secret,
        device_name="Android",
        public_key_spki=public_spki(key),
    )
    harness.store.revoke_principal(first.device_id)

    with pytest.raises(PairingDenied):
        harness.store.exchange_pairing(
            assistant_id=bundle.assistant_id,
            pairing_secret=bundle.pairing_secret,
            device_name="Android",
            public_key_spki=public_spki(key),
        )
