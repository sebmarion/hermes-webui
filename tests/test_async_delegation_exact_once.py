"""Durable exact-once async-delegation delivery contracts."""

from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.delegation_wakeup_store import DelegationWakeupStore


class _ImmediateThread:
    def __init__(self, *, target, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def _event(delegation_id="deleg_1", session_key="session-a"):
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_id": f"proc-{delegation_id}",
        "session_key": session_key,
        "status": "completed",
        "summary": "child finished",
    }


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    from api import background_process as bp
    from api import config as cfg

    store = DelegationWakeupStore(tmp_path / "wakeups.sqlite3")
    monkeypatch.setattr(bp, "_WAKEUP_STORE", store, raising=False)
    monkeypatch.setattr(bp, "format_wakeup_prompt", lambda evt: f"[IMPORTANT: {evt['delegation_id']} finished]")
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bp, "_WAKEUP_THREAD_FACTORY", _ImmediateThread, raising=False)
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        cfg.PROCESS_SESSION_INDEX.clear()
        cfg.PROCESS_SESSION_INDEX["session-a"] = "session-a"
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    return bp, cfg, store


def _install_start(monkeypatch, responses=None):
    calls = []
    responses = list(responses or [{"_status": 200, "stream_id": "stream-1"}])
    fake = types.ModuleType("api.routes")

    def start_session_turn(session_id, message, *, source, delegation_id):
        calls.append({
            "session_id": session_id, "message": message, "source": source,
            "delegation_id": delegation_id,
        })
        return responses.pop(0) if responses else {"_status": 200, "stream_id": "stream-more"}

    fake.start_session_turn = start_session_turn
    monkeypatch.setitem(sys.modules, "api.routes", fake)
    return calls


def _install_tracker(monkeypatch, calls):
    import tools

    module = types.ModuleType("tools.async_delegation")

    def mark_async_delegation_delivered(evt):
        calls.append(evt)
        return True

    module.mark_async_delegation_delivered = mark_async_delegation_delivered
    monkeypatch.setitem(sys.modules, "tools.async_delegation", module)
    monkeypatch.setattr(tools, "async_delegation", module, raising=False)


def test_idle_and_closed_tab_delivery_persists_then_acks_then_starts(delivery, monkeypatch):
    bp, _cfg, store = delivery
    starts = _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    order = []
    real_record = store.record_pending

    def record(**kwargs):
        result = real_record(**kwargs)
        order.append("durable_insert")
        return result

    monkeypatch.setattr(store, "record_pending", record)
    real_ack = bp._try_mark_async_delegation_tracker

    def ack(*, evt):
        order.append("tracker_ack")
        return real_ack(evt=evt)

    monkeypatch.setattr(bp, "_try_mark_async_delegation_tracker", ack)

    bp._process_one(_event())

    assert order[:2] == ["durable_insert", "tracker_ack"]
    assert [evt["delegation_id"] for evt in tracker] == ["deleg_1"]
    assert len(starts) == 1  # no SessionChannel/browser subscriber required
    assert store.get("deleg_1")["state"] == "delivered"
    assert store.get("deleg_1")["tracker_acked_at"] is not None


def test_active_turn_remains_durable_pending_until_idle_claim(delivery, monkeypatch):
    bp, cfg, store = delivery
    starts = _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS["active"] = {"session_id": "session-a"}

    bp._process_one(_event())

    assert starts == []
    assert store.get("deleg_1")["state"] == "pending"
    assert len(tracker) == 1
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    assert bp.drain_deferred_wakeups_for_session("session-a") == 1
    assert len(starts) == 1
    assert store.get("deleg_1")["state"] == "delivered"


def test_restart_replays_pending_once_from_fresh_store(delivery, monkeypatch):
    bp, cfg, store = delivery
    starts = _install_start(monkeypatch)
    _install_tracker(monkeypatch, [])
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS["active"] = {"session_id": "session-a"}
    bp._process_one(_event())
    path = store.path
    store.close()

    restarted = DelegationWakeupStore(path)
    monkeypatch.setattr(bp, "_WAKEUP_STORE", restarted)
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    assert bp.replay_pending_delegation_wakeups() == 1
    assert bp.replay_pending_delegation_wakeups() == 0
    assert len(starts) == 1
    assert restarted.get("deleg_1")["state"] == "delivered"


