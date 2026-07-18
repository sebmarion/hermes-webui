"""Crash-safe, proof-bound derived view state for one conversation lineage.

The projection is a rebuildable WebUI artifact.  It never mutates session
metadata and is eligible only when both its target content-proof digest and
durable message watermark exactly match the caller's current proof.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Mapping

from api.todo_state import _normalize_snapshot

try:  # ``fcntl`` is the POSIX primitive that makes CAS safe across workers.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through the fail-closed path
    _fcntl = None


VIEW_STATE_VERSION = 1
MAX_VIEW_STATE_BYTES = 512 * 1024
PROJECTION_LOCK_TIMEOUT_SECONDS = 1.25
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCKS = tuple(threading.RLock() for _ in range(64))


class ViewStateStoreError(RuntimeError):
    """Persisted projection state is corrupt, unsupported, or mismatched."""


@contextmanager
def _exclusive_projection_lock(path: Path):
    """Hold an advisory cross-process lock for one projection key.

    Atomic replace prevents torn readers but cannot make the read/compare/write
    sequence a CAS across workers.  Refuse publication on platforms without
    ``fcntl`` rather than silently downgrading that invariant.
    """
    if _fcntl is None:
        raise ViewStateStoreError("projection advisory locking unavailable")
    lock_path = path.parent / f".{path.name}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ViewStateStoreError("projection advisory locking unavailable") from exc
    try:
        deadline = time.monotonic() + PROJECTION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ViewStateStoreError(
                        "projection advisory locking unavailable"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise ViewStateStoreError(
                        "projection advisory locking unavailable"
                    ) from exc
                time.sleep(0.005)
        yield
    finally:
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


@dataclass(frozen=True, order=True)
class MessageWatermark:
    timestamp: float
    message_id: int


@dataclass(frozen=True)
class ConversationViewState:
    version: int
    profile: str
    root_id: str
    watermark: MessageWatermark
    target_content_proof_digest: str
    snapshot: dict
    snapshot_digest: str
    empty_tombstone: bool
    generation: int


@dataclass(frozen=True)
class ViewStateCasResult:
    saved: bool
    reason: str
    state: ConversationViewState


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return the canonical digest referenced by a reconciliation receipt."""
    try:
        encoded = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("todo snapshot must be finite JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_identity(profile: str, root_id: str) -> tuple[str, str]:
    if not isinstance(profile, str) or not profile or len(profile) > 512:
        raise ValueError("profile must be a bounded non-empty string")
    if not isinstance(root_id, str) or not root_id or len(root_id) > 512:
        raise ValueError("root_id must be a bounded non-empty string")
    return profile, root_id


def _validate_watermark(value: MessageWatermark) -> MessageWatermark:
    if not isinstance(value, MessageWatermark):
        raise ValueError("watermark must be MessageWatermark")
    timestamp = value.timestamp
    message_id = value.message_id
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
    ):
        raise ValueError("watermark timestamp must be finite and non-negative")
    if type(message_id) is not int or message_id < 0:
        raise ValueError("watermark message_id must be a non-negative integer")
    return MessageWatermark(float(timestamp), message_id)


def _validate_proof_digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError("target content proof digest must be canonical sha256")
    return value


def _normalize_projection_snapshot(value: Any) -> dict:
    normalized = _normalize_snapshot(value)
    if normalized is None:
        raise ValueError("todo snapshot is malformed")
    result = deepcopy(normalized)
    if isinstance(value, dict) and "ts" in value:
        timestamp = value["ts"]
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
        ):
            raise ValueError("todo snapshot timestamp must be finite")
        result["ts"] = float(timestamp)
    # Force a finite JSON round trip now rather than failing halfway through a
    # publication after the previous projection has been inspected.
    snapshot_digest(result)
    return result


