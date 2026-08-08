"""User-owned session mutations retire transparent-recovery ownership.

These are behavioral integration tests: the HTTP cases execute the real route
handlers and the slash-command cases execute the real session operations.  All
session and recovery state is confined to ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from io import BytesIO
from types import SimpleNamespace

import pytest


def _message(role: str, content: str, timestamp: float) -> dict:
    return {
        "role": role,
        "content": content,
        "timestamp": timestamp,
    }


@pytest.fixture
def isolated_session_state(tmp_path, monkeypatch):
    from api import config, models, routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_index = session_dir / "_index.json"

    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_index)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_index)
    models.SESSIONS.clear()

    # Keep route tests hermetic: these seams publish metadata or touch provider,
    # terminal, attachment, and CLI state that is unrelated to receipt ownership.
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda _s: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_clear_session_list_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_k: {})
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", lambda _sid: None)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)

    import api.state_sync as state_sync
    import api.terminal as terminal
    import api.upload as upload

    monkeypatch.setattr(state_sync, "clear_session_title", lambda *_a, **_k: True)
    monkeypatch.setattr(terminal, "close_terminal", lambda _sid: None)
    monkeypatch.setattr(
        upload,
        "_session_attachment_dir",
        lambda sid: tmp_path / "attachments" / sid,
    )

    yield SimpleNamespace(
        session_dir=session_dir,
        workspace=tmp_path / "workspace",
    )
    models.SESSIONS.clear()


def _seed_session(state, *, sid: str):
    from api.models import Session

    messages = [
        _message("user", "first request", 1.0),
        _message("assistant", "first answer", 2.0),
        _message("user", "unfinished request", 3.0),
        _message("assistant", "partial answer", 4.0),
    ]
    session = Session(
        session_id=sid,
        title="Recovery mutation test",
        profile="default",
        workspace=str(state.workspace),
        messages=list(messages),
        context_messages=list(messages),
    )
    session.save()
    return session


def _claim_recovery(
    session,
    *,
    parent_run_id: str = "parent-run",
    failed_request: str = "Finish the unfinished request.",
    attachments: list[dict] | None = None,
) -> dict:
    from api.compression_recovery import _recovery_fingerprint
    from api.compression_recovery_receipts import claim_compression_recovery

    context_messages = [
        {"role": "assistant", "content": "Trusted bounded checkpoint."},
        {"role": "user", "content": failed_request},
    ]
    attachments = list(attachments or [])
    seed = {
        "session_id": session.session_id,
        "parent_run_id": parent_run_id,
        "context_messages": context_messages,
        "attachments": attachments,
        "trust_source": "assistant_checkpoint",
        "fingerprint": "",
    }
    seed["fingerprint"] = _recovery_fingerprint(
        session_id=session.session_id,
        parent_run_id=parent_run_id,
        context_messages=context_messages,
        attachments=attachments,
    )
    return claim_compression_recovery(session, parent_run_id, seed)


def _post(monkeypatch, path: str, body: dict) -> dict:
    from api import routes

    encoded = json.dumps(body).encode("utf-8")
    captured: dict = {}

    def capture(_handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status, headers=extra_headers)
        return True

    monkeypatch.setattr(routes, "j", capture)
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(encoded))},
        rfile=BytesIO(encoded),
    )
    routes.handle_post(handler, SimpleNamespace(path=path))
    assert captured, f"{path} did not emit a response"
    return captured


def _assert_superseded(claim: dict, sid: str) -> None:
    from api.compression_recovery_receipts import (
        load_receipts,
        session_has_pending_compression_recovery,
    )

    receipt = load_receipts()["receipts"][claim["claim_key"]]
    assert receipt["state"] == "discarded"
    assert receipt["discarded_reason"] == "superseded_by_user"
    assert session_has_pending_compression_recovery(sid) is False


def test_route_change_retires_pending_recovery(
    isolated_session_state,
    monkeypatch,
):
    from api import routes
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="mutation-update")
    claim = _claim_recovery(session)
    new_workspace = isolated_session_state.workspace / "changed"
    monkeypatch.setattr(
        routes,
        "resolve_trusted_workspace",
        lambda raw: new_workspace if str(raw) == str(new_workspace) else raw,
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)

    response = _post(
        monkeypatch,
        "/api/session/update",
        {"session_id": session.session_id, "workspace": str(new_workspace)},
    )

    assert response["status"] == 200
    assert response["payload"]["session"]["workspace"] == str(new_workspace)
    _assert_superseded(claim, session.session_id)
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert persisted.compression_recovery == {}


def test_clear_retires_pending_recovery(isolated_session_state, monkeypatch):
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="mutation-clear")
    claim = _claim_recovery(session)

    response = _post(
        monkeypatch,
        "/api/session/clear",
        {"session_id": session.session_id},
    )

    assert response["status"] == 200
    assert response["payload"]["ok"] is True
    _assert_superseded(claim, session.session_id)
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert persisted.messages == []
    assert persisted.compression_recovery == {}


def test_truncate_retires_pending_recovery(isolated_session_state, monkeypatch):
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="mutation-truncate")
    claim = _claim_recovery(session)

    response = _post(
        monkeypatch,
        "/api/session/truncate",
        {"session_id": session.session_id, "keep_count": 2},
    )

    assert response["status"] == 200
    assert response["payload"]["ok"] is True
    _assert_superseded(claim, session.session_id)
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert len(persisted.messages) == 2
    assert persisted.compression_recovery == {}


@pytest.mark.skipif(os.name == "nt", reason="strict managed store requires POSIX")
def test_delete_purges_recovery_seed_and_is_terminal_for_managed_startup(
    isolated_session_state,
    monkeypatch,
):
    from api.compression_recovery_receipts import (
        load_receipts,
        recover_managed_compression_recoveries_exact,
        session_has_pending_compression_recovery,
        verify_managed_compression_recoveries_exact,
    )
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="mutation-delete")
    secret_request = "DELETE-ME failed customer request with private details"
    secret_attachment = isolated_session_state.session_dir.parent / "private-proof.txt"
    secret_attachment.write_text("private proof", encoding="utf-8")
    claim = _claim_recovery(
        session,
        failed_request=secret_request,
        attachments=[
            {
                "name": secret_attachment.name,
                "path": str(secret_attachment),
                "mime": "text/plain",
            }
        ],
    )

    response = _post(
        monkeypatch,
        "/api/session/delete",
        {"session_id": session.session_id},
    )

    assert response["status"] == 200
    assert response["payload"]["ok"] is True
    receipts_after_delete = load_receipts()["receipts"]
    assert claim["claim_key"] not in receipts_after_delete
    assert session_has_pending_compression_recovery(session.session_id) is False
    assert Session.load(session.session_id) is None
    raw_store = (
        isolated_session_state.session_dir / "_compression_recoveries.json"
    ).read_text(encoding="utf-8")
    assert secret_request not in raw_store
    assert str(secret_attachment) not in raw_store

    isolated_session_state.session_dir.chmod(0o700)
    transaction_id = "managed-deleted-seed-purge-0001"
    manifest_sha256 = "a" * 64
    managed = recover_managed_compression_recoveries_exact(
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
        start=lambda *_a, **_k: pytest.fail("deleted recovery must never replay"),
    )
    verified = verify_managed_compression_recoveries_exact(
        managed,
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
    )
    assert managed.outcome.value == "ABSENT", managed.errors
    assert verified.outcome.value == "ABSENT", verified.errors


def _seed_receipt_retention_state(state):
    """Create two evictable rows plus one row in every protected state."""
    from api import compression_recovery_receipts as receipts

    def claimed(suffix: str):
        session = _seed_session(state, sid=f"retention-{suffix}")
        return session, _claim_recovery(
            session,
            parent_run_id=f"parent-{suffix}",
            failed_request=f"Retain or evict receipt {suffix} exactly.",
        )

    safe_old_session, safe_old = claimed("safe-old")
    receipts.retire_session_compression_recoveries(
        safe_old_session,
        reason="superseded_by_user",
    )
    safe_new_session, safe_new = claimed("safe-new")
    receipts.retire_session_compression_recoveries(
        safe_new_session,
        reason="superseded_by_user",
    )

    _claimed_session, protected_claimed = claimed("claimed")
    _starting_session, protected_starting = claimed("starting")
    receipts._reserve_start(protected_starting["claim_key"])

    _started_session, protected_started = claimed("started")
    protected_started = receipts.settle_compression_recovery(
        protected_started["session_id"],
        protected_started["parent_run_id"],
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "retention-started-stream",
        },
    )
    assert protected_started is not None
    assert protected_started["state"] == "started"

    _blocked_session, protected_blocked = claimed("blocked-ambiguity")
    _reserved, blocked_token = receipts._reserve_start(
        protected_blocked["claim_key"]
    )
    assert blocked_token
    receipts._block_reserved_recovery_admission(
        protected_blocked,
        blocked_token,
        reason="ambiguous_submitted_successor",
    )

    # Make eviction order deterministic rather than relying on timer precision.
    with receipts._store_lock():
        store = receipts._load_store()
        store["receipts"][safe_old["claim_key"]].update(
            discarded_at=1.0,
            updated_at=1.0,
        )
        store["receipts"][safe_new["claim_key"]].update(
            discarded_at=2.0,
            updated_at=2.0,
        )
        receipts._save_store(store)

    return {
        "safe_old": safe_old,
        "safe_new": safe_new,
        "claimed": protected_claimed,
        "starting": protected_starting,
        "started": protected_started,
        "blocked": protected_blocked,
    }


@pytest.mark.parametrize("boundary", ["count", "bytes"])
def test_claim_admission_prunes_only_oldest_nonblocking_discarded_receipt(
    isolated_session_state,
    monkeypatch,
    boundary,
):
    from api import compression_recovery_receipts as receipts

    rows = _seed_receipt_retention_state(isolated_session_state)
    receipt_path = isolated_session_state.session_dir / "_compression_recoveries.json"
    if boundary == "count":
        monkeypatch.setattr(receipts, "_MAX_RECEIPTS", 6)
    else:
        # Existing state remains readable, but one same-sized admission cannot
        # fit unless at least the oldest evictable row is removed first.
        monkeypatch.setattr(
            receipts,
            "_MAX_STORE_BYTES",
            receipt_path.stat().st_size + 768,
        )

    new_session = _seed_session(isolated_session_state, sid=f"retention-new-{boundary}")
    admitted = _claim_recovery(
        new_session,
        parent_run_id=f"parent-new-{boundary}",
        failed_request=f"Admit the new {boundary} boundary recovery.",
    )

    saved = receipts.load_receipts()["receipts"]
    assert admitted["claim_key"] in saved
    assert rows["safe_old"]["claim_key"] not in saved
    assert rows["safe_new"]["claim_key"] in saved
    for protected in ("claimed", "starting", "started", "blocked"):
        assert rows[protected]["claim_key"] in saved
    assert saved[rows["claimed"]["claim_key"]]["state"] == "claimed"
    assert saved[rows["starting"]["claim_key"]]["state"] == "starting"
    assert saved[rows["started"]["claim_key"]]["state"] == "started"
    blocked = saved[rows["blocked"]["claim_key"]]
    assert blocked["state"] == "discarded"
    assert blocked["discarded_reason"] == "ambiguous_submitted_successor"


@pytest.mark.parametrize("operation", ["retry_last", "undo_last"])
def test_retry_and_undo_retire_pending_recovery(
    isolated_session_state,
    operation,
):
    from api import session_ops
    from api.models import Session

    session = _seed_session(
        isolated_session_state,
        sid=f"mutation-{operation}",
    )
    claim = _claim_recovery(session)

    result = getattr(session_ops, operation)(session.session_id)

    assert result["removed_count"] == 2
    _assert_superseded(claim, session.session_id)
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert [row["content"] for row in persisted.messages] == [
        "first request",
        "first answer",
    ]
    assert persisted.compression_recovery == {}


def _mark_started_recovery_without_terminal_proof(
    session,
    claim: dict,
    *,
    persist_terminal_transcript: bool,
) -> dict:
    """Model the save/journal failure window after successor admission.

    The exact submitted identity is durable, but no terminal event carrying
    ``recovery_terminal_persisted=True`` exists.  Depending on the parameter,
    the final assistant transcript either made it to the sidecar before the
    terminal-journal append failed, or its own save failed first.
    """
    from api import compression_recovery_receipts as receipts
    from api.turn_journal import append_turn_journal_event

    stream_id = f"recovery-{session.session_id}"
    turn_id = f"turn-{session.session_id}"

    def admitted_start(sid, prompt, **kwargs):
        append_turn_journal_event(
            sid,
            {
                "event": "submitted",
                "turn_id": turn_id,
                "stream_id": stream_id,
                "role": "user",
                "content": prompt,
                "attachments": kwargs["attachments"],
                "profile": "default",
                "source": receipts.SOURCE,
                "recovery_claim_token": kwargs["recovery_claim_token"],
                "recovery_fingerprint": kwargs["recovery_fingerprint"],
            },
        )
        return {"session_id": sid, "stream_id": stream_id, "turn_id": turn_id}

    started = receipts.settle_compression_recovery(
        session.session_id,
        claim["parent_run_id"],
        start=admitted_start,
    )
    assert started is not None
    assert started["state"] == "started"

    # Admission installed this bounded same-task context before the successor
    # ran. Model its terminal writeback state without inventing terminal proof.
    session.context_messages = list(claim["seed"]["context_messages"])
    if persist_terminal_transcript:
        completed = _message(
            "assistant",
            "The recovered request completed before terminal journaling failed.",
            5.0,
        )
        session.messages.append(completed)
        session.context_messages.append(completed)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_user_source = None
    session.compression_recovery = receipts._session_phase_payload(
        started,
        "running",
    )
    session.save(touch_updated_at=False)
    return started


class _ObservedNoopThread:
    """A worker-start double that satisfies the production handoff fence."""

    def __init__(self, *args, **kwargs):
        self.ident = None

    def start(self):
        self.ident = 424242

    def is_alive(self):
        return False


@pytest.mark.parametrize(
    "persist_terminal_transcript",
    [False, True],
    ids=["transcript-save-failed", "terminal-journal-failed"],
)
def test_stale_started_recovery_does_not_block_an_in_process_human_send(
    isolated_session_state,
    monkeypatch,
    persist_terminal_transcript,
):
    """A dead successor residue is reconciled and superseded in one send.

    A started receipt is not, by itself, live-worker evidence. If neither the
    stream registry nor ACTIVE_RUNS owns the successor after its terminal
    persistence window, an ordinary human turn must be able to retire the
    resulting blocker and continue the same task without a 503/retry loop.
    """
    from api import compression_recovery_receipts as receipts
    from api import config, routes

    session = _seed_session(
        isolated_session_state,
        sid=f"started-stale-{int(persist_terminal_transcript)}",
    )
    claim = _claim_recovery(session, parent_run_id="started-stale-parent")
    started = _mark_started_recovery_without_terminal_proof(
        session,
        claim,
        persist_terminal_transcript=persist_terminal_transcript,
    )
    recovery_stream_id = started["child_stream_id"]
    with config.STREAMS_LOCK:
        config.STREAMS.pop(recovery_stream_id, None)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(recovery_stream_id, None)

    monkeypatch.setattr(routes.threading, "Thread", _ObservedNoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: None,
    )
    human_request = "Continue this same task with my newer instruction."

    response = routes._start_chat_stream_for_session(
        session,
        msg=human_request,
        attachments=[],
        workspace=str(isolated_session_state.workspace),
        model="gpt-4o",
        model_provider=None,
        source="webui",
        external_runtime_owned=False,
    )

    try:
        saved = receipts.load_receipts()["receipts"][claim["claim_key"]]
        assert response.get("_status", 200) < 400, response
        assert saved["state"] == "discarded"
        assert saved["discarded_reason"] == "superseded_by_user"
        assert session.pending_user_message == human_request
        assert session.pending_user_source == "webui"
    finally:
        human_stream_id = response.get("stream_id")
        if human_stream_id:
            with config.STREAMS_LOCK:
                config.STREAMS.pop(human_stream_id, None)
            routes.unregister_stream_owner(human_stream_id)


def test_live_started_recovery_keeps_its_owner_without_returning_503(
    isolated_session_state,
    monkeypatch,
):
    """Live stream evidence wins over a competing human turn.

    The request may be conflict-rejected while the same-task recovery worker is
    demonstrably live, but that is an ordinary 409 ownership conflict—not a
    receipt-store 503—and it must not discard the recovery owner.
    """
    from api import compression_recovery_receipts as receipts
    from api import config, routes

    session = _seed_session(isolated_session_state, sid="started-live-owner")
    claim = _claim_recovery(session, parent_run_id="started-live-parent")
    started = _mark_started_recovery_without_terminal_proof(
        session,
        claim,
        persist_terminal_transcript=False,
    )
    recovery_stream_id = started["child_stream_id"]
    with config.STREAMS_LOCK:
        config.STREAMS[recovery_stream_id] = object()

    monkeypatch.setattr(routes.threading, "Thread", _ObservedNoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: None,
    )
    try:
        response = routes._start_chat_stream_for_session(
            session,
            msg="Do not race the live recovery; accept this when it is safe.",
            attachments=[],
            workspace=str(isolated_session_state.workspace),
            model="gpt-4o",
            model_provider=None,
            source="webui",
            external_runtime_owned=False,
        )

        saved = receipts.load_receipts()["receipts"][claim["claim_key"]]
        assert response.get("_status") == 409, response
        assert saved["state"] == "started"
        assert saved["child_stream_id"] == recovery_stream_id
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(recovery_stream_id, None)


def test_failed_human_finish_receipt_save_keeps_live_webui_worker_owner(
    isolated_session_state,
    monkeypatch,
):
    from api import compression_recovery_receipts as receipts
    from api import config, routes
    from api.turn_journal import append_turn_journal_event

    session = _seed_session(isolated_session_state, sid="human-finish-live-owner")
    claim = _claim_recovery(session, parent_run_id="human-finish-live-parent")
    reserved = receipts.reserve_human_compression_supersession(
        session,
        attachments=[],
    )
    stream_id = "human-finish-live-stream"
    append_turn_journal_event(
        session.session_id,
        {
            "event": "submitted",
            "turn_id": "human-finish-live-turn",
            "stream_id": stream_id,
            "role": "user",
            "content": "The human turn that replaced automatic recovery.",
            "attachments": [],
            "profile": "default",
            "source": "webui",
            "recovery_claim_token": reserved["start_token"],
            "recovery_fingerprint": claim["fingerprint"],
        },
    )

    # Model the request thread exiting after its worker started: process identity
    # still matches, but the original owner thread no longer exists.
    with receipts._store_lock():
        store = receipts._load_store()
        store["receipts"][claim["claim_key"]]["owner_thread"] = 987654321
        receipts._save_store(store)

    original_save_store = receipts._save_store

    def fail_finish_store(_store):
        raise OSError("simulated human finish receipt save failure")

    monkeypatch.setattr(receipts, "_save_store", fail_finish_store)
    with pytest.raises(OSError, match="simulated human finish receipt save failure"):
        receipts.finish_human_compression_supersession(
            claim["claim_key"],
            reserved["start_token"],
        )
    monkeypatch.setattr(receipts, "_save_store", original_save_store)

    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = object()
    monkeypatch.setattr(routes.threading, "Thread", _ObservedNoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: None,
    )
    try:
        response = routes._start_chat_stream_for_session(
            session,
            msg="A competing human turn must not steal the live worker.",
            attachments=[],
            workspace=str(isolated_session_state.workspace),
            model="gpt-4o",
            model_provider=None,
            source="webui",
            external_runtime_owned=False,
        )

        saved = receipts.load_receipts()["receipts"][claim["claim_key"]]
        assert response.get("_status") == 409, response
        assert response.get("type") == "compression_recovery_in_progress", response
        assert saved["state"] == "starting"
        assert saved["launch_mode"] == "human_supersession"
        assert saved["start_token"] == reserved["start_token"]
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)


def _rotated_canonical_session(state, source, receipt, *, phase: str):
    from api import compression_recovery_receipts as receipts
    from api.models import Session

    canonical = Session(
        session_id=f"{source.session_id}-canonical",
        title=source.title,
        profile=source.profile,
        workspace=source.workspace,
        model="gpt-4o",
        messages=list(source.messages or []),
        context_messages=list(receipt["seed"]["context_messages"]),
    )
    canonical.parent_session_id = source.session_id
    canonical.compression_recovery = receipts._session_phase_payload(
        receipt,
        phase,
        reason=("ambiguous_submitted_successor" if phase == "blocked" else ""),
    )
    canonical.save(touch_updated_at=False)
    return canonical


def test_rotated_canonical_human_send_supersedes_stale_source_receipt_in_place(
    isolated_session_state,
    monkeypatch,
):
    """The canonical task owns a stale receipt still keyed to its old ID."""
    from api import compression_recovery_receipts as receipts
    from api import config, routes

    source = _seed_session(isolated_session_state, sid="rotated-human-source")
    claim = _claim_recovery(
        source,
        parent_run_id="rotated-human-parent",
    )
    started = _mark_started_recovery_without_terminal_proof(
        source,
        claim,
        persist_terminal_transcript=False,
    )
    recovery_stream_id = started["child_stream_id"]
    with config.STREAMS_LOCK:
        config.STREAMS.pop(recovery_stream_id, None)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(recovery_stream_id, None)
    canonical = _rotated_canonical_session(
        isolated_session_state,
        source,
        started,
        phase="running",
    )

    monkeypatch.setattr(routes.threading, "Thread", _ObservedNoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: None,
    )
    human_request = "Continue the rotated task with this newer instruction."
    response = routes._start_chat_stream_for_session(
        canonical,
        msg=human_request,
        attachments=[],
        workspace=str(isolated_session_state.workspace),
        model="gpt-4o",
        model_provider=None,
        source="webui",
        external_runtime_owned=False,
    )

    try:
        saved = receipts.load_receipts()["receipts"][claim["claim_key"]]
        assert response.get("_status", 200) < 400, response
        assert response["session_id"] == canonical.session_id
        assert saved["state"] == "discarded"
        assert saved["discarded_reason"] == "superseded_by_user"
        assert saved["presentation_session_id"] == canonical.session_id
        assert canonical.pending_user_message == human_request
        assert canonical.pending_user_source == "webui"
    finally:
        human_stream_id = response.get("stream_id")
        if human_stream_id:
            with config.STREAMS_LOCK:
                config.STREAMS.pop(human_stream_id, None)
            routes.unregister_stream_owner(human_stream_id)


def test_resend_to_recovery_rotation_source_reuses_canonical_owner(
    isolated_session_state,
    monkeypatch,
):
    """A stale original ID must not split recovery ownership after rotation."""
    from api import compression_recovery_receipts as receipts
    from api import config, models, routes
    from api.models import Session

    source = _seed_session(
        isolated_session_state,
        sid="rotated-original-resend-source",
    )
    claim = _claim_recovery(
        source,
        parent_run_id="rotated-original-resend-parent",
    )
    started = _mark_started_recovery_without_terminal_proof(
        source,
        claim,
        persist_terminal_transcript=False,
    )
    recovery_stream_id = started["child_stream_id"]
    with config.STREAMS_LOCK:
        config.STREAMS.pop(recovery_stream_id, None)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(recovery_stream_id, None)

    source_sid = source.session_id
    source.pre_compression_snapshot = True
    source.save(touch_updated_at=False)
    canonical = _rotated_canonical_session(
        isolated_session_state,
        source,
        started,
        phase="running",
    )
    receipts.bind_recovery_presentation_session(
        canonical,
        source_session_id=source_sid,
        child_stream_id=recovery_stream_id,
    )
    source_before_resend = source.path.read_bytes()

    # Reproduce a stale browser URL after the in-place=false Agent rotation:
    # only the archived source ID is supplied, with no mutated in-memory
    # canonical Session object left to hide an ownership split.
    models.SESSIONS.clear()
    requested_source = Session.load(source_sid)
    assert requested_source is not None
    assert requested_source.pre_compression_snapshot is True

    monkeypatch.setattr(routes.threading, "Thread", _ObservedNoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **_kwargs: None,
    )
    human_request = "Continue from the original conversation URL."
    response = routes._start_chat_stream_for_session(
        requested_source,
        msg=human_request,
        attachments=[],
        workspace=str(isolated_session_state.workspace),
        model="gpt-4o",
        model_provider=None,
        source="webui",
        external_runtime_owned=False,
    )

    try:
        saved_receipt = receipts.load_receipts()["receipts"][claim["claim_key"]]
        saved_canonical = Session.load(canonical.session_id)
        assert response.get("_status", 200) < 400, response
        assert response["session_id"] == canonical.session_id
        assert saved_receipt["state"] == "discarded"
        assert saved_receipt["discarded_reason"] == "superseded_by_user"
        assert saved_receipt["presentation_session_id"] == canonical.session_id
        assert saved_canonical is not None
        assert saved_canonical.pending_user_message == human_request
        assert saved_canonical.pending_user_source == "webui"
        assert source.path.read_bytes() == source_before_resend
    finally:
        human_stream_id = response.get("stream_id")
        if human_stream_id:
            with config.STREAMS_LOCK:
                config.STREAMS.pop(human_stream_id, None)
            routes.unregister_stream_owner(human_stream_id)


def test_deleting_rotated_canonical_purges_blocking_source_receipt_seed(
    isolated_session_state,
    monkeypatch,
):
    """Canonical deletion follows claim identity and removes the old-ID seed."""
    from api import compression_recovery_receipts as receipts
    from api.models import Session

    source = _seed_session(isolated_session_state, sid="rotated-delete-source")
    secret_request = "ROTATED DELETE private failed request"
    secret_attachment = (
        isolated_session_state.session_dir.parent / "rotated-private-proof.txt"
    )
    secret_attachment.write_text("private rotated proof", encoding="utf-8")
    claim = _claim_recovery(
        source,
        parent_run_id="rotated-delete-parent",
        failed_request=secret_request,
        attachments=[
            {
                "name": secret_attachment.name,
                "path": str(secret_attachment),
                "mime": "text/plain",
            }
        ],
    )
    _reserved, token = receipts._reserve_start(claim["claim_key"])
    assert token
    blocked = receipts._block_reserved_recovery_admission(
        claim,
        token,
        reason="ambiguous_submitted_successor",
    )
    canonical = _rotated_canonical_session(
        isolated_session_state,
        source,
        blocked,
        phase="blocked",
    )

    response = _post(
        monkeypatch,
        "/api/session/delete",
        {"session_id": canonical.session_id},
    )

    assert response["status"] == 200
    assert response["payload"]["ok"] is True
    assert claim["claim_key"] not in receipts.load_receipts()["receipts"]
    raw_store = (
        isolated_session_state.session_dir / "_compression_recoveries.json"
    ).read_text(encoding="utf-8")
    assert secret_request not in raw_store
    assert str(secret_attachment) not in raw_store
    assert Session.load(canonical.session_id) is None


def test_runner_pending_gate_follows_claim_key_across_session_rotation(
    isolated_session_state,
    monkeypatch,
):
    """An external runner cannot bypass old-ID recovery ownership."""
    from api import compression_recovery_receipts as receipts
    from api import routes

    source = _seed_session(isolated_session_state, sid="rotated-runner-source")
    claim = _claim_recovery(
        source,
        parent_run_id="rotated-runner-parent",
    )
    canonical = _rotated_canonical_session(
        isolated_session_state,
        source,
        claim,
        phase="claimed",
    )

    class RunnerClient:
        def start_run(self, _request):
            raise AssertionError("rotated pending recovery escaped to runner")

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(
        "api.runtime_adapter.runtime_adapter_runner_enabled",
        lambda: True,
    )
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: RunnerClient())

    assert receipts.session_has_pending_compression_recovery(
        canonical.session_id,
        claim_key=claim["claim_key"],
    ) is True
    response = routes._start_run(
        canonical,
        msg="A human instruction on the rotated canonical task.",
        attachments=[],
        workspace=str(isolated_session_state.workspace),
        model="gpt-4o",
        model_provider=None,
        normalized_model=False,
        source="webui",
        route="/api/chat/start",
    )

    assert response["_status"] == 409
    assert response["code"] == "compression_recovery_runner_supersession_unsupported"


def test_delete_live_hidden_recovery_returns_typed_conflict_without_mutation(
    isolated_session_state,
    monkeypatch,
):
    """Deletion cannot revoke a demonstrably live hidden successor owner."""
    import time

    from api import compression_recovery_receipts as receipts
    from api import config
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="delete-live-recovery")
    claim = _claim_recovery(
        session,
        parent_run_id="delete-live-parent",
        failed_request="Finish the live hidden recovery before deletion.",
    )
    started = _mark_started_recovery_without_terminal_proof(
        session,
        claim,
        persist_terminal_transcript=False,
    )
    recovery_stream_id = started["child_stream_id"]
    session.active_stream_id = recovery_stream_id
    session.pending_user_message = None
    session.pending_user_source = receipts.SOURCE
    session.save(touch_updated_at=False)
    sidecar_before = session.path.read_bytes()

    with config.STREAMS_LOCK:
        config.STREAMS[recovery_stream_id] = object()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS[recovery_stream_id] = {
            "session_id": session.session_id,
            "stream_id": recovery_stream_id,
            "started_at": time.time(),
        }
    try:
        response = _post(
            monkeypatch,
            "/api/session/delete",
            {"session_id": session.session_id},
        )

        assert response["status"] == 409, response
        assert response["payload"].get("type") == "compression_recovery_in_progress"
        saved = receipts.load_receipts()["receipts"][claim["claim_key"]]
        assert saved["state"] == "started"
        assert saved["child_stream_id"] == recovery_stream_id
        persisted = Session.load(session.session_id)
        assert persisted is not None
        assert persisted.path.read_bytes() == sidecar_before
        assert persisted.active_stream_id == recovery_stream_id
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(recovery_stream_id, None)
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.pop(recovery_stream_id, None)


def test_delete_stale_started_recovery_still_purges_receipt_and_session(
    isolated_session_state,
    monkeypatch,
):
    """A started receipt without live registry evidence cannot brick deletion."""
    from api import compression_recovery_receipts as receipts
    from api import config
    from api.models import Session

    session = _seed_session(isolated_session_state, sid="delete-stale-recovery")
    claim = _claim_recovery(
        session,
        parent_run_id="delete-stale-parent",
        failed_request="This stale hidden recovery may be deleted.",
    )
    started = _mark_started_recovery_without_terminal_proof(
        session,
        claim,
        persist_terminal_transcript=False,
    )
    recovery_stream_id = started["child_stream_id"]
    with config.STREAMS_LOCK:
        config.STREAMS.pop(recovery_stream_id, None)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(recovery_stream_id, None)

    response = _post(
        monkeypatch,
        "/api/session/delete",
        {"session_id": session.session_id},
    )

    assert response["status"] == 200, response
    assert response["payload"]["ok"] is True
    assert claim["claim_key"] not in receipts.load_receipts()["receipts"]
    assert Session.load(session.session_id) is None
