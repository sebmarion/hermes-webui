"""Regression coverage for execution-lineage admission ownership."""

from __future__ import annotations

import hashlib

import pytest


@pytest.fixture
def clean_admission_state(monkeypatch):
    from api import config as cfg

    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
        cfg._RUN_ADMISSION_RESERVATIONS.clear()
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_STATE", "open")
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TOKEN_DIGEST", None)
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TOKEN", None)
        monkeypatch.setattr(cfg, "_RUN_ADMISSION_TRANSACTION_ID", None)
    yield cfg
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
        cfg._RUN_ADMISSION_RESERVATIONS.clear()


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


def test_invalid_profile_fails_closed(monkeypatch):
    from api import execution_lineage

    with pytest.raises(execution_lineage.ExecutionLineageUnavailable):
        execution_lineage.resolve_execution_lineage("root", profile="../foreign")
