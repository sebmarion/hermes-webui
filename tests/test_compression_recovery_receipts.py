"""Durable ownership tests for same-session compression recovery."""

import json
import os
import stat

import pytest

from api import config, models
from api.models import Session
import api.compression_recovery_receipts as receipts


@pytest.fixture
def receipt_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return session_dir


def _seed(*, sid="same-session", parent="parent-run", attachments=None):
    from api.compression_recovery import _recovery_fingerprint

    seed = {
        "session_id": sid,
        "parent_run_id": parent,
        "context_messages": [
            {"role": "assistant", "content": "Bounded trusted checkpoint."},
            {"role": "user", "content": "Finish the exact requested work."},
        ],
        "attachments": list(attachments or []),
        "trust_source": "assistant_checkpoint",
        "fingerprint": "",
    }
    seed["fingerprint"] = _recovery_fingerprint(
        session_id=sid,
        parent_run_id=parent,
        context_messages=seed["context_messages"],
        attachments=seed["attachments"],
    )
    return seed


def _session(*, sid="same-session", profile="default"):
    return Session(
        session_id=sid,
        title="Same title",
        profile=profile,
        messages=[{"role": "user", "content": "Finish the exact requested work."}],
    )


def test_absent_startup_store_does_not_create_state_directories(tmp_path, monkeypatch):
    absent_session_dir = tmp_path / "absent-state" / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", absent_session_dir)

    recovered = receipts.recover_pending_compression_recoveries()

    assert recovered == 0
    assert not absent_session_dir.exists()


def test_duplicate_claim_is_idempotent_and_stays_in_same_session(receipt_store):
    session = _session()
    first = receipts.claim_compression_recovery(session, "parent-run", _seed())
    second = receipts.claim_compression_recovery(session, "parent-run", _seed())

    assert first == second
    assert first["session_id"] == "same-session"
    assert first["state"] == "claimed"
    assert len(first["fingerprint"]) == 64
    assert session.compression_recovery["phase"] == "claimed"
    assert session.compression_recovery["automatic_recovery"] is True
    assert session.recommended_recovery_action is None


def test_duplicate_claim_after_user_supersession_does_not_resurrect_pending_ui(
    receipt_store,
):
    session = _session()
    seed = _seed()
    receipts.claim_compression_recovery(session, "parent-run", seed)
    receipts.supersede_pending_compression_recovery(session)

    duplicate = receipts.claim_compression_recovery(session, "parent-run", seed)

    assert duplicate["state"] == "discarded"
    assert duplicate["discarded_reason"] == "superseded_by_user"
    assert session.compression_recovery == {}
    assert session.recommended_recovery_action is None


def test_duplicate_settle_starts_exactly_one_same_session_successor(receipt_store):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    starts = []

    def start(sid, prompt, **kwargs):
        starts.append((sid, prompt, kwargs))
        return {"session_id": sid, "stream_id": "successor-run"}

    settled = receipts.settle_compression_recovery(
        "same-session", "parent-run", start=start
    )
    duplicate = receipts.settle_compression_recovery(
        "same-session", "parent-run", start=start
    )

    assert settled["state"] == duplicate["state"] == "started"
    assert settled["child_stream_id"] == "successor-run"
    assert len(starts) == 1
    sid, prompt, kwargs = starts[0]
    assert sid == "same-session"
    assert prompt == receipts.RECOVERY_CONTROL_PROMPT
    assert kwargs["attachments"] == []
    assert kwargs["recovery_claim_token"]
    assert kwargs["recovery_fingerprint"] == claimed["fingerprint"]
    assert kwargs["recovery_context_messages"] == claimed["seed"]["context_messages"]


