"""Fail-closed adapters joining bounded conversation proof primitives.

This module owns no route or runtime globals.  Route integration supplies the
environment, state-db path, sidecar proof, and active-run mapping explicitly.
Every conversion is deliberately strict so an incomplete proof selects the
existing exact legacy path instead of manufacturing cursor eligibility.
"""
from __future__ import annotations

from contextlib import closing, nullcontext
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from api.bounded_runtime_overlay import RuntimeOwner
from api.bounded_session_view import (
    ProofCapability,
    ResolvedTarget,
    detect_proof_v1_capability,
    read_target_content_proof,
)
from api.bounded_sidecar_proof import SidecarLineageProof, prove_sidecar_lineage
from api.agent_sessions import (
    open_state_db_readonly,
    shared_state_db_identity,
)
from api.session_message_paging import inspect_message_paging_capability
from api.conversation_view_state import (
    ConversationViewStateStore,
    MessageWatermark,
    ViewStateStoreError,
)
from api.conversation_receipts import (
    CONTENT_PROOF_KIND,
    MISSING_SIDECAR_MARKER,
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
    ConversationReceipt,
    ReceiptStoreError,
    canonical_proof_digest,
)
from api.profiles import _profiles_match


_MAX_IDENTIFIER_LENGTH = 512
_MAX_MEMBERS = 256
_MAX_ACTIVE_RUNS = 256
_MAX_OWNER_PROOFS = 256
_MAX_RUNTIME_RECORD_BYTES = 64 * 1024
_MAX_VISIBLE_MESSAGES = 4_096
_MAX_VISIBLE_DIGEST_BYTES = (2 * 1024 * 1024) + (512 * 1024)
_DIGEST_PREFIX = "sha256:"
_RECEIPT_FAST_PATH = "HERMES_WEBUI_RECEIPT_FAST_PATH"
_DERIVED_VIEW_READS = "HERMES_WEBUI_DERIVED_VIEW_STATE_READS"
_BOUNDED_VIEW_SHADOW = "HERMES_WEBUI_BOUNDED_VIEW_SHADOW"
_CURSOR_MODE = "HERMES_WEBUI_MESSAGE_CURSOR_V1"
_EXACT_SHADOW_MATCH_CAPABILITY = object()
BOUNDED_VIEW_IMPLEMENTATION_ID = "bounded-view-v1"
PROOF_SCHEMA_ID = "agent-proof-v1"


class IntegrationProofError(ValueError):
    """An adapter input is incomplete, raced, malformed, or unbounded."""


@dataclass(frozen=True)
class ShadowReadiness:
    """The durable enablement decision supplied by the shadow-evidence store."""

    ready: bool
    reason: str
    cohort: str


@dataclass(frozen=True)
class PublicCursorGate:
    public_cursor: bool
    reason: str


@dataclass(frozen=True)
class ExactShadowMatch:
    """Typed proof that candidate and unchanged oracle were exactly equivalent."""

    profile: str
    root_id: str
    lineage_fingerprint: str
    target_content_proof_digest: str
    lineage_sidecar_proof_digest: str
    candidate_visible_digest: str
    oracle_visible_digest: str
    candidate_count: int
    oracle_count: int
    capability_marker: object | None = field(default=None, repr=False, compare=False)

    @property
    def exact(self) -> bool:
        return (
            self.capability_marker is _EXACT_SHADOW_MATCH_CAPABILITY
            and _identifier(self.profile)
            and _identifier(self.root_id)
            and _digest(self.lineage_fingerprint)
            and _digest(self.target_content_proof_digest)
            and _digest(self.lineage_sidecar_proof_digest)
            and _digest(self.candidate_visible_digest)
            and self.candidate_visible_digest == self.oracle_visible_digest
            and type(self.candidate_count) is int
            and self.candidate_count >= 0
            and self.candidate_count == self.oracle_count
        )


@dataclass(frozen=True)
class StateTailWatermark:
    """One member's bounded state-db tail, in resolved lineage order."""

    member_id: str
    message_id: int
    timestamp: float | int


@dataclass(frozen=True)
class BoundedStateSnapshot:
    """One read-only SQLite snapshot of proof capability, epochs, and tails."""

    capability: ProofCapability
    target_content_proof: tuple[tuple[str, int], ...]
    state_tail_watermarks: tuple[StateTailWatermark, ...]


@dataclass(frozen=True)
class DurableTodoProjection:
    """A projection that was durably read back before receipt publication."""

    generation: int
    message_id: int
    timestamp: float | int
    snapshot_digest: str


