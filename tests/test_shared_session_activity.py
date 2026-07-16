import sqlite3
import sys
from types import SimpleNamespace


def test_shared_activity_lifecycle_and_expiry(tmp_path, monkeypatch):
    import api.agent_sessions as agent_sessions
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    state_sync.sync_session_activity(
        "sid",
        "run-1",
        phase="starting",
        started_at=100,
        heartbeat_at=100,
        profile="default",
    )
    state_sync.sync_session_activity(
        "sid",
        "run-1",
        phase="tool",
        started_at=100,
        heartbeat_at=110,
        model="fixture-model",
        cwd="/tmp/fixture-workspace",
        profile="default",
    )
    state_sync.sync_session_activity(
        "sid",
        "run-2",
        phase="running",
        started_at=105,
        heartbeat_at=112,
        profile="default",
    )

    fresh = agent_sessions.read_shared_session_activity(
        db_path, {"sid"}, now=120, ttl_seconds=20
    )
    assert fresh["sid"] == {
        "is_working": True,
        "activity_phase": "running",
        "activity_started_at": 100.0,
        "activity_heartbeat_at": 112.0,
    }

    stale = agent_sessions.read_shared_session_activity(
        db_path, {"sid"}, now=133, ttl_seconds=20
    )
    assert stale == {}

    state_sync.clear_session_activity("sid", "run-1", profile="default")
    state_sync.clear_session_activity("sid", "run-2", profile="default")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_activity").fetchone()[0] == 0


def test_activity_read_degrades_when_table_is_missing(tmp_path):
    import api.agent_sessions as agent_sessions

    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).close()

    assert agent_sessions.read_shared_session_activity(db_path, {"sid"}) == {}


def test_activity_sync_uses_lightweight_sqlite_when_sessiondb_is_unavailable(
    tmp_path, monkeypatch
):
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    def _sessiondb_must_not_be_used(profile=None):
        raise AssertionError("activity heartbeat must not construct SessionDB")

    monkeypatch.setattr(state_sync, "_get_state_db", _sessiondb_must_not_be_used)

    state_sync.sync_session_activity(
        "sid",
        "run-1",
        phase="tool",
        started_at=100,
        heartbeat_at=110,
        model="fixture-model",
        cwd="/tmp/fixture-workspace",
        profile="default",
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT phase, started_at, heartbeat_at FROM session_activity"
        ).fetchone()
        session = conn.execute(
            "SELECT source, model, cwd, message_count FROM sessions WHERE id = ?",
            ("sid",),
        ).fetchone()
    assert row == ("tool", 100.0, 110.0)
    assert session == ("webui", "fixture-model", "/tmp/fixture-workspace", 0)


def test_activity_sync_backfills_workspace_on_existing_session(tmp_path, monkeypatch):
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, model, cwd) VALUES (?, ?, ?, ?)",
            ("sid", "webui", "fixture-model", None),
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    state_sync.sync_session_activity(
        "sid",
        "run-1",
        phase="running",
        started_at=100,
        heartbeat_at=110,
        cwd="/tmp/fixture-workspace",
        profile="default",
    )

    with sqlite3.connect(db_path) as conn:
        cwd = conn.execute(
            "SELECT cwd FROM sessions WHERE id = ?", ("sid",)
        ).fetchone()[0]
    assert cwd == "/tmp/fixture-workspace"


def test_activity_sync_preserves_existing_authoritative_workspace(tmp_path, monkeypatch):
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, model, cwd) VALUES (?, ?, ?, ?)",
            ("sid", "webui", "fixture-model", "/tmp/current-workspace"),
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    state_sync.sync_session_activity(
        "sid",
        "run-1",
        phase="running",
        started_at=100,
        heartbeat_at=110,
        cwd="/tmp/stale-workspace",
        profile="default",
    )

    with sqlite3.connect(db_path) as conn:
        cwd = conn.execute(
            "SELECT cwd FROM sessions WHERE id = ?", ("sid",)
        ).fetchone()[0]
    assert cwd == "/tmp/current-workspace"


