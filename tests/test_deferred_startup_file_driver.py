from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import deferred_release_manifest
from deferred_startup_replay import (
    AFTER_INTENT,
    DeferredStartupCrash,
    DeferredStartupManifestReceipt,
    DeferredStartupStep,
    DeferredStartupStepState,
    Reconciliation,
    replay_deferred_startup,
)


TRANSACTION_ID = "startup-file-driver-" + ("x" * 32)
PROCESS_EPOCH = "startup-file-driver-process-epoch-" + ("e" * 32)


def _receipt(
    *,
    transaction_id: str = TRANSACTION_ID,
    version: int | None = None,
    sha256: str | None = None,
) -> DeferredStartupManifestReceipt:
    return DeferredStartupManifestReceipt(
        transaction_id=transaction_id,
        version=(
            deferred_release_manifest.MANIFEST_VERSION if version is None else version
        ),
        sha256=(
            deferred_release_manifest.deferred_release_manifest_sha256()
            if sha256 is None
            else sha256
        ),
    )


def _private_journal_path(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    return parent / "deferred-startup.json"


def _journal_payload(
    *,
    generation: int,
    steps: dict,
    previous_sha256: str = "0" * 64,
) -> dict:
    next_generation = 1
    attempt_steps = {}
    for step_name, record in steps.items():
        if type(record) is dict and record.get("intent") is True:
            attempt = {
                "attempt": 1,
                "process_epoch": PROCESS_EPOCH,
                "prior_completion_absent_policy": "deny",
                "intent": {"generation": next_generation},
            }
            next_generation += 1
            if "completion" in record:
                completion = dict(record["completion"])
                completion["generation"] = next_generation
                attempt["completion"] = completion
                next_generation += 1
            elif "indeterminate" in record:
                indeterminate = dict(record["indeterminate"])
                indeterminate["generation"] = next_generation
                attempt["indeterminate"] = indeterminate
                next_generation += 1
            attempt_steps[step_name] = {"attempts": [attempt]}
        else:
            attempt_steps[step_name] = record
    return {
        "version": 2,
        "generation": generation,
        "previous_sha256": previous_sha256,
        "transaction_id": TRANSACTION_ID,
        "manifest_receipt": {
            "version": deferred_release_manifest.MANIFEST_VERSION,
            "sha256": deferred_release_manifest.deferred_release_manifest_sha256(),
        },
        "steps": attempt_steps,
    }


def _canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _driver(path: Path, **kwargs):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    return DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        **kwargs,
    )


def _record_completed_step(path: str, step_name: str) -> None:
    driver = _driver(Path(path))
    driver.record_intent(TRANSACTION_ID, _receipt(), PROCESS_EPOCH, step_name)
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        step_name,
        recovered=False,
    )


def _extend_journal_multiple_generations(path: str) -> None:
    driver = _driver(Path(path))
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )
    driver.record_intent(TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "plugins")


def _exit_after_temp_fsync(path: str) -> None:
    from deferred_startup_file_driver import (
        AFTER_TEMP_FSYNC,
        DeferredStartupFileDriver,
    )

    def exit_on_temp(point):
        if point == AFTER_TEMP_FSYNC:
            os._exit(73)

    driver = DeferredStartupFileDriver(
        Path(path),
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=exit_on_temp,
    )
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )


def _exit_at_publish_point(path: str, crash_point: str) -> None:
    from deferred_startup_file_driver import DeferredStartupFileDriver

    def exit_at_point(point):
        if point == crash_point:
            os._exit(74)

    driver = DeferredStartupFileDriver(
        Path(path),
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=exit_at_point,
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )


def test_file_driver_persists_exact_schema_and_reconstructs_state(tmp_path):
    path = _private_journal_path(tmp_path)
    driver = _driver(path)

    assert (
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        == DeferredStartupStepState()
    )

    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=True,
    )

    empty = _journal_payload(generation=0, steps={})
    intent = _journal_payload(
        generation=1,
        previous_sha256=_canonical_sha256(empty),
        steps={"credential_permissions": {"intent": True}},
    )
    assert json.loads(path.read_bytes()) == _journal_payload(
        generation=2,
        previous_sha256=_canonical_sha256(intent),
        steps={
            "credential_permissions": {
                "intent": True,
                "completion": {"recovered": True},
            },
        },
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(".json.lock").stat().st_mode & 0o777 == 0o600
    assert _driver(path).read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)


