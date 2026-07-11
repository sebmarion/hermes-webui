"""Delegate children stay addressable but do not pollute the default sidebar."""
import json
import sqlite3
from pathlib import Path

from api.agent_sessions import read_importable_agent_session_rows
from api.models import _hide_from_default_sidebar, _preserve_messageful_sidebar_discoverability

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, session_source TEXT, model_config TEXT,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
    """)
    rows = [
        ("parent", "Parent", 10.0, "tui", None, None),
        ("normal", "Normal", 20.0, "tui", None, None),
        ("padded-child", "Padded child", 200.0, " subagent ", None, "parent"),
    ]
    rows.extend(
        (f"child-{i}", f"Child {i}", 100.0 + i, "subagent",
         json.dumps({"_delegate_from": "parent"}), "parent")
        for i in range(12)
    )
    conn.executemany(
        "INSERT INTO sessions (id,title,model,message_count,started_at,source,model_config,parent_session_id) VALUES (?,?,NULL,1,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO messages (session_id,role,timestamp) VALUES (?,'user',?)",
        [(row[0], row[2]) for row in rows],
    )
    conn.commit()
    conn.close()


def test_delegate_children_do_not_consume_candidate_limit(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    rows = read_importable_agent_session_rows(db, limit=2, exclude_sources=("webui",))
    assert [row["id"] for row in rows] == ["normal", "parent"]
    assert all(row.get("delegate_from") in (None, "") for row in rows)


def test_delegate_children_remain_available_to_explicit_diagnostic_call(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    rows = read_importable_agent_session_rows(
        db, limit=2, exclude_sources=("webui",), include_children=True
    )
    assert [row["id"] for row in rows] == ["padded-child", "child-11"]
    assert rows[0]["delegate_from"] is None
    assert rows[1]["delegate_from"] == "parent"


def test_model_layer_defensively_hides_materialized_delegate_rows():
    assert _hide_from_default_sidebar({"session_id": "c", "source_tag": "subagent"}) is True
    assert _hide_from_default_sidebar({
        "session_id": "c", "source_tag": "cli", "raw_source": " subagent "
    }) is True
    assert _hide_from_default_sidebar({"session_id": "c", "source": "webui", "delegate_from": "p"}) is True
    assert _hide_from_default_sidebar({"session_id": "p", "source": "webui"}) is False
    assert _hide_from_default_sidebar({"session_id": "continuation", "source": "tui", "parent_session_id": "p"}) is False


def test_discoverability_rescue_never_reintroduces_delegate_child():
    child = {
        "session_id": "orphan-child",
        "source": "webui",
        "delegate_from": "missing-parent",
        "message_count": 4,
    }
    assert _preserve_messageful_sidebar_discoverability([child], []) == []


def test_discoverability_rescue_keeps_generic_parented_lineage_eligible():
    continuation = {
        "session_id": "continuation",
        "source": "webui",
        "parent_session_id": "missing-parent",
        "message_count": 4,
    }
    rescued = _preserve_messageful_sidebar_discoverability([continuation], [])
    assert [row["session_id"] for row in rescued] == ["continuation"]


def test_explicit_subagent_menu_has_no_archive_action():
    start = SESSIONS_JS.index("function _openSessionActionMenu(session, anchorEl){")
    end = SESSIONS_JS.index("document.addEventListener('click'", start)
    block = SESSIONS_JS[start:end]
    assert "const isSubagentSession = _isSubagentSession(session);" in block
    assert "if(isReadOnly||isSubagentSession){" in block
    assert "if(!isSubagentSession){" in block
    assert block.index("if(!isSubagentSession){") < block.index("await _archiveSession(session,!session.archived);")