def test_fast_successor_completion_cannot_be_overwritten_by_late_start_result(
    receipt_store,
):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())

    def start(sid, prompt, **kwargs):
        journal_dir = receipt_store / "_turn_journal"
        journal_dir.mkdir(exist_ok=True)
        submitted = {
            "event": "submitted",
            "turn_id": "fast-turn",
            "stream_id": "fast-stream",
            "role": "user",
            "content": prompt,
            "attachments": kwargs["attachments"],
            "profile": "default",
            "source": receipts.SOURCE,
            "recovery_claim_token": kwargs["recovery_claim_token"],
            "recovery_fingerprint": kwargs["recovery_fingerprint"],
        }
        completed = {
            "event": "completed",
            "turn_id": "fast-turn",
            "stream_id": "fast-stream",
            "recovery_terminal_persisted": True,
        }
        (journal_dir / f"{sid}.jsonl").write_text(
            json.dumps(submitted) + "\n" + json.dumps(completed) + "\n",
            encoding="utf-8",
        )
        with config.STREAMS_LOCK:
            config.STREAMS["fast-stream"] = object()
        try:
            receipts.settle_recovery_after_durable_terminal(
                session,
                child_stream_id="fast-stream",
            )
        finally:
            with config.STREAMS_LOCK:
                config.STREAMS.pop("fast-stream", None)
        return {"session_id": sid, "stream_id": "fast-stream"}

    settled = receipts.settle_compression_recovery(
        "same-session",
        "parent-run",
        start=start,
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]

    assert settled["state"] == "discarded"
    assert saved["discarded_reason"] == "successor_settled"
    assert session.compression_recovery == {}


def test_explicit_launch_failure_remains_retryable(receipt_store):
    receipts.claim_compression_recovery(_session(), "parent-run", _seed())

    failed = receipts.settle_compression_recovery(
        "same-session",
        "parent-run",
        start=lambda *_args, **_kwargs: {
            "_status": 409,
            "recovery_launch_failed": True,
        },
    )
    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "recovered-run",
        }
    )

    assert failed["state"] == "claimed"
    assert recovered == 1


def test_prior_exact_launch_failure_does_not_poison_a_new_start_token(
    receipt_store,
    monkeypatch,
):
    from api.turn_journal import (
        append_turn_journal_event,
        append_turn_journal_event_for_stream,
    )

    claimed = receipts.claim_compression_recovery(
        _session(),
        "parent-run",
        _seed(),
    )
    _first, first_token = receipts._reserve_start(claimed["claim_key"])
    receipts._mark_launching(claimed["claim_key"], first_token)
    append_turn_journal_event(
        "same-session",
        {
            "event": "submitted",
            "stream_id": "first-failed-stream",
            "role": "user",
            "content": receipts.RECOVERY_CONTROL_PROMPT,
            "attachments": [],
            "profile": "default",
            "source": receipts.SOURCE,
            "recovery_claim_token": first_token,
            "recovery_fingerprint": claimed["fingerprint"],
        },
    )
    append_turn_journal_event_for_stream(
        "same-session",
        "first-failed-stream",
        {
            "event": "launch_failed",
            "source": receipts.SOURCE,
            "recovery_claim_token": first_token,
            "recovery_fingerprint": claimed["fingerprint"],
        },
    )
    monkeypatch.setattr(receipts, "_owner_is_live", lambda _receipt: False)

    _second, second_token = receipts._reserve_start(claimed["claim_key"])
    assert second_token and second_token != first_token
    receipts._mark_launching(claimed["claim_key"], second_token)
    starts = []

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda sid, _prompt, **_kwargs: (
            starts.append(sid)
            or {"session_id": sid, "stream_id": "recovered-stream"}
        )
    )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert recovered == 1
    assert starts == ["same-session"]
    assert saved["state"] == "started"


def test_start_exception_before_submission_remains_retryable(receipt_store):
    receipts.claim_compression_recovery(_session(), "parent-run", _seed())

    def fail_before_submit(*_args, **_kwargs):
        raise RuntimeError("start failed before journal admission")

    failed = receipts.settle_compression_recovery(
        "same-session", "parent-run", start=fail_before_submit
    )
    recovered = receipts.settle_compression_recovery(
        "same-session",
        "parent-run",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "retry-run",
        },
    )

    assert failed["state"] == "claimed"
    assert recovered["state"] == "started"


@pytest.mark.parametrize("phase", ["reserved", "launching"])
def test_dead_owner_without_submitted_successor_is_reclaimed(
    receipt_store, monkeypatch, phase
):
    claimed = receipts.claim_compression_recovery(
        _session(), "parent-run", _seed()
    )
    path = receipt_store / "_compression_recoveries.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    row = store["receipts"][claimed["claim_key"]]
    row.update(
        {
            "state": "starting",
            "launch_phase": phase,
            "owner_pid": os.getpid() + 1_000_000,
            "owner_start_token": "dead-owner",
            "start_token": "dead-start",
            "starting_at": 1.0,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(receipts, "_pid_is_alive", lambda _pid: False)

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "reclaimed-run",
        }
    )

    assert recovered == 1