def test_file_driver_survives_real_replay_restart_without_duplicate_mutation(
    tmp_path,
):
    path = _private_journal_path(tmp_path)
    effects = set()
    mutation_count = [0]

    def step():
        def mutate():
            mutation_count[0] += 1
            effects.add("credential_permissions")

        return DeferredStartupStep(
            name="credential_permissions",
            mutator=mutate,
            reconciler=lambda: (
                Reconciliation.PROVED_COMPLETE
                if "credential_permissions" in effects
                else Reconciliation.PROVED_ABSENT
            ),
        )

    def crash_after_intent(point, _step_name):
        if point == AFTER_INTENT:
            raise DeferredStartupCrash(point)

    with pytest.raises(DeferredStartupCrash, match=AFTER_INTENT):
        replay_deferred_startup(
            transaction_id=TRANSACTION_ID,
            manifest_receipt=_receipt(),
            process_epoch=PROCESS_EPOCH,
            steps=(step(),),
            driver=_driver(path),
            crash_hook=crash_after_intent,
        )

    result = replay_deferred_startup(
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        process_epoch=PROCESS_EPOCH,
        steps=(step(),),
        driver=_driver(path),
    )

    assert result.completed == ("credential_permissions",)
    assert mutation_count == [1]


@pytest.mark.parametrize("binding", ("transaction", "version", "digest"))
def test_file_driver_rejects_stale_journal_binding(tmp_path, binding):
    from deferred_startup_file_driver import (
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )

    transaction_id = TRANSACTION_ID
    receipt = _receipt()
    if binding == "transaction":
        transaction_id = "stale-file-driver-" + ("s" * 32)
        receipt = _receipt(transaction_id=transaction_id)
    elif binding == "version":
        receipt = _receipt(version=receipt.version + 1)
    else:
        receipt = _receipt(sha256="f" * 64)

    with pytest.raises(DeferredStartupFileDriverError, match="binding"):
        DeferredStartupFileDriver(
            path,
            transaction_id=transaction_id,
            manifest_receipt=receipt,
        )


def test_file_driver_preserves_all_steps_across_interprocess_writers(tmp_path):
    path = _private_journal_path(tmp_path)
    step_names = tuple(f"step_{index}" for index in range(8))
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_record_completed_step,
            args=(str(path), step_name),
        )
        for step_name in step_names
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0] * len(processes)
    driver = _driver(path)
    assert all(
        driver.read_step_state(TRANSACTION_ID, _receipt(), PROCESS_EPOCH, step_name)
        == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)
        for step_name in step_names
    )