@dataclass(frozen=True)
class CurrentConversationProof:
    """Receipt-compatible proof plus the bounded per-member tails it derives from."""

    profile: str
    target: ResolvedTarget
    canonical_sidecar_id: str
    lineage_sidecar_proof: tuple[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str], ...
    ]
    sidecar_generation: int
    sidecar_stat: tuple[str, int, int, int]
    truncation_watermark: float | int | None
    state_tail_watermarks: tuple[StateTailWatermark, ...]
    state_message_watermark: tuple[int, float | int]
    state_content_proof: tuple[str, tuple[tuple[str, int], ...]]
    settled_display_message_count: int
    visible_transcript_digest: str
    todo_projection: DurableTodoProjection
    shadow_match: ExactShadowMatch | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Return exactly the receipt validator's current-proof inputs.

        The tail vector remains available to route diagnostics/callers, while
        the receipt schema binds its deterministic latest member watermark.
        """
        proof_digest = canonical_proof_digest(
            self.target.lineage_fingerprint, self.state_content_proof
        )
        return {
            "profile": self.profile,
            "root_id": self.target.root_id,
            "member_ids": self.target.member_ids,
            "lineage_fingerprint": self.target.lineage_fingerprint,
            "canonical_sidecar_id": self.canonical_sidecar_id,
            "lineage_sidecar_proof": self.lineage_sidecar_proof,
            "sidecar_generation": self.sidecar_generation,
            "sidecar_stat": self.sidecar_stat,
            "truncation_watermark": self.truncation_watermark,
            "state_message_watermark": self.state_message_watermark,
            "state_content_proof": self.state_content_proof,
            "settled_display_message_count": self.settled_display_message_count,
            "visible_transcript_digest": self.visible_transcript_digest,
            "todo_projection_generation": self.todo_projection.generation,
            "todo_projection_watermark": self.state_message_watermark,
            "todo_projection_target_content_proof_digest": proof_digest,
            "todo_projection_snapshot_digest": self.todo_projection.snapshot_digest,
            "state_tail_watermarks": self.state_tail_watermarks,
        }

    def receipt_candidate(self, *, generation: int = 0) -> ConversationReceipt:
        """Create a receipt candidate only after an exact typed shadow match."""
        if not _shadow_match_binds(
            self.shadow_match,
            profile=self.profile,
            target=self.target,
            state_content_proof=self.state_content_proof,
            lineage_sidecar_proof=self.lineage_sidecar_proof,
            visible_transcript_digest=self.visible_transcript_digest,
            settled_display_message_count=self.settled_display_message_count,
        ):
            raise IntegrationProofError("exact shadow match is required before receipt publication")
        try:
            current = self.to_mapping()
            current.pop("state_tail_watermarks")
            return ConversationReceipt(
                version=1,
                generation=generation,
                **current,
            )
        except (ReceiptStoreError, TypeError, ValueError) as exc:
            raise IntegrationProofError("current proof cannot form a durable receipt") from exc


def evaluate_public_cursor_gate(
    environment: Mapping[str, str] | None,
    capability: ProofCapability,
    receipt_is_durable: bool,
    shadow_readiness: ShadowReadiness,
) -> PublicCursorGate:
    """Require all independent switches and typed proof before cursor exposure."""
    env = environment if isinstance(environment, Mapping) else {}
    if env.get(_CURSOR_MODE) != "on":
        return PublicCursorGate(False, "cursor_mode_not_on")
    if env.get(_RECEIPT_FAST_PATH) != "1":
        return PublicCursorGate(False, "receipt_fast_path_disabled")
    if env.get(_DERIVED_VIEW_READS) != "1":
        return PublicCursorGate(False, "derived_view_reads_disabled")
    if env.get(_BOUNDED_VIEW_SHADOW) != "1":
        return PublicCursorGate(False, "shadow_gate_disabled")
    if not (
        isinstance(capability, ProofCapability)
        and capability.available is True
        and capability.capability_marker is VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
    ):
        return PublicCursorGate(False, "unverifiable_current_state")
    if receipt_is_durable is not True:
        return PublicCursorGate(False, "receipt_not_durable")
    if not (
        isinstance(shadow_readiness, ShadowReadiness)
        and shadow_readiness.ready is True
        and _identifier(shadow_readiness.reason)
        and _identifier(shadow_readiness.cohort)
    ):
        return PublicCursorGate(False, "shadow_not_ready")
    return PublicCursorGate(True, "ready")


def detect_readonly_proof_capability(db_path: str | Path) -> ProofCapability:
    """Run the existing proof-contract detector through a read-only DB handle."""
    try:
        path = Path(db_path).resolve(strict=True)
        if not path.is_file():
            return ProofCapability(False, "unverifiable_current_state")
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            return detect_proof_v1_capability(connection)
    except (OSError, sqlite3.Error, ValueError):
        return ProofCapability(False, "unverifiable_current_state")


def read_bounded_state_snapshot(
    *,
    db_path: str | Path,
    member_ids: tuple[str, ...],
    expected_database_identity: tuple[str, int | None, int | None],
) -> BoundedStateSnapshot:
    """Read the declared content epochs and one indexed tail per member."""
    if not _members(member_ids) or not _database_identity(
        expected_database_identity
    ):
        raise IntegrationProofError("bounded state snapshot identity is invalid")
    path = Path(db_path)
    if shared_state_db_identity(path) != expected_database_identity:
        raise IntegrationProofError("state database identity changed")
    try:
        with closing(open_state_db_readonly(path)) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            if shared_state_db_identity(path) != expected_database_identity:
                raise IntegrationProofError("state database identity changed")
            capability = detect_proof_v1_capability(connection)
            if (
                capability.available is not True
                or capability.capability_marker
                is not VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
            ):
                raise IntegrationProofError("unverifiable current state")
            paging = inspect_message_paging_capability(
                connection, db_identity=expected_database_identity
            )
            if not paging.supported or not paging.message_index:
                raise IntegrationProofError("indexed state tails are unavailable")
            # The shared capability detector compares its declaration marker as
            # a tuple.  Switch to named rows only after the detector completes;
            # the paging inspector accepts either representation.
            connection.row_factory = sqlite3.Row
            proof = read_target_content_proof(connection, member_ids)
            if proof is None or proof[0] != CONTENT_PROOF_KIND:
                raise IntegrationProofError("target content proof is unavailable")
            rows = _content_proof(member_ids, proof[1])[1]
            quoted_index = '"' + paging.message_index.replace('"', '""') + '"'
            tails = []
            for member_id in member_ids:
                raw = connection.execute(
                    f"SELECT id, timestamp FROM messages INDEXED BY {quoted_index} "
                    "WHERE session_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (member_id,),
                ).fetchone()
                if raw is None:
                    tails.append(StateTailWatermark(member_id, 0, 0.0))
                    continue
                message_id = raw["id"]
                timestamp = raw["timestamp"]
                if timestamp is None:
                    timestamp = 0.0
                tail = StateTailWatermark(member_id, message_id, timestamp)
                if (
                    type(message_id) is not int
                    or message_id < 0
                    or not _finite_nonnegative(timestamp)
                ):
                    raise IntegrationProofError("state tail watermark is malformed")
                tails.append(tail)
            if shared_state_db_identity(path) != expected_database_identity:
                raise IntegrationProofError("state database identity changed")
            return BoundedStateSnapshot(capability, rows, tuple(tails))
    except IntegrationProofError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        raise IntegrationProofError("bounded state snapshot is unavailable") from exc


def read_unpublished_current_proof_from_sources(
    *,
    target: ResolvedTarget,
    profile: str,
    db_path: str | Path,
    expected_database_identity: tuple[str, int | None, int | None],
    sidecar_dir: str | Path,
    shadow_match: ExactShadowMatch,
) -> dict[str, Any]:
    """Re-read a publishable exact-match binding before a receipt exists.

    Unlike :func:`read_current_proof_from_sources`, this reader intentionally
    does not require an existing receipt or todo projection.  It is only for
    bootstrapping those artifacts from a typed exact shadow match; every value
    is re-derived from proof-v1 SQLite state and the sidecar lineage.
    """
    if not isinstance(target, ResolvedTarget) or not _identifier(profile):
        raise IntegrationProofError("unpublished current proof inputs are invalid")
    state = read_bounded_state_snapshot(
        db_path=db_path,
        member_ids=target.member_ids,
        expected_database_identity=expected_database_identity,
    )
    if state.capability.capability_marker is not VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY:
        raise IntegrationProofError("unverifiable current state")
    sidecars = prove_sidecar_lineage(sidecar_dir, target.member_ids, profile)
    paths = {
        member_id: str(Path(sidecar_dir) / f"{member_id}.json")
        for member_id in target.member_ids
    }
    content_proof = _content_proof(target.member_ids, state.target_content_proof)
    sidecar_proof, generation, sidecar_stat, truncation = _sidecar_binding(
        target, sidecars, paths
    )
    tails, watermark = _state_tails(target.member_ids, state.state_tail_watermarks)
    if not _shadow_match_binds(
        shadow_match,
        profile=profile,
        target=target,
        state_content_proof=content_proof,
        lineage_sidecar_proof=sidecar_proof,
        visible_transcript_digest=shadow_match.candidate_visible_digest,
        settled_display_message_count=shadow_match.candidate_count,
    ):
        raise IntegrationProofError("exact shadow match is not current")
    return {
        "profile": profile,
        "root_id": target.root_id,
        "member_ids": target.member_ids,
        "lineage_fingerprint": target.lineage_fingerprint,
        "canonical_sidecar_id": target.canonical_id,
        "lineage_sidecar_proof": sidecar_proof,
        "sidecar_generation": generation,
        "sidecar_stat": sidecar_stat,
        "truncation_watermark": truncation,
        "state_message_watermark": watermark,
        "state_content_proof": content_proof,
        "state_content_proof_capability": VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
        "settled_display_message_count": shadow_match.candidate_count,
        "visible_transcript_digest": shadow_match.candidate_visible_digest,
        "state_tail_watermarks": tails,
    }


def shadow_readiness_from_evidence(evidence: Any) -> ShadowReadiness:
    """Convert the durable store's readiness without manufacturing readiness."""
    ready = getattr(evidence, "ready", None)
    reason = getattr(evidence, "reason", None)
    if type(ready) is not bool or not _identifier(reason):
        return ShadowReadiness(False, "shadow_readiness_invalid", "invalid")
    cohort = f"{BOUNDED_VIEW_IMPLEMENTATION_ID}.{PROOF_SCHEMA_ID}"
    return ShadowReadiness(ready, reason, cohort)


