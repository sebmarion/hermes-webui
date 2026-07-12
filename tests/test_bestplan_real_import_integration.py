"""Real-import crash-boundary proof for BestPlan -> Hermes -> WebUI delivery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types

import pytest


def _agent_modules(monkeypatch):
    root = os.environ.get("HERMES_AGENT_REPAIR_ROOT", "").strip()
    if not root:
        pytest.skip("set HERMES_AGENT_REPAIR_ROOT for coordinated Hermes/WebUI proof")
    monkeypatch.syspath_prepend(root)
    from agent import bestplan_state
    from tools import async_delegation

    required = (
        "recover_bestplan_dispatch_outbox",
        "BaselineFingerprintError",
    )
    if not all(hasattr(bestplan_state, name) for name in required):
        pytest.skip("installed Hermes lacks the coordinated BestPlan hardening contract")
    return bestplan_state, async_delegation


class _ImmediateThread:
    def __init__(self, *, target, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)

    def is_alive(self):
        return False


def test_real_checkpoint_crash_recovery_insert_ack_wakeup_and_restart(
    tmp_path, monkeypatch,
):
    bestplan, async_delegation = _agent_modules(monkeypatch)
    from api import background_process as bp
    from api import config as web_cfg
    from api.delegation_wakeup_store import DelegationWakeupStore

    hermes_home = tmp_path / "profiles" / "coder"
    hermes_home.mkdir(parents=True)
    tracker_path = hermes_home / "async_delegations.json"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PROFILE", "coder")

    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    (workspace / "file.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
    canonical = str(workspace.resolve())
    manifest = {
        "version": 1,
        "mode": "delegate",
        "risk": "low",
        "slices": [{
            "id": "work",
            "kind": "implement",
            "goal": "Update the leased file",
            "depends_on": [],
            "capability": "fast_fallback",
            "workspace": canonical,
            "allowed_paths": ["file.txt"],
            "read_only": False,
            "expected_artifacts": ["file.txt"],
            "acceptance": ["file is independently inspected"],
        }],
        "merge_policy": "No automatic integration.",
        "stop_condition": "Evidence is returned to the parent.",
        "escalation_predicates": ["independent_review_required"],
    }
    envelope = (
        f"{bestplan.BESTPLAN_ENVELOPE_START}\n"
        + json.dumps({"version": 1, "manifest": manifest})
        + f"\n{bestplan.BESTPLAN_ENVELOPE_END}"
    )
    plan_store = bestplan.BestplanStore(db_path=hermes_home / "state.db")
    capture = bestplan.capture_bestplan_response(
        "Plan commentary\n" + envelope,
        session_id="session-a",
        profile="coder",
        workspace=canonical,
        store=plan_store,
    )
    assert capture.executable is True

    def strict_dispatcher(**kwargs):
        return async_delegation.dispatch_async_delegation_batch(
            goals=[task["goal"] for task in kwargs["tasks"]],
            context="controlled integration runner",
            toolsets=None,
            role="leaf",
            model=kwargs["resolved_runtimes"][0]["model"],
            session_key="session-a",
            origin_ui_session_id="session-a",
            parent_session_id="session-a",
            runner=lambda: {
                "results": [{
                    "status": "completed",
                    "summary": "controlled child evidence",
                    "model": kwargs["resolved_runtimes"][0]["model"],
                }],
                "total_duration_seconds": 0.01,
            },
            max_async_children=1,
            delegation_id=kwargs["dispatch_id"],
            origin_profile="coder",
            origin_tracker_path=str(tracker_path),
            bestplan_plan_id=kwargs["plan_id"],
            resolved_runtimes=kwargs["resolved_runtimes"],
        )

    go = bestplan.try_resolve_go(
        "go",
        session_id="session-a",
        profile="coder",
        workspace=canonical,
        parent_agent=types.SimpleNamespace(),
        config={"autonomy": {"go_enabled": True}},
        store=plan_store,
        runtime_resolver=lambda _tasks, _parent: [{
            "route": "code_worker", "provider": "controlled", "model": "coder-real-record",
        }],
        strict_dispatcher=strict_dispatcher,
    )
    assert go.status == "waiting"

    deadline = time.monotonic() + 5
    original_event = None
    from tools.process_registry import process_registry
    while time.monotonic() < deadline and original_event is None:
        try:
            original_event = process_registry.completion_queue.get(timeout=0.1)
        except Exception:
            pass
    assert original_event is not None
    assert original_event["resolved_runtimes"][0]["model"] == "coder-real-record"
    assert plan_store.get_plan(capture.plan_id)["state"] == "completed_unverified"

    # Crash boundary: Hermes checkpoint exists, but WebUI never inserted the
    # event. A genuinely fresh Python process runs WebUI's profile-aware
    # startup recovery helper and returns the recovered canonical event.
    recovery_code = """
