"""Durable ownership for transparent same-session compression recovery."""

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
    strict_store_lock,
    strict_store_save,
    verify_exact,
)
from api.process_identity import process_start_token


logger = logging.getLogger(__name__)

SOURCE = "compression_recovery"
CONTROL_KEY = "_compression_recovery_control"
RECOVERY_CONTROL_PROMPT = (
    "Continue the exact unfinished request already present above. Inspect the "
    "current workspace and existing results before repeating any action."
)

_RECEIPT_VERSION = 1
_MAX_RECEIPTS = 4096
_MAX_STORE_BYTES = 16 * 1024 * 1024
_LOCK = threading.RLock()
_MANAGED_EXACT = ContextVar("compression_recovery_managed_exact", default=False)
_BLOCKING_DISCARD_REASONS = frozenset(
    {
        "ambiguous_submitted_successor",
        "ambiguous_launch_response",
        "ambiguous_started_successor",
        "ambiguous_human_supersession",
        "recovery_attachment_unavailable",
        "recovery_reservation_mismatch",
        "recovery_successor_exhausted",
    }
)


class CompressionRecoveryReceiptStoreError(RuntimeError):
    """Raised when durable compression-recovery ownership is unavailable."""


class CompressionRecoverySupersessionConflict(
    CompressionRecoveryReceiptStoreError
):
    """Raised before mutation when human and recovery attachments conflict."""


class CompressionRecoveryAdmissionBlocked(
    CompressionRecoveryReceiptStoreError
):
    """Raised after an exact reservation is durably blocked at admission."""

    def __init__(self, reason: str, receipt: dict):
        self.reason = str(reason or "recovery_admission_invalid")
        self.receipt = copy.deepcopy(receipt)
        super().__init__(self.reason)


class CompressionRecoveryInProgress(CompressionRecoveryReceiptStoreError):
    """Raised when a demonstrably live recovery still owns the task."""


def _receipt_path() -> Path:
    return Path(config.SESSION_DIR) / "_compression_recoveries.json"


def _lock_path() -> Path:
    return Path(config.SESSION_DIR) / "_compression_recoveries.lock"


@contextmanager
def _store_lock():
    if _MANAGED_EXACT.get():
        with strict_store_lock(_lock_path(), _LOCK):
            yield
        return
    with _LOCK:
        Path(config.SESSION_DIR).mkdir(parents=True, exist_ok=True)
        handle = open(_lock_path(), "a+b")
        try:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            if os.name == "nt":  # pragma: no cover - Windows compatibility
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


@contextmanager
def _verification_store_lock():
    with strict_store_lock(_lock_path(), _LOCK, create=False):
        yield


def _empty_store() -> dict:
    return {"version": _RECEIPT_VERSION, "receipts": {}}


def _claim_key(session_id: str, parent_run_id: str) -> str:
    return hashlib.sha256(f"{session_id}\0{parent_run_id}".encode("utf-8")).hexdigest()


def _bounded_text(value, *, field: str, maximum: int = 65_536) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} must be bounded non-empty text")
    return value


def _validate_seed(seed: object, *, session_id: str, parent_run_id: str) -> dict:
    if not isinstance(seed, dict) or set(seed) != {
        "session_id",
        "parent_run_id",
        "context_messages",
        "attachments",
        "trust_source",
        "fingerprint",
    }:
        raise ValueError("compression recovery seed schema is invalid")
    if seed.get("session_id") != session_id or seed.get("parent_run_id") != parent_run_id:
        raise ValueError("compression recovery seed identity is invalid")
    context_messages = seed.get("context_messages")
    attachments = seed.get("attachments")
    if (
        not isinstance(context_messages, list)
        or not context_messages
        or not isinstance(attachments, list)
        or len(attachments) > 20
    ):
        raise ValueError("compression recovery seed content is invalid")
    try:
        encoded = json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("compression recovery seed is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > 512_000:
        raise ValueError("compression recovery seed is too large")
    fingerprint = _bounded_text(seed.get("fingerprint"), field="fingerprint", maximum=64)
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise ValueError("compression recovery seed fingerprint is invalid")
    if seed.get("trust_source") not in {"summary", "assistant_checkpoint", "user_request"}:
        raise ValueError("compression recovery trust source is invalid")
    if not isinstance(context_messages[-1], dict) or context_messages[-1].get("role") != "user":
        raise ValueError("compression recovery seed must end at the user request")

    from api.compression_recovery import _recovery_fingerprint

    expected = _recovery_fingerprint(
        session_id=session_id,
        parent_run_id=parent_run_id,
        context_messages=context_messages,
        attachments=attachments,
    )
    if expected != fingerprint:
        raise ValueError("compression recovery seed fingerprint does not match")
    return copy.deepcopy(seed)


def _validate_receipt(key: str, receipt: object) -> None:
    if not isinstance(receipt, dict):
        raise ValueError(f"{key}: compression recovery receipt must be an object")
    allowed = {
        "claim_key",
        "session_id",
        "parent_run_id",
        "profile",
        "source",
        "state",
        "fingerprint",
        "presentation_session_id",
        "seed",
        "claimed_at",
        "updated_at",
        "owner_pid",
        "owner_start_token",
        "owner_thread",
        "start_token",
        "launch_phase",
        "launch_mode",
        "starting_at",
        "child_stream_id",
        "child_turn_id",
        "started_at",
        "completed_start_token",
        "discarded_reason",
        "discarded_at",
    }
    if set(receipt) - allowed:
        raise ValueError(f"{key}: compression recovery receipt schema is invalid")
    session_id = _bounded_text(receipt.get("session_id"), field="session_id")
    parent_run_id = _bounded_text(receipt.get("parent_run_id"), field="parent_run_id")
    if key != receipt.get("claim_key") or key != _claim_key(session_id, parent_run_id):
        raise ValueError(f"{key}: compression recovery claim identity is invalid")
    if receipt.get("source") != SOURCE:
        raise ValueError(f"{key}: compression recovery source is invalid")
    profile = receipt.get("profile")
    if profile is not None and (not isinstance(profile, str) or len(profile) > 256):
        raise ValueError(f"{key}: compression recovery profile is invalid")
    if "presentation_session_id" in receipt:
        _bounded_text(
            receipt.get("presentation_session_id"),
            field="presentation_session_id",
        )
    state = receipt.get("state")
    if state not in {"claimed", "starting", "started", "discarded"}:
        raise ValueError(f"{key}: compression recovery state is invalid")
    fingerprint = _bounded_text(
        receipt.get("fingerprint"),
        field="fingerprint",
        maximum=64,
    )
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError(f"{key}: compression recovery fingerprint is invalid")
    compact_terminal = (
        state == "discarded"
        and str(receipt.get("discarded_reason") or "")
        not in _BLOCKING_DISCARD_REASONS
        and "seed" not in receipt
    )
    if not compact_terminal:
        _validate_seed(
            receipt.get("seed"),
            session_id=session_id,
            parent_run_id=parent_run_id,
        )
        if fingerprint != receipt["seed"]["fingerprint"]:
            raise ValueError(f"{key}: compression recovery fingerprint is invalid")
    for field in ("claimed_at", "updated_at"):
        if isinstance(receipt.get(field), bool) or not isinstance(receipt.get(field), (int, float)):
            raise ValueError(f"{key}: {field} is invalid")
    if state == "starting":
        if (
            isinstance(receipt.get("owner_pid"), bool)
            or not isinstance(receipt.get("owner_pid"), int)
            or receipt["owner_pid"] <= 1
        ):
            raise ValueError(f"{key}: compression recovery owner is invalid")
        _bounded_text(receipt.get("start_token"), field="start_token")
        _bounded_text(receipt.get("owner_start_token"), field="owner_start_token")
        if receipt.get("launch_phase") not in {"reserved", "launching"}:
            raise ValueError(f"{key}: compression recovery launch phase is invalid")
        if receipt.get("launch_mode", "automatic") not in {
            "automatic",
            "human_supersession",
        }:
            raise ValueError(f"{key}: compression recovery launch mode is invalid")
    if state == "started":
        _bounded_text(receipt.get("child_stream_id"), field="child_stream_id")
    if state == "discarded":
        _bounded_text(receipt.get("discarded_reason"), field="discarded_reason")


def _validate_store(store: object) -> dict:
    if (
        not isinstance(store, dict)
        or set(store) != {"version", "receipts"}
        or store.get("version") != _RECEIPT_VERSION
        or not isinstance(store.get("receipts"), dict)
        or len(store["receipts"]) > _MAX_RECEIPTS
    ):
        raise ValueError("compression recovery receipt store schema is invalid")
    for key, receipt in store["receipts"].items():
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(ch not in "0123456789abcdef" for ch in key)
        ):
            raise ValueError("compression recovery receipt key is invalid")
        _validate_receipt(key, receipt)
    return store


