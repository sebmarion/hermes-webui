from __future__ import annotations

import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest


def test_managed_background_worker_api_is_explicit():
    from api import background_process as bp

    assert all(
        hasattr(bp, name)
        for name in (
            "start_managed_drain_worker",
            "verify_managed_drain_worker",
            "stop_managed_drain_worker",
            "start_managed_session_channel_reaper",
            "verify_managed_session_channel_reaper",
            "stop_managed_session_channel_reaper",
            "_reset_background_worker_lifecycle_for_tests",
        )
    )


@pytest.fixture
def workers(monkeypatch):
    from api import background_process as bp

    reset = getattr(bp, "_reset_background_worker_lifecycle_for_tests", None)
    assert callable(reset), "managed background-worker reset hook is missing"
    reset()
    yield bp
    for stop_name in (
        "stop_managed_drain_worker",
        "stop_managed_session_channel_reaper",
    ):
        stop = getattr(bp, stop_name, None)
        if callable(stop):
            try:
                stop(timeout=0.5)
            except Exception:
                pass
    reset()


def _blocking_loop(release: threading.Event):
    def loop(readiness: threading.Event) -> None:
        readiness.set()
        release.wait(5.0)

    return loop


def test_drain_start_has_no_hidden_recovery_or_replay(workers, monkeypatch):
    bp = workers
    release = threading.Event()
    calls: list[str] = []
    monkeypatch.setattr(bp, "_drain_loop", lambda: release.wait(5.0))
    monkeypatch.setattr(
        bp,
        "recover_profile_async_delegations",
        lambda: calls.append("recover") or 0,
    )
    monkeypatch.setattr(
        bp,
        "replay_pending_delegation_wakeups",
        lambda: calls.append("replay") or 0,
    )

    try:
        assert bp.start_drain_thread() is True
        assert calls == []
        bp.recover_profile_async_delegations()
        bp.replay_pending_delegation_wakeups()
        assert calls == ["recover", "replay"]
    finally:
        release.set()
        bp.stop_drain_thread(timeout=1.0)


def test_async_tracker_recovery_restores_private_mode_after_atomic_replace(
    workers, monkeypatch, tmp_path
):
    """Recovery must preserve the owner-only tracker contract across writers."""
    bp = workers
    import api.profiles as profiles

    tracker = tmp_path / "async_delegations.json"
    tracker.write_text("{}", encoding="utf-8")
    tracker.chmod(0o644)
    observed_before = []

    def recover_async_delegations(*, tracker_path):
        target = Path(tracker_path)
        observed_before.append(target.stat().st_mode & 0o777)
        replacement = target.with_suffix(".replacement")
        replacement.write_text("{}", encoding="utf-8")
        replacement.chmod(0o644)
        os.replace(replacement, target)
        return {"queued": 1}

    fake_module = types.ModuleType("tools.async_delegation")
    fake_module.recover_async_delegations = recover_async_delegations
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_module)
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"path": str(tmp_path)}],
    )

    assert bp.recover_profile_async_delegations() == 1
    assert observed_before == [0o600]
    assert tracker.stat().st_mode & 0o777 == 0o600


def test_unmanaged_legacy_start_does_not_require_process_epoch(
    workers, monkeypatch
):
    from api import managed_background_workers as managed

    bp = workers
    release = threading.Event()
    monkeypatch.setattr(managed, "current_process_epoch", lambda: None)
    monkeypatch.setattr(bp, "_drain_loop", lambda: release.wait(5.0))
    try:
        assert bp.start_drain_thread() is True
        assert bp._DRAIN_THREAD is not None
        assert bp._DRAIN_THREAD.is_alive()
    finally:
        release.set()
        bp.stop_drain_thread(timeout=1.0)


