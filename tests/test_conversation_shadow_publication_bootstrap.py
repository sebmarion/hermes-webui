"""Contracts for exact-shadow settlement publication bootstrap."""

from __future__ import annotations

import json

import pytest

from api.bounded_conversation_integration import (
    exact_visible_digest,
    prove_exact_shadow_match_for_current,
)
from api.conversation_receipts import (
    ConversationReceiptStore,
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
)
from api.conversation_view_state import ConversationViewStateStore


def _current(*, proof_generation: int = 7):
    return {
        "profile": "default",
        "root_id": "root",
        "member_ids": ("root", "tip"),
        "lineage_fingerprint": "sha256:" + ("a" * 64),
        "canonical_sidecar_id": "root",
        "lineage_sidecar_proof": (
            ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
            ("tip", "missing"),
        ),
        "sidecar_generation": 4,
        "sidecar_stat": ("/profiles/default/root.json", 10, 20, 30),
        "truncation_watermark": 12.5,
        "state_message_watermark": (901, 123.0),
        "state_content_proof": (
            "agent_target_content_epoch_v1",
            (("root", 42), ("tip", proof_generation)),
        ),
        "state_content_proof_capability": VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
        "settled_display_message_count": 2,
        "visible_transcript_digest": "sha256:" + ("0" * 64),
    }


def _messages():
    return (
        {"role": "user", "content": "ship it", "timestamp": 120.0},
        {
            "role": "tool",
            "timestamp": 123.0,
            "content": json.dumps(
                {
                    "todos": [
                        {"id": "todo-1", "content": "ship it", "status": "pending"}
                    ],
                    "summary": {"total": 1},
                }
            ),
        },
    )


def _match(current, messages):
    current["visible_transcript_digest"] = exact_visible_digest(messages)
    return prove_exact_shadow_match_for_current(
        current=current,
        candidate_messages=messages,
        oracle_messages=messages,
        candidate_count=current["settled_display_message_count"],
        oracle_count=current["settled_display_message_count"],
    )


class _EvidenceStore:
    def __init__(self):
        self.recorded = []

    def record(self, proof):
        self.recorded.append(proof)
        return "recorded"


def _test_proof_source(tmp_path):
    from api.conversation_shadow_publication import ExactShadowCurrentProofSource
    from tests.test_bounded_conversation_integration import _route_target

    return ExactShadowCurrentProofSource.from_sources(
        target=_route_target(),
        profile="default",
        db_path=tmp_path / "state.db",
        expected_database_identity=(str(tmp_path / "state.db"), None, None),
        sidecar_dir=tmp_path / "sidecars",
    )


def test_exact_shadow_bootstrap_publishes_then_records_only_content_free_evidence(tmp_path, monkeypatch):
    import api.conversation_shadow_publication as bootstrap
    from api.conversation_shadow_publication import (
        ShadowPublicationEvidenceRequest,
        publish_exact_shadow_settlement,
    )

    current = _current()
    messages = _messages()
    evidence = _EvidenceStore()
    monkeypatch.setattr(
        bootstrap,
        "read_unpublished_current_proof_from_sources",
        lambda **_kwargs: current,
    )
    published = publish_exact_shadow_settlement(
        receipt_store=ConversationReceiptStore(tmp_path),
        view_state_store=ConversationViewStateStore(tmp_path),
        canonical_messages=messages,
        proof_source=_test_proof_source(tmp_path),
        exact_match=_match(current, messages),
        evidence_store=evidence,
        evidence_request=ShadowPublicationEvidenceRequest(
            implementation_id="bounded-view-v1",
            schema_id="agent-proof-v1",
            request_generation=99,
        ),
    )

    assert published.published is True
    assert published.reason == "published_and_recorded"
    assert len(evidence.recorded) == 1
    proof = evidence.recorded[0]
    assert proof.profile == "default"
    assert proof.request_generation == 99
    assert proof.candidate_complete is True
    assert proof.oracle_complete is True
    assert proof.lineage_unchanged is True
    assert proof.gates_passed is True
    assert proof.difference_reasons == ()
    assert "ship it" not in repr(proof)


def test_exact_shadow_bootstrap_never_publishes_or_records_without_verified_proof_capability(tmp_path, monkeypatch):
    import api.conversation_shadow_publication as bootstrap
    from api.conversation_shadow_publication import (
        ShadowPublicationEvidenceRequest,
        publish_exact_shadow_settlement,
    )

    current = _current()
    messages = _messages()
    match = _match(current, messages)
    current.pop("state_content_proof_capability")
    evidence = _EvidenceStore()
    monkeypatch.setattr(
        bootstrap,
        "read_unpublished_current_proof_from_sources",
        lambda **_kwargs: current,
    )

    result = publish_exact_shadow_settlement(
        receipt_store=ConversationReceiptStore(tmp_path),
        view_state_store=ConversationViewStateStore(tmp_path),
        canonical_messages=messages,
        proof_source=_test_proof_source(tmp_path),
        exact_match=match,
        evidence_store=evidence,
        evidence_request=ShadowPublicationEvidenceRequest(
            implementation_id="bounded-view-v1",
            schema_id="agent-proof-v1",
            request_generation=99,
        ),
    )

    assert result.published is False
    assert result.reason == "current_proof_unavailable"
    assert evidence.recorded == []


