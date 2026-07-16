import sqlite3
from dataclasses import FrozenInstanceError

import pytest


def _make_db(path, *, with_parent_index=True, with_projection_meta=True):
    conn = sqlite3.connect(path)
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
        """
    )
    if with_parent_index:
        conn.execute(
            "CREATE INDEX idx_sessions_parent ON sessions(parent_session_id)"
        )
    if with_projection_meta:
        conn.executescript(
            """
            CREATE TABLE session_projection_meta (
                id INTEGER PRIMARY KEY,
                generation INTEGER
            );
            INSERT INTO session_projection_meta(id, generation) VALUES (1, 7);
            """
        )
    conn.commit()
    conn.close()


def _insert(
    path,
    sid,
    *,
    source="webui",
    session_source=None,
    title=None,
    model="model",
    model_config=None,
    started_at=1.0,
    ended_at=None,
    end_reason=None,
    parent_session_id=None,
    message_count=1,
    cwd="/workspace",
    archived=0,
    pinned=0,
    last_activity_at=None,
):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                id, source, session_source, title, model, model_config,
                started_at, ended_at, end_reason, parent_session_id,
                message_count, cwd, archived, pinned, last_activity_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                source,
                session_source,
                title or sid,
                model,
                model_config,
                started_at,
                ended_at,
                end_reason,
                parent_session_id,
                message_count,
                cwd,
                archived,
                pinned,
                started_at if last_activity_at is None else last_activity_at,
            ),
        )


def _make_chain(path):
    _make_db(path)
    _insert(
        path,
        "root",
        title="Root title",
        started_at=10,
        ended_at=20,
        end_reason="compression",
        message_count=4,
        archived=1,
        last_activity_at=20,
    )
    _insert(
        path,
        "middle",
        title="Root title #2",
        started_at=21,
        ended_at=30,
        end_reason="compression",
        parent_session_id="root",
        message_count=3,
        last_activity_at=30,
    )
    _insert(
        path,
        "tip",
        title="Renamed tip",
        model="tip-model",
        started_at=31,
        parent_session_id="middle",
        message_count=2,
        cwd="/tip",
        last_activity_at=40,
    )


def test_resolves_compression_snapshots_with_frozen_path_receipt(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_chain(db)

    root = resolve_shared_session(db, "root")
    middle = resolve_shared_session(db, "middle")
    tip = resolve_shared_session(db, "tip")

    assert root.status == "found"
    assert root.requested_id == "root"
    assert root.canonical_id == "tip"
    assert root.root_id == "root"
    assert root.tip_id == "tip"
    assert root.member_ids == ("root", "middle", "tip")
    assert root.canonical_row["id"] == "tip"
    assert root.canonical_row["title"] == "Renamed tip"
    assert root.canonical_row["cwd"] == "/tip"
    assert root.canonical_row["message_count"] == 2
    assert root.canonical_row["actual_message_count"] == 2
    assert root.canonical_row["archived"] is False
    assert root.canonical_row["pinned"] is False
    assert root.global_projection_generation_hint == 7
    assert root.lineage_fingerprint
    assert middle.canonical_id == "tip"
    assert middle.member_ids == ("root", "middle", "tip")
    assert tip.canonical_id == "tip"
    assert tip.member_ids == ("root", "middle", "tip")
    with pytest.raises(FrozenInstanceError):
        root.canonical_id = "other"
    with pytest.raises(TypeError):
        root.canonical_row["title"] = "mutated"


def test_history_mode_keeps_requested_physical_row(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_chain(db)

    result = resolve_shared_session(db, "middle", mode="history")

    assert result.status == "found"
    assert result.canonical_id == "middle"
    assert result.root_id == "root"
    assert result.tip_id == "middle"
    assert result.member_ids == ("root", "middle")
    assert result.canonical_row["id"] == "middle"

    direct = resolve_shared_session(db, "tip")
    assert direct.canonical_row["archived"] is False
    assert direct.canonical_row["pinned"] is False


def test_generated_continuation_title_keeps_root_visible_title(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_chain(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET title = 'Root title #3' WHERE id = 'tip'")

    result = resolve_shared_session(db, "root")

    assert result.canonical_id == "tip"
    assert result.canonical_row["title"] == "Root title"


def test_direct_live_sibling_is_stable_but_snapshot_uses_ranked_path(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(
        db,
        "root",
        started_at=1,
        ended_at=2,
        end_reason="compression",
        last_activity_at=2,
    )
    _insert(
        db,
        "deep",
        started_at=3,
        ended_at=4,
        end_reason="compression",
        parent_session_id="root",
        last_activity_at=4,
    )
    _insert(
        db,
        "deep-tip",
        started_at=5,
        parent_session_id="deep",
        last_activity_at=5,
    )
    _insert(
        db,
        "newer-live-sibling",
        started_at=100,
        parent_session_id="root",
        last_activity_at=100,
    )

    assert resolve_shared_session(db, "root").canonical_id == "deep-tip"
    sibling = resolve_shared_session(db, "newer-live-sibling")
    assert sibling.canonical_id == "newer-live-sibling"
    assert sibling.member_ids == ("root", "newer-live-sibling")


@pytest.mark.parametrize(
    ("sid", "row_kwargs"),
    [
        ("fork", {"session_source": "fork"}),
        ("tool", {"source": "tool"}),
        ("branch", {"model_config": '{"_branched_from":"root"}'}),
        ("delegate", {"model_config": '{"_delegate_from":"root"}'}),
        ("cross", {"source": "cli"}),
        ("missing-source", {"source": None}),
        ("too-early", {"started_at": 1.5}),
    ],
)
def test_continuation_guard_rejections_remain_direct(tmp_path, sid, row_kwargs):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(
        db,
        "root",
        started_at=1,
        ended_at=2,
        end_reason="compression",
    )
    child_kwargs = {"started_at": 3, "parent_session_id": "root", **row_kwargs}
    _insert(db, sid, **child_kwargs)

    result = resolve_shared_session(db, sid)

    assert result.status == "found"
    assert result.canonical_id == sid
    assert result.root_id == sid
    assert result.member_ids == (sid,)


def test_cli_close_is_not_a_shared_continuation(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", ended_at=2, end_reason="cli_close")
    _insert(db, "child", started_at=3, parent_session_id="root")

    assert resolve_shared_session(db, "root").canonical_id == "root"
    assert resolve_shared_session(db, "child").root_id == "child"


def test_semantically_tied_siblings_fail_closed_as_ambiguous(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", ended_at=2, end_reason="compression")
    for sid in ("a", "b"):
        _insert(
            db,
            sid,
            started_at=3,
            parent_session_id="root",
            last_activity_at=3,
        )

    result = resolve_shared_session(db, "root")

    assert result.status == "ambiguous"
    assert result.canonical_id == "root"
    assert result.root_id == "root"
    assert result.tip_id == "root"
    assert result.member_ids == ("root",)


def test_clear_ranked_siblings_match_collection_projection(tmp_path):
    from api.agent_sessions import read_shared_session_rows, resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", ended_at=2, end_reason="compression")
    _insert(
        db,
        "compression-child",
        started_at=3,
        ended_at=4,
        end_reason="compression",
        parent_session_id="root",
    )
    _insert(db, "compression-tip", started_at=5, parent_session_id="compression-child")
    _insert(db, "newer-live", started_at=100, parent_session_id="root")

    expected = next(
        row["id"]
        for row in read_shared_session_rows(db, include_archived=True)
        if row.get("_lineage_root_id") == "root"
    )
    assert resolve_shared_session(db, "root").canonical_id == expected


def test_zero_message_continuation_uses_collection_root_fallback(tmp_path):
    from api.agent_sessions import read_shared_session_rows, resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(
        db,
        "root",
        ended_at=2,
        end_reason="compression",
        message_count=2,
    )
    _insert(
        db,
        "empty-tip",
        started_at=3,
        parent_session_id="root",
        message_count=0,
    )

    projected = read_shared_session_rows(db, include_archived=True)
    assert [row["id"] for row in projected] == ["root"]
    resolved = resolve_shared_session(db, "root")
    assert resolved.canonical_id == "root"
    assert resolved.member_ids == ("root",)


@pytest.mark.parametrize("shape", ["cycle", "self_parent", "missing_parent"])
def test_broken_lineage_fails_closed(tmp_path, shape):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    if shape == "cycle":
        _insert(
            db,
            "a",
            started_at=1,
            ended_at=2,
            end_reason="compression",
            parent_session_id="b",
        )
        _insert(
            db,
            "b",
            started_at=3,
            ended_at=4,
            end_reason="compression",
            parent_session_id="a",
        )
        sid = "a"
    elif shape == "self_parent":
        _insert(
            db,
            "a",
            ended_at=2,
            end_reason="compression",
            parent_session_id="a",
        )
        sid = "a"
    else:
        _insert(db, "a", parent_session_id="gone")
        sid = "a"

    result = resolve_shared_session(db, sid)

    assert result.status == "degraded"
    assert result.canonical_id == sid
    assert result.member_ids == (sid,)


@pytest.mark.parametrize(("rows", "expected_status"), [(256, "found"), (257, "degraded")])
def test_total_lineage_row_cap_is_explicit(tmp_path, rows, expected_status):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        payload = []
        for idx in range(rows):
            payload.append(
                (
                    f"s{idx}",
                    "webui",
                    None,
                    f"s{idx}",
                    "model",
                    None,
                    float(idx * 2 + 1),
                    float(idx * 2 + 2) if idx < rows - 1 else None,
                    "compression" if idx < rows - 1 else None,
                    f"s{idx - 1}" if idx else None,
                    1,
                    "/workspace",
                    0,
                    0,
                    float(idx * 2 + 1),
                )
            )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    result = resolve_shared_session(db, "s0")

    assert result.status == expected_status
    assert result.canonical_id == (f"s{rows - 1}" if rows == 256 else "s0")


def test_broad_sibling_fanout_hits_same_total_row_cap(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, "root", ended_at=2, end_reason="compression")
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO sessions (
                id, source, title, model, started_at, parent_session_id,
                message_count, archived, pinned, last_activity_at
            ) VALUES (?, 'webui', ?, 'model', ?, 'root', 1, 0, 0, ?)
            """,
            [(f"child-{idx}", f"child-{idx}", idx + 3, idx + 3) for idx in range(256)],
        )

    result = resolve_shared_session(db, "root")

    assert result.status == "degraded"
    assert result.canonical_id == "root"


