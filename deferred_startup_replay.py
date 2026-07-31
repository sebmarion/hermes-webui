"""Pure transaction-bound replay for managed deferred startup steps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

import deferred_release_manifest


_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_PROCESS_EPOCH_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
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
    PROVED_RETRY_SAFE_PARTIAL = "proved-retry-safe-partial"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


class PriorCompletionAbsentPolicy(str, Enum):
    """Whether a new process epoch may recreate a vanished completed effect."""

    DENY = "deny"
    ALLOW_RERUN = "allow-rerun"


class RetrySafePartialPolicy(str, Enum):
    """Whether an explicitly retry-safe partial effect may be mutated again."""

    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True, slots=True)
class DeferredStartupManifestReceipt:
    transaction_id: str
    version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeferredStartupStepState:
    attempt_number: int = 0
    intent: bool = False
    completion: bool = False
    indeterminate: bool = False
    prior_completion: bool = False
    prior_indeterminate: bool = False
    prior_unresolved: bool = False


@dataclass(frozen=True, slots=True)
class DeferredStartupStep:
    name: str
    mutator: Callable[[], object]
    reconciler: Callable[[], Reconciliation]
    prior_completion_absent_policy: PriorCompletionAbsentPolicy = (
        PriorCompletionAbsentPolicy.DENY
    )
    retry_safe_partial_policy: RetrySafePartialPolicy = RetrySafePartialPolicy.DENY


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
        process_epoch: str,
        step_name: str,
        *,
        prior_completion_absent_policy: PriorCompletionAbsentPolicy,
        retry_safe_partial_policy: RetrySafePartialPolicy,
    ) -> DeferredStartupStepState: ...

    def record_intent(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        prior_completion_absent_policy: PriorCompletionAbsentPolicy,
        retry_safe_partial_policy: RetrySafePartialPolicy,
    ) -> None: ...

    def record_completion(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        recovered: bool,
    ) -> None: ...

    def record_indeterminate(
        self,
        transaction_id: str,
        manifest_receipt: DeferredStartupManifestReceipt,
        process_epoch: str,
        step_name: str,
        *,
        reason: str,
    ) -> None: ...


CrashHook = Callable[[str, str], None]


def _validate_binding(
    transaction_id: object,
    manifest_receipt: object,
    process_epoch: object,
    steps: object,
) -> tuple[
    str,
    DeferredStartupManifestReceipt,
    str,
    tuple[DeferredStartupStep, ...],
]:
    if (
        type(transaction_id) is not str
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
    ):
        raise DeferredStartupBindingError("startup transaction id is invalid")
    if (
        type(process_epoch) is not str
        or _PROCESS_EPOCH_RE.fullmatch(process_epoch) is None
    ):
        raise DeferredStartupBindingError("startup process epoch is invalid")
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
            or type(step.prior_completion_absent_policy)
            is not PriorCompletionAbsentPolicy
            or type(step.retry_safe_partial_policy) is not RetrySafePartialPolicy
            or step.name in names
        ):
            raise DeferredStartupBindingError("startup step definitions are invalid")
        names.add(step.name)
    return transaction_id, manifest_receipt, process_epoch, steps


def _validate_state(value: object) -> DeferredStartupStepState:
    if type(value) is not DeferredStartupStepState:
        raise DeferredStartupBindingError("durable startup step state is invalid")
    if not all(
        type(flag) is bool
        for flag in (
            value.intent,
            value.completion,
            value.indeterminate,
            value.prior_completion,
            value.prior_indeterminate,
            value.prior_unresolved,
        )
    ):
        raise DeferredStartupBindingError("durable startup step state is invalid")
    if (
        type(value.attempt_number) is not int
        or isinstance(value.attempt_number, bool)
        or value.attempt_number < 0
        or (value.intent and value.attempt_number < 1)
        or (not value.intent and value.attempt_number != 0)
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
    process_epoch: str,
    driver: DeferredStartupDurableDriver,
) -> Reconciliation:
    result, reason, cause = _probe_reconciliation(step)
    if result is None:
        _mark_indeterminate(
            transaction_id=transaction_id,
            manifest_receipt=manifest_receipt,
            process_epoch=process_epoch,
            driver=driver,
            step_name=step.name,
            reason=reason,
            cause=cause,
        )
    return result


def _probe_reconciliation(
    step: DeferredStartupStep,
) -> tuple[Reconciliation | None, str, Exception | None]:
    """Probe without writing, for a new epoch that has no intent yet."""

    try:
        result = step.reconciler()
    except Exception as exc:
        return None, "reconciler-failed", exc
    if type(result) is not Reconciliation:
        return None, "invalid-reconciliation", None
    return result, result.value, None


def _mark_indeterminate(
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    process_epoch: str,
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
            process_epoch,
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
    process_epoch: str,
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
        process_epoch=process_epoch,
        driver=driver,
    )
    if result is Reconciliation.PROVED_COMPLETE:
        driver.record_completion(
            transaction_id,
            manifest_receipt,
            process_epoch,
            step.name,
            recovered=False,
        )
        return
    if result is Reconciliation.PROVED_ABSENT:
        raise DeferredStartupRetryableError(
            f"deferred startup step remains absent: {step.name}"
        )
    if result is Reconciliation.PROVED_RETRY_SAFE_PARTIAL:
        raise DeferredStartupRetryableError(
            f"deferred startup step remains retry-safe partial: {step.name}"
        )
    _mark_indeterminate(
        transaction_id=transaction_id,
        manifest_receipt=manifest_receipt,
        process_epoch=process_epoch,
        driver=driver,
        step_name=step.name,
        reason=result.value,
    )


def replay_deferred_startup(
    *,
    transaction_id: str,
    manifest_receipt: DeferredStartupManifestReceipt,
    process_epoch: str,
    steps: tuple[DeferredStartupStep, ...],
    driver: DeferredStartupDurableDriver,
    crash_hook: CrashHook | None = None,
) -> DeferredStartupReplayResult:
    """Reconcile and execute ordered startup steps behind durable intent."""
    transaction_id, manifest_receipt, process_epoch, steps = _validate_binding(
        transaction_id,
        manifest_receipt,
        process_epoch,
        steps,
    )
    completed: list[str] = []
    for step in steps:
        state = _validate_state(
            driver.read_step_state(
                transaction_id,
                manifest_receipt,
                process_epoch,
                step.name,
                prior_completion_absent_policy=(step.prior_completion_absent_policy),
                retry_safe_partial_policy=step.retry_safe_partial_policy,
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
                process_epoch=process_epoch,
                driver=driver,
            )
            if result is not Reconciliation.PROVED_COMPLETE:
                raise DeferredStartupIndeterminateError(
                    f"deferred startup step is indeterminate: {step.name}"
                )
        elif state.intent:
            result = _reconcile(
                step,
                transaction_id=transaction_id,
                manifest_receipt=manifest_receipt,
                process_epoch=process_epoch,
                driver=driver,
            )
            if result is Reconciliation.PROVED_COMPLETE:
                driver.record_completion(
                    transaction_id,
                    manifest_receipt,
                    process_epoch,
                    step.name,
                    recovered=True,
                )
            elif result is Reconciliation.PROVED_ABSENT:
                if (
                    (state.prior_completion or state.prior_unresolved)
                    and step.prior_completion_absent_policy
                    is not PriorCompletionAbsentPolicy.ALLOW_RERUN
                ):
                    denied_reason = (
                        "prior-completion-absent-policy-denied"
                        if state.prior_completion
                        else "prior-intent-absent-policy-denied"
                    )
                    _mark_indeterminate(
                        transaction_id=transaction_id,
                        manifest_receipt=manifest_receipt,
                        process_epoch=process_epoch,
                        driver=driver,
                        step_name=step.name,
                        reason=denied_reason,
                    )
                _mutate_and_prove(
                    step,
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    crash_hook=crash_hook,
                )
            elif result is Reconciliation.PROVED_RETRY_SAFE_PARTIAL:
                if step.retry_safe_partial_policy is not RetrySafePartialPolicy.ALLOW:
                    _mark_indeterminate(
                        transaction_id=transaction_id,
                        manifest_receipt=manifest_receipt,
                        process_epoch=process_epoch,
                        driver=driver,
                        step_name=step.name,
                        reason="retry-safe-partial-policy-denied",
                    )
                _mutate_and_prove(
                    step,
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    crash_hook=crash_hook,
                )
            else:
                _mark_indeterminate(
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    step_name=step.name,
                    reason=result.value,
                )
        elif state.prior_indeterminate:
            raise DeferredStartupIndeterminateError(
                f"deferred startup step is indeterminate: {step.name}"
            )
        elif state.prior_completion or state.prior_unresolved:
            result, failure_reason, failure_cause = _probe_reconciliation(step)
            driver.record_intent(
                transaction_id,
                manifest_receipt,
                process_epoch,
                step.name,
                prior_completion_absent_policy=(step.prior_completion_absent_policy),
                retry_safe_partial_policy=step.retry_safe_partial_policy,
            )
            if crash_hook is not None:
                crash_hook(AFTER_INTENT, step.name)
            if result is None:
                _mark_indeterminate(
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    step_name=step.name,
                    reason=failure_reason,
                    cause=failure_cause,
                )
            if result is Reconciliation.PROVED_COMPLETE:
                driver.record_completion(
                    transaction_id,
                    manifest_receipt,
                    process_epoch,
                    step.name,
                    recovered=True,
                )
            elif result is Reconciliation.PROVED_ABSENT:
                if (
                    (state.prior_completion or state.prior_unresolved)
                    and step.prior_completion_absent_policy
                    is not PriorCompletionAbsentPolicy.ALLOW_RERUN
                ):
                    denied_reason = (
                        "prior-completion-absent-policy-denied"
                        if state.prior_completion
                        else "prior-intent-absent-policy-denied"
                    )
                    _mark_indeterminate(
                        transaction_id=transaction_id,
                        manifest_receipt=manifest_receipt,
                        process_epoch=process_epoch,
                        driver=driver,
                        step_name=step.name,
                        reason=denied_reason,
                    )
                _mutate_and_prove(
                    step,
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    crash_hook=crash_hook,
                )
            elif result is Reconciliation.PROVED_RETRY_SAFE_PARTIAL:
                if step.retry_safe_partial_policy is not RetrySafePartialPolicy.ALLOW:
                    _mark_indeterminate(
                        transaction_id=transaction_id,
                        manifest_receipt=manifest_receipt,
                        process_epoch=process_epoch,
                        driver=driver,
                        step_name=step.name,
                        reason="retry-safe-partial-policy-denied",
                    )
                _mutate_and_prove(
                    step,
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    crash_hook=crash_hook,
                )
            else:
                _mark_indeterminate(
                    transaction_id=transaction_id,
                    manifest_receipt=manifest_receipt,
                    process_epoch=process_epoch,
                    driver=driver,
                    step_name=step.name,
                    reason=result.value,
                )
        else:
            driver.record_intent(
                transaction_id,
                manifest_receipt,
                process_epoch,
                step.name,
                prior_completion_absent_policy=(step.prior_completion_absent_policy),
                retry_safe_partial_policy=step.retry_safe_partial_policy,
            )
            if crash_hook is not None:
                crash_hook(AFTER_INTENT, step.name)
            _mutate_and_prove(
                step,
                transaction_id=transaction_id,
                manifest_receipt=manifest_receipt,
                process_epoch=process_epoch,
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