class ConversationViewStateStore:
    """Atomic hashed projection store with proof-aware compare-and-swap."""

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir) / "conversation_view_state"

    @staticmethod
    def _key(profile: str, root_id: str) -> str:
        return hashlib.sha256(f"{profile}\0{root_id}".encode("utf-8")).hexdigest()

    def _lock(self, profile: str, root_id: str) -> threading.RLock:
        index = int(self._key(profile, root_id)[:8], 16) % len(_LOCKS)
        return _LOCKS[index]

    def path_for(self, profile: str, root_id: str) -> Path:
        profile, root_id = _validate_identity(profile, root_id)
        return self.root / f"{self._key(profile, root_id)}.json"

    def read(
        self,
        *,
        profile: str,
        root_id: str,
        target_content_proof_digest: str | None = None,
        watermark: MessageWatermark | None = None,
    ) -> ConversationViewState | None:
        profile, root_id = _validate_identity(profile, root_id)
        if target_content_proof_digest is not None:
            target_content_proof_digest = _validate_proof_digest(
                target_content_proof_digest
            )
        if watermark is not None:
            watermark = _validate_watermark(watermark)
        with self._lock(profile, root_id):
            state = self._read_locked(profile, root_id)
        if state is None:
            return None
        if (
            target_content_proof_digest is not None
            and state.target_content_proof_digest != target_content_proof_digest
        ):
            return None
        if watermark is not None and state.watermark != watermark:
            return None
        return state

    def compare_and_swap(
        self,
        *,
        profile: str,
        root_id: str,
        watermark: MessageWatermark,
        target_content_proof_digest: str,
        snapshot: Mapping[str, Any],
    ) -> ViewStateCasResult:
        profile, root_id = _validate_identity(profile, root_id)
        watermark = _validate_watermark(watermark)
        proof_digest = _validate_proof_digest(target_content_proof_digest)
        normalized_snapshot = _normalize_projection_snapshot(snapshot)
        normalized_digest = snapshot_digest(normalized_snapshot)
        with self._lock(profile, root_id):
            path = self.path_for(profile, root_id)
            with _exclusive_projection_lock(path):
                current = self._read_locked(profile, root_id)
                if current is not None and current.target_content_proof_digest == proof_digest:
                    if watermark < current.watermark:
                        return ViewStateCasResult(False, "older_replay", current)
                    if watermark == current.watermark:
                        if normalized_digest == current.snapshot_digest:
                            return ViewStateCasResult(True, "unchanged", current)
                        return ViewStateCasResult(
                            False,
                            "conflicting_same_watermark",
                            current,
                        )
                generation = (current.generation if current is not None else 0) + 1
                accepted = ConversationViewState(
                    version=VIEW_STATE_VERSION,
                    profile=profile,
                    root_id=root_id,
                    watermark=watermark,
                    target_content_proof_digest=proof_digest,
                    snapshot=normalized_snapshot,
                    snapshot_digest=normalized_digest,
                    empty_tombstone=normalized_snapshot["todos"] == [],
                    generation=generation,
                )
                self._atomic_write(path, self._to_raw(accepted))
                return ViewStateCasResult(True, "saved", accepted)

    def delete(self, *, profile: str, root_id: str) -> None:
        profile, root_id = _validate_identity(profile, root_id)
        path = self.path_for(profile, root_id)
        with self._lock(profile, root_id):
            with _exclusive_projection_lock(path):
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    _fsync_directory(path.parent)

    def _read_locked(
        self,
        profile: str,
        root_id: str,
    ) -> ConversationViewState | None:
        path = self.path_for(profile, root_id)
        try:
            with path.open("rb") as handle:
                encoded = handle.read(MAX_VIEW_STATE_BYTES + 1)
            if len(encoded) > MAX_VIEW_STATE_BYTES:
                raise ViewStateStoreError("conversation view state exceeds byte limit")
            raw = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ViewStateStoreError("conversation view state is unreadable") from exc
        try:
            return self._from_raw(raw, profile=profile, root_id=root_id)
        except (TypeError, ValueError, KeyError) as exc:
            raise ViewStateStoreError("conversation view state is malformed") from exc

    @staticmethod
    def _to_raw(state: ConversationViewState) -> dict:
        return {
            "version": state.version,
            "profile": state.profile,
            "root_id": state.root_id,
            "watermark": {
                "timestamp": state.watermark.timestamp,
                "message_id": state.watermark.message_id,
            },
            "target_content_proof_digest": state.target_content_proof_digest,
            "snapshot": state.snapshot,
            "snapshot_digest": state.snapshot_digest,
            "empty_tombstone": state.empty_tombstone,
            "generation": state.generation,
        }

    @staticmethod
    def _from_raw(
        raw: Any,
        *,
        profile: str,
        root_id: str,
    ) -> ConversationViewState:
        required = {
            "version",
            "profile",
            "root_id",
            "watermark",
            "target_content_proof_digest",
            "snapshot",
            "snapshot_digest",
            "empty_tombstone",
            "generation",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("invalid projection object")
        if raw["version"] != VIEW_STATE_VERSION:
            raise ValueError("unsupported projection version")
        if raw["profile"] != profile or raw["root_id"] != root_id:
            raise ValueError("projection identity mismatch")
        watermark_raw = raw["watermark"]
        if not isinstance(watermark_raw, dict) or set(watermark_raw) != {
            "timestamp",
            "message_id",
        }:
            raise ValueError("invalid projection watermark")
        watermark = _validate_watermark(
            MessageWatermark(
                timestamp=watermark_raw["timestamp"],
                message_id=watermark_raw["message_id"],
            )
        )
        proof_digest = _validate_proof_digest(raw["target_content_proof_digest"])
        normalized_snapshot = _normalize_projection_snapshot(raw["snapshot"])
        if normalized_snapshot != raw["snapshot"]:
            raise ValueError("projection snapshot is not normalized")
        digest = snapshot_digest(normalized_snapshot)
        if raw["snapshot_digest"] != digest:
            raise ValueError("projection snapshot digest mismatch")
        empty_tombstone = normalized_snapshot["todos"] == []
        if type(raw["empty_tombstone"]) is not bool or raw["empty_tombstone"] != empty_tombstone:
            raise ValueError("projection empty tombstone mismatch")
        generation = raw["generation"]
        if type(generation) is not int or generation < 1:
            raise ValueError("projection generation is invalid")
        return ConversationViewState(
            version=VIEW_STATE_VERSION,
            profile=profile,
            root_id=root_id,
            watermark=watermark,
            target_content_proof_digest=proof_digest,
            snapshot=normalized_snapshot,
            snapshot_digest=digest,
            empty_tombstone=empty_tombstone,
            generation=generation,
        )

    @staticmethod
    def _atomic_write(path: Path, raw: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    raw,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            _fsync_directory(path.parent)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
