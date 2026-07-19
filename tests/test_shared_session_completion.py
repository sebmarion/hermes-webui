"""Durable completion-event bridge tests.

These tests intentionally use a minimal SQLite database instead of SessionDB:
the completion/activity overlay must remain additive and lightweight.
"""

from __future__ import annotations

import sqlite3


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, cwd TEXT, "
            "message_count INTEGER NOT NULL DEFAULT 0)"
        )


def test_finish_session_activity_inserts_idempotent_completion(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)

    state_sync.sync_session_activity(
        "sid",
        "run-1",
        started_at=100,
        heartbeat_at=110,
        profile="default",
    )
    first = state_sync.finish_session_activity(
        "sid",
        "run-1",
        profile="default",
        lineage_session_ids={"sid"},
        emit_completion=True,
        completion_session_id="sid",
        source=state_sync.COMPLETION_SOURCE_WEBUI_NATIVE,
        completed_at=120,
    )
    second = state_sync.finish_session_activity(
        "sid",
        "run-1",
        profile="default",
        lineage_session_ids={"sid"},
        emit_completion=True,
        completion_session_id="sid",
        source=state_sync.COMPLETION_SOURCE_WEBUI_NATIVE,
        completed_at=120,
    )

    assert first["activity_deleted"] is True
    assert first["inserted"] is True
    assert first["generation"] == 1
    assert second["activity_deleted"] is False
    assert second["inserted"] is False
    assert second["generation"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_activity").fetchone()[0] == 0
        assert conn.execute(
            "SELECT session_id, run_id, source, completed_at, outcome "
            "FROM session_completion_events"
        ).fetchall() == [(
            "sid",
            "run-1",
            state_sync.COMPLETION_SOURCE_WEBUI_NATIVE,
            120.0,
            "completed",
        )]


def test_finish_session_activity_does_not_emit_while_alias_is_active(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    state_sync.sync_session_activity("old-tip", "run-1", heartbeat_at=110, profile="default")
    state_sync.sync_session_activity("new-tip", "run-2", heartbeat_at=119, profile="default")

    result = state_sync.finish_session_activity(
        "old-tip",
        "run-1",
        profile="default",
        lineage_session_ids={"old-tip", "new-tip"},
        emit_completion=True,
        completion_session_id="new-tip",
        source=state_sync.COMPLETION_SOURCE_WEBUI_NATIVE,
        completed_at=120,
    )

    assert result["activity_deleted"] is True
    assert result["inserted"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_completion_events").fetchone()[0] == 0


def test_clear_only_finalization_removes_activity_without_event(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    state_sync.sync_session_activity("sid", "run-1", profile="default")

    result = state_sync.finish_session_activity(
        "sid",
        "run-1",
        profile="default",
        lineage_session_ids={"sid"},
        emit_completion=False,
    )

    assert result["activity_deleted"] is True
    assert result["inserted"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_completion_events").fetchone()[0] == 0


def test_completion_reads_degrade_without_table(tmp_path):
    from api import agent_sessions

    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).close()

    assert agent_sessions.read_shared_session_completions(db_path, {"sid"}) == {}


def test_completion_reads_latest_generation_per_physical_session(tmp_path, monkeypatch):
    from api import state_sync, agent_sessions

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    for index in (1, 2):
        state_sync.sync_session_activity("sid", f"run-{index}", profile="default")
        state_sync.finish_session_activity(
            "sid",
            f"run-{index}",
            profile="default",
            lineage_session_ids={"sid"},
            emit_completion=True,
            completion_session_id="sid",
            source=state_sync.COMPLETION_SOURCE_WEBUI_NATIVE,
            completed_at=100 + index,
        )

    rows = agent_sessions.read_shared_session_completions(db_path, {"sid"})
    assert rows["sid"]["generation"] == 2
    assert rows["sid"]["run_id"] == "run-2"
    assert rows["sid"]["completed_at"] == 102.0


def test_completion_source_constants_are_stable():
    from api import state_sync

    assert state_sync.COMPLETION_SOURCE_WEBUI_NATIVE == "webui-native"
    assert state_sync.COMPLETION_SOURCE_WEBUI_GATEWAY == "webui-gateway"


def test_completion_schema_has_durable_fields_and_lineage_index(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    state_sync.sync_session_activity("sid", "run", profile="default")
    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(session_completion_events)")]
        assert columns == ["generation", "session_id", "run_id", "source", "completed_at", "outcome"]
        indexes = conn.execute("PRAGMA index_list(session_completion_events)").fetchall()
        index_names = {row[1] for row in indexes}
        assert "idx_session_completion_events_session_generation" in index_names
        index_columns = conn.execute(
            "PRAGMA index_info(idx_session_completion_events_session_generation)"
        ).fetchall()
        assert [row[2] for row in index_columns] == ["session_id", "generation"]


def test_unknown_completion_source_is_clear_only(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    state_sync.sync_session_activity("sid", "run", profile="default")
    result = state_sync.finish_session_activity(
        "sid", "run", profile="default", emit_completion=True, source="unknown"
    )
    assert result["activity_deleted"] is True
    assert result["inserted"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_completion_events").fetchone()[0] == 0


def test_same_run_id_is_distinct_per_transport_source(tmp_path, monkeypatch):
    from api import state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db_path", lambda profile=None: db_path)
    for source, sid in (
        (state_sync.COMPLETION_SOURCE_WEBUI_NATIVE, "native"),
        (state_sync.COMPLETION_SOURCE_WEBUI_GATEWAY, "gateway"),
    ):
        state_sync.sync_session_activity(sid, "same-run", profile="default")
        state_sync.finish_session_activity(
            sid, "same-run", profile="default", emit_completion=True,
            completion_session_id=sid, source=source,
        )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_completion_events").fetchone()[0] == 2
