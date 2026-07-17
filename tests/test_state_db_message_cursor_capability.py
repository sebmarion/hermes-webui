import sqlite3

import pytest


def _schema(
    conn,
    *,
    timestamp_type="REAL",
    include_active=True,
    message_index=True,
    parent_index=True,
):
    active = ", active INTEGER" if include_active else ""
    conn.executescript(
        f"""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp {timestamp_type}
            {active}
        );
        """
    )
    if parent_index:
        conn.execute(
            "CREATE INDEX idx_sessions_parent ON sessions(parent_session_id)"
        )
    if message_index:
        conn.execute(
            "CREATE INDEX idx_messages_session "
            "ON messages(session_id, timestamp, id)"
        )


def _inspect(conn, identity=("fixture", 1, 1)):
    from api.session_message_paging import inspect_message_paging_capability

    return inspect_message_paging_capability(conn, db_identity=identity)


def test_supported_schema_returns_frozen_capability(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        cap = _inspect(conn, (str(db), 1, 1))

    assert cap.supported is True
    assert cap.schema_version == schema_version
    assert cap.ordering_columns == ("timestamp", "id")
    assert cap.message_index == "idx_messages_session"
    assert cap.has_active is True
    assert cap.fallback_reason is None
    with pytest.raises((AttributeError, TypeError)):
        cap.supported = False


def test_missing_incremental_blob_api_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn)

        class ConnectionWithoutBlobApi:
            blobopen = None

            def __getattr__(self, name):
                return getattr(conn, name)

        cap = _inspect(ConnectionWithoutBlobApi(), (str(db), "no-blob", 1))

    assert cap.supported is False
    assert cap.fallback_reason == "missing_blob_api"


def test_active_column_is_optional(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn, include_active=False)
        cap = _inspect(conn, (str(db), 1, 2))

    assert cap.supported is True
    assert cap.has_active is False


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("DROP TABLE messages", "missing_messages_table"),
        ("DROP INDEX idx_messages_session", "missing_message_index"),
        ("DROP INDEX idx_sessions_parent", "missing_session_parent_index"),
    ],
)
def test_missing_required_schema_fails_closed(tmp_path, mutation, reason):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        conn.execute(mutation)
        cap = _inspect(conn, (str(db), mutation, 1))

    assert cap.supported is False
    assert cap.fallback_reason == reason


def test_missing_stable_message_id_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id TEXT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp, id);
            """
        )
        cap = _inspect(conn, (str(db), "unstable-id", 1))

    assert cap.supported is False
    assert cap.fallback_reason == "unstable_message_id"


def test_int_primary_key_is_not_treated_as_rowid_alias(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp, id);
            """
        )
        cap = _inspect(conn, (str(db), "int-primary-key", 1))

    assert cap.supported is False
    assert cap.fallback_reason == "unstable_message_id"


def test_text_timestamp_affinity_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn, timestamp_type="TEXT")
        cap = _inspect(conn, (str(db), "text-ts", 1))

    assert cap.supported is False
    assert cap.fallback_reason == "unsupported_timestamp_affinity"


@pytest.mark.parametrize("timestamp_type", ["TEXT", "DATETIME", "DATE", "TIME"])
def test_non_numeric_timestamp_declarations_fail_closed(tmp_path, timestamp_type):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn, timestamp_type=timestamp_type)
        cap = _inspect(conn, (str(db), timestamp_type, 1))

    assert cap.supported is False
    assert cap.fallback_reason == "unsupported_timestamp_affinity"


def test_non_integer_active_affinity_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT);
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                active TEXT
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp);
            """
        )
        cap = _inspect(conn, (str(db), "text-active", 1))

    assert cap.supported is False
    assert cap.fallback_reason == "unsupported_active_affinity"


@pytest.mark.parametrize(
    "index_sql",
    [
        "CREATE INDEX idx_messages_session "
        "ON messages(session_id, timestamp) WHERE active != 0",
        "CREATE INDEX idx_messages_session "
        "ON messages(session_id COLLATE NOCASE, timestamp)",
    ],
)
def test_partial_or_collated_message_index_fails_closed(tmp_path, index_sql):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn, message_index=False)
        conn.execute(index_sql)
        cap = _inspect(conn, (str(db), index_sql, 1))

    assert cap.supported is False
    assert cap.fallback_reason == "missing_message_index"


def test_integer_primary_key_supplies_implicit_index_tie_break(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn, message_index=False)
        conn.execute(
            "CREATE INDEX idx_messages_session "
            "ON messages(session_id, timestamp)"
        )
        cap = _inspect(conn, (str(db), "implicit-rowid", 1))

    assert cap.supported is True
    assert cap.message_index == "idx_messages_session"


def test_capability_probe_is_read_only_and_within_six_statements(tmp_path):
    from api.session_message_paging import clear_message_paging_capability_cache

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as writable:
        _schema(writable)
        writable.commit()
    clear_message_paging_capability_cache()

    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        before = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        statements = []
        conn.set_trace_callback(statements.append)
        cap = _inspect(conn, (str(db), db.stat().st_size, db.stat().st_mtime_ns))
        conn.set_trace_callback(None)
        after = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()

    assert cap.supported is True
    assert len(statements) <= 6
    assert all(
        not statement.lstrip().upper().startswith(("CREATE ", "DROP ", "ALTER "))
        for statement in statements
    )
    assert after == before


def test_cache_hit_avoids_repeated_schema_probes_and_invalidates(tmp_path):
    from api.session_message_paging import clear_message_paging_capability_cache

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        _schema(conn)
        clear_message_paging_capability_cache()
        identity = (str(db), 1, 1)
        first = _inspect(conn, identity)
        statements = []
        conn.set_trace_callback(statements.append)
        second = _inspect(conn, identity)
        conn.set_trace_callback(None)

        assert second is first
        assert [statement.strip().upper() for statement in statements] == [
            "PRAGMA SCHEMA_VERSION"
        ]

        changed_identity = _inspect(conn, (str(db), 2, 1))
        assert changed_identity is not first

        conn.execute("CREATE TABLE capability_epoch(value INTEGER)")
        changed_schema = _inspect(conn, identity)
        assert changed_schema.schema_version > first.schema_version
        assert changed_schema is not first
