from __future__ import annotations

import sys
import types

import pytest


class _Resolved:
    def __init__(self, resolved, status="no_plan"):
        self.resolved = resolved
        self.status = status

    def to_agent_result(self, *, conversation_history, user_message, host_agent=None):
        response = "waiting" if self.status == "waiting" else self.status
        return {
            "final_response": response,
            "messages": [
                *conversation_history,
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response},
            ],
            "api_calls": 0,
            "completed": True,
        }


def _install_bestplan_module(monkeypatch, *, resolved):
    calls = {"resolve": [], "capture": [], "stores": []}
    module = types.ModuleType("agent.bestplan_state")

    class BestplanStore:
        def __init__(self, db_path, *, reconcile_push_state=True):
            self.db_path = db_path
            self.reconcile_push_state = reconcile_push_state
            self.closed = False
            calls["stores"].append(self)

        def close(self):
            self.closed = True

    def try_resolve_go(message, **kwargs):
        calls["resolve"].append((message, kwargs))
        return resolved

    def capture_bestplan_agent_result(result, **kwargs):
        calls["capture"].append((result, kwargs))
        return {**result, "captured": True}

    module.BestplanStore = BestplanStore
    module.BESTPLAN_HOST_CAPABILITY_VERSION = 2
    module.try_resolve_go = try_resolve_go
    module.capture_bestplan_agent_result = capture_bestplan_agent_result
    module.is_bestplan_invocation = lambda message: "bestplan" in str(message).lower()
    module.is_go_enabled = lambda config=None: True
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)
    return calls


def _install_local_push_module(monkeypatch, *, resolved=None, error=None):
    calls = []
    module = types.ModuleType("agent.bestplan_local_push")

    def try_resolve_local_push(message, **kwargs):
        calls.append((message, kwargs))
        if error is not None:
            raise error
        return resolved

    module.try_resolve_local_push = try_resolve_local_push
    monkeypatch.setitem(sys.modules, "agent.bestplan_local_push", module)
    return calls


def test_accepted_go_returns_host_waiting_without_model_loop(tmp_path, monkeypatch):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(True, "waiting"))

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("accepted go must not enter the model loop")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(),
        original_message="go",
        invocation_message="go",
        conversation_history=[{"role": "assistant", "content": "plan"}],
        run_conversation_kwargs={"user_message": "go"},
        session_id="session-a",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": True}},
    )

    assert result["final_response"] == "waiting"
    assert result["messages"][-2:][0]["role"] == "user"
    assert result["messages"][-1]["role"] == "assistant"
    assert len(calls["resolve"]) == 1
    kwargs = calls["resolve"][0][1]
    assert kwargs["profile"] == "coder"
    assert kwargs["workspace"] == "/tmp/work"
    assert kwargs["parent_agent"].__class__ is Agent
    assert kwargs["store"].db_path == tmp_path / "state.db"
    assert kwargs["store"].reconcile_push_state is False
    assert kwargs["store"].closed is True


def test_host_result_internal_type_error_is_not_retried(tmp_path, monkeypatch):
    import api.streaming as streaming

    class InternalTypeErrorResolved:
        resolved = True

        def __init__(self):
            self.calls = 0

        def to_agent_result(
            self, *, conversation_history, user_message, host_agent=None,
        ):
            self.calls += 1
            raise TypeError("internal host result failure")

    resolved = InternalTypeErrorResolved()
    _install_bestplan_module(monkeypatch, resolved=resolved)

    with pytest.raises(TypeError, match="internal host result failure"):
        streaming._try_resolve_bestplan_go_ingress(
            object(), original_message="go", conversation_history=[],
            session_id="session-a", profile="coder", workspace="/tmp/work",
            profile_home=str(tmp_path), config={},
        )

    assert resolved.calls == 1


