"""Behavioral tests for issue #4029 stale-completion age gate.

These exercise the real `_drain_webui_process_notifications` against a fake
process_registry, proving that:
  - a completion older than the cap is dropped (consumed, not requeued),
  - a fresh completion is still delivered,
  - events without `created_at` are never dropped,
  - the env override (incl. disable via 0) is honored.
"""
import importlib
import json
import queue
import sys
import time
import types

import pytest


def _install_fake_registry(monkeypatch, events):
    """Build a fake `tools.process_registry` module whose `process_registry`
    exposes the surface `_drain_webui_process_notifications` touches."""
    q = queue.Queue()
    for e in events:
        q.put(e)

    class _FakeRegistry:
        def __init__(self):
            self.completion_queue = q
            self.consumed_event_ids = set()
            self.finish_calls = []
            self.fail_committed = False

        def finish_notification_delivery(self, event, committed):
            self.finish_calls.append((event, committed))
            if committed:
                if self.fail_committed:
                    self.completion_queue.put(event)
                    return False
                stable_id = event.get("event_id") or event.get("delegation_id")
                self.consumed_event_ids.add(stable_id)
                return True
            self.completion_queue.put(event)
            return False

    reg = _FakeRegistry()
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = reg
    # ensure parent package exists
    if "tools" not in sys.modules:
        pkg = types.ModuleType("tools")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)
    return reg


def _make_event(sid, created_at, session_key="websess-1"):
    return {
        "event_id": f"process:{sid}:completion",
        "type": "completion",
        "session_id": sid,
        "session_key": session_key,
        "command": f"cmd-{sid}",
        "exit_code": 0,
        "output": f"out-{sid}",
        **({"created_at": created_at} if created_at is not None else {}),
    }


def _make_session(tmp_path, session_id, *, messages=None):
    from api import models

    return models.Session(
        session_id=session_id,
        title="Process wakeup receipt",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(messages or []),
    )


def _clear_staged_process_completion_events():
    from api import background_process as bp

    with bp._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        bp._STAGED_PROCESS_COMPLETION_EVENTS.clear()


def _call_process_wakeup_start(routes, session, *, session_lock_held=False):
    return routes._start_chat_stream_for_session(
        session,
        msg="Background process finished",
        attachments=[],
        workspace=session.workspace,
        model=session.model,
        source="process_wakeup",
        session_lock_held=session_lock_held,
    )


def _configure_receipt_route_without_provider(monkeypatch, tmp_path, failure_message):
    from api import background_process as bp, config, models, routes

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

    def provider_must_not_start(*_args, **_kwargs):
        raise AssertionError(failure_message)

    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        provider_must_not_start,
    )
    return bp, config, models, routes


@pytest.fixture
def streaming():
    return importlib.import_module("api.streaming")


def test_stale_completion_is_dropped_and_fresh_is_delivered(streaming, monkeypatch):
    now = time.time()
    fresh = _make_event("fresh", now - 60)          # 1 min old -> keep
    stale = _make_event("stale", now - 7 * 3600)    # 7 h old -> drop (cap 6h)
    reg = _install_fake_registry(monkeypatch, [stale, fresh])
    # default cap = 6h
    monkeypatch.delenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", raising=False)

    claims = []
    out = streaming._drain_webui_process_notifications(
        "websess-1",
        claimed_events=claims,
    )

    joined = "\n".join(out)
    assert "fresh" in joined, "fresh completion should be delivered"
    assert "stale" not in joined, "stale completion must be dropped"
    assert claims == [fresh]
    assert claims[0] is fresh
    assert reg.finish_calls == [(stale, True)]
    assert "process:stale:completion" in reg.consumed_event_ids
    assert "process:fresh:completion" not in reg.consumed_event_ids

    # The fresh event is ACKed only after its synthetic turn commits.
    assert streaming._finalize_process_completion_claims(
        reg,
        claims,
        committed=True,
    ) is True
    assert "process:fresh:completion" in reg.consumed_event_ids
    assert reg.completion_queue.empty()


def test_event_without_created_at_is_never_dropped(streaming, monkeypatch):
    legacy = _make_event("legacy", None)
    reg = _install_fake_registry(monkeypatch, [legacy])
    monkeypatch.delenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", raising=False)

    claims = []
    out = streaming._drain_webui_process_notifications(
        "websess-1",
        claimed_events=claims,
    )

    assert any("legacy" in n for n in out), "event without timestamp must be delivered"
    assert claims == [legacy]


