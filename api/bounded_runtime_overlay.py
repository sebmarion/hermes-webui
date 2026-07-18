"""Bounded, non-durable runtime overlays for a single active run.

The settled transcript remains canonical.  This module only reads the one
journal owned by an already-proven active run and returns presentation data that
the response assembler may overlay before redaction.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Callable, Iterable

from api.run_journal import (
    _iter_bounded_raw_jsonl_lines,
    _run_path,
    _terminal_state_for_event,
)


DEFAULT_MAX_JOURNAL_BYTES = 256 * 1024
DEFAULT_MAX_JOURNAL_ROWS = 256
VERIFIED_RUNTIME_OVERLAY_CAPABILITY = object()
_RUN_JOURNAL_FIELDS = frozenset(
    {
        "version",
        "event_id",
        "seq",
        "run_id",
        "session_id",
        "event",
        "type",
        "created_at",
        "terminal",
        "terminal_state",
        "payload",
    }
)


@dataclass(frozen=True)
class RuntimeOwner:
    """Proof supplied by the active in-memory stream registry."""

    profile: str
    session_id: str
    run_id: str
    active: bool = True
    capability_token: str | None = None


@dataclass(frozen=True)
class RuntimeOverlayResult:
    """An additive runtime view, or a typed fail-closed degradation."""

    status: str
    messages: list[dict]
    journal_events: list[dict]
    pending_user_message: str | None = None
    rows_read: int = 0
    bytes_read: int = 0
    capability_marker: object | None = None

    @property
    def available(self) -> bool:
        return self.status == "ok"


def _message_identities(message: dict) -> frozenset[tuple[str, str]]:
    """Return every stable identifier, never a content-derived fallback."""
    identities: set[tuple[str, str]] = set()
    for key in ("_state_db_message_id", "message_id", "id", "_id"):
        value = message.get(key)
        if value is not None and str(value):
            identities.add(("message", str(value)))
    for key in ("_runtime_message_id", "event_id"):
        value = message.get(key)
        if value is not None and str(value):
            identities.add(("runtime", str(value)))
    return frozenset(identities)


def _runtime_identities(message: dict) -> frozenset[str]:
    return frozenset(
        message[key]
        for key in ("_runtime_message_id", "event_id")
        if isinstance(message.get(key), str) and message[key]
    )


def _runtime_ids_match_run(message: dict, run_id: str) -> bool:
    prefix = f"{run_id}:"
    values = _runtime_identities(message)
    return bool(values) and all(
        value.startswith(prefix) and len(value) > len(prefix) for value in values
    )


def _serialized_bytes(value: dict) -> int | None:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return None


def _strict_bounded_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _finite_json(value: object) -> bool:
    """Accept only finite JSON values after strict JSON decoding."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    return False


def _reject_json_constant(_value: str):
    raise ValueError("non-finite JSON constant")


def _valid_owned_event(event: dict, *, session_id: str, run_id: str) -> tuple[bool, bool]:
    """Return ``(valid, owner_matches)`` for the exact append_run_event schema."""
    if not isinstance(event, dict):
        return False, True
    owner_matches = (
        event.get("run_id") == run_id and event.get("session_id") == session_id
    )
    if not owner_matches:
        return False, False
    if set(event) != _RUN_JOURNAL_FIELDS:
        return False, True
    seq = event["seq"]
    event_name = event["event"]
    payload = event["payload"]
    created_at = event["created_at"]
    terminal_state = _terminal_state_for_event(event_name, payload)
    valid = (
        type(event["version"]) is int
        and event["version"] == 1
        and type(seq) is int
        and seq >= 1
        and isinstance(event["event_id"], str)
        and event["event_id"] == f"{run_id}:{seq}"
        and isinstance(event_name, str)
        and bool(event_name)
        and event_name == event_name.strip()
        and isinstance(event["type"], str)
        and event["type"] == event_name
        and isinstance(payload, dict)
        and _finite_json(payload)
        and _finite_number(created_at)
        and type(event["terminal"]) is bool
        and event["terminal"] is bool(terminal_state)
        and event["terminal_state"] == terminal_state
    )
    return valid, True


_TOOL_ID_KEYS = ("id", "tid", "tool_call_id", "tool_use_id", "call_id")