def test_legacy_host_result_without_host_agent_is_called_once(
    tmp_path, monkeypatch,
):
    import api.streaming as streaming

    class LegacyResolved:
        resolved = True

        def __init__(self):
            self.calls = 0

        def to_agent_result(self, *, conversation_history, user_message):
            self.calls += 1
            return {
                "final_response": "legacy",
                "messages": [
                    *conversation_history,
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": "legacy"},
                ],
            }

    resolved = LegacyResolved()
    _install_bestplan_module(monkeypatch, resolved=resolved)

    result = streaming._try_resolve_bestplan_go_ingress(
        object(), original_message="go", conversation_history=[],
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={},
    )

    assert result["final_response"] == "legacy"
    assert resolved.calls == 1


def test_no_plan_runs_model_then_captures_bestplan_envelope(tmp_path, monkeypatch):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(False))

    class Agent:
        def __init__(self):
            self.calls = []

        def run_conversation(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "final_response": "plan envelope",
                "messages": [{"role": "assistant", "content": "plan envelope"}],
            }

    agent = Agent()
    result = streaming._run_agent_with_bestplan_ingress(
        agent,
        original_message="/bestplan fix it",
        invocation_message="[IMPORTANT: The user has invoked the bestplan skill]",
        conversation_history=[],
        run_conversation_kwargs={"user_message": "expanded skill"},
        session_id="session-a",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": True}},
    )

    assert len(agent.calls) == 1
    assert result["captured"] is True
    assert len(calls["capture"]) == 1
    assert calls["capture"][0][1]["invocation_message"].startswith("[IMPORTANT:")
    assert calls["capture"][0][1]["provisional"] is True


def test_capture_internal_type_error_is_not_retried(tmp_path, monkeypatch):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    module = sys.modules["agent.bestplan_state"]

    def capture_bestplan_agent_result(result, *, host_agent=None, **_kwargs):
        calls["capture"].append((result, host_agent))
        raise TypeError("internal capture failure")

    module.capture_bestplan_agent_result = capture_bestplan_agent_result

    with pytest.raises(TypeError, match="internal capture failure"):
        streaming._capture_bestplan_result(
            {"final_response": "plan", "messages": []},
            invocation_message="/bestplan fix it",
            session_id="session-a",
            profile="coder",
            workspace="/tmp/work",
            profile_home=str(tmp_path),
            host_agent=object(),
            config={},
            local_execution=True,
        )

    assert len(calls["capture"]) == 1
    assert calls["stores"][0].closed is True


def test_legacy_capture_without_host_agent_is_called_once(tmp_path, monkeypatch):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    module = sys.modules["agent.bestplan_state"]

    def capture_bestplan_agent_result(
        result,
        *,
        invocation_message,
        session_id,
        profile,
        workspace,
        store,
        provisional,
        config,
        local_execution,
    ):
        calls["capture"].append((result, invocation_message))
        return {**result, "captured": True}

    module.capture_bestplan_agent_result = capture_bestplan_agent_result

    result = streaming._capture_bestplan_result(
        {"final_response": "plan", "messages": []},
        invocation_message="/bestplan fix it",
        session_id="session-a",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        host_agent=object(),
        config={},
        local_execution=True,
    )

    assert result["captured"] is True
    assert len(calls["capture"]) == 1
    assert calls["stores"][0].closed is True