def test_missing_schema_or_parent_index_fails_closed(tmp_path):
    from api.agent_sessions import resolve_shared_session

    missing = resolve_shared_session(tmp_path / "missing.db", "wanted")
    assert missing.status == "missing"
    assert missing.canonical_id == "wanted"

    old = tmp_path / "old.db"
    sqlite3.connect(old).execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)").connection.close()
    assert resolve_shared_session(old, "wanted").status == "degraded"

    no_index = tmp_path / "no-index.db"
    _make_db(no_index, with_parent_index=False)
    _insert(no_index, "wanted")
    assert resolve_shared_session(no_index, "wanted").status == "degraded"

    partial = tmp_path / "partial-index.db"
    _make_db(partial, with_parent_index=False)
    with sqlite3.connect(partial) as conn:
        conn.execute(
            "CREATE INDEX idx_sessions_parent "
            "ON sessions(parent_session_id) WHERE source = 'webui'"
        )
    _insert(partial, "wanted")
    assert resolve_shared_session(partial, "wanted").status == "degraded"

    collated = tmp_path / "collated-index.db"
    _make_db(collated, with_parent_index=False)
    with sqlite3.connect(collated) as conn:
        conn.execute(
            "CREATE INDEX idx_sessions_parent "
            "ON sessions(parent_session_id COLLATE NOCASE)"
        )
    _insert(collated, "wanted")
    assert resolve_shared_session(collated, "wanted").status == "degraded"


