"""Strict receipts and stable store snapshots for managed continuation recovery."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from api.process_identity import process_start_token

MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_RECEIPTS = 4096
_EPOCH_LOCK = threading.Lock()
_EPOCH: tuple[int, str, str] | None = None
_AUTHORITY: ContextVar["_StoreAuthority | None"] = ContextVar(
    "managed_continuation_authority", default=None
)


class ManagedContinuationOutcome(str, Enum):
    ABSENT = "ABSENT"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class StoreIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "StoreIdentity":
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )


@dataclass(frozen=True)
class ManagedContinuationRecoveryReceipt:
    outcome: ManagedContinuationOutcome
    store_path: str
    store_identity_before: StoreIdentity | None
    store_sha256_before: str | None
    store_identity_after: StoreIdentity | None
    store_sha256_after: str | None
    transaction_id: str
    manifest_sha256: str
    process_pid: int
    process_start_token: str
    process_epoch: str
    receipt_classifications: tuple[tuple[str, str], ...] = ()
    receipt_bindings: tuple[tuple[str, str, str, str], ...] = ()
    started_receipt_keys: tuple[str, ...] = ()
    retryable_receipt_keys: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": (
                "complete"
                if self.outcome
                in {
                    ManagedContinuationOutcome.ABSENT,
                    ManagedContinuationOutcome.COMPLETE,
                }
                else self.outcome.value.lower()
            ),
            "outcome": self.outcome.value,
            "store_path": self.store_path,
            "store_identity_before": (
                asdict(self.store_identity_before)
                if self.store_identity_before is not None
                else None
            ),
            "store_sha256_before": self.store_sha256_before,
            "store_identity_after": (
                asdict(self.store_identity_after)
                if self.store_identity_after is not None
                else None
            ),
            "store_sha256_after": self.store_sha256_after,
            "transaction_id": self.transaction_id,
            "manifest_sha256": self.manifest_sha256,
            "process_pid": self.process_pid,
            "process_start_token": self.process_start_token,
            "process_epoch": self.process_epoch,
            "receipt_classifications": self.receipt_classifications,
            "receipt_bindings": self.receipt_bindings,
            "started_receipt_keys": self.started_receipt_keys,
            "retryable_receipt_keys": self.retryable_receipt_keys,
            "errors": self.errors,
        }


@dataclass(frozen=True)
class ManagedContinuationVerificationReceipt:
    outcome: ManagedContinuationOutcome
    store_path: str
    store_identity: StoreIdentity | None
    store_sha256: str | None
    transaction_id: str
    manifest_sha256: str
    process_pid: int
    process_start_token: str
    process_epoch: str
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _StoreAuthority:
    parent: Path
    parent_fd: int
    parent_identity: StoreIdentity


def _validate_private_directory(value: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise RuntimeError(
            "managed continuation parent must be owner-held mode 0700"
        )


def _validate_private_file(value: os.stat_result, *, kind: str) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        raise RuntimeError(
            f"managed continuation {kind} must be owner-held mode 0600"
        )


def _check_parent_rebind(authority: _StoreAuthority) -> None:
    opened = os.fstat(authority.parent_fd)
    named = os.stat(authority.parent, follow_symlinks=False)
    _validate_private_directory(opened)
    _validate_private_directory(named)
    expected = (
        authority.parent_identity.device,
        authority.parent_identity.inode,
    )
    if (
        (opened.st_dev, opened.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise RuntimeError("managed continuation parent path was replaced")


@contextmanager
def strict_store_lock(
    path: Path,
    thread_lock: threading.RLock,
    *,
    create: bool = True,
):
    """Hold the exact private named lock inode through a parent dirfd."""
    path = Path(path)
    if (
        os.name == "nt"
        or not getattr(os, "O_NOFOLLOW", 0)
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
    ):
        raise RuntimeError("managed continuation locking requires POSIX openat")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    lock_flags = (
        os.O_RDWR
        | (os.O_CREAT if create else 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    import fcntl

    with thread_lock:
        parent_fd = os.open(path.parent, directory_flags)
        descriptor = -1
        authority = None
        authority_token = None
        try:
            parent_stat = os.fstat(parent_fd)
            _validate_private_directory(parent_stat)
            authority = _StoreAuthority(
                path.parent,
                parent_fd,
                StoreIdentity.from_stat(parent_stat),
            )
            _check_parent_rebind(authority)
            created = False
            try:
                descriptor = os.open(
                    path.name, lock_flags & ~os.O_CREAT, dir_fd=parent_fd
                )
            except FileNotFoundError:
                if not create:
                    raise
                descriptor = os.open(
                    path.name,
                    lock_flags | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                created = True
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                os.fsync(parent_fd)
            opened = os.fstat(descriptor)
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            _validate_private_file(opened, kind="lock")
            if (
                StoreIdentity.from_stat(opened) != StoreIdentity.from_stat(named)
            ):
                raise RuntimeError("managed continuation lock identity is invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = os.fstat(descriptor)
            named_locked = os.stat(
                path.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                StoreIdentity.from_stat(locked)
                != StoreIdentity.from_stat(named_locked)
                or stat.S_IMODE(locked.st_mode) != 0o600
            ):
                raise RuntimeError(
                    "managed continuation lock was replaced during acquisition"
                )
            _check_parent_rebind(authority)
            authority_token = _AUTHORITY.set(authority)
            try:
                yield
                _check_parent_rebind(authority)
                named_after = os.stat(
                    path.name, dir_fd=parent_fd, follow_symlinks=False
                )
                if StoreIdentity.from_stat(os.fstat(descriptor)) != (
                    StoreIdentity.from_stat(named_after)
                ):
                    raise RuntimeError(
                        "managed continuation lock was replaced while held"
                    )
            finally:
                if authority_token is not None:
                    _AUTHORITY.reset(authority_token)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)


def managed_process_epoch() -> tuple[int, str, str]:
    global _EPOCH
    pid = os.getpid()
    token = process_start_token(pid)
    if not token:
        raise RuntimeError("managed continuation process identity unavailable")
    with _EPOCH_LOCK:
        if _EPOCH is None or _EPOCH[:2] != (pid, token):
            _EPOCH = (pid, token, f"continuation_{uuid.uuid4().hex}")
        return _EPOCH


def _current_managed_process_epoch() -> tuple[int, str, str] | None:
    pid = os.getpid()
    token = process_start_token(pid)
    with _EPOCH_LOCK:
        if _EPOCH is None or _EPOCH[:2] != (pid, token):
            return None
        return _EPOCH


def validate_binding(transaction_id: str, manifest_sha256: str) -> None:
    if (
        not isinstance(transaction_id, str)
        or not transaction_id
        or len(transaction_id.encode("utf-8")) > 512
    ):
        raise ValueError("managed continuation transaction identity is invalid")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest_sha256)
    ):
        raise ValueError("managed continuation manifest digest is invalid")


def stable_store_snapshot(
    path: Path,
    *,
    max_bytes: int = MAX_STORE_BYTES,
) -> tuple[dict, StoreIdentity | None, str | None]:
    """Read JSON and hash from one bounded, no-follow regular-file descriptor."""
    path = Path(path)
    authority = _AUTHORITY.get()
    if authority is None or authority.parent != path.parent:
        raise RuntimeError("managed continuation store authority is not held")
    _check_parent_rebind(authority)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | os.O_NOFOLLOW
    )
    try:
        descriptor = os.open(path.name, flags, dir_fd=authority.parent_fd)
    except FileNotFoundError:
        return {"version": 1, "receipts": {}}, None, None
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        _validate_private_file(opened, kind="store")
        if opened.st_size > max_bytes:
            raise ValueError("managed continuation store is not a bounded file")
        payload = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    current = os.stat(
        path.name, dir_fd=authority.parent_fd, follow_symlinks=False
    )
    _check_parent_rebind(authority)
    opened_identity = StoreIdentity.from_stat(opened)
    if (
        len(payload) > max_bytes
        or opened_identity != StoreIdentity.from_stat(after)
        or opened_identity != StoreIdentity.from_stat(current)
    ):
        raise ValueError("managed continuation store changed during snapshot")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("managed continuation store JSON is malformed") from exc
    return value, opened_identity, hashlib.sha256(payload).hexdigest()


def strict_store_save(path: Path, store: dict) -> None:
    """Atomically persist a managed store through its held private dirfd."""
    path = Path(path)
    authority = _AUTHORITY.get()
    if authority is None or authority.parent != path.parent:
        raise RuntimeError("managed continuation store authority is not held")
    _check_parent_rebind(authority)
    payload = json.dumps(
        store,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_STORE_BYTES:
        raise ValueError("managed continuation store exceeds bounded size")
    try:
        current = os.stat(
            path.name, dir_fd=authority.parent_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        current = None
    if current is not None:
        _validate_private_file(current, kind="store")
    temporary = f".{path.name}.managed-{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=authority.parent_fd,
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("managed continuation store write made no progress")
            offset += written
        os.fsync(descriptor)
        temporary_stat = os.fstat(descriptor)
        _validate_private_file(temporary_stat, kind="temporary store")
        named_temporary = os.stat(
            temporary, dir_fd=authority.parent_fd, follow_symlinks=False
        )
        if StoreIdentity.from_stat(temporary_stat) != StoreIdentity.from_stat(
            named_temporary
        ):
            raise RuntimeError(
                "managed continuation temporary store was replaced"
            )
        _check_parent_rebind(authority)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=authority.parent_fd,
            dst_dir_fd=authority.parent_fd,
        )
        os.fsync(authority.parent_fd)
        published = os.stat(
            path.name, dir_fd=authority.parent_fd, follow_symlinks=False
        )
        _validate_private_file(published, kind="store")
        if published.st_size != len(payload):
            raise RuntimeError("managed continuation store publication is invalid")
        _check_parent_rebind(authority)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=authority.parent_fd)
        except FileNotFoundError:
            pass


def verify_exact(
    receipt: ManagedContinuationRecoveryReceipt,
    *,
    path: Path,
    store_lock,
    validate_store,
    session_id_for,
    terminal_states: set[str],
    transaction_id: str,
    manifest_sha256: str,
    max_receipts: int,
    process_token_lookup,
) -> ManagedContinuationVerificationReceipt:
    """Read-only verification of one exact recovery postcondition."""
    identity = None
    digest = None
    pid = 0
    token = ""
    epoch = ""
    errors = []
    outcome = ManagedContinuationOutcome.AMBIGUOUS
    try:
        validate_binding(transaction_id, manifest_sha256)
        if type(receipt) is not ManagedContinuationRecoveryReceipt:
            raise ValueError("managed continuation verification receipt is invalid")
        current = _current_managed_process_epoch()
        if current is not None:
            pid, token, epoch = current
        if (
            receipt.transaction_id != transaction_id
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.store_path != os.fspath(path)
            or receipt.outcome
            not in {
                ManagedContinuationOutcome.ABSENT,
                ManagedContinuationOutcome.COMPLETE,
                ManagedContinuationOutcome.PARTIAL,
            }
        ):
            raise ValueError("managed continuation verification receipt mismatch")
        with store_lock():
            store, identity, digest = stable_store_snapshot(path)
            receipts = validate_store(store, max_receipts=max_receipts)
            if (
                identity != receipt.store_identity_after
                or digest != receipt.store_sha256_after
            ):
                raise ValueError(
                    "managed continuation verification store changed"
                )
            classifications = []
            bindings = []
            retryable = []
            for key, row in sorted(receipts.items()):
                state = row["state"]
                session_id = session_id_for(row)
                if state == "claimed":
                    classifications.append((key, "claimed_retryable"))
                    retryable.append(key)
                elif state == "starting":
                    actual = process_token_lookup(row["owner_pid"])
                    if actual == row["owner_start_token"]:
                        classifications.append((key, "live_owner_starting"))
                        retryable.append(key)
                        bindings.append(
                            (key, session_id, "", row["start_token"])
                        )
                    elif actual is not None:
                        raise ValueError(
                            f"{key}: verification PID identity mismatch"
                        )
                    elif row["launch_phase"] == "launching":
                        raise ValueError(
                            f"{key}: verification launch state is ambiguous"
                        )
                    else:
                        classifications.append(
                            (key, "dead_owner_starting_retryable")
                        )
                        retryable.append(key)
                elif state == "started":
                    if "completed_start_token" not in row:
                        classifications.append((key, "started_legacy_inert"))
                    else:
                        classifications.append((key, "started_exact"))
                        bindings.append(
                            (
                                key,
                                session_id,
                                row["child_stream_id"],
                                row["completed_start_token"],
                            )
                        )
                elif state in terminal_states:
                    classifications.append((key, f"terminal_{state}"))
                    bindings.append((key, session_id, "", ""))
                else:
                    raise ValueError(f"{key}: verification state is invalid")
            expected_outcome = (
                ManagedContinuationOutcome.ABSENT
                if not receipts
                else (
                    ManagedContinuationOutcome.PARTIAL
                    if retryable
                    else ManagedContinuationOutcome.COMPLETE
                )
            )
            if (
                expected_outcome is not receipt.outcome
                or tuple(sorted(classifications))
                != receipt.receipt_classifications
                or tuple(sorted(bindings)) != receipt.receipt_bindings
                or tuple(sorted(set(retryable)))
                != receipt.retryable_receipt_keys
            ):
                raise ValueError(
                    "managed continuation verification postcondition mismatch"
                )
            outcome = expected_outcome
    except Exception as exc:
        errors.append(str(exc))
        outcome = ManagedContinuationOutcome.AMBIGUOUS
    return ManagedContinuationVerificationReceipt(
        outcome=outcome,
        store_path=os.fspath(path),
        store_identity=identity,
        store_sha256=digest,
        transaction_id=str(transaction_id or ""),
        manifest_sha256=str(manifest_sha256 or ""),
        process_pid=pid,
        process_start_token=token,
        process_epoch=epoch,
        errors=tuple(errors),
    )


def recover_exact(
    *,
    path: Path,
    store_lock,
    validate_store,
    start_one,
    session_id_for,
    terminal_states: set[str],
    transaction_id: str,
    manifest_sha256: str,
    max_receipts: int,
    process_token_lookup,
) -> ManagedContinuationRecoveryReceipt:
    """Enumerate, classify, and recover one continuation receipt authority."""
    before_identity = None
    before_sha = None
    after_identity = None
    after_sha = None
    classifications: list[tuple[str, str]] = []
    bindings: list[tuple[str, str, str, str]] = []
    started: list[str] = []
    retryable: list[str] = []
    errors: list[str] = []
    pid = 0
    token = ""
    epoch = ""
    try:
        validate_binding(transaction_id, manifest_sha256)
        pid, token, epoch = managed_process_epoch()

        def classify(rows):
            row_classifications = []
            row_bindings = []
            row_retryable = []
            row_eligible = []
            for key, receipt in sorted(rows.items()):
                state = receipt["state"]
                session_id = session_id_for(receipt)
                if state == "claimed":
                    row_classifications.append((key, "claimed_retryable"))
                    row_retryable.append(key)
                    row_eligible.append(key)
                elif state == "starting":
                    owner_pid = receipt["owner_pid"]
                    expected = receipt["owner_start_token"]
                    actual = process_token_lookup(owner_pid)
                    phase = receipt["launch_phase"]
                    if actual == expected:
                        row_classifications.append((key, "live_owner_starting"))
                        row_retryable.append(key)
                        row_bindings.append(
                            (key, session_id, "", receipt["start_token"])
                        )
                    elif actual is not None:
                        raise ValueError(
                            f"{key}: starting owner PID identity mismatch"
                        )
                    elif phase == "launching":
                        raise ValueError(
                            f"{key}: launch-before-started-write is ambiguous"
                        )
                    else:
                        row_classifications.append(
                            (key, "dead_owner_starting_retryable")
                        )
                        row_retryable.append(key)
                        row_eligible.append(key)
                elif state == "started":
                    if "completed_start_token" not in receipt:
                        row_classifications.append(
                            (key, "started_legacy_inert")
                        )
                    else:
                        row_classifications.append((key, "started_exact"))
                        row_bindings.append(
                            (
                                key,
                                session_id,
                                receipt["child_stream_id"],
                                receipt["completed_start_token"],
                            )
                        )
                elif state in terminal_states:
                    row_classifications.append((key, f"terminal_{state}"))
                    row_bindings.append((key, session_id, "", ""))
                else:
                    raise ValueError(f"{key}: unknown continuation state")
            return (
                row_classifications,
                row_bindings,
                row_retryable,
                row_eligible,
            )

        with store_lock():
            store, before_identity, before_sha = stable_store_snapshot(path)
            receipts = validate_store(store, max_receipts=max_receipts)
        initial_classifications, _initial_bindings, _initial_retryable, eligible = (
            classify(receipts)
        )
        initial_classification_by_key = dict(initial_classifications)

        for key in eligible:
            result, did_start = start_one(key)
            if not isinstance(result, dict):
                raise ValueError(f"{key}: recovery returned no durable receipt")
            state = result.get("state")
            if did_start and state == "started":
                started.append(key)
            elif state != "claimed" and state not in terminal_states:
                raise ValueError(f"{key}: recovery post-state is ambiguous")

        with store_lock():
            final_store, after_identity, after_sha = stable_store_snapshot(path)
            final_receipts = validate_store(
                final_store, max_receipts=max_receipts
            )
            if set(final_receipts) != set(receipts):
                raise ValueError(
                    "managed continuation receipt set changed during recovery"
                )
            for key, before in receipts.items():
                after = final_receipts[key]
                if session_id_for(before) != session_id_for(after):
                    raise ValueError(
                        f"{key}: continuation session identity changed"
                    )
                before_state = before["state"]
                after_state = after["state"]
                if before_state == "started" and (
                    after_state != "started"
                    or after["child_stream_id"] != before["child_stream_id"]
                    or after.get("completed_start_token")
                    != before.get("completed_start_token")
                ):
                    raise ValueError(
                        f"{key}: started continuation regressed"
                    )
                if before_state in terminal_states and after_state != before_state:
                    raise ValueError(
                        f"{key}: terminal continuation changed"
                    )
                if (
                    initial_classification_by_key.get(key)
                    == "live_owner_starting"
                    and after_state == "claimed"
                ):
                    raise ValueError(
                        f"{key}: live owner continuation regressed"
                    )
                if (
                    initial_classification_by_key.get(key)
                    == "live_owner_starting"
                    and after_state == "started"
                    and (
                    after["completed_start_token"] != before["start_token"]
                    )
                ):
                    raise ValueError(
                        f"{key}: continuation start token changed"
                    )
            classifications, bindings, retryable, _unused = classify(
                final_receipts
            )
        if not final_receipts:
            outcome = ManagedContinuationOutcome.ABSENT
        elif retryable:
            outcome = ManagedContinuationOutcome.PARTIAL
        else:
            outcome = ManagedContinuationOutcome.COMPLETE
    except Exception as exc:
        outcome = ManagedContinuationOutcome.AMBIGUOUS
        errors.append(str(exc))

    return ManagedContinuationRecoveryReceipt(
        outcome=outcome,
        store_path=os.fspath(path),
        store_identity_before=before_identity,
        store_sha256_before=before_sha,
        store_identity_after=after_identity,
        store_sha256_after=after_sha,
        transaction_id=str(transaction_id or ""),
        manifest_sha256=str(manifest_sha256 or ""),
        process_pid=pid,
        process_start_token=token,
        process_epoch=epoch,
        receipt_classifications=tuple(sorted(classifications)),
        receipt_bindings=tuple(sorted(bindings)),
        started_receipt_keys=tuple(sorted(started)),
        retryable_receipt_keys=tuple(sorted(set(retryable))),
        errors=tuple(errors),
    )
