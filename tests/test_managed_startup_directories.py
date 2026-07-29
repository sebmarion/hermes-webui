from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from deferred_startup_replay import Reconciliation
from managed_startup_directories import (
    AFTER_CREATED_DIRECTORY_FSYNC,
    AFTER_MKDIR,
    AFTER_PARENT_FSYNC,
    ManagedStartupDirectoriesBindingError,
    ManagedStartupDirectoriesCrash,
    ManagedStartupDirectoriesError,
    ManagedStartupDirectoriesReceipt,
    ensure_managed_startup_directories,
    verify_managed_startup_directories,
)


def _desired(tmp_path: Path) -> tuple[str, ...]:
    return (
        str(tmp_path / "state" / "sessions"),
        str(tmp_path / "state" / "credentials"),
    )


@pytest.mark.parametrize(
    "desired",
    (
        [],
        (),
        ("relative/path",),
        ("/",),
        ("/tmp",),
        ("/private/tmp",),
        ("//private/tmp/state",),
        ("///private/tmp/state",),
        ("/tmp/../tmp/state",),
        ("/tmp/state/",),
        ("/tmp/state", "/tmp/state"),
        tuple(f"/private/tmp/state-{index}" for index in range(33)),
        ("/" + ("x" * 4096),),
        ("/" + "/".join("x" for _ in range(33)),),
    ),
)
def test_desired_directory_binding_is_exact_bounded_and_not_broad(desired):
    with pytest.raises(ManagedStartupDirectoriesBindingError):
        verify_managed_startup_directories(desired)


def test_ensure_creates_exact_directories_and_returns_immutable_evidence(tmp_path):
    desired = _desired(tmp_path)

    receipt = ensure_managed_startup_directories(desired)

    assert type(receipt) is ManagedStartupDirectoriesReceipt
    assert receipt.version == 1
    assert receipt.desired_directories == desired
    assert receipt.missing_directories == ()
    assert receipt.created_directories == (
        str(tmp_path / "state"),
        desired[0],
        desired[1],
    )
    assert tuple(evidence.path for evidence in receipt.evidence) == desired
    assert all(evidence.uid == os.getuid() for evidence in receipt.evidence)
    assert all(evidence.mode == 0o700 for evidence in receipt.evidence)
    assert all(stat.S_ISDIR(os.stat(path).st_mode) for path in desired)
    assert all(stat.S_IMODE(os.stat(path).st_mode) == 0o700 for path in desired)
    with pytest.raises(FrozenInstanceError):
        receipt.version = 2


def test_verifier_distinguishes_absent_partial_complete_and_ambiguous(tmp_path):
    desired = _desired(tmp_path)

    absent = verify_managed_startup_directories(desired)
    assert absent.outcome is Reconciliation.PROVED_ABSENT
    assert absent.receipt.evidence == ()
    assert absent.receipt.missing_directories == desired

    Path(desired[0]).mkdir(parents=True, mode=0o700)
    os.chmod(Path(desired[0]).parent, 0o700)
    os.chmod(desired[0], 0o700)
    partial = verify_managed_startup_directories(desired)
    assert partial.outcome is Reconciliation.PROVED_RETRY_SAFE_PARTIAL
    assert tuple(item.path for item in partial.receipt.evidence) == (desired[0],)
    assert partial.receipt.missing_directories == (desired[1],)

    Path(desired[1]).mkdir(mode=0o700)
    complete = verify_managed_startup_directories(desired)
    assert complete.outcome is Reconciliation.PROVED_COMPLETE
    assert complete.receipt.missing_directories == ()

    os.chmod(desired[1], 0o770)
    ambiguous = verify_managed_startup_directories(desired)
    assert ambiguous.outcome is Reconciliation.AMBIGUOUS
    assert ambiguous.receipt is None
    assert ambiguous.reason == "unsafe-directory"


