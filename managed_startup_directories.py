"""Strict, restart-safe reconciliation for managed startup state directories."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from typing import Callable

from deferred_startup_replay import Reconciliation


MAX_DESIRED_DIRECTORIES = 32
MAX_DIRECTORY_DEPTH = 32
MAX_DIRECTORY_PATH_BYTES = 4096
MIN_DIRECTORY_DEPTH = 3

AFTER_MKDIR = "after-mkdir"
AFTER_CREATED_DIRECTORY_FSYNC = "after-created-directory-fsync"
AFTER_PARENT_FSYNC = "after-parent-fsync"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CHMOD_DIR_FD = os.chmod in os.supports_dir_fd
_CHMOD_NOFOLLOW = os.chmod in os.supports_follow_symlinks


class ManagedStartupDirectoriesError(RuntimeError):
    """Managed startup directories could not be reconciled safely."""


class ManagedStartupDirectoriesBindingError(ManagedStartupDirectoriesError):
    """The exact desired directory tuple is invalid."""


class ManagedStartupDirectoriesCrash(BaseException):
    """Synthetic crash used by deterministic durability-boundary tests."""


class _UnsafeDirectory(ManagedStartupDirectoriesError):
    pass


class _RetrySafePartial(ManagedStartupDirectoriesError):
    pass


@dataclass(frozen=True, slots=True)
class _DirectoryStat:
    mode_raw: int
    device: int
    inode: int
    uid: int

    @classmethod
    def from_os_stat(cls, value: object) -> _DirectoryStat:
        if type(value) is cls:
            return value
        return cls(
            mode_raw=value.st_mode,
            device=value.st_dev,
            inode=value.st_ino,
            uid=value.st_uid,
        )


@dataclass(frozen=True, slots=True)
class ManagedStartupDirectoryEvidence:
    path: str
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class ManagedStartupDirectoriesReceipt:
    version: int
    desired_directories: tuple[str, ...]
    evidence: tuple[ManagedStartupDirectoryEvidence, ...]
    missing_directories: tuple[str, ...]
    created_directories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedStartupDirectoriesVerification:
    outcome: Reconciliation
    receipt: ManagedStartupDirectoriesReceipt | None
    reason: str


CrashHook = Callable[[str, str], None]


def _validate_desired_directories(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > MAX_DESIRED_DIRECTORIES:
        raise ManagedStartupDirectoriesBindingError(
            "managed startup desired directories are invalid"
        )
    validated: list[str] = []
    seen: set[str] = set()
    for path in value:
        if (
            type(path) is not str
            or not path
            or "\x00" in path
            or not os.path.isabs(path)
            or path.startswith("//")
            or os.path.normpath(path) != path
            or path.endswith("/")
            or len(os.fsencode(path)) > MAX_DIRECTORY_PATH_BYTES
        ):
            raise ManagedStartupDirectoriesBindingError(
                "managed startup desired directory is invalid"
            )
        components = tuple(component for component in path.split("/") if component)
        if (
            len(components) < MIN_DIRECTORY_DEPTH
            or len(components) > MAX_DIRECTORY_DEPTH
            or any(component in {".", ".."} for component in components)
            or path in seen
        ):
            raise ManagedStartupDirectoriesBindingError(
                "managed startup desired directory is broad or invalid"
            )
        seen.add(path)
        validated.append(path)
    return tuple(validated)


def _validate_crash_hook(value: object) -> CrashHook | None:
    if value is not None and not callable(value):
        raise ManagedStartupDirectoriesBindingError(
            "managed startup directory crash hook is invalid"
        )
    return value


def _directory_stat(fd: int) -> _DirectoryStat:
    try:
        return _DirectoryStat.from_os_stat(os.fstat(fd))
    except OSError as exc:
        raise _UnsafeDirectory("managed startup directory is unsafe") from exc


def _require_secure_directory_flags() -> None:
    if _NOFOLLOW == 0 or _DIRECTORY == 0:
        raise ManagedStartupDirectoriesError(
            "secure managed startup directory traversal is unavailable"
        )


def _validate_opened_directory(
    fd: int,
    *,
    final: bool,
    created: bool,
) -> _DirectoryStat:
    opened = _directory_stat(fd)
    mode = stat.S_IMODE(opened.mode_raw)
    if not stat.S_ISDIR(opened.mode_raw):
        raise _UnsafeDirectory("managed startup path is not a directory")
    current_uid = os.getuid()
    if final or created:
        if opened.uid != current_uid or mode != 0o700:
            raise _UnsafeDirectory(
                "managed startup directory has an unsafe owner or mode"
            )
    else:
        root_owned_sticky = (
            opened.uid == 0
            and bool(opened.mode_raw & stat.S_ISVTX)
            and bool(mode & 0o002)
        )
        if opened.uid not in {0, current_uid} or (
            mode & 0o022 and not root_owned_sticky
        ):
            raise _UnsafeDirectory(
                "managed startup directory ancestor has an unsafe owner or mode"
            )
    return opened


def _evidence(path: str, opened: _DirectoryStat) -> ManagedStartupDirectoryEvidence:
    return ManagedStartupDirectoryEvidence(
        path=path,
        device=opened.device,
        inode=opened.inode,
        uid=opened.uid,
        mode=stat.S_IMODE(opened.mode_raw),
    )


def _same_directory_identity(left: object, right: object) -> bool:
    left_stat = _DirectoryStat.from_os_stat(left)
    right_stat = _DirectoryStat.from_os_stat(right)
    return (left_stat.device, left_stat.inode) == (
        right_stat.device,
        right_stat.inode,
    )


def _stat_child(parent_fd: int, component: str) -> os.stat_result:
    try:
        return os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _UnsafeDirectory(
            "managed startup directory entry cannot be inspected"
        ) from exc


def _is_retry_safe_restrictive_directory(value: os.stat_result) -> bool:
    mode = stat.S_IMODE(value.st_mode)
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.getuid()
        and mode != 0o700
        and mode & ~0o700 == 0
    )


def _repair_retry_safe_directory(
    parent_fd: int,
    component: str,
    entry_before: os.stat_result,
) -> None:
    if not (_CHMOD_DIR_FD and _CHMOD_NOFOLLOW):
        raise ManagedStartupDirectoriesError(
            "secure managed startup directory chmod is unavailable"
        )
    if not _is_retry_safe_restrictive_directory(entry_before):
        raise _UnsafeDirectory("managed startup directory is not a retry-safe partial")
    try:
        os.chmod(
            component,
            0o700,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        entry_after = _stat_child(parent_fd, component)
    except ManagedStartupDirectoriesError:
        raise
    except OSError as exc:
        raise ManagedStartupDirectoriesError(
            "managed startup directory mode could not be secured"
        ) from exc
    if (
        not _same_directory_identity(entry_before, entry_after)
        or not stat.S_ISDIR(entry_after.st_mode)
        or entry_after.st_uid != os.getuid()
        or stat.S_IMODE(entry_after.st_mode) != 0o700
    ):
        raise _UnsafeDirectory(
            "managed startup directory changed while securing its mode"
        )


def _open_child(parent_fd: int, component: str) -> int:
    child_fd = -1
    try:
        entry_before = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        child_fd = os.open(
            component,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            dir_fd=parent_fd,
        )
        opened = os.fstat(child_fd)
        if not _same_directory_identity(entry_before, opened):
            raise _UnsafeDirectory(
                "managed startup directory identity changed while opening"
            )
        result = child_fd
        child_fd = -1
        return result
    except FileNotFoundError:
        raise
    except ManagedStartupDirectoriesError:
        raise
    except OSError as exc:
        if exc.errno in {
            errno.EACCES,
            errno.ELOOP,
            errno.ENOTDIR,
            errno.EPERM,
        }:
            raise _UnsafeDirectory("managed startup directory is unsafe") from exc
        raise ManagedStartupDirectoriesError(
            "managed startup directory cannot be opened"
        ) from exc
    finally:
        if child_fd >= 0:
            os.close(child_fd)


def _recheck_parent_entry(
    parent_fd: int,
    component: str,
    opened: _DirectoryStat,
) -> None:
    try:
        entry_after = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise _UnsafeDirectory(
            "managed startup directory parent entry changed"
        ) from exc
    rebound = _DirectoryStat.from_os_stat(entry_after)
    if rebound != opened:
        raise _UnsafeDirectory(
            "managed startup directory parent entry identity or metadata changed"
        )


def _recheck_unopened_parent_entry(
    parent_fd: int,
    component: str,
    expected: os.stat_result,
) -> None:
    rebound = _stat_child(parent_fd, component)
    if _DirectoryStat.from_os_stat(rebound) != _DirectoryStat.from_os_stat(expected):
        raise _UnsafeDirectory("managed startup directory unopened entry changed")


def _recheck_retained_chain(
    bindings: list[tuple[int, str, int, _DirectoryStat]],
) -> None:
    for parent_fd, component, child_fd, opened in bindings:
        rebound = _directory_stat(child_fd)
        if rebound != opened:
            raise _UnsafeDirectory("managed startup directory changed while held open")
        _recheck_parent_entry(
            parent_fd,
            component,
            rebound,
        )


def _fsync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ManagedStartupDirectoriesError(
            "managed startup directory durability fsync failed"
        ) from exc


def _inject_crash(crash_hook: CrashHook | None, boundary: str, path: str) -> None:
    if crash_hook is not None:
        crash_hook(boundary, path)


def _walk_directory(
    path: str,
    *,
    create: bool,
    created_directories: list[str] | None = None,
    crash_hook: CrashHook | None = None,
) -> ManagedStartupDirectoryEvidence | None:
    _require_secure_directory_flags()
    components = tuple(component for component in path.split("/") if component)
    opened_fds: list[int] = []
    bindings: list[tuple[int, str, int, _DirectoryStat]] = []
    current_path = ""
    try:
        try:
            root_fd = os.open(
                "/",
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            )
        except OSError as exc:
            raise ManagedStartupDirectoriesError(
                "managed startup directory root cannot be opened"
            ) from exc
        opened_fds.append(root_fd)
        _validate_opened_directory(root_fd, final=False, created=False)
        for index, component in enumerate(components):
            parent_fd = opened_fds[-1]
            current_path += "/" + component
            final = index == len(components) - 1
            made = False
            try:
                entry_before = _stat_child(parent_fd, component)
            except FileNotFoundError:
                if not create:
                    _recheck_retained_chain(bindings)
                    return None
                if index + 1 < MIN_DIRECTORY_DEPTH:
                    raise _UnsafeDirectory(
                        "managed startup directory creation target is too broad"
                    ) from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    made = True
                    if (
                        created_directories is not None
                        and current_path not in created_directories
                    ):
                        created_directories.append(current_path)
                    _inject_crash(crash_hook, AFTER_MKDIR, current_path)
                except FileExistsError:
                    made = False
                except OSError as exc:
                    raise ManagedStartupDirectoriesError(
                        "managed startup directory cannot be created"
                    ) from exc
                entry_before = _stat_child(parent_fd, component)
            if (
                index + 1 >= MIN_DIRECTORY_DEPTH
                and _is_retry_safe_restrictive_directory(entry_before)
            ):
                if not create:
                    _recheck_unopened_parent_entry(
                        parent_fd,
                        component,
                        entry_before,
                    )
                    _recheck_retained_chain(bindings)
                    raise _RetrySafePartial(
                        "managed startup directory is a retry-safe partial"
                    )
                _repair_retry_safe_directory(
                    parent_fd,
                    component,
                    entry_before,
                )
            child_fd = _open_child(parent_fd, component)
            opened_fds.append(child_fd)
            try:
                if made:
                    try:
                        os.fchmod(child_fd, 0o700)
                    except OSError as exc:
                        raise ManagedStartupDirectoriesError(
                            "managed startup directory mode could not be secured"
                        ) from exc
                opened = _validate_opened_directory(
                    child_fd,
                    final=final,
                    created=made,
                )
                bindings.append((parent_fd, component, child_fd, opened))
                mode = stat.S_IMODE(opened.mode_raw)
                should_sync = made or (
                    create
                    and index + 1 >= MIN_DIRECTORY_DEPTH
                    and opened.uid == os.getuid()
                    and mode == 0o700
                )
                if should_sync:
                    _fsync_directory(child_fd)
                    _inject_crash(
                        crash_hook,
                        AFTER_CREATED_DIRECTORY_FSYNC,
                        current_path,
                    )
                    _fsync_directory(parent_fd)
                    _inject_crash(crash_hook, AFTER_PARENT_FSYNC, current_path)
                evidence = _evidence(current_path, opened) if final else None
            except BaseException:
                raise
        _recheck_retained_chain(bindings)
        return evidence
    finally:
        for opened_fd in reversed(opened_fds):
            os.close(opened_fd)


def _verify_validated(
    desired_directories: tuple[str, ...],
) -> ManagedStartupDirectoriesVerification:
    evidence: list[ManagedStartupDirectoryEvidence] = []
    missing: list[str] = []
    retry_safe_partial = False
    try:
        for path in desired_directories:
            try:
                observed = _walk_directory(path, create=False)
            except _RetrySafePartial:
                retry_safe_partial = True
                missing.append(path)
                continue
            if observed is None:
                missing.append(path)
            else:
                evidence.append(observed)
    except ManagedStartupDirectoriesError:
        return ManagedStartupDirectoriesVerification(
            outcome=Reconciliation.AMBIGUOUS,
            receipt=None,
            reason="unsafe-directory",
        )
    receipt = ManagedStartupDirectoriesReceipt(
        version=1,
        desired_directories=desired_directories,
        evidence=tuple(evidence),
        missing_directories=tuple(missing),
        created_directories=(),
    )
    if retry_safe_partial:
        outcome = Reconciliation.PROVED_RETRY_SAFE_PARTIAL
    elif not evidence:
        outcome = Reconciliation.PROVED_ABSENT
    elif missing:
        outcome = Reconciliation.PROVED_RETRY_SAFE_PARTIAL
    else:
        outcome = Reconciliation.PROVED_COMPLETE
    return ManagedStartupDirectoriesVerification(
        outcome=outcome,
        receipt=receipt,
        reason=outcome.value,
    )


def verify_managed_startup_directories(
    desired_directories: tuple[str, ...],
) -> ManagedStartupDirectoriesVerification:
    """Inspect the exact desired tuple without mutating the filesystem."""

    desired = _validate_desired_directories(desired_directories)
    return _verify_validated(desired)


def ensure_managed_startup_directories(
    desired_directories: tuple[str, ...],
    *,
    crash_hook: CrashHook | None = None,
) -> ManagedStartupDirectoriesReceipt:
    """Create missing safe components and return a stable evidence receipt."""

    desired = _validate_desired_directories(desired_directories)
    crash_hook = _validate_crash_hook(crash_hook)
    created: list[str] = []
    initial_evidence: list[ManagedStartupDirectoryEvidence] = []
    try:
        for path in desired:
            observed = _walk_directory(
                path,
                create=True,
                created_directories=created,
                crash_hook=crash_hook,
            )
            if observed is None:
                raise ManagedStartupDirectoriesError(
                    "managed startup directory creation did not converge"
                )
            initial_evidence.append(observed)
    except _UnsafeDirectory:
        raise
    stable = _verify_validated(desired)
    if (
        stable.outcome is not Reconciliation.PROVED_COMPLETE
        or stable.receipt is None
        or stable.receipt.evidence != tuple(initial_evidence)
    ):
        raise ManagedStartupDirectoriesError(
            "managed startup directory stable snapshot changed"
        )
    return ManagedStartupDirectoriesReceipt(
        version=1,
        desired_directories=desired,
        evidence=stable.receipt.evidence,
        missing_directories=(),
        created_directories=tuple(created),
    )
