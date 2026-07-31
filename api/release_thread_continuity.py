"""Deterministic state for managed release checkpoint and resume control.

This module deliberately has no model, network, process, or filesystem calls.
The signed release API and cutover controller own those side effects; this
module owns the small state-machine invariants they must not duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from typing import Any


CHECKPOINT_TIMEOUT_SECONDS = 300.0
_DELIVERY_STATES = {"not_sent", "intent", "accepted", "inactive", "ambiguous", "undelivered"}
_THREAD_STATES = {
    "active",
    "acknowledged",
    "settled_without_ack",
    "owner_changed",
    "unavailable",
    "forced",
}


class CheckpointStateError(RuntimeError):
    """A checkpoint transition would violate the release contract."""


@dataclass(frozen=True)
class CheckpointDeadline:
    """One persisted wall/monotonic deadline tuple for a transaction."""

    wall_started_at: float
    wall_deadline: float
    monotonic_started_at: float
    monotonic_deadline: float
    boot_id: str

    @classmethod
    def create(
        cls,
        *,
        wall_started_at: float,
        monotonic_started_at: float,
        boot_id: str,
        timeout_seconds: float = CHECKPOINT_TIMEOUT_SECONDS,
    ) -> "CheckpointDeadline":
        if float(timeout_seconds) != CHECKPOINT_TIMEOUT_SECONDS:
            raise ValueError("checkpoint deadline must be exactly 300 seconds")
        if not str(boot_id or "").strip():
            raise ValueError("checkpoint boot identity is required")
        wall_started = float(wall_started_at)
        monotonic_started = float(monotonic_started_at)
        return cls(
            wall_started_at=wall_started,
            wall_deadline=wall_started + CHECKPOINT_TIMEOUT_SECONDS,
            monotonic_started_at=monotonic_started,
            monotonic_deadline=monotonic_started + CHECKPOINT_TIMEOUT_SECONDS,
            boot_id=str(boot_id),
        )

    def reached(self, *, wall_now: float, monotonic_now: float, boot_id: str) -> bool:
        """Fail closed on either clock, boot change, or invalid clock evidence."""
        if str(boot_id or "") != self.boot_id:
            return True
        try:
            wall = float(wall_now)
            monotonic = float(monotonic_now)
        except (TypeError, ValueError):
            return True
        return wall >= self.wall_deadline or monotonic >= self.monotonic_deadline


def _target_id(*, service: str, session_id: str, stream_id: str) -> str:
    payload = json.dumps(
        {
            "service": str(service),
            "session_id": str(session_id),
            "stream_id": str(stream_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _validate_nonempty(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


class CheckpointLedger:
    """In-memory transaction ledger used by durable API/controller adapters.

    Persistence is intentionally supplied by the caller so this class can be
    tested without touching real Hermes state. Callers must persist the
    returned transition before performing the corresponding external action.
    """

    def __init__(self, *, transaction_id: str):
        self.transaction_id = _validate_nonempty(transaction_id, "transaction_id")
        self._reservations: dict[str, dict[str, str]] = {}
        self._targets: dict[str, dict[str, Any]] = {}
        self._population_closed = False
        self._forced_reservations: tuple[str, ...] = ()

    @property
    def reservation_count(self) -> int:
        return len(self._reservations)

    @property
    def population_closed(self) -> bool:
        return self._population_closed

    @property
    def forced_reservations(self) -> tuple[str, ...]:
        return self._forced_reservations

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(self._targets)

    def reserve(self, reservation_id: str, *, service: str, kind: str) -> bool:
        reservation = _validate_nonempty(reservation_id, "reservation_id")
        if self._population_closed:
            raise CheckpointStateError("checkpoint population is already closed")
        if reservation in self._reservations:
            return False
        self._reservations[reservation] = {
            "service": _validate_nonempty(service, "service"),
            "kind": _validate_nonempty(kind, "kind"),
        }
        return True

    def release_reservation(self, reservation_id: str) -> bool:
        return self._reservations.pop(str(reservation_id), None) is not None

    def enroll(
        self,
        *,
        service: str,
        session_id: str,
        stream_id: str,
        backend: str,
    ) -> str:
        if self._population_closed:
            raise CheckpointStateError("checkpoint population is already closed")
        service = _validate_nonempty(service, "service")
        session_id = _validate_nonempty(session_id, "session_id")
        stream_id = _validate_nonempty(stream_id, "stream_id")
        backend = _validate_nonempty(backend, "backend")
        target = _target_id(
            service=service,
            session_id=session_id,
            stream_id=stream_id,
        )
        existing = self._targets.get(target)
        if existing is not None:
            if existing["backend"] != backend:
                raise CheckpointStateError("checkpoint target backend changed")
            return target
        self._targets[target] = {
            "service": service,
            "session_id": session_id,
            "stream_id": stream_id,
            "backend": backend,
            "delivery": {"status": "not_sent"},
            "state": "active",
        }
        return target

    def close_population(self, *, forced: bool = False) -> tuple[str, ...]:
        if self._population_closed:
            return self._forced_reservations
        if self._reservations and not forced:
            raise CheckpointStateError("checkpoint population has live reservations")
        self._forced_reservations = tuple(sorted(self._reservations))
        if not forced:
            self._reservations.clear()
        else:
            self._reservations.clear()
        self._population_closed = True
        return self._forced_reservations

    def _target(self, target_id: str) -> dict[str, Any]:
        try:
            return self._targets[str(target_id)]
        except KeyError as exc:
            raise CheckpointStateError("checkpoint target is unknown") from exc

    def mark_delivery_intent(self, target_id: str) -> bool:
        target = self._target(target_id)
        delivery = target["delivery"]
        if delivery["status"] != "not_sent":
            return False
        delivery.update({"status": "intent"})
        return True

    def record_delivery(self, target_id: str, status: str) -> bool:
        target = self._target(target_id)
        status = _validate_nonempty(status, "delivery status")
        if status not in _DELIVERY_STATES - {"not_sent", "intent"}:
            raise ValueError("delivery status is invalid")
        delivery = target["delivery"]
        if delivery["status"] == status:
            return False
        if delivery["status"] not in {"intent", "not_sent"}:
            return False
        delivery.update({"status": status})
        return True

    def delivery_state(self, target_id: str) -> dict[str, str]:
        return dict(self._target(target_id)["delivery"])

    def record_status(self, target_id: str, status: str) -> bool:
        target = self._target(target_id)
        status = _validate_nonempty(status, "thread status")
        if status not in _THREAD_STATES:
            raise ValueError("thread status is invalid")
        if target["state"] == status:
            return False
        if target["state"] in {"acknowledged", "settled_without_ack", "forced"}:
            return False
        target["state"] = status
        return True

    @property
    def all_targets_resolved(self) -> bool:
        return self._population_closed and all(
            target["state"] in {"acknowledged", "settled_without_ack"}
            for target in self._targets.values()
        )

    def resume_sessions(self) -> tuple[str, ...]:
        states: dict[str, list[str]] = {}
        for target in self._targets.values():
            states.setdefault(target["session_id"], []).append(target["state"])
        return tuple(
            sorted(
                session_id
                for session_id, values in states.items()
                if fold_resume_state(values) in {"acknowledged", "forced"}
            )
        )

    def export(self) -> dict[str, Any]:
        """Return a JSON-safe durable snapshot of the ledger."""
        return {
            "schema": "hermes.release_checkpoint_ledger.v1",
            "transaction_id": self.transaction_id,
            "reservations": copy.deepcopy(self._reservations),
            "targets": copy.deepcopy(self._targets),
            "population_closed": self._population_closed,
            "forced_reservations": list(self._forced_reservations),
        }

    @classmethod
    def from_export(cls, payload: dict[str, Any]) -> "CheckpointLedger":
        if not isinstance(payload, dict):
            raise ValueError("checkpoint ledger payload is invalid")
        if payload.get("schema") != "hermes.release_checkpoint_ledger.v1":
            raise ValueError("checkpoint ledger schema is invalid")
        ledger = cls(transaction_id=payload.get("transaction_id"))
        reservations = payload.get("reservations")
        targets = payload.get("targets")
        forced = payload.get("forced_reservations")
        if (
            not isinstance(reservations, dict)
            or not isinstance(targets, dict)
            or not isinstance(payload.get("population_closed"), bool)
            or not isinstance(forced, list)
        ):
            raise ValueError("checkpoint ledger fields are invalid")
        ledger._reservations = copy.deepcopy(reservations)
        ledger._targets = copy.deepcopy(targets)
        ledger._population_closed = payload["population_closed"]
        ledger._forced_reservations = tuple(str(value) for value in forced)
        return ledger


def fold_resume_state(states: list[str] | tuple[str, ...]) -> str:
    """Fold duplicate stream states using the reviewed conservative order."""
    values = tuple(str(value) for value in states)
    if not values:
        raise ValueError("at least one resume state is required")
    order = {
        "settled_without_ack": 0,
        "acknowledged": 1,
        "forced": 2,
        "interrupted": 2,
        "owner_changed": 3,
        "unavailable": 3,
    }
    unknown = [value for value in values if value not in order]
    if unknown:
        raise ValueError("resume state is invalid")
    return max(values, key=lambda value: order[value])