def test_live_owner_reservation_is_not_stolen(receipt_store, monkeypatch):
    claimed = receipts.claim_compression_recovery(
        _session(), "parent-run", _seed()
    )
    path = receipt_store / "_compression_recoveries.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    row = store["receipts"][claimed["claim_key"]]
    row.update(
        {
            "state": "starting",
            "launch_phase": "reserved",
            "owner_pid": 123,
            "owner_start_token": "live-owner",
            "start_token": "live-start",
            "starting_at": 1.0,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(receipts, "_owner_is_live", lambda _row: True)
    starts = []

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda *_args, **_kwargs: starts.append(True)
    )

    assert recovered == 0
    assert starts == []


def test_same_process_dead_owner_thread_is_not_treated_as_live(monkeypatch):
    monkeypatch.setattr(receipts, "process_start_token", lambda _pid: "same-process")
    monkeypatch.setattr(receipts.threading, "enumerate", lambda: [])

    assert receipts._owner_is_live(
        {
            "owner_pid": os.getpid(),
            "owner_start_token": "same-process",
            "owner_thread": 987654321,
        }
    ) is False


def test_malformed_turn_journal_discards_dead_launch_instead_of_replaying(
    receipt_store, monkeypatch
):
    claimed = receipts.claim_compression_recovery(_session(), "parent-run", _seed())
    path = receipt_store / "_compression_recoveries.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    row = store["receipts"][claimed["claim_key"]]
    row.update(
        {
            "state": "starting",
            "launch_phase": "launching",
            "owner_pid": os.getpid() + 1_000_000,
            "owner_start_token": "dead-owner",
            "start_token": "dead-start",
            "starting_at": 1.0,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")
    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir()
    (journal_dir / "same-session.jsonl").write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(receipts, "_pid_is_alive", lambda _pid: False)
    starts = []

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda *_args, **_kwargs: starts.append(True)
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    recovered_session = Session.load("same-session")

    assert recovered == 0
    assert starts == []
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "ambiguous_submitted_successor"
    assert recovered_session.compression_recovery["phase"] == "blocked"
    assert recovered_session.compression_recovery["automatic_recovery"] is False
    assert (
        recovered_session.compression_recovery["reason"]
        == "ambiguous_submitted_successor"
    )


def test_conflicting_submitted_identity_discards_dead_launch(receipt_store, monkeypatch):
    claimed = receipts.claim_compression_recovery(_session(), "parent-run", _seed())
    path = receipt_store / "_compression_recoveries.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    row = store["receipts"][claimed["claim_key"]]
    row.update(
        {
            "state": "starting",
            "launch_phase": "launching",
            "owner_pid": os.getpid() + 1_000_000,
            "owner_start_token": "dead-owner",
            "start_token": "dead-start",
            "starting_at": 1.0,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")
    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir()
    event = {
        "event": "submitted",
        "turn_id": "turn-conflict",
        "stream_id": "stream-conflict",
        "source": receipts.SOURCE,
        "recovery_claim_token": "dead-start",
        "recovery_fingerprint": "0" * 64,
    }
    (journal_dir / "same-session.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(receipts, "_pid_is_alive", lambda _pid: False)

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda *_args, **_kwargs: pytest.fail("conflicting launch must not replay")
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]

    assert recovered == 0
    assert saved["state"] == "discarded"


def test_started_receipt_with_terminal_journal_clears_restart_presentation(
    receipt_store,
):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    started = receipts.settle_compression_recovery(
        "same-session",
        "parent-run",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "finished-stream",
        },
    )
    session.compression_recovery = receipts._session_phase_payload(
        started,
        "running",
    )
    session.save(touch_updated_at=False)
    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir(exist_ok=True)
    submitted = {
        "event": "submitted",
        "turn_id": "finished-turn",
        "stream_id": "finished-stream",
        "role": "user",
        "content": receipts.RECOVERY_CONTROL_PROMPT,
        "attachments": [],
        "profile": "default",
        "source": receipts.SOURCE,
        "recovery_claim_token": started["completed_start_token"],
        "recovery_fingerprint": claimed["fingerprint"],
    }
    completed = {
        "event": "completed",
        "turn_id": "finished-turn",
        "stream_id": "finished-stream",
        "source": receipts.SOURCE,
        "recovery_terminal_persisted": True,
    }
    (journal_dir / "same-session.jsonl").write_text(
        json.dumps(submitted) + "\n" + json.dumps(completed) + "\n",
        encoding="utf-8",
    )

    recovered = receipts.recover_pending_compression_recoveries()
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    recovered_session = Session.load("same-session")

    assert recovered == 0
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "successor_settled"
    assert recovered_session.compression_recovery == {}


def test_durable_rotated_successor_settles_source_receipt_and_canonical_presentation(
    receipt_store,
):
    source = _session(sid="rotation-source")
    source.save(touch_updated_at=False)
    seed = _seed(sid=source.session_id, parent="rotation-parent")
    claimed = receipts.claim_compression_recovery(
        source,
        "rotation-parent",
        seed,
    )
    started = receipts.settle_compression_recovery(
        source.session_id,
        "rotation-parent",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "rotated-child-stream",
        },
    )
    canonical = _session(sid="rotation-canonical")
    canonical.parent_session_id = source.session_id
    canonical.messages.append(
        {"role": "assistant", "content": "Durably completed after rotation."}
    )
    canonical.compression_recovery = receipts._session_phase_payload(
        started,
        "running",
    )
    canonical.save(touch_updated_at=False)
    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir(exist_ok=True)
    submitted = {
        "event": "submitted",
        "turn_id": "rotated-child-turn",
        "stream_id": "rotated-child-stream",
        "role": "user",
        "content": receipts.RECOVERY_CONTROL_PROMPT,
        "attachments": [],
        "profile": "default",
        "source": receipts.SOURCE,
        "recovery_claim_token": started["completed_start_token"],
        "recovery_fingerprint": claimed["fingerprint"],
    }
    completed = {
        "event": "completed",
        "turn_id": "rotated-child-turn",
        "stream_id": "rotated-child-stream",
        "source": receipts.SOURCE,
        "recovery_terminal_persisted": True,
    }
    (journal_dir / f"{source.session_id}.jsonl").write_text(
        json.dumps(submitted) + "\n" + json.dumps(completed) + "\n",
        encoding="utf-8",
    )

    settled = receipts.settle_recovery_after_durable_terminal(
        canonical,
        child_stream_id="rotated-child-stream",
    )
    saved_receipt = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    saved_canonical = Session.load(canonical.session_id)

    assert settled is not None
    assert saved_receipt["state"] == "discarded"
    assert saved_receipt["discarded_reason"] == "successor_settled"
    assert saved_canonical.compression_recovery == {}


def test_rotated_terminal_presentation_save_failure_repairs_canonical_on_restart(
    receipt_store,
    monkeypatch,
):
    source = _session(sid="rotation-crash-source")
    source.save(touch_updated_at=False)
    seed = _seed(sid=source.session_id, parent="rotation-crash-parent")
    claimed = receipts.claim_compression_recovery(
        source,
        "rotation-crash-parent",
        seed,
    )
    started = receipts.settle_compression_recovery(
        source.session_id,
        "rotation-crash-parent",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "rotated-crash-stream",
        },
    )
    canonical = _session(sid="rotation-crash-canonical")
    canonical.parent_session_id = source.session_id
    canonical.messages.append(
        {"role": "assistant", "content": "Durably completed before phase cleanup."}
    )
    canonical.compression_recovery = receipts._session_phase_payload(
        started,
        "running",
    )
    canonical.save(touch_updated_at=False)
    source_before_restart_repair = source.path.read_bytes()

    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir(exist_ok=True)
    submitted = {
        "event": "submitted",
        "turn_id": "rotated-crash-turn",
        "stream_id": "rotated-crash-stream",
        "role": "user",
        "content": receipts.RECOVERY_CONTROL_PROMPT,
        "attachments": [],
        "profile": "default",
        "source": receipts.SOURCE,
        "recovery_claim_token": started["completed_start_token"],
        "recovery_fingerprint": claimed["fingerprint"],
    }
    completed = {
        "event": "completed",
        "turn_id": "rotated-crash-turn",
        "stream_id": "rotated-crash-stream",
        "source": receipts.SOURCE,
        "recovery_terminal_persisted": True,
    }
    (journal_dir / f"{source.session_id}.jsonl").write_text(
        json.dumps(submitted) + "\n" + json.dumps(completed) + "\n",
        encoding="utf-8",
    )

    original_save = Session.save
    failed_once = False

    def fail_first_canonical_phase_clear(self, *args, **kwargs):
        nonlocal failed_once
        if (
            self.session_id == canonical.session_id
            and self.compression_recovery == {}
            and not failed_once
        ):
            failed_once = True
            raise OSError("simulated canonical presentation save failure")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_first_canonical_phase_clear)

    settled = receipts.settle_recovery_after_durable_terminal(
        canonical,
        child_stream_id="rotated-crash-stream",
    )
    terminal_receipt = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert settled is not None
    assert failed_once is True
    assert terminal_receipt["state"] == "discarded"
    assert terminal_receipt["discarded_reason"] == "successor_settled"
    assert "seed" not in terminal_receipt
    assert Session.load(canonical.session_id).compression_recovery["phase"] == "running"

    # Model a fresh process: reconciliation must use durable tombstone identity,
    # not the already-mutated in-memory canonical Session object.
    models.SESSIONS.clear()
    recovered = receipts.recover_pending_compression_recoveries()

    repaired_canonical = Session.load(canonical.session_id)
    assert recovered == 0
    assert repaired_canonical.compression_recovery == {}
    assert source.path.read_bytes() == source_before_restart_repair
    repaired_receipt = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert repaired_receipt["state"] == "discarded"
    assert repaired_receipt["discarded_reason"] == "successor_settled"
    assert "seed" not in repaired_receipt


def test_rotated_terminal_before_settlement_repairs_canonical_from_journal_on_restart(
    receipt_store,
):
    source = _session(sid="rotation-presolve-source")
    source.save(touch_updated_at=False)
    seed = _seed(sid=source.session_id, parent="rotation-presolve-parent")
    claimed = receipts.claim_compression_recovery(
        source,
        "rotation-presolve-parent",
        seed,
    )
    started = receipts.settle_compression_recovery(
        source.session_id,
        "rotation-presolve-parent",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "rotated-presolve-stream",
        },
    )
    canonical = _session(sid="rotation-presolve-canonical")
    canonical.parent_session_id = source.session_id
    canonical.messages.append(
        {"role": "assistant", "content": "Terminal transcript committed before crash."}
    )
    canonical.compression_recovery = receipts._session_phase_payload(
        started,
        "running",
    )
    canonical.save(touch_updated_at=False)
    source_before_restart_repair = source.path.read_bytes()

    journal_dir = receipt_store / "_turn_journal"
    journal_dir.mkdir(exist_ok=True)
    submitted = {
        "event": "submitted",
        "turn_id": "rotated-presolve-turn",
        "stream_id": "rotated-presolve-stream",
        "role": "user",
        "content": receipts.RECOVERY_CONTROL_PROMPT,
        "attachments": [],
        "profile": "default",
        "source": receipts.SOURCE,
        "recovery_claim_token": started["completed_start_token"],
        "recovery_fingerprint": claimed["fingerprint"],
    }
    completed = {
        "event": "completed",
        "turn_id": "rotated-presolve-turn",
        "stream_id": "rotated-presolve-stream",
        "source": receipts.SOURCE,
        "recovery_terminal_persisted": True,
        "recovery_presentation_session_id": canonical.session_id,
    }
    (journal_dir / f"{source.session_id}.jsonl").write_text(
        json.dumps(submitted) + "\n" + json.dumps(completed) + "\n",
        encoding="utf-8",
    )

    # Crash before settle_recovery_after_durable_terminal(): only the started
    # source receipt, canonical transcript, and exact terminal journal survive.
    models.SESSIONS.clear()
    recovered = receipts.recover_pending_compression_recoveries()

    repaired_canonical = Session.load(canonical.session_id)
    repaired_receipt = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert recovered == 0
    assert repaired_canonical.compression_recovery == {}
    assert source.path.read_bytes() == source_before_restart_repair
    assert repaired_receipt["state"] == "discarded"
    assert repaired_receipt["discarded_reason"] == "successor_settled"
    assert repaired_receipt["presentation_session_id"] == canonical.session_id
    assert "seed" not in repaired_receipt


