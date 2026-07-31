"""Process-epoch-bound authority for WebUI background worker threads."""

from __future__ import annotations

import math
import os
import secrets
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from api.process_identity import process_start_token


class ManagedBackgroundWorkerError(RuntimeError):
    """Base error for strict background-worker lifecycle operations."""


class ManagedBackgroundWorkerStartError(ManagedBackgroundWorkerError):
    """A worker could not prove that it entered and remained live."""


class ManagedBackgroundWorkerStopError(ManagedBackgroundWorkerError):
    """A worker could not be stopped within the bounded join."""


class ManagedBackgroundWorkerOutcome(str, Enum):
    COMPLETE = "complete"
    ABSENT = "absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ProcessEpoch:
    pid: int
    start_token: str


@dataclass(frozen=True)
class ManagedBackgroundWorkerReceipt:
    process_epoch: ProcessEpoch
    worker_kind: str
    generation: int
    worker_identity: str
    thread_identity: int
    thread_name: str


@dataclass(frozen=True)
class ManagedBackgroundWorkerStart:
    receipt: ManagedBackgroundWorkerReceipt
    started: bool


@dataclass(frozen=True)
class ManagedBackgroundWorkerVerification:
    outcome: ManagedBackgroundWorkerOutcome
    receipt: ManagedBackgroundWorkerReceipt | None
    reason: str | None


@dataclass
class _WorkerState:
    receipt: ManagedBackgroundWorkerReceipt
    thread: threading.Thread
    readiness: threading.Event
    exited: threading.Event
    stop_event: threading.Event
    start_failed: bool = False


def current_process_epoch() -> ProcessEpoch | None:
    pid = os.getpid()
    token = process_start_token(pid)
    if not token:
        return None
    return ProcessEpoch(pid=pid, start_token=token)


