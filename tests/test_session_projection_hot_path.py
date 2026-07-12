import json
import threading
import time

import api.models as models
import api.routes as routes
from api import route_session_list_cache


def test_index_projection_read_never_loads_sidecars_or_state_db(monkeypatch, tmp_path):
    index_file = tmp_path / "_index.json"
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": "visible",
                    "title": "Visible",
                    "message_count": 2,
                    "updated_at": 20.0,
                    "profile": "default",
                },
                {
                    "session_id": "empty",
                    "title": "Untitled",
                    "message_count": 0,
                    "updated_at": 10.0,
                    "profile": "default",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sidecar load"))),
    )
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    with models.LOCK:
        models.SESSIONS.clear()

    rows = models.read_session_index_projection()

    assert [row["session_id"] for row in rows] == ["visible"]


def test_seed_payload_applies_profile_archive_and_source_filters(monkeypatch):
    monkeypatch.setattr(
        models,
        "read_session_index_projection",
        lambda: [
            {
                "session_id": "visible",
                "title": "Visible",
                "message_count": 2,
                "updated_at": 30.0,
                "profile": "default",
                "archived": False,
            },
            {
                "session_id": "archived",
                "title": "Archived",
                "message_count": 1,
                "updated_at": 20.0,
                "profile": "default",
                "archived": True,
            },
            {
                "session_id": "other",
                "title": "Other",
                "message_count": 1,
                "updated_at": 10.0,
                "profile": "other",
                "archived": False,
            },
        ],
    )

    payload = routes._build_session_list_seed_payload(
        active_profile="default",
        all_profiles=False,
        include_archived=False,
        exclude_hidden=False,
        sidebar_source=None,
        archived_limit=None,
        archived_offset=0,
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["visible"]
    assert payload["archived_count"] == 1
    assert payload["other_profile_count"] == 1


def test_projection_token_refreshes_off_the_caller_thread(monkeypatch, tmp_path):
    from api import session_projection

    session_projection._reset_for_tests()
    started = threading.Event()
    release = threading.Event()

    def slow_read(_path):
        started.set()
        release.wait(1.0)
        return ("projection", 7)

    monkeypatch.setattr(session_projection, "_read_projection_token", slow_read)
    before = time.monotonic()
    initial = session_projection.projection_token(tmp_path / "state.db")
    elapsed = time.monotonic() - before

    assert elapsed < 0.1
    assert initial == ("cold", 0)
    assert started.wait(1.0)
    release.set()
    for _ in range(50):
        if session_projection.projection_token(tmp_path / "state.db") == ("projection", 7):
            break
        time.sleep(0.01)
    assert session_projection.projection_token(tmp_path / "state.db") == ("projection", 7)


def test_route_source_stamp_never_opens_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_cache_state_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_projection_token",
        lambda _path: ("projection", 9),
    )
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_cache_state_db_fingerprint",
        lambda _path: (_ for _ in ()).throw(AssertionError("request-time sqlite")),
    )
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_cache_streaming_freeze_marker",
        lambda: None,
    )
    key = route_session_list_cache._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    stamp = route_session_list_cache._session_list_cache_source_stamp(key)

    assert stamp[0] == ("projection", 9)
    assert stamp[1] == ("projection", 9)
    assert stamp[5] == ("projection", 9)