def test_file_driver_transitions_are_write_once_and_idempotent(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )

    with pytest.raises(DeferredStartupFileDriverError, match="conflicting"):
        driver.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=True,
        )
    with pytest.raises(DeferredStartupFileDriverError, match="conflicting"):
        driver.record_indeterminate(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            reason="ambiguous",
        )
    with pytest.raises(DeferredStartupFileDriverError, match="no durable intent"):
        driver.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "plugins",
            recovered=False,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b'{"version":',
        b"not-json\n",
        b"",
    ),
)
def test_file_driver_fails_closed_on_partial_or_corrupt_journal(
    tmp_path,
    payload,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    path.write_bytes(payload)
    os.chmod(path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError):
        _driver(path).read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_enforces_size_and_private_metadata(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    path.write_bytes(b"x" * 1025)
    os.chmod(path, 0o600)
    with pytest.raises(DeferredStartupFileDriverError, match="too large"):
        _driver(path, max_bytes=1024).read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )

    path.write_bytes(b"{}")
    os.chmod(path, 0o644)
    with pytest.raises(DeferredStartupFileDriverError, match="unsafe"):
        _driver(path)

    path.unlink()
    os.chmod(path.parent, 0o755)
    with pytest.raises(DeferredStartupFileDriverError, match="parent is unsafe"):
        _driver(path)


def test_file_driver_rejects_relative_dotdot_and_symlinked_ancestor(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    with pytest.raises(DeferredStartupFileDriverError, match="canonical"):
        _driver(Path("relative.json"))
    with pytest.raises(DeferredStartupFileDriverError, match="canonical"):
        _driver(tmp_path / "missing" / ".." / "journal.json")

    real_parent = tmp_path / "real-private"
    real_parent.mkdir(mode=0o700)
    os.chmod(real_parent, 0o700)
    linked_parent = tmp_path / "linked-private"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(DeferredStartupFileDriverError, match="canonical|symlink"):
        _driver(linked_parent / "journal.json")


@pytest.mark.parametrize(
    ("crash_point", "expected_state"),
    (
        ("after-temp-fsync", DeferredStartupStepState(attempt_number=1, intent=True)),
        (
            "after-publish",
            DeferredStartupStepState(attempt_number=1, intent=True, completion=True),
        ),
    ),
)
def test_file_driver_reconstructs_after_atomic_write_crash(
    tmp_path,
    crash_point,
    expected_state,
):
    from deferred_startup_file_driver import (
        AFTER_PUBLISH,
        AFTER_REPLACE,
        AFTER_TEMP_FSYNC,
        DeferredStartupFileDriver,
    )

    assert AFTER_REPLACE == AFTER_PUBLISH
    assert crash_point in (AFTER_TEMP_FSYNC, AFTER_PUBLISH)
    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash(point):
        if point == crash_point:
            raise DeferredStartupCrash(point)

    crashing_driver = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash,
    )
    with pytest.raises(DeferredStartupCrash, match=crash_point):
        crashing_driver.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    assert (
        _driver(path).read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        == expected_state
    )


def test_file_driver_rejects_sensitive_reason_values(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )

    for reason in ("authorization", "fence_token", "bearer-secret"):
        with pytest.raises(DeferredStartupFileDriverError, match="sensitive"):
            driver.record_indeterminate(
                TRANSACTION_ID,
                _receipt(),
                PROCESS_EPOCH,
                "credential_permissions",
                reason=reason,
            )


@pytest.mark.parametrize("step_name", ("authorization", "cookie", "fence_token"))
def test_file_driver_rejects_sensitive_step_keys(tmp_path, step_name):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    with pytest.raises(DeferredStartupFileDriverError, match="sensitive"):
        _driver(path).record_intent(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            step_name,
        )


def test_file_driver_fails_closed_when_path_is_replaced_during_parse(
    tmp_path,
    monkeypatch,
):
    import deferred_startup_file_driver as file_driver

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    original_loads = file_driver.json.loads

    def replace_then_parse(payload, *args, **kwargs):
        replacement = path.with_name("replacement")
        replacement.write_text("{}")
        os.chmod(replacement, 0o600)
        os.replace(replacement, path)
        return original_loads(payload, *args, **kwargs)

    monkeypatch.setattr(file_driver.json, "loads", replace_then_parse)
    with pytest.raises(
        file_driver.DeferredStartupFileDriverError,
        match="changed during access",
    ):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_rejects_direct_symlink_hardlink_and_unsafe_lock(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    target = path.with_name("target")
    target.write_text("{}")
    os.chmod(target, 0o600)
    path.symlink_to(target)
    with pytest.raises(DeferredStartupFileDriverError, match="unsafe"):
        _driver(path)

    path.unlink()
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    hardlink = path.with_name("hardlink")
    os.link(path, hardlink)
    with pytest.raises(DeferredStartupFileDriverError, match="unsafe"):
        _driver(path)
    hardlink.unlink()

    lock_path = path.with_suffix(".json.lock")
    os.chmod(lock_path, 0o644)
    with pytest.raises(DeferredStartupFileDriverError, match="lock is unsafe"):
        _driver(path)


def test_file_driver_rejects_directory_target_and_too_small_size_bound(
    tmp_path,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    path.mkdir(mode=0o700)
    with pytest.raises(DeferredStartupFileDriverError, match="unsafe"):
        _driver(path)

    path.rmdir()
    with pytest.raises(DeferredStartupFileDriverError, match="too large"):
        _driver(path, max_bytes=128)


def test_file_driver_rejects_duplicate_keys_and_non_exact_scalar_types(
    tmp_path,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    payload = (
        '{"version":true,"version":1,'
        f'"transaction_id":"{TRANSACTION_ID}",'
        '"manifest_receipt":'
        + json.dumps(
            {
                "version": deferred_release_manifest.MANIFEST_VERSION,
                "sha256": (
                    deferred_release_manifest.deferred_release_manifest_sha256()
                ),
            },
            separators=(",", ":"),
        )
        + ',"steps":{}}\n'
    )
    path.write_text(payload)
    os.chmod(path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError, match="JSON|schema"):
        _driver(path).read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_fsyncs_temp_and_uses_no_clobber_publish(
    tmp_path,
    monkeypatch,
):
    import deferred_startup_file_driver as file_driver

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    events = []
    original_fsync = file_driver.os.fsync
    original_replace = file_driver.os.replace
    original_link = file_driver.os.link
    original_unlink = file_driver.os.unlink
    original_rename_noreplace = file_driver.DeferredStartupFileDriver._rename_noreplace

    def recording_fsync(descriptor):
        mode = os.fstat(descriptor).st_mode
        events.append("fsync-directory" if stat.S_ISDIR(mode) else "fsync-file")
        return original_fsync(descriptor)

    def recording_replace(*args, **kwargs):
        events.append("replace")
        return original_replace(*args, **kwargs)

    def recording_link(*args, **kwargs):
        events.append("link-no-clobber")
        return original_link(*args, **kwargs)

    def recording_unlink(*args, **kwargs):
        events.append("unlink")
        return original_unlink(*args, **kwargs)

    def recording_rename_noreplace(*args, **kwargs):
        events.append("rename-no-clobber")
        return original_rename_noreplace(*args, **kwargs)

    monkeypatch.setattr(file_driver.os, "fsync", recording_fsync)
    monkeypatch.setattr(file_driver.os, "replace", recording_replace)
    monkeypatch.setattr(file_driver.os, "link", recording_link)
    monkeypatch.setattr(file_driver.os, "unlink", recording_unlink)
    monkeypatch.setattr(
        file_driver.DeferredStartupFileDriver,
        "_rename_noreplace",
        staticmethod(recording_rename_noreplace),
    )

    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    assert "replace" not in events
    assert events[:6] == [
        "fsync-file",
        "link-no-clobber",
        "fsync-directory",
        "rename-no-clobber",
        "unlink",
        "fsync-directory",
    ]
    assert events[6:] == [
        "fsync-file",
        "rename-no-clobber",
        "fsync-directory",
        "link-no-clobber",
        "fsync-directory",
        "rename-no-clobber",
        "unlink",
        "fsync-directory",
        "rename-no-clobber",
        "unlink",
        "fsync-directory",
    ]


def test_file_driver_fails_closed_on_replacement_after_atomic_replace(
    tmp_path,
):
    from deferred_startup_file_driver import (
        AFTER_REPLACE,
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)

    def replace_committed_target(point):
        if point != AFTER_REPLACE:
            return
        attacker = path.with_name("attacker")
        attacker.write_text("{}")
        os.chmod(attacker, 0o600)
        os.replace(attacker, path)

    driver = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=replace_committed_target,
    )
    with pytest.raises(DeferredStartupFileDriverError, match="changed"):
        driver.record_intent(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_exposes_no_cleanup_or_delete_api(tmp_path):
    driver = _driver(_private_journal_path(tmp_path))

    assert not hasattr(driver, "cleanup")
    assert not hasattr(driver, "delete")


def test_file_driver_rejects_replaced_parent_identity(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    original_parent = path.parent.with_name("original-private")
    path.parent.rename(original_parent)
    path.parent.mkdir(mode=0o700)
    os.chmod(path.parent, 0o700)
    (path.parent / path.name).write_bytes((original_parent / path.name).read_bytes())
    os.chmod(path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError, match="parent.*changed"):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_rejects_rollback_to_earlier_valid_generation(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    earlier = path.read_bytes()
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )
    path.write_bytes(earlier)
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="generation|anchor|rollback",
    ):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_rejects_same_generation_with_different_content(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    forked = json.loads(path.read_bytes())
    forked["steps"]["plugins"] = {
        "attempts": [
            {
                "attempt": 1,
                "process_epoch": PROCESS_EPOCH + "-fork",
                "prior_completion_absent_policy": "deny",
                "intent": {"generation": 1},
            },
        ],
    }
    path.write_text(json.dumps(forked, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError, match="generation"):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_accepts_other_process_monotonic_generation_extension(
    tmp_path,
):
    path = _private_journal_path(tmp_path)
    original = _driver(path)
    process = multiprocessing.get_context("spawn").Process(
        target=_extend_journal_multiple_generations,
        args=(str(path),),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    assert original.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)
    assert original.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "plugins",
    ) == DeferredStartupStepState(attempt_number=1, intent=True)
    original.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "plugins",
        recovered=False,
    )
    assert json.loads(path.read_bytes())["generation"] == 4


def test_file_driver_reconstruction_binds_latest_generation(tmp_path):
    path = _private_journal_path(tmp_path)
    first = _driver(path)
    first.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    first.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=True,
    )

    reconstructed = _driver(path)

    assert reconstructed.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)
    assert json.loads(path.read_bytes())["generation"] == 2


def test_file_driver_serializes_threads_through_one_driver(tmp_path):
    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    step_names = tuple(f"thread_step_{index}" for index in range(8))

    def complete(step_name):
        driver.record_intent(TRANSACTION_ID, _receipt(), PROCESS_EPOCH, step_name)
        driver.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            step_name,
            recovered=False,
        )

    with ThreadPoolExecutor(max_workers=len(step_names)) as executor:
        tuple(executor.map(complete, step_names))

    assert json.loads(path.read_bytes())["generation"] == 16
    assert all(
        driver.read_step_state(TRANSACTION_ID, _receipt(), PROCESS_EPOCH, step_name)
        == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)
        for step_name in step_names
    )


