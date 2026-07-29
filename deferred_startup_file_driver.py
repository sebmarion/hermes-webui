"""Private durable file journal for deferred startup replay.

Durability threat contract: the private anchor detects journal-only and other
asymmetric rollback while the bound private parent directory remains stable.
Coordinated rollback or replacement of the entire parent is outside this
driver's trust domain and MUST be detected by controller binding of
``attestation_receipt()`` in the independent main transaction journal before
the deferred-startup boundary.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from deferred_startup_replay import (
    DeferredStartupBindingError,
    DeferredStartupManifestReceipt,
    DeferredStartupStepState,
    PriorCompletionAbsentPolicy,
)


JOURNAL_VERSION = 2
ANCHOR_VERSION = 2
DEFAULT_MAX_BYTES = 1024 * 1024
MAX_CONFIGURABLE_BYTES = 4 * 1024 * 1024
MAX_RECOVERY_ARTIFACTS = 32
AFTER_TEMP_FSYNC = "after-temp-fsync"
BEFORE_DISPLACEMENT = "before-displacement"
AFTER_DISPLACEMENT = "after-displacement"
BEFORE_PUBLISH = "before-publish"
AFTER_PUBLISH = "after-publish"
AFTER_RESTORE_LINK = "after-restore-link"
AFTER_REPLACE = AFTER_PUBLISH
AFTER_JOURNAL_BEFORE_ANCHOR = "after-journal-before-anchor"

_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_PROCESS_EPOCH_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_STEP_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[a-z][a-z0-9-]{0,127}")
_SENSITIVE_JOURNAL_KEYS = {
    "authorization",
    "cookie",
    "fence_token",
    "set-cookie",
    "x-hermes-release-fence",
}
_SENSITIVE_VALUE_MARKERS = (
    "authorization",
    "bearer",
    "cookie",
    "fence-token",
    "fence_token",
    "secret",
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

FileFingerprint = tuple[int, int, int, int, int]
FileReceipt = tuple[FileFingerprint, str]


@dataclass(frozen=True, slots=True)
class DeferredStartupFileAttestation:
    """Immutable controller-binding receipt for the driver's durable state."""

    schema_version: int
    transaction_id: str
    manifest_version: int
    manifest_sha256: str
    parent_device: int
    parent_inode: int
    journal_generation: int
    journal_sha256: str
    anchor_generation: int
    anchor_sha256: str
    latest_process_epoch: str | None
    attempt_count: int
    attempt_topology_sha256: str
    status: str

    def as_dict(self) -> dict:
        """Return a fresh exact-schema mapping suitable for canonical hashing."""

        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "manifest_receipt": {
                "version": self.manifest_version,
                "sha256": self.manifest_sha256,
            },
            "parent_identity": {
                "device": self.parent_device,
                "inode": self.parent_inode,
            },
            "journal": {
                "generation": self.journal_generation,
                "sha256": self.journal_sha256,
            },
            "anchor": {
                "generation": self.anchor_generation,
                "sha256": self.anchor_sha256,
            },
            "attempt_topology": {
                "latest_process_epoch": self.latest_process_epoch,
                "attempt_count": self.attempt_count,
                "sha256": self.attempt_topology_sha256,
            },
            "status": self.status,
        }


class DeferredStartupFileDriverError(DeferredStartupBindingError):
    """The durable startup journal cannot be trusted or updated safely."""


CrashHook = Callable[[str], None]


