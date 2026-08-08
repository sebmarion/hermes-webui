import copy

import pytest

from api.compression_recovery import (
    COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS,
    CompressionRecoveryBlocked,
    build_same_session_recovery_seed,
    validate_recovery_attachments_for_use,
)
from api.models import Session


def _session(*, sid="recovery-seed", messages=None, context_messages=None):
    return Session(
        session_id=sid,
        title="Unchanged title",
        messages=list(messages or []),
        context_messages=list(context_messages or []),
    )


def _build(session, request, *, parent="parent-run", attachments=None, partial=""):
    return build_same_session_recovery_seed(
        session,
        parent_run_id=parent,
        failed_user_text=request,
        attachments=attachments,
        partial_assistant_text=partial,
    )


def _assistant_contents(seed):
    return [
        row["content"]
        for row in seed["context_messages"]
        if row.get("role") == "assistant"
    ]


def test_seed_prefers_newest_trusted_summary_and_preserves_exact_request():
    request = "Ok audit it and do the other steps you said"
    checkpoint = "Detailed assistant checkpoint that is deliberately not selected. " * 3
    old_summary = "Old compacted summary"
    newest_summary = "Newest compacted summary with sk_live_1234567890secret"
    session = _session(
        messages=[
            {"role": "assistant", "content": checkpoint},
            {"role": "user", "content": request},
        ],
        context_messages=[
            {"role": "assistant", "content": old_summary, "_compressed_summary": True},
            {"role": "assistant", "content": newest_summary, "_compressed_summary": True},
        ],
    )

    seed = _build(session, request)

    assert seed["trust_source"] == "summary"
    assert seed["context_messages"][-1] == {"role": "user", "content": request}
    assistant_text = "\n".join(_assistant_contents(seed))
    assert "Newest compacted summary" in assistant_text
    assert old_summary not in assistant_text
    assert "Detailed assistant checkpoint" not in assistant_text
    assert "sk_live_1234567890secret" not in assistant_text


def test_seed_uses_prior_assistant_plan_for_deictic_authorization():
    request = "Ok audit it and do the other steps you said"
    plan = (
        "Audit the v1.0.1 source archive, run the isolated shadow-mode plugin "
        "check, verify strict binding and fail-closed behavior, then report the diff."
    )
    session = _session(
        messages=[
            {"role": "user", "content": "What do you think of this integration?"},
            {"role": "assistant", "content": plan, "timestamp": 10},
            {"role": "user", "content": request, "timestamp": 11},
        ]
    )

    seed = _build(session, request)

    assert seed["trust_source"] == "assistant_checkpoint"
    assert plan in "\n".join(_assistant_contents(seed))
    assert seed["context_messages"][-1]["content"] == request


def test_seed_accepts_independently_substantive_request_without_reference():
    request = (
        "Inspect api/routes.py for every compression_exhausted call site, add "
        "behavioral regression tests, and report the exact test command."
    )
    seed = _build(_session(messages=[{"role": "user", "content": request}]), request)

    assert seed["trust_source"] == "user_request"
    assert seed["context_messages"] == [{"role": "user", "content": request}]


@pytest.mark.parametrize(
    "request_text",
    ["continue", "go on", "do it", "ok do that", "handle the other steps you said"],
)
def test_seed_blocks_generic_or_deictic_request_without_reference(request_text):
    session = _session(
        messages=[
            {"role": "assistant", "content": "Sure."},
            {"role": "user", "content": request_text},
        ]
    )

    with pytest.raises(CompressionRecoveryBlocked) as exc_info:
        _build(session, request_text)

    assert exc_info.value.reason == "no_trustworthy_seed"


