"""Read message history for an already-resolved shared-session lineage."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from api.agent_sessions import open_state_db_readonly


_MEMBER_QUERY_CHUNK = 500
_MESSAGE_FETCH_CHUNK = 512
_MAX_MEMBER_ID_LENGTH = 1024
_JSON_COLUMNS = {
    "tool_calls",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
}
_OPTIONAL_COLUMNS = (
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "reasoning",
    "reasoning_details",
    "codex_reasoning_items",
    "reasoning_content",
    "codex_message_items",
)


class ResolvedSessionHistoryUnavailable(RuntimeError):
    """The bounded reader cannot distinguish history from an empty result."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _unavailable_history(
    reason: str,
    *,
    require_available: bool,
) -> list[dict[str, Any]]:
    if require_available:
        raise ResolvedSessionHistoryUnavailable(reason)
    return []


def _normalize_member_ids(member_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(member_ids, (str, bytes)):
        raise ValueError("member_ids must be an iterable of session IDs")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in member_ids:
        if not isinstance(value, str):
            raise ValueError("member IDs must be strings")
        member_id = value.strip()
        if not member_id:
            continue
        if "\x00" in member_id or len(member_id) > _MAX_MEMBER_ID_LENGTH:
            raise ValueError("invalid member ID")
        if member_id not in seen:
            seen.add(member_id)
            normalized.append(member_id)
    return tuple(normalized)


def _decode_optional_value(column: str, value: Any) -> Any:
    if column not in _JSON_COLUMNS or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _timestamp_sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, 0.0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return (1, number)
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            if text and math.isfinite(number):
                return (1, number)
        return (2, text)
    return (2, str(value))


def _message_id_sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value)
    if isinstance(value, float) and math.isfinite(value):
        return (0, value)
    if value is None:
        return (2, "")
    return (1, str(value))


def read_resolved_session_history(
    *,
    db_path: Path,
    member_ids: Iterable[str],
    include_inactive: bool = False,
    require_available: bool = False,
) -> list[dict[str, Any]]:
    """Read chronological messages for explicit, previously resolved members.

    This adapter deliberately knows nothing about the ``sessions`` table. The
    caller owns canonical-session and lineage resolution; every message query
    here remains scoped to the supplied member IDs.
    """
    members = _normalize_member_ids(member_ids)
    if not members:
        return []

    path = Path(db_path)
    if not path.exists():
        return _unavailable_history(
            "missing_database",
            require_available=require_available,
        )

    member_order = {member_id: index for index, member_id in enumerate(members)}
    collected: list[tuple[dict[str, Any], Any, int, int]] = []
    sequence = 0

    try:
        with closing(open_state_db_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            available = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if not {"id", "session_id", "role", "content", "timestamp"}.issubset(
                available
            ):
                return _unavailable_history(
                    "unsupported_schema",
                    require_available=require_available,
                )

            selected = ["id", "session_id", "role", "content"]
            selected.append("timestamp")
            selected.extend(
                column for column in _OPTIONAL_COLUMNS if column in available
            )

            visibility_clauses: list[str] = []
            if not include_inactive:
                if "active" in available:
                    visibility_clauses.append("(active IS NULL OR active != 0)")
            visibility_sql = "".join(
                f" AND {clause}" for clause in visibility_clauses
            )

            for start in range(0, len(members), _MEMBER_QUERY_CHUNK):
                chunk = members[start : start + _MEMBER_QUERY_CHUNK]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"SELECT {', '.join(selected)} FROM messages "
                    f"WHERE session_id IN ({placeholders}){visibility_sql}",
                    chunk,
                )
                while True:
                    rows = cursor.fetchmany(_MESSAGE_FETCH_CHUNK)
                    if not rows:
                        break
                    for row in rows:
                        message: dict[str, Any] = {
                            "role": row["role"],
                            "content": row["content"],
                            "timestamp": row["timestamp"],
                        }
                        state_message_id = row["id"]

                        for column in _OPTIONAL_COLUMNS:
                            if column not in available:
                                continue
                            value = row[column]
                            if value in (None, ""):
                                continue
                            message[column] = _decode_optional_value(column, value)
                        if (
                            message.get("role") == "tool"
                            and message.get("tool_name")
                            and not message.get("name")
                        ):
                            message["name"] = message["tool_name"]

                        collected.append(
                            (
                                message,
                                state_message_id,
                                member_order.get(str(row["session_id"]), len(members)),
                                sequence,
                            )
                        )
                        sequence += 1
    except ResolvedSessionHistoryUnavailable:
        raise
    except Exception as exc:
        if require_available:
            raise ResolvedSessionHistoryUnavailable("read_failed") from exc
        return []

    collected.sort(
        key=lambda item: (
            _timestamp_sort_key(item[0]["timestamp"]),
            _message_id_sort_key(item[1]),
            item[2],
            item[3],
        )
    )
    return [message for message, _message_id, _member_order, _sequence in collected]
