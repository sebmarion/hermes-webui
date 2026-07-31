"""Production binding for transaction-safe managed WebUI startup replay.

The replay engine is intentionally generic.  This module is the single place
that binds its steps to concrete, receipt-producing WebUI/Agent capabilities.
Missing capabilities are a startup error; managed startup never substitutes a
best-effort or synthetic-complete operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

import fcntl

import deferred_release_manifest as release_manifest
from deferred_startup_file_driver import DeferredStartupFileDriver
from deferred_startup_replay import (
    DeferredStartupManifestReceipt,
    DeferredStartupStep,
    PriorCompletionAbsentPolicy,
    Reconciliation,
    RetrySafePartialPolicy,
)


_TRANSACTION_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_PROFILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_JOURNAL_ENV = "HERMES_WEBUI_STARTUP_ATTEMPT_JOURNAL"
_CONFIG_JOURNAL_ENV = "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL"
_SAFE_PARTIAL_STEPS = frozenset(
    {
        "internal_recovery_key",
        "state_directories",
        "startup_configuration",
        "async_delegation_recovery",
        "tool_limit_continuation_recovery",
        "goal_continuation_recovery",
        "background_services",
    }
)
_RERUN_IF_ABSENT_STEPS = frozenset(
    {
        "state_directories",
        "internal_recovery_key",
        "startup_profile_state",
        "provider_model_seed",
        "startup_configuration",
        "plugins",
        "process_completion_recovery",
        "async_delegation_recovery",
        "tool_limit_continuation_recovery",
        "goal_continuation_recovery",
        "background_services",
    }
)


class ManagedStartupBindingError(RuntimeError):
    """A production startup authority or capability is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class ManagedStartupOperation:
    name: str
    operation: str
    mutator: Callable[[], object]
    verifier: Callable[[object | None], object]
    receipt_type_id: str


@dataclass(frozen=True, slots=True)
class ManagedStartupReceiptCodec:
    type_id: str
    python_type: type


@dataclass(frozen=True, slots=True)
class AsyncProfile:
    profile_id: str
    tracker_path: Path


@dataclass(frozen=True, slots=True)
class AsyncProfileManifest:
    generation: str
    profiles: tuple[AsyncProfile, ...]
    expected_profile_ids: tuple[str, ...]
    source_digest: str


@dataclass(frozen=True, slots=True)
class ManagedStartupReceiptBundle:
    transaction_id: str
    manifest_sha256: str
    configuration_journal: str
    receipt_journal_generation: int
    receipt_journal_sha256: str
    receipts: tuple[tuple[str, object | None], ...]


@dataclass(frozen=True, slots=True)
class _Verification:
    outcome: object
    receipt: object | None


@dataclass(frozen=True, slots=True)
class _BackgroundReceipt:
    drain: object
    reaper: object


def _canonical_file(value: object, *, name: str) -> Path:
    if type(value) is not str or not value:
        raise ManagedStartupBindingError(f"{name} is missing")
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or Path(os.path.abspath(path)) != path
        or not path.name
    ):
        raise ManagedStartupBindingError(f"{name} is not canonical")
    return path