def test_reserved_seed_revalidates_attachment_at_admission(
    receipt_store,
    tmp_path,
):
    attached = tmp_path / "evidence.txt"
    attached.write_text("evidence", encoding="utf-8")
    attachment = {
        "name": "evidence.txt",
        "path": str(attached),
        "mime": "text/plain",
    }
    session = _session()
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-run",
        _seed(attachments=[attachment]),
    )
    _reserved, token = receipts._reserve_start(claimed["claim_key"])
    attached.unlink()

    with pytest.raises(
        receipts.CompressionRecoveryAdmissionBlocked,
        match="recovery_attachment_unavailable",
    ):
        receipts.reserved_recovery_seed(
            session,
            claim_token=token,
            fingerprint=claimed["fingerprint"],
            context_messages=claimed["seed"]["context_messages"],
            attachments=[attachment],
        )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "recovery_attachment_unavailable"


def test_human_supersession_discards_only_claimed_receipt(receipt_store):
    session = _session()
    receipts.claim_compression_recovery(session, "parent-run", _seed())

    superseded = receipts.supersede_pending_compression_recovery(session)
    repeated = receipts.supersede_pending_compression_recovery(session)

    assert superseded["state"] == "discarded"
    assert superseded["discarded_reason"] == "superseded_by_user"
    assert repeated is None
    assert session.context_messages == _seed()["context_messages"]
    assert session.compression_recovery == {}
    assert session.recommended_recovery_action is None


