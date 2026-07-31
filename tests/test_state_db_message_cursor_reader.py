import json
import sqlite3
from types import MappingProxyType

import pytest

from api.agent_sessions import SharedSessionResolution, shared_state_db_identity


def _make_db(
    path,
    *,
    unrelated=0,
    unrelated_messages=None,
    timestamp_type="REAL",
):
    assert timestamp_type in {"REAL", "INTEGER"}
    with sqlite3.connect(path) as conn:
        conn.executescript(
            f"""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                title TEXT
            );
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp {timestamp_type},
                active INTEGER NOT NULL DEFAULT 1,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp);
            INSERT INTO sessions(id, parent_session_id, title)
                VALUES ('root', NULL, 'Root');
            INSERT INTO sessions(id, parent_session_id, title)
                VALUES ('tip', 'root', 'Tip');
            """
        )
        if unrelated:
            conn.executemany(
                "INSERT INTO sessions(id, title) VALUES (?, ?)",
                ((f"other-{index}", "Other") for index in range(unrelated)),
            )
            message_count = (
                unrelated * 10
                if unrelated_messages is None
                else int(unrelated_messages)
            )
            conn.executemany(
                "INSERT INTO messages(session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'noise', ?)",
                (
                    (f"other-{index % unrelated}", float(index))
                    for index in range(message_count)
                ),
            )


def _resolution(path):
    return SharedSessionResolution(
        requested_id="root",
        canonical_id="tip",
        root_id="root",
        tip_id="tip",
        member_ids=("root", "tip"),
        canonical_row=MappingProxyType({"id": "tip", "title": "Tip"}),
        lineage_fingerprint="sha256:" + ("a" * 64),
        global_projection_generation_hint=1,
        mode="navigation",
        status="found",
        database_identity=shared_state_db_identity(path),
    )


def _insert(path, message_id, session_id, role, content, timestamp, *, active=1):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, session_id, role, content, timestamp, active),
        )


def test_reader_returns_chronological_visible_tail_across_members(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, 1, "root", "user", "null-oldest", None)
    _insert(db, 2, "root", "assistant", "tie-low-id", 2)
    _insert(db, 3, "tip", "tool", "hidden-tool", 2)
    _insert(db, 4, "tip", "assistant", "tie-high-id", 2)
    _insert(db, 5, "root", "user", "inactive", 3, active=0)
    _insert(db, 6, "tip", "user", "newest", 4)

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=3,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["content"] for message in page.messages] == [
        "tie-low-id",
        "tie-high-id",
        "newest",
    ]
    assert [message["_state_db_message_id"] for message in page.messages] == [2, 4, 6]
    assert page.visible_count == 3
    assert page.raw_rows_examined <= 256
    assert page.query_plan_indexed is True
    assert page.sql_count <= 3 + len(_resolution(db).member_ids)


def test_reader_continuation_boundaries_reconstruct_visible_history(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    for message_id in range(1, 8):
        _insert(
            db,
            message_id,
            "root" if message_id < 4 else "tip",
            "user" if message_id % 2 else "assistant",
            f"m{message_id}",
            message_id,
        )

    newest = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=3,
        cursor=None,
    )
    older = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=4,
        cursor=newest.before_boundaries,
    )

    assert [message["content"] for message in older.messages + newest.messages] == [
        f"m{message_id}" for message_id in range(1, 8)
    ]
    assert newest.has_more is True
    assert older.has_more is False


def test_every_message_query_shape_has_a_bounded_indexed_physical_plan(tmp_path):
    from api.session_message_paging import (
        MessageCursorBoundary,
        _message_page_plan_is_indexed,
        _message_page_query,
    )

    db = tmp_path / "state.db"
    _make_db(db)
    boundaries = (
        None,
        MessageCursorBoundary("tip", 10.0, 10, inclusive=False),
        MessageCursorBoundary("tip", 10.0, 10, inclusive=True),
        MessageCursorBoundary("tip", None, 10, inclusive=False),
        MessageCursorBoundary("tip", None, 10, inclusive=True),
    )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        for boundary in boundaries:
            statement, params = _message_page_query(
                selected=(
                    "id",
                    "session_id",
                    "role",
                    "content",
                    "timestamp",
                    "active",
                ),
                quoted_index='"idx_messages_session"',
                member_id="tip",
                boundary=boundary,
                raw_budget=256,
            )
            details = [
                str(row["detail"])
                for row in conn.execute(
                    f"EXPLAIN QUERY PLAN {statement}",
                    params,
                ).fetchall()
            ]
            assert _message_page_plan_is_indexed(
                conn,
                statement,
                params,
                "idx_messages_session",
                boundary,
            )
            assert not any("TEMP B-TREE" in detail.upper() for detail in details)
            assert not any("SCAN messages" in detail for detail in details)


