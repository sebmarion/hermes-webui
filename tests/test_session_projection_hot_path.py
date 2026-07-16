import json
import sqlite3
import threading
import time
from collections import OrderedDict

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


def test_all_sessions_does_not_hold_global_lock_while_compacting_live_sessions(
    monkeypatch, tmp_path
):
    """A slow live-session projection must not block detail/session loads."""
    index_file = tmp_path / "_index.json"
    index_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", lambda: frozenset())
    monkeypatch.setattr(models, "_session_dir_has_persisted_session_files", lambda: False)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    session = models.Session(
        session_id="projection-lock-free",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hello"}],
    )
    models.SESSIONS[session.session_id] = session
    compact_started = threading.Event()
    release_compact = threading.Event()

    original_compact = models.Session.compact

    def blocking_compact(self, *args, **kwargs):
        if self.session_id == session.session_id:
            compact_started.set()
            release_compact.wait(2.0)
        return original_compact(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "compact", blocking_compact)

    projection = threading.Thread(target=models.all_sessions, kwargs={"include_lineage_metadata": False})
    projection.start()
    assert compact_started.wait(1.0)

    detail_loaded = threading.Event()

    def load_detail():
        models.get_session(session.session_id, metadata_only=True)
        detail_loaded.set()

    detail = threading.Thread(target=load_detail)
    detail.start()
    try:
        assert detail_loaded.wait(0.2), (
            "session detail loads must not wait for the sidebar projection's "
            "expensive Session.compact() call"
        )
    finally:
        release_compact.set()
        projection.join(2.0)
        detail.join(2.0)

    assert not projection.is_alive()
    assert not detail.is_alive()


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


def test_seed_payload_uses_state_db_archive_and_pin_truth(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, title TEXT, message_count INTEGER, "
            "archived INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.executemany(
            "INSERT INTO sessions "
            "(id, source, title, message_count, archived, pinned) "
            "VALUES (?, 'webui', ?, 1, ?, ?)",
            [
                ("state-archived", "State archived", 1, 0),
                ("state-active", "State active", 0, 1),
            ],
        )

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(
        models,
        "read_session_index_projection",
        lambda: [
            {
                "session_id": "state-archived",
                "title": "Stale active pin",
                "message_count": 1,
                "updated_at": 20.0,
                "profile": "default",
                "archived": False,
                "pinned": True,
            },
            {
                "session_id": "state-active",
                "title": "Stale archived unpin",
                "message_count": 1,
                "updated_at": 10.0,
                "profile": "default",
                "archived": True,
                "pinned": False,
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

    assert [row["session_id"] for row in payload["sessions"]] == ["state-active"]
    assert payload["sessions"][0]["pinned"] is True
    assert payload["archived_count"] == 1


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


def test_projection_token_keeps_last_known_generation_on_transient_read_error(monkeypatch, tmp_path):
    from api import session_projection

    session_projection._reset_for_tests()
    path = tmp_path / "state.db"
    reads = iter([("projection", 11), sqlite3.OperationalError("database is locked")])

    def read(_path):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(session_projection, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(session_projection, "_read_projection_token", read)

    session_projection.projection_token(path)
    for _ in range(100):
        token = session_projection.projection_token(path)
        if token == ("projection", 11):
            break
        time.sleep(0.01)
    assert token == ("projection", 11)

    # The next background read fails. The caller must keep generation 11,
    # never bounce through a WAL/stat legacy token.
    session_projection.projection_token(path)
    time.sleep(0.05)
    assert session_projection.projection_token(path) == ("projection", 11)


def test_cli_cache_context_uses_projection_generation_not_message_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        models,
        "_sqlite_file_stat_cache_key",
        lambda _path: (_ for _ in ()).throw(AssertionError("message/WAL fingerprint")),
    )
    monkeypatch.setattr(
        models,
        "_agent_session_projection_token",
        lambda _path: ("projection", 12),
    )

    _home, _db, _profile, key = models._resolve_cli_sessions_context(None)

    assert ("projection", 12) in key


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
