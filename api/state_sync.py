"""
Hermes Web UI -- shared state.db write bridge.

Mirrors WebUI session metadata (token usage, title, model) into the
hermes-agent state.db so that /insights, session lists, and cost
tracking include WebUI activity.

The historical ``sync_to_insights`` setting is retained for configuration
compatibility, but it no longer gates conversation persistence. All operations
are wrapped in try/except -- if state.db is unavailable, locked, or the schema
doesn't match, the WebUI continues normally and the failure is diagnosable in
logs.

The bridge uses absolute token counts (not deltas) because the WebUI
Session object already accumulates totals across turns. This avoids
any double-counting risk.
"""
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_ACTIVITY_TTL_SECONDS = 20.0
_ACTIVITY_SQLITE_TIMEOUT_SECONDS = 1.0
_ACTIVITY_SCHEMA_READY: set[str] = set()
_ACTIVITY_SCHEMA_LOCK = threading.Lock()


def _ensure_shared_activity_schema(db, *, db_key: str | None = None) -> bool:
    """Create the additive runtime activity table on older state databases.

    Activity is deliberately kept off ``SessionDB``: constructing that wrapper
    runs the full agent schema initialization, which is an unnecessary and
    expensive operation for a five-second heartbeat on a large state.db.
    """
    execute_write = getattr(db, "_execute_write", None)

    def _write(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_activity (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                phase TEXT NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                PRIMARY KEY (session_id, run_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_session_activity_heartbeat
            ON session_activity (heartbeat_at)
            """
        )

    key = str(db_key or "")
    with _ACTIVITY_SCHEMA_LOCK:
        if key and key in _ACTIVITY_SCHEMA_READY:
            return True
        try:
            if callable(execute_write):
                execute_write(_write)
            else:
                _write(db)
                db.commit()
            if key:
                _ACTIVITY_SCHEMA_READY.add(key)
            return True
        except Exception:
            logger.debug("Failed to ensure state.db session activity schema", exc_info=True)
            return False


def _get_state_db_path(profile: Optional[str] = None) -> Path | None:
    """Resolve a profile's state.db without constructing ``SessionDB``."""
    if profile is not None:
        try:
            from api.profiles import (
                _PROFILE_ID_RE,
                _is_root_profile,
                _resolve_profile_home_for_name,
            )

            if not (_is_root_profile(profile) or _PROFILE_ID_RE.fullmatch(profile)):
                logger.warning(
                    "state_sync: refusing invalid profile name %r — skipping "
                    "write rather than leaking to the default state.db (#2762).",
                    profile,
                )
                return None
            hermes_home = Path(_resolve_profile_home_for_name(profile)).expanduser().resolve()
        except Exception:
            logger.warning(
                "state_sync: could not resolve profile %r — skipping write rather "
                "than leaking to the active profile (#2762).",
                profile,
            )
            return None
    else:
        try:
            from api.profiles import get_active_hermes_home

            hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
        except Exception:
            logger.debug("Failed to resolve hermes home, using default")
            hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))

    db_path = hermes_home / "state.db"
    return db_path if db_path.exists() else None


def _open_activity_db(profile: Optional[str] = None):
    """Open a short-lived lightweight writer for the activity overlay."""
    db_path = _get_state_db_path(profile)
    if db_path is None:
        return None, None
    try:
        db = sqlite3.connect(str(db_path), timeout=_ACTIVITY_SQLITE_TIMEOUT_SECONDS)
        db.execute(
            f"PRAGMA busy_timeout={int(_ACTIVITY_SQLITE_TIMEOUT_SECONDS * 1000)}"
        )
        return db, db_path
    except Exception:
        logger.debug("Failed to open state.db activity writer", exc_info=True)
        return None, None


def _ensure_shared_session_row(
    db,
    session_id: str,
    *,
    source: str,
    model: str | None,
    cwd: str | None,
    started_at: float,
) -> None:
    """Create a minimal canonical row before a first WebUI turn has messages.

    WebUI sidecars can be actively streaming before the normal completion
    writeback creates their state.db row. Hermes One cannot list an activity
    record that has no canonical session row, so create only the identity and
    stable launch metadata here. Empty rows remain hidden once activity expires.
    """
    try:
        existing = db.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
        ).fetchone()
        columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if existing:
            # Agent/WebUI startup may create the canonical row before the
            # active-run bridge knows the selected workspace. A heartbeat is
            # still stable launch metadata, so backfill an absent cwd before
            # returning. A later state.db workspace mutation is authoritative
            # and must not be overwritten by a stale run entry.
            if "cwd" in columns and cwd is not None:
                db.execute(
                    """
                    UPDATE sessions SET cwd = ?
                    WHERE id = ? AND (cwd IS NULL OR TRIM(cwd) = '')
                    """,
                    (str(cwd), session_id),
                )
            return
        values = {"id": session_id}
        for name, value in (
            ("source", source),
            ("model", model),
            ("cwd", cwd),
            ("started_at", started_at),
        ):
            if name in columns and value is not None:
                values[name] = value
        names = list(values)
        placeholders = ", ".join("?" for _ in names)
        db.execute(
            f"INSERT OR IGNORE INTO sessions ({', '.join(names)}) "
            f"VALUES ({placeholders})",
            tuple(values[name] for name in names),
        )
    except Exception:
        logger.debug("Failed to create canonical session row for %s", session_id, exc_info=True)