def test_reader_runtime_sql_count_includes_every_non_capability_execute(
    tmp_path,
    monkeypatch,
):
    import api.session_message_paging as paging

    db = tmp_path / "state.db"
    _make_db(db)
    for message_id in range(1, 8):
        _insert(db, message_id, "tip", "user", f"m{message_id}", message_id)
    resolution = _resolution(db)
    paging.clear_message_paging_capability_cache()
    original_open = paging.open_state_db_readonly
    with original_open(db) as conn:
        paging.inspect_message_paging_capability(
            conn,
            db_identity=resolution.database_identity,
        )

    statements = []

    class CountingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

        def execute(self, statement, params=()):
            statements.append(str(statement))
            return self._connection.execute(statement, params)

    monkeypatch.setattr(
        paging,
        "open_state_db_readonly",
        lambda path, log=None: CountingConnection(original_open(path, log=log)),
    )

    page = paging.read_state_db_message_page(
        db_path=db,
        resolution=resolution,
        visible_limit=3,
        cursor=None,
    )

    runtime = [
        statement
        for statement in statements
        if not statement.strip().lower().startswith("pragma schema_version")
    ]
    assert not any("EXPLAIN" in statement.upper() for statement in runtime)
    assert len(runtime) == page.sql_count
    assert page.sql_count <= 3 + len(resolution.member_ids)


def test_deep_continuation_skips_newer_rows_without_more_reported_work(tmp_path):
    from api.session_message_paging import (
        MessageCursorBoundary,
        read_state_db_message_page,
    )

    base = tmp_path / "base.db"
    deep = tmp_path / "deep.db"
    _make_db(base)
    _make_db(deep)
    with sqlite3.connect(base) as conn:
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, 'tip', 'user', ?, ?)",
            ((index, f"m{index}", float(index)) for index in range(1, 21)),
        )
    with sqlite3.connect(deep) as conn:
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, 'tip', 'user', ?, ?)",
            ((index, f"m{index}", float(index)) for index in range(1, 10_021)),
        )

    boundary = (MessageCursorBoundary("tip", 20.0, 20),)
    base_page = read_state_db_message_page(
        db_path=base,
        resolution=_resolution(base),
        visible_limit=10,
        cursor=boundary,
    )
    deep_page = read_state_db_message_page(
        db_path=deep,
        resolution=_resolution(deep),
        visible_limit=10,
        cursor=boundary,
    )

    assert deep_page.messages == base_page.messages
    assert [message["_state_db_message_id"] for message in deep_page.messages] == list(
        range(10, 20)
    )
    assert deep_page.raw_rows_examined == base_page.raw_rows_examined
    assert deep_page.sql_count == base_page.sql_count
    assert deep_page.query_plan_indexed is True


def test_integer_timestamp_cursor_preserves_values_above_float_precision(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db, timestamp_type="INTEGER")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (1, 'tip', 'user', 'newest tip', 9007199254740993)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (100, 'tip', 'user', 'older tip', 9007199254740992)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (200, 'root', 'user', 'root head', 9007199254740991)"
        )

    newest = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    older = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=10,
        cursor=newest.before_boundaries,
    )

    assert [message["_state_db_message_id"] for message in newest.messages] == [1]
    assert next(
        boundary for boundary in newest.before_boundaries
        if boundary.member_id == "tip"
    ).timestamp == 9007199254740993
    assert [message["_state_db_message_id"] for message in older.messages] == [
        200,
        100,
    ]


def test_reader_rejects_database_replaced_after_resolution(tmp_path):
    from api.session_message_paging import (
        MessagePagingUnavailable,
        read_state_db_message_page,
    )

    db = tmp_path / "state.db"
    replacement = tmp_path / "replacement.db"
    _make_db(db)
    resolution = _resolution(db)
    _make_db(replacement)
    _insert(replacement, 1, "tip", "user", "replacement", 1)
    replacement.replace(db)

    with pytest.raises(MessagePagingUnavailable, match="database_identity_changed"):
        read_state_db_message_page(
            db_path=db,
            resolution=resolution,
            visible_limit=1,
            cursor=None,
        )


