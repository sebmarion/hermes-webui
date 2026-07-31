"""Durable, exactly-once continuation of turns stopped by the tool limit.

The receipt is deliberately separate from Session JSON.  It is profile-tagged,
atomically replaced, and guarded by both a process mutex and an advisory file
lock so duplicate terminal callbacks (or two WebUI processes) cannot create two
children for one parent run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from api import config
from api.models import Session
from api.process_identity import process_start_token
from api.managed_continuation_recovery import (
    recover_exact,
    stable_store_snapshot,
    strict_store_save,
    strict_store_lock,
    verify_exact,
)

logger = logging.getLogger(__name__)

SOURCE = "tool_limit_continuation"
CONTROL_KEY = "_tool_limit_continuation_control"
DEFAULTS = {
    "enabled": True,
    "max_segments": 12,
    "max_wall_seconds": 14400,
    "no_progress_limit": 3,
}
_RECEIPT_VERSION = 1
_MAX_MANAGED_RECEIPTS = 4096
_LOCK = threading.RLock()
_MANAGED_EXACT = ContextVar("tool_continuation_managed_exact", default=False)


class ContinuationReceiptStoreError(RuntimeError):
    """Raised when durable continuation ownership cannot be read safely."""


def _receipt_path() -> Path:
    return Path(config.SESSION_DIR) / "_tool_limit_continuations.json"


def _lock_path() -> Path:
    return Path(config.SESSION_DIR) / "_tool_limit_continuations.lock"


@contextmanager
def _store_lock():
    """Serialize receipt transactions in this and sibling server processes."""
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
        raise ContinuationReceiptStoreError(
            f"tool-limit continuation receipt is unreadable: {path}"
        ) from exc
    if not isinstance(raw, dict):
        raise ContinuationReceiptStoreError(
            f"tool-limit continuation receipt root must be an object: {path}"
        )
    if raw.get("version") != _RECEIPT_VERSION:
        raise ContinuationReceiptStoreError(
            "unsupported tool-limit continuation receipt version "
            f"{raw.get('version')!r}; expected {_RECEIPT_VERSION}"
        )
    if not isinstance(raw.get("receipts"), dict):
        raise ContinuationReceiptStoreError(
            f"tool-limit continuation receipts must be an object: {path}"
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


def _positive_int(value, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def settings_for_session(session) -> dict:
    """Read this session's profile config.yaml; no environment overrides."""
    profile = str(getattr(session, "profile", None) or "default")
    try:
        from api.profiles import get_hermes_home_for_profile
        cfg = config.get_config_for_profile_home(get_hermes_home_for_profile(profile))
    except Exception:
        cfg = config.get_config()
    section = cfg.get("tool_limit_continuation", {}) if isinstance(cfg, dict) else {}
    if not isinstance(section, dict) and isinstance(cfg, dict):
        section = {}
    if not section and isinstance(cfg, dict):
        webui = cfg.get("webui", {})
        if isinstance(webui, dict) and isinstance(webui.get("tool_limit_continuation"), dict):
            section = webui["tool_limit_continuation"]
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": section.get("enabled", DEFAULTS["enabled"]) is not False,
        "max_segments": _positive_int(section.get("max_segments"), DEFAULTS["max_segments"]),
        "max_wall_seconds": _positive_int(section.get("max_wall_seconds"), DEFAULTS["max_wall_seconds"]),
        "no_progress_limit": _positive_int(section.get("no_progress_limit"), DEFAULTS["no_progress_limit"]),
    }


def _git_fingerprint(workspace: str) -> str | None:
    try:
        root = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return hashlib.sha256((head + "\0" + status).encode()).hexdigest()
    except Exception:
        return None