@pytest.mark.parametrize(
    ("generation", "steps"),
    (
        (1, {}),
        (0, {"one": {"intent": True}}),
        (
            1,
            {
                "one": {
                    "intent": True,
                    "completion": {"recovered": False},
                }
            },
        ),
        (
            1,
            {
                "one": {
                    "intent": True,
                    "indeterminate": {"reason": "ambiguous"},
                }
            },
        ),
        (1, {"one": {"intent": True}, "two": {"intent": True}}),
        (99, {"one": {"intent": True}}),
    ),
)
def test_file_driver_rejects_impossible_initial_generation_topology(
    tmp_path,
    generation,
    steps,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    path.write_text(
        json.dumps(
            _journal_payload(generation=generation, steps=steps),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="generation.*topology|step record",
    ):
        _driver(path)


@pytest.mark.parametrize(
    ("generation_delta", "added_steps"),
    (
        (1, {}),
        (10, {}),
        (
            1,
            {
                "plugins": {
                    "intent": True,
                    "completion": {"recovered": False},
                }
            },
        ),
    ),
)
def test_file_driver_rejects_later_generation_jump_without_exact_state_count(
    tmp_path,
    generation_delta,
    added_steps,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    tampered = json.loads(path.read_bytes())
    tampered["generation"] += generation_delta
    tampered["steps"].update(added_steps)
    path.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="generation.*topology|step record",
    ):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_rejects_in_place_rewrite_during_parse(
    tmp_path,
    monkeypatch,
):
    import deferred_startup_file_driver as file_driver

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    original_loads = file_driver.json.loads

    def rewrite_then_parse(payload, *args, **kwargs):
        path.write_bytes(b"{}")
        os.chmod(path, 0o600)
        return original_loads(payload, *args, **kwargs)

    monkeypatch.setattr(file_driver.json, "loads", rewrite_then_parse)
    with pytest.raises(
        file_driver.DeferredStartupFileDriverError,
        match="changed during access|bytes changed",
    ):
        driver.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )


def test_file_driver_rejects_in_place_rewrite_before_replace(tmp_path):
    from deferred_startup_file_driver import (
        AFTER_TEMP_FSYNC,
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def rewrite_current(point):
        if point == AFTER_TEMP_FSYNC:
            path.write_bytes(b"{}")
            os.chmod(path, 0o600)

    racing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=rewrite_current,
    )
    with pytest.raises(
        DeferredStartupFileDriverError,
        match="changed during (update|displacement)",
    ):
        racing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )


def test_file_driver_rejects_old_snapshot_after_full_reconstruction(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID, _receipt(), PROCESS_EPOCH, "credential_permissions"
    )
    old_journal = path.read_bytes()
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )
    del driver
    path.write_bytes(old_journal)
    os.chmod(path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError, match="anchor|rollback"):
        _driver(path)


def test_file_driver_rejects_anchor_hash_mismatch(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    anchor_path = path.with_name(path.name + ".anchor")
    assert anchor_path.exists()
    anchor = json.loads(anchor_path.read_bytes())
    anchor["journal_sha256"] = "f" * 64
    anchor_path.write_bytes(_canonical_json_bytes(anchor) + b"\n")
    os.chmod(anchor_path, 0o600)

    with pytest.raises(DeferredStartupFileDriverError, match="anchor"):
        _driver(path)


@pytest.mark.parametrize("with_existing_generation", (False, True))
def test_file_driver_recovers_exact_journal_ahead_of_anchor(
    tmp_path,
    with_existing_generation,
):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    path = _private_journal_path(tmp_path)
    if with_existing_generation:
        _driver(path).record_intent(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )

    def crash_between_journal_and_anchor(point):
        if point == "after-journal-before-anchor":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_between_journal_and_anchor,
    )
    if with_existing_generation:
        transition = lambda: crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )
        expected = DeferredStartupStepState(
            attempt_number=1, intent=True, completion=True
        )
    else:
        transition = lambda: crashing.record_intent(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        expected = DeferredStartupStepState(attempt_number=1, intent=True)

    with pytest.raises(DeferredStartupCrash, match="after-journal-before-anchor"):
        transition()

    reconstructed = _driver(path)
    assert (
        reconstructed.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        == expected
    )
    anchor = json.loads(path.with_name(path.name + ".anchor").read_bytes())
    assert anchor["generation"] == (2 if with_existing_generation else 1)


def test_file_driver_real_crash_leaves_temp_then_reconstructs_and_cleans(
    tmp_path,
):
    path = _private_journal_path(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_exit_after_temp_fsync,
        args=(str(path),),
    )
    process.start()
    process.join(timeout=10)

    assert process.exitcode == 73
    temp_prefix = f".{path.name}."
    assert any(
        child.name.startswith(temp_prefix) and child.name.endswith(".tmp")
        for child in path.parent.iterdir()
    )
    reconstructed = _driver(path)
    assert (
        reconstructed.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        == DeferredStartupStepState()
    )
    assert not any(
        child.name.startswith(temp_prefix) and child.name.endswith(".tmp")
        for child in path.parent.iterdir()
    )


@pytest.mark.parametrize("failure", ("count", "size", "mode"))
def test_file_driver_bounds_and_validates_matching_orphan_temps(
    tmp_path,
    failure,
):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    count = 33 if failure == "count" else 1
    for index in range(count):
        orphan = path.parent / f".{path.name}.{index:032x}.tmp"
        orphan.write_bytes(b"x" * (129 if failure == "size" else 1))
        os.chmod(orphan, 0o644 if failure == "mode" else 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="temporary|orphan|recovery artifact",
    ):
        _driver(path, max_bytes=128)


def test_file_driver_writes_exact_canonical_journal_and_anchor_bytes(tmp_path):
    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    empty = _journal_payload(generation=0, steps={})
    journal = _journal_payload(
        generation=1,
        previous_sha256=_canonical_sha256(empty),
        steps={"credential_permissions": {"intent": True}},
    )
    expected_journal_bytes = _canonical_json_bytes(journal) + b"\n"
    expected_anchor = {
        "version": 2,
        "transaction_id": TRANSACTION_ID,
        "manifest_receipt": {
            "version": deferred_release_manifest.MANIFEST_VERSION,
            "sha256": deferred_release_manifest.deferred_release_manifest_sha256(),
        },
        "generation": 1,
        "journal_sha256": _canonical_sha256(journal),
    }

    assert path.read_bytes() == expected_journal_bytes
    assert path.with_name(path.name + ".anchor").read_bytes() == (
        _canonical_json_bytes(expected_anchor) + b"\n"
    )


def test_file_driver_never_overwrites_replacement_after_validation_before_displacement(
    tmp_path,
):
    from deferred_startup_file_driver import (
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    replacement_bytes = b"newer replacement must survive"

    def replace_after_validation(point):
        if point == "before-displacement":
            replacement = path.with_name("newer-replacement")
            replacement.write_bytes(replacement_bytes)
            os.chmod(replacement, 0o600)
            os.replace(replacement, path)

    racing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=replace_after_validation,
    )
    with pytest.raises(DeferredStartupFileDriverError, match="changed|publish"):
        racing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    assert path.read_bytes() == replacement_bytes


def test_file_driver_recovers_exact_old_state_after_crash_after_displacement(
    tmp_path,
):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash_after_displacement(point):
        if point == "after-displacement":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_after_displacement,
    )
    with pytest.raises(DeferredStartupCrash, match="after-displacement"):
        crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    assert _driver(path).read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True)