@pytest.mark.parametrize(
    "value",
    [0, -1, 101, True, False, 1.0, "30", None],
)
def test_reader_rejects_visible_limit_outside_strict_integer_contract(
    tmp_path,
    value,
):
    from api.session_message_paging import (
        MessagePageValidationError,
        read_state_db_message_page,
    )

    db = tmp_path / "state.db"
    _make_db(db)
    with pytest.raises(MessagePageValidationError, match="visible_limit"):
        read_state_db_message_page(
            db_path=db,
            resolution=_resolution(db),
            visible_limit=value,
            cursor=None,
        )


@pytest.mark.parametrize("value", [1, 100])
def test_reader_accepts_visible_limit_boundaries(tmp_path, value):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, 1, "tip", "user", "visible", 1)

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=value,
        cursor=None,
    )

    assert page.visible_count == 1


def test_hidden_rows_exhaust_raw_budget_with_short_advancing_page(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, 'tip', 'tool', 'hidden', ?)",
            ((index, float(index)) for index in range(1, 401)),
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.messages == ()
    assert page.visible_count == 0
    assert page.raw_rows_examined == 256
    assert page.has_more is True
    assert page.before_boundaries


def test_unrelated_scale_does_not_change_target_work(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    base = tmp_path / "base.db"
    scaled = tmp_path / "scaled.db"
    _make_db(base)
    _make_db(scaled, unrelated=10_000, unrelated_messages=1_000_000)
    for path in (base, scaled):
        for message_id in range(1, 41):
            _insert(
                path,
                2_000_000 + message_id,
                "tip",
                "user",
                f"target-{message_id}",
                message_id,
            )

    base_page = read_state_db_message_page(
        db_path=base,
        resolution=_resolution(base),
        visible_limit=30,
        cursor=None,
    )
    scaled_page = read_state_db_message_page(
        db_path=scaled,
        resolution=_resolution(scaled),
        visible_limit=30,
        cursor=None,
    )

    assert scaled_page.messages == base_page.messages
    assert scaled_page.raw_rows_examined == base_page.raw_rows_examined
    assert scaled_page.sql_count == base_page.sql_count


def test_tool_pair_crossing_visible_boundary_uses_bounded_closure(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (10, 'tip', 'assistant', '', 10, ?) ",
            ('[{"id":"call-1","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (11, 'tip', 'assistant', 'visible boundary', 11)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (12, 'tip', 'tool', 'result', 12, 'call-1')"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["_state_db_message_id"] for message in page.messages] == [
        10,
        11,
        12,
    ]
    assert page.visible_count == 2
    assert page.tool_pair_status == "complete"
    assert page.raw_rows_examined <= 256 + 64
    assert page.closure_rows_examined <= 64
    assert page.ordinary_serialized_bytes <= 2 * 1024 * 1024
    assert page.closure_serialized_bytes <= 512 * 1024
    assert page.serialized_bytes <= 2_621_440


def test_complete_page_stops_before_unrelated_hidden_tool_result(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?)",
            ('[{"id":"call-older","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (2, 'tip', 'tool', 'older result', 2, 'call-older')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (3, 'tip', 'assistant', '', 3, ?)",
            ('[{"id":"call-newer","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (4, 'tip', 'tool', 'newer result', 4, 'call-newer')"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    continuation = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=page.before_boundaries,
    )

    assert page.mode == "cursor_v1"
    assert [
        message["_state_db_message_id"]
        for message in page.messages
    ] == [3, 4]
    assert page.has_more is True
    assert continuation.mode == "cursor_v1"
    assert [
        message["_state_db_message_id"]
        for message in continuation.messages
    ] == [1, 2]


@pytest.mark.parametrize("partner_rank", [257, 320])
def test_tool_closure_can_use_full_post_budget_allowance(tmp_path, partner_rank):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        rows = []
        for rank in range(1, partner_rank + 1):
            message_id = 1_000 - rank
            if rank == 1:
                role, content, tool_call_id, tool_calls = (
                    "tool",
                    "result",
                    "call-edge",
                    None,
                )
            elif rank == 256:
                role, content, tool_call_id, tool_calls = (
                    "user",
                    "visible boundary",
                    None,
                    None,
                )
            elif rank == partner_rank:
                role, content, tool_call_id, tool_calls = (
                    "assistant",
                    "",
                    None,
                    '[{"id":"call-edge","type":"function"}]',
                )
            else:
                role, content, tool_call_id, tool_calls = "", "hidden", None, None
            rows.append(
                (
                    message_id,
                    "tip",
                    role,
                    content,
                    message_id,
                    tool_call_id,
                    tool_calls,
                )
            )
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp, "
            "tool_call_id, tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["content"] for message in page.messages] == [
        "",
        "visible boundary",
        "result",
    ]
    assert page.raw_rows_examined == partner_rank
    assert page.closure_rows_examined == partner_rank - 256
    assert page.tool_pair_status == "complete"


def test_tool_closure_one_row_beyond_allowance_fails_closed(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    partner_rank = 321
    with sqlite3.connect(db) as conn:
        rows = []
        for rank in range(1, partner_rank + 1):
            message_id = 1_000 - rank
            if rank == 1:
                role, content, tool_call_id, tool_calls = (
                    "tool",
                    "result",
                    "call-too-far",
                    None,
                )
            elif rank == 256:
                role, content, tool_call_id, tool_calls = (
                    "user",
                    "visible boundary",
                    None,
                    None,
                )
            elif rank == partner_rank:
                role, content, tool_call_id, tool_calls = (
                    "assistant",
                    "",
                    None,
                    '[{"id":"call-too-far","type":"function"}]',
                )
            else:
                role, content, tool_call_id, tool_calls = "", "hidden", None, None
            rows.append(
                (
                    message_id,
                    "tip",
                    role,
                    content,
                    message_id,
                    tool_call_id,
                    tool_calls,
                )
            )
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp, "
            "tool_call_id, tool_calls) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "legacy_required"
    assert page.messages == ()
    assert page.fallback_reason == "tool_pair_outside_closure"
    assert page.raw_rows_examined <= 256 + 64


def test_tool_pair_with_older_result_uses_bounded_closure(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (9, 'tip', 'tool', 'older result', 9, 'call-1')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (10, 'tip', 'assistant', '', 10, ?)",
            ('[{"id":"call-1","type":"function"}]',),
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["_state_db_message_id"] for message in page.messages] == [9, 10]
    assert page.tool_pair_status == "complete"


def test_tool_closure_returns_every_crossed_visible_row_without_page_loss(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (96, 'tip', 'user', 'older page', 96)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (97, 'tip', 'assistant', '', 97, ?)",
            ('[{"id":"call-1","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (98, 'tip', 'user', 'must-not-drop', 98)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (99, 'tip', 'assistant', 'visible boundary', 99)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (100, 'tip', 'tool', 'result', 100, 'call-1')"
        )

    newest = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    older = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=newest.before_boundaries,
    )

    reconstructed = older.messages + newest.messages
    assert [message["_state_db_message_id"] for message in reconstructed] == [
        96,
        97,
        98,
        99,
        100,
    ]
    assert newest.tool_pair_status == "complete"


def test_tool_closure_recursively_completes_crossed_result_pairs(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (96, 'tip', 'assistant', '', 96, ?)",
            ('[{"id":"call-b","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (97, 'tip', 'assistant', '', 97, ?)",
            ('[{"id":"call-a","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (98, 'tip', 'tool', 'result-b', 98, 'call-b')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (99, 'tip', 'user', 'visible boundary', 99)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (100, 'tip', 'tool', 'result-a', 100, 'call-a')"
        )

    newest = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    older = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=newest.before_boundaries,
    )

    assert newest.mode == "cursor_v1"
    assert [message["_state_db_message_id"] for message in newest.messages] == [
        96,
        97,
        98,
        99,
        100,
    ]
    assert newest.tool_pair_status == "complete"
    assert older.mode == "cursor_v1"
    assert older.messages == ()


@pytest.mark.parametrize(
    ("cursor", "expected_mode"),
    [(None, "legacy_required"), ((), "cursor_restart_required")],
)
def test_duplicate_tool_results_fail_closed_without_dropping_rows(
    tmp_path,
    cursor,
    expected_mode,
):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, '[{\"id\":\"call-a\"}]')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (2, 'tip', 'tool', 'first', 2, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (3, 'tip', 'tool', 'second', 3, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (4, 'tip', 'user', 'final', 4)"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=2,
        cursor=cursor,
    )

    assert page.mode == expected_mode
    assert page.messages == ()
    assert page.fallback_reason == "ambiguous_tool_multiplicity"


def test_duplicate_tool_result_behind_first_partner_fails_closed(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (100, 'tip', 'assistant', '', 100, '[{\"id\":\"call-a\"}]')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (99, 'tip', 'tool', 'first', 99, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (98, 'tip', 'tool', 'second', 98, 'call-a')"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "legacy_required"
    assert page.messages == ()
    assert page.fallback_reason == "ambiguous_tool_multiplicity"


def test_duplicate_tool_calls_fail_closed(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        for message_id in (1, 2):
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
                "VALUES (?, 'tip', 'assistant', '', ?, '[{\"id\":\"call-a\"}]')",
                (message_id, message_id),
            )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (3, 'tip', 'tool', 'result', 3, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (4, 'tip', 'user', 'final', 4)"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=3,
        cursor=None,
    )

    assert page.mode == "legacy_required"
    assert page.messages == ()
    assert page.fallback_reason == "ambiguous_tool_multiplicity"


def test_inactive_duplicate_tool_result_is_ignored(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, '[{\"id\":\"call-a\"}]')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active, tool_call_id) "
            "VALUES (2, 'tip', 'tool', 'active', 2, 1, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active, tool_call_id) "
            "VALUES (3, 'tip', 'tool', 'inactive', 3, 0, 'call-a')"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (4, 'tip', 'user', 'final', 4)"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=2,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["_state_db_message_id"] for message in page.messages] == [1, 2, 4]


@pytest.mark.parametrize(("inactive_role", "inactive_id"), [("tool", 11), ("assistant", 10)])
def test_inactive_tool_pair_member_fails_closed(inactive_role, inactive_id, tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active, tool_calls) "
            "VALUES (10, 'tip', 'assistant', '', 10, ?, ?)",
            (0 if inactive_role == "assistant" else 1, '[{"id":"call-1"}]'),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active, tool_call_id) "
            "VALUES (11, 'tip', 'tool', 'result', 11, ?, 'call-1')",
            (0 if inactive_role == "tool" else 1,),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (12, 'tip', 'user', 'visible', 12)"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=2,
        cursor=None,
    )

    assert page.mode == "legacy_required"
    assert page.messages == ()
    assert page.fallback_reason == "tool_pair_outside_closure"


def test_tool_pair_outside_closure_allowance_returns_typed_fallback(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?) ",
            ('[{"id":"call-far","type":"function"}]',),
        )
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (?, 'tip', 'tool', 'filler', ?, ?)",
            ((index, index, f"orphan-{index}") for index in range(2, 68)),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (68, 'tip', 'assistant', 'visible boundary', 68)"
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (69, 'tip', 'tool', 'result', 69, 'call-far')"
        )

    initial = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    later = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=(),
    )

    assert initial.mode == "legacy_required"
    assert initial.messages == ()
    assert initial.fallback_reason == "tool_pair_outside_closure"
    assert later.mode == "cursor_restart_required"
    assert later.messages == ()


