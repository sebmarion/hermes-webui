from __future__ import annotations

import copy
import os
import sys
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest


@pytest.fixture
def managed_provider_models(monkeypatch):
    import api.config as api_config
    import managed_startup_provider_models as managed

    managed._reset_managed_startup_provider_models_for_tests()
    epoch = managed.ProcessEpoch(51001, "provider-model-start")
    core = {
        "alpha": ["old", "new-model"],
        "nous": ["vendor/new"],
        "unknown": ["not-curated"],
    }
    config = SimpleNamespace(
        _PROVIDER_MODELS=api_config._ProviderModelsCatalog(
            {
                "alpha": [{"id": "old", "label": "Old"}],
                "nous": [{"id": "@nous:vendor/old", "label": "Old via Nous"}],
            }
        ),
        _resolve_provider_alias=lambda provider: provider,
        _get_label_for_model=lambda model, _models: f"Label {model}",
    )
    monkeypatch.setattr(managed, "_current_process_epoch", lambda: epoch)
    monkeypatch.setattr(managed, "_startup_mutations_are_admitted", lambda: True)
    monkeypatch.setattr(managed, "_load_config_module", lambda: config)
    monkeypatch.setattr(
        managed,
        "_load_core_provider_models",
        lambda: copy.deepcopy(core),
    )
    yield managed, config, core, epoch
    managed._reset_managed_startup_provider_models_for_tests()


def test_provider_model_reconciler_is_typed_atomic_and_idempotent(
    managed_provider_models,
):
    managed, config, _core, epoch = managed_provider_models
    old_catalog = config._PROVIDER_MODELS
    old_copy = copy.deepcopy(old_catalog)
    old_snapshot = old_catalog._managed_provider_models_snapshot()

    absent = managed.verify_managed_startup_provider_models()
    first = managed.reconcile_managed_startup_provider_models()
    second = managed.reconcile_managed_startup_provider_models()
    complete = managed.verify_managed_startup_provider_models(first)

    assert (
        absent.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PROVED_ABSENT
    )
    assert isinstance(first, managed.ManagedStartupProviderModelsReceipt)
    assert first == second
    assert first.process_epoch == epoch
    assert first.provider_count == 2
    assert first.model_count == 4
    assert len(first.desired_sha256) == 64
    assert old_snapshot == old_copy
    assert config._PROVIDER_MODELS is old_catalog
    assert config._PROVIDER_MODELS["alpha"][-1] == {
        "id": "new-model",
        "label": "Label new-model",
    }
    assert config._PROVIDER_MODELS["nous"][-1]["id"] == "@nous:vendor/new"
    assert "unknown" not in config._PROVIDER_MODELS
    assert (
        complete.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PROVED_COMPLETE
    )
    with pytest.raises(FrozenInstanceError):
        first.desired_sha256 = "0" * 64


def test_provider_model_reconciler_reports_tamper_partial_and_repairs(
    managed_provider_models,
):
    managed, config, _core, _epoch = managed_provider_models
    receipt = managed.reconcile_managed_startup_provider_models()
    config._PROVIDER_MODELS["alpha"].pop()

    partial = managed.verify_managed_startup_provider_models(receipt)
    repaired = managed.reconcile_managed_startup_provider_models()

    assert (
        partial.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PARTIAL
    )
    assert repaired == receipt
    assert config._PROVIDER_MODELS["alpha"][-1]["id"] == "new-model"