def _validate_managed_store(store: dict, *, max_receipts: int) -> dict:
    validated = _validate_store(store)
    rows = validated["receipts"]
    if (
        isinstance(max_receipts, bool)
        or not isinstance(max_receipts, int)
        or max_receipts < 1
        or len(rows) > max_receipts
    ):
        raise ValueError("compression recovery managed receipt count is invalid")
    for key, receipt in rows.items():
        for field in ("claimed_at", "updated_at"):
            value = receipt.get(field)
            if not math.isfinite(float(value)):
                raise ValueError(f"{key}: {field} must be finite")
        if receipt.get("state") == "starting":
            value = receipt.get("starting_at")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{key}: starting_at is invalid")
        if receipt.get("state") == "started":
            value = receipt.get("started_at")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{key}: started_at is invalid")
            if "completed_start_token" in receipt:
                _bounded_text(
                    receipt.get("completed_start_token"),
                    field="completed_start_token",
                )
        if receipt.get("state") == "discarded":
            value = receipt.get("discarded_at")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{key}: discarded_at is invalid")
    return rows


def _load_store() -> dict:
    path = _receipt_path()
    if _MANAGED_EXACT.get():
        return _validate_store(stable_store_snapshot(path, max_bytes=_MAX_STORE_BYTES)[0])
    try:
        if path.stat().st_size > _MAX_STORE_BYTES:
            raise ValueError("compression recovery receipt store is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _validate_store(raw)
    except FileNotFoundError:
        return _empty_store()
    except Exception as exc:
        raise CompressionRecoveryReceiptStoreError(
            f"compression recovery receipt store is unreadable: {path}"
        ) from exc


def _save_store(store: dict) -> None:
    for receipt in store.get("receipts", {}).values():
        if isinstance(receipt, dict):
            _compact_nonblocking_terminal_receipt(receipt)
    _validate_store(store)
    path = _receipt_path()
    if _MANAGED_EXACT.get():
        strict_store_save(path, store)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    if len(payload.encode("utf-8")) > _MAX_STORE_BYTES:
        raise CompressionRecoveryReceiptStoreError(
            "compression recovery receipt store exceeds its size bound"
        )
    temp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _session_phase_payload(receipt: dict, phase: str, *, reason: str = "") -> dict:
    payload = {
        "terminal_state": "compression_exhausted",
        "phase": phase,
        "automatic_recovery": phase in {"claimed", "starting", "running"},
        "same_session": True,
        "source_session_id": receipt.get("session_id"),
        "parent_run_id": receipt.get("parent_run_id"),
        "claim_key": receipt.get("claim_key"),
        "fingerprint": receipt.get("fingerprint"),
        "created_at": receipt.get("claimed_at"),
        "title": "Recovering context" if phase != "blocked" else "Context recovery blocked",
        "summary": (
            "Hermes is continuing this request in the same conversation."
            if phase != "blocked"
            else "Hermes could not safely reconstruct enough context to continue automatically."
        ),
    }
    if reason:
        payload["reason"] = str(reason)[:1200]
    return payload


def _persist_session_phase(session, receipt: dict, phase: str, *, reason: str = "") -> bool:
    session.compression_recovery = _session_phase_payload(receipt, phase, reason=reason)
    session.recommended_recovery_action = None
    try:
        session.save(touch_updated_at=False)
        return True
    except Exception:
        if _MANAGED_EXACT.get():
            raise CompressionRecoveryReceiptStoreError(
                "managed compression recovery session phase could not be persisted"
            ) from None
        logger.exception(
            "failed to persist compression recovery phase %s for session %s",
            phase,
            receipt.get("session_id"),
        )
        return False


def _discard_requires_blocker(receipt: dict) -> bool:
    return (
        receipt.get("state") == "discarded"
        and str(receipt.get("discarded_reason") or "") in _BLOCKING_DISCARD_REASONS
    )


def _compact_nonblocking_terminal_receipt(receipt: dict) -> None:
    """Keep an idempotency tombstone without retained conversation content."""

    if receipt.get("state") != "discarded" or _discard_requires_blocker(receipt):
        return
    for field in (
        "seed",
        "owner_pid",
        "owner_start_token",
        "owner_thread",
        "start_token",
        "launch_phase",
        "launch_mode",
        "starting_at",
        "child_stream_id",
        "child_turn_id",
        "started_at",
        "completed_start_token",
    ):
        receipt.pop(field, None)


def _prune_old_terminal_tombstones(store: dict, *, protect_key: str) -> None:
    """Make bounded room for one claim without dropping live/blocking authority."""

    for receipt in store["receipts"].values():
        _compact_nonblocking_terminal_receipt(receipt)

    def _encoded_size() -> int:
        return len(
            json.dumps(
                store,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
        )

    def _over_limit() -> bool:
        return (
            len(store["receipts"]) > _MAX_RECEIPTS
            or _encoded_size() > _MAX_STORE_BYTES
        )

    prunable = sorted(
        (
            (key, receipt)
            for key, receipt in store["receipts"].items()
            if key != protect_key
            and receipt.get("state") == "discarded"
            and not _discard_requires_blocker(receipt)
        ),
        key=lambda item: (
            float(item[1].get("discarded_at") or item[1].get("updated_at") or 0),
            item[0],
        ),
    )
    while _over_limit() and prunable:
        key, _receipt = prunable.pop(0)
        store["receipts"].pop(key, None)
    if _over_limit():
        raise CompressionRecoveryReceiptStoreError(
            "compression recovery receipt store has no safely prunable capacity"
        )


def _session_recovery_matches_receipt(session, receipt: dict) -> bool:
    recovery = dict(getattr(session, "compression_recovery", None) or {})
    claim_key = str(recovery.get("claim_key") or "")
    fingerprint = str(recovery.get("fingerprint") or "")
    parent_run_id = str(recovery.get("parent_run_id") or "")
    return bool(
        (claim_key and claim_key == receipt.get("claim_key"))
        or (fingerprint and fingerprint == receipt.get("fingerprint"))
        or (
            parent_run_id
            and parent_run_id == str(receipt.get("parent_run_id") or "")
        )
    )


def _reconcile_receipt_presentation(receipt: dict) -> None:
    """Repair crash-stale recovery presentation from terminal receipt truth."""

    if receipt.get("state") != "discarded":
        return
    reason = str(receipt.get("discarded_reason") or "")
    if reason in {"superseded_by_user", "session_deleted"}:
        return
    presentation_sid = str(receipt.get("presentation_session_id") or "")
    source_sid = str(receipt.get("session_id") or "")
    candidate_sids = list(dict.fromkeys(sid for sid in (presentation_sid, source_sid) if sid))
    from api.models import get_session

    for sid in candidate_sids:
        try:
            with config._get_session_agent_lock(sid):
                try:
                    session = get_session(sid)
                except KeyError:
                    continue
                current_recovery = dict(
                    getattr(session, "compression_recovery", None) or {}
                )
                if (
                    not _session_recovery_matches_receipt(session, receipt)
                    and not (_discard_requires_blocker(receipt) and not current_recovery)
                ):
                    return
                active_stream_id = str(
                    getattr(session, "active_stream_id", None) or ""
                )
                pending_source = str(
                    getattr(session, "pending_user_source", None) or ""
                )
                if active_stream_id and pending_source not in {"", SOURCE}:
                    return
                if _discard_requires_blocker(receipt):
                    _persist_session_phase(session, receipt, "blocked", reason=reason)
                    return
                session.compression_recovery = {}
                session.recommended_recovery_action = None
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    if _MANAGED_EXACT.get():
                        raise CompressionRecoveryReceiptStoreError(
                            "managed compression recovery terminal presentation could not be persisted"
                        ) from None
                    logger.exception(
                        "failed to clear settled compression recovery presentation for %s",
                        sid,
                    )
                return
        except Exception:
            if _MANAGED_EXACT.get():
                raise
            logger.exception(
                "failed to reconcile compression recovery presentation for %s",
                sid,
            )
            return
    if _MANAGED_EXACT.get():
        raise CompressionRecoveryReceiptStoreError(
            "managed compression recovery presentation session is unavailable"
        )


def claim_compression_recovery(session, parent_run_id: str, seed: dict) -> dict:
    """Persist one idempotent claim before exposing automatic recovery."""

    session_id = str(getattr(session, "session_id", "") or "").strip()
    parent_id = str(parent_run_id or "").strip()
    if not session_id or not parent_id:
        raise ValueError("session_id and parent_run_id are required")
    validated_seed = _validate_seed(seed, session_id=session_id, parent_run_id=parent_id)
    key = _claim_key(session_id, parent_id)
    now = time.time()
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None:
            receipt = {
                "claim_key": key,
                "session_id": session_id,
                "parent_run_id": parent_id,
                "profile": str(getattr(session, "profile", None) or "default"),
                "source": SOURCE,
                "state": "claimed",
                "fingerprint": validated_seed["fingerprint"],
                "seed": validated_seed,
                "claimed_at": now,
                "updated_at": now,
            }
            store["receipts"][key] = receipt
            _prune_old_terminal_tombstones(store, protect_key=key)
            _save_store(store)
        elif (
            receipt.get("fingerprint") != validated_seed["fingerprint"]
            or (
                receipt.get("seed") is not None
                and receipt.get("seed") != validated_seed
            )
        ):
            raise CompressionRecoveryReceiptStoreError(
                "compression recovery duplicate claim conflicts with durable ownership"
            )
        result = copy.deepcopy(receipt)
    if result.get("state") == "started":
        result = _reconcile_started_receipt(key) or result
        _reconcile_receipt_presentation(result)
    state = str(result.get("state") or "")
    if state == "claimed":
        disposition = "pending"
        _persist_session_phase(session, result, "claimed")
    elif state == "starting":
        disposition = "pending"
        current = dict(getattr(session, "compression_recovery", None) or {})
        if current.get("phase") not in {"starting", "running"}:
            _persist_session_phase(session, result, "starting")
    elif state == "started":
        disposition = "running"
    elif _discard_requires_blocker(result):
        disposition = "blocked"
        _persist_session_phase(
            session,
            result,
            "blocked",
            reason=str(result.get("discarded_reason") or ""),
        )
    else:
        disposition = "settled"
    result["claim_disposition"] = disposition
    return result


def _pid_is_alive(pid: int | None) -> bool:
    try:
        pid = int(pid or 0)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _owner_is_live(receipt: dict) -> bool:
    pid = receipt.get("owner_pid")
    if not _pid_is_alive(pid):
        return False
    expected = str(receipt.get("owner_start_token") or "")
    actual = process_start_token(int(pid)) if expected else None
    if not (expected and actual == expected):
        return False
    if int(pid) == os.getpid():
        owner_thread = receipt.get("owner_thread")
        if isinstance(owner_thread, bool) or not isinstance(owner_thread, int):
            return False
        return any(
            thread.ident == owner_thread and thread.is_alive()
            for thread in threading.enumerate()
        )
    return True


def _submitted_outcome(receipt: dict) -> tuple[str, dict | None]:
    sid = str(receipt.get("session_id") or "")
    token = str(
        receipt.get("start_token")
        or receipt.get("completed_start_token")
        or ""
    )
    fingerprint = str(receipt.get("fingerprint") or "")
    try:
        from api.turn_journal import read_turn_journal

        journal = read_turn_journal(sid)
        if journal.get("malformed"):
            return "ambiguous", None
        events = list(journal.get("events") or [])
    except Exception:
        logger.exception("compression recovery turn-journal reconciliation failed")
        return "ambiguous", None
    # A fingerprint identifies the recovery seed, not one launch attempt. An
    # exact launch_failed retry deliberately keeps that fingerprint and rotates
    # the start token, so older attempts must not poison the current one.
    identity_events = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("source") == SOURCE
        and str(event.get("recovery_claim_token") or "") == token
    ]
    if any(
        str(event.get("recovery_claim_token") or "") != token
        or str(event.get("recovery_fingerprint") or "") != fingerprint
        for event in identity_events
    ):
        return "ambiguous", identity_events[-1] if identity_events else None
    matches = [
        event for event in identity_events if event.get("event") == "submitted"
    ]
    if not matches:
        return "absent", None
    if len(matches) != 1:
        return "ambiguous", matches[-1]
    submitted = matches[0]
    stream_id = str(submitted.get("stream_id") or "")
    turn_id = str(submitted.get("turn_id") or "")
    if (
        not stream_id
        or not turn_id
        or submitted.get("role") != "user"
        or submitted.get("content") != RECOVERY_CONTROL_PROMPT
        or submitted.get("attachments") != receipt["seed"]["attachments"]
        or str(submitted.get("profile") or "default")
        != str(receipt.get("profile") or "default")
    ):
        return "ambiguous", submitted
    exact = [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("stream_id") or "") == stream_id
        and str(event.get("turn_id") or "") == turn_id
    ]
    if any(
        str(event.get("stream_id") or "") == stream_id
        and str(event.get("turn_id") or "") != turn_id
        for event in events
        if isinstance(event, dict)
    ):
        return "ambiguous", submitted
    terminals = [
        event for event in exact if event.get("event") in {"completed", "interrupted"}
    ]
    durable_terminals = [
        event
        for event in terminals
        if event.get("recovery_terminal_persisted") is True
    ]
    if len(terminals) == 1 and len(durable_terminals) == 1:
        evidence = copy.deepcopy(submitted)
        evidence["_terminal_event"] = copy.deepcopy(durable_terminals[0])
        return "terminal", evidence
    if terminals:
        return "ambiguous", submitted
    launch_failed = [event for event in exact if event.get("event") == "launch_failed"]
    if len(launch_failed) == 1 and all(
        event.get("event") in {"submitted", "launch_failed"} for event in exact
    ):
        return "launch_failed", submitted
    if launch_failed:
        return "ambiguous", submitted
    try:
        with config.STREAMS_LOCK:
            stream_live = stream_id in config.STREAMS
        with config.ACTIVE_RUNS_LOCK:
            active_live = stream_id in (config.ACTIVE_RUNS or {})
        if stream_live or active_live:
            return "live", submitted
    except Exception:
        return "ambiguous", submitted
    try:
        from api.run_journal import read_run_events, select_authoritative_terminal_event

        run = read_run_events(sid, stream_id)
        if run.get("malformed"):
            return "ambiguous", submitted
        run_events = list(run.get("events") or [])
        run_terminal = select_authoritative_terminal_event(run_events)
        if (
            run_terminal is not None
            and run_terminal.get("recovery_terminal_persisted") is True
        ):
            evidence = copy.deepcopy(submitted)
            evidence["_run_terminal_event"] = copy.deepcopy(run_terminal)
            return "terminal", evidence
        if run_events:
            return "ambiguous", submitted
    except Exception:
        logger.exception("compression recovery run-journal reconciliation failed")
        return "ambiguous", submitted
    return "ambiguous", submitted


