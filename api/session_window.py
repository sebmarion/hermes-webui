"""Bounded newest-first session windows for release-lite task opening.

This module is intentionally independent of the legacy full-session merger.
Every dependency that can touch state is injectable so route tests can prove
that a lazy-tail request never enters the complete-history path.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Mapping

from api.session_message_paging import (
    MESSAGE_CURSOR_VERSION,
    MessageCursorClaims,
    MessageCursorExpected,
    decode_message_cursor,
    encode_message_cursor,
    message_cursor_database_identity_digest,
)


INITIAL_VISIBLE_LIMIT = 30
MAX_VISIBLE_LIMIT = 50
MAX_LINEAGE_DEPTH = 128
MAX_RAW_ROWS = 512
MAX_TOOL_CLOSURE_ROWS = 64
MAX_SERIALIZED_BYTES = 2_621_440
READ_BUDGET_SECONDS = 0.750
RECONNECT_TOKEN_VERSION = 1
RECONNECT_TOKEN_TTL_SECONDS = 120
MAX_RECONNECT_TOKEN_BYTES = 8 * 1024

_SCHEMA = "lazy_tail_v1"
_VALID_STATES = frozenset({"ready", "reconnecting", "legacy_required", "stale"})
_PROCESS_RECONNECT_SIGNING_KEY = secrets.token_bytes(32)


class SessionWindowRequestError(ValueError):
    """A public session-window request is malformed or cross-target."""

    def __init__(self, code: str, *, status: int = 400):
        self.code = str(code)
        self.status = int(status)
        super().__init__(self.code)


@dataclass(frozen=True)
class ReconnectClaims:
    version: int
    profile_id: str
    canonical_session_id: str
    stream_id: str
    checkpoint_event_id: str
    expires_at: int


@dataclass(frozen=True)
class ReconnectExpected:
    profile_id: str
    canonical_session_id: str
    stream_id: str
    checkpoint_event_id: str


@dataclass(frozen=True)
class SessionWindowRequest:
    session_id: str
    visible_limit: int
    older_cursor: str | None
    resolve_model: bool

    @classmethod
    def parse(cls, query: Mapping[str, list[str]]) -> "SessionWindowRequest":
        session_id = _single_query_value(query, "session_id")
        if not session_id:
            raise SessionWindowRequestError("missing_session_id")
        if "\x00" in session_id or len(session_id) > 512:
            raise SessionWindowRequestError("invalid_session_id")

        raw_limit = _single_query_value(query, "msg_limit")
        if raw_limit == "":
            visible_limit = INITIAL_VISIBLE_LIMIT
        else:
            try:
                visible_limit = int(raw_limit)
            except (TypeError, ValueError) as exc:
                raise SessionWindowRequestError("invalid_msg_limit") from exc
        if (
            isinstance(visible_limit, bool)
            or not 1 <= visible_limit <= MAX_VISIBLE_LIMIT
        ):
            raise SessionWindowRequestError("invalid_msg_limit")

        older_cursor = _single_query_value(query, "older_cursor") or None
        if older_cursor is not None and len(older_cursor.encode("utf-8")) > 16 * 1024:
            raise SessionWindowRequestError("invalid_older_cursor")

        raw_resolve_model = _single_query_value(query, "resolve_model")
        if raw_resolve_model not in {"", "0", "1"}:
            raise SessionWindowRequestError("invalid_resolve_model")
        resolve_model = raw_resolve_model == "1"

        requested_canonical = _single_query_value(query, "canonical_session_id")
        if older_cursor is not None and requested_canonical and requested_canonical != session_id:
            raise SessionWindowRequestError("cursor_target_mismatch")

        return cls(
            session_id=session_id,
            visible_limit=visible_limit,
            older_cursor=older_cursor,
            resolve_model=resolve_model,
        )


@dataclass(frozen=True)
class SessionWindowDependencies:
    active_profile: Callable[[], str]
    state_db_path: Callable[[str], Path | str]
    resolve_shared_session: Callable[[Path, str], Any]
    confirm_shared_session_target: Callable[[Path, Any], bool]
    read_state_db_message_page: Callable[..., Any]
    encode_older_cursor: Callable[..., str]
    decode_older_cursor: Callable[..., Any]
    capture_runtime: Callable[[str, str], dict | None]
    monotonic: Callable[[], float] = time.monotonic
    wall_time: Callable[[], float] = time.time
    diagnostic_sink: Callable[[dict[str, Any]], None] | None = None


def build_session_window(
    request: SessionWindowRequest,
    *,
    deps: SessionWindowDependencies | None = None,
) -> dict:
    """Return one bounded settled tail without invoking legacy reconstruction."""
    if not isinstance(request, SessionWindowRequest):
        raise SessionWindowRequestError("invalid_request")
    dependencies = deps or default_session_window_dependencies()
    return _build_session_window_attempt(
        request,
        dependencies,
        handoff_retry_count=0,
    )


def _build_session_window_attempt(
    request: SessionWindowRequest,
    dependencies: SessionWindowDependencies,
    *,
    handoff_retry_count: int,
) -> dict:
    profile = str(dependencies.active_profile() or "default").strip() or "default"
    db_path = Path(dependencies.state_db_path(profile))
    started = dependencies.monotonic()

    try:
        resolution = dependencies.resolve_shared_session(db_path, request.session_id)
    except Exception:
        return _typed_state(
            request,
            state="legacy_required",
            reason="resolution_failed",
        )
    if getattr(resolution, "status", None) != "found":
        reason = (
            "session_not_found"
            if getattr(resolution, "status", None) == "missing"
            else "resolution_unavailable"
        )
        return _typed_state(
            request,
            state="legacy_required",
            reason=reason,
        )
    members = tuple(getattr(resolution, "member_ids", ()) or ())
    if not 1 <= len(members) <= MAX_LINEAGE_DEPTH:
        return _typed_state(
            request,
            state="legacy_required",
            reason="lineage_limit",
            resolution=resolution,
        )
    if len(set(members)) != len(members) or any(
        not isinstance(member, str) or not member.strip() or "\x00" in member
        for member in members
    ):
        return _typed_state(
            request,
            state="legacy_required",
            reason="invalid_lineage",
            resolution=resolution,
        )
    if not dependencies.confirm_shared_session_target(db_path, resolution):
        if handoff_retry_count == 0:
            return _build_session_window_attempt(
                request,
                dependencies,
                handoff_retry_count=1,
            )
        return _typed_state(
            request,
            state="stale",
            reason="target_changed",
            resolution=resolution,
        )

    cursor_claims = None
    if request.older_cursor is not None:
        try:
            cursor_claims = dependencies.decode_older_cursor(
                token=request.older_cursor,
                profile=profile,
                resolution=resolution,
            )
        except Exception as exc:
            raise SessionWindowRequestError("invalid_older_cursor") from exc

    canonical_id = str(getattr(resolution, "canonical_id", "") or "")
    runtime_before = _validated_runtime_snapshot(
        dependencies.capture_runtime(profile, canonical_id)
    )
    try:
        page = dependencies.read_state_db_message_page(
            db_path=db_path,
            resolution=resolution,
            visible_limit=request.visible_limit,
            cursor=cursor_claims,
        )
    except Exception:
        return _typed_state(
            request,
            state="legacy_required",
            reason="bounded_reader_unavailable",
            resolution=resolution,
        )
    if getattr(page, "mode", None) != "cursor_v1":
        return _typed_state(
            request,
            state="legacy_required",
            reason=str(getattr(page, "fallback_reason", None) or "bounded_reader_unavailable"),
            resolution=resolution,
        )
    if not dependencies.confirm_shared_session_target(db_path, resolution):
        if handoff_retry_count == 0:
            return _build_session_window_attempt(
                request,
                dependencies,
                handoff_retry_count=1,
            )
        return _typed_state(
            request,
            state="stale",
            reason="target_changed",
            resolution=resolution,
        )
    runtime_after = _validated_runtime_snapshot(
        dependencies.capture_runtime(profile, canonical_id)
    )
    if _runtime_stream_id(runtime_before) != _runtime_stream_id(runtime_after):
        if handoff_retry_count == 0:
            return _build_session_window_attempt(
                request,
                dependencies,
                handoff_retry_count=1,
            )
        return _typed_state(
            request,
            state="reconnecting",
            reason="reconnect_ambiguous",
            resolution=resolution,
        )

    messages = list(getattr(page, "messages", ()) or ())
    if len(messages) > request.visible_limit:
        return _typed_state(
            request,
            state="legacy_required",
            reason="visible_limit_exceeded",
            resolution=resolution,
        )
    serialized_bytes = int(getattr(page, "serialized_bytes", 0) or 0)
    raw_rows_examined = int(getattr(page, "raw_rows_examined", 0) or 0)
    if (
        serialized_bytes > MAX_SERIALIZED_BYTES
        or raw_rows_examined > MAX_RAW_ROWS + MAX_TOOL_CLOSURE_ROWS
        or dependencies.monotonic() - started > READ_BUDGET_SECONDS
    ):
        return _typed_state(
            request,
            state="legacy_required",
            reason="read_budget_exceeded",
            resolution=resolution,
        )

    has_older = bool(getattr(page, "has_more", False))
    older_cursor = None
    if has_older:
        try:
            older_cursor = dependencies.encode_older_cursor(
                profile=profile,
                resolution=resolution,
                boundaries=tuple(getattr(page, "before_boundaries", ()) or ()),
            )
        except Exception:
            return _typed_state(
                request,
                state="legacy_required",
                reason="cursor_unavailable",
                resolution=resolution,
            )

    runtime_snapshot = runtime_after
    active_stream_id = (
        str(runtime_snapshot.get("stream_id") or "")
        if isinstance(runtime_snapshot, dict)
        else ""
    ) or None
    state = "reconnecting" if active_stream_id else "ready"
    reconnect_token = None
    if active_stream_id:
        checkpoint_event_id = str(
            runtime_snapshot.get("through_event_id") or ""
        )
        if not checkpoint_event_id:
            return _typed_state(
                request,
                state="reconnecting",
                reason="reconnect_checkpoint_unavailable",
                resolution=resolution,
            )
        reconnect_token = encode_reconnect_token(
            ReconnectClaims(
                version=RECONNECT_TOKEN_VERSION,
                profile_id=profile,
                canonical_session_id=canonical_id,
                stream_id=active_stream_id,
                checkpoint_event_id=checkpoint_event_id,
                expires_at=int(dependencies.wall_time()) + RECONNECT_TOKEN_TTL_SECONDS,
            )
        )
    result = {
        "requested_session_id": request.session_id,
        "canonical_session_id": str(resolution.canonical_id),
        "title": str((getattr(resolution, "canonical_row", {}) or {}).get("title") or ""),
        "model": str((getattr(resolution, "canonical_row", {}) or {}).get("model") or ""),
        "workspace": str((getattr(resolution, "canonical_row", {}) or {}).get("cwd") or ""),
        "messages": messages,
        "runtime_snapshot": runtime_snapshot,
        "conversation_window": {
            "schema": _SCHEMA,
            "state": state,
            "source": "state_db",
            "visible_count": len(messages),
            "has_older": has_older,
            "older_cursor": older_cursor,
            "newest_message_id": _stable_message_id(messages[-1]) if messages else None,
            "active_stream_id": active_stream_id,
            "reconnect_token": reconnect_token,
            "exact_total_available": False,
            "status_reason": None,
        },
    }
    _emit_diagnostic(
        dependencies,
        {
            "state": state,
            "lineage_depth": len(members),
            "sql_count": int(getattr(page, "sql_count", 0) or 0),
            "raw_rows_examined": raw_rows_examined,
            "visible_rows": len(messages),
            "serialized_bytes": serialized_bytes,
            "state_db_read_ms": max(
                0,
                int((dependencies.monotonic() - started) * 1000),
            ),
            "handoff_retry_count": handoff_retry_count,
        },
    )
    return result


def encode_reconnect_token(
    claims: ReconnectClaims,
    *,
    signing_key: bytes | None = None,
) -> str:
    """Encode bounded, signed authority for one exact live checkpoint."""
    if not isinstance(claims, ReconnectClaims):
        raise ValueError("invalid_reconnect_claims")
    if claims.version != RECONNECT_TOKEN_VERSION:
        raise ValueError("invalid_reconnect_version")
    values = (
        claims.profile_id,
        claims.canonical_session_id,
        claims.stream_id,
        claims.checkpoint_event_id,
    )
    if any(
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 1024
        for value in values
    ):
        raise ValueError("invalid_reconnect_claims")
    if (
        isinstance(claims.expires_at, bool)
        or not isinstance(claims.expires_at, int)
        or claims.expires_at <= 0
    ):
        raise ValueError("invalid_reconnect_expiry")
    payload = json.dumps(
        {
            "v": claims.version,
            "p": claims.profile_id,
            "s": claims.canonical_session_id,
            "r": claims.stream_id,
            "e": claims.checkpoint_event_id,
            "x": claims.expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key = _PROCESS_RECONNECT_SIGNING_KEY if signing_key is None else signing_key
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    token = f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"
    if len(token.encode("ascii")) > MAX_RECONNECT_TOKEN_BYTES:
        raise ValueError("reconnect_token_too_large")
    return token


def decode_reconnect_token(
    token: str,
    *,
    expected: ReconnectExpected | None = None,
    signing_key: bytes | None = None,
    now: float | None = None,
) -> ReconnectClaims:
    """Verify signature, expiry, and every authority-binding field."""
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8")) > MAX_RECONNECT_TOKEN_BYTES
        or token.count(".") != 1
    ):
        raise ValueError("invalid_reconnect_token")
    if expected is not None and not isinstance(expected, ReconnectExpected):
        raise ValueError("invalid_reconnect_expectation")
    payload_token, signature_token = token.split(".", 1)
    payload = _b64url_decode(payload_token)
    signature = _b64url_decode(signature_token)
    key = _PROCESS_RECONNECT_SIGNING_KEY if signing_key is None else signing_key
    wanted = hmac.new(key, payload, hashlib.sha256).digest()
    if len(signature) != len(wanted) or not hmac.compare_digest(signature, wanted):
        raise ValueError("invalid_reconnect_signature")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_reconnect_payload") from exc
    if not isinstance(raw, dict) or set(raw) != {"v", "p", "s", "r", "e", "x"}:
        raise ValueError("invalid_reconnect_payload")
    claims = ReconnectClaims(
        version=raw.get("v"),
        profile_id=raw.get("p"),
        canonical_session_id=raw.get("s"),
        stream_id=raw.get("r"),
        checkpoint_event_id=raw.get("e"),
        expires_at=raw.get("x"),
    )
    encode_reconnect_token(claims, signing_key=key)
    if claims.version != RECONNECT_TOKEN_VERSION:
        raise ValueError("invalid_reconnect_version")
    actual_now = time.time() if now is None else float(now)
    if actual_now > claims.expires_at:
        raise ValueError("expired_reconnect_token")
    if expected is not None and (
        claims.profile_id != expected.profile_id
        or claims.canonical_session_id != expected.canonical_session_id
        or claims.stream_id != expected.stream_id
        or claims.checkpoint_event_id != expected.checkpoint_event_id
    ):
        raise ValueError("reconnect_target_mismatch")
    return claims


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_reconnect_token")
    try:
        return base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except Exception as exc:
        raise ValueError("invalid_reconnect_token") from exc


def _validated_runtime_snapshot(snapshot: Any) -> dict | None:
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "run_snapshot_v1":
        return None
    stream_id = snapshot.get("stream_id")
    checkpoint = snapshot.get("through_event_id")
    if any(
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 1024
        for value in (stream_id, checkpoint)
    ):
        return None
    try:
        serialized = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(serialized) > MAX_SERIALIZED_BYTES:
        return None
    return snapshot


def _runtime_stream_id(snapshot: dict | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    return str(snapshot.get("stream_id") or "") or None


def _typed_state(
    request: SessionWindowRequest,
    *,
    state: str,
    reason: str,
    resolution: Any = None,
) -> dict:
    if state not in _VALID_STATES:
        state = "legacy_required"
    canonical = str(getattr(resolution, "canonical_id", "") or request.session_id)
    return {
        "requested_session_id": request.session_id,
        "canonical_session_id": canonical,
        "messages": [],
        "runtime_snapshot": None,
        "conversation_window": {
            "schema": _SCHEMA,
            "state": state,
            "source": "state_db",
            "visible_count": 0,
            "has_older": False,
            "older_cursor": None,
            "newest_message_id": None,
            "active_stream_id": None,
            "reconnect_token": None,
            "exact_total_available": False,
            "status_reason": str(reason or "unknown"),
        },
    }


def _single_query_value(query: Mapping[str, list[str]], key: str) -> str:
    raw = query.get(key, [])
    if not isinstance(raw, (list, tuple)) or len(raw) > 1:
        raise SessionWindowRequestError(f"invalid_{key}")
    if not raw:
        return ""
    value = raw[0]
    if not isinstance(value, str):
        raise SessionWindowRequestError(f"invalid_{key}")
    return value.strip()


def _stable_message_id(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    for key in ("_state_db_message_id", "id", "message_id"):
        value = message.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _emit_diagnostic(deps: SessionWindowDependencies, diagnostic: dict[str, Any]) -> None:
    if deps.diagnostic_sink is None:
        return
    try:
        deps.diagnostic_sink(dict(diagnostic))
    except Exception:
        return


def default_session_window_dependencies(
    *,
    capture_runtime: Callable[[str, str], dict | None] | None = None,
) -> SessionWindowDependencies:
    from api.agent_sessions import resolve_shared_session
    from api.bounded_target_confirmation import confirm_shared_session_target
    from api.models import _agent_state_db_path
    from api.profiles import get_active_profile_name
    from api.session_message_paging import read_state_db_message_page

    def state_db_path(profile: str) -> Path:
        path = _agent_state_db_path(profile=profile)
        return Path(path) if path is not None else Path("__missing_state_db__")

    def encode_cursor(*, profile, resolution, boundaries):
        claims = MessageCursorClaims(
            version=MESSAGE_CURSOR_VERSION,
            profile=profile,
            canonical_id=str(resolution.canonical_id),
            lineage_fingerprint=str(resolution.lineage_fingerprint),
            source_mode="state_db",
            database_identity_digest=message_cursor_database_identity_digest(
                tuple(resolution.database_identity)
            ),
            global_generation_hint=resolution.global_projection_generation_hint,
            receipt_generation=None,
            receipt_proof_digest=None,
            boundaries=tuple(boundaries),
        )
        return encode_message_cursor(
            claims,
            member_ids=tuple(resolution.member_ids),
        )

    def decode_cursor(*, token, profile, resolution):
        expected = MessageCursorExpected(
            profile=profile,
            canonical_id=str(resolution.canonical_id),
            lineage_fingerprint=str(resolution.lineage_fingerprint),
            source_mode="state_db",
            database_identity_digest=message_cursor_database_identity_digest(
                tuple(resolution.database_identity)
            ),
            global_generation_hint=resolution.global_projection_generation_hint,
            receipt_generation=None,
            receipt_proof_digest=None,
            member_ids=tuple(resolution.member_ids),
        )
        return decode_message_cursor(token, expected=expected)

    return SessionWindowDependencies(
        active_profile=get_active_profile_name,
        state_db_path=state_db_path,
        resolve_shared_session=resolve_shared_session,
        confirm_shared_session_target=confirm_shared_session_target,
        read_state_db_message_page=read_state_db_message_page,
        encode_older_cursor=encode_cursor,
        decode_older_cursor=decode_cursor,
        capture_runtime=capture_runtime or (lambda _profile, _canonical_id: None),
    )