def test_host_created_capture_uses_local_execution_and_current_config(
    tmp_path, monkeypatch,
):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    monkeypatch.setattr(
        streaming,
        "_bestplan_result_is_successful_for_capture",
        lambda *_args, **_kwargs: True,
    )
    current_config = {"autonomy": {"go_enabled": False}, "marker": "current"}
    session = types.SimpleNamespace(
        session_id="session-host",
        profile="coder",
        workspace="/tmp/work",
        active_stream_id="stream-a",
    )
    cancel_event = types.SimpleNamespace(is_set=lambda: False)

    captured, accepted, _plan_id = (
        streaming._capture_bestplan_result_after_writeback_fence(
            {
                "final_response": "plan envelope",
                "messages": [{"role": "assistant", "content": "plan envelope"}],
            },
            bestplan_config={"count": 3},
            config=current_config,
            ephemeral=False,
            session=session,
            stream_id="stream-a",
            cancel_event=cancel_event,
            host_agent=object(),
            captured_terminal_error=None,
            previous_messages=[],
            previous_context_messages=[],
            message="fix it",
            source="webui",
            active_turn_identity=None,
            profile_home=str(tmp_path),
            provisional_plan_ids=[],
        )
    )

    assert accepted is True
    assert captured["captured"] is True
    kwargs = calls["capture"][0][1]
    assert kwargs["local_execution"] is True
    assert kwargs["config"] is current_config
    assert kwargs["store"].db_path == tmp_path / "state.db"
    assert kwargs["store"].closed is True


def test_stale_client_task_recovers_bestplan_identity_for_host_capture(
    tmp_path, monkeypatch,
):
    import api.streaming as streaming

    calls = _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    invocation = streaming._bestplan_capture_invocation_message(
        "fix the envelope leak",
        {"count": 4},
    )
    result = streaming._capture_bestplan_result(
        {
            "final_response": "receipt plus plan envelope",
            "messages": [
                {"role": "assistant", "content": "receipt plus plan envelope"}
            ],
        },
        invocation_message=invocation,
        session_id="session-stale",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
    )

    assert invocation == "/bestplan 4 fix the envelope leak"
    assert result["captured"] is True
    assert calls["capture"][0][1]["invocation_message"] == invocation
    assert calls["capture"][0][1]["provisional"] is True
    assert calls["capture"][0][1].get("local_execution") is not True


@pytest.mark.parametrize(
    "config",
    [{}, {"autonomy": {"go_enabled": False}}, {"autonomy": {"go_enabled": True}}],
)
def test_missing_core_bare_go_always_fails_closed(tmp_path, monkeypatch, config):
    import api.streaming as streaming

    monkeypatch.setitem(sys.modules, "agent.bestplan_state", None)

    class Agent:
        def __init__(self):
            self.calls = 0

        def run_conversation(self, **_kwargs):
            self.calls += 1
            return {"final_response": "ordinary", "messages": []}

    agent = Agent()
    common = {
        "agent": agent,
        "invocation_message": "go",
        "conversation_history": [],
        "run_conversation_kwargs": {"user_message": "go"},
        "session_id": "session-a",
        "profile": "coder",
        "workspace": "/tmp/work",
        "profile_home": str(tmp_path),
    }
    result = streaming._run_agent_with_bestplan_ingress(
        original_message="go", config=config, **common
    )
    assert result["host_ingress"]["status"] == "resolver_unavailable"
    assert agent.calls == 0


@pytest.mark.parametrize("token", ["go", "push", "no"])
def test_recognized_control_core_api_error_fails_closed(
    tmp_path, monkeypatch, token,
):
    import api.streaming as streaming

    monkeypatch.setitem(
        sys.modules, "agent.bestplan_state", types.ModuleType("agent.bestplan_state")
    )

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("recognized control API errors must not hit the model")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(), original_message=token, invocation_message=token,
        conversation_history=[], run_conversation_kwargs={"user_message": token},
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={},
    )

    assert result["host_ingress"]["status"] == "resolver_error"


