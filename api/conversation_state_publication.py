"""Publish a settled todo projection and reconciliation receipt as one transaction.

The caller owns canonical settlement.  It supplies the complete durable
canonical-message snapshot and a bounded supplier which re-resolves the same
lineage's current proof.  This module deliberately does not know about routes,
streaming, or Agent session locks.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from api.conversation_receipts import (
    ConversationReceipt,
    ConversationReceiptStore,
    ReceiptStoreError,
    canonical_proof_digest,
    validate_receipt,
)
from api.conversation_view_state import (
    ConversationViewState,
    ConversationViewStateStore,
    MessageWatermark,
    ViewStateStoreError,
    snapshot_digest,
)
from api.bounded_conversation_integration import (
    ExactShadowMatch,
    IntegrationProofError,
    exact_shadow_match_accepts_current,
    exact_visible_digest,
)
from api.todo_state import derive_todo_state


class ConversationStatePublicationError(RuntimeError):
    """A settled projection could not be proven and published exactly once."""


@dataclass(frozen=True)
class SettledConversationStatePublication:
    """The only artifacts eligible for a later bounded-view read."""

    projection: ConversationViewState
    receipt: ConversationReceipt


_MAX_CANONICAL_SNAPSHOT_BYTES = (2 * 1024 * 1024) + (512 * 1024)


def _immutable_canonical_snapshot(
    messages: Sequence[dict[str, Any]],
    shadow_match: ExactShadowMatch,
) -> tuple[dict[str, Any], ...]:
    """Copy and bind the todo source to the exact transcript acceptance."""
    if not isinstance(messages, (list, tuple)):
        raise ConversationStatePublicationError(
            "canonical messages must be a bounded sequence"
        )
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    chunks: list[str] = []
    encoded_bytes = 0
    try:
        for chunk in encoder.iterencode(messages):
            encoded_bytes += len(chunk.encode("utf-8"))
            if encoded_bytes > _MAX_CANONICAL_SNAPSHOT_BYTES:
                raise ConversationStatePublicationError(
                    "canonical messages exceed the exact shadow bound"
                )
            chunks.append(chunk)
        decoded = json.loads("".join(chunks))
    except ConversationStatePublicationError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ConversationStatePublicationError(
            "canonical messages are not finite JSON"
        ) from exc
    if not isinstance(decoded, list) or any(type(message) is not dict for message in decoded):
        raise ConversationStatePublicationError("canonical messages are malformed")
    if any(
        message.get("_content_truncated") is True
        or "_content_original_chars" in message
        or "_content_original_bytes" in message
        for message in decoded
    ):
        raise ConversationStatePublicationError(
            "canonical messages contain an incomplete payload"
        )
    snapshot = tuple(decoded)
    try:
        digest = exact_visible_digest(snapshot)
    except IntegrationProofError as exc:
        raise ConversationStatePublicationError(
            "canonical messages are not an exact shadow snapshot"
        ) from exc
    if (
        not isinstance(shadow_match, ExactShadowMatch)
        or digest != shadow_match.candidate_visible_digest
        or len(snapshot) != shadow_match.candidate_count
    ):
        raise ConversationStatePublicationError(
            "canonical messages do not match exact shadow acceptance"
        )
    return snapshot


def _empty_todo_tombstone() -> dict[str, Any]:
    """A durable no-todo result must supersede an older non-empty projection."""
    return {"todos": [], "summary": {}, "version": 1}


def _current_mapping(current_supplier: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    try:
        current = current_supplier()
    except Exception as exc:
        raise ConversationStatePublicationError("current proof supplier failed") from exc
    if not isinstance(current, Mapping):
        raise ConversationStatePublicationError("current proof supplier returned no mapping")
    return dict(current)


def _watermark(current: Mapping[str, Any]) -> MessageWatermark:
    try:
        message_id, timestamp = current["state_message_watermark"]
        return MessageWatermark(timestamp=timestamp, message_id=message_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConversationStatePublicationError("current proof has invalid message watermark") from exc


def _projection_bound_current(
    current: Mapping[str, Any], projection: ConversationViewState
) -> dict[str, Any]:
    bound = dict(current)
    bound.update(
        {
            "todo_projection_generation": projection.generation,
            "todo_projection_watermark": (
                projection.watermark.message_id,
                projection.watermark.timestamp,
            ),
            "todo_projection_target_content_proof_digest": (
                projection.target_content_proof_digest
            ),
            "todo_projection_snapshot_digest": projection.snapshot_digest,
        }
    )
    return bound


def _receipt_current_supplier(
    *,
    current_supplier: Callable[[], Mapping[str, Any]],
    view_state_store: ConversationViewStateStore,
    candidate: ConversationReceipt,
    expected_projection: ConversationViewState,
    expected_snapshot_digest: str,
) -> Mapping[str, Any]:
    """Re-read the durable projection for every receipt-store proof check.

    Returning an unbound current mapping intentionally makes receipt validation
    fail closed.  ``ConversationReceiptStore.publish_if_current`` then either
    avoids publication or removes exactly the receipt it just replaced.
    """
    try:
        current = _current_mapping(current_supplier)
    except ConversationStatePublicationError:
        return {}
    try:
        durable = view_state_store.read(
            profile=candidate.profile,
            root_id=candidate.root_id,
            target_content_proof_digest=(
                candidate.todo_projection_target_content_proof_digest
            ),
            watermark=MessageWatermark(
                timestamp=candidate.todo_projection_watermark[1],
                message_id=candidate.todo_projection_watermark[0],
            ),
        )
    except (ValueError, ViewStateStoreError):
        return current
    if (
        durable is None
        or durable.generation != expected_projection.generation
        or durable.watermark != expected_projection.watermark
        or durable.target_content_proof_digest
        != expected_projection.target_content_proof_digest
        or durable.snapshot_digest != expected_snapshot_digest
        or durable.snapshot_digest != expected_projection.snapshot_digest
    ):
        return current
    return _projection_bound_current(current, durable)


def _candidate(current: Mapping[str, Any], projection: ConversationViewState) -> ConversationReceipt:
    """Bind the explicit canonical sidecar row and the complete lineage vector."""
    try:
        return ConversationReceipt(
            version=1,
            profile=current["profile"],
            root_id=current["root_id"],
            member_ids=current["member_ids"],
            lineage_fingerprint=current["lineage_fingerprint"],
            canonical_sidecar_id=current["canonical_sidecar_id"],
            lineage_sidecar_proof=current["lineage_sidecar_proof"],
            sidecar_generation=current["sidecar_generation"],
            sidecar_stat=current["sidecar_stat"],
            truncation_watermark=current["truncation_watermark"],
            state_message_watermark=current["state_message_watermark"],
            state_content_proof=current["state_content_proof"],
            settled_display_message_count=current["settled_display_message_count"],
            visible_transcript_digest=current["visible_transcript_digest"],
            todo_projection_generation=projection.generation,
            todo_projection_watermark=(
                projection.watermark.message_id,
                projection.watermark.timestamp,
            ),
            todo_projection_target_content_proof_digest=(
                projection.target_content_proof_digest
            ),
            todo_projection_snapshot_digest=projection.snapshot_digest,
            generation=0,
        )
    except (KeyError, ReceiptStoreError) as exc:
        raise ConversationStatePublicationError("current proof cannot form a receipt") from exc


def _fault(fault_hook: Callable[[str], Any] | None, stage: str) -> None:
    if fault_hook is None:
        return
    try:
        fault_hook(stage)
    except Exception as exc:
        raise ConversationStatePublicationError(f"fault hook interrupted at {stage}") from exc


def publish_settled_conversation_state(
    *,
    receipt_store: ConversationReceiptStore,
    view_state_store: ConversationViewStateStore,
    canonical_messages: Sequence[dict[str, Any]],
    current_supplier: Callable[[], Mapping[str, Any]],
    shadow_match: ExactShadowMatch,
    fault_hook: Callable[[str], Any] | None = None,
) -> SettledConversationStatePublication:
    """Publish the projection first and its exact receipt last.

    ``canonical_messages`` is the complete, already-durable requested-lineage
    snapshot.  The supplier must perform a fresh bounded canonical resolution
    and re-read sidecar/proof state on every call.  Any mismatch leaves only a
    rebuildable projection; without the final receipt it is never cursor-usable.
    """
    canonical_snapshot = _immutable_canonical_snapshot(
        canonical_messages, shadow_match
    )

    initial = _current_mapping(current_supplier)
    if not exact_shadow_match_accepts_current(shadow_match, initial):
        raise ConversationStatePublicationError(
            "exact shadow acceptance is not bound to current proof"
        )
    try:
        proof_digest = canonical_proof_digest(
            initial["lineage_fingerprint"], initial["state_content_proof"]
        )
        watermark = _watermark(initial)
    except (KeyError, ReceiptStoreError) as exc:
        raise ConversationStatePublicationError("current proof is unverifiable") from exc
    _fault(fault_hook, "after_current_read")

    todo_snapshot = derive_todo_state(canonical_snapshot) or _empty_todo_tombstone()
    try:
        derived_snapshot_digest = snapshot_digest(todo_snapshot)
    except ValueError as exc:
        raise ConversationStatePublicationError("derived todo snapshot is malformed") from exc
    try:
        cas = view_state_store.compare_and_swap(
            profile=initial["profile"],
            root_id=initial["root_id"],
            watermark=watermark,
            target_content_proof_digest=proof_digest,
            snapshot=todo_snapshot,
        )
    except (KeyError, ValueError, ViewStateStoreError, OSError) as exc:
        raise ConversationStatePublicationError("todo projection publication failed") from exc
    if not cas.saved:
        raise ConversationStatePublicationError(
            f"todo projection publication rejected: {cas.reason}"
        )
    try:
        projection = view_state_store.read(
            profile=initial["profile"],
            root_id=initial["root_id"],
            target_content_proof_digest=proof_digest,
            watermark=watermark,
        )
    except (ValueError, ViewStateStoreError) as exc:
        raise ConversationStatePublicationError("todo projection read-back failed") from exc
    if (
        projection is None
        or projection.generation != cas.state.generation
        or projection.watermark != watermark
        or projection.target_content_proof_digest != proof_digest
        or projection.snapshot_digest != derived_snapshot_digest
        or projection.snapshot_digest != cas.state.snapshot_digest
    ):
        raise ConversationStatePublicationError("todo projection read-back is mismatched")
    _fault(fault_hook, "after_todo_cas")

    candidate = _candidate(initial, projection)
    receipt_current = lambda: _receipt_current_supplier(
        current_supplier=current_supplier,
        view_state_store=view_state_store,
        candidate=candidate,
        expected_projection=projection,
        expected_snapshot_digest=derived_snapshot_digest,
    )
    before_receipt = validate_receipt(candidate, current=receipt_current())
    if not before_receipt.valid:
        raise ConversationStatePublicationError(
            f"current proof changed before receipt publication: {before_receipt.reason}"
        )
    _fault(fault_hook, "before_receipt_publish")
    try:
        receipt = receipt_store.publish_if_current(
            candidate,
            receipt_current,
        )
    except (ReceiptStoreError, ConversationStatePublicationError) as exc:
        raise ConversationStatePublicationError("receipt publication failed") from exc
    _fault(fault_hook, "after_receipt_publication")
    return SettledConversationStatePublication(projection=projection, receipt=receipt)
