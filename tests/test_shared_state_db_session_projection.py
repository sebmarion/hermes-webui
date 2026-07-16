import sqlite3
import time

import pytest


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
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
            last_activity_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        """
    )
    rows = [
        ("root", "webui", "Shared title", "root-model", None, 10, 20, "compression", None, 4, "/root", 1, 20),
        ("tip", "webui", None, "tip-model", None, 21, None, None, "root", 2, "/workspace", 0, 40),
        ("branch", "webui", "Branch", "branch-model", '{"_branched_from":"root"}', 22, None, None, "root", 1, "/branch", 0, 30),
        ("delegate", "subagent", "Delegate", "delegate-model", '{"_delegate_from":"root"}', 23, None, None, "root", 1, "/delegate", 0, 50),
        ("tool", "tool", "Tool", "tool-model", None, 24, None, None, "root", 1, "/tool", 0, 60),
        ("cross", "cli", "Cross source", "cli-model", None, 25, None, None, "root", 1, "/cli", 0, 70),
        ("tui-row", "tui", "TUI conversation", "tui-model", None, 26, None, None, None, 1, "/tui", 1, 80),
        ("acp-row", "acp", "ACP conversation", "acp-model", None, 27, None, None, None, 1, "/acp", 0, 90),
        ("cron-row", "cron", "Cron run", "cron-model", None, 28, None, None, None, 1, "/cron", 0, 100),
    ]
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, 'user', ?, ?)",
        [
            (1, "root", "old", 20),
            (2, "tip", "new", 40),
            (3, "branch", "branch", 30),
            (4, "delegate", "delegate", 50),
            (5, "tool", "tool", 60),
            (6, "cross", "cross", 70),
            (7, "tui-row", "tui", 80),
            (8, "acp-row", "acp", 90),
            (9, "cron-row", "cron", 100),
        ],
    )
    conn.commit()
    conn.close()


def test_shared_projection_collapses_only_valid_same_source_continuations(tmp_path):
    from api.agent_sessions import read_shared_session_rows

    db = tmp_path / "state.db"
    _make_db(db)

    rows = read_shared_session_rows(db, source="webui")

    assert [row["id"] for row in rows] == ["tip", "branch"]
    tip = next(row for row in rows if row["id"] == "tip")
    assert tip["_lineage_root_id"] == "root"
    assert tip["_lineage_tip_id"] == "tip"
    assert tip["message_count"] == 2
    assert tip["last_activity"] == 40
    assert tip["cwd"] == "/workspace"
    assert tip["archived"] is False


def test_shared_projection_prefers_continuation_tip_over_late_root_activity(tmp_path):
    from api.agent_sessions import read_shared_session_rows

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        # A delayed write can leave the compressed root with a newer activity
        # timestamp than its physical continuation. The visible id must still
        # be the valid continuation tip.
        conn.execute("UPDATE sessions SET last_activity_at = 100 WHERE id = 'root'")
        conn.commit()

    rows = read_shared_session_rows(db, source="webui")

    tip = next(row for row in rows if row["id"] == "tip")
    assert tip["_lineage_root_id"] == "root"
    assert tip["_lineage_tip_id"] == "tip"
    assert all(row["id"] != "root" for row in rows)


def test_shared_projection_uses_tip_title_when_continuation_renamed(tmp_path):
    from api.agent_sessions import read_shared_session_rows

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET title = 'Tip title' WHERE id = 'tip'")
        conn.commit()

    rows = read_shared_session_rows(db, source="webui")

    tip = next(row for row in rows if row["id"] == "tip")
    assert tip["title"] == "Tip title"


@pytest.mark.parametrize("source", ["webui", "tui"])
def test_shared_projection_uses_root_title_for_generated_continuation_suffix(
    tmp_path,
    source,
):
    from api.agent_sessions import read_shared_session_rows

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE sessions SET source = ? WHERE id IN ('root', 'tip')",
            (source,),
        )
        conn.execute("UPDATE sessions SET title = 'Shared title #2' WHERE id = 'tip'")
        conn.commit()

    rows = read_shared_session_rows(db, source=source)

    tip = next(row for row in rows if row["id"] == "tip")
    assert tip["title"] == "Shared title"


def test_shared_projection_preserves_non_compression_children(tmp_path):
    from api.agent_sessions import read_shared_session_rows

    db = tmp_path / "state.db"
    _make_db(db)

    rows = read_shared_session_rows(db, source=None)

    ids = {row["id"] for row in rows}
    assert {"tip", "branch", "delegate", "tool", "cross"}.issubset(ids)
    assert "root" not in ids


def test_shared_projection_reads_all_interactive_sources_without_background_rows(tmp_path):
    from api.agent_sessions import (
        SHARED_INTERACTIVE_SESSION_SOURCES,
        read_shared_session_rows,
    )

    db = tmp_path / "state.db"
    _make_db(db)

    rows = read_shared_session_rows(
        db,
        sources=SHARED_INTERACTIVE_SESSION_SOURCES,
    )

    sources = {str(row.get("source") or "").lower() for row in rows}
    assert {"webui", "cli", "tui", "acp"}.issubset(sources)
    assert not sources & {"cron", "webhook", "tool", "subagent"}


def test_shared_projection_resolves_old_compression_id_to_tip(tmp_path):
    from api.agent_sessions import resolve_shared_session_id

    db = tmp_path / "state.db"
    _make_db(db)

    assert resolve_shared_session_id(db, "root") == "tip"
    assert resolve_shared_session_id(db, "tip") == "tip"
    assert resolve_shared_session_id(db, "branch") == "branch"


def test_shared_pin_is_stored_only_on_logical_lineage_root(tmp_path, monkeypatch):
    import api.state_sync as state_sync

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
        )
        # Reproduce the stale duplicate bits written by older WebUI builds.
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id IN ('root', 'tip')")
        conn.commit()

    class SQLiteStateDB:
        def __init__(self, path):
            self.conn = sqlite3.connect(path)

        def ensure_session(self, **_kwargs):
            return None

        def set_session_pinned(self, session_id, pinned):
            self.conn.execute(
                "UPDATE sessions SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, session_id),
            )
            self.conn.commit()

        def _execute_write(self, callback):
            result = callback(self.conn)
            self.conn.commit()
            return result

        def close(self):
            return None

    state_db = SQLiteStateDB(db_path)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: state_db)

    state_sync.sync_session_pinned("tip", True, profile="default")

    with sqlite3.connect(db_path) as conn:
        pins = dict(conn.execute("SELECT id, pinned FROM sessions").fetchall())
    assert pins["root"] == 1
    assert pins["tip"] == 0
    assert pins["branch"] == 0

    state_sync.sync_session_pinned("tip", False, profile="default")

    with sqlite3.connect(db_path) as conn:
        pins = dict(conn.execute("SELECT id, pinned FROM sessions").fetchall())
    assert pins["root"] == 0
    assert pins["tip"] == 0
    assert pins["branch"] == 0


def test_shared_metadata_writeback_does_not_depend_on_insights_toggle(monkeypatch):
    import api.state_sync as state_sync

    class FakeDB:
        def __init__(self):
            self.calls = []

        def ensure_session(self, **kwargs):
            self.calls.append(("ensure", kwargs))

        def update_token_counts(self, **kwargs):
            self.calls.append(("usage", kwargs))

        def set_session_title(self, *args):
            self.calls.append(("title", args))

        def update_session_cwd(self, *args):
            self.calls.append(("cwd", args))

        def set_session_archived(self, *args):
            self.calls.append(("archived", args))

        def set_session_pinned(self, *args):
            self.calls.append(("pinned", args))

        def _execute_write(self, callback):
            self.calls.append(("message_count", True))
            callback(type("Conn", (), {"execute": lambda *_args: None})())

        def close(self):
            self.calls.append(("close", True))

    db = FakeDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    state_sync.sync_session_usage(
        "sid",
        title="Renamed",
        cwd="/workspace",
        archived=True,
        pinned=True,
        message_count=3,
    )

    assert any(kind == "cwd" and args == ("sid", "/workspace") for kind, args in db.calls)
    assert any(kind == "archived" and args == ("sid", True) for kind, args in db.calls)
    assert any(kind == "title" and args == ("sid", "Renamed") for kind, args in db.calls)
    assert any(kind == "pinned" and args == ("sid", True) for kind, args in db.calls)


def test_shared_title_backfill_does_not_touch_usage(monkeypatch):
    import api.state_sync as state_sync

    class FakeDB:
        def __init__(self):
            self.calls = []

        def ensure_session(self, **kwargs):
            self.calls.append(("ensure", kwargs))

        def _execute_write(self, callback):
            self.calls.append(("lineage", True))
            class Conn:
                def execute(inner, sql, params=()):
                    self.calls.append(("title", (sql, params)))
                    class Result:
                        rowcount = 1

                        def fetchall(result):
                            if sql.startswith("PRAGMA table_info"):
                                return [
                                    (0, "id"),
                                    (1, "parent_session_id"),
                                    (2, "end_reason"),
                                    (3, "source"),
                                    (4, "title"),
                                ]
                            if sql.startswith("SELECT id, archived"):
                                return []
                            return [("sid",)]

                    return Result()

            callback(Conn())

        def close(self):
            self.calls.append(("close", True))

    db = FakeDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    state_sync.sync_session_title("sid", "Canonical title")

    assert any(
        call[0] == "title" and call[1][1] == ("Canonical title", "sid")
        for call in db.calls
    )
    assert not any(kind == "usage" for kind, _args in db.calls)


def test_shared_projection_exposes_canonical_pin_from_state_db(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'tip'")
        conn.commit()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")

    rows, _legacy = models.shared_webui_sidebar_projection(
        [{"session_id": "tip", "title": "sidecar", "message_count": 2}],
        profile="default",
    )

    tip = next(row for row in rows if row["session_id"] == "tip")
    assert tip["pinned"] is True


def test_sidebar_projection_does_not_repin_after_state_db_unpin(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # The migration marker means legacy sidecar pins have already been copied
    # into state.db. A later Hermes One unpin must remain authoritative even
    # while the old sidecar still contains its historical pin bit.
    (tmp_path / ".webui-shared-pins-state-db-v2.migrated").touch()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")

    rows, _legacy = models.shared_webui_sidebar_projection(
        [{"session_id": "tip", "title": "sidecar", "message_count": 2, "pinned": True}],
        profile="default",
    )

    tip = next(row for row in rows if row["session_id"] == "tip")
    assert tip["pinned"] is False


def test_sidebar_projection_overlays_fresh_shared_activity(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        now = time.time()
        conn.executescript(
            """
            CREATE TABLE session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO session_activity VALUES (?, ?, ?, ?, ?, ?)",
            ("tip", "run-1", "webui", "tool", now - 5, now),
        )
        conn.commit()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(models, "_active_stream_ids", lambda: {"stream-1"})

    rows, _legacy = models.shared_webui_sidebar_projection(
        [{"session_id": "tip", "title": "sidecar", "message_count": 2}],
        profile="default",
    )

    tip = next(row for row in rows if row["session_id"] == "tip")
    assert tip["is_working"] is True
    assert tip["activity_phase"] == "tool"
    assert tip["activity_heartbeat_at"] == now