def test_seed_excludes_unsafe_rows_and_uses_only_substantive_checkpoint():
    request = "continue"
    valid = (
        "The safe checkpoint is to update the shared terminal handler, preserve "
        "the visible transcript, and start only after unregister_active_run."
    )
    session = _session(
        messages=[
            {"role": "assistant", "content": "raw tool preamble", "tool_calls": [{"name": "shell"}]},
            {"role": "tool", "content": "raw tool output"},
            {"role": "assistant", "content": "secret reasoning", "reasoning": "hidden"},
            {"role": "assistant", "content": "terminal failure details", "_error": True},
            {"role": "assistant", "content": "synthetic control", "_synthetic": True},
            {"role": "assistant", "content": "recovery control", "_recovery_control": True},
            {"role": "assistant", "content": "tool-limit control", "_tool_limit_continuation_control": True},
            {"role": "assistant", "content": valid},
            {"role": "user", "content": request},
        ]
    )

    seed = _build(session, request)
    combined = "\n".join(_assistant_contents(seed))

    assert seed["trust_source"] == "assistant_checkpoint"
    assert valid in combined
    for excluded in (
        "raw tool preamble",
        "raw tool output",
        "secret reasoning",
        "terminal failure details",
        "synthetic control",
        "recovery control",
        "tool-limit control",
    ):
        assert excluded not in combined


def test_partial_is_redacted_labelled_and_bounded():
    request = "continue"
    summary = "Trusted summary"
    partial = "sk_live_1234567890secret " + ("partial work " * 2_000)
    session = _session(
        context_messages=[
            {"role": "assistant", "content": summary, "_compressed_summary": True}
        ]
    )

    seed = _build(session, request, partial=partial)
    assistant_text = "\n".join(_assistant_contents(seed))

    assert "Partial, unverified work" in assistant_text
    assert "sk_live_1234567890secret" not in assistant_text
    assert sum(len(str(row.get("content") or "")) for row in seed["context_messages"]) <= (
        COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS
    )
    assert seed["context_messages"][-1]["content"] == request


def test_context_management_partial_is_not_copied():
    request = "continue"
    session = _session(
        context_messages=[
            {"role": "assistant", "content": "Trusted summary", "_compressed_summary": True}
        ]
    )

    seed = _build(
        session,
        request,
        partial="[Compression recovery control] start another continuation",
    )

    assert "start another continuation" not in "\n".join(_assistant_contents(seed))


def test_exact_request_is_never_truncated_to_fit_budget():
    request = "x" * (COMPRESSION_RECOVERY_CONTEXT_MAX_CHARS + 1)

    with pytest.raises(CompressionRecoveryBlocked) as exc_info:
        _build(_session(messages=[{"role": "user", "content": request}]), request)

    assert exc_info.value.reason == "user_request_exceeds_context_budget"


def test_fingerprint_is_deterministic_and_binds_every_identity(tmp_path):
    request = "Inspect the named file and implement the bounded recovery helper."
    attached = tmp_path / "evidence.txt"
    attached.write_text("evidence", encoding="utf-8")
    attachment = {"name": "evidence.txt", "path": str(attached), "mime": "text/plain", "size": 8}
    session = _session(sid="seed-a", messages=[{"role": "user", "content": request}])

    first = _build(session, request, parent="run-a", attachments=[attachment])
    again = _build(session, request, parent="run-a", attachments=[attachment])
    other_parent = _build(session, request, parent="run-b", attachments=[attachment])
    other_session = _build(
        _session(sid="seed-b", messages=[{"role": "user", "content": request}]),
        request,
        parent="run-a",
        attachments=[attachment],
    )
    other_attachment = dict(attachment, name="renamed.txt")
    other_file = _build(session, request, parent="run-a", attachments=[other_attachment])

    assert first["fingerprint"] == again["fingerprint"]
    assert len(first["fingerprint"]) == 64
    assert set(first["fingerprint"]) <= set("0123456789abcdef")
    assert len(
        {
            first["fingerprint"],
            other_parent["fingerprint"],
            other_session["fingerprint"],
            other_file["fingerprint"],
        }
    ) == 4


def test_attachment_normalization_preserves_safe_fields_and_drops_unknown(tmp_path):
    attached = tmp_path / "image.png"
    attached.write_bytes(b"png")
    raw = {
        "name": "image.png",
        "path": str(attached),
        "mime": "image/png",
        "size": 3,
        "is_image": True,
        "secret_extra": "drop me",
    }

    normalized = validate_recovery_attachments_for_use([raw])

    assert normalized == [
        {
            "name": "image.png",
            "path": str(attached),
            "mime": "image/png",
            "size": 3,
            "is_image": True,
        }
    ]


