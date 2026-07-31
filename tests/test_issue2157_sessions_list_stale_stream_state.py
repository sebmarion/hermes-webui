import io
import json
import threading
from urllib.parse import urlparse

import api.models as models
import api.profiles as profiles
import api.routes as routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_sessions_list_reconciles_stale_stream_state_before_serializing(monkeypatch):
    routes._session_list_cache_clear()
    repaired = {"value": False}
    all_sessions_calls = {"count": 0}
    refresh_finished = threading.Event()
    repair_finished = threading.Event()

    class _Session:
        def __init__(self):
            self.session_id = "stale-session"
            self.active_stream_id = "stale-stream"

    def projected_rows():
        if repaired["value"]:
            active_stream_id = None
            is_streaming = False
        else:
            active_stream_id = "stale-stream"
            is_streaming = False
        rows = [
            {
                "session_id": "stale-session",
                "title": "Stale Session",
                "profile": "default",
                "message_count": 1,
                "active_stream_id": active_stream_id,
                "is_streaming": is_streaming,
                "updated_at": 1,
                "last_message_at": 1,
            }
        ]
        return rows

    def fake_all_sessions(diag=None, **_kwargs):
        all_sessions_calls["count"] += 1
        rows = projected_rows()
        refresh_finished.set()
        return rows

    def fake_get_session(session_id, metadata_only=False):
        assert session_id == "stale-session"
        assert metadata_only is True
        return _Session()

    def fake_clear_stale_stream_state(session):
        repaired["value"] = True
        session.active_stream_id = None
        repair_finished.set()
        return True

    monkeypatch.setattr(routes, "all_sessions", fake_all_sessions)
    monkeypatch.setattr(models, "read_session_index_projection", projected_rows)
    monkeypatch.setattr(routes, "get_session", fake_get_session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", fake_clear_stale_stream_state)
    monkeypatch.setattr(
        routes,
        "_schedule_stale_stream_state_reconciliation",
        lambda rows: routes._reconcile_stale_stream_state_for_session_rows(rows) or True,
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/sessions")
    routes.handle_get(handler, parsed)

    assert handler.status == 200
    payload = handler.json_body()
    sessions = payload["sessions"]
    assert refresh_finished.wait(1.0)
    assert repair_finished.wait(1.0)
    assert all_sessions_calls["count"] == 1
    assert repaired["value"] is True
    # The request serves its already-built projection; repair is visible after
    # the normal cache refresh rather than blocking this response.
    assert sessions[0]["active_stream_id"] == "stale-stream"
    assert sessions[0]["is_streaming"] is False

    routes._session_list_cache_clear()
    refresh_finished.clear()
    second = _FakeHandler()
    routes.handle_get(second, parsed)
    second_sessions = second.json_body()["sessions"]
    assert refresh_finished.wait(1.0)
    assert all_sessions_calls["count"] == 2
    assert second_sessions[0]["active_stream_id"] is None
    routes._session_list_cache_clear()


def test_reconcile_stale_stream_state_skips_live_stream_rows(monkeypatch):
    loaded = []

    def fake_get_session(session_id, metadata_only=False):
        loaded.append((session_id, metadata_only))
        raise AssertionError("live stream rows should not be loaded for cleanup")

    monkeypatch.setattr(routes, "get_session", fake_get_session)

    changed = routes._reconcile_stale_stream_state_for_session_rows([
        {
            "session_id": "live-session",
            "active_stream_id": "live-stream",
            "is_streaming": True,
        }
    ])

    assert changed is False
    assert loaded == []


def test_stale_stream_reconciliation_is_admission_tracked(monkeypatch):
    calls = []

    def fake_start_admitted_auxiliary_thread(**kwargs):
        calls.append(kwargs)
        kwargs["target"](*kwargs.get("args", ()), **(kwargs.get("kwargs") or {}))
        return True

    monkeypatch.setattr(
        routes,
        "start_admitted_auxiliary_thread",
        fake_start_admitted_auxiliary_thread,
    )
    monkeypatch.setattr(
        routes,
        "_reconcile_stale_stream_state_for_session_rows",
        lambda rows: rows == [{"session_id": "stale"}],
    )

    assert routes._schedule_stale_stream_state_reconciliation(
        [{"session_id": "stale"}]
    ) is True
    assert len(calls) == 1
    assert calls[0]["kind"] == "session_sidecar_reconciliation"
    assert calls[0]["name"] == "stale-stream-reconciliation"
    assert routes._STALE_STREAM_RECONCILIATION_LOCK.locked() is False