def test_large_tool_result_uses_bounded_representation(tmp_path, monkeypatch):
    import api.session_message_paging as paging

    original_payload = paging._message_page_row_payload
    original_open = paging.open_state_db_readonly
    projected_tool_lengths = []
    bytes_read_by_field = {}

    class CountingBlob:
        def __init__(self, blob, field):
            self._blob = blob
            self._field = field

        def __enter__(self):
            self._blob.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._blob.__exit__(exc_type, exc_value, traceback)

        def __len__(self):
            return len(self._blob)

        def read(self, size=-1):
            data = self._blob.read(size)
            bytes_read_by_field[self._field] = (
                bytes_read_by_field.get(self._field, 0) + len(data)
            )
            return data

    class CountingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

        def blobopen(self, table, column, rowid, **kwargs):
            return CountingBlob(
                self._connection.blobopen(table, column, rowid, **kwargs),
                column,
            )

    def checked_payload(row, selected_optional):
        if str(row["role"]).strip().lower() == "tool":
            projected_tool_lengths.append(len(str(row["content"] or "")))
        return original_payload(row, selected_optional)

    monkeypatch.setattr(paging, "_message_page_row_payload", checked_payload)
    monkeypatch.setattr(
        paging,
        "open_state_db_readonly",
        lambda path, log=None: CountingConnection(original_open(path, log=log)),
    )

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?)",
            ('[{"id":"call-large","type":"function"}]',),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (2, 'tip', ' TOOL ', ?, 2, 'call-large')",
            ("x" * (8 * 1024 * 1024),),
        )

    page = paging.read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    tool = page.messages[-1]
    assert tool["role"] == " TOOL "
    assert tool["_content_truncated"] is True
    assert len(tool["content"]) < 16 * 1024
    assert projected_tool_lengths == [4097]
    assert bytes_read_by_field["content"] <= paging._TOOL_CONTENT_PROJECTION_BYTES
    assert page.closure_serialized_bytes <= 512 * 1024