def test_missing_attachment_blocks_instead_of_silent_drop(tmp_path):
    missing = tmp_path / "missing.txt"

    with pytest.raises(CompressionRecoveryBlocked) as exc_info:
        validate_recovery_attachments_for_use(
            [{"name": "missing.txt", "path": str(missing), "mime": "text/plain"}]
        )

    assert exc_info.value.reason == "attachment_missing"


def test_conflicting_attachment_identity_blocks(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    with pytest.raises(CompressionRecoveryBlocked) as exc_info:
        validate_recovery_attachments_for_use(
            [
                {"name": "evidence.txt", "path": str(first), "mime": "text/plain"},
                {"name": "evidence.txt", "path": str(second), "mime": "text/plain"},
            ]
        )

    assert exc_info.value.reason == "attachment_conflict"


def test_seed_construction_does_not_mutate_visible_user_row_or_session(tmp_path):
    request = "Inspect the attached evidence and implement the exact recovery change."
    attached = tmp_path / "evidence.txt"
    attached.write_text("proof", encoding="utf-8")
    attachment = {"name": "evidence.txt", "path": str(attached), "mime": "text/plain"}
    user_row = {
        "role": "user",
        "content": request,
        "timestamp": 123.5,
        "_source": "webui",
        "attachments": [copy.deepcopy(attachment)],
    }
    session = _session(messages=[user_row])
    before_messages = copy.deepcopy(session.messages)
    before_context = copy.deepcopy(session.context_messages)
    before_title = session.title

    seed = _build(session, request, attachments=[attachment])

    assert session.messages == before_messages
    assert session.context_messages == before_context
    assert session.title == before_title
    assert seed["attachments"] == [attachment]
    assert sum(
        row.get("role") == "user" and row.get("content") == request
        for row in seed["context_messages"]
    ) == 1


def test_terminal_claim_hands_off_once_to_same_session_after_unregister(
    tmp_path, monkeypatch
):
    from api import background_process, config, models, routes, streaming
    from api import goal_continuation

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()

    sid = "same-conversation"
    request = "Ok audit it and do the other steps you said"
    plan = (
        "Audit the source archive, run the isolated shadow-mode plugin check, "
        "verify strict binding and fail-closed behavior, then report the diff."
    )
    session = Session(
        session_id=sid,
        title="Unchanged conversation",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Assess this integration."},
            {"role": "assistant", "content": plan},
            {"role": "user", "content": request},
        ],
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    accepted, presentation = streaming._claim_same_session_compression_recovery(
        session,
        "parent-stream",
        failed_user_text=request,
        attachments=[],
        source="webui",
    )
    starts = []

    def start_session_turn(session_id, prompt, **kwargs):
        starts.append((session_id, prompt, kwargs))
        return {
            "_status": 200,
            "session_id": session_id,
            "stream_id": "recovery-stream",
        }

    monkeypatch.setattr(routes, "start_session_turn", start_session_turn)
    monkeypatch.setattr(
        background_process,
        "drain_deferred_wakeups_for_session",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        goal_continuation,
        "recover_pending_goal_continuations",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "api.execution_lineage.resolve_execution_lineage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("not needed")),
    )

    first = background_process.recover_successors_after_unregister(
        sid,
        parent_run_id="parent-stream",
        session=session,
        profile="default",
    )
    duplicate = background_process.recover_successors_after_unregister(
        sid,
        parent_run_id="parent-stream",
        session=session,
        profile="default",
    )

    assert accepted is True
    assert presentation["phase"] == "claimed"
    assert first["compression"] == 1
    assert duplicate["compression"] == 0
    assert len(starts) == 1
    start_sid, _prompt, kwargs = starts[0]
    assert start_sid == sid
    assert kwargs["source"] == "compression_recovery"
    assert kwargs["recovery_context_messages"][-1] == {
        "role": "user",
        "content": request,
    }
    assert not any(
        row.get("role") == "assistant" and row.get("_error")
        for row in session.messages
    )
    assert [
        path.name
        for path in session_dir.glob("*.json")
        if not path.name.startswith("_")
    ] == [f"{sid}.json"]


def test_duplicate_terminal_callback_blocks_stale_started_receipt(
    tmp_path,
    monkeypatch,
):
    from api import config, models, streaming
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()

    request = "Finish this exact request without resurrecting a stale recovery."
    session = Session(
        session_id="duplicate-terminal",
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": request}],
    )
    seed = _build(session, request, parent="parent-run")
    claimed = receipts.claim_compression_recovery(session, "parent-run", seed)
    receipts.settle_compression_recovery(
        session.session_id,
        "parent-run",
        start=lambda sid, _prompt, **_kwargs: {
            "session_id": sid,
            "stream_id": "stale-started-stream",
        },
    )
    session.compression_recovery = {}
    session.save(touch_updated_at=False)

    accepted, presentation = streaming._claim_same_session_compression_recovery(
        session,
        "parent-run",
        failed_user_text=request,
        attachments=[],
        source="webui",
    )
    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]

    assert accepted is False
    assert presentation["phase"] == "blocked"
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "ambiguous_started_successor"


