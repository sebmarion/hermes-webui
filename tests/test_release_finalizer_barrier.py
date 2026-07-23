"""Deterministic barriers around stream teardown and release commit."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from api import config


def test_startup_continuation_recovery_is_admission_tracked():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text(
        encoding="utf-8"
    )
    tool_start = source.index("def _recover_tool_limit_continuations_on_startup")
    goal_start = source.index("def _recover_goal_continuations_on_startup")
    ack_start = source.index("def _handle_bg_task_complete_ack")

    tool_block = source[tool_start:goal_start]
    goal_block = source[goal_start:ack_start]
    assert "start_admitted_auxiliary_thread(" in tool_block
    assert 'kind="tool_limit_continuation_recovery"' in tool_block
    assert "start_admitted_auxiliary_thread(" in goal_block
    assert 'kind="goal_continuation_recovery"' in goal_block


def test_heartbeat_start_failure_leaves_no_active_row_or_reservation(
    monkeypatch,
    isolated_admission,
):
    reservation = config.reserve_run_admission(kind="chat")
    monkeypatch.setattr(
        config,
        "_ensure_active_activity_heartbeat_thread",
        lambda: (_ for _ in ()).throw(RuntimeError("thread start failed")),
    )

    with pytest.raises(config.RunAdmissionClosed, match="heartbeat"):
        config.register_active_run(
            "heartbeat-failure",
            admission_reservation_id=reservation,
        )

    snapshot = config.run_admission_snapshot()
    assert snapshot["active_runs"] == 0
    assert snapshot["reservations"] == 0


@pytest.fixture
def isolated_admission(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(config, "LAST_RUN_FINISHED_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_RESERVATIONS", {})
    monkeypatch.setattr(config, "_RUN_ADMISSION_STATE", "open")
    monkeypatch.setattr(config, "_RUN_ADMISSION_GENERATION", 0)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TOKEN_DIGEST", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_EXPECTED_IDENTITY", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_FENCED_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LEASE_EXPIRES_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LOCAL", threading.local())
    monkeypatch.setattr(config, "_ensure_active_activity_heartbeat_thread", lambda: None)
    monkeypatch.setattr(config, "_sync_active_run_activity", lambda *_a, **_k: None)
    monkeypatch.setattr(config, "_publish_active_run_activity_change", lambda *_a, **_k: None)
    return {"pid": 123, "started_at": 456.0, "instance_id": "finalizer-test"}


def test_gateway_durable_finalizer_blocks_release_commit(monkeypatch, isolated_admission):
    from api import gateway_chat
    from api import goal_continuation
    from api import state_sync

    stream_id = "gateway-finalizer-stream"
    session_id = "gateway-finalizer-session"
    streams = {stream_id: queue.Queue()}
    entered_finalizer = threading.Event()
    release_finalizer = threading.Event()

    def pause_durable_finish(*_args, **_kwargs):
        entered_finalizer.set()
        assert release_finalizer.wait(2.0)

    monkeypatch.setattr(gateway_chat, "STREAMS", streams)
    monkeypatch.setattr(gateway_chat, "CANCEL_FLAGS", {})
    monkeypatch.setattr(gateway_chat, "STREAM_GOAL_RELATED", {})
    monkeypatch.setattr(gateway_chat, "STREAM_PARTIAL_TEXT", {})
    monkeypatch.setattr(gateway_chat, "STREAM_REASONING_TEXT", {})
    monkeypatch.setattr(gateway_chat, "STREAM_LIVE_TOOL_CALLS", {})
    monkeypatch.setattr(gateway_chat, "STREAM_LAST_EVENT_ID", {})
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_a, **_k: None)
    monkeypatch.setattr(
        gateway_chat,
        "get_session",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    monkeypatch.setattr(gateway_chat, "unregister_stream_owner", lambda *_a, **_k: None)
    monkeypatch.setattr(goal_continuation, "settle_goal_continuation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        goal_continuation,
        "recover_pending_goal_continuations",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(state_sync, "finish_session_activity", pause_durable_finish)

    reservation = config.reserve_run_admission(
        kind="gateway-chat",
        session_id=session_id,
    )
    worker = threading.Thread(
        target=gateway_chat._run_gateway_chat_streaming,
        args=(session_id, "hello", "model", "/tmp", stream_id),
        kwargs={"admission_reservation_id": reservation},
    )
    worker.start()

    assert entered_finalizer.wait(2.0)
    assert stream_id not in streams
    fenced = config.fence_run_admission(isolated_admission)
    with pytest.raises(config.RunAdmissionBusy):
        config.commit_run_admission(
            fenced["token"],
            expected_identity=isolated_admission,
        )

    release_finalizer.set()
    worker.join(timeout=2.0)
    assert worker.is_alive() is False
    committed = config.commit_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert committed["state"] == "committing"


def test_native_durable_finalizer_blocks_release_commit(monkeypatch, isolated_admission):
    from api import background_process
    from api import goal_continuation
    from api import state_sync
    from api import streaming

    stream_id = "native-finalizer-stream"
    session_id = "native-finalizer-session"
    streams = {stream_id: queue.Queue()}
    entered_finalizer = threading.Event()
    release_finalizer = threading.Event()

    class _Meter:
        def begin_session(self, *_args, **_kwargs):
            return None

        def end_session(self, *_args, **_kwargs):
            return None

        def get_interval(self):
            return 10.0

        def get_stats(self):
            return {}

    def pause_durable_finish(*_args, **_kwargs):
        entered_finalizer.set()
        assert release_finalizer.wait(2.0)

    monkeypatch.setattr(streaming, "STREAMS", streams)
    monkeypatch.setattr(streaming, "CANCEL_FLAGS", {})
    monkeypatch.setattr(streaming, "AGENT_INSTANCES", {})
    monkeypatch.setattr(streaming, "STREAM_GOAL_RELATED", {})
    monkeypatch.setattr(streaming, "STREAM_PARTIAL_TEXT", {})
    monkeypatch.setattr(streaming, "STREAM_REASONING_TEXT", {})
    monkeypatch.setattr(streaming, "STREAM_LIVE_TOOL_CALLS", {})
    monkeypatch.setattr(streaming, "STREAM_LAST_EVENT_ID", {})
    monkeypatch.setattr(streaming, "RunJournalWriter", lambda *_a, **_k: None)
    monkeypatch.setattr(streaming, "meter", lambda: _Meter())
    monkeypatch.setattr(
        streaming,
        "get_session",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    monkeypatch.setattr(streaming, "_set_turn_session_identity", lambda *_a, **_k: None)
    monkeypatch.setattr(streaming, "_reset_turn_session_identity", lambda *_a, **_k: None)
    monkeypatch.setattr(streaming, "unregister_stream_owner", lambda *_a, **_k: None)
    monkeypatch.setattr(goal_continuation, "settle_goal_continuation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        goal_continuation,
        "recover_pending_goal_continuations",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(state_sync, "finish_session_activity", pause_durable_finish)
    monkeypatch.setattr(
        background_process,
        "drain_deferred_wakeups_for_session",
        lambda *_a, **_k: None,
    )

    reservation = config.reserve_run_admission(
        kind="native-chat",
        session_id=session_id,
    )
    worker = threading.Thread(
        target=streaming._run_agent_streaming,
        args=(session_id, "hello", "model", "/tmp", stream_id),
        kwargs={"admission_reservation_id": reservation},
    )
    worker.start()

    assert entered_finalizer.wait(2.0)
    assert stream_id not in streams
    fenced = config.fence_run_admission(isolated_admission)
    with pytest.raises(config.RunAdmissionBusy):
        config.commit_run_admission(
            fenced["token"],
            expected_identity=isolated_admission,
        )

    release_finalizer.set()
    worker.join(timeout=2.0)
    assert worker.is_alive() is False
    committed = config.commit_run_admission(
        fenced["token"],
        expected_identity=isolated_admission,
    )
    assert committed["state"] == "committing"