def _journal_tool_id(payload: dict) -> str:
    for key in _TOOL_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _journal_runtime_message(events: list[dict], run_id: str) -> dict | None:
    """Project one already-bounded, owner-verified journal into one live row."""
    assistant_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    last_timestamp: float | int | None = None

    for event in events:
        payload = event["payload"]
        event_name = event["event"]
        last_timestamp = event["created_at"]
        if event_name == "token":
            text = payload.get("text")
            if isinstance(text, str) and text:
                assistant_parts.append(text)
            continue
        if event_name == "reasoning":
            text = payload.get("text")
            if isinstance(text, str) and text:
                reasoning_parts.append(text)
            continue
        if event_name == "interim_assistant":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if payload.get("already_streamed") is True:
                if not assistant_parts:
                    assistant_parts.append(text.strip())
            else:
                if assistant_parts:
                    assistant_parts.append("\n\n")
                assistant_parts.append(text.strip())
            continue
        if event_name == "tool":
            name = payload.get("name")
            if not isinstance(name, str) or not name or name == "clarify":
                continue
            tool_id = _journal_tool_id(payload)
            call = {
                "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
                "done": False,
                "id": tool_id,
                "is_error": False,
                "name": name,
                "preview": str(payload.get("preview") or ""),
            }
            tool_calls.append(call)
            continue
        if event_name == "tool_complete":
            name = payload.get("name")
            tool_id = _journal_tool_id(payload)
            for call in reversed(tool_calls):
                if call["done"]:
                    continue
                if (tool_id and call["id"] == tool_id) or (
                    not tool_id and isinstance(name, str) and call["name"] == name
                ):
                    call["done"] = True
                    call["is_error"] = payload.get("is_error") is True
                    if payload.get("preview") is not None:
                        call["preview"] = str(payload.get("preview") or "")
                    break

    content = "".join(assistant_parts)
    reasoning = "".join(reasoning_parts)
    if not content and not reasoning and not tool_calls:
        return None
    message: dict = {
        "_journal_snapshot": True,
        "_live": True,
        "_partial": True,
        "_runtime_message_id": f"{run_id}:assistant",
        "content": content,
        "role": "assistant",
    }
    if reasoning:
        message["reasoning"] = reasoning
    if tool_calls:
        message["_partial_tool_calls"] = tool_calls
    if last_timestamp is not None:
        message["timestamp"] = last_timestamp
    return message


def _base_result(status: str, settled_messages: Iterable[dict]) -> RuntimeOverlayResult:
    return RuntimeOverlayResult(
        status=status,
        messages=[deepcopy(message) for message in settled_messages if isinstance(message, dict)],
        journal_events=[],
    )


def _read_owned_journal(
    *,
    session_id: str,
    run_id: str,
    profile: str,
    session_dir: Path | None,
    max_bytes: int,
    max_rows: int,
) -> tuple[str, list[dict], int, int]:
    """Read one exact journal path with strict identity and resource checks."""
    try:
        path = _run_path(session_id, run_id, session_dir=session_dir)
    except ValueError:
        return "runtime_owner_invalid", [], 0, 0

    events: list[dict] = []
    expected_seq = 1
    bytes_read = 0
    try:
        for _line_no, raw, total_bytes in _iter_bounded_raw_jsonl_lines(path, max_bytes=max_bytes):
            bytes_read = total_bytes
            if not raw.strip():
                continue
            try:
                event = json.loads(
                    raw.decode("utf-8"), parse_constant=_reject_json_constant
                )
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                return "runtime_journal_malformed", [], len(events), bytes_read
            valid, owner_matches = _valid_owned_event(
                event, session_id=session_id, run_id=run_id
            )
            if not owner_matches:
                return "runtime_journal_owner_mismatch", [], len(events), bytes_read
            if not valid or event["seq"] != expected_seq:
                return "runtime_journal_malformed", [], len(events), bytes_read
            expected_seq += 1
            events.append(event)
            if len(events) > max_rows:
                return "runtime_journal_limit_rows", [], len(events), bytes_read
    except ValueError as exc:
        if str(exc) == "replay_limit_bytes":
            return "runtime_journal_limit_bytes", [], len(events), bytes_read
        return "runtime_journal_malformed", [], len(events), bytes_read
    except OSError:
        return "runtime_journal_io_error", [], len(events), bytes_read
    return "ok", events, len(events), bytes_read


