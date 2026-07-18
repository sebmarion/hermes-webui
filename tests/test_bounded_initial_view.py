"""Focused proof-first contracts for the bounded initial-view core."""

from __future__ import annotations

import sqlite3
import json

import pytest


def _receipt(**changes):
    from api.conversation_receipts import ConversationReceipt, canonical_proof_digest

    values = {
        "version": 1,
        "profile": "default",
        "root_id": "root",
        "member_ids": ("root", "tip"),
        "lineage_fingerprint": "sha256:" + ("b" * 64),
        "canonical_sidecar_id": "root",
        "lineage_sidecar_proof": (
            ("root", (4, ("/state/root.json", 101, 102, 0o600, 2, 1, 3))),
            ("tip", "missing"),
        ),
        "sidecar_generation": 4,
        "sidecar_stat": ("/state/root.json", 1, 2, 3),
        "truncation_watermark": 7,
        "state_message_watermark": (99, 20.0),
        "state_content_proof": (
            "agent_target_content_epoch_v1",
            (("root", 3), ("tip", 5)),
        ),
        "settled_display_message_count": 2,
        "visible_transcript_digest": "sha256:" + ("a" * 64),
        "todo_projection_generation": 1,
        "todo_projection_watermark": (99, 20.0),
        "todo_projection_target_content_proof_digest": canonical_proof_digest(
            "sha256:" + ("b" * 64),
            ("agent_target_content_epoch_v1", (("root", 3), ("tip", 5))),
        ),
        "todo_projection_snapshot_digest": "sha256:" + ("d" * 64),
        "generation": 11,
    }
    values.update(changes)
    if "todo_projection_watermark" not in changes:
        values["todo_projection_watermark"] = values["state_message_watermark"]
    if "todo_projection_target_content_proof_digest" not in changes:
        values["todo_projection_target_content_proof_digest"] = canonical_proof_digest(
            values["lineage_fingerprint"], values["state_content_proof"]
        )
    return ConversationReceipt(**values)


def _current(receipt):
    return {
        "profile": receipt.profile,
        "root_id": receipt.root_id,
        "member_ids": receipt.member_ids,
        "lineage_fingerprint": receipt.lineage_fingerprint,
        "canonical_sidecar_id": receipt.canonical_sidecar_id,
        "lineage_sidecar_proof": receipt.lineage_sidecar_proof,
        "sidecar_generation": receipt.sidecar_generation,
        "sidecar_stat": receipt.sidecar_stat,
        "truncation_watermark": receipt.truncation_watermark,
        "state_message_watermark": receipt.state_message_watermark,
        "state_content_proof": receipt.state_content_proof,
        "settled_display_message_count": receipt.settled_display_message_count,
        "visible_transcript_digest": receipt.visible_transcript_digest,
        "todo_projection_generation": receipt.todo_projection_generation,
        "todo_projection_watermark": receipt.todo_projection_watermark,
        "todo_projection_target_content_proof_digest": receipt.todo_projection_target_content_proof_digest,
        "todo_projection_snapshot_digest": receipt.todo_projection_snapshot_digest,
    }


def _dependencies(*, receipt=None, current=None, events=None):
    from api.bounded_session_view import (
        BoundedViewDependencies,
        LegacyView,
        PageView,
        ResolvedTarget,
    )

    events = [] if events is None else events
    receipt = _receipt() if receipt is None else receipt
    current = _current(receipt) if current is None else current
    target = ResolvedTarget(
        requested_id="root",
        canonical_id="tip",
        root_id="root",
        member_ids=("root", "tip"),
        lineage_fingerprint="sha256:" + ("b" * 64),
        database_identity_digest="sha256:" + ("c" * 64),
        global_generation_hint=7,
        source_mode="state_db",
    )

    def resolve(profile, requested_id):
        events.append("resolve")
        assert (profile, requested_id) == ("default", "root")
        return target

    def current_reader(resolution, capability):
        events.append("current")
        assert resolution == target
        return dict(current)

    def receipt_loader(profile, root_id):
        events.append("receipt")
        assert (profile, root_id) == ("default", "root")
        return receipt

    def legacy_loader(resolution, limit):
        events.append("legacy")
        assert resolution == target
        return LegacyView(messages=[{"_state_db_message_id": 1, "content": "legacy"}], message_count=1)

    def page_loader(resolution, cursor, limit):
        events.append("page")
        assert resolution == target
        messages = [{"_state_db_message_id": 2, "content": "bounded"}]
        return PageView(
            messages=messages,
            has_more=True,
            visible_count=1,
            raw_rows_examined=2,
            serialized_bytes=_page_bytes(messages),
            before_boundaries=_boundaries(),
        )

    return BoundedViewDependencies(
        resolve=resolve,
        confirm_target=lambda _target: True,
        read_current=current_reader,
        load_receipt=receipt_loader,
        load_legacy=legacy_loader,
        load_page=page_loader,
        capability=lambda: _proof_capability(),
        renderable=lambda message: message.get("role") in {"user", "assistant"},
    )


