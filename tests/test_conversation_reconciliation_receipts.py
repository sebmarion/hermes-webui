"""Contract tests for content-free conversation reconciliation receipts."""

from __future__ import annotations

import dataclasses
import json
import math
import multiprocessing
import threading

import pytest

from api.conversation_receipts import (
    ConversationReceipt,
    ConversationReceiptStore,
    MAX_PERSISTED_JSON_BYTES,
    ReceiptStoreError,
    VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
    canonical_proof_digest,
    validate_receipt,
)


def _proof(*generations: int):
    return (
        "agent_target_content_epoch_v1",
        (("root", generations[0]), ("tip", generations[1])),
    )


def _lineage_sidecar_proof():
    return (
        (
            "root",
            (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30)),
        ),
        ("tip", "missing"),
    )


def _descriptor_from_compatibility(sidecar_stat):
    path, mtime_ns, size, ctime_ns = sidecar_stat
    return path, 101, 102, 0o600, size, mtime_ns, ctime_ns


def _receipt(**changes):
    values = {
        "version": 1,
        "profile": "default",
        "root_id": "root",
        "member_ids": ("root", "tip"),
        "lineage_fingerprint": "lineage-v1",
        "canonical_sidecar_id": "root",
        "lineage_sidecar_proof": _lineage_sidecar_proof(),
        "sidecar_generation": 4,
        "sidecar_stat": ("/profiles/default/root.json", 10, 20, 30),
        "truncation_watermark": 12.5,
        "state_message_watermark": (901, 123.0),
        "state_content_proof": _proof(42, 7),
        "settled_display_message_count": 88,
        "visible_transcript_digest": "sha256:" + ("0" * 64),
        "todo_projection_generation": 1,
        "todo_projection_watermark": (901, 123.0),
        "todo_projection_target_content_proof_digest": canonical_proof_digest(
            "lineage-v1", _proof(42, 7)
        ),
        "todo_projection_snapshot_digest": "sha256:" + ("1" * 64),
        "generation": 3,
    }
    values.update(changes)
    if "canonical_sidecar_id" not in changes:
        values["canonical_sidecar_id"] = values["member_ids"][0]
    if "lineage_sidecar_proof" not in changes:
        values["lineage_sidecar_proof"] = tuple(
            (
                member_id,
                (
                    values["sidecar_generation"],
                    _descriptor_from_compatibility(values["sidecar_stat"]),
                )
                if member_id == values["canonical_sidecar_id"]
                else "missing",
            )
            for member_id in values["member_ids"]
        )
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
        "state_content_proof_capability": VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY,
        "settled_display_message_count": receipt.settled_display_message_count,
        "visible_transcript_digest": receipt.visible_transcript_digest,
        "todo_projection_generation": receipt.todo_projection_generation,
        "todo_projection_watermark": receipt.todo_projection_watermark,
        "todo_projection_target_content_proof_digest": (
            receipt.todo_projection_target_content_proof_digest
        ),
        "todo_projection_snapshot_digest": receipt.todo_projection_snapshot_digest,
    }


def _publish_in_other_process(state_dir, root_id, start, results):
    start.wait()
    receipt = _receipt(
        root_id=root_id,
        member_ids=(root_id,),
        state_content_proof=(
            "agent_target_content_epoch_v1",
            ((root_id, 1),),
        ),
        generation=0,
    )
    try:
        results.put(("ok", ConversationReceiptStore(state_dir).publish(receipt).generation))
    except BaseException as exc:  # pragma: no cover - parent assertion owns it
        results.put(("error", repr(exc)))