def test_env_zero_disables_age_gate(streaming, monkeypatch):
    now = time.time()
    ancient = _make_event("ancient", now - 10 * 24 * 3600)  # 10 days old
    reg = _install_fake_registry(monkeypatch, [ancient])
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "0")

    claims = []
    out = streaming._drain_webui_process_notifications(
        "websess-1",
        claimed_events=claims,
    )

    assert any("ancient" in n for n in out), "age gate disabled -> even ancient delivered"
    assert claims == [ancient]


def test_uncommitted_direct_drain_requeues_exact_event(streaming, monkeypatch):
    event = _make_event("retry", time.time())
    reg = _install_fake_registry(monkeypatch, [event])

    out = streaming._drain_webui_process_notifications("websess-1")

    assert any("retry" in notification for notification in out)
    assert reg.finish_calls == [(event, False)]
    assert reg.finish_calls[0][0] is event
    assert reg.completion_queue.get_nowait() is event


def test_ack_failure_explicitly_requeues_exact_event(streaming, monkeypatch):
    event = _make_event("ack-failure", time.time())
    reg = _install_fake_registry(monkeypatch, [])
    reg.fail_committed = True

    assert streaming._finalize_process_completion_claims(
        reg,
        [event],
        committed=True,
    ) is False

    assert reg.finish_calls == [(event, True)]
    assert all(call[0] is event for call in reg.finish_calls)
    assert reg.completion_queue.get_nowait() is event


def test_staged_wakeup_handoff_preserves_exact_event_identity(monkeypatch):
    from api import background_process as bp

    event = _make_event("staged", time.time(), session_key="websess-stage")
    with bp._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        bp._STAGED_PROCESS_COMPLETION_EVENTS.clear()

    assert bp.stage_process_completion_event("websess-stage", event) is True
    assert bp.stage_process_completion_event("websess-stage", event) is True

    claimed = bp.claim_staged_process_completion_events("websess-stage")
    assert claimed == [event]
    assert claimed[0] is event
    assert bp.claim_staged_process_completion_events("websess-stage") == []


def test_failed_settlement_releases_proactive_seen_claim(streaming, monkeypatch):
    from api import config

    event = _make_event("retry-seen", time.time())
    reg = _install_fake_registry(monkeypatch, [])
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
        config.BG_TASK_COMPLETE_EVENTS_SEEN["websess-1"] = {"retry-seen"}

    assert streaming._finalize_process_completion_claims(
        reg,
        [event],
        committed=False,
    ) is False

    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        assert "retry-seen" not in config.BG_TASK_COMPLETE_EVENTS_SEEN.get(
            "websess-1", set()
        )
    assert reg.finish_calls == [(event, False)]
    assert reg.completion_queue.get_nowait() is event


def test_async_delegation_uses_same_atomic_settlement(streaming, monkeypatch):
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-stable",
        "session_key": "websess-1",
    }
    reg = _install_fake_registry(monkeypatch, [])

    claimed = streaming._validated_process_completion_events(
        [event],
        session_id="websess-1",
    )
    assert claimed == [event]
    assert claimed[0] is event
    assert streaming._finalize_process_completion_claims(
        reg,
        claimed,
        committed=True,
    ) is True
    assert reg.finish_calls == [(event, True)]


def test_helper_reads_env_override(streaming, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "120")
    assert streaming._stale_completion_max_age_seconds() == 120.0
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "not-a-number")
    # invalid -> falls back to default 6h
    assert streaming._stale_completion_max_age_seconds() == 6 * 60 * 60


def test_process_completion_receipt_requires_durable_sidecar(
    streaming,
    monkeypatch,
    tmp_path,
):
    from api import config, models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    event = _make_event(
        "receipt-process",
        time.time(),
        session_key="receipt-sidecar",
    )
    terminal = {
        "role": "assistant",
        "content": "**Error:** provider unavailable",
        "timestamp": int(time.time()),
        "_error": True,
    }
    receipt_digests = streaming._stamp_process_completion_receipts(
        terminal,
        [event],
        session_id="receipt-sidecar",
    )
    assert len(receipt_digests) == 1
    assert event["event_id"] not in receipt_digests[0]

    persisted = _make_session(
        tmp_path,
        "receipt-sidecar",
        messages=[terminal],
    )
    persisted.save(skip_index=True)

    # A stale cache entry without the receipt must not override the sidecar.
    cached = _make_session(
        tmp_path,
        "receipt-sidecar",
        messages=[{"role": "assistant", "content": "stale cache"}],
    )
    with config.LOCK:
        config.SESSIONS["receipt-sidecar"] = cached
    try:
        assert streaming._durable_process_completion_receipt_status(
            "receipt-sidecar",
            [event],
        ) == "committed"
    finally:
        with config.LOCK:
            config.SESSIONS.pop("receipt-sidecar", None)

    reloaded = models.Session.load("receipt-sidecar")
    assert reloaded is not None
    assert reloaded.messages == [terminal]
    raw_messages = json.loads(persisted.path.read_text(encoding="utf-8"))["messages"]
    raw_metadata = [
        {key: value for key, value in message.items() if key != "content"}
        for message in raw_messages
    ]
    assert event["event_id"] not in json.dumps(raw_metadata, sort_keys=True)