def test_exact_shadow_bootstrap_drops_evidence_when_current_proof_changes_after_publication(
    tmp_path, monkeypatch
):
    import api.conversation_shadow_publication as bootstrap
    from api.conversation_shadow_publication import (
        ShadowPublicationEvidenceRequest,
        publish_exact_shadow_settlement,
    )

    stable = _current()
    messages = _messages()
    match = _match(stable, messages)
    raced = _current(proof_generation=8)
    raced["visible_transcript_digest"] = stable["visible_transcript_digest"]
    states = iter((stable, raced))
    evidence = _EvidenceStore()
    published_marker = object()
    monkeypatch.setattr(
        bootstrap,
        "publish_settled_conversation_state",
        lambda **_kwargs: published_marker,
    )
    monkeypatch.setattr(
        bootstrap,
        "read_unpublished_current_proof_from_sources",
        lambda **_kwargs: next(states),
    )

    result = publish_exact_shadow_settlement(
        receipt_store=ConversationReceiptStore(tmp_path),
        view_state_store=ConversationViewStateStore(tmp_path),
        canonical_messages=messages,
        proof_source=_test_proof_source(tmp_path),
        exact_match=match,
        evidence_store=evidence,
        evidence_request=ShadowPublicationEvidenceRequest(
            implementation_id="bounded-view-v1",
            schema_id="agent-proof-v1",
            request_generation=99,
        ),
    )

    assert result.publication is published_marker
    assert result.published is True
    assert result.reason == "published_evidence_skipped_current_changed"
    assert evidence.recorded == []


def test_exact_shadow_bootstrap_fails_closed_when_the_publisher_raises_unexpectedly(
    tmp_path, monkeypatch
):
    import api.conversation_shadow_publication as bootstrap
    from api.conversation_shadow_publication import (
        ShadowPublicationEvidenceRequest,
        publish_exact_shadow_settlement,
    )

    current = _current()
    messages = _messages()
    evidence = _EvidenceStore()
    monkeypatch.setattr(
        bootstrap,
        "publish_settled_conversation_state",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    monkeypatch.setattr(
        bootstrap,
        "read_unpublished_current_proof_from_sources",
        lambda **_kwargs: current,
    )

    result = publish_exact_shadow_settlement(
        receipt_store=ConversationReceiptStore(tmp_path),
        view_state_store=ConversationViewStateStore(tmp_path),
        canonical_messages=messages,
        proof_source=_test_proof_source(tmp_path),
        exact_match=_match(current, messages),
        evidence_store=evidence,
        evidence_request=ShadowPublicationEvidenceRequest(
            implementation_id="bounded-view-v1",
            schema_id="agent-proof-v1",
            request_generation=99,
        ),
    )

    assert result.published is False
    assert result.reason == "publication_failed"
    assert evidence.recorded == []


def test_exact_shadow_bootstrap_never_records_evidence_for_a_different_canonical_snapshot(
    tmp_path, monkeypatch
):
    import api.conversation_shadow_publication as bootstrap
    from api.conversation_shadow_publication import (
        ShadowPublicationEvidenceRequest,
        publish_exact_shadow_settlement,
    )

    current = _current()
    accepted = _messages()
    match = _match(current, accepted)
    evidence = _EvidenceStore()
    receipts = ConversationReceiptStore(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "read_unpublished_current_proof_from_sources",
        lambda **_kwargs: current,
    )

    result = publish_exact_shadow_settlement(
        receipt_store=receipts,
        view_state_store=ConversationViewStateStore(tmp_path),
        canonical_messages=(
            {"role": "user", "content": "different", "timestamp": 120.0},
            {"role": "assistant", "content": "different", "timestamp": 123.0},
        ),
        proof_source=_test_proof_source(tmp_path),
        exact_match=match,
        evidence_store=evidence,
        evidence_request=ShadowPublicationEvidenceRequest(
            implementation_id="bounded-view-v1",
            schema_id="agent-proof-v1",
            request_generation=99,
        ),
    )

    assert result.published is False
    assert result.reason == "publication_failed"
    assert evidence.recorded == []
    assert receipts.load("default", "root") is None


def test_exact_shadow_current_proof_source_rereads_proof_v1_and_sidecars(tmp_path):
    from api.agent_sessions import shared_state_db_identity
    from api.bounded_conversation_integration import prove_exact_shadow_match
    from api.bounded_sidecar_proof import prove_sidecar_lineage
    from api.conversation_shadow_publication import ExactShadowCurrentProofSource
    from tests.test_bounded_conversation_integration import (
        _route_target,
        _write_proof_v1_database,
        _write_route_sidecar,
    )

    db_path = _write_proof_v1_database(tmp_path)
    sidecar_dir = tmp_path / "sidecars"
    sidecar_dir.mkdir()
    _write_route_sidecar(sidecar_dir, "root")
    _write_route_sidecar(sidecar_dir, "tip")
    target = _route_target()
    sidecars = prove_sidecar_lineage(sidecar_dir, target.member_ids, "default")
    messages = _messages()
    exact_match = prove_exact_shadow_match(
        target=target,
        profile="default",
        sidecars=sidecars,
        sidecar_paths={
            member_id: str(sidecar_dir / f"{member_id}.json")
            for member_id in target.member_ids
        },
        target_content_proof=(("root", 1), ("tip", 1)),
        candidate_messages=messages,
        oracle_messages=messages,
        candidate_count=2,
        oracle_count=2,
    )
    source = ExactShadowCurrentProofSource.from_sources(
        target=target,
        profile="default",
        db_path=db_path,
        expected_database_identity=shared_state_db_identity(db_path),
        sidecar_dir=sidecar_dir,
    )

    current = source.current_supplier(exact_match)()

    assert current["state_content_proof_capability"] is VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
    assert current["member_ids"] == ("root", "tip")
    assert current["settled_display_message_count"] == 2
    assert current["visible_transcript_digest"] == exact_match.candidate_visible_digest

    (sidecar_dir / "tip.json").write_text(
        json.dumps(
            {
                "session_id": "tip",
                "profile": "default",
                "sidecar_generation": 5,
                "truncation_watermark": 12.5,
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="exact shadow match is not current"):
        source.current_supplier(exact_match)()