def test_store_is_profile_root_isolated_and_uses_safe_hashed_paths(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    unsafe_root = "../../not-a-path"
    identity = {
        "root_id": unsafe_root,
        "member_ids": (unsafe_root, "tip"),
        "state_content_proof": (
            "agent_target_content_epoch_v1",
            ((unsafe_root, 42), ("tip", 7)),
        ),
    }
    first = store.publish(_receipt(profile="default", **identity))
    second = store.publish(_receipt(profile="work", **identity))

    assert first.generation == 4
    assert second.generation == 5
    assert store.load("default", unsafe_root) == first
    assert store.load("work", unsafe_root) == second
    assert all(".." not in path.name and "/" not in path.name for path in tmp_path.rglob("*"))
    assert len(list(tmp_path.rglob("*.json"))) == 4


@pytest.mark.parametrize("artifact", ["receipt", "high_water", "marker"])
def test_persisted_json_reads_are_byte_bounded(tmp_path, artifact):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))
    paths = {
        "receipt": store.receipt_path("default", "root"),
        "high_water": store.high_water_path(),
        "marker": store.initialization_marker_path(),
    }
    paths[artifact].write_bytes(b"{" + (b" " * MAX_PERSISTED_JSON_BYTES))

    with pytest.raises(ReceiptStoreError, match="byte limit"):
        if artifact == "receipt":
            store.load("default", "root")
        else:
            store.publish(_receipt(generation=0))


def test_validator_requires_identity_verified_agent_capability_marker():
    receipt = _receipt()
    current = _current(receipt)
    current.pop("state_content_proof_capability")

    missing = validate_receipt(receipt, current=current)
    assert missing.valid is False
    assert missing.reason == "unverifiable_current_state"

    current["state_content_proof_capability"] = "agent_target_content_epoch_v1"
    spoofed = validate_receipt(receipt, current=current)
    assert spoofed.valid is False
    assert spoofed.reason == "unverifiable_current_state"


