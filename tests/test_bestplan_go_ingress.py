from __future__ import annotations

import sys
import types


class _Resolved:
    def __init__(self, resolved, status="no_plan"):
        self.resolved = resolved
        self.status = status

    def to_agent_result(self, *, conversation_history, user_message):
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
    module.try_resolve_go = try_resolve_go
    module.capture_bestplan_agent_result = capture_bestplan_agent_result
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