@pytest.mark.parametrize("token", ["push", "no"])
def test_bare_push_reply_resolves_before_go_without_model(
    tmp_path, monkeypatch, token,
):
    import api.streaming as streaming

    go_calls = _install_bestplan_module(
        monkeypatch, resolved=_Resolved(False, "not_a_trigger")
    )
    push_calls = _install_local_push_module(
        monkeypatch, resolved=_Resolved(True, "push_declined")
    )

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("resolved push/no must not enter the model loop")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(),
        original_message=token,
        invocation_message=token,
        conversation_history=[{"role": "assistant", "content": "push?"}],
        run_conversation_kwargs={"user_message": token},
        session_id="session-push",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": False}},
    )

    assert result["final_response"] == "push_declined"
    assert len(push_calls) == 1
    assert go_calls["resolve"] == []
    kwargs = push_calls[0][1]
    assert kwargs["session_id"] == "session-push"
    assert kwargs["profile"] == "coder"
    assert kwargs["workspace"] == "/tmp/work"
    assert kwargs["store"].db_path == tmp_path / "state.db"
    assert kwargs["store"].reconcile_push_state is False
    assert kwargs["store"].closed is True


@pytest.mark.parametrize("token", ["push", "no"])
def test_unmatched_bare_push_reply_is_consumed_without_model(
    tmp_path, monkeypatch, token,
):
    import api.streaming as streaming

    _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    push_calls = _install_local_push_module(
        monkeypatch, resolved=_Resolved(False, "no_push_prompt")
    )

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("bare push/no must never reach the model")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(), original_message=token, invocation_message=token,
        conversation_history=[], run_conversation_kwargs={"user_message": token},
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={},
    )

    assert result["host_ingress"]["status"] == "no_push_prompt"
    assert "No remote write was started" in result["final_response"]
    assert push_calls[0][1]["store"].reconcile_push_state is False
    assert push_calls[0][1]["store"].closed is True


def test_resolved_control_precedes_unrelated_completion_drain_and_ack(
    tmp_path, monkeypatch,
):
    import queue
    from collections import OrderedDict

    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", lambda _profile: tmp_path
    )
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})

    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    streaming.STREAMS.clear()
    streaming.CANCEL_FLAGS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.STREAM_PARTIAL_TEXT.clear()
    streaming.STREAM_REASONING_TEXT.clear()
    streaming.STREAM_LIVE_TOOL_CALLS.clear()

    class FakeAgent:
        model_calls = 0

        def __init__(self, **_kwargs):
            self.session_id = "session-control-order"
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **_kwargs):
            type(self).model_calls += 1
            raise AssertionError("resolved host control must not enter the model")

        def interrupt(self, _message):
            return None

    _install_bestplan_module(monkeypatch, resolved=_Resolved(True, "waiting"))
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", None, None),
    )
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(
        config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: []
    )

    pending_event = {"type": "async_delegation", "task_id": "unrelated-task"}
    drain_calls = []
    ack_calls = []

    def drain_notifications(*_args, **_kwargs):
        drain_calls.append(pending_event)
        return ["unrelated completion"]

    def accept_notifications(*_args, **_kwargs):
        ack_calls.append(pending_event)
        return []

    monkeypatch.setattr(
        streaming, "_drain_webui_process_notifications", drain_notifications
    )
    monkeypatch.setattr(
        streaming, "_accept_pending_async_delegations", accept_notifications
    )

    session = models.Session(
        session_id="session-control-order",
        title="BestPlan control order",
        workspace=str(tmp_path),
        model="test-model",
        messages=[{"role": "assistant", "content": "Plan is ready."}],
        context_messages=[{"role": "assistant", "content": "Plan is ready."}],
    )
    stream_id = "stream-control-order"
    session.active_stream_id = stream_id
    session.pending_user_message = "go"
    session.pending_started_at = 10.0
    session.save(touch_updated_at=False)
    models.SESSIONS[session.session_id] = session
    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text="go",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    assert drain_calls == []
    assert ack_calls == []
    assert FakeAgent.model_calls == 0


