"""Focused contracts for the route-facing bounded conversation adapter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from api.bounded_session_view import ProofCapability
from api.bounded_sidecar_proof import (
    SidecarLineageProof,
    SidecarMetadataProof,
    SidecarStatSignature,
)
from api.conversation_receipts import VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY


def _digest(char: str) -> str:
    return "sha256:" + (char * 64)


def _resolution():
    from api.agent_sessions import SharedSessionResolution

    return SharedSessionResolution(
        requested_id="root",
        canonical_id="tip",
        root_id="root",
        tip_id="tip",
        member_ids=("root", "tip"),
        canonical_row={"id": "tip"},
        lineage_fingerprint=_digest("a"),
        global_projection_generation_hint=7,
        mode="navigation",
        status="found",
        database_identity=("/safe/state.db", 10, 20),
    )


def _sidecars():
    signature = SidecarStatSignature(1, 2, 0o100600, 44, 55, 66)
    return SidecarLineageProof(
        member_ids=("root", "tip"),
        members=(
            SidecarMetadataProof("root", "missing", "sidecar_missing"),
            SidecarMetadataProof("tip", "present", "ok", signature, 4, 12.5),
        ),
    )


def _proof_inputs():
    from api.bounded_conversation_integration import StateTailWatermark

    return (
        (("root", 9), ("tip", 11)),
        (
            StateTailWatermark("root", 100, 20.0),
            StateTailWatermark("tip", 101, 21.0),
        ),
    )


def _visible_messages():
    return (
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    )


def _shadow_match(target, generations, *, sidecars=None):
    from api.bounded_conversation_integration import prove_exact_shadow_match

    messages = _visible_messages()
    return prove_exact_shadow_match(
        target=target,
        profile="default",
        sidecars=_sidecars() if sidecars is None else sidecars,
        sidecar_paths={"tip": "/safe/tip.json"},
        target_content_proof=generations,
        candidate_messages=messages,
        oracle_messages=messages,
        candidate_count=2,
        oracle_count=2,
    )


def _write_proof_v1_database(tmp_path):
    """Create the exact disk-backed schema required by the route reader."""
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                message_generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                active INTEGER
            );
            CREATE INDEX idx_messages_session_timestamp_id
                ON messages(session_id, timestamp, id);
            CREATE TABLE agent_contract_capabilities (
                capability TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );
            INSERT INTO agent_contract_capabilities
                VALUES ('target_message_generation', 1);
            CREATE TRIGGER proof_messages_insert AFTER INSERT ON messages BEGIN
                UPDATE sessions SET message_generation = message_generation + 1 WHERE id = NEW.session_id;
            END;
            CREATE TRIGGER proof_messages_delete AFTER DELETE ON messages BEGIN
                UPDATE sessions SET message_generation = message_generation + 1 WHERE id = OLD.session_id;
            END;
            CREATE TRIGGER proof_messages_update_same_session AFTER UPDATE ON messages
            WHEN OLD.session_id = NEW.session_id BEGIN
                UPDATE sessions SET message_generation = message_generation + 1 WHERE id = NEW.session_id;
            END;
            CREATE TRIGGER proof_messages_update_moved_session AFTER UPDATE ON messages
            WHEN OLD.session_id != NEW.session_id BEGIN
                UPDATE sessions SET message_generation = message_generation + 1 WHERE id = OLD.session_id;
                UPDATE sessions SET message_generation = message_generation + 1 WHERE id = NEW.session_id;
            END;
            """
        )
        connection.executemany(
            "INSERT INTO sessions(id) VALUES (?)", [("root",), ("tip",)]
        )
        connection.executemany(
            "INSERT INTO messages(id, session_id, role, content, timestamp, active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (7, "root", "user", "root message", 10.0, 1),
                (8, "tip", "assistant", "tip message", 20.0, 1),
            ],
        )
    return database


def _write_route_sidecar(directory, session_id="tip", *, file_session_id=None):
    payload = {
        "session_id": session_id,
        "profile": "default",
        "sidecar_generation": 4,
        "truncation_watermark": 12.5,
        "messages": [],
    }
    path = directory / f"{file_session_id or session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _route_target():
    from api.bounded_session_view import ResolvedTarget

    return ResolvedTarget(
        requested_id="root",
        canonical_id="tip",
        root_id="root",
        member_ids=("root", "tip"),
        lineage_fingerprint=_digest("a"),
        database_identity_digest=_digest("b"),
        global_generation_hint=None,
        source_mode="state_db",
    )


