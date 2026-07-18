"""Content-free durable evidence for bounded-view shadow comparisons.

This module deliberately knows nothing about sessions, receipts, or response
payloads.  Callers supply only the stable cohort identifiers and boolean proof
outcome from an already-completed candidate/oracle comparison.  The persisted
aggregate contains counters, timestamps, typed difference codes, and hashed
cohort keys--never transcript content, local paths, or credentials.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import Callable, Iterator

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by monkeypatch on Unix CI
    _fcntl = None


EVIDENCE_VERSION = 3
MIN_SAMPLE_COUNT = 1_000
MIN_OBSERVED_SPAN_SECONDS = 7 * 24 * 60 * 60
_FILENAME = f"conversation-shadow-evidence-v{EVIDENCE_VERSION}.json"
_LOCK_SUFFIX = ".lock"
_KEY_SUFFIX = ".key"
MAX_COHORTS = 8
MAX_DISABLED_TOMBSTONES = 8
MAX_EVIDENCE_BYTES = 128 * 1024
_COHORT_KEY_BYTES = 32
_GENERATION_BLOOM_BITS = 32_768
_GENERATION_BLOOM_HEX_LENGTH = _GENERATION_BLOOM_BITS // 4
_GENERATION_BLOOM_HASHES = 4
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}
_ALLOWED_DIFFERENCE_REASONS = frozenset(
    {
        "truncation_difference",
        "tool_pair_difference",
        "visible_count_difference",
        "visible_identity_difference",
        "visible_order_difference",
    }
)
_COHORT_FIELDS = frozenset(
    {
        "clock_regressed",
        "difference_count",
        "difference_reasons",
        "disabled_at",
        "first_sample_at",
        "implementation_id",
        "last_sample_at",
        "profile_binding",
        "sample_count",
        "schema_id",
        "generation_bloom",
    }
)
_STATE_FIELDS = frozenset(
    {"version", "cohorts", "disabled_tombstones", "tombstone_capacity_exhausted"}
)


@dataclass(frozen=True)
class ShadowProofInput:
    """Content-free result of one candidate-versus-legacy comparison."""

    implementation_id: str
    schema_id: str
    profile: str
    request_generation: int
    candidate_complete: bool
    oracle_complete: bool
    lineage_unchanged: bool
    gates_passed: bool
    difference_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShadowReadiness:
    """Fail-closed server/bootstrap enablement decision for one cohort."""

    ready: bool
    reason: str
    sample_count: int = 0
    difference_count: int = 0
    first_sample_at: float | None = None
    last_sample_at: float | None = None
    observed_span_seconds: float = 0.0
    disabled_at: float | None = None


class ConversationShadowEvidenceStore:
    """Atomically persists the small, per-cohort shadow-evidence aggregate."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        sample_rate: int = 1,
    ) -> None:
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate < 1:
            raise ValueError("sample_rate must be positive")
        self._path = Path(state_dir) / _FILENAME
        self._key_path = self._path.with_suffix(_KEY_SUFFIX)
        self._clock = clock
        self._sample_rate = sample_rate
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(self._path.resolve(), threading.RLock())

    def readiness(self, proof: ShadowProofInput) -> ShadowReadiness:
        """Read the current aggregate without treating a manual flag as proof."""
        prerequisite_reason = self._prerequisite_reason(proof)
        if prerequisite_reason:
            return ShadowReadiness(False, prerequisite_reason)
        with self._lock:
            with self._advisory_lock() as acquired:
                if not acquired:
                    return ShadowReadiness(False, "advisory_lock_unavailable")
                state, error = self._read_state()
                if error:
                    return ShadowReadiness(False, error)
                assert state is not None
                if not state["cohorts"]:
                    return ShadowReadiness(False, "evidence_missing")
                secret, error = self._load_cohort_secret(create=False)
                if error:
                    return ShadowReadiness(False, error)
                assert secret is not None
                global_disable = self._tombstone_capacity_readiness(state)
                if global_disable is not None:
                    return global_disable
                tombstone = self._tombstone_readiness(state, proof, secret)
                if tombstone is not None:
                    return tombstone
                if self._path.exists():
                    try:
                        _fsync_directory(self._path.parent)
                    except OSError:
                        return ShadowReadiness(False, "persistence_failed")
                now, error = self._current_time()
                if error:
                    return ShadowReadiness(False, error)
                assert now is not None
                return self._readiness_from_state(state, proof, secret, now=now)

    def record(self, proof: ShadowProofInput) -> ShadowReadiness:
        """Durably record one eligible sampled comparison, or fail closed.

        A caller must invoke this only after both full results used the same
        resolved lineage/request generation.  The boolean fields make that
        requirement mechanically checkable here, while keeping this store
        independent of the surrounding conversation implementation.
        """
        prerequisite_reason = self._prerequisite_reason(proof)
        with self._lock:
            with self._advisory_lock() as acquired:
                if not acquired:
                    return ShadowReadiness(False, "advisory_lock_unavailable")
                state, error = self._read_state()
                if error:
                    return ShadowReadiness(False, error)
                assert state is not None
                if prerequisite_reason:
                    return ShadowReadiness(False, prerequisite_reason)
                secret, error = self._load_cohort_secret(create=True)
                if error:
                    return ShadowReadiness(False, error)
                assert secret is not None
                global_disable = self._tombstone_capacity_readiness(state)
                if global_disable is not None:
                    return global_disable
                key = _cohort_key(proof, secret)
                tombstone = self._tombstone_readiness(state, proof, secret)
                if tombstone is not None and not proof.difference_reasons:
                    return tombstone
                if not self._is_sampled(proof):
                    return self._readiness_from_state(state, proof, secret, "not_sampled")

                cohorts = state["cohorts"]
                persisted = cohorts.get(key)
                if persisted is not None and not _cohort_matches_proof(persisted, proof, secret):
                    return ShadowReadiness(False, "evidence_corrupt")
                cohort = dict(persisted or _new_cohort(proof, secret))
                now, error = self._current_time()
                if error:
                    return ShadowReadiness(False, error)
                assert now is not None
                last = cohort.get("last_sample_at")
                if last is not None and now < last:
                    cohort["clock_regressed"] = True
                    cohorts[key] = cohort
                    if not self._write_state(state):
                        return ShadowReadiness(False, "persistence_failed")
                    return self._readiness_from_cohort(cohort, "clock_regressed")

                if cohort.get("clock_regressed"):
                    return self._readiness_from_cohort(cohort, "clock_regressed")

                reasons = tuple(sorted(set(proof.difference_reasons)))
                if reasons:
                    tombstones = state["disabled_tombstones"]
                    if key not in tombstones and len(tombstones) >= MAX_DISABLED_TOMBSTONES:
                        state["tombstone_capacity_exhausted"] = True
                        if not self._write_state(state):
                            return ShadowReadiness(False, "persistence_failed")
                        return ShadowReadiness(False, "disabled_tombstone_capacity_exhausted")
                    if key not in tombstones:
                        tombstones[key] = now
                    counts = dict(cohort["difference_reasons"])
                    for reason in reasons:
                        counts[reason] = int(counts.get(reason, 0)) + 1
                    cohort["difference_reasons"] = counts
                    cohort["difference_count"] = int(cohort["difference_count"]) + 1
                    if cohort.get("disabled_at") is None:
                        cohort["disabled_at"] = now
                elif _generation_seen(cohort["generation_bloom"], proof.request_generation, secret):
                    return self._readiness_from_cohort(cohort, "duplicate_generation")
                elif cohort.get("disabled_at") is None:
                    cohort["sample_count"] = int(cohort["sample_count"]) + 1
                    if cohort.get("first_sample_at") is None:
                        cohort["first_sample_at"] = now
                    cohort["last_sample_at"] = now
                else:
                    return self._readiness_from_cohort(cohort, "latched_disabled")

                cohort["generation_bloom"] = _mark_generation(
                    cohort["generation_bloom"], proof.request_generation, secret
                )
                if persisted is None:
                    _evict_old_cohorts(cohorts, MAX_COHORTS - 1)
                cohorts[key] = cohort
                if not self._write_state(state):
                    return ShadowReadiness(False, "persistence_failed")
                return self._readiness_from_cohort(
                    cohort, "semantic_difference" if reasons else None
                )

    def _readiness_from_state(
        self,
        state: dict,
        proof: ShadowProofInput,
        secret: bytes,
        override_reason: str | None = None,
        *,
        now: float | None = None,
    ) -> ShadowReadiness:
        cohort = state["cohorts"].get(_cohort_key(proof, secret))
        if not isinstance(cohort, dict):
            return ShadowReadiness(False, override_reason or "evidence_missing")
        if not _cohort_matches_proof(cohort, proof, secret):
            return ShadowReadiness(False, "evidence_corrupt")
        last = cohort.get("last_sample_at")
        if now is not None and last is not None and now < last:
            return self._readiness_from_cohort(cohort, "clock_regressed")
        return self._readiness_from_cohort(cohort, override_reason)

    @staticmethod
    def _tombstone_capacity_readiness(state: dict) -> ShadowReadiness | None:
        if state["tombstone_capacity_exhausted"]:
            return ShadowReadiness(False, "disabled_tombstone_capacity_exhausted")
        return None

    @staticmethod
    def _tombstone_readiness(
        state: dict, proof: ShadowProofInput, secret: bytes
    ) -> ShadowReadiness | None:
        disabled_at = state["disabled_tombstones"].get(_cohort_key(proof, secret))
        if disabled_at is None:
            return None
        return ShadowReadiness(False, "latched_disabled", disabled_at=disabled_at)

    def _current_time(self) -> tuple[float | None, str | None]:
        raw_now = self._clock()
        if not _is_finite_timestamp(raw_now):
            return None, "clock_invalid"
        return float(raw_now), None

    @staticmethod
    def _readiness_from_cohort(
        cohort: dict,
        override_reason: str | None = None,
    ) -> ShadowReadiness:
        count = int(cohort.get("sample_count", 0))
        differences = int(cohort.get("difference_count", 0))
        first = cohort.get("first_sample_at")
        last = cohort.get("last_sample_at")
        finite_span = (
            _is_finite_timestamp(first)
            and _is_finite_timestamp(last)
            and first <= last
        )
        span = float(last - first) if finite_span else 0.0
        finite_span = finite_span and math.isfinite(span)
        disabled_at = cohort.get("disabled_at")
        if override_reason:
            reason = override_reason
        elif cohort.get("clock_regressed"):
            reason = "clock_regressed"
        elif disabled_at is not None:
            reason = "latched_disabled"
        elif differences:
            reason = "semantic_difference"
        elif count < MIN_SAMPLE_COUNT:
            reason = "insufficient_samples"
        elif not finite_span:
            reason = "evidence_corrupt"
        elif span < MIN_OBSERVED_SPAN_SECONDS:
            reason = "insufficient_observed_span"
        else:
            reason = "ready"
        return ShadowReadiness(
            ready=reason == "ready",
            reason=reason,
            sample_count=count,
            difference_count=differences,
            first_sample_at=first,
            last_sample_at=last,
            observed_span_seconds=span,
            disabled_at=disabled_at,
        )

    @staticmethod
    def _prerequisite_reason(proof: ShadowProofInput) -> str | None:
        if any(
            not isinstance(value, bool)
            for value in (
                proof.candidate_complete,
                proof.oracle_complete,
                proof.lineage_unchanged,
                proof.gates_passed,
            )
        ):
            return "invalid_prerequisite"
        if not all((proof.candidate_complete, proof.oracle_complete, proof.lineage_unchanged)):
            return "incomplete_comparison"
        if not proof.gates_passed:
            return "current_gates_failed"
        if not _valid_identifier(proof.implementation_id) or not _valid_identifier(proof.schema_id):
            return "invalid_cohort"
        if not _valid_identifier(proof.profile):
            return "invalid_cohort"
        if (
            isinstance(proof.request_generation, bool)
            or not isinstance(proof.request_generation, int)
            or proof.request_generation < 0
        ):
            return "invalid_request_generation"
        if any(not _valid_reason(reason) for reason in proof.difference_reasons):
            return "invalid_difference_reason"
        return None

    @contextmanager
    def _advisory_lock(self) -> Iterator[bool]:
        """Serialize aggregate transactions across independent WebUI processes."""
        if _fcntl is None:
            yield False
            return
        descriptor: int | None = None
        acquired = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                f"{self._path}{_LOCK_SUFFIX}",
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            acquired = True
        except (AttributeError, OSError):
            acquired = False
        try:
            yield acquired
        finally:
            if descriptor is not None:
                if acquired:
                    try:
                        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
                    except (AttributeError, OSError):
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _is_sampled(self, proof: ShadowProofInput) -> bool:
        if self._sample_rate == 1:
            return True
        material = (
            f"{proof.implementation_id}\x1f{proof.schema_id}\x1f{proof.request_generation}"
        ).encode("utf-8")
        bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return bucket % self._sample_rate == 0

    def _load_cohort_secret(self, *, create: bool) -> tuple[bytes | None, str | None]:
        if self._key_path.exists():
            try:
                metadata = self._key_path.stat()
                raw = self._key_path.read_bytes()
            except OSError:
                return None, "evidence_corrupt"
            if metadata.st_mode & 0o077 or len(raw) != _COHORT_KEY_BYTES:
                return None, "evidence_corrupt"
            return raw, None
        if not create:
            return None, "evidence_corrupt"
        secret = secrets.token_bytes(_COHORT_KEY_BYTES)
        if not self._atomic_write_bytes(self._key_path, secret, mode=0o600):
            return None, "persistence_failed"
        return secret, None

    def _read_state(self) -> tuple[dict | None, str | None]:
        if not self._path.exists():
            return {
                "version": EVIDENCE_VERSION,
                "cohorts": {},
                "disabled_tombstones": {},
                "tombstone_capacity_exhausted": False,
            }, None
        try:
            raw = self._path.read_bytes()
            if len(raw) > MAX_EVIDENCE_BYTES:
                return None, "evidence_corrupt"
            data = _strict_json_object(raw)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None, "evidence_corrupt"
        if (
            not isinstance(data, dict)
            or set(data) != _STATE_FIELDS
            or isinstance(data.get("version"), bool)
            or not isinstance(data.get("version"), int)
        ):
            return None, "evidence_corrupt"
        if data["version"] != EVIDENCE_VERSION:
            return None, "unsupported_version"
        if not isinstance(data.get("cohorts"), dict) or not isinstance(
            data.get("disabled_tombstones"), dict
        ) or not isinstance(data.get("tombstone_capacity_exhausted"), bool):
            return None, "evidence_corrupt"
        if len(data["cohorts"]) > MAX_COHORTS or any(
            not _valid_cohort_key(key) or not _valid_cohort(cohort)
            for key, cohort in data["cohorts"].items()
        ):
            return None, "evidence_corrupt"
        tombstones = data["disabled_tombstones"]
        if len(tombstones) > MAX_DISABLED_TOMBSTONES or any(
            not _valid_cohort_key(key) or not _is_finite_timestamp(disabled_at)
            for key, disabled_at in tombstones.items()
        ) or any(
            cohort["difference_count"] and key not in tombstones
            for key, cohort in data["cohorts"].items()
        ):
            return None, "evidence_corrupt"
        return data, None

    def _write_state(self, state: dict) -> bool:
        try:
            payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(payload) > MAX_EVIDENCE_BYTES:
            return False
        return self._atomic_write_bytes(self._path, payload, mode=0o600)

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> bool:
        tmp_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            _fsync_directory(path.parent)
            return True
        except OSError:
            return False
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def _cohort_key(proof: ShadowProofInput, secret: bytes) -> str:
    """Key a private profile cohort without retaining an enumerable profile hash."""
    payload = "\x1f".join((proof.implementation_id, proof.schema_id, proof.profile))
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return "hmac-sha256:" + digest


