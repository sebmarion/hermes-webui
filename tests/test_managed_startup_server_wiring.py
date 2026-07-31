"""Focused server ownership tests for managed startup coordination."""

from types import SimpleNamespace

import pytest


class _Driver:
    def read_step_state(self, *_args, **_kwargs):
        raise AssertionError("not used by coordinator installation")

    def record_intent(self, *_args, **_kwargs):
        raise AssertionError("not used by coordinator installation")

    def record_completion(self, *_args, **_kwargs):
        raise AssertionError("not used by coordinator installation")

    def record_indeterminate(self, *_args, **_kwargs):
        raise AssertionError("not used by coordinator installation")


def _reset_server_managed_startup(monkeypatch, server):
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_DRIVER", None)
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_STEPS", None)
    monkeypatch.setattr(
        server,
        "_MANAGED_STARTUP_COORDINATOR",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_MANAGED_STARTUP_ACCEPTANCE_EVIDENCE",
        None,
        raising=False,
    )


def test_managed_accept_builds_retains_and_configures_production_coordinator(
    monkeypatch,
):
    import managed_startup_coordinator
    import server

    _reset_server_managed_startup(monkeypatch, server)
    monkeypatch.setattr(
        server.api_config,
        "startup_run_admission_is_closed",
        lambda: True,
    )
    driver = _Driver()
    coordinator = SimpleNamespace(driver=driver, steps=())
    builds = []
    acceptors = []
    monkeypatch.setattr(
        managed_startup_coordinator,
        "build_production_managed_startup_coordinator",
        lambda: builds.append("built") or coordinator,
    )
    monkeypatch.setattr(
        server.api_config,
        "configure_startup_acceptor",
        acceptors.append,
    )

    assert server._prepare_startup_mutators() == "deferred"
    assert builds == []
    with pytest.raises(RuntimeError, match="step mapping changed"):
        server._run_managed_deferred_startup("t" * 40)
    with pytest.raises(RuntimeError, match="step mapping changed"):
        server._run_managed_deferred_startup("t" * 40)

    assert builds == ["built"]
    assert server._MANAGED_STARTUP_COORDINATOR is coordinator
    assert server._DEFERRED_STARTUP_REPLAY_DRIVER is driver
    assert server._DEFERRED_STARTUP_REPLAY_STEPS is coordinator.steps
    assert acceptors == [server._run_managed_deferred_startup]


def test_managed_replay_accepts_wrapped_mutators_and_publishes_typed_evidence(
    monkeypatch,
):
    import deferred_release_manifest
    import server
    from deferred_startup_replay import (
        DeferredStartupManifestReceipt,
        DeferredStartupStep,
        Reconciliation,
    )

    _reset_server_managed_startup(monkeypatch, server)

    def canonical_first():
        return None

    def canonical_second():
        return None

    def wrapped_first():
        return canonical_first()

    def wrapped_second():
        return canonical_second()

    steps = (
        DeferredStartupStep(
            name="first",
            mutator=wrapped_first,
            reconciler=lambda: Reconciliation.PROVED_COMPLETE,
        ),
        DeferredStartupStep(
            name="second",
            mutator=wrapped_second,
            reconciler=lambda: Reconciliation.PROVED_COMPLETE,
        ),
    )
    driver = _Driver()
    driver_attestation = object()
    typed_receipt_bundle = object()
    process_receipt = server._ManagedDeferredStartupProcessReceipt(
        version=1,
        pid=123,
        process_epoch="process-epoch",
        process_start_token_sha256="a" * 64,
    )
    manifest_receipt = DeferredStartupManifestReceipt(
        transaction_id="transaction-id",
        version=deferred_release_manifest.MANIFEST_VERSION,
        sha256=deferred_release_manifest.deferred_release_manifest_sha256(),
    )
    coordinator = SimpleNamespace(
        transaction_id="transaction-id",
        manifest_receipt=manifest_receipt,
        driver=driver,
        steps=steps,
        driver_attestation=lambda: driver_attestation,
        step_receipt_bundle=lambda: typed_receipt_bundle,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_DRIVER", driver)
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_STEPS", steps)
    monkeypatch.setattr(server, "_MANAGED_STARTUP_COORDINATOR", coordinator)
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (
            ("first", canonical_first),
            ("second", canonical_second),
        ),
    )
    monkeypatch.setattr(
        server,
        "_managed_deferred_startup_process_receipt",
        lambda: process_receipt,
    )
    monkeypatch.setattr(
        server,
        "replay_deferred_startup",
        lambda **_kwargs: SimpleNamespace(
            transaction_id="transaction-id",
            completed=("first", "second"),
        ),
    )

    result = server._run_managed_deferred_startup("transaction-id")
    evidence = server.managed_startup_acceptance_evidence()

    assert result["status"] == "started"
    assert result["transaction_id"] == "transaction-id"
    assert result["completed"] == ["first", "second"]
    assert result["acceptance_evidence"] is evidence
    assert evidence.driver_attestation is driver_attestation
    assert evidence.step_receipt_bundle is typed_receipt_bundle
    assert evidence.process_receipt is process_receipt
    assert server.managed_startup_acceptance_evidence() is evidence


