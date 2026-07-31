"""Strict process-epoch reconciliation for deferred startup configuration."""

from __future__ import annotations

import errno
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import weakref
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Callable

from api.process_identity import process_start_token

_MAX_SETTINGS_BYTES = 4 * 1_048_576
_MAX_JOURNAL_BYTES = 8 * 1_048_576
_JOURNAL_ENV = "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"
_JOURNAL_PHASES = (
    "intent",
    "settings-durable",
    "pending-consumed",
    "cli-published",
    "complete",
)
_STABLE_CLI_INSTANCES = weakref.WeakValueDictionary()
_RETRYABLE_ERRNOS = {
    errno.EAGAIN,
    errno.EBUSY,
    errno.EINTR,
    errno.EIO,
    errno.ENOSPC,
    getattr(errno, "ESTALE", errno.EIO),
}


class ManagedStartupConfigurationError(RuntimeError):
    """Base error for managed startup configuration."""


class ManagedStartupConfigurationAdmissionError(ManagedStartupConfigurationError):
    """A startup configuration mutation was attempted outside admission."""


class ManagedStartupConfigurationUnavailable(ManagedStartupConfigurationError):
    """The desired state or its filesystem proof could not be established."""


class ManagedStartupConfigurationAmbiguous(ManagedStartupConfigurationError):
    """Observed state is foreign/newer and must not be overwritten."""


class ManagedStartupConfigurationRetry(str, Enum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class ManagedStartupConfigurationMutationError(ManagedStartupConfigurationError):
    """A classified settings or process-publication mutation failure."""

    def __init__(
        self,
        message: str,
        *,
        retry: ManagedStartupConfigurationRetry,
    ) -> None:
        super().__init__(message)
        self.retry = retry


class ManagedStartupConfigurationVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PROVED_RETRY_SAFE_PARTIAL = "proved-retry-safe-partial"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


def _cli_digest(values: tuple[str, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ProcessEpoch:
    pid: int
    start_token: str


@dataclass(frozen=True)
class ReleaseBinding:
    transaction_id: str
    manifest_sha256: str


@dataclass(frozen=True)
class SettingsFileEvidence:
    exists: bool
    device: int | None
    inode: int | None
    mode: int | None
    owner: int | None
    nlink: int | None
    size: int
    sha256: str | None


@dataclass(frozen=True)
class SettingsParentEvidence:
    device: int
    inode: int
    mode: int
    owner: int


@dataclass(frozen=True)
class PendingStartupSettingsRecord:
    generation: int
    path: str
    source: SettingsFileEvidence
    desired_bytes: bytes = field(repr=False)
    desired_sha256: str
    schema_valid: bool


@dataclass(frozen=True)
class PendingStartupSettingsFailure:
    error_type: str
    message: str


@dataclass(frozen=True)
class DurableSettingsReceipt:
    status: str
    path: str
    sha256: str | None
    device: int | None
    inode: int | None
    mode: int | None
    size: int
    generation: int | None = None
    parent: SettingsParentEvidence | None = None
    source: SettingsFileEvidence | None = None
    post: SettingsFileEvidence | None = None
    schema_valid: bool = False
    migrated_from_mode: int | None = None


@dataclass(frozen=True)
class ProcessCliToolsetsReceipt:
    toolsets: tuple[str, ...]
    publication_id: int
    generation: int
    sha256: str


@dataclass(frozen=True)
class ManagedStartupConfigurationReceipt:
    process_epoch: ProcessEpoch
    release_binding: ReleaseBinding
    desired_sha256: str
    settings: DurableSettingsReceipt
    cli: ProcessCliToolsetsReceipt


@dataclass(frozen=True)
class ManagedStartupConfigurationVerification:
    outcome: ManagedStartupConfigurationVerificationOutcome
    receipt: ManagedStartupConfigurationReceipt | None
    settings_complete: bool
    cli_complete: bool
    reason: str | None


class StableCliToolsets(Sequence):
    """Never-rebound proxy so preaccept cached readers observe publication."""

    def __init__(self, values=()):
        self._lock = threading.Lock()
        self._values = tuple(values)
        self._generation = 0
        self._sha256 = _cli_digest(self._values)
        _STABLE_CLI_INSTANCES[id(self)] = self

    def _reset_lock_after_fork(self):
        self._lock = threading.Lock()

    def __getitem__(self, index):
        with self._lock:
            return self._values[index]

    def __len__(self):
        with self._lock:
            return len(self._values)

    def __iter__(self):
        with self._lock:
            return iter(self._values)

    def __eq__(self, other):
        if isinstance(other, (list, tuple, StableCliToolsets)):
            return tuple(self) == tuple(other)
        return NotImplemented

    def snapshot(self) -> tuple[tuple[str, ...], int, str]:
        with self._lock:
            return self._values, self._generation, self._sha256

    def publish(
        self,
        values: tuple[str, ...],
        *,
        force_generation: bool = False,
    ) -> tuple[int, str]:
        digest = _cli_digest(values)
        with self._lock:
            if self._values != values or force_generation:
                self._values = values
                self._generation += 1
                self._sha256 = digest
            return self._generation, self._sha256


@dataclass(frozen=True)
class _DesiredConfiguration:
    process_epoch: ProcessEpoch
    release_binding: ReleaseBinding
    settings_path: Path
    pending: PendingStartupSettingsRecord | None = field(repr=False)
    settings_bytes: bytes | None = field(repr=False)
    settings_sha256: str | None
    cli_toolsets: tuple[str, ...]
    desired_sha256: str


@dataclass
class _ConfigurationState:
    desired: _DesiredConfiguration
    settings_receipt: DurableSettingsReceipt | None = None
    pending_consumed: bool = False
    operation_phase: str = "captured"
    replace_preimage: SettingsFileEvidence | None = None
    planned_postimage: SettingsFileEvidence | None = None
    cli_publication: StableCliToolsets | None = None
    receipt: ManagedStartupConfigurationReceipt | None = None
    journal_path: Path | None = None
    journal_evidence: SettingsFileEvidence | None = None


_STATE_LOCK = threading.Lock()
_STATE: _ConfigurationState | None = None
_JOURNAL_PATH_OVERRIDE: Path | None = None


def _current_process_epoch() -> ProcessEpoch | None:
    pid = os.getpid()
    token = process_start_token(pid)
    return ProcessEpoch(pid, token) if token else None


def _startup_mutations_are_admitted() -> bool:
    try:
        from api import config
    except ImportError:
        return False
    return bool(config._startup_mutations_are_admitted())


def _require_admission(stage: str) -> None:
    if not _startup_mutations_are_admitted():
        raise ManagedStartupConfigurationAdmissionError(
            f"{stage} requires admitted startup"
        )


def _load_config_module() -> ModuleType:
    try:
        from api import config
    except ImportError as exc:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration module is unavailable"
        ) from exc
    return config


def _configured_release_binding(
    config: ModuleType,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
) -> ReleaseBinding:
    transaction = str(
        transaction_id
        if transaction_id is not None
        else getattr(config, "_RUN_ADMISSION_TRANSACTION_ID", "")
        or ""
    ).strip()
    manifest = str(
        manifest_sha256
        if manifest_sha256 is not None
        else os.environ.get("HERMES_WEBUI_MANIFEST_SHA256", "")
    ).strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", transaction):
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration transaction binding is unavailable"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", manifest):
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration manifest binding is unavailable"
        )
    return ReleaseBinding(transaction, manifest)