def test_receipt_round_trip_requires_exact_ordered_lineage_sidecar_proof():
    raw = _receipt().to_dict()
    raw.update(
        {
            "canonical_sidecar_id": "root",
            "lineage_sidecar_proof": [
                ["root", [4, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
                ["tip", "missing"],
            ],
        }
    )

    receipt = ConversationReceipt.from_dict(raw)

    assert receipt.canonical_sidecar_id == "root"
    assert receipt.lineage_sidecar_proof == _lineage_sidecar_proof()
    assert receipt.to_dict()["lineage_sidecar_proof"] == raw["lineage_sidecar_proof"]


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_sidecar_id": "outside-lineage"},
        {"canonical_sidecar_id": "tip"},
        {"lineage_sidecar_proof": []},
        {
            "lineage_sidecar_proof": [
                ["tip", "missing"],
                ["root", [4, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [4, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
                ["root", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", "missing"],
                ["tip", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [True, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
                ["tip", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [5, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
                ["tip", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [4, ["/profiles/default/root.json", 101.0, 102, 0o600, 20, 10, 30]]],
                ["tip", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [4, ["/" + ("x" * 4096), 101, 102, 0o600, 20, 10, 30]]],
                ["tip", "missing"],
            ]
        },
        {
            "lineage_sidecar_proof": [
                ["root", [4, ["/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30]]],
                ["tip", "absent"],
            ]
        },
    ],
)
def test_receipt_rejects_malformed_or_incomplete_lineage_sidecar_proof(changes):
    raw = _receipt().to_dict()
    raw.update(changes)

    with pytest.raises(ReceiptStoreError):
        ConversationReceipt.from_dict(raw)


def test_publish_rejects_non_tuple_in_memory_lineage_sidecar_proof(tmp_path):
    invalid = dataclasses.replace(
        _receipt(),
        lineage_sidecar_proof=[
            ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
            ("tip", "missing"),
        ],
    )

    with pytest.raises(ReceiptStoreError):
        ConversationReceiptStore(tmp_path).publish(invalid)


@pytest.mark.parametrize(
    "lineage_sidecar_proof,compatibility_changes",
    [
        (
            (
                ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
                ("ancestor", (6, ("/profiles/default/ancestor.json", 111, 112, 0o600, 21, 11, 31))),
                ("tip", "missing"),
            ),
            {},
        ),
        (
            (
                ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
                ("ancestor", "missing"),
                ("tip", "missing"),
            ),
            {},
        ),
        (
            (
                ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
                ("ancestor", (5, ("/profiles/default/ancestor.json", 111, 112, 0o600, 21, 11, 31))),
                ("tip", (1, ("/profiles/default/tip.json", 121, 122, 0o600, 22, 12, 32))),
            ),
            {},
        ),
        (
            (
                ("root", (5, ("/profiles/default/root.json", 101, 102, 0o600, 23, 13, 33))),
                ("ancestor", (5, ("/profiles/default/ancestor.json", 111, 112, 0o600, 21, 11, 31))),
                ("tip", "missing"),
            ),
            {
                "sidecar_generation": 5,
                "sidecar_stat": ("/profiles/default/root.json", 13, 23, 33),
            },
        ),
    ],
)
def test_cursor_restarts_for_any_lineage_sidecar_change(
    lineage_sidecar_proof, compatibility_changes
):
    original_proof = (
        ("root", (4, ("/profiles/default/root.json", 101, 102, 0o600, 20, 10, 30))),
        ("ancestor", (5, ("/profiles/default/ancestor.json", 111, 112, 0o600, 21, 11, 31))),
        ("tip", "missing"),
    )
    receipt = _receipt(
        member_ids=("root", "ancestor", "tip"),
        state_content_proof=(
            "agent_target_content_epoch_v1",
            (("root", 42), ("ancestor", 13), ("tip", 7)),
        ),
        lineage_sidecar_proof=original_proof,
        generation=9,
    )
    current = _current(receipt)
    current["lineage_sidecar_proof"] = lineage_sidecar_proof
    current.update(compatibility_changes)

    result = validate_receipt(
        receipt,
        current=current,
        cursor_epoch=9,
        cursor_proof_digest=canonical_proof_digest(
            receipt.lineage_fingerprint, receipt.state_content_proof
        ),
    )

    assert result.valid is False
    assert result.reason == "cursor_restart_required"


def test_multiprocess_publishers_receive_distinct_store_epochs(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_publish_in_other_process,
            args=(str(tmp_path), root_id, start, results),
        )
        for root_id in ("process-a", "process-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    published = [results.get(timeout=1) for _ in processes]
    assert sorted(published) == [("ok", 1), ("ok", 2)]


def test_store_fails_closed_when_interprocess_lock_support_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("api.conversation_receipts.fcntl", None)

    with pytest.raises(ReceiptStoreError, match="interprocess lock unavailable"):
        ConversationReceiptStore(tmp_path).publish(_receipt(generation=0))


def test_publish_if_current_removes_just_published_receipt_when_proof_changes_at_replace(
    tmp_path, monkeypatch
):
    store = ConversationReceiptStore(tmp_path)
    candidate = _receipt(generation=0)
    current = _current(candidate)
    original_replace = store._replace_prepared

    def replace_then_mutate(tmp, path):
        original_replace(tmp, path)
        current["sidecar_generation"] += 1

    monkeypatch.setattr(store, "_replace_prepared", replace_then_mutate)

    with pytest.raises(ReceiptStoreError, match="changed after publication"):
        store.publish_if_current(candidate, lambda: current)

    with pytest.raises(ReceiptStoreError, match="guarded"):
        store.load("default", "root")
    assert store.publish(_receipt(generation=0)).generation == 2


def test_validator_fails_closed_without_declared_agent_content_proof():
    receipt = _receipt()
    current = _current(receipt)
    current["state_content_proof"] = None

    result = validate_receipt(receipt, current=current)

    assert result.valid is False
    assert result.reason == "unverifiable_current_state"


@pytest.mark.parametrize(
    "changed",
    [
        {"state_content_proof": _proof(43, 7)},
        {"state_content_proof": _proof(42, 8)},
        {"lineage_fingerprint": "lineage-v2"},
    ],
)
def test_validator_restarts_cursor_when_member_generation_or_lineage_changes(changed):
    receipt = _receipt()
    current = _current(receipt)
    current.update(changed)

    result = validate_receipt(
        receipt,
        current=current,
        cursor_epoch=receipt.generation,
        cursor_proof_digest=canonical_proof_digest(
            receipt.lineage_fingerprint, receipt.state_content_proof
        ),
    )

    assert result.valid is False
    assert result.reason == "cursor_restart_required"


def test_validator_rejects_hints_as_content_proof_and_checks_all_proof_fields():
    receipt = _receipt()
    current = _current(receipt)
    current["state_content_proof"] = ("message_generation", (("root", 42), ("tip", 7)))
    current["sidecar_stat"] = ("/profiles/default/root.json", 999, 20, 30)

    result = validate_receipt(receipt, current=current)

    assert result.valid is False
    assert result.reason == "unverifiable_current_state"


def test_store_fails_closed_for_corrupt_or_missing_initialized_high_water(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    receipt = store.publish(_receipt())
    high_water = store.high_water_path()
    high_water.write_text("not json", encoding="utf-8")

    with pytest.raises(ReceiptStoreError, match="high-water"):
        store.publish(_receipt(generation=receipt.generation))

    high_water.unlink()
    with pytest.raises(ReceiptStoreError, match="missing"):
        store.publish(_receipt(generation=receipt.generation))


def test_publication_failure_consumes_epoch_and_delete_recreate_never_reuses_it(
    tmp_path, monkeypatch
):
    store = ConversationReceiptStore(tmp_path)
    original_replace = __import__("api.conversation_receipts", fromlist=["os"]).os.replace
    receipt_path = store.receipt_path("default", "root")

    def fail_receipt_publish(source, destination):
        if destination == receipt_path:
            raise OSError("replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr("api.conversation_receipts.os.replace", fail_receipt_publish)
    with pytest.raises(ReceiptStoreError, match="atomic write"):
        store.publish(_receipt(generation=0))
    monkeypatch.setattr("api.conversation_receipts.os.replace", original_replace)

    published = store.publish(_receipt(generation=0))
    assert published.generation == 2
    store.delete("default", "root")
    recreated = store.publish(_receipt(generation=0))
    assert recreated.generation == 3


def test_concurrent_stale_publishers_get_distinct_monotonic_epochs(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    receipts = []
    failures = []
    barrier = threading.Barrier(2)

    def publish():
        try:
            barrier.wait()
            receipts.append(store.publish(_receipt(generation=0)))
        except BaseException as exc:  # pragma: no cover - assertion below owns it
            failures.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(receipt.generation for receipt in receipts) == [1, 2]


def test_concurrent_distinct_lineages_share_one_store_epoch_sequence(tmp_path):
    stores = [ConversationReceiptStore(tmp_path), ConversationReceiptStore(tmp_path)]
    receipts = []
    failures = []
    barrier = threading.Barrier(2)

    def publish(store, suffix):
        root_id = f"root-{suffix}"
        try:
            barrier.wait()
            receipts.append(
                store.publish(
                    _receipt(
                        root_id=root_id,
                        member_ids=(root_id, f"tip-{suffix}"),
                        state_content_proof=(
                            "agent_target_content_epoch_v1",
                            ((root_id, 1), (f"tip-{suffix}", 1)),
                        ),
                        generation=0,
                    )
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below owns it
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(stores[index], index))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(receipt.generation for receipt in receipts) == [1, 2]


def test_failed_publish_burns_store_epoch_before_other_lineage(tmp_path, monkeypatch):
    store = ConversationReceiptStore(tmp_path)
    original_replace = __import__("api.conversation_receipts", fromlist=["os"]).os.replace
    failed_path = store.receipt_path("default", "root")

    def fail_one_receipt(source, destination):
        if destination == failed_path:
            raise OSError("receipt replace failed")
        return original_replace(source, destination)

    monkeypatch.setattr("api.conversation_receipts.os.replace", fail_one_receipt)
    with pytest.raises(ReceiptStoreError, match="atomic write"):
        store.publish(_receipt(generation=0))
    monkeypatch.setattr("api.conversation_receipts.os.replace", original_replace)

    other = store.publish(
        _receipt(
            root_id="other",
            member_ids=("other", "other-tip"),
            state_content_proof=(
                "agent_target_content_epoch_v1",
                (("other", 2), ("other-tip", 2)),
            ),
            generation=0,
        )
    )
    assert other.generation == 2


def test_missing_or_corrupt_store_marker_fails_closed_after_initialization(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))
    marker = store.initialization_marker_path()

    marker.unlink()
    with pytest.raises(ReceiptStoreError, match="marker"):
        store.publish(_receipt(generation=0))

    marker.write_text("not-json", encoding="utf-8")
    with pytest.raises(ReceiptStoreError, match="marker"):
        store.publish(_receipt(generation=0))


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"profile": ""},
        {"profile": "p" * 513},
        {"root_id": ""},
        {"member_ids": []},
        {"member_ids": ["root", "root"]},
        {"member_ids": ["tip"]},
        {"member_ids": ["root"] * 257},
        {"member_ids": ["root", "m" * 513]},
        {"lineage_fingerprint": ""},
        {"sidecar_generation": True},
        {"sidecar_generation": 4.0},
        {"sidecar_generation": "4"},
        {"sidecar_stat": [10, 20, 30]},
        {"sidecar_stat": ["/x", 1.0, 2, 3]},
        {"truncation_watermark": math.inf},
        {"truncation_watermark": True},
        {"state_message_watermark": [901]},
        {"state_message_watermark": [901.0, 123.0]},
        {"state_message_watermark": [901, math.nan]},
        {
            "state_content_proof": [
                "agent_target_content_epoch_v1",
                [["root", 42.0], ["tip", 7]],
            ]
        },
        {
            "state_content_proof": [
                "agent_target_content_epoch_v1",
                [["root", True], ["tip", 7]],
            ]
        },
        {
            "state_content_proof": [
                "coincidentally_named_generation_column",
                [["root", 42], ["tip", 7]],
            ]
        },
        {"settled_display_message_count": 88.0},
        {"visible_transcript_digest": "sha256:not-a-digest"},
        {"generation": "3"},
    ],
)
def test_receipt_parser_rejects_unbounded_or_coerced_fields(changes):
    raw = _receipt().to_dict()
    raw.update(changes)

    with pytest.raises(ReceiptStoreError):
        ConversationReceipt.from_dict(raw)


@pytest.mark.parametrize("raw", [[], {"version": 1}, {**_receipt().to_dict(), "extra": 1}])
def test_receipt_parser_requires_one_exact_top_level_mapping(raw):
    with pytest.raises(ReceiptStoreError):
        ConversationReceipt.from_dict(raw)


def test_load_wraps_json_shape_and_version_failures_as_receipt_store_error(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    path = store.receipt_path("default", "root")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([]), encoding="utf-8")

    with pytest.raises(ReceiptStoreError):
        store.load("default", "root")


def test_load_fails_closed_when_store_high_water_is_no_longer_verifiable(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))
    store.high_water_path().write_text("not-json", encoding="utf-8")

    with pytest.raises(ReceiptStoreError, match="high-water"):
        store.load("default", "root")


def test_store_counter_version_requires_an_exact_integer(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))
    store.high_water_path().write_text(
        json.dumps({"version": True, "high_water": 1}), encoding="utf-8"
    )

    with pytest.raises(ReceiptStoreError, match="version"):
        store.publish(_receipt(generation=0))


def test_publish_and_validate_reject_invalid_in_memory_receipts(tmp_path):
    invalid = dataclasses.replace(_receipt(), generation=True)

    with pytest.raises(ReceiptStoreError):
        ConversationReceiptStore(tmp_path).publish(invalid)
    result = validate_receipt(invalid, current=_current(invalid))
    assert result.valid is False
    assert result.reason == "receipt_invalid"


def test_publish_rejects_noncanonical_in_memory_container_shapes(tmp_path):
    invalid = dataclasses.replace(_receipt(), member_ids=["root", "tip"])

    with pytest.raises(ReceiptStoreError):
        ConversationReceiptStore(tmp_path).publish(invalid)


def test_validator_binds_visible_transcript_digest():
    receipt = _receipt()
    current = _current(receipt)
    current["visible_transcript_digest"] = "sha256:" + ("1" * 64)

    result = validate_receipt(receipt, current=current)

    assert result.valid is False
    assert result.reason == "receipt_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("todo_projection_generation", 2),
        ("todo_projection_watermark", (902, 123.0)),
        ("todo_projection_target_content_proof_digest", "sha256:" + ("2" * 64)),
        ("todo_projection_snapshot_digest", "sha256:" + ("3" * 64)),
    ],
)
def test_validator_binds_each_todo_projection_receipt_field(field, value):
    receipt = _receipt()
    current = _current(receipt)
    current[field] = value

    result = validate_receipt(receipt, current=current)

    assert result.valid is False
    assert result.reason == "receipt_mismatch"


def test_publish_if_current_reads_complete_proof_before_and_after_epoch_allocation(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    candidate = _receipt(generation=0)
    calls = []

    def current_supplier():
        calls.append(len(calls))
        return _current(candidate)

    published = store.publish_if_current(candidate, current_supplier)

    assert published.generation == 1
    assert calls == [0, 1, 2]
    assert store.load("default", "root") == published


def test_publish_if_current_aborts_before_allocation_on_initial_mismatch(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    candidate = _receipt(generation=0)
    current = _current(candidate)
    current["visible_transcript_digest"] = "sha256:" + ("1" * 64)

    with pytest.raises(ReceiptStoreError, match="current proof"):
        store.publish_if_current(candidate, lambda: current)

    assert not store.high_water_path().exists()
    assert not store.initialization_marker_path().exists()


def test_publish_if_current_burns_epoch_when_second_proof_read_detects_race(tmp_path):
    store = ConversationReceiptStore(tmp_path)
    candidate = _receipt(generation=0)
    stable = _current(candidate)
    raced = dict(stable, sidecar_generation=candidate.sidecar_generation + 1)
    reads = iter((stable, raced))

    with pytest.raises(ReceiptStoreError, match="changed during publication"):
        store.publish_if_current(candidate, lambda: next(reads))

    published = store.publish(_receipt(generation=0))
    assert published.generation == 2


def test_directory_fsync_failure_is_reported_and_consumes_epoch(tmp_path, monkeypatch):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))

    def fail_directory_fsync(path):
        raise OSError("directory fsync failed")

    monkeypatch.setattr("api.conversation_receipts._fsync_directory", fail_directory_fsync)
    with pytest.raises(ReceiptStoreError, match="atomic write"):
        store.publish(_receipt(generation=0))
    monkeypatch.undo()

    assert store.publish(_receipt(generation=0)).generation == 3


def test_replace_directory_fsync_failure_leaves_receipt_guarded_until_republish(
    tmp_path, monkeypatch
):
    store = ConversationReceiptStore(tmp_path)
    candidate = _receipt(generation=0)
    original_fsync = __import__(
        "api.conversation_receipts", fromlist=["_fsync_directory"]
    )._fsync_directory
    calls = 0

    def fail_receipt_replace_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 4:  # marker, high-water, guard, then receipt replacement
            raise OSError("receipt directory fsync failed")
        original_fsync(path)

    monkeypatch.setattr(
        "api.conversation_receipts._fsync_directory", fail_receipt_replace_fsync
    )
    with pytest.raises(ReceiptStoreError, match="atomic write"):
        store.publish(candidate)
    monkeypatch.undo()

    with pytest.raises(ReceiptStoreError, match="guarded"):
        ConversationReceiptStore(tmp_path).load("default", "root")

    published = ConversationReceiptStore(tmp_path).publish(candidate)
    assert published.generation == 2
    assert store.load("default", "root") == published


def test_delete_fsync_failure_keeps_tombstone_and_republish_makes_receipt_eligible(
    tmp_path, monkeypatch
):
    store = ConversationReceiptStore(tmp_path)
    store.publish(_receipt(generation=0))
    original_fsync = __import__(
        "api.conversation_receipts", fromlist=["_fsync_directory"]
    )._fsync_directory
    calls = 0

    def fail_delete_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 2:  # durable tombstone guard, then deleted receipt directory
            raise OSError("delete directory fsync failed")
        original_fsync(path)

    monkeypatch.setattr(
        "api.conversation_receipts._fsync_directory", fail_delete_fsync
    )
    with pytest.raises(ReceiptStoreError, match="deletion"):
        store.delete("default", "root")
    monkeypatch.undo()

    with pytest.raises(ReceiptStoreError, match="guarded"):
        ConversationReceiptStore(tmp_path).load("default", "root")

    republished = ConversationReceiptStore(tmp_path).publish(_receipt(generation=0))
    assert republished.generation == 2
    assert store.load("default", "root") == republished


def test_rollback_fsync_failure_leaves_tombstone_and_republish_recovers(tmp_path, monkeypatch):
    store = ConversationReceiptStore(tmp_path)
    store.publish(
        _receipt(
            root_id="initial",
            member_ids=("initial",),
            state_content_proof=("agent_target_content_epoch_v1", (("initial", 1),)),
            generation=0,
        )
    )
    candidate = _receipt(generation=0)
    current = _current(candidate)
    original_replace = store._replace_prepared
    original_fsync = __import__(
        "api.conversation_receipts", fromlist=["_fsync_directory"]
    )._fsync_directory

    def replace_then_mutate(tmp, path):
        original_replace(tmp, path)
        current["sidecar_generation"] += 1

    calls = 0

    def fail_rollback_fsync(path):
        nonlocal calls
        calls += 1
        if calls == 5:  # high-water, guard, receipt, tombstone, then deletion
            raise OSError("rollback directory fsync failed")
        original_fsync(path)

    monkeypatch.setattr(store, "_replace_prepared", replace_then_mutate)
    monkeypatch.setattr(
        "api.conversation_receipts._fsync_directory", fail_rollback_fsync
    )
    with pytest.raises(ReceiptStoreError, match="rollback"):
        store.publish_if_current(candidate, lambda: current)
    monkeypatch.undo()

    with pytest.raises(ReceiptStoreError, match="guarded"):
        ConversationReceiptStore(tmp_path).load("default", "root")

    republished = ConversationReceiptStore(tmp_path).publish(_receipt(generation=0))
    assert republished.generation == 3
    assert store.load("default", "root") == republished


def test_receipt_parser_rejects_integer_larger_than_persisted_counter_range():
    raw = _receipt().to_dict()
    raw["generation"] = 2**100

    with pytest.raises(ReceiptStoreError):
        ConversationReceipt.from_dict(raw)


def test_aba_proof_cannot_validate_old_cursor_even_when_content_shape_returns():
    original = _receipt(state_content_proof=_proof(42, 7), generation=9)
    current = _current(original)
    # Content can look like A again, but Agent-owned member generations are monotonic.
    current["state_content_proof"] = _proof(44, 9)

    result = validate_receipt(
        original,
        current=current,
        cursor_epoch=9,
        cursor_proof_digest=canonical_proof_digest(
            original.lineage_fingerprint, original.state_content_proof
        ),
    )

    assert result.valid is False
    assert result.reason == "cursor_restart_required"