def sync_session_activity(
    session_id: str,
    run_id: str,
    *,
    phase: str = "running",
    started_at: float | None = None,
    heartbeat_at: float | None = None,
    source: str = "webui",
    model: str | None = None,
    cwd: str | None = None,
    profile: Optional[str] = None,
) -> None:
    """Upsert one live worker heartbeat without creating conversation history."""
    sid = str(session_id or "").strip()
    rid = str(run_id or "").strip()
    if not sid or not rid:
        return
    now = float(heartbeat_at if heartbeat_at is not None else time.time())
    started = float(started_at if started_at is not None else now)
    db, db_path = _open_activity_db(profile)
    if not db:
        return
    try:
        if not _ensure_shared_activity_schema(db, db_key=str(db_path)):
            return
        _ensure_shared_session_row(
            db,
            sid,
            source=str(source or "webui"),
            model=model,
            cwd=cwd,
            started_at=started,
        )
        db.execute(
            """
            INSERT INTO session_activity
                (session_id, run_id, source, phase, started_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, run_id) DO UPDATE SET
                source = excluded.source,
                phase = excluded.phase,
                started_at = MIN(session_activity.started_at, excluded.started_at),
                heartbeat_at = excluded.heartbeat_at
            """,
            (sid, rid, str(source or "webui"), str(phase or "running"), started, now),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to sync session activity for %s", sid, exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db after activity sync", exc_info=True)


def clear_session_activity(
    session_id: str,
    run_id: str,
    *,
    profile: Optional[str] = None,
) -> None:
    """Remove one worker heartbeat when its run has finished or been cancelled."""
    sid = str(session_id or "").strip()
    rid = str(run_id or "").strip()
    if not sid or not rid:
        return
    db, db_path = _open_activity_db(profile)
    if not db:
        return
    try:
        if not _ensure_shared_activity_schema(db, db_key=str(db_path)):
            return
        db.execute(
            "DELETE FROM session_activity WHERE session_id = ? AND run_id = ?",
            (sid, rid),
        )
        db.commit()
    except Exception:
        logger.debug("Failed to clear session activity for %s", sid, exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db after activity clear", exc_info=True)


def _ensure_shared_pinned_column(db) -> None:
    """Add the shared pin bit to older Hermes Agent state databases.

    ``pinned`` is deliberately additive: Hermes Agent versions that predate
    shared pins continue to work, and the WebUI can upgrade the live schema
    without rewriting any session or message rows.
    """
    execute_write = getattr(db, "_execute_write", None)
    if not callable(execute_write):
        return

    def _write(conn):
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "pinned" not in columns:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )

    try:
        execute_write(_write)
    except Exception:
        logger.debug("Failed to ensure state.db sessions.pinned column", exc_info=True)


def _set_shared_pinned(db, session_id: str, pinned: bool) -> None:
    """Persist a pin using the Agent API when available, else additive SQL."""
    setter = getattr(db, "set_session_pinned", None)
    if callable(setter):
        setter(session_id, bool(pinned))
        return
    execute_write = getattr(db, "_execute_write", None)
    if not callable(execute_write):
        return

    def _write(conn):
        conn.execute(
            "UPDATE sessions SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, session_id),
        )

    execute_write(_write)


