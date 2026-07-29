"""Strict, process-epoch-bound reconciliation of startup profile state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType

from api.process_identity import process_start_token

_MAX_PROFILE_ENV_BYTES = 1_048_576


class ManagedStartupProfileError(RuntimeError):
    """Base error for strict startup profile reconciliation."""


class ManagedStartupProfileAdmissionError(ManagedStartupProfileError):
    """Profile mutation was attempted outside admitted startup."""


class ManagedStartupProfileUnavailable(ManagedStartupProfileError):
    """The desired profile or process identity could not be proved."""


class ManagedStartupProfilePostconditionError(ManagedStartupProfileError):
    """Profile mutation completed without an exact postcondition."""


class ManagedStartupProfileVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ProcessEpoch:
    pid: int
    start_token: str


@dataclass(frozen=True)
class ManagedStartupProfileReceipt:
    process_epoch: ProcessEpoch
    desired_sha256: str
    profile_name: str
    hermes_home: str
    hermes_home_device: int
    hermes_home_inode: int
    hermes_home_mode: int
    isolated: bool
    env_keys: tuple[str, ...]


@dataclass(frozen=True)
class ManagedStartupProfileVerification:
    outcome: ManagedStartupProfileVerificationOutcome
    receipt: ManagedStartupProfileReceipt | None
    reason: str | None


@dataclass(frozen=True)
class _DesiredProfileSnapshot:
    process_epoch: ProcessEpoch
    profile_name: str
    hermes_home: Path
    hermes_home_device: int
    hermes_home_inode: int
    hermes_home_mode: int
    isolated: bool
    env_values: tuple[tuple[str, str], ...] = field(repr=False)
    prior_managed_env_keys: tuple[str, ...]
    protected_env: tuple[tuple[str, bool, str], ...] = field(repr=False)
    desired_sha256: str


@dataclass(frozen=True)
class _ManagedProfileState:
    desired: _DesiredProfileSnapshot
    receipt: ManagedStartupProfileReceipt


_STATE_LOCK = threading.Lock()
_STATE: _ManagedProfileState | None = None
# Contract: every snapshot, apply, repair, and verification is serialized by
# this lock. Code outside this reconciler cannot participate in that protocol;
# its environment mutations are therefore treated as tamper and detected by
# the exact postcondition checks rather than silently incorporated.


@dataclass(frozen=True)
class _HeldProfileHome:
    descriptors: tuple[int, ...] = field(repr=False)
    value: os.stat_result

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]


def _current_process_epoch() -> ProcessEpoch | None:
    pid = os.getpid()
    token = process_start_token(pid)
    if not token:
        return None
    return ProcessEpoch(pid, token)


def _startup_mutations_are_admitted() -> bool:
    try:
        from api import config
    except ImportError:
        return False
    return bool(config._startup_mutations_are_admitted())


def _load_profiles_module() -> ModuleType:
    try:
        from api import profiles
    except ImportError as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile module is unavailable"
        ) from exc
    return profiles


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_owner_private_regular(value: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise ManagedStartupProfileUnavailable(f"{label} is not a regular file")
    if value.st_uid != os.getuid():
        raise ManagedStartupProfileUnavailable(f"{label} has an unexpected owner")
    if value.st_nlink != 1:
        raise ManagedStartupProfileUnavailable(f"{label} has multiple links")
    if stat.S_IMODE(value.st_mode) & 0o077:
        raise ManagedStartupProfileUnavailable(f"{label} is not owner-private")


def _rebind_profile_chain(
    descriptors: tuple[int, ...] | list[int],
    components: tuple[str, ...],
) -> None:
    root_entry = os.stat("/", follow_symlinks=False)
    if _stat_identity(root_entry) != _stat_identity(os.fstat(descriptors[0])):
        raise ManagedStartupProfileUnavailable(
            "startup profile root identity changed"
        )
    for index, component in enumerate(components):
        entry = os.stat(
            component,
            dir_fd=descriptors[index],
            follow_symlinks=False,
        )
        child = os.fstat(descriptors[index + 1])
        if _stat_identity(entry) != _stat_identity(child):
            raise ManagedStartupProfileUnavailable(
                "startup profile path component became detached"
            )


@contextmanager
def _open_profile_home(home_value: Path | str):
    raw_home = os.fspath(home_value)
    home = Path(raw_home)
    canonical = Path(os.path.normpath(raw_home))
    if (
        "//" in raw_home
        or raw_home != str(home)
        or not home.is_absolute()
        or home != canonical
        or home == Path("/")
    ):
        raise ManagedStartupProfileUnavailable(
            "startup profile home is not one canonical absolute path"
        )
    components = tuple(home.parts[1:])
    if not components or any(component in ("", ".", "..") for component in components):
        raise ManagedStartupProfileUnavailable(
            "startup profile home has an invalid component"
        )
    descriptors: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("/", flags)
        descriptors.append(descriptor)
        for component in components:
            entry = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            descriptor = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            if _stat_identity(entry) != _stat_identity(os.fstat(descriptor)):
                raise ManagedStartupProfileUnavailable(
                    "startup profile path component changed while opening"
                )
        value = os.fstat(descriptors[-1])
        if not stat.S_ISDIR(value.st_mode):
            raise ManagedStartupProfileUnavailable(
                "startup profile home is not a directory"
            )
        if value.st_uid != os.getuid():
            raise ManagedStartupProfileUnavailable(
                "startup profile home has an unexpected owner"
            )
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise ManagedStartupProfileUnavailable(
                "startup profile home is not owner-private"
            )
        _rebind_profile_chain(descriptors, components)
        try:
            yield _HeldProfileHome(tuple(descriptors), value)
        finally:
            _rebind_profile_chain(descriptors, components)
    except ManagedStartupProfileUnavailable:
        raise
    except OSError as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile home could not be opened safely"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_fd_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_PROFILE_ENV_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_profile_env(
    home_descriptor: int,
    protected_keys: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        entry_before = os.stat(
            ".env",
            dir_fd=home_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(".env", flags, dir_fd=home_descriptor)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile environment could not be opened safely"
        ) from exc
    try:
        descriptor_before = os.fstat(descriptor)
        _validate_owner_private_regular(
            descriptor_before,
            "startup profile environment",
        )
        if _stat_identity(entry_before) != _stat_identity(descriptor_before):
            raise ManagedStartupProfileUnavailable(
                "startup profile environment changed while opening"
            )
        if descriptor_before.st_size > _MAX_PROFILE_ENV_BYTES:
            raise ManagedStartupProfileUnavailable(
                "startup profile environment exceeds size limit"
            )
        raw = _read_fd_bounded(descriptor)
        descriptor_mid = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        repeated = _read_fd_bounded(descriptor)
        descriptor_after = os.fstat(descriptor)
        entry_after = os.stat(
            ".env",
            dir_fd=home_descriptor,
            follow_symlinks=False,
        )
        if len(raw) > _MAX_PROFILE_ENV_BYTES or len(repeated) > _MAX_PROFILE_ENV_BYTES:
            raise ManagedStartupProfileUnavailable(
                "startup profile environment exceeds size limit"
            )
        identities = {
            _stat_identity(value)
            for value in (
                entry_before,
                descriptor_before,
                descriptor_mid,
                descriptor_after,
                entry_after,
            )
        }
        if len(identities) != 1 or raw != repeated:
            raise ManagedStartupProfileUnavailable(
                "startup profile environment changed while reading"
            )
        text = raw.decode("utf-8")
    except ManagedStartupProfileUnavailable:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile environment could not be read"
        ) from exc
    finally:
        os.close(descriptor)

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        env_value = raw_value.strip().strip('"').strip("'")
        if key and env_value and key not in protected_keys:
            values[key] = env_value
    return tuple(sorted(values.items()))


def _capture_desired_profile(
    profiles: ModuleType,
    epoch: ProcessEpoch,
) -> _DesiredProfileSnapshot:
    try:
        isolated = bool(profiles._is_isolated_profile_mode())
        if isolated:
            profile_name = str(profiles._isolated_profile_name()).strip()
            home = Path(profiles._INITIAL_HERMES_HOME).expanduser()
            if (
                home.parent.name != "profiles"
                or home.name != profile_name
                or home.parent.parent == home.parent
            ):
                raise ManagedStartupProfileUnavailable(
                    "isolated profile home is outside its single profile root"
                )
        else:
            profile_name = str(profiles._read_active_profile_file()).strip()
            home = Path(
                profiles._resolve_profile_home_for_name(profile_name)
            ).expanduser()
            base_home = Path(profiles._DEFAULT_HERMES_HOME).expanduser()
            named_home = base_home / "profiles" / profile_name
            if home not in (base_home, named_home):
                raise ManagedStartupProfileUnavailable(
                    "startup profile home is outside its single profile root"
                )
        if not profile_name or not home.is_absolute():
            raise ManagedStartupProfileUnavailable(
                "startup profile desired state is invalid"
            )
        protected_keys = frozenset(profiles._PROTECTED_ENV_KEYS)
        with _open_profile_home(home) as held_home:
            home_value = held_home.value
            env_values = _read_profile_env(
                held_home.descriptor,
                protected_keys,
            )
        prior_managed_env_keys = tuple(
            sorted(set(getattr(profiles, "_loaded_profile_env_keys", set())))
        )
        if set(prior_managed_env_keys) & protected_keys:
            raise ManagedStartupProfileUnavailable(
                "protected environment was marked as profile-managed"
            )
        protected_env = tuple(
            (
                key,
                key in os.environ,
                os.environ.get(key, ""),
            )
            for key in sorted(protected_keys)
        )
    except ManagedStartupProfileUnavailable:
        raise
    except Exception as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile desired state could not be captured"
        ) from exc

    canonical = json.dumps(
        {
            "epoch": {
                "pid": epoch.pid,
                "start_token": epoch.start_token,
            },
            "profile_name": profile_name,
            "hermes_home": str(home),
            "hermes_home_identity": (
                home_value.st_dev,
                home_value.st_ino,
                stat.S_IMODE(home_value.st_mode),
            ),
            "isolated": isolated,
            "environment": env_values,
            "prior_managed_env_keys": prior_managed_env_keys,
            "protected_environment": protected_env,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _DesiredProfileSnapshot(
        process_epoch=epoch,
        profile_name=profile_name,
        hermes_home=home,
        hermes_home_device=home_value.st_dev,
        hermes_home_inode=home_value.st_ino,
        hermes_home_mode=stat.S_IMODE(home_value.st_mode),
        isolated=isolated,
        env_values=env_values,
        prior_managed_env_keys=prior_managed_env_keys,
        protected_env=protected_env,
        desired_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _receipt_for_desired(
    desired: _DesiredProfileSnapshot,
) -> ManagedStartupProfileReceipt:
    return ManagedStartupProfileReceipt(
        process_epoch=desired.process_epoch,
        desired_sha256=desired.desired_sha256,
        profile_name=desired.profile_name,
        hermes_home=str(desired.hermes_home),
        hermes_home_device=desired.hermes_home_device,
        hermes_home_inode=desired.hermes_home_inode,
        hermes_home_mode=desired.hermes_home_mode,
        isolated=desired.isolated,
        env_keys=tuple(key for key, _value in desired.env_values),
    )


def _cached_module_postconditions(home: Path) -> tuple[tuple[str, bool], ...]:
    checks: list[tuple[str, bool]] = []
    for module_name in ("tools.skills_tool", "tools.skill_manager_tool"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        checks.extend(
            (
                (
                    f"{module_name}.HERMES_HOME",
                    getattr(module, "HERMES_HOME", None) == home,
                ),
                (
                    f"{module_name}.SKILLS_DIR",
                    getattr(module, "SKILLS_DIR", None) == home / "skills",
                ),
            )
        )

    cron_jobs = sys.modules.get("cron.jobs")
    if cron_jobs is not None:
        cron_dir = home / "cron"
        checks.extend(
            (
                ("cron.jobs.HERMES_DIR", getattr(cron_jobs, "HERMES_DIR", None) == home),
                ("cron.jobs.CRON_DIR", getattr(cron_jobs, "CRON_DIR", None) == cron_dir),
                (
                    "cron.jobs.JOBS_FILE",
                    getattr(cron_jobs, "JOBS_FILE", None) == cron_dir / "jobs.json",
                ),
                (
                    "cron.jobs.OUTPUT_DIR",
                    getattr(cron_jobs, "OUTPUT_DIR", None) == cron_dir / "output",
                ),
            )
        )

    cron_scheduler = sys.modules.get("cron.scheduler")
    if cron_scheduler is not None:
        cron_dir = home / "cron"
        checks.extend(
            (
                (
                    "cron.scheduler._hermes_home",
                    getattr(cron_scheduler, "_hermes_home", None) == home,
                ),
                (
                    "cron.scheduler._LOCK_DIR",
                    getattr(cron_scheduler, "_LOCK_DIR", None) == cron_dir,
                ),
                (
                    "cron.scheduler._LOCK_FILE",
                    getattr(cron_scheduler, "_LOCK_FILE", None)
                    == cron_dir / ".tick.lock",
                ),
            )
        )
        run_job = getattr(cron_scheduler, "run_job", None)
        if run_job is not None:
            checks.append(
                (
                    "cron.scheduler.run_job.profile_isolated",
                    bool(getattr(run_job, "_webui_profile_isolated", False)),
                )
            )
    return tuple(checks)


def _postcondition_checks(
    profiles: ModuleType,
    desired: _DesiredProfileSnapshot,
) -> tuple[tuple[str, bool], ...]:
    env_values = dict(desired.env_values)
    with _open_profile_home(desired.hermes_home) as held_home:
        home_value = held_home.value
    checks: list[tuple[str, bool]] = [
        (
            "profile_home_identity",
            (
                home_value.st_dev,
                home_value.st_ino,
                stat.S_IMODE(home_value.st_mode),
            )
            == (
                desired.hermes_home_device,
                desired.hermes_home_inode,
                desired.hermes_home_mode,
            ),
        ),
        (
            "active_profile",
            str(getattr(profiles, "_active_profile", "")).strip()
            == desired.profile_name,
        ),
        (
            "HERMES_HOME",
            os.environ.get("HERMES_HOME") == str(desired.hermes_home),
        ),
        (
            "loaded_profile_env_keys",
            set(getattr(profiles, "_loaded_profile_env_keys", set()))
            == set(env_values),
        ),
    ]
    checks.extend(
        (
            f"profile_env:{key}",
            os.environ.get(key) == expected,
        )
        for key, expected in desired.env_values
    )
    checks.extend(
        (
            f"prior_profile_env_absent:{key}",
            key not in os.environ,
        )
        for key in desired.prior_managed_env_keys
        if key not in env_values
    )
    checks.extend(
        (
            f"protected_env:{key}",
            (key in os.environ) is was_present
            and (not was_present or os.environ.get(key) == expected),
        )
        for key, was_present, expected in desired.protected_env
    )
    checks.extend(_cached_module_postconditions(desired.hermes_home))
    return tuple(checks)


def _verification_for_state(
    profiles: ModuleType,
    state: _ManagedProfileState,
) -> ManagedStartupProfileVerification:
    try:
        checks = _postcondition_checks(profiles, state.desired)
    except Exception:
        return ManagedStartupProfileVerification(
            ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
            state.receipt,
            "profile_postcondition_unobservable",
        )
    failed = tuple(name for name, complete in checks if not complete)
    if not failed:
        return ManagedStartupProfileVerification(
            ManagedStartupProfileVerificationOutcome.PROVED_COMPLETE,
            state.receipt,
            None,
        )
    return ManagedStartupProfileVerification(
        ManagedStartupProfileVerificationOutcome.PARTIAL,
        state.receipt,
        "profile_postcondition_mismatch:" + ",".join(failed),
    )


def _apply_desired_profile(
    profiles: ModuleType,
    desired: _DesiredProfileSnapshot,
) -> None:
    if not _startup_mutations_are_admitted():
        raise ManagedStartupProfileAdmissionError(
            "startup profile mutation requires admitted startup"
        )
    protected_keys = frozenset(key for key, _present, _value in desired.protected_env)
    current_loaded_keys = frozenset(
        getattr(profiles, "_loaded_profile_env_keys", set())
    )
    protected_tracking = current_loaded_keys & protected_keys
    if protected_tracking:
        raise ManagedStartupProfilePostconditionError(
            "protected environment was marked profile-managed before mutation: "
            + ",".join(sorted(protected_tracking))
        )
    for key, was_present, expected in desired.protected_env:
        if (key in os.environ) is not was_present or (
            was_present and os.environ.get(key) != expected
        ):
            raise ManagedStartupProfilePostconditionError(
                f"protected environment changed before mutation: {key}"
            )
    with _open_profile_home(desired.hermes_home) as held_home:
        home_value = held_home.value
        if (
            home_value.st_dev,
            home_value.st_ino,
            stat.S_IMODE(home_value.st_mode),
        ) != (
            desired.hermes_home_device,
            desired.hermes_home_inode,
            desired.hermes_home_mode,
        ):
            raise ManagedStartupProfileUnavailable(
                "startup profile home identity changed before mutation"
            )
        removal_keys = set(desired.prior_managed_env_keys)
        removal_keys.update(current_loaded_keys)
        for key in removal_keys:
            os.environ.pop(key, None)
        profiles._loaded_profile_env_keys = set()
        profiles._active_profile = desired.profile_name
        profiles._set_hermes_home(desired.hermes_home)
        profiles.install_cron_scheduler_profile_isolation()
        for key, value in desired.env_values:
            os.environ[key] = value
        profiles._loaded_profile_env_keys = {
            key for key, _value in desired.env_values
        }


def apply_managed_startup_profile_state() -> ManagedStartupProfileReceipt:
    """Apply or repair one stable desired profile snapshot for this process."""

    global _STATE
    if not _startup_mutations_are_admitted():
        raise ManagedStartupProfileAdmissionError(
            "startup profile mutation requires admitted startup"
        )
    epoch = _current_process_epoch()
    if epoch is None:
        raise ManagedStartupProfileUnavailable(
            "startup profile process epoch is unavailable"
        )
    try:
        profiles = _load_profiles_module()
    except ImportError as exc:
        raise ManagedStartupProfileUnavailable(
            "startup profile module is unavailable"
        ) from exc
    with _STATE_LOCK:
        if _STATE is not None and _STATE.desired.process_epoch != epoch:
            _STATE = None
        if _STATE is None:
            desired = _capture_desired_profile(profiles, epoch)
            _STATE = _ManagedProfileState(
                desired,
                _receipt_for_desired(desired),
            )
        current = _verification_for_state(profiles, _STATE)
        if (
            current.outcome
            is ManagedStartupProfileVerificationOutcome.PROVED_COMPLETE
        ):
            return _STATE.receipt
        try:
            _apply_desired_profile(profiles, _STATE.desired)
        except ManagedStartupProfileError:
            raise
        except Exception as exc:
            failed = _verification_for_state(profiles, _STATE)
            raise ManagedStartupProfilePostconditionError(
                failed.reason or "startup profile mutation failed"
            ) from exc
        verified = _verification_for_state(profiles, _STATE)
        if (
            verified.outcome
            is not ManagedStartupProfileVerificationOutcome.PROVED_COMPLETE
        ):
            raise ManagedStartupProfilePostconditionError(
                verified.reason or "startup profile postcondition is incomplete"
            )
        return _STATE.receipt


def verify_managed_startup_profile_state(
    receipt: ManagedStartupProfileReceipt | None = None,
) -> ManagedStartupProfileVerification:
    """Verify exact current-process profile postconditions without mutation."""

    epoch = _current_process_epoch()
    if epoch is None:
        return ManagedStartupProfileVerification(
            ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
            None,
            "process_epoch_unavailable",
        )
    try:
        profiles = _load_profiles_module()
    except (ManagedStartupProfileUnavailable, ImportError):
        return ManagedStartupProfileVerification(
            ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
            None,
            "profile_module_unavailable",
        )
    with _STATE_LOCK:
        state = _STATE
        if state is None:
            if receipt is not None:
                if receipt.process_epoch != epoch:
                    reason = "managed_profile_receipt_from_foreign_epoch"
                else:
                    reason = "managed_profile_receipt_without_state"
                return ManagedStartupProfileVerification(
                    ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
                    receipt,
                    reason,
                )
            return ManagedStartupProfileVerification(
                ManagedStartupProfileVerificationOutcome.PROVED_ABSENT,
                None,
                "managed_profile_state_not_installed",
            )
        if state.desired.process_epoch != epoch:
            return ManagedStartupProfileVerification(
                ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
                state.receipt,
                "managed_profile_state_from_foreign_epoch",
            )
        if receipt is not None and receipt != state.receipt:
            return ManagedStartupProfileVerification(
                ManagedStartupProfileVerificationOutcome.AMBIGUOUS,
                state.receipt,
                "managed_profile_receipt_mismatch",
            )
        return _verification_for_state(profiles, state)


def _reset_after_fork() -> None:
    global _STATE_LOCK, _STATE
    _STATE_LOCK = threading.Lock()
    _STATE = None


def _reset_managed_startup_profile_for_tests() -> None:
    global _STATE
    with _STATE_LOCK:
        _STATE = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
