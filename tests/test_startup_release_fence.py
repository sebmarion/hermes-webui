"""Fail-closed managed startup and signed acceptance contracts."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from copy import deepcopy
from types import SimpleNamespace

import pytest

from api import config


TRANSACTION_ID = "startup-transaction-" + ("x" * 32)
IDENTITY = {"pid": 123, "started_at": 456.0, "instance_id": "candidate-a"}

EXPECTED_DEFERRED_RELEASE_DESCRIPTORS = [
    {
        "name": "candidate_accept_intent",
        "owner": "release_controller",
        "operation": "record_phase:candidate_accept_intent",
        "condition": "always",
    },
    {
        "name": "gateway_opening",
        "owner": "release_controller",
        "operation": "open_pair_after_promotion",
        "condition": "always",
    },
    {
        "name": "credential_permissions",
        "owner": "webui_server",
        "operation": "api.startup.fix_credential_permissions",
        "condition": "always",
    },
    {
        "name": "internal_recovery_key",
        "owner": "webui_server",
        "operation": "server._materialize_internal_recovery_key",
        "condition": "always",
    },
    {
        "name": "state_directories",
        "owner": "webui_server",
        "operation": "server._create_state_directories",
        "condition": "always",
    },
    {
        "name": "startup_profile_state",
        "owner": "webui_server",
        "operation": "api.config.apply_startup_profile_state",
        "condition": "always",
    },
    {
        "name": "provider_model_seed",
        "owner": "webui_server",
        "operation": "api.config.seed_startup_provider_models",
        "condition": "always",
    },
    {
        "name": "startup_configuration",
        "owner": "webui_server",
        "operation": "api.config.apply_deferred_startup_configuration",
        "condition": "always",
    },
    {
        "name": "session_recovery",
        "owner": "webui_server",
        "operation": "server._recover_startup_sessions",
        "condition": "always",
    },
    {
        "name": "plugins",
        "owner": "webui_server",
        "operation": "server._load_startup_plugins",
        "condition": "always",
    },
    {
        "name": "process_completion_recovery",
        "owner": "webui_server",
        "operation": "server._recover_process_completion_notifications",
        "condition": "always",
    },
    {
        "name": "async_delegation_recovery",
        "owner": "webui_server",
        "operation": "server._recover_async_delegation_notifications",
        "condition": "always",
    },
    {
        "name": "tool_limit_continuation_recovery",
        "owner": "webui_server",
        "operation": "server._recover_tool_limit_continuations_for_startup",
        "condition": "startup_run_admission_closed",
    },
    {
        "name": "goal_continuation_recovery",
        "owner": "webui_server",
        "operation": "server._recover_goal_continuations_for_startup",
        "condition": "startup_run_admission_closed",
    },
    {
        "name": "background_services",
        "owner": "webui_server",
        "operation": "server._start_startup_background_services",
        "condition": "always",
    },
    {
        "name": "full_open_health",
        "owner": "release_controller",
        "operation": "inspect_accepted_binding:require_full_health",
        "condition": "always",
    },
    {
        "name": "pair_gate_release",
        "owner": "release_controller",
        "operation": "release_pair_after_acceptance",
        "condition": "always",
    },
    {
        "name": "pair_opened",
        "owner": "release_controller",
        "operation": "record_phase:pair_opened",
        "condition": "always",
    },
    {
        "name": "watchdog_restoration",
        "owner": "release_controller",
        "operation": "_restore_watchdog_cron",
        "condition": "always",
    },
]
EXPECTED_DEFERRED_RELEASE_MANIFEST = {
    "version": 1,
    "descriptors": EXPECTED_DEFERRED_RELEASE_DESCRIPTORS,
}
EXPECTED_DEFERRED_RELEASE_MANIFEST_SHA256 = (
    "040d95fe27e21611ec01c5d63da7a8767bc120e1d771593df17446be0943a38b"
)


def _manifest_as_dict(manifest):
    return {
        "version": manifest.version,
        "descriptors": [
            {
                "name": descriptor.name,
                "owner": descriptor.owner,
                "operation": descriptor.operation,
                "condition": descriptor.condition,
            }
            for descriptor in manifest.descriptors
        ],
    }


def test_deferred_release_manifest_is_exact_versioned_and_immutable():
    release_manifest = importlib.import_module("deferred_release_manifest")

    manifest = release_manifest.deferred_release_manifest()

    assert _manifest_as_dict(manifest) == EXPECTED_DEFERRED_RELEASE_MANIFEST
    with pytest.raises(FrozenInstanceError):
        manifest.version = 2
    with pytest.raises(FrozenInstanceError):
        manifest.descriptors[0].name = "changed"


def test_deferred_release_manifest_has_stable_canonical_digest():
    release_manifest = importlib.import_module("deferred_release_manifest")

    canonical = release_manifest.canonical_manifest_bytes()

    assert canonical == json.dumps(
        EXPECTED_DEFERRED_RELEASE_MANIFEST,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (
        release_manifest.deferred_release_manifest_sha256()
        == EXPECTED_DEFERRED_RELEASE_MANIFEST_SHA256
    )
    assert hashlib.sha256(canonical).hexdigest() == (
        EXPECTED_DEFERRED_RELEASE_MANIFEST_SHA256
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda manifest: manifest.update(version=2),
        lambda manifest: manifest["descriptors"].pop(),
        lambda manifest: manifest["descriptors"].insert(
            0,
            {
                "name": "unknown",
                "owner": "release_controller",
                "operation": "unknown",
                "condition": "always",
            },
        ),
        lambda manifest: manifest["descriptors"].reverse(),
        lambda manifest: manifest["descriptors"][0].update(operation="changed"),
        lambda manifest: manifest["descriptors"][0].update(unknown=True),
    ),
)
def test_deferred_release_manifest_rejects_any_contract_change(mutate):
    release_manifest = importlib.import_module("deferred_release_manifest")
    candidate = deepcopy(EXPECTED_DEFERRED_RELEASE_MANIFEST)
    mutate(candidate)

    with pytest.raises(ValueError, match="deferred release manifest"):
        release_manifest.validate_deferred_release_manifest(candidate)


@pytest.mark.parametrize(
    "case",
    (
        "bool-version",
        "float-version",
        "list-descriptors",
        "tuple-subclass",
        "str-subclass",
        "descriptor-subclass",
        "malformed-descriptor",
    ),
)
def test_deferred_release_manifest_rejects_non_exact_typed_values(case):
    release_manifest = importlib.import_module("deferred_release_manifest")
    canonical = release_manifest.deferred_release_manifest()

    class ManifestStr(str):
        pass

    class ManifestTuple(tuple):
        pass

    class DescriptorSubclass(release_manifest.DeferredReleaseDescriptor):
        pass

    if case == "bool-version":
        candidate = release_manifest.DeferredReleaseManifest(
            True,
            canonical.descriptors,
        )
    elif case == "float-version":
        candidate = release_manifest.DeferredReleaseManifest(
            1.0,
            canonical.descriptors,
        )
    elif case == "list-descriptors":
        candidate = release_manifest.DeferredReleaseManifest(
            1,
            list(canonical.descriptors),
        )
    elif case == "tuple-subclass":
        candidate = release_manifest.DeferredReleaseManifest(
            1,
            ManifestTuple(canonical.descriptors),
        )
    elif case == "str-subclass":
        first = canonical.descriptors[0]
        changed = release_manifest.DeferredReleaseDescriptor(
            ManifestStr(first.name),
            first.owner,
            first.operation,
            first.condition,
        )
        candidate = release_manifest.DeferredReleaseManifest(
            1,
            (changed, *canonical.descriptors[1:]),
        )
    elif case == "descriptor-subclass":
        first = canonical.descriptors[0]
        changed = DescriptorSubclass(
            first.name,
            first.owner,
            first.operation,
            first.condition,
        )
        candidate = release_manifest.DeferredReleaseManifest(
            1,
            (changed, *canonical.descriptors[1:]),
        )
    else:
        candidate = release_manifest.DeferredReleaseManifest(
            1,
            (SimpleNamespace(name="candidate_accept_intent"),)
            + canonical.descriptors[1:],
        )

    with pytest.raises(ValueError, match="deferred release manifest"):
        release_manifest.validate_deferred_release_manifest(candidate)


def test_deferred_release_manifest_conditional_startup_subset():
    release_manifest = importlib.import_module("deferred_release_manifest")
    manifest = release_manifest.deferred_release_manifest()

    normal = release_manifest.webui_startup_descriptors(
        manifest,
        startup_admission_closed=False,
    )
    fenced = release_manifest.webui_startup_descriptors(
        manifest,
        startup_admission_closed=True,
    )

    normal_names = [descriptor.name for descriptor in normal]
    fenced_names = [descriptor.name for descriptor in fenced]
    assert normal_names == [
        descriptor["name"]
        for descriptor in EXPECTED_DEFERRED_RELEASE_DESCRIPTORS
        if descriptor["owner"] == "webui_server"
        and descriptor["condition"] == "always"
    ]
    assert fenced_names == [
        descriptor["name"]
        for descriptor in EXPECTED_DEFERRED_RELEASE_DESCRIPTORS
        if descriptor["owner"] == "webui_server"
    ]


def test_server_deferred_startup_mapping_matches_canonical_subset(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    normal_names = [name for name, _mutator in server._deferred_startup_steps()]
    _select_managed_candidate(monkeypatch)
    fenced_names = [name for name, _mutator in server._deferred_startup_steps()]

    expected_normal = [
        descriptor["name"]
        for descriptor in EXPECTED_DEFERRED_RELEASE_DESCRIPTORS
        if descriptor["owner"] == "webui_server"
        and descriptor["condition"] == "always"
    ]
    expected_fenced = [
        descriptor["name"]
        for descriptor in EXPECTED_DEFERRED_RELEASE_DESCRIPTORS
        if descriptor["owner"] == "webui_server"
    ]
    assert normal_names == expected_normal
    assert fenced_names == expected_fenced
    assert [
        f"{mutator.__module__}.{mutator.__name__}"
        for _name, mutator in server._deferred_startup_steps()
    ] == [
        descriptor["operation"]
        for descriptor in EXPECTED_DEFERRED_RELEASE_DESCRIPTORS
        if descriptor["owner"] == "webui_server"
    ]


def test_server_deferred_startup_mapping_rejects_non_callable_before_return(
    monkeypatch,
):
    import server

    monkeypatch.setattr(server, "_recover_startup_sessions", None)

    with pytest.raises(RuntimeError, match="callable mapping changed"):
        server._deferred_startup_steps()


def test_server_deferred_startup_mapping_rejects_swapped_callable_association(
    monkeypatch,
):
    import server

    recover = server._recover_startup_sessions
    plugins = server._load_startup_plugins
    monkeypatch.setattr(server, "_recover_startup_sessions", plugins)
    monkeypatch.setattr(server, "_load_startup_plugins", recover)

    with pytest.raises(RuntimeError, match="callable mapping changed"):
        server._deferred_startup_steps()


def _tree_bytes_and_mtimes(root):
    """Return a stable receipt for every path below an isolated state root."""
    receipt = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else str(path.relative_to(root))
        stat = path.stat(follow_symlinks=False)
        receipt[relative] = {
            "kind": "dir" if path.is_dir() else "file",
            "mtime_ns": stat.st_mtime_ns,
            "bytes": None if path.is_dir() else path.read_bytes(),
        }
    return receipt


@pytest.fixture
def isolated_startup_admission(monkeypatch):
    for key in (
        "HERMES_WEBUI_RELEASE_PATH",
        "HERMES_WEBUI_MANIFEST_SHA256",
        "HERMES_WEBUI_LAUNCH_MODE",
        "HERMES_WEBUI_STARTUP_FENCED",
        "HERMES_WEBUI_STARTUP_TRANSACTION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(config, "_RUN_ADMISSION_RESERVATIONS", {})
    monkeypatch.setattr(config, "_RUN_ADMISSION_STATE", "open")
    monkeypatch.setattr(config, "_RUN_ADMISSION_GENERATION", 0)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TOKEN_DIGEST", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TOKEN", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_EXPECTED_IDENTITY", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_FENCED_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LEASE_EXPIRES_AT", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_TRANSACTION_ID", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_ACTION", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_TOKEN_DIGEST", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LAST_EXPECTED_IDENTITY", None)
    monkeypatch.setattr(config, "_RUN_ADMISSION_STARTUP_ERROR", None, raising=False)
    monkeypatch.setattr(config, "_RUN_ADMISSION_STARTUP_ACCEPTOR", None, raising=False)
    monkeypatch.setattr(config, "_RUN_ADMISSION_LOCAL", threading.local())
    return monkeypatch


def _select_managed_candidate(monkeypatch, *, transaction_id=TRANSACTION_ID):
    monkeypatch.setenv("HERMES_WEBUI_RELEASE_PATH", "/immutable/webui/candidate")
    monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", "selector")
    monkeypatch.setenv("HERMES_WEBUI_STARTUP_FENCED", "1")
    monkeypatch.setenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", transaction_id)
    return config.initialize_run_admission_from_environment()


def _claim_startup_fence():
    return config.fence_run_admission(
        IDENTITY,
        transaction_id=TRANSACTION_ID,
    )


def test_selected_managed_candidate_starts_fenced_and_never_lease_expires(
    monkeypatch,
    isolated_startup_admission,
):
    now = [100.0]
    monkeypatch.setattr(config.time, "time", lambda: now[0])

    snapshot = _select_managed_candidate(monkeypatch)

    assert snapshot["state"] == "startup-fenced"
    assert snapshot["transaction_id"] == TRANSACTION_ID
    assert snapshot["lease_expires_at"] is None
    now[0] = 10_000_000.0
    assert config.run_admission_snapshot()["state"] == "startup-fenced"
    with pytest.raises(config.RunAdmissionClosed):
        config.reserve_run_admission(kind="must-stay-closed")


def test_promoted_managed_release_without_startup_markers_starts_open(
    monkeypatch,
    isolated_startup_admission,
):
    monkeypatch.setenv("HERMES_WEBUI_RELEASE_PATH", "/immutable/webui/candidate")
    monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", "selector")

    snapshot = config.initialize_run_admission_from_environment()

    assert snapshot["state"] == "open"
    assert snapshot["startup_error"] is None
    reservation = config.reserve_run_admission(kind="managed-steady-state")
    assert config.release_run_admission(reservation) is True


@pytest.mark.parametrize(
    ("fenced", "transaction"),
    (
        ("1", None),
        (None, TRANSACTION_ID),
        ("0", TRANSACTION_ID),
        ("1", "malformed"),
        ("", TRANSACTION_ID),
        ("1", ""),
    ),
)
def test_partial_or_malformed_managed_startup_contract_fails_closed(
    monkeypatch,
    isolated_startup_admission,
    fenced,
    transaction,
):
    monkeypatch.setenv("HERMES_WEBUI_RELEASE_PATH", "/immutable/webui/candidate")
    monkeypatch.setenv("HERMES_WEBUI_LAUNCH_MODE", "selector")
    if fenced is not None:
        monkeypatch.setenv("HERMES_WEBUI_STARTUP_FENCED", fenced)
    if transaction is not None:
        monkeypatch.setenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", transaction)

    snapshot = config.initialize_run_admission_from_environment()

    assert snapshot["state"] == "startup-invalid"
    assert snapshot["startup_error"] == "invalid_startup_fence_environment"
    with pytest.raises(config.RunAdmissionClosed):
        config.reserve_run_admission(kind="invalid-managed-startup")


def test_exact_startup_transaction_accepts_once_and_opens_admission(
    monkeypatch,
    isolated_startup_admission,
):
    calls = []
    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(lambda: calls.append("started"))
    fenced = _claim_startup_fence()

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    repeated = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert repeated["state"] == "open"
    assert calls == ["started"]
    reservation = config.reserve_run_admission(kind="post-accept")
    assert config.release_run_admission(reservation) is True


def test_wrong_startup_transaction_or_identity_cannot_accept(
    monkeypatch,
    isolated_startup_admission,
):
    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(lambda: None)
    fenced = _claim_startup_fence()

    with pytest.raises(config.RunAdmissionConflict):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity=IDENTITY,
            transaction_id="wrong-transaction-" + ("y" * 32),
        )
    with pytest.raises(config.RunAdmissionIdentityMismatch):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity={**IDENTITY, "pid": 999},
            transaction_id=TRANSACTION_ID,
        )
    assert config.run_admission_snapshot()["state"] == "startup-fenced"


def test_failed_deferred_start_keeps_startup_fenced_and_can_retry(
    monkeypatch,
    isolated_startup_admission,
):
    attempts = []

    def flaky_start():
        attempts.append("attempt")
        if len(attempts) == 1:
            raise RuntimeError("synthetic deferred failure")

    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(flaky_start)
    fenced = _claim_startup_fence()

    with pytest.raises(config.RunAdmissionBusy, match="deferred startup failed"):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity=IDENTITY,
            transaction_id=TRANSACTION_ID,
        )
    failed = config.run_admission_snapshot()
    assert failed["state"] == "startup-fenced"
    assert failed["startup_error"] == "deferred_startup_failed"

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    assert accepted["state"] == "open"
    assert attempts == ["attempt", "attempt"]


def test_startup_acceptor_has_scoped_admission_for_deferred_recovery(
    monkeypatch,
    isolated_startup_admission,
):
    reservations = []

    def schedule_recovery():
        reservation = config.reserve_run_admission(kind="startup-recovery")
        reservations.append(reservation)
        config.release_run_admission(reservation)

    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(schedule_recovery)
    fenced = _claim_startup_fence()
    with pytest.raises(config.RunAdmissionClosed):
        config.reserve_run_admission(kind="ordinary-pre-accept")

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert len(reservations) == 1


def test_deferred_recovery_worker_inherits_scoped_startup_admission(
    monkeypatch,
    isolated_startup_admission,
):
    nested_done = threading.Event()
    errors = []

    def schedule_recovery():
        def recovery_worker():
            try:
                reservation = config.reserve_run_admission(
                    kind="nested-startup-recovery"
                )
                config.release_run_admission(reservation)
            except Exception as exc:  # captured for the assertion thread
                errors.append(exc)
            finally:
                nested_done.set()

        assert config.start_admitted_auxiliary_thread(
            kind="startup-recovery-worker",
            target=recovery_worker,
            name="startup-recovery-worker-test",
        )
        assert nested_done.wait(2.0)

    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(schedule_recovery)
    fenced = _claim_startup_fence()

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert errors == []


def test_signed_release_control_fence_and_accept_are_transaction_bound(
    monkeypatch,
    isolated_startup_admission,
):
    from api import release_control

    calls = []
    monkeypatch.setattr(
        release_control,
        "_release_control_signing_key",
        lambda: b"k" * 32,
    )
    monkeypatch.setattr(
        release_control,
        "current_release_process_identity",
        lambda **_kwargs: dict(IDENTITY),
    )
    _select_managed_candidate(monkeypatch)
    config.configure_startup_acceptor(lambda: calls.append("started"))

    fenced = release_control.execute_release_control(
        {
            "action": "fence",
            "nonce": "f" * 32,
            "transaction_id": TRANSACTION_ID,
            "expected": IDENTITY,
        }
    )
    accepted = release_control.execute_release_control(
        {
            "action": "accept",
            "nonce": "a" * 32,
            "transaction_id": TRANSACTION_ID,
            "expected": IDENTITY,
        },
        fence_token=fenced["fence_token"],
    )

    assert fenced["status"] == "startup-fenced"
    assert accepted["status"] == "accepted"
    assert accepted["transaction_id"] == TRANSACTION_ID
    assert accepted["identity"] == IDENTITY
    assert len(accepted["attestation"]) == 64
    assert calls == ["started"]


def test_startup_release_auth_never_generates_a_missing_signing_key(
    monkeypatch,
    tmp_path,
    isolated_startup_admission,
):
    from api import release_control

    _select_managed_candidate(monkeypatch)
    missing_state = tmp_path / "missing-state"
    monkeypatch.setattr(config, "STATE_DIR", missing_state)
    fallback_calls = []
    monkeypatch.setattr(
        release_control,
        "_signing_key",
        lambda: fallback_calls.append("generated") or (b"x" * 32),
    )
    monkeypatch.setattr(release_control.time, "time", lambda: 100.0)
    body = {
        "action": "inspect",
        "nonce": "n" * 32,
        "transaction_id": TRANSACTION_ID,
    }
    handler = SimpleNamespace(
        client_address=("127.0.0.1", 1),
        headers={
            "X-Hermes-Release-Timestamp": "100",
            "X-Hermes-Release-Signature": hmac.new(
                b"not-the-missing-key" * 2,
                release_control.release_control_signing_bytes(body, "100"),
                hashlib.sha256,
            ).hexdigest(),
        },
    )

    allowed, _error = release_control.verify_release_control_request(handler, body)

    assert allowed is False
    assert fallback_calls == []
    assert missing_state.exists() is False


def test_signed_process_identity_binds_startup_selector_and_paired_artifacts():
    from api import release_control

    build = {
        "status": "managed",
        "valid": True,
        "build_id": "webui-build-a",
        "commit": "w" * 40,
        "tree": "x" * 40,
        "manifest_sha256": "1" * 64,
        "agent_commit": "a" * 40,
        "agent_tree": "b" * 40,
        "agent_manifest_sha256": "2" * 64,
        "runtime_manifest_sha256": "3" * 64,
        "selector_generation": 7,
        "release_path": "/immutable/webui/webui-build-a",
        "launch_mode": "selector",
        "selector_verified": True,
        "selector_state_path": "/immutable/control/selector.json",
        "selector_lock_path": "/immutable/control/selector.lock",
        "launchd_label": "com.example.hermes-webui",
        "startup_fenced": True,
        "startup_transaction_id": TRANSACTION_ID,
    }

    identity = release_control.current_release_process_identity(
        build_identity=build
    )

    for key in (
        "commit",
        "tree",
        "agent_commit",
        "agent_tree",
        "agent_manifest_sha256",
        "runtime_manifest_sha256",
        "launch_mode",
        "selector_verified",
        "selector_state_path",
        "selector_lock_path",
        "launchd_label",
        "startup_fenced",
        "startup_transaction_id",
    ):
        assert identity[key] == build[key]


def test_server_defers_mutators_until_accept_and_retries_only_incomplete_steps(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    calls = []
    second_attempts = [0]

    def first():
        calls.append("first")

    def flaky_second():
        calls.append("second")
        second_attempts[0] += 1
        if second_attempts[0] == 1:
            raise RuntimeError("synthetic second-step failure")

    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (("first", first), ("second", flaky_second)),
    )
    _select_managed_candidate(monkeypatch)

    assert server._prepare_startup_mutators() == "deferred"
    assert calls == []
    fenced = _claim_startup_fence()

    with pytest.raises(config.RunAdmissionBusy):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity=IDENTITY,
            transaction_id=TRANSACTION_ID,
        )
    assert calls == ["first", "second"]

    config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    assert calls == ["first", "second", "second"]
    assert server._DEFERRED_STARTUP_COMPLETED == {"first", "second"}


def test_managed_deferred_start_includes_detached_continuation_recovery(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    _select_managed_candidate(monkeypatch)
    names = [name for name, _mutator in server._deferred_startup_steps()]

    assert "startup_profile_state" in names
    assert "provider_model_seed" in names
    assert "startup_configuration" in names
    assert "process_completion_recovery" in names
    assert "async_delegation_recovery" in names
    assert "tool_limit_continuation_recovery" in names
    assert "goal_continuation_recovery" in names
    assert names.index("state_directories") < names.index("startup_profile_state")
    assert names.index("startup_profile_state") < names.index("provider_model_seed")
    assert names.index("provider_model_seed") < names.index("startup_configuration")
    assert names.index("startup_configuration") < names.index("session_recovery")
    assert names.index("session_recovery") < names.index(
        "process_completion_recovery"
    )
    assert names.index("process_completion_recovery") < names.index(
        "async_delegation_recovery"
    )
    assert names.index("async_delegation_recovery") < names.index(
        "tool_limit_continuation_recovery"
    )
    assert names.index("goal_continuation_recovery") < names.index(
        "background_services"
    )


def test_process_completion_recovery_runs_once_inside_signed_accept(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    calls = []
    fake_registry = SimpleNamespace(
        recover_from_checkpoint=lambda: calls.append("checkpoint") or 0,
        recover_completion_notifications=lambda: calls.append("recover") or 2,
        completion_activity_snapshot=lambda: {
            "process_checkpoint_available": True,
            "process_checkpoint_reason": "verified",
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.process_registry",
        SimpleNamespace(process_registry=fake_registry),
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (
            (
                "process_completion_recovery",
                server._recover_process_completion_notifications,
            ),
        ),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    repeated = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert repeated["state"] == "open"
    assert calls == ["checkpoint", "recover"]


def test_async_delegation_recovery_runs_once_inside_signed_accept(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "tools.async_delegation",
        SimpleNamespace(
            recover_async_delegations=lambda: calls.append("recover")
            or {"queued": 2, "lost": 1}
        ),
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (
            (
                "async_delegation_recovery",
                server._recover_async_delegation_notifications,
            ),
        ),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    repeated = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert repeated["state"] == "open"
    assert calls == ["recover"]


def test_managed_accept_waits_for_one_shot_recovery_terminal_success(
    monkeypatch,
    isolated_startup_admission,
):
    import server
    from api import routes

    entered = threading.Event()
    release = threading.Event()
    accepted = []
    failures = []

    def delayed_recovery(*, strict=False):
        assert strict is True
        entered.set()
        assert release.wait(2.0)
        return {"status": "complete", "recovered": 1}

    monkeypatch.setattr(
        routes,
        "_recover_tool_limit_continuations_on_startup",
        delayed_recovery,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (
            (
                "tool_limit_continuation_recovery",
                server._recover_tool_limit_continuations_for_startup,
            ),
        ),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    def accept():
        try:
            accepted.append(
                config.accept_startup_run_admission(
                    fenced["token"],
                    expected_identity=IDENTITY,
                    transaction_id=TRANSACTION_ID,
                )
            )
        except Exception as exc:  # captured for the assertion thread
            failures.append(exc)

    worker = threading.Thread(target=accept, name="startup-accept-test")
    worker.start()
    assert entered.wait(2.0)
    assert worker.is_alive() is True
    assert config.run_admission_snapshot()["state"] == "startup-accepting"
    with pytest.raises(config.RunAdmissionClosed):
        config.reserve_run_admission(kind="ordinary-during-recovery")

    release.set()
    worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert failures == []
    assert accepted[0]["state"] == "open"


def test_failed_one_shot_recovery_keeps_managed_startup_fenced(
    monkeypatch,
    isolated_startup_admission,
):
    import server
    from api import routes

    def failed_recovery(*, strict=False):
        assert strict is True
        raise RuntimeError("synthetic continuation recovery failure")

    monkeypatch.setattr(
        routes,
        "_recover_goal_continuations_on_startup",
        failed_recovery,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: (
            (
                "goal_continuation_recovery",
                server._recover_goal_continuations_for_startup,
            ),
        ),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    with pytest.raises(config.RunAdmissionBusy, match="deferred startup failed"):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity=IDENTITY,
            transaction_id=TRANSACTION_ID,
        )

    snapshot = config.run_admission_snapshot()
    assert snapshot["state"] == "startup-fenced"
    assert snapshot["startup_error"] == "deferred_startup_failed"
    assert server._DEFERRED_STARTUP_COMPLETED == set()


@pytest.mark.parametrize(
    ("step_name", "server_recovery", "module_name", "receipts"),
    (
        (
            "tool_limit_continuation_recovery",
            "_recover_tool_limit_continuations_for_startup",
            "tool_limit_continuation",
            {
                "tool-receipt": {
                    "state": "claimed",
                    "child_session_id": "child-session",
                },
                "tool-starting-receipt": {
                    "state": "starting",
                    "child_session_id": "starting-child-session",
                    "owner_pid": os.getpid(),
                },
            },
        ),
        (
            "goal_continuation_recovery",
            "_recover_goal_continuations_for_startup",
            "goal_continuation",
            {
                "goal-receipt": {
                    "state": "claimed",
                    "session_id": "goal-session",
                },
                "goal-starting-receipt": {
                    "state": "starting",
                    "session_id": "starting-goal-session",
                    "owner_pid": os.getpid(),
                },
            },
        ),
    ),
)
def test_pending_continuation_recovery_rejects_accept_without_launching_turn(
    monkeypatch,
    isolated_startup_admission,
    step_name,
    server_recovery,
    module_name,
    receipts,
):
    import server
    from api import routes

    continuation_module = __import__(f"api.{module_name}", fromlist=[module_name])
    launched = []
    recovery_calls = []
    monkeypatch.setattr(
        continuation_module,
        "load_receipts",
        lambda: {"version": 1, "receipts": receipts},
    )
    recover_name = (
        "recover_pending_continuations"
        if module_name == "tool_limit_continuation"
        else "recover_pending_goal_continuations"
    )
    monkeypatch.setattr(
        continuation_module,
        recover_name,
        lambda **_kwargs: recovery_calls.append("mutated") or 1,
    )
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *_args, **_kwargs: launched.append("turn") or {},
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: ((step_name, getattr(server, server_recovery)),),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    with pytest.raises(config.RunAdmissionBusy, match="deferred startup failed"):
        config.accept_startup_run_admission(
            fenced["token"],
            expected_identity=IDENTITY,
            transaction_id=TRANSACTION_ID,
        )

    assert config.run_admission_snapshot()["state"] == "startup-fenced"
    assert server._DEFERRED_STARTUP_COMPLETED == set()
    assert recovery_calls == []
    assert launched == []
    with pytest.raises(config.RunAdmissionClosed):
        config.reserve_run_admission(kind="ordinary-after-pending-recovery")


@pytest.mark.parametrize(
    ("step_name", "server_recovery", "module_name"),
    (
        (
            "tool_limit_continuation_recovery",
            "_recover_tool_limit_continuations_for_startup",
            "tool_limit_continuation",
        ),
        (
            "goal_continuation_recovery",
            "_recover_goal_continuations_for_startup",
            "goal_continuation",
        ),
    ),
)
def test_zero_pending_continuation_recovery_accepts_without_mutation(
    monkeypatch,
    isolated_startup_admission,
    step_name,
    server_recovery,
    module_name,
):
    import server

    continuation_module = __import__(f"api.{module_name}", fromlist=[module_name])
    recovery_calls = []
    monkeypatch.setattr(
        continuation_module,
        "load_receipts",
        lambda: {"version": 1, "receipts": {}},
    )
    recover_name = (
        "recover_pending_continuations"
        if module_name == "tool_limit_continuation"
        else "recover_pending_goal_continuations"
    )
    monkeypatch.setattr(
        continuation_module,
        recover_name,
        lambda **_kwargs: recovery_calls.append("mutated") or 0,
    )
    monkeypatch.setattr(server, "_DEFERRED_STARTUP_COMPLETED", set())
    monkeypatch.setattr(
        server,
        "_deferred_startup_steps",
        lambda: ((step_name, getattr(server, server_recovery)),),
    )
    _select_managed_candidate(monkeypatch)
    assert server._prepare_startup_mutators() == "deferred"
    fenced = _claim_startup_fence()

    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert server._DEFERRED_STARTUP_COMPLETED == {step_name}
    assert recovery_calls == []


def test_background_services_must_be_alive_before_startup_can_complete(
    monkeypatch,
):
    import server
    from api import background_process

    stopped = []

    class DeadWorker:
        @staticmethod
        def is_alive():
            return False

    monkeypatch.setattr(background_process, "_DRAIN_THREAD", DeadWorker())
    monkeypatch.setattr(background_process, "_REAPER_THREAD", DeadWorker())
    monkeypatch.setattr(background_process, "start_drain_thread", lambda: True)
    monkeypatch.setattr(
        background_process,
        "start_session_channel_reaper",
        lambda: True,
    )
    monkeypatch.setattr(
        background_process,
        "stop_drain_thread",
        lambda: stopped.append("drain"),
    )
    monkeypatch.setattr(
        background_process,
        "stop_session_channel_reaper",
        lambda: stopped.append("reaper"),
    )

    with pytest.raises(RuntimeError, match="drain thread is not alive"):
        server._start_startup_background_services()

    assert stopped == ["drain"]


def test_deferred_startup_configuration_commits_once_only_during_accept(
    monkeypatch,
    tmp_path,
    isolated_startup_admission,
):
    settings_file = tmp_path / "settings.json"
    settings_text = '{"default_workspace":"/accepted/workspace"}'
    resolver_calls = []
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        config,
        "_DEFERRED_STARTUP_SETTINGS_TEXT",
        settings_text,
    )
    monkeypatch.setattr(config, "CLI_TOOLSETS", ["fenced-fallback"])
    monkeypatch.setattr(
        config,
        "_resolve_cli_toolsets",
        lambda: resolver_calls.append("resolved") or ["accepted-toolset"],
    )
    _select_managed_candidate(monkeypatch)

    with pytest.raises(config.RunAdmissionClosed):
        config.apply_deferred_startup_configuration()
    assert settings_file.exists() is False

    config.configure_startup_acceptor(config.apply_deferred_startup_configuration)
    fenced = _claim_startup_fence()
    accepted = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )
    repeated = config.accept_startup_run_admission(
        fenced["token"],
        expected_identity=IDENTITY,
        transaction_id=TRANSACTION_ID,
    )

    assert accepted["state"] == "open"
    assert repeated["state"] == "open"
    assert settings_file.read_text(encoding="utf-8") == settings_text
    assert config._DEFERRED_STARTUP_SETTINGS_TEXT is None
    assert config.CLI_TOOLSETS == ["accepted-toolset"]
    assert resolver_calls == ["resolved"]


def test_startup_fence_request_allowlist_is_health_and_loopback_control_only(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    _select_managed_candidate(monkeypatch)

    assert server._startup_request_allowed("GET", "/health") is True
    assert (
        server._startup_request_allowed("POST", "/api/internal/release-control")
        is True
    )
    assert server._startup_request_allowed("GET", "/api/internal/release-control") is False
    assert server._startup_request_allowed("GET", "/") is False
    assert server._startup_request_allowed("GET", "/api/sessions") is False
    assert server._startup_request_allowed("POST", "/api/internal/recovery/start") is False
    assert server._startup_request_allowed("OPTIONS", "/health") is False


def test_startup_fence_blocks_get_and_write_handlers_before_route_dispatch(
    monkeypatch,
    isolated_startup_admission,
):
    import server

    responses = []
    dispatched = []
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda *_a: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda *_a: None)
    monkeypatch.setattr(server, "j", lambda _h, payload, status=200: responses.append((status, payload)))
    _select_managed_candidate(monkeypatch)

    get_handler = object.__new__(server.Handler)
    get_handler.path = "/api/sessions"
    get_handler.command = "GET"
    get_handler.headers = {}
    server.Handler.do_GET(get_handler)

    write_handler = object.__new__(server.Handler)
    write_handler.path = "/api/internal/recovery/start"
    write_handler.command = "POST"
    write_handler.headers = {}
    server.Handler._handle_write(
        write_handler,
        lambda *_a: dispatched.append("write"),
    )

    assert dispatched == []
    expected = {
        "error": "WebUI candidate is awaiting release acceptance",
        "code": "startup_fence",
        "retryable": True,
    }
    assert responses == [(503, expected), (503, expected)]


def test_startup_fenced_deep_health_is_in_memory_and_does_not_build_index(
    monkeypatch,
    tmp_path,
    isolated_startup_admission,
):
    from api import models, routes

    missing_index = tmp_path / "sessions-index.json"
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", missing_index)
    monkeypatch.setattr(routes, "SESSIONS", {"already-loaded": object()})
    monkeypatch.setattr(routes, "_stream_runtime_diagnostics", lambda: {})
    monkeypatch.setattr(
        routes,
        "all_sessions",
        lambda: (_ for _ in ()).throw(AssertionError("projection must not run")),
    )
    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("project storage must not be read")
        ),
    )
    monkeypatch.setattr(
        routes,
        "_active_state_db_path",
        lambda: (_ for _ in ()).throw(AssertionError("state DB must not open")),
    )
    _select_managed_candidate(monkeypatch)

    checks, healthy = routes._deep_health_checks(
        stream_check={"status": "ok", "active_streams": 0}
    )

    assert healthy is True
    assert checks["startup_fence"] == {
        "status": "fenced",
        "mutation_free": True,
    }
    assert checks["sessions"] == {
        "status": "deferred",
        "loaded_count": 1,
    }
    assert missing_index.exists() is False


def test_pair_gate_does_not_defer_deep_health_after_startup_acceptance(
    monkeypatch,
    tmp_path,
):
    from api import routes

    monkeypatch.setattr(
        routes.api_config,
        "startup_run_admission_is_closed",
        lambda: True,
    )
    monkeypatch.setattr(
        routes.api_config,
        "run_admission_snapshot",
        lambda: {"state": "open", "effective_state": "pair-gated"},
    )
    monkeypatch.setattr(routes, "_stream_runtime_diagnostics", lambda: {})
    monkeypatch.setattr(routes, "all_sessions", lambda: ["session-a"])
    monkeypatch.setattr(routes, "load_projects", lambda **_kwargs: {"project-a": {}})
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "missing.db")

    checks, healthy = routes._deep_health_checks(
        stream_check={"status": "ok", "active_streams": 0}
    )

    assert healthy is True
    assert checks["sessions"]["status"] == "ok"
    assert checks["projects"]["status"] == "ok"
    assert checks["state_db"]["status"] == "missing"
    assert "startup_fence" not in checks


def test_fresh_managed_import_and_prepare_do_not_mutate_state_before_accept(
    tmp_path,
):
    """Managed env must be installed before imports to exercise the real fence."""
    repo_root = os.path.dirname(os.path.dirname(__file__))
    isolated_root = tmp_path / "managed-startup"
    home = isolated_root / "home"
    hermes_home = isolated_root / "hermes"
    state_dir = hermes_home / "webui"
    workspace = isolated_root / "workspace"
    for directory in (home, hermes_home, state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Textually noncanonical and not yet created. Import-time normalization
    # used to rewrite settings.json and workspace discovery could mkdir here.
    noncanonical_workspace = workspace / ".." / workspace.name
    settings_file = state_dir / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "theme": "dark",
                "default_workspace": str(noncanonical_workspace),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (state_dir / "state.db").write_bytes(b"sentinel-state-db")
    before = _tree_bytes_and_mtimes(isolated_root)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "HERMES_BASE_HOME": str(hermes_home),
            "HERMES_WEBUI_STATE_DIR": str(state_dir),
            "HERMES_WEBUI_TEST_STATE_DIR": str(state_dir),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
            "HERMES_WEBUI_AGENT_DIR": os.path.join(
                os.path.dirname(repo_root), "agent"
            ),
            "HERMES_WEBUI_RELEASE_PATH": repo_root,
            "HERMES_WEBUI_LAUNCH_MODE": "selector",
            "HERMES_WEBUI_MANIFEST_SHA256": "a" * 64,
            "HERMES_WEBUI_STARTUP_FENCED": "1",
            "HERMES_WEBUI_STARTUP_TRANSACTION_ID": "t" * 40,
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
            "HERMES_CONFIG_PATH": str(hermes_home / "config.yaml"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    script = """
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