def read_current_proof_from_sources(
    *,
    target: ResolvedTarget,
    profile: str,
    db_path: str | Path,
    expected_database_identity: tuple[str, int | None, int | None],
    sidecar_dir: str | Path,
    receipt: ConversationReceipt,
    view_state_store: ConversationViewStateStore,
) -> CurrentConversationProof:
    """Recompute every independent receipt binding from bounded sources."""
    if not isinstance(receipt, ConversationReceipt):
        raise IntegrationProofError("durable receipt is required")
    state = read_bounded_state_snapshot(
        db_path=db_path,
        member_ids=target.member_ids,
        expected_database_identity=expected_database_identity,
    )
    sidecars = prove_sidecar_lineage(sidecar_dir, target.member_ids, profile)
    paths = {
        member_id: str(Path(sidecar_dir) / f"{member_id}.json")
        for member_id in target.member_ids
    }
    content_proof = (CONTENT_PROOF_KIND, state.target_content_proof)
    proof_digest = canonical_proof_digest(
        target.lineage_fingerprint, content_proof
    )
    _, watermark = _state_tails(
        target.member_ids, state.state_tail_watermarks
    )
    try:
        projection = view_state_store.read(
            profile=profile,
            root_id=target.root_id,
            target_content_proof_digest=proof_digest,
            watermark=MessageWatermark(
                timestamp=watermark[1], message_id=watermark[0]
            ),
        )
    except (ValueError, ViewStateStoreError) as exc:
        raise IntegrationProofError("durable todo projection is unavailable") from exc
    if projection is None:
        raise IntegrationProofError("durable todo projection is unavailable")
    todo = DurableTodoProjection(
        projection.generation,
        projection.watermark.message_id,
        projection.watermark.timestamp,
        projection.snapshot_digest,
    )
    return build_current_proof(
        target=target,
        profile=profile,
        sidecars=sidecars,
        sidecar_paths=paths,
        target_content_proof=state.target_content_proof,
        state_tail_watermarks=state.state_tail_watermarks,
        settled_display_message_count=receipt.settled_display_message_count,
        visible_transcript_digest=receipt.visible_transcript_digest,
        todo_projection=todo,
    )


