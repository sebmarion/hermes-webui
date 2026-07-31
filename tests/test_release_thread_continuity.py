"""Pure contract tests for managed release thread continuity."""

import threading

import pytest


@pytest.fixture
def isolated_admission(monkeypatch):
    from api import config

    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(config, "STREAMS", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {})
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
    monkeypatch.setattr(config, "_RUN_ADMISSION_CHECKPOINT_DEADLINE", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_CHECKPOINT_FORCED_RESERVATIONS", ())
    monkeypatch.setattr(config, "_RUN_ADMISSION_LOCAL", threading.local())
    return {"pid": 123, "started_at": 456.0, "instance_id": "instance-a"}


def test_checkpoint_deadline_is_one_fixed_300_second_window():
    from api.release_thread_continuity import CheckpointDeadline

    deadline = CheckpointDeadline.create(
        wall_started_at=1_000.0,
        monotonic_started_at=50.0,
        boot_id="boot-a",
    )

    assert deadline.wall_deadline == 1_300.0
    assert deadline.monotonic_deadline == 350.0
    assert deadline.reached(wall_now=1_299.999, monotonic_now=349.999, boot_id="boot-a") is False
    assert deadline.reached(wall_now=1_300.0, monotonic_now=349.0, boot_id="boot-a") is True
    assert deadline.reached(wall_now=1_100.0, monotonic_now=350.0, boot_id="boot-a") is True
    assert deadline.reached(wall_now=1_100.0, monotonic_now=60.0, boot_id="boot-b") is True


def test_deadline_rejects_an_arbitrary_longer_timeout():
    from api.release_thread_continuity import CheckpointDeadline

    with pytest.raises(ValueError, match="300"):
        CheckpointDeadline.create(
            wall_started_at=1_000.0,
            monotonic_started_at=50.0,
            boot_id="boot-a",
            timeout_seconds=301,
        )


def test_roster_enrolls_each_exact_stream_once_and_tracks_reservations():
    from api.release_thread_continuity import CheckpointLedger

    ledger = CheckpointLedger(transaction_id="tx-1")
    ledger.reserve("reservation-1", service="webui", kind="chat")
    target = ledger.enroll(
        service="webui",
        session_id="session-1",
        stream_id="stream-1",
        backend="local",
    )

    assert ledger.enroll(
        service="webui",
        session_id="session-1",
        stream_id="stream-1",
        backend="local",
    ) == target
    assert ledger.reservation_count == 1

    ledger.release_reservation("reservation-1")
    ledger.close_population()
    assert ledger.population_closed is True
    assert ledger.target_ids == (target,)


def test_roster_includes_stream_owner_during_active_run_teardown(isolated_admission):
    from api import config

    config.STREAMS["stream-gap"] = object()
    config.STREAM_SESSION_OWNERS["stream-gap"] = "session-gap"
    roster = config.run_admission_snapshot()["checkpoint_active_roster"]

    assert roster == [
        {
            "run_id": "stream-gap",
            "stream_id": "stream-gap",
            "session_id": "session-gap",
            "backend": "local",
            "phase": "stream-only",
        }
    ]


def test_delivery_is_intent_before_action_and_duplicate_safe():
    from api.release_thread_continuity import CheckpointLedger

    ledger = CheckpointLedger(transaction_id="tx-2")
    target = ledger.enroll(
        service="webui",
        session_id="session-2",
        stream_id="stream-2",
        backend="local",
    )

    assert ledger.mark_delivery_intent(target) is True
    assert ledger.mark_delivery_intent(target) is False
    assert ledger.delivery_state(target)["status"] == "intent"
    assert ledger.record_delivery(target, "accepted") is True
    assert ledger.record_delivery(target, "accepted") is False
    assert ledger.delivery_state(target)["status"] == "accepted"


def test_population_cannot_close_with_live_reservation_unless_forced():
    from api.release_thread_continuity import CheckpointLedger, CheckpointStateError

    ledger = CheckpointLedger(transaction_id="tx-3")
    ledger.reserve("reservation-3", service="gateway", kind="run")

    with pytest.raises(CheckpointStateError, match="reservation"):
        ledger.close_population()

    forced = ledger.close_population(forced=True)
    assert forced == ("reservation-3",)
    assert ledger.population_closed is True
    assert ledger.forced_reservations == ("reservation-3",)


def test_settled_without_ack_is_safe_but_not_resume_eligible():
    from api.release_thread_continuity import CheckpointLedger

    ledger = CheckpointLedger(transaction_id="tx-4")
    target = ledger.enroll(
        service="webui",
        session_id="session-4",
        stream_id="stream-4",
        backend="gateway",
    )
    ledger.close_population()
    ledger.record_status(target, "settled_without_ack")

    assert ledger.all_targets_resolved is True
    assert ledger.resume_sessions() == ()


def test_resume_fold_is_conservative_across_duplicate_streams():
    from api.release_thread_continuity import fold_resume_state

    assert fold_resume_state(["settled_without_ack", "acknowledged"]) == "acknowledged"
    assert fold_resume_state(["acknowledged", "forced"]) == "forced"
    assert fold_resume_state(["forced", "owner_changed"]) == "owner_changed"
    assert fold_resume_state(["settled_without_ack"]) == "settled_without_ack"


def test_run_admission_checkpoint_pin_survives_the_legacy_lease(isolated_admission):
    from api import config

    transaction_id = "a" * 32
    fenced = config.fence_run_admission(isolated_admission, transaction_id=transaction_id)
    pinned = config.pin_run_admission_checkpoint(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
        deadline={
            "wall_started_at": 1_000.0,
            "wall_deadline": 1_300.0,
            "monotonic_started_at": 50.0,
            "monotonic_deadline": 350.0,
            "boot_id": "boot-a",
        },
    )

    assert pinned["state"] == "checkpoint-fenced"
    assert pinned["lease_expires_at"] is None


def test_checkpoint_stopping_rejects_a_reserved_worker_upgrade(isolated_admission):
    from api import config

    reservation = config.reserve_run_admission(kind="chat", session_id="s-stop")
    transaction_id = "b" * 32
    fenced = config.fence_run_admission(isolated_admission, transaction_id=transaction_id)
    config.pin_run_admission_checkpoint(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
        deadline={
            "wall_started_at": 1_000.0,
            "wall_deadline": 1_300.0,
            "monotonic_started_at": 50.0,
            "monotonic_deadline": 350.0,
            "boot_id": "boot-a",
        },
    )
    stopped = config.stop_run_admission_checkpoint(
        fenced["token"],
        expected_identity=isolated_admission,
        transaction_id=transaction_id,
        forced=True,
    )

    assert stopped["state"] == "checkpoint-stopping"
    with pytest.raises(config.RunAdmissionClosed):
        config.register_active_run(
            "stream-stop",
            admission_reservation_id=reservation,
            session_id="s-stop",
        )


def test_signed_release_control_can_begin_and_close_checkpoint(isolated_admission, monkeypatch):
    from api import release_control

    identity = dict(isolated_admission)
    transaction_id = "c" * 32
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
        "foreign_owner_active_processes": 0,
        "finalizing_processes": 0,
        "durable_undelivered_completions": 0,
        "process_completion_activity_available": True,
        "process_checkpoint_available": True,
        "process_checkpoint_reason": "verified",
    }
    monkeypatch.setattr(release_control, "current_release_process_identity", lambda: identity)
    monkeypatch.setattr(release_control, "_release_control_signing_key", lambda: b"k" * 32)
    monkeypatch.setattr(release_control, "release_activity_snapshot", lambda: dict(activity))

    fenced = release_control.execute_release_control(
        {
            "action": "fence",
            "transaction_id": transaction_id,
            "nonce": "n" * 32,
            "expected": identity,
        }
    )
    token = fenced["fence_token"]
    begun = release_control.execute_release_control(
        {
            "action": "begin_checkpoint",
            "transaction_id": transaction_id,
            "nonce": "o" * 32,
            "expected": identity,
            "deadline": {
                "wall_started_at": 1_000.0,
                "wall_deadline": 1_300.0,
                "monotonic_started_at": 50.0,
                "monotonic_deadline": 350.0,
                "boot_id": "boot-a",
            },
        },
        fence_token=token,
    )
    assert begun["status"] == "checkpoint-fenced"

    closed = release_control.execute_release_control(
        {
            "action": "checkpoint_threads_close",
            "transaction_id": transaction_id,
            "nonce": "p" * 32,
            "expected": identity,
            "forced": False,
        },
        fence_token=token,
    )
    assert closed["status"] == "checkpoint-stopping"