@pytest.mark.parametrize(
    "message",
    [
        " go", "go ", "GO", "go\n", "go\npush", "please go",
        " push", "push ", "PUSH", "push\n", "please push",
        " no", "no ", "NO", " NO ", "no\n", "please no",
    ],
)
def test_non_exact_control_turn_never_instantiates_bestplan_store(
    tmp_path, monkeypatch, message,
):
    import api.streaming as streaming

    module = types.ModuleType("agent.bestplan_state")

    class BestplanStore:
        def __init__(self, **_kwargs):
            raise AssertionError("ordinary turns must not open/DDL bestplan state")

    module.BestplanStore = BestplanStore
    module.is_bestplan_invocation = lambda message: str(message).startswith("/bestplan")
    module.capture_bestplan_agent_result = lambda result, **_kwargs: result
    module.try_resolve_go = lambda *_args, **_kwargs: _Resolved(False)
    module.is_go_enabled = lambda _config=None: True
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)
    _install_local_push_module(
        monkeypatch,
        error=AssertionError("non-control input must not call the push resolver"),
    )

    class Agent:
        def run_conversation(self, **_kwargs):
            return {"final_response": "ordinary", "messages": []}

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(),
        original_message=message,
        invocation_message=message,
        conversation_history=[],
        run_conversation_kwargs={"user_message": message},
        session_id="session-a",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": True}},
    )
    assert result["final_response"] == "ordinary"


@pytest.mark.parametrize("config", [{}, {"autonomy": {"go_enabled": False}}])
def test_bare_go_reaches_agent_policy_when_webui_flag_is_disabled_or_absent(
    tmp_path, monkeypatch, config,
):
    import api.streaming as streaming

    calls = []
    module = types.ModuleType("agent.bestplan_state")

    class BestplanStore:
        def __init__(self, db_path, *, reconcile_push_state=True):
            self.db_path = db_path
            self.reconcile_push_state = reconcile_push_state

        def close(self):
            return None

    def try_resolve_go(message, **kwargs):
        calls.append((message, kwargs))
        return _Resolved(False, "disabled")

    module.BESTPLAN_HOST_CAPABILITY_VERSION = 2
    module.BestplanStore = BestplanStore
    module.is_go_enabled = lambda config=None: False
    module.try_resolve_go = try_resolve_go
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)

    assert streaming._try_resolve_bestplan_go_ingress(
        object(), original_message="go", conversation_history=[],
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config=config,
    ) is None
    assert len(calls) == 1
    assert calls[0][1]["config"] is config
    assert calls[0][1]["store"].db_path == tmp_path / "state.db"
    assert calls[0][1]["store"].reconcile_push_state is False


def test_go_resolver_error_fails_closed_when_webui_flag_is_false(
    tmp_path, monkeypatch,
):
    import api.streaming as streaming

    module = types.ModuleType("agent.bestplan_state")
    module.BESTPLAN_HOST_CAPABILITY_VERSION = 2
    module.BestplanStore = lambda db_path, **kwargs: types.SimpleNamespace(
        db_path=db_path, close=lambda: None, **kwargs,
    )
    module.is_go_enabled = lambda _config=None: False
    module.try_resolve_go = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("broken go resolver")
    )
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("recognized go resolver errors must not hit the model")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(), original_message="go", invocation_message="go",
        conversation_history=[], run_conversation_kwargs={"user_message": "go"},
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={"autonomy": {"go_enabled": False}},
    )

    assert result["host_ingress"]["status"] == "resolver_error"


@pytest.mark.parametrize("token", ["push", "no"])
def test_push_resolver_error_fails_closed_without_model(
    tmp_path, monkeypatch, token,
):
    import api.streaming as streaming

    _install_bestplan_module(monkeypatch, resolved=_Resolved(False))
    _install_local_push_module(
        monkeypatch, error=RuntimeError("broken push resolver")
    )

    class Agent:
        def run_conversation(self, **_kwargs):
            raise AssertionError("recognized push resolver errors must not hit the model")

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(), original_message=token, invocation_message=token,
        conversation_history=[], run_conversation_kwargs={"user_message": token},
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={},
    )

    assert result["host_ingress"]["status"] == "push_stale"