@pytest.mark.parametrize(
    "stored_case",
    [
        "wrong_type",
        "empty",
        "invalid_digest",
        "duplicate_within_message",
        "duplicate_across_messages",
        "valid_plus_invalid",
        "user_role",
        "tool_role",
    ],
)
def test_malformed_stored_receipt_metadata_restages_claim_without_provider(
    streaming,
    monkeypatch,
    tmp_path,
    stored_case,
):
    bp, _config, _models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        f"provider preparation reached for malformed receipt {stored_case}",
    )
    session_id = f"malformed-receipt-{stored_case}"
    event = _make_event(
        f"malformed-{stored_case}",
        time.time(),
        session_key=session_id,
    )
    digest = streaming._process_completion_receipt_digest(
        event,
        session_id=session_id,
    )
    assert digest is not None
    key = streaming._PROCESS_COMPLETION_RECEIPTS_KEY

    if stored_case == "wrong_type":
        messages = [{"role": "assistant", "content": "terminal", key: digest}]
    elif stored_case == "empty":
        messages = [{"role": "assistant", "content": "terminal", key: []}]
    elif stored_case == "invalid_digest":
        messages = [
            {"role": "assistant", "content": "terminal", key: ["invalid"]}
        ]
    elif stored_case == "duplicate_within_message":
        messages = [
            {"role": "assistant", "content": "terminal", key: [digest, digest]}
        ]
    elif stored_case == "duplicate_across_messages":
        messages = [
            {"role": "assistant", "content": "first", key: [digest]},
            {"role": "assistant", "content": "second", key: [digest]},
        ]
    elif stored_case == "valid_plus_invalid":
        messages = [
            {"role": "assistant", "content": "first", key: [digest]},
            {"role": "assistant", "content": "second", key: ["invalid"]},
        ]
    elif stored_case == "user_role":
        messages = [{"role": "user", "content": "not terminal", key: [digest]}]
    else:
        messages = [{"role": "tool", "content": "not terminal", key: [digest]}]

    session = _make_session(tmp_path, session_id, messages=messages)
    session.save(skip_index=True)
    _clear_staged_process_completion_events()
    try:
        assert bp.stage_process_completion_event(session_id, event) is True
        response = _call_process_wakeup_start(routes, session)
        assert response == {
            "error": "Durable process-completion receipt is unavailable",
            "code": "durable_completion_receipt_unavailable",
            "retryable": True,
            "_status": 503,
        }
        preserved = bp.claim_staged_process_completion_events(session_id)
        assert preserved == [event]
        assert preserved[0] is event
    finally:
        _clear_staged_process_completion_events()


def test_valid_unrelated_assistant_receipt_is_absent(streaming, monkeypatch, tmp_path):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session_id = "unrelated-receipt-owner"
    required = _make_event("required-receipt", time.time(), session_key=session_id)
    unrelated = _make_event("unrelated-receipt", time.time(), session_key=session_id)
    terminal = {"role": "assistant", "content": "different completion"}
    assert streaming._stamp_process_completion_receipts(
        terminal,
        [unrelated],
        session_id=session_id,
    )
    session = _make_session(tmp_path, session_id, messages=[terminal])
    session.save(skip_index=True)

    assert streaming._durable_process_completion_receipt_status(
        session_id,
        [required],
    ) == "absent"