def test_provider_model_reconciler_publish_exception_never_reports_success(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    before = copy.deepcopy(config._PROVIDER_MODELS)

    def fail_publish(_config, _target):
        raise RuntimeError("synthetic publish failure")

    monkeypatch.setattr(managed, "_publish_catalog", fail_publish)

    with pytest.raises(managed.ManagedStartupProviderModelsPostconditionError):
        managed.reconcile_managed_startup_provider_models()

    assert config._PROVIDER_MODELS == before
    verification = managed.verify_managed_startup_provider_models()
    assert (
        verification.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PARTIAL
    )


def test_provider_model_reconciler_rechecks_admission_before_publish(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    before = copy.deepcopy(config._PROVIDER_MODELS)
    admission_checks = iter((True, False))
    monkeypatch.setattr(
        managed,
        "_startup_mutations_are_admitted",
        lambda: next(admission_checks),
    )

    with pytest.raises(managed.ManagedStartupProviderModelsAdmissionError):
        managed.reconcile_managed_startup_provider_models()

    assert config._PROVIDER_MODELS == before


def test_provider_model_reconciler_rejects_same_epoch_upstream_drift(
    managed_provider_models,
):
    managed, config, core, _epoch = managed_provider_models
    first = managed.reconcile_managed_startup_provider_models()
    core["alpha"].append("drifted")

    with pytest.raises(managed.ManagedStartupProviderModelsDesiredDriftError):
        managed.reconcile_managed_startup_provider_models()

    verification = managed.verify_managed_startup_provider_models(first)
    assert (
        verification.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS
    )
    assert all(model["id"] != "drifted" for model in config._PROVIDER_MODELS["alpha"])


def test_provider_model_reconciler_rechecks_upstream_after_publish(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    stable = {"alpha": ["old", "before-publish"]}
    drifted = {"alpha": ["old", "after-publish"]}
    reads = 0

    def drift_after_initial_capture():
        nonlocal reads
        reads += 1
        return copy.deepcopy(stable if reads <= 2 else drifted)

    monkeypatch.setattr(
        managed,
        "_load_core_provider_models",
        drift_after_initial_capture,
    )

    with pytest.raises(managed.ManagedStartupProviderModelsDesiredDriftError):
        managed.reconcile_managed_startup_provider_models()

    assert any(
        model["id"] == "before-publish" for model in config._PROVIDER_MODELS["alpha"]
    )
    assert (
        managed.verify_managed_startup_provider_models().outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS
    )


def test_provider_model_complete_fast_path_rechecks_upstream_before_success(
    managed_provider_models,
    monkeypatch,
):
    managed, config, core, _epoch = managed_provider_models
    managed.reconcile_managed_startup_provider_models()
    catalog = config._PROVIDER_MODELS
    real_snapshot = catalog._managed_provider_models_snapshot
    snapshot_reads = 0

    def drift_during_target_verification():
        nonlocal snapshot_reads
        snapshot_reads += 1
        result = real_snapshot()
        if snapshot_reads == 1:
            core["alpha"].append("drift-during-verification")
        return result

    monkeypatch.setattr(
        catalog,
        "_managed_provider_models_snapshot",
        drift_during_target_verification,
    )
    monkeypatch.setattr(
        managed,
        "_publish_catalog",
        lambda *_args: pytest.fail("complete fast path attempted publication"),
    )

    with pytest.raises(managed.ManagedStartupProviderModelsDesiredDriftError):
        managed.reconcile_managed_startup_provider_models()


def test_provider_model_reconciler_recaptures_after_new_process_epoch(
    managed_provider_models,
    monkeypatch,
):
    managed, config, core, first_epoch = managed_provider_models
    first = managed.reconcile_managed_startup_provider_models()
    second_epoch = managed.ProcessEpoch(51002, "provider-model-fork")
    core["alpha"].append("after-fork")
    monkeypatch.setattr(
        managed,
        "_current_process_epoch",
        lambda: second_epoch,
    )

    foreign = managed.verify_managed_startup_provider_models(first)
    second = managed.reconcile_managed_startup_provider_models()

    assert (
        foreign.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS
    )
    assert second.process_epoch == second_epoch
    assert second.process_epoch != first_epoch
    assert any(
        model["id"] == "after-fork" for model in config._PROVIDER_MODELS["alpha"]
    )


def test_provider_model_reconciler_fork_hook_resets_state_and_lock(
    managed_provider_models,
):
    managed, _config, _core, _epoch = managed_provider_models
    managed.reconcile_managed_startup_provider_models()
    inherited_lock = managed._STATE_LOCK

    managed._reset_after_fork()
    verification = managed.verify_managed_startup_provider_models()

    assert managed._STATE_LOCK is not inherited_lock
    assert (
        verification.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PROVED_ABSENT
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_provider_model_receipt_is_ambiguous_in_actual_fork(
    managed_provider_models,
):
    managed, _config, _core, _epoch = managed_provider_models
    receipt = managed.reconcile_managed_startup_provider_models()
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(read_fd)
            outcome = managed.verify_managed_startup_provider_models(
                receipt
            ).outcome.value
            os.write(write_fd, outcome.encode("ascii"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        child_outcome = os.read(read_fd, 128).decode("ascii")
    finally:
        os.close(read_fd)
        waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert (
        child_outcome
        == managed.ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS.value
    )


def test_provider_model_reconciler_unavailable_source_is_terminal(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    before = copy.deepcopy(config._PROVIDER_MODELS)

    def unavailable():
        raise ImportError("core unavailable")

    monkeypatch.setattr(managed, "_load_core_provider_models", unavailable)

    with pytest.raises(managed.ManagedStartupProviderModelsUnavailable):
        managed.reconcile_managed_startup_provider_models()

    verification = managed.verify_managed_startup_provider_models()
    assert (
        verification.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS
    )
    assert config._PROVIDER_MODELS == before


def test_provider_model_reconciler_rejects_unstable_or_unbounded_desired(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    calls = 0

    def unstable():
        nonlocal calls
        calls += 1
        return {"alpha": ["one" if calls % 2 else "two"]}

    monkeypatch.setattr(managed, "_load_core_provider_models", unstable)
    with pytest.raises(managed.ManagedStartupProviderModelsUnavailable):
        managed.reconcile_managed_startup_provider_models()

    monkeypatch.setattr(
        managed,
        "_load_core_provider_models",
        lambda: {
            f"provider-{index}": [] for index in range(managed.MAX_PROVIDER_COUNT + 1)
        },
    )
    with pytest.raises(managed.ManagedStartupProviderModelsUnavailable):
        managed.reconcile_managed_startup_provider_models()

    assert config._PROVIDER_MODELS == {
        "alpha": [{"id": "old", "label": "Old"}],
        "nous": [{"id": "@nous:vendor/old", "label": "Old via Nous"}],
    }


def test_provider_model_reconciler_trims_model_ids_and_rejects_blank_ids(
    managed_provider_models,
    monkeypatch,
):
    managed, config, _core, _epoch = managed_provider_models
    monkeypatch.setattr(
        managed,
        "_load_core_provider_models",
        lambda: {"alpha": ["  trimmed-model  "]},
    )

    managed.reconcile_managed_startup_provider_models()

    assert config._PROVIDER_MODELS["alpha"][-1]["id"] == "trimmed-model"

    managed._reset_managed_startup_provider_models_for_tests()
    monkeypatch.setattr(
        managed,
        "_load_core_provider_models",
        lambda: {"alpha": ["   "]},
    )
    with pytest.raises(managed.ManagedStartupProviderModelsUnavailable):
        managed.reconcile_managed_startup_provider_models()


def test_provider_model_reconciler_concurrent_readers_never_see_partial_target(
    managed_provider_models,
):
    managed, config, _core, _epoch = managed_provider_models
    before = copy.deepcopy(config._PROVIDER_MODELS)
    cached_alias = config._PROVIDER_MODELS
    failures: list[object] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            observed = cached_alias._managed_provider_models_snapshot()
            alpha_ids = tuple(model["id"] for model in observed["alpha"])
            nous_ids = tuple(model["id"] for model in observed["nous"])
            if (alpha_ids, nous_ids) not in {
                (("old",), ("@nous:vendor/old",)),
                (
                    ("old", "new-model"),
                    ("@nous:vendor/old", "@nous:vendor/new"),
                ),
            }:
                failures.append((alpha_ids, nous_ids))
                stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        managed.reconcile_managed_startup_provider_models()
    finally:
        stop.set()
        thread.join(timeout=5)

    assert failures == []
    assert cached_alias is config._PROVIDER_MODELS
    assert before == {
        "alpha": [{"id": "old", "label": "Old"}],
        "nous": [{"id": "@nous:vendor/old", "label": "Old via Nous"}],
    }


def test_provider_model_catalog_proxy_preserves_unmanaged_mutation_api(
    managed_provider_models,
):
    _managed, config, _core, _epoch = managed_provider_models
    cached_alias = config._PROVIDER_MODELS

    cached_alias["alpha"].append({"id": "legacy-list", "label": "Legacy"})
    cached_alias["beta"] = [{"id": "legacy-map", "label": "Legacy"}]

    assert config._PROVIDER_MODELS["alpha"][-1]["id"] == "legacy-list"
    assert config._PROVIDER_MODELS["beta"][0]["id"] == "legacy-map"


def test_onboarding_cached_catalog_alias_observes_atomic_snapshot_swap():
    import api.config as api_config
    import api.onboarding as onboarding

    catalog = api_config._PROVIDER_MODELS
    original = copy.deepcopy(catalog)
    replacement = copy.deepcopy(catalog)
    replacement["anthropic"] = list(replacement["anthropic"]) + [
        {"id": "managed-snapshot-test", "label": "Managed snapshot test"}
    ]
    try:
        catalog._replace_managed_provider_models_snapshot(replacement)
        setup = onboarding._build_setup_catalog({})
        anthropic = next(
            provider for provider in setup["providers"] if provider["id"] == "anthropic"
        )
        assert any(
            model["id"] == "managed-snapshot-test" for model in anthropic["models"]
        )
    finally:
        catalog._replace_managed_provider_models_snapshot(original)


def test_config_provider_model_adapters_return_strict_typed_results(
    managed_provider_models,
    monkeypatch,
):
    import api.config as api_config

    managed, _config, _core, _epoch = managed_provider_models
    monkeypatch.setattr(
        api_config,
        "_startup_mutations_are_admitted",
        lambda: True,
    )

    receipt = api_config.seed_startup_provider_models()
    verification = api_config.verify_startup_provider_models(receipt)

    assert isinstance(
        receipt,
        managed.ManagedStartupProviderModelsReceipt,
    )
    assert (
        verification.outcome
        is managed.ManagedStartupProviderModelsVerificationOutcome.PROVED_COMPLETE
    )


def test_config_provider_model_adapter_fails_closed_when_unavailable(
    managed_provider_models,
    monkeypatch,
):
    import api.config as api_config

    monkeypatch.setattr(
        api_config,
        "_startup_mutations_are_admitted",
        lambda: True,
    )
    monkeypatch.setitem(sys.modules, "managed_startup_provider_models", None)

    with pytest.raises(RuntimeError, match="reconciler is unavailable"):
        api_config.seed_startup_provider_models()
