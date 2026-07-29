from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import deferred_release_manifest
import deferred_startup_replay as replay_module
from deferred_startup_file_driver import (
    DeferredStartupFileDriver,
    DeferredStartupFileDriverError,
)
from deferred_startup_replay import (
    AFTER_INTENT,
    DeferredStartupCrash,
    DeferredStartupBindingError,
    DeferredStartupIndeterminateError,
    DeferredStartupManifestReceipt,
    DeferredStartupStep,
    Reconciliation,
    replay_deferred_startup,
)


TRANSACTION_ID = "attempt-journal-transaction-" + ("t" * 32)
EPOCH_ONE = "controller-process-epoch-one-" + ("a" * 32)
EPOCH_TWO = "controller-process-epoch-two-" + ("b" * 32)


def _receipt() -> DeferredStartupManifestReceipt:
    return DeferredStartupManifestReceipt(
        transaction_id=TRANSACTION_ID,
        version=deferred_release_manifest.MANIFEST_VERSION,
        sha256=deferred_release_manifest.deferred_release_manifest_sha256(),
    )


def _journal_path(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    return parent / "deferred-startup.json"


def _driver(path: Path) -> DeferredStartupFileDriver:
    return DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
    )


def _step(
    reconciliations: list[Reconciliation],
    mutations: list[str],
    *,
    policy=None,
) -> DeferredStartupStep:
    if policy is None:
        policy = replay_module.PriorCompletionAbsentPolicy.DENY
    return DeferredStartupStep(
        name="plugins",
        mutator=lambda: mutations.append("plugins"),
        reconciler=lambda: reconciliations.pop(0),
        prior_completion_absent_policy=policy,
    )


def _replay(
    path: Path,
    process_epoch: str,
    step: DeferredStartupStep,
):
    return replay_deferred_startup(
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        process_epoch=process_epoch,
        steps=(step,),
        driver=_driver(path),
    )


@pytest.mark.parametrize(
    "process_epoch",
    (
        "",
        "pid-123",
        "contains spaces " + ("x" * 32),
        "x" * 129,
        123,
    ),
)
def test_process_epoch_is_strictly_validated_before_driver_or_mutation(
    tmp_path,
    process_epoch,
):
    path = _journal_path(tmp_path)
    mutations: list[str] = []

    with pytest.raises(
        (DeferredStartupBindingError, DeferredStartupFileDriverError),
        match="process epoch",
    ):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=process_epoch,
            steps=(
                _step(
                    [Reconciliation.PROVED_COMPLETE],
                    mutations,
                ),
            ),
            driver=_driver(path),
        )

    assert mutations == []