def _human_supersession_submission_state(receipt: dict) -> tuple[str, str]:
    """Classify the exact WebUI worker bound to a failed handoff commit."""

    if (
        receipt.get("state") != "starting"
        or receipt.get("launch_mode") != "human_supersession"
    ):
        return "absent", ""
    sid = str(
        receipt.get("presentation_session_id")
        or receipt.get("session_id")
        or ""
    )
    token = str(receipt.get("start_token") or "")
    fingerprint = str(receipt.get("fingerprint") or "")
    try:
        from api.turn_journal import read_turn_journal

        journal = read_turn_journal(sid)
        if journal.get("malformed"):
            return "ambiguous", ""
        matches = [
            event
            for event in list(journal.get("events") or [])
            if isinstance(event, dict)
            and event.get("event") == "submitted"
            and event.get("source") == "webui"
            and event.get("role") == "user"
            and str(event.get("recovery_claim_token") or "") == token
            and str(event.get("recovery_fingerprint") or "") == fingerprint
            and str(event.get("stream_id") or "")
            and str(event.get("turn_id") or "")
        ]
        if not matches:
            return "absent", ""
        if len(matches) != 1:
            return "ambiguous", ""
        stream_id = str(matches[0].get("stream_id") or "")
        with config.STREAMS_LOCK:
            stream_live = stream_id in config.STREAMS
        with config.ACTIVE_RUNS_LOCK:
            active_live = stream_id in (config.ACTIVE_RUNS or {})
        return ("live", stream_id) if stream_live or active_live else (
            "submitted",
            stream_id,
        )
    except Exception:
        logger.exception("human compression supersession liveness check failed")
        return "ambiguous", ""