def _proof_capability():
    from api.bounded_session_view import ProofCapability
    from api.conversation_receipts import VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY

    return ProofCapability(True, "valid", VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY)


def _boundaries():
    from api.session_message_paging import MessageCursorBoundary

    return (
        MessageCursorBoundary("root", 1.0, 1),
        MessageCursorBoundary("tip", 2.0, 2),
    )


def _page_bytes(messages):
    return len(
        json.dumps(tuple(messages), ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False).encode("utf-8")
    )


def _overlay_capability():
    from api.bounded_session_view import VERIFIED_RUNTIME_OVERLAY_CAPABILITY

    return VERIFIED_RUNTIME_OVERLAY_CAPABILITY


def test_missing_initial_receipt_uses_exact_legacy_before_any_page_read():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewRequest

    events = []
    dependencies = _dependencies(receipt=None, events=events)
    dependencies = dependencies.__class__(**{**dependencies.__dict__, "load_receipt": lambda *_: None})

    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )

    assert result.status == 200
    assert result.mode == "legacy"
    assert result.messages == [{"_state_db_message_id": 1, "content": "legacy"}]
    assert result.message_count == 1
    assert result.before_cursor is None
    assert result.fallback_reason == "receipt_missing"
    assert events == ["resolve", "current", "legacy"]


def test_initial_unverifiable_current_schema_is_exact_legacy_without_page_read():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        ProofCapability,
    )

    events = []
    dependencies = _dependencies(events=events)
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "capability": lambda: ProofCapability(False, "unverifiable_current_state"),
        }
    )

    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )

    assert result.mode == "legacy"
    assert result.fallback_reason == "unverifiable_current_state"
    assert events == ["resolve", "legacy"]


def test_valid_initial_and_continuation_bind_cursor_to_receipt_epoch_and_proof_digest():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewRequest

    events = []
    assembler = BoundedSessionViewAssembler(_dependencies(events=events), cursor_secret=b"x" * 32)
    initial = assembler.assemble(BoundedViewRequest(profile="default", requested_id="root", limit=30))

    assert initial.mode == "cursor_v1"
    assert initial.message_count == 2
    assert initial.before_cursor
    assert initial.messages[0]["content"] == "bounded"
    assert events == [
        "resolve",
        "current",
        "receipt",
        "page",
        "current",
        "receipt",
        "current",
        "receipt",
    ]

    continuation = assembler.assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30, cursor=initial.before_cursor)
    )
    assert continuation.status == 200
    assert continuation.mode == "cursor_v1"
    assert continuation.before_cursor
    assert events[-10:] == [
        "resolve",
        "current",
        "receipt",
        "current",
        "receipt",
        "page",
        "current",
        "receipt",
        "current",
        "receipt",
    ]


def test_continuation_proof_mismatch_returns_restart_with_zero_messages_and_no_page_read():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewRequest

    receipt = _receipt()
    current = _current(receipt)
    events = []
    assembler = BoundedSessionViewAssembler(
        _dependencies(receipt=receipt, current=current, events=events), cursor_secret=b"x" * 32
    )
    initial = assembler.assemble(BoundedViewRequest(profile="default", requested_id="root", limit=30))
    events.clear()
    current["state_content_proof"] = (
        "agent_target_content_epoch_v1",
        (("root", 4), ("tip", 5)),
    )

    result = assembler.assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30, cursor=initial.before_cursor)
    )

    assert result.status == 409
    assert result.error == "cursor_restart_required"
    assert result.messages == []
    assert result.before_cursor is None
    assert events == ["resolve", "current", "receipt"]


