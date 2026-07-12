from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace


def test_bestplan_completion_is_observational_and_never_starts_parent_turn(tmp_path, monkeypatch):
    from api import background_process as bp
    from api.delegation_wakeup_store import DelegationWakeupStore

    store = DelegationWakeupStore(tmp_path / "private" / "wakeups.sqlite3")
    monkeypatch.setattr(bp, "_WAKEUP_STORE", store, raising=False)
    monkeypatch.setattr(
        bp, "_load_async_event_session",
        lambda sid, profile: SimpleNamespace(session_id=sid, profile=profile),
    )
    ack_states = []

    def ack_tracker(**kwargs):
        ack_states.append(store.get(kwargs["evt"]["delegation_id"])["state"])
        return True

    monkeypatch.setattr(bp, "_try_mark_async_delegation_tracker", ack_tracker)
    starts = []
    monkeypatch.setattr(
        bp, "dispatch_pending_delegation_wakeups_for_session",
        lambda sid: starts.append(sid) or 1,
    )
    emitted = []
    monkeypatch.setattr(
        bp, "_emit_bg_task_complete_events_coalesced",
        lambda sid, payload: emitted.append((sid, payload)),
    )

    event = {
        "type": "async_delegation",
        "delegation_id": "bestplan-bp-1",
        "bestplan_plan_id": "bp-1",
        "session_key": "session-a",
        "origin_ui_session_id": "session-a",
        "origin_profile": "coder",
        "origin_tracker_path": str(tmp_path / "coder" / "async_delegations.json"),
        "status": "completed",
        "results": [{"status": "completed", "summary": "evidence only"}],
    }
    bp._process_async_delegation_event(event)

    row = store.get("bestplan-bp-1")
    assert row["state"] == "observed"
    assert row["tracker_acked_at"] is not None
    assert ack_states == ["observed"]
    assert starts == []
    assert emitted and emitted[0][0] == "session-a"


def test_shutdown_waits_for_blocking_wakeup_worker_before_returning(monkeypatch):
    from api import background_process as bp

    gate = threading.Event()
    worker = threading.Thread(target=lambda: gate.wait(), daemon=True)
    worker.start()
    with bp._WAKEUP_THREADS_LOCK:
        bp._WAKEUP_THREADS.add(worker)

    returned = threading.Event()
    stopper = threading.Thread(
        target=lambda: (bp.stop_drain_thread(timeout=None), returned.set()),
        daemon=True,
    )
    stopper.start()
    time.sleep(0.05)
    assert not returned.is_set()
    gate.set()
    stopper.join(timeout=2)
    assert returned.is_set()
    bp._WAKEUP_INTAKE_STOP.clear()


def test_target_turn_delegation_id_is_durable_and_duplicate_returns_same_turn(tmp_path, monkeypatch):
    from api import models, routes, turn_journal

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(turn_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    session = models.Session(
        session_id="session-a", title="Test", messages=[], workspace=str(tmp_path),
        model="openai/gpt-5.4-mini", model_provider="openai",
    )
    monkeypatch.setitem(models.SESSIONS, session.session_id, session)
    calls = []
    monkeypatch.setattr(
        routes, "_run_agent_streaming",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    first = routes.start_session_turn(
        session.session_id, "completion", delegation_id="deleg-1"
    )
    second = routes.start_session_turn(
        session.session_id, "completion", delegation_id="deleg-1"
    )
    assert first["turn_id"] == second["turn_id"]
    assert second["idempotent_replay"] is True
    assert len(calls) == 1
    events = turn_journal.read_turn_journal(session.session_id)["events"]
    assert sum(
        e.get("delegation_id") == "deleg-1" and e.get("event") == "submitted"
        for e in events
    ) == 1


def test_target_turn_fails_closed_when_submitted_journal_cannot_persist(tmp_path, monkeypatch):
    from api import models, routes, turn_journal

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(turn_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    session = models.Session(
        session_id="session-journal-fail", title="Test", messages=[],
        workspace=str(tmp_path), model="openai/gpt-5.4-mini",
        model_provider="openai",
    )
    monkeypatch.setitem(models.SESSIONS, session.session_id, session)
    provider_calls = []
    monkeypatch.setattr(
        routes, "_run_agent_streaming",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )
    real_append = turn_journal.append_turn_journal_event

    def fail_submitted(session_id, event, **kwargs):
        if event.get("event") == "submitted":
            raise OSError("disk unavailable")
        return real_append(session_id, event, **kwargs)

    monkeypatch.setattr(turn_journal, "append_turn_journal_event", fail_submitted)
    response = routes.start_session_turn(
        session.session_id, "completion", delegation_id="deleg-journal-fail",
    )
    assert response["_status"] == 503
    assert provider_calls == []


def test_concurrent_durable_reservation_has_one_live_owner(tmp_path, monkeypatch):
    from api import turn_journal

    monkeypatch.setattr(turn_journal, "_default_session_dir", lambda: tmp_path / "sessions")
    first, claimed = turn_journal.reserve_delegation_turn("session-a", "deleg-race")
    assert claimed is True
    second, claimed_again = turn_journal.reserve_delegation_turn("session-a", "deleg-race")
    assert claimed_again is False
    assert second["turn_id"] == first["turn_id"]

    turn_journal.release_delegation_turn(
        "session-a", "deleg-race", reason="controlled failure",
    )
    recovered, claimed_after_release = turn_journal.reserve_delegation_turn(
        "session-a", "deleg-race",
    )
    assert claimed_after_release is True
    assert recovered["turn_id"] != first["turn_id"]


def test_fresh_process_crash_after_start_before_wakeup_mark_never_starts_second_turn(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ)
    env.update({
        "HERMES_WEBUI_STATE_DIR": str(state),
        "HERMES_HOME": str(state),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(filter(None, (os.getcwd(), env.get("PYTHONPATH", "")))),
    })
    first_code = r'''
import os
from api import models, routes
s = models.Session(session_id="crash-session", title="Crash", messages=[],
                   workspace=os.environ["WORKSPACE"],
                   model="openai/gpt-5.4-mini", model_provider="openai")
models.SESSIONS[s.session_id] = s
s.save()
routes._run_agent_streaming = lambda *args, **kwargs: None
response = routes.start_session_turn(s.session_id, "completion", delegation_id="deleg-crash")
assert response.get("stream_id")
os._exit(17)
'''
    env["WORKSPACE"] = str(tmp_path)
    first = subprocess.run([sys.executable, "-c", first_code], env=env, cwd=os.getcwd())
    assert first.returncode == 17

    second_code = r'''
import json
from api import routes
calls = []
routes._run_agent_streaming = lambda *args, **kwargs: calls.append(1)
response = routes.start_session_turn("crash-session", "completion", delegation_id="deleg-crash")
print("RESULT=" + json.dumps({"response": response, "calls": calls}))
'''
    second = subprocess.run(
        [sys.executable, "-c", second_code], env=env, cwd=os.getcwd(),
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(next(
        line.removeprefix("RESULT=") for line in second.stdout.splitlines()
        if line.startswith("RESULT=")
    ))
    assert payload["response"]["idempotent_replay"] is True
    assert payload["calls"] == []