def test_final_desired_directory_rejects_group_or_world_permissions(tmp_path):
    desired = _desired(tmp_path)
    target = Path(desired[0])
    target.mkdir(parents=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    os.chmod(target, 0o755)

    verification = verify_managed_startup_directories(desired)

    assert verification.outcome is Reconciliation.AMBIGUOUS
    with pytest.raises(ManagedStartupDirectoriesError, match="mode|unsafe"):
        ensure_managed_startup_directories(desired)


@pytest.mark.parametrize("mode", (0o000, 0o100, 0o500, 0o600))
def test_owner_only_restrictive_partial_is_repaired_to_exact_mode(tmp_path, mode):
    desired = _desired(tmp_path)
    target = Path(desired[0])
    target.mkdir(parents=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    os.chmod(target, mode)

    try:
        verification = verify_managed_startup_directories(desired)

        assert verification.outcome is Reconciliation.PROVED_RETRY_SAFE_PARTIAL
        receipt = ensure_managed_startup_directories(desired)
        assert receipt.missing_directories == ()
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
    finally:
        if target.exists():
            os.chmod(target, 0o700)


def test_retry_repairs_hostile_umask_partial_created_before_chmod(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    partial = Path(tmp_path / "state")
    real_mkdir = module.os.mkdir

    def hostile_umask_mkdir(path, mode=0o777, *, dir_fd=None):
        result = real_mkdir(path, mode=mode, dir_fd=dir_fd)
        module.os.chmod(path, 0o000, dir_fd=dir_fd, follow_symlinks=False)
        return result

    monkeypatch.setattr(module.os, "mkdir", hostile_umask_mkdir)

    def crash_after_partial_mkdir(point, path):
        if point == AFTER_MKDIR and path == str(partial):
            raise ManagedStartupDirectoriesCrash(point)

    with pytest.raises(ManagedStartupDirectoriesCrash, match=AFTER_MKDIR):
        ensure_managed_startup_directories(
            desired,
            crash_hook=crash_after_partial_mkdir,
        )

    try:
        assert stat.S_IMODE(os.stat(partial).st_mode) == 0o000
        assert (
            verify_managed_startup_directories(desired).outcome
            is Reconciliation.PROVED_RETRY_SAFE_PARTIAL
        )

        monkeypatch.setattr(module.os, "mkdir", real_mkdir)
        receipt = ensure_managed_startup_directories(desired)

        assert receipt.missing_directories == ()
        assert stat.S_IMODE(os.stat(partial).st_mode) == 0o700
    finally:
        if partial.exists():
            os.chmod(partial, 0o700)


def test_absent_outcome_rejects_ancestor_replacement_before_early_return(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = (str(tmp_path / "state" / "sessions"),)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    real_stat_child = module._stat_child
    replaced = False

    def replace_ancestor_on_missing(parent_fd, component):
        nonlocal replaced
        try:
            return real_stat_child(parent_fd, component)
        except FileNotFoundError:
            if component == "sessions" and not replaced:
                replaced = True
                state.rename(tmp_path / "state-displaced")
                state.mkdir(mode=0o700)
            raise

    monkeypatch.setattr(module, "_stat_child", replace_ancestor_on_missing)

    result = verify_managed_startup_directories(desired)

    assert result.outcome is Reconciliation.AMBIGUOUS
    assert result.reason == "unsafe-directory"


def test_retry_safe_partial_rejects_ancestor_replacement_before_early_exit(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = (str(tmp_path / "state" / "sessions"),)
    state = tmp_path / "state"
    target = state / "sessions"
    target.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    target.chmod(0o500)
    real_stat_child = module._stat_child
    replaced = False

    def replace_ancestor_after_restrictive_stat(parent_fd, component):
        nonlocal replaced
        result = real_stat_child(parent_fd, component)
        if component == "sessions" and not replaced:
            replaced = True
            state.rename(tmp_path / "state-displaced")
            state.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(
        module,
        "_stat_child",
        replace_ancestor_after_restrictive_stat,
    )

    try:
        result = verify_managed_startup_directories(desired)
        assert result.outcome is Reconciliation.AMBIGUOUS
        assert result.reason == "unsafe-directory"
    finally:
        displaced_target = tmp_path / "state-displaced" / "sessions"
        if displaced_target.exists():
            displaced_target.chmod(0o700)


def test_created_directories_are_fchmoded_before_durable_completion(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    real_fchmod = module.os.fchmod
    modes = []

    def record_fchmod(fd, mode):
        modes.append(mode)
        return real_fchmod(fd, mode)

    monkeypatch.setattr(module.os, "fchmod", record_fchmod)

    ensure_managed_startup_directories(desired)

    assert modes == [0o700, 0o700, 0o700]


@pytest.mark.parametrize("unsafe_kind", ("symlink", "file"))
def test_symlink_and_non_directory_fail_closed(tmp_path, unsafe_kind):
    desired = _desired(tmp_path)
    target = Path(desired[0])
    target.parent.mkdir(mode=0o700)
    if unsafe_kind == "symlink":
        target.symlink_to(tmp_path)
    else:
        target.write_text("not a directory")

    verification = verify_managed_startup_directories(desired)

    assert verification.outcome is Reconciliation.AMBIGUOUS
    assert verification.reason == "unsafe-directory"
    with pytest.raises(
        ManagedStartupDirectoriesError,
        match="unsafe|cannot be opened",
    ):
        ensure_managed_startup_directories(desired)


def test_foreign_owned_desired_directory_fails_closed(tmp_path, monkeypatch):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    ensure_managed_startup_directories(desired)
    target_inode = os.stat(desired[0]).st_ino
    real_fstat = module.os.fstat

    def foreign_target(fd):
        opened = real_fstat(fd)
        if opened.st_ino == target_inode:
            return replace(
                module._DirectoryStat.from_os_stat(opened),
                uid=os.getuid() + 1,
            )
        return opened

    monkeypatch.setattr(module.os, "fstat", foreign_target)

    assert (
        verify_managed_startup_directories(desired).outcome is Reconciliation.AMBIGUOUS
    )
    with pytest.raises(ManagedStartupDirectoriesError, match="owner|unsafe"):
        ensure_managed_startup_directories(desired)


@pytest.mark.parametrize(
    "boundary",
    (AFTER_MKDIR, AFTER_CREATED_DIRECTORY_FSYNC, AFTER_PARENT_FSYNC),
)
def test_crash_after_each_durability_boundary_converges_on_restart(
    tmp_path,
    boundary,
):
    desired = _desired(tmp_path)
    crashed = []

    def crash_once(point, path):
        if point == boundary and not crashed:
            crashed.append(path)
            raise ManagedStartupDirectoriesCrash(point)

    with pytest.raises(ManagedStartupDirectoriesCrash, match=boundary):
        ensure_managed_startup_directories(desired, crash_hook=crash_once)

    receipt = ensure_managed_startup_directories(desired)

    assert receipt.missing_directories == ()
    assert (
        verify_managed_startup_directories(desired).outcome
        is Reconciliation.PROVED_COMPLETE
    )


@pytest.mark.parametrize(
    "crash_boundary",
    (AFTER_MKDIR, AFTER_CREATED_DIRECTORY_FSYNC),
)
def test_retry_fsyncs_preexisting_partial_child_and_parent_before_complete(
    tmp_path,
    crash_boundary,
):
    desired = _desired(tmp_path)
    partial = str(tmp_path / "state")

    def crash_on_partial(point, path):
        if point == crash_boundary and path == partial:
            raise ManagedStartupDirectoriesCrash(point)

    with pytest.raises(ManagedStartupDirectoriesCrash, match=crash_boundary):
        ensure_managed_startup_directories(desired, crash_hook=crash_on_partial)

    retry_events = []
    receipt = ensure_managed_startup_directories(
        desired,
        crash_hook=lambda point, path: retry_events.append((point, path)),
    )

    partial_events = [point for point, path in retry_events if path == partial]
    assert partial_events[:2] == [
        AFTER_CREATED_DIRECTORY_FSYNC,
        AFTER_PARENT_FSYNC,
    ]
    assert receipt.missing_directories == ()


def test_retry_after_fsync_error_replays_child_then_parent_durability(tmp_path):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    partial = str(tmp_path / "state")
    real_fsync = module.os.fsync
    failed = False

    def fail_once(fd):
        nonlocal failed
        if Path(partial).exists() and not failed:
            failed = True
            raise OSError("synthetic first fsync failure")
        return real_fsync(fd)

    module.os.fsync = fail_once
    try:
        with pytest.raises(ManagedStartupDirectoriesError, match="fsync|durable"):
            ensure_managed_startup_directories(desired)
    finally:
        module.os.fsync = real_fsync

    retry_events = []
    receipt = ensure_managed_startup_directories(
        desired,
        crash_hook=lambda point, path: retry_events.append((point, path)),
    )

    partial_events = [point for point, path in retry_events if path == partial]
    assert partial_events[:2] == [
        AFTER_CREATED_DIRECTORY_FSYNC,
        AFTER_PARENT_FSYNC,
    ]
    assert receipt.missing_directories == ()


def test_replacement_race_after_mkdir_is_rejected(tmp_path):
    desired = _desired(tmp_path)
    raced = []

    def replace_with_symlink(point, path):
        if point == AFTER_MKDIR and not raced:
            raced.append(path)
            created = Path(path)
            created.rmdir()
            created.symlink_to(tmp_path)

    with pytest.raises(ManagedStartupDirectoriesError, match="unsafe|changed"):
        ensure_managed_startup_directories(
            desired,
            crash_hook=replace_with_symlink,
        )


def test_stable_snapshot_rejects_replacement_after_final_fsync(tmp_path):
    desired = (str(tmp_path / "state" / "sessions"),)
    replaced = []

    def replace_after_fsync(point, path):
        if point == AFTER_PARENT_FSYNC and path == desired[0] and not replaced:
            replaced.append(path)
            final = Path(path)
            displaced = final.with_name("sessions-displaced")
            final.rename(displaced)
            final.mkdir(mode=0o700)

    with pytest.raises(ManagedStartupDirectoriesError, match="changed|snapshot"):
        ensure_managed_startup_directories(
            desired,
            crash_hook=replace_after_fsync,
        )


def test_parent_entry_replacement_after_child_open_is_rejected(tmp_path):
    desired = (str(tmp_path / "state" / "sessions"),)
    replaced = []

    def replace_after_child_open(point, path):
        if (
            point == AFTER_CREATED_DIRECTORY_FSYNC
            and path == desired[0]
            and not replaced
        ):
            replaced.append(path)
            final = Path(path)
            final.rename(final.with_name("sessions-displaced"))
            final.mkdir(mode=0o700)

    with pytest.raises(ManagedStartupDirectoriesError, match="identity|changed"):
        ensure_managed_startup_directories(
            desired,
            crash_hook=replace_after_child_open,
        )


def test_ancestor_replacement_after_deeper_child_open_is_rejected(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = (str(tmp_path / "state" / "sessions"),)
    state = tmp_path / "state"
    replaced = []

    def replace_ancestor_after_deeper_open(point, path):
        if point == AFTER_PARENT_FSYNC and path == desired[0] and not replaced:
            replaced.append(path)
            state.rename(tmp_path / "state-displaced")
            state.mkdir(mode=0o700)

    def stable_verifier_must_not_be_needed(_desired):
        pytest.fail("ancestor replacement escaped the held-chain recheck")

    monkeypatch.setattr(
        module,
        "_verify_validated",
        stable_verifier_must_not_be_needed,
    )

    with pytest.raises(ManagedStartupDirectoriesError, match="identity|changed"):
        ensure_managed_startup_directories(
            desired,
            crash_hook=replace_ancestor_after_deeper_open,
        )


def test_fsync_error_closes_all_owned_file_descriptors(tmp_path, monkeypatch):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    before = len(os.listdir("/dev/fd"))
    calls = []

    def fail_fsync(_fd):
        calls.append("fsync")
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)

    with pytest.raises(ManagedStartupDirectoriesError, match="fsync|durable"):
        ensure_managed_startup_directories(desired)

    assert calls == ["fsync"]
    assert len(os.listdir("/dev/fd")) == before


def test_component_open_error_is_ambiguous_and_leaks_no_descriptors(
    tmp_path,
    monkeypatch,
):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    Path(tmp_path / "state").mkdir(mode=0o700)
    real_open = module.os.open
    before = len(os.listdir("/dev/fd"))

    def fail_state_open(path, flags, *args, **kwargs):
        if path == "state":
            raise PermissionError("synthetic component denial")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail_state_open)

    verification = verify_managed_startup_directories(desired)
    assert verification.outcome is Reconciliation.AMBIGUOUS
    assert verification.reason == "unsafe-directory"
    with pytest.raises(
        ManagedStartupDirectoriesError,
        match="unsafe|cannot be opened",
    ):
        ensure_managed_startup_directories(desired)
    assert len(os.listdir("/dev/fd")) == before


def test_missing_nofollow_support_fails_closed(tmp_path, monkeypatch):
    import managed_startup_directories as module

    desired = _desired(tmp_path)
    monkeypatch.setattr(module, "_NOFOLLOW", 0)

    verification = verify_managed_startup_directories(desired)
    assert verification.outcome is Reconciliation.AMBIGUOUS
    with pytest.raises(ManagedStartupDirectoriesError, match="unavailable"):
        ensure_managed_startup_directories(desired)
