"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# parent_session_id -> list of task dicts
_BACKGROUND_TASKS: dict[str, list[dict[str, Any]]] = {}
_BACKGROUND_TASKS_FILE = STATE_DIR / "background_tasks.json"
_BACKGROUND_TASKS_LOADED = False

# btw ephemeral session tracking: parent_sid -> {ephemeral_sid, stream_id, question}
_BTW_TRACKING: dict[str, dict[str, Any]] = {}


def _validated_background_tasks(raw: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        raise ValueError("background task state must be an object")
    validated: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for parent_sid, rows in raw.items():
        if not isinstance(parent_sid, str) or not parent_sid or not isinstance(rows, list):
            raise ValueError("background task state has an invalid parent")
        clean_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("background task row is invalid")
            required_strings = ("task_id", "bg_session_id", "stream_id", "prompt")
            if any(not isinstance(row.get(key), str) or not row.get(key) for key in required_strings):
                raise ValueError("background task row identity is invalid")
            status_value = row.get("status")
            if status_value not in {"running", "done"}:
                raise ValueError("background task row status is invalid")
            started_at = row.get("started_at")
            if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
                raise ValueError("background task row timestamp is invalid")
            answer = row.get("answer")
            completed_at = row.get("completed_at")
            if answer is not None and not isinstance(answer, str):
                raise ValueError("background task row answer is invalid")
            if completed_at is not None and (
                not isinstance(completed_at, (int, float))
                or isinstance(completed_at, bool)
            ):
                raise ValueError("background task completion time is invalid")
            clean_rows.append(dict(row))
            total += 1
            if total > 4096:
                raise ValueError("background task state exceeds its safety bound")
        if clean_rows:
            validated[parent_sid] = clean_rows
    return validated


def _persist_background_tasks_locked() -> None:
    path = Path(_BACKGROUND_TASKS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(_BACKGROUND_TASKS, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _load_background_tasks_locked() -> None:
    global _BACKGROUND_TASKS_LOADED
    if _BACKGROUND_TASKS_LOADED:
        return
    path = Path(_BACKGROUND_TASKS_FILE)
    loaded: dict[str, list[dict[str, Any]]] = {}
    if path.exists():
        if path.is_symlink():
            raise RuntimeError("background task state must not be a symlink")
        opened = path.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077
            or opened.st_size > 8 * 1024 * 1024
        ):
            raise RuntimeError("background task state permissions are unsafe")
        try:
            loaded = _validated_background_tasks(json.loads(path.read_bytes()))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("background task state is invalid") from exc
    _BACKGROUND_TASKS.clear()
    _BACKGROUND_TASKS.update(loaded)
    _BACKGROUND_TASKS_LOADED = True

    # A WebUI process cannot resume an in-process streaming worker after a
    # restart. Surface that terminal outcome exactly once instead of leaving a
    # permanent running row or silently dropping the user's task.
    interrupted = False
    now = time.time()
    for rows in _BACKGROUND_TASKS.values():
        for row in rows:
            if row.get("status") == "running":
                row["status"] = "done"
                row["answer"] = "(background task interrupted because WebUI restarted)"
                row["completed_at"] = now
                interrupted = True
    if interrupted:
        _persist_background_tasks_locked()


def track_background(parent_sid: str, bg_sid: str, stream_id: str,
                     task_id: str, prompt: str) -> None:
    with _lock:
        _load_background_tasks_locked()
        _BACKGROUND_TASKS.setdefault(parent_sid, []).append({
            "task_id": task_id,
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt": prompt,
            "status": "running",
            "started_at": time.time(),
            "answer": None,
            "completed_at": None,
        })
        _persist_background_tasks_locked()


def track_btw(parent_sid: str, ephemeral_sid: str, stream_id: str,
              question: str) -> None:
    with _lock:
        _BTW_TRACKING[parent_sid] = {
            "ephemeral_session_id": ephemeral_sid,
            "stream_id": stream_id,
            "question": question,
        }


def complete_background(parent_sid: str, task_id: str, answer: str) -> None:
    with _lock:
        _load_background_tasks_locked()
        for t in _BACKGROUND_TASKS.get(parent_sid, []):
            if t["task_id"] == task_id and t["status"] == "running":
                t["status"] = "done"
                t["answer"] = answer
                t["completed_at"] = time.time()
                _persist_background_tasks_locked()
                break


def get_results(parent_sid: str) -> list[dict[str, Any]]:
    """Return completed background task results and remove only the done ones
    from tracking.  Tasks still in ``status="running"`` MUST stay in the list
    so that ``complete_background()`` can still find them when the worker
    thread finishes — otherwise the first poll during a long-running task
    silently drops it and the result is lost forever.
    """
    with _lock:
        _load_background_tasks_locked()
        tasks = _BACKGROUND_TASKS.get(parent_sid, [])
        done = [t for t in tasks if t["status"] == "done"]
        still_running = [t for t in tasks if t["status"] != "done"]
        if still_running:
            _BACKGROUND_TASKS[parent_sid] = still_running
        else:
            _BACKGROUND_TASKS.pop(parent_sid, None)
        if done:
            _persist_background_tasks_locked()
        return [{
            "task_id": t["task_id"],
            "prompt": t["prompt"],
            "answer": t["answer"],
            "completed_at": t["completed_at"],
        } for t in done]


def get_background_tasks(parent_sid: str) -> list[dict[str, Any]]:
    """Return all background tasks (running and done) for a parent session."""
    with _lock:
        _load_background_tasks_locked()
        return list(_BACKGROUND_TASKS.get(parent_sid, []))


def cleanup_btw(parent_sid: str) -> dict[str, Any] | None:
    """Remove and return btw tracking for a parent session."""
    with _lock:
        return _BTW_TRACKING.pop(parent_sid, None)