def _profile_binding(proof: ShadowProofInput, secret: bytes) -> str:
    """Bind a cohort row to its private profile without retaining the profile."""
    return hmac.new(
        secret,
        ("profile-binding\\x1f" + proof.profile).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _new_cohort(proof: ShadowProofInput, secret: bytes) -> dict:
    return {
        "implementation_id": proof.implementation_id,
        "schema_id": proof.schema_id,
        "profile_binding": _profile_binding(proof, secret),
        "sample_count": 0,
        "difference_count": 0,
        "difference_reasons": {},
        "disabled_at": None,
        "first_sample_at": None,
        "last_sample_at": None,
        "clock_regressed": False,
        "generation_bloom": "0" * _GENERATION_BLOOM_HEX_LENGTH,
    }


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and all(
        char.isalnum() or char in "._-" for char in value
    )


def _valid_reason(value: object) -> bool:
    return isinstance(value, str) and value in _ALLOWED_DIFFERENCE_REASONS


def _valid_cohort_key(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("hmac-sha256:"):
        return False
    digest = value.removeprefix("hmac-sha256:")
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _is_finite_timestamp(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _valid_cohort(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) != _COHORT_FIELDS:
        return False
    if not _valid_identifier(value.get("implementation_id")):
        return False
    if not _valid_identifier(value.get("schema_id")):
        return False
    if not _valid_profile_binding(value.get("profile_binding")):
        return False
    for key in ("sample_count", "difference_count"):
        if (
            isinstance(value.get(key), bool)
            or not isinstance(value.get(key), int)
            or value[key] < 0
        ):
            return False
    if not isinstance(value.get("difference_reasons"), dict):
        return False
    if not _valid_generation_bloom(value.get("generation_bloom")):
        return False
    if any(
        not _valid_reason(reason)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        for reason, count in value["difference_reasons"].items()
    ):
        return False
    for key in ("disabled_at", "first_sample_at", "last_sample_at"):
        if value.get(key) is not None and not _is_finite_timestamp(value[key]):
            return False
    first = value["first_sample_at"]
    last = value["last_sample_at"]
    if value["sample_count"] == 0:
        if first is not None or last is not None:
            return False
    elif first is None or last is None or first > last:
        return False
    differences = value["difference_count"]
    reason_counts = value["difference_reasons"]
    disabled_at = value["disabled_at"]
    if differences == 0:
        if reason_counts or disabled_at is not None:
            return False
    elif not reason_counts or disabled_at is None:
        return False
    if any(count > differences for count in reason_counts.values()):
        return False
    return isinstance(value.get("clock_regressed"), bool)


def _cohort_matches_proof(
    cohort: dict, proof: ShadowProofInput, secret: bytes
) -> bool:
    return (
        cohort.get("implementation_id") == proof.implementation_id
        and cohort.get("schema_id") == proof.schema_id
        and hmac.compare_digest(
            cohort.get("profile_binding", ""), _profile_binding(proof, secret)
        )
    )


def _valid_profile_binding(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _valid_generation_bloom(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _GENERATION_BLOOM_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _generation_positions(request_generation: int, secret: bytes) -> tuple[int, ...]:
    digest = hmac.new(
        secret, str(request_generation).encode("ascii"), hashlib.sha256
    ).digest()
    return tuple(
        int.from_bytes(digest[index * 4 : (index + 1) * 4], "big")
        % _GENERATION_BLOOM_BITS
        for index in range(_GENERATION_BLOOM_HASHES)
    )


def _generation_seen(bloom: str, request_generation: int, secret: bytes) -> bool:
    bits = bytes.fromhex(bloom)
    return all(bits[position // 8] & (1 << (position % 8)) for position in _generation_positions(request_generation, secret))


def _mark_generation(bloom: str, request_generation: int, secret: bytes) -> str:
    bits = bytearray.fromhex(bloom)
    for position in _generation_positions(request_generation, secret):
        bits[position // 8] |= 1 << (position % 8)
    return bits.hex()


def _evict_old_cohorts(cohorts: dict[str, dict], limit: int) -> None:
    while len(cohorts) > limit:
        key = min(
            cohorts,
            key=lambda item: (_cohort_activity_timestamp(cohorts[item]), item),
        )
        del cohorts[key]


def _cohort_activity_timestamp(cohort: dict) -> float:
    for field in ("last_sample_at", "disabled_at", "first_sample_at"):
        value = cohort.get(field)
        if value is not None:
            return float(value)
    return float("-inf")


def _strict_json_object(raw: bytes) -> dict:
    def reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite JSON literal")

    decoded = raw.decode("utf-8")
    value = json.loads(
        decoded,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError("top-level evidence is not an object")
    return value


def _fsync_directory(directory: Path) -> None:
    """Fsync the published directory entry or propagate durability failure."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