def assemble_runtime_overlay(
    settled_messages: Iterable[dict],
    *,
    profile: str,
    session_id: str,
    owner: RuntimeOwner | None,
    owner_verifier: Callable[[RuntimeOwner], bool] | None = None,
    run_id: str | None = None,
    session_dir: Path | None = None,
    in_memory_messages: Iterable[dict] = (),
    pending_user_message: str | None = None,
    pending_user_message_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
    max_rows: int = DEFAULT_MAX_JOURNAL_ROWS,
) -> RuntimeOverlayResult:
    """Build a bounded overlay for exactly one proven active run.

    Any ownership, journal, budget, or identity failure returns the settled
    page unchanged and omits all runtime content.  The caller can expose the
    returned typed status as a diagnostic/fallback reason without persisting it.
    """
    settled = [deepcopy(message) for message in settled_messages if isinstance(message, dict)]
    if type(max_bytes) is not int or type(max_rows) is not int or max_bytes < 0 or max_rows < 0:
        return _base_result("runtime_limit_invalid", settled)
    if owner is None or owner.active is not True:
        return _base_result("no_active_owner", settled)
    if not _strict_bounded_string(profile) or not _strict_bounded_string(session_id):
        return _base_result("runtime_owner_invalid", settled)
    if not (
        _strict_bounded_string(owner.profile)
        and _strict_bounded_string(owner.session_id)
        and _strict_bounded_string(owner.run_id)
    ):
        return _base_result("runtime_owner_invalid", settled)
    if owner.profile != profile:
        return _base_result("runtime_owner_profile_mismatch", settled)
    if owner.session_id != session_id:
        return _base_result("runtime_owner_session_mismatch", settled)
    if (
        not isinstance(owner.capability_token, str)
        or not owner.capability_token
        or len(owner.capability_token) > 512
    ):
        return _base_result("runtime_owner_unverified", settled)
    if run_id is not None:
        if not _strict_bounded_string(run_id):
            return _base_result("runtime_owner_invalid", settled)
        if run_id != owner.run_id:
            return _base_result("runtime_owner_run_mismatch", settled)
    if owner_verifier is None:
        return _base_result("runtime_owner_unverified", settled)
    try:
        owner_verified = owner_verifier(owner) is True
    except Exception:
        owner_verified = False
    if not owner_verified:
        return _base_result("runtime_owner_unverified", settled)

    status, events, rows_read, bytes_read = _read_owned_journal(
        session_id=session_id,
        run_id=owner.run_id,
        profile=profile,
        session_dir=session_dir,
        max_bytes=max_bytes,
        max_rows=max_rows,
    )
    if status != "ok":
        return RuntimeOverlayResult(status, settled, [], rows_read=rows_read, bytes_read=bytes_read)

    identities: set[tuple[str, str]] = set()
    for message in settled:
        identities.update(_message_identities(message))
    merged = list(settled)
    runtime_rows = rows_read
    runtime_bytes = bytes_read

    pending: str | None = None
    candidates: list[dict] = []
    if pending_user_message is not None:
        if not isinstance(pending_user_message, str):
            return RuntimeOverlayResult(
                "runtime_message_malformed",
                settled,
                [],
                rows_read=rows_read,
                bytes_read=bytes_read,
            )
        pending = pending_user_message
        candidates.append(
            {
                "_runtime_message_id": (
                    f"{owner.run_id}:pending-user"
                    if pending_user_message_id is None
                    else pending_user_message_id
                ),
                "role": "user",
                "content": pending,
                "_pending": True,
                "_live": True,
            }
        )

    for candidate in candidates:
        if not _runtime_ids_match_run(candidate, owner.run_id):
            return RuntimeOverlayResult(
                "runtime_message_run_mismatch", settled, [], rows_read=rows_read, bytes_read=bytes_read
            )
        size = _serialized_bytes(candidate)
        if size is None:
            return RuntimeOverlayResult(
                "runtime_message_malformed", settled, [], rows_read=rows_read, bytes_read=bytes_read
            )
        runtime_rows += 1
        runtime_bytes += size
        if runtime_rows > max_rows:
            return RuntimeOverlayResult(
                "runtime_overlay_limit_rows", settled, [], rows_read=runtime_rows, bytes_read=runtime_bytes
            )
        if runtime_bytes > max_bytes:
            return RuntimeOverlayResult(
                "runtime_overlay_limit_bytes", settled, [], rows_read=runtime_rows, bytes_read=runtime_bytes
            )
        candidate_identities = _message_identities(candidate)
        if not identities.intersection(candidate_identities):
            identities.update(candidate_identities)
            merged.append(deepcopy(candidate))

    in_memory_assistant_present = False
    for message in in_memory_messages:
        if not isinstance(message, dict):
            return RuntimeOverlayResult("runtime_message_malformed", settled, [], rows_read=rows_read, bytes_read=bytes_read)
        message_identities = _message_identities(message)
        runtime_identities = _runtime_identities(message)
        if not message_identities or not runtime_identities:
            return RuntimeOverlayResult(
                "runtime_message_identity_missing", settled, [], rows_read=rows_read, bytes_read=bytes_read
            )
        if not _runtime_ids_match_run(message, owner.run_id):
            return RuntimeOverlayResult(
                "runtime_message_run_mismatch", settled, [], rows_read=rows_read, bytes_read=bytes_read
            )
        if message.get("role") == "assistant":
            in_memory_assistant_present = True
        size = _serialized_bytes(message)
        if size is None:
            return RuntimeOverlayResult(
                "runtime_message_malformed", settled, [], rows_read=rows_read, bytes_read=bytes_read
            )
        runtime_rows += 1
        runtime_bytes += size
        if runtime_rows > max_rows:
            return RuntimeOverlayResult(
                "runtime_overlay_limit_rows", settled, [], rows_read=runtime_rows, bytes_read=runtime_bytes
            )
        if runtime_bytes > max_bytes:
            return RuntimeOverlayResult(
                "runtime_overlay_limit_bytes", settled, [], rows_read=runtime_rows, bytes_read=runtime_bytes
            )
        if identities.intersection(message_identities):
            continue
        identities.update(message_identities)
        merged.append(deepcopy(message))

    journal_message = _journal_runtime_message(events, owner.run_id)
    if journal_message is not None and not in_memory_assistant_present:
        journal_identities = _message_identities(journal_message)
        # A verified in-memory buffer is newer than the same-run journal
        # projection.  Stable runtime identity makes this a proof-based
        # replacement decision rather than content deduplication.
        if not identities.intersection(journal_identities):
            size = _serialized_bytes(journal_message)
            if size is None:
                return RuntimeOverlayResult(
                    "runtime_message_malformed",
                    settled,
                    [],
                    rows_read=runtime_rows,
                    bytes_read=runtime_bytes,
                )
            runtime_rows += 1
            runtime_bytes += size
            if runtime_rows > max_rows:
                return RuntimeOverlayResult(
                    "runtime_overlay_limit_rows",
                    settled,
                    [],
                    rows_read=runtime_rows,
                    bytes_read=runtime_bytes,
                )
            if runtime_bytes > max_bytes:
                return RuntimeOverlayResult(
                    "runtime_overlay_limit_bytes",
                    settled,
                    [],
                    rows_read=runtime_rows,
                    bytes_read=runtime_bytes,
                )
            identities.update(journal_identities)
            merged.append(journal_message)

    try:
        owner_still_verified = owner_verifier(owner) is True
    except Exception:
        owner_still_verified = False
    if not owner_still_verified:
        return RuntimeOverlayResult(
            "runtime_owner_rotated", settled, [], rows_read=runtime_rows, bytes_read=runtime_bytes
        )
    return RuntimeOverlayResult(
        "ok",
        merged,
        deepcopy(events),
        pending,
        runtime_rows,
        runtime_bytes,
        VERIFIED_RUNTIME_OVERLAY_CAPABILITY,
    )


# The descriptive name is retained for the eventual bounded-session assembler.
build_bounded_runtime_overlay = assemble_runtime_overlay
