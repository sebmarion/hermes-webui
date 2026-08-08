from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import deferred_release_manifest as release_manifest
from deferred_startup_replay import (
    PriorCompletionAbsentPolicy,
    Reconciliation,
    RetrySafePartialPolicy,
)
from managed_startup_coordinator import (
    DurableStartupReceiptStore,
    ManagedStartupBindingError,
    ManagedStartupOperation,
    ManagedStartupReceiptCodec,
    _bind_configuration_journal,
    _required_callable,
    build_async_profile_manifest,
    build_managed_startup_coordinator,
)


TRANSACTION_ID = "managed_startup_transaction_000001"


@dataclass(frozen=True)
class Receipt:
    name: str
    generation: int = 1


@dataclass(frozen=True)
class Verification:
    outcome: str
    receipt: Receipt | None


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HERMES_WEBUI_STARTUP_TRANSACTION_ID": TRANSACTION_ID,
        "HERMES_WEBUI_MANIFEST_SHA256": "a" * 64,
        "HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256": (
            release_manifest.deferred_release_manifest_sha256()
        ),
        "HERMES_WEBUI_STARTUP_ATTEMPT_JOURNAL": str(
            tmp_path / "attempts.json"
        ),
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL": str(
            tmp_path / "configuration.json"
        ),
    }


def _operations(
    *,
    stale: bool = False,
    background_partial: bool = False,
    async_partial: bool = False,
):
    descriptors = release_manifest.webui_startup_descriptors(
        release_manifest.deferred_release_manifest(),
        startup_admission_closed=True,
    )
    operations = []
    for descriptor in descriptors:
        receipt = Receipt(descriptor.name)

        def mutate(receipt=receipt):
            return receipt

        def verify(prior, receipt=receipt, descriptor=descriptor):
            if stale and descriptor.name == "credential_permissions":
                return Verification("PROVED_COMPLETE", Receipt(receipt.name, 2))
            if background_partial and descriptor.name == "background_services":
                return Verification("PARTIAL", prior)
            if async_partial and descriptor.name == "async_delegation_recovery":
                return Verification("PARTIAL", prior)
            return Verification("PROVED_COMPLETE", prior or receipt)

        operations.append(
            ManagedStartupOperation(
                name=descriptor.name,
                operation=descriptor.operation,
                mutator=mutate,
                verifier=verify,
                receipt_type_id="test.receipt.v1",
            )
        )
    return tuple(operations)


def _codecs():
    return (ManagedStartupReceiptCodec("test.receipt.v1", Receipt),)


def test_builds_canonical_manifest_order_and_driver_attestation(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )

    assert tuple(step.name for step in coordinator.steps) == tuple(
        descriptor.name
        for descriptor in release_manifest.webui_startup_descriptors(
            release_manifest.deferred_release_manifest(),
            startup_admission_closed=True,
        )
    )
    assert coordinator.manifest_receipt.sha256 == (
        release_manifest.deferred_release_manifest_sha256()
    )
    assert coordinator.driver_attestation().transaction_id == TRANSACTION_ID


def test_accepts_distinct_package_and_deferred_manifest_bindings(tmp_path):
    environment = _environment(tmp_path)
    canonical_deferred = release_manifest.deferred_release_manifest_sha256()
    environment["HERMES_WEBUI_MANIFEST_SHA256"] = "a" * 64
    environment["HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256"] = (
        canonical_deferred
    )

    coordinator = build_managed_startup_coordinator(
        environment=environment,
        operations=_operations(),
        receipt_codecs=_codecs(),
    )

    assert coordinator.manifest_receipt.sha256 == canonical_deferred


@pytest.mark.parametrize(
    "missing",
    [
        "HERMES_WEBUI_STARTUP_TRANSACTION_ID",
        "HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256",
        "HERMES_WEBUI_STARTUP_ATTEMPT_JOURNAL",
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL",
    ],
)
def test_missing_managed_environment_binding_fails_closed(tmp_path, missing):
    environment = _environment(tmp_path)
    environment.pop(missing)
    with pytest.raises(ManagedStartupBindingError):
        build_managed_startup_coordinator(
            environment=environment,
            operations=_operations(),
            receipt_codecs=_codecs(),
        )


