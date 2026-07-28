from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


def _route_capture(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args: False)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (
            captured.update(payload=payload, status=status) or True
        ),
    )
    return routes, captured


def test_session_window_route_is_404_when_server_gate_is_off(monkeypatch):
    routes, captured = _route_capture(monkeypatch)
    monkeypatch.delenv("HERMES_WEBUI_LAZY_TAIL_V1", raising=False)

    routes.handle_get(
        SimpleNamespace(),
        urlparse("/api/session-window?session_id=requested&msg_limit=30"),
    )

    assert captured == {"payload": {"error": "not found"}, "status": 404}


def test_session_window_route_returns_lazy_contract_without_legacy_fallback(
    monkeypatch,
):
    import api.session_window as session_window

    routes, captured = _route_capture(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LAZY_TAIL_V1", "1")
    request_seen = []
    payload = {
        "requested_session_id": "requested",
        "canonical_session_id": "canonical",
        "messages": [],
        "runtime_snapshot": None,
        "conversation_window": {
            "schema": "lazy_tail_v1",
            "state": "ready",
        },
    }
    monkeypatch.setattr(
        session_window,
        "build_session_window",
        lambda request, **kwargs: request_seen.append((request, kwargs)) or payload,
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lazy route must not load a full WebUI session")
        ),
    )
    monkeypatch.setattr(
        routes,
        "read_resolved_session_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lazy route must not merge legacy history")
        ),
    )

    routes.handle_get(
        SimpleNamespace(),
        urlparse(
            "/api/session-window"
            "?session_id=requested&msg_limit=30&resolve_model=0"
        ),
    )

    assert captured == {"payload": payload, "status": 200}
    assert request_seen[0][0].session_id == "requested"
    assert request_seen[0][0].visible_limit == 30


@pytest.mark.parametrize(
    "query",
    (
        "msg_limit=30",
        "session_id=requested&msg_limit=51",
        "session_id=requested&msg_limit=text",
    ),
)
def test_session_window_route_maps_typed_request_errors(query, monkeypatch):
    routes, captured = _route_capture(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LAZY_TAIL_V1", "1")

    routes.handle_get(
        SimpleNamespace(),
        urlparse(f"/api/session-window?{query}"),
    )

    assert captured["status"] == 400
    assert captured["payload"]["code"]
    assert "session" not in captured["payload"]


def test_app_shell_injects_independent_literal_browser_gate(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    monkeypatch.delenv("HERMES_WEBUI_LAZY_TAIL_BROWSER_V1", raising=False)
    off = routes._render_index_shell_base()
    assert "lazyTailV1:false" in off
    assert "__LAZY_TAIL_BROWSER_V1__" not in off

    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    monkeypatch.setenv("HERMES_WEBUI_LAZY_TAIL_BROWSER_V1", "1")
    on = routes._render_index_shell_base()
    assert "lazyTailV1:true" in on
