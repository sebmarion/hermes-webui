import sqlite3

import pytest


def _make_messages_db(
    path,
    *,
    include_timestamp=True,
    include_required=True,
    include_id=True,
):
    timestamp_col = ", timestamp REAL" if include_timestamp else ""
    id_col = "id INTEGER PRIMARY KEY," if include_id else ""
    if include_required:
        required_cols = "session_id TEXT, role TEXT, content TEXT"
    else:
        required_cols = "session_id TEXT, role TEXT"
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE messages (
            {id_col}
            {required_cols}
            {timestamp_col},
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            reasoning TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            reasoning_content TEXT,
            codex_message_items TEXT,
            token_count INTEGER,
            finish_reason TEXT,
            platform_message_id TEXT,
            observed INTEGER,
            active INTEGER,
            compacted INTEGER,
            effect_disposition TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_message(
    path,
    message_id,
    session_id,
    role,
    content,
    *,
    timestamp=None,
    active=1,
    compacted=0,
    **optional,
):
    with sqlite3.connect(path) as conn:
        columns = ["id", "session_id", "role", "content"]
        values = [message_id, session_id, role, content]
        available = {
            row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "timestamp" in available:
            columns.append("timestamp")
            values.append(timestamp)
        for name, value in {"active": active, "compacted": compacted, **optional}.items():
            if name in available:
                columns.append(name)
                values.append(value)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO messages ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def test_reads_only_resolved_members_in_deterministic_order(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 10, "tip", "assistant", "tip", timestamp=30)
    _insert_message(db, 2, "root", "user", "root", timestamp=10)
    _insert_message(db, 5, "middle", "assistant", "middle", timestamp=20)
    _insert_message(db, 1, "unrelated", "user", "secret", timestamp=1)

    messages = read_resolved_session_history(
        db_path=db,
        member_ids=("root", "middle", "tip"),
    )

    assert [message["content"] for message in messages] == ["root", "middle", "tip"]
    assert all(message["content"] != "secret" for message in messages)


def test_tied_null_and_string_timestamps_have_stable_order(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 9, "tip", "assistant", "numeric-string", timestamp="20")
    _insert_message(db, 3, "root", "user", "null", timestamp=None)
    _insert_message(db, 8, "middle", "assistant", "tie-later-id", timestamp=10)
    _insert_message(db, 7, "root", "assistant", "tie-earlier-id", timestamp="10")
    _insert_message(db, 11, "tip", "assistant", "text", timestamp="later")

    messages = read_resolved_session_history(
        db_path=db,
        member_ids=("root", "middle", "tip"),
    )

    assert [message["content"] for message in messages] == [
        "null",
        "tie-earlier-id",
        "tie-later-id",
        "numeric-string",
        "text",
    ]


def test_optional_tool_reasoning_and_metadata_fields_are_preserved(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(
        db,
        1,
        "root",
        "tool",
        "done",
        timestamp=1,
        tool_call_id="call-1",
        tool_name="todo",
        tool_calls='[{"id":"call-1"}]',
        reasoning="reason",
        reasoning_details='[{"type":"summary"}]',
        codex_reasoning_items='[{"id":"r1"}]',
        reasoning_content="reason-content",
        codex_message_items='[{"id":"m1"}]',
        token_count=12,
        finish_reason="tool_calls",
        platform_message_id="platform-1",
        observed=1,
        effect_disposition="accepted",
    )

    [message] = read_resolved_session_history(db_path=db, member_ids=("root",))

    assert message["tool_calls"] == [{"id": "call-1"}]
    assert message["reasoning_details"] == [{"type": "summary"}]
    assert message["codex_reasoning_items"] == [{"id": "r1"}]
    assert message["codex_message_items"] == [{"id": "m1"}]
    assert message["tool_call_id"] == "call-1"
    assert message["name"] == message["tool_name"] == "todo"
    assert "token_count" not in message
    assert "finish_reason" not in message
    assert "platform_message_id" not in message
    assert "observed" not in message
    assert "effect_disposition" not in message
    assert "_state_db_message_id" not in message


def test_invalid_json_is_preserved_and_empty_optional_values_are_omitted(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(
        db,
        1,
        "root",
        "assistant",
        "x",
        timestamp=1,
        tool_calls="not-json",
        reasoning_details="",
    )

    [message] = read_resolved_session_history(db_path=db, member_ids=("root",))

    assert message["tool_calls"] == "not-json"
    assert "reasoning_details" not in message


def test_inactive_rows_are_excluded_by_default_with_current_reader_semantics(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 1, "root", "user", "active", timestamp=1)
    _insert_message(db, 2, "root", "assistant", "inactive", timestamp=2, active=0)
    _insert_message(db, 3, "root", "assistant", "compacted", timestamp=3, compacted=1)

    visible = read_resolved_session_history(db_path=db, member_ids=("root",))
    audit = read_resolved_session_history(
        db_path=db,
        member_ids=("root",),
        include_inactive=True,
    )

    assert [message["content"] for message in visible] == ["active", "compacted"]
    assert [message["content"] for message in audit] == [
        "active",
        "inactive",
        "compacted",
    ]


def test_duplicate_and_missing_member_ids_are_safe(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 1, "root", "user", "once", timestamp=1)

    messages = read_resolved_session_history(
        db_path=db,
        member_ids=("root", "missing", "root", "", "  "),
    )

    assert [message["content"] for message in messages] == ["once"]
    assert read_resolved_session_history(db_path=db, member_ids=()) == []
    with pytest.raises(ValueError):
        read_resolved_session_history(db_path=db, member_ids="root")
    with pytest.raises(ValueError):
        read_resolved_session_history(db_path=db, member_ids=("bad\x00id",))


def test_missing_timestamp_or_other_required_schema_fails_closed(tmp_path):
    from api.session_history import read_resolved_session_history

    without_ts = tmp_path / "without-timestamp.db"
    _make_messages_db(without_ts, include_timestamp=False)
    _insert_message(without_ts, 1, "root", "user", "old-schema")
    assert read_resolved_session_history(
        db_path=without_ts,
        member_ids=("root",),
    ) == []

    missing_required = tmp_path / "missing-required.db"
    _make_messages_db(missing_required, include_required=False)
    assert read_resolved_session_history(
        db_path=missing_required,
        member_ids=("root",),
    ) == []
    assert read_resolved_session_history(
        db_path=tmp_path / "missing.db",
        member_ids=("root",),
    ) == []

    without_id = tmp_path / "without-id.db"
    _make_messages_db(without_id, include_id=False)
    assert read_resolved_session_history(db_path=without_id, member_ids=("root",)) == []


def test_output_matches_current_state_db_reader(tmp_path, monkeypatch):
    import api.models as models
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 2, "root", "assistant", "reply", timestamp=2)
    _insert_message(
        db,
        1,
        "root",
        "tool",
        "result",
        timestamp=1,
        tool_call_id="call-1",
        tool_name="lookup",
        tool_calls='[{"id":"call-1"}]',
        reasoning_details="   ",
        token_count=999,
    )
    _insert_message(db, 3, "root", "assistant", "inactive", timestamp=3, active=0)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    expected = models.get_state_db_session_messages("root")
    actual = read_resolved_session_history(db_path=db, member_ids=("root",))

    assert actual == expected


def test_member_ids_are_chunked_under_sqlite_variable_limits(tmp_path):
    from api.session_history import read_resolved_session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 1, "s0", "user", "first", timestamp=1)
    _insert_message(db, 2, "s1000", "assistant", "last", timestamp=2)

    messages = read_resolved_session_history(
        db_path=db,
        member_ids=tuple(f"s{idx}" for idx in range(1001)),
    )

    assert [message["content"] for message in messages] == ["first", "last"]


class _TrackingConnection:
    def __init__(self, conn, statements):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "closed", False)
        conn.set_trace_callback(statements.append)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "row_factory":
            self._conn.row_factory = value
        else:
            object.__setattr__(self, name, value)

    def close(self):
        self.closed = True
        self._conn.close()


def test_query_shape_never_rewalks_lineage_and_connection_closes(tmp_path, monkeypatch):
    import api.agent_sessions as agent_sessions
    import api.session_history as session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)
    _insert_message(db, 1, "root", "user", "x", timestamp=1)
    statements = []
    wrapped = _TrackingConnection(sqlite3.connect(db), statements)
    monkeypatch.setattr(session_history, "open_state_db_readonly", lambda _path: wrapped)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("lineage must already be resolved")

    monkeypatch.setattr(agent_sessions, "resolve_shared_session", forbidden)
    monkeypatch.setattr(agent_sessions, "read_session_lineage_metadata", forbidden)
    monkeypatch.setattr(agent_sessions, "read_shared_session_rows", forbidden)
    monkeypatch.setattr(agent_sessions, "read_importable_agent_session_rows", forbidden)

    assert session_history.read_resolved_session_history(
        db_path=db,
        member_ids=("root",),
    )[0]["content"] == "x"
    assert wrapped.closed

    selected = [
        " ".join(statement.lower().split())
        for statement in statements
        if statement.lstrip().lower().startswith("select")
    ]
    assert selected
    assert all(" from sessions" not in statement for statement in selected)
    message_selects = [statement for statement in selected if " from messages" in statement]
    assert message_selects
    assert all(" where session_id in " in statement for statement in message_selects)
    assert any(statement.strip().lower() == "begin" for statement in statements)


def test_connection_closes_when_message_query_raises(tmp_path, monkeypatch):
    import api.session_history as session_history

    db = tmp_path / "state.db"
    _make_messages_db(db)

    class BrokenConnection(_TrackingConnection):
        def execute(self, sql, parameters=()):
            if "FROM messages" in sql:
                raise sqlite3.OperationalError("boom")
            return self._conn.execute(sql, parameters)

    wrapped = BrokenConnection(sqlite3.connect(db), [])
    monkeypatch.setattr(session_history, "open_state_db_readonly", lambda _path: wrapped)

    assert session_history.read_resolved_session_history(
        db_path=db,
        member_ids=("root",),
    ) == []
    assert wrapped.closed
