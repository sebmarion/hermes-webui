"""Pure transaction-bound replay for managed deferred startup steps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

import deferred_release_manifest


_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_STEP_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

AFTER_INTENT = "after-intent"
AFTER_MUTATION_BEFORE_COMPLETION = "after-mutation-before-completion"
AFTER_COMPLETION_BEFORE_NEXT = "after-completion-before-next"


class DeferredStartupReplayError(RuntimeError):
    """Base error for managed deferred startup replay."""


class DeferredStartupBindingError(DeferredStartupReplayError):
    """The transaction, manifest receipt, or step contract is invalid."""


class DeferredStartupRetryableError(DeferredStartupReplayError):
    """The durable intent remains safe to reconcile on a later attempt."""


class DeferredStartupIndeterminateError(DeferredStartupReplayError):
    """A step cannot be proved safe to accept or retry."""


class DeferredStartupCrash(BaseException):
    """Synthetic crash used only by deterministic crash-boundary tests."""


class Reconciliation(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class DeferredStartupManifestReceipt:
    transaction_id: str
    version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeferredStartupStepState:
    intent: bool = False
    completion: bool = False
    indeterminate: bool = False


@dataclass(frozen=True, slots=True)
class DeferredStartupStep:
    name: str
    mutator: Callable[[], object]
    reconciler: Callable[[], Reconciliation]


@dataclass(frozen=True, slots=True)
class DeferredStartupReplayResult:
    transaction_id: str
    manifest_receipt: DeferredStartupManifestReceipt
    completed: tuple[str, ...]


class DeferredStartupDurableDriver(Protocol):
    def read_step_state(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        step_name: str,
    ) -> DeferredStartupStepState: ...

    def record_intent(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        step_name: str,
    ) -> None: ...

    def record_completion(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        step_name: str,
        *,
        recovered: bool,
    ) -> None: ...

    def record_indeterminate(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        step_name: str,
        *,
        reason: str,
    ) -> None: ...


CrashHook = Callable[[str, str], None]


def _validate_binding(
    transaction_id: object,
    manifest_receipt: object,
    steps: object,
) -> tuple[str, DeferredStartupManifestReceipt, tuple[DeferredStartupStep, ...]]:
    if (
        type(transaction_id) is not str
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
    ):
        raise DeferredStartupBindingError("startup transaction id is invalid")
    if type(manifest_receipt) is not DeferredStartupManifestReceipt:
        raise DeferredStartupBindingError("startup manifest receipt is invalid")
    if (
        type(manifest_receipt.transaction_id) is not str
        or manifest_receipt.transaction_id != transaction_id
        or type(manifest_receipt.version) is not int
        or manifest_receipt.version != deferred_release_manifest.MANIFEST_VERSION
        or type(manifest_receipt.sha256) is not str
        or _SHA256_RE.fullmatch(manifest_receipt.sha256) is None
        or manifest_receipt.sha256
        != deferred_release_manifest.deferred_release_manifest_sha256()
    ):
        raise DeferredStartupBindingError(
            "startup manifest receipt does not match the canonical manifest"
        )
    if type(steps) is not tuple:
        raise DeferredStartupBindingError("startup step definitions are invalid")
    names: set[str] = set()
    for step in steps:
        if (
            type(step) is not DeferredStartupStep
            or type(step.name) is not str
            or _STEP_NAME_RE.fullmatch(step.name) is None
            or not callable(step.mutator)
            or not callable(step.reconciler)
            or step.name in names
        ):
            raise DeferredStartupBindingError("startup step definitions are invalid")
        names.add(step.name)
    return transaction_id, manifest_receipt, steps


def _validate_state(value: object) -> DeferredStartupStepState:
    if type(value) is not DeferredStartupStepState:
        raise DeferredStartupBindingError("durable startup step state is invalid")
    if not all(
        type(flag) is bool
        for flag in (value.intent, value.completion, value.indeterminate)
    ):
        raise DeferredStartupBindingError("durable startup step state is invalid")
    if (value.completion or value.indeterminate) and not value.intent:
        raise DeferredStartupBindingError("durable startup step state is invalid")
    if value.completion and value.indeterminate:
        raise DeferredStartupBindingError("durable startup step state is invalid")
    return value


def _reconcile(
    step: DeferredStartupStep,
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    driver: DeferredStartupDurableDriver,
) -> Reconciliation:
    try:
        result = step.reconciler()
    except Exception as exc:
        _mark_indeterminate(
            transaction_id=transaction_id,
            manifest_receipt=manifest_receipt,
            driver=driver,
            step_name=step.name,
            reason="reconciler-failed",
            cause=exc,
        )
    if type(result) is not Reconciliation:
        _mark_indeterminate(
            transaction_id=transaction_id,
            manifest_receipt=manifest_receipt,
            driver=driver,
            step_name=step.name,
            reason="invalid-reconciliation",
        )
    return result


def _mark_indeterminate(
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    driver: DeferredStartupDurableDriver,
    step_name: str,
    reason: str,
    cause: Exception | None = None,
) -> None:
    terminal_error = DeferredStartupIndeterminateError(
        f"deferred startup step is indeterminate: {step_name}"
    )
    try:
        driver.record_indeterminate(
            transaction_id,
            manifest_receipt,
            step_name,
            reason=reason,
        )
    except Exception as write_error:
        raise terminal_error from write_error
    if cause is not None:
        raise terminal_error from cause
    raise terminal_error


def _mutate_and_prove(
    step: DeferredStartupStep,
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    driver: DeferredStartupDurableDriver,
    crash_hook: CrashHook | None,
) -> None:
    try:
        step.mutator()
    except Exception as exc:
        raise DeferredStartupRetryableError(
            f"deferred startup step mutation failed: {step.name}"
        ) from exc
    if crash_hook is not None:
        crash_hook(AFTER_MUTATION_BEFORE_COMPLETION, step.name)
    result = _reconcile(
        step,
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
        driver=driver,
    )
    if result is Reconciliation.PROVED_COMPLETE:
        driver.record_completion(
            transaction_id,
            manifest_receipt,
            step.name,
            recovered=False,
        )
        return
    if result is Reconciliation.PROVED_ABSENT:
        raise DeferredStartupRetryableError(
            f"deferred startup step remains absent: {step.name}"
        )
    _mark_indeterminate(
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
        driver=driver,
        step_name=step.name,
        reason=result.value,
    )


def replay_deferred_startup(
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    steps: tuple[DeferredStartupStep, ...],
    driver: DeferredStartupDurableDriver,
    crash_hook: CrashHook | None = None,
) -> DeferredStartupReplayResult:
    """Reconcile and execute ordered startup steps behind durable intent."""
    transaction_id, manifest_receipt, steps = _validate_binding(
        transaction_id,
        manifest_receipt,
        steps,
    )
    completed: list[str] = []
    for step in steps:
        state = _validate_state(
            driver.read_step_state(
                transaction_id,
                manifest_receipt,
                step.name,
            )
        )
        if state.indeterminate:
            raise DeferredStartupIndeterminateError(
                f"deferred startup step is indeterminate: {step.name}"
            )
        if state.completion:
            result = _reconcile(
                step,
                transaction_id=transaction_id,
                manifest_receipt=manifest_receipt,
                driver=driver,
            )
            if result is not Reconciliation.PROVED_COMPLETE:
                _mark_indeterminate(
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    driver=driver,
                    step_name=step.name,
                    reason=f"completed-{result.value}",
                )
        elif state.intent:
            result = _reconcile(
                step,
                transaction_id=transaction_id,
                manifest_receipt=manifest_receipt,
                driver=driver,
            )
            if result is Reconciliation.PROVED_COMPLETE:
                driver.record_completion(
                    transaction_id,
                    manifest_receipt,
                    step.name,
                    recovered=True,
                )
            elif result is Reconciliation.PROVED_ABSENT:
                _mutate_and_prove(
                    step,
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    driver=driver,
                    crash_hook=crash_hook,
                )
            else:
                _mark_indeterminate(
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    driver=driver,
                    step_name=step.name,
                    reason=result.value,
                )
        else:
            driver.record_intent(
                transaction_id,
                manifest_receipt,
                step.name,
            )
            if crash_hook is not None:
                crash_hook(AFTER_INTENT, step.name)
            _mutate_and_prove(
                step,
                transaction_id=transaction_id,
                manifest_receipt=manifest_receipt,
                driver=driver,
                crash_hook=crash_hook,
            )
        completed.append(step.name)
        if crash_hook is not None:
            crash_hook(AFTER_COMPLETION_BEFORE_NEXT, step.name)
    return DeferredStartupReplayResult(
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
        completed=tuple(completed),
    )
