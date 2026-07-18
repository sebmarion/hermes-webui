"""Contract tests for the settled todo-projection/receipt publication transaction."""

from __future__ import annotations

import json

import pytest

from api.conversation_receipts import (
    ReceiptStoreError,
    ConversationReceiptStore,
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
    canonical_proof_digest,
    validate_receipt,
)
from api.conversation_state_publication import (
    ConversationStatePublicationError,
    publish_settled_conversation_state,
)
from api.conversation_view_state import ConversationViewStateStore, MessageWatermark
from api.bounded_conversation_integration import (
    exact_visible_digest,
    prove_exact_shadow_match_for_current,
)


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


def test_publication_carries_complete_lineage_sidecar_proof(tmp_path):
    receipts, projections = _stores(tmp_path)
    current = _current()
    current.update(
        {
            "canonical_sidecar_id": "root",
            "lineage_sidecar_proof": (
                (
                    "root",
                    (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30)),
                ),
                ("tip", "missing"),
            ),
        }
    )

    published = publish_settled_conversation_state(
        receipt_store=receipts,
        view_state_store=projections,
        canonical_messages=_todo_messages(),
        current_supplier=lambda: current,
        shadow_match=_shadow(current, _todo_messages()),
    )

    assert published.receipt.canonical_sidecar_id == "root"
    assert published.receipt.lineage_sidecar_proof == current["lineage_sidecar_proof"]


def _todo_messages():
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


def _stores(tmp_path):
    return ConversationReceiptStore(tmp_path), ConversationViewStateStore(tmp_path)


def _shadow(current, messages):
    current["visible_transcript_digest"] = exact_visible_digest(messages)
    return prove_exact_shadow_match_for_current(
        current=current,
        candidate_messages=messages,
        oracle_messages=messages,
        candidate_count=current["settled_display_message_count"],
        oracle_count=current["settled_display_message_count"],
    )


def _assert_no_usable_receipt(store):
    try:
        receipt = store.load("default", "root")
    except ReceiptStoreError:
        return
    assert receipt is None


def test_publication_requires_a_typed_shadow_match_bound_to_the_same_epoch(tmp_path):
    receipts, projections = _stores(tmp_path)
    current = _current()
    stale = _current(proof_generation=6)
    stale_match = _shadow(stale, _todo_messages())

    for match in (None, stale_match):
        with pytest.raises(
            ConversationStatePublicationError,
            match="exact shadow acceptance",
        ):
            publish_settled_conversation_state(
                receipt_store=receipts,
                view_state_store=projections,
                canonical_messages=_todo_messages(),
                current_supplier=lambda: current,
                shadow_match=match,
            )

    _assert_no_usable_receipt(receipts)
    assert projections.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=canonical_proof_digest(
            current["lineage_fingerprint"], current["state_content_proof"]
        ),
        watermark=MessageWatermark(timestamp=123.0, message_id=901),
    ) is None


def test_publication_derives_empty_tombstone_and_binds_exact_projection(tmp_path):
    receipts, projections = _stores(tmp_path)
    current = _current()

    published = publish_settled_conversation_state(
        receipt_store=receipts,
        view_state_store=projections,
        canonical_messages=(),
        current_supplier=lambda: current,
        shadow_match=_shadow(current, ()),
    )

    assert published.projection.empty_tombstone is True
    assert published.projection.snapshot["todos"] == []
    assert published.receipt.todo_projection_generation == published.projection.generation
    assert published.receipt.todo_projection_watermark == (901, 123.0)
    assert (
        published.receipt.todo_projection_target_content_proof_digest
        == canonical_proof_digest(
            current["lineage_fingerprint"], current["state_content_proof"]
        )
    )
    assert published.receipt.todo_projection_snapshot_digest == published.projection.snapshot_digest
    assert receipts.load("default", "root") == published.receipt


@pytest.mark.parametrize("fault_stage", ("after_current_read", "after_todo_cas", "before_receipt_publish"))
def test_partial_publication_fault_never_leaves_a_usable_receipt(tmp_path, fault_stage):
    receipts, projections = _stores(tmp_path)
    current = _current()

    def crash(stage):
        if stage == fault_stage:
            raise RuntimeError(f"crash at {stage}")

    with pytest.raises(ConversationStatePublicationError, match=fault_stage):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: current,
            shadow_match=_shadow(current, _todo_messages()),
            fault_hook=crash,
        )

    _assert_no_usable_receipt(receipts)


