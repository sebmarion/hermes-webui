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


def test_session_window_route_is_404_when_server_gate_is_explicitly_disabled(
    monkeypatch,
):
    routes, captured = _route_capture(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_LAZY_TAIL_V1", "0")

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
    monkeypatch.delenv("HERMES_WEBUI_LAZY_TAIL_V1", raising=False)
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


def test_lazy_tail_metadata_preserves_non_transcript_session_state(monkeypatch):
    import api.routes as routes

    session = SimpleNamespace(
        profile="default",
        pending_attachments=[{"name": "queued.txt"}],
        pending_started_at=123.0,
        pending_user_source="webui",
        compact=lambda: {
            "session_id": "canonical",
            "read_only": True,
            "model_provider": "openai",
            "profile": "default",
            "enabled_toolsets": ["web"],
            "composer_draft": {"text": "draft"},
            "project_id": "project-1",
            "worktree_path": "/workspace/task",
            "messages": [{"role": "user", "content": "must not leak"}],
            "message_count": 999,
        },
    )
    resolution = SimpleNamespace(
        canonical_id="canonical",
        canonical_row={},
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda sid, metadata_only=False: (
            session
            if sid == "canonical" and metadata_only is True
            else None
        ),
    )
    monkeypatch.setattr(
        routes,
        "_session_requires_cli_metadata_lookup",
        lambda _session: False,
    )
    monkeypatch.setattr(
        routes,
        "_apply_resolution_metadata_to_payload",
        lambda raw, _resolution: raw,
    )
    monkeypatch.setattr(
        routes,
        "_is_subagent_child_session_id",
        lambda _sid: False,
    )

    metadata = routes._lazy_tail_session_metadata("default", resolution)

    assert metadata["session_id"] == "canonical"
    assert metadata["read_only"] is True
    assert metadata["model_provider"] == "openai"
    assert metadata["profile"] == "default"
    assert metadata["enabled_toolsets"] == ["web"]
    assert metadata["composer_draft"] == {"text": "draft"}
    assert metadata["project_id"] == "project-1"
    assert metadata["worktree_path"] == "/workspace/task"
    assert metadata["pending_attachments"] == [{"name": "queued.txt"}]
    assert "messages" not in metadata
    assert "message_count" not in metadata


def test_lazy_tail_metadata_uses_targeted_state_metadata_without_sidecar(
    monkeypatch,
):
    import api.routes as routes

    resolution = SimpleNamespace(
        canonical_id="canonical",
        canonical_row={"source": "webui"},
    )
    targeted = {
        "session_id": "canonical",
        "title": "State-only task",
        "workspace": "/workspace",
        "model": "model",
        "model_provider": None,
        "profile": "default",
        "source": "webui",
        "source_tag": "webui",
        "is_cli_session": False,
        "messages": [{"role": "user", "content": "must not leak"}],
        "message_count": 504,
    }
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda _sid, metadata_only=False: None,
    )
    monkeypatch.setattr(
        routes,
        "_targeted_cli_metadata_for_resolution",
        lambda _resolution: dict(targeted),
    )
    monkeypatch.setattr(
        routes,
        "_apply_resolution_metadata_to_payload",
        lambda raw, _resolution: raw,
    )
    monkeypatch.setattr(
        routes,
        "_is_subagent_child_session_id",
        lambda _sid: False,
    )

    metadata = routes._lazy_tail_session_metadata("default", resolution)

    assert metadata["session_id"] == "canonical"
    assert metadata["title"] == "State-only task"
    assert metadata["workspace"] == "/workspace"
    assert metadata["read_only"] is False
    assert metadata["is_cli_session"] is False
    assert metadata["pending_attachments"] == []
    assert "messages" not in metadata
    assert "message_count" not in metadata


def test_lazy_tail_metadata_fails_closed_without_read_only(monkeypatch):
    import api.routes as routes

    session = SimpleNamespace(
        profile="default",
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        compact=lambda: {
            "session_id": "canonical",
            "model_provider": "openai",
        },
    )
    resolution = SimpleNamespace(
        canonical_id="canonical",
        canonical_row={},
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda _sid, metadata_only=False: session if metadata_only else None,
    )
    monkeypatch.setattr(
        routes,
        "_session_requires_cli_metadata_lookup",
        lambda _session: False,
    )
    monkeypatch.setattr(
        routes,
        "_apply_resolution_metadata_to_payload",
        lambda raw, _resolution: raw,
    )
    monkeypatch.setattr(
        routes,
        "_is_subagent_child_session_id",
        lambda _sid: False,
    )

    assert routes._lazy_tail_session_metadata("default", resolution) is None