def test_file_driver_rejects_v1_journal_instead_of_migrating_it(tmp_path):
    path = _journal_path(tmp_path)
    v1 = {
        "version": 1,
        "generation": 0,
        "previous_sha256": "0" * 64,
        "transaction_id": TRANSACTION_ID,
        "manifest_receipt": {
            "version": _receipt().version,
            "sha256": _receipt().sha256,
        },
        "steps": {},
    }
    path.write_text(json.dumps(v1, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="schema|version",
    ):
        _driver(path)


def test_new_epoch_recovers_prior_completion_without_mutation(tmp_path):
    path = _journal_path(tmp_path)
    first_mutations: list[str] = []
    _replay(
        path,
        EPOCH_ONE,
        _step(
            [Reconciliation.PROVED_COMPLETE],
            first_mutations,
        ),
    )
    second_mutations: list[str] = []

    result = _replay(
        path,
        EPOCH_TWO,
        _step(
            [Reconciliation.PROVED_COMPLETE],
            second_mutations,
        ),
    )

    assert result.completed == ("plugins",)
    assert first_mutations == ["plugins"]
    assert second_mutations == []
    attempts = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"]
    assert attempts == [
        {
            "attempt": 1,
            "process_epoch": EPOCH_ONE,
            "prior_completion_absent_policy": "deny",
            "intent": {"generation": 1},
            "completion": {"recovered": False, "generation": 2},
        },
        {
            "attempt": 2,
            "process_epoch": EPOCH_TWO,
            "prior_completion_absent_policy": "deny",
            "intent": {"generation": 3},
            "completion": {"recovered": True, "generation": 4},
        },
    ]


def test_new_epoch_prior_completion_absent_fails_closed_without_policy(tmp_path):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    mutations: list[str] = []

    with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
        _replay(
            path,
            EPOCH_TWO,
            _step([Reconciliation.PROVED_ABSENT], mutations),
        )

    assert mutations == []
    attempts = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"]
    assert attempts[-1] == {
        "attempt": 2,
        "process_epoch": EPOCH_TWO,
        "prior_completion_absent_policy": "deny",
        "intent": {"generation": 3},
        "indeterminate": {
            "reason": "prior-completion-absent-policy-denied",
            "generation": 4,
        },
    }


def test_new_epoch_prior_completion_absent_mutates_only_with_explicit_policy(
    tmp_path,
):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    mutations: list[str] = []

    _replay(
        path,
        EPOCH_TWO,
        _step(
            [
                Reconciliation.PROVED_ABSENT,
                Reconciliation.PROVED_COMPLETE,
            ],
            mutations,
            policy=replay_module.PriorCompletionAbsentPolicy.ALLOW_RERUN,
        ),
    )

    assert mutations == ["plugins"]
    attempts = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"]
    assert attempts[-1] == {
        "attempt": 2,
        "process_epoch": EPOCH_TWO,
        "prior_completion_absent_policy": "allow-rerun",
        "intent": {"generation": 3},
        "completion": {"recovered": False, "generation": 4},
    }


@pytest.mark.parametrize(
    ("policy", "should_mutate"),
    (
        (replay_module.PriorCompletionAbsentPolicy.DENY, False),
        (replay_module.PriorCompletionAbsentPolicy.ALLOW_RERUN, True),
    ),
)
def test_new_epoch_prior_unresolved_absence_obeys_explicit_policy(
    tmp_path,
    policy,
    should_mutate,
):
    path = _journal_path(tmp_path)

    def crash_after_intent(point, _step_name):
        if point == AFTER_INTENT:
            raise DeferredStartupCrash(point)

    with pytest.raises(DeferredStartupCrash):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=EPOCH_ONE,
            steps=(
                _step(
                    [Reconciliation.PROVED_ABSENT],
                    [],
                    policy=policy,
                ),
            ),
            driver=_driver(path),
            crash_hook=crash_after_intent,
        )

    mutations: list[str] = []
    replay = lambda: _replay(
        path,
        EPOCH_TWO,
        _step(
            (
                [
                    Reconciliation.PROVED_ABSENT,
                    Reconciliation.PROVED_COMPLETE,
                ]
                if should_mutate
                else [Reconciliation.PROVED_ABSENT]
            ),
            mutations,
            policy=policy,
        ),
    )
    if should_mutate:
        replay()
    else:
        with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
            replay()

    assert mutations == (["plugins"] if should_mutate else [])
    terminal = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"][-1]
    if should_mutate:
        assert terminal["completion"]["recovered"] is False
    else:
        assert terminal["indeterminate"]["reason"] == (
            "prior-intent-absent-policy-denied"
        )


@pytest.mark.parametrize(
    "reconciliation",
    (Reconciliation.PARTIAL, Reconciliation.AMBIGUOUS),
)
def test_new_epoch_uncertain_prior_completion_records_indeterminate(
    tmp_path,
    reconciliation,
):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )

    with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
        _replay(path, EPOCH_TWO, _step([reconciliation], []))

    attempt = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"][-1]
    assert attempt["process_epoch"] == EPOCH_TWO
    assert attempt["indeterminate"]["reason"] == reconciliation.value


def test_new_epoch_reconciler_failure_records_indeterminate_after_fresh_intent(
    tmp_path,
):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )

    with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
        _replay(
            path,
            EPOCH_TWO,
            DeferredStartupStep(
                name="plugins",
                mutator=lambda: pytest.fail("must not mutate"),
                reconciler=lambda: (_ for _ in ()).throw(
                    RuntimeError("synthetic reconciliation failure")
                ),
            ),
        )

    attempt = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"][-1]
    assert attempt["intent"]["generation"] == 3
    assert attempt["indeterminate"] == {
        "reason": "reconciler-failed",
        "generation": 4,
    }