def test_file_driver_publish_is_no_clobber_and_preserves_intervening_target(tmp_path):
    from deferred_startup_file_driver import (
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    replacement_bytes = b"intervening target must survive"

    def install_intervening_target(point):
        if point == "before-publish":
            path.write_bytes(replacement_bytes)
            os.chmod(path, 0o600)

    racing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=install_intervening_target,
    )
    with pytest.raises(DeferredStartupFileDriverError, match="write|publish|changed"):
        racing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    assert path.read_bytes() == replacement_bytes


def test_file_driver_recovers_exact_new_state_after_crash_after_publish(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash_after_publish(point):
        if point == "after-publish":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_after_publish,
    )
    with pytest.raises(DeferredStartupCrash, match="after-publish"):
        crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    assert _driver(path).read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)


def test_orphan_cleanup_never_deletes_replacement_installed_after_open(
    tmp_path,
    monkeypatch,
):
    import deferred_startup_file_driver as file_driver

    path = _private_journal_path(tmp_path)
    orphan = path.parent / f".{path.name}.{'a' * 32}.tmp"
    orphan.write_bytes(b"original orphan")
    os.chmod(orphan, 0o600)
    replacement_bytes = b"unvalidated replacement"
    original_open = file_driver.os.open
    injected = False

    def open_then_replace(name, *args, **kwargs):
        nonlocal injected
        descriptor = original_open(name, *args, **kwargs)
        if str(name).endswith(".qtn") and not injected:
            injected = True
            replacement = path.parent / "cleanup-race-replacement"
            replacement.write_bytes(replacement_bytes)
            os.chmod(replacement, 0o600)
            os.replace(replacement, orphan)
        return descriptor

    monkeypatch.setattr(file_driver.os, "open", open_then_replace)

    _driver(path)

    assert orphan.read_bytes() == replacement_bytes