def test_checkpoint_threads_delivers_once_and_reconciliation_does_not_resend(
    isolated_admission,
    monkeypatch,
):
    from api import config, release_control, streaming

    identity = dict(isolated_admission)
    transaction_id = "d" * 32
    monkeypatch.setattr(release_control, "current_release_process_identity", lambda: identity)
    monkeypatch.setattr(release_control, "_release_control_signing_key", lambda: b"k" * 32)
    monkeypatch.setattr(release_control, "release_activity_snapshot", lambda: {})
    monkeypatch.setattr(
        config,
        "ACTIVE_RUNS",
        {"stream-d": {"stream_id": "stream-d", "session_id": "session-d", "backend": "local"}},
    )
    monkeypatch.setattr(config, "STREAMS", {"stream-d": object()})
    fenced = release_control.execute_release_control(
        {
            "action": "fence",
            "transaction_id": transaction_id,
            "nonce": "q" * 32,
            "expected": identity,
        }
    )
    token = fenced["fence_token"]
    release_control.execute_release_control(
        {
            "action": "begin_checkpoint",
            "transaction_id": transaction_id,
            "nonce": "r" * 32,
            "expected": identity,
            "deadline": {
                "wall_started_at": 1_000.0,
                "wall_deadline": 1_300.0,
                "monotonic_started_at": 50.0,
                "monotonic_deadline": 350.0,
                "boot_id": "boot-a",
            },
        },
        fence_token=token,
    )
    delivered = []

    def fake_delivery(**kwargs):
        delivered.append(kwargs)
        return {"status": "accepted", "stream_id": kwargs["stream_id"]}

    monkeypatch.setattr(streaming, "deliver_release_checkpoint", fake_delivery)
    body = {
        "action": "checkpoint_threads",
        "transaction_id": transaction_id,
        "nonce": "s" * 32,
        "expected": identity,
    }
    first = release_control.execute_release_control(body, fence_token=token)
    second = release_control.execute_release_control(
        {**body, "nonce": "t" * 32},
        fence_token=token,
    )

    assert first["status"] == "checkpoint-dispatched"
    assert second["status"] == "checkpoint-dispatched"
    assert len(delivered) == 1
