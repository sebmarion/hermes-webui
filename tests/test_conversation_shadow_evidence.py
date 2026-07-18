import json
import math
import multiprocessing
import os
import threading

import pytest


def _proof(**overrides):
    from api.conversation_shadow_evidence import ShadowProofInput

    values = {
        "implementation_id": "bounded-view-v1",
        "schema_id": "agent-proof-v1",
        "profile": "default",
        "request_generation": 7,
        "candidate_complete": True,
        "oracle_complete": True,
        "lineage_unchanged": True,
        "gates_passed": True,
        "difference_reasons": (),
    }
    values.update(overrides)
    return ShadowProofInput(**values)


def _evidence_path(tmp_path):
    import api.conversation_shadow_evidence as shadow

    return tmp_path / shadow._FILENAME


def _record_in_process(state_dir, start, results, generation):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(state_dir, clock=lambda: 10.0)
    start.wait()
    results.put(store.record(_proof(request_generation=generation)).reason)


def _record_difference_in_process(state_dir, start, results):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(state_dir, clock=lambda: 10.0)
    start.wait()
    results.put(
        store.record(
            _proof(difference_reasons=("visible_identity_difference",))
        ).reason
    )


def _hold_advisory_lock(lock_path, acquired, release):
    import fcntl

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        acquired.set()
        release.wait(timeout=10)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _make_ready_store(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(shadow, "MIN_SAMPLE_COUNT", 1)
    monkeypatch.setattr(shadow, "MIN_OBSERVED_SPAN_SECONDS", 1)
    now = [0.0]
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    assert store.record(_proof()).reason == "insufficient_observed_span"
    now[0] = 1.0
    assert store.record(_proof(request_generation=8)).ready
    return store, now


def test_readiness_requires_a_thousand_zero_diff_samples_over_seven_days(tmp_path):
    from api.conversation_shadow_evidence import (
        MIN_OBSERVED_SPAN_SECONDS,
        MIN_SAMPLE_COUNT,
        ConversationShadowEvidenceStore,
    )

    now = [1_000_000.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    for generation in range(MIN_SAMPLE_COUNT - 1):
        readiness = store.record(_proof(request_generation=generation))
    assert not readiness.ready
    assert readiness.reason == "insufficient_samples"

    now[0] += MIN_OBSERVED_SPAN_SECONDS
    readiness = store.record(_proof(request_generation=MIN_SAMPLE_COUNT))
    assert readiness.ready
    assert readiness.sample_count == MIN_SAMPLE_COUNT
    assert readiness.observed_span_seconds == MIN_OBSERVED_SPAN_SECONDS
    assert readiness.difference_count == 0


def test_zero_timestamp_is_retained_as_first_sample_and_can_prove_finite_span(tmp_path):
    from api.conversation_shadow_evidence import (
        MIN_OBSERVED_SPAN_SECONDS,
        MIN_SAMPLE_COUNT,
        ConversationShadowEvidenceStore,
    )

    now = [0.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    store.record(_proof(request_generation=0))
    for generation in range(1, MIN_SAMPLE_COUNT - 1):
        store.record(_proof(request_generation=generation))
    now[0] = float(MIN_OBSERVED_SPAN_SECONDS)
    readiness = store.record(_proof(request_generation=MIN_SAMPLE_COUNT))
    assert readiness.ready
    assert readiness.first_sample_at == 0.0
    assert readiness.observed_span_seconds == MIN_OBSERVED_SPAN_SECONDS
    assert math.isfinite(readiness.observed_span_seconds)


def test_mixed_cohorts_and_incomplete_proofs_never_contribute_to_readiness(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    now = [10.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    assert store.record(_proof(candidate_complete=False)).reason == "incomplete_comparison"
    assert store.record(_proof(schema_id="agent-proof-v2")).sample_count == 1
    assert store.readiness(_proof()).reason == "evidence_missing"


def test_semantic_difference_latches_disable_and_persists_typed_reason(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    now = [10.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    readiness = store.record(_proof(difference_reasons=("visible_order_difference",)))
    assert not readiness.ready
    assert readiness.reason == "semantic_difference"
    assert readiness.disabled_at == 10.0
    assert readiness.difference_count == 1

    now[0] = 2_000_000.0
    assert store.record(_proof(request_generation=8)).reason == "latched_disabled"
    data = json.loads(_evidence_path(tmp_path).read_text())
    cohort = next(iter(data["cohorts"].values()))
    assert cohort["difference_reasons"] == {"visible_order_difference": 1}
    assert "default" not in json.dumps(data)
    assert "content" not in json.dumps(data)


def test_zero_disabled_timestamp_is_not_replaced_by_later_differences(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    now = [0.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    store.record(_proof(difference_reasons=("visible_order_difference",)))
    now[0] = 42.0
    readiness = store.record(
        _proof(
            request_generation=8,
            difference_reasons=("visible_count_difference",),
        )
    )
    assert readiness.disabled_at == 0.0
    assert readiness.difference_count == 2


def test_new_implementation_is_a_new_cohort_without_erasing_disabled_history(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    store.record(_proof(difference_reasons=("visible_count_difference",)))
    assert store.record(_proof(implementation_id="bounded-view-v2")).sample_count == 1
    data = json.loads(_evidence_path(tmp_path).read_text())
    assert len(data["cohorts"]) == 2


@pytest.mark.parametrize("payload", ["{", '{"version": 99, "cohorts": {}}'])
def test_corrupt_or_future_evidence_fails_closed(tmp_path, payload):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    path = _evidence_path(tmp_path)
    path.write_text(payload)
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.readiness(_proof()).reason in {"evidence_corrupt", "unsupported_version"}
    assert store.record(_proof()).reason in {"evidence_corrupt", "unsupported_version"}


def test_malformed_cohort_fields_fail_closed(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    path = _evidence_path(tmp_path)
    path.write_text(
        json.dumps({"version": 2, "cohorts": {"bad": {"sample_count": "many"}}})
    )
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.readiness(_proof()).reason == "evidence_corrupt"
    assert store.record(_proof()).reason == "evidence_corrupt"


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf")])
def test_invalid_injected_clock_values_fail_closed(tmp_path, value):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: value)
    assert store.record(_proof()).reason == "clock_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_sample_at", True),
        ("first_sample_at", float("nan")),
        ("last_sample_at", float("inf")),
        ("disabled_at", float("-inf")),
    ],
)
def test_non_finite_or_boolean_persisted_timestamps_fail_closed(tmp_path, field, value):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    store.record(_proof())
    path = _evidence_path(tmp_path)
    data = json.loads(path.read_text())
    cohort = next(iter(data["cohorts"].values()))
    cohort[field] = value
    path.write_text(json.dumps(data))
    assert store.readiness(_proof()).reason == "evidence_corrupt"


def test_persisted_sample_timestamps_must_be_ordered(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    store.record(_proof())
    path = _evidence_path(tmp_path)
    data = json.loads(path.read_text())
    cohort = next(iter(data["cohorts"].values()))
    cohort["first_sample_at"] = 11.0
    cohort["last_sample_at"] = 10.0
    path.write_text(json.dumps(data))
    assert store.readiness(_proof()).reason == "evidence_corrupt"


def test_readiness_rechecks_every_current_proof_prerequisite(tmp_path):
    from api.conversation_shadow_evidence import (
        MIN_OBSERVED_SPAN_SECONDS,
        MIN_SAMPLE_COUNT,
        ConversationShadowEvidenceStore,
    )

    now = [0.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    for generation in range(MIN_SAMPLE_COUNT - 1):
        store.record(_proof(request_generation=generation))
    now[0] = float(MIN_OBSERVED_SPAN_SECONDS)
    store.record(_proof(request_generation=MIN_SAMPLE_COUNT))

    for field in (
        "candidate_complete",
        "oracle_complete",
        "lineage_unchanged",
        "gates_passed",
    ):
        decision = store.readiness(_proof(**{field: False}))
        assert not decision.ready
        assert decision.reason == (
            "current_gates_failed" if field == "gates_passed" else "incomplete_comparison"
        )


def test_each_incomplete_or_gate_failure_is_not_recorded(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    for field in (
        "candidate_complete",
        "oracle_complete",
        "lineage_unchanged",
        "gates_passed",
    ):
        result = store.record(_proof(**{field: False}))
        assert result.reason == (
            "current_gates_failed" if field == "gates_passed" else "incomplete_comparison"
        )
    assert store.readiness(_proof()).reason == "evidence_missing"


def test_sample_rate_is_deterministic_and_bounded_across_stores(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    stores = [
        ConversationShadowEvidenceStore(tmp_path / name, clock=lambda: 10.0, sample_rate=5)
        for name in ("one", "two")
    ]
    outcomes = [
        [store.record(_proof(request_generation=generation)).reason for generation in range(50)]
        for store in stores
    ]
    assert outcomes[0] == outcomes[1]
    assert "not_sampled" in outcomes[0]
    assert "insufficient_samples" in outcomes[0]
    assert outcomes[0].count("insufficient_samples") < 20


def test_profile_isolation_and_persisted_shape_do_not_leak_profile_or_request_data(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    private_profile = "customer_secret_profile"
    store.record(_proof(profile=private_profile, request_generation=987654321))
    assert store.readiness(_proof(profile="other_profile")).reason == "evidence_missing"

    raw = _evidence_path(tmp_path).read_text()
    data = json.loads(raw)
    assert private_profile not in raw
    assert "987654321" not in raw
    assert set(data) == {
        "version",
        "cohorts",
        "disabled_tombstones",
        "tombstone_capacity_exhausted",
    }
    cohort_key, cohort = next(iter(data["cohorts"].items()))
    assert cohort_key.startswith("hmac-sha256:")
    assert set(cohort) == {
        "clock_regressed",
        "difference_count",
        "difference_reasons",
        "disabled_at",
        "first_sample_at",
        "implementation_id",
        "last_sample_at",
        "profile_binding",
        "sample_count",
        "schema_id",
        "generation_bloom",
    }


def test_free_form_difference_text_is_rejected_without_persistence(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    result = store.record(
        _proof(difference_reasons=("secret_transcript_words",))
    )
    assert result.reason == "invalid_difference_reason"
    assert not _evidence_path(tmp_path).exists()


def test_clock_regression_latches_fail_closed(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    now = [20.0]
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    store.record(_proof())
    now[0] = 19.0
    assert store.record(_proof(request_generation=8)).reason == "clock_regressed"
    now[0] = 30.0
    assert store.record(_proof(request_generation=9)).reason == "clock_regressed"


def test_atomic_write_failure_preserves_last_durable_aggregate(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.record(_proof()).sample_count == 1
    monkeypatch.setattr(shadow.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("nope")))
    assert store.record(_proof(request_generation=8)).reason == "persistence_failed"
    monkeypatch.undo()
    assert store.readiness(_proof()).sample_count == 1


def test_directory_fsync_failure_fails_publication_closed(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.record(_proof()).sample_count == 1
    monkeypatch.setattr(
        shadow,
        "_fsync_directory",
        lambda _directory: (_ for _ in ()).throw(OSError("no durable directory entry")),
    )
    result = store.record(_proof(request_generation=8))
    assert not result.ready
    assert result.reason == "persistence_failed"
    assert store.readiness(_proof()).reason == "persistence_failed"


def test_missing_advisory_lock_support_fails_closed(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(shadow, "_fcntl", None, raising=False)
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    result = store.record(_proof())
    assert result.reason == "advisory_lock_unavailable"
    assert not _evidence_path(tmp_path).exists()


def test_advisory_lock_acquisition_error_fails_closed(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(
        shadow._fcntl,
        "flock",
        lambda *_args: (_ for _ in ()).throw(OSError("lock denied")),
    )
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.record(_proof()).reason == "advisory_lock_unavailable"
    assert not _evidence_path(tmp_path).exists()


def test_concurrent_processes_do_not_lose_read_modify_write_updates(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_record_in_process,
            args=(str(tmp_path), start, results, generation),
        )
        for generation in range(8)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    completed = outcomes.count("insufficient_samples")
    assert completed >= 1
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.readiness(_proof()).sample_count == completed


def test_cross_process_match_cannot_overwrite_disable_latch(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_record_in_process,
            args=(str(tmp_path), start, results, 7),
        ),
        context.Process(
            target=_record_difference_in_process,
            args=(str(tmp_path), start, results),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert {results.get(timeout=2) for _ in processes} <= {
        "advisory_lock_unavailable",
        "insufficient_samples",
        "semantic_difference",
        "latched_disabled",
        "duplicate_generation",
    }
    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    readiness = store.readiness(_proof())
    assert not readiness.ready
    assert readiness.reason in {"evidence_missing", "insufficient_samples", "latched_disabled"}
    assert readiness.difference_count in {0, 1}


@pytest.mark.parametrize("clock", [lambda: float("nan"), lambda: -1.0])
def test_ready_evidence_rejects_invalid_or_backwards_current_clock(tmp_path, monkeypatch, clock):
    store, _now = _make_ready_store(tmp_path, monkeypatch)
    store._clock = clock

    decision = store.readiness(_proof())

    assert not decision.ready
    assert decision.reason in {"clock_invalid", "clock_regressed"}


def test_rekeyed_ready_cohort_with_wrong_build_identity_fails_closed(tmp_path, monkeypatch):
    store, _now = _make_ready_store(tmp_path, monkeypatch)
    path = _evidence_path(tmp_path)
    data = json.loads(path.read_text())
    cohort = next(iter(data["cohorts"].values()))
    cohort["implementation_id"] = "bounded-view-v0"
    path.write_text(json.dumps(data))

    decision = store.readiness(_proof())

    assert not decision.ready
    assert decision.reason == "evidence_corrupt"


def test_swapped_same_build_profile_cohorts_fail_closed(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(shadow, "MIN_SAMPLE_COUNT", 1)
    monkeypatch.setattr(shadow, "MIN_OBSERVED_SPAN_SECONDS", 1)
    now = [0.0]
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    unproven = _proof(profile="unproven", request_generation=7)
    proven = _proof(profile="proven", request_generation=8)
    store.record(unproven)
    store.record(proven)
    now[0] = 1.0
    assert store.record(_proof(profile="proven", request_generation=9)).ready

    path = _evidence_path(tmp_path)
    data = json.loads(path.read_text())
    secret = path.with_suffix(shadow._KEY_SUFFIX).read_bytes()
    unproven_key = shadow._cohort_key(unproven, secret)
    proven_key = shadow._cohort_key(proven, secret)
    data["cohorts"][unproven_key], data["cohorts"][proven_key] = (
        data["cohorts"][proven_key],
        data["cohorts"][unproven_key],
    )
    path.write_text(json.dumps(data))

    decision = store.readiness(unproven)
    assert not decision.ready
    assert decision.reason == "evidence_corrupt"


def test_same_request_generation_is_counted_at_most_once(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.record(_proof()).sample_count == 1
    replay = store.record(_proof())

    assert replay.sample_count == 1
    assert replay.reason == "duplicate_generation"


def test_duplicate_generation_with_semantic_difference_latches_disabled(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    assert store.record(_proof()).reason == "insufficient_samples"

    contradictory = store.record(
        _proof(difference_reasons=("visible_identity_difference",))
    )

    assert contradictory.reason == "semantic_difference"
    assert contradictory.difference_count == 1
    assert store.readiness(_proof()).reason == "latched_disabled"


@pytest.mark.parametrize(
    "field", ["candidate_complete", "oracle_complete", "lineage_unchanged", "gates_passed"]
)
def test_prerequisite_flags_must_be_real_booleans(tmp_path, field):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    result = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0).record(
        _proof(**{field: 1})
    )

    assert result.reason == "invalid_prerequisite"
    assert not _evidence_path(tmp_path).exists()


def test_cohort_retention_is_strictly_bounded(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    store = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0)
    for index in range(9):
        store.record(_proof(profile=f"profile-{index}", request_generation=index))

    data = json.loads(_evidence_path(tmp_path).read_text())
    assert len(data["cohorts"]) <= 8


def test_evicted_disabled_profile_cannot_rebuild_ready_evidence(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(shadow, "MIN_SAMPLE_COUNT", 1)
    monkeypatch.setattr(shadow, "MIN_OBSERVED_SPAN_SECONDS", 1)
    now = [0.0]
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    disabled = _proof(
        profile="disabled-profile",
        request_generation=1,
        difference_reasons=("visible_order_difference",),
    )
    assert store.record(disabled).reason == "semantic_difference"
    for index in range(8):
        now[0] = float(index + 1)
        store.record(_proof(profile=f"other-{index}", request_generation=index + 10))

    now[0] = 20.0
    assert store.record(_proof(profile="disabled-profile", request_generation=100)).reason == "latched_disabled"
    now[0] = 21.0
    decision = store.record(_proof(profile="disabled-profile", request_generation=101))
    assert not decision.ready
    assert decision.reason == "latched_disabled"


def test_tombstone_capacity_exhaustion_disables_all_readiness(tmp_path, monkeypatch):
    import api.conversation_shadow_evidence as shadow

    monkeypatch.setattr(shadow, "MIN_SAMPLE_COUNT", 1)
    monkeypatch.setattr(shadow, "MIN_OBSERVED_SPAN_SECONDS", 1)
    now = [0.0]
    store = shadow.ConversationShadowEvidenceStore(tmp_path, clock=lambda: now[0])
    for index in range(shadow.MAX_DISABLED_TOMBSTONES):
        now[0] = float(index)
        assert store.record(
            _proof(
                profile=f"disabled-{index}",
                request_generation=index,
                difference_reasons=("visible_count_difference",),
            )
        ).reason == "semantic_difference"

    exhausted = store.record(
        _proof(
            profile="overflow", request_generation=999, difference_reasons=("visible_count_difference",)
        )
    )
    assert exhausted.reason == "disabled_tombstone_capacity_exhausted"
    assert not store.readiness(_proof(profile="clean-profile", request_generation=1000)).ready
    assert store.readiness(_proof(profile="clean-profile", request_generation=1000)).reason == "disabled_tombstone_capacity_exhausted"


def test_profile_identity_uses_keyed_digest_not_enumerable_raw_sha256(tmp_path):
    import hashlib

    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    profile = "customer_secret_profile"
    proof = _proof(profile=profile)
    ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0).record(proof)
    raw = _evidence_path(tmp_path).read_text()
    data = json.loads(raw)
    persisted_key = next(iter(data["cohorts"]))
    raw_sha = hashlib.sha256(
        "\x1f".join((proof.implementation_id, proof.schema_id, profile)).encode("utf-8")
    ).hexdigest()

    assert profile not in raw
    assert persisted_key.startswith("hmac-sha256:")
    assert persisted_key != f"sha256:{raw_sha}"


def test_oversized_evidence_file_fails_closed_before_json_parsing(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    path = _evidence_path(tmp_path)
    path.write_bytes(b'{"version":1,"cohorts":{}}' + b" " * (128 * 1024))

    decision = ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0).readiness(_proof())

    assert not decision.ready
    assert decision.reason == "evidence_corrupt"


def test_cross_process_lock_contention_fails_closed_without_waiting(tmp_path):
    from api.conversation_shadow_evidence import ConversationShadowEvidenceStore

    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    lock_path = f"{_evidence_path(tmp_path)}.lock"
    holder = context.Process(target=_hold_advisory_lock, args=(str(lock_path), acquired, release))
    holder.start()
    assert acquired.wait(timeout=5)

    result = []
    done = threading.Event()

    def record() -> None:
        result.append(ConversationShadowEvidenceStore(tmp_path, clock=lambda: 10.0).record(_proof()))
        done.set()

    worker = threading.Thread(target=record)
    worker.start()
    completed_without_waiting = done.wait(timeout=0.3)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)

    assert holder.exitcode == 0
    assert completed_without_waiting
    assert result[0].reason == "advisory_lock_unavailable"