def test_active_run_session_rotation_moves_shared_heartbeat(tmp_path, monkeypatch):
    import api.config as config
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    monkeypatch.setattr(config, "_ensure_active_activity_heartbeat_thread", lambda: None)
    monkeypatch.setattr(config, "_publish_active_run_activity_change", lambda _entry: None)
    monkeypatch.setattr(config, "unregister_stream_owner", lambda _stream_id: None)

    original_runs = config.ACTIVE_RUNS.copy()
    try:
        config.ACTIVE_RUNS.clear()
        config.register_active_run(
            "run-1", session_id="compression-parent", phase="running", profile="default"
        )
        config.update_active_run(
            "run-1", session_id="compression-tip", phase="running", profile="default"
        )

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT session_id, run_id FROM session_activity ORDER BY session_id"
            ).fetchall()
        assert rows == [("compression-tip", "run-1")]
    finally:
        config.unregister_active_run("run-1")
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update(original_runs)


def test_async_delegation_keeps_parent_activity_visible_until_children_finish(
    tmp_path, monkeypatch
):
    import api.config as config
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source) VALUES (?, ?)",
            ("parent-session", "webui"),
        )
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    records = [
        {
            "delegation_id": "deleg-1",
            "session_key": "parent-session",
            "status": "running",
            "dispatched_at": 100.0,
            "model": "review-model",
        }
    ]
    monkeypatch.setitem(
        sys.modules,
        "tools.async_delegation",
        SimpleNamespace(list_async_delegations=lambda: list(records)),
    )
    published = []
    monkeypatch.setattr(
        config,
        "_publish_active_run_activity_change",
        lambda entry: published.append((entry.get("session_id"), entry.get("profile"))),
    )

    original_hints = dict(config._SESSION_ACTIVITY_PROFILE_HINTS)
    original_rows = dict(config._ACTIVE_DELEGATION_ACTIVITY_ROWS)
    try:
        config._SESSION_ACTIVITY_PROFILE_HINTS.clear()
        config._SESSION_ACTIVITY_PROFILE_HINTS["parent-session"] = "default"
        config._ACTIVE_DELEGATION_ACTIVITY_ROWS.clear()

        config._sync_active_delegation_activity()
        with sqlite3.connect(db_path) as conn:
            active = conn.execute(
                "SELECT session_id, run_id, phase, started_at FROM session_activity"
            ).fetchall()
        assert active == [
            ("parent-session", "delegation:deleg-1", "delegated", 100.0)
        ]

        records.clear()
        config._sync_active_delegation_activity()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM session_activity").fetchone()[0] == 0
        assert published == [
            ("parent-session", "default"),
            ("parent-session", "default"),
        ]
    finally:
        config._SESSION_ACTIVITY_PROFILE_HINTS.clear()
        config._SESSION_ACTIVITY_PROFILE_HINTS.update(original_hints)
        config._ACTIVE_DELEGATION_ACTIVITY_ROWS.clear()
        config._ACTIVE_DELEGATION_ACTIVITY_ROWS.update(original_rows)


def test_active_run_registry_mirrors_start_phase_and_finish(monkeypatch):
    import api.config as config

    original_runs = config.ACTIVE_RUNS.copy()
    calls = []
    try:
        config.ACTIVE_RUNS.clear()
        monkeypatch.setattr(config, "_ensure_active_activity_heartbeat_thread", lambda: None)
        monkeypatch.setattr(
            config,
            "_sync_active_run_activity",
            lambda entry, clear=False: calls.append(("sync", dict(entry), clear)),
        )
        monkeypatch.setattr(
            config,
            "_publish_active_run_activity_change",
            lambda entry: calls.append(("publish", dict(entry), False)),
        )
        monkeypatch.setattr(config, "unregister_stream_owner", lambda _stream_id: None)

        config.register_active_run(
            "run-1", session_id="sid", phase="starting", profile="default"
        )
        config.update_active_run("run-1", phase="tool")
        config.unregister_active_run("run-1")

        assert [call[0] for call in calls] == [
            "sync",
            "publish",
            "sync",
            "publish",
            "sync",
            "publish",
        ]
        assert calls[0][1]["session_id"] == "sid"
        assert calls[2][1]["phase"] == "tool"
        assert calls[4][2] is True
    finally:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update(original_runs)