def prove_exact_shadow_match(
    *,
    target: ResolvedTarget,
    profile: str,
    sidecars: SidecarLineageProof,
    sidecar_paths: Mapping[str, str],
    target_content_proof: Sequence[tuple[str, int]],
    candidate_messages: Sequence[Mapping[str, Any]],
    oracle_messages: Sequence[Mapping[str, Any]],
    candidate_count: int,
    oracle_count: int,
) -> ExactShadowMatch:
    """Create a target-bound acceptance only for one exact, unchanged oracle."""
    if not isinstance(target, ResolvedTarget) or not _members(target.member_ids):
        raise IntegrationProofError("resolved target is invalid")
    if not _identifier(profile):
        raise IntegrationProofError("profile is invalid")
    lineage_sidecar_proof, _, _, _ = _sidecar_binding(
        target, sidecars, sidecar_paths
    )
    state_content_proof = _content_proof(
        target.member_ids, target_content_proof
    )
    return _prove_exact_shadow_bindings(
        profile=profile,
        root_id=target.root_id,
        lineage_fingerprint=target.lineage_fingerprint,
        state_content_proof=state_content_proof,
        lineage_sidecar_proof=lineage_sidecar_proof,
        candidate_messages=candidate_messages,
        oracle_messages=oracle_messages,
        candidate_count=candidate_count,
        oracle_count=oracle_count,
    )


