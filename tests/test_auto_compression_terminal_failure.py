"""Regression coverage for compression-exhausted stream finalization."""

import copy
import json
import queue
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from api import compression_recovery_receipts, config, models, streaming
from api.models import Session
from api.streaming import (
    _agent_result_terminal_failure,
    _session_lacks_final_assistant_answer,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _make_state_db(path: Path, sid: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
            "model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "webui", "Native recovery", "test-model", 1.0, len(rows)),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (
                    sid,
                    row["role"],
                    row["content"],
                    row.get("timestamp", 1.0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _started_native_recovery(tmp_path, monkeypatch, *, stream_id):
    from api.compression_recovery import _recovery_fingerprint
    from api.turn_journal import append_turn_journal_event

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    request = "Finish the exact native recovery work from the trusted checkpoint."
    context_messages = [
        {"role": "assistant", "content": "Trusted completed checkpoint."},
        {"role": "user", "content": request},
    ]
    session = Session(
        session_id=f"native-recovery-{stream_id}",
        title="Native recovery",
        workspace=str(tmp_path),
        model="test-model",
        messages=[{"role": "assistant", "content": "Trusted completed checkpoint."}],
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    streaming.SESSIONS[session.session_id] = session
    seed = {
        "session_id": session.session_id,
        "parent_run_id": "native-parent",
        "context_messages": context_messages,
        "attachments": [],
        "trust_source": "assistant_checkpoint",
        "fingerprint": "",
    }
    seed["fingerprint"] = _recovery_fingerprint(
        session_id=session.session_id,
        parent_run_id="native-parent",
        context_messages=context_messages,
        attachments=[],
    )
    claimed = compression_recovery_receipts.claim_compression_recovery(
        session,
        "native-parent",
        seed,
    )

    def start_recovery(sid, prompt, **kwargs):
        submitted = append_turn_journal_event(
            sid,
            {
                "event": "submitted",
                "stream_id": stream_id,
                "role": "user",
                "content": prompt,
                "attachments": kwargs["attachments"],
                "source": compression_recovery_receipts.SOURCE,
                "profile": "default",
                "recovery_claim_token": kwargs["recovery_claim_token"],
                "recovery_fingerprint": kwargs["recovery_fingerprint"],
            },
        )
        return {
            "session_id": sid,
            "stream_id": stream_id,
            "turn_id": submitted["turn_id"],
        }

    started = compression_recovery_receipts.settle_compression_recovery(
        session.session_id,
        "native-parent",
        start=start_recovery,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = compression_recovery_receipts.RECOVERY_CONTROL_PROMPT
    session.pending_attachments = []
    session.pending_started_at = 1.0
    session.pending_user_source = compression_recovery_receipts.SOURCE
    session.context_messages = context_messages
    session.compression_recovery = compression_recovery_receipts._session_phase_payload(
        started,
        "running",
    )
    session.save(touch_updated_at=False)
    config.STREAMS[stream_id] = queue.Queue()
    return session, claimed


def test_native_recovery_dispatch_uses_exact_seed_not_state_db_history(
    tmp_path,
    monkeypatch,
):
    stream_id = "native-recovery-exact-seed"
    session, claimed = _started_native_recovery(
        tmp_path,
        monkeypatch,
        stream_id=stream_id,
    )
    state_db_path = tmp_path / "state.db"
    _make_state_db(
        state_db_path,
        session.session_id,
        [
            {"role": "user", "content": "stale database request", "timestamp": 1.0},
            {
                "role": "assistant",
                "content": "stale database answer",
                "timestamp": 2.0,
            },
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db_path)
    captured = {}

    class FakeAgent:
        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            captured["conversation_history"] = copy.deepcopy(history)
            return {
                "completed": True,
                "final_response": "Recovered answer",
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "Recovered answer"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as scoped:
        scoped.setattr(streaming, "get_session", lambda _sid: session)
        scoped.setattr(
            streaming,
            "_set_streaming_session_id_mirror_suppression",
            lambda: None,
            raising=False,
        )
        scoped.setattr(
            streaming,
            "_set_streaming_secret_scope",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
        scoped.setattr(
            streaming,
            "_set_streaming_runtime_env",
            lambda *_args, **_kwargs: None,
            raising=False,
        )
        scoped.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        scoped.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("test-model", "openai", None),
        )
        scoped.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        scoped.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        scoped.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=compression_recovery_receipts.RECOVERY_CONTROL_PROMPT,
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )

    expected_seed = claimed["seed"]["context_messages"]
    assert [
        (row.get("role"), row.get("content"))
        for row in captured["conversation_history"]
    ] == [
        (row.get("role"), row.get("content"))
        for row in expected_seed
    ]


def test_compression_exhausted_after_session_rotation_preserves_snapshot_and_errors_on_continuation(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    old_sid = "old_sid"
    new_sid = "new_sid"
    stream_id = "stream-compression-exhausted"
    session = Session(
        session_id=old_sid,
        title="Compression test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = "Context budget rejected locally: compaction_made_no_progress."

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("I am still working through the files.")
            self.session_id = new_sid
            return {
                "failed": True,
                "partial": True,
                "compression_exhausted": True,
                "error": "Context budget rejected locally: compaction_made_no_progress.",
                "final_response": "Context budget rejected locally: compaction_made_no_progress.",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "I am still working through the files."},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
                    {
                        "role": "assistant",
                        "content": "Context budget rejected locally: compaction_made_no_progress.",
                    },
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=old_sid,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected apperror SSE payload"
    payload = apperror_payloads[-1]
    assert payload["type"] == "compression_exhausted"
    assert payload["session"]["session_id"] == new_sid
    assert payload["old_session_id"] == old_sid
    assert payload["new_session_id"] == new_sid
    assert payload["compression_recovery"]["terminal_state"] == "compression_exhausted"
    assert payload["compression_recovery"]["source_session_id"] == new_sid
    assert payload["compression_recovery"]["phase"] == "blocked"
    assert payload["automatic_recovery"] is False

    old_payload = json.loads((session_dir / f"{old_sid}.json").read_text(encoding="utf-8"))
    new_payload = json.loads((session_dir / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert old_payload["pre_compression_snapshot"] is True
    assert old_payload["active_stream_id"] is None
    assert old_payload["pending_user_message"] is None
    assert new_payload["session_id"] == new_sid
    assert new_payload["parent_session_id"] == old_sid
    assert new_payload["pre_compression_snapshot"] is False
    assert new_payload["recommended_recovery_action"] is None
    assert new_payload["compression_recovery"]["phase"] == "blocked"
    assert new_payload["messages"][-1]["_error"] is True
    assert new_payload["messages"][-1]["_compressionRecovery"]["phase"] == "blocked"
    assert "Context compression exhausted" in new_payload["messages"][-1]["content"]
    assert not any(
        "Context budget rejected locally: compaction_made_no_progress." in str(message.get("content") or "")
        for message in new_payload["messages"]
        if not message.get("_error")
    )
    assert not any(
        "Context budget rejected locally: compaction_made_no_progress." in str(message.get("content") or "")
        or message.get("content") == "Do the long task."
        for message in new_payload.get("context_messages", [])
    )
    assert old_sid not in streaming.SESSIONS
    assert streaming.SESSIONS[new_sid].session_id == new_sid


@pytest.mark.parametrize("agent_outcome", ["success", "error"])
@pytest.mark.parametrize("failure_mode", [None, "terminal_save", "terminal_journal"])
def test_native_recovery_settles_only_after_durable_terminal(
    tmp_path,
    monkeypatch,
    agent_outcome,
    failure_mode,
):
    stream_id = f"native-{agent_outcome}-{failure_mode or 'durable'}"
    session, claimed = _started_native_recovery(
        tmp_path,
        monkeypatch,
        stream_id=stream_id,
    )
    failure_seen = []

    class FakeAgent:
        def __init__(self, stream_delta_callback=None, session_id=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if agent_outcome == "error":
                raise RuntimeError("synthetic recovery provider failure")
            history = list(kwargs.get("conversation_history") or [])
            return {
                "completed": True,
                "final_response": "Recovered answer",
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "Recovered answer"},
                ],
            }

        def interrupt(self, _message):
            return None

    if failure_mode == "terminal_save":
        original_save = models.Session.save

        def fail_terminal_save(current, *args, **kwargs):
            if (
                current.session_id == session.session_id
                and current.active_stream_id is None
            ):
                failure_seen.append("terminal_save")
                raise OSError("synthetic native terminal save failure")
            return original_save(current, *args, **kwargs)

        monkeypatch.setattr(models.Session, "save", fail_terminal_save)
    elif failure_mode == "terminal_journal":
        original_append = streaming.append_turn_journal_event_for_stream

        def fail_terminal_journal(session_id, current_stream_id, event, **kwargs):
            if event.get("recovery_terminal_persisted") is True:
                failure_seen.append("terminal_journal")
                raise OSError("synthetic native terminal journal failure")
            return original_append(session_id, current_stream_id, event, **kwargs)

        monkeypatch.setattr(
            streaming,
            "append_turn_journal_event_for_stream",
            fail_terminal_journal,
        )

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as scoped:
        scoped.setattr(streaming, "get_session", lambda _sid: session)
        scoped.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        scoped.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("test-model", "openai", None),
        )
        scoped.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        scoped.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        scoped.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=compression_recovery_receipts.RECOVERY_CONTROL_PROMPT,
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )

    receipt = compression_recovery_receipts.load_receipts()["receipts"][
        claimed["claim_key"]
    ]
    if failure_mode is None:
        assert receipt["state"] == "discarded"
        assert receipt["discarded_reason"] == "successor_settled"
        assert Session.load(session.session_id).compression_recovery == {}
    else:
        assert failure_seen
        assert not (
            receipt["state"] == "discarded"
            and receipt.get("discarded_reason") == "successor_settled"
        )


def test_native_rotated_recovery_settles_source_receipt_and_canonical_presentation(
    tmp_path,
    monkeypatch,
):
    from api.turn_journal import read_turn_journal

    stream_id = "native-rotated-recovery"
    source, claimed = _started_native_recovery(
        tmp_path,
        monkeypatch,
        stream_id=stream_id,
    )
    source_sid = source.session_id
    canonical_sid = f"{source_sid}-canonical"

    class RotatingAgent:
        def __init__(self, stream_delta_callback=None, session_id=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            self.session_id = canonical_sid
            history = list(kwargs.get("conversation_history") or [])
            return {
                "completed": True,
                "final_response": "Recovered after rotation",
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "Recovered after rotation"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as scoped:
        scoped.setattr(streaming, "get_session", lambda _sid: source)
        scoped.setattr(streaming, "_get_ai_agent", lambda: RotatingAgent)
        scoped.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("test-model", "openai", None),
        )
        scoped.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        scoped.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        scoped.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=source_sid,
            msg_text=compression_recovery_receipts.RECOVERY_CONTROL_PROMPT,
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )

    terminal_events = [
        event
        for event in read_turn_journal(source_sid)["events"]
        if event.get("stream_id") == stream_id
        and event.get("event") in {"completed", "interrupted"}
    ]
    assert terminal_events[-1]["recovery_terminal_persisted"] is True
    receipt = compression_recovery_receipts.load_receipts()["receipts"][
        claimed["claim_key"]
    ]
    canonical = Session.load(canonical_sid)

    assert receipt["session_id"] == source_sid
    assert receipt["state"] == "discarded"
    assert receipt["discarded_reason"] == "successor_settled"
    assert canonical.parent_session_id == source_sid
    assert canonical.compression_recovery == {}


def test_native_rotated_terminal_journal_failure_repairs_canonical_blocker_on_restart(
    tmp_path,
    monkeypatch,
):
    stream_id = "native-rotated-terminal-journal-failure"
    source, claimed = _started_native_recovery(
        tmp_path,
        monkeypatch,
        stream_id=stream_id,
    )
    source_sid = source.session_id
    canonical_sid = f"{source_sid}-canonical"
    source_path = Path(models.SESSION_DIR) / f"{source_sid}.json"
    journal_failures = []

    class RotatingAgent:
        def __init__(self, stream_delta_callback=None, session_id=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            self.session_id = canonical_sid
            history = list(kwargs.get("conversation_history") or [])
            return {
                "completed": True,
                "final_response": "Recovered before terminal journal failure",
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {
                        "role": "assistant",
                        "content": "Recovered before terminal journal failure",
                    },
                ],
            }

        def interrupt(self, _message):
            return None

    original_append = streaming.append_turn_journal_event_for_stream

    def fail_recovery_terminal(session_id, current_stream_id, event, **kwargs):
        if event.get("recovery_terminal_persisted") is True:
            journal_failures.append((session_id, current_stream_id))
            raise OSError("synthetic rotated terminal journal failure")
        return original_append(session_id, current_stream_id, event, **kwargs)

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as scoped:
        scoped.setattr(streaming, "get_session", lambda _sid: source)
        scoped.setattr(streaming, "_get_ai_agent", lambda: RotatingAgent)
        scoped.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("test-model", "openai", None),
        )
        scoped.setattr(
            streaming,
            "append_turn_journal_event_for_stream",
            fail_recovery_terminal,
        )
        scoped.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        scoped.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        scoped.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=source_sid,
            msg_text=compression_recovery_receipts.RECOVERY_CONTROL_PROMPT,
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )

    before_restart = compression_recovery_receipts.load_receipts()["receipts"][
        claimed["claim_key"]
    ]
    canonical_before_restart = Session.load(canonical_sid)
    source_before_restart_repair = source_path.read_bytes()
    assert journal_failures == [(source_sid, stream_id)]
    assert before_restart["state"] == "started"
    assert before_restart["presentation_session_id"] == canonical_sid
    assert canonical_before_restart.compression_recovery["phase"] == "running"

    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    recovered = compression_recovery_receipts.recover_pending_compression_recoveries()

    repaired_receipt = compression_recovery_receipts.load_receipts()["receipts"][
        claimed["claim_key"]
    ]
    repaired_canonical = Session.load(canonical_sid)
    assert recovered == 0
    assert repaired_receipt["state"] == "discarded"
    assert repaired_receipt["discarded_reason"] == "ambiguous_started_successor"
    assert repaired_receipt["presentation_session_id"] == canonical_sid
    assert repaired_canonical.compression_recovery["phase"] == "blocked"
    assert (
        repaired_canonical.compression_recovery["reason"]
        == "ambiguous_started_successor"
    )
    assert source_path.read_bytes() == source_before_restart_repair


def test_compression_exhausted_result_is_terminal_failure_even_after_streamed_text():
    result = {
        "failed": True,
        "partial": True,
        "compression_exhausted": True,
        "error": "Context length exceeded: 119,194 tokens. Cannot compress further.",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I am still working through the files."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
        ],
    }

    assert _agent_result_terminal_failure(result) is True
    assert _session_lacks_final_assistant_answer(result["messages"]) is True


def test_terminal_failure_gates_shape_check_to_no_streamed_text():
    src = _read("api/streaming.py")
    start = src.find("_is_agent_result_terminal = _agent_result_terminal_failure(result)")
    assert start != -1, "terminal failure result assignment not found"
    end = src.find("if _terminal_failure:", start)
    assert end != -1, "terminal failure guard not found"
    block = src[start:end]

    assert "_is_agent_result_terminal = _agent_result_terminal_failure(result)" in block
    assert "_is_agent_result_terminal" in block
    assert "_saved_transcript_lacks_final_answer" in block
    assert "_classification['type'] not in {'cancelled', 'interrupted'}" in block
    assert "not _token_sent" not in block
    assert "_session_lacks_final_assistant_answer(_all_result_messages)" not in block


def test_completed_tool_tail_without_final_assistant_is_not_successful_done():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_assistant_content_with_tool_calls_is_not_final_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_before_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_context_compaction_marker_before_tool_tail_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {
            "role": "assistant",
            "content": "I will inspect the result.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_final_assistant_text_is_successful_terminal_answer():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_assistant_tool_call_turn_followed_by_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_compression_exhausted_apperror_clears_reference_ui_and_labels_error():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('apperror'")
    assert start != -1, "apperror listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1, "warning listener after apperror not found"
    block = src[start:end]

    assert "const isCompressionExhausted=d.type==='compression_exhausted';" in block
    assert "isCompressionExhausted?'Context compression exhausted'" in block
    assert "if(typeof clearCompressionUi==='function') clearCompressionUi();" in block
    assert "window._compressionUi=null;" in block
    assert "const eventSid=d.old_session_id||d.session_id||'';" in block
    assert "const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';" in block
    assert "if(d.session&&typeof d.session==='object')" in block
    assert "S.session=d.session;" in block


def test_apperror_matches_only_current_or_continuation_session_for_background_errors():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('apperror'")
    assert start != -1, "apperror listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1, "warning listener after apperror not found"
    block = src[start:end]

    assert "const eventSid=d.old_session_id||d.session_id||'';" in block
    assert "const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';" in block
    assert "const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||continuationSid===currentSid));" in block


def test_apperror_payload_enriched_before_enqueue(tmp_path, monkeypatch):
    class _CaptureQueue:
        def __init__(self):
            self.events = []

        def put_nowait(self, item):
            event, payload = item
            self.events.append((event, payload, copy.deepcopy(payload)))

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()

    old_sid = "old_sid_capture"
    new_sid = "new_sid_capture"
    stream_id = "stream-compression-exhausted-capture"
    session = models.Session(
        session_id=old_sid,
        title="Compression test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    captured = _CaptureQueue()
    streaming.STREAMS[stream_id] = captured

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = "Context length exceeded: cannot compress further."

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("I am still working through the files.")
            self.session_id = new_sid
            return {
                "failed": True,
                "partial": True,
                "compression_exhausted": True,
                "error": "Context length exceeded: cannot compress further.",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "I am still working through the files."},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setattr(streaming, "redact_session_data", lambda s: s)

        streaming._run_agent_streaming(
            session_id=old_sid,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    apperror_payloads = [
        (payload, payload_before)
        for event, payload, payload_before in captured.events
        if event == "apperror"
    ]
    assert apperror_payloads, "expected apperror SSE payload"
    payload_after, payload_before = apperror_payloads[-1]
    assert payload_after == payload_before, "apperror payload changed after enqueue"
    assert payload_after["session_id"] == new_sid
    assert payload_after["old_session_id"] == old_sid
    assert payload_after["new_session_id"] == new_sid
    assert payload_after["automatic_recovery"] is False
    assert payload_after["compression_recovery"]["phase"] == "blocked"


def test_exception_apperror_payload_includes_session_id_before_enqueue():
    src = _read("api/streaming.py")
    start = src.find("_error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)")
    assert start != -1, "exception apperror payload path not found"
    end = src.find("put('apperror', _error_payload)", start)
    assert end != -1, "exception apperror enqueue not found"
    block = src[start:end]

    assert "_error_payload['session_id'] = getattr(s, 'session_id', session_id)" in block
    assert "_error_payload['old_session_id'] = session_id" in block