def _get_state_db(profile: Optional[str] = None):
    """Get a SessionDB instance for a profile's state.db.

    When ``profile`` is provided the function resolves *that* profile's
    home directory directly (via ``_resolve_profile_home_for_name``).
    If resolution fails (unknown profile name, IO error, etc.) the
    function returns ``None`` rather than silently falling back to
    ``HERMES_HOME`` — silently routing the write to the wrong DB
    would defeat the point of the explicit-profile path (#2762).

    When ``profile`` is None it falls back to the TLS-based
    ``get_active_hermes_home()`` lookup for backward compatibility,
    with a final ``HERMES_HOME`` fallback only on that path. TLS may be
    unset in background/worker threads, in which case the lookup falls
    through to the process-global active profile and can write to the
    wrong DB. Callers that know the session's profile (e.g.
    ``sync_session_usage`` after a stream completes on a background
    thread) should pass it explicitly to avoid that race.

    Returns None if hermes_state is not importable, the explicit
    profile cannot be resolved, or the DB is unavailable. Each caller
    is responsible for calling db.close() when done.
    """
    try:
        from hermes_state import SessionDB
    except ImportError:
        return None

    db_path = _get_state_db_path(profile)
    if db_path is None:
        return None

    try:
        return SessionDB(db_path)
    except Exception:
        logger.debug("Failed to open state.db")
        return None


def sync_session_start(session_id: str, model=None, profile: Optional[str] = None) -> None:
    """Register a WebUI session in state.db (idempotent).
    Called when a session's first message is sent.

    ``profile`` lets the caller name the target state.db explicitly,
    avoiding the TLS-vs-background-thread mismatch in #2762. When
    omitted, the active profile is resolved from TLS (then process
    globals) as before.
    """
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        db.ensure_session(
            session_id=session_id,
            source='webui',
            model=model,
        )
        _ensure_shared_pinned_column(db)
    except Exception:
        logger.debug("Failed to sync session start to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def _sync_compression_lineage_field(db, session_id: str, field: str, value) -> None:
    """Apply shared metadata consistently within one compression lineage.

    Hermes Agent stores each compression segment as a physical row. Title and
    archive changes mirror across valid members so older IDs cannot resurrect
    stale metadata; pins are cleared across the lineage and stored only on its
    logical root. Keep the operation best-effort for older agent schemas and
    never follow branches, delegates, tool rows, or cross-source children.
    """
    if field not in {"title", "archived", "pinned"}:
        return
    execute_write = getattr(db, "_execute_write", None)
    if not callable(execute_write):
        return

    def _write(conn):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        required = {"id", "parent_session_id", "end_reason", "source"}
        if not required.issubset(columns):
            if field in columns:
                conn.execute(
                    f"UPDATE sessions SET {field} = ? WHERE id = ?",
                    (value, session_id),
                )
            return
        if "model_config" in columns:
            branch_guard = "(x.model_config IS NULL OR NOT json_valid(x.model_config) OR (COALESCE(json_extract(x.model_config, '$._branched_from'), '') = '' AND COALESCE(json_extract(x.model_config, '$._delegate_from'), '') = ''))"
        else:
            branch_guard = "1 = 1"
        lineage_query = f"""
            WITH RECURSIVE lineage(id, source) AS (
                SELECT id, LOWER(TRIM(COALESCE(source, '')))
                FROM sessions WHERE id = ?
                UNION
                SELECT parent.id, LOWER(TRIM(COALESCE(parent.source, '')))
                FROM sessions child
                JOIN lineage current ON current.id = child.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE parent.end_reason = 'compression'
                  AND LOWER(TRIM(COALESCE(parent.source, ''))) = current.source
                  AND LOWER(TRIM(COALESCE(child.source, ''))) = current.source
                  AND {branch_guard.replace('x.', 'child.')}
            UNION
                SELECT child.id, LOWER(TRIM(COALESCE(child.source, '')))
                FROM sessions parent
                JOIN lineage current ON current.id = parent.id
                JOIN sessions child ON child.parent_session_id = parent.id
                WHERE parent.end_reason = 'compression'
                  AND LOWER(TRIM(COALESCE(child.source, ''))) = current.source
                  AND LOWER(TRIM(COALESCE(child.source, ''))) <> 'tool'
                  AND {branch_guard.replace('x.', 'child.')}
            )
            SELECT id FROM lineage
        """
        lineage_ids = [
            row[0] for row in conn.execute(lineage_query, (session_id,)).fetchall()
        ]
        if not lineage_ids:
            return

        placeholders = ", ".join("?" for _ in lineage_ids)
        if field == "pinned":
            # A pin belongs to the logical conversation, not every physical
            # compression segment. Keep one durable bit on the lineage root so
            # raw state.db clients cannot render every hidden segment as pinned.
            # Clearing the full lineage first also repairs pins written by older
            # WebUI builds when the user next pins or unpins the conversation.
            root_row = conn.execute(
                f"SELECT id FROM sessions "
                f"WHERE id IN ({placeholders}) "
                f"AND (parent_session_id IS NULL "
                f"OR parent_session_id NOT IN ({placeholders})) "
                f"LIMIT 1",
                (*lineage_ids, *lineage_ids),
            ).fetchone()
            root_id = root_row[0] if root_row is not None else session_id
            conn.execute(
                f"UPDATE sessions SET pinned = 0 WHERE id IN ({placeholders})",
                tuple(lineage_ids),
            )
            if bool(value):
                conn.execute(
                    "UPDATE sessions SET pinned = 1 WHERE id = ?",
                    (root_id,),
                )
            return

        if field == "title":
            # Titles are unique in state.db, but several old physical
            # compression rows can carry the same WebUI title. Keep the title
            # on the visible requested row and clear hidden lineage copies
            # before setting it. An archived outside-lineage duplicate is also
            # stale for the active projection and may be cleared; never steal
            # a title from another active conversation.
            conflict = conn.execute(
                f"SELECT id, archived FROM sessions "
                f"WHERE title = ? AND id NOT IN ({placeholders}) LIMIT 1",
                (value, *lineage_ids),
            ).fetchone()
            if conflict is not None and not bool(conflict[1]):
                return
            if conflict is not None:
                conn.execute(
                    "UPDATE sessions SET title = NULL WHERE id = ?",
                    (conflict[0],),
                )
            conn.execute(
                f"UPDATE sessions SET title = NULL "
                f"WHERE id IN ({placeholders}) AND id != ?",
                (*lineage_ids, session_id),
            )
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (value, session_id),
            )
            bump = getattr(db, "_bump_session_projection_for_id", None)
            if callable(bump):
                try:
                    bump(conn, session_id)
                except Exception:
                    # Older state schemas may not have the projection marker;
                    # the title write itself remains valid and durable.
                    pass
            return

        conn.execute(
            f"UPDATE sessions SET {field} = ? "
            f"WHERE id IN ({placeholders})",
            (value, *lineage_ids),
        )

    try:
        execute_write(_write)
    except Exception:
        logger.debug("Failed to sync %s across compression lineage", field, exc_info=True)


