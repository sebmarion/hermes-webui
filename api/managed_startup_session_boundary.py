"""Bind managed startup to private session state without reading its payloads.

The managed release path never performs legacy session recovery.  Its startup
receipt therefore proves the state boundary it will *not* mutate rather than
parsing the production transcript corpus or serializing the production SQLite
database.  The deep audit remains available in
``api.managed_startup_session_recovery`` as an explicit operator diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from api.managed_startup_session_recovery import SessionRecoveryOutcome


_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BOUNDARY_SCHEMA = "webui.managed-session-boundary.v1"


class ManagedStartupSessionBoundaryError(RuntimeError):
    """The managed startup session boundary could not be proved safely."""


@dataclass(frozen=True)
class ManagedStartupSessionBoundaryReceipt:
    outcome: SessionRecoveryOutcome
    transaction_id: str
    manifest_sha256: str
    session_dir: str
    session_dir_identity: tuple[int, ...]
    state_db_path: str | None
    state_db_parent_identity: tuple[int, ...] | None
    state_db_bundle: tuple[tuple[str, tuple[int, ...] | None], ...]
    evidence_sha256: str


@dataclass(frozen=True)
class ManagedStartupSessionBoundaryVerification:
    outcome: SessionRecoveryOutcome
    receipt: ManagedStartupSessionBoundaryReceipt | None
    reason: str | None


def _full_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _binding_identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _canonical_absolute(value: Path | str, label: str) -> Path:
    raw = os.fspath(value)
    path = Path(raw)
    if (
        not path.is_absolute()
        or raw != str(path)
        or Path(os.path.normpath(raw)) != path
        or path == Path("/")
        or any(part in ("", ".", "..") for part in path.parts[1:])
    ):
        raise ManagedStartupSessionBoundaryError(
            f"{label} is not one canonical absolute path"
        )
    return path


@contextmanager
def _held_private_directory(
    path_value: Path | str,
    *,
    label: str,
) -> Iterator[tuple[int, os.stat_result]]:
    path = _canonical_absolute(path_value, label)
    components = path.parts[1:]
    descriptors: list[int] = []
    identities: list[tuple[int, ...]] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open("/", flags)
        descriptors.append(descriptor)
        identities.append(_binding_identity(os.fstat(descriptor)))
        for component in components:
            before = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            child = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(child)
            held = os.fstat(child)
            if (
                not stat.S_ISDIR(held.st_mode)
                or _binding_identity(before) != _binding_identity(held)
            ):
                raise ManagedStartupSessionBoundaryError(
                    f"{label} identity is unsafe"
                )
            identities.append(_binding_identity(held))
            descriptor = child
        final = os.fstat(descriptors[-1])
        if (
            final.st_uid != os.getuid()
            or stat.S_IMODE(final.st_mode) != 0o700
        ):
            raise ManagedStartupSessionBoundaryError(
                f"{label} is not owner-private"
            )
        final_identity = _full_identity(final)
        yield descriptors[-1], final
        if _full_identity(os.fstat(descriptors[-1])) != final_identity:
            raise ManagedStartupSessionBoundaryError(
                f"{label} changed while held"
            )
        parent = descriptors[0]
        if _binding_identity(os.fstat(parent)) != identities[0]:
            raise ManagedStartupSessionBoundaryError(
                f"{label} root identity changed"
            )
        for index, component in enumerate(components):
            rebound = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if _binding_identity(rebound) != identities[index + 1]:
                raise ManagedStartupSessionBoundaryError(
                    f"{label} component changed while held"
                )
            parent = descriptors[index + 1]
    except ManagedStartupSessionBoundaryError:
        raise
    except OSError as exc:
        raise ManagedStartupSessionBoundaryError(
            f"{label} could not be held safely"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _held_state_db_bundle(
    path: Path,
) -> Iterator[
    tuple[
        tuple[int, ...],
        tuple[tuple[str, tuple[int, ...] | None], ...],
    ]
]:
    descriptors: list[int] = []
    members = (("main", ""), ("wal", "-wal"), ("shm", "-shm"))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with _held_private_directory(
        path.parent,
        label="state database parent",
    ) as (parent_fd, parent_value):
        rows: list[tuple[str, tuple[int, ...] | None]] = []
        try:
            for label, suffix in members:
                name = path.name + suffix
                try:
                    before = os.stat(
                        name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    rows.append((label, None))
                    continue
                descriptor = os.open(name, flags, dir_fd=parent_fd)
                descriptors.append(descriptor)
                held = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or held.st_uid != os.getuid()
                    or held.st_nlink != 1
                    or stat.S_IMODE(held.st_mode) != 0o600
                    or _full_identity(before) != _full_identity(held)
                ):
                    raise ManagedStartupSessionBoundaryError(
                        f"state database {label} is not a private regular file"
                    )
                rows.append((label, _full_identity(held)))
            bundle = tuple(rows)
            if bundle[0][1] is None and any(
                identity is not None for _label, identity in bundle[1:]
            ):
                raise ManagedStartupSessionBoundaryError(
                    "state database sidecar exists without main database"
                )
            yield _full_identity(parent_value), bundle
            descriptor_index = 0
            for (label, suffix), (_receipt_label, expected) in zip(
                members,
                bundle,
                strict=True,
            ):
                name = path.name + suffix
                try:
                    rebound = os.stat(
                        name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    rebound = None
                if expected is None:
                    if rebound is not None:
                        raise ManagedStartupSessionBoundaryError(
                            f"state database {label} appeared while held"
                        )
                    continue
                held = os.fstat(descriptors[descriptor_index])
                descriptor_index += 1
                if (
                    rebound is None
                    or _full_identity(rebound) != expected
                    or _full_identity(held) != expected
                ):
                    raise ManagedStartupSessionBoundaryError(
                        f"state database {label} changed while held"
                    )
        except ManagedStartupSessionBoundaryError:
            raise
        except OSError as exc:
            raise ManagedStartupSessionBoundaryError(
                "state database bundle could not be held safely"
            ) from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _binding(
    transaction_id: str | None,
    manifest_sha256: str | None,
) -> tuple[str, str]:
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
        or not isinstance(manifest_sha256, str)
        or _SHA256_RE.fullmatch(manifest_sha256) is None
    ):
        raise ManagedStartupSessionBoundaryError(
            "managed session boundary binding is invalid"
        )
    return transaction_id, manifest_sha256


def attest_managed_startup_session_boundary(
    session_dir: Path | str,
    state_db_path: Path | str | None,
    *,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
) -> ManagedStartupSessionBoundaryReceipt:
    """Attest the private no-mutation boundary for a managed release startup."""

    transaction_id, manifest_sha256 = _binding(
        transaction_id,
        manifest_sha256,
    )
    session_path = _canonical_absolute(session_dir, "session directory")
    db_path = (
        _canonical_absolute(state_db_path, "state database")
        if state_db_path is not None
        else None
    )
    with _held_private_directory(
        session_path,
        label="session directory",
    ) as (_session_fd, session_value):
        session_identity = _full_identity(session_value)
        if db_path is None:
            parent_identity = None
            bundle = ()
        else:
            with _held_state_db_bundle(db_path) as (
                parent_identity,
                bundle,
            ):
                pass
    canonical = json.dumps(
        {
            "schema": _BOUNDARY_SCHEMA,
            "transaction_id": transaction_id,
            "manifest_sha256": manifest_sha256,
            "session_dir": str(session_path),
            "session_dir_identity": session_identity,
            "state_db_path": str(db_path) if db_path else None,
            "state_db_parent_identity": parent_identity,
            "state_db_bundle": bundle,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ManagedStartupSessionBoundaryReceipt(
        SessionRecoveryOutcome.PROVED_COMPLETE,
        transaction_id,
        manifest_sha256,
        str(session_path),
        session_identity,
        str(db_path) if db_path else None,
        parent_identity,
        bundle,
        hashlib.sha256(canonical).hexdigest(),
    )


def verify_managed_startup_session_boundary(
    receipt: ManagedStartupSessionBoundaryReceipt | None,
    *,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
) -> ManagedStartupSessionBoundaryVerification:
    """Re-attest and require the exact immutable managed boundary receipt."""

    if type(receipt) is not ManagedStartupSessionBoundaryReceipt:
        return ManagedStartupSessionBoundaryVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            None,
            "managed_session_boundary_receipt_missing",
        )
    observed_transaction = (
        transaction_id
        if transaction_id is not None
        else receipt.transaction_id
    )
    observed_manifest = (
        manifest_sha256
        if manifest_sha256 is not None
        else receipt.manifest_sha256
    )
    try:
        observed = attest_managed_startup_session_boundary(
            receipt.session_dir,
            receipt.state_db_path,
            transaction_id=observed_transaction,
            manifest_sha256=observed_manifest,
        )
    except ManagedStartupSessionBoundaryError:
        return ManagedStartupSessionBoundaryVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            receipt,
            "managed_session_boundary_unobservable",
        )
    if observed != receipt:
        return ManagedStartupSessionBoundaryVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            receipt,
            "managed_session_boundary_receipt_mismatch",
        )
    return ManagedStartupSessionBoundaryVerification(
        SessionRecoveryOutcome.PROVED_COMPLETE,
        observed,
        None,
    )
