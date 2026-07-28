from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import api.routes as routes


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def test_release_lite_client_event_fields_are_bounded_and_sanitized():
    payload = {
        "event": "lazy_tail_older_page",
        "source": "session-window",
        "session_id": "task-1",
        "stream_id": "run-1",
        "state": "ready",
        "source_mode": "lazy_tail_v1",
        "page_index": 4,
        "visible_count": 50,
        "elapsed_ms": 321,
        "anchor_result": "preserved",
        "reconnect_status": "attached",
        "messages": ["secret transcript"],
        "command": "secret command",
        "arguments": {"secret": True},
        "tool_output": "secret output",
        "path": "/Users/private",
    }

    sanitized = routes._sanitize_client_event_payload(payload)

    assert sanitized == {
        "event": "lazy_tail_older_page",
        "source": "session-window",
        "session_id": "task-1",
        "stream_id": "run-1",
        "state": "ready",
        "source_mode": "lazy_tail_v1",
        "anchor_result": "preserved",
        "reconnect_status": "attached",
        "page_index": 4,
        "visible_count": 50,
        "elapsed_ms": 321,
    }
    assert "secret" not in repr(sanitized)
    assert "/Users" not in repr(sanitized)


def test_release_lite_client_event_numeric_fields_clamp_and_reject_booleans():
    sanitized = routes._sanitize_client_event_payload(
        {
            "event": "lazy_tail_first_paint",
            "page_index": -20,
            "visible_count": "999999999999",
            "elapsed_ms": True,
        }
    )

    assert sanitized["page_index"] == 0
    assert sanitized["visible_count"] == 1_000_000
    assert "elapsed_ms" not in sanitized


def test_session_window_route_publishes_request_diagnostics(monkeypatch):
    import api.session_window as session_window

    metrics = {}
    stages = []
    finished = []

    class FakeDiagnostics:
        @classmethod
        def maybe_start(cls, method, path, **_kwargs):
            assert (method, path) == ("GET", "/api/session-window")
            return cls()

        def set_metric(self, name, value):
            metrics[name] = value

        def stage(self, name):
            stages.append(name)

        def finish(self):
            finished.append(True)

    payload = {
        "requested_session_id": "requested",
        "canonical_session_id": "canonical",
        "messages": [],
        "runtime_snapshot": None,
        "conversation_window": {
            "schema": "lazy_tail_v1",
            "state": "ready",
            "status_reason": None,
        },
    }

    def fake_defaults(**kwargs):
        kwargs["diagnostic_sink"](
            {
                "state": "ready",
                "lineage_depth": 45,
                "sql_count": 47,
                "raw_rows_examined": 45,
                "visible_rows": 30,
                "serialized_bytes": 4096,
                "state_db_read_ms": 12,
                "handoff_retry_count": 0,
            }
        )
        return SimpleNamespace()

    monkeypatch.setattr(routes, "RequestDiagnostics", FakeDiagnostics)
    monkeypatch.setattr(
        session_window,
        "default_session_window_dependencies",
        fake_defaults,
    )
    monkeypatch.setattr(
        session_window,
        "build_session_window",
        lambda _request, deps: payload,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, response, status=200: (response, status),
    )

    result = routes._handle_session_window(
        SimpleNamespace(),
        urlparse("/api/session-window?session_id=requested&msg_limit=30"),
    )

    assert result == (payload, 200)
    assert metrics == {
        "lineage_depth": 45,
        "sql_count": 47,
        "raw_rows_examined": 45,
        "visible_rows": 30,
        "serialized_bytes": 4096,
        "state_db_read_ms": 12,
        "handoff_retry_count": 0,
    }
    assert stages == ["state_ready"]
    assert finished == [True]


def test_browser_emits_each_release_lite_rollout_event():
    assert "function recordLazyTailEvent(event, details={})" in SESSIONS_JS
    assert "lazy_tail_first_paint" in SESSIONS_JS
    assert "lazy_tail_older_page" in SESSIONS_JS
    assert "lazy_tail_legacy_explicit" in SESSIONS_JS
    assert "anchor_result:" in SESSIONS_JS

    assert "lazy_tail_reconnect_start" in MESSAGES_JS
    assert "lazy_tail_reconnect_attached" in MESSAGES_JS
    assert "lazy_tail_reconnect_failed" in MESSAGES_JS


def test_lazy_tail_browser_diagnostics_do_not_serialize_sensitive_fields():
    helper_start = SESSIONS_JS.index(
        "function recordLazyTailEvent(event, details={})"
    )
    helper_end = SESSIONS_JS.index(
        "\nfunction _currentLoadedRenderableMessageCount",
        helper_start,
    )
    helper = SESSIONS_JS[helper_start:helper_end]

    for forbidden in (
        "messages",
        "command",
        "arguments",
        "tool_output",
        "location.pathname",
        "location.search",
    ):
        assert forbidden not in helper