def sync_session_metadata(
    session_id: str,
    *,
    title: Optional[str] = None,
    cwd: Optional[str] = None,
    archived: Optional[bool] = None,
    pinned: Optional[bool] = None,
    profile: Optional[str] = None,
) -> bool:
    """Update shared conversation metadata without touching usage counters."""
    db = _get_state_db(profile=profile)
    if not db:
        return False
    try:
        db.ensure_session(session_id=session_id, source="webui")
        if pinned is not None:
            _ensure_shared_pinned_column(db)
            _set_shared_pinned(db, session_id, bool(pinned))
            _sync_compression_lineage_field(
                db, session_id, "pinned", 1 if pinned else 0
            )
        if cwd is not None:
            db.update_session_cwd(session_id, str(cwd))
        if archived is not None:
            setter = getattr(db, "set_session_archived", None)
            if callable(setter):
                setter(session_id, bool(archived))
            _sync_compression_lineage_field(
                db, session_id, "archived", 1 if archived else 0
            )
        if title is not None:
            normalized_title = str(title).strip()
            if normalized_title:
                try:
                    db.set_session_title(session_id, normalized_title)
                except Exception:
                    logger.debug("Failed to set shared session title", exc_info=True)
                _sync_compression_lineage_field(
                    db, session_id, "title", normalized_title
                )
        return True
    except Exception:
        logger.debug("Failed to sync shared session metadata to state.db", exc_info=True)
        return False
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_usage(session_id: str, input_tokens: int=0, output_tokens: int=0,
                       estimated_cost=None, model=None, title: Optional[str] = None,
                       message_count: Optional[int] = None, profile: Optional[str] = None,
                       cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                       api_call_count: Optional[int] = None,
                       cwd: Optional[str] = None,
                       archived: Optional[bool] = None,
                       pinned: Optional[bool] = None) -> None:
    """Update token usage and title for a WebUI session in state.db.
    Called after each turn completes. Uses absolute=True to set totals
    (the WebUI Session already accumulates across turns).

    ``profile`` lets the caller name the target state.db explicitly,
    which is what fixes #2762: this function is invoked from the
    agent streaming worker thread, where the request-thread's TLS
    profile context has not been propagated. Without an explicit
    profile, the TLS lookup falls back to the process-global active
    profile and writes the session's usage to the wrong state.db
    (e.g. ``hiyuki``'s instead of the cookie-switched ``maiko``'s).
    """
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        # Ensure session exists first (idempotent)
        db.ensure_session(session_id=session_id, source='webui', model=model)
        if pinned is not None:
            _ensure_shared_pinned_column(db)
            try:
                _set_shared_pinned(db, session_id, bool(pinned))
                _sync_compression_lineage_field(
                    db, session_id, "pinned", 1 if pinned else 0
                )
            except Exception:
                logger.debug("Failed to sync session pin to state.db")
        if cwd is not None:
            try:
                db.update_session_cwd(session_id, str(cwd))
            except Exception:
                logger.debug("Failed to sync session workspace to state.db")
        if archived is not None:
            try:
                db.set_session_archived(session_id, bool(archived))
            except Exception:
                logger.debug("Failed to sync session archive state to state.db")
            try:
                _sync_compression_lineage_field(
                    db, session_id, "archived", 1 if archived else 0
                )
            except Exception:
                logger.debug("Failed to sync archive lineage to state.db")
        # Set absolute token counts. WebUI's sidecar already accumulates
        # input/output/cache totals across turns, so mirror the same absolute
        # values into state.db. Omitting cache counters makes insights/reporting
        # show false 0% hit rates even when the live stream and sidecar saw warm
        # prefix reads.
        db.update_token_counts(
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            api_call_count=api_call_count,
            estimated_cost_usd=estimated_cost,
            model=model,
            absolute=True,
        )
        # Update title if we have one, using the public API
        if title is not None:
            try:
                db.set_session_title(session_id, title)
            except Exception:
                logger.debug("Failed to sync session title to state.db")
            try:
                _sync_compression_lineage_field(db, session_id, "title", title)
            except Exception:
                logger.debug("Failed to sync title lineage to state.db")
        # Update message count
        if message_count is not None:
            try:
                def _set_msg_count(conn):
                    conn.execute(
                        "UPDATE sessions SET message_count = ? WHERE id = ?",
                        (message_count, session_id),
                    )
                db._execute_write(_set_msg_count)
            except Exception:
                logger.debug("Failed to sync message count to state.db")
    except Exception:
        logger.debug("Failed to sync session usage to state.db")
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_pinned(
    session_id: str,
    pinned: bool,
    *,
    profile: Optional[str] = None,
) -> None:
    """Mirror one UI pin to state.db without touching usage counters."""
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        db.ensure_session(session_id=session_id, source="webui")
        _ensure_shared_pinned_column(db)
        _set_shared_pinned(db, session_id, bool(pinned))
        _sync_compression_lineage_field(
            db, session_id, "pinned", 1 if pinned else 0
        )
    except Exception:
        logger.debug("Failed to sync session pin to state.db", exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_archived(
    session_id: str,
    archived: bool,
    *,
    profile: Optional[str] = None,
) -> None:
    """Mirror one archive mutation to state.db across its compression lineage."""
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        db.ensure_session(session_id=session_id, source="webui")
        setter = getattr(db, "set_session_archived", None)
        if callable(setter):
            setter(session_id, bool(archived))
        _sync_compression_lineage_field(
            db, session_id, "archived", 1 if archived else 0
        )
    except Exception:
        logger.debug("Failed to sync session archive state to state.db", exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")


def sync_session_title(
    session_id: str,
    title: str,
    *,
    profile: Optional[str] = None,
) -> None:
    """Mirror a WebUI title into state.db without touching usage counters."""
    normalized = str(title or "").strip()
    if not normalized or normalized.lower() == "untitled":
        return
    db = _get_state_db(profile=profile)
    if not db:
        return
    try:
        # This is a compatibility/backfill bridge, not the interactive title
        # mutation API.  SessionDB.set_session_title deliberately rejects a
        # title already used by another physical row.  A WebUI title must be
        # allowed to replace the generated title on the visible compression
        # tip, after which the same value is propagated through that lineage.
        _sync_compression_lineage_field(db, session_id, "title", normalized)
    except Exception:
        logger.debug("Failed to sync session title to state.db", exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close state.db")
