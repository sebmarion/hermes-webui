"""Non-blocking invalidation monitor for the Agent session projection."""

import sqlite3
import threading
import time
from pathlib import Path


# Keep externally-written legacy state.db changes visible within one normal
# sidebar convergence window. The read itself runs on the monitor thread, so a
# shorter interval does not add SQLite work to the request path.
_POLL_INTERVAL_SECONDS = 0.1
_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}


def _path_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (0, 0)


def _path_identity(path: Path) -> tuple[int, int]:
    """Return the stable filesystem identity for a database inode."""
    try:
        stat = path.stat()
        return (int(stat.st_dev), int(stat.st_ino))
    except OSError:
        return (0, 0)


def _legacy_data_version(db_path: Path) -> tuple:
    """Return a bounded commit fingerprint without pinning a SQLite reader.

    Legacy databases do not have ``session_projection_meta``.  An earlier
    implementation kept a read-only connection open so ``PRAGMA data_version``
    could detect external commits.  That long-lived WAL reader interacts badly
    with the vulnerable SQLite 3.50 runtime when another writer reuses/resets a
    WAL (the exact shape exercised by archive/import flows): the reader keeps an
    old WAL snapshot alive and later writers can become invisible or report
    ``disk I/O error``.  The monitor is already off the request path, so use a
    short-lived read and a content/path fingerprint instead.  Closing the
    connection on every poll is deliberate: invalidation must never pin the
    live state.db or its ``-wal``/``-shm`` sidecars.
    """
    identity = _path_identity(db_path)
    if identity == (0, 0):
        return identity
    db_stamp = _path_stamp(db_path)
    wal_stamp = _path_stamp(Path(f"{db_path}-wal"))
    parts: list[tuple[str, object]] = []
    try:
        conn = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=0.05,
        )
        try:
            conn.execute("PRAGMA busy_timeout=50")
            # MAX(rowid) is indexed/cheap for the legacy schema and advances
            # for new external rows. Pair it with a per-table row count so a
            # cleanup followed by rowid reuse still changes the fingerprint.
            for table in ("sessions", "messages", "session_completion_events"):
                try:
                    row = conn.execute(
                        f"SELECT MAX(rowid), COUNT(*) FROM {table}"
                    ).fetchone()
                except sqlite3.Error:
                    row = (None, None)
                parts.append((table, tuple(row or (None, None))))
        finally:
            conn.close()
    except sqlite3.Error:
        # Keep the path/WAL stamps as a useful conservative fallback while a
        # writer is briefly changing the database or the schema is incomplete.
        pass
    return (identity, db_stamp, wal_stamp, tuple(parts))


def _read_projection_token(db_path: Path):
    """Read a non-blocking filesystem token for the state database.

    This monitor must not open SQLite at all.  A background read-only handle
    can overlap an external ``PRAGMA journal_mode=WAL``/checkpoint and pin a
    stale WAL index on the vulnerable SQLite runtime used by the local Agent.
    The request/cache paths perform their own bounded read-only content checks;
    this owner only needs to notice that the database or its WAL sidecars
    changed and schedule a rebuild.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return ("missing", 0)
    return (
        "filesystem",
        _path_identity(db_path),
        _path_stamp(db_path),
        _path_stamp(Path(f"{db_path}-wal")),
        _path_stamp(Path(f"{db_path}-shm")),
    )


def projection_token(db_path: Path | None):
    """Return the in-memory token and schedule any SQLite work off-thread."""
    if db_path is None:
        return ("missing", 0)
    path = Path(db_path).expanduser().resolve(strict=False)
    key = str(path)
    now = time.monotonic()
    with _LOCK:
        state = _STATE.setdefault(
            key,
            {"token": ("cold", 0), "last_started": 0.0, "inflight": False},
        )
        token = state["token"]
        should_start = (
            not state["inflight"]
            and now - float(state["last_started"] or 0.0) >= _POLL_INTERVAL_SECONDS
        )
        if should_start:
            state["inflight"] = True
            state["last_started"] = now

    if should_start:
        def _refresh() -> None:
            try:
                refreshed = _read_projection_token(path)
            except Exception:
                refreshed = token
            with _LOCK:
                current = _STATE.get(key)
                if current is not None:
                    current["token"] = refreshed
                    current["inflight"] = False

        try:
            threading.Thread(
                target=_refresh,
                name="session-projection-monitor",
                daemon=True,
            ).start()
        except Exception:
            with _LOCK:
                state = _STATE.get(key)
                if state is not None:
                    state["inflight"] = False
    return token


def _reset_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
