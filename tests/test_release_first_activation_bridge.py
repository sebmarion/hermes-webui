"""FIRST-activation CLI and legacy cron exclusion contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace

import pytest

from scripts import webui_release_cutover as cutover


def _candidate_identity(tmp_path: Path) -> dict:
    interpreter = tmp_path / "runtime" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o555)
    agent = tmp_path / "agent"
    site = tmp_path / "runtime" / "site-packages"
    python_home = tmp_path / "runtime" / "python-home"
    for path in (agent, site, python_home):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "build_id": "candidate-build",
        "interpreter_path": str(interpreter),
        "agent_source_path": str(agent),
        "runtime_python_home_path": str(python_home),
        "runtime_site_packages_path": str(site),
        "manifest_sha256": "1" * 64,
        "agent_source_manifest_sha256": "2" * 64,
        "runtime_manifest_sha256": "3" * 64,
    }


def _cli_plan(tmp_path: Path) -> tuple[dict, dict, Path]:
    bin_dir = tmp_path / "bin"
    shim_dir = tmp_path / "shims"
    bin_dir.mkdir(mode=0o700)
    shim_dir.mkdir(mode=0o700)
    old = tmp_path / "legacy-hermes"
    old.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    old.chmod(0o755)
    link = bin_dir / "hermes"
    link.symlink_to(old)
    plan = {
        "transaction_id": "first-activation-transaction-000001",
        "cli_link": str(link),
        "cli_old_target": str(old),
        "cli_shim_dir": str(shim_dir),
        "expected_candidate_identity": _candidate_identity(tmp_path),
    }
    prepared = {
        "legacy": {
            "cli": cutover._file_identity_receipt(link),
        }
    }
    return plan, prepared, link


def test_first_activation_cli_is_deny_gated_then_exactly_restored(tmp_path):
    plan, prepared, link = _cli_plan(tmp_path)

    intent = cutover._bootstrap_cli_gate_stage_intent_receipt(plan, prepared)
    installed = cutover._install_or_adopt_bootstrap_cli_gate(plan, intent)

    maintenance = intent["maintenance_shim"]
    candidate = intent["candidate_shim"]
    assert os.readlink(link) == maintenance["path"]
    assert Path(maintenance["path"]).stat().st_mode & 0o777 == 0o555
    assert Path(candidate["path"]).stat().st_mode & 0o777 == 0o555
    assert Path(maintenance["path"]).stat().st_nlink == 1
    deny_payload = Path(maintenance["path"]).read_text(encoding="utf-8")
    assert "import " not in deny_payload
    assert "hermes_cli" not in deny_payload
    assert installed["bounded_host_assumption"][
        "malicious_concurrent_same_uid_actor_excluded"
    ] is False

    restored = cutover._restore_bootstrap_cli_link(
        plan,
        prepared,
        {
            "cli_maintenance_gate_stage_intent": intent,
            "cli_maintenance_gate_installed": installed,
        },
    )

    assert restored["status"] == "restored"
    assert os.readlink(link) == plan["cli_old_target"]
    assert cutover._file_identity_receipt(link) == prepared["legacy"]["cli"]


def test_first_activation_cli_restore_refuses_foreign_target(tmp_path):
    plan, prepared, link = _cli_plan(tmp_path)
    intent = cutover._bootstrap_cli_gate_stage_intent_receipt(plan, prepared)
    installed = cutover._install_or_adopt_bootstrap_cli_gate(plan, intent)
    foreign = tmp_path / "foreign"
    foreign.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    foreign.chmod(0o755)
    link.unlink()
    link.symlink_to(foreign)

    with pytest.raises(cutover.DrainIdentityMismatch, match="foreign target"):
        cutover._restore_bootstrap_cli_link(
            plan,
            prepared,
            {
                "cli_maintenance_gate_stage_intent": intent,
                "cli_maintenance_gate_installed": installed,
            },
        )

    assert os.readlink(link) == str(foreign)


def test_first_activation_cli_crash_after_gate_publish_is_recoverable(tmp_path):
    plan, prepared, link = _cli_plan(tmp_path)
    intent = cutover._bootstrap_cli_gate_stage_intent_receipt(plan, prepared)
    cutover._install_or_adopt_bootstrap_cli_gate(plan, intent)
    assert os.readlink(link) == intent["maintenance_shim"]["path"]

    restored = cutover._restore_bootstrap_cli_link(
        plan,
        prepared,
        {"cli_maintenance_gate_stage_intent": intent},
    )

    assert restored["status"] == "restored"
    assert os.readlink(link) == plan["cli_old_target"]


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_candidate_cli_stage_rejects_alias_at_immutable_path(tmp_path, attack):
    plan, _prepared, _link = _cli_plan(tmp_path)
    payload = cutover._render_cli_shim(plan["expected_candidate_identity"])
    digest = hashlib.sha256(payload).hexdigest()
    expected = (
        Path(plan["cli_shim_dir"])
        / f"hermes-{plan['expected_candidate_identity']['build_id']}-{digest[:16]}"
    )
    foreign = tmp_path / "foreign-shim"
    foreign.write_bytes(payload)
    foreign.chmod(0o555)
    if attack == "symlink":
        expected.symlink_to(foreign)
    else:
        os.link(foreign, expected)

    with pytest.raises(cutover.ReleaseBuildError, match="immutable CLI shim"):
        cutover.stage_immutable_cli_shim(
            plan,
            plan["expected_candidate_identity"],
        )


def test_cli_cas_detects_inode_swap_before_replace(tmp_path, monkeypatch):
    plan, prepared, link = _cli_plan(tmp_path)
    intent = cutover._bootstrap_cli_gate_stage_intent_receipt(plan, prepared)
    real_read = cutover._read_cli_symlink_identity
    reads = 0

    def swap_after_first_read(path):
        nonlocal reads
        value = real_read(path)
        reads += 1
        if reads == 1:
            path.unlink()
            path.symlink_to(tmp_path / "foreign")
        return value

    monkeypatch.setattr(
        cutover,
        "_read_cli_symlink_identity",
        swap_after_first_read,
    )

    with pytest.raises(cutover.DrainIdentityMismatch, match="changed before CAS"):
        cutover._install_or_adopt_bootstrap_cli_gate(plan, intent)

    assert os.readlink(link) == str(tmp_path / "foreign")


def test_candidate_cli_stays_gated_until_durable_pair_opened(tmp_path):
    plan, prepared, link = _cli_plan(tmp_path)
    stage = cutover._bootstrap_cli_gate_stage_intent_receipt(plan, prepared)
    cutover._install_or_adopt_bootstrap_cli_gate(plan, stage)

    with pytest.raises(cutover.ReleaseBuildError, match="pair_opened"):
        cutover._bootstrap_cli_candidate_activation_intent(
            plan,
            stage,
            {},
        )
    assert os.readlink(link) == stage["maintenance_shim"]["path"]

    activation = cutover._bootstrap_cli_candidate_activation_intent(
        plan,
        stage,
        {
            "pair_opened": {
                "status": "verified",
                "owner_hash": "a" * 64,
                "payload_sha256": "b" * 64,
            }
        },
    )
    activated = cutover._activate_or_adopt_bootstrap_cli_candidate(
        plan,
        activation,
    )

    assert activated["status"] == "activated"
    assert os.readlink(link) == stage["candidate_shim"]["path"]
    adopted_after_crash = cutover._activate_or_adopt_bootstrap_cli_candidate(
        plan,
        activation,
    )
    assert adopted_after_crash["activation"]["status"] == "adopted"


def _cron_plan(
    tmp_path: Path,
    *,
    lock_mode: int | None = None,
) -> tuple[dict, Path]:
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    store = home / "process_notifications.json"
    store.write_text("{}\n", encoding="utf-8")
    store.chmod(0o600)
    plan = {
        "transaction_id": "legacy-cron-lock-transaction-000001",
        "synthetic_process_notifications_path": str(store),
        "timeout_seconds": 0.1,
        "interval_seconds": 0.001,
    }
    path = home / "cron" / ".tick.lock"
    if lock_mode is not None:
        path.parent.mkdir(mode=0o700)
        path.write_bytes(b"legacy-tick-lock\n")
        path.chmod(lock_mode)
    return plan, path


def _normalize_tick_lock(plan: dict) -> tuple[dict, dict]:
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    normalized = cutover._normalize_legacy_cron_tick_lock(plan, intent)
    return intent, normalized


def test_legacy_cron_tick_lock_is_private_exact_and_reentrant(tmp_path):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    intent, normalized = _normalize_tick_lock(plan)

    first = cutover._acquire_legacy_cron_tick_lock(plan)
    second = cutover._acquire_legacy_cron_tick_lock(plan)

    assert first == second
    assert first["status"] == "held"
    assert first["path"] == str(path)
    assert first["mode"] == 0o600
    assert first["nlink"] == 1
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert first["bounded_host_assumption"][
        "malicious_concurrent_same_uid_actor_excluded"
    ] is False
    released = cutover._release_legacy_cron_tick_lock(plan)
    assert released["status"] == "released"
    restored = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
    )
    assert restored["status"] == "restored"
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == b"legacy-tick-lock\n"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "world-writable"])
def test_legacy_cron_tick_lock_rejects_unsafe_leaf(tmp_path, attack):
    plan, path = _cron_plan(tmp_path)
    path.parent.mkdir(mode=0o700)
    target = tmp_path / "foreign-lock"
    target.write_bytes(b"")
    target.chmod(0o600)
    if attack == "symlink":
        path.symlink_to(target)
    elif attack == "hardlink":
        os.link(target, path)
    else:
        path.write_bytes(b"")
        path.chmod(0o666)

    with pytest.raises(cutover.ReleaseBuildError, match="cron tick lock"):
        cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)


def test_legacy_cron_tick_lock_detects_path_inode_swap(tmp_path):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    _intent, _normalized = _normalize_tick_lock(plan)
    held = cutover._acquire_legacy_cron_tick_lock(plan)
    path.unlink()
    path.write_bytes(b"")
    path.chmod(0o600)

    with pytest.raises(cutover.DrainIdentityMismatch, match="identity changed"):
        cutover._verify_legacy_cron_tick_lock(plan, held)

    handle = cutover._LEGACY_CRON_TICK_LOCKS.pop(plan["transaction_id"])
    handle.close()


@pytest.mark.parametrize("original_mode", [0o600, 0o644])
def test_legacy_cron_tick_lock_normalization_is_durable_and_reversible(
    tmp_path,
    original_mode,
):
    plan, path = _cron_plan(tmp_path, lock_mode=original_mode)
    before = path.read_bytes()

    intent, normalized = _normalize_tick_lock(plan)

    assert intent["original"]["mode"] == original_mode
    assert normalized["normalized"]["mode"] == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    restored = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
    )
    assert path.stat().st_mode & 0o777 == original_mode
    assert path.read_bytes() == before
    assert restored["status"] in {"already-restored", "restored"}


def test_legacy_cron_tick_lock_restore_adopts_exact_prior_restore(tmp_path):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    intent, normalized = _normalize_tick_lock(plan)

    first = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
    )
    second = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
    )

    assert first["status"] == "restored"
    assert second["status"] == "already-restored"
    assert second["restored"]["mode"] == 0o644
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_first_tick_lock_normalization_accepts_empty_mtime_churn_under_lock(
    tmp_path,
):
    plan, path = _cron_plan(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"")
    path.chmod(0o644)
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    before = path.stat()
    os.utime(
        path,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
    )

    normalized, held = (
        cutover._normalize_and_acquire_legacy_cron_tick_lock(plan, intent)
    )
    try:
        restored = cutover._restore_legacy_cron_tick_lock(
            plan,
            intent,
            normalized,
        )
    finally:
        cutover._release_legacy_cron_tick_lock(
            plan,
            allow_restored_mode=True,
        )

    assert held["status"] == "held"
    assert normalized["normalized"]["mode"] == 0o600
    assert restored["status"] == "restored"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_first_tick_lock_normalization_rejects_nonempty_mtime_churn(
    tmp_path,
):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    before = path.stat()
    os.utime(
        path,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before normalization",
    ):
        cutover._normalize_and_acquire_legacy_cron_tick_lock(plan, intent)


def test_first_tick_lock_postcheck_failure_restores_mode_before_unlock(
    tmp_path,
    monkeypatch,
):
    plan, path = _cron_plan(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"")
    path.chmod(0o644)
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    real_match = cutover._legacy_cron_tick_receipts_match_locked_mtime_churn
    calls = 0

    def fail_postcheck(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            return False
        return real_match(*args, **kwargs)

    monkeypatch.setattr(
        cutover,
        "_legacy_cron_tick_receipts_match_locked_mtime_churn",
        fail_postcheck,
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed after normalization",
    ):
        cutover._normalize_and_acquire_legacy_cron_tick_lock(plan, intent)

    handle = cutover._LEGACY_CRON_TICK_LOCKS.get(plan["transaction_id"])
    assert handle is None or handle.closed
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_first_tick_lock_normalize_failure_restores_mode_before_unlock(
    tmp_path,
    monkeypatch,
):
    plan, path = _cron_plan(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"")
    path.chmod(0o644)
    intent = cutover._legacy_cron_tick_lock_normalize_intent_receipt(plan)
    real_match = cutover._legacy_cron_tick_receipts_match_locked_mtime_churn
    calls = 0

    def fail_after_chmod(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return real_match(*args, **kwargs)

    monkeypatch.setattr(
        cutover,
        "_legacy_cron_tick_receipts_match_locked_mtime_churn",
        fail_after_chmod,
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed during normalization",
    ):
        cutover._normalize_and_acquire_legacy_cron_tick_lock(plan, intent)

    handle = cutover._LEGACY_CRON_TICK_LOCKS.get(plan["transaction_id"])
    assert handle is None or handle.closed
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_legacy_cron_tick_lock_restore_refuses_foreign_mutation(tmp_path):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    intent, normalized = _normalize_tick_lock(plan)
    path.unlink()
    path.write_bytes(b"foreign\n")
    path.chmod(0o600)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before restore",
    ):
        cutover._restore_legacy_cron_tick_lock(
            plan,
            intent,
            normalized,
        )

    assert path.read_bytes() == b"foreign\n"


def test_absent_legacy_cron_tick_lock_is_removed_on_abort(tmp_path):
    plan, path = _cron_plan(tmp_path)
    intent, normalized = _normalize_tick_lock(plan)
    assert path.stat().st_mode & 0o777 == 0o600

    restored = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
    )

    assert restored["status"] == "removed"
    assert not path.exists()


def test_legacy_cron_tick_restore_accepts_only_receipted_snapshot_rebind(
    tmp_path,
):
    plan, path = _cron_plan(tmp_path, lock_mode=0o644)
    intent, normalized = _normalize_tick_lock(plan)
    payload = path.read_bytes()
    path.unlink()
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before restore",
    ):
        cutover._restore_legacy_cron_tick_lock(
            plan,
            intent,
            normalized,
        )

    restored = cutover._restore_legacy_cron_tick_lock(
        plan,
        intent,
        normalized,
        state_restore={
            "status": "restored",
            "state_snapshot_id": plan["transaction_id"],
        },
    )
    assert restored["status"] == "restored"
    assert restored["snapshot_rebound"] is True
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == payload


def _dispatcher_plan(
    tmp_path: Path,
    *,
    parent_mode: int,
) -> tuple[dict, Path, Path]:
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    store = home / "process_notifications.json"
    store.write_text('{"version":1,"events":{}}\n', encoding="utf-8")
    store.chmod(0o600)
    parent = home / "kanban"
    parent.mkdir(mode=parent_mode)
    parent.chmod(parent_mode)
    lock = parent / ".dispatcher.lock"
    lock.touch(mode=0o644)
    return (
        {
            "transaction_id": "dispatcher-lock-transaction-000001",
            "synthetic_process_notifications_path": str(store),
            "timeout_seconds": 1,
            "interval_seconds": 0.01,
        },
        parent,
        lock,
    )


def test_legacy_dispatcher_lock_accepts_owned_nonwritable_0755_parent(
    tmp_path,
):
    plan, parent, lock = _dispatcher_plan(tmp_path, parent_mode=0o755)

    try:
        held = cutover._acquire_legacy_dispatcher_lock(plan)
        verified = cutover._verify_legacy_dispatcher_lock(plan, held)

        assert held["status"] == "held"
        assert verified == held
        assert parent.stat().st_mode & 0o777 == 0o755
        assert lock.stat().st_mode & 0o777 == 0o600
    finally:
        cutover._release_legacy_dispatcher_lock(plan)


@pytest.mark.parametrize("parent_mode", [0o775, 0o757])
def test_legacy_dispatcher_lock_rejects_group_or_world_writable_parent(
    tmp_path,
    parent_mode,
):
    plan, _parent, lock = _dispatcher_plan(
        tmp_path,
        parent_mode=parent_mode,
    )

    with pytest.raises(cutover.ReleaseBuildError, match="path is unsafe"):
        cutover._acquire_legacy_dispatcher_lock(plan)

    assert lock.stat().st_mode & 0o777 == 0o644


def test_legacy_dispatcher_lock_rejects_foreign_owned_parent(
    tmp_path,
    monkeypatch,
):
    plan, _parent, lock = _dispatcher_plan(tmp_path, parent_mode=0o755)
    real_uid = os.getuid()
    monkeypatch.setattr(cutover.os, "getuid", lambda: real_uid + 1)

    with pytest.raises(cutover.ReleaseBuildError, match="path is unsafe"):
        cutover._acquire_legacy_dispatcher_lock(plan)

    assert lock.stat().st_mode & 0o777 == 0o644


def test_legacy_dispatcher_lock_rejects_leaf_symlink(tmp_path):
    plan, _parent, lock = _dispatcher_plan(tmp_path, parent_mode=0o755)
    foreign = tmp_path / "foreign-dispatcher.lock"
    foreign.write_bytes(b"foreign\n")
    lock.unlink()
    lock.symlink_to(foreign)

    with pytest.raises(cutover.ReleaseBuildError, match="path is unsafe"):
        cutover._acquire_legacy_dispatcher_lock(plan)

    assert lock.is_symlink()
    assert foreign.read_bytes() == b"foreign\n"


def _write_json(path: Path, payload: dict, *, mode: int) -> bytes:
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    path.write_bytes(encoded)
    path.chmod(mode)
    return encoded


def _synthetic_plan(
    tmp_path: Path,
    *,
    async_mode: int = 0o644,
) -> tuple[dict, dict[str, bytes]]:
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    process_path = home / "process_notifications.json"
    delegation_path = home / "async_delegations.json"
    process_payload = {
        "version": 1,
        "events": {
            "process:proc_delivered:completion": {
                "event_id": "process:proc_delivered:completion",
                "type": "completion",
                "session_id": "proc_delivered",
                "delivered": True,
            },
            "process:proc_queued:completion": {
                "event_id": "process:proc_queued:completion",
                "type": "completion",
                "session_id": "proc_queued",
                "delivered": False,
            },
        },
    }
    delegation_payload = {
        "version": 1,
        "records": {
            "deleg_delivered": {
                "delegation_id": "deleg_delivered",
                "status": "completed",
                "delivery_status": "delivered",
                "record": {
                    "delegation_id": "deleg_delivered",
                    "status": "completed",
                    "parent_session_id": None,
                },
            },
            "deleg_queued": {
                "delegation_id": "deleg_queued",
                "status": "lost",
                "delivery_status": "queued",
                "record": {
                    "delegation_id": "deleg_queued",
                    "status": "lost",
                    "parent_session_id": None,
                },
            },
        },
    }
    process_bytes = _write_json(
        process_path,
        process_payload,
        mode=0o600,
    )
    delegation_bytes = _write_json(
        delegation_path,
        delegation_payload,
        mode=async_mode,
    )
    plan = {
        "transaction_id": "synthetic-store-transaction-000001",
        "synthetic_process_notifications_path": str(process_path),
        "synthetic_process_notifications_expected_sha256": hashlib.sha256(
            process_bytes
        ).hexdigest(),
        "synthetic_process_notification_ids": [
            "proc_delivered",
            "proc_queued",
        ],
        "synthetic_async_delegations_path": str(delegation_path),
        "synthetic_async_delegations_expected_sha256": hashlib.sha256(
            delegation_bytes
        ).hexdigest(),
        "synthetic_async_delegation_ids": [
            "deleg_delivered",
            "deleg_queued",
        ],
        "synthetic_quarantine_root": str(tmp_path / "quarantine"),
    }
    return plan, {
        "process_notifications": process_bytes,
        "async_delegations": delegation_bytes,
    }


def _normalize_synthetic_stores(plan: dict) -> tuple[dict, dict]:
    intent = cutover._synthetic_store_mode_normalize_intent_receipt(plan)
    normalized = cutover._normalize_synthetic_completion_store_modes(
        plan,
        intent,
    )
    return intent, normalized


def test_synthetic_store_mode_cas_uses_one_descriptor_snapshot(
    tmp_path,
    monkeypatch,
):
    store = tmp_path / "async_delegations.json"
    _write_json(
        store,
        {"version": 1, "records": {}},
        mode=0o644,
    )
    expected, _value = cutover._read_synthetic_store_receipt(
        store,
        label="synthetic async delegation store",
        allowed_modes={0o644},
    )

    def reject_second_path_snapshot(*_args, **_kwargs):
        raise AssertionError("mode CAS reopened the path before descriptor CAS")

    monkeypatch.setattr(
        cutover,
        "_read_synthetic_store_receipt",
        reject_second_path_snapshot,
    )

    observed = cutover._set_synthetic_store_mode(
        store,
        label="synthetic async delegation store",
        expected=expected,
        allowed_current_modes={0o644},
        target_mode=0o600,
    )

    assert observed["mode"] == 0o600
    assert observed["sha256"] == expected["sha256"]
    assert store.stat().st_ino == expected["inode"]


def test_synthetic_store_mode_cas_is_idempotent_at_target_mode(
    tmp_path,
    monkeypatch,
):
    store = tmp_path / "async_delegations.json"
    _write_json(
        store,
        {"version": 1, "records": {}},
        mode=0o600,
    )
    expected, _value = cutover._read_synthetic_store_receipt(
        store,
        label="synthetic async delegation store",
        allowed_modes={0o600},
    )

    def reject_mode_change(*_args, **_kwargs):
        raise AssertionError("mode CAS changed a store already at the target mode")

    monkeypatch.setattr(cutover.os, "fchmod", reject_mode_change)

    observed = cutover._set_synthetic_store_mode(
        store,
        label="synthetic async delegation store",
        expected=expected,
        allowed_current_modes={0o644},
        target_mode=0o600,
    )

    assert observed == expected
    assert store.stat().st_mode & 0o777 == 0o600


def test_synthetic_terminal_mixed_delivery_is_normalized_and_never_replayed(
    tmp_path,
):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    mode_intent, normalized = _normalize_synthetic_stores(plan)

    assert mode_intent["stores"]["process_notifications"]["original"][
        "mode"
    ] == 0o600
    assert mode_intent["stores"]["async_delegations"]["original"][
        "mode"
    ] == 0o644
    assert normalized["stores"]["async_delegations"]["normalized"][
        "mode"
    ] == 0o600
    inspected = cutover._inspect_synthetic_completion_stores(plan)
    assert inspected["process_notifications"]["delivered"] == 1
    assert inspected["process_notifications"]["queued"] == 1
    assert inspected["async_delegations"]["delivered"] == 1
    assert inspected["async_delegations"]["queued"] == 1
    assert inspected["async_delegations"]["terminal"] == 2

    quarantine_intent = cutover._synthetic_quarantine_intent_receipt(plan)
    quarantined = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
    )

    assert quarantined["status"] == "quarantined-never-replay"
    for name, empty_key in (
        ("process_notifications", "events"),
        ("async_delegations", "records"),
    ):
        store = quarantined[name]
        source = Path(store["source"]["path"])
        quarantine = Path(store["quarantine"]["path"])
        assert json.loads(source.read_bytes()) == {
            "version": 1,
            empty_key: {},
        }
        assert source.stat().st_mode & 0o777 == 0o600
        assert quarantine.read_bytes() == original[name]
        assert quarantine.stat().st_mode & 0o777 == 0o600


def test_synthetic_process_store_accepts_generation_bound_event_ids(tmp_path):
    plan, _original = _synthetic_plan(tmp_path, async_mode=0o600)
    path = Path(plan["synthetic_process_notifications_path"])
    payload = json.loads(path.read_bytes())
    legacy_id = "process:proc_delivered:completion"
    event = payload["events"].pop(legacy_id)
    process_start_token = "darwin-proc:123:456:789"
    generation = hashlib.sha256(process_start_token.encode()).hexdigest()[:24]
    event_id = f"process:proc_delivered:{generation}:completion"
    event["event_id"] = event_id
    event["process_start_token"] = process_start_token
    payload["events"][event_id] = event
    encoded = _write_json(path, payload, mode=0o600)
    plan["synthetic_process_notifications_expected_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    inspected = cutover._inspect_synthetic_completion_stores(plan)

    assert inspected["process_notifications"]["ids"] == [
        "proc_delivered",
        "proc_queued",
    ]


def test_synthetic_process_store_rejects_forged_generation_event_id(tmp_path):
    plan, _original = _synthetic_plan(tmp_path, async_mode=0o600)
    path = Path(plan["synthetic_process_notifications_path"])
    payload = json.loads(path.read_bytes())
    legacy_id = "process:proc_delivered:completion"
    event = payload["events"].pop(legacy_id)
    event_id = "process:proc_delivered:" + ("0" * 24) + ":completion"
    event["event_id"] = event_id
    event["process_start_token"] = "darwin-proc:123:456:789"
    payload["events"][event_id] = event
    encoded = _write_json(path, payload, mode=0o600)
    plan["synthetic_process_notifications_expected_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="event identity is invalid",
    ):
        cutover._inspect_synthetic_completion_stores(plan)


def test_synthetic_interrupted_delegation_is_terminal(tmp_path):
    plan, _original = _synthetic_plan(tmp_path, async_mode=0o600)
    path = Path(plan["synthetic_async_delegations_path"])
    payload = json.loads(path.read_bytes())
    entry = payload["records"]["deleg_queued"]
    entry["status"] = "interrupted"
    entry["record"]["status"] = "interrupted"
    entry["delivery_status"] = "delivered"
    encoded = _write_json(path, payload, mode=0o600)
    plan["synthetic_async_delegations_expected_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    inspected = cutover._inspect_synthetic_completion_stores(plan)

    assert inspected["async_delegations"]["terminal"] == 2
    assert inspected["async_delegations"]["delivered"] == 2
    assert inspected["async_delegations"]["running"] == 0


def test_synthetic_store_mode_abort_restores_exact_original_bytes_and_mode(
    tmp_path,
):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    intent, normalized = _normalize_synthetic_stores(plan)

    restored = cutover._restore_synthetic_completion_store_modes(
        plan,
        intent,
        normalized,
    )

    assert restored["status"] == "restored"
    for name, plan_key, expected_mode in (
        (
            "process_notifications",
            "synthetic_process_notifications_path",
            0o600,
        ),
        (
            "async_delegations",
            "synthetic_async_delegations_path",
            0o644,
        ),
    ):
        path = Path(plan[plan_key])
        assert path.read_bytes() == original[name]
        assert path.stat().st_mode & 0o777 == expected_mode


def test_synthetic_store_mode_rollback_restores_mode_without_replay(tmp_path):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    intent, normalized = _normalize_synthetic_stores(plan)
    quarantine_intent = cutover._synthetic_quarantine_intent_receipt(plan)
    quarantined = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
    )

    restored = cutover._restore_synthetic_completion_store_modes(
        plan,
        intent,
        normalized,
        quarantined=quarantined,
    )

    assert restored["status"] == "restored-with-quarantine"
    process = Path(plan["synthetic_process_notifications_path"])
    delegation = Path(plan["synthetic_async_delegations_path"])
    assert json.loads(process.read_bytes())["events"] == {}
    assert json.loads(delegation.read_bytes())["records"] == {}
    assert process.stat().st_mode & 0o777 == 0o600
    assert delegation.stat().st_mode & 0o777 == 0o644
    for name in ("process_notifications", "async_delegations"):
        quarantine = Path(quarantined[name]["quarantine"]["path"])
        assert quarantine.read_bytes() == original[name]
        assert quarantine.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("store_name", "mutation", "message"),
    [
        ("async_delegations", "running", "terminal"),
        ("async_delegations", "unknown", "terminal"),
        ("process_notifications", "nonallowlisted", "id set"),
    ],
)
def test_synthetic_store_rejects_nonterminal_unknown_or_nonallowlisted(
    tmp_path,
    store_name,
    mutation,
    message,
):
    plan, _original = _synthetic_plan(tmp_path, async_mode=0o600)
    if store_name == "async_delegations":
        path = Path(plan["synthetic_async_delegations_path"])
        payload = json.loads(path.read_bytes())
        status = "running" if mutation == "running" else "mystery"
        payload["records"]["deleg_queued"]["status"] = status
        payload["records"]["deleg_queued"]["record"]["status"] = status
        encoded = _write_json(path, payload, mode=0o600)
        plan["synthetic_async_delegations_expected_sha256"] = hashlib.sha256(
            encoded
        ).hexdigest()
    else:
        plan["synthetic_process_notification_ids"] = ["proc_delivered"]
    _intent, _normalized = _normalize_synthetic_stores(plan)

    with pytest.raises(cutover.ReleaseBuildError, match=message):
        cutover._inspect_synthetic_completion_stores(plan)


def test_synthetic_store_restore_refuses_foreign_inode_or_content(tmp_path):
    plan, _original = _synthetic_plan(tmp_path, async_mode=0o644)
    intent, normalized = _normalize_synthetic_stores(plan)
    delegation = Path(plan["synthetic_async_delegations_path"])
    delegation.unlink()
    delegation.write_text('{"foreign":true}\n', encoding="utf-8")
    delegation.chmod(0o600)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before restore",
    ):
        cutover._restore_synthetic_completion_store_modes(
            plan,
            intent,
            normalized,
        )

    assert delegation.read_text(encoding="utf-8") == '{"foreign":true}\n'


def test_synthetic_quarantine_crash_resume_preserves_exact_bytes(tmp_path):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    _intent, _normalized = _normalize_synthetic_stores(plan)
    quarantine_intent = cutover._synthetic_quarantine_intent_receipt(plan)

    with pytest.raises(cutover.InjectedCutoverCrash):
        cutover._quarantine_synthetic_completion_stores(
            plan,
            quarantine_intent,
            crash_at="after_process_store",
        )
    quarantined = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
    )

    for name in ("process_notifications", "async_delegations"):
        quarantine = Path(quarantined[name]["quarantine"]["path"])
        assert quarantine.read_bytes() == original[name]


def test_synthetic_rollback_requarantine_discards_restored_snapshot_copy(
    tmp_path,
):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    _intent, _normalized = _normalize_synthetic_stores(plan)
    quarantine_intent = cutover._synthetic_quarantine_intent_receipt(plan)
    first = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
    )
    for name, plan_key in (
        (
            "process_notifications",
            "synthetic_process_notifications_path",
        ),
        ("async_delegations", "synthetic_async_delegations_path"),
    ):
        source = Path(plan[plan_key])
        source.unlink()
        source.write_bytes(original[name])
        source.chmod(0o600)

    second = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
    )

    assert second["status"] == "quarantined-never-replay"
    assert json.loads(
        Path(plan["synthetic_process_notifications_path"]).read_bytes()
    )["events"] == {}
    assert json.loads(
        Path(plan["synthetic_async_delegations_path"]).read_bytes()
    )["records"] == {}
    for name in ("process_notifications", "async_delegations"):
        quarantine = Path(first[name]["quarantine"]["path"])
        assert quarantine.read_bytes() == original[name]


def test_synthetic_first_quarantine_requires_receipted_snapshot_rebind(
    tmp_path,
):
    plan, original = _synthetic_plan(tmp_path, async_mode=0o644)
    _intent, _normalized = _normalize_synthetic_stores(plan)
    quarantine_intent = cutover._synthetic_quarantine_intent_receipt(plan)
    for name, plan_key in (
        (
            "process_notifications",
            "synthetic_process_notifications_path",
        ),
        ("async_delegations", "synthetic_async_delegations_path"),
    ):
        source = Path(plan[plan_key])
        source.unlink()
        source.write_bytes(original[name])
        source.chmod(0o600)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before quarantine CAS",
    ):
        cutover._quarantine_synthetic_completion_stores(
            plan,
            quarantine_intent,
        )

    quarantined = cutover._quarantine_synthetic_completion_stores(
        plan,
        quarantine_intent,
        state_restore={
            "status": "restored",
            "state_snapshot_id": plan["transaction_id"],
        },
    )
    assert quarantined["status"] == "quarantined-never-replay"
    for name in ("process_notifications", "async_delegations"):
        quarantine = Path(quarantined[name]["quarantine"]["path"])
        assert quarantine.read_bytes() == original[name]


def test_graceful_gateway_stop_takes_tick_lock_before_process_admission(
    tmp_path,
    monkeypatch,
):
    plan, _path = _cron_plan(tmp_path)
    gateway = {"pid": 41, "pid_start_token": "gateway-start"}
    prepared = {"gateway": gateway}
    intent = {
        "planned_stop": {
            "path": str(cutover._legacy_gateway_planned_stop_path(plan)),
            "payload": {"release_transaction_id": plan["transaction_id"]},
        }
    }
    tick = {
        "status": "held",
        "path": str(cutover._legacy_cron_tick_lock_path(plan)),
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_acquire_legacy_cron_tick_lock",
        lambda _plan: events.append("tick-acquire") or tick,
    )
    monkeypatch.setattr(
        cutover,
        "_verify_legacy_cron_tick_lock",
        lambda _plan, receipt: events.append("tick-verify") or receipt,
    )

    def retirement(*_args, **_kwargs):
        events.append("process-admission")
        raise cutover.ReleaseBuildError("stop after ordering proof")

    monkeypatch.setattr(
        cutover,
        "_run_process_registry_retirement_barrier",
        retirement,
    )

    with pytest.raises(cutover.ReleaseBuildError, match="ordering proof"):
        cutover._gracefully_stop_legacy_gateway(plan, prepared, intent)

    assert events == ["tick-acquire", "tick-verify", "process-admission"]
    assert cutover._legacy_cron_tick_lock_path(plan).name == ".tick.lock"
    assert ".jobs.lock" not in str(cutover._legacy_cron_tick_lock_path(plan))


def test_launchd_service_override_receipt_is_exact_label_scoped(monkeypatch):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
    }
    output = """
        disabled services = {
            "ai.hermes.gateway.backup" => disabled
            "ai.hermes.gateway" => enabled
        }
    """
    monkeypatch.setattr(
        cutover,
        "_run_launchctl",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )

    receipt = cutover._launchd_service_override_receipt(
        plan,
        gateway=True,
    )

    assert receipt == {
        "target": "gui/501/ai.hermes.gateway",
        "domain": "gui/501",
        "label": "ai.hermes.gateway",
        "disabled": False,
        "override": "enabled",
    }


def test_launchd_service_override_receipt_rejects_malformed_exact_row(
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
    }
    monkeypatch.setattr(
        cutover,
        "_run_launchctl",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                'disabled services = {\n'
                '  "ai.hermes.gateway" => unknown\n'
                '}\n'
            ),
            stderr="",
        ),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="state is invalid",
    ):
        cutover._launchd_service_override_receipt(
            plan,
            gateway=True,
        )


def test_set_launchd_service_disabled_uses_exact_target_and_rechecks(
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
    }
    control = {
        "status": "prepared",
        "initial": {
            "target": "gui/501/ai.hermes.gateway",
            "domain": "gui/501",
            "label": "ai.hermes.gateway",
            "disabled": False,
            "override": "absent",
        },
        "restore_semantics": "enabled",
    }
    disabled = False
    calls = []

    def launchctl(*args, **_kwargs):
        nonlocal disabled
        calls.append(args)
        if args == ("print-disabled", "gui/501"):
            value = "disabled" if disabled else "enabled"
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    'disabled services = {\n'
                    f'  "ai.hermes.gateway" => {value}\n'
                    '}\n'
                ),
                stderr="",
            )
        if args == ("disable", "gui/501/ai.hermes.gateway"):
            disabled = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected launchctl call: {args!r}")

    monkeypatch.setattr(cutover, "_run_launchctl", launchctl)

    receipt = cutover._set_launchd_service_disabled(
        plan,
        control,
        disabled=True,
    )

    assert calls == [
        ("print-disabled", "gui/501"),
        ("disable", "gui/501/ai.hermes.gateway"),
        ("print-disabled", "gui/501"),
    ]
    assert receipt["status"] == "disabled"
    assert receipt["after"]["disabled"] is True


def test_gateway_stop_intent_durably_captures_restart_control(
    tmp_path,
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "legacy_state_db": str(tmp_path / "state.db"),
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
        "transaction_id": "restart-control-intent-transaction-000001",
    }
    prepared = {
        "gateway": {
            "pid": 41,
            "pid_start_token": "gateway-start",
        }
    }
    control = {
        "target": "gui/501/ai.hermes.gateway",
        "domain": "gui/501",
        "label": "ai.hermes.gateway",
        "disabled": False,
        "override": "absent",
    }
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 41)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: "gateway-start")
    monkeypatch.setattr(
        cutover,
        "_legacy_process_checkpoint_receipt",
        lambda _plan: {"status": "verified", "active_records": 0},
    )
    monkeypatch.setattr(
        cutover,
        "_read_legacy_gateway_status",
        lambda *_args, **_kwargs: (
            {"start_time": 123},
            {"sha256": "a" * 64, "mtime_ns": 100},
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": False,
            "inode": None,
            "mtime_ns": None,
            "path": str(tmp_path / ".clean_shutdown"),
            "sha256": None,
            "size": 0,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_legacy_gateway_log_baselines",
        lambda _plan: [],
    )
    monkeypatch.setattr(
        cutover,
        "_launchd_service_override_receipt",
        lambda _plan, *, gateway: control,
    )

    intent = cutover._legacy_gateway_stop_intent_receipt(
        plan,
        prepared,
        {"status": "verified"},
    )

    assert intent["launchd_restart_control"] == {
        "status": "prepared",
        "initial": control,
        "restore_semantics": "enabled",
    }


def test_exact_gateway_retire_disables_restart_before_sigint_and_reenables(
    tmp_path,
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "interval_seconds": 0.01,
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
        "timeout_seconds": 30,
    }
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    initial = {
        "target": "gui/501/ai.hermes.gateway",
        "domain": "gui/501",
        "label": "ai.hermes.gateway",
        "disabled": False,
        "override": "absent",
    }
    intent = {
        "launchd_restart_control": {
            "status": "prepared",
            "initial": initial,
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    state = {"alive": True, "job_loaded": True, "disabled": False}
    events = []

    def set_disabled(_plan, control, *, disabled):
        assert control == intent["launchd_restart_control"]
        state["disabled"] = disabled
        events.append("disable" if disabled else "enable")
        return {
            "status": "disabled" if disabled else "enabled",
            "target": initial["target"],
        }

    def signal_process(pid, sent_signal):
        assert pid == 41
        assert sent_signal == signal.SIGINT
        assert state["disabled"] is True
        events.append("sigint")

    def wait_for_exit(row, timeout, *, allow_exact_signaled_zombie):
        assert row == identity
        assert timeout == 30
        assert allow_exact_signaled_zombie is True
        events.append("wait")
        state["alive"] = False

    def bootout(_plan, *, gateway, required):
        assert gateway is True
        assert required is False
        assert state["alive"] is False
        events.append("bootout")
        state["job_loaded"] = False
        return {
            "status": "stopped",
            "target": initial["target"],
        }

    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        set_disabled,
    )
    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda _row: state["alive"],
    )
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: (
            41 if gateway and state["job_loaded"] and state["alive"] else None
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_listener_pid",
        lambda _port: 41 if state["alive"] else None,
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda _pid: "gateway-start" if state["alive"] else None,
    )
    monkeypatch.setattr(cutover.os, "kill", signal_process)
    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        wait_for_exit,
    )
    monkeypatch.setattr(cutover, "_bootout_job", bootout)
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": True,
            "mtime_ns": 200,
            "sha256": "a" * 64,
        },
    )

    receipt = cutover._retire_exact_legacy_gateway(
        plan,
        identity,
        intent,
        prepare_stop=lambda: events.append("prepare") or {
            "status": "prepared"
        },
    )

    assert events == [
        "disable",
        "prepare",
        "sigint",
        "wait",
        "bootout",
        "enable",
    ]
    assert receipt["status"] == "stopped"
    assert receipt["signal"] == "SIGINT"
    assert state == {"alive": False, "job_loaded": False, "disabled": False}


def test_listener_probe_distinguishes_clean_absence_from_ambiguity(monkeypatch):
    results = iter(
        [
            SimpleNamespace(returncode=1, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="41\n42\n", stderr=""),
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="lsof: probe failed\n",
            ),
        ]
    )
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: next(results),
    )

    with pytest.raises(cutover.ListenerAbsent, match="no listener"):
        cutover._listener_pid(8642)
    with pytest.raises(
        cutover.ListenerProbeAmbiguous,
        match="unavailable or ambiguous",
    ):
        cutover._listener_pid(8642)
    with pytest.raises(
        cutover.ListenerProbeAmbiguous,
        match="probe failed",
    ):
        cutover._listener_pid(8642)


def test_exact_gateway_retire_resumes_after_clean_process_exit(
    tmp_path,
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "interval_seconds": 0.01,
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
        "timeout_seconds": 30,
    }
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    control = {
        "status": "prepared",
        "initial": {
            "target": "gui/501/ai.hermes.gateway",
            "domain": "gui/501",
            "label": "ai.hermes.gateway",
            "disabled": False,
            "override": "absent",
        },
        "restore_semantics": "enabled",
    }
    intent = {
        "launchd_restart_control": control,
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda _plan, _control, *, disabled: (
            events.append("disable" if disabled else "enable")
            or {"status": "disabled" if disabled else "enabled"}
        ),
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("clean resumed exit must not be signalled again")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": True,
            "mtime_ns": 200,
            "sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: events.append("bootout")
        or {"status": "stopped"},
    )
    monkeypatch.setattr(
        cutover,
        "_listener_pid",
        lambda _port: None,
    )
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: None,
    )

    receipt = cutover._retire_exact_legacy_gateway(
        plan,
        identity,
        intent,
        prepare_stop=lambda: (_ for _ in ()).throw(
            AssertionError("already-clean exit must not prepare another signal")
        ),
    )

    assert events == ["disable", "bootout", "enable"]
    assert receipt["status"] == "stopped"
    assert receipt["signal"] == "already-cleanly-exited"


def test_exact_gateway_retire_rejects_unclean_prior_exit_and_reenables(
    tmp_path,
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "interval_seconds": 0.01,
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
        "timeout_seconds": 30,
    }
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    intent = {
        "launchd_restart_control": {
            "status": "prepared",
            "initial": {
                "target": "gui/501/ai.hermes.gateway",
                "domain": "gui/501",
                "label": "ai.hermes.gateway",
                "disabled": False,
                "override": "absent",
            },
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda _plan, _control, *, disabled: (
            events.append("disable" if disabled else "enable")
            or {"status": "disabled" if disabled else "enabled"}
        ),
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": False,
            "mtime_ns": None,
            "sha256": None,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: events.append("unsafe-bootout"),
    )

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="fresh clean-shutdown",
    ):
        cutover._retire_exact_legacy_gateway(
            plan,
            identity,
            intent,
            prepare_stop=lambda: {"status": "prepared"},
        )

    assert events == ["disable", "enable"]


def test_exact_gateway_retire_rejects_ambiguous_listener_absence(
    tmp_path,
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "synthetic_process_notifications_path": str(
            tmp_path / "process_notifications.json"
        ),
        "timeout_seconds": 30,
    }
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    intent = {
        "launchd_restart_control": {
            "status": "prepared",
            "initial": {
                "target": "gui/501/ai.hermes.gateway",
                "disabled": False,
            },
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": {
            "exists": False,
            "mtime_ns": None,
        },
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda _plan, _control, *, disabled: (
            events.append("disable" if disabled else "enable")
            or {"status": "disabled" if disabled else "enabled"}
        ),
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_regular_file_baseline",
        lambda *_args, **_kwargs: {
            "exists": True,
            "mtime_ns": 200,
            "sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: events.append("bootout")
        or {"status": "stopped"},
    )
    monkeypatch.setattr(
        cutover,
        "_listener_pid",
        lambda _port: (_ for _ in ()).throw(
            cutover.ListenerProbeAmbiguous("ambiguous listener owners")
        ),
    )
    monkeypatch.setattr(cutover, "_job_pid", lambda *_args, **_kwargs: None)

    with pytest.raises(
        cutover.ListenerProbeAmbiguous,
        match="ambiguous listener owners",
    ):
        cutover._retire_exact_legacy_gateway(
            plan,
            identity,
            intent,
            prepare_stop=lambda: {"status": "prepared"},
        )

    assert events == ["disable", "bootout", "enable"]


def test_abort_restore_enables_restart_before_adopting_replacement(
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
    }
    prepared = {
        "gateway": {
            "pid": 41,
            "pid_start_token": "retired-start",
        }
    }
    control = {
        "status": "prepared",
        "initial": {
            "target": "gui/501/ai.hermes.gateway",
            "disabled": False,
        },
        "restore_semantics": "enabled",
    }
    events = []
    replacement = {
        "status": "verified",
        "pid": 52,
        "pid_start_token": "replacement-start",
    }
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda _plan, _control, *, disabled: (
            events.append("enable")
            or {"status": "enabled", "target": "gui/501/ai.hermes.gateway"}
        )
        if disabled is False
        else (_ for _ in ()).throw(AssertionError("abort must enable")),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda _plan, *, prepared, gateway: (
            events.append("attest") or replacement
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("healthy replacement must be adopted")
        ),
    )

    receipt = cutover._restore_legacy_gateway_before_snapshot_abort(
        plan,
        prepared,
        {"launchd_restart_control": control},
    )

    assert events == ["enable", "attest"]
    assert receipt["gateway"] == replacement
    assert receipt["recovery"]["status"] == "adopted-restored-binding"


def test_abort_restore_restarts_when_exact_gateway_is_cleanly_absent(
    monkeypatch,
):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
        "gateway_rollback_plist": "/tmp/legacy-gateway.plist",
    }
    prepared = {
        "gateway": {
            "pid": 41,
            "pid_start_token": "retired-start",
        }
    }
    control = {
        "status": "prepared",
        "initial": {
            "target": "gui/501/ai.hermes.gateway",
            "disabled": False,
        },
        "restore_semantics": "enabled",
    }
    events = []
    restored = {
        "status": "verified",
        "pid": 53,
        "pid_start_token": "restarted-start",
    }
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda _plan, _control, *, disabled: (
            events.append("enable")
            or {"status": "enabled", "target": "gui/501/ai.hermes.gateway"}
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.ReleaseBuildError("listener absent")
        ),
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(
        cutover,
        "_listener_pid",
        lambda _port: (_ for _ in ()).throw(
            cutover.ListenerAbsent("no listener owns TCP port 8642")
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda _plan, *, gateway, required: (
            events.append("bootout")
            or {"status": "not-loaded", "target": "gui/501/ai.hermes.gateway"}
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_bootstrap_job",
        lambda _plan, plist, *, gateway: (
            events.append(("bootstrap", plist))
            or {"status": "started", "target": "gui/501/ai.hermes.gateway"}
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_wait_for_legacy_binding",
        lambda _plan, *, prepared, gateway: (
            events.append("wait") or restored
        ),
    )

    receipt = cutover._restore_legacy_gateway_before_snapshot_abort(
        plan,
        prepared,
        {"launchd_restart_control": control},
    )

    assert events == [
        "enable",
        "bootout",
        ("bootstrap", "/tmp/legacy-gateway.plist"),
        "wait",
    ]
    assert receipt["gateway"]["pid"] == 53
    assert receipt["gateway"]["restart"]["status"] == "started"
    assert receipt["recovery"]["status"] == "restarted-cleanly-absent-binding"


def test_abort_restore_rejects_foreign_gateway_owner(monkeypatch):
    plan = {
        "gateway_launchd_domain": "gui/501",
        "gateway_launchd_label": "ai.hermes.gateway",
        "gateway_listener_port": 8642,
    }
    prepared = {
        "gateway": {
            "pid": 41,
            "pid_start_token": "retired-start",
            "command": "expected command",
        }
    }
    control = {
        "status": "prepared",
        "initial": {
            "target": "gui/501/ai.hermes.gateway",
            "disabled": False,
        },
        "restore_semantics": "enabled",
    }
    bootouts = []
    monkeypatch.setattr(
        cutover,
        "_set_launchd_service_disabled",
        lambda *_args, **_kwargs: {"status": "enabled"},
    )
    monkeypatch.setattr(
        cutover,
        "_attest_restored_legacy_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cutover.ReleaseBuildError("runtime changed")
        ),
    )
    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: False)
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover, "_listener_pid", lambda _port: 99)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: 99,
    )
    monkeypatch.setattr(
        cutover,
        "_listener_process_receipt",
        lambda *_args, **_kwargs: {"pid": 99, "command": "foreign command"},
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: bootouts.append(True),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="unexpected gateway owner",
    ):
        cutover._restore_legacy_gateway_before_snapshot_abort(
            plan,
            prepared,
            {"launchd_restart_control": control},
        )

    assert bootouts == []


def test_abort_restore_reacquires_tick_lock_after_crash(monkeypatch):
    plan = {"transaction_id": "abort-recovery-transaction-000001"}
    prepared = {"pre_managed_controls": {}}
    frozen = {"writers": []}
    tick_intent = {
        "original": {
            "status": "present",
            "mode": 0o644,
        }
    }
    phases = {
        "legacy_cron_tick_lock_normalize_intent": tick_intent,
        "legacy_cron_tick_lock_normalized": {"status": "normalized"},
    }
    events = []
    monkeypatch.setattr(
        cutover,
        "_restore_pre_managed_control_state",
        lambda *_args, **_kwargs: {"status": "restored"},
    )
    monkeypatch.setattr(
        cutover,
        "_restore_bootstrap_cli_link",
        lambda *_args, **_kwargs: {"status": "restored"},
    )
    monkeypatch.setattr(
        cutover,
        "_acquire_legacy_cron_tick_lock_modes",
        lambda _plan, *, allowed_modes: (
            events.append(("acquire", allowed_modes))
            or {"status": "held"}
        ),
    )

    def restore_tick(_plan, intent, normalization):
        assert intent is tick_intent
        assert normalization == {"status": "normalized"}
        assert events == [("acquire", {0o600, 0o644})]
        events.append("restore")
        return {"status": "already-restored"}

    monkeypatch.setattr(
        cutover,
        "_restore_legacy_cron_tick_lock",
        restore_tick,
    )
    monkeypatch.setattr(
        cutover,
        "_release_legacy_cron_tick_lock",
        lambda *_args, **_kwargs: (
            events.append("release") or {"status": "released"}
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_restore_legacy_gateway_before_snapshot_abort",
        lambda *_args, **_kwargs: {
            "gateway": {"status": "verified"},
            "launchd_restart": {"status": "enabled"},
            "recovery": {"status": "restored"},
        },
    )
    monkeypatch.setattr(
        cutover,
        "_restore_or_resume_frozen_legacy_webui",
        lambda *_args, **_kwargs: {
            "binding": {"status": "verified"},
            "writers": {"status": "resumed"},
        },
    )
    monkeypatch.setattr(
        cutover,
        "_restore_watchdog_cron",
        lambda *_args, **_kwargs: {"status": "restored"},
    )

    receipt = cutover._restore_legacy_before_snapshot_abort(
        plan,
        prepared,
        frozen,
        phases,
        cutover.ReleaseBuildError("resume boundary failed"),
    )

    assert events == [
        ("acquire", {0o600, 0o644}),
        "restore",
        "release",
    ]
    assert receipt["status"] == "aborted"
    assert receipt["cron_tick_lock"]["restore"]["status"] == (
        "already-restored"
    )


def test_gateway_bootout_freezes_before_marker_and_resumes_after_bootout(
    monkeypatch,
):
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    events = []
    process_state = {"value": "S"}

    def signal_process(pid, sent_signal):
        assert pid == 41
        events.append(("signal", sent_signal))
        process_state["value"] = "T" if sent_signal == signal.SIGSTOP else "S"

    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: process_state["value"],
    )
    monkeypatch.setattr(cutover.os, "kill", signal_process)
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: 41,
    )
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda _plan, *, gateway, required: (
            events.append(("bootout", gateway, required))
            or {"status": "stopped"}
        ),
    )

    def wait_for_exact_exit(
        row,
        timeout,
        *,
        allow_exact_signaled_zombie,
    ):
        events.append(
            (
                "wait-exit",
                row,
                timeout,
                allow_exact_signaled_zombie,
            )
        )

    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        wait_for_exact_exit,
    )

    receipt = cutover._bootout_exact_frozen_legacy_gateway(
        {"timeout_seconds": 1.0, "interval_seconds": 0.001},
        identity,
        prepare_stop=lambda: events.append(("prepare-stop",)) or {
            "status": "prepared"
        },
    )

    assert events == [
        ("signal", signal.SIGSTOP),
        ("prepare-stop",),
        ("bootout", True, True),
        ("signal", signal.SIGCONT),
        ("wait-exit", identity, 1.0, True),
    ]
    assert receipt["status"] == "stopped"
    assert receipt["bootout"]["retirement"] == "pending-exact-frozen-root"
    assert receipt["prepare_stop"] == {"status": "prepared"}


def test_gateway_bootout_resumes_exact_root_when_prepare_fails(monkeypatch):
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    signals = []
    process_state = {"value": "S"}

    def signal_process(_pid, sent_signal):
        signals.append(sent_signal)
        process_state["value"] = "T" if sent_signal == signal.SIGSTOP else "S"

    monkeypatch.setattr(cutover, "_exact_process_is_alive", lambda _row: True)
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: process_state["value"],
    )
    monkeypatch.setattr(cutover.os, "kill", signal_process)
    monkeypatch.setattr(
        cutover,
        "_bootout_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed preparation must not boot out launchd")
        ),
    )

    with pytest.raises(cutover.ReleaseBuildError, match="checkpoint changed"):
        cutover._bootout_exact_frozen_legacy_gateway(
            {"timeout_seconds": 1.0, "interval_seconds": 0.001},
            identity,
            prepare_stop=lambda: (_ for _ in ()).throw(
                cutover.ReleaseBuildError("checkpoint changed")
            ),
        )

    assert signals == [signal.SIGSTOP, signal.SIGCONT]


def test_gateway_bootout_never_resumes_reused_pid(monkeypatch):
    identity = {"pid": 41, "pid_start_token": "gateway-start"}
    signals = []
    process_state = {"value": "S", "exact": True}

    def signal_process(_pid, sent_signal):
        signals.append(sent_signal)
        process_state["value"] = "T" if sent_signal == signal.SIGSTOP else "S"

    def bootout(_plan, *, gateway, required):
        assert gateway is True
        assert required is True
        process_state["exact"] = False
        return {"status": "stopped"}

    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda _row: process_state["exact"],
    )
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda _pid, _field: process_state["value"],
    )
    monkeypatch.setattr(cutover.os, "kill", signal_process)
    job_pids = iter([41, None])
    monkeypatch.setattr(
        cutover,
        "_job_pid",
        lambda _plan, *, gateway: next(job_pids),
    )
    monkeypatch.setattr(cutover, "_bootout_job", bootout)
    monkeypatch.setattr(
        cutover,
        "wait_for_exact_process_exit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity loss must fail before exit wait")
        ),
    )

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="changed before SIGCONT",
    ):
        cutover._bootout_exact_frozen_legacy_gateway(
            {"timeout_seconds": 1.0, "interval_seconds": 0.001},
            identity,
            prepare_stop=lambda: {"status": "prepared"},
        )

    assert signals == [signal.SIGSTOP]


def test_mutable_writer_barrier_accepts_only_exact_stopped_writers(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.db"
    state.write_bytes(b"state")
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="p41\naw\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "writer-start" if pid == 41 else None,
    )
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T+")

    receipt = cutover._prove_no_mutable_writers(
        {"mutable_state_paths": [str(state)]}
    )

    assert receipt["writer_pids"] == [41]
    assert receipt["stopped_writers"] == [
        {
            "pid": 41,
            "pid_start_token": "writer-start",
            "state": "stopped",
        }
    ]
    assert (
        receipt["bounded_host_assumption"]
        == cutover._FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
    )
    assert (
        cutover._prove_no_mutable_writers(
            {"mutable_state_paths": [str(state)]},
            expected=receipt,
        )
        == receipt
    )


def test_mutable_writer_barrier_rejects_uncheckpointed_sqlite_wal(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.db"
    state.write_bytes(b"state")
    Path(f"{state}-wal").write_bytes(b"committed-wal-data")
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="p41\naw\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "writer-start" if pid == 41 else None,
    )
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="SQLite WAL is not checkpointed",
    ):
        cutover._prove_no_mutable_writers(
            {"mutable_state_paths": [str(state)]}
        )


def test_mutable_writer_barrier_rejects_runnable_writer(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    state.write_bytes(b"state")
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="p41\nau\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "writer-start" if pid == 41 else None,
    )
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "S")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="runnable writable handles: 41",
    ):
        cutover._prove_no_mutable_writers(
            {"mutable_state_paths": [str(state)]}
        )


def test_mutable_writer_barrier_rejects_identity_loss(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    state.write_bytes(b"state")
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="p41\naw\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(cutover, "_pid_start_token", lambda _pid: None)
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T")

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="writer identity is unavailable",
    ):
        cutover._prove_no_mutable_writers(
            {"mutable_state_paths": [str(state)]}
        )


def test_mutable_writer_barrier_rejects_handle_set_change(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.db"
    state.write_bytes(b"state")
    outputs = iter(("p41\naw\n", ""))
    monkeypatch.setattr(
        cutover.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=next(outputs),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "writer-start" if pid == 41 else None,
    )
    monkeypatch.setattr(cutover, "_ps_value", lambda _pid, _field: "T")
    plan = {"mutable_state_paths": [str(state)]}
    receipt = cutover._prove_no_mutable_writers(plan)

    with pytest.raises(
        cutover.DrainIdentityMismatch,
        match="mutable writer barrier changed",
    ):
        cutover._prove_no_mutable_writers(plan, expected=receipt)


def test_process_tree_freeze_ignores_tokenless_zombie_descendant(monkeypatch):
    root = {"pid": 41, "pid_start_token": "root-start"}
    signals = []
    monkeypatch.setattr(
        cutover,
        "_process_parent_table",
        lambda: {41: 1, 42: 41},
    )
    monkeypatch.setattr(
        cutover,
        "_pid_start_token",
        lambda pid: "root-start" if pid == 41 else None,
    )
    monkeypatch.setattr(
        cutover,
        "_exact_process_is_alive",
        lambda receipt: (
            receipt.get("pid") == 41
            and receipt.get("pid_start_token") == "root-start"
        ),
    )
    monkeypatch.setattr(
        cutover,
        "_ps_value",
        lambda pid, _field: "T" if pid == 41 else "Z",
    )
    monkeypatch.setattr(
        cutover.os,
        "kill",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    frozen = cutover._freeze_exact_process_tree(root, role="webui")

    assert frozen == {
        "role": "webui",
        "status": "frozen",
        "tree": [
            {
                "pid": 41,
                "ppid": None,
                "pid_start_token": "root-start",
                "state": "T",
            }
        ],
    }
    assert signals == [(41, signal.SIGSTOP)]
    assert (
        cutover._verify_frozen_prepared_writers(
            {"listener_port": 8787},
            {"legacy": root},
            {"status": "frozen", "writers": [frozen]},
        )
        == {"status": "frozen", "writers": [frozen]}
    )


def test_legacy_shutdown_parser_accepts_natural_nonzero_to_zero_drain():
    receipt = cutover._parse_legacy_gateway_shutdown_log(
        "Shutdown phase: drain done "
        "(timed_out=False, active_at_start=3, active_now=0, "
        "cron_at_start=7, cron_now=0)\n"
        "API server stopped\n"
        "Gateway stopped\n"
    )

    assert receipt == {
        "timed_out": False,
        "active_at_start": 3,
        "active_now": 0,
        "cron_at_start": 7,
        "cron_now": 0,
    }


def _terminal_gateway_status_fixture(state: str) -> tuple[dict, dict, dict]:
    return (
        {
            "kind": "hermes-gateway",
            "pid": 41,
            "gateway_state": state,
            "active_agents": 0,
        },
        {"sha256": "a" * 64, "mtime_ns": 200},
        {"mtime_ns": 100},
    )


def test_legacy_terminal_status_accepts_planned_double_signal_run_intent():
    status, receipt, baseline = _terminal_gateway_status_fixture("running")

    observed = cutover._legacy_gateway_terminal_status_receipt(
        status,
        receipt,
        status_baseline=baseline,
        gateway_pid=41,
        shutdown_log=(
            "Received UNKNOWN as a planned gateway stop — exiting cleanly\n"
            "Received SIGTERM — initiating shutdown\n"
            "Gateway stopped by an unexpected signal — persisting "
            "gateway_state=running so container_boot auto-starts\n"
        ),
    )

    assert observed["gateway_state"] == "running"
    assert observed["compatibility"] == (
        "planned-stop-double-signal-run-intent"
    )


@pytest.mark.parametrize(
    "shutdown_log",
    [
        "Gateway stopped by an unexpected signal — persisting "
        "gateway_state=running so container_boot auto-starts\n",
        "Received UNKNOWN as a planned gateway stop — exiting cleanly\n",
    ],
)
def test_legacy_terminal_status_rejects_unproved_running_state(shutdown_log):
    status, receipt, baseline = _terminal_gateway_status_fixture("running")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="not a fresh clean stop",
    ):
        cutover._legacy_gateway_terminal_status_receipt(
            status,
            receipt,
            status_baseline=baseline,
            gateway_pid=41,
            shutdown_log=shutdown_log,
        )


def test_legacy_terminal_status_rejects_reversed_double_signal_receipt():
    status, receipt, baseline = _terminal_gateway_status_fixture("running")

    with pytest.raises(
        cutover.ReleaseBuildError,
        match="not a fresh clean stop",
    ):
        cutover._legacy_gateway_terminal_status_receipt(
            status,
            receipt,
            status_baseline=baseline,
            gateway_pid=41,
            shutdown_log=(
                "Gateway stopped by an unexpected signal — persisting "
                "gateway_state=running so container_boot auto-starts\n"
                "Received UNKNOWN as a planned gateway stop — exiting cleanly\n"
            ),
        )


@pytest.mark.parametrize(
    "line",
    [
        "timed_out=True, active_at_start=3, active_now=0, "
        "cron_at_start=7, cron_now=0",
        "timed_out=False, active_at_start=3, active_now=1, "
        "cron_at_start=7, cron_now=0",
        "timed_out=False, active_at_start=3, active_now=0, "
        "cron_at_start=7, cron_now=1",
        "timed_out=False, active_at_start=-1, active_now=0, "
        "cron_at_start=0, cron_now=0",
    ],
)
def test_legacy_shutdown_parser_rejects_nonzero_terminal_or_invalid_counts(line):
    with pytest.raises(cutover.ReleaseBuildError, match="zero-work clean stop"):
        cutover._parse_legacy_gateway_shutdown_log(
            f"Shutdown phase: drain done ({line})\n"
            "API server stopped\n"
            "Gateway stopped\n"
        )


def test_bootstrap_phase_order_holds_cli_gate_and_tick_lock_through_stop():
    prerequisites = cutover._BOOTSTRAP_PHASE_PREREQUISITES

    assert prerequisites["cli_maintenance_gate_stage_intent"] == (
        "writers_frozen",
    )
    assert prerequisites["cli_maintenance_gate_installed"] == (
        "cli_maintenance_gate_stage_intent",
    )
    assert prerequisites["legacy_cron_tick_lock_normalize_intent"] == (
        "cli_maintenance_gate_installed",
    )
    assert prerequisites["legacy_cron_tick_lock_normalized"] == (
        "legacy_cron_tick_lock_normalize_intent",
    )
    assert prerequisites["legacy_cron_tick_lock_acquired"] == (
        "legacy_cron_tick_lock_normalized",
    )
    assert prerequisites["legacy_gateway_drain_intent"] == (
        "legacy_cron_tick_lock_acquired",
    )
    assert prerequisites["synthetic_store_mode_normalize_intent"] == (
        "legacy_gateway_gracefully_stopped",
    )
    assert prerequisites["synthetic_store_modes_normalized"] == (
        "synthetic_store_mode_normalize_intent",
    )
    assert prerequisites["legacy_dispatcher_lock_acquired"] == (
        "synthetic_store_modes_normalized",
    )
    assert prerequisites["legacy_cron_tick_lock_released"] == (
        "services_stopped",
    )
    assert prerequisites["ingress_gate_started"] == (
        "legacy_cron_tick_lock_released",
    )
    assert prerequisites["cli_candidate_activate_intent"] == (
        "candidate_pair_accepted",
    )
    assert prerequisites["cli_candidate_activated"] == (
        "cli_candidate_activate_intent",
    )