def test_new_epoch_crash_after_intent_never_mutates_under_old_receipt(tmp_path):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    mutations: list[str] = []

    def crash_after_intent(point, step_name):
        assert step_name == "plugins"
        if point == AFTER_INTENT:
            raise DeferredStartupCrash(point)

    with pytest.raises(DeferredStartupCrash, match=AFTER_INTENT):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=EPOCH_TWO,
            steps=(
                _step(
                    [Reconciliation.PROVED_ABSENT],
                    mutations,
                    policy=replay_module.PriorCompletionAbsentPolicy.ALLOW_RERUN,
                ),
            ),
            driver=_driver(path),
            crash_hook=crash_after_intent,
        )

    assert mutations == []
    attempts = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"]
    assert attempts[-1] == {
        "attempt": 2,
        "process_epoch": EPOCH_TWO,
        "prior_completion_absent_policy": "allow-rerun",
        "intent": {"generation": 3},
    }


def test_reused_epoch_with_changed_strict_policy_fails_closed(tmp_path):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )

    def crash_after_intent(point, _step_name):
        if point == AFTER_INTENT:
            raise DeferredStartupCrash(point)

    with pytest.raises(DeferredStartupCrash):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=EPOCH_TWO,
            steps=(
                _step(
                    [Reconciliation.PROVED_ABSENT],
                    [],
                    policy=replay_module.PriorCompletionAbsentPolicy.ALLOW_RERUN,
                ),
            ),
            driver=_driver(path),
            crash_hook=crash_after_intent,
        )

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="policy binding",
    ):
        _replay(
            path,
            EPOCH_TWO,
            _step([Reconciliation.PROVED_ABSENT], []),
        )


def test_deny_resume_after_fresh_intent_never_mutates_absent_prior_effect(
    tmp_path,
):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )

    def crash_after_intent(point, _step_name):
        if point == AFTER_INTENT:
            raise DeferredStartupCrash(point)

    with pytest.raises(DeferredStartupCrash):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=EPOCH_TWO,
            steps=(
                _step(
                    [Reconciliation.PROVED_ABSENT],
                    [],
                    policy=replay_module.PriorCompletionAbsentPolicy.DENY,
                ),
            ),
            driver=_driver(path),
            crash_hook=crash_after_intent,
        )

    mutations: list[str] = []
    with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
        _replay(
            path,
            EPOCH_TWO,
            _step(
                [Reconciliation.PROVED_ABSENT],
                mutations,
                policy=replay_module.PriorCompletionAbsentPolicy.DENY,
            ),
        )

    assert mutations == []
    attempt = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"][-1]
    assert attempt["indeterminate"]["reason"] == (
        "prior-completion-absent-policy-denied"
    )


def test_completed_attempt_is_never_reused_or_rerun_in_same_epoch(tmp_path):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    mutations: list[str] = []

    with pytest.raises(DeferredStartupIndeterminateError, match="plugins"):
        _replay(
            path,
            EPOCH_ONE,
            _step([Reconciliation.PROVED_ABSENT], mutations),
        )

    assert mutations == []
    attempts = json.loads(path.read_bytes())["steps"]["plugins"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["completion"] == {
        "recovered": False,
        "generation": 2,
    }


def test_driver_rejects_invalid_process_epoch_on_every_operation(tmp_path):
    path = _journal_path(tmp_path)
    driver = _driver(path)
    invalid_epoch = "pid-123"

    operations = (
        lambda: driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            invalid_epoch,
            "plugins",
        ),
        lambda: driver.record_intent(
            TRANSACTION_ID,
            _receipt(),
            invalid_epoch,
            "plugins",
        ),
        lambda: driver.record_completion(
            TRANSACTION_ID,
            _receipt(),
            invalid_epoch,
            "plugins",
            recovered=False,
        ),
        lambda: driver.record_indeterminate(
            TRANSACTION_ID,
            _receipt(),
            invalid_epoch,
            "plugins",
            reason="ambiguous",
        ),
    )

    for operation in operations:
        with pytest.raises(DeferredStartupFileDriverError, match="process epoch"):
            operation()


