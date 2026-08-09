"""Real-import crash-boundary proof for BestPlan -> Hermes -> WebUI delivery."""

from __future__ import annotations

import json
import os
import queue
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


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


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
    # event. A genuinely fresh Python process gets the canonical event from
    # ProcessRegistry's automatic startup recovery, without a second manual
    # profile sweep or duplicate queue publication.
    recovery_code = """
import json
import queue
from api import background_process as bp
from tools.process_registry import process_registry
event = process_registry.completion_queue.get(timeout=2)
try:
    duplicate = process_registry.completion_queue.get_nowait()
except queue.Empty:
    duplicate = None
print("RECOVERY_JSON=" + json.dumps({"event": event, "duplicate": duplicate}))
"""
    recovery_env = dict(os.environ)
    recovery_env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        os.environ["HERMES_AGENT_REPAIR_ROOT"],
        str(os.getcwd()),
        recovery_env.get("PYTHONPATH", ""),
    )))
    recovered_process = subprocess.run(
        [sys.executable, "-c", recovery_code],
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
    replayed = recovery["event"]
    assert replayed["restored"] is True
    assert replayed["delegation_id"] == go.delegation_id
    assert recovery["duplicate"] is None

    web_store_path = tmp_path / "webui" / "private" / "wakeups.sqlite3"
    web_store = DelegationWakeupStore(web_store_path)
    monkeypatch.setattr(bp, "_WAKEUP_STORE", web_store, raising=False)
    from api import models

    session = models.Session(
        session_id="session-a", title="BestPlan", messages=[],
        workspace=canonical, profile="coder",
        model="openai/gpt-5.4-mini", model_provider="openai",
    )
    monkeypatch.setitem(models.SESSIONS, session.session_id, session)
    with web_cfg.PROCESS_SESSION_INDEX_LOCK:
        web_cfg.PROCESS_SESSION_INDEX.clear()
    parent_turns = []
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda *args, **kwargs: parent_turns.append((args, kwargs)),
    )

    bp._process_one(replayed)

    delivered = web_store.get(go.delegation_id)
    assert delivered["state"] == "observed"
    assert delivered["tracker_acked_at"] is not None
    assert parent_turns == []
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
    from api import models

    trackers = {}
    for profile in ("coder", "reviewer"):
        session = models.Session(
            session_id=f"session-{profile}", title=profile, messages=[],
            workspace=str(tmp_path), profile=profile,
            model="openai/gpt-5.4-mini", model_provider="openai",
        )
        monkeypatch.setitem(models.SESSIONS, session.session_id, session)
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
            bestplan_plan_id=f"bp-{profile}",
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

    parent_turns = []
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda *args, **kwargs: parent_turns.append((args, kwargs)),
    )
    for profile, event in events.items():
        bp._process_one(event)
        assert store.get(event["delegation_id"])["state"] == "observed"
        own = async_delegation._read_persisted_unlocked(trackers[profile])
        other_profile = "reviewer" if profile == "coder" else "coder"
        other = async_delegation._read_persisted_unlocked(trackers[other_profile])
        assert own["records"][event["delegation_id"]]["delivery_status"] == "delivered"
        assert event["delegation_id"] not in other["records"]
    assert parent_turns == []


def test_real_generic_target_turn_once_across_duplicate_restart_two_profiles_and_closed_tab(
    tmp_path, monkeypatch,
):
    # Pin the production legacy-direct adapter because provider execution is
    # the sole stub in this integration. A developer's runner-local shell
    # setting must not redirect the turn into an unconfigured external runner.
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    _bestplan, async_delegation = _agent_modules(monkeypatch)
    from api import background_process as bp
    from api import models, process_event_utils, routes, turn_journal
    from tools.process_registry import process_registry

    process_event_utils._reset_legacy_async_delivery_dedupe_for_tests()
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda _session, _requested: str(tmp_path),
    )
    provider_calls = []
    monkeypatch.setattr(
        routes, "_run_agent_streaming",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )
    claims = []
    real_claim = bp.claim_async_delegation_delivery

    def record_claim(event, consumer):
        claim = real_claim(event, consumer)
        claims.append(claim)
        return claim

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", record_claim)
    wakeup_starts = []
    real_start = bp._start_async_delegation_wakeup_turn

    def record_start(*args, **kwargs):
        wakeup_starts.append((args, kwargs))
        return real_start(*args, **kwargs)

    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", record_start)
    admissions = []
    real_start_session_turn = routes.start_session_turn

    def record_admission(*args, **kwargs):
        try:
            response = real_start_session_turn(*args, **kwargs)
        except Exception as exc:
            admissions.append({"exception": repr(exc)})
            raise
        admissions.append(response)
        return response

    monkeypatch.setattr(routes, "start_session_turn", record_admission)

    events = {}
    trackers = {}
    for profile in ("coder", "reviewer"):
        sid = f"target-{profile}"
        session = models.Session(
            session_id=sid, title=profile, messages=[], workspace=str(tmp_path),
            profile=profile, model="openai/gpt-5.4-mini", model_provider="openai",
        )
        monkeypatch.setitem(models.SESSIONS, sid, session)
        tracker = tmp_path / profile / "async_delegations.json"
        trackers[profile] = tracker
        dispatched = async_delegation.dispatch_async_delegation_batch(
            goals=[f"{profile} generic"], context=None, toolsets=None, role="leaf",
            model="controlled", session_key=sid, origin_ui_session_id=sid,
            parent_session_id=sid,
            runner=lambda p=profile: {
                "results": [{"status": "completed", "summary": f"{p} result"}]
            },
            max_async_children=4, delegation_id=f"generic-{profile}",
            origin_profile=profile, origin_tracker_path=str(tracker),
            bestplan_plan_id="", resolved_runtimes=[],
        )
        assert dispatched["status"] == "dispatched"

    deadline = time.monotonic() + 5
    while len(events) < 2 and time.monotonic() < deadline:
        event = process_registry.completion_queue.get(timeout=1)
        if str(event.get("delegation_id", "")).startswith("generic-"):
            events[event["origin_profile"]] = event
    assert set(events) == {"coder", "reviewer"}
    assert all(
        not bp._session_has_active_turn(f"target-{profile}")
        for profile in events
    )

    for event in events.values():
        bp._process_one(event)
    assert len(claims) == 2
    assert all(claim is not None for claim in claims)
    assert len(wakeup_starts) == 2
    assert _wait_until(lambda: len(admissions) == 2)
    assert all(result.get("stream_id") for result in admissions), admissions
    assert _wait_until(lambda: len(provider_calls) == 2)
    assert _wait_until(
        lambda: all(
            async_delegation._read_persisted_unlocked(trackers[profile])
            ["records"][event["delegation_id"]]["delivery_status"]
            == "delivered"
            for profile, event in events.items()
        )
    )

    for event in events.values():
        bp._process_one(dict(event))
    assert claims[2:] == [None, None]
    assert len(provider_calls) == 2
    for profile, event in events.items():
        journal = turn_journal.read_turn_journal(f"target-{profile}")["events"]
        assert sum(
            item.get("event") == "worker_started"
            and item.get("delegation_id") == event["delegation_id"]
            for item in journal
        ) == 1

    # A restarted Agent recovers from the per-profile tracker. Delivered rows
    # are terminal, so neither profile republishes a parent-turn event.
    for profile in ("coder", "reviewer"):
        recovered = queue.Queue()
        result = async_delegation.recover_async_delegations(
            trackers[profile], target_queue=recovered, mark_restored=True,
        )
        assert result["queued"] == 0
        assert recovered.empty()
    assert len(provider_calls) == 2