@pytest.mark.parametrize(
    "descriptor_index,replacement",
    ((1, 999), (2, 998), (3, 0o640)),
)
def test_continuation_descriptor_identity_change_requires_cursor_restart(
    descriptor_index, replacement
):
    """Device, inode, and mode are receipt identity, not compatibility hints."""
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewRequest

    receipt = _receipt()
    current = _current(receipt)
    events = []
    assembler = BoundedSessionViewAssembler(
        _dependencies(receipt=receipt, current=current, events=events),
        cursor_secret=b"x" * 32,
    )
    initial = assembler.assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert initial.mode == "cursor_v1"
    events.clear()

    member_id, (generation, descriptor) = current["lineage_sidecar_proof"][0]
    changed_descriptor = list(descriptor)
    changed_descriptor[descriptor_index] = replacement
    current["lineage_sidecar_proof"] = (
        (member_id, (generation, tuple(changed_descriptor))),
        *current["lineage_sidecar_proof"][1:],
    )

    result = assembler.assemble(
        BoundedViewRequest(
            profile="default",
            requested_id="root",
            limit=30,
            cursor=initial.before_cursor,
        )
    )

    assert result.status == 409
    assert result.error == "cursor_restart_required"
    assert result.messages == []
    assert result.before_cursor is None
    assert events == ["resolve", "current", "receipt"]


def test_malformed_cursor_is_rejected_only_after_current_proof_is_read():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewRequest

    events = []
    result = BoundedSessionViewAssembler(_dependencies(events=events)).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30, cursor="broken.cursor")
    )

    assert result.status == 400
    assert result.error == "invalid_message_cursor"
    assert result.messages == []
    assert events == ["resolve", "current", "receipt"]


def test_proof_change_after_page_read_restarts_continuation_and_reloads_initial_as_legacy():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewDependencies, BoundedViewRequest

    receipt = _receipt()
    current = _current(receipt)
    initial = BoundedSessionViewAssembler(
        _dependencies(receipt=receipt, current=current), cursor_secret=b"x" * 32
    ).assemble(BoundedViewRequest(profile="default", requested_id="root", limit=30))
    events = []
    dependencies = _dependencies(receipt=receipt, current=current, events=events)
    original_page = dependencies.load_page

    def changing_page(*args):
        page = original_page(*args)
        current["state_content_proof"] = (
            "agent_target_content_epoch_v1", (("root", 4), ("tip", 5))
        )
        return page

    dependencies = BoundedViewDependencies(
        **{**dependencies.__dict__, "load_page": changing_page}
    )
    continuation = BoundedSessionViewAssembler(
        dependencies, cursor_secret=b"x" * 32
    ).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30, cursor=initial.before_cursor)
    )
    assert continuation.status == 409
    assert continuation.messages == []
    assert "legacy" not in events

    current.update(_current(receipt))
    events.clear()
    first = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert first.mode == "legacy"
    assert first.before_cursor is None
    assert events[-1] == "legacy"


def test_typed_overlay_failure_cannot_emit_cursor_but_no_active_owner_is_safe():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        OverlayView,
    )

    dependencies = _dependencies()
    failed = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "overlay": lambda messages, target: OverlayView("runtime_journal_malformed", messages),
        }
    )
    fallback = BoundedSessionViewAssembler(failed).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert fallback.mode == "legacy"
    assert fallback.before_cursor is None

    safe = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "overlay": lambda messages, target: OverlayView("no_active_owner", messages),
        }
    )
    cursor = BoundedSessionViewAssembler(safe).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert cursor.mode == "cursor_v1"
    assert cursor.before_cursor


def test_overlay_is_append_only_and_exactly_increments_message_count_by_renderable_live_delta():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        OverlayView,
    )

    dependencies = _dependencies(receipt=_receipt(settled_display_message_count=1))
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "overlay": lambda messages, target: OverlayView(
                "ok",
                [*messages, {"_runtime_message_id": "run:1", "role": "assistant", "content": "live"}],
                capability_marker=_overlay_capability(),
            ),
        }
    )

    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert result.mode == "cursor_v1"
    assert result.message_count == 2