def _reset_start_fields(receipt: dict) -> None:
    for field in (
        "owner_pid",
        "owner_start_token",
        "owner_thread",
        "start_token",
        "launch_phase",
        "launch_mode",
        "starting_at",
    ):
        receipt.pop(field, None)


def _terminal_presentation_session_id(evidence: dict | None) -> str:
    evidence = evidence if isinstance(evidence, dict) else {}
    terminal = dict(
        evidence.get("_terminal_event")
        or evidence.get("_run_terminal_event")
        or {}
    )
    value = str(terminal.get("recovery_presentation_session_id") or "").strip()
    if not value or len(value.encode("utf-8")) > 65_536:
        return ""
    return value


def _reconcile_dead_starting(store: dict, key: str, receipt: dict) -> str:
    outcome, submitted = _submitted_outcome(receipt)
    now = time.time()
    start_token = str(receipt.get("start_token") or "")
    if receipt.get("launch_mode") == "human_supersession":
        human_state, _human_stream = _human_supersession_submission_state(receipt)
        if human_state in {"ambiguous", "live"}:
            return "starting"
        if human_state == "submitted":
            receipt.update(
                {
                    "state": "discarded",
                    "discarded_reason": "superseded_by_user",
                    "discarded_at": now,
                    "updated_at": now,
                }
            )
            _reset_start_fields(receipt)
            _save_store(store)
            return "discarded"
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": "ambiguous_human_supersession",
                "discarded_at": now,
                "updated_at": now,
            }
        )
        _reset_start_fields(receipt)
        _save_store(store)
        return "discarded"
    if outcome in {"absent", "launch_failed"}:
        receipt.update({"state": "claimed", "updated_at": now})
        _reset_start_fields(receipt)
        _save_store(store)
        return "claimed"
    if outcome == "live" and submitted is not None:
        receipt.update(
            {
                "state": "started",
                "child_stream_id": str(submitted.get("stream_id") or "reconciled"),
                "child_turn_id": str(submitted.get("turn_id") or ""),
                "started_at": now,
                "completed_start_token": start_token,
                "updated_at": now,
            }
        )
        _reset_start_fields(receipt)
        _save_store(store)
        return "started"
    if outcome == "terminal" and submitted is not None:
        terminal = dict(submitted.get("_terminal_event") or {})
        terminal_reason = str(terminal.get("reason") or "")
        discard_reason = (
            "recovery_successor_exhausted"
            if terminal_reason == "compression_exhausted"
            else "successor_settled"
        )
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": discard_reason,
                "discarded_at": now,
                "updated_at": now,
            }
        )
        presentation_sid = _terminal_presentation_session_id(submitted)
        if presentation_sid:
            receipt["presentation_session_id"] = presentation_sid
        _reset_start_fields(receipt)
        _save_store(store)
        return "discarded"
    receipt.update(
        {
            "state": "discarded",
            "discarded_reason": "ambiguous_submitted_successor",
            "discarded_at": now,
            "updated_at": now,
        }
    )
    _reset_start_fields(receipt)
    _save_store(store)
    return "discarded"


def _reserve_start(key: str) -> tuple[dict | None, str | None]:
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None or receipt.get("state") in {"started", "discarded"}:
            return copy.deepcopy(receipt) if receipt else None, None
        if receipt.get("state") == "starting":
            if _owner_is_live(receipt):
                return copy.deepcopy(receipt), None
            reconciled = _reconcile_dead_starting(store, key, receipt)
            if reconciled != "claimed":
                return copy.deepcopy(receipt), None
        token = uuid.uuid4().hex
        owner_token = process_start_token(os.getpid())
        if not owner_token:
            raise CompressionRecoveryReceiptStoreError(
                "compression recovery process identity is unavailable"
            )
        now = time.time()
        receipt.update(
            {
                "state": "starting",
                "owner_pid": os.getpid(),
                "owner_start_token": owner_token,
                "owner_thread": threading.get_ident(),
                "start_token": token,
                "launch_phase": "reserved",
                "launch_mode": "automatic",
                "starting_at": now,
                "updated_at": now,
            }
        )
        _save_store(store)
        return copy.deepcopy(receipt), token


def _reconcile_started_receipt(key: str) -> dict | None:
    """Classify an orphaned accepted start without ever replaying it."""

    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if receipt is None or receipt.get("state") != "started":
            return copy.deepcopy(receipt) if receipt else None
        outcome, submitted = _submitted_outcome(receipt)
        if outcome == "live":
            return copy.deepcopy(receipt)
        terminal = dict((submitted or {}).get("_terminal_event") or {})
        terminal_reason = str(terminal.get("reason") or "")
        reason = (
            "recovery_successor_exhausted"
            if outcome == "terminal" and terminal_reason == "compression_exhausted"
            else (
                "successor_settled"
                if outcome == "terminal"
                else "ambiguous_started_successor"
            )
        )
        now = time.time()
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": reason,
                "discarded_at": now,
                "updated_at": now,
            }
        )
        if outcome == "terminal":
            presentation_sid = _terminal_presentation_session_id(submitted)
            if presentation_sid:
                receipt["presentation_session_id"] = presentation_sid
        _save_store(store)
        return copy.deepcopy(receipt)


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
        receipt.update({"launch_phase": "launching", "updated_at": time.time()})
        _save_store(store)
        return copy.deepcopy(receipt)


