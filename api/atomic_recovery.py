"""Authenticated, atomic WebUI-owned recovery start.

The watchdog may identify a candidate, but only the WebUI process owns enough
state to validate and reserve a WebUI turn without a validation-to-launch gap.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from api.auth import _is_loopback, _signing_key
from api.config import LOCK, SESSIONS, _get_session_agent_lock


RECOVERY_PROMPT = (
    "This session stopped before its work was conclusively finished. "
    "Continue the user's last unanswered request from the persisted conversation state. "
    "Treat every persisted tool result as already executed; inspect and reconcile actual state "
    "before repeating any file edit, git operation, message, browser action, or external side effect. "
    "If continuation is unsafe or the workspace does not match the task, reply exactly "
    "'RECOVERY_BLOCKED: <reason>'. Only when the requested work is actually complete, finish "
    "with a final line 'RECOVERED: <summary>'."
)

_AUTH_WINDOW_SECONDS = 60
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{20}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STALE_SIDECAR_SECONDS = 3600


def ensure_internal_recovery_key() -> None:
    """Materialize the durable HMAC key before the watchdog can call us."""
    _signing_key()


def _canonical_request_body(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def internal_recovery_signing_bytes(body: dict, timestamp: str) -> bytes:
    return b"hermes-webui-recovery-v1\n" + str(timestamp).encode("ascii") + b"\n" + _canonical_request_body(body)


def verify_internal_recovery_request(handler, body: dict) -> tuple[bool, str | None]:
    """Verify loopback origin plus a fresh HMAC from the durable WebUI key."""
    try:
        remote = str(handler.client_address[0])
    except Exception:
        return False, "Internal recovery requires a loopback client"
    if not _is_loopback(remote):
        return False, "Internal recovery requires a loopback client"
    timestamp = str(handler.headers.get("X-Hermes-Recovery-Timestamp") or "").strip()
    signature = str(handler.headers.get("X-Hermes-Recovery-Signature") or "").strip().lower()
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False, "Internal recovery authentication failed"
    if abs(time.time() - request_time) > _AUTH_WINDOW_SECONDS:
        return False, "Internal recovery authentication failed"
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False, "Internal recovery authentication failed"
    try:
        expected = hmac.new(
            _signing_key(),
            internal_recovery_signing_bytes(body, timestamp),
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False, "Internal recovery authentication failed"
    if not hmac.compare_digest(signature, expected):
        return False, "Internal recovery authentication failed"
    return True, None


def _state_db_path(profile: str) -> Path:
    configured = os.environ.get("HERMES_SESSION_WATCHDOG_DB")
    if configured:
        hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        configured_profile = (
            hermes_home.name
            if hermes_home.parent.name == "profiles"
            else "default"
        )
        if profile != configured_profile:
            raise ValueError("configured recovery database does not own requested profile")
        return Path(configured).expanduser()
    from api.profiles import get_hermes_home_for_profile

    return Path(get_hermes_home_for_profile(profile)) / "state.db"


def _transcript_hash(messages: list[dict], last_user_id: int) -> str:
    relevant = [
        {
            "id": message.get("id"),
            "role": message.get("role"),
            "content": message.get("content"),
            "tool_call_id": message.get("tool_call_id"),
            "tool_calls": message.get("tool_calls"),
            "tool_name": message.get("tool_name"),
            "finish_reason": message.get("finish_reason"),
        }
        for message in messages
        if int(message.get("id") or 0) >= last_user_id
    ]
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _read_db_turn(session_id: str, profile: str) -> dict:
    db_path = _state_db_path(profile)
    if not db_path.is_file():
        raise ValueError("authoritative state database is unavailable")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        with con:
            session = con.execute(
                "SELECT id, source, model, cwd, git_repo_root FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("authoritative session is missing")
            last_user = con.execute(
                """
                SELECT id, content, timestamp FROM messages
                WHERE session_id = ? AND role = 'user'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if last_user is None:
                raise ValueError("authoritative session has no user turn")
            try:
                last_user_id = int(last_user["id"])
                last_user_at = float(last_user["timestamp"] or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("authoritative logical turn is malformed") from exc
            last_user_text = str(last_user["content"] or "")
            rows = con.execute(
                """
                SELECT id, role, content, tool_calls, tool_name, tool_call_id,
                       timestamp, finish_reason
                FROM messages
                WHERE session_id = ? AND id >= ?
                ORDER BY id ASC
                """,
                (session_id, last_user_id),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("authoritative state database is malformed") from exc
    finally:
        try:
            con.close()
        except UnboundLocalError:
            pass
    messages = [dict(row) for row in rows]
    return {
        "source": str(session["source"] or ""),
        "model": session["model"],
        "cwd": session["cwd"],
        "git_repo_root": session["git_repo_root"],
        "last_user_id": last_user_id,
        "last_user_at": last_user_at,
        "last_user_text": last_user_text,
        "transcript_hash": _transcript_hash(messages, last_user_id),
    }


def _read_sidecar(session_id: str) -> dict:
    from api.models import SESSION_DIR

    path = Path(SESSION_DIR) / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("authoritative WebUI sidecar is malformed") from exc
    if not isinstance(payload, dict) or str(payload.get("session_id") or "") != session_id:
        raise ValueError("authoritative WebUI sidecar is malformed")
    return payload


def _read_journal_state(session_id: str) -> dict:
    from api.turn_journal import read_turn_journal

    journal = read_turn_journal(session_id)
    if journal.get("malformed"):
        raise ValueError("authoritative WebUI turn journal is malformed")
    events = journal.get("events") or []
    checked: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("authoritative WebUI turn journal is malformed")
        try:
            created_at = float(event.get("created_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("authoritative WebUI turn journal is malformed") from exc
        normalized = dict(event)
        normalized["_created_at"] = created_at
        checked.append(normalized)
    checked.sort(key=lambda event: event["_created_at"])
    if not checked:
        return {"events": []}
    latest_turn_id = str(checked[-1].get("turn_id") or "")
    if not latest_turn_id:
        raise ValueError("authoritative WebUI turn journal is malformed")
    latest = [event for event in checked if str(event.get("turn_id") or "") == latest_turn_id]
    terminals = [event for event in latest if str(event.get("event") or "") in {"completed", "interrupted"}]
    if len(terminals) > 1:
        raise ValueError("authoritative WebUI turn journal has conflicting terminal state")
    return {
        "events": checked,
        "latest_at": latest[-1]["_created_at"],
        "terminal_event": str(terminals[-1].get("event") or "") if terminals else None,
        "terminal_at": terminals[-1]["_created_at"] if terminals else 0,
    }


def _resolved_existing_directory(raw, label: str) -> Path:
    try:
        path = Path(str(raw or "")).expanduser().resolve(strict=True)
    except Exception as exc:
        raise ValueError(f"{label} workspace is invalid") from exc
    if not path.is_dir():
        raise ValueError(f"{label} workspace is invalid")
    return path


def _reconcile_stale_sidecar_owner(session, db_state: dict) -> str | None:
    """Clear only an old, provably dead sidecar owner while the session lock is held."""
    from api import config as live_config

    stream_id = str(getattr(session, "active_stream_id", None) or "").strip()
    pending = str(getattr(session, "pending_user_message", None) or "")
    if not stream_id and not pending:
        return None
    if stream_id:
        with live_config.STREAMS_LOCK:
            stream_alive = stream_id in live_config.STREAMS
        with live_config.ACTIVE_RUNS_LOCK:
            worker_alive = stream_id in live_config.ACTIVE_RUNS
        if stream_alive or worker_alive:
            return "Recovery blocked by a live WebUI owner"
    try:
        owner_started_at = float(
            getattr(session, "pending_started_at", None)
            or getattr(session, "updated_at", None)
            or 0
        )
    except (TypeError, ValueError):
        return "Recovery blocked by malformed persisted owner state"
    if not owner_started_at or time.time() - owner_started_at < _STALE_SIDECAR_SECONDS:
        return "Recovery blocked by fresh persisted WebUI owner state"
    if pending:
        latest_user = str(db_state.get("last_user_text") or "")
        if " ".join(pending.split()) != " ".join(latest_user.split()):
            return "Recovery blocked because pending text is not the authoritative database turn"
        if list(getattr(session, "pending_attachments", None) or []):
            return "Recovery blocked because stale pending attachments cannot be reconciled safely"
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.save(touch_updated_at=False)
    return None


def _blocked(error: str, status: int = 409) -> dict:
    return {"error": error, "_status": status}


def start_atomic_webui_recovery(body: dict) -> dict:
    """Validate and reserve a recovery turn while retaining session ownership."""
    if not isinstance(body, dict):
        return _blocked("Recovery request is malformed", 400)
    session_id = str(body.get("session_id") or "").strip()
    profile = str(body.get("profile") or "").strip()
    source = str(body.get("source") or "").strip().lower()
    fingerprint = str(body.get("transcript_hash") or "").strip().lower()
    claim_token = str(body.get("recovery_claim_token") or "").strip()
    try:
        expected_last_user_id = int(body.get("last_user_id"))
    except (TypeError, ValueError):
        return _blocked("Recovery request is malformed", 400)
    if (
        not session_id
        or session_id in {".", ".."}
        or not _SESSION_ID_RE.fullmatch(session_id)
        or not profile
        or profile in {".", ".."}
        or not _PROFILE_ID_RE.fullmatch(profile)
        or source != "webui"
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or not claim_token
        or len(claim_token) > 256
    ):
        return _blocked("Recovery request is malformed", 400)

    session_lock = _get_session_agent_lock(session_id)
    with session_lock:
        try:
            db_state = _read_db_turn(session_id, profile)
            sidecar = _read_sidecar(session_id)
            journal = _read_journal_state(session_id)
            requested_workspace = _resolved_existing_directory(body.get("workspace"), "requested")
            sidecar_workspace = _resolved_existing_directory(
                sidecar.get("workspace") or sidecar.get("workspace_path"), "WebUI"
            )
            db_workspace = _resolved_existing_directory(db_state.get("cwd"), "state database")
        except ValueError as exc:
            return _blocked(str(exc))

        if db_state["source"].lower() != "webui":
            return _blocked("Recovery source is not owned by WebUI")
        sidecar_profile = str(sidecar.get("profile") or "default").strip() or "default"
        if sidecar_profile != profile:
            return _blocked("Recovery profile does not own the WebUI session")
        if expected_last_user_id != db_state["last_user_id"]:
            return _blocked("Recovery logical turn changed after watchdog validation")
        if fingerprint != db_state["transcript_hash"]:
            return _blocked("Recovery fingerprint mismatch after watchdog validation")
        if requested_workspace != sidecar_workspace or requested_workspace != db_workspace:
            return _blocked("Recovery workspace mismatch after watchdog validation")
        requested_git_root = str(body.get("git_repo_root") or "").strip()
        db_git_root = str(db_state.get("git_repo_root") or "").strip()
        if bool(requested_git_root) != bool(db_git_root):
            return _blocked("Recovery workspace/repository mismatch after watchdog validation")
        if requested_git_root and db_git_root:
            try:
                if Path(requested_git_root).expanduser().resolve(strict=True) != Path(db_git_root).expanduser().resolve(strict=True):
                    return _blocked("Recovery workspace/repository mismatch after watchdog validation")
            except OSError:
                return _blocked("Recovery workspace/repository mismatch after watchdog validation")
        if str(sidecar.get("profile") or "default") != profile:
            return _blocked("Recovery profile does not own the WebUI sidecar")
        terminal = journal.get("terminal_event")
        if terminal and float(journal.get("terminal_at") or 0) >= db_state["last_user_at"]:
            return _blocked(f"Recovery blocked because the latest turn is {terminal}")
        for event in journal.get("events") or []:
            if str(event.get("source") or "") != "cron-recovery":
                continue
            if (
                str(event.get("recovery_claim_token") or "") == claim_token
                or str(event.get("recovery_fingerprint") or "") == fingerprint
            ):
                return _blocked("Duplicate recovery reservation already exists")

        from api import routes
        from api.models import Session

        try:
            session = Session.load(session_id)
        except Exception:
            return _blocked("Authoritative WebUI session is malformed")
        if session is None:
            return _blocked("Authoritative WebUI session is missing")
        session_profile = str(getattr(session, "profile", None) or "default").strip() or "default"
        if session_profile != profile:
            return _blocked("Recovery profile does not own the loaded WebUI session")
        from api.gateway_chat import webui_gateway_chat_enabled
        from api.runtime_adapter import runtime_adapter_runner_enabled

        if webui_gateway_chat_enabled():
            return _blocked("Atomic recovery is unavailable for Gateway-owned chat execution", 501)
        if runtime_adapter_runner_enabled():
            return _blocked("Atomic recovery is unavailable when an external runner owns reservation", 501)
        stale_owner_error = _reconcile_stale_sidecar_owner(session, db_state)
        if stale_owner_error:
            return _blocked(stale_owner_error)
        if _resolved_existing_directory(getattr(session, "workspace", None), "session") != requested_workspace:
            return _blocked("Recovery workspace mismatch after session reload")
        if not (getattr(session, "model", None) or db_state.get("model")):
            return _blocked("Authoritative WebUI session model is missing")
        with LOCK:
            cached_session = SESSIONS.get(session_id)
            if cached_session is not None and cached_session is not session:
                # A human request may already hold a reference returned by
                # get_session() before recovery acquired this session lock.
                # Preserve that object identity while replacing its persisted
                # snapshot; otherwise the stale human object can start a second
                # stream immediately after recovery releases the lock.
                # Update persisted fields in place rather than swapping or
                # clearing the object: concurrent readers may already hold this
                # reference, and must never observe a transient empty __dict__.
                cached_session.__dict__.update(session.__dict__)
                session = cached_session
            SESSIONS[session_id] = session
            SESSIONS.move_to_end(session_id)

        return routes._start_run(
            session,
            msg=RECOVERY_PROMPT,
            attachments=[],
            workspace=str(requested_workspace),
            model=getattr(session, "model", None) or db_state.get("model"),
            model_provider=getattr(session, "model_provider", None),
            normalized_model=False,
            source="cron-recovery",
            route="internal_atomic_recovery",
            session_lock_held=True,
            recovery_claim_token=claim_token,
            recovery_fingerprint=fingerprint,
        )