def _route_receipt():
    from api.conversation_receipts import ConversationReceipt

    return ConversationReceipt(
        version=1,
        profile="default",
        root_id="root",
        member_ids=("root", "tip"),
        lineage_fingerprint=_digest("a"),
        canonical_sidecar_id="tip",
        lineage_sidecar_proof=(("root", "missing"), ("tip", "missing")),
        sidecar_generation=0,
        sidecar_stat=("/proof/tip.json", 0, 0, 0),
        truncation_watermark=None,
        state_message_watermark=(8, 20.0),
        state_content_proof=(
            "agent_target_content_epoch_v1",
            (("root", 1), ("tip", 1)),
        ),
        settled_display_message_count=2,
        visible_transcript_digest=_digest("c"),
        todo_projection_generation=1,
        todo_projection_watermark=(8, 20.0),
        todo_projection_target_content_proof_digest=_digest("d"),
        todo_projection_snapshot_digest=_digest("e"),
    )


def _projection_snapshot():
    return {
        "todos": [],
        "summary": {},
        "version": 1,
        "ts": 20.0,
    }


def _save_projection(store, *, proof_digest):
    from api.conversation_view_state import MessageWatermark

    result = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=8),
        target_content_proof_digest=proof_digest,
        snapshot=_projection_snapshot(),
    )
    assert result.saved is True


def test_read_bounded_state_snapshot_accepts_only_the_complete_disk_proof_v1_contract(tmp_path):
    from api.agent_sessions import shared_state_db_identity
    from api.bounded_conversation_integration import (
        StateTailWatermark,
        read_bounded_state_snapshot,
    )

    database = _write_proof_v1_database(tmp_path)
    snapshot = read_bounded_state_snapshot(
        db_path=database,
        member_ids=("root", "tip"),
        expected_database_identity=shared_state_db_identity(database),
    )

    assert snapshot.capability.available is True
    assert snapshot.capability.capability_marker is VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
    assert snapshot.target_content_proof == (("root", 1), ("tip", 1))
    assert snapshot.state_tail_watermarks == (
        StateTailWatermark("root", 7, 10.0),
        StateTailWatermark("tip", 8, 20.0),
    )


def test_read_bounded_state_snapshot_fails_closed_on_identity_mismatch_and_midread_race(
    tmp_path, monkeypatch
):
    import api.bounded_conversation_integration as integration
    from api.agent_sessions import shared_state_db_identity

    database = _write_proof_v1_database(tmp_path)
    identity = shared_state_db_identity(database)

    with pytest.raises(integration.IntegrationProofError, match="identity changed"):
        integration.read_bounded_state_snapshot(
            db_path=database,
            member_ids=("root", "tip"),
            expected_database_identity=(identity[0], identity[1], identity[2] + 1),
        )

    observed_identities = iter((identity, (identity[0], identity[1], identity[2] + 1)))
    monkeypatch.setattr(
        integration,
        "shared_state_db_identity",
        lambda _path: next(observed_identities),
    )
    with pytest.raises(integration.IntegrationProofError, match="identity changed"):
        integration.read_bounded_state_snapshot(
            db_path=database,
            member_ids=("root", "tip"),
            expected_database_identity=identity,
        )


def test_shadow_readiness_from_evidence_never_promotes_malformed_values_to_ready():
    from api.bounded_conversation_integration import shadow_readiness_from_evidence

    malformed_evidence = (
        object(),
        SimpleNamespace(ready=1, reason="ready"),
        SimpleNamespace(ready=True, reason=""),
        SimpleNamespace(ready=True, reason="contains\0nul"),
    )

    for evidence in malformed_evidence:
        readiness = shadow_readiness_from_evidence(evidence)
        assert readiness.ready is False
        assert readiness.reason == "shadow_readiness_invalid"
        assert readiness.cohort == "invalid"