def _finish_start(key: str, token: str, response: object) -> dict | None:
    response = response if isinstance(response, dict) else {}
    try:
        status = int(response.get("_status", 200) or 200)
    except (TypeError, ValueError):
        status = 500
    stream_id = str(response.get("stream_id") or response.get("active_stream_id") or "")
    with _store_lock():
        snapshot = copy.deepcopy(_load_store()["receipts"].get(key))
    if (
        snapshot is None
        or snapshot.get("state") != "starting"
        or snapshot.get("start_token") != token
    ):
        return snapshot
    same_session = str(response.get("session_id") or "") == snapshot.get("session_id")
    response_started = status < 400 and bool(stream_id) and same_session
    outcome, submitted = (
        ("live", None) if response_started else _submitted_outcome(snapshot)
    )
    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(key)
        if (
            receipt is None
            or receipt.get("state") != "starting"
            or receipt.get("start_token") != token
        ):
            return copy.deepcopy(receipt) if receipt else None
        now = time.time()
        started = outcome in {"live", "terminal"}
        if started:
            if submitted is not None:
                stream_id = str(submitted.get("stream_id") or "")
            if not stream_id:
                receipt.update(
                    {
                        "state": "discarded",
                        "discarded_reason": "ambiguous_launch_response",
                        "discarded_at": now,
                        "updated_at": now,
                    }
                )
                started = False
            elif outcome == "terminal":
                terminal = dict((submitted or {}).get("_terminal_event") or {})
                terminal_reason = str(terminal.get("reason") or "")
                receipt.update(
                    {
                        "state": "discarded",
                        "discarded_reason": (
                            "recovery_successor_exhausted"
                            if terminal_reason == "compression_exhausted"
                            else "successor_settled"
                        ),
                        "discarded_at": now,
                        "updated_at": now,
                    }
                )
                presentation_sid = _terminal_presentation_session_id(submitted)
                if presentation_sid:
                    receipt["presentation_session_id"] = presentation_sid
                started = False
            else:
                receipt.update(
                    {
                        "state": "started",
                        "child_stream_id": stream_id,
                        "child_turn_id": str(
                            (submitted or {}).get("turn_id")
                            or response.get("turn_id")
                            or ""
                        ),
                        "started_at": now,
                        "completed_start_token": token,
                        "updated_at": now,
                    }
                )
        elif outcome in {"absent", "launch_failed"}:
            receipt.update(
                {
                    "state": "claimed",
                    "updated_at": now,
                }
            )
        else:
            # A submitted-but-unconfirmed successor is never replayed blindly.
            receipt.update(
                {
                    "state": "discarded",
                    "discarded_reason": "ambiguous_launch_response",
                    "discarded_at": now,
                    "updated_at": now,
                }
            )
        _reset_start_fields(receipt)
        _save_store(store)
        result = copy.deepcopy(receipt)
        result["started_now"] = bool(started)
        return result


def settle_compression_recovery(
    session_id: str,
    parent_run_id: str,
    *,
    start: Callable[..., dict] | None = None,
) -> dict | None:
    """Start one durable same-session successor after the parent unregisters."""

    sid = str(session_id or "").strip()
    parent_id = str(parent_run_id or "").strip()
    key = _claim_key(sid, parent_id)
    receipt, token = _reserve_start(key)
    if receipt is None or token is None:
        if receipt is not None:
            _reconcile_receipt_presentation(receipt)
        return receipt
    try:
        receipt = _mark_launching(key, token)
    except Exception:
        # Reconcile the reservation while this owner still has exact token
        # authority. A same-process thread must not strand it indefinitely.
        try:
            result = _finish_start(key, token, {"_status": 500})
            if result is not None:
                _reconcile_receipt_presentation(result)
        except Exception:
            logger.exception(
                "compression recovery reservation reconciliation failed for %s",
                sid,
            )
        raise
    if receipt is None or receipt.get("launch_phase") != "launching":
        return receipt
    starter = start
    if starter is None:
        from api.routes import start_session_turn

        starter = start_session_turn
    try:
        response = starter(
            sid,
            RECOVERY_CONTROL_PROMPT,
            source=SOURCE,
            expected_profile=receipt.get("profile"),
            attachments=copy.deepcopy(receipt["seed"]["attachments"]),
            recovery_claim_token=token,
            recovery_fingerprint=receipt["fingerprint"],
            recovery_context_messages=copy.deepcopy(
                receipt["seed"]["context_messages"]
            ),
        )
    except Exception:
        logger.exception(
            "compression recovery successor start failed for session %s parent %s",
            sid,
            parent_id,
        )
        response = {"_status": 500}
    try:
        result = _finish_start(key, token, response)
    except Exception:
        try:
            result = _finish_start(key, token, {"_status": 500})
        except Exception:
            logger.exception(
                "compression recovery launch reconciliation failed for %s",
                sid,
            )
            raise
    if result is not None:
        _reconcile_receipt_presentation(result)
    return result


def recover_pending_compression_recoveries(
    *,
    start: Callable[..., dict] | None = None,
    session_id: str | None = None,
) -> int:
    """Recover claimed receipts and dead reservations without blind replay."""

    # This helper is also scheduled during module import in unmanaged mode.
    # An absent receipt is terminally empty; do not create state directories
    # behind startup/test cleanup merely to prove that absence.
    if not _receipt_path().is_file():
        return 0

    with _store_lock():
        rows = [
            copy.deepcopy(receipt)
            for receipt in _load_store()["receipts"].values()
            if receipt.get("state") in {"claimed", "starting", "started", "discarded"}
            and (session_id is None or receipt.get("session_id") == session_id)
            and (
                receipt.get("state") in {"claimed", "started", "discarded"}
                or not _owner_is_live(receipt)
            )
        ]
    started = 0
    for receipt in rows:
        if receipt.get("state") == "started":
            reconciled = _reconcile_started_receipt(str(receipt.get("claim_key") or ""))
            if reconciled is not None:
                _reconcile_receipt_presentation(reconciled)
            continue
        if receipt.get("state") == "discarded":
            _reconcile_receipt_presentation(receipt)
            continue
        result = settle_compression_recovery(
            str(receipt.get("session_id") or ""),
            str(receipt.get("parent_run_id") or ""),
            start=start,
        )
        if result and result.get("started_now") is True:
            started += 1
    return started


def _block_reserved_recovery_admission(
    receipt: dict,
    claim_token: str,
    *,
    reason: str,
) -> dict:
    """Discard one exact reservation that cannot safely reach its use point."""

    key = str(receipt.get("claim_key") or "")
    with _store_lock():
        store = _load_store()
        current = store["receipts"].get(key)
        if (
            not isinstance(current, dict)
            or current.get("state") != "starting"
            or current.get("start_token") != claim_token
            or current.get("fingerprint") != receipt.get("fingerprint")
        ):
            raise CompressionRecoveryReceiptStoreError(
                "compression recovery reservation changed during admission"
            )
        now = time.time()
        current.update(
            {
                "state": "discarded",
                "discarded_reason": str(reason),
                "discarded_at": now,
                "updated_at": now,
            }
        )
        _reset_start_fields(current)
        _save_store(store)
        return copy.deepcopy(current)