def test_concurrent_duplicate_event_starts_once(delivery, monkeypatch):
    bp, _cfg, store = delivery
    starts = _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    evt = _event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _n: bp._process_one(dict(evt)), range(16)))
    assert len(starts) == 1
    assert len(tracker) == 1
    assert store.get("deleg_1")["state"] == "delivered"


def test_concurrent_duplicate_active_event_acks_once(delivery, monkeypatch):
    bp, cfg, store = delivery
    _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS["active"] = {"session_id": "session-a"}

    evt = _event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _n: bp._process_one(dict(evt)), range(16)))

    assert len(tracker) == 1
    assert store.get("deleg_1")["state"] == "pending"
    assert store.get("deleg_1")["tracker_acked_at"] is not None


def test_cross_session_collision_fails_closed_without_second_ack(delivery, monkeypatch):
    bp, cfg, store = delivery
    _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS["active"] = {"session_id": "session-a"}
    bp._process_one(_event())
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        cfg.PROCESS_SESSION_INDEX["session-b"] = "session-b"
    bp._process_one(_event(session_key="session-b"))
    assert len(tracker) == 1
    assert store.get("deleg_1")["session_id"] == "session-a"


@pytest.mark.parametrize("failure", ["mapping", "format", "persist"])
def test_drop_or_persistence_failure_never_acks(delivery, monkeypatch, failure):
    bp, cfg, store = delivery
    _install_start(monkeypatch)
    tracker = []
    _install_tracker(monkeypatch, tracker)
    if failure == "mapping":
        with cfg.PROCESS_SESSION_INDEX_LOCK:
            cfg.PROCESS_SESSION_INDEX.clear()
    elif failure == "format":
        monkeypatch.setattr(bp, "format_wakeup_prompt", lambda _evt: None)
    else:
        monkeypatch.setattr(store, "record_pending", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    bp._process_one(_event())

    assert tracker == []
    assert store.get("deleg_1") is None


def test_idle_thread_start_failure_leaves_pending_for_replay(delivery, monkeypatch):
    bp, _cfg, store = delivery
    _install_start(monkeypatch)
    _install_tracker(monkeypatch, [])

    class BrokenThread(_ImmediateThread):
        def start(self):
            raise RuntimeError("cannot start")

    monkeypatch.setattr(bp, "_WAKEUP_THREAD_FACTORY", BrokenThread)
    bp._process_one(_event())
    assert store.get("deleg_1")["state"] == "pending"


def test_active_to_idle_race_releases_claim_then_replays(delivery, monkeypatch):
    bp, _cfg, store = delivery
    starts = _install_start(
        monkeypatch,
        responses=[{"_status": 409, "error": "active"}, {"_status": 200, "stream_id": "ok"}],
    )
    _install_tracker(monkeypatch, [])
    bp._process_one(_event())
    assert store.get("deleg_1")["state"] == "pending"
    assert bp.drain_deferred_wakeups_for_session("session-a") == 1
    assert len(starts) == 2
    assert store.get("deleg_1")["state"] == "delivered"


def test_real_tracker_signature_is_single_event_argument(delivery, monkeypatch):
    bp, _cfg, _store = delivery
    seen = []
    _install_tracker(monkeypatch, seen)
    bp._try_mark_async_delegation_tracker(evt=_event())
    assert seen[0]["delegation_id"] == "deleg_1"


def test_generic_process_completion_does_not_enter_delegation_store(delivery, monkeypatch):
    bp, cfg, store = delivery
    calls = []
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(bp, "format_wakeup_prompt", lambda _evt: "[IMPORTANT: process done]")
    fake_registry = types.SimpleNamespace(
        get=lambda _pid: types.SimpleNamespace(session_key="session-a"),
        is_completion_consumed=lambda _pid: False,
    )
    module = types.ModuleType("tools.process_registry")
    module.process_registry = fake_registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", module)
    with cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()

    bp._process_one({"type": "completion", "session_id": "proc-1", "exit_code": 0})

    assert len(calls) == 1
    assert store.list_pending() == []


def test_streaming_uses_canonical_delivery_context_without_made_up_env_flag(monkeypatch):
    import api.streaming as streaming

    env = streaming._build_agent_thread_env({}, "/tmp/work", "session-a", "/tmp/home")
    assert "HERMES_SUPPORTS_DETACHED_COMPLETION" not in env

    calls = []
    token = object()
    module = types.ModuleType("gateway.session_context")

    def bind_delivery_context(**kwargs):
        calls.append(("bind", kwargs))
        return token

    def reset_delivery_context(value):
        calls.append(("reset", value))

    module.bind_delivery_context = bind_delivery_context
    module.reset_delivery_context = reset_delivery_context
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)

    tokens = streaming._set_turn_session_identity("session-a")
    streaming._reset_turn_session_identity(tokens)

    assert calls == [
        (
            "bind",
            {
                "session_key": "session-a",
                "session_id": "session-a",
                "ui_session_id": "session-a",
                "async_delivery": True,
                "profile": "",
                "hermes_home": "",
                "capability_version": 1,
            },
        ),
        ("reset", token),
    ]