class DeferredStartupFileDriver:
    """Lock-serialized replay driver with stable-parent rollback detection.

    Its anchor covers asymmetric rollback only. A controller MUST bind
    :meth:`attestation_receipt` in an independent transaction journal to detect
    coordinated replacement or rollback of this driver's entire private parent.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        max_bytes: int = DEFAULT_MAX_BYTES,
        _crash_hook: CrashHook | None = None,
    ) -> None:
        if _NOFOLLOW == 0:
            raise DeferredStartupFileDriverError(
                "O_NOFOLLOW is required for the startup journal"
            )
        journal_path = Path(path)
        if (
            not journal_path.is_absolute()
            or ".." in journal_path.parts
            or Path(os.path.abspath(journal_path)) != journal_path
        ):
            raise DeferredStartupFileDriverError(
                "startup journal path is not absolute and canonical"
            )
        self._path = journal_path
        self._parent = journal_path.parent
        self._journal_name = journal_path.name
        self._lock_name = journal_path.name + ".lock"
        self._anchor_name = journal_path.name + ".anchor"
        temp_targets = (
            re.escape(self._journal_name),
            re.escape(self._anchor_name),
        )
        artifact_target = rf"({'|'.join(temp_targets)})"
        self._artifact_name_re = re.compile(
            rf"\.{artifact_target}\.([0-9a-f]{{32}})\.(tmp|bak)"
        )
        self._quarantine_name_re = re.compile(
            rf"\.{artifact_target}\.([0-9a-f]{{32}})\.(tmp|bak)"
            rf"\.([0-9a-f]{{32}})\.qtn"
        )
        self._transaction_id = self._validate_transaction_id(transaction_id)
        self._manifest_receipt = self._validate_bound_receipt(
            transaction_id,
            manifest_receipt,
        )
        if (
            type(max_bytes) is not int
            or max_bytes < 1
            or max_bytes > MAX_CONFIGURABLE_BYTES
        ):
            raise DeferredStartupFileDriverError(
                "startup journal size bound is invalid"
            )
        if _crash_hook is not None and not callable(_crash_hook):
            raise DeferredStartupFileDriverError(
                "startup journal crash hook is invalid"
            )
        self._max_bytes = max_bytes
        self._crash_hook = _crash_hook
        self._state_lock = threading.RLock()
        self._bound_parent_identity: tuple[int, int] | None = None
        self._last_generation: int | None = None
        self._last_canonical_sha256: str | None = None
        self._last_steps: tuple[tuple[str, object], ...] = ()
        with self._open_parent() as parent_fd:
            opened_parent = os.fstat(parent_fd)
            self._bound_parent_identity = (
                opened_parent.st_dev,
                opened_parent.st_ino,
            )
        with self._state_lock:
            with self._locked() as parent_fd:
                journal, journal_receipt = self._read_unlocked(parent_fd)
                self._reconcile_anchor_unlocked(
                    parent_fd,
                    journal,
                    journal_receipt,
                )
            self._accept_observed_journal(journal)

    def attestation_receipt(self) -> DeferredStartupFileAttestation:
        """Return an immutable, secret-free receipt of consistent durable state."""

        with self._state_lock:
            with self._locked() as parent_fd:
                journal, journal_receipt = self._read_unlocked(parent_fd)
                self._reconcile_anchor_unlocked(
                    parent_fd,
                    journal,
                    journal_receipt,
                )
                anchor_result = self._read_anchor_unlocked(parent_fd)
                if anchor_result is None:
                    raise DeferredStartupFileDriverError(
                        "startup anchor is missing for attestation"
                    )
                anchor, _anchor_receipt = anchor_result
                journal_sha256 = self._journal_sha256(journal)
                if (
                    anchor["generation"] != journal["generation"]
                    or anchor["journal_sha256"] != journal_sha256
                ):
                    raise DeferredStartupFileDriverError(
                        "startup journal and anchor are inconsistent"
                    )
                parent_status = os.fstat(parent_fd)
                parent_identity = (
                    parent_status.st_dev,
                    parent_status.st_ino,
                )
                if parent_identity != self._bound_parent_identity:
                    raise DeferredStartupFileDriverError(
                        "startup journal parent changed after binding"
                    )
                anchor_sha256 = hashlib.sha256(
                    self._canonical_journal_bytes(anchor)
                ).hexdigest()
            self._accept_observed_journal(journal)
            return DeferredStartupFileAttestation(
                schema_version=2,
                transaction_id=self._transaction_id,
                manifest_version=self._manifest_receipt.version,
                manifest_sha256=self._manifest_receipt.sha256,
                parent_device=parent_identity[0],
                parent_inode=parent_identity[1],
                journal_generation=journal["generation"],
                journal_sha256=journal_sha256,
                anchor_generation=anchor["generation"],
                anchor_sha256=anchor_sha256,
                latest_process_epoch=self._latest_process_epoch(journal),
                attempt_count=self._attempt_count(journal),
                attempt_topology_sha256=hashlib.sha256(
                    self._canonical_journal_bytes(journal["steps"])
                ).hexdigest(),
                status="stable-parent-consistent",
            )

    def read_step_state(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        prior_completion_absent_policy: PriorCompletionAbsentPolicy = (
            PriorCompletionAbsentPolicy.DENY
        ),
    ) -> DeferredStartupStepState:
        if type(prior_completion_absent_policy) is not PriorCompletionAbsentPolicy:
            raise DeferredStartupFileDriverError(
                "startup journal step policy is invalid"
            )
        process_epoch, step_name = self._validate_call(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step_name,
        )
        with self._state_lock:
            with self._locked() as parent_fd:
                journal, journal_receipt = self._read_unlocked(parent_fd)
                self._reconcile_anchor_unlocked(
                    parent_fd,
                    journal,
                    journal_receipt,
                )
            self._accept_observed_journal(journal)
            record = journal["steps"].get(step_name)
            attempts = [] if record is None else record["attempts"]
            current = next(
                (
                    attempt
                    for attempt in attempts
                    if attempt["process_epoch"] == process_epoch
                ),
                None,
            )
            prior = [
                attempt
                for attempt in attempts
                if attempt["process_epoch"] != process_epoch
            ]
            prior_completion = any("completion" in attempt for attempt in prior)
            prior_indeterminate = any("indeterminate" in attempt for attempt in prior)
            prior_unresolved = any(
                "completion" not in attempt and "indeterminate" not in attempt
                for attempt in prior
            )
            if current is None:
                return DeferredStartupStepState(
                    prior_completion=prior_completion,
                    prior_indeterminate=prior_indeterminate,
                    prior_unresolved=prior_unresolved,
                )
            if (
                current["prior_completion_absent_policy"]
                != prior_completion_absent_policy.value
            ):
                raise DeferredStartupFileDriverError(
                    "startup journal process epoch policy binding does not match"
                )
            if current is not attempts[-1]:
                raise DeferredStartupFileDriverError(
                    "startup stale process epoch is not the newest attempt"
                )
            return DeferredStartupStepState(
                attempt_number=current["attempt"],
                intent=True,
                completion="completion" in current,
                indeterminate="indeterminate" in current,
                prior_completion=prior_completion,
                prior_indeterminate=prior_indeterminate,
                prior_unresolved=prior_unresolved,
            )

    def record_intent(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        prior_completion_absent_policy: PriorCompletionAbsentPolicy = (
            PriorCompletionAbsentPolicy.DENY
        ),
    ) -> None:
        if type(prior_completion_absent_policy) is not PriorCompletionAbsentPolicy:
            raise DeferredStartupFileDriverError(
                "startup journal step policy is invalid"
            )
        self._transition(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step_name,
            "intent",
            {"policy": prior_completion_absent_policy.value},
        )

    def record_completion(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        recovered: bool,
    ) -> None:
        if type(recovered) is not bool:
            raise DeferredStartupFileDriverError(
                "startup completion receipt is invalid"
            )
        self._transition(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step_name,
            "completion",
            {"recovered": recovered},
        )

    def record_indeterminate(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        reason: str,
    ) -> None:
        if type(reason) is str and self._contains_sensitive_value(reason):
            raise DeferredStartupFileDriverError(
                "startup indeterminate receipt contains sensitive data"
            )
        if type(reason) is not str or _REASON_RE.fullmatch(reason) is None:
            raise DeferredStartupFileDriverError(
                "startup indeterminate receipt is invalid"
            )
        self._transition(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step_name,
            "indeterminate",
            {"reason": reason},
        )

    def _transition(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        transition: str,
        payload: dict | None,
    ) -> None:
        process_epoch, step_name = self._validate_call(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step_name,
        )
        with self._state_lock:
            with self._locked() as parent_fd:
                journal, fingerprint = self._read_unlocked(parent_fd)
                self._reconcile_anchor_unlocked(
                    parent_fd,
                    journal,
                    fingerprint,
                )
                self._accept_observed_journal(journal)
                previous_sha256 = self._journal_sha256(journal)
                record = journal["steps"].setdefault(
                    step_name,
                    {"attempts": []},
                )
                attempts = record["attempts"]
                current = next(
                    (
                        attempt
                        for attempt in attempts
                        if attempt["process_epoch"] == process_epoch
                    ),
                    None,
                )
                if transition == "intent":
                    if current is not None:
                        if (
                            current["prior_completion_absent_policy"]
                            != payload["policy"]
                        ):
                            raise DeferredStartupFileDriverError(
                                "startup process epoch has a conflicting policy"
                            )
                        if current is not attempts[-1]:
                            raise DeferredStartupFileDriverError(
                                "startup stale process epoch is not the newest attempt"
                            )
                        if "completion" in current or "indeterminate" in current:
                            raise DeferredStartupFileDriverError(
                                "startup terminal process epoch cannot be reused"
                            )
                        return
                    proposed = {
                        "attempt": len(attempts) + 1,
                        "process_epoch": process_epoch,
                        "prior_completion_absent_policy": payload["policy"],
                        "intent": {"generation": journal["generation"] + 1},
                    }
                    attempts.append(proposed)
                elif current is None:
                    raise DeferredStartupFileDriverError(
                        "startup step transition has no durable intent"
                    )
                elif current is not attempts[-1]:
                    raise DeferredStartupFileDriverError(
                        "startup stale process epoch is not the newest attempt"
                    )
                elif "completion" in current or "indeterminate" in current:
                    existing_payload = current.get(transition)
                    if existing_payload is not None and all(
                        existing_payload.get(key) == value
                        for key, value in (payload or {}).items()
                    ):
                        return
                    raise DeferredStartupFileDriverError(
                        "startup step already has a conflicting receipt"
                    )
                else:
                    proposed = dict(payload or {})
                    proposed["generation"] = journal["generation"] + 1
                    current[transition] = proposed
                journal["previous_sha256"] = previous_sha256
                journal["generation"] += 1
                validated_candidate = self._validate_journal(journal)
                candidate_steps = tuple(
                    (name, self._freeze(record))
                    for name, record in sorted(validated_candidate["steps"].items())
                )
                if not self._is_monotonic_step_extension(
                    self._last_steps,
                    candidate_steps,
                ):
                    raise DeferredStartupFileDriverError(
                        "startup journal candidate is not a monotonic extension"
                    )
                self._write_unlocked(parent_fd, journal, fingerprint)
                self._inject_crash(AFTER_JOURNAL_BEFORE_ANCHOR)
                self._write_anchor_unlocked(parent_fd, journal)
                self._accept_observed_journal(journal)

    def _validate_call(
        self,
        transaction_id: object,
        manifest_receipt: object,
        process_epoch: object,
        step_name: object,
    ) -> tuple[str, str]:
        if transaction_id != self._transaction_id:
            raise DeferredStartupFileDriverError(
                "startup journal transaction binding does not match"
            )
        if (
            type(manifest_receipt) is not DeferredStartupManifestReceipt
            or manifest_receipt != self._manifest_receipt
        ):
            raise DeferredStartupFileDriverError(
                "startup journal manifest binding does not match"
            )
        process_epoch = self._validate_process_epoch(process_epoch)
        if type(step_name) is not str or _STEP_NAME_RE.fullmatch(step_name) is None:
            raise DeferredStartupFileDriverError("startup journal step name is invalid")
        if step_name.strip().lower() in _SENSITIVE_JOURNAL_KEYS:
            raise DeferredStartupFileDriverError(
                "startup journal step name is sensitive"
            )
        return process_epoch, step_name

    @staticmethod
    def _validate_transaction_id(value: object) -> str:
        if type(value) is not str or _TRANSACTION_ID_RE.fullmatch(value) is None:
            raise DeferredStartupFileDriverError(
                "startup journal transaction id is invalid"
            )
        return value

    @staticmethod
    def _validate_process_epoch(value: object) -> str:
        if type(value) is not str or _PROCESS_EPOCH_RE.fullmatch(value) is None:
            raise DeferredStartupFileDriverError(
                "startup journal process epoch is invalid"
            )
        return value

    @staticmethod
    def _validate_bound_receipt(
        transaction_id: str,
        value: object,
    ) -> DeferredStartupManifestReceipt:
        if (
            type(value) is not DeferredStartupManifestReceipt
            or value.transaction_id != transaction_id
            or type(value.version) is not int
            or isinstance(value.version, bool)
            or value.version < 1
            or type(value.sha256) is not str
            or _SHA256_RE.fullmatch(value.sha256) is None
        ):
            raise DeferredStartupFileDriverError(
                "startup journal manifest receipt is invalid"
            )
        return value

    @contextlib.contextmanager
    def _open_parent(self) -> Iterator[int]:
        self._validate_ancestor_chain()
        try:
            descriptor = os.open(
                self._parent,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            )
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                "startup journal parent is unavailable"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise DeferredStartupFileDriverError("startup journal parent is unsafe")
            current = os.stat(self._parent, follow_symlinks=False)
            current_identity = (current.st_dev, current.st_ino)
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
            )
            if current_identity != opened_identity:
                raise DeferredStartupFileDriverError(
                    "startup journal parent changed during access"
                )
            if (
                self._bound_parent_identity is not None
                and opened_identity != self._bound_parent_identity
            ):
                raise DeferredStartupFileDriverError(
                    "startup journal parent identity changed"
                )
            yield descriptor
        finally:
            os.close(descriptor)

    def _validate_ancestor_chain(self) -> None:
        if not self._parent.exists():
            raise DeferredStartupFileDriverError(
                "startup journal parent does not exist"
            )
        try:
            if self._parent.resolve(strict=True) != self._parent:
                raise DeferredStartupFileDriverError(
                    "startup journal parent is not canonical"
                )
            current = Path(self._parent.anchor)
            for component in self._parent.parts[1:]:
                current /= component
                opened = current.lstat()
                if stat.S_ISLNK(opened.st_mode):
                    raise DeferredStartupFileDriverError(
                        "startup journal path has a symlinked ancestor"
                    )
        except DeferredStartupFileDriverError:
            raise
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                "startup journal ancestor is unsafe"
            ) from exc

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        with self._open_parent() as parent_fd:
            flags = os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC
            lock_fd = -1
            for _attempt in range(3):
                try:
                    lock_fd = os.open(
                        self._lock_name,
                        flags,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    break
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise DeferredStartupFileDriverError(
                        "startup journal lock is unavailable"
                    ) from exc
            if lock_fd < 0:
                raise DeferredStartupFileDriverError(
                    "startup journal lock is unavailable"
                )
            try:
                self._validate_opened_file(
                    parent_fd,
                    self._lock_name,
                    lock_fd,
                    "lock",
                )
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                self._validate_opened_file(
                    parent_fd,
                    self._lock_name,
                    lock_fd,
                    "lock",
                )
                self._reconcile_recovery_artifacts_unlocked(parent_fd)
                yield parent_fd
            finally:
                os.close(lock_fd)

    def _read_unlocked(
        self,
        parent_fd: int,
    ) -> tuple[dict, FileReceipt | None]:
        result = self._read_json_unlocked(
            parent_fd,
            self._journal_name,
            "journal",
            missing_ok=True,
        )
        if result is None:
            return self._empty_journal(), None
        raw, receipt = result
        return self._validate_journal(raw), receipt

    def _read_json_unlocked(
        self,
        parent_fd: int,
        name: str,
        label: str,
        *,
        missing_ok: bool,
    ) -> tuple[object, FileReceipt] | None:
        flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
        try:
            descriptor = os.open(
                name,
                flags,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise DeferredStartupFileDriverError(
                f"startup {label} is missing"
            ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise DeferredStartupFileDriverError(
                    f"startup {label} is unsafe"
                ) from exc
            raise DeferredStartupFileDriverError(
                f"startup {label} is unreadable"
            ) from exc
        try:
            opened = self._validate_opened_file(
                parent_fd,
                name,
                descriptor,
                label,
            )
            opened_fingerprint = self._file_fingerprint(opened)
            immutable_payload = self._read_descriptor_bounded(descriptor)
            if len(immutable_payload) > self._max_bytes:
                raise DeferredStartupFileDriverError("startup journal is too large")
            payload_sha256 = hashlib.sha256(immutable_payload).hexdigest()
            try:
                raw = json.loads(
                    immutable_payload,
                    object_pairs_hook=self._reject_duplicate_pairs,
                )
            except DeferredStartupFileDriverError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeferredStartupFileDriverError(
                    f"startup {label} JSON is invalid"
                ) from exc
            parsed_fingerprint = self._file_fingerprint(os.fstat(descriptor))
            os.lseek(descriptor, 0, os.SEEK_SET)
            reread_payload = self._read_descriptor_bounded(descriptor)
            reread_fingerprint = self._file_fingerprint(os.fstat(descriptor))
            if (
                parsed_fingerprint != opened_fingerprint
                or reread_fingerprint != opened_fingerprint
                or reread_payload != immutable_payload
                or hashlib.sha256(reread_payload).hexdigest() != payload_sha256
            ):
                raise DeferredStartupFileDriverError(
                    f"startup {label} bytes changed during access"
                )
            self._validate_opened_file(
                parent_fd,
                name,
                descriptor,
                label,
            )
        finally:
            os.close(descriptor)
        return raw, (opened_fingerprint, payload_sha256)

    def _read_descriptor_bounded(self, descriptor: int) -> bytes:
        payload = bytearray()
        while len(payload) <= self._max_bytes:
            chunk = os.read(
                descriptor,
                min(65536, self._max_bytes + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        return bytes(payload)

    def _write_unlocked(
        self,
        parent_fd: int,
        journal: dict,
        expected_receipt: FileReceipt | None,
    ) -> None:
        validated = self._validate_journal(journal)
        payload = (
            json.dumps(
                validated,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > self._max_bytes:
            raise DeferredStartupFileDriverError("startup journal is too large")
        self._atomic_write_named_unlocked(
            parent_fd,
            self._journal_name,
            payload,
            expected_receipt,
            inject_journal_crashes=True,
        )

    def _atomic_write_named_unlocked(
        self,
        parent_fd: int,
        target_name: str,
        payload: bytes,
        expected_receipt: FileReceipt | None,
        *,
        inject_journal_crashes: bool,
    ) -> None:
        token = secrets.token_hex(16)
        temp_name = f".{target_name}.{token}.tmp"
        backup_name = f".{target_name}.{token}.bak"
        temp_fd = -1
        preserved_receipt: FileReceipt | None = None
        try:
            temp_fd = os.open(
                temp_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            opened = os.fstat(temp_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise DeferredStartupFileDriverError(
                    "startup journal temporary file is unsafe"
                )
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise DeferredStartupFileDriverError(
                        "startup journal write made no progress"
                    )
                view = view[written:]
            os.fsync(temp_fd)
            if inject_journal_crashes:
                self._inject_crash(AFTER_TEMP_FSYNC)
            self._inject_crash(BEFORE_DISPLACEMENT)
            if expected_receipt is not None:
                try:
                    self._rename_noreplace(
                        parent_fd,
                        target_name,
                        backup_name,
                    )
                except FileNotFoundError as exc:
                    raise DeferredStartupFileDriverError(
                        "startup journal changed during displacement"
                    ) from exc
                os.fsync(parent_fd)
                self._inject_crash(AFTER_DISPLACEMENT)
                backup_receipt, _backup_payload = self._read_artifact_unlocked(
                    parent_fd,
                    backup_name,
                    "preserved target",
                    allowed_nlinks={1},
                )
                if not self._receipt_matches_rename(
                    expected_receipt,
                    backup_receipt,
                ):
                    self._restore_no_clobber(
                        parent_fd,
                        backup_name,
                        target_name,
                    )
                    raise DeferredStartupFileDriverError(
                        "startup journal changed during displacement"
                    )
                preserved_receipt = backup_receipt
            self._inject_crash(BEFORE_PUBLISH)
            os.link(
                temp_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
            if inject_journal_crashes:
                self._inject_crash(AFTER_PUBLISH)
            published_temp_receipt = (
                self._file_fingerprint(os.fstat(temp_fd)),
                hashlib.sha256(payload).hexdigest(),
            )
            self._validate_published_temp(
                parent_fd,
                target_name,
                temp_fd,
                payload,
            )
            self._quarantine_and_delete_unlocked(
                parent_fd,
                temp_name,
                expected_receipt=published_temp_receipt,
                label="published temporary file",
                allowed_nlinks={2},
            )
            self._validate_published_payload(
                parent_fd,
                target_name,
                payload,
            )
            if expected_receipt is not None:
                assert preserved_receipt is not None
                self._quarantine_and_delete_unlocked(
                    parent_fd,
                    backup_name,
                    expected_receipt=preserved_receipt,
                    label="preserved target",
                )
        except DeferredStartupFileDriverError:
            raise
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                "startup journal write failed"
            ) from exc
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)

    def _inject_crash(self, point: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(point)

    def _validate_published_temp(
        self,
        parent_fd: int,
        target_name: str,
        temp_fd: int,
        payload: bytes,
    ) -> None:
        try:
            current = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                "startup published target changed during validation"
            ) from exc
        opened = os.fstat(temp_fd)
        if (
            stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 2
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise DeferredStartupFileDriverError(
                "startup published target changed during validation"
            )
        os.lseek(temp_fd, 0, os.SEEK_SET)
        if self._read_descriptor_bounded(temp_fd) != payload:
            raise DeferredStartupFileDriverError(
                "startup published target content changed during validation"
            )

    def _validate_published_payload(
        self,
        parent_fd: int,
        target_name: str,
        payload: bytes,
    ) -> None:
        _receipt, observed = self._read_artifact_unlocked(
            parent_fd,
            target_name,
            "published target",
            allowed_nlinks={1},
        )
        if observed != payload:
            raise DeferredStartupFileDriverError(
                "startup published target content changed during validation"
            )

    @staticmethod
    def _validate_opened_file(
        parent_fd: int,
        name: str,
        descriptor: int,
        label: str,
    ) -> os.stat_result:
        opened = os.fstat(descriptor)
        try:
            current = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                f"startup journal {label} changed during access"
            ) from exc
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise DeferredStartupFileDriverError(
                f"startup journal {label} changed during access"
            )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise DeferredStartupFileDriverError(f"startup journal {label} is unsafe")
        return opened

    @staticmethod
    def _file_fingerprint(opened: os.stat_result) -> FileFingerprint:
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )

    @staticmethod
    def _rename_noreplace(
        parent_fd: int,
        source_name: str,
        destination_name: str,
    ) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        source = os.fsencode(source_name)
        destination = os.fsencode(destination_name)
        if sys.platform == "darwin":
            rename = libc.renameatx_np
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int
            result = rename(
                parent_fd,
                source,
                parent_fd,
                destination,
                0x00000004,
            )
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            rename.restype = ctypes.c_int
            result = rename(
                parent_fd,
                source,
                parent_fd,
                destination,
                0x00000001,
            )
        else:
            raise DeferredStartupFileDriverError(
                "atomic no-clobber rename is unavailable"
            )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                os.strerror(error_number),
                source_name,
                destination_name,
            )

    def _read_artifact_unlocked(
        self,
        parent_fd: int,
        name: str,
        label: str,
        *,
        allowed_nlinks: set[int],
    ) -> tuple[FileReceipt, bytes]:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                f"startup {label} is unreadable"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            try:
                current = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise DeferredStartupFileDriverError(
                    f"startup {label} changed during access"
                ) from exc
            if (
                stat.S_ISLNK(current.st_mode)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink not in allowed_nlinks
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > self._max_bytes
            ):
                raise DeferredStartupFileDriverError(f"startup {label} is unsafe")
            fingerprint = self._file_fingerprint(opened)
            payload = self._read_descriptor_bounded(descriptor)
            after_read = self._file_fingerprint(os.fstat(descriptor))
            if fingerprint != after_read or len(payload) > self._max_bytes:
                raise DeferredStartupFileDriverError(
                    f"startup {label} changed during access"
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            reread = self._read_descriptor_bounded(descriptor)
            after_reread = self._file_fingerprint(os.fstat(descriptor))
            if (
                reread != payload
                or after_reread != fingerprint
                or hashlib.sha256(reread).hexdigest()
                != hashlib.sha256(payload).hexdigest()
            ):
                raise DeferredStartupFileDriverError(
                    f"startup {label} changed during access"
                )
            current_after = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (current_after.st_dev, current_after.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise DeferredStartupFileDriverError(
                    f"startup {label} changed during access"
                )
        finally:
            os.close(descriptor)
        return (fingerprint, hashlib.sha256(payload).hexdigest()), payload

    def _restore_no_clobber(
        self,
        parent_fd: int,
        preserved_name: str,
        target_name: str,
    ) -> None:
        try:
            os.link(
                preserved_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return
        os.fsync(parent_fd)
        self._inject_crash(AFTER_RESTORE_LINK)
        os.unlink(preserved_name, dir_fd=parent_fd)
        os.fsync(parent_fd)

    def _quarantine_and_delete_unlocked(
        self,
        parent_fd: int,
        name: str,
        *,
        expected_receipt: FileReceipt | None,
        label: str,
        allowed_nlinks: set[int] | None = None,
    ) -> None:
        match = self._artifact_name_re.fullmatch(name)
        if match is None:
            raise DeferredStartupFileDriverError(
                "startup recovery artifact name is invalid"
            )
        target_name = match.group(1)
        operation_token = match.group(2)
        artifact_kind = match.group(3)
        quarantine_name = (
            f".{target_name}.{operation_token}.{artifact_kind}."
            f"{secrets.token_hex(16)}.qtn"
        )
        self._rename_noreplace(parent_fd, name, quarantine_name)
        try:
            receipt, _payload = self._read_artifact_unlocked(
                parent_fd,
                quarantine_name,
                label,
                allowed_nlinks={1} if allowed_nlinks is None else allowed_nlinks,
            )
        except DeferredStartupFileDriverError:
            self._restore_no_clobber(parent_fd, quarantine_name, name)
            raise
        if expected_receipt is not None and not self._receipt_matches_rename(
            expected_receipt,
            receipt,
        ):
            self._restore_no_clobber(parent_fd, quarantine_name, name)
            raise DeferredStartupFileDriverError(
                f"startup {label} changed during quarantine"
            )
        os.unlink(quarantine_name, dir_fd=parent_fd)
        os.fsync(parent_fd)

    @staticmethod
    def _receipt_matches_rename(
        before: FileReceipt,
        after: FileReceipt,
    ) -> bool:
        before_fingerprint, before_sha256 = before
        after_fingerprint, after_sha256 = after
        return (
            before_fingerprint[:4] == after_fingerprint[:4]
            and after_fingerprint[4] >= before_fingerprint[4]
            and before_sha256 == after_sha256
        )

    def _reconcile_recovery_artifacts_unlocked(self, parent_fd: int) -> None:
        try:
            names = os.listdir(parent_fd)
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                "startup recovery artifact scan failed"
            ) from exc
        matches = [
            (name, self._artifact_name_re.fullmatch(name))
            for name in names
            if self._artifact_name_re.fullmatch(name) is not None
        ]
        quarantines = [
            (name, self._quarantine_name_re.fullmatch(name))
            for name in names
            if self._quarantine_name_re.fullmatch(name) is not None
        ]
        if len(matches) + len(quarantines) > MAX_RECOVERY_ARTIFACTS:
            raise DeferredStartupFileDriverError("too many startup recovery artifacts")
        for name, match in quarantines:
            assert match is not None
            self._read_artifact_unlocked(
                parent_fd,
                name,
                "quarantined recovery artifact",
                allowed_nlinks={1, 2},
            )
            original_name = f".{match.group(1)}.{match.group(2)}.{match.group(3)}"
            try:
                self._rename_noreplace(
                    parent_fd,
                    name,
                    original_name,
                )
            except FileExistsError as exc:
                raise DeferredStartupFileDriverError(
                    "startup quarantine recovery is ambiguous"
                ) from exc
            os.fsync(parent_fd)
        if quarantines:
            try:
                names = os.listdir(parent_fd)
            except OSError as exc:
                raise DeferredStartupFileDriverError(
                    "startup recovery artifact rescan failed"
                ) from exc
            matches = [
                (name, self._artifact_name_re.fullmatch(name))
                for name in names
                if self._artifact_name_re.fullmatch(name) is not None
            ]
        operations: dict[tuple[str, str], dict[str, str]] = {}
        for name, match in matches:
            if match is None:
                continue
            key = (match.group(1), match.group(2))
            operation = operations.setdefault(key, {})
            kind = match.group(3)
            if kind in operation:
                raise DeferredStartupFileDriverError(
                    "duplicate startup recovery artifact"
                )
            operation[kind] = name
        targets = [key[0] for key in operations]
        if len(targets) != len(set(targets)):
            raise DeferredStartupFileDriverError(
                "multiple startup recovery transactions are ambiguous"
            )
        for (target_name, _token), operation in sorted(operations.items()):
            temp_name = operation.get("tmp")
            backup_name = operation.get("bak")
            target_result = self._read_artifact_if_present(
                parent_fd,
                target_name,
                "recovery target",
                allowed_nlinks={1, 2},
            )
            temp_result = (
                None
                if temp_name is None
                else self._read_artifact_unlocked(
                    parent_fd,
                    temp_name,
                    "recovery temporary file",
                    allowed_nlinks={1, 2},
                )
            )
            backup_result = (
                None
                if backup_name is None
                else self._read_artifact_unlocked(
                    parent_fd,
                    backup_name,
                    "recovery preserved target",
                    allowed_nlinks={1, 2},
                )
            )
            if backup_name is not None and target_result is None:
                assert backup_result is not None
                backup_status = os.stat(
                    backup_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if backup_status.st_nlink != 1:
                    raise DeferredStartupFileDriverError(
                        "startup two-link recovery state is ambiguous"
                    )
                if not self._artifact_proves_prior_target(
                    parent_fd,
                    target_name,
                    backup_result[1],
                ):
                    raise DeferredStartupFileDriverError(
                        "startup preserved target cannot be proven"
                    )
                self._restore_no_clobber(
                    parent_fd,
                    backup_name,
                    target_name,
                )
                if temp_name is not None and temp_result is not None:
                    self._quarantine_and_delete_unlocked(
                        parent_fd,
                        temp_name,
                        expected_receipt=temp_result[0],
                        label="recovery temporary file",
                    )
                continue
            if backup_name is not None:
                assert target_result is not None
                assert backup_result is not None
                backup_status = os.stat(
                    backup_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                target_status = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if backup_status.st_nlink == 2:
                    if (
                        target_status.st_nlink != 2
                        or (target_status.st_dev, target_status.st_ino)
                        != (backup_status.st_dev, backup_status.st_ino)
                        or target_result != backup_result
                        or not self._artifact_proves_prior_target(
                            parent_fd,
                            target_name,
                            backup_result[1],
                        )
                    ):
                        raise DeferredStartupFileDriverError(
                            "startup two-link recovery state is ambiguous"
                        )
                    self._inject_crash(AFTER_RESTORE_LINK)
                    self._quarantine_and_delete_unlocked(
                        parent_fd,
                        backup_name,
                        expected_receipt=backup_result[0],
                        label="restored preserved target",
                        allowed_nlinks={2},
                    )
                    if temp_name is not None and temp_result is not None:
                        self._quarantine_and_delete_unlocked(
                            parent_fd,
                            temp_name,
                            expected_receipt=temp_result[0],
                            label="recovery temporary file",
                        )
                    continue
                target_is_published_temp = (
                    target_status.st_nlink == 2
                    and temp_result is not None
                    and target_result[0][0][0:2] == temp_result[0][0][0:2]
                    and target_result[1] == temp_result[1]
                )
                if target_status.st_nlink != 1 and not target_is_published_temp:
                    raise DeferredStartupFileDriverError(
                        "startup two-link recovery state is ambiguous"
                    )
                if not self._artifact_proves_published_target(
                    parent_fd,
                    target_name,
                    target_result[1],
                ):
                    raise DeferredStartupFileDriverError(
                        "startup published target cannot be proven"
                    )
                if temp_name is not None and temp_result is not None:
                    if (
                        target_result[0][0][0:2] != temp_result[0][0][0:2]
                        or target_result[1] != temp_result[1]
                    ):
                        raise DeferredStartupFileDriverError(
                            "startup recovery target is ambiguous"
                        )
                    self._quarantine_and_delete_unlocked(
                        parent_fd,
                        temp_name,
                        expected_receipt=temp_result[0],
                        label="recovery temporary file",
                        allowed_nlinks={2},
                    )
                self._quarantine_and_delete_unlocked(
                    parent_fd,
                    backup_name,
                    expected_receipt=backup_result[0],
                    label="recovery preserved target",
                )
                continue
            if temp_name is None or temp_result is None:
                raise DeferredStartupFileDriverError(
                    "startup recovery artifact is incomplete"
                )
            temp_is_published = target_result is not None and (
                target_result[0][0][0:2] == temp_result[0][0][0:2]
            )
            if temp_is_published:
                if not self._artifact_proves_published_target(
                    parent_fd,
                    target_name,
                    target_result[1],
                ):
                    raise DeferredStartupFileDriverError(
                        "startup published target cannot be proven"
                    )
            self._quarantine_and_delete_unlocked(
                parent_fd,
                temp_name,
                expected_receipt=temp_result[0],
                label="orphan temporary file",
                allowed_nlinks={2} if temp_is_published else {1},
            )

    def _read_artifact_if_present(
        self,
        parent_fd: int,
        name: str,
        label: str,
        *,
        allowed_nlinks: set[int],
    ) -> tuple[FileReceipt, bytes] | None:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DeferredStartupFileDriverError(
                f"startup {label} is unreadable"
            ) from exc
        return self._read_artifact_unlocked(
            parent_fd,
            name,
            label,
            allowed_nlinks=allowed_nlinks,
        )

    def _artifact_proves_prior_target(
        self,
        parent_fd: int,
        target_name: str,
        payload: bytes,
    ) -> bool:
        try:
            if target_name == self._journal_name:
                journal = self._validate_journal(
                    json.loads(payload, object_pairs_hook=self._reject_duplicate_pairs)
                )
                anchor_result = self._read_artifact_if_present(
                    parent_fd,
                    self._anchor_name,
                    "anchor proof",
                    allowed_nlinks={1, 2},
                )
                if anchor_result is None:
                    return False
                anchor = self._validate_anchor(
                    json.loads(
                        anchor_result[1],
                        object_pairs_hook=self._reject_duplicate_pairs,
                    )
                )
                return anchor["generation"] == journal["generation"] and anchor[
                    "journal_sha256"
                ] == self._journal_sha256(journal)
            anchor = self._validate_anchor(
                json.loads(payload, object_pairs_hook=self._reject_duplicate_pairs)
            )
            journal_result = self._read_artifact_if_present(
                parent_fd,
                self._journal_name,
                "journal proof",
                allowed_nlinks={1, 2},
            )
            journal = (
                self._empty_journal()
                if journal_result is None
                else self._validate_journal(
                    json.loads(
                        journal_result[1],
                        object_pairs_hook=self._reject_duplicate_pairs,
                    )
                )
            )
            journal_sha256 = self._journal_sha256(journal)
            return (
                anchor["generation"] == journal["generation"]
                and anchor["journal_sha256"] == journal_sha256
            ) or (
                journal["generation"] == anchor["generation"] + 1
                and journal["previous_sha256"] == anchor["journal_sha256"]
            )
        except (
            DeferredStartupFileDriverError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return False

    def _artifact_proves_published_target(
        self,
        parent_fd: int,
        target_name: str,
        payload: bytes,
    ) -> bool:
        try:
            if target_name == self._journal_name:
                journal = self._validate_journal(
                    json.loads(payload, object_pairs_hook=self._reject_duplicate_pairs)
                )
                anchor_result = self._read_artifact_if_present(
                    parent_fd,
                    self._anchor_name,
                    "anchor proof",
                    allowed_nlinks={1, 2},
                )
                if anchor_result is None:
                    return False
                anchor = self._validate_anchor(
                    json.loads(
                        anchor_result[1],
                        object_pairs_hook=self._reject_duplicate_pairs,
                    )
                )
                journal_sha256 = self._journal_sha256(journal)
                return (
                    anchor["generation"] == journal["generation"]
                    and anchor["journal_sha256"] == journal_sha256
                ) or (
                    journal["generation"] == anchor["generation"] + 1
                    and journal["previous_sha256"] == anchor["journal_sha256"]
                )
            anchor = self._validate_anchor(
                json.loads(payload, object_pairs_hook=self._reject_duplicate_pairs)
            )
            journal_result = self._read_artifact_if_present(
                parent_fd,
                self._journal_name,
                "journal proof",
                allowed_nlinks={1, 2},
            )
            journal = (
                self._empty_journal()
                if journal_result is None
                else self._validate_journal(
                    json.loads(
                        journal_result[1],
                        object_pairs_hook=self._reject_duplicate_pairs,
                    )
                )
            )
            return anchor["generation"] == journal["generation"] and anchor[
                "journal_sha256"
            ] == self._journal_sha256(journal)
        except (
            DeferredStartupFileDriverError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return False

    def _empty_journal(self) -> dict:
        return {
            "version": JOURNAL_VERSION,
            "generation": 0,
            "previous_sha256": "0" * 64,
            "transaction_id": self._transaction_id,
            "manifest_receipt": {
                "version": self._manifest_receipt.version,
                "sha256": self._manifest_receipt.sha256,
            },
            "steps": {},
        }

    def _validate_journal(self, raw: object) -> dict:
        expected_receipt = {
            "version": self._manifest_receipt.version,
            "sha256": self._manifest_receipt.sha256,
        }
        if (
            type(raw) is not dict
            or set(raw)
            != {
                "version",
                "generation",
                "previous_sha256",
                "transaction_id",
                "manifest_receipt",
                "steps",
            }
            or type(raw.get("version")) is not int
            or raw["version"] != JOURNAL_VERSION
            or type(raw.get("generation")) is not int
            or raw["generation"] < 0
            or type(raw.get("previous_sha256")) is not str
            or _SHA256_RE.fullmatch(raw["previous_sha256"]) is None
            or type(raw.get("transaction_id")) is not str
            or raw["transaction_id"] != self._transaction_id
            or type(raw.get("manifest_receipt")) is not dict
            or raw["manifest_receipt"] != expected_receipt
            or type(raw["manifest_receipt"].get("version")) is not int
            or type(raw["manifest_receipt"].get("sha256")) is not str
            or type(raw.get("steps")) is not dict
        ):
            raise DeferredStartupFileDriverError(
                "startup journal schema or binding is invalid"
            )
        validated_steps: dict[str, dict] = {}
        observed_generations: set[int] = set()
        for step_name, record in raw["steps"].items():
            if (
                type(step_name) is not str
                or _STEP_NAME_RE.fullmatch(step_name) is None
                or step_name.strip().lower() in _SENSITIVE_JOURNAL_KEYS
                or type(record) is not dict
                or set(record) != {"attempts"}
                or type(record.get("attempts")) is not list
            ):
                raise DeferredStartupFileDriverError(
                    "startup journal step record is invalid"
                )
            attempts: list[dict] = []
            process_epochs: set[str] = set()
            prior_indeterminate = False
            for expected_attempt, attempt in enumerate(
                record["attempts"],
                start=1,
            ):
                if prior_indeterminate:
                    raise DeferredStartupFileDriverError(
                        "startup journal has an attempt after indeterminate state"
                    )
                if (
                    type(attempt) is not dict
                    or set(attempt)
                    not in (
                        {
                            "attempt",
                            "process_epoch",
                            "prior_completion_absent_policy",
                            "intent",
                        },
                        {
                            "attempt",
                            "process_epoch",
                            "prior_completion_absent_policy",
                            "intent",
                            "completion",
                        },
                        {
                            "attempt",
                            "process_epoch",
                            "prior_completion_absent_policy",
                            "intent",
                            "indeterminate",
                        },
                    )
                    or type(attempt.get("attempt")) is not int
                    or isinstance(attempt.get("attempt"), bool)
                    or attempt["attempt"] != expected_attempt
                    or type(attempt.get("process_epoch")) is not str
                    or _PROCESS_EPOCH_RE.fullmatch(attempt["process_epoch"]) is None
                    or attempt["process_epoch"] in process_epochs
                    or type(attempt.get("prior_completion_absent_policy")) is not str
                    or attempt["prior_completion_absent_policy"]
                    not in {policy.value for policy in PriorCompletionAbsentPolicy}
                    or type(attempt.get("intent")) is not dict
                    or set(attempt["intent"]) != {"generation"}
                    or type(attempt["intent"].get("generation")) is not int
                    or isinstance(attempt["intent"].get("generation"), bool)
                    or attempt["intent"]["generation"] < 1
                ):
                    raise DeferredStartupFileDriverError(
                        "startup journal attempt topology is invalid"
                    )
                process_epochs.add(attempt["process_epoch"])
                intent_generation = attempt["intent"]["generation"]
                if intent_generation in observed_generations:
                    raise DeferredStartupFileDriverError(
                        "startup journal attempt generation is duplicated"
                    )
                observed_generations.add(intent_generation)
                if "completion" in attempt:
                    completion = attempt["completion"]
                    if (
                        type(completion) is not dict
                        or set(completion) != {"recovered", "generation"}
                        or type(completion["recovered"]) is not bool
                        or type(completion["generation"]) is not int
                        or isinstance(completion["generation"], bool)
                        or completion["generation"] <= intent_generation
                        or completion["generation"] in observed_generations
                    ):
                        raise DeferredStartupFileDriverError(
                            "startup journal completion receipt is invalid"
                        )
                    observed_generations.add(completion["generation"])
                if "indeterminate" in attempt:
                    indeterminate = attempt["indeterminate"]
                    if (
                        type(indeterminate) is not dict
                        or set(indeterminate) != {"reason", "generation"}
                        or type(indeterminate["reason"]) is not str
                        or _REASON_RE.fullmatch(indeterminate["reason"]) is None
                        or self._contains_sensitive_value(indeterminate["reason"])
                        or type(indeterminate["generation"]) is not int
                        or isinstance(indeterminate["generation"], bool)
                        or indeterminate["generation"] <= intent_generation
                        or indeterminate["generation"] in observed_generations
                    ):
                        raise DeferredStartupFileDriverError(
                            "startup journal indeterminate receipt is invalid"
                        )
                    observed_generations.add(indeterminate["generation"])
                    prior_indeterminate = True
                attempts.append(attempt)
            if not attempts:
                raise DeferredStartupFileDriverError(
                    "startup journal step attempts are empty"
                )
            for index, attempt in enumerate(attempts):
                if index > 0 and (
                    attempt["intent"]["generation"]
                    <= attempts[index - 1]["intent"]["generation"]
                ):
                    raise DeferredStartupFileDriverError(
                        "startup journal attempt generations are reordered"
                    )
                if index + 1 < len(attempts):
                    terminal = attempt.get("completion") or attempt.get("indeterminate")
                    if (
                        terminal is not None
                        and terminal["generation"]
                        >= attempts[index + 1]["intent"]["generation"]
                    ):
                        raise DeferredStartupFileDriverError(
                            "startup journal terminal generation follows a newer attempt"
                        )
            validated_steps[step_name] = {"attempts": attempts}
        expected_generation = len(observed_generations)
        if raw["generation"] != expected_generation:
            raise DeferredStartupFileDriverError(
                "startup journal generation does not match step topology"
            )
        if observed_generations != set(range(1, expected_generation + 1)):
            raise DeferredStartupFileDriverError(
                "startup journal attempt generations are reordered or missing"
            )
        if raw["generation"] == 0 and raw["previous_sha256"] != "0" * 64:
            raise DeferredStartupFileDriverError(
                "startup empty journal chain is invalid"
            )
        return {
            "version": JOURNAL_VERSION,
            "generation": raw["generation"],
            "previous_sha256": raw["previous_sha256"],
            "transaction_id": self._transaction_id,
            "manifest_receipt": expected_receipt,
            "steps": validated_steps,
        }

    def _read_anchor_unlocked(
        self,
        parent_fd: int,
    ) -> tuple[dict, FileReceipt] | None:
        result = self._read_json_unlocked(
            parent_fd,
            self._anchor_name,
            "startup anchor",
            missing_ok=True,
        )
        if result is None:
            return None
        raw, receipt = result
        return self._validate_anchor(raw), receipt

    def _validate_anchor(self, raw: object) -> dict:
        expected_receipt = {
            "version": self._manifest_receipt.version,
            "sha256": self._manifest_receipt.sha256,
        }
        if (
            type(raw) is not dict
            or set(raw)
            != {
                "version",
                "transaction_id",
                "manifest_receipt",
                "generation",
                "journal_sha256",
            }
            or type(raw.get("version")) is not int
            or raw["version"] != ANCHOR_VERSION
            or type(raw.get("transaction_id")) is not str
            or raw["transaction_id"] != self._transaction_id
            or type(raw.get("manifest_receipt")) is not dict
            or raw["manifest_receipt"] != expected_receipt
            or type(raw.get("generation")) is not int
            or raw["generation"] < 0
            or type(raw.get("journal_sha256")) is not str
            or _SHA256_RE.fullmatch(raw["journal_sha256"]) is None
        ):
            raise DeferredStartupFileDriverError(
                "startup anchor schema or binding is invalid"
            )
        return {
            "version": ANCHOR_VERSION,
            "transaction_id": self._transaction_id,
            "manifest_receipt": expected_receipt,
            "generation": raw["generation"],
            "journal_sha256": raw["journal_sha256"],
        }

    def _anchor_for_journal(self, journal: dict) -> dict:
        return {
            "version": ANCHOR_VERSION,
            "transaction_id": self._transaction_id,
            "manifest_receipt": {
                "version": self._manifest_receipt.version,
                "sha256": self._manifest_receipt.sha256,
            },
            "generation": journal["generation"],
            "journal_sha256": self._journal_sha256(journal),
        }

    def _write_anchor_unlocked(
        self,
        parent_fd: int,
        journal: dict,
    ) -> None:
        current = self._read_anchor_unlocked(parent_fd)
        expected_receipt = None if current is None else current[1]
        anchor = self._validate_anchor(self._anchor_for_journal(journal))
        payload = self._canonical_journal_bytes(anchor) + b"\n"
        if len(payload) > self._max_bytes:
            raise DeferredStartupFileDriverError("startup anchor is too large")
        self._atomic_write_named_unlocked(
            parent_fd,
            self._anchor_name,
            payload,
            expected_receipt,
            inject_journal_crashes=False,
        )

    def _reconcile_anchor_unlocked(
        self,
        parent_fd: int,
        journal: dict,
        journal_receipt: FileReceipt | None,
    ) -> None:
        current_sha256 = self._journal_sha256(journal)
        anchor_result = self._read_anchor_unlocked(parent_fd)
        if journal_receipt is None:
            if anchor_result is None:
                self._write_anchor_unlocked(parent_fd, journal)
                return
            anchor, _anchor_receipt = anchor_result
            if anchor["generation"] == 0 and anchor["journal_sha256"] == current_sha256:
                return
            raise DeferredStartupFileDriverError(
                "startup journal rollback detected by anchor"
            )
        if anchor_result is None:
            raise DeferredStartupFileDriverError(
                "startup anchor is missing for journal"
            )
        anchor, _anchor_receipt = anchor_result
        if (
            anchor["generation"] == journal["generation"]
            and anchor["journal_sha256"] == current_sha256
        ):
            return
        if (
            journal["generation"] == anchor["generation"] + 1
            and journal["previous_sha256"] == anchor["journal_sha256"]
        ):
            self._write_anchor_unlocked(parent_fd, journal)
            return
        raise DeferredStartupFileDriverError(
            "startup anchor does not match journal; rollback or fork detected"
        )

    def _accept_observed_journal(self, journal: dict) -> None:
        generation = journal["generation"]
        canonical_sha256 = self._journal_sha256(journal)
        frozen_steps = tuple(
            (name, self._freeze(record))
            for name, record in sorted(journal["steps"].items())
        )
        if self._last_generation is None:
            self._bind_observed_state(
                generation,
                canonical_sha256,
                frozen_steps,
            )
            return
        if generation < self._last_generation:
            raise DeferredStartupFileDriverError(
                "startup journal generation moved backwards"
            )
        if generation == self._last_generation:
            if canonical_sha256 != self._last_canonical_sha256:
                raise DeferredStartupFileDriverError(
                    "startup journal generation has conflicting content"
                )
            return
        if not self._is_monotonic_step_extension(
            self._last_steps,
            frozen_steps,
        ):
            raise DeferredStartupFileDriverError(
                "startup journal generation is not a monotonic extension"
            )
        self._bind_observed_state(
            generation,
            canonical_sha256,
            frozen_steps,
        )

    def _bind_observed_state(
        self,
        generation: int,
        canonical_sha256: str,
        frozen_steps: tuple[tuple[str, object], ...],
    ) -> None:
        self._last_generation = generation
        self._last_canonical_sha256 = canonical_sha256
        self._last_steps = frozen_steps

    @staticmethod
    def _is_monotonic_step_extension(
        previous: tuple[tuple[str, object], ...],
        current: tuple[tuple[str, object], ...],
    ) -> bool:
        current_by_name = dict(current)
        for step_name, previous_record in previous:
            current_record = current_by_name.get(step_name)
            if current_record is None:
                return False
            if current_record == previous_record:
                continue
            if not isinstance(previous_record, tuple) or not isinstance(
                current_record,
                tuple,
            ):
                return False
            previous_attempts = dict(previous_record).get("attempts")
            current_attempts = dict(current_record).get("attempts")
            if not isinstance(previous_attempts, tuple) or not isinstance(
                current_attempts,
                tuple,
            ):
                return False
            if len(current_attempts) < len(previous_attempts):
                return False
            for index, previous_attempt in enumerate(previous_attempts):
                current_attempt = current_attempts[index]
                if current_attempt == previous_attempt:
                    continue
                if index != len(previous_attempts) - 1:
                    return False
                previous_fields = dict(previous_attempt)
                current_fields = dict(current_attempt)
                if set(previous_fields) != {
                    "attempt",
                    "process_epoch",
                    "prior_completion_absent_policy",
                    "intent",
                }:
                    return False
                if any(
                    current_fields.get(key) != value
                    for key, value in previous_fields.items()
                ):
                    return False
                if set(current_fields) not in (
                    {
                        "attempt",
                        "process_epoch",
                        "prior_completion_absent_policy",
                        "intent",
                        "completion",
                    },
                    {
                        "attempt",
                        "process_epoch",
                        "prior_completion_absent_policy",
                        "intent",
                        "indeterminate",
                    },
                ):
                    return False
        return True

    @staticmethod
    def _attempt_count(journal: dict) -> int:
        return sum(len(record["attempts"]) for record in journal["steps"].values())

    @staticmethod
    def _latest_process_epoch(journal: dict) -> str | None:
        latest: tuple[int, str] | None = None
        for record in journal["steps"].values():
            for attempt in record["attempts"]:
                candidate = (
                    attempt["intent"]["generation"],
                    attempt["process_epoch"],
                )
                if latest is None or candidate[0] > latest[0]:
                    latest = candidate
        return None if latest is None else latest[1]

    @staticmethod
    def _canonical_journal_bytes(journal: dict) -> bytes:
        return json.dumps(
            journal,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _journal_sha256(cls, journal: dict) -> str:
        return hashlib.sha256(cls._canonical_journal_bytes(journal)).hexdigest()

    @classmethod
    def _freeze(cls, value: object) -> object:
        if type(value) is dict:
            return tuple(
                (key, cls._freeze(item)) for key, item in sorted(value.items())
            )
        if type(value) is list:
            return tuple(cls._freeze(item) for item in value)
        return value

    @staticmethod
    def _contains_sensitive_value(value: str) -> bool:
        lowered = value.strip().lower()
        return any(marker in lowered for marker in _SENSITIVE_VALUE_MARKERS)

    @staticmethod
    def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise DeferredStartupFileDriverError(
                    "startup journal JSON has duplicate keys"
                )
            result[key] = value
        return result