def test_legacy_session_key_binding_survives_old_hermes_helper(monkeypatch):
    import contextvars
    import api.streaming as streaming

    module = types.ModuleType("gateway.session_context")
    module._SESSION_KEY = contextvars.ContextVar("legacy_session_key", default="")
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)

    tokens = streaming._set_turn_session_identity("session-a")
    assert module._SESSION_KEY.get() == "session-a"
    streaming._reset_turn_session_identity(tokens)
    assert module._SESSION_KEY.get() == ""


def test_immediately_previous_bind_signature_negotiates_without_breaking_routing(monkeypatch):
    import api.streaming as streaming

    calls = []
    module = types.ModuleType("gateway.session_context")

    def bind_delivery_context(*, session_key, session_id, ui_session_id="", async_delivery):
        calls.append((session_key, session_id, ui_session_id, async_delivery))
        return "old-token"

    module.bind_delivery_context = bind_delivery_context
    module.reset_delivery_context = lambda token: calls.append(("reset", token))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)

    tokens = streaming._set_turn_session_identity(
        "session-a", profile="coder", hermes_home="/profiles/coder"
    )
    assert calls == [("session-a", "session-a", "session-a", True)]
    assert tokens["delivery_context"] == "old-token"
    assert tokens["bestplan_capability_version"] == 0
    streaming._reset_turn_session_identity(tokens)
    assert calls[-1] == ("reset", "old-token")


def test_immediately_previous_core_capture_signature_is_planning_only(monkeypatch, tmp_path):
    import api.streaming as streaming

    module = types.ModuleType("agent.bestplan_state")
    module.is_bestplan_invocation = lambda _message: True
    module.capture_bestplan_agent_result = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("old core capture must not persist executable authority")
    )
    module.BestplanStore = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("old core capture must not open state")
    )
    module.BESTPLAN_HOST_CAPABILITY_VERSION = 1
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)
    envelope = (
        "commentary\n<<<HERMES_BESTPLAN_V1>>>\n{}\n"
        "<<<END_HERMES_BESTPLAN_V1>>>"
    )
    result = streaming._capture_bestplan_result(
        {"final_response": envelope, "messages": [{"role": "assistant", "content": envelope}]},
        invocation_message="/bestplan x", session_id="s", profile="coder",
        workspace="/tmp/work", profile_home=str(tmp_path),
    )
    assert result["bestplan_capture"]["executable"] is False
    assert "planning-only" in result["final_response"]
    assert "HERMES_BESTPLAN" not in result["final_response"]


def test_capability_v1_go_cannot_dispatch_pre_hardening_plan(monkeypatch, tmp_path):
    import api.streaming as streaming

    module = types.ModuleType("agent.bestplan_state")
    module.BESTPLAN_HOST_CAPABILITY_VERSION = 1
    module.is_go_enabled = lambda _config=None: True
    module.BestplanStore = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("V1 go must not open legacy pending authority")
    )
    module.try_resolve_go = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("V1 go must not resolve legacy pending authority")
    )
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)

    result = streaming._try_resolve_bestplan_go_ingress(
        object(),
        original_message="go",
        conversation_history=[],
        session_id="s",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": True}},
    )

    assert result["host_ingress"]["status"] == "capability_upgrade_required"
    assert "V2" in result["final_response"]


def test_immediately_previous_recovery_signature_preserves_generic_root_replay(monkeypatch):
    import api.background_process as bp
    import api.profiles as profiles

    calls = []
    module = types.ModuleType("tools.async_delegation")

    def recover_async_delegations():
        calls.append("legacy")
        return {"queued": 1}

    module.recover_async_delegations = recover_async_delegations
    monkeypatch.setitem(sys.modules, "tools.async_delegation", module)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"path": "/profiles/default"}])
    assert bp.recover_profile_async_delegations() == 1
    assert calls == ["legacy"]