def progress_fingerprint(session) -> str | None:
    """Conservative machine-progress signal; prose alone never stops a chain."""
    git = _git_fingerprint(str(getattr(session, "workspace", "") or ""))
    facts = []
    for call in list(getattr(session, "tool_calls", None) or [])[-32:]:
        if isinstance(call, dict):
            facts.append({k: call.get(k) for k in ("name", "args", "is_error", "done", "result")})
    for message in list(getattr(session, "context_messages", None) or [])[-64:]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" or message.get("_verification") or message.get("verification"):
            facts.append(message)
        elif any(k in message for k in ("_verification_pending", "pending_verification", "_tool_activity")):
            facts.append({k: message.get(k) for k in (
                "_verification_pending", "pending_verification", "_tool_activity"
            ) if k in message})
    if git is None and not facts:
        return None
    encoded = json.dumps({"git": git, "facts": facts}, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _claim_key(parent_session_id: str, parent_run_id: str) -> str:
    raw = f"{parent_session_id}\0{parent_run_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _event_payload(receipt: dict) -> dict:
    payload = {k: receipt.get(k) for k in (
        "execution_id", "root_session_id", "parent_session_id", "child_session_id",
        "parent_run_id", "continuation_index", "state",
    )}
    if payload["state"] == "claimed":
        payload["state"] = "accepted"
    if payload["state"] == "blocked":
        payload["blocked_reason"] = receipt.get("blocked_reason")
    return payload


def _emit(receipt: dict, emit: Callable[[str, dict], None] | None = None) -> None:
    payload = _event_payload(receipt)
    if emit is not None:
        emit("tool_limit_continuation", payload)
        return
    try:
        from api.background_process import get_session_channel
        session_ids = {
            str(receipt.get("root_session_id") or ""),
            str(receipt.get("parent_session_id") or ""),
        }
        for session_id in session_ids:
            if not session_id:
                continue
            channel = get_session_channel(session_id)
            if channel is not None:
                channel.emit("tool_limit_continuation", payload)
    except Exception:
        logger.debug("tool-limit continuation SSE fan-out failed", exc_info=True)


def _new_child(parent, *, execution_id: str, root_session_id: str, index: int, prompt: str) -> Session:
    control = {
        "execution_id": execution_id,
        "root_session_id": root_session_id,
        "parent_session_id": parent.session_id,
        "continuation_index": index,
        "instruction": prompt,
    }
    context = copy.deepcopy(list(getattr(parent, "context_messages", None) or []))
    context.append({"role": "user", "content": prompt, CONTROL_KEY: control})
    child = Session(
        title=getattr(parent, "title", "Untitled"),
        workspace=getattr(parent, "workspace", ""),
        model=getattr(parent, "model", ""),
        model_provider=getattr(parent, "model_provider", None),
        profile=getattr(parent, "profile", None),
        project_id=getattr(parent, "project_id", None),
        personality=getattr(parent, "personality", None),
        enabled_toolsets=copy.deepcopy(getattr(parent, "enabled_toolsets", None)),
        parent_session_id=parent.session_id,
        messages=[],
        context_messages=context,
        source_tag=SOURCE,
        raw_source=SOURCE,
        session_source=SOURCE,
    )
    child.tool_limit_continuation = control
    child.continuation_execution_id = execution_id
    child.continuation_index = index
    child.root_session_id = root_session_id
    return child


def _child_snapshot(child: Session) -> dict:
    """Serializable reconstruction data for the receipt/session two-file gap."""
    return {
        key: copy.deepcopy(value)
        for key, value in child.__dict__.items()
        if not key.startswith("_")
    }


def _ensure_receipt_child(receipt: dict) -> Session | None:
    child_id = str(receipt.get("child_session_id") or "")
    if not child_id:
        return None
    child = Session.load(child_id)
    if child is not None:
        return child
    snapshot = receipt.get("child_snapshot")
    if not isinstance(snapshot, dict):
        return None
    snapshot = copy.deepcopy(snapshot)
    snapshot["session_id"] = child_id
    child = Session(**snapshot)
    child.save()
    return child


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


def _reserve_start(key: str) -> tuple[dict | None, str | None]:
    """Atomically reserve one claimed receipt for a single start attempt."""
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            return None, None
        state = receipt.get("state")
        if state not in ("claimed", "starting"):
            return copy.deepcopy(receipt), None
        if state == "starting" and _pid_is_alive(receipt.get("owner_pid")):
            return copy.deepcopy(receipt), None
        token = uuid.uuid4().hex
        now = time.time()
        receipt.update(
            {
                "state": "starting",
                "owner_pid": os.getpid(),
                "owner_start_token": _process_start_token(os.getpid()),
                "owner_thread": threading.get_ident(),
                "start_token": token,
                "launch_phase": "reserved",
                "starting_at": now,
                "updated_at": now,
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


def _response_confirms_started_child(
    receipt: dict,
    response: dict,
    *,
    require_session_id: bool = False,
) -> tuple[bool, str | None]:
    """Accept a launch only with a stream id, including a proven replayed 409."""
    try:
        status = int(response.get("_status", 200) or 200)
    except (TypeError, ValueError):
        status = 500
    stream_id = response.get("stream_id") or response.get("active_stream_id")
    if require_session_id and str(response.get("session_id") or "") != str(
        receipt.get("child_session_id") or ""
    ):
        return False, None
    if status < 400:
        return bool(stream_id), str(stream_id) if stream_id else None
    if status != 409 or not stream_id:
        return False, None

    # A process may die after start_session_turn persisted the hidden child
    # owner but before registering its worker. The sidecar fields alone cannot
    # distinguish that crash window from a live turn. Reconcile a 409 only when
    # the route explicitly proved the exact stream exists in this process's
    # STREAMS/ACTIVE_RUNS registry; a stale sidecar remains retryable.
    if response.get("active_stream_confirmed_live") is not True:
        return False, None
    child = Session.load(str(receipt.get("child_session_id") or ""))
    if child is None:
        return False, None
    child_stream_id = str(getattr(child, "active_stream_id", None) or "")
    child_source = str(getattr(child, "pending_user_source", None) or "")
    confirmed = child_stream_id == str(stream_id) and child_source == SOURCE
    return confirmed, str(stream_id) if confirmed else None


def _finish_start(
    key: str,
    token: str,
    response: dict | None,
    *,
    require_session_id: bool = False,
) -> tuple[dict | None, bool]:
    response = response if isinstance(response, dict) else {}
    with _store_lock():
        current = copy.deepcopy(_load_store()["receipts"].get(key))
    if current is None:
        return None, False
    succeeded, stream_id = _response_confirms_started_child(
        current, response, require_session_id=require_session_id
    )
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            return None, False
        if receipt.get("state") != "starting" or receipt.get("start_token") != token:
            return copy.deepcopy(receipt), False
        now = time.time()
        if succeeded:
            receipt.update(
                {
                    "state": "started",
                    "child_stream_id": stream_id,
                    "started_at": now,
                    "updated_at": now,
                    "completed_start_token": token,
                }
            )
        else:
            # Explicit launch failures and unproven 409s remain durable claims.
            # Startup recovery or a duplicate terminal callback can retry once
            # the child session is idle.
            receipt.update({"state": "claimed", "updated_at": now})
            receipt.pop("child_stream_id", None)
            receipt.pop("completed_start_token", None)
        for field in (
            "owner_pid",
            "owner_start_token",
            "owner_thread",
            "start_token",
            "starting_at",
            "launch_phase",
        ):
            receipt.pop(field, None)
        _save_store(store)
        return copy.deepcopy(receipt), succeeded


def _blocked_reason_text(reason: str | None) -> str:
    return {
        "disabled": "Automatic continuation is disabled.",
        "max_segments": "The maximum continuation segment count was reached.",
        "max_wall_seconds": "The maximum continuation time was reached.",
        "no_progress": "No machine-verifiable progress was detected.",
        "child_recovery_failed": "The durable continuation child could not be recovered.",
        "continuation_state_unavailable": "Durable continuation state could not be claimed safely.",
    }.get(str(reason or ""), "The continuation safety envelope stopped this run.")


def _persist_blocked_terminal(session, reason: str | None) -> None:
    """Make a genuine blocker durable in settled/replayed transcript state."""
    if session is None:
        return
    reason = str(reason or "blocked")
    detail = _blocked_reason_text(reason)
    status_card = {
        "title": "Tool-limit continuation stopped",
        "subtitle": detail,
        "rows": [
            {"label": "State", "value": "Limit reached"},
            {"label": "Reason", "value": reason.replace("_", " ")},
            {"label": "Next step", "value": "Review progress and start a narrower follow-up."},
        ],
    }
    messages = list(getattr(session, "messages", None) or [])
    target = None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("_error"):
            if (
                message.get("_terminal_state") == "tool_limit_reached"
                and message.get("_terminal_reason") == reason
            ):
                return
            continue
        content = message.get("content")
        text = "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        ) if isinstance(content, list) else str(content or "")
        if not message.get("tool_calls") and text.strip():
            target = message
            break
    if target is None:
        target = {
            "role": "assistant",
            "content": f"Tool-limit continuation stopped. {detail}",
            "timestamp": int(time.time()),
            "_error": True,
        }
        messages.append(target)
    target["_terminal_state"] = "tool_limit_reached"
    target["_terminal_reason"] = reason
    target["_statusCard"] = status_card
    session.messages = messages
    session.save()


def persist_terminal_failure(
    session,
    parent_run_id: str,
    *,
    reason: str = "continuation_state_unavailable",
    emit: Callable[[str, dict], None] | None = None,
) -> dict:
    """Persist and disclose a handoff failure without consulting the receipt store.

    This is the fail-closed escape hatch for receipt read/write failures. It
    deliberately avoids `_load_store()` so a corrupt ownership file cannot
    suppress the genuine terminal blocker.
    """
    meta = getattr(session, "tool_limit_continuation", None)
    if not isinstance(meta, dict):
        meta = {}
    receipt = {
        "execution_id": str(meta.get("execution_id") or uuid.uuid4().hex),
        "root_session_id": str(meta.get("root_session_id") or session.session_id),
        "parent_session_id": str(session.session_id),
        "parent_run_id": str(parent_run_id),
        "child_session_id": None,
        "continuation_index": int(meta.get("continuation_index") or 0) + 1,
        "state": "blocked",
        "blocked_reason": str(reason or "continuation_state_unavailable"),
    }
    _persist_blocked_terminal(session, receipt["blocked_reason"])
    _emit(receipt, emit)
    return receipt


def _block_unrecoverable_child(key: str, token: str) -> dict | None:
    result = None
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            return None
        if receipt.get("state") != "starting" or receipt.get("start_token") != token:
            return copy.deepcopy(receipt)
        _blocked_receipt(receipt, "child_recovery_failed")
        for field in (
            "owner_pid",
            "owner_start_token",
            "owner_thread",
            "start_token",
            "starting_at",
            "launch_phase",
        ):
            receipt.pop(field, None)
        _save_store(store)
        result = copy.deepcopy(receipt)
    parent = Session.load(str(result.get("parent_session_id") or ""))
    _persist_blocked_terminal(parent, result.get("blocked_reason"))
    return result


def _start_receipt(
    key: str,
    *,
    start: Callable[[str, str], dict] | None = None,
    managed_exact: bool = False,
    crash_hook: Callable[[str], None] | None = None,
) -> tuple[dict | None, bool]:
    """Reserve, reconstruct, and launch one receipt without holding its lock."""
    receipt, token = _reserve_start(key)
    if receipt is None or token is None:
        return receipt, False
    if managed_exact and crash_hook is not None:
        crash_hook("claim_committed")
    try:
        child = _ensure_receipt_child(receipt)
    except Exception:
        logger.exception(
            "tool-limit continuation child recovery failed for %s",
            receipt.get("child_session_id"),
        )
        child = None
    if child is None:
        return _block_unrecoverable_child(key, token), False

    starter = start
    if starter is None:
        from api.routes import start_session_turn

        starter = lambda sid, text: start_session_turn(sid, text, source=SOURCE)
    prompt = str(
        receipt.get("continuation_prompt")
        or "Continue the unfinished task from the parent segment. Use the inherited context, "
        "perform the remaining tool work and verification, and return a normal final answer."
    )
    receipt = _mark_launching(key, token)
    if receipt is None or receipt.get("launch_phase") != "launching":
        return receipt, False
    try:
        response = starter(str(receipt["child_session_id"]), prompt) or {}
    except Exception:
        logger.exception(
            "tool-limit continuation start failed for child %s",
            receipt.get("child_session_id"),
        )
        response = {"error": "continuation start failed", "_status": 500}
    if managed_exact and crash_hook is not None:
        crash_hook("launch_returned")
    result = _finish_start(
        key, token, response, require_session_id=managed_exact
    )
    if managed_exact and result[1] and crash_hook is not None:
        crash_hook("started_committed")
    return result


def _blocked_receipt(base: dict, reason: str) -> dict:
    base.update({"state": "blocked", "blocked_reason": reason, "child_session_id": None,
                 "updated_at": time.time()})
    return base


def handle_terminal(
    session,
    parent_run_id: str,
    *,
    tool_limit_reached: bool,
    start: Callable[[str, str], dict] | None = None,
    emit: Callable[[str, dict], None] | None = None,
    now: float | None = None,
) -> dict | None:
    """Settle one segment and, on exhaustion, claim/start exactly one child."""
    now = float(now if now is not None else time.time())
    meta = getattr(session, "tool_limit_continuation", None)
    if not isinstance(meta, dict):
        meta = {}
    execution_id = str(meta.get("execution_id") or uuid.uuid4().hex)
    root_id = str(meta.get("root_session_id") or session.session_id)
    current_index = int(meta.get("continuation_index") or 0)

    if not tool_limit_reached:
        if not meta:
            return None
        with _store_lock():
            store = _load_store()
            for receipt in store["receipts"].values():
                if receipt.get("execution_id") == execution_id and receipt.get("state") != "blocked":
                    receipt["state"] = "completed"
                    receipt["updated_at"] = now
            _save_store(store)
        completed = {
            "execution_id": execution_id, "root_session_id": root_id,
            "parent_session_id": getattr(session, "parent_session_id", None),
            "child_session_id": session.session_id, "continuation_index": current_index,
            "state": "completed",
        }
        _emit(completed, emit)
        return completed

    settings = settings_for_session(session)
    key = _claim_key(str(session.session_id), str(parent_run_id))
    prompt = (
        "Continue the unfinished task from the parent segment. Use the inherited context, "
        "perform the remaining tool work and verification, and return a normal final answer."
    )
    with _store_lock():
        store = _load_store()
        existing = store["receipts"].get(key)
        if existing is not None:
            receipt = dict(existing)
        else:
            chain = [r for r in store["receipts"].values() if r.get("execution_id") == execution_id]
            chain_started = min([float(r.get("chain_started_at", now)) for r in chain] or [now])
            next_index = current_index + 1
            fingerprint = progress_fingerprint(session)
            repeats = 0
            if fingerprint is not None:
                for prior in sorted(chain, key=lambda r: int(r.get("continuation_index", 0)), reverse=True):
                    if prior.get("progress_fingerprint") != fingerprint:
                        break
                    repeats += 1
            receipt = {
                "claim_key": key, "execution_id": execution_id, "profile": getattr(session, "profile", None),
                "root_session_id": root_id, "parent_session_id": session.session_id,
                "parent_run_id": str(parent_run_id), "child_session_id": None,
                "continuation_index": next_index, "chain_started_at": chain_started,
                "claimed_at": now, "updated_at": now, "progress_fingerprint": fingerprint,
                "state": "claimed",
            }
            if not settings["enabled"]:
                _blocked_receipt(receipt, "disabled")
            elif next_index > settings["max_segments"]:
                _blocked_receipt(receipt, "max_segments")
            elif now - chain_started >= settings["max_wall_seconds"]:
                _blocked_receipt(receipt, "max_wall_seconds")
            elif fingerprint is not None and repeats >= settings["no_progress_limit"]:
                _blocked_receipt(receipt, "no_progress")
            else:
                child = _new_child(session, execution_id=execution_id, root_session_id=root_id,
                                   index=next_index, prompt=prompt)
                receipt["child_session_id"] = child.session_id
                receipt["child_snapshot"] = _child_snapshot(child)
                receipt["continuation_prompt"] = prompt
                # Claim first. Recovery reconstructs this exact child id from
                # the snapshot if the process dies in the two-file gap.
                store["receipts"][key] = receipt
                _save_store(store)
                child.save()
            store["receipts"][key] = receipt
            _save_store(store)

    if receipt.get("state") == "blocked":
        _persist_blocked_terminal(session, receipt.get("blocked_reason"))
    _emit(receipt, emit)
    if receipt.get("state") in ("claimed", "starting"):
        latest, _started = _start_receipt(key, start=start)
        if latest is not None:
            return latest
    return receipt


def recover_pending_continuations(
    *, start: Callable[[str, str], dict] | None = None,
    emit: Callable[[str, dict], None] | None = None,
) -> int:
    """Restart each durable claimed-but-not-started child once."""
    with _store_lock():
        pending = [
            key
            for key, receipt in _load_store()["receipts"].items()
            if receipt.get("child_session_id")
            and (
                receipt.get("state") == "claimed"
                or (
                    receipt.get("state") == "starting"
                    and not _pid_is_alive(receipt.get("owner_pid"))
                )
            )
        ]
    started = 0
    for key in pending:
        receipt, did_start = _start_receipt(key, start=start)
        if did_start:
            started += 1
            if receipt is not None:
                _emit(receipt, emit)
    return started


_MANAGED_RECEIPT_FIELDS = {
    "claim_key", "execution_id", "profile", "root_session_id",
    "parent_session_id", "parent_run_id", "child_session_id",
    "child_snapshot", "continuation_prompt", "continuation_index",
    "chain_started_at", "claimed_at", "updated_at", "progress_fingerprint",
    "state", "blocked_reason", "owner_pid", "owner_start_token",
    "owner_thread", "start_token", "launch_phase", "starting_at",
    "child_stream_id", "started_at", "completed_start_token",
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
        raise ValueError("tool continuation store root schema is invalid")
    if store.get("version") != _RECEIPT_VERSION:
        raise ValueError("tool continuation store version is invalid")
    receipts = store.get("receipts")
    if (
        not isinstance(receipts, dict)
        or isinstance(max_receipts, bool)
        or not isinstance(max_receipts, int)
        or max_receipts < 1
        or len(receipts) > max_receipts
    ):
        raise ValueError("tool continuation receipt count is invalid")
    for key, receipt in receipts.items():
        if not isinstance(receipt, dict) or set(receipt) - _MANAGED_RECEIPT_FIELDS:
            raise ValueError(f"{key}: tool continuation receipt schema is invalid")
        claim_key = _managed_text(receipt, "claim_key")
        parent = _managed_text(receipt, "parent_session_id")
        run_id = _managed_text(receipt, "parent_run_id")
        if (
            key != claim_key
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or key != _claim_key(parent, run_id)
        ):
            raise ValueError(f"{key}: tool continuation claim identity is invalid")
        for field in ("execution_id", "root_session_id"):
            _managed_text(receipt, field)
        for field in ("claimed_at", "updated_at", "chain_started_at"):
            _managed_timestamp(receipt, field)
        index = receipt.get("continuation_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 1000000:
            raise ValueError(f"{key}: continuation_index is invalid")
        state = receipt.get("state")
        if state not in {"claimed", "starting", "started", "blocked", "completed"}:
            raise ValueError(f"{key}: tool continuation state is invalid")
        if state in {"claimed", "starting", "started"}:
            _managed_text(receipt, "child_session_id")
            if not isinstance(receipt.get("child_snapshot"), dict):
                raise ValueError(f"{key}: child_snapshot is invalid")
            _managed_text(receipt, "continuation_prompt")
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
        if state == "blocked":
            _managed_text(receipt, "blocked_reason")
    return receipts


def recover_managed_continuations_exact(
    *,
    transaction_id: str,
    manifest_sha256: str,
    start: Callable[[str, str], dict] | None = None,
    emit: Callable[[str, dict], None] | None = None,
    crash_hook: Callable[[str], None] | None = None,
):
    """Recover the exact bounded store under the managed startup transaction."""
    scope = _MANAGED_EXACT.set(True)
    try:
        result = recover_exact(
            path=_receipt_path(),
            store_lock=_store_lock,
            validate_store=_validate_managed_store,
            start_one=lambda key: _start_receipt(
                key,
                start=start,
                managed_exact=True,
                crash_hook=crash_hook,
            ),
            session_id_for=lambda receipt: str(
                receipt.get("child_session_id")
                or receipt.get("parent_session_id")
                or ""
            ),
            terminal_states={"blocked", "completed"},
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha256,
            max_receipts=_MAX_MANAGED_RECEIPTS,
            process_token_lookup=_process_start_token,
        )
        if emit is not None:
            with _store_lock():
                current = _load_store()["receipts"]
            for key in result.started_receipt_keys:
                receipt = current.get(key)
                if isinstance(receipt, dict):
                    _emit(receipt, emit)
        return result
    finally:
        _MANAGED_EXACT.reset(scope)


def verify_managed_continuations_exact(
    receipt,
    *,
    transaction_id: str,
    manifest_sha256: str,
):
    """Read-only verification of an exact managed tool continuation receipt."""
    return verify_exact(
        receipt,
        path=_receipt_path(),
        store_lock=_verification_store_lock,
        validate_store=_validate_managed_store,
        session_id_for=lambda row: str(
            row.get("child_session_id") or row.get("parent_session_id") or ""
        ),
        terminal_states={"blocked", "completed"},
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
        max_receipts=_MAX_MANAGED_RECEIPTS,
        process_token_lookup=_process_start_token,
    )


def load_receipts() -> dict:
    """Read-only test/diagnostic snapshot."""
    with _store_lock():
        return copy.deepcopy(_load_store())


def latest_receipt_for_session(session_id: str) -> dict | None:
    """Return the latest durable handoff involving one lineage segment.

    Session channels are intentionally memory-only, so a WebUI restart can
    lose the live handoff frame even though execution recovers from this
    receipt store. Reconnect callers use the latest segment to migrate straight
    to the active/final child; older segment receipts would only replay stale
    intermediate children.
    """
    session_id = str(session_id or "")
    if not session_id:
        return None
    with _store_lock():
        candidates = [
            receipt
            for receipt in _load_store()["receipts"].values()
            if session_id in {
                str(receipt.get("root_session_id") or ""),
                str(receipt.get("parent_session_id") or ""),
                str(receipt.get("child_session_id") or ""),
            }
        ]
        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda receipt: (
                float(receipt.get("updated_at") or receipt.get("claimed_at") or 0),
                int(receipt.get("continuation_index") or 0),
            ),
        )
        return copy.deepcopy(latest)


def replay_frames_for_session(
    session_id: str,
    *,
    active_stream_for_session: Callable[[str], str | None],
    session_updated_at: float | None = None,
) -> list[tuple[str, dict]]:
    """Build idempotent reconnect frames from durable lineage state.

    A settled receipt is historical once its displayed ancestor has newer
    activity.  Suppressing that stale replay prevents a later human turn on
    the root/intermediate session from being redirected back to an old final
    child.  In-flight receipts still replay regardless of ancestor recency so
    a recovered execution remains attachable.
    """
    receipt = latest_receipt_for_session(session_id)
    if receipt is None:
        return []
    if (
        receipt.get("state") in {"completed", "blocked"}
        and str(session_id or "") != str(receipt.get("child_session_id") or "")
        and session_updated_at is not None
    ):
        try:
            receipt_updated_at = float(
                receipt.get("updated_at") or receipt.get("claimed_at") or 0
            )
            if float(session_updated_at) > receipt_updated_at:
                return []
        except (TypeError, ValueError):
            pass
    frames = [("tool_limit_continuation", _event_payload(receipt))]
    child_session_id = str(receipt.get("child_session_id") or "")
    if not child_session_id:
        return frames
    try:
        stream_id = str(active_stream_for_session(child_session_id) or "")
    except Exception:
        logger.debug(
            "tool-limit continuation reconnect could not inspect child stream %s",
            child_session_id,
            exc_info=True,
        )
        stream_id = ""
    if stream_id:
        frames.append((
            "server_turn_started",
            {
                "session_id": child_session_id,
                "child_session_id": child_session_id,
                "stream_id": stream_id,
                "execution_id": receipt.get("execution_id"),
                "root_session_id": receipt.get("root_session_id"),
                "parent_session_id": receipt.get("parent_session_id"),
                "source": "subscribe_recovery",
                "recovered": True,
            },
        ))
    return frames