def test_overlay_ok_without_capability_or_prefix_identity_falls_back_before_cursor_emission():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        OverlayView,
    )

    dependencies = _dependencies()
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "overlay": lambda messages, target: OverlayView("ok", list(reversed(messages))),
        }
    )
    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert result.mode == "legacy"
    assert result.before_cursor is None


def test_content_proof_change_during_overlay_cannot_emit_a_stale_cursor():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        OverlayView,
    )

    receipt = _receipt()
    current = _current(receipt)
    dependencies = _dependencies(receipt=receipt, current=current)

    def rotate_proof(messages, _target):
        current["state_content_proof"] = (
            "agent_target_content_epoch_v1",
            (("root", 3), ("tip", 6)),
        )
        return OverlayView("no_active_owner", messages)

    dependencies = BoundedViewDependencies(
        **{**dependencies.__dict__, "overlay": rotate_proof}
    )
    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )

    assert result.mode == "legacy"
    assert result.before_cursor is None


def test_runtime_overlay_cannot_override_canonical_metadata():
    from api.bounded_session_view import (
        BoundedSessionViewAssembler,
        BoundedViewDependencies,
        BoundedViewRequest,
        OverlayView,
    )

    dependencies = _dependencies()
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "overlay": lambda messages, _target: OverlayView(
                "ok",
                messages,
                metadata={"title": "runtime", "archived": True, "pinned": True},
                capability_marker=_overlay_capability(),
            ),
        }
    )
    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )

    assert result.mode == "legacy"
    assert result.before_cursor is None


def test_legacy_publication_is_injected_and_runs_only_after_exact_legacy_read():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewDependencies, BoundedViewRequest

    events = []
    dependencies = _dependencies(events=events)
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "load_receipt": lambda *_: None,
            "publish_legacy": lambda resolution, view, reason, current: _published(
                events, reason, view.message_count, current["profile"]
            ),
        }
    )

    BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )

    assert events == ["resolve", "current", "legacy", "current", ("publish", "receipt_missing", 1, "default")]


def _published(events, reason, count, profile):
    from api.bounded_session_view import LegacyPublicationResult

    events.append(("publish", reason, count, profile))
    return LegacyPublicationResult(True, "published")


def _make_proof_db(*, triggers=True, capability=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, message_generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, active INTEGER);
        """
    )
    if capability:
        conn.executescript(
            """
            CREATE TABLE agent_contract_capabilities (capability TEXT PRIMARY KEY, version INTEGER NOT NULL);
            INSERT INTO agent_contract_capabilities VALUES ('target_message_generation', 1);
            """
        )
    if triggers:
        conn.executescript(
            """
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
    return conn


def test_capability_detection_requires_marker_generation_column_and_all_write_semantics():
    from api.bounded_session_view import detect_proof_v1_capability

    assert detect_proof_v1_capability(_make_proof_db()).available
    assert detect_proof_v1_capability(_make_proof_db(triggers=False)).reason == "unverifiable_current_state"
    assert detect_proof_v1_capability(_make_proof_db(capability=False)).reason == "unverifiable_current_state"


def test_capability_detection_rejects_a_combined_update_trigger_without_declared_same_and_move_semantics():
    from api.bounded_session_view import detect_proof_v1_capability

    conn = _make_proof_db(triggers=False)
    conn.executescript(
        """
        CREATE TRIGGER proof_messages_insert AFTER INSERT ON messages BEGIN
            UPDATE sessions SET message_generation = message_generation + 1 WHERE id = NEW.session_id;
        END;
        CREATE TRIGGER proof_messages_delete AFTER DELETE ON messages BEGIN
            UPDATE sessions SET message_generation = message_generation + 1 WHERE id = OLD.session_id;
        END;
        CREATE TRIGGER proof_messages_update AFTER UPDATE ON messages BEGIN
            UPDATE sessions SET message_generation = message_generation + 1 WHERE id = OLD.session_id;
            UPDATE sessions SET message_generation = message_generation + 1 WHERE id = NEW.session_id;
        END;
        """
    )
    assert detect_proof_v1_capability(conn).reason == "unverifiable_current_state"