def prove_exact_shadow_match_for_current(
    *,
    current: Mapping[str, Any],
    candidate_messages: Sequence[Mapping[str, Any]],
    oracle_messages: Sequence[Mapping[str, Any]],
    candidate_count: int,
    oracle_count: int,
) -> ExactShadowMatch:
    """Prove exact equality from one already-normalized publication snapshot."""
    if not isinstance(current, Mapping):
        raise IntegrationProofError("current proof mapping is required")
    try:
        match = _prove_exact_shadow_bindings(
            profile=current["profile"],
            root_id=current["root_id"],
            lineage_fingerprint=current["lineage_fingerprint"],
            state_content_proof=current["state_content_proof"],
            lineage_sidecar_proof=current["lineage_sidecar_proof"],
            candidate_messages=candidate_messages,
            oracle_messages=oracle_messages,
            candidate_count=candidate_count,
            oracle_count=oracle_count,
        )
    except (KeyError, ReceiptStoreError) as exc:
        raise IntegrationProofError("current proof mapping is incomplete") from exc
    if (
        current.get("visible_transcript_digest") != match.candidate_visible_digest
        or current.get("settled_display_message_count") != match.candidate_count
    ):
        raise IntegrationProofError("shadow equality is not bound to current transcript")
    return match


def exact_shadow_match_accepts_current(
    match: ExactShadowMatch, current: Mapping[str, Any]
) -> bool:
    """Validate a typed match against a fresh publication proof mapping."""
    if not isinstance(current, Mapping):
        return False
    try:
        return (
            isinstance(match, ExactShadowMatch)
            and match.exact
            and match.profile == current["profile"]
            and match.root_id == current["root_id"]
            and match.lineage_fingerprint == current["lineage_fingerprint"]
            and match.target_content_proof_digest
            == canonical_proof_digest(
                current["lineage_fingerprint"], current["state_content_proof"]
            )
            and match.lineage_sidecar_proof_digest
            == _lineage_sidecar_digest(current["lineage_sidecar_proof"])
            and match.candidate_visible_digest
            == current["visible_transcript_digest"]
            and match.candidate_count
            == current["settled_display_message_count"]
        )
    except (KeyError, ReceiptStoreError, IntegrationProofError, TypeError):
        return False


def resolved_target_from_shared_resolution(resolution: Any) -> ResolvedTarget:
    """Convert one found Stage 1 navigation receipt without resolving again."""
    try:
        members = resolution.member_ids
        identity = resolution.database_identity
        requested = resolution.requested_id
        canonical = resolution.canonical_id
        root = resolution.root_id
        tip = resolution.tip_id
        fingerprint = resolution.lineage_fingerprint
        hint = resolution.global_projection_generation_hint
    except (AttributeError, TypeError) as exc:
        raise IntegrationProofError("shared resolution is incomplete") from exc
    if (
        getattr(resolution, "status", None) != "found"
        or getattr(resolution, "mode", None) != "navigation"
        or type(members) is not tuple
        or not _members(members)
        or requested not in members
        or canonical != tip
        or root != members[0]
        or canonical != members[-1]
        or not all(_identifier(value) for value in (requested, canonical, root))
        or not _digest(fingerprint)
        or type(identity) is not tuple
        or not _database_identity(identity)
        or (hint is not None and (type(hint) is not int or hint < 0))
    ):
        raise IntegrationProofError("shared resolution is not a bounded navigation target")
    return ResolvedTarget(
        requested_id=requested,
        canonical_id=canonical,
        root_id=root,
        member_ids=members,
        lineage_fingerprint=fingerprint,
        database_identity_digest=_database_identity_digest(identity),
        global_generation_hint=hint,
        source_mode="state_db",
    )


