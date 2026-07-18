"""Contract tests for crash-safe, proof-bound conversation todo projections."""

from __future__ import annotations

import json
import errno
import multiprocessing
import re
import warnings

import pytest

from api.conversation_view_state import (
    ConversationViewStateStore,
    MessageWatermark,
    ViewStateStoreError,
    snapshot_digest,
)


PROOF_A = "sha256:" + ("a" * 64)
PROOF_B = "sha256:" + ("b" * 64)


def _concurrent_projection_publish(state_dir, proof, content, barrier, results):
    """Publish after both forked workers have read under the old CAS protocol."""
    from api.conversation_view_state import ConversationViewStateStore, MessageWatermark

    original_write = ConversationViewStateStore._atomic_write

    def delayed_write(path, raw):
        try:
            barrier.wait(timeout=1.0)
        except Exception:
            # With the inter-process lock one worker intentionally times out
            # here while it excludes the other from the read/write interval.
            pass
        original_write(path, raw)

    ConversationViewStateStore._atomic_write = staticmethod(delayed_write)
    try:
        result = ConversationViewStateStore(state_dir).compare_and_swap(
            profile="default",
            root_id="root",
            watermark=MessageWatermark(timestamp=20.0, message_id=9),
            target_content_proof_digest=proof,
            snapshot=_snapshot(content),
        )
        results.put((result.saved, result.state.generation))
    finally:
        ConversationViewStateStore._atomic_write = original_write


def _snapshot(content="ship it", *, ts=20.0):
    return {
        "todos": [{"id": "1", "content": content, "status": "pending"}],
        "summary": {
            "total": 1,
            "pending": 1,
            "in_progress": 0,
            "completed": 0,
            "cancelled": 0,
        },
        "version": 1,
        "ts": ts,
    }


def test_compare_and_swap_persists_explicit_empty_tombstone(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    saved = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_A,
        snapshot={"todos": [], "summary": {}, "version": 1, "ts": 20.0},
    )

    assert saved.saved is True
    assert saved.reason == "saved"
    assert saved.state.snapshot["todos"] == []
    assert saved.state.empty_tombstone is True
    assert saved.state.generation == 1
    assert saved.state.snapshot_digest == snapshot_digest(saved.state.snapshot)
    assert store.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=PROOF_A,
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
    ) == saved.state


def test_same_proof_rejects_older_replay_and_conflicting_same_watermark(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    accepted = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_A,
        snapshot=_snapshot("new"),
    )

    older = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=19.0, message_id=8),
        target_content_proof_digest=PROOF_A,
        snapshot=_snapshot("old", ts=19.0),
    )
    conflict = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_A,
        snapshot=_snapshot("different"),
    )

    assert older.saved is False
    assert older.reason == "older_replay"
    assert conflict.saved is False
    assert conflict.reason == "conflicting_same_watermark"
    assert store.read(profile="default", root_id="root").snapshot == accepted.state.snapshot


def test_exact_repeat_is_idempotent_without_advancing_generation(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    kwargs = {
        "profile": "default",
        "root_id": "root",
        "watermark": MessageWatermark(timestamp=20.0, message_id=9),
        "target_content_proof_digest": PROOF_A,
        "snapshot": _snapshot(),
    }
    first = store.compare_and_swap(**kwargs)
    repeated = store.compare_and_swap(**kwargs)

    assert repeated.saved is True
    assert repeated.reason == "unchanged"
    assert repeated.state.generation == first.state.generation == 1


def test_new_content_proof_accepts_interior_change_at_same_watermark(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    old = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_A,
        snapshot=_snapshot("before"),
    )
    new = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_B,
        snapshot=_snapshot("after"),
    )

    assert new.saved is True
    assert new.state.generation == old.state.generation + 1
    assert store.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=PROOF_A,
    ) is None
    assert store.read(
        profile="default",
        root_id="root",
        target_content_proof_digest=PROOF_B,
    ) == new.state


def test_projection_cas_serializes_cross_process_publishers(tmp_path):
    """Distinct concurrent proofs get distinct persisted generations."""
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("cross-process advisory-lock regression requires fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_projection_publish,
            args=(tmp_path, proof, content, barrier, results),
        )
        for proof, content in ((PROOF_A, "first"), (PROOF_B, "second"))
    ]
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="This process.*is multi-threaded.*",
            category=DeprecationWarning,
        )
        for worker in workers:
            worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    published = [results.get(timeout=2) for _ in workers]
    assert [saved for saved, _generation in published] == [True, True]
    assert sorted(generation for _saved, generation in published) == [1, 2]
    assert ConversationViewStateStore(tmp_path).read(
        profile="default", root_id="root"
    ).generation == 2


def test_projection_cas_fails_closed_without_advisory_locking(tmp_path, monkeypatch):
    import api.conversation_view_state as view_state

    monkeypatch.setattr(view_state, "_fcntl", None)
    with pytest.raises(ViewStateStoreError, match="advisory locking unavailable"):
        ConversationViewStateStore(tmp_path).compare_and_swap(
            profile="default",
            root_id="root",
            watermark=MessageWatermark(timestamp=1.0, message_id=1),
            target_content_proof_digest=PROOF_A,
            snapshot=_snapshot(),
        )


