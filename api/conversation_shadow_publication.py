"""Fail-closed bootstrap from an exact shadow match to durable settlement.

This is deliberately an in-process seam: routes keep their exact legacy
response path while, when the Agent proof-v1 capability exists, they may turn
one exact candidate/oracle comparison into the durable projection and receipt
needed by the bounded reader.  It does not expose transcript content or make a
cursor-enable decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from api.bounded_conversation_integration import (
    ExactShadowMatch,
    ResolvedTarget,
    exact_shadow_match_accepts_current,
    read_unpublished_current_proof_from_sources,
)
from api.conversation_receipts import (
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
    ConversationReceiptStore,
)
from api.conversation_shadow_evidence import ShadowProofInput
from api.conversation_state_publication import (
    SettledConversationStatePublication,
    publish_settled_conversation_state,
)
from api.conversation_view_state import ConversationViewStateStore


_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_CURRENT_PROOF_SOURCE_CAPABILITY = object()


class ShadowEvidenceRecorder(Protocol):
    """The minimal content-free durable-evidence dependency."""

    def record(self, proof: ShadowProofInput) -> Any: ...


@dataclass(frozen=True)
class ShadowPublicationEvidenceRequest:
    """Stable, content-free cohort data supplied by the route integration."""

    implementation_id: str
    schema_id: str
    request_generation: int


@dataclass(frozen=True)
class ExactShadowPublicationResult:
    """A non-enabling result; callers must still use the normal public gate."""

    published: bool
    reason: str
    publication: SettledConversationStatePublication | Any | None = None
    evidence: Any | None = None


@dataclass(frozen=True)
class ExactShadowCurrentProofSource:
    """Typed constructor for independently re-read bootstrap proof state.

    The only public constructor binds a resolved target to the SQLite identity
    observed for the request.  Each supplier invocation opens the database and
    re-proves its proof-v1 epochs plus the whole sidecar lineage; callers never
    provide a receipt-shaped current mapping themselves.
    """

    target: ResolvedTarget
    profile: str
    db_path: Path
    expected_database_identity: tuple[str, int | None, int | None]
    sidecar_dir: Path
    _capability: object | None = None

    @classmethod
    def from_sources(
        cls,
        *,
        target: ResolvedTarget,
        profile: str,
        db_path: str | Path,
        expected_database_identity: tuple[str, int | None, int | None],
        sidecar_dir: str | Path,
    ) -> "ExactShadowCurrentProofSource":
        return cls(
            target=target,
            profile=profile,
            db_path=Path(db_path),
            expected_database_identity=expected_database_identity,
            sidecar_dir=Path(sidecar_dir),
            _capability=_CURRENT_PROOF_SOURCE_CAPABILITY,
        )

    def current_supplier(
        self, exact_match: ExactShadowMatch
    ) -> Callable[[], Mapping[str, Any]]:
        if self._capability is not _CURRENT_PROOF_SOURCE_CAPABILITY:
            raise ValueError("current proof source is untrusted")

        def supplier() -> Mapping[str, Any]:
            return read_unpublished_current_proof_from_sources(
                target=self.target,
                profile=self.profile,
                db_path=self.db_path,
                expected_database_identity=self.expected_database_identity,
                sidecar_dir=self.sidecar_dir,
                shadow_match=exact_match,
            )

        return supplier


def _read_current(
    current_supplier: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    try:
        current = current_supplier()
    except Exception:
        return None
    return current if isinstance(current, Mapping) else None


def _proof_is_publishable(
    exact_match: ExactShadowMatch,
    current: Mapping[str, Any] | None,
) -> bool:
    """Require both opaque exact-match authority and verified proof-v1 state."""
    return (
        isinstance(current, Mapping)
        and current.get("state_content_proof_capability")
        is VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
        and exact_shadow_match_accepts_current(exact_match, current)
    )


def _valid_evidence_request(request: ShadowPublicationEvidenceRequest) -> bool:
    return (
        isinstance(request, ShadowPublicationEvidenceRequest)
        and isinstance(request.implementation_id, str)
        and _IDENTIFIER_RE.fullmatch(request.implementation_id) is not None
        and isinstance(request.schema_id, str)
        and _IDENTIFIER_RE.fullmatch(request.schema_id) is not None
        and type(request.request_generation) is int
        and request.request_generation >= 0
    )


def publish_exact_shadow_settlement(
    *,
    receipt_store: ConversationReceiptStore,
    view_state_store: ConversationViewStateStore,
    canonical_messages: Sequence[dict[str, Any]],
    proof_source: ExactShadowCurrentProofSource,
    exact_match: ExactShadowMatch,
    evidence_store: ShadowEvidenceRecorder,
    evidence_request: ShadowPublicationEvidenceRequest,
) -> ExactShadowPublicationResult:
    """Publish only a proof-v1-bound exact match, then record evidence last.

    ``exact_match`` contains the opaque in-process capability binding profile,
    root, lineage fingerprint, both transcript digests, and both counts.  The
    ``proof_source`` independently re-reads the current proof sources on every
    invocation; its mapping includes ``state_content_proof_capability`` only
    when the proof-v1 schema detector verified that contract for that read.
    """
    if not _valid_evidence_request(evidence_request):
        return ExactShadowPublicationResult(False, "evidence_request_invalid")
    if not callable(getattr(evidence_store, "record", None)):
        return ExactShadowPublicationResult(False, "evidence_store_unavailable")
    if not isinstance(proof_source, ExactShadowCurrentProofSource):
        return ExactShadowPublicationResult(False, "current_proof_source_unavailable")
    try:
        current_supplier = proof_source.current_supplier(exact_match)
    except Exception:
        return ExactShadowPublicationResult(False, "current_proof_source_unavailable")

    before = _read_current(current_supplier)
    if not _proof_is_publishable(exact_match, before):
        return ExactShadowPublicationResult(False, "current_proof_unavailable")

    try:
        publication = publish_settled_conversation_state(
            receipt_store=receipt_store,
            view_state_store=view_state_store,
            canonical_messages=canonical_messages,
            current_supplier=current_supplier,
            shadow_match=exact_match,
        )
    except Exception:
        return ExactShadowPublicationResult(False, "publication_failed")

    # The publisher repeats its own receipt/projection proof checks.  This
    # independent re-read is specifically for evidence: a race after receipt
    # publication must never become a qualifying shadow sample.
    after = _read_current(current_supplier)
    if not _proof_is_publishable(exact_match, after):
        return ExactShadowPublicationResult(
            True,
            "published_evidence_skipped_current_changed",
            publication=publication,
        )

    proof = ShadowProofInput(
        implementation_id=evidence_request.implementation_id,
        schema_id=evidence_request.schema_id,
        profile=exact_match.profile,
        request_generation=evidence_request.request_generation,
        candidate_complete=True,
        oracle_complete=True,
        lineage_unchanged=True,
        gates_passed=True,
    )
    try:
        evidence = evidence_store.record(proof)
    except Exception:
        return ExactShadowPublicationResult(
            True,
            "published_evidence_unavailable",
            publication=publication,
        )
    return ExactShadowPublicationResult(
        True,
        "published_and_recorded",
        publication=publication,
        evidence=evidence,
    )