def test_lazy_tail_metadata_never_falls_back_to_full_session_load(
    tmp_path,
    monkeypatch,
):
    import json
    import api.routes as routes

    sid = "strict-prefix"
    sidecar = {
        "session_id": sid,
        "title": "Strict prefix",
        "workspace": "/workspace",
        "model": "model",
        "model_provider": "provider",
        "created_at": 1.0,
        "updated_at": 2.0,
        "profile": "default",
        "read_only": False,
        "messages": [
            {"role": "user", "content": "large transcript must not parse"}
        ],
        "tool_calls": [],
    }
    (tmp_path / f"{sid}.json").write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(
        routes.Session,
        "load",
        classmethod(
            lambda _cls, _sid: (_ for _ in ()).throw(
                AssertionError("strict lazy metadata must never full-load")
            )
        ),
    )
    monkeypatch.setattr(
        routes,
        "_session_requires_cli_metadata_lookup",
        lambda _session: False,
    )
    monkeypatch.setattr(
        routes,
        "_apply_resolution_metadata_to_payload",
        lambda raw, _resolution: raw,
    )
    monkeypatch.setattr(
        routes,
        "_is_subagent_child_session_id",
        lambda _sid: False,
    )

    metadata = routes._lazy_tail_session_metadata(
        "default",
        SimpleNamespace(canonical_id=sid, canonical_row={}),
    )

    assert metadata["session_id"] == sid
    assert metadata["read_only"] is False
    assert metadata["model_provider"] == "provider"
    assert "messages" not in metadata


def test_app_shell_injects_independent_literal_browser_gate(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    monkeypatch.delenv("HERMES_WEBUI_LAZY_TAIL_BROWSER_V1", raising=False)
    default_on = routes._render_index_shell_base()
    assert "lazyTailV1:true" in default_on
    assert "__LAZY_TAIL_BROWSER_V1__" not in default_on

    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})
    monkeypatch.setenv("HERMES_WEBUI_LAZY_TAIL_BROWSER_V1", "0")
    disabled = routes._render_index_shell_base()
    assert "lazyTailV1:false" in disabled


def test_legacy_compat_builder_accepts_only_a_ready_bounded_window(
    tmp_path, monkeypatch
):
    import api.routes as routes
    import api.session_window as session_window
    from tests.test_release_lite_session_window import _ready_dependencies

    resolution = SimpleNamespace(requested_id="root", canonical_id="tip")
    payload = {
        "requested_session_id": "root",
        "canonical_session_id": "tip",
        "messages": [{"role": "assistant", "content": "tail"}],
        "session_metadata": {
            "session_id": "tip",
            "read_only": False,
            "model_provider": None,
        },
        "conversation_window": {
            "schema": "lazy_tail_v1",
            "state": "ready",
        },
    }
    request_seen = []
    monkeypatch.setattr(
        session_window,
        "default_session_window_dependencies",
        lambda **_kwargs: _ready_dependencies(),
    )
    monkeypatch.setattr(
        session_window,
        "build_session_window",
        lambda request, **_kwargs: request_seen.append(request) or payload,
    )

    result = routes._build_legacy_compat_session_window(
        handler=SimpleNamespace(),
        resolution=resolution,
        profile="default",
        db_path=tmp_path / "state.db",
        visible_limit=30,
    )

    assert result is payload
    assert request_seen[0].session_id == "root"
    assert request_seen[0].visible_limit == 30
    assert request_seen[0].older_cursor is None


def test_legacy_compat_builder_retries_tool_heavy_50_row_overflow_at_30(
    tmp_path, monkeypatch
):
    import api.routes as routes
    import api.session_window as session_window
    from tests.test_release_lite_session_window import _ready_dependencies

    resolution = SimpleNamespace(requested_id="root", canonical_id="tip")
    fallback = {
        "requested_session_id": "root",
        "canonical_session_id": "tip",
        "messages": [],
        "session_metadata": None,
        "conversation_window": {
            "schema": "lazy_tail_v1",
            "state": "legacy_required",
            "status_reason": "visible_limit_exceeded",
        },
    }
    ready = {
        "requested_session_id": "root",
        "canonical_session_id": "tip",
        "messages": [{"role": "assistant", "content": "tail"}],
        "session_metadata": {
            "session_id": "tip",
            "read_only": False,
            "model_provider": None,
        },
        "conversation_window": {
            "schema": "lazy_tail_v1",
            "state": "ready",
        },
    }
    request_seen = []
    responses = iter((fallback, ready))
    monkeypatch.setattr(
        session_window,
        "default_session_window_dependencies",
        lambda **_kwargs: _ready_dependencies(),
    )
    monkeypatch.setattr(
        session_window,
        "build_session_window",
        lambda request, **_kwargs: request_seen.append(request) or next(responses),
    )

    result = routes._build_legacy_compat_session_window(
        handler=SimpleNamespace(),
        resolution=resolution,
        profile="default",
        db_path=tmp_path / "state.db",
        visible_limit=50,
    )

    assert result is ready
    assert [request.visible_limit for request in request_seen] == [50, 30]