def test_store_is_profile_root_isolated_and_uses_only_hashed_filenames(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    for profile, root_id in (("default", "../../root"), ("work", "../../root")):
        store.compare_and_swap(
            profile=profile,
            root_id=root_id,
            watermark=MessageWatermark(timestamp=1.0, message_id=1),
            target_content_proof_digest=PROOF_A,
            snapshot={"todos": [], "summary": {}, "version": 1},
        )

    files = list((tmp_path / "conversation_view_state").glob("*.json"))
    assert len(files) == 2
    assert all(".." not in path.name and "/" not in path.name for path in files)
    assert store.read(profile="default", root_id="../../root") is not None
    assert store.read(profile="work", root_id="../../root") is not None


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        '{"version":99}',
        '{"version":1,"profile":"default"}',
    ],
)
def test_corrupt_or_future_projection_fails_closed(tmp_path, payload):
    store = ConversationViewStateStore(tmp_path)
    path = store.path_for("default", "root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ViewStateStoreError):
        store.read(profile="default", root_id="root")


def test_oversized_projection_is_rejected_before_json_parsing(tmp_path, monkeypatch):
    import api.conversation_view_state as view_state

    store = ConversationViewStateStore(tmp_path)
    path = store.path_for("default", "root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b" " * (view_state.MAX_VIEW_STATE_BYTES + 1))
    monkeypatch.setattr(
        view_state.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized projection reached JSON parsing")
        ),
    )

    with pytest.raises(ViewStateStoreError, match="byte limit"):
        store.read(profile="default", root_id="root")


def test_projection_lock_contention_times_out_fail_closed(tmp_path, monkeypatch):
    import api.conversation_view_state as view_state

    class BusyLock:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 8

        @staticmethod
        def flock(_descriptor, operation):
            if operation != BusyLock.LOCK_UN:
                raise BlockingIOError(errno.EAGAIN, "busy")

    monkeypatch.setattr(view_state, "_fcntl", BusyLock)
    monkeypatch.setattr(view_state, "PROJECTION_LOCK_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(ViewStateStoreError, match="advisory locking unavailable"):
        ConversationViewStateStore(tmp_path).compare_and_swap(
            profile="default",
            root_id="root",
            watermark=MessageWatermark(timestamp=1.0, message_id=1),
            target_content_proof_digest=PROOF_A,
            snapshot=_snapshot(),
        )


def test_atomic_replace_failure_preserves_previous_projection(tmp_path, monkeypatch):
    import api.conversation_view_state as view_state

    store = ConversationViewStateStore(tmp_path)
    first = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=1.0, message_id=1),
        target_content_proof_digest=PROOF_A,
        snapshot=_snapshot("first", ts=1.0),
    )
    monkeypatch.setattr(
        view_state.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        store.compare_and_swap(
            profile="default",
            root_id="root",
            watermark=MessageWatermark(timestamp=2.0, message_id=2),
            target_content_proof_digest=PROOF_B,
            snapshot=_snapshot("second", ts=2.0),
        )

    monkeypatch.undo()
    assert store.read(profile="default", root_id="root") == first.state


@pytest.mark.parametrize(
    ("watermark", "proof", "snapshot"),
    [
        (MessageWatermark(timestamp=float("nan"), message_id=1), PROOF_A, _snapshot()),
        (MessageWatermark(timestamp=1.0, message_id=True), PROOF_A, _snapshot()),
        (MessageWatermark(timestamp=1.0, message_id=1), "sha256:nope", _snapshot()),
        (MessageWatermark(timestamp=1.0, message_id=1), PROOF_A, {"todos": "bad"}),
    ],
)
def test_invalid_watermark_proof_or_snapshot_is_rejected(
    tmp_path,
    watermark,
    proof,
    snapshot,
):
    store = ConversationViewStateStore(tmp_path)
    with pytest.raises(ValueError):
        store.compare_and_swap(
            profile="default",
            root_id="root",
            watermark=watermark,
            target_content_proof_digest=proof,
            snapshot=snapshot,
        )


def test_projection_module_does_not_mutate_session_metadata():
    import inspect
    import api.conversation_view_state as view_state

    source = inspect.getsource(view_state)
    for forbidden in (
        "updated_at",
        "unread",
        "manual_title",
        "archived",
        "pinned",
        "Session.save",
    ):
        assert re.search(rf"\b{re.escape(forbidden)}\b", source) is None


def test_persisted_projection_is_normalized_and_content_proof_bound(tmp_path):
    store = ConversationViewStateStore(tmp_path)
    result = store.compare_and_swap(
        profile="default",
        root_id="root",
        watermark=MessageWatermark(timestamp=20.0, message_id=9),
        target_content_proof_digest=PROOF_A,
        snapshot={"todos": [], "summary": "invalid summary", "ts": 20.0},
    )

    raw = json.loads(store.path_for("default", "root").read_text())
    assert raw["target_content_proof_digest"] == PROOF_A
    assert raw["snapshot"] == {
        "todos": [],
        "summary": {},
        "version": 1,
        "ts": 20.0,
    }
    assert result.state.snapshot == raw["snapshot"]