def test_user_owned_session_mutation_retires_pending_recovery(receipt_store):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    session.save(touch_updated_at=False)

    retired = receipts.retire_session_compression_recoveries(
        session,
        reason="superseded_by_user",
    )
    session.save(touch_updated_at=False)

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    recovered_session = Session.load("same-session")
    assert retired == 1
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "superseded_by_user"
    assert recovered_session.compression_recovery == {}
    assert receipts.session_has_pending_compression_recovery("same-session") is False


def test_nonblocking_terminal_receipt_keeps_only_compact_idempotency_tombstone(
    receipt_store,
):
    session = _session()
    seed = _seed()
    claimed = receipts.claim_compression_recovery(session, "parent-run", seed)

    receipts.retire_session_compression_recoveries(
        session,
        reason="superseded_by_user",
    )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    duplicate = receipts.claim_compression_recovery(
        session,
        "parent-run",
        seed,
    )
    assert saved["state"] == "discarded"
    assert saved["fingerprint"] == claimed["fingerprint"]
    assert "seed" not in saved
    assert duplicate["claim_disposition"] == "settled"


@pytest.mark.skipif(os.name == "nt", reason="directory fsync requires POSIX")
def test_unmanaged_receipt_publication_fsyncs_parent_directory(
    receipt_store,
    monkeypatch,
):
    original_fsync = os.fsync
    fsynced_directory = []

    def record_fsync(descriptor):
        fsynced_directory.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return original_fsync(descriptor)

    monkeypatch.setattr(receipts.os, "fsync", record_fsync)
    receipts.claim_compression_recovery(_session(), "parent-run", _seed())

    assert True in fsynced_directory


