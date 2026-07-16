import json
import time
from pathlib import Path

import pytest

from api import config, models
from api.models import Session
import api.tool_limit_continuation as tlc


@pytest.fixture
def store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(tlc, "progress_fingerprint", lambda _s: "progress-a")
    monkeypatch.setattr(
        tlc,
        "settings_for_session",
        lambda _s: {
            "enabled": True,
            "max_segments": 12,
            "max_wall_seconds": 14400,
            "no_progress_limit": 3,
        },
    )
    return session_dir


def parent(**kwargs):
    return Session(
        title="Durable task",
        workspace=kwargs.pop("workspace", str(Path.home())),
        model="test-model",
        model_provider="test-provider",
        profile="work",
        project_id="project-1",
        personality="technical",
        enabled_toolsets=["terminal", "web"],
        context_messages=[{"role": "user", "content": "do the task"}],
        **kwargs,
    )


def test_duplicate_terminal_callback_creates_and_starts_one_child(store):
    p = parent()
    p.save()
    starts = []
    events = []
    start = lambda sid, prompt: starts.append((sid, prompt)) or {"stream_id": "child-run"}
    emit = lambda name, payload: events.append((name, payload))

    first = tlc.handle_terminal(p, "parent-run", tool_limit_reached=True, start=start, emit=emit)
    second = tlc.handle_terminal(p, "parent-run", tool_limit_reached=True, start=start, emit=emit)

    assert first["child_session_id"] == second["child_session_id"]
    assert len(starts) == 1
    assert len([p for p in store.glob("*.json") if p.name != "_index.json"]) == 3  # parent, child, receipt
    assert events[0][0] == "tool_limit_continuation"
    assert events[0][1]["parent_run_id"] == "parent-run"
    assert set(events[0][1]) == {
        "execution_id", "root_session_id", "parent_session_id", "child_session_id",
        "parent_run_id", "continuation_index", "state",
    }


def test_restart_recovery_starts_claimed_receipt_once(store):
    p = parent()
    p.save()
    tlc.handle_terminal(
        p, "run-1", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 500},
    )
    starts = []
    start = lambda sid, prompt: starts.append((sid, prompt)) or {"stream_id": "recovered"}

    assert tlc.recover_pending_continuations(start=start) == 1
    assert tlc.recover_pending_continuations(start=start) == 0
    assert len(starts) == 1


def test_explicit_start_failure_and_competing_409_remain_retryable(store):
    p = parent()
    p.save()

    failed = tlc.handle_terminal(
        p,
        "run-retry",
        tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 409, "active_stream_id": "competing"},
    )

    assert failed["state"] == "claimed"
    starts = []
    assert tlc.recover_pending_continuations(
        start=lambda sid, prompt: starts.append((sid, prompt)) or {"stream_id": "retry-stream"}
    ) == 1
    assert len(starts) == 1


def test_start_exception_preserves_claim_for_restart_recovery(store):
    p = parent()
    p.save()

    def fail_start(_sid, _prompt):
        raise RuntimeError("launch failed")

    receipt = tlc.handle_terminal(
        p,
        "run-exception",
        tool_limit_reached=True,
        start=fail_start,
    )

    assert receipt["state"] == "claimed"
    assert tlc.recover_pending_continuations(
        start=lambda _sid, _prompt: {"stream_id": "recovered-stream"}
    ) == 1


def test_restart_reclaims_dead_process_start_reservation(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p,
        "run-dead-owner",
        tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 500},
    )
    key = receipt["claim_key"]
    with tlc._store_lock():
        persisted = tlc._load_store()
        persisted["receipts"][key].update(
            {
                "state": "starting",
                "owner_pid": 999_999_999,
                "start_token": "abandoned",
            }
        )
        tlc._save_store(persisted)

    assert tlc.recover_pending_continuations(
        start=lambda _sid, _prompt: {"stream_id": "reclaimed-stream"}
    ) == 1
    assert tlc.load_receipts()["receipts"][key]["state"] == "started"


def test_restart_reconciles_409_only_with_live_worker_proof(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p,
        "run-started-before-crash",
        tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 500},
    )
    child = Session.load(receipt["child_session_id"])
    child.active_stream_id = "already-running"
    child.pending_user_source = tlc.SOURCE
    child.pending_started_at = 1
    child.save()

    assert tlc.recover_pending_continuations(
        start=lambda _sid, _prompt: {
            "_status": 409,
            "active_stream_id": "already-running",
            "active_stream_confirmed_live": True,
        }
    ) == 1
    persisted = tlc.load_receipts()["receipts"][receipt["claim_key"]]
    assert persisted["state"] == "started"
    assert persisted["child_stream_id"] == "already-running"