def _bounded_timeout(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite non-negative number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return timeout


class ManagedBackgroundWorker:
    """Own one exact named worker and its process-local authority receipt."""

    def __init__(self, worker_kind: str, thread_name: str):
        if not worker_kind or not thread_name:
            raise ValueError("worker identity must be non-empty")
        self.worker_kind = worker_kind
        self.thread_name = thread_name
        self._lock = threading.RLock()
        self._generation = 0
        self._state: _WorkerState | None = None
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self.reset_after_fork)

    def _named_live_threads(self) -> tuple[threading.Thread, ...]:
        return tuple(
            thread
            for thread in threading.enumerate()
            if thread.name == self.thread_name and thread.is_alive()
        )

    def _verify_locked(
        self,
        *,
        published_thread: threading.Thread | None,
        receipt: ManagedBackgroundWorkerReceipt | None,
    ) -> ManagedBackgroundWorkerVerification:
        epoch = current_process_epoch()
        if epoch is None:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                self._state.receipt if self._state is not None else receipt,
                "process_epoch_unavailable",
            )
        live = self._named_live_threads()
        state = self._state
        if published_thread is not None:
            is_alive = getattr(published_thread, "is_alive", None)
            if not callable(is_alive):
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    state.receipt if state is not None else receipt,
                    "published_worker_is_invalid",
                )
            try:
                published_liveness = is_alive()
            except Exception:
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    state.receipt if state is not None else receipt,
                    "published_worker_is_invalid",
                )
            if type(published_liveness) is not bool:
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    state.receipt if state is not None else receipt,
                    "published_worker_is_invalid",
                )
        if state is None:
            if live:
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    receipt,
                    "unreferenced_live_worker",
                )
            if published_thread is not None:
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    receipt,
                    "published_worker_without_state",
                )
            if receipt is not None:
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                    receipt,
                    (
                        "receipt_from_foreign_process_epoch"
                        if receipt.process_epoch != epoch
                        else "receipt_without_worker_state"
                    ),
                )
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.ABSENT,
                None,
                "managed_worker_not_started",
            )
        authoritative = state.receipt
        if authoritative.process_epoch != epoch:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                authoritative,
                "worker_state_from_foreign_process_epoch",
            )
        if receipt is not None and receipt != authoritative:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                authoritative,
                "worker_receipt_mismatch",
            )
        if len(live) > 1:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                authoritative,
                "duplicate_live_workers",
            )
        if live and live[0] is not state.thread:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                authoritative,
                "unreferenced_live_worker",
            )
        if published_thread is not state.thread:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.AMBIGUOUS,
                authoritative,
                "published_worker_identity_mismatch",
            )
        if state.start_failed:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.PARTIAL,
                authoritative,
                "worker_start_failed",
            )
        if not state.readiness.is_set():
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.PARTIAL,
                authoritative,
                "worker_not_ready",
            )
        if state.exited.is_set() or not state.thread.is_alive():
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.PARTIAL,
                authoritative,
                "worker_exited_after_entry",
            )
        if state.stop_event.is_set():
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.PARTIAL,
                authoritative,
                "worker_stop_requested",
            )
        if len(live) != 1:
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.PARTIAL,
                authoritative,
                "worker_live_identity_unobservable",
            )
        return ManagedBackgroundWorkerVerification(
            ManagedBackgroundWorkerOutcome.COMPLETE,
            authoritative,
            None,
        )

    def verify(
        self,
        *,
        get_published_thread: Callable[[], threading.Thread | None],
        receipt: ManagedBackgroundWorkerReceipt | None = None,
    ) -> ManagedBackgroundWorkerVerification:
        with self._lock:
            return self._verify_locked(
                published_thread=get_published_thread(),
                receipt=receipt,
            )

    def start(
        self,
        *,
        target: Callable[[threading.Event], None],
        stop_event: threading.Event,
        get_published_thread: Callable[[], threading.Thread | None],
        publish_thread: Callable[[threading.Thread | None], None],
        readiness_timeout: float,
    ) -> ManagedBackgroundWorkerStart:
        timeout = _bounded_timeout(readiness_timeout, "readiness timeout")
        started = False
        with self._lock:
            current = self._verify_locked(
                published_thread=get_published_thread(),
                receipt=None,
            )
            if current.outcome is ManagedBackgroundWorkerOutcome.COMPLETE:
                assert self._state is not None
                state = self._state
            elif (
                self._state is not None
                and self._state.thread.is_alive()
                and not self._state.exited.is_set()
                and current.outcome is ManagedBackgroundWorkerOutcome.PARTIAL
                and current.reason == "worker_not_ready"
            ):
                state = self._state
            else:
                if current.outcome is ManagedBackgroundWorkerOutcome.AMBIGUOUS:
                    raise ManagedBackgroundWorkerStartError(
                        current.reason or "worker authority is ambiguous"
                    )
                if self._state is not None and self._state.thread.is_alive():
                    raise ManagedBackgroundWorkerStartError(
                        current.reason or "existing worker is not restart-safe"
                    )
                self._generation += 1
                readiness = threading.Event()
                exited = threading.Event()
                stop_event.clear()
                identity = secrets.token_hex(16)
                epoch = current_process_epoch()
                if epoch is None:
                    raise ManagedBackgroundWorkerStartError(
                        "process epoch is unavailable"
                    )

                def runner() -> None:
                    try:
                        target(readiness)
                    finally:
                        exited.set()

                thread = threading.Thread(
                    target=runner,
                    name=self.thread_name,
                    daemon=True,
                )
                receipt = ManagedBackgroundWorkerReceipt(
                    process_epoch=epoch,
                    worker_kind=self.worker_kind,
                    generation=self._generation,
                    worker_identity=identity,
                    thread_identity=id(thread),
                    thread_name=self.thread_name,
                )
                state = _WorkerState(
                    receipt=receipt,
                    thread=thread,
                    readiness=readiness,
                    exited=exited,
                    stop_event=stop_event,
                )
                self._state = state
                # Publication precedes start: the worker can never run while
                # absent from the module's authoritative thread reference.
                publish_thread(thread)
                try:
                    thread.start()
                except BaseException as exc:
                    state.start_failed = True
                    exited.set()
                    raise ManagedBackgroundWorkerStartError(
                        f"{self.worker_kind} worker thread start failed"
                    ) from exc
                started = True
        if not state.readiness.wait(timeout):
            raise ManagedBackgroundWorkerStartError(
                f"{self.worker_kind} worker did not enter before readiness timeout"
            )
        with self._lock:
            verified = self._verify_locked(
                published_thread=get_published_thread(),
                receipt=state.receipt,
            )
        if verified.outcome is not ManagedBackgroundWorkerOutcome.COMPLETE:
            raise ManagedBackgroundWorkerStartError(
                verified.reason or f"{self.worker_kind} worker is not complete"
            )
        return ManagedBackgroundWorkerStart(
            receipt=state.receipt,
            started=started,
        )

    def stop(
        self,
        *,
        stop_event: threading.Event,
        get_published_thread: Callable[[], threading.Thread | None],
        publish_thread: Callable[[threading.Thread | None], None],
        timeout: float,
    ) -> ManagedBackgroundWorkerVerification:
        bounded = _bounded_timeout(timeout, "stop timeout")
        with self._lock:
            state = self._state
            if state is None:
                verification = self._verify_locked(
                    published_thread=get_published_thread(),
                    receipt=None,
                )
                if verification.outcome is ManagedBackgroundWorkerOutcome.AMBIGUOUS:
                    raise ManagedBackgroundWorkerStopError(
                        verification.reason or "worker authority is ambiguous"
                    )
                return verification
            stop_event.set()
            thread = state.thread
            captured_generation = state.receipt.generation
        if thread.is_alive():
            thread.join(timeout=bounded)
        with self._lock:
            current = self._state
            if current is None:
                if self._generation != captured_generation:
                    raise ManagedBackgroundWorkerStopError(
                        "worker generation changed during concurrent stop"
                    )
                if self._named_live_threads() or get_published_thread() is not None:
                    raise ManagedBackgroundWorkerStopError(
                        "worker authority changed while concurrent stop completed"
                    )
                return ManagedBackgroundWorkerVerification(
                    ManagedBackgroundWorkerOutcome.ABSENT,
                    None,
                    "managed_worker_already_stopped",
                )
            if current is not state:
                raise ManagedBackgroundWorkerStopError(
                    "worker generation changed during concurrent stop"
                )
            if thread.is_alive():
                raise ManagedBackgroundWorkerStopError(
                    f"{self.worker_kind} worker remained live after bounded stop"
                )
            live = self._named_live_threads()
            if live:
                raise ManagedBackgroundWorkerStopError(
                    "unreferenced or duplicate worker remained after stop"
                )
            if get_published_thread() is not thread:
                raise ManagedBackgroundWorkerStopError(
                    "published worker identity changed during stop"
                )
            publish_thread(None)
            self._state = None
            return ManagedBackgroundWorkerVerification(
                ManagedBackgroundWorkerOutcome.ABSENT,
                None,
                "managed_worker_stopped",
            )

    def reset_after_fork(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._state = None

    def reset_for_tests(self) -> None:
        with self._lock:
            if self._state is not None and self._state.thread.is_alive():
                raise ManagedBackgroundWorkerError(
                    "cannot reset live managed worker authority"
                )
            self._generation = 0
            self._state = None