def reserved_recovery_seed(
    session,
    *,
    claim_token: str,
    fingerprint: str,
    context_messages: list[dict],
    attachments: list[dict],
) -> dict:
    """Validate a reserved route start and return its authoritative seed."""

    sid = str(getattr(session, "session_id", "") or "")
    with _store_lock():
        matches = [
            receipt
            for receipt in _load_store()["receipts"].values()
            if receipt.get("session_id") == sid
            and receipt.get("state") == "starting"
            and receipt.get("start_token") == claim_token
        ]
    if len(matches) != 1:
        raise CompressionRecoveryReceiptStoreError(
            "compression recovery reservation is unavailable"
        )
    receipt = matches[0]
    try:
        from api.compression_recovery import validate_recovery_attachments_for_use

        validated_attachments = validate_recovery_attachments_for_use(
            copy.deepcopy(receipt["seed"].get("attachments") or [])
        )
    except Exception as exc:
        blocked = _block_reserved_recovery_admission(
            receipt,
            claim_token,
            reason="recovery_attachment_unavailable",
        )
        raise CompressionRecoveryAdmissionBlocked(
            "recovery_attachment_unavailable",
            blocked,
        ) from exc
    if (
        receipt.get("fingerprint") != fingerprint
        or receipt["seed"].get("context_messages") != context_messages
        or validated_attachments != attachments
        or str(receipt.get("profile") or "default")
        != str(getattr(session, "profile", None) or "default")
    ):
        blocked = _block_reserved_recovery_admission(
            receipt,
            claim_token,
            reason="recovery_reservation_mismatch",
        )
        raise CompressionRecoveryAdmissionBlocked(
            "recovery_reservation_mismatch",
            blocked,
        )
    return copy.deepcopy(receipt["seed"])


def _human_supersession_material(
    store: dict,
    session,
    *,
    expected_fingerprint: str | None,
    attachments: list[dict] | None,
    attachments_supported: bool,
) -> tuple[dict, dict, list[dict]] | None:
    sid = str(getattr(session, "session_id", "") or "")
    recovery_identity = dict(getattr(session, "compression_recovery", None) or {})
    recovery_claim_key = str(recovery_identity.get("claim_key") or "")

    def _matches_session(receipt: dict) -> bool:
        return bool(
            receipt.get("session_id") == sid
            or receipt.get("presentation_session_id") == sid
            or (
                recovery_claim_key
                and receipt.get("claim_key") == recovery_claim_key
            )
        )

    ownership_changed = False
    settled_terminal = False
    for receipt in store["receipts"].values():
        if not _matches_session(receipt) or receipt.get("state") not in {
            "starting",
            "started",
        }:
            continue
        human_state, _human_stream = _human_supersession_submission_state(receipt)
        if human_state == "ambiguous":
            raise CompressionRecoveryInProgress(
                "human recovery supersession ownership is ambiguous"
            )
        if human_state == "live":
            raise CompressionRecoveryInProgress(
                "human recovery supersession worker is already running"
            )
        if human_state == "submitted":
            receipt.update(
                {
                    "state": "discarded",
                    "discarded_reason": "superseded_by_user",
                    "discarded_at": time.time(),
                    "updated_at": time.time(),
                    "presentation_session_id": sid,
                }
            )
            _reset_start_fields(receipt)
            _compact_nonblocking_terminal_receipt(receipt)
            ownership_changed = True
            settled_terminal = True
            continue
        if receipt.get("state") == "starting" and _owner_is_live(receipt):
            raise CompressionRecoveryInProgress(
                "compression recovery is already starting"
            )
        outcome, evidence = _submitted_outcome(receipt)
        if outcome == "live":
            raise CompressionRecoveryInProgress(
                "compression recovery is already running"
            )
        if outcome == "terminal":
            terminal = dict((evidence or {}).get("_terminal_event") or {})
            terminal_reason = str(terminal.get("reason") or "")
            receipt.update(
                {
                    "state": "discarded",
                    "discarded_reason": (
                        "recovery_successor_exhausted"
                        if terminal_reason == "compression_exhausted"
                        else "successor_settled"
                    ),
                    "discarded_at": time.time(),
                    "updated_at": time.time(),
                }
            )
            receipt["presentation_session_id"] = sid
            _reset_start_fields(receipt)
            _compact_nonblocking_terminal_receipt(receipt)
            ownership_changed = True
            settled_terminal = True
            continue

        # The per-session admission lock is held and neither live registry owns
        # this successor. Reclaim the exact seed for the human's newer message;
        # this avoids a permanent 503 after transcript/journal write failure.
        receipt.update({"state": "claimed", "updated_at": time.time()})
        receipt["presentation_session_id"] = sid
        _reset_start_fields(receipt)
        for field in (
            "child_stream_id",
            "child_turn_id",
            "started_at",
            "completed_start_token",
        ):
            receipt.pop(field, None)
        ownership_changed = True
    if ownership_changed:
        _save_store(store)
    if settled_terminal:
        session.compression_recovery = {}
        session.recommended_recovery_action = None
    candidates = [
        receipt
        for receipt in store["receipts"].values()
        if _matches_session(receipt)
        and receipt.get("state") == "claimed"
        and (
            expected_fingerprint is None
            or receipt.get("fingerprint") == expected_fingerprint
        )
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise CompressionRecoveryReceiptStoreError(
            "multiple compression recovery claims require reconciliation"
        )
    receipt = candidates[0]
    seed = _validate_seed(
        receipt.get("seed"),
        session_id=str(receipt.get("session_id") or ""),
        parent_run_id=str(receipt.get("parent_run_id") or ""),
    )
    if str(receipt.get("profile") or "default") != str(
        getattr(session, "profile", None) or "default"
    ):
        raise CompressionRecoveryReceiptStoreError(
            "compression recovery profile conflicts with the human turn"
        )
    from api.compression_recovery import (
        CompressionRecoveryBlocked,
        validate_recovery_attachments_for_use,
    )

    try:
        merged_attachments = validate_recovery_attachments_for_use(
            [
                *copy.deepcopy(seed["attachments"]),
                *copy.deepcopy(attachments or []),
            ]
        )
    except CompressionRecoveryBlocked as exc:
        if exc.reason == "attachment_conflict":
            raise CompressionRecoverySupersessionConflict(
                "compression recovery attachment conflicts with the human turn"
            ) from exc
        raise CompressionRecoveryReceiptStoreError(
            f"compression recovery attachment cannot be reused: {exc.reason}"
        ) from exc
    if not attachments_supported:
        raise CompressionRecoverySupersessionConflict(
            "pending compression recovery requires the streaming chat endpoint"
        )
    return receipt, seed, merged_attachments


def supersede_pending_compression_recovery(
    session,
    *,
    expected_fingerprint: str | None = None,
    attachments: list[dict] | None = None,
    attachments_supported: bool = True,
) -> dict | None:
    """Install one claimed seed, then discard it for a lock-owning human send."""

    sid = str(getattr(session, "session_id", "") or "")
    with _store_lock():
        store = _load_store()
        material = _human_supersession_material(
            store,
            session,
            expected_fingerprint=expected_fingerprint,
            attachments=attachments,
            attachments_supported=attachments_supported,
        )
        if material is None:
            return None
        receipt, seed, merged_attachments = material
        previous_context = copy.deepcopy(getattr(session, "context_messages", None) or [])
        previous_recovery = copy.deepcopy(
            getattr(session, "compression_recovery", None)
        )
        previous_action = getattr(session, "recommended_recovery_action", None)
        session.context_messages = copy.deepcopy(seed["context_messages"])
        session.compression_recovery = {}
        session.recommended_recovery_action = None
        try:
            session.save(touch_updated_at=False)
        except Exception:
            session.context_messages = previous_context
            session.compression_recovery = previous_recovery
            session.recommended_recovery_action = previous_action
            raise
        now = time.time()
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": "superseded_by_user",
                "discarded_at": now,
                "updated_at": now,
            }
        )
        try:
            _save_store(store)
        except Exception:
            session.context_messages = previous_context
            session.compression_recovery = previous_recovery
            session.recommended_recovery_action = previous_action
            try:
                session.save(touch_updated_at=False)
            except Exception:
                logger.exception(
                    "failed to roll back compression recovery supersession for %s",
                    sid,
                )
            raise
        result = copy.deepcopy(receipt)
        result["merged_attachments"] = merged_attachments
    return result