def test_restart_does_not_accept_stale_hidden_child_sidecar_as_live_worker(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p,
        "run-stale-before-crash",
        tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 500},
    )
    child = Session.load(receipt["child_session_id"])
    child.active_stream_id = "stale-stream"
    child.pending_user_source = tlc.SOURCE
    child.pending_started_at = time.time()
    child.save()

    assert tlc.recover_pending_continuations(
        start=lambda _sid, _prompt: {
            "_status": 409,
            "active_stream_id": "stale-stream",
        }
    ) == 0
    persisted = tlc.load_receipts()["receipts"][receipt["claim_key"]]
    assert persisted["state"] == "claimed"
    assert "child_stream_id" not in persisted


def test_actual_start_path_replaces_dead_process_hidden_owner(store, monkeypatch):
    from api import routes

    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p,
        "run-real-route-crash",
        tool_limit_reached=True,
        start=lambda _sid, _prompt: {"_status": 500},
    )
    child = Session.load(receipt["child_session_id"])
    child.active_stream_id = "orphaned-before-worker-registration"
    child.pending_user_source = tlc.SOURCE
    child.pending_started_at = time.time()
    child.pending_server_instance_id = "dead-server-instance"
    child.save()
    child = Session.load(child.session_id)
    assert child.pending_server_instance_id == "dead-server-instance"

    class FakeThread:
        ident = 1

        def __init__(self, *args, **kwargs):
            self._alive = False

        def start(self):
            self._alive = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(routes.threading, "Thread", FakeThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    routes.STREAMS.clear()
    routes.ACTIVE_RUNS.clear()

    response = routes._start_chat_stream_for_session(
        child,
        msg=str(receipt["continuation_prompt"]),
        workspace=child.workspace,
        model=child.model,
        model_provider=child.model_provider,
        source=tlc.SOURCE,
    )

    assert response.get("_status", 200) < 400
    assert response["stream_id"] != "orphaned-before-worker-registration"
    assert response["stream_id"] in routes.STREAMS
    routes.STREAMS.pop(response["stream_id"], None)


@pytest.mark.parametrize("registered,expected", [(False, False), (True, True)])
def test_start_session_turn_409_proves_only_registered_live_stream(
    store, monkeypatch, registered, expected
):
    from api import routes

    child = parent()
    child.save()
    stream_id = "registered-live-stream"
    child.active_stream_id = stream_id
    child.pending_user_source = tlc.SOURCE

    monkeypatch.setattr(routes, "get_session", lambda _sid: child)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda _session, _workspace: child.workspace
    )
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda _session, _provider: (None, None, {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kwargs: (model, provider, False),
    )
    monkeypatch.setattr(
        routes,
        "_start_run",
        lambda *_args, **_kwargs: {
            "_status": 409,
            "active_stream_id": stream_id,
            "error": "session already has an active stream",
        },
    )
    routes.STREAMS.clear()
    if registered:
        routes.STREAMS[stream_id] = object()

    response = routes.start_session_turn(
        child.session_id,
        "Continue safely.",
        source=tlc.SOURCE,
    )

    assert bool(response.get("active_stream_confirmed_live")) is expected
    routes.STREAMS.pop(stream_id, None)


@pytest.mark.parametrize(
    "raw",
    [
        "{not-json",
        json.dumps({"version": 999, "receipts": {}}),
        json.dumps({"version": 1, "receipts": []}),
    ],
)
def test_receipt_store_corruption_fails_closed_without_creating_child(store, raw):
    tlc._receipt_path().write_text(raw, encoding="utf-8")
    p = parent()
    p.save()

    with pytest.raises(tlc.ContinuationReceiptStoreError):
        tlc.handle_terminal(p, "run-corrupt-store", tool_limit_reached=True)

    assert [
        path.name for path in store.glob("*.json") if not path.name.startswith("_")
    ] == [f"{p.session_id}.json"]
    assert tlc._receipt_path().read_text(encoding="utf-8") == raw


def test_two_exhausted_segments_then_normal_completion(store):
    p = parent()
    p.save()
    first = tlc.handle_terminal(
        p, "run-1", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "child-stream-1"},
    )
    child1 = Session.load(first["child_session_id"])
    second = tlc.handle_terminal(
        child1, "run-2", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "child-stream-2"},
    )
    child2 = Session.load(second["child_session_id"])
    complete = tlc.handle_terminal(child2, "run-3", tool_limit_reached=False)

    assert first["continuation_index"] == 1
    assert second["continuation_index"] == 2
    assert complete["state"] == "completed"
    receipts = tlc.load_receipts()["receipts"].values()
    assert {r["state"] for r in receipts} == {"completed"}