def test_read_current_proof_from_sources_rejects_projection_and_sidecar_mismatches(
    tmp_path,
):
    from api.agent_sessions import shared_state_db_identity
    from api.bounded_conversation_integration import (
        CONTENT_PROOF_KIND,
        IntegrationProofError,
        read_current_proof_from_sources,
    )
    from api.conversation_receipts import canonical_proof_digest
    from api.conversation_view_state import ConversationViewStateStore

    database = _write_proof_v1_database(tmp_path)
    identity = shared_state_db_identity(database)
    target = _route_target()
    sidecar_dir = tmp_path / "sidecars"
    sidecar_dir.mkdir()
    _write_route_sidecar(sidecar_dir)
    proof_digest = canonical_proof_digest(
        target.lineage_fingerprint,
        (CONTENT_PROOF_KIND, (("root", 1), ("tip", 1))),
    )
    receipt = _route_receipt()

    matched_store = ConversationViewStateStore(tmp_path / "matched-projection")
    _save_projection(matched_store, proof_digest=proof_digest)
    current = read_current_proof_from_sources(
        target=target,
        profile="default",
        db_path=database,
        expected_database_identity=identity,
        sidecar_dir=sidecar_dir,
        receipt=receipt,
        view_state_store=matched_store,
    )
    assert current.state_content_proof == (
        CONTENT_PROOF_KIND,
        (("root", 1), ("tip", 1)),
    )
    assert current.canonical_sidecar_id == "tip"

    mismatched_store = ConversationViewStateStore(tmp_path / "mismatched-projection")
    _save_projection(mismatched_store, proof_digest=_digest("f"))
    with pytest.raises(IntegrationProofError, match="durable todo projection"):
        read_current_proof_from_sources(
            target=target,
            profile="default",
            db_path=database,
            expected_database_identity=identity,
            sidecar_dir=sidecar_dir,
            receipt=receipt,
            view_state_store=mismatched_store,
        )

    _write_route_sidecar(sidecar_dir, session_id="other", file_session_id="tip")
    with pytest.raises(IntegrationProofError, match="sidecar lineage"):
        read_current_proof_from_sources(
            target=target,
            profile="default",
            db_path=database,
            expected_database_identity=identity,
            sidecar_dir=sidecar_dir,
            receipt=receipt,
            view_state_store=matched_store,
        )


def test_strict_gate_requires_every_explicit_receipt_view_shadow_and_cursor_setting():
    from api.bounded_conversation_integration import (
        ShadowReadiness,
        evaluate_public_cursor_gate,
    )

    environment = {
        "HERMES_WEBUI_RECEIPT_FAST_PATH": "1",
        "HERMES_WEBUI_DERIVED_VIEW_STATE_READS": "1",
        "HERMES_WEBUI_BOUNDED_VIEW_SHADOW": "1",
        "HERMES_WEBUI_MESSAGE_CURSOR_V1": "on",
    }
    ready = ShadowReadiness(True, "ready", cohort="proof-v1")
    capability = ProofCapability(True, "valid", VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY)

    assert evaluate_public_cursor_gate(environment, capability, True, ready).public_cursor is True
    assert evaluate_public_cursor_gate(
        {**environment, "HERMES_WEBUI_MESSAGE_CURSOR_V1": "shadow"},
        capability,
        True,
        ready,
    ).reason == "cursor_mode_not_on"
    assert evaluate_public_cursor_gate(
        {**environment, "HERMES_WEBUI_BOUNDED_VIEW_SHADOW": "true"},
        capability,
        True,
        ready,
    ).reason == "shadow_gate_disabled"
    assert evaluate_public_cursor_gate(environment, capability, False, ready).reason == "receipt_not_durable"
    assert evaluate_public_cursor_gate(
        environment,
        capability,
        True,
        ShadowReadiness(False, "insufficient_samples", cohort="proof-v1"),
    ).reason == "shadow_not_ready"


def test_readonly_capability_detector_never_mutates_database_and_rejects_nonproof_schema(tmp_path):
    from api.bounded_conversation_integration import detect_readonly_proof_capability

    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        before = connection.execute("PRAGMA schema_version").fetchone()[0]

    result = detect_readonly_proof_capability(database)

    with sqlite3.connect(database) as connection:
        after = connection.execute("PRAGMA schema_version").fetchone()[0]
    assert result.available is False
    assert result.reason == "unverifiable_current_state"
    assert before == after