@pytest.mark.parametrize(
    ("start_name", "verify_name", "stop_name", "loop_name", "kind"),
    [
        (
            "start_managed_drain_worker",
            "verify_managed_drain_worker",
            "stop_managed_drain_worker",
            "_drain_loop",
            "drain",
        ),
        (
            "start_managed_session_channel_reaper",
            "verify_managed_session_channel_reaper",
            "stop_managed_session_channel_reaper",
            "_reaper_loop",
            "session-channel-reaper",
        ),
    ],
)
def test_managed_worker_receipt_is_epoch_bound_and_exact(
    workers,
    monkeypatch,
    start_name,
    verify_name,
    stop_name,
    loop_name,
    kind,
):
    from api.managed_background_workers import (
        ManagedBackgroundWorkerOutcome,
        current_process_epoch,
    )

    bp = workers
    release = threading.Event()
    monkeypatch.setattr(bp, loop_name, _blocking_loop(release))
    verify = getattr(bp, verify_name)

    assert verify().outcome is ManagedBackgroundWorkerOutcome.ABSENT
    started = getattr(bp, start_name)(readiness_timeout=1.0)
    assert started.started is True
    receipt = started.receipt
    assert receipt.process_epoch == current_process_epoch()
    assert receipt.worker_kind == kind
    assert receipt.generation == 1
    assert receipt.thread_identity > 0
    assert receipt.worker_identity
    assert verify(receipt).outcome is ManagedBackgroundWorkerOutcome.COMPLETE
    assert getattr(bp, start_name)(readiness_timeout=1.0) == type(started)(
        receipt=receipt,
        started=False,
    )

    release.set()
    getattr(bp, stop_name)(timeout=1.0)
    assert verify().outcome is ManagedBackgroundWorkerOutcome.ABSENT


def test_readiness_is_not_published_before_loop_entry(workers, monkeypatch):
    bp = workers
    enter = threading.Event()
    release = threading.Event()

    def gated_loop(readiness: threading.Event) -> None:
        enter.wait(5.0)
        readiness.set()
        release.wait(5.0)

    monkeypatch.setattr(bp, "_drain_loop", gated_loop)
    result: list[object] = []
    caller = threading.Thread(
        target=lambda: result.append(
            bp.start_managed_drain_worker(readiness_timeout=2.0)
        )
    )
    caller.start()
    time.sleep(0.05)
    assert caller.is_alive()
    assert result == []
    enter.set()
    caller.join(timeout=1.0)
    assert not caller.is_alive()
    assert result and result[0].started is True
    release.set()


def test_start_then_die_is_partial_and_never_returns_a_receipt(workers, monkeypatch):
    from api.managed_background_workers import (
        ManagedBackgroundWorkerOutcome,
        ManagedBackgroundWorkerStartError,
    )

    bp = workers

    def die_after_entry(readiness: threading.Event) -> None:
        readiness.set()

    monkeypatch.setattr(bp, "_drain_loop", die_after_entry)
    with pytest.raises(ManagedBackgroundWorkerStartError):
        bp.start_managed_drain_worker(readiness_timeout=1.0)
    verification = bp.verify_managed_drain_worker()
    assert verification.outcome is ManagedBackgroundWorkerOutcome.PARTIAL
    assert verification.reason == "worker_exited_after_entry"


def test_worker_is_published_before_thread_can_enter(workers, monkeypatch):
    bp = workers
    release = threading.Event()
    observed: list[bool] = []

    def inspect_global(readiness: threading.Event) -> None:
        observed.append(bp._DRAIN_THREAD is threading.current_thread())
        readiness.set()
        release.wait(5.0)

    monkeypatch.setattr(bp, "_drain_loop", inspect_global)
    try:
        result = bp.start_managed_drain_worker(readiness_timeout=1.0)
        assert result.started is True
        assert observed == [True]
    finally:
        release.set()


def test_thread_start_crash_is_typed_and_keeps_published_evidence(
    workers, monkeypatch
):
    from api import managed_background_workers as managed

    bp = workers
    observed: list[bool] = []

    class BrokenThread:
        def __init__(self, *, target, name, daemon):
            self._target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            observed.append(bp._DRAIN_THREAD is self)
            raise OSError("thread start crashed")

        @staticmethod
        def is_alive():
            return False

    monkeypatch.setattr(managed.threading, "Thread", BrokenThread)
    with pytest.raises(
        managed.ManagedBackgroundWorkerStartError,
        match="drain worker thread start failed",
    ):
        bp.start_managed_drain_worker(readiness_timeout=1.0)
    assert observed == [True]
    assert bp._DRAIN_THREAD is not None
    verification = bp.verify_managed_drain_worker()
    assert verification.outcome is managed.ManagedBackgroundWorkerOutcome.PARTIAL
    assert verification.reason == "worker_start_failed"


