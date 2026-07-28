"""Read-only capability and bounded paging primitives for conversation history."""

from __future__ import annotations

import base64
import hashlib
import heapq
import hmac
import json
import math
import re
import secrets
import sqlite3
import struct
import threading
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeAlias
from urllib.parse import parse_qsl

from api.agent_sessions import open_state_db_readonly, shared_state_db_identity


_CAPABILITY_CACHE_MAX = 64
MESSAGE_CURSOR_VERSION = 1
MAX_MESSAGE_CURSOR_TOKEN_BYTES = 16 * 1024
_MAX_CURSOR_BOUNDARIES = 256
_CURSOR_BOUNDARY_RECORD_BYTES = 20
_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1
_PROCESS_CURSOR_SIGNING_KEY = secrets.token_bytes(32)
_ORDINARY_MESSAGE_BYTES_MAX = 2 * 1024 * 1024
_TOOL_CLOSURE_BYTES_MAX = 512 * 1024
_TOOL_CLOSURE_RAW_ROWS_MAX = 64
_COMBINED_MESSAGE_BYTES_MAX = _ORDINARY_MESSAGE_BYTES_MAX + _TOOL_CLOSURE_BYTES_MAX
_SHADOW_MAX_MESSAGES = 4096
_SHADOW_MAX_PAGES = 512
_LIMITED_TOOL_CONTENT_MAX_CHARS = 4096
_TOOL_CONTENT_PROJECTION_CHARS = _LIMITED_TOOL_CONTENT_MAX_CHARS + 1
_TOOL_CONTENT_PROJECTION_BYTES = _TOOL_CONTENT_PROJECTION_CHARS * 4
_LIMITED_ORDINARY_CONTENT_MAX_CHARS = 4096
_ORDINARY_CONTENT_PROJECTION_CHARS = _LIMITED_ORDINARY_CONTENT_MAX_CHARS + 1
_ORDINARY_CONTENT_PROJECTION_BYTES = _ORDINARY_CONTENT_PROJECTION_CHARS * 4
_ORDINARY_OPTIONAL_PROJECTION_BYTES = 128 * 1024
_PAIRING_TOOL_CALLS_MAX_CHARS = 32 * 1024
_PAIRING_TOOL_CALL_ID_MAX_CHARS = 1024
_PAIRING_ROLE_MAX_CHARS = 32
_LIMITED_TOOL_CONTENT_NOTICE = (
    "\n\n[Tool output truncated in paginated session response; "
    "load the full transcript to inspect the complete result.]"
)
_LIMITED_ORDINARY_CONTENT_NOTICE = (
    "\n\n[Message content truncated in paginated session response; "
    "load the full transcript before editing, resending, or forking this message.]"
)
_PAIRING_FIELD_CHAR_LIMITS = {
    "role": _PAIRING_ROLE_MAX_CHARS,
    "tool_call_id": _PAIRING_TOOL_CALL_ID_MAX_CHARS,
    "tool_calls": _PAIRING_TOOL_CALLS_MAX_CHARS,
}
_INDEX_COLUMNS_RE = re.compile(
    r"\bON\s+(?:[`\"\[]?[^\s(\]`\"]+[`\"\]]?)\s*\((.*)\)\s*;?$",
    re.IGNORECASE | re.DOTALL,
)
_NUMERIC_TIMESTAMP_TYPES = {
    "INTEGER",
    "INT",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "REAL",
    "FLOAT",
    "DOUBLE",
    "NUMERIC",
    "DECIMAL",
}
_INTEGER_TYPES = {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"}
_MESSAGE_PAGE_OPTIONAL_COLUMNS = (
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "reasoning",
    "reasoning_details",
    "codex_reasoning_items",
    "reasoning_content",
    "codex_message_items",
)
_MESSAGE_PAGE_JSON_COLUMNS = {
    "tool_calls",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
}


@dataclass(frozen=True)
class MessagePagingCapability:
    supported: bool
    schema_version: int
    message_index: str | None
    has_active: bool
    fallback_reason: str | None
    ordering_columns: tuple[str, str] = ("timestamp", "id")
    message_columns: tuple[str, ...] = ()


class MessageCursorError(ValueError):
    """An opaque cursor is malformed, untrusted, stale, or cross-context."""


class MessageCursorRequestMismatch(MessageCursorError):
    """A valid cursor belongs to a different request/profile context."""


class MessageCursorStateMismatch(MessageCursorError):
    """A valid cursor no longer matches the target's proven state."""


@dataclass(frozen=True)
class MessageCursorBoundary:
    member_id: str
    timestamp: float | int | None
    message_id: int
    inclusive: bool = False


@dataclass(frozen=True)
class MessageCursorClaims:
    version: int
    profile: str
    canonical_id: str
    lineage_fingerprint: str
    source_mode: str
    database_identity_digest: str
    global_generation_hint: int | None
    receipt_generation: int | None
    receipt_proof_digest: str | None
    boundaries: tuple[MessageCursorBoundary, ...]


@dataclass(frozen=True)
class MessageCursorExpected:
    profile: str
    canonical_id: str
    lineage_fingerprint: str
    source_mode: str
    database_identity_digest: str
    global_generation_hint: int | None
    receipt_generation: int | None
    receipt_proof_digest: str | None
    member_ids: tuple[str, ...] = ()


class MessagePageValidationError(ValueError):
    """A direct reader argument violates the physical paging contract."""


class MessagePagingUnavailable(RuntimeError):
    """The current state cannot be read with the bounded cursor contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class StateDBMessagePage:
    mode: str
    messages: tuple[dict[str, Any], ...]
    before_boundaries: tuple[MessageCursorBoundary, ...]
    has_more: bool
    visible_count: int
    raw_rows_examined: int
    serialized_bytes: int
    sql_count: int
    query_plan_indexed: bool
    ordinary_serialized_bytes: int = 0
    closure_serialized_bytes: int = 0
    closure_rows_examined: int = 0
    tool_pair_status: str = "not_evaluated"
    fallback_reason: str | None = None


@dataclass(frozen=True)
class MessagePageShadowObservation:
    mode: str
    matched: bool | None
    fallback_reason: str | None
    visible_count: int
    raw_rows_examined: int
    serialized_bytes: int
    sql_count: int
    query_plan_indexed: bool

    def as_diagnostic(self) -> dict[str, Any]:
        """Return the bounded, content-free shape allowed in shadow logs."""
        return {
            "stage": "state_message_page",
            "mode": self.mode,
            "matched": self.matched,
            "fallback_reason": self.fallback_reason,
            "visible_count": self.visible_count,
            "raw_rows_examined": self.raw_rows_examined,
            "serialized_bytes": self.serialized_bytes,
            "sql_count": self.sql_count,
            "query_plan_indexed": self.query_plan_indexed,
        }


MessagePageShadowExactMatchConsumer: TypeAlias = Callable[
    [tuple[Any, ...], tuple[Any, ...], int, int], None
]


@dataclass(frozen=True)
class MessagePagingNegotiation:
    requested: bool
    visible_limit: int | None
    cursor_token: str | None


_CAPABILITY_CACHE: OrderedDict[
    tuple[tuple[Any, ...], int],
    MessagePagingCapability,
] = OrderedDict()
_CAPABILITY_CACHE_LOCK = threading.RLock()


def parse_message_paging_negotiation(query_string: str) -> MessagePagingNegotiation:
    """Parse cursor-only parameters without changing legacy numeric semantics."""
    values: dict[str, list[str]] = {}
    for key, value in parse_qsl(str(query_string or ""), keep_blank_values=True):
        if key in {"message_paging", "msg_limit", "msg_cursor", "msg_before"}:
            values.setdefault(key, []).append(value)
    modes = values.get("message_paging", [])
    if not modes:
        if values.get("msg_cursor"):
            raise MessagePageValidationError(
                "msg_cursor requires message_paging=cursor_v1"
            )
        return MessagePagingNegotiation(False, None, None)
    if len(modes) != 1 or modes[0] != "cursor_v1":
        raise MessagePageValidationError("message_paging must be cursor_v1")
    limits = values.get("msg_limit", [])
    if len(limits) != 1 or re.fullmatch(r"[0-9]+", limits[0]) is None:
        raise MessagePageValidationError(
            "cursor msg_limit must be one integer from 1 to 100"
        )
    visible_limit = int(limits[0])
    if not 1 <= visible_limit <= 100:
        raise MessagePageValidationError(
            "cursor msg_limit must be one integer from 1 to 100"
        )
    cursor_values = values.get("msg_cursor", [])
    if len(cursor_values) > 1:
        raise MessagePageValidationError("msg_cursor must not be repeated")
    if values.get("msg_before"):
        raise MessagePageValidationError(
            "msg_before and cursor paging are mutually exclusive"
        )
    cursor_token = cursor_values[0] if cursor_values else None
    if cursor_token is not None:
        if (
            not cursor_token
            or len(cursor_token.encode("utf-8")) > MAX_MESSAGE_CURSOR_TOKEN_BYTES
            or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", cursor_token) is None
        ):
            raise MessagePageValidationError("msg_cursor is malformed")
    return MessagePagingNegotiation(True, visible_limit, cursor_token)


def _cursor_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _cursor_b64decode(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise MessageCursorError("cursor encoding is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise MessageCursorError("cursor encoding is invalid") from exc


def _cursor_text(value: Any, field: str, *, max_length: int = 1024) -> str:
    if not isinstance(value, str):
        raise MessageCursorError(f"cursor {field} is invalid")
    clean = value.strip()
    if not clean or "\x00" in clean or len(clean) > max_length:
        raise MessageCursorError(f"cursor {field} is invalid")
    return clean


def _cursor_optional_generation(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MessageCursorError(f"cursor {field} is invalid")
    return value


def _cursor_optional_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    digest = _cursor_text(value, field)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise MessageCursorError(f"cursor {field} is invalid")
    return digest


def _validated_cursor_members(member_ids: Any) -> tuple[str, ...]:
    if not isinstance(member_ids, (list, tuple)):
        raise MessageCursorError("cursor member_ids are invalid")
    if len(member_ids) > _MAX_CURSOR_BOUNDARIES:
        raise MessageCursorError("cursor member_ids are invalid")
    members = tuple(
        _cursor_text(member_id, "member_id")
        for member_id in member_ids
    )
    if len(set(members)) != len(members):
        raise MessageCursorError("cursor member_ids contain duplicates")
    return members


def _validated_cursor_boundaries(
    boundaries: Any,
) -> tuple[MessageCursorBoundary, ...]:
    if not isinstance(boundaries, (list, tuple)):
        raise MessageCursorError("cursor boundaries are invalid")
    if len(boundaries) > _MAX_CURSOR_BOUNDARIES:
        raise MessageCursorError("cursor boundaries are invalid")
    normalized = []
    seen = set()
    for raw in boundaries:
        if isinstance(raw, MessageCursorBoundary):
            member_id, timestamp, message_id, inclusive = (
                raw.member_id,
                raw.timestamp,
                raw.message_id,
                raw.inclusive,
            )
        elif isinstance(raw, (list, tuple)) and len(raw) in {3, 4}:
            member_id, timestamp, message_id = raw[:3]
            inclusive = raw[3] if len(raw) == 4 else False
        else:
            raise MessageCursorError("cursor boundaries are invalid")
        member_id = _cursor_text(member_id, "boundary member_id")
        if member_id in seen:
            raise MessageCursorError("cursor boundaries contain duplicate members")
        seen.add(member_id)
        if timestamp is not None:
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise MessageCursorError("cursor boundary timestamp is invalid")
            if isinstance(timestamp, float) and not math.isfinite(timestamp):
                raise MessageCursorError("cursor boundary timestamp is invalid")
            if isinstance(timestamp, int) and not (
                _SQLITE_INT64_MIN <= timestamp <= _SQLITE_INT64_MAX
            ):
                raise MessageCursorError("cursor boundary timestamp is invalid")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or not 0 <= message_id <= _SQLITE_INT64_MAX
        ):
            raise MessageCursorError("cursor boundary message_id is invalid")
        if not isinstance(inclusive, bool):
            raise MessageCursorError("cursor boundary inclusivity is invalid")
        normalized.append(
            MessageCursorBoundary(member_id, timestamp, message_id, inclusive)
        )
    return tuple(normalized)


def _validated_cursor_claims(claims: MessageCursorClaims) -> MessageCursorClaims:
    if not isinstance(claims, MessageCursorClaims):
        raise MessageCursorError("cursor claims are invalid")
    if isinstance(claims.version, bool) or not isinstance(claims.version, int):
        raise MessageCursorError("cursor version is invalid")
    fingerprint = _cursor_text(claims.lineage_fingerprint, "lineage_fingerprint")
    database_digest = _cursor_text(
        claims.database_identity_digest,
        "database_identity_digest",
    )
    for field, digest in (
        ("lineage_fingerprint", fingerprint),
        ("database_identity_digest", database_digest),
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise MessageCursorError(f"cursor {field} is invalid")
    receipt_generation = _cursor_optional_generation(
        claims.receipt_generation,
        "receipt_generation",
    )
    receipt_proof_digest = _cursor_optional_digest(
        claims.receipt_proof_digest,
        "receipt_proof_digest",
    )
    if (receipt_generation is None) != (receipt_proof_digest is None):
        raise MessageCursorError(
            "cursor receipt generation and proof digest must be present together"
        )
    return MessageCursorClaims(
        version=claims.version,
        profile=_cursor_text(claims.profile, "profile", max_length=256),
        canonical_id=_cursor_text(claims.canonical_id, "canonical_id"),
        lineage_fingerprint=fingerprint,
        source_mode=_cursor_text(claims.source_mode, "source_mode", max_length=64),
        database_identity_digest=database_digest,
        global_generation_hint=_cursor_optional_generation(
            claims.global_generation_hint,
            "global_generation_hint",
        ),
        receipt_generation=receipt_generation,
        receipt_proof_digest=receipt_proof_digest,
        boundaries=_validated_cursor_boundaries(claims.boundaries),
    )


def _encode_cursor_boundaries(
    boundaries: tuple[MessageCursorBoundary, ...],
    member_ids: tuple[str, ...],
) -> str:
    member_index = {member_id: index for index, member_id in enumerate(member_ids)}
    encoded = bytearray()
    for boundary in boundaries:
        ordinal = member_index.get(boundary.member_id)
        if ordinal is None:
            raise MessageCursorError("cursor boundary member is outside lineage")
        if boundary.timestamp is None:
            timestamp_tag = 0
            timestamp_bytes = b"\x00" * 8
        elif isinstance(boundary.timestamp, int):
            timestamp_tag = 1
            timestamp_bytes = struct.pack(">q", boundary.timestamp)
        else:
            timestamp_tag = 2
            timestamp_bytes = struct.pack(">d", boundary.timestamp)
        encoded.extend(
            struct.pack(
                ">HBB",
                ordinal,
                timestamp_tag,
                int(boundary.inclusive),
            )
        )
        encoded.extend(timestamp_bytes)
        encoded.extend(struct.pack(">Q", boundary.message_id))
    return _cursor_b64encode(bytes(encoded))


def _decode_cursor_boundaries(
    value: Any,
    member_ids: tuple[str, ...],
) -> tuple[MessageCursorBoundary, ...]:
    encoded = _cursor_text(
        value,
        "boundaries",
        max_length=MAX_MESSAGE_CURSOR_TOKEN_BYTES,
    )
    raw = _cursor_b64decode(encoded)
    if (
        len(raw) % _CURSOR_BOUNDARY_RECORD_BYTES
        or len(raw) // _CURSOR_BOUNDARY_RECORD_BYTES > _MAX_CURSOR_BOUNDARIES
    ):
        raise MessageCursorError("cursor boundaries are invalid")
    boundaries = []
    seen_ordinals = set()
    for offset in range(0, len(raw), _CURSOR_BOUNDARY_RECORD_BYTES):
        ordinal, timestamp_tag, inclusive = struct.unpack_from(">HBB", raw, offset)
        if (
            ordinal >= len(member_ids)
            or ordinal in seen_ordinals
            or timestamp_tag not in {0, 1, 2}
            or inclusive not in {0, 1}
        ):
            raise MessageCursorError("cursor boundaries are invalid")
        seen_ordinals.add(ordinal)
        if timestamp_tag == 0:
            if raw[offset + 4 : offset + 12] != b"\x00" * 8:
                raise MessageCursorError("cursor boundaries are invalid")
            timestamp = None
        elif timestamp_tag == 1:
            timestamp = struct.unpack_from(">q", raw, offset + 4)[0]
        else:
            timestamp = struct.unpack_from(">d", raw, offset + 4)[0]
        message_id = struct.unpack_from(">Q", raw, offset + 12)[0]
        boundaries.append(
            MessageCursorBoundary(
                member_ids[ordinal],
                timestamp,
                message_id,
                bool(inclusive),
            )
        )
    return _validated_cursor_boundaries(boundaries)


def _cursor_payload(
    claims: MessageCursorClaims,
    member_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "boundaries": _encode_cursor_boundaries(claims.boundaries, member_ids),
        "canonical_id": claims.canonical_id,
        "database_identity_digest": claims.database_identity_digest,
        "global_generation_hint": claims.global_generation_hint,
        "lineage_fingerprint": claims.lineage_fingerprint,
        "profile": claims.profile,
        "receipt_generation": claims.receipt_generation,
        "receipt_proof_digest": claims.receipt_proof_digest,
        "source_mode": claims.source_mode,
        "version": claims.version,
    }


def message_cursor_database_identity_digest(db_identity: tuple[Any, ...]) -> str:
    """Hash a local DB identity so cursor claims never expose filesystem paths."""
    encoded = json.dumps(
        list(tuple(db_identity)),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def encode_message_cursor(
    claims: MessageCursorClaims,
    *,
    signing_key: bytes | None = None,
    member_ids: tuple[str, ...] | None = None,
) -> str:
    """Encode canonical claims with an HMAC-SHA256 integrity tag."""
    normalized = _validated_cursor_claims(claims)
    key = _PROCESS_CURSOR_SIGNING_KEY if signing_key is None else signing_key
    if not isinstance(key, bytes) or len(key) < 16:
        raise MessageCursorError("cursor signing key is invalid")
    members = _validated_cursor_members(
        tuple(boundary.member_id for boundary in normalized.boundaries)
        if member_ids is None
        else member_ids
    )
    payload = json.dumps(
        _cursor_payload(normalized, members),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    token = f"{_cursor_b64encode(payload)}.{_cursor_b64encode(signature)}"
    if len(token.encode("ascii")) > MAX_MESSAGE_CURSOR_TOKEN_BYTES:
        raise MessageCursorError("cursor is too large")
    return token


def _claims_from_payload(payload: Any) -> MessageCursorClaims:
    expected_keys = {
        "boundaries",
        "canonical_id",
        "database_identity_digest",
        "global_generation_hint",
        "lineage_fingerprint",
        "profile",
        "receipt_generation",
        "receipt_proof_digest",
        "source_mode",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise MessageCursorError("cursor claims are invalid")
    return _validated_cursor_claims(
        MessageCursorClaims(
            version=payload["version"],
            profile=payload["profile"],
            canonical_id=payload["canonical_id"],
            lineage_fingerprint=payload["lineage_fingerprint"],
            source_mode=payload["source_mode"],
            database_identity_digest=payload["database_identity_digest"],
            global_generation_hint=payload["global_generation_hint"],
            receipt_generation=payload["receipt_generation"],
            receipt_proof_digest=payload["receipt_proof_digest"],
            boundaries=(),
        )
    )


def decode_message_cursor(
    token: str,
    *,
    signing_key: bytes | None = None,
    expected: MessageCursorExpected,
) -> MessageCursorClaims:
    """Verify, decode, and bind an opaque cursor to the current request state."""
    if not isinstance(token, str):
        raise MessageCursorError("cursor encoding is invalid")
    if len(token.encode("utf-8")) > MAX_MESSAGE_CURSOR_TOKEN_BYTES:
        raise MessageCursorError("cursor is too large")
    parts = token.split(".")
    if len(parts) != 2:
        raise MessageCursorError("cursor encoding is invalid")
    payload_bytes = _cursor_b64decode(parts[0])
    signature = _cursor_b64decode(parts[1])
    key = _PROCESS_CURSOR_SIGNING_KEY if signing_key is None else signing_key
    if not isinstance(key, bytes) or len(key) < 16:
        raise MessageCursorError("cursor signing key is invalid")
    expected_signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
        signature,
        expected_signature,
    ):
        raise MessageCursorError("cursor signature is invalid")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageCursorError("cursor payload is invalid") from exc
    if not isinstance(expected, MessageCursorExpected):
        raise MessageCursorError("cursor expected state is invalid")
    members = _validated_cursor_members(expected.member_ids)
    claims = _claims_from_payload(payload)
    if claims.version != MESSAGE_CURSOR_VERSION:
        raise MessageCursorError("cursor version is unsupported")
    if claims.profile != expected.profile:
        raise MessageCursorRequestMismatch("cursor profile does not match")
    for field in (
        "canonical_id",
        "lineage_fingerprint",
        "source_mode",
        "database_identity_digest",
        "receipt_generation",
        "receipt_proof_digest",
    ):
        if getattr(claims, field) != getattr(expected, field):
            raise MessageCursorStateMismatch(f"cursor {field} does not match")
    return MessageCursorClaims(
        version=claims.version,
        profile=claims.profile,
        canonical_id=claims.canonical_id,
        lineage_fingerprint=claims.lineage_fingerprint,
        source_mode=claims.source_mode,
        database_identity_digest=claims.database_identity_digest,
        global_generation_hint=claims.global_generation_hint,
        receipt_generation=claims.receipt_generation,
        receipt_proof_digest=claims.receipt_proof_digest,
        boundaries=_decode_cursor_boundaries(payload["boundaries"], members),
    )


def clear_message_paging_capability_cache() -> None:
    """Clear the process-local schema capability cache (tests and DB swaps)."""
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE.clear()


def _column_map(rows) -> dict[str, sqlite3.Row | tuple]:
    result = {}
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        result[str(name)] = row
    return result


def _column_value(row, key: str, tuple_index: int):
    return row[key] if isinstance(row, sqlite3.Row) else row[tuple_index]


def _declared_timestamp_is_orderable(declared_type: str) -> bool:
    upper = str(declared_type or "").strip().upper()
    base = upper.split("(", 1)[0].strip()
    return base in _NUMERIC_TIMESTAMP_TYPES


def _index_columns(sql: str | None) -> tuple[str, ...]:
    if not isinstance(sql, str):
        return ()
    normalized = sql.strip()
    if re.search(r"\bWHERE\b|\bCOLLATE\b", normalized, re.IGNORECASE):
        return ()
    match = _INDEX_COLUMNS_RE.search(normalized)
    if not match:
        return ()
    columns = []
    for raw in match.group(1).split(","):
        item = raw.strip()
        column_match = re.fullmatch(
            r"[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)[`\"\]]?(?:\s+ASC)?",
            item,
            re.IGNORECASE,
        )
        if not column_match:
            return ()
        columns.append(column_match.group(1).lower())
    return tuple(columns)


def _unsupported(
    schema_version: int,
    reason: str,
    *,
    has_active: bool = False,
) -> MessagePagingCapability:
    return MessagePagingCapability(
        supported=False,
        schema_version=schema_version,
        message_index=None,
        has_active=has_active,
        fallback_reason=reason,
    )


def _inspect_uncached(
    conn: sqlite3.Connection,
    schema_version: int,
) -> MessagePagingCapability:
    if not callable(getattr(conn, "blobopen", None)):
        return _unsupported(schema_version, "missing_blob_api")
    encoding_row = conn.execute("PRAGMA encoding").fetchone()
    if not encoding_row or str(encoding_row[0]).strip().upper() != "UTF-8":
        return _unsupported(schema_version, "unsupported_database_encoding")
    session_rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
    if not session_rows:
        return _unsupported(schema_version, "missing_sessions_table")
    message_rows = conn.execute("PRAGMA table_info(messages)").fetchall()
    if not message_rows:
        return _unsupported(schema_version, "missing_messages_table")

    sessions = _column_map(session_rows)
    messages = _column_map(message_rows)
    session_id = sessions.get("id")
    if session_id is None or not int(_column_value(session_id, "pk", 5) or 0):
        return _unsupported(schema_version, "missing_session_primary_key")

    required = {"id", "session_id", "role", "content", "timestamp"}
    if not required.issubset(messages):
        return _unsupported(schema_version, "missing_message_columns")
    message_id = messages["id"]
    message_id_type = str(_column_value(message_id, "type", 2) or "").upper()
    if (
        not int(_column_value(message_id, "pk", 5) or 0)
        or message_id_type.strip() != "INTEGER"
    ):
        return _unsupported(schema_version, "unstable_message_id")
    timestamp_type = str(_column_value(messages["timestamp"], "type", 2) or "")
    has_active = "active" in messages
    if not _declared_timestamp_is_orderable(timestamp_type):
        return _unsupported(
            schema_version,
            "unsupported_timestamp_affinity",
            has_active=has_active,
        )
    if has_active:
        active_type = str(_column_value(messages["active"], "type", 2) or "")
        if active_type.strip().upper() not in _INTEGER_TYPES:
            return _unsupported(
                schema_version,
                "unsupported_active_affinity",
                has_active=True,
            )

    index_rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name IN ('sessions', 'messages')"
    ).fetchall()
    parent_index = None
    message_index = None
    for row in index_rows:
        name = str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
        table = str(row["tbl_name"] if isinstance(row, sqlite3.Row) else row[1])
        sql = row["sql"] if isinstance(row, sqlite3.Row) else row[2]
        columns = _index_columns(sql)
        if table == "sessions" and columns[:1] == ("parent_session_id",):
            parent_index = name
        if table != "messages":
            continue
        if columns[:2] == ("session_id", "timestamp") and (
            len(columns) == 2
            or columns[2] == "id"
        ):
            message_index = name

    if parent_index is None:
        return _unsupported(
            schema_version,
            "missing_session_parent_index",
            has_active=has_active,
        )
    if message_index is None:
        return _unsupported(
            schema_version,
            "missing_message_index",
            has_active=has_active,
        )
    return MessagePagingCapability(
        supported=True,
        schema_version=schema_version,
        message_index=message_index,
        has_active=has_active,
        fallback_reason=None,
        message_columns=tuple(messages),
    )


def inspect_message_paging_capability(
    conn: sqlite3.Connection,
    *,
    db_identity: tuple[Any, ...],
) -> MessagePagingCapability:
    """Inspect the Agent schema without creating tables, indexes, or triggers."""
    try:
        raw_version = conn.execute("PRAGMA schema_version").fetchone()
        schema_version = int(raw_version[0])
        identity = tuple(db_identity)
        cache_key = (identity, schema_version)
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        return _unsupported(-1, "capability_inspection_failed")

    with _CAPABILITY_CACHE_LOCK:
        cached = _CAPABILITY_CACHE.get(cache_key)
        if cached is not None:
            _CAPABILITY_CACHE.move_to_end(cache_key)
            return cached

    try:
        capability = _inspect_uncached(conn, schema_version)
    except (sqlite3.Error, KeyError, TypeError, ValueError, IndexError):
        return _unsupported(schema_version, "capability_inspection_failed")

    with _CAPABILITY_CACHE_LOCK:
        cached = _CAPABILITY_CACHE.get(cache_key)
        if cached is not None:
            _CAPABILITY_CACHE.move_to_end(cache_key)
            return cached
        for stale_key in tuple(_CAPABILITY_CACHE):
            if stale_key[0] == identity and stale_key != cache_key:
                del _CAPABILITY_CACHE[stale_key]
        _CAPABILITY_CACHE[cache_key] = capability
        while len(_CAPABILITY_CACHE) > _CAPABILITY_CACHE_MAX:
            _CAPABILITY_CACHE.popitem(last=False)
    return capability


def _message_page_raw_budget(visible_limit: int) -> int:
    return max(256, min(2048, 8 * visible_limit))


def _message_page_boundary_map(
    cursor: MessageCursorClaims | tuple[MessageCursorBoundary, ...] | None,
    members: tuple[str, ...],
) -> dict[str, MessageCursorBoundary]:
    if cursor is None:
        boundaries = ()
    elif isinstance(cursor, MessageCursorClaims):
        boundaries = _validated_cursor_boundaries(cursor.boundaries)
    else:
        try:
            boundaries = _validated_cursor_boundaries(cursor)
        except MessageCursorError as exc:
            raise MessagePageValidationError(str(exc)) from exc
    allowed = set(members)
    if any(boundary.member_id not in allowed for boundary in boundaries):
        raise MessagePageValidationError("cursor boundary member is outside lineage")
    return {boundary.member_id: boundary for boundary in boundaries}


def _normalized_row_timestamp(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MessagePagingUnavailable("non_normalizable_timestamp")
    if isinstance(value, float) and not math.isfinite(value):
        raise MessagePagingUnavailable("non_normalizable_timestamp")
    return value


def _message_page_heap_key(row: sqlite3.Row, member_index: int) -> tuple:
    timestamp = _normalized_row_timestamp(row["timestamp"])
    message_id = row["id"]
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise MessagePagingUnavailable("unstable_message_id")
    if timestamp is None:
        return (1, 0.0, -message_id, member_index)
    return (0, -timestamp, -message_id, member_index)


def _decode_message_page_value(column: str, value: Any) -> Any:
    if column not in _MESSAGE_PAGE_JSON_COLUMNS or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _message_page_row_payload(
    row: sqlite3.Row,
    selected_optional: tuple[str, ...],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": row["role"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "_state_db_message_id": row["id"],
    }
    for column in selected_optional:
        value = row[column]
        if value in (None, ""):
            continue
        payload[column] = _decode_message_page_value(column, value)
    if (
        payload.get("role") == "tool"
        and payload.get("tool_name")
        and not payload.get("name")
    ):
        payload["name"] = payload["tool_name"]
    return payload


def _message_page_row_is_active(row: sqlite3.Row, *, has_active: bool) -> bool:
    if has_active:
        active = row["active"]
        if active is not None and active == 0:
            return False
    return True


def _message_page_row_is_visible(row: sqlite3.Row, *, has_active: bool) -> bool:
    if not _message_page_row_is_active(row, has_active=has_active):
        return False
    role = str(row["role"] or "").strip().lower()
    return bool(role and role != "tool")


def _tool_call_id_values_from_row(row: sqlite3.Row) -> tuple[str, ...]:
    if "tool_calls" not in row.keys():
        return ()
    raw = row["tool_calls"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ()
    if not isinstance(raw, list):
        return ()
    result = []
    for call in raw:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id") or call.get("tool_call_id")
        if call_id:
            result.append(str(call_id))
    return tuple(result)


def _tool_call_ids_from_row(row: sqlite3.Row) -> set[str]:
    return set(_tool_call_id_values_from_row(row))


def _tool_result_id_from_row(row: sqlite3.Row) -> str | None:
    if str(row["role"] or "").strip().lower() != "tool":
        return None
    if "tool_call_id" not in row.keys():
        return None
    value = row["tool_call_id"]
    return str(value) if value not in (None, "") else None


def _bounded_tool_page_payload(
    payload: dict[str, Any],
    *,
    original_bytes: int | None = None,
    content_was_truncated: bool = False,
) -> dict[str, Any]:
    content = payload.get("content")
    if content in (None, ""):
        return payload
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            text = str(content)
    known_original_bytes = (
        original_bytes
        if isinstance(original_bytes, int) and original_bytes >= 0
        else len(text.encode("utf-8"))
    )
    if (
        not content_was_truncated
        and len(text) <= _LIMITED_TOOL_CONTENT_MAX_CHARS
    ):
        return payload
    clipped = dict(payload)
    preview = text[:_LIMITED_TOOL_CONTENT_MAX_CHARS] + _LIMITED_TOOL_CONTENT_NOTICE
    if isinstance(content, str):
        clipped["content"] = preview
    elif isinstance(content, list):
        clipped["content"] = [{"type": "text", "text": preview}]
    elif isinstance(content, dict):
        clipped["content"] = {"_truncated": True, "preview": preview}
    else:
        clipped["content"] = preview
    clipped["_content_truncated"] = True
    clipped["_content_original_bytes"] = known_original_bytes
    return clipped


def _serialized_message_bytes(messages) -> int:
    return len(
        json.dumps(
            tuple(messages),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _message_row_chronological_key(row: sqlite3.Row) -> tuple:
    timestamp = _normalized_row_timestamp(row["timestamp"])
    if timestamp is None:
        return (0, 0.0, int(row["id"]))
    return (1, timestamp, int(row["id"]))


def _typed_page_fallback(
    *,
    cursor_supplied: bool,
    reason: str,
    raw_rows_examined: int,
    sql_count: int,
    closure_rows_examined: int = 0,
) -> StateDBMessagePage:
    return StateDBMessagePage(
        mode="cursor_restart_required" if cursor_supplied else "legacy_required",
        messages=(),
        before_boundaries=(),
        has_more=False,
        visible_count=0,
        raw_rows_examined=raw_rows_examined,
        serialized_bytes=2,
        sql_count=sql_count,
        query_plan_indexed=True,
        closure_rows_examined=closure_rows_examined,
        tool_pair_status="incomplete",
        fallback_reason=reason,
    )


def _quoted_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _pairing_type_alias(field: str) -> str:
    return f"_pairing_type_{field}"


def _message_pairing_projection(
    *,
    available: set[str],
    has_active: bool,
) -> tuple[str, ...]:
    selected = ["id", "session_id", "timestamp"]
    if has_active:
        selected.append("active")
    for field in _PAIRING_FIELD_CHAR_LIMITS:
        if field != "role" and field not in available:
            continue
        selected.append(
            f"typeof({_quoted_identifier(field)}) "
            f"AS {_quoted_identifier(_pairing_type_alias(field))}"
        )
    return tuple(selected)


def _message_pairing_projection_is_bounded(row: sqlite3.Row) -> bool:
    return not any(
        bool(row[name])
        for name in (
            "_role_oversized",
            "_tool_call_id_oversized",
            "_tool_calls_oversized",
        )
        if name in row.keys()
    )


def _message_payload_type_query(
    *,
    selected_optional: tuple[str, ...],
    row_ids: tuple[int, ...],
) -> tuple[
    tuple[tuple[str, str], ...],
    str,
    tuple[int, ...],
]:
    row_placeholders = ", ".join("?" for _ in row_ids)
    fields = ("content", *selected_optional)
    type_aliases = tuple(
        (field, f"_payload_type_{index}")
        for index, field in enumerate(fields)
    )
    type_columns = ["id"]
    type_columns.extend(
        f"typeof({_quoted_identifier(field)}) AS {alias}"
        for field, alias in type_aliases
    )
    statement = (
        f"SELECT {', '.join(type_columns)} FROM messages "
        f"WHERE id IN ({row_placeholders})"
    )
    return type_aliases, statement, row_ids


def _message_blob_size(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    field: str,
    value_type: str,
) -> int:
    if value_type == "null":
        return 0
    if value_type != "text":
        raise MessagePagingUnavailable("unsupported_payload_type")
    try:
        with conn.blobopen("messages", field, message_id, readonly=True) as blob:
            return len(blob)
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MessagePagingUnavailable("payload_blob_failed") from exc


def _read_message_text_blob(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    field: str,
    value_type: str,
    byte_limit: int | None = None,
) -> tuple[str | None, int, bool]:
    if value_type == "null":
        return None, 0, False
    size = _message_blob_size(
        conn,
        message_id=message_id,
        field=field,
        value_type=value_type,
    )
    read_size = size if byte_limit is None else min(size, byte_limit)
    try:
        with conn.blobopen("messages", field, message_id, readonly=True) as blob:
            raw = blob.read(read_size)
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MessagePagingUnavailable("payload_blob_failed") from exc
    if len(raw) != read_size:
        raise MessagePagingUnavailable("payload_blob_failed")
    truncated = read_size < size
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not truncated or exc.end != len(raw):
            raise MessagePagingUnavailable("invalid_payload_encoding") from exc
        value = raw[: exc.start].decode("utf-8")
    return value, size, truncated


def _hydrate_message_pairing_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    available: set[str],
) -> dict[str, Any]:
    """Read only bounded pairing metadata for one already-indexed row."""
    hydrated = dict(row)
    message_id = int(row["id"])
    for field, max_chars in _PAIRING_FIELD_CHAR_LIMITS.items():
        if field != "role" and field not in available:
            continue
        alias = _pairing_type_alias(field)
        value_type = str(row[alias] or "").strip().lower()
        value, _size, truncated = _read_message_text_blob(
            conn,
            message_id=message_id,
            field=field,
            value_type=value_type,
            byte_limit=(max_chars + 1) * 4,
        )
        oversized = truncated or (
            isinstance(value, str) and len(value) > max_chars
        )
        hydrated[field] = None if oversized else value
        hydrated[f"_{field}_oversized"] = oversized
    return hydrated


def _message_page_query(
    *,
    selected: tuple[str, ...],
    quoted_index: str,
    member_id: str,
    boundary: MessageCursorBoundary | None,
    raw_budget: int,
) -> tuple[str, tuple[Any, ...]]:
    columns = ", ".join(selected)
    if boundary is None:
        return (
            f"SELECT {columns} FROM messages INDEXED BY {quoted_index} "
            "WHERE session_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (member_id, raw_budget),
        )
    comparator = "<=" if boundary.inclusive else "<"
    if boundary.timestamp is None:
        return (
            f"SELECT {columns} FROM messages INDEXED BY {quoted_index} "
            f"WHERE session_id = ? AND timestamp IS NULL AND id {comparator} ? "
            "ORDER BY id DESC LIMIT ?",
            (member_id, boundary.message_id, raw_budget),
        )
    # Split equal-timestamp, lower-timestamp, and NULL ranges into one compound
    # statement. Each arm gets an index range seek; a single OR predicate makes
    # SQLite walk every newer row for the session before applying the boundary.
    statement = (
        f"SELECT {columns} FROM messages INDEXED BY {quoted_index} "
        f"WHERE session_id = ? AND timestamp = ? AND id {comparator} ? "
        "UNION ALL "
        f"SELECT {columns} FROM messages INDEXED BY {quoted_index} "
        "WHERE session_id = ? AND timestamp < ? "
        "UNION ALL "
        f"SELECT {columns} FROM messages INDEXED BY {quoted_index} "
        "WHERE session_id = ? AND timestamp IS NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT ?"
    )
    return (
        statement,
        (
            member_id,
            boundary.timestamp,
            boundary.message_id,
            member_id,
            boundary.timestamp,
            member_id,
            raw_budget,
        ),
    )


def _message_page_plan_is_indexed(
    conn: sqlite3.Connection,
    statement: str,
    params: tuple[Any, ...],
    index_name: str,
    boundary: MessageCursorBoundary | None,
) -> bool:
    details = [
        str(row["detail"] if isinstance(row, sqlite3.Row) else row[3]).upper()
        for row in conn.execute(f"EXPLAIN QUERY PLAN {statement}", params).fetchall()
    ]
    if any("USE TEMP B-TREE" in detail for detail in details):
        return False
    expected_index = str(index_name).upper()
    message_steps = [detail for detail in details if " MESSAGES " in f" {detail} "]
    if not message_steps or not all(
        "SEARCH " in detail
        and expected_index in detail
        and "SCAN MESSAGES" not in detail
        for detail in message_steps
    ):
        return False
    if boundary is None:
        return all("SESSION_ID=?" in detail for detail in message_steps)
    if boundary.timestamp is None:
        return len(message_steps) == 1 and all(
            "SESSION_ID=?" in detail
            and "TIMESTAMP=?" in detail
            and ("ROWID<?" in detail or "ID<?" in detail)
            for detail in message_steps
        )
    equal_range = [
        detail
        for detail in message_steps
        if "SESSION_ID=?" in detail
        and "TIMESTAMP=?" in detail
        and ("ROWID<?" in detail or "ID<?" in detail)
    ]
    lower_range = [
        detail
        for detail in message_steps
        if "SESSION_ID=?" in detail and "TIMESTAMP<?" in detail
    ]
    null_range = [
        detail
        for detail in message_steps
        if "SESSION_ID=?" in detail
        and "TIMESTAMP=?" in detail
        and "ROWID<?" not in detail
        and "ID<?" not in detail
    ]
    return (
        len(message_steps) == 3
        and len(equal_range) == 1
        and len(lower_range) == 1
        and len(null_range) == 1
    )


def read_state_db_message_page(
    *,
    db_path: Path,
    resolution: Any,
    visible_limit: int,
    cursor: MessageCursorClaims | tuple[MessageCursorBoundary, ...] | None,
) -> StateDBMessagePage:
    """Read a newest-first bounded raw merge and return chronological messages."""
    if (
        isinstance(visible_limit, bool)
        or not isinstance(visible_limit, int)
        or not 1 <= visible_limit <= 100
    ):
        raise MessagePageValidationError("visible_limit must be an integer from 1 to 100")
    members = tuple(str(member).strip() for member in resolution.member_ids)
    if not members or any(not member or "\x00" in member for member in members):
        raise MessagePageValidationError("resolution member_ids are invalid")
    if len(set(members)) != len(members):
        raise MessagePageValidationError("resolution member_ids contain duplicates")

    raw_budget = _message_page_raw_budget(visible_limit)
    if len(members) > raw_budget:
        raise MessagePagingUnavailable("lineage_exceeds_raw_budget")
    prior_boundaries = _message_page_boundary_map(cursor, members)
    path = Path(db_path)
    if not path.is_file():
        raise MessagePagingUnavailable("missing_database")
    expected_database_identity = tuple(resolution.database_identity)
    if shared_state_db_identity(path) != expected_database_identity:
        raise MessagePagingUnavailable("database_identity_changed")

    statements: list[str] = []
    try:
        with closing(open_state_db_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            capability = inspect_message_paging_capability(
                conn,
                db_identity=tuple(resolution.database_identity),
            )
            if not capability.supported or not capability.message_index:
                raise MessagePagingUnavailable(
                    capability.fallback_reason or "unsupported_schema"
                )
            available = set(capability.message_columns)
            selected_optional = tuple(
                column for column in _MESSAGE_PAGE_OPTIONAL_COLUMNS if column in available
            )
            selected = _message_pairing_projection(
                available=available,
                has_active=capability.has_active,
            )
            quoted_index = _quoted_identifier(capability.message_index)
            query_specs = [
                (
                    *_message_page_query(
                    selected=selected,
                        quoted_index=quoted_index,
                        member_id=member_id,
                        boundary=prior_boundaries.get(member_id),
                        raw_budget=(
                            raw_budget + _TOOL_CLOSURE_RAW_ROWS_MAX
                        ),
                        ),
                    prior_boundaries.get(member_id),
                )
                for member_id in members
            ]
            conn.set_trace_callback(statements.append)
            conn.execute("BEGIN")
            # Query shapes are validated exhaustively in the physical-plan
            # contract tests. At runtime INDEXED BY and the schema-versioned
            # capability gate make a missing/incompatible index fail closed;
            # repeating EXPLAIN here would add unreported SQL to every page.
            query_plan_indexed = True

            cursors: list[sqlite3.Cursor] = []
            exhausted = [False] * len(members)
            needs_probe = [False] * len(members)
            consumed = dict(prior_boundaries)
            heap: list[tuple[tuple, int, sqlite3.Row]] = []
            raw_rows_examined = 0
            consumed_rows: list[sqlite3.Row] = []

            for member_index, member_id in enumerate(members):
                statement, params, _boundary = query_specs[member_index]
                message_cursor = conn.execute(statement, params)
                cursors.append(message_cursor)
                if raw_rows_examined >= raw_budget:
                    continue
                raw_row = message_cursor.fetchone()
                if raw_row is None:
                    exhausted[member_index] = True
                    continue
                row = _hydrate_message_pairing_row(
                    conn,
                    raw_row,
                    available=available,
                )
                raw_rows_examined += 1
                consumed[member_id] = MessageCursorBoundary(
                    member_id=member_id,
                    timestamp=_normalized_row_timestamp(row["timestamp"]),
                    message_id=int(row["id"]),
                    inclusive=True,
                )
                heapq.heappush(
                    heap,
                    (_message_page_heap_key(row, member_index), member_index, row),
                )

            selected_rows: list[sqlite3.Row] = []
            selected_boundary_snapshots: list[dict[str, MessageCursorBoundary]] = []
            while heap and len(selected_rows) < visible_limit:
                _key, member_index, row = heapq.heappop(heap)
                boundary_before_row = dict(consumed)
                consumed_rows.append(row)
                member_id = members[member_index]
                timestamp = _normalized_row_timestamp(row["timestamp"])
                consumed[member_id] = MessageCursorBoundary(
                    member_id=member_id,
                    timestamp=timestamp,
                    message_id=int(row["id"]),
                    inclusive=False,
                )
                needs_probe[member_index] = True
                if _message_page_row_is_visible(
                    row,
                    has_active=capability.has_active,
                ):
                    selected_rows.append(row)
                    selected_boundary_snapshots.append(boundary_before_row)
                if raw_rows_examined >= raw_budget:
                    continue
                if len(selected_rows) >= visible_limit and heap:
                    continue
                raw_next_row = cursors[member_index].fetchone()
                needs_probe[member_index] = False
                if raw_next_row is None:
                    exhausted[member_index] = True
                    continue
                next_row = _hydrate_message_pairing_row(
                    conn,
                    raw_next_row,
                    available=available,
                )
                raw_rows_examined += 1
                consumed[member_id] = MessageCursorBoundary(
                    member_id=member_id,
                    timestamp=_normalized_row_timestamp(next_row["timestamp"]),
                    message_id=int(next_row["id"]),
                    inclusive=True,
                )
                heapq.heappush(
                    heap,
                    (
                        _message_page_heap_key(next_row, member_index),
                        member_index,
                        next_row,
                    ),
                )

            if any(
                not _message_pairing_projection_is_bounded(row)
                for row in consumed_rows
            ):
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="pairing_metadata_budget",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                )

            tool_call_counts: dict[str, int] = {}
            tool_result_counts: dict[str, int] = {}
            result_rows_by_id: dict[str, sqlite3.Row] = {}

            def _record_tool_multiplicity(row: sqlite3.Row) -> bool:
                if not _message_page_row_is_active(
                    row,
                    has_active=capability.has_active,
                ):
                    return False
                ambiguous = False
                for call_id in _tool_call_id_values_from_row(row):
                    tool_call_counts[call_id] = tool_call_counts.get(call_id, 0) + 1
                    ambiguous = ambiguous or tool_call_counts[call_id] > 1
                result_id = _tool_result_id_from_row(row)
                if result_id:
                    tool_result_counts[result_id] = (
                        tool_result_counts.get(result_id, 0) + 1
                    )
                    ambiguous = ambiguous or tool_result_counts[result_id] > 1
                    result_rows_by_id.setdefault(result_id, row)
                return ambiguous

            if any(_record_tool_multiplicity(row) for row in consumed_rows):
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="ambiguous_tool_multiplicity",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                )
            known_call_ids = set(tool_call_counts)
            # An active result already crossed by the visible-window scan is
            # part of the pair-closure obligation even when its call is just
            # outside the visible limit. Selected call rows impose the inverse
            # obligation. Closure walks older rows until both sides are proven.
            required_pair_ids = known_call_ids | set(result_rows_by_id)
            closure_visible_rows: list[sqlite3.Row] = []
            closure_rows_examined = 0
            closure_fetches = 0

            def _missing_pair_ids():
                return (
                    required_pair_ids - known_call_ids,
                    required_pair_ids - set(result_rows_by_id),
                )

            missing_call_ids, missing_result_ids = _missing_pair_ids()
            pair_scan_pending = bool(required_pair_ids)
            closure_stop_boundaries: dict[str, MessageCursorBoundary] | None = None
            while (
                (missing_call_ids or missing_result_ids or pair_scan_pending)
                and closure_rows_examined < _TOOL_CLOSURE_RAW_ROWS_MAX
            ):
                for member_index, needs_row in enumerate(tuple(needs_probe)):
                    if not needs_row:
                        continue
                    if closure_fetches >= _TOOL_CLOSURE_RAW_ROWS_MAX:
                        break
                    raw_next_row = cursors[member_index].fetchone()
                    needs_probe[member_index] = False
                    if raw_next_row is None:
                        exhausted[member_index] = True
                        continue
                    next_row = _hydrate_message_pairing_row(
                        conn,
                        raw_next_row,
                        available=available,
                    )
                    closure_fetches += 1
                    raw_rows_examined += 1
                    consumed[members[member_index]] = MessageCursorBoundary(
                        member_id=members[member_index],
                        timestamp=_normalized_row_timestamp(next_row["timestamp"]),
                        message_id=int(next_row["id"]),
                        inclusive=True,
                    )
                    heapq.heappush(
                        heap,
                        (
                            _message_page_heap_key(next_row, member_index),
                            member_index,
                            next_row,
                        ),
                    )
                # Popping another member while one member lacks its next head
                # would violate the global newest-first merge order.
                if any(needs_probe):
                    break
                if not heap:
                    pair_scan_pending = False
                    break
                _key, member_index, row = heapq.heappop(heap)
                closure_rows_examined += 1
                member_id = members[member_index]
                if not _message_pairing_projection_is_bounded(row):
                    return _typed_page_fallback(
                        cursor_supplied=cursor is not None,
                        reason="pairing_metadata_budget",
                        raw_rows_examined=raw_rows_examined,
                        sql_count=len(statements),
                        closure_rows_examined=closure_rows_examined,
                    )
                if _record_tool_multiplicity(row):
                    return _typed_page_fallback(
                        cursor_supplied=cursor is not None,
                        reason="ambiguous_tool_multiplicity",
                        raw_rows_examined=raw_rows_examined,
                        sql_count=len(statements),
                        closure_rows_examined=closure_rows_examined,
                    )
                row_is_active = _message_page_row_is_active(
                    row,
                    has_active=capability.has_active,
                )
                row_is_visible = _message_page_row_is_visible(
                    row,
                    has_active=capability.has_active,
                )
                # Once all current pair obligations are satisfied, inspect
                # adjacent hidden tool rows for duplicate partners. The next
                # visible row is a bounded sentinel belonging to the next page;
                # leave its boundary inclusive so it is never dropped.
                if not missing_call_ids and not missing_result_ids and row_is_visible:
                    closure_stop_boundaries = dict(consumed)
                    pair_scan_pending = False
                    break
                consumed_rows.append(row)
                consumed[member_id] = MessageCursorBoundary(
                    member_id=member_id,
                    timestamp=_normalized_row_timestamp(row["timestamp"]),
                    message_id=int(row["id"]),
                    inclusive=False,
                )
                needs_probe[member_index] = True
                if row_is_active:
                    if row_is_visible:
                        closure_visible_rows.append(row)
                        row_call_ids = _tool_call_ids_from_row(row)
                        known_call_ids.update(row_call_ids)
                        required_pair_ids.update(row_call_ids)
                    result_id = _tool_result_id_from_row(row)
                    if result_id:
                        result_rows_by_id[result_id] = row
                        required_pair_ids.add(result_id)
                missing_call_ids, missing_result_ids = _missing_pair_ids()

            if missing_call_ids or missing_result_ids:
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="tool_pair_outside_closure",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                    closure_rows_examined=closure_rows_examined,
                )

            visible_rows = selected_rows + closure_visible_rows
            call_ids = set().union(
                *(_tool_call_ids_from_row(row) for row in visible_rows),
            ) if visible_rows else set()
            tool_rows = [
                result_rows_by_id[call_id]
                for call_id in sorted(call_ids)
                if call_id in result_rows_by_id
            ]
            metadata_rows = {
                int(row["id"]): row
                for row in visible_rows + tool_rows
            }
            type_aliases: tuple[tuple[str, str], ...] = ()
            payload_types_by_id: dict[int, sqlite3.Row] = {}
            payload_sizes_by_id: dict[int, dict[str, int]] = {}
            if metadata_rows:
                row_ids = tuple(sorted(metadata_rows))
                type_aliases, type_sql, type_params = _message_payload_type_query(
                    selected_optional=selected_optional,
                    row_ids=row_ids,
                )
                type_rows = conn.execute(type_sql, type_params).fetchall()
                payload_types_by_id = {
                    int(row["id"]): row
                    for row in type_rows
                }
                if set(payload_types_by_id) != set(row_ids):
                    raise MessagePagingUnavailable("payload_rows_changed")
                for message_id, type_row in payload_types_by_id.items():
                    field_sizes = {}
                    for field, alias in type_aliases:
                        value_type = str(type_row[alias] or "").strip().lower()
                        field_sizes[field] = _message_blob_size(
                            conn,
                            message_id=message_id,
                            field=field,
                            value_type=value_type,
                        )
                    payload_sizes_by_id[message_id] = field_sizes

            def _materialize_payload(
                metadata_row: sqlite3.Row,
                *,
                limit_tool_content: bool = False,
            ) -> dict[str, Any]:
                message_id = int(metadata_row["id"])
                type_row = payload_types_by_id[message_id]
                values: dict[str, Any] = {
                    "id": message_id,
                    "role": metadata_row["role"],
                    "timestamp": metadata_row["timestamp"],
                }
                content_truncated = False
                content_original_bytes = 0
                for field, alias in type_aliases:
                    value_type = str(type_row[alias] or "").strip().lower()
                    byte_limit = (
                        _TOOL_CONTENT_PROJECTION_BYTES
                        if limit_tool_content and field == "content"
                        else None
                    )
                    value, size, truncated = _read_message_text_blob(
                        conn,
                        message_id=message_id,
                        field=field,
                        value_type=value_type,
                        byte_limit=byte_limit,
                    )
                    if limit_tool_content and field == "content" and value is not None:
                        content_original_bytes = size
                        content_truncated = truncated or (
                            len(value) > _TOOL_CONTENT_PROJECTION_CHARS
                        )
                        value = value[:_TOOL_CONTENT_PROJECTION_CHARS]
                    values[field] = value
                payload = _message_page_row_payload(values, selected_optional)
                if limit_tool_content:
                    payload = _bounded_tool_page_payload(
                        payload,
                        original_bytes=content_original_bytes,
                        content_was_truncated=content_truncated,
                    )
                return payload

            def _materialize_bounded_ordinary_payload(
                metadata_row: sqlite3.Row,
            ) -> dict[str, Any]:
                message_id = int(metadata_row["id"])
                type_row = payload_types_by_id[message_id]
                field_sizes = payload_sizes_by_id[message_id]
                aliases = dict(type_aliases)
                content_alias = aliases["content"]
                content_type = str(type_row[content_alias] or "").strip().lower()
                content, content_size, content_was_truncated = (
                    _read_message_text_blob(
                        conn,
                        message_id=message_id,
                        field="content",
                        value_type=content_type,
                        byte_limit=_ORDINARY_CONTENT_PROJECTION_BYTES,
                    )
                )
                content_is_truncated = content_was_truncated or (
                    isinstance(content, str)
                    and len(content) > _LIMITED_ORDINARY_CONTENT_MAX_CHARS
                )
                if content_is_truncated and isinstance(content, str):
                    content = (
                        content[:_LIMITED_ORDINARY_CONTENT_MAX_CHARS]
                        + _LIMITED_ORDINARY_CONTENT_NOTICE
                    )
                payload: dict[str, Any] = {
                    "role": metadata_row["role"],
                    "content": content,
                    "timestamp": metadata_row["timestamp"],
                    "_state_db_message_id": message_id,
                    "_payload_complete": False,
                    "_payload_original_bytes": sum(field_sizes.values()),
                    "_payload_truncation_reason": "cursor_page_budget",
                }
                if content_is_truncated:
                    payload.update(
                        {
                            "_content_truncated": True,
                            "_content_complete": False,
                            "_content_original_bytes": content_size,
                            "_content_truncation_reason": "cursor_page_budget",
                        }
                    )
                else:
                    payload["_content_complete"] = True

                optional_budget = _ORDINARY_OPTIONAL_PROJECTION_BYTES
                omitted_fields = []
                for field, alias in type_aliases:
                    if field == "content" or not field_sizes[field]:
                        continue
                    size = field_sizes[field]
                    if size > optional_budget:
                        omitted_fields.append(field)
                        continue
                    value_type = str(type_row[alias] or "").strip().lower()
                    value, _size, truncated = _read_message_text_blob(
                        conn,
                        message_id=message_id,
                        field=field,
                        value_type=value_type,
                        byte_limit=optional_budget,
                    )
                    if truncated:
                        omitted_fields.append(field)
                        continue
                    optional_budget -= size
                    if value not in (None, ""):
                        payload[field] = _decode_message_page_value(field, value)
                if omitted_fields:
                    payload["_payload_fields_omitted"] = tuple(omitted_fields)
                if (
                    payload.get("role") == "tool"
                    and payload.get("tool_name")
                    and not payload.get("name")
                ):
                    payload["name"] = payload["tool_name"]
                if _serialized_message_bytes((payload,)) > _ORDINARY_MESSAGE_BYTES_MAX:
                    raise MessagePagingUnavailable("bounded_payload_budget")
                return payload

            has_tool_context = bool(call_ids or tool_rows or closure_visible_rows)
            ordinary_payloads: list[dict[str, Any]] = []
            accepted_selected_rows: list[sqlite3.Row] = []
            ordinary_raw_bytes = 0
            short_page_boundaries: dict[str, MessageCursorBoundary] | None = None
            for row_index, row in enumerate(selected_rows):
                message_id = int(row["id"])
                row_raw_bytes = sum(payload_sizes_by_id[message_id].values())
                if row_raw_bytes > _ORDINARY_MESSAGE_BYTES_MAX:
                    bounded_payload = _materialize_bounded_ordinary_payload(row)
                    candidate_payloads = ordinary_payloads + [bounded_payload]
                    if (
                        _serialized_message_bytes(candidate_payloads)
                        > _ORDINARY_MESSAGE_BYTES_MAX
                    ):
                        if not ordinary_payloads or has_tool_context:
                            return _typed_page_fallback(
                                cursor_supplied=cursor is not None,
                                reason="ordinary_payload_budget",
                                raw_rows_examined=raw_rows_examined,
                                sql_count=len(statements),
                                closure_rows_examined=closure_rows_examined,
                            )
                        short_page_boundaries = selected_boundary_snapshots[row_index]
                        break
                    ordinary_payloads.append(bounded_payload)
                    accepted_selected_rows.append(row)
                    ordinary_raw_bytes += _serialized_message_bytes(
                        (bounded_payload,)
                    )
                    continue
                if ordinary_raw_bytes + row_raw_bytes > _ORDINARY_MESSAGE_BYTES_MAX:
                    if not ordinary_payloads or has_tool_context:
                        return _typed_page_fallback(
                            cursor_supplied=cursor is not None,
                            reason="ordinary_payload_budget",
                            raw_rows_examined=raw_rows_examined,
                            sql_count=len(statements),
                            closure_rows_examined=closure_rows_examined,
                        )
                    short_page_boundaries = selected_boundary_snapshots[row_index]
                    break
                payload = _materialize_payload(row)
                candidate_payloads = ordinary_payloads + [payload]
                if (
                    _serialized_message_bytes(candidate_payloads)
                    > _ORDINARY_MESSAGE_BYTES_MAX
                ):
                    if (
                        _serialized_message_bytes((payload,))
                        > _ORDINARY_MESSAGE_BYTES_MAX
                    ):
                        bounded_payload = _materialize_bounded_ordinary_payload(row)
                        bounded_candidates = ordinary_payloads + [bounded_payload]
                        if (
                            _serialized_message_bytes(bounded_candidates)
                            <= _ORDINARY_MESSAGE_BYTES_MAX
                        ):
                            ordinary_payloads.append(bounded_payload)
                            accepted_selected_rows.append(row)
                            ordinary_raw_bytes += _serialized_message_bytes(
                                (bounded_payload,)
                            )
                            continue
                    if not ordinary_payloads or has_tool_context:
                        return _typed_page_fallback(
                            cursor_supplied=cursor is not None,
                            reason="ordinary_payload_budget",
                            raw_rows_examined=raw_rows_examined,
                            sql_count=len(statements),
                            closure_rows_examined=closure_rows_examined,
                        )
                    short_page_boundaries = selected_boundary_snapshots[row_index]
                    break
                ordinary_payloads.append(payload)
                accepted_selected_rows.append(row)
                ordinary_raw_bytes += row_raw_bytes

            if short_page_boundaries is not None:
                selected_rows = accepted_selected_rows
                closure_visible_rows = []
                tool_rows = []
                call_ids = set()
                visible_rows = list(selected_rows)
                metadata_rows = {
                    int(row["id"]): row
                    for row in selected_rows
                }

            closure_source_rows = closure_visible_rows + tool_rows
            closure_raw_bytes = 0
            for row in closure_source_rows:
                message_id = int(row["id"])
                role = str(row["role"] or "").strip().lower()
                for field, size in payload_sizes_by_id[message_id].items():
                    if role == "tool" and field == "content":
                        size = min(size, _TOOL_CONTENT_PROJECTION_BYTES)
                    closure_raw_bytes += size
            if closure_raw_bytes > _TOOL_CLOSURE_BYTES_MAX:
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="tool_closure_payload_budget",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                    closure_rows_examined=closure_rows_examined,
                )
            closure_payloads = [
                _materialize_payload(row)
                for row in closure_visible_rows
            ] + [
                _materialize_payload(row, limit_tool_content=True)
                for row in tool_rows
            ]
            ordinary_serialized_bytes = _serialized_message_bytes(ordinary_payloads)
            closure_serialized_bytes = _serialized_message_bytes(closure_payloads)
            if closure_serialized_bytes > _TOOL_CLOSURE_BYTES_MAX:
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="tool_closure_payload_budget",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                    closure_rows_examined=closure_rows_examined,
                )

            has_more = (
                True
                if (
                    short_page_boundaries is not None
                    or closure_stop_boundaries is not None
                )
                else bool(heap) or any(needs_probe) or (
                    raw_rows_examined >= raw_budget and not all(exhausted)
                )
            )
            payload_by_id = {
                int(payload["_state_db_message_id"]): payload
                for payload in ordinary_payloads + closure_payloads
            }
            payload_rows = metadata_rows
            chronological = tuple(
                payload_by_id[message_id]
                for message_id in sorted(
                    payload_by_id,
                    key=lambda current_id: _message_row_chronological_key(
                        payload_rows[current_id]
                    ),
                )
            )
            boundary_source = (
                short_page_boundaries
                if short_page_boundaries is not None
                else closure_stop_boundaries
                if closure_stop_boundaries is not None
                else consumed
            )
            before_boundaries = tuple(
                boundary_source[member]
                for member in members
                if member in boundary_source
            )
            serialized_bytes = _serialized_message_bytes(chronological)
            if serialized_bytes > _COMBINED_MESSAGE_BYTES_MAX:
                return _typed_page_fallback(
                    cursor_supplied=cursor is not None,
                    reason="combined_payload_budget",
                    raw_rows_examined=raw_rows_examined,
                    sql_count=len(statements),
                    closure_rows_examined=closure_rows_examined,
                )
            result_ids = {
                result_id
                for row in tool_rows
                if (result_id := _tool_result_id_from_row(row))
            }
            tool_pair_status = (
                "none"
                if not call_ids
                else "complete"
                if call_ids.issubset(result_ids)
                else "partial"
            )
            if shared_state_db_identity(path) != expected_database_identity:
                raise MessagePagingUnavailable("database_identity_changed")
            return StateDBMessagePage(
                mode="cursor_v1",
                messages=chronological,
                before_boundaries=before_boundaries,
                has_more=has_more,
                visible_count=len(visible_rows),
                raw_rows_examined=raw_rows_examined,
                serialized_bytes=serialized_bytes,
                sql_count=len(statements),
                query_plan_indexed=query_plan_indexed,
                ordinary_serialized_bytes=ordinary_serialized_bytes,
                closure_serialized_bytes=closure_serialized_bytes,
                closure_rows_examined=closure_rows_examined,
                tool_pair_status=tool_pair_status,
            )
    except (MessagePageValidationError, MessagePagingUnavailable):
        raise
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, IndexError) as exc:
        raise MessagePagingUnavailable("read_failed") from exc


def _shadow_comparable_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    comparable = dict(message)
    comparable.pop("_state_db_message_id", None)
    comparable.pop("_content_original_chars", None)
    comparable.pop("_content_original_bytes", None)
    return comparable


def evaluate_message_page_shadow(
    *,
    db_path: Path,
    resolution: Any,
    visible_limit: int,
    legacy_messages: Any,
    on_exact_match: MessagePageShadowExactMatchConsumer | None = None,
) -> MessagePageShadowObservation:
    """Compare a complete bounded page sequence with the exact legacy merge.

    The returned observation intentionally contains only booleans, counters,
    the typed mode/reason, and the plan verdict. Transcript content and local
    paths never leave this function through diagnostics. ``on_exact_match`` is
    an in-process observational seam; it receives normalized candidate and
    oracle tuples plus their append-only display counts only after equality.
    """
    legacy = tuple(
        _shadow_comparable_message(message)
        for message in list(legacy_messages or [])
    )
    if len(legacy) > _SHADOW_MAX_MESSAGES:
        return MessagePageShadowObservation(
            mode="legacy_required",
            matched=None,
            fallback_reason="shadow_oracle_limit",
            visible_count=0,
            raw_rows_examined=0,
            serialized_bytes=0,
            sql_count=0,
            query_plan_indexed=True,
        )
    cursor: tuple[MessageCursorBoundary, ...] | None = None
    seen_boundaries = set()
    page_batches: list[tuple[Any, ...]] = []
    total_visible = 0
    total_raw_rows = 0
    total_serialized_bytes = 0
    total_sql = 0
    all_plans_indexed = True
    for _page_index in range(_SHADOW_MAX_PAGES):
        try:
            page = read_state_db_message_page(
                db_path=db_path,
                resolution=resolution,
                visible_limit=visible_limit,
                cursor=cursor,
            )
        except MessagePagingUnavailable as exc:
            return MessagePageShadowObservation(
                mode="legacy_required" if cursor is None else "cursor_restart_required",
                matched=None,
                fallback_reason=exc.reason,
                visible_count=total_visible,
                raw_rows_examined=total_raw_rows,
                serialized_bytes=total_serialized_bytes,
                sql_count=total_sql,
                query_plan_indexed=False,
            )
        total_visible += page.visible_count
        total_raw_rows += page.raw_rows_examined
        total_serialized_bytes += page.serialized_bytes
        total_sql += page.sql_count
        all_plans_indexed = all_plans_indexed and page.query_plan_indexed
        if page.mode != "cursor_v1":
            return MessagePageShadowObservation(
                mode=page.mode,
                matched=None,
                fallback_reason=page.fallback_reason or page.mode,
                visible_count=total_visible,
                raw_rows_examined=total_raw_rows,
                serialized_bytes=total_serialized_bytes,
                sql_count=total_sql,
                query_plan_indexed=all_plans_indexed,
            )
        page_batches.append(
            tuple(_shadow_comparable_message(message) for message in page.messages)
        )
        if not page.has_more:
            break
        boundary_key = tuple(
            (
                boundary.member_id,
                boundary.timestamp,
                boundary.message_id,
                boundary.inclusive,
            )
            for boundary in page.before_boundaries
        )
        if not boundary_key or boundary_key in seen_boundaries:
            return MessagePageShadowObservation(
                mode="cursor_restart_required",
                matched=None,
                fallback_reason="shadow_cursor_stalled",
                visible_count=total_visible,
                raw_rows_examined=total_raw_rows,
                serialized_bytes=total_serialized_bytes,
                sql_count=total_sql,
                query_plan_indexed=all_plans_indexed,
            )
        seen_boundaries.add(boundary_key)
        cursor = page.before_boundaries
    else:
        return MessagePageShadowObservation(
            mode="cursor_restart_required",
            matched=None,
            fallback_reason="shadow_oracle_limit",
            visible_count=total_visible,
            raw_rows_examined=total_raw_rows,
            serialized_bytes=total_serialized_bytes,
            sql_count=total_sql,
            query_plan_indexed=all_plans_indexed,
        )
    bounded = tuple(
        message
        for batch in reversed(page_batches)
        for message in batch
    )
    matched = bounded == legacy
    if matched and on_exact_match is not None:
        try:
            on_exact_match(bounded, legacy, len(bounded), len(legacy))
        except Exception:
            # Consumers are observational and must not affect the legacy path.
            pass
    return MessagePageShadowObservation(
        mode="cursor_v1",
        matched=matched,
        fallback_reason=None if matched else "semantic_mismatch",
        visible_count=total_visible,
        raw_rows_examined=total_raw_rows,
        serialized_bytes=total_serialized_bytes,
        sql_count=total_sql,
        query_plan_indexed=all_plans_indexed,
    )