def test_batched_tool_calls_with_bounded_pairing_metadata_remain_pageable(
    tmp_path,
):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    calls = [
        {
            "id": f"call_{index:024d}",
            "call_id": f"call_{index:024d}",
            "response_item_id": f"item_{index:024d}",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "x" * 128}),
            },
        }
        for index in range(9)
    ]
    serialized_calls = json.dumps(calls)
    assert 1024 < len(serialized_calls) <= 4096
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?)",
            (serialized_calls,),
        )
        conn.executemany(
            "INSERT INTO messages("
            "id, session_id, role, content, timestamp, tool_call_id"
            ") VALUES (?, 'tip', 'tool', 'ok', ?, ?)",
            (
                (index + 2, index + 2, call["id"])
                for index, call in enumerate(calls)
            ),
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=10,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert page.fallback_reason is None
    assert page.tool_pair_status == "complete"


def test_large_realistic_tool_batch_stays_within_pairing_budget(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    calls = [
        {
            "id": f"call_{index:024d}",
            "call_id": f"call_{index:024d}",
            "response_item_id": f"item_{index:024d}",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "x" * 2048}),
            },
        }
        for index in range(9)
    ]
    serialized_calls = json.dumps(calls)
    assert 16 * 1024 < len(serialized_calls) <= 32 * 1024
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?)",
            (serialized_calls,),
        )
        conn.executemany(
            "INSERT INTO messages("
            "id, session_id, role, content, timestamp, tool_call_id"
            ") VALUES (?, 'tip', 'tool', 'ok', ?, ?)",
            (
                (index + 2, index + 2, call["id"])
                for index, call in enumerate(calls)
            ),
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=10,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert page.fallback_reason is None
    assert page.tool_pair_status == "complete"


def test_oversized_pairing_metadata_uses_only_bounded_blob_reads(
    tmp_path,
    monkeypatch,
):
    import api.session_message_paging as paging

    original_open = paging.open_state_db_readonly
    bytes_read_by_field = {}
    statements = []

    class CountingBlob:
        def __init__(self, blob, field):
            self._blob = blob
            self._field = field

        def __enter__(self):
            self._blob.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._blob.__exit__(exc_type, exc_value, traceback)

        def __len__(self):
            return len(self._blob)

        def read(self, size=-1):
            data = self._blob.read(size)
            bytes_read_by_field[self._field] = (
                bytes_read_by_field.get(self._field, 0) + len(data)
            )
            return data

    class CountingConnection:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

        def blobopen(self, table, column, rowid, **kwargs):
            return CountingBlob(
                self._connection.blobopen(table, column, rowid, **kwargs),
                column,
            )

        def execute(self, statement, params=()):
            statements.append(str(statement))
            return self._connection.execute(statement, params)

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', '', 1, ?)",
            ('[{"id":"call-large"}]' + ("x" * (8 * 1024 * 1024)),),
        )
    monkeypatch.setattr(
        paging,
        "open_state_db_readonly",
        lambda path, log=None: CountingConnection(original_open(path, log=log)),
    )

    page = paging.read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "legacy_required"
    assert page.fallback_reason == "pairing_metadata_budget"
    assert 0 < bytes_read_by_field["tool_calls"]
    assert bytes_read_by_field["tool_calls"] <= (
        (paging._PAIRING_TOOL_CALLS_MAX_CHARS + 1) * 4
    )
    assert not any("substr(" in statement.lower() for statement in statements)
    assert not any("length(" in statement.lower() for statement in statements)