def test_unreferenced_and_duplicate_named_workers_are_ambiguous(
    workers, monkeypatch
):
    from api.managed_background_workers import ManagedBackgroundWorkerOutcome

    bp = workers
    release = threading.Event()
    rogue = threading.Thread(
        target=lambda: release.wait(5.0),
        name="hermes-webui-bg-task-complete-drain",
        daemon=True,
    )
    rogue.start()
    try:
        verification = bp.verify_managed_drain_worker()
        assert verification.outcome is ManagedBackgroundWorkerOutcome.AMBIGUOUS
        assert verification.reason == "unreferenced_live_worker"
    finally:
        release.set()
        rogue.join(timeout=1.0)

    managed_release = threading.Event()
    duplicate_release = threading.Event()
    monkeypatch.setattr(bp, "_drain_loop", _blocking_loop(managed_release))
    bp.start_managed_drain_worker(readiness_timeout=1.0)
    duplicate = threading.Thread(
        target=lambda: duplicate_release.wait(5.0),
        name="hermes-webui-bg-task-complete-drain",
        daemon=True,
    )
    duplicate.start()
    try:
        verification = bp.verify_managed_drain_worker()
        assert verification.outcome is ManagedBackgroundWorkerOutcome.AMBIGUOUS
        assert verification.reason == "duplicate_live_workers"
    finally:
        duplicate_release.set()
        duplicate.join(timeout=1.0)
        managed_release.set()


def test_malformed_published_worker_is_typed_ambiguous(workers, monkeypatch):
    from api.managed_background_workers import ManagedBackgroundWorkerOutcome

    bp = workers
    monkeypatch.setattr(bp, "_DRAIN_THREAD", object())
    verification = bp.verify_managed_drain_worker()
    assert verification.outcome is ManagedBackgroundWorkerOutcome.AMBIGUOUS
    assert verification.reason == "published_worker_is_invalid"


def test_managed_stop_is_bounded_and_reports_partial(workers, monkeypatch):
    from api.managed_background_workers import (
        ManagedBackgroundWorkerOutcome,
        ManagedBackgroundWorkerStopError,
    )

    bp = workers
    release = threading.Event()
    monkeypatch.setattr(bp, "_drain_loop", _blocking_loop(release))
    bp.start_managed_drain_worker(readiness_timeout=1.0)
    started = time.monotonic()
    with pytest.raises(ManagedBackgroundWorkerStopError):
        bp.stop_managed_drain_worker(timeout=0.02)
    assert time.monotonic() - started < 0.5
    assert (
        bp.verify_managed_drain_worker().outcome
        is ManagedBackgroundWorkerOutcome.PARTIAL
    )
    release.set()


def test_concurrent_managed_stop_of_same_generation_is_idempotent(
    workers, monkeypatch
):
    from api.managed_background_workers import ManagedBackgroundWorkerOutcome

    bp = workers
    release = threading.Event()
    monkeypatch.setattr(bp, "_drain_loop", _blocking_loop(release))
    bp.start_managed_drain_worker(readiness_timeout=1.0)
    worker = bp._DRAIN_THREAD
    assert worker is not None
    original_join = worker.join
    both_joining = threading.Barrier(2, action=release.set)

    def synchronized_join(timeout=None):
        both_joining.wait(timeout=1.0)
        return original_join(timeout=timeout)

    monkeypatch.setattr(worker, "join", synchronized_join)
    results = []
    errors = []

    def stop() -> None:
        try:
            results.append(bp.stop_managed_drain_worker(timeout=1.0))
        except Exception as exc:  # asserted below
            errors.append(exc)

    stoppers = [threading.Thread(target=stop) for _ in range(2)]
    for stopper in stoppers:
        stopper.start()
    for stopper in stoppers:
        stopper.join(timeout=2.0)

    assert all(not stopper.is_alive() for stopper in stoppers)
    assert errors == []
    assert len(results) == 2
    assert all(
        result.outcome is ManagedBackgroundWorkerOutcome.ABSENT
        for result in results
    )
    assert bp.verify_managed_drain_worker().outcome is (
        ManagedBackgroundWorkerOutcome.ABSENT
    )