import json
import sys
from api import background_process as bp
from api import profiles
from tools.process_registry import process_registry
profiles.list_profiles_api = lambda: [{"name": "coder", "path": sys.argv[1]}]
queued = bp.recover_profile_async_delegations()
event = process_registry.completion_queue.get(timeout=2)
print("RECOVERY_JSON=" + json.dumps({"queued": queued, "event": event}))
"""
    recovery_env = dict(os.environ)
    recovery_env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        os.environ["HERMES_AGENT_REPAIR_ROOT"],
        str(os.getcwd()),
        recovery_env.get("PYTHONPATH", ""),
    )))
    recovered_process = subprocess.run(
        [sys.executable, "-c", recovery_code, str(hermes_home)],
        cwd=os.getcwd(),
        env=recovery_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    recovery_line = next(
        line for line in recovered_process.stdout.splitlines()
        if line.startswith("RECOVERY_JSON=")
    )
    recovery = json.loads(recovery_line.removeprefix("RECOVERY_JSON="))
    assert recovery["queued"] == 1
    replayed = recovery["event"]

    web_store_path = tmp_path / "webui" / "private" / "wakeups.sqlite3"
    web_store = DelegationWakeupStore(web_store_path)
    monkeypatch.setattr(bp, "_WAKEUP_STORE", web_store, raising=False)
    monkeypatch.setattr(bp, "_WAKEUP_THREAD_FACTORY", _ImmediateThread, raising=False)
    monkeypatch.setattr(
        bp,
        "_load_async_event_session",
        lambda sid, profile: types.SimpleNamespace(session_id=sid, profile=profile),
    )
    starts = []
    routes = types.ModuleType("api.routes")
    routes.start_session_turn = lambda sid, prompt, *, source: (
        starts.append((sid, prompt, source)) or {"_status": 200, "stream_id": "controlled"}
    )
    monkeypatch.setitem(sys.modules, "api.routes", routes)
    with web_cfg.PROCESS_SESSION_INDEX_LOCK:
        web_cfg.PROCESS_SESSION_INDEX.clear()

    bp._process_one(replayed)

    delivered = web_store.get(go.delegation_id)
    assert delivered["state"] == "delivered"
    assert delivered["tracker_acked_at"] is not None
    assert starts and starts[0][0] == "session-a"
    persisted = async_delegation._read_persisted_unlocked(tracker_path)
    assert persisted["records"][go.delegation_id]["delivery_status"] == "delivered"

    web_store.close()
    restarted = DelegationWakeupStore(web_store_path)
    monkeypatch.setattr(bp, "_WAKEUP_STORE", restarted, raising=False)
    assert bp.replay_pending_delegation_wakeups() == 0


def test_real_two_profile_trackers_never_cross_ack(tmp_path, monkeypatch):
    _bestplan, async_delegation = _agent_modules(monkeypatch)
    from api import background_process as bp
    from api.delegation_wakeup_store import DelegationWakeupStore
    from tools.process_registry import process_registry

    store = DelegationWakeupStore(tmp_path / "webui" / "private" / "wakeups.sqlite3")
    monkeypatch.setattr(bp, "_WAKEUP_STORE", store, raising=False)
    monkeypatch.setattr(bp, "_WAKEUP_THREAD_FACTORY", _ImmediateThread, raising=False)
    monkeypatch.setattr(
        bp,
        "_load_async_event_session",
        lambda sid, profile: types.SimpleNamespace(session_id=sid, profile=profile),
    )
    routes = types.ModuleType("api.routes")
    routes.start_session_turn = lambda sid, prompt, *, source: {
        "_status": 200, "stream_id": f"stream-{sid}",
    }
    monkeypatch.setitem(sys.modules, "api.routes", routes)

    trackers = {}
    for profile in ("coder", "reviewer"):
        tracker = tmp_path / profile / "async_delegations.json"
        trackers[profile] = tracker
        result = async_delegation.dispatch_async_delegation_batch(
            goals=[f"{profile} work"], context=None, toolsets=None, role="leaf",
            model=f"{profile}-model", session_key=f"session-{profile}",
            origin_ui_session_id=f"session-{profile}",
            parent_session_id=f"session-{profile}",
            runner=lambda p=profile: {
                "results": [{"status": "completed", "summary": f"{p} evidence"}]
            },
            max_async_children=4,
            delegation_id=f"bestplan-{profile}",
            origin_profile=profile,
            origin_tracker_path=str(tracker),
            bestplan_plan_id="",
            resolved_runtimes=[{
                "route": "code_worker", "provider": "controlled", "model": f"{profile}-model",
            }],
        )
        assert result["status"] == "dispatched"

    events = {}
    deadline = time.monotonic() + 5
    while len(events) < 2 and time.monotonic() < deadline:
        event = process_registry.completion_queue.get(timeout=1)
        if event.get("delegation_id") in {"bestplan-coder", "bestplan-reviewer"}:
            events[event["origin_profile"]] = event
    assert set(events) == {"coder", "reviewer"}

    for profile, event in events.items():
        bp._process_one(event)
        own = async_delegation._read_persisted_unlocked(trackers[profile])
        other_profile = "reviewer" if profile == "coder" else "coder"
        other = async_delegation._read_persisted_unlocked(trackers[other_profile])
        assert own["records"][event["delegation_id"]]["delivery_status"] == "delivered"
        assert event["delegation_id"] not in other["records"]
