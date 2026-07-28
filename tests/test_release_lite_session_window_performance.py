import sqlite3
import time
from dataclasses import replace
from types import MappingProxyType

from api.agent_sessions import SharedSessionResolution, shared_state_db_identity
from api.session_message_paging import read_state_db_message_page
from api.session_window import SessionWindowRequest, build_session_window
from tests.test_release_lite_session_window import _ready_dependencies


LINEAGE_SEGMENTS = 45
TOTAL_ROWS = 42_632
TOOL_CALL_ROWS = 21_993
UNRELATED_SESSIONS = 10_000


def _create_scale_db(path, *, include_unrelated):
    members = tuple(f"segment-{index:02d}" for index in range(LINEAGE_SEGMENTS))
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                title TEXT,
                model TEXT,
                cwd TEXT
            );
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp);
            """
        )
        conn.executemany(
            "INSERT INTO sessions(id, parent_session_id, title, model, cwd) "
            "VALUES (?, ?, 'Scale task', 'model', '/workspace')",
            (
                (
                    member,
                    None if index == 0 else members[index - 1],
                )
                for index, member in enumerate(members)
            ),
        )
        conn.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp) "
            "VALUES (?, ?, 'user', ?, ?)",
            (
                (
                    index + 1,
                    member,
                    f"target-{index:02d}",
                    float(index + 1),
                )
                for index, member in enumerate(members)
            ),
        )
        if include_unrelated:
            conn.executemany(
                "INSERT INTO sessions(id, title) VALUES (?, 'Noise')",
                ((f"other-{index}",) for index in range(UNRELATED_SESSIONS)),
            )
            unrelated_rows = TOTAL_ROWS - LINEAGE_SEGMENTS
            conn.executemany(
                "INSERT INTO messages("
                "id, session_id, role, content, timestamp, tool_calls"
                ") VALUES (?, ?, 'assistant', 'noise', ?, ?)",
                (
                    (
                        LINEAGE_SEGMENTS + index + 1,
                        f"other-{index % UNRELATED_SESSIONS}",
                        float(index + 1),
                        '[{"id":"noise-call","type":"function"}]'
                        if index < TOOL_CALL_ROWS
                        else None,
                    )
                    for index in range(unrelated_rows)
                ),
            )
    return members


def _create_large_target_db(path):
    members = tuple(f"segment-{index:02d}" for index in range(LINEAGE_SEGMENTS))
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                title TEXT,
                model TEXT,
                cwd TEXT
            );
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL,
                active INTEGER NOT NULL DEFAULT 1,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            CREATE INDEX idx_messages_session
                ON messages(session_id, timestamp);
            """
        )
        conn.executemany(
            "INSERT INTO sessions(id, parent_session_id, title, model, cwd) "
            "VALUES (?, ?, 'Huge target', 'model', '/workspace')",
            (
                (
                    member,
                    None if index == 0 else members[index - 1],
                )
                for index, member in enumerate(members)
            ),
        )

        rows = []
        message_id = 1
        unmatched_calls = TOOL_CALL_ROWS - (TOTAL_ROWS - TOOL_CALL_ROWS)
        for call_index in range(unmatched_calls):
            member = members[call_index % LINEAGE_SEGMENTS]
            call_id = f"call-unmatched-{call_index}"
            rows.append(
                (
                    message_id,
                    member,
                    "assistant",
                    f"tool call {call_index}",
                    float(message_id),
                    None,
                    f'[{{"id":"{call_id}","type":"function"}}]',
                )
            )
            message_id += 1
        paired_calls = TOTAL_ROWS - TOOL_CALL_ROWS
        for call_index in range(paired_calls):
            member = members[call_index % LINEAGE_SEGMENTS]
            call_id = f"call-{call_index}"
            rows.append(
                (
                    message_id,
                    member,
                    "assistant",
                    f"tool call {call_index}",
                    float(message_id),
                    None,
                    f'[{{"id":"{call_id}","type":"function"}}]',
                )
            )
            message_id += 1
            rows.append(
                (
                    message_id,
                    member,
                    "tool",
                    f"tool result {call_index}",
                    float(message_id),
                    call_id,
                    None,
                )
            )
            message_id += 1
        assert len(rows) == TOTAL_ROWS
        conn.executemany(
            "INSERT INTO messages("
            "id, session_id, role, content, timestamp, tool_call_id, tool_calls"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return members


def _resolution(path, members):
    canonical = members[-1]
    return SharedSessionResolution(
        requested_id=members[0],
        canonical_id=canonical,
        root_id=members[0],
        tip_id=canonical,
        member_ids=members,
        canonical_row=MappingProxyType(
            {
                "id": canonical,
                "title": "Scale task",
                "model": "model",
                "cwd": "/workspace",
            }
        ),
        lineage_fingerprint="sha256:" + ("b" * 64),
        global_projection_generation_hint=1,
        mode="navigation",
        status="found",
        database_identity=shared_state_db_identity(path),
    )


def _read(path, members):
    started = time.monotonic()
    page = read_state_db_message_page(
        db_path=path,
        resolution=_resolution(path, members),
        visible_limit=30,
        cursor=None,
    )
    return page, time.monotonic() - started


def test_large_fixture_keeps_target_page_work_structurally_bounded(tmp_path):
    base = tmp_path / "base.db"
    scaled = tmp_path / "scaled.db"
    base_members = _create_scale_db(base, include_unrelated=False)
    scaled_members = _create_scale_db(scaled, include_unrelated=True)

    base_page, base_elapsed = _read(base, base_members)
    scaled_page, scaled_elapsed = _read(scaled, scaled_members)

    assert [row["_state_db_message_id"] for row in scaled_page.messages] == [
        row["_state_db_message_id"] for row in base_page.messages
    ]
    assert scaled_page.visible_count == base_page.visible_count == 30
    assert scaled_page.raw_rows_examined == base_page.raw_rows_examined
    assert scaled_page.sql_count == base_page.sql_count
    assert scaled_page.query_plan_indexed is base_page.query_plan_indexed is True
    assert scaled_page.raw_rows_examined <= 576
    assert scaled_elapsed < max(5.0, base_elapsed * 50)

    with sqlite3.connect(scaled) as conn:
        total_rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        tool_rows = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tool_calls IS NOT NULL"
        ).fetchone()[0]
        unrelated_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id LIKE 'other-%'"
        ).fetchone()[0]
    assert total_rows == TOTAL_ROWS
    assert tool_rows == TOOL_CALL_ROWS
    assert unrelated_sessions == UNRELATED_SESSIONS