root = Path(os.environ["HERMES_BASE_HOME"]).parent

def snapshot():
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else str(path.relative_to(root))
        stat = path.stat(follow_symlinks=False)
        result[relative] = {
            "mtime_ns": stat.st_mtime_ns,
            "sha256": None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result

def changed(before, after):
    return sorted(
        key for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    )

initial = snapshot()
started_threads = []
sqlite_connects = []
real_thread_start = threading.Thread.start
real_sqlite_connect = sqlite3.connect

def tracked_thread_start(thread, *args, **kwargs):
    started_threads.append(thread.name)
    return real_thread_start(thread, *args, **kwargs)

def tracked_sqlite_connect(database, *args, **kwargs):
    sqlite_connects.append(str(database))
    return real_sqlite_connect(database, *args, **kwargs)

threading.Thread.start = tracked_thread_start
sqlite3.connect = tracked_sqlite_connect
from api import config
after_config = snapshot()
import server
after_server = snapshot()

mode = server._prepare_startup_mutators()
after_prepare = snapshot()
print("FRESH_IMPORT_RECEIPT=" + json.dumps({
    "admission": config.run_admission_snapshot(),
    "changed_after_config": changed(initial, after_config),
    "changed_after_server": changed(after_config, after_server),
    "changed_after_prepare": changed(after_server, after_prepare),
    "mode": mode,
    "sqlite_connects": sqlite_connects,
    "started_threads": started_threads,
    "threads": [
        thread.name
        for thread in threading.enumerate()
        if thread is not threading.main_thread()
    ],
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt_line = next(
        line
        for line in completed.stdout.splitlines()
        if line.startswith("FRESH_IMPORT_RECEIPT=")
    )
    receipt = json.loads(receipt_line.split("=", 1)[1])

    assert receipt["admission"]["state"] == "startup-fenced"
    assert receipt["mode"] == "deferred"
    assert receipt["sqlite_connects"] == []
    assert receipt["started_threads"] == []
    assert receipt["threads"] == []
    assert receipt["changed_after_config"] == []
    assert receipt["changed_after_server"] == []
    assert receipt["changed_after_prepare"] == []
    assert _tree_bytes_and_mtimes(isolated_root) == before, receipt