def test_one_driver_accepts_distinct_validated_epochs_without_reconstruction(
    tmp_path,
):
    path = _journal_path(tmp_path)
    driver = _driver(path)

    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
        recovered=False,
    )
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_TWO,
        "plugins",
    )

    assert (
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_TWO,
            "plugins",
        ).attempt_number
        == 2
    )


def test_record_intent_reused_epoch_rejects_policy_conflict_and_terminal_reuse(
    tmp_path,
):
    path = _journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )

    with pytest.raises(DeferredStartupFileDriverError, match="policy"):
        driver.record_intent(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_ONE,
            "plugins",
            prior_completion_absent_policy=(
                replay_module.PriorCompletionAbsentPolicy.ALLOW_RERUN
            ),
        )

    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
        recovered=False,
    )
    with pytest.raises(DeferredStartupFileDriverError, match="terminal|reuse"):
        driver.record_intent(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_ONE,
            "plugins",
        )


def test_stale_epoch_terminal_is_rejected_before_publish_and_fresh_reopen(
    tmp_path,
):
    path = _journal_path(tmp_path)
    stale = _driver(path)
    fresh = _driver(path)
    stale.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    fresh.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_TWO,
        "plugins",
    )
    before = path.read_bytes()

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="newest|stale",
    ):
        stale.record_intent(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_ONE,
            "plugins",
        )
    assert path.read_bytes() == before

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="newest|monotonic|stale",
    ):
        stale.record_completion(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_ONE,
            "plugins",
            recovered=True,
        )

    assert path.read_bytes() == before
    reopened = _driver(path)
    state = reopened.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_TWO,
        "plugins",
    )
    assert state.attempt_number == 2
    assert state.intent is True
    assert state.completion is False


def test_superseded_epoch_read_rejects_before_reconciler_or_mutator(tmp_path):
    path = _journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_TWO,
        "plugins",
    )
    reconciliations: list[str] = []
    mutations: list[str] = []

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="newest|stale|superseded",
    ):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=EPOCH_ONE,
            steps=(
                DeferredStartupStep(
                    name="plugins",
                    reconciler=lambda: (
                        reconciliations.append("called") or Reconciliation.PROVED_ABSENT
                    ),
                    mutator=lambda: mutations.append("called"),
                ),
            ),
            driver=_driver(path),
        )

    assert reconciliations == []
    assert mutations == []


def test_fresh_reopen_rejects_terminal_generation_after_newer_attempt(
    tmp_path,
):
    path = _journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_TWO,
        "plugins",
    )
    payload = json.loads(path.read_bytes())
    payload["previous_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["generation"] = 3
    payload["steps"]["plugins"]["attempts"][0]["completion"] = {
        "recovered": True,
        "generation": 3,
    }
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="terminal generation|reordered",
    ):
        _driver(path)


def test_driver_rejects_any_attempt_after_indeterminate_epoch(tmp_path):
    path = _journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
    )
    driver.record_indeterminate(
        TRANSACTION_ID,
        _receipt(),
        EPOCH_ONE,
        "plugins",
        reason="ambiguous",
    )

    with pytest.raises(DeferredStartupFileDriverError, match="indeterminate"):
        driver.record_intent(
            TRANSACTION_ID,
            _receipt(),
            EPOCH_TWO,
            "plugins",
        )


def test_driver_rejects_attempt_reordering_and_attestation_binds_topology(tmp_path):
    path = _journal_path(tmp_path)
    _replay(
        path,
        EPOCH_ONE,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    driver = _driver(path)
    before = driver.attestation_receipt()

    _replay(
        path,
        EPOCH_TWO,
        _step([Reconciliation.PROVED_COMPLETE], []),
    )
    after = _driver(path).attestation_receipt()

    assert before.latest_process_epoch == EPOCH_ONE
    assert before.attempt_count == 1
    assert after.latest_process_epoch == EPOCH_TWO
    assert after.attempt_count == 2
    assert before.attempt_topology_sha256 != after.attempt_topology_sha256
    payload = json.loads(path.read_bytes())
    payload["steps"]["plugins"]["attempts"][1]["attempt"] = 1
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="attempt|anchor|rollback|fork",
    ):
        _driver(path)