def test_shared_archive_mutation_writes_state_db_without_usage_sync(monkeypatch):
    import api.state_sync as state_sync

    class FakeDB:
        def __init__(self):
            self.calls = []

        def ensure_session(self, **kwargs):
            self.calls.append(("ensure", kwargs))

        def set_session_archived(self, *args):
            self.calls.append(("archived", args))

        def _execute_write(self, callback):
            self.calls.append(("lineage", True))
            callback(type("Conn", (), {"execute": lambda *_args: None})())

        def close(self):
            self.calls.append(("close", True))

    db = FakeDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    state_sync.sync_session_archived("sid", True, profile="default")

    assert any(kind == "archived" and args == ("sid", True) for kind, args in db.calls)
    assert any(kind == "lineage" for kind, _args in db.calls)


def test_shared_metadata_mutation_does_not_zero_usage(monkeypatch):
    import api.state_sync as state_sync

    class FakeDB:
        def __init__(self):
            self.calls = []

        def ensure_session(self, **kwargs):
            self.calls.append(("ensure", kwargs))

        def update_session_cwd(self, *args):
            self.calls.append(("cwd", args))

        def set_session_archived(self, *args):
            self.calls.append(("archived", args))

        def set_session_title(self, *args):
            self.calls.append(("title", args))

        def update_token_counts(self, **kwargs):
            self.calls.append(("usage", kwargs))

        def _execute_write(self, callback):
            self.calls.append(("lineage", True))
            callback(type("Conn", (), {"execute": lambda *_args: None})())

        def close(self):
            self.calls.append(("close", True))

    db = FakeDB()
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: db)

    persisted = state_sync.sync_session_metadata(
        "sid",
        title="Renamed",
        cwd="/workspace",
        archived=True,
        pinned=True,
        profile="default",
    )

    assert any(kind == "cwd" and args == ("sid", "/workspace") for kind, args in db.calls)
    assert any(kind == "archived" and args == ("sid", True) for kind, args in db.calls)
    assert any(kind == "title" and args == ("sid", "Renamed") for kind, args in db.calls)
    assert not any(kind == "usage" for kind, _args in db.calls)
    assert persisted is True