def build_current_proof(
    *,
    target: ResolvedTarget,
    profile: str,
    sidecars: SidecarLineageProof,
    sidecar_paths: Mapping[str, str],
    target_content_proof: Sequence[tuple[str, int]],
    state_tail_watermarks: Sequence[StateTailWatermark],
    settled_display_message_count: int,
    visible_transcript_digest: str,
    todo_projection: DurableTodoProjection,
    shadow_match: ExactShadowMatch | None = None,
) -> CurrentConversationProof:
    """Build all receipt bindings from bounded, already-read source proof."""
    if not isinstance(target, ResolvedTarget) or not _members(target.member_ids):
        raise IntegrationProofError("resolved target is invalid")
    if not _identifier(profile):
        raise IntegrationProofError("profile is invalid")
    sidecar_proof, canonical_generation, canonical_stat, truncation = _sidecar_binding(
        target, sidecars, sidecar_paths
    )
    content_proof = _content_proof(target.member_ids, target_content_proof)
    tails, watermark = _state_tails(target.member_ids, state_tail_watermarks)
    if type(settled_display_message_count) is not int or settled_display_message_count < 0:
        raise IntegrationProofError("settled display message count is invalid")
    if not _digest(visible_transcript_digest):
        raise IntegrationProofError("visible transcript digest is invalid")
    if not _durable_todo(todo_projection, watermark):
        raise IntegrationProofError("todo projection is not durable at the current watermark")
    if shadow_match is not None and not _shadow_match_binds(
        shadow_match,
        profile=profile,
        target=target,
        state_content_proof=content_proof,
        lineage_sidecar_proof=sidecar_proof,
        visible_transcript_digest=visible_transcript_digest,
        settled_display_message_count=settled_display_message_count,
    ):
        raise IntegrationProofError(
            "exact shadow match is not bound to the current proof epoch"
        )
    return CurrentConversationProof(
        profile=profile,
        target=target,
        canonical_sidecar_id=target.canonical_id,
        lineage_sidecar_proof=sidecar_proof,
        sidecar_generation=canonical_generation,
        sidecar_stat=canonical_stat,
        truncation_watermark=truncation,
        state_tail_watermarks=tails,
        state_message_watermark=watermark,
        state_content_proof=content_proof,
        settled_display_message_count=settled_display_message_count,
        visible_transcript_digest=visible_transcript_digest,
        todo_projection=todo_projection,
        shadow_match=shadow_match,
    )


def exact_visible_digest(messages: Sequence[Mapping[str, Any]]) -> str:
    """Digest the exact rendered sequence, rejecting malformed/non-finite JSON."""
    if type(messages) not in (list, tuple):
        raise IntegrationProofError("visible messages must be a bounded sequence")
    if len(messages) > _MAX_VISIBLE_MESSAGES or any(
        type(message) is not dict for message in messages
    ):
        raise IntegrationProofError("visible messages are malformed or unbounded")
    try:
        digest = _bounded_json_digest(messages, max_bytes=_MAX_VISIBLE_DIGEST_BYTES)
    except IntegrationProofError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise IntegrationProofError("visible messages are not finite JSON") from exc
    return _DIGEST_PREFIX + digest


class ActiveRunsOwnerAdapter:
    """Snapshot/reverify an injected ACTIVE_RUNS-like mapping without globals."""

    def __init__(self, active_runs: Mapping[str, Mapping[str, Any]], *, lock: Any = None):
        self._active_runs = active_runs
        self._lock = lock
        self._proofs: dict[str, tuple[str, str, str, str, str]] = {}

    def snapshot(self, *, profile: str, session_id: str) -> RuntimeOwner | None:
        if not _identifier(profile) or not _identifier(session_id):
            return None
        records = self._records()
        if records is None:
            return None
        matches: list[tuple[str, Mapping[str, Any], str]] = []
        for key, raw in records.items():
            if not _identifier(key) or len(key) > 128 or type(raw) is not dict:
                return None
            raw_profile = raw.get("profile")
            raw_session = raw.get("session_id")
            raw_run = raw.get("run_id", key)
            if not all(_identifier(value) for value in (raw_session, raw_run)):
                return None
            try:
                profile_matches = _profiles_match(raw_profile, profile)
            except Exception:
                profile_matches = False
            if profile_matches and raw_session == session_id:
                fingerprint = _runtime_record_digest(raw)
                if fingerprint is None:
                    return None
                matches.append((key, raw, fingerprint))
        if len(matches) != 1:
            return None
        key, raw, fingerprint = matches[0]
        run_id = raw.get("run_id", key)
        token = f"{key}:{fingerprint}"
        if len(self._proofs) >= _MAX_OWNER_PROOFS:
            # Existing verifiers fail closed after a local capacity rollover.
            self._proofs.clear()
        self._proofs[token] = (key, profile, session_id, run_id, fingerprint)
        return RuntimeOwner(profile, session_id, run_id, True, token)

    def verifier(self, owner: RuntimeOwner) -> bool:
        if not isinstance(owner, RuntimeOwner) or not _identifier(owner.capability_token):
            return False
        proof = self._proofs.get(owner.capability_token)
        if proof is None:
            return False
        key, profile, session_id, run_id, fingerprint = proof
        if (owner.profile, owner.session_id, owner.run_id) != (profile, session_id, run_id):
            return False
        records = self._records()
        if records is None:
            return False
        raw = records.get(key)
        if not isinstance(raw, Mapping):
            return False
        return (
            _safe_profiles_match(raw.get("profile"), profile)
            and raw.get("session_id") == session_id
            and raw.get("run_id", key) == run_id
            and _runtime_record_digest(raw) == fingerprint
        )

    def _records(self) -> dict[str, Mapping[str, Any]] | None:
        try:
            with (self._lock if self._lock is not None else nullcontext()):
                if type(self._active_runs) is not dict or len(self._active_runs) > _MAX_ACTIVE_RUNS:
                    return None
                records: dict[str, Mapping[str, Any]] = {}
                for key, raw in self._active_runs.items():
                    if type(raw) is not dict or len(raw) > 32:
                        return None
                    records[key] = dict(raw)
                return records
        except Exception:
            return None