def test_concurrent_stale_stop_cannot_clear_replacement_generation(
    workers, monkeypatch
):
    from api.managed_background_workers import (
        ManagedBackgroundWorkerOutcome,
        ManagedBackgroundWorkerStopError,
    )

    bp = workers
    old_release = threading.Event()
    new_release = threading.Event()
    loop_lock = threading.Lock()
    loop_generation = 0

    def generation_loop(readiness: threading.Event) -> None:
        nonlocal loop_generation
        with loop_lock:
            loop_generation += 1
            generation = loop_generation
        readiness.set()
        (old_release if generation == 1 else new_release).wait(5.0)

    monkeypatch.setattr(bp, "_drain_loop", generation_loop)
    old_start = bp.start_managed_drain_worker(readiness_timeout=1.0)
    old_worker = bp._DRAIN_THREAD
    assert old_worker is not None
    original_join = old_worker.join
    both_joining = threading.Barrier(2, action=old_release.set)
    hold_stale_stopper = threading.Event()
    stale_joined = threading.Event()

    def synchronized_join(timeout=None):
        both_joining.wait(timeout=1.0)
        result = original_join(timeout=timeout)
        if threading.current_thread().name == "stale-stopper":
            stale_joined.set()
            hold_stale_stopper.wait(2.0)
        return result

    monkeypatch.setattr(old_worker, "join", synchronized_join)
    first_results = []
    stale_errors = []

    first = threading.Thread(
        target=lambda: first_results.append(
            bp.stop_managed_drain_worker(timeout=1.0)
        ),
        name="first-stopper",
    )

    def stale_stop() -> None:
        try:
            bp.stop_managed_drain_worker(timeout=1.0)
        except Exception as exc:  # asserted below
            stale_errors.append(exc)

    stale = threading.Thread(target=stale_stop, name="stale-stopper")
    first.start()
    stale.start()
    assert stale_joined.wait(1.5)
    first.join(timeout=1.5)
    assert not first.is_alive()
    assert first_results[0].outcome is ManagedBackgroundWorkerOutcome.ABSENT

    replacement = bp.start_managed_drain_worker(readiness_timeout=1.0)
    try:
        assert replacement.receipt.generation > old_start.receipt.generation
        hold_stale_stopper.set()
        stale.join(timeout=1.5)

        assert not stale.is_alive()
        assert len(stale_errors) == 1
        assert isinstance(stale_errors[0], ManagedBackgroundWorkerStopError)
        assert "generation changed" in str(stale_errors[0])
        assert (
            bp.verify_managed_drain_worker(replacement.receipt).outcome
            is ManagedBackgroundWorkerOutcome.COMPLETE
        )
    finally:
        hold_stale_stopper.set()
        new_release.set()
        stale.join(timeout=1.0)


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\) may lead to deadlocks"
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_resets_worker_authority(workers, monkeypatch):
    from api.managed_background_workers import ManagedBackgroundWorkerOutcome

    bp = workers
    release = threading.Event()
    monkeypatch.setattr(bp, "_drain_loop", _blocking_loop(release))
    bp.start_managed_drain_worker(readiness_timeout=1.0)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are reported through the pipe
        try:
            os.close(read_fd)
            result = bp.verify_managed_drain_worker()
            payload = (
                f"{result.outcome.value}|"
                f"{bp._DRAIN_THREAD is None}|"
                f"{bp._DRAIN_STOP.is_set()}"
            )
            os.write(write_fd, payload.encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        payload = os.read(read_fd, 256).decode()
        _, status = os.waitpid(pid, 0)
        assert status == 0
        assert payload == (
            f"{ManagedBackgroundWorkerOutcome.ABSENT.value}|True|False"
        )
    finally:
        os.close(read_fd)
        release.set()