@pytest.mark.skipif(os.name == "nt", reason="strict managed store requires POSIX")
def test_deleted_session_receipt_is_terminal_for_managed_startup(receipt_store):
    receipt_store.chmod(0o700)
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    session.save(touch_updated_at=False)
    receipts.retire_session_compression_recoveries(
        session.session_id,
        reason="session_deleted",
    )
    session.path.unlink()
    transaction = "managed-deleted-compression-session-0001"
    manifest = "d" * 64

    managed = receipts.recover_managed_compression_recoveries_exact(
        transaction_id=transaction,
        manifest_sha256=manifest,
        start=lambda *_args, **_kwargs: pytest.fail("deleted session must not replay"),
    )
    verified = receipts.verify_managed_compression_recoveries_exact(
        managed,
        transaction_id=transaction,
        manifest_sha256=manifest,
    )
    saved = receipts.load_receipts()["receipts"]

    assert managed.outcome.value == "ABSENT", managed.errors
    assert verified.outcome.value == "ABSENT", verified.errors
    assert claimed["claim_key"] not in saved


def test_human_supersession_reserves_claim_until_worker_start(receipt_store):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())

    reserved = receipts.reserve_human_compression_supersession(
        session,
        attachments=[],
    )
    during = receipts.load_receipts()["receipts"][claimed["claim_key"]]

    assert during["state"] == "starting"
    assert during["launch_mode"] == "human_supersession"
    assert during["start_token"] == reserved["start_token"]
    assert session.context_messages == _seed()["context_messages"]
    assert session.compression_recovery == {}

    finished = receipts.finish_human_compression_supersession(
        claimed["claim_key"],
        reserved["start_token"],
    )

    assert finished["state"] == "discarded"
    assert finished["discarded_reason"] == "superseded_by_user"


