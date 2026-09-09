from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.websockets import WebSocketDisconnect

from cheby_relay.admin import (
    bootstrap,
    bootstrap_ephemeral_continuity,
    pairing_envelope,
    purge_ephemeral_continuity,
    read_ephemeral_continuity_metadata,
    revoke_ephemeral_continuity,
    rotate_node,
    validate_relay_origin,
)
from cheby_relay.config import RelaySettings
from cheby_relay.store import (
    AuthenticationError,
    BindingError,
    EPHEMERAL_CONTINUITY_DEVICE_NAME,
    EphemeralContinuityDenied,
    PairingDenied,
    RelayStore,
)
from .conftest import (
    EnrolledDevice,
    command,
    device_ws_headers,
    node_ws_headers,
    receive_type,
)


def test_admin_bootstrap_writes_separate_0600_files_without_persisting_secrets(tmp_path):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    node_file = tmp_path / "node.json"
    bootstrap(
        database=str(database),
        relay_origin="https://relay.example:443",
        device_output=str(device_file),
        node_output=str(node_file),
    )
    assert os.stat(device_file).st_mode & 0o777 == 0o600
    assert os.stat(node_file).st_mode & 0o777 == 0o600
    decoded = json.loads(device_file.read_text())
    assert list(decoded) == ["v", "accessKey", "secretKey"]
    assert "relayOrigin" not in decoded
    node = json.loads(node_file.read_text())
    assert node["assistantId"] == decoded["accessKey"]
    assert node["nodeToken"].encode() not in database.read_bytes()
    assert decoded["secretKey"].encode() not in database.read_bytes()
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        assert store.raw_secret_columns() == []
        assert store.authenticate(node["nodeToken"], "node").principal_id == node["nodeId"]
    finally:
        store.close()


def test_admin_bootstrap_can_emit_explicit_private_gate_cxc1(tmp_path):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    node_file = tmp_path / "node.json"
    bootstrap(
        database=str(database),
        relay_origin="https://relay.example:27462",
        device_output=str(device_file),
        node_output=str(node_file),
        device_format="cxc1",
    )

    assert device_file.read_text().startswith("CXC1.")
    assistant_id = json.loads(node_file.read_text())["assistantId"]
    decoded = base64.urlsafe_b64decode(device_file.read_text()[5:] + "==")
    assert json.loads(decoded)["assistantId"] == assistant_id


def test_admin_bootstrap_rejects_unknown_device_format_before_creating_files(tmp_path):
    device_file = tmp_path / "device"
    node_file = tmp_path / "node"
    with pytest.raises(ValueError, match="unsupported device bootstrap format"):
        bootstrap(
            database=str(tmp_path / "relay.sqlite3"),
            relay_origin="https://relay.example:27461",
            device_output=str(device_file),
            node_output=str(node_file),
            device_format="legacy",
        )
    assert not device_file.exists()
    assert not node_file.exists()