@pytest.mark.parametrize(
    ("role", "expected_mode"),
    [("🧪" * 32, "cursor_v1"), ("🧪" * 33, "legacy_required")],
)
def test_pairing_role_character_limit_is_unicode_exact(
    tmp_path,
    role,
    expected_mode,
):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, 1, "tip", role, "body", 1)

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == expected_mode
    if expected_mode == "cursor_v1":
        assert page.messages[0]["role"] == role
    else:
        assert page.fallback_reason == "pairing_metadata_budget"


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_oversized_ordinary_page_uses_bounded_incomplete_representation(
    tmp_path,
    monkeypatch,
    role,
):
    import api.session_message_paging as paging

    original_read = paging._read_message_text_blob
    content_read_limits = []

    def checked_read(conn, *, message_id, field, value_type, byte_limit=None):
        if field == "content":
            content_read_limits.append(byte_limit)
        return original_read(
            conn,
            message_id=message_id,
            field=field,
            value_type=value_type,
            byte_limit=byte_limit,
        )

    monkeypatch.setattr(paging, "_read_message_text_blob", checked_read)
    monkeypatch.setattr(
        paging,
        "_message_page_row_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized payload must not materialize")
        ),
    )

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, 1, "tip", role, "x" * (2 * 1024 * 1024 + 1), 1)

    page = paging.read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert len(page.messages) == 1
    message = page.messages[0]
    assert message["role"] == role
    assert message["_content_truncated"] is True
    assert message["_content_complete"] is False
    assert message["_content_truncation_reason"] == "cursor_page_budget"
    assert message["_content_original_bytes"] == 2 * 1024 * 1024 + 1
    assert len(message["content"]) < 16 * 1024
    assert content_read_limits == [paging._ORDINARY_CONTENT_PROJECTION_BYTES]
    assert page.has_more is False
    assert page.serialized_bytes <= 2 * 1024 * 1024