def reserve_human_compression_supersession(
    session,
    *,
    expected_fingerprint: str | None = None,
    attachments: list[dict] | None = None,
) -> dict | None:
    """Fence a claimed recovery until the replacing human worker starts."""

    with _store_lock():
        store = _load_store()
        material = _human_supersession_material(
            store,
            session,
            expected_fingerprint=expected_fingerprint,
            attachments=attachments,
            attachments_supported=True,
        )
        if material is None:
            return None
        receipt, seed, merged_attachments = material
        owner_token = process_start_token(os.getpid())
        if not owner_token:
            raise CompressionRecoveryReceiptStoreError(
                "compression recovery process identity is unavailable"
            )
        token = uuid.uuid4().hex
        now = time.time()
        receipt.update(
            {
                "state": "starting",
                "owner_pid": os.getpid(),
                "owner_start_token": owner_token,
                "owner_thread": threading.get_ident(),
                "start_token": token,
                "launch_phase": "launching",
                "launch_mode": "human_supersession",
                "starting_at": now,
                "updated_at": now,
            }
        )
        _save_store(store)
        result = copy.deepcopy(receipt)
        result["merged_attachments"] = merged_attachments
    session.context_messages = copy.deepcopy(seed["context_messages"])
    session.compression_recovery = {}
    session.recommended_recovery_action = None
    return result


def finish_human_compression_supersession(
    claim_key: str,
    start_token: str,
    *,
    reason: str = "superseded_by_user",
    project: bool = True,
) -> dict:
    """Commit human supersession only after its worker start was observed."""

    with _store_lock():
        store = _load_store()
        receipt = store["receipts"].get(str(claim_key or ""))
        if (
            receipt is None
            or receipt.get("state") != "starting"
            or receipt.get("launch_mode") != "human_supersession"
            or receipt.get("start_token") != start_token
        ):
            raise CompressionRecoveryReceiptStoreError(
                "human compression supersession reservation is unavailable"
            )
        now = time.time()
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": str(reason or "superseded_by_user"),
                "discarded_at": now,
                "updated_at": now,
            }
        )
        _reset_start_fields(receipt)
        _save_store(store)
        result = copy.deepcopy(receipt)
    if project and _discard_requires_blocker(result):
        _reconcile_receipt_presentation(result)
    return result


def _discard_receipt_for_child_stream(
    session_id: str,
    child_stream_id: str,
    *,
    reason: str,
    presentation_session_id: str | None = None,
) -> dict | None:
    sid = str(session_id or "")
    stream_id = str(child_stream_id or "")
    if not sid or not stream_id:
        return None
    with _store_lock():
        store = _load_store()
        candidates: list[dict] = []
        for receipt in store["receipts"].values():
            if receipt.get("session_id") != sid or receipt.get("state") not in {
                "starting",
                "started",
            }:
                continue
            if receipt.get("state") == "started":
                if str(receipt.get("child_stream_id") or "") == stream_id:
                    candidates.append(receipt)
                continue
            _outcome, evidence = _submitted_outcome(receipt)
            if str((evidence or {}).get("stream_id") or "") == stream_id:
                candidates.append(receipt)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise CompressionRecoveryReceiptStoreError(
                "multiple compression recovery receipts match the successor stream"
            )
        receipt = candidates[0]
        now = time.time()
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": str(reason or "successor_settled"),
                "discarded_at": now,
                "updated_at": now,
            }
        )
        presentation_sid = str(presentation_session_id or "").strip()
        if presentation_sid:
            receipt["presentation_session_id"] = presentation_sid
        _reset_start_fields(receipt)
        _save_store(store)
        return copy.deepcopy(receipt)


def session_has_live_compression_recovery(session_or_id) -> bool:
    """Return whether a recovery-owned worker can still write this session.

    This is the deletion fence. It treats uncertain live ownership as active,
    while allowing a stale ``started`` receipt with no process registry owner
    to be deleted and purged.
    """

    session = session_or_id if hasattr(session_or_id, "session_id") else None
    sid = str(
        getattr(session, "session_id", "")
        if session is not None
        else (session_or_id or "")
    ).strip()
    if not sid or not _receipt_path().is_file():
        return False
    recovery_claim_key = ""
    if session is not None:
        recovery_claim_key = str(
            dict(getattr(session, "compression_recovery", None) or {}).get(
                "claim_key"
            )
            or ""
        )

    def _matches(receipt: dict) -> bool:
        return bool(
            receipt.get("session_id") == sid
            or receipt.get("presentation_session_id") == sid
            or (
                recovery_claim_key
                and receipt.get("claim_key") == recovery_claim_key
            )
        )

    with _store_lock():
        store = _load_store()
        candidates = [
            copy.deepcopy(receipt)
            for receipt in store["receipts"].values()
            if _matches(receipt)
            and receipt.get("state") in {"starting", "started"}
        ]

    for receipt in candidates:
        state = str(receipt.get("state") or "")
        if state == "starting":
            if _owner_is_live(receipt):
                return True
            if receipt.get("launch_mode") == "human_supersession":
                outcome, _stream_id = _human_supersession_submission_state(
                    receipt
                )
            else:
                outcome, _submitted = _submitted_outcome(receipt)
            if outcome in {"live", "ambiguous"}:
                return True
            continue

        stream_id = str(receipt.get("child_stream_id") or "")
        if not stream_id:
            continue
        try:
            with config.STREAMS_LOCK:
                stream_live = stream_id in config.STREAMS
            with config.ACTIVE_RUNS_LOCK:
                active_live = stream_id in (config.ACTIVE_RUNS or {})
        except Exception:
            logger.exception(
                "compression recovery deletion liveness check failed for %s",
                sid,
            )
            return True
        if stream_live or active_live:
            return True
    return False


def retire_session_compression_recoveries(
    session_or_id,
    *,
    reason: str,
) -> int:
    """Retire every recovery intent before a user-owned session mutation."""

    if reason not in {"superseded_by_user", "session_deleted"}:
        raise ValueError("unsupported compression recovery retirement reason")
    session = session_or_id if hasattr(session_or_id, "session_id") else None
    sid = str(
        getattr(session, "session_id", "")
        if session is not None
        else (session_or_id or "")
    ).strip()
    if not sid:
        raise ValueError("session_id is required")
    recovery_claim_key = ""
    if session is not None:
        recovery_claim_key = str(
            dict(getattr(session, "compression_recovery", None) or {}).get(
                "claim_key"
            )
            or ""
        )
        session.compression_recovery = {}
        session.recommended_recovery_action = None

    def _matches_session(receipt: dict) -> bool:
        return bool(
            receipt.get("session_id") == sid
            or receipt.get("presentation_session_id") == sid
            or (
                recovery_claim_key
                and receipt.get("claim_key") == recovery_claim_key
            )
        )

    if not _receipt_path().is_file():
        return 0
    retired = 0
    with _store_lock():
        store = _load_store()
        if reason == "session_deleted":
            for key, receipt in list(store["receipts"].items()):
                if not _matches_session(receipt):
                    continue
                store["receipts"].pop(key, None)
                retired += 1
            if retired:
                _save_store(store)
            return retired
        now = time.time()
        for receipt in store["receipts"].values():
            if not _matches_session(receipt):
                continue
            state = str(receipt.get("state") or "")
            if state == "discarded" and not _discard_requires_blocker(receipt):
                continue
            if state not in {"claimed", "starting", "started", "discarded"}:
                continue
            if (
                state == "discarded"
                and receipt.get("discarded_reason") == reason
            ):
                continue
            receipt.update(
                {
                    "state": "discarded",
                    "discarded_reason": reason,
                    "discarded_at": now,
                    "updated_at": now,
                }
            )
            _reset_start_fields(receipt)
            retired += 1
        if retired:
            _save_store(store)
    return retired