@pytest.mark.parametrize("mismatch", ("transaction", "manifest"))
def test_managed_replay_rejects_coordinator_binding_mismatch_before_replay(
    monkeypatch,
    mismatch,
):
    import deferred_release_manifest
    import server
    from deferred_startup_replay import (
        DeferredStartupManifestReceipt,
        DeferredStartupStep,
        Reconciliation,
    )

    _reset_server_managed_startup(monkeypatch, server)

    def first():
        return None

    driver = _Driver()
    steps = (
        DeferredStartupStep(
            name="first",
            mutator=first,
            reconciler=lambda: Reconciliation.PROVED_COMPLETE,
        ),
    )
    transaction_id = "transaction-id"
    manifest_receipt = DeferredStartupManifestReceipt(
        transaction_id=transaction_id,
        version=deferred_release_manifest.MANIFEST_VERSION,
        sha256=deferred_release_manifest.deferred_release_manifest_sha256(),
    )
    if mismatch == "transaction":
        coordinator_transaction = "different-transaction"
        coordinator_manifest = manifest_receipt
    else:
        coordinator_transaction = transaction_id
        coordinator_manifest = DeferredStartupManifestReceipt(
            transaction_id=transaction_id,
            version=deferred_release_manifest.MANIFEST_VERSION,
            sha256="f" * 64,
        )
    coordinator = SimpleNamespace(
        transaction_id=coordinator_transaction,
        manifest_receipt=coordinator_manifest,
        driver=driver,
        steps=steps,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_DRIVER", driver)
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_STEPS", steps)
    monkeypatch.setattr(server, "_MANAGED_STARTUP_COORDINATOR", coordinator)
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (("first", first),),
    )
    monkeypatch.setattr(
        server,
        "replay_deferred_startup",
        lambda **_kwargs: pytest.fail("binding mismatch must precede replay"),
    )

    with pytest.raises(
        RuntimeError,
        match="managed startup coordinator binding changed",
    ):
        server._run_managed_deferred_startup(transaction_id)


def test_managed_replay_rejects_noncanonical_step_order(monkeypatch):
    import server
    from deferred_startup_replay import DeferredStartupStep, Reconciliation

    _reset_server_managed_startup(monkeypatch, server)

    def first():
        return None

    def second():
        return None

    steps = tuple(
        DeferredStartupStep(
            name=name,
            mutator=mutator,
            reconciler=lambda: Reconciliation.PROVED_COMPLETE,
        )
        for name, mutator in (("second", second), ("first", first))
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_DRIVER", _Driver())
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_STEPS", steps)
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (("first", first), ("second", second)),
    )

    with pytest.raises(
        RuntimeError,
        match="durable deferred startup step mapping changed",
    ):
        server._run_managed_deferred_startup("transaction-id")


def test_unmanaged_prepare_does_not_build_coordinator(monkeypatch):
    import managed_startup_coordinator
    import server

    _reset_server_managed_startup(monkeypatch, server)
    calls = []
    monkeypatch.setattr(
        server.api_config,
        "startup_run_admission_is_closed",
        lambda: False,
    )
    monkeypatch.setattr(
        server,
        "_run_deferred_startup_mutators",
        lambda: calls.append("ordinary"),
    )
    monkeypatch.setattr(
        managed_startup_coordinator,
        "build_production_managed_startup_coordinator",
        lambda: pytest.fail("unmanaged startup must not build a coordinator"),
    )

    assert server._prepare_startup_mutators() == "started"
    assert calls == ["ordinary"]
    assert server._MANAGED_STARTUP_COORDINATOR is None


def test_fork_reset_discards_inherited_managed_coordinator_authority(monkeypatch):
    import server

    coordinator = object()
    evidence = object()
    driver = object()
    steps = ()
    monkeypatch.setattr(server, "_MANAGED_STARTUP_COORDINATOR", coordinator)
    monkeypatch.setattr(
        server,
        "_MANAGED_STARTUP_ACCEPTANCE_EVIDENCE",
        evidence,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_DRIVER", driver)
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_REPLAY_STEPS", steps)

    server._reset_deferred_startup_process_state_after_fork()

    assert server._MANAGED_STARTUP_COORDINATOR is None
    assert server._MANAGED_STARTUP_ACCEPTANCE_EVIDENCE is None
    assert server._DEFERRED_STARTUP_REPLAY_DRIVER is None
    assert server._DEFERRED_STARTUP_REPLAY_STEPS is None