def test_existing_assistants_migrate_to_standard_purpose(tmp_path):
    database = tmp_path / "relay.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """CREATE TABLE assistants (
                   id TEXT PRIMARY KEY,
                   pairing_salt BLOB NOT NULL,
                   pairing_verifier BLOB NOT NULL,
                   pairing_consumed_at INTEGER,
                   pairing_retry_until INTEGER,
                   pairing_device_id TEXT,
                   created_at INTEGER NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO assistants VALUES (?,?,?,?,?,?,?)",
            (
                "asst_" + "L" * 22,
                b"salt",
                b"verifier",
                None,
                None,
                None,
                1,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        row = store._db.execute(
            "SELECT purpose FROM assistants WHERE id=?",
            ("asst_" + "L" * 22,),
        ).fetchone()
        assert row["purpose"] == "standard"
        assert store.bootstrap_assistant().assistant_id.startswith("asst_")
    finally:
        store.close()


def test_pairing_code_and_node_origin_validation():
    assert pairing_envelope(
        assistant_id="asst_0000000000000000000000",
        pairing_secret="pair_" + "x" * 43,
    ).startswith("CXC1.")
    for invalid in (
        "http://relay.example",
        "https://user@relay.example",
        "https://relay.example/path",
        "https://relay.example?q=1",
    ):
        with pytest.raises(ValueError):
            validate_relay_origin(invalid)


def test_node_rotation_file_is_private_and_old_token_fails_closed(tmp_path):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    node_file = tmp_path / "node.json"
    rotated_file = tmp_path / "node-rotated.json"
    bootstrap(
        database=str(database),
        relay_origin="https://relay.example",
        device_output=str(device_file),
        node_output=str(node_file),
    )
    original = json.loads(node_file.read_text())
    rotate_node(
        database=str(database),
        assistant_id=original["assistantId"],
        node_id=original["nodeId"],
        output=str(rotated_file),
    )
    assert os.stat(rotated_file).st_mode & 0o777 == 0o600
    rotated = json.loads(rotated_file.read_text())
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        with pytest.raises(AuthenticationError):
            store.authenticate(original["nodeToken"], "node")
        assert store.authenticate(rotated["nodeToken"], "node").principal_id == original["nodeId"]
    finally:
        store.close()


def test_ephemeral_continuity_revocation_is_isolated_and_idempotent(tmp_path):
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        phone = store.bootstrap_assistant()
        ephemeral = store.bootstrap_ephemeral_continuity()
        public_key_spki = base64.b64encode(
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        tokens = store.exchange_pairing(
            assistant_id=ephemeral.assistant_id,
            pairing_secret=ephemeral.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki=public_key_spki,
        )
        assert store.authenticate(
            phone.node_token, "node"
        ).principal_id == phone.node_id
        assert store.authenticate(
            tokens.access_token, "device"
        ).principal_id == tokens.device_id
    finally:
        store.close()

    revoke_ephemeral_continuity(
        database=str(database),
        assistant_id=ephemeral.assistant_id,
        node_id=ephemeral.node_id,
    )
    revoke_ephemeral_continuity(
        database=str(database),
        assistant_id=ephemeral.assistant_id,
        node_id=ephemeral.node_id,
    )

    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        assert store.authenticate(
            phone.node_token, "node"
        ).principal_id == phone.node_id
        with pytest.raises(AuthenticationError):
            store.authenticate(tokens.access_token, "device")
        with pytest.raises(PairingDenied):
            store.exchange_pairing(
                assistant_id=ephemeral.assistant_id,
                pairing_secret=ephemeral.pairing_secret,
                device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
                public_key_spki=public_key_spki,
            )
    finally:
        store.close()

    purge_ephemeral_continuity(
        database=str(database),
        assistant_id=ephemeral.assistant_id,
        node_id=ephemeral.node_id,
    )
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        assert store.authenticate(
            phone.node_token, "node"
        ).principal_id == phone.node_id
        assert store._db.execute(
            "SELECT 1 FROM assistants WHERE id=?",
            (ephemeral.assistant_id,),
        ).fetchone() is None
    finally:
        store.close()


def test_ephemeral_continuity_bootstrap_never_emits_a_node_secret(tmp_path):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    metadata_file = tmp_path / "metadata.json"
    bootstrap_ephemeral_continuity(
        database=str(database),
        device_output=str(device_file),
        metadata_output=str(metadata_file),
    )
    assert stat.S_IMODE(device_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_file.stat().st_mode) == 0o600
    metadata = json.loads(metadata_file.read_text())
    assert set(metadata) == {
        "v",
        "assistantId",
        "nodeId",
        "purpose",
    }
    assert "token" not in metadata_file.read_text().lower()
    assert "secret" not in metadata_file.read_text().lower()
    assert read_ephemeral_continuity_metadata(str(metadata_file)) == (
        metadata["assistantId"],
        metadata["nodeId"],
    )
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        purpose = store._db.execute(
            "SELECT purpose FROM assistants WHERE id=?",
            (metadata["assistantId"],),
        ).fetchone()
        assert purpose["purpose"] == "production_reload_continuity"
        credentials = store._db.execute(
            """SELECT role,revoked_at FROM credentials
               WHERE assistant_id=?""",
            (metadata["assistantId"],),
        ).fetchall()
        assert [
            (row["role"], row["revoked_at"] is not None)
            for row in credentials
        ] == [("node", True)]
    finally:
        store.close()


def test_ephemeral_metadata_rejects_duplicate_fields_and_public_mode(
    tmp_path,
):
    metadata_file = tmp_path / "metadata.json"
    metadata_file.write_text(
        '{"v":1,"v":1,"assistantId":"asst_0000000000000000000000",'
        '"nodeId":"node_0000000000000000000000",'
        '"purpose":"production-reload-continuity"}',
        encoding="ascii",
    )
    metadata_file.chmod(0o600)
    with pytest.raises(ValueError, match="malformed"):
        read_ephemeral_continuity_metadata(str(metadata_file))
    metadata_file.write_text(
        '{"v":1,"assistantId":"asst_0000000000000000000000",'
        '"nodeId":"node_0000000000000000000000",'
        '"purpose":"production-reload-continuity"}',
        encoding="ascii",
    )
    metadata_file.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe"):
        read_ephemeral_continuity_metadata(str(metadata_file))


def test_ephemeral_bootstrap_precommit_failure_has_idempotent_metadata_recovery(
    monkeypatch,
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    metadata_file = tmp_path / "metadata.json"

    def precommit_failure(*_args, **_kwargs):
        raise RuntimeError("simulated precommit failure")

    monkeypatch.setattr(
        RelayStore,
        "bootstrap_ephemeral_continuity",
        precommit_failure,
    )
    with pytest.raises(RuntimeError, match="precommit failure"):
        bootstrap_ephemeral_continuity(
            database=str(database),
            device_output=str(device_file),
            metadata_output=str(metadata_file),
        )
    assert stat.S_IMODE(device_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_file.stat().st_mode) == 0o600
    assistant_id, node_id = read_ephemeral_continuity_metadata(
        str(metadata_file)
    )
    assert assistant_id.startswith("asst_")
    assert node_id.startswith("node_")
    revoke_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )
    revoke_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        assert store._db.execute(
            "SELECT 1 FROM assistants WHERE id=?",
            (assistant_id,),
        ).fetchone() is None
        assert store._db.execute(
            "SELECT 1 FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone() is None
    finally:
        store.close()
    purge_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )
    purge_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )
    device_file.unlink()
    metadata_file.unlink()
    assert list(tmp_path.iterdir()) == [database]


def test_ephemeral_bootstrap_unknown_commit_fences_exact_committed_identity(
    monkeypatch,
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    device_file = tmp_path / "device.cxc1"
    metadata_file = tmp_path / "metadata.json"
    original = RelayStore.bootstrap_ephemeral_continuity

    def unknown_commit(store, prepared=None):
        original(store, prepared)
        raise RuntimeError("simulated unknown commit")

    monkeypatch.setattr(
        RelayStore,
        "bootstrap_ephemeral_continuity",
        unknown_commit,
    )
    with pytest.raises(RuntimeError, match="unknown commit"):
        bootstrap_ephemeral_continuity(
            database=str(database),
            device_output=str(device_file),
            metadata_output=str(metadata_file),
        )
    assistant_id, node_id = read_ephemeral_continuity_metadata(
        str(metadata_file)
    )
    revoke_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        assistant = store._db.execute(
            "SELECT purpose,pairing_consumed_at FROM assistants WHERE id=?",
            (assistant_id,),
        ).fetchone()
        assert assistant is not None
        assert assistant["purpose"] == "production_reload_continuity"
        assert assistant["pairing_consumed_at"] is not None
        node = store._db.execute(
            "SELECT assistant_id,active_generation FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
        assert node is not None
        assert node["assistant_id"] == assistant_id
        assert node["active_generation"] == 0
        credential = store._db.execute(
            """SELECT revoked_at FROM credentials
               WHERE assistant_id=? AND principal_id=? AND role='node'""",
            (assistant_id, node_id),
        ).fetchone()
        assert credential is not None
        assert credential["revoked_at"] is not None
    finally:
        store.close()
    purge_ephemeral_continuity(
        database=str(database),
        assistant_id=assistant_id,
        node_id=node_id,
    )


def test_absent_ephemeral_recovery_rejects_partial_or_standard_identity(
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        standard = store.bootstrap_assistant()
        with pytest.raises(EphemeralContinuityDenied):
            store.revoke_ephemeral_continuity_assistant(
                assistant_id=standard.assistant_id,
                node_id=standard.node_id,
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.purge_ephemeral_continuity_assistant(
                assistant_id=standard.assistant_id,
                node_id=standard.node_id,
            )

        partial = store.bootstrap_ephemeral_continuity()
        with store._db:
            store._db.execute(
                "DELETE FROM credentials WHERE assistant_id=?",
                (partial.assistant_id,),
            )
            store._db.execute(
                "DELETE FROM streams WHERE assistant_id=?",
                (partial.assistant_id,),
            )
            store._db.execute(
                "DELETE FROM nodes WHERE id=?",
                (partial.node_id,),
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.revoke_ephemeral_continuity_assistant(
                assistant_id=partial.assistant_id,
                node_id=partial.node_id,
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.purge_ephemeral_continuity_assistant(
                assistant_id=partial.assistant_id,
                node_id=partial.node_id,
            )

        other = store.bootstrap_ephemeral_continuity()
        absent_assistant = "asst_" + "Z" * 22
        with pytest.raises(EphemeralContinuityDenied):
            store.revoke_ephemeral_continuity_assistant(
                assistant_id=absent_assistant,
                node_id=other.node_id,
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.purge_ephemeral_continuity_assistant(
                assistant_id=absent_assistant,
                node_id=other.node_id,
            )

        ghost_device = store.bootstrap_ephemeral_continuity()
        with store._db:
            store._db.execute(
                "UPDATE assistants SET pairing_device_id=? WHERE id=?",
                ("dev_" + "G" * 22, ghost_device.assistant_id),
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.revoke_ephemeral_continuity_assistant(
                assistant_id=ghost_device.assistant_id,
                node_id=ghost_device.node_id,
            )
        with pytest.raises(EphemeralContinuityDenied):
            store.purge_ephemeral_continuity_assistant(
                assistant_id=ghost_device.assistant_id,
                node_id=ghost_device.node_id,
            )
    finally:
        store.close()


def test_ephemeral_continuity_revocation_rejects_active_node_and_wrong_device(
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    forbidden_node_output = tmp_path / "forbidden-node.json"
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        active_node = store.bootstrap_ephemeral_continuity()
        with pytest.raises(BindingError, match="binding denied"):
            store.rotate_node_credential(
                active_node.assistant_id,
                active_node.node_id,
            )
        with pytest.raises(BindingError, match="binding denied"):
            rotate_node(
                database=str(database),
                assistant_id=active_node.assistant_id,
                node_id=active_node.node_id,
                output=str(forbidden_node_output),
            )
        assert not forbidden_node_output.exists()
        with pytest.raises(BindingError, match="binding denied"):
            store.activate_node_generation(active_node.node_id)
        assert store._db.execute(
            "SELECT active_generation FROM nodes WHERE id=?",
            (active_node.node_id,),
        ).fetchone()[0] == 0
        with store._db:
            store._db.execute(
                """UPDATE credentials SET revoked_at=NULL
                   WHERE assistant_id=? AND principal_id=? AND role='node'""",
                (active_node.assistant_id, active_node.node_id),
            )
        with pytest.raises(
            EphemeralContinuityDenied,
            match="Node must be fenced first",
        ):
            store.revoke_ephemeral_continuity_assistant(
                assistant_id=active_node.assistant_id,
                node_id=active_node.node_id,
            )
        with store._db:
            store._db.execute(
                """UPDATE credentials SET revoked_at=created_at
                   WHERE assistant_id=? AND principal_id=? AND role='node'""",
                (active_node.assistant_id, active_node.node_id),
            )
        store.purge_ephemeral_continuity_assistant(
            assistant_id=active_node.assistant_id,
            node_id=active_node.node_id,
        )

        wrong_device = store.bootstrap_ephemeral_continuity()
        with pytest.raises(PairingDenied):
            store.exchange_pairing(
                assistant_id=wrong_device.assistant_id,
                pairing_secret=wrong_device.pairing_secret,
                device_name="ordinary phone",
                public_key_spki="ordinary-phone-spki",
            )
        store.revoke_ephemeral_continuity_assistant(
            assistant_id=wrong_device.assistant_id,
            node_id=wrong_device.node_id,
        )

        stale = store.bootstrap_ephemeral_continuity()
        stale_created = int(
            store._db.execute(
                "SELECT created_at FROM assistants WHERE id=?",
                (stale.assistant_id,),
            ).fetchone()[0]
        )
        with pytest.raises(PairingDenied):
            store.exchange_pairing(
                assistant_id=stale.assistant_id,
                pairing_secret=stale.pairing_secret,
                device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
                public_key_spki="stale-continuity-spki",
                now=stale_created + 3601,
            )
        with pytest.raises(PairingDenied):
            store.exchange_pairing(
                assistant_id=stale.assistant_id,
                pairing_secret=stale.pairing_secret,
                device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
                public_key_spki="future-continuity-spki",
                now=stale_created - 1,
            )
        boundary_tokens = store.exchange_pairing(
            assistant_id=stale.assistant_id,
            pairing_secret=stale.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki=base64.b64encode(
                b"boundary-continuity-spki"
            ).decode("ascii"),
            now=stale_created + 3600,
        )
        assert boundary_tokens.assistant_id == stale.assistant_id
        store.purge_ephemeral_continuity_assistant(
            assistant_id=stale.assistant_id,
            node_id=stale.node_id,
            now=stale_created + 3601,
        )
    finally:
        store.close()


def test_ephemeral_continuity_principal_has_no_message_or_ack_data_plane(
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        ephemeral = store.bootstrap_ephemeral_continuity()
        tokens = store.exchange_pairing(
            assistant_id=ephemeral.assistant_id,
            pairing_secret=ephemeral.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki="continuity-spki",
        )
        principal = store.authenticate(tokens.access_token, "device")
        assert principal.assistant_purpose == "production_reload_continuity"
        before = {
            table: store._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assistant_id=?",
                (ephemeral.assistant_id,),
            ).fetchone()[0]
            for table in (
                "requests",
                "deliveries",
                "message_idempotency",
                "expiration_notices",
            )
        }
        stream_before = [
            tuple(row)
            for row in store._db.execute(
                """SELECT recipient_role,next_seq,ack_cursor FROM streams
                   WHERE assistant_id=? ORDER BY recipient_role""",
                (ephemeral.assistant_id,),
            ).fetchall()
        ]
        with pytest.raises(BindingError, match="no data plane"):
            store.enqueue(
                principal=principal,
                recipient_role="node",
                message_id="msg_" + "M" * 22,
                payload={
                    "kind": "command",
                    "requestId": "req_" + "R" * 22,
                    "deviceId": tokens.device_id,
                    "operation": "turns.start",
                    "params": {
                        "threadId": "thread-private",
                        "clientMessageId": "continuity-message",
                        "input": [{"type": "text", "text": "must fail"}],
                    },
                },
                expires_at=None,
            )
        with pytest.raises(BindingError, match="no data plane"):
            store.acknowledge(principal=principal, seq=0)
        after = {
            table: store._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assistant_id=?",
                (ephemeral.assistant_id,),
            ).fetchone()[0]
            for table in before
        }
        stream_after = [
            tuple(row)
            for row in store._db.execute(
                """SELECT recipient_role,next_seq,ack_cursor FROM streams
                   WHERE assistant_id=? ORDER BY recipient_role""",
                (ephemeral.assistant_id,),
            ).fetchall()
        ]
        assert after == before
        assert stream_after == stream_before
    finally:
        store.close()


def test_ephemeral_continuity_retry_and_refresh_still_revoke_and_purge(
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    store = RelayStore(RelaySettings(db_path=str(database)))
    try:
        ephemeral = store.bootstrap_ephemeral_continuity()
        created_at = int(
            store._db.execute(
                "SELECT created_at FROM assistants WHERE id=?",
                (ephemeral.assistant_id,),
            ).fetchone()[0]
        )
        public_key_spki = base64.b64encode(
            b"continuity-retry-spki"
        ).decode("ascii")
        first = store.exchange_pairing(
            assistant_id=ephemeral.assistant_id,
            pairing_secret=ephemeral.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki=public_key_spki,
            now=created_at,
        )
        retried = store.exchange_pairing(
            assistant_id=ephemeral.assistant_id,
            pairing_secret=ephemeral.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki=public_key_spki,
            now=created_at + 1,
        )
        refreshed = store.refresh_device(
            device_id=retried.device_id,
            refresh_token=retried.refresh_token,
            now=created_at + 2,
        )
        assert first.device_id == retried.device_id == refreshed.device_id
        credentials = store._db.execute(
            """SELECT role,revoked_at FROM credentials
               WHERE assistant_id=? ORDER BY created_at,id""",
            (ephemeral.assistant_id,),
        ).fetchall()
        assert sum(row["role"] == "node" for row in credentials) == 1
        assert sum(row["role"] == "device" for row in credentials) == 3
        assert sum(
            row["role"] == "device" and row["revoked_at"] is None
            for row in credentials
        ) == 1

        store.revoke_ephemeral_continuity_assistant(
            assistant_id=ephemeral.assistant_id,
            node_id=ephemeral.node_id,
            now=created_at + 3,
        )
        store.revoke_ephemeral_continuity_assistant(
            assistant_id=ephemeral.assistant_id,
            node_id=ephemeral.node_id,
            now=created_at + 4,
        )
        for token in (
            first.access_token,
            retried.access_token,
            refreshed.access_token,
        ):
            with pytest.raises(AuthenticationError):
                store.authenticate(token, "device")
        stale_now = (
            created_at
            + store.settings.credential_retention_seconds
            + 100
        )
        gc_result = store.run_gc(now=stale_now)
        assert gc_result.deleted_credentials == 0
        assert store._db.execute(
            "SELECT COUNT(*) FROM credentials WHERE assistant_id=?",
            (ephemeral.assistant_id,),
        ).fetchone()[0] == 4
        store.purge_ephemeral_continuity_assistant(
            assistant_id=ephemeral.assistant_id,
            node_id=ephemeral.node_id,
            now=stale_now,
        )
        store.purge_ephemeral_continuity_assistant(
            assistant_id=ephemeral.assistant_id,
            node_id=ephemeral.node_id,
            now=stale_now + 1,
        )
        for table in (
            "assistants",
            "nodes",
            "devices",
            "credentials",
            "streams",
        ):
            column = "id" if table == "assistants" else "assistant_id"
            assert store._db.execute(
                f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1",
                (ephemeral.assistant_id,),
            ).fetchone() is None
    finally:
        store.close()


def test_ephemeral_continuity_websocket_allows_only_ready_ping_and_close(
    harness,
):
    ephemeral = harness.store.bootstrap_ephemeral_continuity()
    key = ec.generate_private_key(ec.SECP256R1())
    public_key_spki = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")
    tokens = harness.store.exchange_pairing(
        assistant_id=ephemeral.assistant_id,
        pairing_secret=ephemeral.pairing_secret,
        device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
        public_key_spki=public_key_spki,
    )
    device = EnrolledDevice(
        assistant_id=tokens.assistant_id,
        device_id=tokens.device_id,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        key=key,
    )

    def persistence_snapshot():
        counts = {
            table: harness.store._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assistant_id=?",
                (ephemeral.assistant_id,),
            ).fetchone()[0]
            for table in (
                "requests",
                "deliveries",
                "message_idempotency",
                "expiration_notices",
            )
        }
        streams = [
            tuple(row)
            for row in harness.store._db.execute(
                """SELECT recipient_role,next_seq,ack_cursor FROM streams
                   WHERE assistant_id=? ORDER BY recipient_role""",
                (ephemeral.assistant_id,),
            ).fetchall()
        ]
        return counts, streams

    before = persistence_snapshot()
    with harness.client.websocket_connect(
        "/relay/v1/device",
        headers=device_ws_headers(device),
    ) as socket:
        ready = receive_type(socket, "ready")
        assert ready["assistantId"] == ephemeral.assistant_id
        assert ready["principalId"] == tokens.device_id
        socket.send_json({"v": 1, "type": "ping", "nonce": "abcdefgh"})
        assert receive_type(socket, "pong")["nonce"] == "abcdefgh"
        socket.send_json(
            command(
                device,
                "turns.start",
                {
                    "threadId": "continuity-must-not-exist",
                    "clientMessageId": "continuity-must-not-persist",
                    "input": [{"type": "text", "text": "must fail"}],
                },
            )
        )
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403
    assert persistence_snapshot() == before

    with harness.client.websocket_connect(
        "/relay/v1/device",
        headers=device_ws_headers(device),
    ) as socket:
        receive_type(socket, "ready")
        socket.send_json(
            command(
                device,
                "threads.list",
                {"archived": False},
            )
        )
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403
    assert persistence_snapshot() == before

    with harness.client.websocket_connect(
        "/relay/v1/device",
        headers=device_ws_headers(device),
    ) as socket:
        receive_type(socket, "ready")
        socket.send_json({"v": 1, "type": "ack", "deliverySeq": 0})
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403
    assert persistence_snapshot() == before


def test_continuity_purpose_rejects_tampered_node_websocket_without_state_change(
    harness,
):
    tampered = harness.store.bootstrap_assistant()
    with harness.store._db:
        harness.store._db.execute(
            "UPDATE assistants SET purpose=? WHERE id=?",
            ("production_reload_continuity", tampered.assistant_id),
        )

    def persistence_snapshot():
        assistant = tuple(
            harness.store._db.execute(
                """SELECT pairing_consumed_at,pairing_retry_until,
                          pairing_device_id,purpose
                   FROM assistants WHERE id=?""",
                (tampered.assistant_id,),
            ).fetchone()
        )
        node = tuple(
            harness.store._db.execute(
                "SELECT assistant_id,active_generation FROM nodes WHERE id=?",
                (tampered.node_id,),
            ).fetchone()
        )
        credentials = [
            tuple(row)
            for row in harness.store._db.execute(
                """SELECT id,principal_id,role,expires_at,revoked_at,created_at
                   FROM credentials WHERE assistant_id=? ORDER BY id""",
                (tampered.assistant_id,),
            ).fetchall()
        ]
        streams = [
            tuple(row)
            for row in harness.store._db.execute(
                """SELECT recipient_role,next_seq,ack_cursor FROM streams
                   WHERE assistant_id=? ORDER BY recipient_role""",
                (tampered.assistant_id,),
            ).fetchall()
        ]
        data_plane = {
            table: harness.store._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assistant_id=?",
                (tampered.assistant_id,),
            ).fetchone()[0]
            for table in (
                "devices",
                "requests",
                "deliveries",
                "message_idempotency",
                "expiration_notices",
            )
        }
        return assistant, node, credentials, streams, data_plane

    before = persistence_snapshot()
    with harness.client.websocket_connect(
        "/relay/v1/node",
        headers=node_ws_headers(tampered),
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as denied:
            socket.receive_json()
        assert denied.value.code == 4403
    assert persistence_snapshot() == before


def test_live_relay_connection_observes_external_ephemeral_revocation(
    tmp_path,
):
    database = tmp_path / "relay.sqlite3"
    relay_store = RelayStore(RelaySettings(db_path=str(database)))
    admin_store = None
    try:
        phone = relay_store.bootstrap_assistant()
        phone_principal = relay_store.authenticate(phone.node_token, "node")
        ephemeral = relay_store.bootstrap_ephemeral_continuity()
        spki = base64.b64encode(
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii")
        tokens = relay_store.exchange_pairing(
            assistant_id=ephemeral.assistant_id,
            pairing_secret=ephemeral.pairing_secret,
            device_name=EPHEMERAL_CONTINUITY_DEVICE_NAME,
            public_key_spki=spki,
        )
        device_principal = relay_store.authenticate(
            tokens.access_token,
            "device",
        )

        admin_store = RelayStore(RelaySettings(db_path=str(database)))
        admin_store.revoke_ephemeral_continuity_assistant(
            assistant_id=ephemeral.assistant_id,
            node_id=ephemeral.node_id,
        )

        assert not relay_store.principal_still_valid(device_principal)
        assert relay_store.principal_still_valid(phone_principal)
    finally:
        if admin_store is not None:
            admin_store.close()
        relay_store.close()