@pytest.mark.parametrize(
    ("crash_point", "expected_state"),
    (
        (
            "before-displacement",
            DeferredStartupStepState(attempt_number=1, intent=True),
        ),
        ("after-displacement", DeferredStartupStepState(attempt_number=1, intent=True)),
        ("before-publish", DeferredStartupStepState(attempt_number=1, intent=True)),
        (
            "after-publish",
            DeferredStartupStepState(attempt_number=1, intent=True, completion=True),
        ),
    ),
)
def test_file_driver_abrupt_publish_crash_recovers_exact_old_or_new_state(
    tmp_path,
    crash_point,
    expected_state,
):
    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    script = """
import os
import sys
from pathlib import Path
import deferred_release_manifest
from deferred_startup_file_driver import DeferredStartupFileDriver
from deferred_startup_replay import DeferredStartupManifestReceipt

transaction_id = sys.argv[2]
receipt = DeferredStartupManifestReceipt(
    transaction_id=transaction_id,
    version=deferred_release_manifest.MANIFEST_VERSION,
    sha256=deferred_release_manifest.deferred_release_manifest_sha256(),
)
crash_point = sys.argv[3]
process_epoch = sys.argv[4]
def crash(observed):
    if observed == crash_point:
        os._exit(74)
driver = DeferredStartupFileDriver(
    Path(sys.argv[1]),
    transaction_id=transaction_id,
    manifest_receipt=receipt,
    _crash_hook=crash,
)
driver.record_completion(
    transaction_id,
    receipt,
    process_epoch,
    "credential_permissions",
    recovered=False,
)
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            TRANSACTION_ID,
            crash_point,
            PROCESS_EPOCH,
        ],
        check=False,
    )

    assert process.returncode == 74
    reconstructed = _driver(path)
    assert (
        reconstructed.read_step_state(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
        )
        == expected_state
    )
    assert not any(
        child.name.endswith((".tmp", ".bak", ".qtn")) for child in path.parent.iterdir()
    )


def test_file_driver_rejects_two_link_backup_when_target_is_absent(tmp_path):
    from deferred_startup_file_driver import (
        DeferredStartupFileDriver,
        DeferredStartupFileDriverError,
    )

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash_after_displacement(point):
        if point == "after-displacement":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_after_displacement,
    )
    with pytest.raises(DeferredStartupCrash):
        crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    backup = next(path.parent.glob(f".{path.name}.*.bak"))
    os.link(backup, path.parent / "unrelated-hardlink")
    assert not path.exists()
    assert backup.stat().st_nlink == 2

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="two-link|ambiguous|unsafe",
    ):
        _driver(path)
    assert not path.exists()


def test_file_driver_attestation_receipt_has_exact_immutable_schema(tmp_path):
    from dataclasses import FrozenInstanceError

    from deferred_startup_file_driver import DeferredStartupFileAttestation

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    attestation = driver.attestation_receipt()
    journal = json.loads(path.read_bytes())
    anchor = json.loads(path.with_name(path.name + ".anchor").read_bytes())
    parent_status = path.parent.stat()
    expected = {
        "schema_version": 2,
        "transaction_id": TRANSACTION_ID,
        "manifest_receipt": {
            "version": deferred_release_manifest.MANIFEST_VERSION,
            "sha256": deferred_release_manifest.deferred_release_manifest_sha256(),
        },
        "parent_identity": {
            "device": parent_status.st_dev,
            "inode": parent_status.st_ino,
        },
        "journal": {
            "generation": 1,
            "sha256": _canonical_sha256(journal),
        },
        "anchor": {
            "generation": 1,
            "sha256": _canonical_sha256(anchor),
        },
        "attempt_topology": {
            "latest_process_epoch": PROCESS_EPOCH,
            "attempt_count": 1,
            "sha256": hashlib.sha256(
                _canonical_json_bytes(journal["steps"])
            ).hexdigest(),
        },
        "status": "stable-parent-consistent",
    }

    assert type(attestation) is DeferredStartupFileAttestation
    assert attestation.as_dict() == expected
    mutated_copy = attestation.as_dict()
    mutated_copy["journal"]["generation"] = 999
    assert attestation.as_dict() == expected
    with pytest.raises(FrozenInstanceError):
        attestation.status = "tampered"


def test_file_driver_attestation_advances_reconstructs_and_hashes_canonically(
    tmp_path,
):
    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    initial = driver.attestation_receipt()
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    intent = driver.attestation_receipt()
    driver.record_completion(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
        recovered=False,
    )
    completion = driver.attestation_receipt()

    assert [
        receipt.journal_generation for receipt in (initial, intent, completion)
    ] == [0, 1, 2]
    assert [receipt.anchor_generation for receipt in (initial, intent, completion)] == [
        0,
        1,
        2,
    ]
    assert (
        len({receipt.journal_sha256 for receipt in (initial, intent, completion)}) == 3
    )
    assert (
        len({receipt.anchor_sha256 for receipt in (initial, intent, completion)}) == 3
    )
    assert _driver(path).attestation_receipt() == completion
    canonical = _canonical_json_bytes(completion.as_dict())
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        hashlib.sha256(canonical).hexdigest(),
    )


def test_file_driver_attestation_rejects_parent_replacement(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    original_parent = path.parent.with_name("attestation-original-parent")
    path.parent.rename(original_parent)
    path.parent.mkdir(mode=0o700)
    os.chmod(path.parent, 0o700)

    with pytest.raises(DeferredStartupFileDriverError, match="parent.*changed"):
        driver.attestation_receipt()


def test_file_driver_attestation_rejects_inconsistent_anchor(tmp_path):
    from deferred_startup_file_driver import DeferredStartupFileDriverError

    path = _private_journal_path(tmp_path)
    driver = _driver(path)
    driver.record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )
    anchor_path = path.with_name(path.name + ".anchor")
    anchor = json.loads(anchor_path.read_bytes())
    anchor["journal_sha256"] = "f" * 64
    anchor_path.write_bytes(_canonical_json_bytes(anchor) + b"\n")
    os.chmod(anchor_path, 0o600)

    with pytest.raises(
        DeferredStartupFileDriverError,
        match="anchor|inconsistent|rollback|fork",
    ):
        driver.attestation_receipt()


def test_file_driver_reclassifies_interrupted_backup_quarantine_before_cleanup(
    tmp_path,
):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash_after_publish(point):
        if point == "after-publish":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_after_publish,
    )
    with pytest.raises(DeferredStartupCrash):
        crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    backup = next(path.parent.glob(f".{path.name}.*.bak"))
    operation_token = backup.name.split(".")[-2]
    quarantine = path.parent / (f".{path.name}.{operation_token}.bak.{'b' * 32}.qtn")
    backup.rename(quarantine)

    reconstructed = _driver(path)
    assert reconstructed.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True, completion=True)
    assert not quarantine.exists()


def test_file_driver_replays_repeated_crash_between_restore_link_and_backup_unlink(
    tmp_path,
):
    from deferred_startup_file_driver import DeferredStartupFileDriver

    path = _private_journal_path(tmp_path)
    _driver(path).record_intent(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    )

    def crash_after_displacement(point):
        if point == "after-displacement":
            raise DeferredStartupCrash(point)

    crashing = DeferredStartupFileDriver(
        path,
        transaction_id=TRANSACTION_ID,
        manifest_receipt=_receipt(),
        _crash_hook=crash_after_displacement,
    )
    with pytest.raises(DeferredStartupCrash):
        crashing.record_completion(
            TRANSACTION_ID,
            _receipt(),
            PROCESS_EPOCH,
            "credential_permissions",
            recovered=False,
        )

    script = """