def test_oversized_ordinary_preview_advances_cursor_without_losing_older_row(
    tmp_path,
):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    _insert(db, 1, "tip", "user", "older", 1)
    _insert(db, 2, "tip", "assistant", "x" * (2 * 1024 * 1024 + 1), 2)

    newest = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )
    older = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=newest.before_boundaries,
    )

    assert [
        message["_state_db_message_id"]
        for message in older.messages + newest.messages
    ] == [1, 2]
    assert newest.messages[0]["_content_complete"] is False


def test_escape_heavy_single_message_uses_bounded_preview(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    content = "\x01" * 400_000
    _insert(db, 1, "tip", "assistant", content, 1)

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert page.messages[0]["_content_complete"] is False
    assert page.messages[0]["_content_original_bytes"] == len(content)
    assert page.serialized_bytes <= 2 * 1024 * 1024


def test_oversized_assistant_tool_call_keeps_pair_complete(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_calls) "
            "VALUES (1, 'tip', 'assistant', ?, 1, '[{\"id\":\"call-a\"}]')",
            ("x" * (2 * 1024 * 1024 + 1),),
        )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, timestamp, tool_call_id) "
            "VALUES (2, 'tip', 'tool', 'result', 2, 'call-a')"
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=1,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert [message["_state_db_message_id"] for message in page.messages] == [1, 2]
    assert page.messages[0]["_content_complete"] is False
    assert page.messages[0]["tool_calls"][0]["id"] == "call-a"
    assert page.tool_pair_status == "complete"


def test_ordinary_payload_budget_returns_advancing_short_pages(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, 'tip', 'assistant', ?, ?)",
            ((index, "x" * 50_000, index) for index in range(1, 101)),
        )
    cursor = None
    reconstructed = ()
    for _page_number in range(10):
        page = read_state_db_message_page(
            db_path=db,
            resolution=_resolution(db),
            visible_limit=100,
            cursor=cursor,
        )
        assert page.mode == "cursor_v1"
        assert page.messages
        assert page.serialized_bytes <= 2 * 1024 * 1024
        reconstructed = page.messages + reconstructed
        if not page.has_more:
            break
        assert page.before_boundaries != cursor
        cursor = page.before_boundaries
    else:
        raise AssertionError("short pages did not terminate")

    assert [message["_state_db_message_id"] for message in reconstructed] == list(
        range(1, 101)
    )


def test_raw_payload_below_limit_does_not_use_inflated_fallback(tmp_path):
    from api.session_message_paging import read_state_db_message_page

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, 'tip', 'assistant', ?, ?)",
            ((index, "x" * 400_000, index) for index in range(1, 5)),
        )

    page = read_state_db_message_page(
        db_path=db,
        resolution=_resolution(db),
        visible_limit=4,
        cursor=None,
    )

    assert page.mode == "cursor_v1"
    assert len(page.messages) == 4
    assert page.serialized_bytes < 2 * 1024 * 1024
