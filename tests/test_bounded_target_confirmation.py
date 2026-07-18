import os
import sqlite3


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                session_source TEXT,
                title TEXT,
                model TEXT,
                model_config TEXT,
                started_at REAL,
                ended_at REAL,
                end_reason TEXT,
                parent_session_id TEXT,
                message_count INTEGER,
                cwd TEXT,
                archived INTEGER,
                pinned INTEGER,
                last_activity_at REAL
            );
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE session_projection_meta (
                id INTEGER PRIMARY KEY,
                generation INTEGER
            );
            INSERT INTO session_projection_meta(id, generation) VALUES (1, 7);
            """
        )


def _insert(
    path,
    session_id,
    *,
    source="webui",
    started_at=1.0,
    ended_at=None,
    end_reason=None,
    parent_session_id=None,
    model_config=None,
    last_activity_at=None,
):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                id, source, session_source, title, model, model_config,
                started_at, ended_at, end_reason, parent_session_id,
                message_count, cwd, archived, pinned, last_activity_at
            ) VALUES (?, ?, NULL, ?, 'model', ?, ?, ?, ?, ?, 1, '/workspace', 0, 0, ?)
            """,
            (
                session_id,
                source,
                session_id,
                model_config,
                started_at,
                ended_at,
                end_reason,
                parent_session_id,
                started_at if last_activity_at is None else last_activity_at,
            ),
        )


def _resolved_chain(path):
    from api.agent_sessions import resolve_shared_session

    _make_db(path)
    _insert(path, "root", started_at=1, ended_at=2, end_reason="compression")
    _insert(path, "tip", started_at=3, parent_session_id="root")
    return resolve_shared_session(path, "root")


def _confirm(path, resolution):
    from api.bounded_target_confirmation import confirm_shared_session_target

    return confirm_shared_session_target(path, resolution)


def test_confirmation_accepts_unchanged_target_and_ignores_unrelated_row_change(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)
    _insert(db, "unrelated", started_at=99)

    assert _confirm(db, resolution) is True

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET model_config = '{\"changed\":true}' WHERE id = 'unrelated'")

    assert _confirm(db, resolution) is True


def test_confirmation_does_not_repeat_stage_one_resolution(tmp_path):
    from api.agent_sessions import (
        begin_shared_resolution_call_tracking,
        end_shared_resolution_call_tracking,
        resolve_shared_session,
    )

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", started_at=1, ended_at=2, end_reason="compression")
    _insert(db, "tip", started_at=3, parent_session_id="root")

    begin_shared_resolution_call_tracking()
    try:
        resolution = resolve_shared_session(db, "root")
        assert _confirm(db, resolution) is True
        assert end_shared_resolution_call_tracking() == 1
    finally:
        end_shared_resolution_call_tracking()


def test_confirmation_rejects_fingerprint_field_mutation(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET model_config = '{\"changed\":true}' WHERE id = 'tip'")

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_new_continuation_after_tip(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", started_at=1, ended_at=2, end_reason="compression")
    _insert(
        db,
        "tip",
        started_at=3,
        ended_at=4,
        end_reason="compression",
        parent_session_id="root",
    )
    resolution = resolve_shared_session(db, "root")
    _insert(db, "new-tip", started_at=5, parent_session_id="tip")

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_better_sibling_branch(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)
    _insert(
        db,
        "better",
        started_at=4,
        ended_at=5,
        end_reason="compression",
        parent_session_id="root",
    )

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_ambiguous_sibling_branch(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)
    _insert(db, "sibling", started_at=3, parent_session_id="root")

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_parent_that_becomes_eligible(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "old-parent", source="cli", started_at=1, ended_at=2, end_reason="compression")
    _insert(
        db,
        "root",
        started_at=3,
        ended_at=4,
        end_reason="compression",
        parent_session_id="old-parent",
    )
    _insert(db, "tip", started_at=5, parent_session_id="root")
    resolution = resolve_shared_session(db, "root")
    assert resolution.member_ids == ("root", "tip")

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET source = 'webui' WHERE id = 'old-parent'")

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_database_replacement_even_when_rows_match(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(db.read_bytes())
    os.replace(replacement, db)

    assert _confirm(db, resolution) is False


def test_confirmation_rejects_schema_change_after_resolution(tmp_path):
    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)

    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN later_schema_field TEXT")

    assert _confirm(db, resolution) is False


def test_confirmation_caps_members_and_uses_scoped_sql(tmp_path, monkeypatch):
    import api.bounded_target_confirmation as confirmation

    db = tmp_path / "state.db"
    resolution = _resolved_chain(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO sessions (
                id, source, title, model, started_at, message_count,
                cwd, archived, pinned, last_activity_at
            ) VALUES (?, 'webui', ?, 'model', ?, 1, '/workspace', 0, 0, ?)
            """,
            [(f"unrelated-{index}", f"unrelated-{index}", index + 100, index + 100) for index in range(10_000)],
        )

    statements = []
    original_open = confirmation.open_state_db_readonly

    def tracked_open(path):
        conn = original_open(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(confirmation, "open_state_db_readonly", tracked_open)

    assert _confirm(db, resolution) is True
    assert len(statements) <= 256
    data_queries = [statement.lower() for statement in statements if statement.lstrip().lower().startswith("select")]
    assert data_queries
    assert all(" from messages" not in statement for statement in data_queries)
    assert all(" where " in statement for statement in data_queries if " from sessions" in statement)

    too_many_members = resolution.__class__(
        **{**resolution.__dict__, "member_ids": tuple(f"member-{index}" for index in range(257))}
    )
    assert _confirm(db, too_many_members) is False
