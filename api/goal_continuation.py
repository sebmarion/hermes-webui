"""Durable, server-owned continuation for active WebUI goals.

The goal judge runs before the parent stream emits ``done``.  A successor must
not start until that parent has released its stream/session ownership, and it
must not depend on an open browser tab.  This module records the judge decision
as an atomic receipt, then claims and starts it at the worker teardown boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from api import config
from api.managed_continuation_recovery import (
    recover_exact,
    stable_store_snapshot,
    strict_store_save,
    strict_store_lock,
    verify_exact,
)
from api.process_identity import process_start_token

logger = logging.getLogger(__name__)

SOURCE = "goal_continuation"
CONTROL_KEY = "_goal_continuation_control"
_RECEIPT_VERSION = 1
_MAX_MANAGED_RECEIPTS = 4096
_LOCK = threading.RLock()
_MANAGED_EXACT = ContextVar("goal_continuation_managed_exact", default=False)


class GoalContinuationReceiptStoreError(RuntimeError):
    """Raised when durable continuation ownership cannot be read safely."""


def _receipt_path() -> Path:
    return Path(config.SESSION_DIR) / "_goal_continuations.json"


def _lock_path() -> Path:
    return Path(config.SESSION_DIR) / "_goal_continuations.lock"


@contextmanager
def _store_lock():
    """Serialize receipt transactions across threads and WebUI processes."""
    if _MANAGED_EXACT.get():
        with strict_store_lock(_lock_path(), _LOCK):
            yield
        return
    with _LOCK:
        Path(config.SESSION_DIR).mkdir(parents=True, exist_ok=True)
        fp = open(_lock_path(), "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    fp.seek(0)
                    msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            finally:
                fp.close()


@contextmanager
def _verification_store_lock():
    with strict_store_lock(_lock_path(), _LOCK, create=False):
        yield


def _empty_store() -> dict:
    return {"version": _RECEIPT_VERSION, "receipts": {}}


def _load_store() -> dict:
    path = _receipt_path()
    if _MANAGED_EXACT.get():
        return stable_store_snapshot(path)[0]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_store()
    except Exception as exc:
        raise GoalContinuationReceiptStoreError(
            f"goal continuation receipt is unreadable: {path}"
        ) from exc
    if not isinstance(raw, dict):
        raise GoalContinuationReceiptStoreError(
            f"goal continuation receipt root must be an object: {path}"
        )
    if raw.get("version") != _RECEIPT_VERSION:
        raise GoalContinuationReceiptStoreError(
            "unsupported goal continuation receipt version "
            f"{raw.get('version')!r}; expected {_RECEIPT_VERSION}"
        )
    if not isinstance(raw.get("receipts"), dict):
        raise GoalContinuationReceiptStoreError(
            f"goal continuation receipts must be an object: {path}"
        )
    return raw


def _save_store(store: dict) -> None:
    path = _receipt_path()
    if _MANAGED_EXACT.get():
        strict_store_save(path, store)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(store, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _claim_key(session_id: str, parent_run_id: str) -> str:
    return hashlib.sha256(f"{session_id}\0{parent_run_id}".encode()).hexdigest()


def _goal_revision_is_active(session_id: str, revision, *, profile_home=None) -> bool:
    from api.goals import goal_revision_is_active

    return goal_revision_is_active(session_id, revision, profile_home=profile_home)


def _pid_is_alive(pid: int | None) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_start_token(pid: int | None) -> str | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    return process_start_token(pid)


def claim_goal_continuation(
    *,
    session_id: str,
    parent_run_id: str,
    prompt: str,
    goal_revision,
    profile_home: str | Path | None = None,
    now: float | None = None,
) -> dict:
    """Durably and idempotently record one post-judge continuation decision."""
    session_id = str(session_id or "").strip()
    parent_run_id = str(parent_run_id or "").strip()
    prompt = str(prompt or "").strip()
    if not session_id or not parent_run_id or not prompt:
        raise ValueError("session_id, parent_run_id, and prompt are required")
    try:
        revision = int(goal_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("goal_revision is required") from exc
    timestamp = float(now if now is not None else time.time())
    key = _claim_key(session_id, parent_run_id)
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            receipt = {
                "claim_key": key,
                "session_id": session_id,
                "parent_run_id": parent_run_id,
                "prompt": prompt,
                "goal_revision": revision,
                "profile_home": str(profile_home) if profile_home else None,
                "state": "claimed",
                "claimed_at": timestamp,
                "updated_at": timestamp,
            }
            store["receipts"][key] = receipt
            _save_store(store)
        return copy.deepcopy(receipt)


def _attach_control_to_session(receipt: dict) -> None:
    """Persist structured model-context metadata without a visible user row."""
    try:
        from api.models import get_session

        session = get_session(str(receipt.get("session_id") or ""))
        session.goal_continuation = {
            "claim_key": receipt.get("claim_key"),
            "parent_run_id": receipt.get("parent_run_id"),
            "goal_revision": receipt.get("goal_revision"),
            "instruction": receipt.get("prompt"),
        }
        session.save(touch_updated_at=False)
    except Exception:
        # Source-based transcript suppression still keeps the prompt hidden.
        # The marker is supplementary recovery/diagnostic context, not a start gate.
        logger.debug("failed to persist goal continuation control metadata", exc_info=True)


def _reserve_start(key: str) -> tuple[dict | None, str | None]:
    """Atomically reserve a claimed (or dead-owner starting) receipt."""
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            return None, None
        state = receipt.get("state")
        if state in ("started", "discarded"):
            return copy.deepcopy(receipt), None
        if state == "starting" and _pid_is_alive(receipt.get("owner_pid")):
            return copy.deepcopy(receipt), None
        token = uuid.uuid4().hex
        receipt.update(
            {
                "state": "starting",
                "owner_pid": os.getpid(),
                "owner_start_token": _process_start_token(os.getpid()),
                "owner_thread": threading.get_ident(),
                "start_token": token,
                "launch_phase": "reserved",
                "starting_at": time.time(),
                "updated_at": time.time(),
            }
        )
        _save_store(store)
        return copy.deepcopy(receipt), token


def _mark_launching(key: str, token: str) -> dict | None:
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if (
            receipt is None
            or receipt.get("state") != "starting"
            or receipt.get("start_token") != token
        ):
            return copy.deepcopy(receipt) if receipt else None
        receipt["launch_phase"] = "launching"
        receipt["updated_at"] = time.time()
        _save_store(store)
        return copy.deepcopy(receipt)


def _finish_start(key: str, token: str, response: dict | None) -> dict | None:
    response = response if isinstance(response, dict) else {}
    status = int(response.get("_status", 200) or 200)
    stream_id = response.get("stream_id") or response.get("active_stream_id")
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            return None
        if receipt.get("state") != "starting" or receipt.get("start_token") != token:
            return copy.deepcopy(receipt)
        response_session_id = str(response.get("session_id") or "").strip()
        intended_session_id = str(receipt.get("session_id") or "").strip()
        succeeded = (
            status < 400
            and bool(stream_id)
            and response_session_id == intended_session_id
        )
        now = time.time()
        if succeeded:
            receipt.update(
                {
                    "state": "started",
                    "child_stream_id": str(stream_id),
                    "started_at": now,
                    "updated_at": now,
                    "completed_start_token": token,
                }
            )
        else:
            # A 409 means a competing turn owns the session. Other explicit
            # failures are equally retryable: retain the durable claim so the
            # next idle boundary or process startup can try again.
            receipt.update({"state": "claimed", "updated_at": now})
            receipt.pop("child_stream_id", None)
            receipt.pop("completed_start_token", None)
        for field in (
            "owner_pid",
            "owner_start_token",
            "owner_thread",
            "start_token",
            "launch_phase",
            "starting_at",
        ):
            receipt.pop(field, None)
        _save_store(store)
        return copy.deepcopy(receipt)


def settle_goal_continuation(
    session_id: str,
    parent_run_id: str,
    *,
    start: Callable[[str, str], dict] | None = None,
    _managed_exact: bool = False,
    _crash_hook: Callable[[str], None] | None = None,
) -> dict | None:
    """Validate and start a receipt after its parent stream has fully settled."""
    key = _claim_key(str(session_id or "").strip(), str(parent_run_id or "").strip())
    with _store_lock():
        receipt = copy.deepcopy(_load_store()["receipts"].get(key))
    if receipt is None or receipt.get("state") in ("started", "discarded"):
        return receipt
    if not _goal_revision_is_active(
        str(receipt.get("session_id") or ""),
        receipt.get("goal_revision"),
        profile_home=receipt.get("profile_home"),
    ):
        with _store_lock():
            store = _load_store()
            current = store["receipts"].get(key)
            if current and current.get("state") not in ("started", "discarded"):
                current.update(
                    {
                        "state": "discarded",
                        "discarded_reason": "stale_goal_revision",
                        "updated_at": time.time(),
                    }
                )
                _save_store(store)
            return copy.deepcopy(current) if current else None

    receipt, token = _reserve_start(key)
    if receipt is None or token is None:
        return receipt
    if _managed_exact and _crash_hook is not None:
        _crash_hook("claim_committed")
    _attach_control_to_session(receipt)
    starter = start
    if starter is None:
        from api.routes import start_session_turn

        starter = lambda sid, text: start_session_turn(sid, text, source=SOURCE)
    receipt = _mark_launching(key, token)
    if receipt is None or receipt.get("launch_phase") != "launching":
        return receipt
    try:
        response = starter(str(receipt["session_id"]), str(receipt["prompt"])) or {}
    except Exception:
        logger.exception(
            "goal continuation start failed for session %s parent %s",
            receipt.get("session_id"),
            receipt.get("parent_run_id"),
        )
        response = {"error": "continuation start failed", "_status": 500}
    if _managed_exact and _crash_hook is not None:
        _crash_hook("launch_returned")
    result = _finish_start(key, token, response)
    if (
        _managed_exact
        and result is not None
        and result.get("state") == "started"
        and _crash_hook is not None
    ):
        _crash_hook("started_committed")
    return result


def recover_pending_goal_continuations(
    *,
    start: Callable[[str, str], dict] | None = None,
    session_id: str | None = None,
) -> int:
    """Start valid claimed receipts and reclaim dead-process reservations."""
    with _store_lock():
        rows = [
            copy.deepcopy(receipt)
            for receipt in _load_store()["receipts"].values()
            if receipt.get("state") in ("claimed", "starting")
            and (session_id is None or receipt.get("session_id") == session_id)
            and (
                receipt.get("state") == "claimed"
                or not _pid_is_alive(receipt.get("owner_pid"))
            )
        ]
    started = 0
    for receipt in rows:
        result = settle_goal_continuation(
            str(receipt.get("session_id") or ""),
            str(receipt.get("parent_run_id") or ""),
            start=start,
        )
        if result and result.get("state") == "started":
            started += 1
    return started


_MANAGED_RECEIPT_FIELDS = {
    "claim_key", "session_id", "parent_run_id", "prompt", "goal_revision",
    "profile_home", "state", "claimed_at", "updated_at", "owner_pid",
    "owner_start_token", "owner_thread", "start_token", "launch_phase",
    "starting_at", "child_stream_id", "started_at",
    "completed_start_token", "discarded_reason",
}


def _managed_text(receipt: dict, field: str, *, optional: bool = False) -> str:
    value = receipt.get(field)
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 65536:
        raise ValueError(f"{field} must be bounded non-empty text")
    return value


def _managed_timestamp(receipt: dict, field: str) -> float:
    value = receipt.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a timestamp")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def _validate_managed_store(store: dict, *, max_receipts: int) -> dict:
    root_fields = {"version", "receipts", "pinned", "archived"}
    if (
        not isinstance(store, dict)
        or not {"version", "receipts"}.issubset(store)
        or set(store) - root_fields
        or any(
            field in store and type(store[field]) is not bool
            for field in ("pinned", "archived")
        )
    ):
        raise ValueError("goal continuation store root schema is invalid")
    if store.get("version") != _RECEIPT_VERSION:
        raise ValueError("goal continuation store version is invalid")
    receipts = store.get("receipts")
    if (
        not isinstance(receipts, dict)
        or isinstance(max_receipts, bool)
        or not isinstance(max_receipts, int)
        or max_receipts < 1
        or len(receipts) > max_receipts
    ):
        raise ValueError("goal continuation receipt count is invalid")
    for key, receipt in receipts.items():
        if not isinstance(receipt, dict) or set(receipt) - _MANAGED_RECEIPT_FIELDS:
            raise ValueError(f"{key}: goal continuation receipt schema is invalid")
        claim_key = _managed_text(receipt, "claim_key")
        session_id = _managed_text(receipt, "session_id")
        run_id = _managed_text(receipt, "parent_run_id")
        if (
            key != claim_key
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or key != _claim_key(session_id, run_id)
        ):
            raise ValueError(f"{key}: goal continuation claim identity is invalid")
        _managed_text(receipt, "prompt")
        revision = receipt.get("goal_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError(f"{key}: goal_revision is invalid")
        profile_home = receipt.get("profile_home")
        if profile_home is not None and (
            not isinstance(profile_home, str)
            or len(profile_home.encode("utf-8")) > 65536
        ):
            raise ValueError(f"{key}: profile_home is invalid")
        for field in ("claimed_at", "updated_at"):
            _managed_timestamp(receipt, field)
        state = receipt.get("state")
        if state not in {"claimed", "starting", "started", "discarded"}:
            raise ValueError(f"{key}: goal continuation state is invalid")
        if state == "starting":
            owner_pid = receipt.get("owner_pid")
            if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 1:
                raise ValueError(f"{key}: owner_pid is invalid")
            for field in ("owner_start_token", "start_token"):
                _managed_text(receipt, field)
            if receipt.get("launch_phase") not in {"reserved", "launching"}:
                raise ValueError(f"{key}: launch_phase is invalid")
            _managed_timestamp(receipt, "starting_at")
        if state == "started":
            _managed_text(receipt, "child_stream_id")
            if "completed_start_token" in receipt:
                _managed_text(receipt, "completed_start_token")
            _managed_timestamp(receipt, "started_at")
        if state == "discarded":
            _managed_text(receipt, "discarded_reason")
    return receipts


def recover_managed_goal_continuations_exact(
    *,
    transaction_id: str,
    manifest_sha256: str,
    start: Callable[[str, str], dict] | None = None,
    crash_hook: Callable[[str], None] | None = None,
):
    """Recover the exact bounded goal store under managed startup authority."""
    scope = _MANAGED_EXACT.set(True)
    try:
        return recover_exact(
            path=_receipt_path(),
            store_lock=_store_lock,
            validate_store=_validate_managed_store,
            start_one=lambda key: _start_managed_goal_receipt(
                key,
                start=start,
                crash_hook=crash_hook,
            ),
            session_id_for=lambda receipt: str(receipt.get("session_id") or ""),
            terminal_states={"discarded"},
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha256,
            max_receipts=_MAX_MANAGED_RECEIPTS,
            process_token_lookup=_process_start_token,
        )
    finally:
        _MANAGED_EXACT.reset(scope)


def verify_managed_continuations_exact(
    receipt,
    *,
    transaction_id: str,
    manifest_sha256: str,
):
    """Read-only verification of an exact managed goal continuation receipt."""
    return verify_exact(
        receipt,
        path=_receipt_path(),
        store_lock=_verification_store_lock,
        validate_store=_validate_managed_store,
        session_id_for=lambda row: str(row.get("session_id") or ""),
        terminal_states={"discarded"},
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
        max_receipts=_MAX_MANAGED_RECEIPTS,
        process_token_lookup=_process_start_token,
    )


def _load_receipt_identity(key: str) -> tuple[str, str]:
    with _store_lock():
        receipt = _load_store()["receipts"].get(key)
        if not isinstance(receipt, dict):
            return "", ""
        return (
            str(receipt.get("session_id") or ""),
            str(receipt.get("parent_run_id") or ""),
        )


def _start_managed_goal_receipt(
    key: str,
    *,
    start: Callable[[str, str], dict] | None,
    crash_hook: Callable[[str], None] | None,
) -> tuple[dict | None, bool]:
    session_id, parent_run_id = _load_receipt_identity(key)
    result = settle_goal_continuation(
        session_id,
        parent_run_id,
        start=start,
        _managed_exact=True,
        _crash_hook=crash_hook,
    )
    return result, bool(result and result.get("state") == "started")


def load_receipts() -> dict:
    """Return a read-only receipt snapshot for diagnostics and tests."""
    with _store_lock():
        return copy.deepcopy(_load_store())