def test_capability_detection_rejects_extra_message_trigger_that_can_undo_epoch():
    from api.bounded_session_view import detect_proof_v1_capability

    conn = _make_proof_db()
    conn.executescript(
        """
        CREATE TRIGGER proof_messages_epoch_reset AFTER INSERT ON messages BEGIN
            UPDATE sessions SET message_generation = 0 WHERE id = NEW.session_id;
        END;
        """
    )

    assert detect_proof_v1_capability(conn).reason == "unverifiable_current_state"


def test_capability_detection_rejects_comment_spoofed_named_trigger_bodies():
    from api.bounded_session_view import detect_proof_v1_capability

    conn = _make_proof_db(triggers=False)
    conn.executescript(
        """
        CREATE TRIGGER proof_messages_insert AFTER INSERT ON messages BEGIN SELECT 1; -- new.session_id message_generation = message_generation + 1
        END;
        CREATE TRIGGER proof_messages_delete AFTER DELETE ON messages BEGIN SELECT 1; -- old.session_id message_generation = message_generation + 1
        END;
        CREATE TRIGGER proof_messages_update_same_session AFTER UPDATE ON messages WHEN OLD.session_id = NEW.session_id BEGIN SELECT 1; -- message_generation = message_generation + 1
        END;
        CREATE TRIGGER proof_messages_update_moved_session AFTER UPDATE ON messages WHEN OLD.session_id != NEW.session_id BEGIN SELECT 1; -- message_generation = message_generation + 1 message_generation = message_generation + 1
        END;
        """
    )
    assert detect_proof_v1_capability(conn).reason == "unverifiable_current_state"


def test_target_confirmation_after_page_prevents_stale_canonical_cursor():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewDependencies, BoundedViewRequest

    dependencies = _dependencies()
    dependencies = BoundedViewDependencies(
        **{**dependencies.__dict__, "confirm_target": lambda _target: False}
    )
    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert result.mode == "legacy"
    assert result.before_cursor is None


def test_page_serialized_bytes_must_match_strict_recomputed_payload_size():
    from api.bounded_session_view import BoundedSessionViewAssembler, BoundedViewDependencies, BoundedViewRequest, PageView

    dependencies = _dependencies()
    dependencies = BoundedViewDependencies(
        **{
            **dependencies.__dict__,
            "load_page": lambda *_args: PageView(
                messages=[{"_state_db_message_id": 2, "role": "assistant", "content": "x" * (3 * 1024 * 1024)}],
                has_more=True,
                visible_count=1,
                raw_rows_examined=2,
                serialized_bytes=1,
                before_boundaries=_boundaries(),
            ),
        }
    )
    result = BoundedSessionViewAssembler(dependencies).assemble(
        BoundedViewRequest(profile="default", requested_id="root", limit=30)
    )
    assert result.mode == "legacy"
    assert result.before_cursor is None


def test_target_content_proof_read_is_ordered_and_targets_only_resolved_members():
    from api.bounded_session_view import read_target_content_proof

    conn = _make_proof_db()
    conn.executemany(
        "INSERT INTO sessions(id, message_generation) VALUES (?, ?)",
        [("root", 3), ("tip", 5), ("unrelated", 99)],
    )

    proof = read_target_content_proof(conn, ("tip", "root"))

    assert proof == (
        "agent_target_content_epoch_v1",
        (("tip", 5), ("root", 3)),
    )


def test_shadow_difference_classifies_identity_order_count_truncation_and_tool_pair():
    from api.bounded_session_view import classify_shadow_difference

    assert classify_shadow_difference(
        [{"_state_db_message_id": 1}, {"_state_db_message_id": 2}],
        [{"_state_db_message_id": 2}, {"_state_db_message_id": 1}],
        candidate_count=2,
        legacy_count=3,
        candidate_truncated=False,
        legacy_truncated=True,
    ) == {
        "visible_count_difference",
        "visible_order_difference",
        "truncation_difference",
    }
    assert classify_shadow_difference(
        [{"_state_db_message_id": 1, "tool_calls": [{"id": "call-1"}]}],
        [{"_state_db_message_id": 1, "tool_calls": [{"id": "call-1"}]}, {"tool_call_id": "call-1"}],
        candidate_count=1,
        legacy_count=1,
    ) == {"tool_pair_difference"}
