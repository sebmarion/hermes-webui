"""Content-free, fail-closed reconciliation receipts.

Receipts are WebUI-owned derived state. Counts, timestamps, filesystem stats,
and global projection generations remain hints; cursor eligibility requires the
declared Agent-owned target content proof represented by ``CONTENT_PROOF_KIND``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # Windows has no advisory flock primitive compatible with this contract.
    import fcntl
except ImportError:  # pragma: no cover - exercised through the fail-closed branch
    fcntl = None


RECEIPT_VERSION = 1
CONTENT_PROOF_KIND = "agent_target_content_epoch_v1"
MISSING_SIDECAR_MARKER = "missing"
MAX_MEMBERS = 256
MAX_ID_LENGTH = 512
MAX_PATH_LENGTH = 4096
MAX_PERSISTED_JSON_BYTES = 512 * 1024
MAX_PERSISTED_INTEGER = (1 << 63) - 1
STORE_LOCK_TIMEOUT_SECONDS = 0.25

# This is an in-memory authority token, not a schema value.  The assembler may
# attach it only after its Agent-schema capability detector has verified the
# declared contract and write semantics for CONTENT_PROOF_KIND.
VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY = object()

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STORE_LOCK_STRIPES = tuple(threading.RLock() for _ in range(64))
_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "profile",
        "root_id",
        "member_ids",
        "lineage_fingerprint",
        "canonical_sidecar_id",
        "lineage_sidecar_proof",
        "sidecar_generation",
        "sidecar_stat",
        "truncation_watermark",
        "state_message_watermark",
        "state_content_proof",
        "settled_display_message_count",
        "visible_transcript_digest",
        "todo_projection_generation",
        "todo_projection_watermark",
        "todo_projection_target_content_proof_digest",
        "todo_projection_snapshot_digest",
        "generation",
    }
)


class ReceiptStoreError(RuntimeError):
    """A receipt or its durable ownership state cannot be trusted."""


@dataclasses.dataclass(frozen=True)
class ConversationReceipt:
    """A settled lineage receipt with one explicit canonical sidecar binding.

    ``canonical_sidecar_id`` identifies the lineage member whose full descriptor
    derives the compatibility ``sidecar_generation`` and ``sidecar_stat`` fields.
    Every member, including ancestors and the target/tip, must also have one
    ordered ``lineage_sidecar_proof`` row; absence is represented only by the
    explicit ``MISSING_SIDECAR_MARKER`` value.
    """

    version: int
    profile: str
    root_id: str
    member_ids: tuple[str, ...]
    lineage_fingerprint: str
    canonical_sidecar_id: str
    lineage_sidecar_proof: tuple[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str], ...
    ]
    sidecar_generation: int
    sidecar_stat: tuple[str, int, int, int]
    truncation_watermark: float | int | None
    state_message_watermark: tuple[int, float | int]
    state_content_proof: tuple[str, tuple[tuple[str, int], ...]] | None
    settled_display_message_count: int
    visible_transcript_digest: str
    todo_projection_generation: int
    todo_projection_watermark: tuple[int, float | int]
    todo_projection_target_content_proof_digest: str
    todo_projection_snapshot_digest: str
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        _validate_receipt_instance(self)
        return {
            "version": self.version,
            "profile": self.profile,
            "root_id": self.root_id,
            "member_ids": list(self.member_ids),
            "lineage_fingerprint": self.lineage_fingerprint,
            "canonical_sidecar_id": self.canonical_sidecar_id,
            "lineage_sidecar_proof": _lineage_sidecar_proof_to_json(
                self.lineage_sidecar_proof
            ),
            "sidecar_generation": self.sidecar_generation,
            "sidecar_stat": list(self.sidecar_stat),
            "truncation_watermark": self.truncation_watermark,
            "state_message_watermark": list(self.state_message_watermark),
            "state_content_proof": _proof_to_json(self.state_content_proof),
            "settled_display_message_count": self.settled_display_message_count,
            "visible_transcript_digest": self.visible_transcript_digest,
            "todo_projection_generation": self.todo_projection_generation,
            "todo_projection_watermark": list(self.todo_projection_watermark),
            "todo_projection_target_content_proof_digest": (
                self.todo_projection_target_content_proof_digest
            ),
            "todo_projection_snapshot_digest": self.todo_projection_snapshot_digest,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConversationReceipt":
        try:
            if type(raw) is not dict or set(raw) != _RECEIPT_FIELDS:
                raise ReceiptStoreError("receipt must be one exact top-level mapping")
            receipt = cls(
                version=raw["version"],
                profile=raw["profile"],
                root_id=raw["root_id"],
                member_ids=_parse_member_ids(raw["member_ids"]),
                lineage_fingerprint=raw["lineage_fingerprint"],
                canonical_sidecar_id=raw["canonical_sidecar_id"],
                lineage_sidecar_proof=_parse_lineage_sidecar_proof(
                    raw["lineage_sidecar_proof"]
                ),
                sidecar_generation=raw["sidecar_generation"],
                sidecar_stat=_parse_sidecar_stat(raw["sidecar_stat"]),
                truncation_watermark=raw["truncation_watermark"],
                state_message_watermark=_parse_state_watermark(
                    raw["state_message_watermark"]
                ),
                state_content_proof=_parse_content_proof(raw["state_content_proof"]),
                settled_display_message_count=raw["settled_display_message_count"],
                visible_transcript_digest=raw["visible_transcript_digest"],
                todo_projection_generation=raw["todo_projection_generation"],
                todo_projection_watermark=_parse_state_watermark(
                    raw["todo_projection_watermark"]
                ),
                todo_projection_target_content_proof_digest=raw[
                    "todo_projection_target_content_proof_digest"
                ],
                todo_projection_snapshot_digest=raw["todo_projection_snapshot_digest"],
                generation=raw["generation"],
            )
            _validate_receipt_instance(receipt)
            return receipt
        except ReceiptStoreError:
            raise
        except Exception as exc:
            raise ReceiptStoreError("reconciliation receipt is malformed") from exc


@dataclasses.dataclass(frozen=True)
class ReceiptValidation:
    valid: bool
    reason: str


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if (
        type(value) is not int
        or value < minimum
        or value > MAX_PERSISTED_INTEGER
    ):
        raise ReceiptStoreError(
            f"{label} must be an integer in {minimum}..{MAX_PERSISTED_INTEGER}"
        )
    return value


def _require_number(value: Any, label: str) -> float | int:
    if type(value) not in (int, float):
        raise ReceiptStoreError(f"{label} must be a finite number")
    if type(value) is int and abs(value) > MAX_PERSISTED_INTEGER:
        raise ReceiptStoreError(f"{label} exceeds persisted numeric range")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ReceiptStoreError(f"{label} must be a finite number")
    return value


def _require_identifier(value: Any, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_ID_LENGTH
        or "\0" in value
    ):
        raise ReceiptStoreError(
            f"{label} must be a nonempty identifier <= {MAX_ID_LENGTH} characters"
        )
    return value


def _require_digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ReceiptStoreError(f"{label} must be a canonical sha256 digest")
    return value


def _parse_member_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ReceiptStoreError("member_ids must be an ordered sequence")
    members = tuple(
        _require_identifier(member_id, "member_id") for member_id in value
    )
    if not members or len(members) > MAX_MEMBERS:
        raise ReceiptStoreError(f"member_ids must contain 1..{MAX_MEMBERS} entries")
    if len(set(members)) != len(members):
        raise ReceiptStoreError("member_ids must be unique")
    return members


def _parse_sidecar_stat(value: Any) -> tuple[str, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ReceiptStoreError("sidecar_stat must have exactly four fields")
    path, mtime_ns, size, ctime_ns = value
    if type(path) is not str or not path or len(path) > MAX_PATH_LENGTH or "\0" in path:
        raise ReceiptStoreError("sidecar_stat path is invalid")
    return (
        path,
        _require_int(mtime_ns, "sidecar mtime"),
        _require_int(size, "sidecar size"),
        _require_int(ctime_ns, "sidecar ctime"),
    )


def _parse_sidecar_descriptor(
    value: Any,
) -> tuple[str, int, int, int, int, int, int]:
    """Parse the complete sidecar descriptor used for receipt identity."""
    if not isinstance(value, (list, tuple)) or len(value) != 7:
        raise ReceiptStoreError("sidecar descriptor must have exactly seven fields")
    path, device, inode, mode, size, mtime_ns, ctime_ns = value
    if type(path) is not str or not path or len(path) > MAX_PATH_LENGTH or "\0" in path:
        raise ReceiptStoreError("sidecar descriptor path is invalid")
    return (
        path,
        _require_int(device, "sidecar device"),
        _require_int(inode, "sidecar inode"),
        _require_int(mode, "sidecar mode"),
        _require_int(size, "sidecar size"),
        _require_int(mtime_ns, "sidecar mtime"),
        _require_int(ctime_ns, "sidecar ctime"),
    )


def _sidecar_stat_from_descriptor(
    descriptor: tuple[str, int, int, int, int, int, int],
) -> tuple[str, int, int, int]:
    """Derive the legacy compatibility tuple from one canonical descriptor."""
    path, _device, _inode, _mode, size, mtime_ns, ctime_ns = descriptor
    return path, mtime_ns, size, ctime_ns


def _parse_lineage_sidecar_proof(
    value: Any,
) -> tuple[
    tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str], ...
]:
    if not isinstance(value, (list, tuple)):
        raise ReceiptStoreError("lineage_sidecar_proof must be an ordered sequence")
    if not value or len(value) > MAX_MEMBERS:
        raise ReceiptStoreError(
            f"lineage_sidecar_proof must contain 1..{MAX_MEMBERS} rows"
        )
    normalized: list[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str]
    ] = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ReceiptStoreError(
                "lineage sidecar proof row must have exactly two fields"
            )
        member_id = _require_identifier(row[0], "lineage sidecar member_id")
        state = row[1]
        if type(state) is str:
            if state != MISSING_SIDECAR_MARKER:
                raise ReceiptStoreError("lineage sidecar missing marker is unsupported")
            normalized_state: tuple[
                int, tuple[str, int, int, int, int, int, int]
            ] | str = state
        else:
            if not isinstance(state, (list, tuple)) or len(state) != 2:
                raise ReceiptStoreError(
                    "lineage sidecar state must be generation/descriptor or missing"
                )
            normalized_state = (
                _require_int(state[0], "lineage sidecar generation"),
                _parse_sidecar_descriptor(state[1]),
            )
        normalized.append((member_id, normalized_state))
    member_ids = tuple(member_id for member_id, _ in normalized)
    if len(set(member_ids)) != len(member_ids):
        raise ReceiptStoreError("lineage sidecar proof member_ids must be unique")
    return tuple(normalized)


def _lineage_sidecar_proof_to_json(
    proof: tuple[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str], ...
    ],
) -> list[Any]:
    rows: list[Any] = []
    for member_id, state in proof:
        if state == MISSING_SIDECAR_MARKER:
            rows.append([member_id, MISSING_SIDECAR_MARKER])
        else:
            generation, signature = state
            rows.append([member_id, [generation, list(signature)]])
    return rows


def _validate_sidecar_binding(
    *,
    members: tuple[str, ...],
    canonical_sidecar_id: Any,
    lineage_sidecar_proof: Any,
    sidecar_generation: Any,
    sidecar_stat: Any,
) -> tuple[
    str,
    tuple[
        tuple[str, tuple[int, tuple[str, int, int, int, int, int, int]] | str], ...
    ],
    int,
    tuple[str, int, int, int],
]:
    canonical_id = _require_identifier(canonical_sidecar_id, "canonical_sidecar_id")
    if canonical_id not in members:
        raise ReceiptStoreError("canonical_sidecar_id must be present in member_ids")
    proof = _parse_lineage_sidecar_proof(lineage_sidecar_proof)
    if tuple(member_id for member_id, _ in proof) != members:
        raise ReceiptStoreError(
            "lineage sidecar proof must exactly cover ordered member_ids"
        )
    generation = _require_int(sidecar_generation, "sidecar_generation")
    signature = _parse_sidecar_stat(sidecar_stat)
    canonical_state = next(state for member_id, state in proof if member_id == canonical_id)
    if canonical_state == MISSING_SIDECAR_MARKER:
        raise ReceiptStoreError("canonical sidecar row cannot be missing")
    canonical_generation, canonical_descriptor = canonical_state
    if (
        canonical_generation != generation
        or _sidecar_stat_from_descriptor(canonical_descriptor) != signature
    ):
        raise ReceiptStoreError(
            "canonical sidecar row must derive sidecar_generation and sidecar_stat"
        )
    return canonical_id, proof, generation, signature


def _parse_state_watermark(value: Any) -> tuple[int, float | int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ReceiptStoreError("state_message_watermark must have exactly two fields")
    return (
        _require_int(value[0], "state message row id"),
        _require_number(value[1], "state message timestamp"),
    )


def _parse_content_proof(
    value: Any,
) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ReceiptStoreError("state content proof must have exactly two fields")
    kind, rows = value
    if type(kind) is not str or not isinstance(rows, (list, tuple)):
        raise ReceiptStoreError("state content proof is malformed")
    if kind != CONTENT_PROOF_KIND:
        raise ReceiptStoreError("state content proof contract is unsupported")
    if not rows or len(rows) > MAX_MEMBERS:
        raise ReceiptStoreError(f"content proof must contain 1..{MAX_MEMBERS} rows")
    normalized: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ReceiptStoreError("content proof row must have exactly two fields")
        normalized.append(
            (
                _require_identifier(row[0], "proof member_id"),
                _require_int(row[1], "message generation"),
            )
        )
    return kind, tuple(normalized)


def _proof_to_json(
    proof: tuple[str, tuple[tuple[str, int], ...]] | None,
) -> list[Any] | None:
    if proof is None:
        return None
    return [proof[0], [[member_id, generation] for member_id, generation in proof[1]]]


def _proof_is_declared_for(
    proof: tuple[str, tuple[tuple[str, int], ...]] | None,
    member_ids: tuple[str, ...],
) -> bool:
    return bool(
        proof
        and proof[0] == CONTENT_PROOF_KIND
        and tuple(member_id for member_id, _ in proof[1]) == member_ids
    )


def _validate_receipt_instance(receipt: ConversationReceipt) -> None:
    if type(receipt) is not ConversationReceipt:
        raise ReceiptStoreError("receipt must be a ConversationReceipt")
    if _require_int(receipt.version, "receipt version") != RECEIPT_VERSION:
        raise ReceiptStoreError("unsupported reconciliation receipt version")
    _require_identifier(receipt.profile, "profile")
    root_id = _require_identifier(receipt.root_id, "root_id")
    if type(receipt.member_ids) is not tuple:
        raise ReceiptStoreError("in-memory member_ids must be a tuple")
    members = _parse_member_ids(receipt.member_ids)
    if root_id not in members:
        raise ReceiptStoreError("root_id must be present in member_ids")
    _require_identifier(receipt.lineage_fingerprint, "lineage_fingerprint")
    if type(receipt.lineage_sidecar_proof) is not tuple or any(
        type(row) is not tuple for row in receipt.lineage_sidecar_proof
    ):
        raise ReceiptStoreError("in-memory lineage_sidecar_proof must use tuples")
    if type(receipt.sidecar_stat) is not tuple:
        raise ReceiptStoreError("in-memory sidecar_stat must be a tuple")
    canonical_id, sidecar_proof, _, _ = _validate_sidecar_binding(
        members=members,
        canonical_sidecar_id=receipt.canonical_sidecar_id,
        lineage_sidecar_proof=receipt.lineage_sidecar_proof,
        sidecar_generation=receipt.sidecar_generation,
        sidecar_stat=receipt.sidecar_stat,
    )
    for _, state in receipt.lineage_sidecar_proof:
        if state != MISSING_SIDECAR_MARKER and (
            type(state) is not tuple or type(state[1]) is not tuple
        ):
            raise ReceiptStoreError(
                "in-memory lineage sidecar states must use tuples"
            )
    if canonical_id != receipt.canonical_sidecar_id or sidecar_proof != (
        receipt.lineage_sidecar_proof
    ):
        raise ReceiptStoreError("in-memory lineage sidecar proof is noncanonical")
    if receipt.truncation_watermark is not None:
        _require_number(receipt.truncation_watermark, "truncation_watermark")
    if type(receipt.state_message_watermark) is not tuple:
        raise ReceiptStoreError("in-memory state_message_watermark must be a tuple")
    _parse_state_watermark(receipt.state_message_watermark)
    if receipt.state_content_proof is not None:
        if type(receipt.state_content_proof) is not tuple:
            raise ReceiptStoreError("in-memory state_content_proof must use tuples")
    proof = _parse_content_proof(receipt.state_content_proof)
    if proof is not None and (
        type(receipt.state_content_proof[1]) is not tuple
        or any(type(row) is not tuple for row in receipt.state_content_proof[1])
    ):
        raise ReceiptStoreError("in-memory state_content_proof must use tuples")
    if proof is not None and tuple(row[0] for row in proof[1]) != members:
        raise ReceiptStoreError("content proof must exactly cover ordered member_ids")
    _require_int(
        receipt.settled_display_message_count, "settled_display_message_count"
    )
    _require_digest(receipt.visible_transcript_digest, "visible_transcript_digest")
    if _require_int(receipt.todo_projection_generation, "todo_projection_generation") < 1:
        raise ReceiptStoreError("todo_projection_generation must be positive")
    if type(receipt.todo_projection_watermark) is not tuple:
        raise ReceiptStoreError("in-memory todo_projection_watermark must be a tuple")
    projection_watermark = _parse_state_watermark(receipt.todo_projection_watermark)
    if projection_watermark != receipt.state_message_watermark:
        raise ReceiptStoreError("todo projection watermark must equal state watermark")
    projection_proof = _require_digest(
        receipt.todo_projection_target_content_proof_digest,
        "todo_projection_target_content_proof_digest",
    )
    if proof is None or projection_proof != canonical_proof_digest(
        receipt.lineage_fingerprint, proof
    ):
        raise ReceiptStoreError("todo projection target proof digest is mismatched")
    _require_digest(receipt.todo_projection_snapshot_digest, "todo_projection_snapshot_digest")
    _require_int(receipt.generation, "receipt generation")


def canonical_proof_digest(
    lineage_fingerprint: str, state_content_proof: Any
) -> str:
    """Hash the complete ordered target vector and lineage binding."""
    lineage = _require_identifier(lineage_fingerprint, "lineage_fingerprint")
    proof = _parse_content_proof(state_content_proof)
    if proof is None or proof[0] != CONTENT_PROOF_KIND:
        raise ReceiptStoreError("declared state content proof is required")
    member_ids = tuple(member_id for member_id, _ in proof[1])
    if len(set(member_ids)) != len(member_ids):
        raise ReceiptStoreError("content proof member_ids must be unique")
    payload = json.dumps(
        {"lineage_fingerprint": lineage, "proof": _proof_to_json(proof)},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_current(current: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(current, Mapping):
        raise ReceiptStoreError("current proof must be a mapping")
    members = _parse_member_ids(current.get("member_ids"))
    root_id = _require_identifier(current.get("root_id"), "current root_id")
    if root_id not in members:
        raise ReceiptStoreError("current root_id must be present in member_ids")
    proof = _parse_content_proof(current.get("state_content_proof"))
    if (
        current.get("state_content_proof_capability")
        is not VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY
    ):
        raise ReceiptStoreError("current state lacks verified Agent proof capability")
    if not _proof_is_declared_for(proof, members):
        raise ReceiptStoreError("current state lacks declared target content proof")
    canonical_id, sidecar_proof, sidecar_generation, sidecar_stat = (
        _validate_sidecar_binding(
            members=members,
            canonical_sidecar_id=current.get("canonical_sidecar_id"),
            lineage_sidecar_proof=current.get("lineage_sidecar_proof"),
            sidecar_generation=current.get("sidecar_generation"),
            sidecar_stat=current.get("sidecar_stat"),
        )
    )
    truncation = current.get("truncation_watermark")
    if truncation is not None:
        truncation = _require_number(truncation, "current truncation_watermark")
    parsed = {
        "profile": _require_identifier(current.get("profile"), "current profile"),
        "root_id": root_id,
        "member_ids": members,
        "lineage_fingerprint": _require_identifier(
            current.get("lineage_fingerprint"), "current lineage_fingerprint"
        ),
        "canonical_sidecar_id": canonical_id,
        "lineage_sidecar_proof": sidecar_proof,
        "sidecar_generation": sidecar_generation,
        "sidecar_stat": sidecar_stat,
        "truncation_watermark": truncation,
        "state_message_watermark": _parse_state_watermark(
            current.get("state_message_watermark")
        ),
        "state_content_proof": proof,
        "settled_display_message_count": _require_int(
            current.get("settled_display_message_count"),
            "current settled_display_message_count",
        ),
        "visible_transcript_digest": _require_digest(
            current.get("visible_transcript_digest"),
            "current visible_transcript_digest",
        ),
        "todo_projection_generation": _require_int(
            current.get("todo_projection_generation"),
            "current todo_projection_generation",
            minimum=1,
        ),
        "todo_projection_watermark": _parse_state_watermark(
            current.get("todo_projection_watermark")
        ),
        "todo_projection_target_content_proof_digest": _require_digest(
            current.get("todo_projection_target_content_proof_digest"),
            "current todo_projection_target_content_proof_digest",
        ),
        "todo_projection_snapshot_digest": _require_digest(
            current.get("todo_projection_snapshot_digest"),
            "current todo_projection_snapshot_digest",
        ),
    }
    return parsed


def validate_receipt(
    receipt: ConversationReceipt,
    *,
    current: Mapping[str, Any],
    cursor_epoch: int | None = None,
    cursor_proof_digest: str | None = None,
) -> ReceiptValidation:
    """Validate every receipt binding without promoting weak hints to proof."""
    try:
        _validate_receipt_instance(receipt)
    except ReceiptStoreError:
        return ReceiptValidation(False, "receipt_invalid")
    try:
        current_snapshot = _parse_current(current)
    except ReceiptStoreError:
        return ReceiptValidation(False, "unverifiable_current_state")
    if not _proof_is_declared_for(receipt.state_content_proof, receipt.member_ids):
        return ReceiptValidation(False, "receipt_invalid")

    for field in (
        "profile",
        "root_id",
        "member_ids",
        "lineage_fingerprint",
        "canonical_sidecar_id",
        "lineage_sidecar_proof",
        "sidecar_generation",
        "sidecar_stat",
        "truncation_watermark",
        "state_message_watermark",
        "state_content_proof",
        "settled_display_message_count",
        "visible_transcript_digest",
        "todo_projection_generation",
        "todo_projection_watermark",
        "todo_projection_target_content_proof_digest",
        "todo_projection_snapshot_digest",
    ):
        if current_snapshot[field] != getattr(receipt, field):
            return _mismatch(cursor_epoch)

    digest = canonical_proof_digest(
        receipt.lineage_fingerprint, current_snapshot["state_content_proof"]
    )
    if cursor_epoch is not None:
        if type(cursor_epoch) is not int or cursor_epoch != receipt.generation:
            return ReceiptValidation(False, "cursor_restart_required")
        if cursor_proof_digest != digest:
            return ReceiptValidation(False, "cursor_restart_required")
    return ReceiptValidation(True, "valid")


def _mismatch(cursor_epoch: int | None) -> ReceiptValidation:
    reason = "cursor_restart_required" if cursor_epoch is not None else "receipt_mismatch"
    return ReceiptValidation(False, reason)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ConversationReceiptStore:
    """Atomically publish hashed receipts under one store-wide epoch owner."""

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir) / "conversation_receipts"

    @staticmethod
    def _key(profile: str, root_id: str) -> str:
        profile = _require_identifier(profile, "profile")
        root_id = _require_identifier(root_id, "root_id")
        return hashlib.sha256(f"{profile}\0{root_id}".encode("utf-8")).hexdigest()

    def _lock(self) -> threading.RLock:
        key = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).digest()
        return _STORE_LOCK_STRIPES[int.from_bytes(key[:4], "big") % len(_STORE_LOCK_STRIPES)]

    def receipt_path(self, profile: str, root_id: str) -> Path:
        return self.root / f"{self._key(profile, root_id)}.receipt.json"

    def guard_path(self, profile: str, root_id: str) -> Path:
        return self.root / f"{self._key(profile, root_id)}.receipt.guard.json"

    def high_water_path(self) -> Path:
        return self.root / "_store_epoch_high_water.json"

    def initialization_marker_path(self) -> Path:
        return self.root / "_store_epoch_initialized.json"

    def lock_path(self) -> Path:
        return self.root / ".store.lock"

    @contextmanager
    def _interprocess_lock(self):
        """Serialize epoch allocation across WebUI processes or fail closed."""
        if fcntl is None:
            raise ReceiptStoreError("receipt store interprocess lock unavailable")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + STORE_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ReceiptStoreError(
                            "receipt store interprocess lock unavailable"
                        ) from None
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            yield
        except ReceiptStoreError:
            raise
        except OSError as exc:
            raise ReceiptStoreError(
                "receipt store interprocess lock unavailable"
            ) from exc
        finally:
            if descriptor is not None and fcntl is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def load(self, profile: str, root_id: str) -> ConversationReceipt | None:
        path = self.receipt_path(profile, root_id)
        guard_path = self.guard_path(profile, root_id)
        with self._lock():
            with self._interprocess_lock():
                if self._guard_exists(guard_path):
                    raise ReceiptStoreError("reconciliation receipt is guarded")
                try:
                    raw = self._read_json(path, "reconciliation receipt")
                    receipt = ConversationReceipt.from_dict(raw)
                except FileNotFoundError:
                    return None
                except ReceiptStoreError:
                    raise
                except Exception as exc:
                    raise ReceiptStoreError(
                        f"reconciliation receipt is unreadable: {path.name}"
                    ) from exc
                if receipt.profile != profile or receipt.root_id != root_id:
                    raise ReceiptStoreError("receipt key does not match receipt identity")
                high_water = self._load_or_initialize_high_water()
                if high_water < receipt.generation:
                    raise ReceiptStoreError("receipt epoch exceeds store high-water")
                return receipt

    @staticmethod
    def _guard_exists(path: Path) -> bool:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ReceiptStoreError("reconciliation receipt guard is unreadable") from exc
        return True

    def _write_guard(self, path: Path, state: str, generation: int) -> None:
        self._atomic_write(
            path.with_suffix(".guard.json"),
            {
                "version": RECEIPT_VERSION,
                "state": state,
                "generation": _require_int(generation, "receipt guard generation"),
            },
        )

    @staticmethod
    def _clear_guard(path: Path) -> None:
        guard_path = path.with_suffix(".guard.json")
        try:
            guard_path.unlink(missing_ok=True)
            _fsync_directory(guard_path.parent)
        except OSError as exc:
            raise ReceiptStoreError(
                "receipt guard removal could not be made durable"
            ) from exc

    def _load_or_initialize_high_water(self) -> int:
        marker_path = self.initialization_marker_path()
        high_water_path = self.high_water_path()
        if not marker_path.exists():
            existing_receipt = (
                next(self.root.glob("*.receipt.json"), None)
                if self.root.exists()
                else None
            )
            if high_water_path.exists() or existing_receipt is not None:
                raise ReceiptStoreError("store initialization marker is missing")
            self._atomic_write(
                marker_path, {"version": RECEIPT_VERSION, "initialized": True}
            )
            self._atomic_write(
                high_water_path, {"version": RECEIPT_VERSION, "high_water": 0}
            )
            return 0

        marker = self._read_json(marker_path, "store initialization marker")
        if (
            type(marker) is not dict
            or set(marker) != {"version", "initialized"}
            or type(marker.get("version")) is not int
            or marker.get("version") != RECEIPT_VERSION
            or marker.get("initialized") is not True
        ):
            raise ReceiptStoreError("store initialization marker is malformed")
        if not high_water_path.exists():
            raise ReceiptStoreError("initialized store high-water is missing")
        raw = self._read_json(high_water_path, "store high-water")
        if type(raw) is not dict or set(raw) != {"version", "high_water"}:
            raise ReceiptStoreError("store high-water is malformed")
        if type(raw.get("version")) is not int or raw.get("version") != RECEIPT_VERSION:
            raise ReceiptStoreError("store high-water version is unsupported")
        return _require_int(raw.get("high_water"), "store high-water")

    @staticmethod
    def _read_json(path: Path, label: str) -> Any:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReceiptStoreError(f"{label} is not a regular file")
            if metadata.st_size > MAX_PERSISTED_JSON_BYTES:
                raise ReceiptStoreError(f"{label} exceeds persisted JSON byte limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65536, MAX_PERSISTED_JSON_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PERSISTED_JSON_BYTES:
                    raise ReceiptStoreError(f"{label} exceeds persisted JSON byte limit")
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))
        except FileNotFoundError:
            raise
        except ReceiptStoreError:
            raise
        except Exception as exc:
            raise ReceiptStoreError(f"{label} is unreadable") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _allocate(self, candidate_generation: int) -> int:
        high_water = self._load_or_initialize_high_water()
        generation = max(high_water, candidate_generation, 0) + 1
        self._atomic_write(
            self.high_water_path(),
            {"version": RECEIPT_VERSION, "high_water": generation},
        )
        return generation

    def publish(self, candidate: ConversationReceipt) -> ConversationReceipt:
        _validate_receipt_instance(candidate)
        with self._lock():
            with self._interprocess_lock():
                generation = self._allocate(candidate.generation)
                published = dataclasses.replace(candidate, generation=generation)
                path = self.receipt_path(candidate.profile, candidate.root_id)
                self._write_guard(path, "replace", generation)
                self._atomic_write(path, published.to_dict())
                self._clear_guard(path)
                return published

    def publish_if_current(
        self,
        candidate: ConversationReceipt,
        current_supplier: Callable[[], Mapping[str, Any]],
    ) -> ConversationReceipt:
        """Publish last only if the complete target proof remains unchanged."""
        _validate_receipt_instance(candidate)
        initial = validate_receipt(candidate, current=current_supplier())
        if not initial.valid:
            raise ReceiptStoreError(f"current proof is not publishable: {initial.reason}")

        with self._lock():
            with self._interprocess_lock():
                generation = self._allocate(candidate.generation)
                published = dataclasses.replace(candidate, generation=generation)
                path = self.receipt_path(candidate.profile, candidate.root_id)
                self._write_guard(path, "replace", generation)
                tmp = self._prepare_atomic_write(path, published.to_dict())
                try:
                    final = validate_receipt(candidate, current=current_supplier())
                    if not final.valid:
                        raise ReceiptStoreError(
                            f"current proof changed during publication: {final.reason}"
                        )
                    self._replace_prepared(tmp, path)
                    post_replace = validate_receipt(candidate, current=current_supplier())
                    if not post_replace.valid:
                        self._delete_if_generation(path, published.generation)
                        raise ReceiptStoreError(
                            "current proof changed after publication: "
                            f"{post_replace.reason}"
                        )
                    self._clear_guard(path)
                    return published
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass

    def delete(self, profile: str, root_id: str) -> None:
        with self._lock():
            with self._interprocess_lock():
                path = self.receipt_path(profile, root_id)
                try:
                    self._write_guard(path, "tombstone", 0)
                    path.unlink(missing_ok=True)
                    if self.root.exists():
                        _fsync_directory(self.root)
                except OSError as exc:
                    raise ReceiptStoreError("receipt deletion could not be made durable") from exc

    def _delete_if_generation(self, path: Path, generation: int) -> bool:
        """Delete only the receipt this transaction published, then fsync it."""
        try:
            raw = self._read_json(path, "reconciliation receipt")
            receipt = ConversationReceipt.from_dict(raw)
        except FileNotFoundError:
            return False
        except ReceiptStoreError:
            return False
        if receipt.generation != generation:
            return False
        try:
            self._write_guard(path, "tombstone", generation)
            path.unlink()
            _fsync_directory(path.parent)
            return True
        except OSError as exc:
            raise ReceiptStoreError(
                "post-publication receipt rollback could not be made durable"
            ) from exc

    @staticmethod
    def _prepare_atomic_write(path: Path, value: Mapping[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    value,
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            return tmp
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReceiptStoreError(f"atomic write preparation failed: {path.name}") from exc

    @staticmethod
    def _replace_prepared(tmp: Path, path: Path) -> None:
        try:
            os.replace(tmp, path)
            _fsync_directory(path.parent)
        except Exception as exc:
            raise ReceiptStoreError(f"atomic write failed: {path.name}") from exc

    @classmethod
    def _atomic_write(cls, path: Path, value: Mapping[str, Any]) -> None:
        tmp = cls._prepare_atomic_write(path, value)
        try:
            cls._replace_prepared(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