def _sidecar_binding(
    target: ResolvedTarget,
    sidecars: SidecarLineageProof,
    sidecar_paths: Mapping[str, str],
) -> tuple[
    tuple[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str],
        ...,
    ],
    int,
    tuple[str, int, int, int],
    float | int | None,
]:
    if not isinstance(sidecars, SidecarLineageProof) or tuple(sidecars.member_ids) != target.member_ids:
        raise IntegrationProofError("sidecar lineage is not in resolved member order")
    if len(sidecars.members) != len(target.member_ids):
        raise IntegrationProofError("sidecar lineage is incomplete")
    rows = []
    canonical: tuple[int, tuple[str, int, int, int], float | int | None] | None = None
    for expected_id, member in zip(target.member_ids, sidecars.members, strict=True):
        if getattr(member, "session_id", None) != expected_id:
            raise IntegrationProofError("sidecar lineage is not in resolved member order")
        if member.status == "missing":
            rows.append((expected_id, MISSING_SIDECAR_MARKER))
            continue
        signature = member.stat_signature
        path = sidecar_paths.get(expected_id) if isinstance(sidecar_paths, Mapping) else None
        if (
            member.status != "present"
            or type(member.sidecar_generation) is not int
            or member.sidecar_generation < 0
            or signature is None
            or not _sidecar_path(path)
            or not _finite_nonnegative(member.truncation_watermark, allow_none=True)
        ):
            raise IntegrationProofError("sidecar lineage is unreadable or unbounded")
        descriptor_stat = (
            path,
            signature.device,
            signature.inode,
            signature.mode,
            signature.size,
            signature.mtime_ns,
            signature.ctime_ns,
        )
        compatibility_stat = (
            path,
            signature.mtime_ns,
            signature.size,
            signature.ctime_ns,
        )
        rows.append((expected_id, (member.sidecar_generation, descriptor_stat)))
        if expected_id == target.canonical_id:
            canonical = (
                member.sidecar_generation,
                compatibility_stat,
                member.truncation_watermark,
            )
    if canonical is None:
        raise IntegrationProofError("canonical sidecar is missing")
    return tuple(rows), canonical[0], canonical[1], canonical[2]


def _content_proof(
    members: tuple[str, ...], value: Sequence[tuple[str, int]]
) -> tuple[str, tuple[tuple[str, int], ...]]:
    if type(value) not in (list, tuple):
        raise IntegrationProofError("target content proof is malformed")
    if len(value) != len(members) or len(value) > _MAX_MEMBERS:
        raise IntegrationProofError("target content proof does not exactly cover lineage")
    rows = tuple(value)
    normalized = []
    for member_id, row in zip(members, rows, strict=True):
        if not isinstance(row, tuple) or len(row) != 2 or row[0] != member_id or type(row[1]) is not int or row[1] < 0:
            raise IntegrationProofError("target content proof is not ordered")
        normalized.append((member_id, row[1]))
    return CONTENT_PROOF_KIND, tuple(normalized)


def _state_tails(
    members: tuple[str, ...], value: Sequence[StateTailWatermark]
) -> tuple[tuple[StateTailWatermark, ...], tuple[int, float | int]]:
    if type(value) not in (list, tuple):
        raise IntegrationProofError("state tail watermarks are malformed")
    if len(value) != len(members) or len(value) > _MAX_MEMBERS:
        raise IntegrationProofError("state tail watermarks do not exactly cover lineage")
    tails = tuple(value)
    for member_id, tail in zip(members, tails, strict=True):
        if (
            not isinstance(tail, StateTailWatermark)
            or tail.member_id != member_id
            or type(tail.message_id) is not int
            or tail.message_id < 0
            or not _finite_nonnegative(tail.timestamp)
        ):
            raise IntegrationProofError("state tail watermarks are not ordered")
    latest = max(tails, key=lambda tail: (float(tail.timestamp), tail.message_id))
    return tails, (latest.message_id, latest.timestamp)


def _durable_todo(todo: Any, watermark: tuple[int, float | int]) -> bool:
    return (
        isinstance(todo, DurableTodoProjection)
        and type(todo.generation) is int
        and todo.generation >= 1
        and (todo.message_id, todo.timestamp) == watermark
        and _digest(todo.snapshot_digest)
    )


