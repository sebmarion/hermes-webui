"""Proof-first, dependency-injected bounded initial conversation views.

The assembler owns source selection only.  Existing route helpers own exact
legacy loading, bounded page reads, and redaction.  A request can therefore
return either one complete cursor page or the unchanged exact legacy view, but
never a hybrid assembled from both sources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sqlite3
from typing import Any, Callable, Mapping, Protocol

from api.conversation_receipts import (
    ConversationReceipt,
    ReceiptValidation,
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
    canonical_proof_digest,
    validate_receipt,
)
from api.bounded_runtime_overlay import VERIFIED_RUNTIME_OVERLAY_CAPABILITY
from api.session_message_paging import (
    MESSAGE_CURSOR_VERSION,
    MessageCursorBoundary,
    MessageCursorError,
    MessageCursorExpected,
    MessageCursorRequestMismatch,
    MessageCursorStateMismatch,
    MessageCursorClaims,
    decode_message_cursor,
    encode_message_cursor,
)


_CONTENT_CAPABILITY = "target_message_generation"
_CONTENT_CAPABILITY_VERSION = 1
_MAX_MEMBERS = 256
_MAX_SERIALIZED_BYTES = (2 * 1024 * 1024) + (512 * 1024)
_COMMENT_RE = re.compile(r"--|/\*|\*/")
_RUNTIME_OVERLAY_METADATA_FIELDS = frozenset(
    {
        "active_stream_id",
        "pending_attachments",
        "pending_started_at",
        "pending_user_message",
        "pending_user_source",
        "runtime_journal",
        "runtime_journal_snapshot",
    }
)
_CANONICAL_TRIGGER_DDL = {
    "proof_messages_insert": "create trigger proof_messages_insert after insert on messages begin update sessions set message_generation = message_generation + 1 where id = new.session_id; end",
    "proof_messages_delete": "create trigger proof_messages_delete after delete on messages begin update sessions set message_generation = message_generation + 1 where id = old.session_id; end",
    "proof_messages_update_same_session": "create trigger proof_messages_update_same_session after update on messages when old.session_id = new.session_id begin update sessions set message_generation = message_generation + 1 where id = new.session_id; end",
    "proof_messages_update_moved_session": "create trigger proof_messages_update_moved_session after update on messages when old.session_id != new.session_id begin update sessions set message_generation = message_generation + 1 where id = old.session_id; update sessions set message_generation = message_generation + 1 where id = new.session_id; end",
}


@dataclass(frozen=True)
class ProofCapability:
    available: bool
    reason: str
    capability_marker: object | None = None


@dataclass(frozen=True)
class ResolvedTarget:
    """Bounded Stage 1 resolution plus all cursor binding inputs."""

    requested_id: str
    canonical_id: str
    root_id: str
    member_ids: tuple[str, ...]
    lineage_fingerprint: str
    database_identity_digest: str
    global_generation_hint: int | None
    source_mode: str


@dataclass(frozen=True)
class BoundedViewRequest:
    profile: str
    requested_id: str
    limit: int
    cursor: str | None = None


@dataclass(frozen=True)
class LegacyView:
    messages: list[dict]
    message_count: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageView:
    messages: list[dict]
    has_more: bool
    visible_count: int
    raw_rows_examined: int
    serialized_bytes: int
    before_boundaries: tuple[MessageCursorBoundary, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverlayView:
    """Typed runtime result: only ``ok`` and ``no_active_owner`` are complete."""

    status: str
    messages: list[dict]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    capability_marker: object | None = None


@dataclass(frozen=True)
class LegacyPublicationResult:
    published: bool
    reason: str


@dataclass(frozen=True)
class BoundedViewResult:
    status: int
    mode: str
    messages: list[dict]
    message_count: int | None
    before_cursor: str | None
    has_more: bool = False
    raw_rows_examined: int = 0
    serialized_bytes: int = 0
    fallback_reason: str | None = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class _Resolver(Protocol):
    def __call__(self, profile: str, requested_id: str) -> ResolvedTarget: ...


@dataclass(frozen=True)
class BoundedViewDependencies:
    """All I/O and legacy semantics are injected by route integration."""

    resolve: _Resolver
    confirm_target: Callable[[ResolvedTarget], bool]
    read_current: Callable[[ResolvedTarget, object], Mapping[str, Any]]
    load_receipt: Callable[[str, str], ConversationReceipt | None]
    load_legacy: Callable[[ResolvedTarget, int], LegacyView]
    load_page: Callable[[ResolvedTarget, MessageCursorClaims | None, int], PageView]
    capability: Callable[[], ProofCapability]
    publish_legacy: Callable[[ResolvedTarget, LegacyView, str, Mapping[str, Any]], LegacyPublicationResult] | None = None
    overlay: Callable[[list[dict], ResolvedTarget], OverlayView] | None = None
    redact: Callable[[BoundedViewResult], BoundedViewResult] | None = None
    renderable: Callable[[dict], bool] = lambda _message: False


class BoundedSessionViewAssembler:
    """Validate all current proof before and after a bounded page read."""

    def __init__(
        self,
        dependencies: BoundedViewDependencies,
        *,
        cursor_secret: bytes | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._cursor_secret = cursor_secret

    def assemble(self, request: BoundedViewRequest) -> BoundedViewResult:
        if not _valid_request(request):
            return BoundedViewResult(400, "legacy", [], None, None, error="invalid_request")
        continuation = request.cursor is not None
        try:
            target = self._dependencies.resolve(request.profile, request.requested_id)
        except Exception:
            return _restart() if continuation else self._legacy(request, None, "resolution_failed")
        if not _valid_target(target):
            return _restart() if continuation else self._legacy(request, target, "resolution_failed")

        capability = _safe_capability(self._dependencies.capability)
        if not capability.available or capability.capability_marker is not VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY:
            return _restart() if continuation else self._legacy(request, target, _reason(capability))
        validation, receipt, current_before = self._validate_current(target, request.profile, None)
        if not validation.valid or receipt is None:
            return _restart() if continuation else self._legacy(
                request, target, validation.reason, current_before
            )

        claims: MessageCursorClaims | None = None
        if continuation:
            claims_or_result = self._decode_after_proof(request, target, receipt)
            if isinstance(claims_or_result, BoundedViewResult):
                return claims_or_result
            claims = claims_or_result
            validation, receipt, _ = self._validate_current(target, request.profile, claims)
            if not validation.valid or receipt is None:
                return _restart()
        try:
            page = self._dependencies.load_page(target, claims, request.limit)
            _validate_page(page, target, request.limit)
        except Exception:
            return _restart() if continuation else self._legacy(
                request, target, "message_page_unavailable", current_before
            )

        # A page is not enough proof: a concurrent target/sidecar/receipt
        # transition must never receive a cursor based on the old snapshot.
        validation, current_receipt, _ = self._validate_current(target, request.profile, claims)
        if not validation.valid or current_receipt is None or current_receipt != receipt:
            return _restart() if continuation else self._legacy(request, target, _reason(validation))
        if not self._confirm_target(target):
            return _restart() if continuation else self._legacy(request, target, "target_changed")
        try:
            result = self._cursor_result(target, receipt, page)
            result = self._apply_overlay(result, target)
        except Exception:
            return _restart() if continuation else self._legacy(request, target, "runtime_overlay_unavailable")
        validation, final_receipt, _ = self._validate_current(
            target, request.profile, claims
        )
        if (
            not validation.valid
            or final_receipt is None
            or final_receipt != receipt
        ):
            return _restart() if continuation else self._legacy(
                request, target, _reason(validation)
            )
        if not self._confirm_target(target):
            return _restart() if continuation else self._legacy(request, target, "target_changed")
        return self._finish(result)

    def _decode_after_proof(
        self,
        request: BoundedViewRequest,
        target: ResolvedTarget,
        receipt: ConversationReceipt,
    ) -> MessageCursorClaims | BoundedViewResult:
        expected = _expected_cursor(request.profile, target, receipt)
        try:
            return decode_message_cursor(
                request.cursor or "", signing_key=self._cursor_secret, expected=expected
            )
        except MessageCursorStateMismatch:
            return _restart()
        except (MessageCursorRequestMismatch, MessageCursorError):
            return BoundedViewResult(400, "cursor_v1", [], None, None, error="invalid_message_cursor")

    def _validate_current(
        self,
        target: ResolvedTarget,
        profile: str,
        claims: MessageCursorClaims | None,
    ) -> tuple[ReceiptValidation, ConversationReceipt | None, Mapping[str, Any] | None]:
        try:
            current = dict(self._dependencies.read_current(target, VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY))
            # The schema detector, not an arbitrary current snapshot string,
            # is the authority that may attach this identity marker.
            current["state_content_proof_capability"] = VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
            receipt = self._dependencies.load_receipt(profile, target.root_id)
        except Exception:
            return ReceiptValidation(False, "unverifiable_current_state"), None, None
        if receipt is None:
            return ReceiptValidation(False, "receipt_missing"), None, current
        return validate_receipt(
            receipt,
            current=current,
            cursor_epoch=None if claims is None else claims.receipt_generation,
            cursor_proof_digest=None if claims is None else claims.receipt_proof_digest,
        ), receipt, current

    def _legacy(
        self,
        request: BoundedViewRequest,
        target: ResolvedTarget | None,
        reason: str,
        current_before: Mapping[str, Any] | None = None,
    ) -> BoundedViewResult:
        if target is None:
            return BoundedViewResult(500, "legacy", [], None, None, fallback_reason=reason)
        try:
            legacy = self._dependencies.load_legacy(target, request.limit)
            _validate_legacy(legacy)
        except Exception:
            return BoundedViewResult(500, "legacy", [], None, None, fallback_reason=reason)
        if self._dependencies.publish_legacy is not None and current_before is not None:
            try:
                current_after = dict(
                    self._dependencies.read_current(
                        target, VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
                    )
                )
                current_after["state_content_proof_capability"] = (
                    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
                )
                if current_after == current_before and self._confirm_target(target):
                    published = self._dependencies.publish_legacy(
                        target, legacy, reason, current_after
                    )
                    if not isinstance(published, LegacyPublicationResult):
                        raise ValueError("legacy publication result is invalid")
            except Exception:
                pass
        result = BoundedViewResult(
            200, "legacy", list(legacy.messages), legacy.message_count, None,
            fallback_reason=reason, metadata=dict(legacy.metadata),
        )
        try:
            return self._finish(self._apply_overlay(result, target))
        except Exception:
            # Exact legacy remains safe when optional active state cannot be
            # proven; it is complete because it is the existing oracle.
            return self._finish(result)

    def _cursor_result(
        self, target: ResolvedTarget, receipt: ConversationReceipt, page: PageView
    ) -> BoundedViewResult:
        claims = MessageCursorClaims(
            version=MESSAGE_CURSOR_VERSION,
            profile=receipt.profile,
            canonical_id=target.canonical_id,
            lineage_fingerprint=target.lineage_fingerprint,
            source_mode=target.source_mode,
            database_identity_digest=target.database_identity_digest,
            global_generation_hint=target.global_generation_hint,
            receipt_generation=receipt.generation,
            receipt_proof_digest=canonical_proof_digest(
                target.lineage_fingerprint, receipt.state_content_proof
            ),
            boundaries=page.before_boundaries,
        )
        cursor = None
        if page.has_more:
            cursor = encode_message_cursor(
                claims, signing_key=self._cursor_secret, member_ids=target.member_ids
            )
        return BoundedViewResult(
            200, "cursor_v1", list(page.messages), receipt.settled_display_message_count,
            cursor, has_more=page.has_more, raw_rows_examined=page.raw_rows_examined,
            serialized_bytes=page.serialized_bytes, metadata=dict(page.metadata),
        )

    def _apply_overlay(self, result: BoundedViewResult, target: ResolvedTarget) -> BoundedViewResult:
        if self._dependencies.overlay is None:
            return result
        overlay = self._dependencies.overlay(result.messages, target)
        if not isinstance(overlay, OverlayView) or overlay.status not in {"ok", "no_active_owner"}:
            raise ValueError("runtime overlay is incomplete")
        if not _messages(overlay.messages) or type(overlay.metadata) is not dict:
            raise ValueError("runtime overlay is malformed")
        if (
            len(overlay.metadata) > len(_RUNTIME_OVERLAY_METADATA_FIELDS)
            or not set(overlay.metadata).issubset(_RUNTIME_OVERLAY_METADATA_FIELDS)
        ):
            raise ValueError("runtime overlay contains non-runtime metadata")
        if overlay.status == "no_active_owner":
            if overlay.messages != result.messages or overlay.metadata:
                raise ValueError("no-active-owner overlay changed the settled view")
            return result
        if overlay.capability_marker is not VERIFIED_RUNTIME_OVERLAY_CAPABILITY:
            raise ValueError("runtime overlay lacks final ownership proof")
        if overlay.messages[: len(result.messages)] != result.messages:
            raise ValueError("runtime overlay is not append-only")
        appended = overlay.messages[len(result.messages) :]
        try:
            live_delta = sum(
                1 for message in appended if self._dependencies.renderable(message) is True
            )
        except Exception as exc:
            raise ValueError("runtime renderability is unavailable") from exc
        return BoundedViewResult(
            **{
                **result.__dict__,
                "messages": list(overlay.messages),
                "message_count": (result.message_count or 0) + live_delta,
                "metadata": {**result.metadata, **overlay.metadata},
            }
        )

    def _confirm_target(self, target: ResolvedTarget) -> bool:
        try:
            return self._dependencies.confirm_target(target) is True
        except Exception:
            return False

    def _finish(self, result: BoundedViewResult) -> BoundedViewResult:
        if self._dependencies.redact is None:
            return result
        redacted = self._dependencies.redact(result)
        if not isinstance(redacted, BoundedViewResult):
            raise ValueError("redactor returned an invalid view")
        return redacted


def detect_proof_v1_capability(connection: sqlite3.Connection) -> ProofCapability:
    """Accept only the declared synthetic proof-v1 Agent schema contract."""
    try:
        capability_columns = _table_columns(connection, "agent_contract_capabilities")
        session_columns = _table_columns(connection, "sessions")
        marker = connection.execute(
            "SELECT version FROM agent_contract_capabilities WHERE capability = ?",
            (_CONTENT_CAPABILITY,),
        ).fetchone()
        trigger_sql = {
            row[0]: str(row[1] or "")
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'messages'"
            )
        }
    except (sqlite3.Error, AttributeError, TypeError):
        return ProofCapability(False, "unverifiable_current_state")
    required_capabilities = {
        "capability": ("TEXT", 1),
        "version": ("INTEGER", 0),
    }
    if set(capability_columns) != set(required_capabilities):
        return ProofCapability(False, "unverifiable_current_state")
    for name, (declared_type, pk) in required_capabilities.items():
        column = capability_columns[name]
        if column["type"] != declared_type or column["pk"] != pk:
            return ProofCapability(False, "unverifiable_current_state")
    version = capability_columns["version"]
    generation = session_columns.get("message_generation")
    if (
        marker != (_CONTENT_CAPABILITY_VERSION,)
        or version["notnull"] != 1
        or generation is None
        or generation["type"] != "INTEGER"
        or generation["notnull"] != 1
            or not _proof_trigger_semantics(trigger_sql)
    ):
        return ProofCapability(False, "unverifiable_current_state")
    return ProofCapability(True, "valid", VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY)


def read_target_content_proof(
    connection: sqlite3.Connection, member_ids: tuple[str, ...]
) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    """Read only the ordered resolved-member generation vector by primary key."""
    if not _valid_members(member_ids):
        return None
    placeholders = ",".join("?" for _ in member_ids)
    ordering = " ".join(f"WHEN ? THEN {index}" for index in range(len(member_ids)))
    try:
        rows = connection.execute(
            "SELECT id, message_generation FROM sessions "
            f"WHERE id IN ({placeholders}) ORDER BY CASE id {ordering} ELSE {len(member_ids)} END",
            (*member_ids, *member_ids),
        ).fetchall()
    except (sqlite3.Error, AttributeError, TypeError):
        return None
    if len(rows) != len(member_ids):
        return None
    proof = []
    for expected, row in zip(member_ids, rows, strict=True):
        if row[0] != expected or type(row[1]) is not int or row[1] < 0:
            return None
        proof.append((expected, row[1]))
    return "agent_target_content_epoch_v1", tuple(proof)


def classify_shadow_difference(
    candidate_messages: list[Mapping[str, Any]], legacy_messages: list[Mapping[str, Any]], *,
    candidate_count: int, legacy_count: int,
    candidate_truncated: bool | None = None, legacy_truncated: bool | None = None,
) -> set[str]:
    """Classify only durable, content-free shadow evidence reasons."""
    reasons: set[str] = set()
    if candidate_count != legacy_count:
        reasons.add("visible_count_difference")
    candidate_ids, legacy_ids = _message_ids(candidate_messages), _message_ids(legacy_messages)
    if set(candidate_ids) != set(legacy_ids):
        reasons.add("visible_identity_difference")
    elif candidate_ids != legacy_ids:
        reasons.add("visible_order_difference")
    if candidate_truncated is not None and legacy_truncated is not None and candidate_truncated is not legacy_truncated:
        reasons.add("truncation_difference")
    if _tool_pairs(candidate_messages) != _tool_pairs(legacy_messages):
        reasons.add("tool_pair_difference")
    return reasons


def _expected_cursor(profile: str, target: ResolvedTarget, receipt: ConversationReceipt) -> MessageCursorExpected:
    return MessageCursorExpected(
        profile=profile, canonical_id=target.canonical_id,
        lineage_fingerprint=target.lineage_fingerprint, source_mode=target.source_mode,
        database_identity_digest=target.database_identity_digest,
        global_generation_hint=target.global_generation_hint,
        receipt_generation=receipt.generation,
        receipt_proof_digest=canonical_proof_digest(target.lineage_fingerprint, receipt.state_content_proof),
        member_ids=target.member_ids,
    )


def _restart() -> BoundedViewResult:
    return BoundedViewResult(409, "cursor_v1", [], None, None, error="cursor_restart_required")


def _safe_capability(provider: Callable[[], ProofCapability]) -> ProofCapability:
    try:
        value = provider()
    except Exception:
        return ProofCapability(False, "unverifiable_current_state")
    return value if isinstance(value, ProofCapability) else ProofCapability(False, "unverifiable_current_state")


def _reason(value: Any) -> str:
    reason = getattr(value, "reason", None)
    return reason if isinstance(reason, str) and reason else "unverifiable_current_state"


def _valid_request(request: Any) -> bool:
    return isinstance(request, BoundedViewRequest) and _identifier(request.profile) and _identifier(request.requested_id) and type(request.limit) is int and 1 <= request.limit <= 100 and (request.cursor is None or isinstance(request.cursor, str))


def _valid_target(target: Any) -> bool:
    return isinstance(target, ResolvedTarget) and _valid_members(target.member_ids) and target.requested_id in target.member_ids and target.root_id in target.member_ids and target.canonical_id in target.member_ids and all(_identifier(value) for value in (target.requested_id, target.canonical_id, target.root_id)) and _digest(target.lineage_fingerprint) and _digest(target.database_identity_digest) and target.source_mode == "state_db" and (target.global_generation_hint is None or (type(target.global_generation_hint) is int and target.global_generation_hint >= 0))


def _valid_members(members: Any) -> bool:
    return isinstance(members, tuple) and 1 <= len(members) <= _MAX_MEMBERS and len(set(members)) == len(members) and all(_identifier(member) for member in members)


def _validate_legacy(legacy: Any) -> None:
    if not isinstance(legacy, LegacyView) or not _messages(legacy.messages) or type(legacy.message_count) is not int or legacy.message_count < 0 or not isinstance(legacy.metadata, Mapping):
        raise ValueError("legacy view is malformed")


def _validate_page(page: Any, target: ResolvedTarget, limit: int) -> None:
    if not isinstance(page, PageView) or not _messages(page.messages) or type(page.has_more) is not bool or type(page.visible_count) is not int or not 0 <= page.visible_count <= min(limit, len(page.messages)) or type(page.raw_rows_examined) is not int or not 0 <= page.raw_rows_examined <= max(256, min(2048, 8 * limit)) + 64 or type(page.serialized_bytes) is not int or not 0 <= page.serialized_bytes <= _MAX_SERIALIZED_BYTES or not isinstance(page.metadata, Mapping):
        raise ValueError("page view is malformed")
    if page.serialized_bytes != _strict_message_bytes(page.messages):
        raise ValueError("page serialized byte accounting is invalid")
    boundaries = page.before_boundaries
    if not isinstance(boundaries, tuple) or len(boundaries) > _MAX_MEMBERS:
        raise ValueError("page boundaries are malformed")
    seen = set()
    for boundary in boundaries:
        if not isinstance(boundary, MessageCursorBoundary) or boundary.member_id not in target.member_ids or boundary.member_id in seen or type(boundary.message_id) is not int or boundary.message_id < 0 or type(boundary.inclusive) is not bool:
            raise ValueError("page boundaries are malformed")
        seen.add(boundary.member_id)
    if page.has_more and not boundaries:
        raise ValueError("page has_more requires boundaries")


def _messages(messages: Any) -> bool:
    return isinstance(messages, list) and all(isinstance(message, dict) for message in messages)


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in connection.execute(f"PRAGMA table_info({table})"):
        result[str(row[1])] = {"type": str(row[2]).upper(), "notnull": row[3], "pk": row[5]}
    return result


def _proof_trigger_semantics(triggers: Mapping[str, str]) -> bool:
    if set(triggers) != set(_CANONICAL_TRIGGER_DDL):
        return False
    for name, expected in _CANONICAL_TRIGGER_DDL.items():
        actual = triggers[name]
        if not isinstance(actual, str) or _COMMENT_RE.search(actual):
            return False
        if " ".join(actual.lower().split()) != expected:
            return False
    return True


def _strict_message_bytes(messages: list[dict]) -> int:
    return len(
        json.dumps(
            tuple(messages),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    )


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and "\0" not in value


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:") and all(char in "0123456789abcdef" for char in value[7:])


def _message_ids(messages: list[Mapping[str, Any]]) -> tuple[str, ...]:
    values = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        for key in ("_state_db_message_id", "message_id", "id", "_id"):
            if message.get(key) is not None and str(message[key]):
                values.append(str(message[key]))
                break
    return tuple(values)


def _tool_pairs(messages: list[Mapping[str, Any]]) -> frozenset[tuple[str, bool]]:
    calls, results = set(), set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if isinstance(message.get("tool_calls"), list):
            calls.update(call["id"] for call in message["tool_calls"] if isinstance(call, Mapping) and _identifier(call.get("id")))
        if _identifier(message.get("tool_call_id")):
            results.add(message["tool_call_id"])
    return frozenset((call_id, call_id in results) for call_id in calls)