def test_resolution_and_lineage_sidecars_convert_to_one_strict_target_and_current_proof():
    from api.bounded_conversation_integration import (
        DurableTodoProjection,
        build_current_proof,
        exact_visible_digest,
        resolved_target_from_shared_resolution,
    )

    target = resolved_target_from_shared_resolution(_resolution())
    generations, tails = _proof_inputs()
    proof = build_current_proof(
        target=target,
        profile="default",
        sidecars=_sidecars(),
        sidecar_paths={"tip": "/safe/tip.json"},
        target_content_proof=generations,
        state_tail_watermarks=tails,
        settled_display_message_count=2,
        visible_transcript_digest=exact_visible_digest(_visible_messages()),
        todo_projection=DurableTodoProjection(3, 101, 21.0, _digest("c")),
        shadow_match=_shadow_match(target, generations),
    )

    assert target.member_ids == ("root", "tip")
    assert proof.lineage_sidecar_proof == (
        ("root", "missing"),
        ("tip", (4, ("/safe/tip.json", 1, 2, 0o100600, 44, 55, 66))),
    )
    assert proof.state_message_watermark == (101, 21.0)
    assert proof.state_content_proof == ("agent_target_content_epoch_v1", generations)
    assert proof.to_mapping()["todo_projection_watermark"] == (101, 21.0)
    receipt = proof.receipt_candidate()
    assert receipt.canonical_sidecar_id == "tip"
    assert receipt.lineage_sidecar_proof == proof.lineage_sidecar_proof


def test_current_proof_refuses_missing_or_reordered_lineage_unproven_shadow_and_undurable_todo():
    from api.bounded_conversation_integration import (
        DurableTodoProjection,
        ExactShadowMatch,
        IntegrationProofError,
        build_current_proof,
        exact_visible_digest,
        resolved_target_from_shared_resolution,
    )

    target = resolved_target_from_shared_resolution(_resolution())
    generations, tails = _proof_inputs()
    kwargs = dict(
        target=target,
        profile="default",
        sidecars=_sidecars(),
        sidecar_paths={"tip": "/safe/tip.json"},
        target_content_proof=generations,
        state_tail_watermarks=tails,
        settled_display_message_count=2,
        visible_transcript_digest=exact_visible_digest(_visible_messages()),
        todo_projection=DurableTodoProjection(3, 101, 21.0, _digest("c")),
        shadow_match=_shadow_match(target, generations),
    )
    with __import__("pytest").raises(IntegrationProofError, match="member order"):
        build_current_proof(**{**kwargs, "sidecars": replace(_sidecars(), member_ids=("tip", "root"))})
    with __import__("pytest").raises(IntegrationProofError, match="exact shadow"):
        build_current_proof(
            **{
                **kwargs,
                "shadow_match": ExactShadowMatch(
                    "default",
                    "root",
                    target.lineage_fingerprint,
                    _digest("e"),
                    _digest("f"),
                    _digest("b"),
                    _digest("b"),
                    2,
                    2,
                ),
            }
        )
    with __import__("pytest").raises(IntegrationProofError, match="todo projection"):
        build_current_proof(
            **{**kwargs, "todo_projection": DurableTodoProjection(0, 101, 21.0, _digest("c"))}
        )


def test_exact_visible_digest_is_content_complete_and_order_sensitive():
    from api.bounded_conversation_integration import IntegrationProofError, exact_visible_digest

    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    assert exact_visible_digest(messages) == exact_visible_digest([dict(row) for row in messages])
    assert exact_visible_digest(messages) != exact_visible_digest(list(reversed(messages)))
    with __import__("pytest").raises(IntegrationProofError, match="byte limit"):
        exact_visible_digest([{"role": "user", "content": "x" * (3 * 1024 * 1024)}])