def test_shared_metadata_mutation_reports_unavailable_state_db(monkeypatch):
    import api.state_sync as state_sync

    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None: None)

    assert state_sync.sync_session_metadata("sid", title="Renamed") is False


def test_sidebar_projection_is_state_db_first_and_keeps_legacy_archive(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(models, "_active_stream_ids", lambda: {"stream-1"})

    rows, legacy = models.shared_webui_sidebar_projection(
        [
            {
                "session_id": "tip",
                "title": "stale sidecar title",
                "workspace": "/sidecar",
                "message_count": 99,
                "archived": True,
                "active_stream_id": "stream-1",
            },
            {
                "session_id": "legacy",
                "title": "Old archived chat",
                "message_count": 2,
                "archived": True,
                "updated_at": 5,
            },
            {
                "session_id": "empty",
                "title": "Empty",
                "message_count": 0,
                "archived": True,
            },
        ],
        profile="default",
    )

    tip = next(row for row in rows if row["session_id"] == "tip")
    assert tip["id"] == "tip"
    assert tip["source"] == "webui"
    assert tip["title"] == "Shared title"
    assert tip["message_count"] == 2
    assert tip["workspace"] == "/workspace"
    assert tip["archived"] is False
    assert tip["active_stream_id"] == "stream-1"
    assert [row["session_id"] for row in legacy] == ["legacy"]


def test_interactive_sidebar_projection_preserves_sources_and_canonical_metadata(
    tmp_path,
    monkeypatch,
):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE sessions SET title = ?, cwd = ?, archived = 1, pinned = 1 "
            "WHERE id = 'cross'",
            ("Canonical CLI title", "/canonical-cli"),
        )
        conn.commit()

    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(models, "_active_stream_ids", lambda: {"stream-1"})

    rows, legacy = models.shared_interactive_sidebar_projection(
        [
            {
                "session_id": "cross",
                "source_tag": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_label": "CLI",
                "profile": "stale-profile",
                "title": "Stale sidecar title",
                "workspace": "/stale-sidecar",
                "message_count": 99,
                "archived": False,
                "pinned": False,
                "active_stream_id": "stream-1",
                "composer_draft": {"text": "keep me"},
                "input_tokens": 123,
            },
            {
                "session_id": "legacy",
                "title": "Old archived chat",
                "message_count": 2,
                "archived": True,
                "updated_at": 5,
            },
        ],
        profile="default",
    )

    assert {row["raw_source"] for row in rows} >= {"webui", "cli", "tui", "acp"}
    cli_row = next(row for row in rows if row["session_id"] == "cross")
    assert cli_row["title"] == "Canonical CLI title"
    assert cli_row["workspace"] == "/canonical-cli"
    assert cli_row["archived"] is True
    assert cli_row["pinned"] is True
    assert cli_row["raw_source"] == "cli"
    assert cli_row["session_source"] == "cli"
    assert cli_row["source_label"] == "CLI"
    assert cli_row["profile"] == "default"
    assert cli_row["is_cli_session"] is True
    assert cli_row["_shared_interactive"] is True
    assert cli_row["active_stream_id"] == "stream-1"
    assert cli_row["composer_draft"] == {"text": "keep me"}
    assert cli_row["input_tokens"] == 123
    assert [row["session_id"] for row in legacy] == ["legacy"]


