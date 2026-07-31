"""Release 0B regression coverage for bounded untitled-session previews."""

from __future__ import annotations

import sqlite3

from api import agent_sessions


def _make_state_db(path, *, session_count: int = 12, messages_per_session: int = 4):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            last_activity_at REAL,
            source TEXT,
            session_source TEXT
        );
        CREATE TABLE session_projection_meta (id INTEGER PRIMARY KEY);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    for index in range(session_count):
        sid = f"cli-{index:03d}"
        conn.execute(
            "INSERT INTO sessions VALUES (?, NULL, 'test-model', ?, ?, ?, 'cli', 'cli')",
            (
                sid,
                messages_per_session,
                1_000.0 + index,
                2_000.0 + index,
            ),
        )
        for message_index in range(messages_per_session):
            role = "user" if message_index % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO messages(session_id, role, content, timestamp, active) "
                "VALUES (?, ?, ?, ?, 1)",
                (
                    sid,
                    role,
                    f"{sid}-{role}-{message_index}",
                    2_000.0 + message_index,
                ),
            )
    conn.commit()
    conn.close()


def test_preview_enrichment_receives_only_the_final_visible_page(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    observed_sizes = []

    def _capture(rows, _cursor, _message_columns):
        observed_sizes.append(len(rows))

    monkeypatch.setattr(agent_sessions, "_enrich_untitled_with_preview", _capture)
    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=3,
        exclude_sources=None,
    )

    assert len(rows) == 3
    assert observed_sizes == [3]


def test_preview_query_uses_the_session_timestamp_index(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    conn = sqlite3.connect(db)
    try:
        sql, params = agent_sessions._build_untitled_preview_query(
            "cli-001",
            {"session_id", "role", "content", "timestamp", "active"},
        )
        plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    finally:
        conn.close()

    assert any(
        "SEARCH m USING INDEX idx_messages_session (session_id=?)" in str(row[3])
        for row in plan
    ), plan


def _preview_query_vm_steps(conn, session_id: str) -> int:
    sql, params = agent_sessions._build_untitled_preview_query(
        session_id,
        {"session_id", "role", "content", "timestamp", "active"},
    )
    steps = 0

    def _count_step():
        nonlocal steps
        steps += 1
        return 0

    conn.set_progress_handler(_count_step, 1)
    try:
        conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 0)
    return steps


def test_preview_query_caps_message_rows_examined_per_session(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=1, messages_per_session=0)
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, active) "
            "VALUES ('cli-000', 'assistant', 'noise', ?, 1)",
            ((float(index),) for index in range(10)),
        )
        conn.commit()
        small_prefix_steps = _preview_query_vm_steps(conn, "cli-000")

        conn.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, active) "
            "VALUES ('cli-000', 'assistant', 'noise', ?, 1)",
            ((float(index),) for index in range(10, 10_000)),
        )
        conn.commit()
        large_prefix_steps = _preview_query_vm_steps(conn, "cli-000")
    finally:
        conn.close()

    assert small_prefix_steps < 1_000
    assert large_prefix_steps < 5_000
    assert large_prefix_steps < small_prefix_steps + 4_500


def test_preview_is_first_active_user_message_and_is_bounded(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=2, messages_per_session=4)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE messages SET active = 0 WHERE session_id = 'cli-001' AND role = 'user' "
        "AND timestamp = 2000.0"
    )
    conn.execute(
        "UPDATE messages SET content = ? WHERE session_id = 'cli-001' AND role = 'user' "
        "AND timestamp = 2002.0",
        ("x" * 400,),
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=2,
        exclude_sources=None,
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["cli-001"]["preview"] == "x" * 160
    assert by_id["cli-000"]["preview"] == "cli-000-user-0"


def test_missing_preview_index_fails_closed_without_scanning(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=1)
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX idx_messages_session")
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=1,
        exclude_sources=None,
    )

    assert len(rows) == 1
    assert "preview" not in rows[0]


def test_messages_schema_without_role_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=1)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP TABLE messages;
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        INSERT INTO messages(session_id, content, timestamp, active)
        VALUES ('cli-000', 'assistant text must not become a title', 1.0, 1);
        """
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=1,
        exclude_sources=None,
    )

    assert len(rows) == 1
    assert "preview" not in rows[0]


def test_wrong_same_name_preview_index_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=1)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP INDEX idx_messages_session;
        CREATE INDEX idx_messages_session ON messages(timestamp);
        """
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=1,
        exclude_sources=None,
    )

    assert len(rows) == 1
    assert "preview" not in rows[0]


def test_same_columns_wrong_collation_preview_index_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, session_count=1)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        DROP INDEX idx_messages_session;
        CREATE INDEX idx_messages_session
            ON messages(session_id COLLATE NOCASE, timestamp);
        """
    )
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp, active) "
        "VALUES (?, 'assistant', 'unrelated', ?, 1)",
        (
            (f"unrelated-{index:05d}", float(index))
            for index in range(20_000)
        ),
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=1,
        exclude_sources=None,
    )

    assert len(rows) == 1
    assert "preview" not in rows[0]