def test_latest_receipt_replays_final_child_from_any_lineage_segment(store):
    p = parent()
    p.save()
    first = tlc.handle_terminal(
        p, "run-1", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "child-stream-1"},
        now=10,
    )
    child1 = Session.load(first["child_session_id"])
    second = tlc.handle_terminal(
        child1, "run-2", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "child-stream-2"},
        now=20,
    )
    child2 = Session.load(second["child_session_id"])
    tlc.handle_terminal(child2, "run-3", tool_limit_reached=False, now=30)

    for session_id in (p.session_id, child1.session_id, child2.session_id):
        replay = tlc.latest_receipt_for_session(session_id)
        assert replay["child_session_id"] == child2.session_id
        assert replay["continuation_index"] == 2
        assert replay["state"] == "completed"

    assert tlc.latest_receipt_for_session("unrelated") is None


def test_reconnect_frames_replay_latest_receipt_and_live_child_start(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p, "run-live", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "live-child-stream"},
        now=10,
    )

    frames = tlc.replay_frames_for_session(
        p.session_id,
        active_stream_for_session=lambda sid: (
            "live-child-stream" if sid == receipt["child_session_id"] else None
        ),
    )

    assert frames == [
        (
            "tool_limit_continuation",
            {
                "execution_id": receipt["execution_id"],
                "root_session_id": p.session_id,
                "parent_session_id": p.session_id,
                "child_session_id": receipt["child_session_id"],
                "parent_run_id": "run-live",
                "continuation_index": 1,
                "state": "started",
            },
        ),
        (
            "server_turn_started",
            {
                "session_id": receipt["child_session_id"],
                "child_session_id": receipt["child_session_id"],
                "stream_id": "live-child-stream",
                "execution_id": receipt["execution_id"],
                "root_session_id": p.session_id,
                "parent_session_id": p.session_id,
                "source": "subscribe_recovery",
                "recovered": True,
            },
        ),
    ]


def test_reconnect_does_not_replay_completed_lineage_over_newer_parent_activity(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p, "run-complete", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "child-stream"},
        now=10,
    )
    child = Session.load(receipt["child_session_id"])
    tlc.handle_terminal(child, "child-run", tool_limit_reached=False, now=20)

    assert tlc.replay_frames_for_session(
        p.session_id,
        active_stream_for_session=lambda _sid: None,
        session_updated_at=21,
    ) == []


def test_reconnect_replays_active_lineage_despite_newer_parent_activity(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p, "run-live-newer-parent", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "live-child-stream"},
        now=10,
    )

    frames = tlc.replay_frames_for_session(
        p.session_id,
        active_stream_for_session=lambda _sid: None,
        session_updated_at=float(receipt["updated_at"]) + 10,
    )

    assert frames[0][0] == "tool_limit_continuation"
    assert frames[0][1]["state"] == "started"


def test_session_stream_replays_durable_tool_limit_frames():
    source = Path("api/routes.py").read_text(encoding="utf-8")
    start = source.index("def _handle_session_sse_stream")
    handler = source[start:start + 12_000]

    assert "replay_frames_for_session" in handler
    replay = handler.index("for event_name, event_payload in replay_frames_for_session")
    initial = handler.index("_sse(handler, 'initial'")
    live_self_heal = handler.index("recover_stream_id = active_stream_id_for_session(sid)")
    assert initial < replay < live_self_heal
    assert "_sse(handler, event_name, event_payload)" in handler[replay:live_self_heal]
    assert "Session.load_metadata_only(sid)" in handler[:replay]
    assert "session_updated_at=subscribed_session_updated_at" in handler[replay:live_self_heal]


def test_child_has_hidden_structured_control_and_explicit_lineage_source(store):
    p = parent()
    p.save()
    receipt = tlc.handle_terminal(
        p, "run-hidden", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "s"},
    )
    child = Session.load(receipt["child_session_id"])

    assert child.messages == []
    assert child.pending_user_message is None
    assert child.parent_session_id == p.session_id
    assert child.source_tag == child.raw_source == child.session_source == tlc.SOURCE
    assert child.title == p.title
    assert child.workspace == p.workspace
    assert child.model == p.model
    assert child.model_provider == p.model_provider
    assert child.profile == p.profile
    assert child.project_id == p.project_id
    assert child.personality == p.personality
    assert child.enabled_toolsets == p.enabled_toolsets
    control_message = child.context_messages[-1]
    assert control_message[tlc.CONTROL_KEY]["parent_session_id"] == p.session_id
    assert control_message[tlc.CONTROL_KEY]["continuation_index"] == 1