def test_large_fixture_service_never_enters_legacy_reconstruction(tmp_path):
    db = tmp_path / "huge-target.db"
    members = _create_large_target_db(db)
    resolution = _resolution(db, members)
    direct_page = read_state_db_message_page(
        db_path=db,
        resolution=resolution,
        visible_limit=30,
        cursor=None,
    )
    assert len(direct_page.messages) <= 158, (
        len(direct_page.messages),
        direct_page.visible_count,
        direct_page.raw_rows_examined,
    )
    diagnostics = []
    ticks = iter((10.0, 10.05, 10.06))
    deps = replace(
        _ready_dependencies(),
        state_db_path=lambda _profile: db,
        resolve_shared_session=lambda _path, _sid: resolution,
        read_state_db_message_page=read_state_db_message_page,
        capture_metadata=lambda _profile, _resolution: {
            "session_id": members[-1],
            "read_only": False,
            "model_provider": "provider",
        },
        diagnostic_sink=diagnostics.append,
        monotonic=ticks.__next__,
    )

    result = build_session_window(
        SessionWindowRequest(members[0], 30, None, False),
        deps=deps,
    )

    assert result["conversation_window"]["state"] == "ready", (
        result["conversation_window"],
        diagnostics,
    )
    assert 30 <= len(result["messages"]) <= 158
    assert any(message.get("tool_calls") for message in result["messages"])
    assert any(message.get("role") == "tool" for message in result["messages"])
    stable_ids = [
        message["_state_db_message_id"] for message in result["messages"]
    ]
    assert stable_ids == sorted(stable_ids)
    assert stable_ids[-1] == TOTAL_ROWS
    assert diagnostics[0]["lineage_depth"] == LINEAGE_SEGMENTS
    assert diagnostics[0]["raw_rows_examined"] <= 576
    assert diagnostics[0]["visible_rows"] == len(result["messages"])
    assert diagnostics[0]["serialized_bytes"] <= 2_621_440
    assert diagnostics[0]["state_db_read_ms"] == 60
    assert "message_count" not in result

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == TOTAL_ROWS
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tool_calls IS NOT NULL"
        ).fetchone()[0] == TOOL_CALL_ROWS