def test_all_profiles_projection_uses_each_profiles_canonical_metadata(
    tmp_path,
    monkeypatch,
):
    import api.models as models

    def make_profile_db(path, sid, source, title, cwd, archived, pinned):
        _make_db(path)
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "INSERT INTO sessions "
                "(id, source, title, model, started_at, message_count, cwd, archived, "
                "last_activity_at, pinned) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, source, title, "model", 10, 1, cwd, archived, 20, pinned),
            )
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) "
                "VALUES (1, ?, 'user', 'hello', 20)",
                (sid,),
            )
            conn.commit()

    default_db = tmp_path / "default.db"
    work_db = tmp_path / "work.db"
    make_profile_db(default_db, "default-cli", "cli", "Default title", "/default", 0, 0)
    make_profile_db(work_db, "work-tui", "tui", "Named canonical title", "/work", 1, 1)
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [
                (tmp_path, default_db, "default"),
                (tmp_path, work_db, "work"),
            ],
            (),
        ),
    )
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())

    rows, legacy = models.shared_interactive_sidebar_projection_all_profiles(
        [
            {
                "session_id": "work-tui",
                "profile": "work",
                "source_tag": "tui",
                "raw_source": "tui",
                "title": "Stale sidecar title",
                "workspace": "/stale",
                "message_count": 9,
                "archived": False,
                "pinned": False,
            }
        ]
    )

    assert {row["profile"] for row in rows} == {"default", "work"}
    named = next(row for row in rows if row["profile"] == "work")
    assert named["session_id"] == "work-tui"
    assert named["title"] == "Named canonical title"
    assert named["workspace"] == "/work"
    assert named["archived"] is True
    assert named["pinned"] is True
    assert named["raw_source"] == "tui"
    assert legacy == []