@pytest.mark.parametrize(
    "invalid_tail",
    [
        ["malformed"],
        [{"type": "completion", "session_key": "batch-owner"}],
        [{"type": "watch_match", "event_id": "watch-1", "session_key": "batch-owner"}],
        [
            {
                "type": "completion",
                "event_id": "process:other:completion",
                "session_id": "other",
                "session_key": "different-owner",
            }
        ],
    ],
)
def test_process_completion_receipt_batch_validation_is_all_or_nothing(
    streaming,
    invalid_tail,
):
    valid = _make_event("valid-batch", time.time(), session_key="batch-owner")
    batch = [valid, *invalid_tail]

    claimed = streaming._validated_process_completion_events(
        batch,
        session_id="batch-owner",
    )
    assert claimed == [valid]
    assert claimed[0] is valid
    message = {"role": "assistant", "content": "terminal"}
    assert streaming._stamp_process_completion_receipts(
        message,
        batch,
        session_id="batch-owner",
    ) == []
    assert streaming._PROCESS_COMPLETION_RECEIPTS_KEY not in message
    assert streaming._durable_process_completion_receipt_status(
        "batch-owner",
        batch,
    ) == "invalid"


def test_process_completion_receipt_rejects_duplicate_batch(streaming):
    valid = _make_event("duplicate", time.time(), session_key="batch-owner")
    duplicate = dict(valid)

    claimed = streaming._validated_process_completion_events(
        [valid, duplicate],
        session_id="batch-owner",
    )
    assert claimed == [valid]
    assert claimed[0] is valid
    message = {"role": "assistant", "content": "terminal"}
    assert streaming._stamp_process_completion_receipts(
        message,
        [valid, duplicate],
        session_id="batch-owner",
    ) == []
    assert streaming._PROCESS_COMPLETION_RECEIPTS_KEY not in message
    assert streaming._durable_process_completion_receipt_status(
        "batch-owner",
        [valid, duplicate],
    ) == "invalid"


def test_replayed_committed_wakeup_retries_ack_without_provider(
    streaming,
    monkeypatch,
    tmp_path,
):
    bp, _config, models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached for committed replay",
    )
    event = _make_event(
        "replayed-process",
        time.time(),
        session_key="replayed-receipt",
    )
    terminal_error = {
        "role": "assistant",
        "content": "**Error:** the original wakeup already settled",
        "timestamp": int(time.time()),
        "_error": True,
    }
    assert streaming._stamp_process_completion_receipts(
        terminal_error,
        [event],
        session_id="replayed-receipt",
    )
    session = _make_session(
        tmp_path,
        "replayed-receipt",
        messages=[terminal_error],
    )
    session.save(skip_index=True)
    transcript_before = session.path.read_bytes()
    reg = _install_fake_registry(monkeypatch, [])

    _clear_staged_process_completion_events()
    try:
        assert bp.stage_process_completion_event(session.session_id, event) is True
        reg.fail_committed = True
        first = _call_process_wakeup_start(routes, session)
        assert first == {
            "delivery_already_committed": True,
            "delivery_acknowledged": False,
            "_status": 200,
        }
        assert reg.finish_calls == [(event, True)]
        assert reg.completion_queue.get_nowait() is event
        assert session.path.read_bytes() == transcript_before

        assert bp.stage_process_completion_event(session.session_id, event) is True
        reg.fail_committed = False
        second = _call_process_wakeup_start(
            routes,
            session,
            session_lock_held=True,
        )
        assert second == {
            "delivery_already_committed": True,
            "delivery_acknowledged": True,
            "_status": 200,
        }
        assert reg.finish_calls == [(event, True), (event, True)]
        assert session.path.read_bytes() == transcript_before
    finally:
        _clear_staged_process_completion_events()


@pytest.mark.parametrize("exception_index", [0, 1])
def test_committed_receipt_settlement_exception_preserves_every_claim(
    streaming,
    monkeypatch,
    tmp_path,
    caplog,
    exception_index,
):
    bp, _config, _models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached after settlement exception",
    )
    session_id = "settlement-exception-owner"
    events = [
        _make_event("settlement-first", time.time(), session_key=session_id),
        _make_event("settlement-second", time.time(), session_key=session_id),
    ]
    terminal = {"role": "assistant", "content": "already committed"}
    assert len(
        streaming._stamp_process_completion_receipts(
            terminal,
            events,
            session_id=session_id,
        )
    ) == 2
    session = _make_session(tmp_path, session_id, messages=[terminal])
    session.save(skip_index=True)
    reg = _install_fake_registry(monkeypatch, [])
    settlement_calls = []

    def fail_selected_settlement(event, committed):
        settlement_calls.append((event, committed))
        if len(settlement_calls) - 1 == exception_index:
            raise RuntimeError(f"{event['event_id']} {event['session_key']}")
        return True

    reg.finish_notification_delivery = fail_selected_settlement
    _clear_staged_process_completion_events()
    try:
        for event in events:
            assert bp.stage_process_completion_event(session_id, event) is True
        with caplog.at_level("WARNING"):
            response = _call_process_wakeup_start(routes, session)
        assert response == {
            "error": "Durable process-completion receipt is unavailable",
            "code": "durable_completion_receipt_unavailable",
            "retryable": True,
            "_status": 503,
        }
        assert settlement_calls == [
            (event, True) for event in events[: exception_index + 1]
        ]
        preserved = bp.claim_staged_process_completion_events(session_id)
        assert preserved == events
        assert all(actual is expected for actual, expected in zip(preserved, events))
        assert session_id not in caplog.text
        assert all(event["event_id"] not in caplog.text for event in events)
    finally:
        _clear_staged_process_completion_events()