def test_delivered_row_repairs_missing_tracker_ack_before_early_return(delivery, monkeypatch):
    bp, _cfg, store = delivery
    tracker = []
    _install_tracker(monkeypatch, tracker)
    store.record_pending(
        delegation_id="deleg_1",
        session_id="session-a",
        session_key="session-a",
        wakeup_prompt="done",
        event=_event(),
    )
    claim = store.claim_next("session-a", owner_uuid="old", lease_seconds=60)
    assert store.mark_delivered("deleg_1", claim["claim_token"])
    assert store.get("deleg_1")["tracker_acked_at"] is None

    bp._process_one(_event())

    assert len(tracker) == 1
    assert store.get("deleg_1")["tracker_acked_at"] is not None


def test_event_routes_from_canonical_ui_identity_without_process_index(delivery, monkeypatch):
    bp, cfg, store = delivery
    starts = _install_start(monkeypatch)
    _install_tracker(monkeypatch, [])
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        cfg.PROCESS_SESSION_INDEX.clear()
    monkeypatch.setattr(
        bp,
        "_load_async_event_session",
        lambda sid, profile: types.SimpleNamespace(session_id=sid, profile=profile),
        raising=False,
    )
    evt = _event()
    evt.update({
        "origin_ui_session_id": "session-a",
        "parent_session_id": "session-a",
        "origin_profile": "coder",
        "origin_tracker_path": "/tmp/coder/async_delegations.json",
    })

    bp._process_one(evt)

    assert len(starts) == 1
    row = store.get("deleg_1")
    assert row["origin_profile"] == "coder"
    assert row["origin_tracker_path"].endswith("coder/async_delegations.json")


def test_legacy_process_index_fallback_still_enforces_origin_profile(delivery, monkeypatch):
    bp, cfg, store = delivery
    _install_start(monkeypatch)
    _install_tracker(monkeypatch, [])
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        cfg.PROCESS_SESSION_INDEX["legacy-key"] = "wrong-profile-session"

    checked = []

    def load_session(session_id, origin_profile):
        checked.append((session_id, origin_profile))
        return None

    monkeypatch.setattr(bp, "_load_async_event_session", load_session)
    evt = _event(session_key="legacy-key")
    evt["origin_profile"] = "coder"

    bp._process_one(evt)

    assert ("wrong-profile-session", "coder") in checked
    assert store.get("deleg_1") is None


def test_tracker_ack_requires_explicit_durable_success(delivery, monkeypatch):
    bp, _cfg, store = delivery
    starts = _install_start(monkeypatch)
    module = types.ModuleType("tools.async_delegation")
    module.mark_async_delegation_delivered = lambda _evt: False
    monkeypatch.setitem(sys.modules, "tools.async_delegation", module)

    bp._process_one(_event())

    assert store.get("deleg_1")["tracker_acked_at"] is None
    assert len(starts) == 1