def test_proof_race_after_projection_cas_fails_closed_without_receipt(tmp_path):
    receipts, projections = _stores(tmp_path)
    stable = _current(proof_generation=7)
    raced = _current(proof_generation=8)
    reads = iter((stable, raced))

    with pytest.raises(ConversationStatePublicationError, match="current proof"):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: next(reads),
            shadow_match=_shadow(stable, _todo_messages()),
        )

    _assert_no_usable_receipt(receipts)
    stale = projections.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=canonical_proof_digest(
            stable["lineage_fingerprint"], stable["state_content_proof"]
        ),
        watermark=MessageWatermark(timestamp=123.0, message_id=901),
    )
    assert stale is not None


def test_lineage_sidecar_race_after_projection_cas_fails_closed_without_receipt(tmp_path):
    receipts, projections = _stores(tmp_path)
    stable = _current()
    raced = _current()
    raced["lineage_sidecar_proof"] = (
        ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
        ("tip", (1, ("/profiles/default/tip.json", 121, 122, 0o600, 22, 12, 32))),
    )
    reads = iter((stable, raced))

    with pytest.raises(ConversationStatePublicationError, match="current proof"):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: next(reads),
            shadow_match=_shadow(stable, _todo_messages()),
        )

    _assert_no_usable_receipt(receipts)


def test_receipt_replace_failure_cannot_leave_a_usable_receipt(tmp_path, monkeypatch):
    receipts, projections = _stores(tmp_path)
    current = _current()

    def fail_replace(_tmp, _path):
        raise OSError("replace failed")

    monkeypatch.setattr(receipts, "_replace_prepared", fail_replace)
    with pytest.raises(ConversationStatePublicationError, match="receipt publication"):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: current,
            shadow_match=_shadow(current, _todo_messages()),
        )

    _assert_no_usable_receipt(receipts)


def test_projection_replacement_during_receipt_replace_rolls_back_receipt(
    tmp_path, monkeypatch
):
    """The receipt must not outlive the exact projection it describes."""
    receipts, projections = _stores(tmp_path)
    current = _current()
    original_replace = receipts._replace_prepared
    replacement_proof = "sha256:" + ("f" * 64)

    def replace_then_replace_projection(tmp, path):
        original_replace(tmp, path)
        projections.compare_and_swap(
            profile="default",
            root_id="root",
            watermark=MessageWatermark(timestamp=123.0, message_id=901),
            target_content_proof_digest=replacement_proof,
            snapshot={"todos": [], "summary": {}, "version": 1},
        )

    monkeypatch.setattr(receipts, "_replace_prepared", replace_then_replace_projection)
    with pytest.raises(ConversationStatePublicationError, match="receipt publication"):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: current,
            shadow_match=_shadow(current, _todo_messages()),
        )

    _assert_no_usable_receipt(receipts)
    assert projections.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=canonical_proof_digest(
            current["lineage_fingerprint"], current["state_content_proof"]
        ),
        watermark=MessageWatermark(timestamp=123.0, message_id=901),
    ) is None


def test_projection_replacement_before_receipt_validation_never_publishes_receipt(
    tmp_path,
):
    receipts, projections = _stores(tmp_path)
    current = _current()

    def replace_projection(stage):
        if stage == "after_todo_cas":
            projections.compare_and_swap(
                profile="default",
                root_id="root",
                watermark=MessageWatermark(timestamp=123.0, message_id=901),
                target_content_proof_digest="sha256:" + ("e" * 64),
                snapshot={"todos": [], "summary": {}, "version": 1},
            )

    with pytest.raises(ConversationStatePublicationError, match="current proof"):
        publish_settled_conversation_state(
            receipt_store=receipts,
            view_state_store=projections,
            canonical_messages=_todo_messages(),
            current_supplier=lambda: current,
            shadow_match=_shadow(current, _todo_messages()),
            fault_hook=replace_projection,
        )

    _assert_no_usable_receipt(receipts)


def test_receipt_is_usable_only_after_final_publication_step(tmp_path):
    receipts, projections = _stores(tmp_path)
    current = _current()
    stages = []

    published = publish_settled_conversation_state(
        receipt_store=receipts,
        view_state_store=projections,
        canonical_messages=_todo_messages(),
        current_supplier=lambda: current,
        shadow_match=_shadow(current, _todo_messages()),
        fault_hook=stages.append,
    )

    assert stages == [
        "after_current_read",
        "after_todo_cas",
        "before_receipt_publish",
        "after_receipt_publication",
    ]
    current_with_projection = dict(current)
    current_with_projection.update(
        {
            "todo_projection_generation": published.projection.generation,
            "todo_projection_watermark": (901, 123.0),
            "todo_projection_target_content_proof_digest": published.projection.target_content_proof_digest,
            "todo_projection_snapshot_digest": published.projection.snapshot_digest,
        }
    )
    assert validate_receipt(published.receipt, current=current_with_projection).valid