def test_committed_receipt_settlement_exception_with_failed_restage_is_uncertain(
    streaming,
    monkeypatch,
    tmp_path,
):
    bp, _config, _models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached after uncertain settlement",
    )
    session_id = "uncertain-settlement-owner"
    events = [
        _make_event("uncertain-first", time.time(), session_key=session_id),
        _make_event("uncertain-second", time.time(), session_key=session_id),
    ]
    terminal = {"role": "assistant", "content": "already committed"}
    assert streaming._stamp_process_completion_receipts(
        terminal,
        events,
        session_id=session_id,
    )
    session = _make_session(tmp_path, session_id, messages=[terminal])
    session.save(skip_index=True)
    reg = _install_fake_registry(monkeypatch, [])
    reg.finish_notification_delivery = lambda event, committed: (_ for _ in ()).throw(
        RuntimeError("settlement unavailable")
    )
    restage_calls = []

    def fail_first_restage(owner, event):
        restage_calls.append((owner, event))
        return event is not events[0]

    monkeypatch.setattr(bp, "stage_process_completion_event", fail_first_restage)
    monkeypatch.setattr(
        bp,
        "claim_staged_process_completion_events",
        lambda _sid: list(events),
    )

    response = _call_process_wakeup_start(routes, session)

    assert response["_status"] == 500
    assert response["code"] == "durable_completion_claim_preservation_uncertain"
    assert response["retryable"] is True
    assert restage_calls == [(session_id, event) for event in events]


def test_receipt_lookup_failure_preserves_claim_without_provider(
    monkeypatch,
    tmp_path,
):
    bp, config, models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached while receipt was unavailable",
    )
    event = _make_event(
        "lookup-failure",
        time.time(),
        session_key="lookup-failure-owner",
    )
    session = _make_session(tmp_path, "lookup-failure-owner")
    session.save(skip_index=True)
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(
            lambda _cls, _sid: (_ for _ in ()).throw(OSError("sidecar unreadable"))
        ),
    )
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN[session.session_id] = {
            event["event_id"]
        }

    _clear_staged_process_completion_events()
    try:
        assert bp.stage_process_completion_event(session.session_id, event) is True
        response = _call_process_wakeup_start(routes, session)
        assert response["_status"] == 503
        assert response["code"] == "durable_completion_receipt_unavailable"
        assert response["retryable"] is True
        preserved = bp.claim_staged_process_completion_events(session.session_id)
        assert preserved == [event]
        assert preserved[0] is event
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            assert event["event_id"] not in config.BG_TASK_COMPLETE_EVENTS_SEEN.get(
                session.session_id,
                set(),
            )
    finally:
        _clear_staged_process_completion_events()
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            config.BG_TASK_COMPLETE_EVENTS_SEEN.pop(session.session_id, None)


@pytest.mark.parametrize(
    ("source", "session_lock_held"),
    [("webui", False), ("process_wakeup", True)],
)
def test_empty_process_completion_claims_continue_normal_admission(
    monkeypatch,
    tmp_path,
    source,
    session_lock_held,
):
    from api import models, routes

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    reached = []

    class PreparationReached(RuntimeError):
        pass

    def stop_at_preparation(session, **kwargs):
        reached.append((session.session_id, kwargs["source"]))
        raise PreparationReached

    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        stop_at_preparation,
    )
    session = _make_session(tmp_path, f"empty-claims-{source}")
    _clear_staged_process_completion_events()

    with pytest.raises(PreparationReached):
        routes._start_chat_stream_for_session(
            session,
            msg="ordinary admission",
            attachments=[],
            workspace=session.workspace,
            model=session.model,
            source=source,
            session_lock_held=session_lock_held,
        )

    assert reached == [(session.session_id, source)]