def test_reserved_recovery_seed_is_installed_at_same_session_stream_admission(
    tmp_path, monkeypatch
):
    from api import config, models, routes
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "admission-same-session"
    request = "Inspect the exact recovery seam and finish the implementation safely."
    session = Session(
        session_id=sid,
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": request}],
        context_messages=[{"role": "assistant", "content": "Old context must be replaced."}],
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    seed = _build(session, request, parent="parent-admission")
    claimed = receipts.claim_compression_recovery(
        session, "parent-admission", seed
    )
    reserved, token = receipts._reserve_start(claimed["claim_key"])
    receipts._mark_launching(claimed["claim_key"], token)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.ident = None

        def start(self):
            self.ident = 123456
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", NoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)

    response = routes._start_chat_stream_for_session(
        session,
        msg=receipts.RECOVERY_CONTROL_PROMPT,
        attachments=seed["attachments"],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider=None,
        source=receipts.SOURCE,
        recovery_claim_token=token,
        recovery_fingerprint=seed["fingerprint"],
        recovery_context_messages=copy.deepcopy(seed["context_messages"]),
    )

    try:
        assert response.get("_status", 200) < 400
        assert response["session_id"] == sid
        assert session.context_messages == seed["context_messages"]
        assert session.pending_user_message is None
        assert session.pending_user_source == receipts.SOURCE
        assert session.compression_recovery["phase"] == "starting"
    finally:
        stream_id = response.get("stream_id")
        if stream_id:
            routes.STREAMS.pop(stream_id, None)
            routes.unregister_stream_owner(stream_id)


def test_recovery_journal_failure_releases_unregistered_pending_owner(
    tmp_path,
    monkeypatch,
):
    from api import config, models, routes, turn_journal
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "journal-failure-same-session"
    request = "Finish the recovery without leaving a phantom active owner."
    session = Session(
        session_id=sid,
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": request}],
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    seed = _build(session, request, parent="parent-journal-failure")
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-journal-failure",
        seed,
    )
    _reserved, token = receipts._reserve_start(claimed["claim_key"])
    receipts._mark_launching(claimed["claim_key"], token)
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)

    response = routes._start_chat_stream_for_session(
        session,
        msg=receipts.RECOVERY_CONTROL_PROMPT,
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider=None,
        source=receipts.SOURCE,
        recovery_claim_token=token,
        recovery_fingerprint=seed["fingerprint"],
        recovery_context_messages=copy.deepcopy(seed["context_messages"]),
    )

    assert response["_status"] == 500
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_attachments == []
    assert session.pending_user_source is None
    assert session.pending_server_instance_id is None


