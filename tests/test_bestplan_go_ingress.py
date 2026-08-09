from __future__ import annotations

import sys
import types


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
    calls = {"resolve": [], "capture": []}
    module = types.ModuleType("agent.bestplan_state")

    class BestplanStore:
        def __init__(self, db_path):
            self.db_path = db_path

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


def test_missing_core_is_transparent_disabled_but_go_fails_closed_enabled(tmp_path, monkeypatch):
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
        original_message="go", config={"autonomy": {"go_enabled": False}}, **common
    )
    assert result["final_response"] == "ordinary"
    assert agent.calls == 1

    result = streaming._run_agent_with_bestplan_ingress(
        original_message="go", config={"autonomy": {"go_enabled": True}}, **common
    )
    assert result["host_ingress"]["status"] == "resolver_unavailable"
    assert agent.calls == 1


def test_ordinary_turn_never_instantiates_bestplan_store(tmp_path, monkeypatch):
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

    class Agent:
        def run_conversation(self, **_kwargs):
            return {"final_response": "ordinary", "messages": []}

    result = streaming._run_agent_with_bestplan_ingress(
        Agent(),
        original_message="hello",
        invocation_message="hello",
        conversation_history=[],
        run_conversation_kwargs={"user_message": "hello"},
        session_id="session-a",
        profile="coder",
        workspace="/tmp/work",
        profile_home=str(tmp_path),
        config={"autonomy": {"go_enabled": True}},
    )
    assert result["final_response"] == "ordinary"


def test_disabled_bare_go_never_instantiates_bestplan_store(tmp_path, monkeypatch):
    import api.streaming as streaming

    module = types.ModuleType("agent.bestplan_state")

    class BestplanStore:
        def __init__(self, **_kwargs):
            raise AssertionError("disabled bare go must not open/DDL bestplan state")

    module.BestplanStore = BestplanStore
    module.is_go_enabled = lambda config=None: False
    module.try_resolve_go = lambda *_args, **_kwargs: _Resolved(False)
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", module)

    assert streaming._try_resolve_bestplan_go_ingress(
        object(), original_message="go", conversation_history=[],
        session_id="session-a", profile="coder", workspace="/tmp/work",
        profile_home=str(tmp_path), config={"autonomy": {"go_enabled": False}},
    ) is None


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