class DurableStartupReceiptStore:
    """Monotonic latest-per-step typed receipt authority."""

    VERSION = 2
    MAX_BYTES = 16 * 1024 * 1024
    MAX_ENTRIES = 4096
    MAX_GENERATIONS = 1_000_000

    def __init__(
        self,
        path: Path | str,
        *,
        transaction_id: str,
        manifest_sha256: str,
        codecs: tuple[ManagedStartupReceiptCodec, ...],
        step_types: tuple[tuple[str, str], ...],
    ) -> None:
        self.path = _canonical_file(
            os.fspath(path),
            name="managed startup receipt journal",
        )
        if (
            _TRANSACTION_RE.fullmatch(transaction_id) is None
            or _SHA256_RE.fullmatch(manifest_sha256) is None
        ):
            raise ManagedStartupBindingError(
                "managed receipt-store binding is invalid"
            )
        by_id: dict[str, type] = {}
        by_type: dict[type, str] = {}
        for codec in codecs:
            if (
                type(codec) is not ManagedStartupReceiptCodec
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", codec.type_id)
                or not isinstance(codec.python_type, type)
                or codec.type_id in by_id
                or codec.python_type in by_type
                or not (
                    issubclass(codec.python_type, Enum)
                    or (
                        is_dataclass(codec.python_type)
                        and codec.python_type.__dataclass_params__.frozen
                    )
                )
            ):
                raise ManagedStartupBindingError(
                    "managed receipt codec registry is invalid"
                )
            by_id[codec.type_id] = codec.python_type
            by_type[codec.python_type] = codec.type_id
        if not by_id:
            raise ManagedStartupBindingError(
                "managed receipt codec registry is empty"
            )
        self._transaction_id = transaction_id
        self._manifest_sha256 = manifest_sha256
        self._by_id = by_id
        self._by_type = by_type
        if (
            type(step_types) is not tuple
            or not step_types
            or len({name for name, _type_id in step_types}) != len(step_types)
            or any(
                type(name) is not str
                or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
                or type_id not in by_id
                for name, type_id in step_types
            )
        ):
            raise ManagedStartupBindingError(
                "managed receipt step registry is invalid"
            )
        self._ordered_step_types = step_types
        self._step_types = dict(step_types)
        self._thread_lock = threading.RLock()
        self._parent_fd = self._open_private_parent()
        self._leaf = self.path.name
        self._lock_leaf = f".{self._leaf}.lock"
        self._ensure_lock_file()
        with self._locked():
            value, fingerprint = self._read()
            if value is None:
                initial = self._empty()
                self._validate_journal(initial)
                self._write(initial, expected=None)
            else:
                self._validate_journal(value)

    def _open_private_parent(self) -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise ManagedStartupBindingError(
                "managed receipt store requires no-follow directory authority"
            )
        descriptor = os.open(
            self.path.parent,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | directory,
        )
        opened = os.fstat(descriptor)
        current = os.lstat(self.path.parent)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (current.st_dev, current.st_ino)
            or opened.st_uid != os.getuid()
            or current.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise ManagedStartupBindingError(
                "managed receipt-store parent is not private and stable"
            )
        self._parent_identity = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_uid,
            stat.S_IMODE(opened.st_mode),
        )
        return descriptor

    def _assert_parent_current(self) -> None:
        opened = os.fstat(self._parent_fd)
        current = os.lstat(self.path.parent)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_uid,
            stat.S_IMODE(opened.st_mode),
        )
        current_identity = (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
            current.st_uid,
            stat.S_IMODE(current.st_mode),
        )
        if (
            opened_identity != self._parent_identity
            or current_identity != self._parent_identity
            or opened.st_uid != os.getuid()
            or current.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise ManagedStartupBindingError(
                "managed receipt-store parent identity changed"
            )

    def _ensure_lock_file(self) -> None:
        self._assert_parent_current()
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(
                self._lock_leaf,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self._parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(self._parent_fd)
        except FileExistsError:
            descriptor = os.open(
                self._lock_leaf,
                flags,
                dir_fd=self._parent_fd,
            )
        try:
            opened = os.fstat(descriptor)
            current = os.stat(
                self._lock_leaf,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise ManagedStartupBindingError(
                    "managed receipt-store lock is unsafe"
                )
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            self._assert_parent_current()
            descriptor = os.open(
                self._lock_leaf,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                current = os.stat(
                    self._lock_leaf,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_uid != os.getuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or (opened.st_dev, opened.st_ino)
                    != (current.st_dev, current.st_ino)
                ):
                    raise ManagedStartupBindingError(
                        "managed receipt-store lock identity changed"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._assert_parent_current()
                locked = os.fstat(descriptor)
                named = os.stat(
                    self._lock_leaf,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(locked.st_mode)
                    or locked.st_nlink != 1
                    or locked.st_uid != os.getuid()
                    or stat.S_IMODE(locked.st_mode) != 0o600
                    or (locked.st_dev, locked.st_ino)
                    != (named.st_dev, named.st_ino)
                ):
                    raise ManagedStartupBindingError(
                        "managed receipt-store lock identity changed"
                    )
                yield
                self._assert_parent_current()
                after = os.stat(
                    self._lock_leaf,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
                if (after.st_dev, after.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise ManagedStartupBindingError(
                        "managed receipt-store lock changed while held"
                    )
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _empty(self) -> dict:
        return {
            "version": self.VERSION,
            "transaction_id": self._transaction_id,
            "manifest_sha256": self._manifest_sha256,
            "generation": 0,
            "receipts": {},
        }

    def _read(self) -> tuple[dict | None, tuple[tuple[int, ...], str] | None]:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(self._leaf, flags, dir_fd=self._parent_fd)
        except FileNotFoundError:
            return None, None
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > self.MAX_BYTES
            ):
                raise ManagedStartupBindingError(
                    "managed receipt journal is unsafe"
                )
            payload = handle.read(self.MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = os.stat(
            self._leaf,
            dir_fd=self._parent_fd,
            follow_symlinks=False,
        )
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            len(payload) > self.MAX_BYTES
            or identity
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or identity
            != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
        ):
            raise ManagedStartupBindingError(
                "managed receipt journal changed while read"
            )
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ManagedStartupBindingError(
                "managed receipt journal is malformed"
            ) from exc
        return value, (identity, hashlib.sha256(payload).hexdigest())

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    def _write(
        self,
        value: dict,
        *,
        expected: tuple[tuple[int, ...], str] | None,
    ) -> None:
        payload = self._canonical(value)
        if len(payload) > self.MAX_BYTES:
            raise ManagedStartupBindingError(
                "managed receipt journal exceeds its byte budget"
            )
        current_value, current = self._read()
        del current_value
        if current != expected:
            raise ManagedStartupBindingError(
                "managed receipt journal compare-and-swap failed"
            )
        temporary = f".{self._leaf}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._parent_fd,
        )
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _current_value, before_publish = self._read()
            if before_publish != expected:
                raise ManagedStartupBindingError(
                    "managed receipt journal changed before publish"
                )
            os.replace(
                temporary,
                self._leaf,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
            )
            published = True
            os.fsync(self._parent_fd)
            self._assert_parent_current()
        finally:
            if not published:
                try:
                    os.unlink(temporary, dir_fd=self._parent_fd)
                except FileNotFoundError:
                    pass

    def _encode(self, value: object) -> object:
        if value is None or type(value) in {bool, int, str}:
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ManagedStartupBindingError(
                    "managed receipt contains a non-finite number"
                )
            return value
        if type(value) is bytes:
            return {"$bytes": value.hex()}
        if type(value) is tuple:
            return {"$tuple": [self._encode(item) for item in value]}
        if type(value) is list:
            return {"$list": [self._encode(item) for item in value]}
        if type(value) is dict:
            if not all(type(key) is str for key in value):
                raise ManagedStartupBindingError(
                    "managed receipt mapping keys are invalid"
                )
            return {
                "$dict": [
                    [key, self._encode(value[key])] for key in sorted(value)
                ]
            }
        value_type = type(value)
        type_id = self._by_type.get(value_type)
        if type_id is None:
            raise ManagedStartupBindingError(
                "managed receipt contains an unregistered type"
            )
        if issubclass(value_type, Enum):
            return {"$enum": type_id, "value": self._encode(value.value)}
        return {
            "$type": type_id,
            "fields": {
                field.name: self._encode(getattr(value, field.name))
                for field in fields(value)
            },
        }

    def _decode(self, value: object) -> object:
        if value is None or type(value) in {bool, int, float, str}:
            if type(value) is float and not math.isfinite(value):
                raise ManagedStartupBindingError(
                    "managed receipt contains a non-finite number"
                )
            return value
        if not isinstance(value, dict) or len(value) == 0:
            raise ManagedStartupBindingError(
                "managed receipt payload shape is invalid"
            )
        if set(value) == {"$bytes"}:
            raw = value["$bytes"]
            if type(raw) is not str or len(raw) % 2:
                raise ManagedStartupBindingError(
                    "managed receipt byte payload is invalid"
                )
            try:
                return bytes.fromhex(raw)
            except ValueError as exc:
                raise ManagedStartupBindingError(
                    "managed receipt byte payload is invalid"
                ) from exc
        if set(value) in ({"$tuple"}, {"$list"}):
            key = next(iter(value))
            items = value[key]
            if type(items) is not list:
                raise ManagedStartupBindingError(
                    "managed receipt sequence is invalid"
                )
            decoded = [self._decode(item) for item in items]
            return tuple(decoded) if key == "$tuple" else decoded
        if set(value) == {"$dict"}:
            items = value["$dict"]
            if (
                type(items) is not list
                or not all(
                    type(item) is list
                    and len(item) == 2
                    and type(item[0]) is str
                    for item in items
                )
                or [item[0] for item in items]
                != sorted({item[0] for item in items})
            ):
                raise ManagedStartupBindingError(
                    "managed receipt mapping is invalid"
                )
            return {item[0]: self._decode(item[1]) for item in items}
        if set(value) == {"$enum", "value"}:
            enum_type = self._by_id.get(value["$enum"])
            if enum_type is None or not issubclass(enum_type, Enum):
                raise ManagedStartupBindingError(
                    "managed receipt enum type is not registered"
                )
            try:
                return enum_type(self._decode(value["value"]))
            except (TypeError, ValueError) as exc:
                raise ManagedStartupBindingError(
                    "managed receipt enum value is invalid"
                ) from exc
        if set(value) != {"$type", "fields"}:
            raise ManagedStartupBindingError(
                "managed receipt typed payload is invalid"
            )
        receipt_type = self._by_id.get(value["$type"])
        raw_fields = value["fields"]
        if (
            receipt_type is None
            or issubclass(receipt_type, Enum)
            or type(raw_fields) is not dict
            or set(raw_fields) != {field.name for field in fields(receipt_type)}
        ):
            raise ManagedStartupBindingError(
                "managed receipt dataclass type is not registered"
            )
        try:
            return receipt_type(
                **{
                    field.name: self._decode(raw_fields[field.name])
                    for field in fields(receipt_type)
                }
            )
        except (TypeError, ValueError) as exc:
            raise ManagedStartupBindingError(
                "managed receipt dataclass value is invalid"
            ) from exc

    def _validate_journal(self, value: object) -> dict:
        if (
            type(value) is not dict
            or set(value)
            != {
                "version",
                "transaction_id",
                "manifest_sha256",
                "generation",
                "receipts",
            }
            or value.get("version") != self.VERSION
            or type(value.get("generation")) is not int
            or isinstance(value.get("generation"), bool)
            or value["generation"] < 0
            or value["generation"] > self.MAX_GENERATIONS
            or type(value.get("receipts")) is not dict
            or len(value["receipts"]) > self.MAX_ENTRIES
        ):
            raise ManagedStartupBindingError(
                "managed receipt journal schema is invalid"
            )
        if value.get("transaction_id") != self._transaction_id:
            raise ManagedStartupBindingError(
                "managed receipt journal transaction does not match"
            )
        if value.get("manifest_sha256") != self._manifest_sha256:
            raise ManagedStartupBindingError(
                "managed receipt journal manifest does not match"
            )
        seen_generations: set[int] = set()
        for step, entry in value["receipts"].items():
            if (
                type(step) is not str
                or step not in self._step_types
                or type(entry) is not dict
                or set(entry) != {"generation", "type", "payload", "sha256"}
                or type(entry.get("generation")) is not int
                or isinstance(entry.get("generation"), bool)
                or entry["generation"] <= 0
                or entry["generation"] > value["generation"]
                or entry["generation"] in seen_generations
                or type(entry.get("type")) is not str
                or entry["type"] not in self._by_id
                or self._step_types.get(step) != entry["type"]
                or type(entry.get("sha256")) is not str
                or _SHA256_RE.fullmatch(entry["sha256"]) is None
                or hashlib.sha256(
                    self._canonical(entry["payload"])
                ).hexdigest()
                != entry["sha256"]
            ):
                raise ManagedStartupBindingError(
                    "managed receipt journal entry is invalid"
                )
            seen_generations.add(entry["generation"])
            decoded = self._decode(entry["payload"])
            if type(decoded) is not self._by_id[entry["type"]]:
                raise ManagedStartupBindingError(
                    "managed receipt journal type binding is invalid"
                )
        return value

    def persist(self, step: str, type_id: str, receipt: object) -> object:
        if (
            self._step_types.get(step) != type_id
            or type(receipt) is not self._by_id.get(type_id)
        ):
            raise ManagedStartupBindingError(
                "managed receipt does not match its registered type"
            )
        payload = self._encode(receipt)
        payload_sha = hashlib.sha256(self._canonical(payload)).hexdigest()
        with self._locked():
            value, fingerprint = self._read()
            value = self._validate_journal(value)
            current = value["receipts"].get(step)
            if current and (
                current["type"],
                current["sha256"],
            ) == (type_id, payload_sha):
                return receipt
            if (
                step not in value["receipts"]
                and len(value["receipts"]) >= self.MAX_ENTRIES
            ):
                raise ManagedStartupBindingError(
                    "managed receipt journal entry budget is exhausted"
                )
            generation = value["generation"] + 1
            updated = {
                **value,
                "generation": generation,
                "receipts": {
                    **value["receipts"],
                    step: {
                        "generation": generation,
                        "type": type_id,
                        "payload": payload,
                        "sha256": payload_sha,
                    },
                },
            }
            self._validate_journal(updated)
            self._write(updated, expected=fingerprint)
        return receipt

    def load(self, step: str, type_id: str) -> object | None:
        if self._step_types.get(step) != type_id:
            raise ManagedStartupBindingError(
                "managed receipt step binding is invalid"
            )
        with self._locked():
            value, _fingerprint = self._read()
            value = self._validate_journal(value)
            entry = value["receipts"].get(step)
            if entry is None:
                return None
            if entry["type"] != type_id:
                raise ManagedStartupBindingError(
                    "managed receipt step type changed"
                )
            return self._decode(entry["payload"])

    def attestation(self) -> tuple[int, str]:
        with self._locked():
            value, fingerprint = self._read()
            value = self._validate_journal(value)
            if fingerprint is None:
                raise ManagedStartupBindingError(
                    "managed receipt journal is absent"
                )
            return value["generation"], fingerprint[1]

    def snapshot(
        self,
        step_types: tuple[tuple[str, str], ...],
    ) -> tuple[int, str, tuple[tuple[str, object | None], ...]]:
        if step_types != self._ordered_step_types:
            raise ManagedStartupBindingError(
                "managed receipt snapshot binding changed"
            )
        with self._locked():
            value, fingerprint = self._read()
            value = self._validate_journal(value)
            if fingerprint is None:
                raise ManagedStartupBindingError(
                    "managed receipt journal is absent"
                )
            receipts = []
            for name, type_id in step_types:
                entry = value["receipts"].get(name)
                if entry is None:
                    receipts.append((name, None))
                elif entry["type"] != type_id:
                    raise ManagedStartupBindingError(
                        "managed receipt snapshot type changed"
                    )
                else:
                    receipts.append((name, self._decode(entry["payload"])))
            return value["generation"], fingerprint[1], tuple(receipts)


def _is_frozen_dataclass(value: object) -> bool:
    params = getattr(type(value), "__dataclass_params__", None)
    return (
        value is not None
        and is_dataclass(value)
        and params is not None
        and params.frozen is True
    )


def _verification_parts(value: object) -> tuple[object, object | None]:
    outcome = getattr(value, "outcome", None)
    receipt = getattr(value, "receipt", None)
    if receipt is None and _is_frozen_dataclass(value):
        receipt_fields = {field.name for field in fields(value)}
        # Several strict verifiers return the receipt fields directly.
        if "outcome" in receipt_fields and "reason" not in receipt_fields:
            receipt = value
    raw = getattr(outcome, "value", outcome)
    if type(raw) is not str:
        raise ManagedStartupBindingError("managed verifier returned no typed outcome")
    return raw, receipt


def _map_outcome(raw: str, *, safe_partial: bool) -> Reconciliation:
    normalized = raw.strip().lower().replace("_", "-")
    if normalized in {"complete", "proved-complete"}:
        return Reconciliation.PROVED_COMPLETE
    if normalized in {"absent", "proved-absent"}:
        return Reconciliation.PROVED_ABSENT
    if normalized == "proved-retry-safe-partial":
        return Reconciliation.PROVED_RETRY_SAFE_PARTIAL
    if normalized == "partial":
        return (
            Reconciliation.PROVED_RETRY_SAFE_PARTIAL
            if safe_partial
            else Reconciliation.PARTIAL
        )
    if normalized == "ambiguous":
        return Reconciliation.AMBIGUOUS
    raise ManagedStartupBindingError("managed verifier returned an unknown outcome")


@dataclass(slots=True)
class ManagedStartupCoordinator:
    transaction_id: str
    manifest_receipt: DeferredStartupManifestReceipt
    driver: DeferredStartupFileDriver
    steps: tuple[DeferredStartupStep, ...]
    receipt_store: DurableStartupReceiptStore
    _operation_types: tuple[tuple[str, str], ...]
    configuration_journal: Path

    def driver_attestation(self):
        return self.driver.attestation_receipt()

    def step_receipt_bundle(self) -> ManagedStartupReceiptBundle:
        generation, journal_sha, receipts = self.receipt_store.snapshot(
            self._operation_types
        )
        return ManagedStartupReceiptBundle(
            transaction_id=self.transaction_id,
            manifest_sha256=self.manifest_receipt.sha256,
            configuration_journal=os.fspath(self.configuration_journal),
            receipt_journal_generation=generation,
            receipt_journal_sha256=journal_sha,
            receipts=receipts,
        )


def build_async_profile_manifest(
    rows: object,
    *,
    generation: str,
) -> AsyncProfileManifest:
    if (
        type(generation) is not str
        or _PROFILE_RE.fullmatch(generation) is None
        or not isinstance(rows, (list, tuple))
    ):
        raise ManagedStartupBindingError("async profile authority is invalid")
    canonical = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ManagedStartupBindingError("async profile inventory is invalid")
        profile_id = row.get("name")
        raw_path = row.get("path")
        if (
            type(profile_id) is not str
            or _PROFILE_RE.fullmatch(profile_id) is None
            or profile_id in seen
        ):
            raise ManagedStartupBindingError(
                "async profile inventory is incomplete or duplicated"
            )
        path = _canonical_file(raw_path, name="async profile tracker authority")
        try:
            parent = path.resolve(strict=True)
        except OSError as exc:
            raise ManagedStartupBindingError(
                "async profile authority is unavailable"
            ) from exc
        if parent != path:
            raise ManagedStartupBindingError(
                "async profile authority is not canonical"
            )
        seen.add(profile_id)
        canonical.append((profile_id, path))
    canonical.sort(key=lambda item: item[0])
    source = [
        {"profile_id": profile_id, "profile_path": os.fspath(path)}
        for profile_id, path in canonical
    ]
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AsyncProfileManifest(
        generation=generation,
        profiles=tuple(
            AsyncProfile(profile_id, path / "async_delegations.json")
            for profile_id, path in canonical
        ),
        expected_profile_ids=tuple(profile_id for profile_id, _ in canonical),
        source_digest=digest,
    )


def build_managed_startup_coordinator(
    *,
    environment: Mapping[str, str],
    operations: tuple[ManagedStartupOperation, ...],
    receipt_codecs: tuple[ManagedStartupReceiptCodec, ...],
) -> ManagedStartupCoordinator:
    transaction_id = environment.get("HERMES_WEBUI_STARTUP_TRANSACTION_ID")
    manifest_sha = environment.get(
        "HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256"
    )
    if (
        type(transaction_id) is not str
        or _TRANSACTION_RE.fullmatch(transaction_id) is None
    ):
        raise ManagedStartupBindingError("managed startup transaction is invalid")
    canonical_sha = release_manifest.deferred_release_manifest_sha256()
    if (
        type(manifest_sha) is not str
        or _SHA256_RE.fullmatch(manifest_sha) is None
        or manifest_sha != canonical_sha
    ):
        raise ManagedStartupBindingError(
            "managed startup manifest does not match the canonical manifest"
        )
    attempt_journal = _canonical_file(
        environment.get(_ATTEMPT_JOURNAL_ENV),
        name=_ATTEMPT_JOURNAL_ENV,
    )
    configuration_journal = _canonical_file(
        environment.get(_CONFIG_JOURNAL_ENV),
        name=_CONFIG_JOURNAL_ENV,
    )
    if attempt_journal == configuration_journal:
        raise ManagedStartupBindingError("managed startup journals overlap")

    descriptors = release_manifest.webui_startup_descriptors(
        release_manifest.deferred_release_manifest(),
        startup_admission_closed=True,
    )
    expected = tuple((item.name, item.operation) for item in descriptors)
    actual = tuple((item.name, item.operation) for item in operations)
    if actual != expected or any(
        type(item) is not ManagedStartupOperation
        or not callable(item.mutator)
        or not callable(item.verifier)
        or type(item.receipt_type_id) is not str
        for item in operations
    ):
        raise ManagedStartupBindingError(
            "managed startup capabilities do not match the canonical manifest"
        )

    manifest_receipt = DeferredStartupManifestReceipt(
        transaction_id=transaction_id,
        version=release_manifest.MANIFEST_VERSION,
        sha256=canonical_sha,
    )
    driver = DeferredStartupFileDriver(
        attempt_journal,
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
    )
    receipt_store = DurableStartupReceiptStore(
        attempt_journal.with_name(attempt_journal.name + ".receipts"),
        transaction_id=transaction_id,
        manifest_sha256=canonical_sha,
        codecs=receipt_codecs,
        step_types=tuple(
            (operation.name, operation.receipt_type_id)
            for operation in operations
        ),
    )
    steps = []
    for operation in operations:
        safe_partial = operation.name in _SAFE_PARTIAL_STEPS

        def mutate(operation=operation):
            receipt = operation.mutator()
            if not _is_frozen_dataclass(receipt):
                raise ManagedStartupBindingError(
                    "managed mutator returned a mutable or untyped receipt"
                )
            return receipt_store.persist(
                operation.name,
                operation.receipt_type_id,
                receipt,
            )

        def reconcile(
            operation=operation,
            safe_partial=safe_partial,
        ):
            try:
                prior = receipt_store.load(
                    operation.name,
                    operation.receipt_type_id,
                )
                if prior is None:
                    return Reconciliation.AMBIGUOUS
                verification = operation.verifier(prior)
                raw, verified_receipt = _verification_parts(verification)
                result = _map_outcome(raw, safe_partial=safe_partial)
            except Exception:
                return Reconciliation.AMBIGUOUS
            if (
                result is Reconciliation.PROVED_COMPLETE
                and verified_receipt != prior
            ):
                return Reconciliation.AMBIGUOUS
            return result

        steps.append(
            DeferredStartupStep(
                name=operation.name,
                mutator=mutate,
                reconciler=reconcile,
                prior_completion_absent_policy=(
                    PriorCompletionAbsentPolicy.ALLOW_RERUN
                    if operation.name in _RERUN_IF_ABSENT_STEPS
                    else PriorCompletionAbsentPolicy.DENY
                ),
                retry_safe_partial_policy=(
                    RetrySafePartialPolicy.ALLOW
                    if safe_partial
                    else RetrySafePartialPolicy.DENY
                ),
            )
        )
    return ManagedStartupCoordinator(
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
        driver=driver,
        steps=tuple(steps),
        receipt_store=receipt_store,
        _operation_types=tuple(
            (operation.name, operation.receipt_type_id)
            for operation in operations
        ),
        configuration_journal=configuration_journal,
    )


def _required_callable(owner: object, name: str, *, capability: str):
    value = getattr(owner, name, None)
    if not callable(value):
        raise ManagedStartupBindingError(
            f"paired runtime lacks read-only {capability} verifier"
        )
    return value


def _bind_configuration_journal(module, path: Path) -> Path:
    configure = _required_callable(
        module,
        "configure_managed_startup_configuration_journal",
        capability="startup configuration journal",
    )
    try:
        configured = configure(path)
    except Exception as exc:
        raise ManagedStartupBindingError(
            "managed startup configuration journal binding failed"
        ) from exc
    if not isinstance(configured, Path) or configured != path:
        raise ManagedStartupBindingError(
            "managed startup configuration journal was rebound"
        )
    return configured


def _verified_receipt_after(mutate, verify):
    mutate()
    result = verify(None)
    raw, receipt = _verification_parts(result)
    if _map_outcome(raw, safe_partial=False) is not Reconciliation.PROVED_COMPLETE:
        raise ManagedStartupBindingError(
            "managed mutation did not produce a proved-complete receipt"
        )
    if not _is_frozen_dataclass(receipt):
        raise ManagedStartupBindingError(
            "managed mutation did not produce an immutable receipt"
        )
    return receipt


def _verify_recreatable_process_local(
    verifier,
    receipt,
    *,
    foreign_epoch_reasons: frozenset[str],
):
    result = verifier(receipt)
    raw, _verified = _verification_parts(result)
    if (
        _map_outcome(raw, safe_partial=False) is Reconciliation.AMBIGUOUS
        and getattr(result, "reason", None) in foreign_epoch_reasons
    ):
        absent = verifier(None)
        absent_raw, _ = _verification_parts(absent)
        if (
            _map_outcome(absent_raw, safe_partial=False)
            is Reconciliation.PROVED_ABSENT
        ):
            return _Verification("PROVED_ABSENT", None)
    return result


def _background_verification(drain, reaper, receipt):
    if receipt is not None:
        from api.managed_background_workers import current_process_epoch

        current = current_process_epoch()
        if (
            current is None
            or receipt.drain.process_epoch != current
            or receipt.reaper.process_epoch != current
        ):
            drain_absent = drain(None)
            reaper_absent = reaper(None)
            drain_raw, _ = _verification_parts(drain_absent)
            reaper_raw, _ = _verification_parts(reaper_absent)
            if {
                _map_outcome(drain_raw, safe_partial=True),
                _map_outcome(reaper_raw, safe_partial=True),
            } == {Reconciliation.PROVED_ABSENT}:
                return _Verification("PROVED_ABSENT", None)
            return _Verification("AMBIGUOUS", receipt)
    drain_result = drain(receipt.drain if receipt is not None else None)
    reaper_result = reaper(receipt.reaper if receipt is not None else None)
    drain_raw, drain_receipt = _verification_parts(drain_result)
    reaper_raw, reaper_receipt = _verification_parts(reaper_result)
    values = {
        _map_outcome(drain_raw, safe_partial=True),
        _map_outcome(reaper_raw, safe_partial=True),
    }
    if Reconciliation.AMBIGUOUS in values:
        outcome = "AMBIGUOUS"
    elif values == {Reconciliation.PROVED_COMPLETE}:
        outcome = "PROVED_COMPLETE"
    elif values == {Reconciliation.PROVED_ABSENT}:
        outcome = "PROVED_ABSENT"
    else:
        outcome = "PARTIAL"
    combined = None
    if drain_receipt is not None and reaper_receipt is not None:
        combined = _BackgroundReceipt(drain_receipt, reaper_receipt)
    return _Verification(outcome, combined)


def build_production_managed_startup_coordinator(
    *,
    environment: Mapping[str, str] | None = None,
) -> ManagedStartupCoordinator:
    """Resolve every canonical step to a strict production capability.

    Agent and continuation recovery must provide separate read-only verifiers.
    A recovery method that internally post-checks its own mutation is not a
    replay reconciler and is deliberately insufficient here.
    """

    environment = os.environ if environment is None else environment
    transaction_id = environment.get("HERMES_WEBUI_STARTUP_TRANSACTION_ID", "")
    manifest_sha = release_manifest.deferred_release_manifest_sha256()

    from api import (
        atomic_recovery,
        auth,
        background_process,
        config,
        goal_continuation,
        managed_startup_configuration,
        managed_continuation_recovery,
        managed_startup_session_boundary,
        models,
        plugins,
        profiles,
        routes,
        startup,
        tool_limit_continuation,
    )
    from api.managed_startup_session_boundary import (
        attest_managed_startup_session_boundary,
        verify_managed_startup_session_boundary,
    )
    from api import managed_background_workers
    from api import managed_startup_profile
    import managed_startup_provider_models
    from managed_startup_directories import (
        ensure_managed_startup_directories,
        verify_managed_startup_directories,
    )
    import managed_startup_directories
    from tools import async_delegation
    from tools import durable_state
    from tools import process_registry as process_registry_module
    from tools.process_registry import process_registry

    active_transaction = str(
        getattr(config, "_RUN_ADMISSION_TRANSACTION_ID", "") or ""
    )
    if (
        not config.startup_run_admission_is_closed()
        or not config._managed_release_selected_from_environment()
        or active_transaction != transaction_id
        or environment.get("HERMES_WEBUI_DEFERRED_RELEASE_MANIFEST_SHA256")
        != manifest_sha
    ):
        raise ManagedStartupBindingError(
            "managed startup admission binding is absent or noncanonical"
        )

    verify_process = _required_callable(
        process_registry,
        "verify_managed_startup_exact",
        capability="process recovery",
    )
    verify_async = _required_callable(
        async_delegation,
        "verify_managed_async_delegations_exact",
        capability="async delegation recovery",
    )
    verify_tool = _required_callable(
        tool_limit_continuation,
        "verify_managed_continuations_exact",
        capability="tool-limit continuation recovery",
    )
    verify_goal = _required_callable(
        goal_continuation,
        "verify_managed_continuations_exact",
        capability="goal continuation recovery",
    )

    configuration_journal = _canonical_file(
        environment.get(_CONFIG_JOURNAL_ENV),
        name=_CONFIG_JOURNAL_ENV,
    )
    _bind_configuration_journal(
        managed_startup_configuration,
        configuration_journal,
    )

    desired_directories = tuple(
        os.fspath(path)
        for path in (
            config.STATE_DIR,
            config.SESSION_DIR,
            config.DEFAULT_WORKSPACE,
        )
    )

    def credential_mutator():
        return _verified_receipt_after(
            startup.strict_fix_credential_permissions,
            lambda _receipt: startup.verify_strict_credential_permissions(),
        )

    def credential_verifier(receipt):
        result = startup.verify_strict_credential_permissions()
        if receipt is not None and result.receipt is not None:
            # ``changed`` is historical mutation evidence; all other fields are
            # the exact current permission authority.
            same = all(
                getattr(result.receipt, field.name) == getattr(receipt, field.name)
                for field in fields(receipt)
                if field.name != "changed"
            )
            if not same:
                return _Verification("AMBIGUOUS", receipt)
            return _Verification(result.outcome, receipt)
        return result

    def key_mutator():
        atomic_recovery.ensure_managed_internal_recovery_key()
        result = auth.verify_strict_signing_key()
        if result.persistence is None or result.cache is None:
            raise ManagedStartupBindingError(
                "managed internal recovery key was not proved complete"
            )
        return atomic_recovery.ManagedInternalRecoveryKeyReceipt(
            result.persistence,
            result.cache,
        )

    def key_verifier(receipt):
        result = auth.verify_strict_signing_key()
        observed = (
            atomic_recovery.ManagedInternalRecoveryKeyReceipt(
                result.persistence, result.cache
            )
            if result.persistence is not None and result.cache is not None
            else None
        )
        if (
            receipt is not None
            and result.persistence == receipt.persistence
            and result.cache is None
            and getattr(result.outcome, "value", result.outcome) == "partial"
        ):
            return _Verification("PROVED_RETRY_SAFE_PARTIAL", receipt)
        if receipt is not None and observed != receipt:
            return _Verification("AMBIGUOUS", receipt)
        return _Verification(result.outcome, observed)

    def directory_mutator():
        ensure_managed_startup_directories(desired_directories)
        result = verify_managed_startup_directories(desired_directories)
        if result.receipt is None:
            raise ManagedStartupBindingError(
                "managed startup directories produced no receipt"
            )
        return result.receipt

    def directory_verifier(_receipt):
        return verify_managed_startup_directories(desired_directories)

    def plugin_mutator():
        result = plugins.reconcile_strict_managed_plugins()
        if result.receipt is None:
            raise ManagedStartupBindingError("managed plugins produced no receipt")
        return result.receipt

    def session_mutator():
        return attest_managed_startup_session_boundary(
            config.SESSION_DIR,
            models._active_state_db_path(),
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha,
        )

    def session_verifier(receipt):
        return verify_managed_startup_session_boundary(
            receipt,
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha,
        )

    def authoritative_async_manifest():
        local = build_async_profile_manifest(
            profiles.list_profiles_api(),
            generation=f"release_{manifest_sha[:32]}",
        )
        profile_type = async_delegation.ManagedAsyncDelegationProfile
        manifest_type = async_delegation.ManagedAsyncDelegationProfileManifest
        return manifest_type(
            local.generation,
            tuple(
                profile_type(item.profile_id, item.tracker_path)
                for item in local.profiles
            ),
            local.expected_profile_ids,
            local.source_digest,
        )

    async_outbox = Path(config.STATE_DIR) / (
        "managed_async_delegation_completion_outbox.json"
    )

    def async_mutator():
        manifest = authoritative_async_manifest()
        return async_delegation.recover_managed_async_delegations_exact(
            manifest,
            outbox_path=async_outbox,
            completion_queue=process_registry.completion_queue,
        )

    def async_verifier(receipt):
        manifest = authoritative_async_manifest()
        if (
            receipt is not None
            and (
                receipt.manifest_generation != manifest.generation
                or receipt.manifest_source_digest != manifest.source_digest
            )
        ):
            return _Verification("AMBIGUOUS", receipt)
        result = verify_async(
            receipt,
            manifest,
            completion_queue=process_registry.completion_queue,
        )
        return _Verification(result.outcome, receipt)

    def tool_mutator():
        return tool_limit_continuation.recover_managed_continuations_exact(
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha,
            start=lambda session_id, prompt: routes.start_session_turn(
                session_id,
                prompt,
                source="tool_limit_continuation",
            ),
        )

    def goal_mutator():
        return goal_continuation.recover_managed_goal_continuations_exact(
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha,
            start=lambda session_id, prompt: routes.start_session_turn(
                session_id,
                prompt,
                source="goal_continuation",
            ),
        )

    def background_mutator():
        drain = background_process.start_managed_drain_worker().receipt
        try:
            reaper = (
                background_process.start_managed_session_channel_reaper().receipt
            )
        except Exception:
            background_process.stop_managed_drain_worker()
            raise
        return _BackgroundReceipt(drain, reaper)

    operations_by_name = {
        "credential_permissions": (credential_mutator, credential_verifier),
        "internal_recovery_key": (key_mutator, key_verifier),
        "state_directories": (directory_mutator, directory_verifier),
        "startup_profile_state": (
            config.apply_startup_profile_state,
            lambda receipt: _verify_recreatable_process_local(
                config.verify_startup_profile_state,
                receipt,
                foreign_epoch_reasons=frozenset(
                    {"managed_profile_receipt_from_foreign_epoch"}
                ),
            ),
        ),
        "provider_model_seed": (
            config.seed_startup_provider_models,
            lambda receipt: _verify_recreatable_process_local(
                config.verify_startup_provider_models,
                receipt,
                foreign_epoch_reasons=frozenset(
                    {"managed_provider_models_receipt_without_epoch_state"}
                ),
            ),
        ),
        "startup_configuration": (
            config.apply_deferred_startup_configuration,
            lambda receipt: _verify_recreatable_process_local(
                config.verify_deferred_startup_configuration,
                receipt,
                foreign_epoch_reasons=frozenset(
                    {"configuration_receipt_from_foreign_epoch"}
                ),
            ),
        ),
        "session_recovery": (session_mutator, session_verifier),
        "plugins": (plugin_mutator, lambda _receipt: plugins.verify_strict_managed_plugins()),
        "process_completion_recovery": (
            process_registry.recover_managed_startup_exact,
            lambda receipt: _Verification(
                verify_process(receipt).outcome,
                receipt,
            ),
        ),
        "async_delegation_recovery": (async_mutator, async_verifier),
        "tool_limit_continuation_recovery": (
            tool_mutator,
            lambda receipt: _Verification(
                verify_tool(
                    receipt,
                    transaction_id=transaction_id,
                    manifest_sha256=manifest_sha,
                ).outcome,
                receipt,
            ),
        ),
        "goal_continuation_recovery": (
            goal_mutator,
            lambda receipt: _Verification(
                verify_goal(
                    receipt,
                    transaction_id=transaction_id,
                    manifest_sha256=manifest_sha,
                ).outcome,
                receipt,
            ),
        ),
        "background_services": (
            background_mutator,
            lambda receipt: _background_verification(
                background_process.verify_managed_drain_worker,
                background_process.verify_managed_session_channel_reaper,
                receipt,
            ),
        ),
    }
    descriptors = release_manifest.webui_startup_descriptors(
        release_manifest.deferred_release_manifest(),
        startup_admission_closed=True,
    )
    operations = tuple(
        ManagedStartupOperation(
            descriptor.name,
            descriptor.operation,
            *operations_by_name[descriptor.name],
            {
                "credential_permissions": "webui.credential-receipt.v1",
                "internal_recovery_key": "webui.recovery-key-receipt.v1",
                "state_directories": "webui.directories-receipt.v1",
                "startup_profile_state": "webui.profile-receipt.v1",
                "provider_model_seed": "webui.provider-models-receipt.v1",
                "startup_configuration": "webui.configuration-receipt.v1",
                "session_recovery": "webui.session-boundary-receipt.v1",
                "plugins": "webui.plugins-receipt.v1",
                "process_completion_recovery": "agent.process-recovery-receipt.v1",
                "async_delegation_recovery": "agent.async-recovery-receipt.v1",
                "tool_limit_continuation_recovery": "webui.continuation-receipt.v1",
                "goal_continuation_recovery": "webui.continuation-receipt.v1",
                "background_services": "webui.background-receipt.v1",
            }[descriptor.name],
        )
        for descriptor in descriptors
    )
    codecs = (
        ManagedStartupReceiptCodec(
            "webui.credential-receipt.v1",
            startup.ManagedCredentialPermissionReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.credential-status.v1",
            startup.ManagedCredentialPermissionStatus,
        ),
        ManagedStartupReceiptCodec(
            "webui.recovery-key-receipt.v1",
            atomic_recovery.ManagedInternalRecoveryKeyReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.signing-key-persistence.v1",
            auth.ManagedSigningKeyPersistenceReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.signing-key-cache.v1",
            auth.ManagedSigningKeyCacheReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.directories-receipt.v1",
            managed_startup_directories.ManagedStartupDirectoriesReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.directory-evidence.v1",
            managed_startup_directories.ManagedStartupDirectoryEvidence,
        ),
        ManagedStartupReceiptCodec(
            "webui.profile-receipt.v1",
            managed_startup_profile.ManagedStartupProfileReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.profile-process-epoch.v1",
            managed_startup_profile.ProcessEpoch,
        ),
        ManagedStartupReceiptCodec(
            "webui.provider-models-receipt.v1",
            managed_startup_provider_models.ManagedStartupProviderModelsReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.provider-models-process-epoch.v1",
            managed_startup_provider_models.ProcessEpoch,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-receipt.v1",
            managed_startup_configuration.ManagedStartupConfigurationReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-process-epoch.v1",
            managed_startup_configuration.ProcessEpoch,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-release-binding.v1",
            managed_startup_configuration.ReleaseBinding,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-settings-receipt.v1",
            managed_startup_configuration.DurableSettingsReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-parent-evidence.v1",
            managed_startup_configuration.SettingsParentEvidence,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-file-evidence.v1",
            managed_startup_configuration.SettingsFileEvidence,
        ),
        ManagedStartupReceiptCodec(
            "webui.configuration-cli-receipt.v1",
            managed_startup_configuration.ProcessCliToolsetsReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.session-boundary-receipt.v1",
            managed_startup_session_boundary.ManagedStartupSessionBoundaryReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.session-outcome.v1",
            managed_startup_session_boundary.SessionRecoveryOutcome,
        ),
        ManagedStartupReceiptCodec(
            "webui.plugins-receipt.v1",
            plugins.ManagedPluginSnapshotReceipt,
        ),
        ManagedStartupReceiptCodec(
            "agent.process-recovery-receipt.v1",
            process_registry_module.ManagedProcessRecoveryReceipt,
        ),
        ManagedStartupReceiptCodec(
            "agent.file-identity.v1",
            durable_state.FileIdentity,
        ),
        ManagedStartupReceiptCodec(
            "agent.process-recovery-outcome.v1",
            process_registry_module.ManagedProcessRecoveryOutcome,
        ),
        ManagedStartupReceiptCodec(
            "agent.async-recovery-receipt.v1",
            async_delegation.ManagedAsyncDelegationRecoveryReceipt,
        ),
        ManagedStartupReceiptCodec(
            "agent.async-event-postcondition.v1",
            async_delegation.ManagedAsyncEventPostcondition,
        ),
        ManagedStartupReceiptCodec(
            "agent.async-recovery-outcome.v1",
            async_delegation.ManagedAsyncDelegationRecoveryOutcome,
        ),
        ManagedStartupReceiptCodec(
            "webui.continuation-receipt.v1",
            managed_continuation_recovery.ManagedContinuationRecoveryReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.continuation-store-identity.v1",
            managed_continuation_recovery.StoreIdentity,
        ),
        ManagedStartupReceiptCodec(
            "webui.continuation-outcome.v1",
            managed_continuation_recovery.ManagedContinuationOutcome,
        ),
        ManagedStartupReceiptCodec(
            "webui.background-receipt.v1",
            _BackgroundReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.background-worker-receipt.v1",
            managed_background_workers.ManagedBackgroundWorkerReceipt,
        ),
        ManagedStartupReceiptCodec(
            "webui.background-process-epoch.v1",
            managed_background_workers.ProcessEpoch,
        ),
    )
    return build_managed_startup_coordinator(
        environment=environment,
        operations=operations,
        receipt_codecs=codecs,
    )