def test_active_runs_adapter_snapshots_and_reverifies_only_the_exact_owner():
    from api.bounded_conversation_integration import ActiveRunsOwnerAdapter

    active_runs = {
        "run-1": {"session_id": "tip", "profile": "default", "run_id": "run-1"}
    }
    adapter = ActiveRunsOwnerAdapter(active_runs)
    owner = adapter.snapshot(profile="default", session_id="tip")

    assert owner is not None
    assert (owner.profile, owner.session_id, owner.run_id, owner.active) == (
        "default",
        "tip",
        "run-1",
        True,
    )
    assert owner.capability_token.startswith("run-1:")
    assert adapter.verifier(owner) is True
    active_runs["run-1"] = {"session_id": "tip", "profile": "default", "run_id": "rotated"}
    assert adapter.verifier(owner) is False
    replacement = adapter.snapshot(profile="default", session_id="tip")
    assert replacement is not None
    assert replacement.run_id == "rotated"
    assert replacement != owner


def test_shadow_match_is_bound_to_target_content_epoch_and_sidecar_descriptor():
    from api.bounded_conversation_integration import (
        DurableTodoProjection,
        IntegrationProofError,
        build_current_proof,
        exact_visible_digest,
        resolved_target_from_shared_resolution,
    )

    target = resolved_target_from_shared_resolution(_resolution())
    generations, tails = _proof_inputs()
    match = _shadow_match(target, generations)
    common = {
        "target": target,
        "profile": "default",
        "sidecars": _sidecars(),
        "sidecar_paths": {"tip": "/safe/tip.json"},
        "state_tail_watermarks": tails,
        "settled_display_message_count": 2,
        "visible_transcript_digest": exact_visible_digest(_visible_messages()),
        "todo_projection": DurableTodoProjection(3, 101, 21.0, _digest("c")),
        "shadow_match": match,
    }

    with __import__("pytest").raises(IntegrationProofError, match="exact shadow"):
        build_current_proof(
            **common,
            target_content_proof=(("root", 10), ("tip", 11)),
        )
    changed_signature = SidecarStatSignature(9, 8, 0o100400, 44, 55, 66)
    changed_sidecars = SidecarLineageProof(
        member_ids=("root", "tip"),
        members=(
            SidecarMetadataProof("root", "missing", "sidecar_missing"),
            SidecarMetadataProof("tip", "present", "ok", changed_signature, 4, 12.5),
        ),
    )
    with __import__("pytest").raises(IntegrationProofError, match="exact shadow"):
        build_current_proof(
            **{**common, "sidecars": changed_sidecars},
            target_content_proof=generations,
        )


def test_active_runs_adapter_normalizes_root_profile_and_caps_registry_and_proofs():
    from api.bounded_conversation_integration import ActiveRunsOwnerAdapter

    default_run = {"run-1": {"session_id": "tip", "profile": None}}
    adapter = ActiveRunsOwnerAdapter(default_run)
    owner = adapter.snapshot(profile="default", session_id="tip")
    assert owner is not None
    assert adapter.verifier(owner) is True

    oversized = {
        f"run-{index}": {"session_id": f"session-{index}", "profile": "default"}
        for index in range(257)
    }
    assert ActiveRunsOwnerAdapter(oversized).snapshot(
        profile="default", session_id="session-0"
    ) is None

    rotating = {"run": {"session_id": "tip", "profile": "default"}}
    bounded = ActiveRunsOwnerAdapter(rotating)
    for index in range(300):
        rotating["run"] = {
            "session_id": "tip",
            "profile": "default",
            "run_id": f"run-{index}",
        }
        assert bounded.snapshot(profile="default", session_id="tip") is not None
    assert len(bounded._proofs) <= 256


class _ExplodingOversizedSequence(Sequence):
    def __len__(self):
        return 257

    def __getitem__(self, _index):
        raise AssertionError("oversized sequence was consumed before rejection")


def test_bounded_adapters_reject_oversized_sequences_before_materializing_them():
    from types import SimpleNamespace

    from api.bounded_conversation_integration import (
        IntegrationProofError,
        resolved_target_from_shared_resolution,
    )

    resolution = _resolution()
    malformed = SimpleNamespace(**{
        **resolution.__dict__,
        "member_ids": _ExplodingOversizedSequence(),
    })
    with __import__("pytest").raises(IntegrationProofError):
        resolved_target_from_shared_resolution(malformed)