def _runtime_record_digest(raw: Mapping[str, Any]) -> str | None:
    try:
        return _bounded_json_digest(raw, max_bytes=_MAX_RUNTIME_RECORD_BYTES)
    except (
        IntegrationProofError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
        RecursionError,
    ):
        return None


def _bounded_json_digest(value: Any, *, max_bytes: int) -> str:
    """Hash canonical JSON incrementally and reject before retaining it."""
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            total += len(encoded)
            if total > max_bytes:
                raise IntegrationProofError("canonical JSON exceeds byte limit")
            digest.update(encoded)
    except IntegrationProofError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise IntegrationProofError("canonical JSON is malformed") from exc
    return digest.hexdigest()


def _lineage_sidecar_digest(value: Any) -> str:
    return _DIGEST_PREFIX + _bounded_json_digest(
        value, max_bytes=_MAX_VISIBLE_DIGEST_BYTES
    )


def _prove_exact_shadow_bindings(
    *,
    profile: str,
    root_id: str,
    lineage_fingerprint: str,
    state_content_proof: Any,
    lineage_sidecar_proof: Any,
    candidate_messages: Sequence[Mapping[str, Any]],
    oracle_messages: Sequence[Mapping[str, Any]],
    candidate_count: int,
    oracle_count: int,
) -> ExactShadowMatch:
    if not _identifier(profile) or not _identifier(root_id) or not _digest(
        lineage_fingerprint
    ):
        raise IntegrationProofError("shadow proof identity is invalid")
    candidate_digest = exact_visible_digest(candidate_messages)
    oracle_digest = exact_visible_digest(oracle_messages)
    if (
        type(candidate_count) is not int
        or candidate_count < 0
        or type(oracle_count) is not int
        or oracle_count < 0
        or candidate_count != oracle_count
        or candidate_digest != oracle_digest
    ):
        raise IntegrationProofError("candidate and oracle are not exactly equivalent")
    try:
        content_digest = canonical_proof_digest(
            lineage_fingerprint, state_content_proof
        )
        sidecar_digest = _lineage_sidecar_digest(lineage_sidecar_proof)
    except ReceiptStoreError as exc:
        raise IntegrationProofError("shadow proof binding is malformed") from exc
    return ExactShadowMatch(
        profile=profile,
        root_id=root_id,
        lineage_fingerprint=lineage_fingerprint,
        target_content_proof_digest=content_digest,
        lineage_sidecar_proof_digest=sidecar_digest,
        candidate_visible_digest=candidate_digest,
        oracle_visible_digest=oracle_digest,
        candidate_count=candidate_count,
        oracle_count=oracle_count,
        capability_marker=_EXACT_SHADOW_MATCH_CAPABILITY,
    )


def _shadow_match_binds(
    match: Any,
    *,
    profile: str,
    target: ResolvedTarget,
    state_content_proof: Any,
    lineage_sidecar_proof: Any,
    visible_transcript_digest: str,
    settled_display_message_count: int,
) -> bool:
    if not isinstance(match, ExactShadowMatch) or not match.exact:
        return False
    try:
        content_digest = canonical_proof_digest(
            target.lineage_fingerprint, state_content_proof
        )
        sidecar_digest = _lineage_sidecar_digest(lineage_sidecar_proof)
    except (ReceiptStoreError, IntegrationProofError):
        return False
    return (
        match.profile == profile
        and match.root_id == target.root_id
        and match.lineage_fingerprint == target.lineage_fingerprint
        and match.target_content_proof_digest == content_digest
        and match.lineage_sidecar_proof_digest == sidecar_digest
        and match.candidate_visible_digest == visible_transcript_digest
        and match.candidate_count == settled_display_message_count
    )


def _safe_profiles_match(row_profile: Any, active_profile: Any) -> bool:
    try:
        return _profiles_match(row_profile, active_profile) is True
    except Exception:
        return False


def _database_identity_digest(identity: tuple[str, int | None, int | None]) -> str:
    payload = json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def _database_identity(value: tuple[Any, ...]) -> bool:
    return (
        len(value) == 3
        and _identifier(value[0])
        and all(item is None or (type(item) is int and item >= 0) for item in value[1:])
    )


def _members(value: tuple[str, ...]) -> bool:
    return 1 <= len(value) <= _MAX_MEMBERS and len(set(value)) == len(value) and all(_identifier(item) for item in value)


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value) and len(value) <= _MAX_IDENTIFIER_LENGTH and "\0" not in value


def _digest(value: Any) -> bool:
    return type(value) is str and len(value) == 71 and value.startswith(_DIGEST_PREFIX) and all(char in "0123456789abcdef" for char in value[7:])


def _finite_nonnegative(value: Any, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError, OverflowError):
        return False


def _sidecar_path(value: Any) -> bool:
    return type(value) is str and bool(value) and len(value) <= 4096 and "\0" not in value