import os
import sys
from pathlib import Path
import deferred_release_manifest
from deferred_startup_file_driver import DeferredStartupFileDriver
from deferred_startup_replay import DeferredStartupManifestReceipt

transaction_id = sys.argv[2]
receipt = DeferredStartupManifestReceipt(
    transaction_id=transaction_id,
    version=deferred_release_manifest.MANIFEST_VERSION,
    sha256=deferred_release_manifest.deferred_release_manifest_sha256(),
)
def crash(observed):
    if observed == "after-restore-link":
        os._exit(75)
DeferredStartupFileDriver(
    Path(sys.argv[1]),
    transaction_id=transaction_id,
    manifest_receipt=receipt,
    _crash_hook=crash,
)
"""
    for _attempt in range(2):
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(path),
                TRANSACTION_ID,
            ],
            check=False,
        )
        assert process.returncode == 75
        backup = next(path.parent.glob(f".{path.name}.*.bak"))
        assert path.stat().st_ino == backup.stat().st_ino
        assert path.stat().st_nlink == backup.stat().st_nlink == 2

    reconstructed = _driver(path)
    assert reconstructed.read_step_state(
        TRANSACTION_ID,
        _receipt(),
        PROCESS_EPOCH,
        "credential_permissions",
    ) == DeferredStartupStepState(attempt_number=1, intent=True)
    assert not any(
        child.name.endswith((".tmp", ".bak", ".qtn")) for child in path.parent.iterdir()
    )
