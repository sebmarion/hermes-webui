"""Behavioral tests for issue #4029 stale-completion age gate.

These exercise the real `_drain_webui_process_notifications` against a fake
process_registry, proving that:
  - a completion older than the cap is dropped (consumed, not requeued),
  - a fresh completion is still delivered,
  - events without `created_at` are never dropped,
  - the env override (incl. disable via 0) is honored.
"""
import importlib
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
