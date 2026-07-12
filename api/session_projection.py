"""Non-blocking invalidation monitor for the Agent session projection."""

import sqlite3
import threading
import time
from pathlib import Path


_POLL_INTERVAL_SECONDS = 0.5
_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}


def _path_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (0, 0)


def _read_projection_token(db_path: Path):
    """Read the Agent-owned generation, falling back to legacy file stamps."""
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
            row = conn.execute(
                "SELECT generation FROM session_projection_meta WHERE id = 1"
            ).fetchone()
            if row is not None:
                return ("projection", int(row[0] or 0))
        finally:
            conn.close()
    except Exception:
        pass
    return (
        "legacy",
        _path_stamp(db_path),
        _path_stamp(Path(f"{db_path}-wal")),
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
