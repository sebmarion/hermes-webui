"""Read-only, fail-closed session audit for managed release startup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

_MAX_ENTRIES = 16_384
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_DB_ROWS = 250_000
_MAX_MESSAGES = 2_000_000
_MAX_JOURNAL_EVENTS = 250_000
_MAX_NAME_BYTES = 512
_MAX_DB_PAGES = 1_048_576
_MAX_DB_SECONDS = 5.0
_SQLITE_PROGRESS_OPCODES = 1_000
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}")
_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ManagedStartupSessionError(RuntimeError):
    """Base error for the managed startup session audit."""


class ManagedStartupSessionBindingError(ManagedStartupSessionError):
    """The audit receipt binding is absent, partial, or malformed."""


class ManagedStartupSessionAmbiguousError(ManagedStartupSessionError):
    """Session state cannot be proved clean without mutation."""


class SessionRecoveryOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManagedStartupSessionReceipt:
    outcome: SessionRecoveryOutcome
    transaction_id: str | None
    manifest_sha256: str | None
    session_dir: str
    session_dir_device: int
    session_dir_inode: int
    state_db_path: str | None
    state_db_bundle: tuple[tuple[str, tuple[int, ...] | None], ...]
    session_ids: tuple[str, ...]
    inventory_sha256: str
    database_sha256: str | None
    audit_sha256: str


@dataclass(frozen=True)
class ManagedStartupSessionVerification:
    outcome: SessionRecoveryOutcome
    receipt: ManagedStartupSessionReceipt | None
    reason: str | None


@dataclass(frozen=True)
class _Inventory:
    root_identity: tuple[int, ...]
    files: tuple[tuple[str, bytes], ...]
    journal_files: tuple[tuple[str, bytes], ...]
    sha256: str


@dataclass(frozen=True)
class _DatabaseSnapshot:
    session_rows: tuple[tuple[str, str], ...]
    message_counts: tuple[tuple[str, int], ...]
    sha256: str
    image_sha256: str


@dataclass(frozen=True)
class _HeldDatabaseBundle:
    receipt: tuple[tuple[str, tuple[int, ...] | None], ...]
    files: tuple[tuple[str, bytes | None], ...]


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
        raise ManagedStartupSessionAmbiguousError(
            f"{label} is not one canonical absolute path"
        )
    return path


@contextmanager
def _held_directory(path_value: Path | str) -> Iterator[tuple[int, os.stat_result]]:
    path = _canonical_absolute(path_value, "session directory")
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
                component, dir_fd=descriptor, follow_symlinks=False
            )
            child = os.open(component, flags, dir_fd=descriptor)
            descriptors.append(child)
            held = os.fstat(child)
            if (
                not stat.S_ISDIR(held.st_mode)
                or _binding_identity(before) != _binding_identity(held)
            ):
                raise ManagedStartupSessionAmbiguousError(
                    "session directory component identity is unsafe"
                )
            identities.append(_binding_identity(held))
            descriptor = child
        final = os.fstat(descriptors[-1])
        if final.st_uid != os.getuid():
            raise ManagedStartupSessionAmbiguousError(
                "session directory owner is unexpected"
            )
        yield descriptors[-1], final
        parent = descriptors[0]
        if _binding_identity(os.fstat(parent)) != identities[0]:
            raise ManagedStartupSessionAmbiguousError(
                "session directory root identity changed"
            )
        for index, component in enumerate(components):
            rebound = os.stat(
                component, dir_fd=parent, follow_symlinks=False
            )
            if _binding_identity(rebound) != identities[index + 1]:
                raise ManagedStartupSessionAmbiguousError(
                    "session directory component changed during audit"
                )
            parent = descriptors[index + 1]
    except ManagedStartupSessionError:
        raise
    except OSError as exc:
        raise ManagedStartupSessionAmbiguousError(
            "session directory could not be held safely"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_file_at(
    directory_fd: int,
    name: str,
    *,
    total: list[int],
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        entry_before = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            held_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(held_before.st_mode)
                or held_before.st_uid != os.getuid()
                or held_before.st_nlink != 1
                or held_before.st_size > _MAX_FILE_BYTES
            ):
                raise ManagedStartupSessionAmbiguousError(
                    f"session audit file is unsafe: {name}"
                )
            chunks: list[bytes] = []
            remaining = _MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            held_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        entry_after = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
    except ManagedStartupSessionError:
        raise
    except OSError as exc:
        raise ManagedStartupSessionAmbiguousError(
            f"session audit file could not be read: {name}"
        ) from exc
    if (
        len(raw) > _MAX_FILE_BYTES
        or len(
            {
                _full_identity(value)
                for value in (
                    entry_before,
                    held_before,
                    held_after,
                    entry_after,
                )
            }
        )
        != 1
    ):
        raise ManagedStartupSessionAmbiguousError(
            f"session audit file changed while reading: {name}"
        )
    total[0] += len(raw)
    if total[0] > _MAX_TOTAL_BYTES:
        raise ManagedStartupSessionAmbiguousError(
            "session audit byte budget exceeded"
        )
    return raw


def _read_journal_inventory(
    root_fd: int,
    *,
    total: list[int],
) -> tuple[tuple[str, bytes], ...]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(
            "_turn_journal", dir_fd=root_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return ()
    try:
        descriptor = os.open("_turn_journal", flags, dir_fd=root_fd)
        held = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(held.st_mode)
            or held.st_uid != os.getuid()
            or _binding_identity(before) != _binding_identity(held)
        ):
            raise ManagedStartupSessionAmbiguousError(
                "turn journal directory is unsafe"
            )
        names = _bounded_directory_names(
            descriptor,
            label="turn journal",
        )
        rows = tuple(
            (
                name,
                _read_file_at(descriptor, name, total=total),
            )
            for name in names
        )
        after = os.stat(
            "_turn_journal", dir_fd=root_fd, follow_symlinks=False
        )
        if _binding_identity(after) != _binding_identity(held):
            raise ManagedStartupSessionAmbiguousError(
                "turn journal directory changed during audit"
            )
        return rows
    except ManagedStartupSessionError:
        raise
    except OSError as exc:
        raise ManagedStartupSessionAmbiguousError(
            "turn journal directory could not be held safely"
        ) from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _bounded_directory_names(directory_fd: int, *, label: str) -> tuple[str, ...]:
    names: list[str] = []
    for entry in os.scandir(directory_fd):
        names.append(entry.name)
        if len(names) > _MAX_ENTRIES:
            raise ManagedStartupSessionAmbiguousError(
                f"{label} entry budget exceeded"
            )
    return tuple(sorted(names))


def _capture_inventory_at(
    root_fd: int,
    root_value: os.stat_result,
) -> _Inventory:
    total = [0]
    names = _bounded_directory_names(
        root_fd,
        label="session directory",
    )
    relevant = tuple(
        name
        for name in names
        if name.endswith(".json") or name.endswith(".json.bak")
    )
    if any(len(name.encode()) > _MAX_NAME_BYTES for name in relevant):
        raise ManagedStartupSessionAmbiguousError(
            "session audit filename exceeds limit"
        )
    files = tuple(
        (name, _read_file_at(root_fd, name, total=total))
        for name in relevant
    )
    journal_files = _read_journal_inventory(root_fd, total=total)
    root_identity = _binding_identity(root_value)
    canonical = json.dumps(
        {
            "root": root_identity,
            "files": [
                (name, hashlib.sha256(raw).hexdigest(), len(raw))
                for name, raw in files
            ],
            "journals": [
                (name, hashlib.sha256(raw).hexdigest(), len(raw))
                for name, raw in journal_files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _Inventory(
        root_identity,
        files,
        journal_files,
        hashlib.sha256(canonical).hexdigest(),
    )


def _capture_inventory(session_dir: Path) -> _Inventory:
    with _held_directory(session_dir) as (root_fd, root_value):
        return _capture_inventory_at(root_fd, root_value)


def _read_held_descriptor(
    descriptor: int,
    expected: tuple[int, ...],
    *,
    total: list[int],
) -> bytes:
    before = os.fstat(descriptor)
    if _full_identity(before) != expected or before.st_size > _MAX_TOTAL_BYTES:
        raise ManagedStartupSessionAmbiguousError(
            "state database bundle entry exceeds its prebound size"
        )

    def read_once() -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    first = read_once()
    middle = os.fstat(descriptor)
    second = read_once()
    after = os.fstat(descriptor)
    if (
        len(first) != before.st_size
        or first != second
        or any(
            _full_identity(value) != expected
            for value in (before, middle, after)
        )
    ):
        raise ManagedStartupSessionAmbiguousError(
            "state database bundle changed while snapshotting"
        )
    total[0] += len(first)
    if total[0] > _MAX_TOTAL_BYTES:
        raise ManagedStartupSessionAmbiguousError(
            "state database bundle byte budget exceeded"
        )
    return first


@contextmanager
def _held_db_bundle(
    path: Path,
) -> Iterator[_HeldDatabaseBundle]:
    descriptors: list[int] = []
    suffixes = ("", "-wal", "-shm")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _held_directory(path.parent) as (parent_fd, _parent_value):
        rows: list[tuple[str, tuple[int, ...] | None]] = []
        try:
            for suffix in suffixes:
                name = path.name + suffix
                try:
                    before = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    rows.append((suffix or "main", None))
                    continue
                descriptor = os.open(name, flags, dir_fd=parent_fd)
                descriptors.append(descriptor)
                held = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or held.st_uid != os.getuid()
                    or held.st_nlink != 1
                    or _full_identity(before) != _full_identity(held)
                ):
                    raise ManagedStartupSessionAmbiguousError(
                        "state database bundle entry is unsafe"
                    )
                rows.append((suffix or "main", _full_identity(held)))
            bound = tuple(rows)
            total = [0]
            descriptor_index = 0
            files: list[tuple[str, bytes | None]] = []
            for label, expected in bound:
                if expected is None:
                    files.append((label, None))
                    continue
                files.append(
                    (
                        label,
                        _read_held_descriptor(
                            descriptors[descriptor_index],
                            expected,
                            total=total,
                        ),
                    )
                )
                descriptor_index += 1
            yield _HeldDatabaseBundle(bound, tuple(files))
            descriptor_index = 0
            for suffix, (_label, expected) in zip(suffixes, bound, strict=True):
                name = path.name + suffix
                try:
                    rebound = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    rebound = None
                if expected is None:
                    if rebound is not None:
                        raise ManagedStartupSessionAmbiguousError(
                            "state database bundle changed during audit"
                        )
                    continue
                held = os.fstat(descriptors[descriptor_index])
                descriptor_index += 1
                if (
                    rebound is None
                    or _full_identity(rebound) != expected
                    or _full_identity(held) != expected
                ):
                    raise ManagedStartupSessionAmbiguousError(
                        "state database bundle changed during audit"
                    )
        except ManagedStartupSessionError:
            raise
        except OSError as exc:
            raise ManagedStartupSessionAmbiguousError(
                "state database bundle could not be held safely"
            ) from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"pragma table_info({table})").fetchall()
    }


def _query_connection_strict(
    connection: sqlite3.Connection,
) -> tuple[_DatabaseSnapshot, int]:
    deadline = time.monotonic() + _MAX_DB_SECONDS

    def progress() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(progress, _SQLITE_PROGRESS_OPCODES)
    connection.execute("pragma query_only=on")
    connection.execute("pragma busy_timeout=0")
    page_size = int(connection.execute("pragma page_size").fetchone()[0])
    page_count = int(connection.execute("pragma page_count").fetchone()[0])
    if (
        page_size < 512
        or page_size > 65_536
        or page_count < 0
        or page_count > _MAX_DB_PAGES
        or page_size * page_count > _MAX_TOTAL_BYTES
    ):
        raise ManagedStartupSessionAmbiguousError(
            "state database page budget exceeded"
        )
    version = int(connection.execute("pragma data_version").fetchone()[0])
    quick = connection.execute("pragma quick_check(1)").fetchall()
    if quick != [("ok",)]:
        raise ManagedStartupSessionAmbiguousError(
            "state database integrity is not proved"
        )
    sessions_columns = _table_columns(connection, "sessions")
    messages_columns = _table_columns(connection, "messages")
    if not {"id", "source"}.issubset(sessions_columns) or not {
        "session_id",
        "role",
        "content",
    }.issubset(messages_columns):
        raise ManagedStartupSessionAmbiguousError(
            "state database schema is incompatible"
        )
    session_result = connection.execute(
        "select id, source from sessions limit ?",
        (_MAX_DB_ROWS + 1,),
    ).fetchall()
    if len(session_result) > _MAX_DB_ROWS:
        raise ManagedStartupSessionAmbiguousError(
            "state database session row budget exceeded"
        )
    session_rows = tuple(
        sorted(
            (str(row[0]), str(row[1] or "").strip().lower())
            for row in session_result
        )
    )
    counts: dict[str, int] = {}
    message_result = connection.execute(
        "select session_id from messages limit ?",
        (_MAX_MESSAGES + 1,),
    )
    observed_messages = 0
    while True:
        rows = message_result.fetchmany(4096)
        if not rows:
            break
        observed_messages += len(rows)
        if observed_messages > _MAX_MESSAGES:
            raise ManagedStartupSessionAmbiguousError(
                "state database message row budget exceeded"
            )
        for row in rows:
            sid = str(row[0])
            counts[sid] = counts.get(sid, 0) + 1
    message_counts = tuple(sorted(counts.items()))
    serialized = connection.serialize()
    if len(serialized) > _MAX_TOTAL_BYTES:
        raise ManagedStartupSessionAmbiguousError(
            "serialized state database exceeds byte budget"
        )
    canonical = json.dumps(
        {
            "sessions": session_rows,
            "message_counts": message_counts,
            "data_version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        _DatabaseSnapshot(
            session_rows,
            message_counts,
            hashlib.sha256(canonical).hexdigest(),
            hashlib.sha256(serialized).hexdigest(),
        ),
        version,
    )


def _query_state_db_strict(path: Path) -> _DatabaseSnapshot:
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        try:
            connection.execute("begin")
            snapshot, version_before = _query_connection_strict(connection)
            version_after = int(
                connection.execute("pragma data_version").fetchone()[0]
            )
            connection.execute("commit")
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
    except ManagedStartupSessionError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, IndexError) as exc:
        raise ManagedStartupSessionAmbiguousError(
            "state database query could not be proved"
        ) from exc
    if version_before != version_after:
        raise ManagedStartupSessionAmbiguousError(
            "state database changed during read transaction"
        )
    return snapshot


def _write_reconstructed_bundle(
    root: Path,
    path: Path,
    held: _HeldDatabaseBundle,
) -> Path:
    reconstructed = root / path.name
    for (label, raw), suffix in zip(held.files, ("", "-wal", "-shm"), strict=True):
        if raw is None:
            continue
        target = root / (path.name + suffix)
        target.write_bytes(raw)
        target.chmod(0o600)
    return reconstructed


@contextmanager
def _held_database_snapshot(
    path: Path,
    held: _HeldDatabaseBundle,
) -> Iterator[_DatabaseSnapshot]:
    main_identity = held.receipt[0][1]
    if main_identity is None:
        if any(identity is not None for _name, identity in held.receipt[1:]):
            raise ManagedStartupSessionAmbiguousError(
                "state database sidecar exists without main database"
            )
        yield None
        return
    connection: sqlite3.Connection | None = None
    try:
        before_open = os.stat(path, follow_symlinks=False)
        if _full_identity(before_open) != main_identity:
            raise ManagedStartupSessionAmbiguousError(
                "state database pathname changed before connection"
            )
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("begin")
        after_open = os.stat(path, follow_symlinks=False)
        if _full_identity(after_open) != main_identity:
            raise ManagedStartupSessionAmbiguousError(
                "state database pathname changed while connecting"
            )
        live, version = _query_connection_strict(connection)
        with tempfile.TemporaryDirectory(
            prefix="hermes-session-audit-"
        ) as temporary:
            reconstructed_path = _write_reconstructed_bundle(
                Path(temporary),
                path,
                held,
            )
            reconstructed = _query_state_db_strict(reconstructed_path)
        if (
            live.sha256 != reconstructed.sha256
            or live.image_sha256 != reconstructed.image_sha256
        ):
            raise ManagedStartupSessionAmbiguousError(
                "state database live snapshot does not match held bundle"
            )
        yield live
        if (
            int(connection.execute("pragma data_version").fetchone()[0])
            != version
            or hashlib.sha256(connection.serialize()).hexdigest()
            != live.image_sha256
        ):
            raise ManagedStartupSessionAmbiguousError(
                "state database changed before final inventory completed"
            )
        connection.execute("commit")
    except ManagedStartupSessionError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, IndexError) as exc:
        raise ManagedStartupSessionAmbiguousError(
            "state database held snapshot could not be proved"
        ) from exc
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
            connection.close()


def _parse_json(raw: bytes, label: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ManagedStartupSessionAmbiguousError(
            f"{label} is malformed"
        ) from exc


def _validate_session_payload(raw: bytes, expected_id: str) -> tuple[int, set[str]]:
    value = _parse_json(raw, f"session {expected_id}")
    if not isinstance(value, dict):
        raise ManagedStartupSessionAmbiguousError("session payload is malformed")
    payload_id = value.get("session_id")
    if payload_id is not None and payload_id != expected_id:
        raise ManagedStartupSessionAmbiguousError(
            "session payload identity mismatch"
        )
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
        raise ManagedStartupSessionAmbiguousError(
            "session message payload is malformed or unbounded"
        )
    user_content = {
        str(message.get("content") or "").strip()
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    }
    return len(messages), user_content


def _records_clear_sentinel(live: dict, backup: dict) -> bool:
    clear_generation = live.get("clear_generation")
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if backup.get("clear_generation") == clear_generation:
        return False
    expected = {
        "messages": [],
        "context_messages": [],
        "truncation_watermark": 0.0,
        "truncation_boundary": 0.0,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_started_at": None,
        "pending_user_source": None,
    }
    return all(key in live and live.get(key) == value for key, value in expected.items())


def _live_supersedes_backup_by_clear_generation(
    live: dict,
    backup: dict,
) -> bool:
    clear_generation = live.get("clear_generation")
    if not isinstance(clear_generation, str) or not clear_generation:
        return False
    if backup.get("clear_generation") == clear_generation:
        return False
    live_messages = live.get("messages")
    return (
        isinstance(live_messages, list)
        and bool(live_messages)
        and live.get("truncation_watermark") == 0.0
        and live.get("truncation_boundary") == 0.0
    )


def _records_intentional_compress_shrink(live: dict) -> bool:
    context_messages = live.get("context_messages")
    messages = live.get("messages")
    has_shorter_context = (
        isinstance(context_messages, list)
        and isinstance(messages, list)
        and len(context_messages) < len(messages)
    )
    anchor_summary = str(live.get("compression_anchor_summary") or "").strip()
    anchor_key = live.get("compression_anchor_message_key")
    mode = str(live.get("compression_anchor_mode") or "").strip().lower()
    watermark = live.get("truncation_watermark")
    return (
        mode == "manual"
        or (has_shorter_context and bool(anchor_summary) and anchor_key is not None)
        or (has_shorter_context and watermark is not None)
    )


def _backup_predates_intentional_shrink(live: dict, backup: dict) -> bool:
    backup_context = backup.get("context_messages")
    backup_context = backup_context if isinstance(backup_context, list) else []
    try:
        from api.models import _context_messages_include_compression_marker

        if _context_messages_include_compression_marker(backup_context):
            return False
    except Exception:
        pass
    live_context = live.get("context_messages")
    live_context_size = len(live_context) if isinstance(live_context, list) else 0
    return len(backup_context) > live_context_size


def _intentional_backup_shrink(live_raw: bytes, backup_raw: bytes) -> bool:
    live = _parse_json(live_raw, "live session shrink guard")
    backup = _parse_json(backup_raw, "backup session shrink guard")
    if not isinstance(live, dict) or not isinstance(backup, dict):
        return False
    return (
        _records_clear_sentinel(live, backup)
        or _live_supersedes_backup_by_clear_generation(live, backup)
        or (
            _records_intentional_compress_shrink(live)
            and _backup_predates_intentional_shrink(live, backup)
        )
    )


def _validate_journals(
    journal_files: tuple[tuple[str, bytes], ...],
    live_user_content: dict[str, set[str]],
) -> None:
    event_count = 0
    states: dict[tuple[str, str], dict] = {}
    for name, raw in journal_files:
        if not name.endswith(".jsonl"):
            if name.endswith(".lock"):
                continue
            raise ManagedStartupSessionAmbiguousError(
                "turn journal contains an unexpected entry"
            )
        sid = name[:-6].split("~", 1)[0]
        if _SESSION_ID_RE.fullmatch(sid) is None:
            raise ManagedStartupSessionAmbiguousError(
                "turn journal filename is malformed"
            )
        for line in raw.splitlines():
            event_count += 1
            if event_count > _MAX_JOURNAL_EVENTS:
                raise ManagedStartupSessionAmbiguousError(
                    "turn journal event budget exceeded"
                )
            event = _parse_json(line, "turn journal event")
            if (
                not isinstance(event, dict)
                or event.get("version") != 1
                or event.get("session_id") != sid
                or not isinstance(event.get("turn_id"), str)
                or not event["turn_id"]
                or not isinstance(event.get("event"), str)
                or not event["event"]
            ):
                raise ManagedStartupSessionAmbiguousError(
                    "turn journal event is malformed"
                )
            key = sid, event["turn_id"]
            previous = states.get(key)
            try:
                created_at = float(event.get("created_at") or 0)
                previous_at = (
                    float(previous.get("created_at") or 0) if previous else -1
                )
            except (TypeError, ValueError) as exc:
                raise ManagedStartupSessionAmbiguousError(
                    "turn journal timestamp is malformed"
                ) from exc
            if previous is None or created_at >= previous_at:
                states[key] = event
    for (sid, _turn_id), event in states.items():
        if event["event"] in {"completed", "interrupted"}:
            continue
        content = str(event.get("content") or "").strip()
        if content and content not in live_user_content.get(sid, set()):
            raise ManagedStartupSessionAmbiguousError(
                "turn journal has a pending turn"
            )


def _audit_semantics(
    inventory: _Inventory,
    database: _DatabaseSnapshot | None,
) -> tuple[str, ...]:
    files = dict(inventory.files)
    tombstone_ids: set[str] = set()
    tombstone_raw = files.get("_deleted_webui_sessions.json")
    if tombstone_raw is not None:
        tombstone = _parse_json(tombstone_raw, "session tombstone")
        if (
            not isinstance(tombstone, dict)
            or tombstone.get("version") != 1
            or not isinstance(tombstone.get("ids"), list)
            or any(
                not isinstance(sid, str)
                or _SESSION_ID_RE.fullmatch(sid) is None
                for sid in tombstone["ids"]
            )
        ):
            raise ManagedStartupSessionAmbiguousError(
                "session tombstone is malformed"
            )
        tombstone_ids = set(tombstone["ids"])

    live_counts: dict[str, int] = {}
    live_user_content: dict[str, set[str]] = {}
    backup_counts: dict[str, int] = {}
    for name, raw in inventory.files:
        if name.startswith("_"):
            continue
        if name.endswith(".json.bak"):
            sid = name[: -len(".json.bak")]
            if _SESSION_ID_RE.fullmatch(sid) is None:
                raise ManagedStartupSessionAmbiguousError(
                    "backup session id is malformed"
                )
            backup_counts[sid] = _validate_session_payload(raw, sid)[0]
        elif name.endswith(".json"):
            sid = name[: -len(".json")]
            if _SESSION_ID_RE.fullmatch(sid) is None:
                raise ManagedStartupSessionAmbiguousError(
                    "live session id is malformed"
                )
            count, user_content = _validate_session_payload(raw, sid)
            live_counts[sid] = count
            live_user_content[sid] = user_content

    index_raw = files.get("_index.json")
    if live_counts and index_raw is None:
        raise ManagedStartupSessionAmbiguousError("session index is absent")
    index_ids: set[str] = set()
    if index_raw is not None:
        index = _parse_json(index_raw, "session index")
        if not isinstance(index, list) or len(index) > _MAX_DB_ROWS:
            raise ManagedStartupSessionAmbiguousError(
                "session index is malformed or unbounded"
            )
        for entry in index:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("session_id"), str)
                or _SESSION_ID_RE.fullmatch(entry["session_id"]) is None
            ):
                raise ManagedStartupSessionAmbiguousError(
                    "session index entry is malformed"
                )
            index_ids.add(entry["session_id"])
    if index_ids != set(live_counts):
        raise ManagedStartupSessionAmbiguousError("session index is stale")
    if tombstone_ids & set(live_counts):
        raise ManagedStartupSessionAmbiguousError(
            "tombstoned session still has a live sidecar"
        )
    for sid, backup_count in backup_counts.items():
        if sid not in live_counts:
            if sid not in tombstone_ids:
                raise ManagedStartupSessionAmbiguousError(
                    "orphan session backup requires review"
                )
        elif backup_count > live_counts[sid] and not _intentional_backup_shrink(
            files[f"{sid}.json"],
            files[f"{sid}.json.bak"],
        ):
            raise ManagedStartupSessionAmbiguousError(
                "session backup contains recoverable messages"
            )

    if database is not None:
        counts = dict(database.message_counts)
        database_ids = {sid for sid, _source in database.session_rows}
        if set(counts) - database_ids:
            raise ManagedStartupSessionAmbiguousError(
                "state database contains orphan messages"
            )
        for sid, source in database.session_rows:
            if _SESSION_ID_RE.fullmatch(sid) is None:
                raise ManagedStartupSessionAmbiguousError(
                    "state database session id is malformed"
                )
            if (
                source in {"webui", "fork"}
                and sid not in live_counts
                and sid not in tombstone_ids
            ):
                raise ManagedStartupSessionAmbiguousError(
                    "state database session is missing its sidecar"
                )
    _validate_journals(inventory.journal_files, live_user_content)
    return tuple(sorted(live_counts))


def _binding(
    transaction_id: str | None,
    manifest_sha256: str | None,
) -> tuple[str, str, SessionRecoveryOutcome]:
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
        or not isinstance(manifest_sha256, str)
        or _SHA256_RE.fullmatch(manifest_sha256) is None
    ):
        raise ManagedStartupSessionBindingError(
            "managed session audit binding is invalid"
        )
    return (
        transaction_id,
        manifest_sha256,
        SessionRecoveryOutcome.PROVED_COMPLETE,
    )


def audit_managed_startup_sessions(
    session_dir: Path | str,
    state_db_path: Path | str | None,
    *,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
) -> ManagedStartupSessionReceipt:
    """Prove session state clean without invoking any recovery mutator."""

    transaction_id, manifest_sha256, outcome = _binding(
        transaction_id, manifest_sha256
    )
    session_path = _canonical_absolute(session_dir, "session directory")
    db_path = (
        _canonical_absolute(state_db_path, "state database")
        if state_db_path is not None
        else None
    )
    with _held_directory(session_path) as (root_fd, root_value):
        inventory_before = _capture_inventory_at(root_fd, root_value)
        if db_path is None:
            database = None
            bundle = ()
            session_ids = _audit_semantics(inventory_before, database)
            inventory_after = _capture_inventory_at(root_fd, root_value)
        else:
            with _held_db_bundle(db_path) as held:
                bundle = held.receipt
                with _held_database_snapshot(db_path, held) as database:
                    session_ids = _audit_semantics(inventory_before, database)
                    inventory_after = _capture_inventory_at(root_fd, root_value)
    if inventory_before != inventory_after:
        raise ManagedStartupSessionAmbiguousError(
            "session inventory changed during audit"
        )
    database_sha = database.sha256 if database else None
    canonical = json.dumps(
        {
            "transaction_id": transaction_id,
            "manifest_sha256": manifest_sha256,
            "inventory": inventory_after.sha256,
            "database": database_sha,
            "bundle": bundle,
            "session_ids": session_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ManagedStartupSessionReceipt(
        outcome,
        transaction_id,
        manifest_sha256,
        str(session_path),
        inventory_after.root_identity[0],
        inventory_after.root_identity[1],
        str(db_path) if db_path else None,
        bundle,
        session_ids,
        inventory_after.sha256,
        database_sha,
        hashlib.sha256(canonical).hexdigest(),
    )


def verify_managed_startup_sessions(
    receipt: ManagedStartupSessionReceipt | None,
    *,
    transaction_id: str | None = None,
    manifest_sha256: str | None = None,
) -> ManagedStartupSessionVerification:
    """Re-run the entire strict audit and compare its exact receipt."""

    if type(receipt) is not ManagedStartupSessionReceipt:
        return ManagedStartupSessionVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            None,
            "managed_session_audit_receipt_missing",
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
        observed = audit_managed_startup_sessions(
            receipt.session_dir,
            receipt.state_db_path,
            transaction_id=observed_transaction,
            manifest_sha256=observed_manifest,
        )
    except ManagedStartupSessionError:
        return ManagedStartupSessionVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            receipt,
            "managed_session_audit_unobservable",
        )
    semantic_receipt = (
        receipt.session_dir,
        receipt.session_dir_device,
        receipt.session_dir_inode,
        receipt.state_db_path,
        receipt.state_db_bundle,
        receipt.session_ids,
        receipt.inventory_sha256,
        receipt.database_sha256,
    )
    semantic_observed = (
        observed.session_dir,
        observed.session_dir_device,
        observed.session_dir_inode,
        observed.state_db_path,
        observed.state_db_bundle,
        observed.session_ids,
        observed.inventory_sha256,
        observed.database_sha256,
    )
    binding_changed = (
        receipt.transaction_id,
        receipt.manifest_sha256,
    ) != (
        observed.transaction_id,
        observed.manifest_sha256,
    )
    if semantic_observed != semantic_receipt or binding_changed:
        return ManagedStartupSessionVerification(
            SessionRecoveryOutcome.AMBIGUOUS,
            receipt,
            "managed_session_audit_receipt_mismatch",
        )
    return ManagedStartupSessionVerification(observed.outcome, observed, None)
