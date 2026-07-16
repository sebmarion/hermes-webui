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
_CONNECTION_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}
_LEGACY_CONNECTIONS: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = {}


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


def _legacy_data_version(db_path: Path) -> tuple[int, int]:
    """Read a legacy SQLite database's commit version without blocking callers.

    ``PRAGMA data_version`` only advances for changes made by *other*
    connections. That is exactly the useful signal here: Hermes Agent and the
    WebUI write state.db through their own connections, while the projection
    monitor keeps one read-only connection per database. Unlike mtime/size or
    ``MAX(rowid)``, it cannot collide when SQLite reuses a rowid after cleanup.
    """
    identity = _path_identity(db_path)
    key = str(db_path)
    if identity == (0, 0):
        return identity
    with _CONNECTION_LOCK:
        cached = _LEGACY_CONNECTIONS.get(key)
        if cached is not None and cached[0] != identity:
            try:
                cached[1].close()
            except Exception:
                pass
            _LEGACY_CONNECTIONS.pop(key, None)
            cached = None
        if cached is None:
            try:
                conn = sqlite3.connect(
                    f"file:{db_path.resolve().as_posix()}?mode=ro",
                    uri=True,
                    timeout=0.05,
                    check_same_thread=False,
                )
                conn.execute("PRAGMA busy_timeout=50")
            except Exception:
                return identity
            _LEGACY_CONNECTIONS[key] = (identity, conn)
            cached = (identity, conn)
        try:
            row = cached[1].execute("PRAGMA data_version").fetchone()
            return identity + (int(row[0]) if row else 0,)
        except Exception:
            try:
                cached[1].close()
            except Exception:
                pass
            _LEGACY_CONNECTIONS.pop(key, None)
            return identity


def _read_projection_token(db_path: Path):
    """Read generation; use legacy stamps only when the table is absent."""
    db_path = Path(db_path)
    if not db_path.exists():
        return ("missing", 0)
    try:
        conn = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=0.05,
        )
        try:
            conn.execute("PRAGMA busy_timeout=50")
            try:
                row = conn.execute(
                    "SELECT generation FROM session_projection_meta WHERE id = 1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    return (
                        "legacy",
                        _legacy_data_version(db_path),
                        _path_stamp(Path(f"{db_path}-wal")),
                    )
                raise
            if row is not None:
                # The Agent normally advances this generation, but external
                # gateway writers and older WebUI integrations may commit
                # rows without touching the projection metadata table. Keep
                # those writes visible as well; data_version is read through a
                # persistent connection so rowid/stat reuse cannot collide.
                return (
                    "projection",
                    int(row[0] or 0),
                    _legacy_data_version(db_path),
                )
            # A partially initialized or older database can have the metadata
            # table without its singleton row. Treat it like a legacy store so
            # external commits still invalidate the sidebar cache.
            return (
                "legacy",
                _legacy_data_version(db_path),
                _path_stamp(Path(f"{db_path}-wal")),
            )
        finally:
            conn.close()
    except Exception:
        raise


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
    with _CONNECTION_LOCK:
        for _identity, conn in _LEGACY_CONNECTIONS.values():
            try:
                conn.close()
            except Exception:
                pass
        _LEGACY_CONNECTIONS.clear()
    with _LOCK:
        _STATE.clear()