def test_cli_compatibility_loader_uses_canonical_state_metadata(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE sessions SET title = ?, cwd = ?, archived = 1, pinned = 1 "
            "WHERE id = 'cross'",
            ("Canonical compatibility title", "/canonical-compat"),
        )
        conn.commit()

    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(
        models,
        "_state_projection_sidecar_metadata",
        lambda sid: {"title": "Stale sidecar title", "archived": False},
    )
    monkeypatch.setattr(models, "_load_webui_deleted_session_tombstone", lambda: frozenset())

    rows = models._load_cli_sessions_uncached(
        tmp_path,
        db,
        "default",
        visible_session_limit=None,
        cron_project_limit=False,
        webhook_project_limit=False,
        include_claude_code=False,
    )

    row = next(item for item in rows if item["session_id"] == "cross")
    assert row["title"] == "Canonical compatibility title"
    assert row["workspace"] == "/canonical-compat"
    assert row["archived"] is True
    assert row["pinned"] is True


def test_sidebar_projection_does_not_overlay_stale_sidecar_stream(tmp_path, monkeypatch):
    import api.models as models

    db = tmp_path / "state.db"
    _make_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "get_last_workspace", lambda: "/fallback")
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())

    rows, _legacy = models.shared_webui_sidebar_projection(
        [
            {
                "session_id": "tip",
                "title": "stale sidecar title",
                "message_count": 2,
                "active_stream_id": "stale-stream",
                "pending_user_message": "old prompt",
                "has_pending_user_message": True,
            }
        ],
        profile="default",
    )

    tip = next(row for row in rows if row["session_id"] == "tip")
    assert tip.get("active_stream_id") is None
    assert tip.get("pending_user_message") is None
    assert tip.get("has_pending_user_message") is not True


def test_compatibility_rest_detail_and_messages_resolve_old_ids(tmp_path, monkeypatch):
    import api.models as models
    import api.routes as routes
    from urllib.parse import urlparse

    db = tmp_path / "state.db"
    _make_db(db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path / "sessions", raising=False)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path / "sessions", raising=False)
    (tmp_path / "sessions").mkdir()

    class Handler:
        def __init__(self, path):
            self.path = path
            self.headers = {}
            self.client_address = ("127.0.0.1", 12345)
            self.wfile = __import__("io").BytesIO()
            self.status = None
            self.response_headers = []

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.response_headers.append((key, value))

        def end_headers(self):
            pass

        @property
        def response_json(self):
            import json
            return json.loads(self.wfile.getvalue().decode())

    detail = Handler("/api/sessions/root")
    routes.handle_get(detail, urlparse(detail.path))
    assert detail.status == 200
    assert detail.response_json["id"] == "tip"
    assert detail.response_json["canonical_session_id"] == "tip"
    assert detail.response_json["message_count"] == 2

    messages = Handler("/api/sessions/root/messages")
    routes.handle_get(messages, urlparse(messages.path))
    assert messages.status == 200
    body = messages.response_json
    assert body["session_id"] == "tip"
    assert [message["content"] for message in body["messages"]] == ["old", "new"]