def session_has_pending_compression_recovery(
    session_id: str,
    *,
    claim_key: str = "",
) -> bool:
    """Return whether a runner-external send would bypass owned recovery state."""

    sid = str(session_id or "").strip()
    expected_claim_key = str(claim_key or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if not _receipt_path().is_file():
        return False
    with _store_lock():
        return any(
            (
                receipt.get("session_id") == sid
                or receipt.get("presentation_session_id") == sid
                or (
                    expected_claim_key
                    and receipt.get("claim_key") == expected_claim_key
                )
            )
            and receipt.get("state") in {"claimed", "starting", "started"}
            for receipt in _load_store()["receipts"].values()
        )


def _recovery_receipt_session_id(session) -> str:
    """Resolve receipt ownership across an internal compression ID rotation."""

    current_sid = str(getattr(session, "session_id", "") or "").strip()
    recovery = dict(getattr(session, "compression_recovery", None) or {})
    source_sid = str(recovery.get("source_session_id") or "").strip()
    if (
        source_sid
        and recovery.get("claim_key")
        and recovery.get("fingerprint")
    ):
        return source_sid
    return current_sid


def bind_recovery_presentation_session(
    session,
    *,
    source_session_id: str,
    child_stream_id: str,
) -> dict:
    """Durably bind a rotating recovery worker to its canonical task sidecar."""

    source_sid = str(source_session_id or "").strip()
    presentation_sid = str(getattr(session, "session_id", "") or "").strip()
    stream_id = str(child_stream_id or "").strip()
    if not source_sid or not presentation_sid or not stream_id:
        raise ValueError("recovery presentation binding identity is required")
    with _store_lock():
        store = _load_store()
        candidates: list[dict] = []
        for receipt in store["receipts"].values():
            if receipt.get("session_id") != source_sid or receipt.get("state") not in {
                "starting",
                "started",
            }:
                continue
            if receipt.get("state") == "started":
                if str(receipt.get("child_stream_id") or "") == stream_id:
                    candidates.append(receipt)
                continue
            _outcome, evidence = _submitted_outcome(receipt)
            if str((evidence or {}).get("stream_id") or "") == stream_id:
                candidates.append(receipt)
        if len(candidates) != 1:
            raise CompressionRecoveryReceiptStoreError(
                "rotated compression recovery presentation owner is ambiguous"
            )
        receipt = candidates[0]
        receipt["presentation_session_id"] = presentation_sid
        receipt["updated_at"] = time.time()
        _save_store(store)
        return copy.deepcopy(receipt)


def clear_recovery_presentation(
    session,
    *,
    child_stream_id: str | None = None,
    reason: str = "successor_settled",
    blocked: bool = False,
) -> dict | None:
    """Settle one successor receipt and update only its recovery presentation."""

    receipt = None
    if child_stream_id:
        receipt = _discard_receipt_for_child_stream(
            _recovery_receipt_session_id(session),
            str(child_stream_id),
            reason=reason,
            presentation_session_id=str(
                getattr(session, "session_id", "") or ""
            ),
        )
    if blocked:
        if receipt is not None:
            session.compression_recovery = _session_phase_payload(
                receipt,
                "blocked",
                reason=reason,
            )
            session.recommended_recovery_action = None
        return receipt
    session.compression_recovery = {}
    session.recommended_recovery_action = None
    return receipt


def settle_recovery_after_durable_terminal(
    session,
    *,
    child_stream_id: str,
) -> dict | None:
    """Settle a successor only after its transcript and terminal proof exist.

    Callers persist the final transcript first, then append an exact turn-journal
    terminal carrying ``recovery_terminal_persisted=True``.  This guard re-reads
    that durable evidence before discarding the receipt, so a save or journal
    failure always leaves startup something truthful to reconcile or block.
    """

    sid = _recovery_receipt_session_id(session)
    stream_id = str(child_stream_id or "").strip()
    if not sid or not stream_id:
        return None
    with _store_lock():
        candidates = [
            copy.deepcopy(receipt)
            for receipt in _load_store()["receipts"].values()
            if receipt.get("session_id") == sid
            and receipt.get("state") in {"starting", "started"}
            and (
                str(receipt.get("child_stream_id") or "") == stream_id
                or receipt.get("state") == "starting"
            )
        ]
    exact: list[dict] = []
    for receipt in candidates:
        outcome, evidence = _submitted_outcome(receipt)
        if (
            outcome == "terminal"
            and str((evidence or {}).get("stream_id") or "") == stream_id
        ):
            exact.append(receipt)
    if not exact:
        return None
    if len(exact) != 1:
        raise CompressionRecoveryReceiptStoreError(
            "multiple compression recovery receipts have durable successor terminals"
        )
    receipt = clear_recovery_presentation(
        session,
        child_stream_id=stream_id,
    )
    if receipt is None:
        return None
    try:
        session.save(touch_updated_at=False)
    except Exception:
        # The transcript and exact terminal already committed. A fresh process
        # repairs this presentation from the discarded receipt; replaying the
        # successor would be less safe than leaving the transient phase stale.
        logger.exception(
            "failed to persist settled compression recovery presentation for %s",
            sid,
        )
    return receipt


def _load_receipt_identity(key: str) -> tuple[str, str]:
    with _store_lock():
        receipt = _load_store()["receipts"].get(key)
        if not isinstance(receipt, dict):
            return "", ""
        return (
            str(receipt.get("session_id") or ""),
            str(receipt.get("parent_run_id") or ""),
        )


def _start_managed_receipt(
    key: str,
    *,
    start: Callable[..., dict] | None,
) -> tuple[dict | None, bool]:
    session_id, parent_run_id = _load_receipt_identity(key)
    result = settle_compression_recovery(
        session_id,
        parent_run_id,
        start=start,
    )
    return result, bool(result and result.get("state") == "started")


def _reconcile_existing_terminal_receipts() -> None:
    """Repair started/discarded restart state before managed enumeration."""

    with _store_lock():
        rows = [
            copy.deepcopy(receipt)
            for receipt in _load_store()["receipts"].values()
            if receipt.get("state") in {"started", "discarded"}
        ]
    for receipt in rows:
        if receipt.get("state") == "started":
            receipt = _reconcile_started_receipt(
                str(receipt.get("claim_key") or "")
            )
        if receipt is not None:
            _reconcile_receipt_presentation(receipt)


def recover_managed_compression_recoveries_exact(
    *,
    transaction_id: str,
    manifest_sha256: str,
    start: Callable[..., dict] | None = None,
):
    """Recover the exact bounded store inside managed startup authority."""

    scope = _MANAGED_EXACT.set(True)
    try:
        _reconcile_existing_terminal_receipts()
        return recover_exact(
            path=_receipt_path(),
            store_lock=_store_lock,
            validate_store=_validate_managed_store,
            start_one=lambda key: _start_managed_receipt(key, start=start),
            session_id_for=lambda receipt: str(receipt.get("session_id") or ""),
            terminal_states={"discarded"},
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha256,
            max_receipts=_MAX_RECEIPTS,
            process_token_lookup=process_start_token,
            reconcile_stale_starting=True,
        )
    finally:
        _MANAGED_EXACT.reset(scope)


def verify_managed_compression_recoveries_exact(
    receipt,
    *,
    transaction_id: str,
    manifest_sha256: str,
):
    """Read-only verification of one managed compression recovery receipt."""

    return verify_exact(
        receipt,
        path=_receipt_path(),
        store_lock=_verification_store_lock,
        validate_store=_validate_managed_store,
        session_id_for=lambda row: str(row.get("session_id") or ""),
        terminal_states={"discarded"},
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
        max_receipts=_MAX_RECEIPTS,
        process_token_lookup=process_start_token,
    )


def load_receipts() -> dict:
    """Return a defensive receipt snapshot for diagnostics and tests."""

    with _store_lock():
        return copy.deepcopy(_load_store())