def test_failed_human_handoff_commit_preserves_exact_live_webui_worker(
    receipt_store,
    monkeypatch,
):
    from api.turn_journal import append_turn_journal_event

    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    reserved = receipts.reserve_human_compression_supersession(
        session,
        attachments=[],
    )
    stream_id = "live-human-supersession"
    append_turn_journal_event(
        session.session_id,
        {
            "event": "submitted",
            "turn_id": "live-human-turn",
            "stream_id": stream_id,
            "role": "user",
            "content": "My newer instruction",
            "attachments": [],
            "profile": "default",
            "source": "webui",
            "recovery_claim_token": reserved["start_token"],
            "recovery_fingerprint": reserved["fingerprint"],
        },
    )
    monkeypatch.setattr(receipts, "_owner_is_live", lambda _receipt: False)
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = object()
    try:
        with pytest.raises(
            receipts.CompressionRecoveryInProgress,
            match="worker is already running",
        ):
            receipts.reserve_human_compression_supersession(
                session,
                attachments=[],
            )
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert saved["state"] == "starting"
    assert saved["launch_mode"] == "human_supersession"
    assert saved["start_token"] == reserved["start_token"]


def test_dead_human_supersession_reservation_blocks_without_replaying(
    receipt_store,
    monkeypatch,
):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    receipts.reserve_human_compression_supersession(session, attachments=[])
    session.save(touch_updated_at=False)
    monkeypatch.setattr(receipts, "_owner_is_live", lambda _receipt: False)

    recovered = receipts.recover_pending_compression_recoveries(
        start=lambda *_args, **_kwargs: pytest.fail("human intent must not replay automatically")
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    recovered_session = Session.load("same-session")

    assert recovered == 0
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "ambiguous_human_supersession"
    assert recovered_session.compression_recovery["phase"] == "blocked"


def test_human_supersession_merges_attachments_before_discard(receipt_store, tmp_path):
    recovered = tmp_path / "recovered.txt"
    recovered.write_text("recovered", encoding="utf-8")
    fresh = tmp_path / "fresh.txt"
    fresh.write_text("fresh", encoding="utf-8")
    recovered_attachment = {
        "name": "recovered.txt",
        "path": str(recovered),
        "mime": "text/plain",
    }
    fresh_attachment = {
        "name": "fresh.txt",
        "path": str(fresh),
        "mime": "text/plain",
    }
    session = _session()
    receipts.claim_compression_recovery(
        session,
        "parent-run",
        _seed(attachments=[recovered_attachment]),
    )

    superseded = receipts.supersede_pending_compression_recovery(
        session,
        attachments=[fresh_attachment],
    )

    assert superseded["merged_attachments"] == [
        recovered_attachment,
        fresh_attachment,
    ]
    assert receipts.load_receipts()["receipts"][superseded["claim_key"]]["state"] == "discarded"


def test_human_attachment_conflict_keeps_recovery_claim_atomic(receipt_store, tmp_path):
    recovered = tmp_path / "recovered.txt"
    recovered.write_text("recovered", encoding="utf-8")
    conflicting = tmp_path / "conflicting.txt"
    conflicting.write_text("conflicting", encoding="utf-8")
    recovered_attachment = {
        "name": "evidence.txt",
        "path": str(recovered),
        "mime": "text/plain",
    }
    session = _session()
    original_context = list(session.context_messages)
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-run",
        _seed(attachments=[recovered_attachment]),
    )

    with pytest.raises(receipts.CompressionRecoverySupersessionConflict):
        receipts.supersede_pending_compression_recovery(
            session,
            attachments=[
                {
                    "name": "evidence.txt",
                    "path": str(conflicting),
                    "mime": "text/plain",
                }
            ],
        )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert saved["state"] == "claimed"
    assert session.context_messages == original_context