def _identity(value: os.stat_result) -> tuple[int, ...]:
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


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    """Identity fields that cannot change from ordinary directory contents."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
    )


def _canonical_absolute_path(value) -> Path:
    raw = os.fspath(value)
    path = Path(raw)
    if (
        "//" in raw
        or raw != str(path)
        or not path.is_absolute()
        or path != Path(os.path.normpath(raw))
        or path == Path("/")
    ):
        raise ManagedStartupConfigurationUnavailable(
            "settings path is not one canonical absolute path"
        )
    return path


def configure_managed_startup_configuration_journal(path_value) -> Path:
    """Bind the per-transaction sidecar path before startup acceptance."""

    global _JOURNAL_PATH_OVERRIDE
    path = _canonical_absolute_path(path_value)
    if _JOURNAL_PATH_OVERRIDE is None:
        _JOURNAL_PATH_OVERRIDE = path
    elif _JOURNAL_PATH_OVERRIDE != path:
        raise ManagedStartupConfigurationAmbiguous(
            "startup configuration journal path was rebound"
        )
    return path


def _configured_journal_path() -> Path:
    if _JOURNAL_PATH_OVERRIDE is not None:
        return _JOURNAL_PATH_OVERRIDE
    raw = str(os.environ.get(_JOURNAL_ENV) or "").strip()
    if not raw:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal path is unavailable"
        )
    return _canonical_absolute_path(raw)


@contextmanager
def _open_settings_parent(path: Path):
    components = tuple(path.parent.parts[1:])
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []

    def rebind() -> None:
        root = os.stat("/", follow_symlinks=False)
        if _directory_identity(root) != _directory_identity(
            os.fstat(descriptors[0])
        ):
            raise ManagedStartupConfigurationUnavailable(
                "settings root identity changed"
            )
        for index, component in enumerate(components):
            entry = os.stat(
                component,
                dir_fd=descriptors[index],
                follow_symlinks=False,
            )
            if _directory_identity(entry) != _directory_identity(
                os.fstat(descriptors[index + 1])
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "settings parent path became detached"
                )

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
            if _directory_identity(entry) != _directory_identity(
                os.fstat(descriptor)
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "settings parent changed while opening"
                )
        parent = os.fstat(descriptors[-1])
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) not in {0o700, 0o755}
        ):
            raise ManagedStartupConfigurationUnavailable(
                "settings parent is not owner-safe mode 0700 or 0755"
            )
        rebind()
        try:
            yield descriptors[-1]
        finally:
            rebind()
    except ManagedStartupConfigurationUnavailable:
        raise
    except OSError as exc:
        raise ManagedStartupConfigurationUnavailable(
            "settings parent could not be opened safely"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _open_journal_parent(path: Path):
    if not isinstance(getattr(os, "O_NOFOLLOW", None), int) or not os.O_NOFOLLOW:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal requires O_NOFOLLOW"
        )
    with _open_settings_parent(path) as parent:
        if stat.S_IMODE(os.fstat(parent).st_mode) != 0o700:
            raise ManagedStartupConfigurationUnavailable(
                "startup configuration journal parent is not private"
            )
        yield parent


@contextmanager
def _journal_lock(path: Path, *, crash_hook=None):
    import fcntl

    with _open_journal_parent(path) as parent:
        name = f".{path.name}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        try:
            state = os.fstat(descriptor)
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_uid != os.getuid()
                or state.st_nlink != 1
                or stat.S_IMODE(state.st_mode) != 0o600
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal lock is unsafe"
                )
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(named) != _identity(state):
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal lock path changed"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = os.fstat(descriptor)
            named_locked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(locked) != _identity(named_locked):
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal lock changed while waiting"
                )
            if crash_hook is not None:
                crash_hook("journal-lock-acquired")
            named_after_hook = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(os.fstat(descriptor)) != _identity(named_after_hook):
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal lock changed after acquisition"
                )
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _clean_journal_orphans(path: Path) -> int:
    prefix = f".{path.name}."
    removed = 0
    with _open_journal_parent(path) as parent:
        names = [
            name
            for name in os.listdir(parent)
            if name.startswith(prefix) and name.endswith(".tmp")
        ]
        if len(names) > 128:
            raise ManagedStartupConfigurationUnavailable(
                "too many startup configuration journal orphans"
            )
        for name in names:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_uid != os.getuid()
                or named.st_nlink != 1
                or stat.S_IMODE(named.st_mode) != 0o600
                or named.st_size > _MAX_JOURNAL_BYTES
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal orphan is unsafe"
                )
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise ManagedStartupConfigurationUnavailable(
                    "startup configuration journal orphan changed"
                ) from exc
            try:
                opened = os.fstat(descriptor)
                rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    _identity(named) != _identity(opened)
                    or _identity(opened) != _identity(rebound)
                ):
                    raise ManagedStartupConfigurationUnavailable(
                        "startup configuration journal orphan changed"
                    )
                os.unlink(name, dir_fd=parent)
            finally:
                os.close(descriptor)
            removed += 1
        if removed:
            os.fsync(parent)
    return removed


def _read_bounded_twice(
    parent_descriptor: int,
    name: str,
    *,
    allow_legacy_mode: bool = False,
    max_bytes: int = _MAX_SETTINGS_BYTES,
) -> tuple[bytes, os.stat_result] | None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ManagedStartupConfigurationUnavailable("file size bound is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        entry_before = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManagedStartupConfigurationUnavailable(
            "settings file could not be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        allowed_modes = {0o600, 0o644} if allow_legacy_mode else {0o600}
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in allowed_modes
        ):
            raise ManagedStartupConfigurationUnavailable(
                "settings file mode/owner/type/link contract is unsafe"
            )
        if before.st_size > max_bytes:
            raise ManagedStartupConfigurationUnavailable(
                "settings file exceeds size limit"
            )

        def read_once() -> bytes:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        first = read_once()
        middle = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        second = read_once()
        after = os.fstat(descriptor)
        entry_after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            len(first) > max_bytes
            or len(second) > max_bytes
        ):
            raise ManagedStartupConfigurationUnavailable(
                "settings file exceeds size limit"
            )
        if (
            len(
                {
                    _identity(value)
                    for value in (
                        entry_before,
                        before,
                        middle,
                        after,
                        entry_after,
                    )
                }
            )
            != 1
            or first != second
        ):
            raise ManagedStartupConfigurationUnavailable(
                "settings file changed while reading"
            )
        return first, after
    except ManagedStartupConfigurationUnavailable:
        raise
    except OSError as exc:
        raise ManagedStartupConfigurationUnavailable(
            "settings file could not be read safely"
        ) from exc
    finally:
        os.close(descriptor)


def _file_evidence(
    observed: tuple[bytes, os.stat_result] | None,
) -> SettingsFileEvidence:
    if observed is None:
        return SettingsFileEvidence(False, None, None, None, None, None, 0, None)
    content, value = observed
    return SettingsFileEvidence(
        True,
        value.st_dev,
        value.st_ino,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_nlink,
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _parent_evidence(descriptor: int) -> SettingsParentEvidence:
    value = os.fstat(descriptor)
    return SettingsParentEvidence(
        value.st_dev,
        value.st_ino,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
    )


def _strict_json_object(payload: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("journal root is not an object")
    return value


def _evidence_from_json(value) -> SettingsFileEvidence:
    if not isinstance(value, dict) or set(value) != {
        "exists",
        "device",
        "inode",
        "mode",
        "owner",
        "nlink",
        "size",
        "sha256",
    }:
        raise ValueError("file evidence schema is invalid")
    evidence = SettingsFileEvidence(**value)
    if not isinstance(evidence.exists, bool):
        raise ValueError("file evidence existence is invalid")
    if evidence.exists:
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (
                    evidence.device,
                    evidence.inode,
                    evidence.mode,
                    evidence.owner,
                    evidence.nlink,
                    evidence.size,
                )
            )
            or evidence.nlink != 1
            or evidence.mode not in {0o600, 0o644}
            or not isinstance(evidence.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.sha256)
        ):
            raise ValueError("file evidence value is invalid")
    elif evidence != SettingsFileEvidence(
        False, None, None, None, None, None, 0, None
    ):
        raise ValueError("absent file evidence is invalid")
    return evidence


def _pending_to_json(
    pending: PendingStartupSettingsRecord | None,
):
    if pending is None:
        return None
    return {
        "generation": pending.generation,
        "path": pending.path,
        "source": asdict(pending.source),
        "desired_base64": base64.b64encode(pending.desired_bytes).decode("ascii"),
        "desired_sha256": pending.desired_sha256,
        "schema_valid": pending.schema_valid,
    }


def _pending_from_json(value) -> PendingStartupSettingsRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "generation",
        "path",
        "source",
        "desired_base64",
        "desired_sha256",
        "schema_valid",
    }:
        raise ValueError("pending journal schema is invalid")
    desired = base64.b64decode(value["desired_base64"], validate=True)
    if (
        len(desired) > _MAX_SETTINGS_BYTES
        or hashlib.sha256(desired).hexdigest() != value["desired_sha256"]
        or value["schema_valid"] is not True
        or not isinstance(json.loads(desired), dict)
    ):
        raise ValueError("pending journal desired state is invalid")
    pending = PendingStartupSettingsRecord(
        value["generation"],
        str(_canonical_absolute_path(value["path"])),
        _evidence_from_json(value["source"]),
        desired,
        value["desired_sha256"],
        True,
    )
    if (
        not isinstance(pending.generation, int)
        or isinstance(pending.generation, bool)
        or pending.generation < 1
    ):
        raise ValueError("pending journal generation is invalid")
    return pending


def _settings_receipt_from_json(value) -> DurableSettingsReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("settings receipt is invalid")
    material = dict(value)
    for name, constructor in (
        ("parent", SettingsParentEvidence),
        ("source", _evidence_from_json),
        ("post", _evidence_from_json),
    ):
        nested = material.get(name)
        if nested is not None:
            if not isinstance(nested, dict):
                raise ValueError("settings receipt evidence is invalid")
            material[name] = (
                constructor(**nested)
                if constructor is SettingsParentEvidence
                else constructor(nested)
            )
    return DurableSettingsReceipt(**material)


def _desired_digest(
    binding: ReleaseBinding,
    settings_path: Path,
    pending: PendingStartupSettingsRecord | None,
    cli_toolsets: tuple[str, ...],
) -> str:
    canonical = json.dumps(
        {
            "transaction_id": binding.transaction_id,
            "manifest_sha256": binding.manifest_sha256,
            "settings_path": str(settings_path),
            "pending": _pending_to_json(pending),
            "cli_toolsets": cli_toolsets,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _journal_payload(state: _ConfigurationState) -> dict:
    desired = state.desired
    cli = None
    if state.cli_publication is not None:
        values, generation, digest = state.cli_publication.snapshot()
        cli = {
            "process_epoch": asdict(desired.process_epoch),
            "toolsets": list(values),
            "generation": generation,
            "sha256": digest,
            "publication_id": id(state.cli_publication),
        }
    payload = {
        "version": 1,
        "transaction_id": desired.release_binding.transaction_id,
        "manifest_sha256": desired.release_binding.manifest_sha256,
        "settings_path": str(desired.settings_path),
        "pending": _pending_to_json(desired.pending),
        "cli_toolsets": list(desired.cli_toolsets),
        "desired_sha256": desired.desired_sha256,
        "phase": state.operation_phase,
        "settings_receipt": (
            asdict(state.settings_receipt)
            if state.settings_receipt is not None
            else None
        ),
        "replace_preimage": (
            asdict(state.replace_preimage)
            if state.replace_preimage is not None
            else None
        ),
        "planned_postimage": (
            asdict(state.planned_postimage)
            if state.planned_postimage is not None
            else None
        ),
        "pending_consumed": state.pending_consumed,
        "cli_receipt": cli,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _read_journal(path: Path) -> tuple[dict | None, SettingsFileEvidence]:
    with _open_journal_parent(path) as parent:
        observed = _read_bounded_twice(
            parent,
            path.name,
            max_bytes=_MAX_JOURNAL_BYTES,
        )
    evidence = _file_evidence(observed)
    if observed is None:
        return None, evidence
    try:
        value = _strict_json_object(observed[0])
    except Exception as exc:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal is malformed"
        ) from exc
    required = {
        "version",
        "transaction_id",
        "manifest_sha256",
        "settings_path",
        "pending",
        "cli_toolsets",
        "desired_sha256",
        "phase",
        "settings_receipt",
        "replace_preimage",
        "planned_postimage",
        "pending_consumed",
        "cli_receipt",
        "evidence_sha256",
    }
    if (
        set(value) != required
        or value["version"] != 1
        or value["phase"] not in _JOURNAL_PHASES
        or not isinstance(value["pending_consumed"], bool)
        or not isinstance(value["cli_toolsets"], list)
        or any(not isinstance(item, str) or not item for item in value["cli_toolsets"])
    ):
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal schema is invalid"
        )
    evidence_sha256 = value.pop("evidence_sha256")
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    value["evidence_sha256"] = evidence_sha256
    if (
        not isinstance(evidence_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256)
        or hashlib.sha256(canonical).hexdigest() != evidence_sha256
    ):
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal evidence digest is invalid"
        )
    return value, evidence


def _write_journal(
    path: Path,
    state: _ConfigurationState,
    *,
    crash_hook=None,
) -> SettingsFileEvidence:
    _require_admission("startup configuration journal mutation")
    payload = (
        json.dumps(
            _journal_payload(state),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal exceeds size limit"
        )
    with _open_journal_parent(path) as parent:
        current = _file_evidence(
            _read_bounded_twice(
                parent,
                path.name,
                max_bytes=_MAX_JOURNAL_BYTES,
            )
        )
        expected = state.journal_evidence or SettingsFileEvidence(
            False, None, None, None, None, None, 0, None
        )
        if current != expected:
            raise ManagedStartupConfigurationAmbiguous(
                "startup configuration journal changed"
            )
        temporary = (
            f".{path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(8)}.tmp"
        )
        descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "journal write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            if crash_hook is not None:
                crash_hook("journal-temp-fsynced")
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            if crash_hook is not None:
                crash_hook("journal-renamed")
            os.fsync(parent)
            if crash_hook is not None:
                crash_hook("journal-parent-fsynced")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        proof = _read_bounded_twice(
            parent,
            path.name,
            max_bytes=_MAX_JOURNAL_BYTES,
        )
    if proof is None or proof[0] != payload:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal postcondition failed"
        )
    evidence = _file_evidence(proof)
    state.journal_evidence = evidence
    return evidence


def capture_pending_startup_settings_record(
    path_value,
    desired_text: str,
    generation: int,
) -> PendingStartupSettingsRecord:
    """Capture the exact source preimage paired with normalized desired bytes."""

    path = _canonical_absolute_path(path_value)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ManagedStartupConfigurationUnavailable(
            "pending settings generation is invalid"
        )
    desired = desired_text.encode("utf-8")
    if len(desired) > _MAX_SETTINGS_BYTES:
        raise ManagedStartupConfigurationUnavailable(
            "pending settings exceeds size limit"
        )
    try:
        schema_valid = isinstance(json.loads(desired), dict)
    except (UnicodeDecodeError, json.JSONDecodeError):
        schema_valid = False
    if not schema_valid:
        raise ManagedStartupConfigurationUnavailable(
            "pending settings JSON schema is invalid"
        )
    with _open_settings_parent(path) as parent:
        source = _file_evidence(
            _read_bounded_twice(
                parent,
                path.name,
                allow_legacy_mode=True,
            )
        )
    return PendingStartupSettingsRecord(
        generation,
        str(path),
        source,
        desired,
        hashlib.sha256(desired).hexdigest(),
        True,
    )


def _settings_receipt(
    path: Path,
    expected: bytes,
) -> DurableSettingsReceipt | None:
    with _open_settings_parent(path) as parent:
        observed = _read_bounded_twice(parent, path.name)
    if observed is None:
        return None
    content, value = observed
    if content != expected:
        return None
    return DurableSettingsReceipt(
        status="proved-complete",
        path=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        device=value.st_dev,
        inode=value.st_ino,
        mode=stat.S_IMODE(value.st_mode),
        size=len(content),
    )


def _retry_for_exception(exc: BaseException) -> ManagedStartupConfigurationRetry:
    if isinstance(exc, OSError) and exc.errno in _RETRYABLE_ERRNOS:
        return ManagedStartupConfigurationRetry.RETRYABLE
    return ManagedStartupConfigurationRetry.TERMINAL


def _atomic_write_settings(
    path: Path,
    content: bytes,
    *,
    pending: PendingStartupSettingsRecord,
    force_replace: bool = False,
    allow_retry_safe_desired: bool = False,
    retry_preimage: SettingsFileEvidence | None = None,
    retry_postimage: SettingsFileEvidence | None = None,
    record_preimage: Callable[[SettingsFileEvidence], None] | None = None,
    record_postimage: Callable[[SettingsFileEvidence], None] | None = None,
    crash_hook=None,
) -> DurableSettingsReceipt:
    if len(content) > _MAX_SETTINGS_BYTES:
        raise ManagedStartupConfigurationUnavailable(
            "pending settings exceeds size limit"
        )
    with _open_settings_parent(path) as parent:
        parent_receipt = _parent_evidence(parent)
        existing = _read_bounded_twice(
            parent,
            path.name,
            allow_legacy_mode=True,
        )
        existing_evidence = _file_evidence(existing)
        if record_preimage is not None:
            record_preimage(existing_evidence)
        migrated_from_mode = (
            0o644
            if pending.source.mode == 0o644 and existing_evidence.mode == 0o600
            else None
        )
        if existing_evidence != pending.source:
            exact_legacy_hardening = (
                allow_retry_safe_desired
                and pending.source.exists
                and pending.source.mode == 0o644
                and existing_evidence.exists
                and existing_evidence.mode == 0o600
                and (
                    existing_evidence.device,
                    existing_evidence.inode,
                    existing_evidence.owner,
                    existing_evidence.nlink,
                    existing_evidence.size,
                    existing_evidence.sha256,
                )
                == (
                    pending.source.device,
                    pending.source.inode,
                    pending.source.owner,
                    pending.source.nlink,
                    pending.source.size,
                    pending.source.sha256,
                )
            )
            retry_preimage_matches = (
                allow_retry_safe_desired
                and retry_preimage is not None
                and existing_evidence == retry_preimage
            )
            desired_postimage_matches = (
                allow_retry_safe_desired
                and retry_postimage is not None
                and existing_evidence == retry_postimage
                and existing_evidence.exists
                and existing_evidence.mode == 0o600
                and existing_evidence.sha256 == pending.desired_sha256
            )
            if (
                not retry_preimage_matches
                and not desired_postimage_matches
                and not exact_legacy_hardening
            ):
                raise ManagedStartupConfigurationAmbiguous(
                    "settings source preimage changed; refusing overwrite"
                )
            if desired_postimage_matches:
                reopened = os.open(
                    path.name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
                try:
                    os.fsync(reopened)
                finally:
                    os.close(reopened)
                os.fsync(parent)
                return DurableSettingsReceipt(
                    "proved-complete",
                    str(path),
                    pending.desired_sha256,
                    existing_evidence.device,
                    existing_evidence.inode,
                    existing_evidence.mode,
                    existing_evidence.size,
                    pending.generation,
                    parent_receipt,
                    pending.source,
                    existing_evidence,
                    pending.schema_valid,
                    None,
                )
        if existing_evidence.exists and existing_evidence.mode == 0o644:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                if _identity(os.fstat(descriptor)) != _identity(existing[1]):
                    raise ManagedStartupConfigurationAmbiguous(
                        "legacy settings changed before hardening"
                    )
                _require_admission("legacy settings permission hardening")
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            migrated_from_mode = 0o644
            existing = _read_bounded_twice(parent, path.name)
            existing_evidence = _file_evidence(existing)
        if not force_replace and existing is not None and existing[0] == content:
            value = existing[1]
            post = _file_evidence(existing)
            return DurableSettingsReceipt(
                "proved-complete",
                str(path),
                hashlib.sha256(content).hexdigest(),
                value.st_dev,
                value.st_ino,
                stat.S_IMODE(value.st_mode),
                len(content),
                pending.generation,
                parent_receipt,
                pending.source,
                post,
                pending.schema_valid,
                migrated_from_mode,
            )
        temporary = (
            f".{path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{secrets.token_hex(8)}.tmp"
        )
        descriptor = None
        try:
            _require_admission("settings temporary-file mutation")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            if crash_hook is not None:
                crash_hook("settings-temp-fsynced")
            planned_state = os.fstat(descriptor)
            planned_postimage = SettingsFileEvidence(
                True,
                planned_state.st_dev,
                planned_state.st_ino,
                stat.S_IMODE(planned_state.st_mode),
                planned_state.st_uid,
                planned_state.st_nlink,
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
            if record_postimage is not None:
                record_postimage(planned_postimage)
            os.close(descriptor)
            descriptor = None
            _require_admission("settings atomic replacement")
            cas_observed = _file_evidence(
                _read_bounded_twice(parent, path.name)
            )
            if cas_observed != existing_evidence:
                raise ManagedStartupConfigurationAmbiguous(
                    "settings preimage changed before atomic replacement"
                )
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            if crash_hook is not None:
                crash_hook("settings-renamed")
            os.fsync(parent)
            if crash_hook is not None:
                crash_hook("settings-parent-fsynced")
        except (
            ManagedStartupConfigurationAdmissionError,
            ManagedStartupConfigurationAmbiguous,
        ):
            raise
        except Exception as exc:
            raise ManagedStartupConfigurationMutationError(
                "settings atomic replacement failed",
                retry=_retry_for_exception(exc),
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        proof = _read_bounded_twice(parent, path.name)
        if proof is None or proof[0] != content:
            raise ManagedStartupConfigurationMutationError(
                "settings postcondition was not exact",
                retry=ManagedStartupConfigurationRetry.RETRYABLE,
            )
        value = proof[1]
        post = _file_evidence(proof)
        return DurableSettingsReceipt(
            "proved-complete",
            str(path),
            hashlib.sha256(content).hexdigest(),
            value.st_dev,
            value.st_ino,
            stat.S_IMODE(value.st_mode),
            len(content),
            pending.generation,
            parent_receipt,
            pending.source,
            post,
            pending.schema_valid,
            migrated_from_mode,
        )


def _capture_desired(
    config: ModuleType,
    epoch: ProcessEpoch,
    binding: ReleaseBinding,
) -> _DesiredConfiguration:
    try:
        with config._DEFERRED_STARTUP_CONFIG_LOCK:
            if not _startup_mutations_are_admitted():
                raise ManagedStartupConfigurationAdmissionError(
                    "configuration snapshot requires admitted startup"
                )
            pending = config._DEFERRED_STARTUP_SETTINGS_TEXT
            if isinstance(pending, PendingStartupSettingsFailure):
                raise ManagedStartupConfigurationUnavailable(
                    "managed pending settings capture failed: "
                    f"{pending.error_type}: {pending.message}"
                )
            if pending is not None and not isinstance(
                pending,
                PendingStartupSettingsRecord,
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "managed pending settings is not generation-tagged"
                )
            settings_bytes = pending.desired_bytes if pending is not None else None
            if (
                settings_bytes is not None
                and len(settings_bytes) > _MAX_SETTINGS_BYTES
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "pending settings exceeds size limit"
                )
            cli_toolsets = tuple(config._resolve_cli_toolsets(strict=True))
            if not _startup_mutations_are_admitted():
                raise ManagedStartupConfigurationAdmissionError(
                    "configuration snapshot admission changed"
                )
            if any(
                not isinstance(value, str) or not value
                for value in cli_toolsets
            ):
                raise ManagedStartupConfigurationUnavailable(
                    "resolved CLI toolsets are invalid"
                )
            settings_path = _canonical_absolute_path(config.SETTINGS_FILE)
            if pending is not None and pending.path != str(settings_path):
                raise ManagedStartupConfigurationUnavailable(
                    "pending settings path does not match configured target"
                )
    except (
        ManagedStartupConfigurationAdmissionError,
        ManagedStartupConfigurationUnavailable,
    ):
        raise
    except Exception as exc:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration desired state could not be captured"
        ) from exc
    settings_digest = (
        hashlib.sha256(settings_bytes).hexdigest()
        if settings_bytes is not None
        else None
    )
    return _DesiredConfiguration(
        epoch,
        binding,
        settings_path,
        pending,
        settings_bytes,
        settings_digest,
        cli_toolsets,
        _desired_digest(binding, settings_path, pending, cli_toolsets),
    )


def _state_from_journal(
    value: dict,
    evidence: SettingsFileEvidence,
    *,
    epoch: ProcessEpoch,
    binding: ReleaseBinding,
    configured_settings_path: Path,
    journal_path: Path,
) -> _ConfigurationState:
    try:
        journal_binding = ReleaseBinding(
            value["transaction_id"],
            value["manifest_sha256"],
        )
        settings_path = _canonical_absolute_path(value["settings_path"])
        pending = _pending_from_json(value["pending"])
        cli_toolsets = tuple(value["cli_toolsets"])
        desired_sha256 = _desired_digest(
            journal_binding,
            settings_path,
            pending,
            cli_toolsets,
        )
        if (
            journal_binding != binding
            or settings_path != configured_settings_path
            or desired_sha256 != value["desired_sha256"]
        ):
            raise ManagedStartupConfigurationAmbiguous(
                "startup configuration journal binding changed"
            )
        settings_receipt = _settings_receipt_from_json(value["settings_receipt"])
        replace_preimage = (
            _evidence_from_json(value["replace_preimage"])
            if value["replace_preimage"] is not None
            else None
        )
        planned_postimage = (
            _evidence_from_json(value["planned_postimage"])
            if value["planned_postimage"] is not None
            else None
        )
        phase_index = _JOURNAL_PHASES.index(value["phase"])
        if pending is None:
            if replace_preimage is not None or planned_postimage is not None:
                raise ValueError("absent settings has retry evidence")
        else:
            if replace_preimage != pending.source:
                raise ValueError("replace preimage is not the captured source")
            if planned_postimage is not None and (
                not planned_postimage.exists
                or planned_postimage.mode != 0o600
                or planned_postimage.owner != os.getuid()
                or planned_postimage.nlink != 1
                or planned_postimage.size != len(pending.desired_bytes)
                or planned_postimage.sha256 != pending.desired_sha256
            ):
                raise ValueError("planned postimage is invalid")
        cli_receipt = value["cli_receipt"]
        if phase_index >= _JOURNAL_PHASES.index("settings-durable"):
            if settings_receipt is None:
                raise ValueError("durable phase lacks settings receipt")
            if pending is not None and (
                settings_receipt.status != "proved-complete"
                or settings_receipt.path != str(settings_path)
                or settings_receipt.sha256 != pending.desired_sha256
                or settings_receipt.mode != 0o600
                or settings_receipt.size != len(pending.desired_bytes)
                or settings_receipt.generation != pending.generation
                or settings_receipt.source != pending.source
                or settings_receipt.post is None
                or settings_receipt.post.sha256 != pending.desired_sha256
                or settings_receipt.post.mode != 0o600
                or (
                    planned_postimage is not None
                    and settings_receipt.post != planned_postimage
                )
                or settings_receipt.device != settings_receipt.post.device
                or settings_receipt.inode != settings_receipt.post.inode
                or settings_receipt.schema_valid is not True
            ):
                raise ValueError("settings receipt is inconsistent")
            if pending is None and (
                settings_receipt.status != "proved-absent"
                or settings_receipt.path != str(settings_path)
                or settings_receipt.sha256 is not None
                or settings_receipt.size != 0
            ):
                raise ValueError("absent settings receipt is inconsistent")
        elif settings_receipt is not None:
            raise ValueError("intent phase has a settings receipt")
        if value["pending_consumed"] != (
            phase_index >= _JOURNAL_PHASES.index("pending-consumed")
        ):
            raise ValueError("pending-consumption phase is inconsistent")
        if phase_index >= _JOURNAL_PHASES.index("cli-published"):
            if not isinstance(cli_receipt, dict) or set(cli_receipt) != {
                "process_epoch",
                "toolsets",
                "generation",
                "sha256",
                "publication_id",
            }:
                raise ValueError("CLI receipt schema is invalid")
            stored_epoch = cli_receipt["process_epoch"]
            if (
                not isinstance(stored_epoch, dict)
                or set(stored_epoch) != {"pid", "start_token"}
                or isinstance(stored_epoch["pid"], bool)
                or not isinstance(stored_epoch["pid"], int)
                or stored_epoch["pid"] <= 1
                or not isinstance(stored_epoch["start_token"], str)
                or not stored_epoch["start_token"]
                or tuple(cli_receipt["toolsets"]) != cli_toolsets
                or cli_receipt["sha256"] != _cli_digest(cli_toolsets)
                or isinstance(cli_receipt["generation"], bool)
                or not isinstance(cli_receipt["generation"], int)
                or cli_receipt["generation"] < 0
                or isinstance(cli_receipt["publication_id"], bool)
                or not isinstance(cli_receipt["publication_id"], int)
                or cli_receipt["publication_id"] <= 0
            ):
                raise ValueError("CLI receipt is invalid")
        elif cli_receipt is not None:
            raise ValueError("pre-publication phase has a CLI receipt")
    except ManagedStartupConfigurationError:
        raise
    except Exception as exc:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration journal schema is invalid"
        ) from exc
    desired = _DesiredConfiguration(
        epoch,
        binding,
        settings_path,
        pending,
        pending.desired_bytes if pending is not None else None,
        pending.desired_sha256 if pending is not None else None,
        cli_toolsets,
        desired_sha256,
    )
    phase = value["phase"]
    state = _ConfigurationState(
        desired=desired,
        settings_receipt=settings_receipt,
        pending_consumed=bool(value["pending_consumed"]),
        operation_phase=phase,
        replace_preimage=replace_preimage,
        planned_postimage=planned_postimage,
        journal_path=journal_path,
        journal_evidence=evidence,
    )
    # A CLI publication receipt is process-local. A reopened process must
    # publish into its own StableCliToolsets instance and write a fresh receipt.
    return state


def _publish_cli_toolsets(
    config: ModuleType,
    desired: tuple[str, ...],
    publication: StableCliToolsets | None = None,
    *,
    force_generation: bool = False,
) -> StableCliToolsets:
    _require_admission("CLI toolset publication")
    current = config.CLI_TOOLSETS
    if publication is not None and current is not publication:
        raise ManagedStartupConfigurationAmbiguous(
            "CLI publication identity was replaced"
        )
    if publication is None:
        publication = current
        if not isinstance(publication, StableCliToolsets):
            publication = StableCliToolsets(tuple(publication))
            config.CLI_TOOLSETS = publication
    publication.publish(desired, force_generation=force_generation)
    return publication


def _settings_complete(state: _ConfigurationState) -> bool:
    desired = state.desired
    if desired.settings_bytes is None:
        return (
            state.settings_receipt is not None
            and state.settings_receipt.status == "proved-absent"
        )
    if state.settings_receipt is None:
        return False
    with _open_settings_parent(desired.settings_path) as parent:
        parent_current = _parent_evidence(parent)
        current = _file_evidence(
            _read_bounded_twice(parent, desired.settings_path.name)
        )
    return (
        current == state.settings_receipt.post
        and parent_current == state.settings_receipt.parent
        and current.sha256 == desired.settings_sha256
        and state.settings_receipt.schema_valid
    )


def _cli_complete(config: ModuleType, state: _ConfigurationState) -> bool:
    if state.cli_publication is None:
        return False
    values, generation, digest = state.cli_publication.snapshot()
    complete = (
        config.CLI_TOOLSETS is state.cli_publication
        and values == state.desired.cli_toolsets
        and digest == _cli_digest(state.desired.cli_toolsets)
    )
    if complete and state.receipt is not None:
        complete = (
            state.receipt.cli.publication_id == id(state.cli_publication)
            and state.receipt.cli.generation == generation
            and state.receipt.cli.sha256 == digest
        )
    return complete


def _pending_slot_status(config: ModuleType, state: _ConfigurationState) -> str:
    with config._DEFERRED_STARTUP_CONFIG_LOCK:
        pending = config._DEFERRED_STARTUP_SETTINGS_TEXT
    if isinstance(pending, PendingStartupSettingsFailure):
        return "capture-failed"
    if pending is None:
        return "consumed" if state.pending_consumed else "missing"
    desired = state.desired.pending
    if not isinstance(pending, PendingStartupSettingsRecord):
        return "invalid"
    if desired is None:
        return "newer"
    if pending == desired:
        return "expected"
    if pending.generation > desired.generation:
        return "newer"
    return "foreign"


def _retry_safe_partial(state: _ConfigurationState) -> bool:
    desired = state.desired
    if (
        state.operation_phase != "intent"
        or desired.pending is None
        or desired.settings_bytes is None
    ):
        return False
    try:
        with _open_settings_parent(desired.settings_path) as parent:
            current = _file_evidence(
                _read_bounded_twice(
                    parent,
                    desired.settings_path.name,
                    allow_legacy_mode=True,
                )
            )
    except ManagedStartupConfigurationUnavailable:
        return False
    source = desired.pending.source
    exact_legacy_hardening = (
        source.exists
        and source.mode == 0o644
        and current.exists
        and current.mode == 0o600
        and (
            current.device,
            current.inode,
            current.owner,
            current.nlink,
            current.size,
            current.sha256,
        )
        == (
            source.device,
            source.inode,
            source.owner,
            source.nlink,
            source.size,
            source.sha256,
        )
    )
    return (
        current == source
        or current == state.replace_preimage
        or exact_legacy_hardening
        or (
        state.planned_postimage is not None
        and current == state.planned_postimage
        and current.mode == 0o600
        and current.sha256 == desired.pending.desired_sha256
        )
    )


def _verification(
    config: ModuleType,
    state: _ConfigurationState,
) -> ManagedStartupConfigurationVerification:
    try:
        pending_status = _pending_slot_status(config, state)
        if pending_status in {"newer", "foreign", "invalid", "capture-failed"}:
            return ManagedStartupConfigurationVerification(
                ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
                state.receipt,
                False,
                _cli_complete(config, state),
                f"configuration_pending_slot_{pending_status}",
            )
        settings_complete = (
            _settings_complete(state)
            and state.pending_consumed
            and pending_status == "consumed"
        )
        cli_complete = _cli_complete(config, state)
    except ManagedStartupConfigurationUnavailable:
        return ManagedStartupConfigurationVerification(
            ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
            state.receipt,
            False,
            _cli_complete(config, state),
            "configuration_postcondition_unobservable",
        )
    if settings_complete and cli_complete and state.receipt is not None:
        return ManagedStartupConfigurationVerification(
            ManagedStartupConfigurationVerificationOutcome.PROVED_COMPLETE,
            state.receipt,
            True,
            True,
            None,
        )
    if _retry_safe_partial(state):
        return ManagedStartupConfigurationVerification(
            ManagedStartupConfigurationVerificationOutcome.PROVED_RETRY_SAFE_PARTIAL,
            state.receipt,
            settings_complete,
            cli_complete,
            "settings_replace_intent_has_exact_retry_safe_preimage",
        )
    return ManagedStartupConfigurationVerification(
        ManagedStartupConfigurationVerificationOutcome.PARTIAL,
        state.receipt,
        settings_complete,
        cli_complete,
        "configuration_postcondition_incomplete",
    )


def apply_managed_startup_configuration(
    *,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
    crash_hook=None,
) -> ManagedStartupConfigurationReceipt:
    """Apply one stable settings/CLI snapshot for the current process epoch."""

    global _STATE
    if not _startup_mutations_are_admitted():
        raise ManagedStartupConfigurationAdmissionError(
            "startup configuration requires admitted startup"
        )
    epoch = _current_process_epoch()
    if epoch is None:
        raise ManagedStartupConfigurationUnavailable(
            "startup configuration process epoch is unavailable"
        )
    config = _load_config_module()
    binding = _configured_release_binding(
        config,
        transaction_id,
        manifest_sha256,
    )
    journal_path = _configured_journal_path()
    configured_settings_path = _canonical_absolute_path(config.SETTINGS_FILE)

    def persisted(phase: str) -> None:
        _write_journal(journal_path, _STATE, crash_hook=crash_hook)
        if crash_hook is not None:
            crash_hook(phase)

    def planned(postimage: SettingsFileEvidence) -> None:
        _STATE.planned_postimage = postimage
        _write_journal(journal_path, _STATE, crash_hook=crash_hook)

    with _STATE_LOCK:
        with _journal_lock(journal_path, crash_hook=crash_hook):
            _clean_journal_orphans(journal_path)
            if _STATE is not None and _STATE.desired.process_epoch != epoch:
                _STATE = None
            journal, journal_evidence = _read_journal(journal_path)
            if _STATE is None:
                if journal is None:
                    desired = _capture_desired(config, epoch, binding)
                    _STATE = _ConfigurationState(
                        desired=desired,
                        operation_phase="intent",
                        replace_preimage=(
                            desired.pending.source if desired.pending else None
                        ),
                        journal_path=journal_path,
                        journal_evidence=journal_evidence,
                    )
                    persisted("intent")
                else:
                    _STATE = _state_from_journal(
                        journal,
                        journal_evidence,
                        epoch=epoch,
                        binding=binding,
                        configured_settings_path=configured_settings_path,
                        journal_path=journal_path,
                    )
            else:
                if (
                    _STATE.desired.release_binding != binding
                    or _STATE.journal_path != journal_path
                    or journal_evidence != _STATE.journal_evidence
                ):
                    raise ManagedStartupConfigurationAmbiguous(
                        "startup configuration release or journal binding changed"
                    )

            current = _verification(config, _STATE)
            if (
                current.outcome
                is ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS
            ):
                raise ManagedStartupConfigurationAmbiguous(
                    current.reason or "startup configuration is ambiguous"
                )
            if (
                current.outcome
                is ManagedStartupConfigurationVerificationOutcome.PROVED_COMPLETE
            ):
                return _STATE.receipt
            if (
                _STATE.operation_phase == "complete"
                and current.outcome
                is ManagedStartupConfigurationVerificationOutcome.PARTIAL
                and not _settings_complete(_STATE)
            ):
                raise ManagedStartupConfigurationAmbiguous(
                    current.reason or "completed configuration drifted"
                )

            desired = _STATE.desired
            if desired.settings_bytes is None:
                _STATE.settings_receipt = DurableSettingsReceipt(
                    "proved-absent",
                    str(desired.settings_path),
                    None,
                    None,
                    None,
                    None,
                    0,
                )
                _STATE.pending_consumed = True
                _STATE.operation_phase = "settings-durable"
                persisted("settings-durable")
            elif not _settings_complete(_STATE):
                if desired.pending is None:
                    raise ManagedStartupConfigurationUnavailable(
                        "durable settings mutation lacks typed pending record"
                    )
                _STATE.settings_receipt = _atomic_write_settings(
                    desired.settings_path,
                    desired.settings_bytes,
                    pending=desired.pending,
                    force_replace=_STATE.settings_receipt is not None,
                    allow_retry_safe_desired=_STATE.operation_phase != "complete",
                    retry_preimage=_STATE.replace_preimage,
                    retry_postimage=_STATE.planned_postimage,
                    record_preimage=lambda evidence: setattr(
                        _STATE,
                        "replace_preimage",
                        evidence,
                    ),
                    record_postimage=planned,
                    crash_hook=crash_hook,
                )
                _STATE.operation_phase = "settings-durable"
                persisted("settings-durable")

            if desired.settings_bytes is not None and not _STATE.pending_consumed:
                with config._DEFERRED_STARTUP_CONFIG_LOCK:
                    pending = config._DEFERRED_STARTUP_SETTINGS_TEXT
                    if isinstance(pending, PendingStartupSettingsFailure):
                        raise ManagedStartupConfigurationUnavailable(
                            "managed pending settings capture failed"
                        )
                    if isinstance(pending, PendingStartupSettingsRecord):
                        if pending != desired.pending:
                            raise ManagedStartupConfigurationAmbiguous(
                                "pending settings changed after snapshot"
                            )
                        _require_admission("pending settings consumption")
                        config._DEFERRED_STARTUP_SETTINGS_TEXT = None
                        if crash_hook is not None:
                            crash_hook("pending-cleared")
                    elif pending is not None:
                        raise ManagedStartupConfigurationAmbiguous(
                            "pending settings changed after snapshot"
                        )
                    # None is retry-safe after reopen: the journal durably binds
                    # the exact generation and the settings postimage is proved.
                    _STATE.pending_consumed = True
                _STATE.operation_phase = "pending-consumed"
                persisted("pending-consumed")

            if not _cli_complete(config, _STATE):
                durable_was_complete = _STATE.operation_phase == "complete"
                try:
                    _STATE.cli_publication = _publish_cli_toolsets(
                        config,
                        desired.cli_toolsets,
                        _STATE.cli_publication,
                        force_generation=_STATE.receipt is not None,
                    )
                    if crash_hook is not None:
                        crash_hook("cli-published-unrecorded")
                except (
                    ManagedStartupConfigurationAmbiguous,
                    ManagedStartupConfigurationAdmissionError,
                ):
                    raise
                except Exception as exc:
                    raise ManagedStartupConfigurationMutationError(
                        "cli publication failed",
                        retry=ManagedStartupConfigurationRetry.TERMINAL,
                    ) from exc
                if not durable_was_complete:
                    _STATE.operation_phase = "cli-published"
                    persisted("cli-published")

            _STATE.receipt = ManagedStartupConfigurationReceipt(
                desired.process_epoch,
                desired.release_binding,
                desired.desired_sha256,
                _STATE.settings_receipt,
                ProcessCliToolsetsReceipt(
                    desired.cli_toolsets,
                    id(_STATE.cli_publication),
                    _STATE.cli_publication.snapshot()[1],
                    _STATE.cli_publication.snapshot()[2],
                ),
            )
            _STATE.operation_phase = "complete"
            persisted("complete")
            verified = _verification(config, _STATE)
            if (
                verified.outcome
                is not ManagedStartupConfigurationVerificationOutcome.PROVED_COMPLETE
            ):
                raise ManagedStartupConfigurationMutationError(
                    verified.reason or "configuration postcondition was not exact",
                    retry=ManagedStartupConfigurationRetry.TERMINAL,
                )
            return _STATE.receipt


def verify_managed_startup_configuration(
    receipt: ManagedStartupConfigurationReceipt | None = None,
) -> ManagedStartupConfigurationVerification:
    epoch = _current_process_epoch()
    if epoch is None:
        return ManagedStartupConfigurationVerification(
            ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
            None,
            False,
            False,
            "process_epoch_unavailable",
        )
    try:
        config = _load_config_module()
    except ManagedStartupConfigurationUnavailable:
        return ManagedStartupConfigurationVerification(
            ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
            None,
            False,
            False,
            "configuration_module_unavailable",
        )
    with _STATE_LOCK:
        if _STATE is None:
            if receipt is None:
                return ManagedStartupConfigurationVerification(
                    ManagedStartupConfigurationVerificationOutcome.PROVED_ABSENT,
                    None,
                    False,
                    False,
                    "managed_configuration_not_installed",
                )
            reason = (
                "configuration_receipt_from_foreign_epoch"
                if receipt.process_epoch != epoch
                else "configuration_receipt_without_state"
            )
            return ManagedStartupConfigurationVerification(
                ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
                receipt,
                False,
                False,
                reason,
            )
        if _STATE.desired.process_epoch != epoch:
            return ManagedStartupConfigurationVerification(
                ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
                _STATE.receipt,
                False,
                False,
                "managed_configuration_from_foreign_epoch",
            )
        if receipt is not None and receipt != _STATE.receipt:
            return ManagedStartupConfigurationVerification(
                ManagedStartupConfigurationVerificationOutcome.AMBIGUOUS,
                _STATE.receipt,
                False,
                False,
                "configuration_receipt_mismatch",
            )
        return _verification(config, _STATE)


def _reset_after_fork() -> None:
    global _STATE_LOCK, _STATE
    for publication in tuple(_STABLE_CLI_INSTANCES.values()):
        publication._reset_lock_after_fork()
    _STATE_LOCK = threading.Lock()
    _STATE = None


def _reset_managed_startup_configuration_for_tests() -> None:
    global _STATE, _JOURNAL_PATH_OVERRIDE
    with _STATE_LOCK:
        _STATE = None
        _JOURNAL_PATH_OVERRIDE = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
