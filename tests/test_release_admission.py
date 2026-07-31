"""Atomic run admission and authenticated release-control contracts."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import config
from api.process_identity import process_start_token


@pytest.fixture
def isolated_admission(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(config, "LAST_RUN_FINISHED_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_RESERVATIONS", {})
    monkeypatch.setattr(config, "_RUN_ADMISSION_STATE", "open")
    monkeypatch.setattr(config, "_RUN_ADMISSION_GENERATION", 0)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TOKEN_DIGEST", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TOKEN", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_EXPECTED_IDENTITY", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_FENCED_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LEASE_EXPIRES_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_TRANSACTION_ID", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_ACTION", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_TOKEN_DIGEST", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_EXPECTED_IDENTITY", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LOCAL", threading.local())
    return {"pid": 123, "started_at": 456.0, "instance_id": "instance-a"}


def test_reservation_and_fence_share_one_linearization_lock(isolated_admission):
    barrier = threading.Barrier(3)
    results = []

    def reserve():
        barrier.wait()
        try:
            reservation = config.reserve_run_admission(
                kind="chat",
                source="race-test",
            )
            results.append(("reserved", reservation))
        except config.RunAdmissionClosed:
            results.append(("reserve-rejected", None))

    def fence():
        barrier.wait()
        try:
            fenced = config.fence_run_admission(isolated_admission)
            results.append(("fenced", fenced["token"]))
        except config.RunAdmissionConflict:
            results.append(("fence-conflict", None))

    threads = [threading.Thread(target=reserve), threading.Thread(target=fence)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert {row[0] for row in results} in (
        {"reserved", "fenced"},
        {"reserve-rejected", "fenced"},
    )
    snapshot = config.run_admission_snapshot()
    assert snapshot["state"] == "fenced"
    if any(row[0] == "reserved" for row in results):
        assert snapshot["reservations"] == 1
    else:
        assert snapshot["reservations"] == 0


def _write_pair_open_gate(
    hermes_home: Path,
    *,
    transaction_id: str,
    epoch: int,
    agent: dict,
    webui: dict,
) -> Path:
    owner_payload = {
        "schema": "hermes.pair_open_gate.v1",
        "action": "hold_pair_open",
        "transaction_id": transaction_id,
        "created_at": "2026-07-23T08:15:30+00:00",
        "epoch": epoch,
        "agent": agent,
        "webui": webui,
    }
    owner_hash = hashlib.sha256(
        json.dumps(
            owner_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        **owner_payload,
        "owner_hash": owner_hash,
    }
    gate = hermes_home / ".pair_open_gate.json"
    gate.write_bytes(
        (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    gate.chmod(0o600)
    return gate


def _pair_open_gate_test_identities(
    tmp_path: Path,
    monkeypatch,
    *,
    epoch: int = 7,
) -> tuple[dict, dict]:
    agent_manifest_sha256 = "a" * 64
    release_pair_id = f"pair_{'b' * 64}"
    release_path = tmp_path / "releases" / "webui-build-a"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_RELEASE_PATH", str(release_path))
    monkeypatch.setenv(
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256",
        agent_manifest_sha256,
    )
    monkeypatch.setenv("HERMES_WEBUI_RELEASE_PAIR_ID", release_pair_id)
    monkeypatch.setenv("HERMES_WEBUI_SELECTOR_GENERATION", str(epoch))
    start_time = process_start_token(os.getpid())
    assert start_time
    return (
        {
            "build_id": agent_manifest_sha256,
            "pid": 456,
            "start_time": "procfs:456:agent-start",
            "instance_epoch": release_pair_id,
        },
        {
            "build_id": release_path.name,
            "pid": os.getpid(),
            "start_time": start_time,
            "instance_epoch": str(epoch),
        },
    )


def test_pair_open_gate_keeps_effective_admission_closed_until_atomic_release(
    tmp_path,
    monkeypatch,
    isolated_admission,
):
    transaction_id = "pair-open-gate-transaction-000001"
    agent, webui = _pair_open_gate_test_identities(tmp_path, monkeypatch)
    monkeypatch.setattr(
        config,
        "_RUN_ADMISSION_LAST_TRANSACTION_ID",
        transaction_id,
    )
    gate = _write_pair_open_gate(
        tmp_path,
        transaction_id=transaction_id,
        epoch=7,
        agent=agent,
        webui=webui,
    )
    payload = json.loads(gate.read_bytes())
    payload_sha256 = hashlib.sha256(gate.read_bytes()).hexdigest()

    gated = config.run_admission_snapshot()

    assert gated["state"] == "open"
    assert gated["effective_state"] == "pair-gated"
    assert gated["pair_gate"] == {
        "status": "active",
        "transaction_id": transaction_id,
        "epoch": 7,
        "owner_hash": payload["owner_hash"],
        "payload_sha256": payload_sha256,
        "agent": agent,
        "webui": webui,
    }
    with pytest.raises(config.RunAdmissionClosed, match="pair-open gate"):
        config.reserve_run_admission(kind="must-remain-fenced")

    gate.unlink()
    released = config.run_admission_snapshot()
    assert released["effective_state"] == "open"
    assert released["pair_gate"] == {
        "status": "absent",
        "transaction_id": None,
        "epoch": None,
        "owner_hash": None,
        "payload_sha256": None,
        "agent": None,
        "webui": None,
    }
    reservation = config.reserve_run_admission(kind="after-pair-release")
    assert config.release_run_admission(reservation) is True


@pytest.mark.parametrize(
    ("transaction_id", "epoch"),
    [
        ("different-pair-open-transaction-01", 7),
        ("pair-open-gate-transaction-000001", 8),
    ],
)
def test_stale_or_mismatched_pair_open_gate_fails_closed(
    tmp_path,
    monkeypatch,
    isolated_admission,
    transaction_id,
    epoch,
):
    expected_transaction = "pair-open-gate-transaction-000001"
    agent, webui = _pair_open_gate_test_identities(tmp_path, monkeypatch)
    monkeypatch.setattr(
        config,
        "_RUN_ADMISSION_LAST_TRANSACTION_ID",
        expected_transaction,
    )
    _write_pair_open_gate(
        tmp_path,
        transaction_id=transaction_id,
        epoch=epoch,
        agent=agent,
        webui=webui,
    )

    snapshot = config.run_admission_snapshot()

    assert snapshot["state"] == "open"
    assert snapshot["effective_state"] == "pair-gated"
    assert snapshot["pair_gate"]["status"] == "invalid"
    with pytest.raises(config.RunAdmissionClosed, match="pair-open gate"):
        config.reserve_run_admission(kind="stale-gate-must-block")


def test_pair_open_gate_same_inode_mutation_during_read_fails_closed(
    tmp_path,
    monkeypatch,
    isolated_admission,
):
    transaction_id = "pair-open-gate-transaction-000001"
    agent, webui = _pair_open_gate_test_identities(tmp_path, monkeypatch)
    monkeypatch.setattr(
        config,
        "_RUN_ADMISSION_LAST_TRANSACTION_ID",
        transaction_id,
    )
    gate = _write_pair_open_gate(
        tmp_path,
        transaction_id=transaction_id,
        epoch=7,
        agent=agent,
        webui=webui,
    )
    original_read = config.os.read
    mutated = False

    def racing_read(descriptor, size):
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            gate.chmod(0o640)
        return chunk

    monkeypatch.setattr(config.os, "read", racing_read)

    snapshot = config.run_admission_snapshot()

    assert snapshot["effective_state"] == "pair-gated"
    assert snapshot["pair_gate"]["status"] == "invalid"
    with pytest.raises(config.RunAdmissionClosed, match="pair-open gate"):
        config.reserve_run_admission(kind="same-inode-race-must-block")


def test_nested_admission_scope_reuses_pre_fence_reservation(isolated_admission):
    with config.run_admission_scope(kind="outer") as (outer_id, outer_state):
        fenced = config.fence_run_admission(isolated_admission)
        with config.run_admission_scope(kind="inner") as (inner_id, inner_state):
            assert inner_id == outer_id
            assert inner_state is outer_state

    snapshot = config.run_admission_snapshot()
    assert snapshot["state"] == "fenced"
    assert snapshot["reservations"] == 0
    config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )


def test_admitted_parent_can_fork_tracked_finalizer_after_fence(isolated_admission):
    parent = config.reserve_run_admission(kind="background")
    fenced = config.fence_run_admission(isolated_admission)

    child = config.fork_run_admission(
        parent,
        kind="background_finalizer",
        session_id="bg-1",
    )
    config.register_active_run(
        "background-finalizer:bg-1",
        admission_reservation_id=child,
        session_id="bg-1",
    )

    assert config.run_admission_snapshot()["active_runs"] == 1
    config.unregister_active_run("background-finalizer:bg-1")
    config.release_run_admission(parent)
    committed = config.commit_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert committed["state"] == "committing"


def test_abandoned_fence_lease_reopens_admission(monkeypatch, isolated_admission):
    now = [100.0]
    monkeypatch.setattr(config.time, "time", lambda: now[0])
    monkeypatch.setattr(config, "RUN_ADMISSION_FENCE_LEASE_SECONDS", 2.0)

    fenced = config.fence_run_admission(isolated_admission)
    assert fenced["admission"]["lease_expires_at"] == 102.0
    now[0] = 103.0

    snapshot = config.run_admission_snapshot()
    assert snapshot["state"] == "open"
    assert snapshot["generation"] == 2
    reservation = config.reserve_run_admission(kind="post-expiry")
    assert config.release_run_admission(reservation) is True


def test_fence_and_commit_are_idempotent_for_one_transaction(isolated_admission):
    transaction_id = "t" * 32

    first = config.fence_run_admission(
        isolated_admission,
        transaction_id=transaction_id,
    )
    repeated = config.fence_run_admission(
        isolated_admission,
        transaction_id=transaction_id,
    )

    assert repeated["token"] == first["token"]
    assert repeated["admission"]["generation"] == 1

    committed = config.commit_run_admission(
        first["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
    )
    repeated_commit = config.commit_run_admission(
        first["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
    )

    assert committed["state"] == "committing"
    assert repeated_commit == committed
    assert repeated_commit["generation"] == 2


def test_different_transaction_cannot_steal_fence(isolated_admission):
    config.fence_run_admission(
        isolated_admission,
        transaction_id="a" * 32,
    )

    with pytest.raises(config.RunAdmissionConflict):
        config.fence_run_admission(
            isolated_admission,
            transaction_id="b" * 32,
        )


def test_committing_fence_never_auto_expires_after_driver_dies(
    monkeypatch,
    isolated_admission,
):
    now = [100.0]
    monkeypatch.setattr(config.time, "time", lambda: now[0])
    monkeypatch.setattr(config, "RUN_ADMISSION_FENCE_LEASE_SECONDS", 2.0)
    transaction_id = "c" * 32
    fenced = config.fence_run_admission(
        isolated_admission,
        transaction_id=transaction_id,
    )
    config.commit_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
    )
    now[0] = 103.0

    snapshot = config.run_admission_snapshot()

    assert snapshot["state"] == "committing"
    assert snapshot["generation"] == 2
    assert snapshot["transaction_id"] == transaction_id


def test_abort_is_idempotent_for_same_transaction(isolated_admission):
    transaction_id = "d" * 32
    fenced = config.fence_run_admission(
        isolated_admission,
        transaction_id=transaction_id,
    )
    first = config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
    )
    repeated = config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
    )

    assert first == repeated
    assert repeated["state"] == "open"
    assert repeated["generation"] == 2


def test_reserved_worker_can_upgrade_while_fenced_but_unreserved_cannot(
    isolated_admission,
):
    reservation = config.reserve_run_admission(kind="chat", session_id="s1")
    fenced = config.fence_run_admission(isolated_admission)

    config.register_active_run(
        "stream-1",
        admission_reservation_id=reservation,
        session_id="s1",
    )
    with pytest.raises(config.RunAdmissionClosed):
        config.register_active_run("stream-2", session_id="s2")

    assert config.run_admission_snapshot()["active_runs"] == 1
    config.unregister_active_run("stream-1")
    committed = config.commit_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert committed["state"] == "committing"


def test_abort_requires_exact_token_and_process_identity(isolated_admission):
    fenced = config.fence_run_admission(isolated_admission)

    with pytest.raises(config.RunAdmissionAuthenticationError):
        config.abort_run_admission("wrong", expected_identity=isolated_admission)
    with pytest.raises(config.RunAdmissionIdentityMismatch):
        config.abort_run_admission(
            fenced["token"],
            expected_identity={**isolated_admission, "pid": 999},
        )

    reopened = config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert reopened["state"] == "open"
    assert reopened["generation"] == 2


def test_commit_refuses_reservations_and_active_runs(isolated_admission):
    reservation = config.reserve_run_admission(kind="cron")
    fenced = config.fence_run_admission(isolated_admission)
    with pytest.raises(config.RunAdmissionBusy):
        config.commit_run_admission(
            fenced["token"],
            expected_identity=isolated_admission,
        )
    config.release_run_admission(reservation)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["legacy-active"] = {"stream_id": "legacy-active"}
    with pytest.raises(config.RunAdmissionBusy):
        config.commit_run_admission(
            fenced["token"],
            expected_identity=isolated_admission,
        )


def _signed_release_headers(body, key, *, timestamp=None):
    from api.release_control import release_control_signing_bytes

    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(
        key,
        release_control_signing_bytes(body, timestamp),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Hermes-Release-Timestamp": timestamp,
        "X-Hermes-Release-Signature": signature,
    }


def test_release_control_hmac_is_loopback_fresh_and_nonce_bound(monkeypatch):
    from api import release_control

    key = b"k" * 32
    body = {
        "action": "fence",
        "nonce": "n" * 32,
        "expected": {"pid": 123, "started_at": 456.0, "instance_id": "i"},
    }
    headers = _signed_release_headers(body, key)
    monkeypatch.setattr(release_control, "_signing_key", lambda: key)
    monkeypatch.setattr(release_control, "_SEEN_NONCES", {})

    remote = SimpleNamespace(client_address=("192.0.2.10", 1), headers=headers)
    assert release_control.verify_release_control_request(remote, body)[0] is False

    loopback = SimpleNamespace(client_address=("127.0.0.1", 1), headers=headers)
    assert release_control.verify_release_control_request(loopback, body) == (True, None)
    assert release_control.verify_release_control_request(loopback, body)[0] is False

    stale_headers = _signed_release_headers(body, key, timestamp=int(time.time()) - 120)
    stale = SimpleNamespace(client_address=("::1", 1), headers=stale_headers)
    assert release_control.verify_release_control_request(stale, body)[0] is False


def test_release_control_inspect_is_exact_and_response_attested(
    monkeypatch, isolated_admission
):
    from api import release_control

    key = b"i" * 32
    transaction_id = "x" * 32
    body = {
        "action": "inspect",
        "nonce": "n" * 32,
        "transaction_id": transaction_id,
    }
    activity = {
        "active_streams": 0,
        "active_async_delegations": 0,
        "async_delegations_available": True,
        "active_background_memory_commits": 0,
        "in_flight_memory_commits": 0,
        "memory_commit_activity_available": True,
        "pending_oauth_flows": 0,
        "oauth_activity_available": True,
        "active_terminals": 0,
        "terminal_activity_available": True,
        "running_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
    }
    monkeypatch.setattr(release_control, "_signing_key", lambda: key)
    monkeypatch.setattr(
        release_control,
        "current_release_process_identity",
        lambda: isolated_admission,
    )
    monkeypatch.setattr(release_control, "release_activity_snapshot", lambda: activity)

    receipt = release_control.execute_release_control(body)

    assert receipt["status"] == "inspected"
    assert receipt["identity"] == isolated_admission
    assert receipt["activity"] == activity
    assert receipt["transaction_id"] == transaction_id
    assert receipt["request_nonce"] == body["nonce"]
    signature = receipt.pop("attestation")
    assert hmac.compare_digest(
        signature,
        hmac.new(
            key,
            release_control.release_control_response_signing_bytes(receipt),
            hashlib.sha256,
        ).hexdigest(),
    )


def test_release_control_rejects_missing_transaction_identity(isolated_admission):
    from api import release_control

    with pytest.raises(ValueError, match="transaction"):
        release_control.execute_release_control(
            {
                "action": "inspect",
                "nonce": "n" * 32,
            }
        )


def test_process_completion_activity_accepts_verified_checkpoint_metadata():
    from api import release_control

    snapshot = {
        "running_processes": 0,
        "foreign_owner_active_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
        "process_checkpoint_available": True,
        "process_checkpoint_reason": "verified",
    }

    assert release_control._process_completion_activity_snapshot(
        lambda: snapshot
    ) == snapshot
    assert release_control._process_completion_activity_snapshot(
        lambda: {
            **snapshot,
            "process_checkpoint_reason": "invalid",
        }
    ) == {
        "process_completion_activity_available": False,
        "process_checkpoint_available": False,
        "process_checkpoint_reason": "unavailable",
    }


@pytest.mark.parametrize(
    "snapshot_update",
    [
        {"foreign_owner_active_processes": True},
        {"foreign_owner_active_processes": -1},
        {"foreign_owner_active_processes": "0"},
    ],
)
def test_process_completion_activity_rejects_invalid_foreign_owner_count(
    snapshot_update,
):
    from api import release_control

    snapshot = {
        "running_processes": 0,
        "foreign_owner_active_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
        "process_checkpoint_available": True,
        "process_checkpoint_reason": "verified",
        **snapshot_update,
    }

    assert release_control._process_completion_activity_snapshot(
        lambda: snapshot
    ) == {
        "process_completion_activity_available": False,
        "process_checkpoint_available": False,
        "process_checkpoint_reason": "unavailable",
    }


@pytest.mark.parametrize("schema_change", ["missing", "extra"])
def test_process_completion_activity_rejects_schema_drift(schema_change):
    from api import release_control

    snapshot = {
        "running_processes": 0,
        "foreign_owner_active_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
        "process_checkpoint_available": True,
        "process_checkpoint_reason": "verified",
    }
    if schema_change == "missing":
        snapshot.pop("foreign_owner_active_processes")
    else:
        snapshot["unknown_process_count"] = 0

    assert release_control._process_completion_activity_snapshot(
        lambda: snapshot
    ) == {
        "process_completion_activity_available": False,
        "process_checkpoint_available": False,
        "process_checkpoint_reason": "unavailable",
    }


def test_release_control_commit_checks_streams_and_delegations(
    monkeypatch, isolated_admission
):
    from api import release_control

    monkeypatch.setattr(release_control, "current_release_process_identity", lambda: isolated_admission)
    fenced = config.fence_run_admission(isolated_admission)

    drained_activity = {
        "active_streams": 0,
        "active_async_delegations": 0,
        "async_delegations_available": True,
        "active_background_memory_commits": 0,
        "in_flight_memory_commits": 0,
        "memory_commit_activity_available": True,
        "pending_oauth_flows": 0,
        "oauth_activity_available": True,
        "active_terminals": 0,
        "terminal_activity_available": True,
        "running_processes": 0,
        "foreign_owner_active_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
        "process_checkpoint_available": True,
        "process_checkpoint_reason": "verified",
    }

    monkeypatch.setattr(
        release_control,
        "release_activity_snapshot",
        lambda: {**drained_activity, "active_streams": 1},
    )
    with pytest.raises(config.RunAdmissionBusy):
        release_control.commit_release_control(
            fenced["token"],
            expected_identity=isolated_admission,
        )

    monkeypatch.setattr(
        release_control,
        "release_activity_snapshot",
        lambda: {**drained_activity, "foreign_owner_active_processes": 1},
    )
    with pytest.raises(config.RunAdmissionBusy):
        release_control.commit_release_control(
            fenced["token"],
            expected_identity=isolated_admission,
        )

    monkeypatch.setattr(
        release_control,
        "release_activity_snapshot",
        lambda: {**drained_activity, "active_async_delegations": 1},
    )
    with pytest.raises(config.RunAdmissionBusy):
        release_control.commit_release_control(
            fenced["token"],
            expected_identity=isolated_admission,
        )

    monkeypatch.setattr(
        release_control,
        "release_activity_snapshot",
        lambda: dict(drained_activity),
    )
    result = release_control.commit_release_control(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert result["admission"]["state"] == "committing"
    assert "token" not in json.dumps(result)


def test_server_write_fence_rejects_before_route_mutation(
    monkeypatch, isolated_admission
):
    import server

    fenced = config.fence_run_admission(isolated_admission)
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/chat/start"
    handler.command = "POST"
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.status = None
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda _key, _value: None
    handler.end_headers = lambda: None
    called = []
    monkeypatch.setattr(server, "check_auth", lambda _handler, _parsed: True)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)

    server.Handler._handle_write(handler, lambda *_args: called.append("mutated"))

    assert handler.status == 503
    assert called == []
    assert b"maintenance_fence" in handler.wfile.getvalue()
    config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )


def test_chat_and_background_entrypoints_reject_before_session_mutation(
    monkeypatch, isolated_admission
):
    from api import routes

    fenced = config.fence_run_admission(isolated_admission)
    response = routes._start_chat_stream_for_session(
        SimpleNamespace(session_id="s1"),
        msg="hello",
        workspace="/tmp",
        model="test-model",
    )
    assert response["_status"] == 503
    assert response["code"] == "maintenance_fence"

    handler = SimpleNamespace(
        wfile=io.BytesIO(),
        send_response=lambda status: setattr(handler, "status", status),
        send_header=lambda _key, _value: None,
        end_headers=lambda: None,
        status=None,
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(AssertionError("must not load session")),
    )
    routes._handle_background(
        handler,
        {"session_id": "s1", "prompt": "work"},
    )
    assert handler.status == 503
    assert b"maintenance_fence" in handler.wfile.getvalue()
    config.abort_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