def test_absent_process_completion_receipt_continues_normal_admission(
    monkeypatch,
    tmp_path,
):
    from api import background_process as bp, models, routes

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    reached = []

    class PreparationReached(RuntimeError):
        pass

    def stop_at_preparation(session, **_kwargs):
        reached.append(session.session_id)
        raise PreparationReached

    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        stop_at_preparation,
    )
    session = _make_session(tmp_path, "absent-receipt-owner")
    session.save(skip_index=True)
    event = _make_event(
        "absent-receipt",
        time.time(),
        session_key=session.session_id,
    )
    _clear_staged_process_completion_events()
    try:
        assert bp.stage_process_completion_event(session.session_id, event) is True
        with pytest.raises(PreparationReached):
            _call_process_wakeup_start(routes, session)
        assert reached == [session.session_id]
    finally:
        _clear_staged_process_completion_events()


def test_invalid_mixed_claim_batch_restages_every_stable_claim_without_provider(
    monkeypatch,
    tmp_path,
):
    bp, _config, _models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached for invalid claims",
    )
    session = _make_session(tmp_path, "mixed-batch-owner")
    valid = _make_event("mixed-valid", time.time(), session_key=session.session_id)
    wrong_owner = _make_event(
        "mixed-wrong-owner",
        time.time(),
        session_key="different-mixed-owner",
    )
    raw_claims = [valid, wrong_owner]
    monkeypatch.setattr(
        bp,
        "claim_staged_process_completion_events",
        lambda _sid: list(raw_claims),
    )
    restage_calls = []

    def record_restage(owner, event):
        restage_calls.append((owner, event))
        return True

    monkeypatch.setattr(bp, "stage_process_completion_event", record_restage)

    response = _call_process_wakeup_start(routes, session)

    assert response["_status"] == 503
    assert response["code"] == "durable_completion_receipt_unavailable"
    assert restage_calls == [
        (session.session_id, valid),
        ("different-mixed-owner", wrong_owner),
    ]
    assert restage_calls[0][1] is valid
    assert restage_calls[1][1] is wrong_owner


def test_invalid_mixed_claim_preservation_failure_is_uncertain_and_exhaustive(
    streaming,
    monkeypatch,
    tmp_path,
    caplog,
):
    bp, config, _models, routes = _configure_receipt_route_without_provider(
        monkeypatch,
        tmp_path,
        "provider preparation reached after preservation failure",
    )
    session = _make_session(tmp_path, "failed-preservation-owner")
    first = _make_event("preserved-first", time.time(), session_key=session.session_id)
    failed = _make_event(
        "preservation-failed",
        time.time(),
        session_key="different-preservation-owner",
    )
    last = _make_event("preserved-last", time.time(), session_key=session.session_id)
    raw_claims = [first, failed, last]
    monkeypatch.setattr(
        bp,
        "claim_staged_process_completion_events",
        lambda _sid: list(raw_claims),
    )
    restage_calls = []

    def fail_one_restage(owner, event):
        restage_calls.append((owner, event))
        if event is failed:
            raise RuntimeError(f"{event['event_id']} {event['session_key']}")
        return True

    monkeypatch.setattr(bp, "stage_process_completion_event", fail_one_restage)
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN[session.session_id] = {
            first["event_id"],
            failed["event_id"],
            last["event_id"],
        }

    try:
        with caplog.at_level("WARNING"):
            response = _call_process_wakeup_start(routes, session)
        assert response["_status"] == 500
        assert response["code"] == "durable_completion_claim_preservation_uncertain"
        assert restage_calls == [
            (session.session_id, first),
            ("different-preservation-owner", failed),
            (session.session_id, last),
        ]
        opaque_token = streaming._process_completion_receipt_digest(
            failed,
            session_id=failed["session_key"],
        )
        assert opaque_token is not None
        assert failed["event_id"] not in caplog.text
        assert failed["session_key"] not in caplog.text
        assert f"type=completion index=1 token={opaque_token[:16]}" in caplog.text
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            # Seen ownership is released only when every exact claim is safe.
            assert first["event_id"] in config.BG_TASK_COMPLETE_EVENTS_SEEN[
                session.session_id
            ]
    finally:
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            config.BG_TASK_COMPLETE_EVENTS_SEEN.pop(session.session_id, None)