def test_projection_generation_is_hint_only(tmp_path):
    from api.agent_sessions import resolve_shared_session

    db = tmp_path / "state.db"
    _make_chain(db)
    before = resolve_shared_session(db, "root")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE session_projection_meta SET generation = 99 WHERE id = 1")
    after = resolve_shared_session(db, "root")

    assert before.global_projection_generation_hint == 7
    assert after.global_projection_generation_hint == 99
    assert after.canonical_id == before.canonical_id
    assert after.lineage_fingerprint == before.lineage_fingerprint

    without_meta = tmp_path / "without-meta.db"
    _make_db(without_meta, with_projection_meta=False)
    _insert(without_meta, "only")
    assert resolve_shared_session(without_meta, "only").global_projection_generation_hint is None

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE session_projection_meta SET generation = 7.5 WHERE id = 1")
    assert resolve_shared_session(db, "root").global_projection_generation_hint is None


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


def test_query_shape_is_scoped_invariant_and_connection_closes(tmp_path, monkeypatch):
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    _make_chain(db)

    def run():
        statements = []
        wrapped = _TrackingConnection(sqlite3.connect(db), statements)
        monkeypatch.setattr(agent_sessions, "open_state_db_readonly", lambda _path: wrapped)
        result = agent_sessions.resolve_shared_session(db, "root")
        return result, statements, wrapped

    first, first_statements, first_conn = run()
    with sqlite3.connect(db) as conn:
        conn.executemany(
            """
            INSERT INTO sessions (
                id, source, title, model, started_at, message_count,
                archived, pinned, last_activity_at
            ) VALUES (?, 'webui', ?, 'model', ?, 1, 0, 0, ?)
            """,
            [(f"unrelated-{idx}", f"u{idx}", idx + 1000, idx + 1000) for idx in range(10_000)],
        )
    second, second_statements, second_conn = run()

    def data_queries(statements):
        return [
            " ".join(stmt.lower().split())
            for stmt in statements
            if stmt.lstrip().lower().startswith("select")
        ]

    assert first.canonical_id == second.canonical_id == "tip"
    assert len(data_queries(first_statements)) == len(data_queries(second_statements))
    assert first_conn.closed and second_conn.closed
    assert first_statements[0].strip().upper() == "BEGIN"
    for statement in data_queries(second_statements):
        assert " from messages" not in statement
        if " from sessions" in statement:
            assert " where " in statement
    child_queries = [
        statement
        for statement in data_queries(second_statements)
        if "parent_session_id =" in statement
    ]
    assert child_queries and all(" limit " in statement for statement in child_queries)


def test_collection_helpers_are_not_used(tmp_path, monkeypatch):
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    _make_chain(db)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("collection projection must not run for one entity")

    monkeypatch.setattr(agent_sessions, "read_shared_session_rows", forbidden)
    monkeypatch.setattr(agent_sessions, "read_importable_agent_session_rows", forbidden)

    result = agent_sessions.resolve_shared_session(db, "root")

    assert result.canonical_id == "tip"


def test_compatibility_id_wrapper_uses_bounded_resolution(tmp_path, monkeypatch):
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    _make_chain(db)
    calls = []
    real = agent_sessions.resolve_shared_session

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_sessions, "resolve_shared_session", tracked)

    assert agent_sessions.resolve_shared_session_id(db, "root") == "tip"
    assert len(calls) == 1