def test_human_turn_fences_pending_recovery_until_worker_start(
    tmp_path,
    monkeypatch,
):
    from api import config, models, routes
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "human-supersession-same-session"
    failed_request = "Finish the original recovery request safely."
    human_request = "Use the recovered context, but do this newer instruction now."
    session = Session(
        session_id=sid,
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": failed_request}],
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    seed = _build(session, failed_request, parent="parent-human")
    claimed = receipts.claim_compression_recovery(session, "parent-human", seed)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.ident = None

        def start(self):
            self.ident = 654321
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", NoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)

    response = routes._start_chat_stream_for_session(
        session,
        msg=human_request,
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider=None,
        source="webui",
    )

    try:
        saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
        assert response.get("_status", 200) < 400
        assert saved["state"] == "discarded"
        assert saved["discarded_reason"] == "superseded_by_user"
        assert session.context_messages == seed["context_messages"]
        assert session.pending_user_message == human_request
        assert session.pending_user_source == "webui"
    finally:
        stream_id = response.get("stream_id")
        if stream_id:
            routes.STREAMS.pop(stream_id, None)
            routes.unregister_stream_owner(stream_id)


def test_human_turn_retires_blocked_recovery_and_keeps_composer_usable(
    tmp_path,
    monkeypatch,
):
    from api import config, models, routes
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    failed_request = (
        "Inspect the recovery blocker and continue the implementation without "
        "replaying any prior tool effects."
    )
    session = Session(
        session_id="blocked-recovery-human-send",
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": failed_request}],
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    routes.SESSIONS[session.session_id] = session
    seed = _build(
        session,
        failed_request,
        parent="blocked-parent",
    )
    claimed = receipts.claim_compression_recovery(
        session,
        "blocked-parent",
        seed,
    )
    _reserved, token = receipts._reserve_start(claimed["claim_key"])
    blocked = receipts._block_reserved_recovery_admission(
        claimed,
        token,
        reason="recovery_attachment_unavailable",
    )
    session.compression_recovery = receipts._session_phase_payload(
        blocked,
        "blocked",
        reason="recovery_attachment_unavailable",
    )
    session.save(touch_updated_at=False)

    class NoopThread:
        def __init__(self, *args, **kwargs):
            self.ident = None

        def start(self):
            self.ident = 112233

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", NoopThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)

    response = routes._start_chat_stream_for_session(
        session,
        msg="Continue with this safer new instruction.",
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider=None,
        source="webui",
    )

    try:
        saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
        assert response.get("_status", 200) < 400
        assert saved["state"] == "discarded"
        assert saved["discarded_reason"] == "superseded_by_user"
        assert session.compression_recovery == {}
        assert session.pending_user_message == "Continue with this safer new instruction."
    finally:
        stream_id = response.get("stream_id")
        if stream_id:
            routes.STREAMS.pop(stream_id, None)
            routes.unregister_stream_owner(stream_id)


def test_human_supersession_blocks_when_launch_and_failure_journal_both_fail(
    tmp_path,
    monkeypatch,
):
    from api import config, models, routes, turn_journal
    from api import compression_recovery_receipts as receipts

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "human-supersession-launch-journal-failure"
    failed_request = "Finish the failed request without losing its context."
    session = Session(
        session_id=sid,
        title="Same title",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": failed_request}],
    )
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    seed = _build(session, failed_request, parent="parent-human-launch-failure")
    claimed = receipts.claim_compression_recovery(
        session,
        "parent-human-launch-failure",
        seed,
    )

    class FailingThread:
        ident = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread launch failed")

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Thread", FailingThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event_for_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("launch failure journal unavailable")
        ),
    )

    response = routes._start_chat_stream_for_session(
        session,
        msg="Use the recovered context for this newer instruction.",
        attachments=[],
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider=None,
        source="webui",
    )

    saved = receipts.load_receipts()["receipts"][claimed["claim_key"]]
    assert response["_status"] == 500
    assert saved["state"] == "discarded"
    assert saved["discarded_reason"] == "ambiguous_human_supersession"
    assert session.compression_recovery["phase"] == "blocked"
    assert session.active_stream_id is None