def test_human_attachment_merge_over_limit_keeps_recovery_claim_atomic(
    receipt_store,
    tmp_path,
):
    recovered_attachments = []
    for index in range(20):
        path = tmp_path / f"recovered-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        recovered_attachments.append(
            {"name": path.name, "path": str(path), "mime": "text/plain"}
        )
    fresh = tmp_path / "fresh.txt"
    fresh.write_text("fresh", encoding="utf-8")
    session = _session()
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-run",
        _seed(attachments=recovered_attachments),
    )

    with pytest.raises(RuntimeError, match="attachment"):
        receipts.supersede_pending_compression_recovery(
            session,
            attachments=[
                {"name": fresh.name, "path": str(fresh), "mime": "text/plain"}
            ],
        )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert saved["state"] == "claimed"


def test_attachmentless_sync_supersession_keeps_attached_claim_pending(
    receipt_store,
    tmp_path,
):
    recovered = tmp_path / "recovered.txt"
    recovered.write_text("recovered", encoding="utf-8")
    attachment = {
        "name": recovered.name,
        "path": str(recovered),
        "mime": "text/plain",
    }
    session = _session()
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-run",
        _seed(attachments=[attachment]),
    )

    with pytest.raises(receipts.CompressionRecoverySupersessionConflict):
        receipts.supersede_pending_compression_recovery(
            session,
            attachments_supported=False,
        )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert saved["state"] == "claimed"


def test_human_cannot_silently_overtake_a_starting_recovery(
    receipt_store,
    monkeypatch,
):
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    path = receipt_store / "_compression_recoveries.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    store["receipts"][claimed["claim_key"]].update(
        {
            "state": "starting",
            "launch_phase": "reserved",
            "owner_pid": os.getpid(),
            "owner_start_token": "live-owner",
            "start_token": "live-start",
            "starting_at": 1.0,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(receipts, "_owner_is_live", lambda _receipt: True)

    with pytest.raises(RuntimeError, match="already starting"):
        receipts.supersede_pending_compression_recovery(session)


@pytest.mark.skipif(os.name == "nt", reason="strict managed store requires POSIX")
def test_managed_recovery_and_verification_use_exact_receipt_authority(receipt_store):
    receipt_store.chmod(0o700)
    receipts.claim_compression_recovery(_session(), "parent-run", _seed())
    transaction = "managed-compression-recovery-transaction-0001"
    manifest = "a" * 64

    managed = receipts.recover_managed_compression_recoveries_exact(
        transaction_id=transaction,
        manifest_sha256=manifest,
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "managed-stream",
        },
    )
    verified = receipts.verify_managed_compression_recoveries_exact(
        managed,
        transaction_id=transaction,
        manifest_sha256=manifest,
    )

    assert managed.outcome.value == "COMPLETE", managed.errors
    assert verified.outcome.value == "COMPLETE"


@pytest.mark.skipif(os.name == "nt", reason="strict managed store requires POSIX")
def test_managed_restart_blocks_unproved_started_successor(receipt_store):
    receipt_store.chmod(0o700)
    session = _session()
    claimed = receipts.claim_compression_recovery(session, "parent-run", _seed())
    started = receipts.settle_compression_recovery(
        "same-session",
        "parent-run",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "crashed-successor",
        },
    )
    session.compression_recovery = receipts._session_phase_payload(started, "running")
    session.save(touch_updated_at=False)
    transaction = "managed-compression-restart-transaction-0001"
    manifest = "b" * 64

    managed = receipts.recover_managed_compression_recoveries_exact(
        transaction_id=transaction,
        manifest_sha256=manifest,
        start=lambda *_args, **_kwargs: pytest.fail("ambiguous start must not replay"),
    )
    verified = receipts.verify_managed_compression_recoveries_exact(
        managed,
        transaction_id=transaction,
        manifest_sha256=manifest,
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    recovered_session = Session.load("same-session")

    assert managed.outcome.value == "COMPLETE", managed.errors
    assert verified.outcome.value == "COMPLETE", verified.errors
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "ambiguous_started_successor"
    assert recovered_session.compression_recovery["phase"] == "blocked"


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"version": 99, "receipts": {}}),
        json.dumps({"version": 1, "receipts": []}),
    ],
)
def test_store_corruption_fails_closed_without_overwrite(receipt_store, raw):
    path = receipt_store / "_compression_recoveries.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(RuntimeError, match="compression recovery receipt"):
        receipts.claim_compression_recovery(_session(), "parent-run", _seed())

    assert path.read_text(encoding="utf-8") == raw
