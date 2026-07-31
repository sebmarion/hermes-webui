"""Regression coverage for #3929-D process-wakeup credential exhaustion pause."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import queue
import hashlib
import sys
import threading
import types
from unittest import mock

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.profiles as profiles
import api.providers as providers
import api.routes as routes
import api.streaming as streaming
from api.models import PROCESS_WAKEUP_PAUSE_ERROR, Session


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.STREAM_GOAL_RELATED.clear()
    config.PENDING_BG_TASK_COMPLETIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.STREAM_GOAL_RELATED.clear()
    config.PENDING_BG_TASK_COMPLETIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


@pytest.fixture(autouse=True)
def _default_live_credential_revalidation(monkeypatch):
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda *_args, **_kwargs: False,
    )


@pytest.fixture(autouse=True)
def _mock_hermes_modules(monkeypatch):
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in injected}
    sys.modules.update(injected)
    yield
    for name, previous in saved.items():
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class _MockAgent:
    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.reasoning_callback = kwargs.get("reasoning_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.context_compressor = None
        self._last_error = None
        self.ephemeral_system_prompt = None

    def interrupt(self, _message):
        pass


class _CredentialPoolEmptyAgent(_MockAgent):
    def run_conversation(self, **_kwargs):
        raise RuntimeError("All 0 credential(s) exhausted for test-provider")


class _RateLimitedAgent(_MockAgent):
    before_terminal_state = None

    def run_conversation(self, **_kwargs):
        type(self).before_terminal_state = copy.deepcopy(
            models.SESSIONS[self.session_id].__dict__
        )
        raise RuntimeError("HTTP 429: rate limit exceeded")


class _StaleCredentialPoolEmptyAgent(_MockAgent):
    def run_conversation(self, **_kwargs):
        session = models.SESSIONS[self.session_id]
        session.active_stream_id = "stream-newer-run"
        session.model = "newer-model"
        session.model_provider = "newer-provider"
        session.pending_user_source = "webui"
        session.save(touch_updated_at=False)
        raise RuntimeError("All 0 credential(s) exhausted for test-provider")


class _SuccessfulAgent(_MockAgent):
    def run_conversation(self, **kwargs):
        history = list(kwargs.get("conversation_history") or [])
        return {
            "messages": history
            + [
                {"role": "user", "content": kwargs.get("persist_user_message", "")},
                {"role": "assistant", "content": "Stream reply"},
            ]
        }


class _CountingSuccessfulAgent(_SuccessfulAgent):
    run_calls = 0

    def run_conversation(self, **kwargs):
        type(self).run_calls += 1
        return super().run_conversation(**kwargs)


class _CancellingSuccessfulAgent(_SuccessfulAgent):
    stream_id = None

    def run_conversation(self, **kwargs):
        cancel_flag = config.CANCEL_FLAGS.get(type(self).stream_id)
        assert cancel_flag is not None
        cancel_flag.set()
        return super().run_conversation(**kwargs)


class _StaleRateLimitedAgent(_MockAgent):
    replacement_stream_id = "stream-newer-owner"

    def run_conversation(self, **_kwargs):
        session = models.SESSIONS[self.session_id]
        session.active_stream_id = type(self).replacement_stream_id
        session.pending_user_message = "newer owner's pending turn"
        session.pending_user_source = "webui"
        session.messages.append(
            {
                "role": "assistant",
                "content": "newer owner transcript",
                "timestamp": 2.0,
            }
        )
        session.context_messages = copy.deepcopy(session.messages)
        session.save(touch_updated_at=False)
        raise RuntimeError("HTTP 429: rate limit exceeded")


class _RotatingSuccessfulAgent(_SuccessfulAgent):
    continuation_session_id = "native-rotated-continuation"
    before_terminal_state = None

    def run_conversation(self, **kwargs):
        type(self).before_terminal_state = copy.deepcopy(
            models.SESSIONS[self.session_id].__dict__
        )
        self.session_id = type(self).continuation_session_id
        return super().run_conversation(**kwargs)


class _AuthSelfHealingAgent(_SuccessfulAgent):
    run_calls = 0

    def run_conversation(self, **kwargs):
        type(self).run_calls += 1
        if type(self).run_calls == 1:
            raise RuntimeError("HTTP 401: authentication token is invalid")
        return super().run_conversation(**kwargs)


class _AuthSelfHealErrorAgent(_MockAgent):
    run_calls = 0

    def run_conversation(self, **kwargs):
        type(self).run_calls += 1
        if type(self).run_calls == 1:
            raise RuntimeError("HTTP 401: authentication token is invalid")
        return {
            "messages": list(kwargs.get("conversation_history") or []),
            "error": {"message": "refreshed credential was also rejected"},
        }


class _AuthSelfHealCancellingAgent(_SuccessfulAgent):
    run_calls = 0
    stream_id = None

    def run_conversation(self, **kwargs):
        type(self).run_calls += 1
        if type(self).run_calls == 1:
            raise RuntimeError("HTTP 401: authentication token is invalid")
        cancel_flag = config.CANCEL_FLAGS.get(type(self).stream_id)
        assert cancel_flag is not None
        cancel_flag.set()
        return super().run_conversation(**kwargs)


def _make_exact_completion_event(session_id: str, suffix: str) -> dict:
    return {
        "type": "completion",
        "event_id": f"process:{suffix}:completion",
        "session_id": f"proc_{suffix}",
        "session_key": session_id,
        "command": f"command-{suffix}",
        "exit_code": 0,
        "output": f"output-{suffix}",
        "created_at": 1.0,
    }


def _install_exact_event_registry(monkeypatch):
    class _ExactEventRegistry:
        def __init__(self):
            self.completion_queue = queue.Queue()
            self.finish_calls = []
            self.fail_committed_remaining = 0
            self.raise_on_finish = False

        def finish_notification_delivery(self, event, committed):
            self.finish_calls.append((event, committed))
            if self.raise_on_finish:
                raise RuntimeError("synthetic settlement failure")
            if committed and self.fail_committed_remaining <= 0:
                return True
            if committed:
                self.fail_committed_remaining -= 1
            self.completion_queue.put(event)
            return False

    registry = _ExactEventRegistry()
    fake_module = types.ModuleType("tools.process_registry")
    fake_module.process_registry = registry
    if "tools" not in sys.modules:
        fake_package = types.ModuleType("tools")
        fake_package.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", fake_package)
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_module)
    return registry


def _run_exact_process_wakeup(
    session: Session,
    tmp_path,
    event: dict,
    agent_class,
):
    stream_id = str(session.active_stream_id)
    stream_queue = queue.Queue()
    streaming.STREAMS[stream_id] = stream_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""
    with mock.patch.object(streaming, "_get_ai_agent", return_value=agent_class), \
         mock.patch.object(
             streaming,
             "resolve_model_provider",
             return_value=("test-model", "test-provider", None),
         ), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
            process_completion_events=[event],
        )
    return [(item[0], item[1]) for item in list(stream_queue.queue)]


def _new_exact_process_wakeup_session(
    tmp_path,
    *,
    session_id: str,
    stream_id: str,
    process_wakeup_pause: dict | None = None,
) -> Session:
    session = Session(
        session_id=session_id,
        title="Exact process wakeup",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "before", "timestamp": 1.0}],
        context_messages=[{"role": "user", "content": "before"}],
        active_stream_id=stream_id,
        pending_user_message="[IMPORTANT: exact process completed]",
        pending_attachments=[{"name": "pending.txt"}],
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
        pending_server_instance_id="server-before",
        process_wakeup_pause=copy.deepcopy(process_wakeup_pause or {}),
        compression_recovery={"sentinel": "recovery-before"},
        recommended_recovery_action="recover-before",
        compression_recovery_source_session_id="source-before",
        compression_recovery_action="action-before",
        truncation_watermark={"sentinel": "watermark-before"},
        truncation_boundary={"sentinel": "boundary-before"},
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    return session


def test_rate_limited_process_wakeup_commits_visible_error_and_settles_exact_event(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_rate_limit_commit",
        stream_id="stream-native-rate-limit-commit",
    )
    event = _make_exact_completion_event(session.session_id, "native-rate-limit")
    registry = _install_exact_event_registry(monkeypatch)

    emitted = _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _RateLimitedAgent,
    )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert registry.finish_calls == [(event, True)]
    assert [message["role"] for message in saved.messages[-2:]] == [
        "user",
        "assistant",
    ]
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1][streaming._PROCESS_COMPLETION_RECEIPTS_KEY]
    assert event["event_id"] not in json.dumps(saved.messages[-1])
    assert saved.process_wakeup_pause == {}
    assert registry.completion_queue.empty()
    assert any(
        event_name == "apperror" and payload.get("type") == "rate_limit"
        for event_name, payload in emitted
    )


def test_rate_limited_process_wakeup_restores_state_when_sidecar_write_fails(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_rate_limit_prepublication_failure",
        stream_id="stream-native-rate-limit-prepublication-failure",
        process_wakeup_pause={"sentinel": "pause-before"},
    )
    event = _make_exact_completion_event(session.session_id, "native-prepublish")
    registry = _install_exact_event_registry(monkeypatch)
    _RateLimitedAgent.before_terminal_state = None
    original_safe_replace = models._safe_replace

    def _fail_terminal_sidecar_replace(src, dst):
        cached = models.SESSIONS.get(session.session_id)
        is_terminal_error = cached is not None and any(
            isinstance(message, dict) and message.get("_error")
            for message in (cached.messages or [])
        )
        if Path(dst) == session.path and is_terminal_error:
            raise OSError("synthetic pre-publication sidecar failure")
        return original_safe_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", _fail_terminal_sidecar_replace)

    _run_exact_process_wakeup(session, tmp_path, event, _RateLimitedAgent)

    before_terminal = _RateLimitedAgent.before_terminal_state
    assert before_terminal is not None
    assert models.SESSIONS[session.session_id].__dict__ == before_terminal
    persisted = Session.load(session.session_id)
    assert persisted is not None
    assert persisted.messages == before_terminal["messages"]
    assert persisted.context_messages == before_terminal["context_messages"]
    assert persisted.active_stream_id == before_terminal["active_stream_id"]
    assert persisted.pending_user_message == before_terminal["pending_user_message"]
    assert persisted.pending_attachments == before_terminal["pending_attachments"]
    assert persisted.pending_started_at == before_terminal["pending_started_at"]
    assert persisted.pending_user_source == before_terminal["pending_user_source"]
    assert persisted.pending_server_instance_id == before_terminal["pending_server_instance_id"]
    assert persisted.process_wakeup_pause == before_terminal["process_wakeup_pause"]
    assert persisted.compression_recovery == before_terminal["compression_recovery"]
    assert persisted.recommended_recovery_action == before_terminal["recommended_recovery_action"]
    assert persisted.truncation_watermark == before_terminal["truncation_watermark"]
    assert persisted.truncation_boundary == before_terminal["truncation_boundary"]
    assert persisted.sidecar_generation == before_terminal["sidecar_generation"]
    assert not any(
        message.get("_error")
        and streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in persisted.messages
        if isinstance(message, dict)
    )
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event


def test_rate_limited_process_wakeup_accepts_receipt_after_index_failure(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_rate_limit_postpublication_failure",
        stream_id="stream-native-rate-limit-postpublication-failure",
    )
    event = _make_exact_completion_event(session.session_id, "native-postpublish")
    registry = _install_exact_event_registry(monkeypatch)
    original_write_index = models._write_session_index
    terminal_index_failures = {"count": 0}

    def _fail_index_after_terminal_sidecar(updates=None, **kwargs):
        terminal = any(
            isinstance(message, dict) and message.get("_error")
            for candidate in (updates or [])
            for message in (candidate.messages or [])
        )
        if terminal:
            terminal_index_failures["count"] += 1
            raise OSError("synthetic post-publication index failure")
        return original_write_index(updates=updates, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", _fail_index_after_terminal_sidecar)

    _run_exact_process_wakeup(session, tmp_path, event, _RateLimitedAgent)

    saved = Session.load(session.session_id)
    assert saved is not None
    assert terminal_index_failures == {"count": 1}
    assert streaming._durable_process_completion_receipt_status(
        session.session_id,
        [event],
    ) == "committed"
    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.empty()
    assert saved.messages[-1].get("_error") is True
    assert saved.messages[-1][streaming._PROCESS_COMPLETION_RECEIPTS_KEY]
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None


def test_successful_process_wakeup_ack_replay_does_not_call_provider_twice(
    tmp_path,
    monkeypatch,
):
    from api import background_process

    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_success_ack_replay",
        stream_id="stream-native-success-ack-replay",
    )
    event = _make_exact_completion_event(session.session_id, "native-success")
    registry = _install_exact_event_registry(monkeypatch)
    registry.fail_committed_remaining = 1
    _CountingSuccessfulAgent.run_calls = 0

    emitted = _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _CountingSuccessfulAgent,
    )

    first_saved = Session.load(session.session_id)
    assert first_saved is not None
    assert _CountingSuccessfulAgent.run_calls == 1
    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.get_nowait() is event
    terminal_assistant = next(
        message
        for message in reversed(first_saved.messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    assert terminal_assistant[streaming._PROCESS_COMPLETION_RECEIPTS_KEY]

    with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
    try:
        assert background_process.stage_process_completion_event(
            session.session_id,
            event,
        ) is True
        monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

        def _provider_must_not_restart(*_args, **_kwargs):
            raise AssertionError("provider preparation reached for committed native replay")

        monkeypatch.setattr(
            routes,
            "_prepare_chat_start_session_for_stream",
            _provider_must_not_restart,
        )
        replay = routes._start_chat_stream_for_session(
            session,
            msg="[IMPORTANT: exact process completed]",
            attachments=[],
            workspace=session.workspace,
            model=session.model,
            source="process_wakeup",
        )
    finally:
        with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
            background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()

    assert replay == {
        "delivery_already_committed": True,
        "delivery_acknowledged": True,
        "_status": 200,
    }
    assert registry.finish_calls == [(event, True), (event, True)]
    assert _CountingSuccessfulAgent.run_calls == 1
    replayed = Session.load(session.session_id)
    assert replayed is not None
    assert sum(
        message.get("content") == "Stream reply"
        for message in replayed.messages
        if isinstance(message, dict)
    ) == 1


def test_auth_self_heal_success_commits_receipt_and_replays_without_provider(
    tmp_path,
    monkeypatch,
):
    from api import background_process

    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_auth_self_heal_ack_replay",
        stream_id="stream-native-auth-self-heal-ack-replay",
    )
    event = _make_exact_completion_event(session.session_id, "native-auth-self-heal")
    registry = _install_exact_event_registry(monkeypatch)
    registry.fail_committed_remaining = 1
    _AuthSelfHealingAgent.run_calls = 0
    heal_runtime = {
        "provider": "test-provider",
        "api_key": "refreshed-synthetic-key",
        "base_url": None,
    }

    with mock.patch.object(
        streaming,
        "_attempt_credential_self_heal",
        return_value=heal_runtime,
    ):
        _run_exact_process_wakeup(
            session,
            tmp_path,
            event,
            _AuthSelfHealingAgent,
        )

    first_saved = Session.load(session.session_id)
    assert first_saved is not None
    assert _AuthSelfHealingAgent.run_calls == 2
    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.get_nowait() is event
    terminal_assistants = [
        message
        for message in first_saved.messages
        if isinstance(message, dict)
        and message.get("role") == "assistant"
        and message.get("content") == "Stream reply"
    ]
    assert len(terminal_assistants) == 1
    assert terminal_assistants[0][streaming._PROCESS_COMPLETION_RECEIPTS_KEY]
    assert not any(
        bool(message.get("_error"))
        for message in first_saved.messages
        if isinstance(message, dict)
    ), first_saved.messages

    transcript_before_replay = copy.deepcopy(first_saved.messages)
    with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
    try:
        assert background_process.stage_process_completion_event(
            session.session_id,
            event,
        ) is True
        monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

        def _provider_must_not_restart(*_args, **_kwargs):
            raise AssertionError(
                "provider preparation reached after auth self-heal receipt commit"
            )

        monkeypatch.setattr(
            routes,
            "_prepare_chat_start_session_for_stream",
            _provider_must_not_restart,
        )
        replay = routes._start_chat_stream_for_session(
            session,
            msg="[IMPORTANT: exact process completed]",
            attachments=[],
            workspace=session.workspace,
            model=session.model,
            source="process_wakeup",
        )
    finally:
        with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
            background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()

    assert replay == {
        "delivery_already_committed": True,
        "delivery_acknowledged": True,
        "_status": 200,
    }
    assert registry.finish_calls == [(event, True), (event, True)]
    assert _AuthSelfHealingAgent.run_calls == 2
    replayed = Session.load(session.session_id)
    assert replayed is not None
    assert replayed.messages == transcript_before_replay


def test_auth_self_heal_error_cannot_stamp_prior_assistant_as_terminal(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_auth_self_heal_error",
        stream_id="stream-native-auth-self-heal-error",
    )
    prior_assistant = {
        "role": "assistant",
        "content": "Prior answer must not become this wakeup's receipt owner",
        "timestamp": 2.0,
    }
    session.messages.append(copy.deepcopy(prior_assistant))
    session.context_messages.append(copy.deepcopy(prior_assistant))
    session.save(touch_updated_at=False)
    event = _make_exact_completion_event(session.session_id, "native-auth-heal-error")
    registry = _install_exact_event_registry(monkeypatch)
    _AuthSelfHealErrorAgent.run_calls = 0

    with mock.patch.object(
        streaming,
        "_attempt_credential_self_heal",
        return_value={
            "provider": "test-provider",
            "api_key": "still-invalid-synthetic-key",
            "base_url": None,
        },
    ):
        _run_exact_process_wakeup(
            session,
            tmp_path,
            event,
            _AuthSelfHealErrorAgent,
        )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert _AuthSelfHealErrorAgent.run_calls == 2
    assert registry.finish_calls == [(event, True)]
    persisted_prior = next(
        message
        for message in saved.messages
        if isinstance(message, dict)
        and message.get("content") == prior_assistant["content"]
    )
    assert streaming._PROCESS_COMPLETION_RECEIPTS_KEY not in persisted_prior
    terminal = saved.messages[-1]
    assert terminal.get("role") == "assistant"
    assert terminal.get("_error") is True
    assert terminal[streaming._PROCESS_COMPLETION_RECEIPTS_KEY]


def test_cancel_during_auth_self_heal_keeps_exact_claim_uncommitted(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_auth_self_heal_cancel",
        stream_id="stream-native-auth-self-heal-cancel",
    )
    event = _make_exact_completion_event(session.session_id, "native-auth-heal-cancel")
    registry = _install_exact_event_registry(monkeypatch)
    _AuthSelfHealCancellingAgent.run_calls = 0
    _AuthSelfHealCancellingAgent.stream_id = session.active_stream_id

    with mock.patch.object(
        streaming,
        "_attempt_credential_self_heal",
        return_value={
            "provider": "test-provider",
            "api_key": "refreshed-synthetic-key",
            "base_url": None,
        },
    ):
        emitted = _run_exact_process_wakeup(
            session,
            tmp_path,
            event,
            _AuthSelfHealCancellingAgent,
        )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert _AuthSelfHealCancellingAgent.run_calls == 2
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event
    assert streaming._durable_process_completion_receipt_status(
        session.session_id,
        [event],
    ) == "absent"
    assert not any(
        streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in saved.messages
        if isinstance(message, dict)
    )
    assert any(event_name == "cancel" for event_name, _payload in emitted)


def test_ordinary_success_skips_native_terminal_full_session_snapshot(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="ordinary_success_without_exact_claim",
        stream_id="stream-ordinary-success-without-exact-claim",
    )
    session.pending_user_source = "webui"
    session.save(touch_updated_at=False)
    stream_queue = queue.Queue()
    streaming.STREAMS[session.active_stream_id] = stream_queue
    config.STREAM_PARTIAL_TEXT[session.active_stream_id] = ""
    snapshot_spy = mock.Mock(wraps=streaming._snapshot_native_terminal_state)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(
             streaming,
             "resolve_model_provider",
             return_value=("test-model", "test-provider", None),
         ), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(
             streaming,
             "_snapshot_native_terminal_state",
             snapshot_spy,
         ):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model=session.model,
            workspace=str(tmp_path),
            stream_id=session.active_stream_id,
        )

    assert snapshot_spy.call_count == 0
    saved = Session.load(session.session_id)
    assert saved is not None
    assert any(
        message.get("role") == "assistant"
        and message.get("content") == "Stream reply"
        for message in saved.messages
        if isinstance(message, dict)
    )


def test_cancelled_process_wakeup_claim_remains_uncommitted(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_cancelled_exact_claim",
        stream_id="stream-native-cancelled-exact-claim",
    )
    event = _make_exact_completion_event(session.session_id, "native-cancelled")
    registry = _install_exact_event_registry(monkeypatch)
    _CancellingSuccessfulAgent.stream_id = session.active_stream_id

    _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _CancellingSuccessfulAgent,
    )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event
    assert not any(
        streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in saved.messages
        if isinstance(message, dict)
    )
    assert not any(
        message.get("content") == "Stream reply"
        for message in saved.messages
        if isinstance(message, dict)
    )


def test_stale_process_wakeup_claim_remains_uncommitted(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_stale_exact_claim",
        stream_id="stream-native-stale-exact-claim",
    )
    event = _make_exact_completion_event(session.session_id, "native-stale")
    registry = _install_exact_event_registry(monkeypatch)

    _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _StaleRateLimitedAgent,
    )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event
    assert saved.active_stream_id == _StaleRateLimitedAgent.replacement_stream_id
    assert saved.pending_user_message == "newer owner's pending turn"
    assert saved.pending_user_source == "webui"
    assert sum(
        message.get("content") == "newer owner transcript"
        for message in saved.messages
        if isinstance(message, dict)
    ) == 1
    assert not any(
        streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in saved.messages
        if isinstance(message, dict)
    )


@pytest.mark.parametrize("readback_status", ["unavailable", "invalid"])
def test_rate_limited_process_wakeup_unreadable_or_invalid_readback_skips_cleanup_save(
    tmp_path,
    monkeypatch,
    readback_status,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_rate_limit_unavailable_readback",
        stream_id="stream-native-rate-limit-unavailable-readback",
    )
    event = _make_exact_completion_event(session.session_id, "native-unavailable")
    registry = _install_exact_event_registry(monkeypatch)
    if readback_status == "invalid":
        session.messages[0][streaming._PROCESS_COMPLETION_RECEIPTS_KEY] = [
            "malformed-receipt"
        ]
        session.save(touch_updated_at=False)
    original_safe_replace = models._safe_replace
    original_load = Session.load
    failure_state = {"readback_unavailable": False, "terminal_save_attempts": 0}

    def _fail_terminal_sidecar_replace(src, dst):
        cached = models.SESSIONS.get(session.session_id)
        has_terminal_receipt = cached is not None and any(
            isinstance(message, dict)
            and message.get("_error")
            and message.get(streaming._PROCESS_COMPLETION_RECEIPTS_KEY)
            for message in (cached.messages or [])
        )
        if Path(dst) == session.path and has_terminal_receipt:
            failure_state["terminal_save_attempts"] += 1
            failure_state["readback_unavailable"] = True
            raise OSError("synthetic terminal sidecar failure")
        return original_safe_replace(src, dst)

    def _fail_fresh_receipt_load(cls, sid):
        if (
            readback_status == "unavailable"
            and failure_state["readback_unavailable"]
            and sid == session.session_id
        ):
            raise OSError("synthetic receipt read-back failure")
        return original_load(sid)

    monkeypatch.setattr(models, "_safe_replace", _fail_terminal_sidecar_replace)
    monkeypatch.setattr(Session, "load", classmethod(_fail_fresh_receipt_load))

    _run_exact_process_wakeup(session, tmp_path, event, _RateLimitedAgent)

    assert failure_state == {
        "readback_unavailable": True,
        "terminal_save_attempts": 1,
    }
    cached = models.SESSIONS[session.session_id]
    assert cached.messages[-1].get("_error") is True
    assert cached.messages[-1][streaming._PROCESS_COMPLETION_RECEIPTS_KEY]
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event

    failure_state["readback_unavailable"] = False
    persisted = original_load(session.session_id)
    assert persisted is not None
    assert not any(
        message.get("_error")
        and streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in persisted.messages
        if isinstance(message, dict)
    )


def test_late_cancel_after_process_wakeup_receipt_commit_keeps_delivery(
    tmp_path,
    monkeypatch,
):
    stream_id = "stream-native-late-cancel-after-receipt"
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_late_cancel_after_receipt",
        stream_id=stream_id,
    )
    event = _make_exact_completion_event(session.session_id, "native-late-cancel")
    registry = _install_exact_event_registry(monkeypatch)
    original_save = Session.save
    cancellation = {"set_after_receipt": 0}

    def _save_and_cancel_after_receipt(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        has_receipt = any(
            isinstance(message, dict)
            and message.get(streaming._PROCESS_COMPLETION_RECEIPTS_KEY)
            for message in (self.messages or [])
        )
        if (
            self.session_id == session.session_id
            and has_receipt
            and cancellation["set_after_receipt"] == 0
        ):
            cancellation["set_after_receipt"] += 1
            config.CANCEL_FLAGS[stream_id].set()
        return result

    monkeypatch.setattr(Session, "save", _save_and_cancel_after_receipt)

    emitted = _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _CountingSuccessfulAgent,
    )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert cancellation == {"set_after_receipt": 1}
    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.empty()
    assert any(
        message.get("content") == "Stream reply"
        and message.get(streaming._PROCESS_COMPLETION_RECEIPTS_KEY)
        for message in saved.messages
        if isinstance(message, dict)
    )
    assert any(event_name == "cancel" for event_name, _payload in emitted)


def test_post_receipt_pause_clear_failure_keeps_first_terminal_commit(
    tmp_path,
    monkeypatch,
):
    stream_id = "stream-native-pause-clear-after-receipt-failure"
    previous_pause = {
        "paused": True,
        "model": "test-model",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 0,
        "credential_state_fingerprint": "fingerprint-before",
    }
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_pause_clear_after_receipt_failure",
        stream_id=stream_id,
        process_wakeup_pause=previous_pause,
    )
    event = _make_exact_completion_event(session.session_id, "native-pause-clear")
    registry = _install_exact_event_registry(monkeypatch)
    original_save = Session.save
    saves = {"receipt_published": 0, "pause_clear_failed": 0}

    def _fail_pause_clear_after_receipt(self, *args, **kwargs):
        has_receipt = any(
            isinstance(message, dict)
            and message.get(streaming._PROCESS_COMPLETION_RECEIPTS_KEY)
            for message in (self.messages or [])
        )
        if (
            self.session_id == session.session_id
            and has_receipt
            and not (self.process_wakeup_pause or {})
            and saves["receipt_published"] == 1
            and saves["pause_clear_failed"] == 0
        ):
            saves["pause_clear_failed"] += 1
            raise OSError("synthetic pause-clear save failure")
        result = original_save(self, *args, **kwargs)
        if self.session_id == session.session_id and has_receipt:
            saves["receipt_published"] += 1
            if saves["receipt_published"] == 1:
                # Keep the stale-owner guard permissive so this regression
                # proves delivery commitment itself is the linearization point.
                self.active_stream_id = stream_id
        return result

    monkeypatch.setattr(Session, "save", _fail_pause_clear_after_receipt)

    emitted = _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _CountingSuccessfulAgent,
    )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert saves == {"receipt_published": 1, "pause_clear_failed": 1}
    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.empty()
    assert saved.process_wakeup_pause == previous_pause
    assert models.SESSIONS[session.session_id].process_wakeup_pause == previous_pause
    assert not any(
        message.get("_error")
        for message in saved.messages
        if isinstance(message, dict)
    )
    receipt_messages = [
        message
        for message in saved.messages
        if isinstance(message, dict)
        and message.get(streaming._PROCESS_COMPLETION_RECEIPTS_KEY)
    ]
    assert len(receipt_messages) == 1
    assert receipt_messages[0].get("content") == "Stream reply"
    assert streaming._durable_process_completion_receipt_status(
        session.session_id,
        [event],
    ) == "committed"
    assert any(event_name == "done" for event_name, _payload in emitted)
    assert not any(event_name == "apperror" for event_name, _payload in emitted)


def test_rate_limited_process_wakeup_ack_replay_does_not_call_provider_twice(
    tmp_path,
    monkeypatch,
):
    from api import background_process

    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_error_ack_replay",
        stream_id="stream-native-error-ack-replay",
    )
    event = _make_exact_completion_event(session.session_id, "native-error-replay")
    registry = _install_exact_event_registry(monkeypatch)
    registry.fail_committed_remaining = 1

    _run_exact_process_wakeup(session, tmp_path, event, _RateLimitedAgent)

    assert registry.finish_calls == [(event, True)]
    assert registry.completion_queue.get_nowait() is event
    with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
    try:
        assert background_process.stage_process_completion_event(
            session.session_id,
            event,
        ) is True
        monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

        def _provider_must_not_restart(*_args, **_kwargs):
            raise AssertionError("provider preparation reached for committed error replay")

        monkeypatch.setattr(
            routes,
            "_prepare_chat_start_session_for_stream",
            _provider_must_not_restart,
        )
        replay = routes._start_chat_stream_for_session(
            session,
            msg="[IMPORTANT: exact process completed]",
            attachments=[],
            workspace=session.workspace,
            model=session.model,
            source="process_wakeup",
        )
    finally:
        with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
            background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()

    assert replay["delivery_already_committed"] is True
    assert replay["delivery_acknowledged"] is True
    assert registry.finish_calls == [(event, True), (event, True)]
    saved = Session.load(session.session_id)
    assert saved is not None
    assert sum(
        bool(message.get("_error"))
        for message in saved.messages
        if isinstance(message, dict)
    ) == 1


def test_process_wakeup_settlement_exception_requeues_and_replays_without_provider(
    tmp_path,
    monkeypatch,
):
    from api import background_process

    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_settlement_exception",
        stream_id="stream-native-settlement-exception",
    )
    event = _make_exact_completion_event(session.session_id, "native-settle-error")
    registry = _install_exact_event_registry(monkeypatch)
    registry.raise_on_finish = True
    replay_responses = []
    with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN[session.session_id] = {
            event["event_id"],
            event["session_id"],
        }
    try:
        _run_exact_process_wakeup(session, tmp_path, event, _RateLimitedAgent)

        assert registry.finish_calls == [(event, True)]
        requeued = registry.completion_queue.get_nowait()
        assert requeued is event
        assert registry.completion_queue.empty()
        assert background_process.has_staged_process_completion_events(
            session.session_id
        ) is True

        registry.raise_on_finish = False
        monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

        def _provider_must_not_restart(*_args, **_kwargs):
            raise AssertionError(
                "provider preparation reached after thrown ACK replay"
            )

        monkeypatch.setattr(
            routes,
            "_prepare_chat_start_session_for_stream",
            _provider_must_not_restart,
        )
        monkeypatch.setattr(
            background_process,
            "_emit_bg_task_complete_events_coalesced",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            background_process,
            "_session_has_active_turn",
            lambda _sid: False,
        )

        def _replay_from_drain(
            owner,
            wakeup_prompt,
            *,
            process_id="",
            async_delegation_id="",
            completion_event=None,
        ):
            assert owner == session.session_id
            assert completion_event is event
            assert background_process.stage_process_completion_event(
                owner,
                completion_event,
            ) is True
            replay_responses.append(
                routes._start_chat_stream_for_session(
                    session,
                    msg=wakeup_prompt,
                    attachments=[],
                    workspace=session.workspace,
                    model=session.model,
                    source="process_wakeup",
                )
            )

        monkeypatch.setattr(
            background_process,
            "_start_server_side_wakeup_turn",
            _replay_from_drain,
        )
        background_process._process_one(requeued)

        assert replay_responses == [
            {
                "delivery_already_committed": True,
                "delivery_acknowledged": True,
                "_status": 200,
            }
        ]
        assert registry.finish_calls == [(event, True), (event, True)]
        assert background_process.has_staged_process_completion_events(
            session.session_id
        ) is False
    finally:
        with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
            background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            config.BG_TASK_COMPLETE_EVENTS_SEEN.pop(session.session_id, None)


def test_process_wakeup_partial_live_requeue_uses_unconsumed_suffix_as_scheduler(
    tmp_path,
    monkeypatch,
):
    from api import background_process

    owner = "native_partial_requeue"
    events = [
        _make_exact_completion_event(owner, "native-partial-first"),
        _make_exact_completion_event(owner, "native-partial-second"),
        _make_exact_completion_event(owner, "native-partial-third"),
    ]
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id=owner,
        stream_id="stream-native-partial-requeue",
    )
    terminal = {"role": "assistant", "content": "already committed"}
    assert len(
        streaming._stamp_process_completion_receipts(
            terminal,
            events,
            session_id=owner,
        )
    ) == len(events)
    session.messages.append(terminal)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.pending_server_instance_id = None
    session.save()
    replay_responses = []

    class _PartialQueue:
        def __init__(self):
            self.calls = []
            self.queued = []

        def put(self, event):
            self.calls.append(event)
            if event is events[1]:
                raise RuntimeError("synthetic live queue failure")
            self.queued.append(event)

    class _RaisingRegistry:
        def __init__(self):
            self.completion_queue = _PartialQueue()
            self.finish_calls = []
            self.consumed = set()
            self.raise_on_second = True

        def finish_notification_delivery(self, event, committed):
            self.finish_calls.append((event, committed))
            if self.raise_on_second and event is events[1]:
                raise RuntimeError("synthetic settlement failure")
            self.consumed.add(event["session_id"])
            return True

        def is_completion_consumed(self, process_id):
            return process_id in self.consumed

    registry = _RaisingRegistry()
    fake_module = types.ModuleType("tools.process_registry")
    fake_module.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_module)
    delivery_ids = {event["event_id"] for event in events}
    with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
        background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN[owner] = set(delivery_ids)
    try:
        assert streaming._settle_native_process_completion_claims(
            registry,
            events,
            committed=True,
        ) is False

        # Event 0 was already ACKed. Recovery attempts only the throwing and
        # unattempted suffix; event 2 remains unconsumed and is the scheduler.
        assert registry.completion_queue.calls == events[1:]
        assert registry.completion_queue.queued == [events[2]]
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            assert owner not in config.BG_TASK_COMPLETE_EVENTS_SEEN
        assert background_process.has_staged_process_completion_events(owner) is True

        registry.raise_on_second = False
        monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

        def _provider_must_not_restart(*_args, **_kwargs):
            raise AssertionError(
                "provider preparation reached after partial thrown-ACK replay"
            )

        monkeypatch.setattr(
            routes,
            "_prepare_chat_start_session_for_stream",
            _provider_must_not_restart,
        )
        monkeypatch.setattr(
            background_process,
            "_emit_bg_task_complete_events_coalesced",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            background_process,
            "_session_has_active_turn",
            lambda _sid: False,
        )

        def _replay_from_drain(
            replay_owner,
            wakeup_prompt,
            *,
            process_id="",
            async_delegation_id="",
            completion_event=None,
        ):
            assert replay_owner == owner
            assert completion_event is events[2]
            assert background_process.stage_process_completion_event(
                replay_owner,
                completion_event,
            ) is True
            replay_responses.append(
                routes._start_chat_stream_for_session(
                    session,
                    msg=wakeup_prompt,
                    attachments=[],
                    workspace=session.workspace,
                    model=session.model,
                    source="process_wakeup",
                )
            )

        monkeypatch.setattr(
            background_process,
            "_start_server_side_wakeup_turn",
            _replay_from_drain,
        )
        background_process._process_one(registry.completion_queue.queued.pop())

        assert replay_responses == [
            {
                "delivery_already_committed": True,
                "delivery_acknowledged": True,
                "_status": 200,
            }
        ]
        assert registry.finish_calls == [
            (events[0], True),
            (events[1], True),
            (events[0], True),
            (events[1], True),
            (events[2], True),
        ]
        assert background_process.has_staged_process_completion_events(owner) is False
    finally:
        with background_process._STAGED_PROCESS_COMPLETION_EVENTS_LOCK:
            background_process._STAGED_PROCESS_COMPLETION_EVENTS.clear()
        with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            config.BG_TASK_COMPLETE_EVENTS_SEEN.pop(owner, None)


def test_returned_false_requeue_cannot_race_ahead_of_seen_release(
    monkeypatch,
):
    from api import background_process

    owner = "native_atomic_false_requeue"
    event = _make_exact_completion_event(owner, "native-atomic-false")
    live_queue = queue.Queue()
    drain_lock_attempted = threading.Event()
    dispatched = threading.Event()
    original_seen_lock = config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK

    class _SignalingSeenLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "native-fast-drain":
                drain_lock_attempted.set()
            return original_seen_lock.acquire(*args, **kwargs)

        def release(self):
            return original_seen_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            self.release()

    signaling_seen_lock = _SignalingSeenLock()
    monkeypatch.setattr(
        config,
        "BG_TASK_COMPLETE_EVENTS_SEEN_LOCK",
        signaling_seen_lock,
    )

    class _FalseRegistry:
        completion_queue = live_queue

        def finish_notification_delivery(self, actual, committed):
            assert actual is event
            assert committed is False
            live_queue.put(actual)
            assert drain_lock_attempted.wait(timeout=2)
            return False

        def is_completion_consumed(self, _process_id):
            return False

    registry = _FalseRegistry()
    fake_module = types.ModuleType("tools.process_registry")
    fake_module.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_module)
    monkeypatch.setattr(
        background_process,
        "_emit_bg_task_complete_events_coalesced",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        background_process,
        "_session_has_active_turn",
        lambda _sid: False,
    )
    monkeypatch.setattr(
        background_process,
        "_start_server_side_wakeup_turn",
        lambda *_args, **_kwargs: dispatched.set(),
    )
    with signaling_seen_lock:
        config.BG_TASK_COMPLETE_EVENTS_SEEN[owner] = {
            event["event_id"],
            event["session_id"],
        }
    with config.PROCESS_SESSION_INDEX_LOCK:
        config.PROCESS_SESSION_INDEX[owner] = owner

    drain = threading.Thread(
        target=lambda: background_process._process_one(
            live_queue.get(timeout=2)
        ),
        name="native-fast-drain",
    )
    drain.start()
    try:
        assert streaming._finalize_process_completion_claims(
            registry,
            [event],
            committed=False,
            raise_on_error=True,
        ) is False
        drain.join(timeout=2)
        assert drain.is_alive() is False
        assert dispatched.is_set() is True
        with signaling_seen_lock:
            assert config.BG_TASK_COMPLETE_EVENTS_SEEN[owner] == {
                event["event_id"]
            }
    finally:
        monkeypatch.setattr(
            config,
            "BG_TASK_COMPLETE_EVENTS_SEEN_LOCK",
            original_seen_lock,
        )
        with original_seen_lock:
            config.BG_TASK_COMPLETE_EVENTS_SEEN.pop(owner, None)
        with config.PROCESS_SESSION_INDEX_LOCK:
            config.PROCESS_SESSION_INDEX.pop(owner, None)


def test_rotated_process_wakeup_owner_cannot_stamp_or_ack_exact_claim(
    tmp_path,
    monkeypatch,
):
    session = _new_exact_process_wakeup_session(
        tmp_path,
        session_id="native_rotation_original_owner",
        stream_id="stream-native-rotation-original-owner",
    )
    event = _make_exact_completion_event(session.session_id, "native-rotation")
    registry = _install_exact_event_registry(monkeypatch)
    _RotatingSuccessfulAgent.before_terminal_state = None

    _run_exact_process_wakeup(
        session,
        tmp_path,
        event,
        _RotatingSuccessfulAgent,
    )

    before_terminal = _RotatingSuccessfulAgent.before_terminal_state
    assert before_terminal is not None
    cached = models.SESSIONS.get(session.session_id)
    assert cached is not None
    assert cached.__dict__ == before_terminal
    assert registry.finish_calls == [(event, False)]
    assert registry.completion_queue.get_nowait() is event
    assert not (
        models.SESSION_DIR
        / f"{_RotatingSuccessfulAgent.continuation_session_id}.json"
    ).exists()
    assert not any(
        streaming._PROCESS_COMPLETION_RECEIPTS_KEY in message
        for message in cached.messages
        if isinstance(message, dict)
    )


class _FakeCredentialPoolEntry:
    def __init__(self, payload):
        self._payload = dict(payload)
        for key, value in self._payload.items():
            setattr(self, key, value)
        self.source = self._payload.get("source", "manual")
        self.label = self._payload.get("label", "")
        self.key_source = self._payload.get("key_source", "")

    def to_dict(self):
        return dict(self._payload)


class _FakeCredentialPool:
    def __init__(self, entries):
        self._entries = list(entries)

    def entries(self):
        return list(self._entries)


def _install_fake_agent_credential_pool(monkeypatch, pool_data):
    fake_agent = types.ModuleType("agent")
    fake_agent.__path__ = []
    fake_pool_module = types.ModuleType("agent.credential_pool")

    def _load_pool(provider_id):
        entries = pool_data.get(provider_id, [])
        return _FakeCredentialPool(_FakeCredentialPoolEntry(entry) for entry in entries)

    fake_pool_module.load_pool = _load_pool
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_pool_module)


def _run_failing_process_wakeup(session: Session, tmp_path, *, stream_id=None):
    stream_id = str(stream_id or session.active_stream_id)
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_CredentialPoolEmptyAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


def _run_failing_process_wakeup_route(
    session: Session,
    tmp_path,
    *,
    route_model,
    route_provider=None,
    resolved_model,
    resolved_provider,
    resolved_base_url=None,
    custom_connection=(None, None),
    stream_id=None,
):
    stream_id = str(stream_id or session.active_stream_id)
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_CredentialPoolEmptyAgent), \
         mock.patch.object(
             streaming,
             "resolve_model_provider",
             return_value=(resolved_model, resolved_provider, resolved_base_url),
         ), \
         mock.patch.object(
             streaming,
             "resolve_custom_provider_connection",
             return_value=custom_connection,
         ), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model=route_model,
            model_provider=route_provider,
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


def _patch_process_wakeup_route(monkeypatch, tmp_path, *, model, provider):
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: (model, provider, False),
    )


def test_credential_empty_process_wakeup_pauses_repeated_automatic_turns(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause",
        title="Wakeup pause",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "Earlier prompt", "timestamp": 1}],
        context_messages=[{"role": "user", "content": "Earlier prompt"}],
        active_stream_id="stream-wakeup-pause-1",
        pending_user_message="[IMPORTANT: Background process first completed.]",
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    events = _run_failing_process_wakeup(session, tmp_path)
    saved = Session.load(session.session_id)
    assert saved is not None
    assert any(event == "apperror" and data["type"] == "credential_pool_empty" for event, data in events)
    assert sum(1 for message in saved.messages if message.get("_error")) == 1
    assert saved.process_wakeup_pause["paused"] is True
    assert saved.process_wakeup_pause["classification"] == "credential_pool_empty"
    assert saved.process_wakeup_pause["suppressed_count"] == 0
    assert saved.process_wakeup_pause["credential_state_fingerprint"]
    context_before = list(saved.context_messages)
    messages_before = list(saved.messages)

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("paused process_wakeup must not start another provider call")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process second completed.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved_after = Session.load(session.session_id)
    assert saved_after is not None
    assert saved_after.messages == messages_before
    assert saved_after.context_messages == context_before
    assert saved_after.process_wakeup_pause["suppressed_count"] == 1
    assert "last_suppressed_at" in saved_after.process_wakeup_pause


def test_cancelled_stale_process_wakeup_credential_failure_records_pause(tmp_path, monkeypatch):
    stream_id = "stream-cancelled-stale-wakeup"
    session = Session(
        session_id="cancelled_stale_wakeup_pause",
        title="Cancelled stale wakeup pause",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "Earlier prompt", "timestamp": 1}],
        context_messages=[{"role": "user", "content": "Earlier prompt"}],
        active_stream_id=stream_id,
        pending_user_message="[IMPORTANT: Background process first completed.]",
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    class _CancelledStaleCredentialPoolEmptyAgent(_MockAgent):
        def run_conversation(self, **_kwargs):
            cancel_flag = config.CANCEL_FLAGS.get(stream_id)
            assert cancel_flag is not None
            cancel_flag.set()
            stale_session = models.SESSIONS[self.session_id]
            stale_session.active_stream_id = "stream-newer-run"
            stale_session.save(touch_updated_at=False)
            raise RuntimeError("All 0 credential(s) exhausted for test-provider")

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_CancelledStaleCredentialPoolEmptyAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    events = [(item[0], item[1]) for item in list(fake_queue.queue)]
    assert any(event == "cancel" for event, _data in events)
    assert not any(event == "apperror" and data["type"] == "credential_pool_empty" for event, data in events)
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["paused"] is True
    assert saved.process_wakeup_pause["classification"] == "credential_pool_empty"
    assert saved.process_wakeup_pause["model"] == "test-model"
    assert saved.process_wakeup_pause["provider"] == "test-provider"
    assert saved.process_wakeup_pause["suppressed_count"] == 0

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("cancelled stale process_wakeup pause must suppress the next automatic wakeup")

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="test-model",
        provider="test-provider",
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process second completed.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved_after = Session.load(session.session_id)
    assert saved_after is not None
    assert saved_after.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_revalidates_when_credential_state_changes(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    auth_json = hermes_home / "auth.json"
    auth_json.write_text('{"credential_pool": {}}\n', encoding="utf-8")
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: hermes_home)
    session = Session(
        session_id="wakeup_pause_credential_refresh",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    auth_json.write_text(
        '{"credential_pool": {"test-provider": [{"id": "refilled-token"}]}}\n',
        encoding="utf-8",
    )
    assert models.process_wakeup_credential_state_fingerprint(session) != paused_fingerprint

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": "stream-credential-refresh", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda provider_id, *, refresh=False: provider_id == "test-provider",
    )

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after credential refill.]",
        source="process_wakeup",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-credential-refresh"
    assert captured == {
        "source": "process_wakeup",
        "model": "test-model",
        "model_provider": "test-provider",
    }
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_process_wakeup_pause_keeps_changed_credential_state_until_provider_is_usable(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    auth_json = hermes_home / "auth.json"
    auth_json.write_text('{"credential_pool": {}}\n', encoding="utf-8")
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: hermes_home)
    session = Session(
        session_id="wakeup_pause_credential_changed_still_unusable",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    auth_json.write_text(
        '{"credential_pool": {"test-provider": [{"id": "refilled-token"}]}}\n',
        encoding="utf-8",
    )
    changed_fingerprint = models.process_wakeup_credential_state_fingerprint(session)
    assert changed_fingerprint != paused_fingerprint

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("changed credential metadata must not clear until the provider is usable")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda *_args, **_kwargs: False,
    )

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after unusable credential metadata changed.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1
    assert saved.process_wakeup_pause["credential_state_fingerprint"] == changed_fingerprint


def test_process_wakeup_pause_keeps_pause_when_config_key_matches_exhausted_pool_secret(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_config_key_matches_exhausted_pool",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session
    exhausted_fingerprint = hashlib.sha256(b"same-key").hexdigest()[:16]
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda _provider: "same-key")
    monkeypatch.setattr(
        providers,
        "_pool_entry_payloads",
        lambda _provider: [{
            "last_status": "dead",
            "secret_fingerprint": exhausted_fingerprint,
        }],
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("same exhausted pool secret must not prove recovery")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed with same exhausted config key.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_keeps_pause_when_config_key_has_no_pool_evidence(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_config_key_no_pool_evidence",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda _provider: "generic-config-key")
    monkeypatch.setattr(providers, "_pool_entry_payloads", lambda _provider: [])
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("a generic configured key is not pool-lane recovery evidence")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed with generic config key.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_keeps_pause_when_unusable_pool_entry_lacks_fingerprint(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_unusable_pool_entry_lacks_fingerprint",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda _provider: "generic-config-key")
    monkeypatch.setattr(
        providers,
        "_pool_entry_payloads",
        lambda _provider: [{"last_status": "dead"}],
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("unknown exhausted pool secret must not prove recovery")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed with unknown exhausted pool key.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_clears_when_config_key_differs_from_exhausted_pool_secret(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_config_key_differs_from_exhausted_pool",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session
    exhausted_fingerprint = hashlib.sha256(b"old-pool-key").hexdigest()
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda _provider: "new-config-key")
    monkeypatch.setattr(
        providers,
        "_pool_entry_payloads",
        lambda _provider: [{
            "last_status": "dead",
            "secret_fingerprint": exhausted_fingerprint,
        }],
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": "stream-config-key-recovered", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed with new config key.]",
        source="process_wakeup",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-config-key-recovered"
    assert captured == {
        "source": "process_wakeup",
        "model": "test-model",
        "model_provider": "test-provider",
    }
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_process_wakeup_pause_successful_clear_serializes_against_concurrent_suppression(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_clear_vs_suppress_race",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    recovery_revalidating = threading.Event()
    release_recovery = threading.Event()
    responses = {}
    starts = []
    starts_lock = threading.Lock()

    def _provider_has_recovery(_session, **_kwargs):
        if threading.current_thread().name == "pause-clear":
            recovery_revalidating.set()
            assert release_recovery.wait(timeout=3)
            return True
        return False

    def _fake_start_run(s, **kwargs):
        with starts_lock:
            starts.append((threading.current_thread().name, kwargs.get("source")))
        return {"stream_id": f"stream-{threading.current_thread().name}", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_process_wakeup_provider_has_recovery_credential", _provider_has_recovery)
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    def _run(name):
        responses[name] = routes.start_session_turn(
            session.session_id,
            f"[IMPORTANT: {name}]",
            source="process_wakeup",
        )

    clear_thread = threading.Thread(target=_run, args=("clear",), name="pause-clear")
    clear_thread.start()
    assert recovery_revalidating.wait(timeout=3)

    suppress_thread = threading.Thread(target=_run, args=("suppress",), name="pause-suppress")
    suppress_thread.start()
    release_recovery.set()

    clear_thread.join(timeout=3)
    suppress_thread.join(timeout=3)
    assert not clear_thread.is_alive()
    assert not suppress_thread.is_alive()

    assert responses["clear"]["_status"] == 200
    assert responses["suppress"]["_status"] == 200
    assert all(resp.get("error") != PROCESS_WAKEUP_PAUSE_ERROR for resp in responses.values())
    assert starts
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_streaming_success_pause_clear_serializes_against_concurrent_suppression(tmp_path, monkeypatch):
    stream_id = "streaming-pause-clear-vs-suppress"
    session_id = "streaming_pause_clear_vs_suppress"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    previous_pause = {
        "paused": True,
        "model": "test-model",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 2,
        "credential_state_fingerprint": "fingerprint-before",
    }
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "before", "timestamp": 1.0}],
        context_messages=[{"role": "user", "content": "before"}],
        active_stream_id=stream_id,
        pending_user_message="recover",
        pending_user_source="process_wakeup",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    responses = {}
    starts = []

    def _fake_start_run(s, **kwargs):
        starts.append((threading.current_thread().name, kwargs.get("source")))
        return {
            "stream_id": f"stream-{threading.current_thread().name}",
            "session_id": s.session_id,
            "_status": 200,
        }

    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    suppress_started = threading.Event()
    suppress_thread_holder = {}

    def _run_suppress():
        suppress_started.set()
        responses["suppress"] = routes.start_session_turn(
            session_id,
            "[IMPORTANT: Background process completed while streaming success settles.]",
            source="process_wakeup",
        )

    original_save = Session.save
    save_calls = {"clear_save": 0}

    def _save_and_race_suppression(self, *args, **kwargs):
        if (
            getattr(self, "session_id", None) == session_id
            and save_calls["clear_save"] == 0
            and not (getattr(self, "process_wakeup_pause", {}) or {})
        ):
            lock = config.SESSION_AGENT_LOCKS.get(session_id)
            assert lock is not None
            assert lock.locked()
            save_calls["clear_save"] += 1
            suppress_thread = threading.Thread(target=_run_suppress, name="pause-suppress")
            suppress_thread_holder["thread"] = suppress_thread
            suppress_thread.start()
            assert suppress_started.wait(timeout=3)
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", _save_and_race_suppression)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    suppress_thread = suppress_thread_holder.get("thread")
    assert suppress_thread is not None
    suppress_thread.join(timeout=3)
    assert not suppress_thread.is_alive()
    assert save_calls["clear_save"] == 1
    assert responses["suppress"]["_status"] == 200
    assert responses["suppress"]["stream_id"] == "stream-pause-suppress"
    assert starts == [("pause-suppress", "process_wakeup")]
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_streaming_success_pause_clear_preserves_concurrent_session_update(tmp_path, monkeypatch):
    stream_id = "streaming-pause-clear-preserves-rename"
    session_id = "streaming_pause_clear_preserves_rename"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    previous_pause = {
        "paused": True,
        "model": "test-model",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 2,
        "credential_state_fingerprint": "fingerprint-before",
    }
    session = Session(
        session_id=session_id,
        title="Original title",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "before", "timestamp": 1.0}],
        context_messages=[{"role": "user", "content": "before"}],
        active_stream_id=stream_id,
        pending_user_message="recover",
        pending_user_source="process_wakeup",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_save = Session.save
    original_get_session = streaming.get_session
    state = {"success_saved": False, "renamed": False}

    def _save_and_mark_success_snapshot(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        if (
            getattr(self, "session_id", None) == session_id
            and getattr(self, "active_stream_id", None) is None
            and any(
                msg.get("role") == "assistant" and msg.get("content") == "Stream reply"
                for msg in (getattr(self, "messages", None) or [])
            )
        ):
            state["success_saved"] = True
        return result

    def _get_session_after_concurrent_rename(sid, *args, **kwargs):
        if sid == session_id and state["success_saved"] and not state["renamed"]:
            latest = Session.load(session_id)
            assert latest is not None
            latest.title = "Renamed during settle"
            original_save(latest, touch_updated_at=False)
            with models.LOCK:
                models.SESSIONS.pop(session_id, None)
            state["renamed"] = True
        return original_get_session(sid, *args, **kwargs)

    monkeypatch.setattr(Session, "save", _save_and_mark_success_snapshot)
    monkeypatch.setattr(streaming, "get_session", _get_session_after_concurrent_rename)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    assert state == {"success_saved": True, "renamed": True}
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.title == "Renamed during settle"
    assert saved.process_wakeup_pause == {}
    done_payloads = [item[1] for item in list(stream_queue.queue) if item[0] == "done"]
    assert done_payloads
    assert done_payloads[-1]["session"]["title"] == "Renamed during settle"


def test_process_wakeup_pause_survives_rotation_style_auth_rewrite(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    auth_json = hermes_home / "auth.json"
    auth_json.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "other-provider": [
                        {
                            "id": "other-token",
                            "source": "oauth",
                            "auth_type": "oauth",
                            "access_token": "old-access",
                            "refresh_token": "old-refresh",
                            "expires_at": 1000,
                            "last_status": "ok",
                            "request_count": 1,
                            "updated_at": "old",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: hermes_home)
    session = Session(
        session_id="wakeup_pause_auth_rotation",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    auth_json.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "other-provider": [
                        {
                            "id": "other-token",
                            "source": "oauth",
                            "auth_type": "oauth",
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_at": 2000,
                            "last_status": "ok",
                            "request_count": 2,
                            "updated_at": "new",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert models.process_wakeup_credential_state_fingerprint(session) == paused_fingerprint

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("token rotation must not clear a paused wakeup")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed during token rotation.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1
    assert saved.process_wakeup_pause["credential_state_fingerprint"] == paused_fingerprint


def test_process_wakeup_pause_revalidates_status_recovery_without_fingerprint_change(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    auth_json = hermes_home / "auth.json"
    exhausted_entry = {
        "id": "pooled-token",
        "label": "Test provider credential",
        "source": "manual",
        "auth_type": "api_key",
        "runtime_api_key": "sk-test-provider",
        "last_status": "exhausted",
        "last_status_at": "2026-07-07T00:00:00Z",
        "last_error_code": "429",
        "last_error_reset_at": "2999-01-01T00:00:00Z",
    }
    pool_data = {"test-provider": [dict(exhausted_entry)]}
    auth_json.write_text(json.dumps({"credential_pool": pool_data}), encoding="utf-8")
    _install_fake_agent_credential_pool(monkeypatch, pool_data)
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: hermes_home)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )
    session = Session(
        session_id="wakeup_pause_status_recovery",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    recovered_entry = dict(exhausted_entry)
    recovered_entry["last_status"] = "ok"
    recovered_entry["last_status_at"] = "2026-07-07T00:01:00Z"
    recovered_entry["last_error_reset_at"] = None
    pool_data["test-provider"] = [recovered_entry]
    auth_json.write_text(json.dumps({"credential_pool": pool_data}), encoding="utf-8")
    assert models.process_wakeup_credential_state_fingerprint(session) == paused_fingerprint

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": "stream-status-recovered", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after status recovery.]",
        source="process_wakeup",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-status-recovered"
    assert captured == {
        "source": "process_wakeup",
        "model": "test-model",
        "model_provider": "test-provider",
    }
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_process_wakeup_pause_revalidates_at_provider_model_with_canonical_provider(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_at_model_recovered",
        workspace=str(tmp_path),
        model="@test-provider:test-model",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    calls = []

    def _provider_has_process_wakeup_recovery_credential(provider_id, *, refresh=False):
        calls.append((provider_id, refresh))
        return provider_id == "test-provider"

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": "stream-at-provider-recovered", "session_id": s.session_id, "_status": 200}

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="@test-provider:test-model",
        provider=None,
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        _provider_has_process_wakeup_recovery_credential,
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after @provider recovery.]",
        source="process_wakeup",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-at-provider-recovered"
    assert calls == [("test-provider", True)]
    assert captured == {
        "source": "process_wakeup",
        "model": "@test-provider:test-model",
        "model_provider": None,
    }
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_process_wakeup_pause_revalidation_uses_session_profile_not_default(tmp_path, monkeypatch):
    base_home = tmp_path / "hermes-home"
    (base_home / "profiles" / "work").mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    profiles.clear_request_profile()

    session = Session(
        session_id="wakeup_pause_named_profile_revalidation",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        profile="work",
        process_wakeup_pause={
            "version": 1,
            "paused": True,
            "source": "process_wakeup",
            "classification": "credential_pool_empty",
            "model": "test-model",
            "provider": "test-provider",
            "first_paused_at": 1.0,
            "last_error_at": 1.0,
            "visible_error_count": 1,
            "suppressed_count": 0,
        },
    )
    session.process_wakeup_pause["credential_state_fingerprint"] = (
        models.process_wakeup_credential_state_fingerprint(session)
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    calls = []

    def _provider_has_process_wakeup_recovery_credential(provider_id, *, refresh=False):
        active_profile = profiles.get_active_profile_name()
        calls.append((provider_id, refresh, active_profile))
        return active_profile == "default"

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("named-profile wakeup must not clear from default-profile credentials")

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="test-model",
        provider="test-provider",
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        _provider_has_process_wakeup_recovery_credential,
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    try:
        response = routes.start_session_turn(
            session.session_id,
            "[IMPORTANT: Background process completed while named profile is still exhausted.]",
            source="process_wakeup",
        )
    finally:
        profiles.clear_request_profile()

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    assert calls == [("test-provider", True, "work")]
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1
    assert profiles.get_active_profile_name() == "default"


@pytest.mark.parametrize(
    ("recovered_status", "recovered_reset_at"),
    [
        ("ok", None),
        ("exhausted", "2000-01-01T00:00:00Z"),
    ],
)
def test_process_wakeup_pause_revalidates_named_profile_sanitized_env_pool_recovery(
    tmp_path,
    monkeypatch,
    recovered_status,
    recovered_reset_at,
):
    base_home = tmp_path / "hermes-home"
    profile_home = base_home / "profiles" / "work"
    profile_home.mkdir(parents=True)
    api_key = "sk-or-profile-recovered"
    (profile_home / ".env").write_text(f"OPENROUTER_API_KEY={api_key}\n", encoding="utf-8")
    auth_json = profile_home / "auth.json"
    secret_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]

    def _write_pool_entry(status, reset_at):
        entry = {
            "id": "openrouter-env",
            "label": "OpenRouter env key",
            "source": "env",
            "auth_type": "api_key",
            "secret_fingerprint": f"sha256:{secret_fingerprint}",
            "last_status": status,
            "last_status_at": "2026-07-10T00:00:00Z",
            "last_error_code": "429",
        }
        if reset_at is not None:
            entry["last_error_reset_at"] = reset_at
        auth_json.write_text(
            json.dumps({"credential_pool": {"openrouter": [entry]}}),
            encoding="utf-8",
        )

    fake_auth_module = types.ModuleType("hermes_cli.auth")

    def _read_credential_pool(provider_id):
        raw = json.loads(config._get_auth_store_path().read_text(encoding="utf-8"))
        return list((raw.get("credential_pool") or {}).get(provider_id, []))

    fake_auth_module.read_credential_pool = _read_credential_pool
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth_module)
    fake_hermes_cli = sys.modules.get("hermes_cli")
    if fake_hermes_cli is not None:
        fake_hermes_cli.auth = fake_auth_module

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base_home)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    profiles.clear_request_profile()

    _write_pool_entry("exhausted", "2999-01-01T00:00:00Z")
    session = Session(
        session_id=f"wakeup_pause_named_profile_sanitized_env_{recovered_status}",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="openrouter",
        profile="work",
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="openrouter",
    )
    assert pause is not None
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    _write_pool_entry(recovered_status, recovered_reset_at)
    assert models.process_wakeup_credential_state_fingerprint(session) == paused_fingerprint

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {
            "stream_id": f"stream-named-profile-recovered-{recovered_status}",
            "session_id": s.session_id,
            "_status": 200,
        }

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="test-model",
        provider="openrouter",
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        providers.provider_has_process_wakeup_recovery_credential,
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    try:
        response = routes.start_session_turn(
            session.session_id,
            "[IMPORTANT: Background process completed after named profile credential recovery.]",
            source="process_wakeup",
        )
    finally:
        profiles.clear_request_profile()

    assert response["_status"] == 200
    assert response["stream_id"] == f"stream-named-profile-recovered-{recovered_status}"
    assert captured == {
        "source": "process_wakeup",
        "model": "test-model",
        "model_provider": "openrouter",
    }
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}
    assert profiles.get_active_profile_name() == "default"


def test_process_wakeup_pause_suppresses_at_provider_model_session(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_at_model",
        workspace=str(tmp_path),
        model="@test-provider:test-model",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="test-model",
        provider="test-provider",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("@provider:model wakeup must be suppressed on the paused lane")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("@test-provider:test-model", None, False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed for @provider:model lane.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["model"] == "test-model"
    assert saved.process_wakeup_pause["provider"] == "test-provider"
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_suppresses_openrouter_tagged_model_session(tmp_path, monkeypatch):
    monkeypatch.setitem(
        config.cfg,
        "model",
        {"provider": "anthropic", "default": "claude-sonnet-4.6"},
    )
    assert models._process_wakeup_pause_key(
        "deepseek/deepseek-r1:free",
        "openrouter",
        "credential_pool_empty",
    ) == models._process_wakeup_pause_key(
        "deepseek/deepseek-r1:free",
        None,
        "credential_pool_empty",
    )
    session = Session(
        session_id="wakeup_pause_openrouter_tagged_model",
        workspace=str(tmp_path),
        model="deepseek/deepseek-r1:free",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="deepseek/deepseek-r1:free",
        provider="openrouter",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("OpenRouter tagged wakeup must be suppressed on the paused lane")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("deepseek/deepseek-r1:free", None, False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed for OpenRouter tagged lane.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["model"] == "deepseek/deepseek-r1:free"
    assert saved.process_wakeup_pause["provider"] == "openrouter"
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_suppresses_local_endpoint_custom_slug_session(tmp_path, monkeypatch):
    monkeypatch.setitem(
        config.cfg,
        "model",
        {
            "provider": "ollama",
            "default": "llama3.2",
            "base_url": "http://ollama.internal:11434/v1",
        },
    )
    endpoint_lane = models._process_wakeup_pause_key(
        "@custom:ollama.internal:11434:llama3.2",
        None,
        "credential_pool_empty",
    )
    assert models._process_wakeup_pause_key(
        "llama3.2",
        "ollama",
        "credential_pool_empty",
    ) == endpoint_lane
    session = Session(
        session_id="wakeup_pause_local_endpoint_slug",
        workspace=str(tmp_path),
        model="@custom:ollama.internal:11434:llama3.2",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="llama3.2",
        provider="ollama",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("local-endpoint wakeup must be suppressed on the resolved provider lane")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("@custom:ollama.internal:11434:llama3.2", None, False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed for local endpoint lane.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["model"] == "llama3.2"
    assert saved.process_wakeup_pause["provider"] == endpoint_lane["provider"]
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_suppresses_custom_provider_tagged_model_session(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_custom_tagged_model",
        workspace=str(tmp_path),
        model="@custom:proxy:model:tag",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="model:tag",
        provider="custom:proxy",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("custom-provider tagged wakeup must be suppressed on the paused lane")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("@custom:proxy:model:tag", None, False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed for custom tagged lane.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["model"] == "model:tag"
    assert saved.process_wakeup_pause["provider"] == "custom:proxy"
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_suppresses_custom_provider_single_segment_session(tmp_path, monkeypatch):
    assert models._process_wakeup_pause_key(
        "tag",
        "custom:proxy",
        "credential_pool_empty",
    ) == models._process_wakeup_pause_key(
        "@custom:proxy:tag",
        None,
        "credential_pool_empty",
    )
    session = Session(
        session_id="wakeup_pause_custom_single_segment",
        workspace=str(tmp_path),
        model="@custom:proxy:tag",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="tag",
        provider="custom:proxy",
    )
    assert pause is not None
    session.save()
    models.SESSIONS[session.session_id] = session

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("custom-provider shorthand wakeup must be suppressed on the paused lane")

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("@custom:proxy:tag", None, False),
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed for custom shorthand lane.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["model"] == "tag"
    assert saved.process_wakeup_pause["provider"] == "custom:proxy"
    assert saved.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_records_route_lane_after_custom_runtime_rewrite(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_custom_runtime_rewrite",
        workspace=str(tmp_path),
        model="@custom:proxy:model:tag",
        model_provider=None,
        messages=[{"role": "user", "content": "Earlier prompt", "timestamp": 1}],
        context_messages=[{"role": "user", "content": "Earlier prompt"}],
        active_stream_id="stream-custom-runtime-rewrite",
        pending_user_message="[IMPORTANT: Background process first completed.]",
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    events = _run_failing_process_wakeup_route(
        session,
        tmp_path,
        route_model="@custom:proxy:model:tag",
        route_provider=None,
        resolved_model="model:tag",
        resolved_provider="custom:proxy",
        custom_connection=(None, "http://proxy.example/v1"),
    )
    saved = Session.load(session.session_id)
    assert saved is not None
    assert any(event == "apperror" and data["type"] == "credential_pool_empty" for event, data in events)
    assert saved.process_wakeup_pause["model"] == "model:tag"
    assert saved.process_wakeup_pause["provider"] == "custom:proxy"

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("post-rewrite custom-provider wakeup must be suppressed")

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="@custom:proxy:model:tag",
        provider=None,
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process second completed.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved_after = Session.load(session.session_id)
    assert saved_after is not None
    assert saved_after.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_records_unset_route_provider_before_runtime_backfill(tmp_path, monkeypatch):
    monkeypatch.setitem(config.cfg, "model", {"default": "claude-sonnet-test"})
    session = Session(
        session_id="wakeup_pause_unset_route_provider",
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider=None,
        messages=[{"role": "user", "content": "Earlier prompt", "timestamp": 1}],
        context_messages=[{"role": "user", "content": "Earlier prompt"}],
        active_stream_id="stream-unset-route-provider",
        pending_user_message="[IMPORTANT: Background process first completed.]",
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    events = _run_failing_process_wakeup_route(
        session,
        tmp_path,
        route_model="claude-sonnet-test",
        route_provider=None,
        resolved_model="claude-sonnet-test",
        resolved_provider=None,
    )
    saved = Session.load(session.session_id)
    assert saved is not None
    assert any(event == "apperror" and data["type"] == "credential_pool_empty" for event, data in events)
    assert saved.process_wakeup_pause["model"] == "claude-sonnet-test"
    assert saved.process_wakeup_pause["provider"] == ""

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("runtime-provider backfill must not clear the paused route lane")

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="claude-sonnet-test",
        provider=None,
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process second completed.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved_after = Session.load(session.session_id)
    assert saved_after is not None
    assert saved_after.process_wakeup_pause["suppressed_count"] == 1


def test_process_wakeup_pause_keeps_empty_provider_lane_after_fingerprint_change(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    auth_json = hermes_home / "auth.json"
    auth_json.write_text('{"credential_pool": {}}\n', encoding="utf-8")
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: hermes_home)
    monkeypatch.setitem(config.cfg, "model", {"default": "claude-sonnet-test"})
    session = Session(
        session_id="wakeup_pause_empty_provider_probe",
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider=None,
    )
    pause = models.record_process_wakeup_provider_unavailable_pause(
        session,
        classification="credential_pool_empty",
        model="claude-sonnet-test",
        provider=None,
    )
    assert pause is not None
    assert pause["provider"] == ""
    paused_fingerprint = pause["credential_state_fingerprint"]
    session.save()
    models.SESSIONS[session.session_id] = session

    auth_json.write_text(
        '{"credential_pool": {"test-provider": [{"id": "refilled-token"}]}}\n',
        encoding="utf-8",
    )
    changed_fingerprint = models.process_wakeup_credential_state_fingerprint(session)
    assert changed_fingerprint != paused_fingerprint

    def _unexpected_start_run(*_args, **_kwargs):
        raise AssertionError("empty-provider lane must prove recovery before restarting")

    _patch_process_wakeup_route(
        monkeypatch,
        tmp_path,
        model="claude-sonnet-test",
        provider=None,
    )
    monkeypatch.setattr(
        routes,
        "provider_has_process_wakeup_recovery_credential",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(routes, "_start_run", _unexpected_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after empty-provider credential change.]",
        source="process_wakeup",
    )

    assert response["_status"] == 409
    assert response["error"] == PROCESS_WAKEUP_PAUSE_ERROR
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 1
    assert saved.process_wakeup_pause["credential_state_fingerprint"] == changed_fingerprint


def test_gateway_cancel_during_completion_save_restores_process_wakeup_pause(tmp_path, monkeypatch):
    stream_id = "gateway-pause-save-race-stream"
    session_id = "gateway_pause_save_race"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gateway.test"})

    class _GatewayResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            payload = {"choices": [{"delta": {"content": "Gateway reply"}}]}
            return iter([
                ("data: " + json.dumps(payload) + "\n").encode("utf-8"),
                b"data: [DONE]\n",
            ])

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _GatewayResponse(),
    )

    previous_pause = {
        "paused": True,
        "model": "claude-sonnet-test",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 0,
        "credential_state_fingerprint": "fingerprint-before",
    }
    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="wake up",
        pending_user_source="process_wakeup",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_save = Session.save
    save_calls = {"count": 0, "cleared_pause": 0}

    def _save_and_cancel_after_success_clear(self, *args, **kwargs):
        save_calls["count"] += 1
        result = original_save(self, *args, **kwargs)
        if (
            getattr(self, "session_id", None) == session_id
            and not getattr(self, "process_wakeup_pause", None)
            and save_calls["cleared_pause"] == 0
        ):
            save_calls["cleared_pause"] += 1
            config.CANCEL_FLAGS[stream_id].set()
        return result

    monkeypatch.setattr(Session, "save", _save_and_cancel_after_success_clear)

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        "wake up",
        "claude-sonnet-test",
        str(tmp_path),
        stream_id,
        model_provider="test-provider",
    )

    assert save_calls["cleared_pause"] == 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == previous_pause
    assert saved.messages == previous_messages
    assert saved.context_messages == previous_messages
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "cancel" in queued_events
    assert "done" not in queued_events


def test_gateway_late_cancel_preserves_completed_webui_turn(tmp_path, monkeypatch):
    stream_id = "gateway-webui-late-cancel-stream"
    session_id = "gateway_webui_late_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gateway.test"})

    class _GatewayResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            payload = {"choices": [{"delta": {"content": "Gateway reply"}}]}
            return iter([
                ("data: " + json.dumps(payload) + "\n").encode("utf-8"),
                b"data: [DONE]\n",
            ])

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _GatewayResponse(),
    )

    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="hello",
        pending_user_source="webui",
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_save = Session.save
    save_calls = {"completed": 0}

    def _save_and_cancel_after_completed_webui_turn(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        if (
            getattr(self, "session_id", None) == session_id
            and save_calls["completed"] == 0
            and getattr(self, "active_stream_id", None) is None
            and getattr(self, "pending_user_message", None) is None
            and any(
                msg.get("role") == "assistant" and msg.get("content") == "Gateway reply"
                for msg in (getattr(self, "messages", None) or [])
            )
        ):
            save_calls["completed"] += 1
            config.CANCEL_FLAGS[stream_id].set()
        return result

    monkeypatch.setattr(Session, "save", _save_and_cancel_after_completed_webui_turn)

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        "hello",
        "claude-sonnet-test",
        str(tmp_path),
        stream_id,
        model_provider="test-provider",
    )

    assert save_calls["completed"] == 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert [msg.get("content") for msg in saved.messages] == [
        "before",
        "hello",
        "Gateway reply",
    ]
    assert [msg.get("content") for msg in saved.context_messages] == [
        "before",
        "hello",
        "Gateway reply",
    ]
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "cancel" in queued_events
    assert "done" not in queued_events


def test_gateway_late_cancel_preserves_existing_pause_for_webui_recovery(tmp_path, monkeypatch):
    stream_id = "gateway-webui-recovery-late-cancel-stream"
    session_id = "gateway_webui_recovery_late_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gateway.test"})

    class _GatewayResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            payload = {"choices": [{"delta": {"content": "Gateway recovery reply"}}]}
            return iter([
                ("data: " + json.dumps(payload) + "\n").encode("utf-8"),
                b"data: [DONE]\n",
            ])

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _GatewayResponse(),
    )

    previous_pause = {
        "paused": True,
        "model": "claude-sonnet-test",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 2,
        "credential_state_fingerprint": "fingerprint-before",
    }
    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="try recovery",
        pending_user_source="webui",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_save = Session.save
    save_calls = {"completed": 0}

    def _save_and_cancel_after_completed_recovery_turn(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        if (
            getattr(self, "session_id", None) == session_id
            and save_calls["completed"] == 0
            and getattr(self, "active_stream_id", None) is None
            and getattr(self, "pending_user_message", None) is None
            and any(
                msg.get("role") == "assistant" and msg.get("content") == "Gateway recovery reply"
                for msg in (getattr(self, "messages", None) or [])
            )
        ):
            save_calls["completed"] += 1
            config.CANCEL_FLAGS[stream_id].set()
        return result

    monkeypatch.setattr(Session, "save", _save_and_cancel_after_completed_recovery_turn)

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        "try recovery",
        "claude-sonnet-test",
        str(tmp_path),
        stream_id,
        model_provider="test-provider",
    )

    assert save_calls["completed"] == 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == previous_pause
    assert [msg.get("content") for msg in saved.messages] == [
        "before",
        "try recovery",
        "Gateway recovery reply",
    ]
    assert [msg.get("content") for msg in saved.context_messages] == [
        "before",
        "try recovery",
        "Gateway recovery reply",
    ]
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "cancel" in queued_events
    assert "done" not in queued_events


def test_gateway_post_save_cancel_after_success_commit_emits_done(tmp_path, monkeypatch):
    stream_id = "gateway-post-save-success-cancel-stream"
    session_id = "gateway_post_save_success_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {"webui_gateway_base_url": "http://gateway.test"})

    class _GatewayResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            payload = {"choices": [{"delta": {"content": "Gateway success reply"}}]}
            return iter([
                ("data: " + json.dumps(payload) + "\n").encode("utf-8"),
                b"data: [DONE]\n",
            ])

    monkeypatch.setattr(
        gateway_chat.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _GatewayResponse(),
    )

    previous_pause = {
        "paused": True,
        "model": "claude-sonnet-test",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 2,
        "credential_state_fingerprint": "fingerprint-before",
    }
    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="claude-sonnet-test",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="try recovery",
        pending_user_source="webui",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_payload = streaming._session_payload_with_full_messages
    payload_calls = {"count": 0}

    def _payload_and_cancel_after_success_commit(*args, **kwargs):
        payload_calls["count"] += 1
        config.CANCEL_FLAGS[stream_id].set()
        return original_payload(*args, **kwargs)

    monkeypatch.setattr(streaming, "_session_payload_with_full_messages", _payload_and_cancel_after_success_commit)

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        "try recovery",
        "claude-sonnet-test",
        str(tmp_path),
        stream_id,
        model_provider="test-provider",
    )

    assert payload_calls["count"] >= 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "done" in queued_events
    assert "stream_end" in queued_events
    assert "cancel" not in queued_events


def test_streaming_late_cancel_after_pause_clear_save_persists_restored_pause(tmp_path, monkeypatch):
    stream_id = "streaming-pause-clear-save-cancel"
    session_id = "streaming_pause_clear_save_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue

    previous_pause = {
        "paused": True,
        "model": "test-model",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 3,
        "credential_state_fingerprint": "fingerprint-before",
    }
    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="wake up",
        pending_user_source="process_wakeup",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_save = Session.save
    save_calls = {"cleared_pause": 0, "restored_pause": 0}

    def _save_and_cancel_after_pause_clear(self, *args, **kwargs):
        result = original_save(self, *args, **kwargs)
        if getattr(self, "session_id", None) != session_id:
            return result
        pause = dict(getattr(self, "process_wakeup_pause", {}) or {})
        if not pause and save_calls["cleared_pause"] == 0:
            save_calls["cleared_pause"] += 1
            config.CANCEL_FLAGS[stream_id].set()
        elif pause == previous_pause and config.CANCEL_FLAGS[stream_id].is_set():
            save_calls["restored_pause"] += 1
        return result

    monkeypatch.setattr(Session, "save", _save_and_cancel_after_pause_clear)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    assert save_calls["cleared_pause"] == 1
    assert save_calls["restored_pause"] >= 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == previous_pause
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "cancel" in queued_events
    assert "done" not in queued_events


def test_streaming_post_save_cancel_after_success_commit_emits_done(tmp_path, monkeypatch):
    stream_id = "streaming-pause-clear-post-save-cancel"
    session_id = "streaming_pause_clear_post_save_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue

    previous_pause = {
        "paused": True,
        "model": "test-model",
        "provider": "test-provider",
        "classification": "credential_pool_empty",
        "first_paused_at": 1.0,
        "last_visible_error_at": 1.0,
        "visible_error_count": 1,
        "suppressed_count": 3,
        "credential_state_fingerprint": "fingerprint-before",
    }
    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="wake up",
        pending_user_source="process_wakeup",
        process_wakeup_pause=dict(previous_pause),
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_payload = streaming._session_payload_with_full_messages
    payload_calls = {"count": 0}

    def _payload_and_cancel_after_success_commit(*args, **kwargs):
        payload_calls["count"] += 1
        config.CANCEL_FLAGS[stream_id].set()
        return original_payload(*args, **kwargs)

    monkeypatch.setattr(streaming, "_session_payload_with_full_messages", _payload_and_cancel_after_success_commit)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    assert payload_calls["count"] >= 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "done" in queued_events
    assert "cancel" not in queued_events


def test_streaming_no_pause_post_save_cancel_after_success_commit_emits_done(tmp_path, monkeypatch):
    stream_id = "streaming-no-pause-post-save-cancel"
    session_id = "streaming_no_pause_post_save_cancel"
    stream_queue = queue.Queue()
    config.STREAMS[stream_id] = stream_queue

    previous_messages = [{"role": "user", "content": "before", "timestamp": 1.0}]
    session = Session(
        session_id=session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=list(previous_messages),
        context_messages=list(previous_messages),
        active_stream_id=stream_id,
        pending_user_message="hello",
        pending_user_source="webui",
    )
    session.save()
    models.SESSIONS[session_id] = session

    original_payload = streaming._session_payload_with_full_messages
    payload_calls = {"count": 0}

    def _payload_and_cancel_after_success_commit(*args, **kwargs):
        payload_calls["count"] += 1
        config.CANCEL_FLAGS[stream_id].set()
        return original_payload(*args, **kwargs)

    monkeypatch.setattr(streaming, "_session_payload_with_full_messages", _payload_and_cancel_after_success_commit)

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_SuccessfulAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    assert payload_calls["count"] >= 1
    saved = Session.load(session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert [msg.get("content") for msg in saved.messages] == [
        "before",
        "hello",
        "Stream reply",
    ]
    queued_events = [item[0] for item in list(stream_queue.queue)]
    assert "done" in queued_events
    assert "stream_end" in queued_events
    assert "cancel" not in queued_events


def test_stale_credential_empty_process_wakeup_still_records_pause(tmp_path):
    session = Session(
        session_id="wakeup_pause_stale",
        title="Wakeup pause stale",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "Earlier prompt", "timestamp": 1}],
        context_messages=[{"role": "user", "content": "Earlier prompt"}],
        active_stream_id="stream-wakeup-pause-stale",
        pending_user_message="[IMPORTANT: Background process first completed.]",
        pending_started_at=1234.0,
        pending_user_source="process_wakeup",
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    stream_id = str(session.active_stream_id)
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    with mock.patch.object(streaming, "_get_ai_agent", return_value=_StaleCredentialPoolEmptyAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.active_stream_id == "stream-newer-run"
    assert saved.model == "newer-model"
    assert saved.model_provider == "newer-provider"
    assert saved.pending_user_source == "webui"
    assert saved.process_wakeup_pause["paused"] is True
    assert saved.process_wakeup_pause["classification"] == "credential_pool_empty"
    assert saved.process_wakeup_pause["model"] == "test-model"
    assert saved.process_wakeup_pause["provider"] == "test-provider"
    assert saved.process_wakeup_pause["suppressed_count"] == 0
    assert not any(message.get("_error") for message in saved.messages)


def test_process_wakeup_pause_resets_when_model_provider_lane_changes(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_reset",
        workspace=str(tmp_path),
        model="old-model",
        model_provider="old-provider",
        process_wakeup_pause={
            "version": 1,
            "paused": True,
            "source": "process_wakeup",
            "classification": "credential_pool_empty",
            "model": "old-model",
            "provider": "old-provider",
            "first_paused_at": 1.0,
            "last_error_at": 1.0,
            "visible_error_count": 1,
            "suppressed_count": 2,
        },
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": "stream-reset", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("new-model", "new-provider", True),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "[IMPORTANT: Background process completed after provider change.]",
        source="process_wakeup",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-reset"
    assert captured == {"model": "new-model", "model_provider": "new-provider"}
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause == {}


def test_success_path_clears_process_wakeup_pause_after_late_cancel_checks():
    src = Path(__file__).parent.parent.joinpath("api", "streaming.py").read_text(encoding="utf-8")
    session_save_idx = src.index('with _stream_writeback_stage(_writeback_timings, "session_save")')
    session_save_cancel_idx = src.index("if cancel_event.is_set():", session_save_idx)
    state_sync_idx = src.index('with _stream_writeback_stage(_writeback_timings, "state_sync")')
    final_cancel_idx = src.index("if cancel_event.is_set():", state_sync_idx)
    pause_snapshot_idx = src.index("_process_wakeup_pause_before_clear =", final_cancel_idx)
    pause_clear_idx = src.index("clear_process_wakeup_pause(s, reason='run_completed')")
    post_clear_cancel_idx = src.index("if cancel_event.is_set():", pause_clear_idx)
    post_clear_restore_idx = src.index(
        "s.process_wakeup_pause = dict(_process_wakeup_pause_before_clear)",
        post_clear_cancel_idx,
    )
    pause_save_idx = src.index('"process_wakeup_pause_clear_save"', post_clear_cancel_idx)
    post_save_cancel_idx = src.index("if cancel_event.is_set():", pause_save_idx)
    post_save_restore_idx = src.index(
        "s.process_wakeup_pause = dict(_process_wakeup_pause_before_clear)",
        post_save_cancel_idx,
    )
    done_payload_idx = src.index('with _stream_writeback_stage(_writeback_timings, "done_payload")')

    assert session_save_idx < session_save_cancel_idx < state_sync_idx
    assert state_sync_idx < final_cancel_idx < pause_snapshot_idx < pause_clear_idx
    assert pause_clear_idx < post_clear_cancel_idx < post_clear_restore_idx < pause_save_idx
    assert pause_save_idx < post_save_cancel_idx < post_save_restore_idx < done_payload_idx


def test_gateway_success_path_checks_cancel_before_clearing_process_wakeup_pause():
    src = Path(__file__).parent.parent.joinpath("api", "gateway_chat.py").read_text(encoding="utf-8")
    current_idx = src.index("if not _stream_writeback_is_current(s, stream_id):")
    early_cancel_idx = src.index("if cancel_event.is_set():", current_idx)
    pending_clear_idx = src.index("s.pending_user_source = None")
    pre_clear_comment_idx = src.index("# Recheck immediately before clearing", pending_clear_idx)
    final_cancel_idx = src.index("if cancel_event.is_set():", pre_clear_comment_idx)
    pause_clear_idx = src.index(
        'clear_process_wakeup_pause(s, reason="run_completed")',
        final_cancel_idx,
    )
    save_idx = src.index("s.save()", pause_clear_idx)

    assert current_idx < early_cancel_idx < pending_clear_idx < final_cancel_idx < pause_clear_idx < save_idx


def test_process_wakeup_pause_does_not_suppress_explicit_non_wakeup_turn(tmp_path, monkeypatch):
    session = Session(
        session_id="wakeup_pause_manual_recover",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        process_wakeup_pause={
            "version": 1,
            "paused": True,
            "source": "process_wakeup",
            "classification": "credential_pool_empty",
            "model": "test-model",
            "provider": "test-provider",
            "first_paused_at": 1.0,
            "last_error_at": 1.0,
            "visible_error_count": 1,
            "suppressed_count": 2,
        },
    )
    session.save()
    models.SESSIONS[session.session_id] = session

    captured = {}

    def _fake_start_run(s, **kwargs):
        captured["source"] = kwargs.get("source")
        captured["message"] = kwargs.get("msg")
        return {"stream_id": "stream-manual-recover", "session_id": s.session_id, "_status": 200}

    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    response = routes.start_session_turn(
        session.session_id,
        "Explicit recovery attempt",
        source="manual_recover",
    )

    assert response["_status"] == 200
    assert response["stream_id"] == "stream-manual-recover"
    assert captured == {"source": "manual_recover", "message": "Explicit recovery attempt"}
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.process_wakeup_pause["suppressed_count"] == 2
    assert "last_suppressed_at" not in saved.process_wakeup_pause