def test_missing_or_reordered_capability_fails_closed(tmp_path):
    operations = _operations()
    with pytest.raises(ManagedStartupBindingError):
        build_managed_startup_coordinator(
            environment=_environment(tmp_path),
            operations=operations[:-1],
            receipt_codecs=_codecs(),
        )
    with pytest.raises(ManagedStartupBindingError):
        build_managed_startup_coordinator(
            environment=_environment(tmp_path),
            operations=tuple(reversed(operations)),
            receipt_codecs=_codecs(),
        )


def test_reconciler_rejects_stale_receipt(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(stale=True),
        receipt_codecs=_codecs(),
    )
    coordinator.steps[0].mutator()
    assert coordinator.steps[0].reconciler() is Reconciliation.AMBIGUOUS


def test_restart_reconciler_adopts_only_current_verified_receipt(tmp_path):
    first = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    first.steps[0].mutator()

    restarted = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    assert (
        restarted.steps[0].reconciler()
        is Reconciliation.PROVED_COMPLETE
    )
    assert restarted.step_receipt_bundle().receipts[0] == (
        "credential_permissions",
        Receipt("credential_permissions"),
    )


def test_missing_durable_receipt_is_ambiguous(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    assert coordinator.steps[0].reconciler() is Reconciliation.AMBIGUOUS


def test_tampered_durable_receipt_fails_closed(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    coordinator.steps[0].mutator()
    payload = coordinator.receipt_store.path.read_bytes()
    coordinator.receipt_store.path.write_bytes(payload.replace(
        b"credential_permissions",
        b"credential_permissionX",
        1,
    ))
    coordinator.receipt_store.path.chmod(0o600)

    with pytest.raises(ManagedStartupBindingError):
        build_managed_startup_coordinator(
            environment=_environment(tmp_path),
            operations=_operations(),
            receipt_codecs=_codecs(),
        )


def test_receipt_store_rejects_foreign_transaction(tmp_path):
    path = tmp_path / "receipts.json"
    first = DurableStartupReceiptStore(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=release_manifest.deferred_release_manifest_sha256(),
        codecs=_codecs(),
        step_types=(("credential_permissions", "test.receipt.v1"),),
    )
    first.persist(
        "credential_permissions",
        "test.receipt.v1",
        Receipt("credential_permissions"),
    )
    with pytest.raises(ManagedStartupBindingError, match="transaction"):
        DurableStartupReceiptStore(
            path,
            transaction_id="foreign_startup_transaction_000001",
            manifest_sha256=release_manifest.deferred_release_manifest_sha256(),
            codecs=_codecs(),
            step_types=(("credential_permissions", "test.receipt.v1"),),
        )


def test_receipt_store_reconstructs_exact_type_in_new_process(tmp_path):
    path = tmp_path / "receipts.json"
    store = DurableStartupReceiptStore(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=release_manifest.deferred_release_manifest_sha256(),
        codecs=_codecs(),
        step_types=(("credential_permissions", "test.receipt.v1"),),
    )
    store.persist(
        "credential_permissions",
        "test.receipt.v1",
        Receipt("credential_permissions"),
    )
    script = """
from pathlib import Path
import deferred_release_manifest as manifest
from managed_startup_coordinator import (
    DurableStartupReceiptStore,
    ManagedStartupReceiptCodec,
)
from tests.test_managed_startup_coordinator import Receipt
store = DurableStartupReceiptStore(
    Path(__import__("sys").argv[1]),
    transaction_id=__import__("sys").argv[2],
    manifest_sha256=manifest.deferred_release_manifest_sha256(),
    codecs=(ManagedStartupReceiptCodec("test.receipt.v1", Receipt),),
    step_types=(("credential_permissions", "test.receipt.v1"),),
)
assert store.load("credential_permissions", "test.receipt.v1") == Receipt(
    "credential_permissions"
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), TRANSACTION_ID],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_production_async_receipt_codec_round_trips_postconditions(tmp_path):
    from tools import async_delegation as agent_async

    postcondition = agent_async.ManagedAsyncEventPostcondition(
        event_id="event-1",
        kind="completion",
        state="queued",
        row_sha256="a" * 64,
        event_sha256="b" * 64,
        immutable_sha256="c" * 64,
        created_at=1.0,
        last_replay_epoch="epoch-1",
    )
    receipt = agent_async.ManagedAsyncDelegationRecoveryReceipt(
        outcome=agent_async.ManagedAsyncDelegationRecoveryOutcome.COMPLETE,
        tracker_paths=("/tmp/tracker.json",),
        event_postconditions=(postcondition,),
        verification_sha256="d" * 64,
    )
    store = DurableStartupReceiptStore(
        tmp_path / "async-receipts.json",
        transaction_id=TRANSACTION_ID,
        manifest_sha256=release_manifest.deferred_release_manifest_sha256(),
        codecs=(
            ManagedStartupReceiptCodec(
                "agent.async-recovery-receipt.v1",
                agent_async.ManagedAsyncDelegationRecoveryReceipt,
            ),
            ManagedStartupReceiptCodec(
                "agent.async-recovery-outcome.v1",
                agent_async.ManagedAsyncDelegationRecoveryOutcome,
            ),
            ManagedStartupReceiptCodec(
                "agent.async-event-postcondition.v1",
                agent_async.ManagedAsyncEventPostcondition,
            ),
        ),
        step_types=(
            ("async_delegation_recovery", "agent.async-recovery-receipt.v1"),
        ),
    )

    store.persist(
        "async_delegation_recovery",
        "agent.async-recovery-receipt.v1",
        receipt,
    )

    loaded = store.load(
        "async_delegation_recovery",
        "agent.async-recovery-receipt.v1",
    )
    assert loaded == receipt
    assert loaded.event_postconditions == (postcondition,)


def test_candidate_async_receipt_survives_fresh_process_legacy_noop(
    tmp_path,
):
    candidate = Path(
        os.environ.get(
            "HERMES_AGENT_CANDIDATE_ROOT",
            Path.home() / "hermes-webui" / ".codex-repair" / "agent",
        )
    )
    if not (candidate / "tools" / "async_delegation.py").is_file():
        pytest.skip("external candidate Agent checkout is unavailable")
    webui = Path(__file__).parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(candidate), str(webui))),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(tmp_path),
    }
    persist = r'''
import json, queue, sys
from pathlib import Path
agent, webui, root = map(Path, sys.argv[1:4])
sys.path[:0] = [str(agent), str(webui)]
from tools import async_delegation as ad
assert Path(ad.__file__).is_relative_to(agent)
from managed_startup_coordinator import DurableStartupReceiptStore, ManagedStartupReceiptCodec
root.chmod(0o700)
tracker = root / "async_delegations.json"
delegation_id = "deleg_default_gen-1_00000000-0000-4000-8000-000000000001"
record = {"delegation_id": delegation_id, "profile_id": "default", "profile_generation": "gen-1", "status": "completed", "delivery_status": "delivered", "completed_at": ad.time.time()}
entry = {"delegation_id": delegation_id, "profile_id": "default", "profile_generation": "gen-1", "status": "completed", "delivery_status": "delivered", "record": dict(record), "event": {"type": "async_delegation", "delegation_id": delegation_id, "profile_id": "default", "profile_generation": "gen-1", "status": "completed"}}
tracker.write_text(json.dumps({"version": 1, "records": {delegation_id: entry}}), encoding="utf-8")
tracker.chmod(0o600)
manifest = ad.ManagedAsyncDelegationProfileManifest("gen-1", (ad.ManagedAsyncDelegationProfile("default", tracker),), ("default",), "a" * 64)
receipt = ad.recover_managed_async_delegations_exact(manifest, outbox_path=root / "outbox.json", completion_queue=queue.Queue())
assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE, receipt
assert receipt.verification_sha256, receipt
codecs = (
    ManagedStartupReceiptCodec("agent.async-recovery-receipt.v1", ad.ManagedAsyncDelegationRecoveryReceipt),
    ManagedStartupReceiptCodec("agent.async-recovery-outcome.v1", ad.ManagedAsyncDelegationRecoveryOutcome),
    ManagedStartupReceiptCodec("agent.async-event-postcondition.v1", ad.ManagedAsyncEventPostcondition),
)
store = DurableStartupReceiptStore(root / "receipts.json", transaction_id="managed_startup_transaction_000001", manifest_sha256="b" * 64, codecs=codecs, step_types=(("async_delegation_recovery", "agent.async-recovery-receipt.v1"),))
store.persist("async_delegation_recovery", "agent.async-recovery-receipt.v1", receipt)
'''
    verify = r'''
import queue, sys
from pathlib import Path
agent, webui, root = map(Path, sys.argv[1:4])
sys.path[:0] = [str(agent), str(webui)]
from tools import async_delegation as ad
assert Path(ad.__file__).is_relative_to(agent)
from managed_startup_coordinator import DurableStartupReceiptStore, ManagedStartupReceiptCodec
tracker = root / "async_delegations.json"
before = tracker.stat()
ad._persistence_path = lambda: tracker
assert ad.recover_async_delegations() == {"queued": 0, "lost": 0}
after = tracker.stat()
assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
codecs = (
    ManagedStartupReceiptCodec("agent.async-recovery-receipt.v1", ad.ManagedAsyncDelegationRecoveryReceipt),
    ManagedStartupReceiptCodec("agent.async-recovery-outcome.v1", ad.ManagedAsyncDelegationRecoveryOutcome),
    ManagedStartupReceiptCodec("agent.async-event-postcondition.v1", ad.ManagedAsyncEventPostcondition),
)
store = DurableStartupReceiptStore(root / "receipts.json", transaction_id="managed_startup_transaction_000001", manifest_sha256="b" * 64, codecs=codecs, step_types=(("async_delegation_recovery", "agent.async-recovery-receipt.v1"),))
receipt = store.load("async_delegation_recovery", "agent.async-recovery-receipt.v1")
assert receipt.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE, receipt
assert receipt.verification_sha256, receipt
manifest = ad.ManagedAsyncDelegationProfileManifest("gen-1", (ad.ManagedAsyncDelegationProfile("default", tracker),), ("default",), "a" * 64)
result = ad.verify_managed_async_delegations_exact(receipt, manifest, completion_queue=queue.Queue())
assert result.outcome is ad.ManagedAsyncDelegationRecoveryOutcome.COMPLETE, result
'''
    for script in (persist, verify):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(candidate),
                str(webui),
                str(tmp_path),
            ],
            cwd=webui,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_coordinator_reconciles_durable_receipt_in_new_process(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    coordinator.steps[0].mutator()
    script = """
from pathlib import Path
from deferred_startup_replay import Reconciliation
from managed_startup_coordinator import build_managed_startup_coordinator
from tests.test_managed_startup_coordinator import _codecs, _environment, _operations
root = Path(__import__("sys").argv[1])
coordinator = build_managed_startup_coordinator(
    environment=_environment(root),
    operations=_operations(),
    receipt_codecs=_codecs(),
)
assert coordinator.steps[0].reconciler() is Reconciliation.PROVED_COMPLETE
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_restart_can_replace_early_step_after_step_fourteen(tmp_path):
    environment = _environment(tmp_path)
    operations = _operations()
    coordinator = build_managed_startup_coordinator(
        environment=environment,
        operations=operations,
        receipt_codecs=_codecs(),
    )
    for step in coordinator.steps:
        step.mutator()

    replacement = Receipt("credential_permissions", 2)
    restarted_operations = (
        replace(operations[0], mutator=lambda: replacement),
        *operations[1:],
    )
    restarted = build_managed_startup_coordinator(
        environment=environment,
        operations=restarted_operations,
        receipt_codecs=_codecs(),
    )
    restarted.steps[0].mutator()

    verified = build_managed_startup_coordinator(
        environment=environment,
        operations=restarted_operations,
        receipt_codecs=_codecs(),
    )
    bundle = verified.step_receipt_bundle()
    assert bundle.receipt_journal_generation == 15
    assert bundle.receipts[0] == ("credential_permissions", replacement)
    assert all(receipt is not None for _name, receipt in bundle.receipts)


def test_mutation_receipt_must_be_durable_before_return(tmp_path, monkeypatch):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    monkeypatch.setattr(
        coordinator.receipt_store,
        "persist",
        lambda *_args: (_ for _ in ()).throw(OSError("crash-before-receipt")),
    )
    with pytest.raises(OSError, match="crash-before-receipt"):
        coordinator.steps[0].mutator()

    restarted = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    assert restarted.steps[0].reconciler() is Reconciliation.AMBIGUOUS


def test_configuration_journal_is_bound_once_and_exact(tmp_path):
    class FakeConfiguration:
        configured = None

        @classmethod
        def configure_managed_startup_configuration_journal(cls, path):
            if cls.configured is None:
                cls.configured = Path(path)
            return cls.configured

    path = tmp_path / "configuration.json"
    assert _bind_configuration_journal(FakeConfiguration, path) == path
    with pytest.raises(ManagedStartupBindingError):
        _bind_configuration_journal(
            FakeConfiguration,
            tmp_path / "different.json",
        )


def test_only_approved_partial_steps_get_retry_policy(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(background_partial=True),
        receipt_codecs=_codecs(),
    )
    policies = {
        step.name: step.retry_safe_partial_policy for step in coordinator.steps
    }
    assert policies["background_services"] is RetrySafePartialPolicy.ALLOW
    assert policies["async_delegation_recovery"] is (
        RetrySafePartialPolicy.ALLOW
    )
    assert policies["compression_recovery"] is RetrySafePartialPolicy.ALLOW
    assert policies["state_directories"] is RetrySafePartialPolicy.ALLOW
    assert policies["tool_limit_continuation_recovery"] is (
        RetrySafePartialPolicy.ALLOW
    )
    assert policies["goal_continuation_recovery"] is (
        RetrySafePartialPolicy.ALLOW
    )
    assert policies["credential_permissions"] is RetrySafePartialPolicy.DENY
    assert policies["internal_recovery_key"] is RetrySafePartialPolicy.ALLOW
    assert policies["startup_configuration"] is RetrySafePartialPolicy.ALLOW
    background = coordinator.steps[-1]
    background.mutator()
    assert (
        background.reconciler()
        is Reconciliation.PROVED_RETRY_SAFE_PARTIAL
    )
    prior_absent = {
        step.name: step.prior_completion_absent_policy
        for step in coordinator.steps
    }
    assert prior_absent["credential_permissions"] is (
        PriorCompletionAbsentPolicy.DENY
    )
    for process_local in (
        "internal_recovery_key",
        "startup_profile_state",
        "provider_model_seed",
        "startup_configuration",
        "plugins",
        "process_completion_recovery",
        "async_delegation_recovery",
        "compression_recovery",
        "tool_limit_continuation_recovery",
        "goal_continuation_recovery",
        "background_services",
    ):
        assert prior_absent[process_local] is (
            PriorCompletionAbsentPolicy.ALLOW_RERUN
        )


def test_async_missing_queue_partial_is_retry_safe_for_recreation(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(async_partial=True),
        receipt_codecs=_codecs(),
    )
    step = next(
        step
        for step in coordinator.steps
        if step.name == "async_delegation_recovery"
    )
    step.mutator()

    assert step.reconciler() is Reconciliation.PROVED_RETRY_SAFE_PARTIAL


def test_receipt_bundle_is_immutable_and_bound_to_transaction(tmp_path):
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=_operations(),
        receipt_codecs=_codecs(),
    )
    coordinator.steps[0].mutator()
    bundle = coordinator.step_receipt_bundle()
    assert bundle.transaction_id == TRANSACTION_ID
    assert bundle.configuration_journal == _environment(tmp_path)[
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"
    ]
    assert bundle.receipt_journal_generation == 1
    assert len(bundle.receipt_journal_sha256) == 64
    assert bundle.receipts[0] == ("credential_permissions", Receipt(
        "credential_permissions"
    ))
    with pytest.raises(AttributeError):
        bundle.transaction_id = "different"


def test_receipt_snapshot_rejects_reordered_step_registry(tmp_path):
    operations = _operations()
    coordinator = build_managed_startup_coordinator(
        environment=_environment(tmp_path),
        operations=operations,
        receipt_codecs=_codecs(),
    )
    step_types = tuple(
        (operation.name, operation.receipt_type_id)
        for operation in operations
    )

    with pytest.raises(ManagedStartupBindingError, match="binding changed"):
        coordinator.receipt_store.snapshot(tuple(reversed(step_types)))


def test_async_profile_manifest_is_complete_sorted_and_source_bound(tmp_path):
    rows = [
        {"name": "zeta", "path": str(tmp_path / "zeta")},
        {"name": "default", "path": str(tmp_path / "default")},
    ]
    for row in rows:
        Path(row["path"]).mkdir()

    manifest = build_async_profile_manifest(rows, generation="release_1")

    assert manifest.expected_profile_ids == ("default", "zeta")
    assert tuple(profile.profile_id for profile in manifest.profiles) == (
        "default",
        "zeta",
    )
    assert len(manifest.source_digest) == 64
    with pytest.raises(ManagedStartupBindingError):
        build_async_profile_manifest(rows + [rows[0]], generation="release_1")


def test_production_capability_never_uses_always_complete_fallback():
    with pytest.raises(
        ManagedStartupBindingError,
        match="read-only process recovery verifier",
    ):
        _required_callable(
            object(),
            "verify_managed_startup_exact",
            capability="process recovery",
        )


def test_compression_recovery_start_forwards_exact_reserved_turn_binding():
    import managed_startup_coordinator as managed

    calls = []
    routes = type(
        "Routes",
        (),
        {
            "start_session_turn": staticmethod(
                lambda *args, **kwargs: calls.append((args, kwargs))
                or {"stream_id": "stream-1"}
            )
        },
    )
    kwargs = {
        "source": "compression_recovery",
        "expected_profile": "default",
        "attachments": [{"name": "proof.txt"}],
        "recovery_claim_token": "claim-token",
        "recovery_fingerprint": "f" * 64,
        "recovery_context_messages": [
            {"role": "user", "content": "finish it"}
        ],
    }

    result = managed._start_compression_recovery_turn(
        routes,
        "session-1",
        "continue exactly",
        **kwargs,
    )

    assert result == {"stream_id": "stream-1"}
    assert calls == [
        (("session-1", "continue exactly"), kwargs),
    ]


def test_production_compression_operation_preserves_release_and_turn_bindings(
    monkeypatch,
    tmp_path,
):
    import managed_startup_coordinator as managed
    from api import (
        compression_recovery_receipts,
        config,
        managed_startup_configuration,
        routes,
    )

    manifest_sha256 = release_manifest.deferred_release_manifest_sha256()
    environment = _environment(tmp_path)
    captured = {}
    recovery_calls = []
    verification_calls = []
    turn_calls = []
    recovery_receipt = object()

    monkeypatch.setattr(config, "startup_run_admission_is_closed", lambda: True)
    monkeypatch.setattr(
        config,
        "_managed_release_selected_from_environment",
        lambda: True,
    )
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", TRANSACTION_ID)
    monkeypatch.setattr(
        managed_startup_configuration,
        "configure_managed_startup_configuration_journal",
        lambda path: Path(path),
    )

    def capture_coordinator(**kwargs):
        captured.update(kwargs)
        return "captured-coordinator"

    def recover_exact(**kwargs):
        recovery_calls.append(kwargs)
        kwargs["start"](
            "session-1",
            "resume exactly",
            source="compression_recovery",
            expected_profile="default",
            attachments=[{"name": "proof.txt"}],
            recovery_claim_token="claim-token",
            recovery_fingerprint="f" * 64,
            recovery_context_messages=[
                {"role": "user", "content": "finish it"}
            ],
        )
        return recovery_receipt

    def verify_exact(receipt, **kwargs):
        verification_calls.append((receipt, kwargs))
        return type("Verification", (), {"outcome": "COMPLETE"})()

    monkeypatch.setattr(
        managed,
        "build_managed_startup_coordinator",
        capture_coordinator,
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "recover_managed_compression_recoveries_exact",
        recover_exact,
    )
    monkeypatch.setattr(
        compression_recovery_receipts,
        "verify_managed_compression_recoveries_exact",
        verify_exact,
    )
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *args, **kwargs: turn_calls.append((args, kwargs))
        or {"stream_id": "stream-1"},
    )
    # Keep the WebUI binding test independent of which paired Agent revision
    # happens to be installed in the test environment.
    tools_package = __import__("tools")
    fake_async = SimpleNamespace(
        verify_managed_async_delegations_exact=lambda *_args, **_kwargs: None,
        ManagedAsyncDelegationRecoveryReceipt=object,
        ManagedAsyncDelegationRecoveryOutcome=object,
        ManagedAsyncEventPostcondition=object,
    )
    fake_process_registry = SimpleNamespace(
        recover_managed_startup_exact=lambda *_args, **_kwargs: None,
        verify_managed_startup_exact=lambda *_args, **_kwargs: None,
    )
    fake_process_module = SimpleNamespace(
        process_registry=fake_process_registry,
        ManagedProcessRecoveryReceipt=object,
        ManagedProcessRecoveryOutcome=object,
    )
    fake_durable_state = SimpleNamespace(FileIdentity=object)
    for name, module in (
        ("async_delegation", fake_async),
        ("process_registry", fake_process_module),
        ("durable_state", fake_durable_state),
    ):
        monkeypatch.setattr(tools_package, name, module, raising=False)
        monkeypatch.setitem(sys.modules, f"tools.{name}", module)

    assert managed.build_production_managed_startup_coordinator(
        environment=environment
    ) == "captured-coordinator"
    operation = next(
        item
        for item in captured["operations"]
        if item.name == "compression_recovery"
    )

    assert operation.mutator() is recovery_receipt
    verified = operation.verifier(recovery_receipt)

    expected_release_binding = {
        "transaction_id": TRANSACTION_ID,
        "manifest_sha256": manifest_sha256,
    }
    assert recovery_calls == [
        {
            **expected_release_binding,
            "start": recovery_calls[0]["start"],
        }
    ]
    assert turn_calls == [
        (
            ("session-1", "resume exactly"),
            {
                "source": "compression_recovery",
                "expected_profile": "default",
                "attachments": [{"name": "proof.txt"}],
                "recovery_claim_token": "claim-token",
                "recovery_fingerprint": "f" * 64,
                "recovery_context_messages": [
                    {"role": "user", "content": "finish it"}
                ],
            },
        )
    ]
    assert verification_calls == [
        (recovery_receipt, expected_release_binding),
    ]
    assert verified.outcome == "COMPLETE"
    assert verified.receipt is recovery_receipt


def test_restart_reconciles_completed_compression_without_relaunch(tmp_path):
    environment = _environment(tmp_path)
    operations = _operations()
    compression_index = next(
        index
        for index, operation in enumerate(operations)
        if operation.name == "compression_recovery"
    )
    first = build_managed_startup_coordinator(
        environment=environment,
        operations=operations,
        receipt_codecs=_codecs(),
    )
    first.steps[compression_index].mutator()

    relaunched = []
    restarted_operations = list(operations)
    restarted_operations[compression_index] = replace(
        operations[compression_index],
        mutator=lambda: relaunched.append("compression"),
    )
    restarted = build_managed_startup_coordinator(
        environment=environment,
        operations=tuple(restarted_operations),
        receipt_codecs=_codecs(),
    )

    assert (
        restarted.steps[compression_index].reconciler()
        is Reconciliation.PROVED_COMPLETE
    )
    assert relaunched == []
