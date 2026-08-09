import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_bestplan_marker_is_transported_without_browser_model_selection():
    messages = (ROOT / "static/messages.js").read_text()
    routes = (ROOT / "api/routes.py").read_text()
    streaming = (ROOT / "api/streaming.py").read_text()
    assert "_pendingBestplanConfig" in messages
    assert "bestplan_config" in messages
    assert "bestplan_config" in routes
    assert "bestplan_config" in streaming
    assert "BestPlan is unavailable on gateway-backed sessions" in routes


def test_server_bestplan_defaults_to_all_four_configured_lanes():
    from api.routes import _parse_bestplan_chat_message
    from api.streaming import _bestplan_capture_invocation_message

    assert _parse_bestplan_chat_message("/bestplan inspect it") == (
        "inspect it",
        {"count": 4},
    )
    assert _parse_bestplan_chat_message("/bp 2 inspect it") == (
        "inspect it",
        {"count": 2},
    )
    assert _bestplan_capture_invocation_message("inspect it", {}) == (
        "/bestplan 4 inspect it"
    )


def _run_chat_start_with_bestplan(monkeypatch, tmp_path, raw_config):
    from api import routes

    class Handler:
        def __init__(self):
            self.status = None
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    class Session:
        session_id = "bestplan-config-session"
        workspace = str(tmp_path)
        model = "test-model"
        model_provider = "test-provider"
        profile = "default"
        messages = []
        context_messages = []
        pending_user_message = None

    captured = []

    monkeypatch.setattr(
        routes, "_agent_runtime_barrier_response", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda *_args, **_kwargs: Session(),
    )
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_args, **_kwargs: (None, None, {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kwargs: (model, provider, None),
    )
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda *_args, resolved_provider=None, **_kwargs: resolved_provider,
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)

    def start_run(_session, **kwargs):
        captured.append(kwargs)
        return {"stream_id": "bestplan-stream"}

    monkeypatch.setattr(routes, "_start_run", start_run)

    handler = Handler()
    routes._handle_chat_start(
        handler,
        {
            "session_id": Session.session_id,
            "message": "inspect it",
            "workspace": str(tmp_path),
            "bestplan_config": raw_config,
        },
    )
    response = {
        "status": handler.status,
        "payload": json.loads(handler.wfile.getvalue().decode("utf-8")),
    }
    return response, captured


def test_empty_bestplan_config_defaults_to_all_four_lanes(monkeypatch, tmp_path):
    response, captured = _run_chat_start_with_bestplan(monkeypatch, tmp_path, {})

    assert response["status"] == 200
    assert len(captured) == 1
    assert captured[0]["bestplan_config"] == {"count": 4}


@pytest.mark.parametrize("raw_config", [False, []])
def test_invalid_bestplan_config_fails_before_starting_stream(
    monkeypatch, tmp_path, raw_config
):
    response, captured = _run_chat_start_with_bestplan(
        monkeypatch, tmp_path, raw_config
    )

    assert response == {
        "status": 400,
        "payload": {"error": "Invalid BestPlan configuration"},
    }
    assert captured == []


def test_host_owned_bestplan_does_not_fall_through_to_generic_skill_resolution():
    messages = (ROOT / "static/messages.js").read_text()
    intercept_start = messages.index("Slash command intercept")
    intercept_end = messages.index(
        "const activeSid=S.session.session_id",
        intercept_start,
    )
    intercept = messages[intercept_start:intercept_end]

    assert (
        "const _hostOwnedTurnCommand=['moa','bestplan','bp'].includes(_agentCmdName);"
        in intercept
    )
    assert (
        "const _bundleCmd=!_hostOwnedTurnCommand&&!_agentCmd&&"
        in intercept
    )
    assert (
        "if(!_hostOwnedTurnCommand&&!_agentCmd&&!_bundleCmd){"
        in intercept
    )


def test_server_recovers_bestplan_routing_from_stale_browser_tabs():
    from api.routes import _parse_bestplan_chat_message

    assert _parse_bestplan_chat_message("ordinary message") == (
        "ordinary message",
        None,
    )
    assert _parse_bestplan_chat_message("/BESTPLAN 5 inspect it") == (
        "inspect it",
        {"count": 5},
    )


def test_bestplan_stream_withholds_machine_deltas_until_terminal_capture():
    source = (ROOT / "api" / "streaming.py").read_text()
    callback = source[source.index("            def on_token(text):"):]
    callback = callback[:callback.index("\n            def ", 1)]

    gate = callback.index("if bestplan_config is not None:")
    assert gate < callback.index("STREAM_PARTIAL_TEXT[stream_id] +=")
    assert gate < callback.index("put('token'")
