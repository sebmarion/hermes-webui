"""Regression coverage for execution-lineage admission ownership."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def clean_admission_state(monkeypatch):
    from api import config as cfg
    from api import background_process as bp

    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
        cfg._RUN_ADMISSION_RESERVATIONS.clear()
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_STATE", "open")
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TOKEN_DIGEST", None)
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TOKEN", None)
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TRANSACTION_ID", None)
    with cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        cfg.DEFERRED_PROCESS_WAKEUPS.clear()
    monkeypatch.setattr(bp, "dispatch_pending_delegation_wakeups_for_session", lambda *_args, **_kwargs: 0)
    yield cfg
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
        cfg._RUN_ADMISSION_RESERVATIONS.clear()
    with cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        cfg.DEFERRED_PROCESS_WAKEUPS.clear()


def test_bound_reservation_blocks_same_execution_lineage(clean_admission_state):
    cfg = clean_admission_state
    first = cfg.reserve_run_admission(kind="chat")
    cfg.bind_run_admission(first, "v1:key:one")
    second = cfg.reserve_run_admission(kind="tool_limit_continuation")

    with pytest.raises(cfg.RunAdmissionLineageBusy):
        cfg.bind_run_admission(second, "v1:key:one")


def test_registration_transfers_immutable_lineage_key(clean_admission_state):
    cfg = clean_admission_state
    reservation = cfg.reserve_run_admission(kind="chat")
    cfg.bind_run_admission(reservation, "v1:key:one")

    cfg.register_active_run(
        "run-one",
        admission_reservation_id=reservation,
        session_id="physical-tip",
        lineage_required=True,
    )

    assert cfg.ACTIVE_RUNS["run-one"]["execution_lineage_key"] == "v1:key:one"
    assert reservation not in cfg._RUN_ADMISSION_RESERVATIONS

    with pytest.raises(ValueError):
        cfg.update_active_run("run-one", execution_lineage_key="v1:key:two")
    assert cfg.ACTIVE_RUNS["run-one"]["execution_lineage_key"] == "v1:key:one"


def test_unkeyed_auxiliary_registration_remains_allowed(clean_admission_state):
    cfg = clean_admission_state
    cfg.register_active_run(
        "aux-one",
        session_id="physical-tip",
        source="background-finalizer",
    )
    assert cfg.ACTIVE_RUNS["aux-one"]["session_id"] == "physical-tip"


def test_lineage_key_is_profile_and_root_digest(monkeypatch, tmp_path):
    from api import agent_sessions
    from api import execution_lineage

    db = tmp_path / "state.db"
    monkeypatch.setattr(execution_lineage, "get_hermes_home_for_profile", lambda _: tmp_path)
    monkeypatch.setattr(
        execution_lineage,
        "resolve_shared_session",
        lambda *_args, **_kwargs: agent_sessions.SharedSessionResolution(
            requested_id="root",
            canonical_id="root",
            root_id="root",
            tip_id="root",
            member_ids=("root",),
            canonical_row=None,
            lineage_fingerprint="fingerprint",
            global_projection_generation_hint=None,
            mode="history",
            status="missing",
            database_identity=(str(db), None, None),
        ),
    )

    resolved = execution_lineage.resolve_execution_lineage(
        "root",
        profile="default",
    )
    expected_payload = execution_lineage.canonical_lineage_payload(
        str(db.resolve()), "root"
    )
    expected = "v1:sha256:" + hashlib.sha256(expected_payload).hexdigest()
    assert resolved.execution_lineage_key == expected
    assert str(db.resolve()) in resolved.state_db_path


def test_agent_only_state_db_is_empty_lineage_authority(monkeypatch, tmp_path):
    """An Agent durable ledger must not block a first WebUI turn."""
    from api import execution_lineage

    db = tmp_path / "state.db"
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE async_delegations "
            "(delegation_id TEXT PRIMARY KEY)"
        )
    monkeypatch.setattr(execution_lineage, "get_hermes_home_for_profile", lambda _: tmp_path)

    resolved = execution_lineage.resolve_execution_lineage(
        "first-webui-session",
        profile="default",
    )

    assert resolved.execution_root_session_id == "first-webui-session"
    assert resolved.compression_member_ids == ("first-webui-session",)


def test_invalid_profile_fails_closed(monkeypatch):
    from api import execution_lineage

    with pytest.raises(execution_lineage.ExecutionLineageUnavailable):
        execution_lineage.resolve_execution_lineage("root", profile="../foreign")


def test_route_binds_after_session_identity_exists_before_turn_mutation(
    clean_admission_state, monkeypatch
):
    from api import execution_lineage, routes

    cfg = clean_admission_state
    reservation = cfg.reserve_run_admission(kind="chat")
    session = SimpleNamespace(session_id="physical-root", profile="default")
    resolution = SimpleNamespace(execution_lineage_key="v1:key:route")
    monkeypatch.setattr(
        execution_lineage,
        "resolve_execution_lineage",
        lambda *_args, **_kwargs: resolution,
    )

    routes._bind_execution_lineage(session, reservation)

    assert cfg._RUN_ADMISSION_RESERVATIONS[reservation]["execution_lineage_key"] == (
        "v1:key:route"
    )


def test_lineage_required_worker_cannot_register_without_a_bound_key(
    clean_admission_state,
):
    cfg = clean_admission_state

    with pytest.raises(cfg.RunAdmissionClosed):
        cfg.register_active_run(
            "unkeyed-turn",
            session_id="physical-root",
            lineage_required=True,
        )


def test_deferred_wakeup_bucket_uses_lineage_and_retains_target(
    clean_admission_state, monkeypatch
):
    from api import background_process as bp

    monkeypatch.setattr(
        bp,
        "_execution_lineage_for_session",
        lambda *_args, **_kwargs: ("v1:key:lineage", "profile-a"),
    )
    bp.record_deferred_wakeup(
        "ancestor",
        "process-1",
        "[wake]",
    )

    entry = clean_admission_state.DEFERRED_PROCESS_WAKEUPS["v1:key:lineage"][0]
    assert entry["target_session_id"] == "ancestor"
    assert entry["target_profile"] == "profile-a"
    assert entry["execution_lineage_key"] == "v1:key:lineage"


def test_deferred_record_reuses_live_owner_key(clean_admission_state, monkeypatch):
    from api import background_process as bp

    monkeypatch.setattr(
        bp,
        "_execution_lineage_for_session",
        lambda *_args, **_kwargs: ("v1:key:recomputed", "profile-a"),
    )
    clean_admission_state.ACTIVE_RUNS["parent-run"] = {
        "session_id": "ancestor",
        "execution_lineage_key": "v1:key:live",
    }

    bp.record_deferred_wakeup("ancestor", "process-1", "[wake]")

    assert "v1:key:live" in clean_admission_state.DEFERRED_PROCESS_WAKEUPS
    assert "v1:key:recomputed" not in clean_admission_state.DEFERRED_PROCESS_WAKEUPS


def test_lineage_active_child_blocks_ancestor_drain_then_releases_once(
    clean_admission_state, monkeypatch
):
    from api import background_process as bp

    cfg = clean_admission_state
    monkeypatch.setattr(
        bp,
        "_execution_lineage_for_session",
        lambda *_args, **_kwargs: ("v1:key:lineage", "profile-a"),
    )
    starts = []
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda *args, **kwargs: starts.append((args, kwargs)),
    )
    bp.record_deferred_wakeup("ancestor", "process-1", "[wake]")
    cfg.ACTIVE_RUNS["child-run"] = {
        "session_id": "child",
        "execution_lineage_key": "v1:key:lineage",
    }

    assert bp.drain_deferred_wakeups_for_session("ancestor") == 0
    assert starts == []
    cfg.ACTIVE_RUNS.pop("child-run")
    assert bp.drain_deferred_wakeups_for_session("ancestor") == 1
    assert len(starts) == 1
    assert starts[0][0][0] == "ancestor"
    assert starts[0][1]["expected_profile"] == "profile-a"
    assert bp.drain_deferred_wakeups_for_session("ancestor") == 0


def test_post_unregister_successor_recovery_order_is_shared(
    clean_admission_state, monkeypatch
):
    from api import background_process as bp
    from api import compression_recovery_receipts, execution_lineage
    from api import goal_continuation, tool_limit_continuation

    events = []
    monkeypatch.setattr(
        execution_lineage,
        "resolve_execution_lineage",
        lambda *_args, **_kwargs: SimpleNamespace(
            execution_root_session_id="root",
            profile="profile-a",
        ),
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "settle_compression_recovery",
        lambda sid, parent: events.append(("compression", {"session_id": sid, "parent": parent}))
        or {"state": "started", "started_now": True},
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "recover_pending_compression_recoveries",
        lambda **kwargs: events.append(("compression_pending", kwargs)) or 0,
    )
    monkeypatch.setattr(
        tool_limit_continuation,
        "recover_pending_continuations",
        lambda **kwargs: events.append(("tool", kwargs)) or 1,
    )
    monkeypatch.setattr(
        goal_continuation,
        "recover_pending_goal_continuations",
        lambda **kwargs: events.append(("goal", kwargs)) or 2,
    )
    monkeypatch.setattr(
        bp,
        "drain_deferred_wakeups_for_session",
        lambda sid: events.append(("deferred", sid)) or 3,
    )

    result = bp.recover_successors_after_unregister(
        "ancestor",
        parent_run_id="parent-run",
        session=SimpleNamespace(session_id="tip", profile="profile-a"),
    )

    assert [item[0] for item in events] == ["compression", "tool", "goal", "deferred"]
    assert events[0][1] == {"session_id": "tip", "parent": "parent-run"}
    assert events[1][1] == {"root_session_id": "root", "profile": "profile-a"}
    assert events[2][1] == {"session_id": "tip"}
    assert result == {"compression": 1, "tool_limit": 1, "goal": 2, "deferred": 3}


def test_post_unregister_retries_older_claim_when_exact_parent_has_none(
    clean_admission_state,
    monkeypatch,
):
    from api import background_process as bp
    from api import compression_recovery_receipts, execution_lineage
    from api import goal_continuation, tool_limit_continuation

    events = []
    monkeypatch.setattr(
        execution_lineage,
        "resolve_execution_lineage",
        lambda *_args, **_kwargs: SimpleNamespace(
            execution_root_session_id="root",
            profile="profile-a",
        ),
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "settle_compression_recovery",
        lambda *_args, **_kwargs: events.append("exact") or None,
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "recover_pending_compression_recoveries",
        lambda **kwargs: events.append(("pending", kwargs)) or 1,
    )
    monkeypatch.setattr(tool_limit_continuation, "recover_pending_continuations", lambda **_kwargs: 0)
    monkeypatch.setattr(goal_continuation, "recover_pending_goal_continuations", lambda **_kwargs: 0)
    monkeypatch.setattr(bp, "drain_deferred_wakeups_for_session", lambda _sid: 0)

    result = bp.recover_successors_after_unregister(
        "tip",
        parent_run_id="different-parent",
        session=SimpleNamespace(session_id="tip", profile="profile-a"),
    )

    assert events == ["exact", ("pending", {"session_id": "tip"})]
    assert result["compression"] == 1


def test_lifecycle_health_redacts_lineage_ownership(clean_admission_state):
    from api import routes

    clean_admission_state.ACTIVE_RUNS["run-one"] = {
        "session_id": "physical-tip",
        "stream_id": "run-one",
        "started_at": 1.0,
        "execution_lineage_key": "v1:opaque",
        "lineage_required": True,
    }

    payload = routes._run_lifecycle_health()

    assert payload["active_runs"] == 1
    assert "execution_lineage_key" not in payload["runs"][0]
    assert "lineage_required" not in payload["runs"][0]