def test_child_continuation_lineage_survives_session_round_trip(store):
    p = parent()
    child = tlc._new_child(
        p,
        execution_id="execution-1",
        root_session_id="root-1",
        index=2,
        prompt="continue",
    )
    child.save()

    loaded = Session.load(child.session_id)

    assert loaded.tool_limit_continuation == {
        "execution_id": "execution-1",
        "root_session_id": "root-1",
        "parent_session_id": p.session_id,
        "continuation_index": 2,
        "instruction": "continue",
    }
    assert loaded.continuation_execution_id == "execution-1"
    assert loaded.continuation_index == 2
    assert loaded.root_session_id == "root-1"


@pytest.mark.parametrize("limit,reason", [("max_segments", "max_segments"), ("max_wall_seconds", "max_wall_seconds")])
def test_global_envelope_blocks_without_child(store, monkeypatch, limit, reason):
    p = parent()
    p.tool_limit_continuation = {
        "execution_id": "chain-x", "root_session_id": "root-x",
        "continuation_index": 1 if limit == "max_segments" else 0,
    }
    settings = {
        "enabled": True, "max_segments": 12 if limit == "max_wall_seconds" else 1,
        "max_wall_seconds": 1, "no_progress_limit": 3,
    }
    monkeypatch.setattr(tlc, "settings_for_session", lambda _s: settings)
    if limit == "max_wall_seconds":
        # Seed a prior receipt so the chain wall-clock origin predates this segment.
        first = tlc.handle_terminal(
            parent(), "seed", tool_limit_reached=True,
            start=lambda _sid, _prompt: {"stream_id": "seed"}, now=10,
        )
        p.tool_limit_continuation["execution_id"] = first["execution_id"]
        p.tool_limit_continuation["root_session_id"] = first["root_session_id"]
        p.tool_limit_continuation["continuation_index"] = 1
        now = 12
    else:
        now = 10
    events = []
    blocked = tlc.handle_terminal(
        p,
        f"run-{limit}",
        tool_limit_reached=True,
        now=now,
        emit=lambda name, payload: events.append((name, payload)),
    )
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == reason
    assert blocked["child_session_id"] is None
    assert events == [
        (
            "tool_limit_continuation",
            {
                "execution_id": blocked["execution_id"],
                "root_session_id": blocked["root_session_id"],
                "parent_session_id": blocked["parent_session_id"],
                "child_session_id": None,
                "parent_run_id": f"run-{limit}",
                "continuation_index": blocked["continuation_index"],
                "state": "blocked",
                "blocked_reason": reason,
            },
        )
    ]


def test_no_progress_blocks_but_unavailable_fingerprint_does_not_false_stop(store, monkeypatch):
    monkeypatch.setattr(tlc, "settings_for_session", lambda _s: {
        "enabled": True, "max_segments": 12, "max_wall_seconds": 14400,
        "no_progress_limit": 1,
    })
    p = parent()
    first = tlc.handle_terminal(
        p, "run-a", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "a"},
    )
    child = Session.load(first["child_session_id"])
    blocked = tlc.handle_terminal(child, "run-b", tool_limit_reached=True)
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == "no_progress"

    monkeypatch.setattr(tlc, "progress_fingerprint", lambda _s: None)
    unrelated = parent()
    allowed = tlc.handle_terminal(
        unrelated, "run-no-facts", tool_limit_reached=True,
        start=lambda _sid, _prompt: {"stream_id": "ok"},
    )
    assert allowed["state"] == "started"


@pytest.mark.parametrize("has_summary", [True, False])
def test_genuine_blocker_persists_replayable_terminal_truth(store, monkeypatch, has_summary):
    messages = [{"role": "user", "content": "do the task"}]
    if has_summary:
        messages.append({"role": "assistant", "content": "partial segment summary"})
    p = parent(messages=messages)
    p.tool_limit_continuation = {
        "execution_id": "chain-blocked",
        "root_session_id": p.session_id,
        "continuation_index": 1,
    }
    p.save()
    monkeypatch.setattr(tlc, "settings_for_session", lambda _s: {
        "enabled": True,
        "max_segments": 1,
        "max_wall_seconds": 14400,
        "no_progress_limit": 3,
    })

    receipt = tlc.handle_terminal(p, "run-blocked", tool_limit_reached=True)
    persisted = Session.load(p.session_id)

    assert receipt["state"] == "blocked"
    terminal = persisted.messages[-1]
    assert terminal["role"] == "assistant"
    assert terminal["_terminal_state"] == "tool_limit_reached"
    assert terminal["_terminal_reason"] == "max_segments"
    assert terminal["_statusCard"]["title"] == "Tool-limit continuation stopped"
    if not has_summary:
        assert terminal["_error"] is True
