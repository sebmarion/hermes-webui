"""Strict process-epoch reconciliation for the provider-model catalog."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from types import ModuleType
from typing import Callable

from api.process_identity import process_start_token


MAX_PROVIDER_COUNT = 256
MAX_MODEL_COUNT = 8192
MAX_PROVIDER_ID_BYTES = 256
MAX_MODEL_ID_BYTES = 2048
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 131_072
MAX_MODEL_FIELDS = 64


class ManagedStartupProviderModelsError(RuntimeError):
    """Strict provider-model reconciliation failed."""


class ManagedStartupProviderModelsAdmissionError(ManagedStartupProviderModelsError):
    """Provider-model mutation was attempted outside admitted startup."""


class ManagedStartupProviderModelsUnavailable(ManagedStartupProviderModelsError):
    """A stable, bounded provider-model source could not be captured."""


class ManagedStartupProviderModelsDesiredDriftError(ManagedStartupProviderModelsError):
    """The upstream desired catalog changed inside one process epoch."""


class ManagedStartupProviderModelsPostconditionError(ManagedStartupProviderModelsError):
    """The provider-model target was not atomically installed."""


class ManagedStartupProviderModelsVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ProcessEpoch:
    pid: int
    start_token: str


@dataclass(frozen=True, slots=True)
class ManagedStartupProviderModelsReceipt:
    process_epoch: ProcessEpoch
    desired_sha256: str
    upstream_sha256: str
    provider_count: int
    model_count: int


@dataclass(frozen=True, slots=True)
class ManagedStartupProviderModelsVerification:
    outcome: ManagedStartupProviderModelsVerificationOutcome
    receipt: ManagedStartupProviderModelsReceipt | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class _DesiredProviderModels:
    process_epoch: ProcessEpoch
    base_sha256: str
    upstream_sha256: str
    desired_sha256: str
    target_payload: bytes = field(repr=False)
    provider_count: int
    model_count: int


@dataclass(frozen=True, slots=True)
class _ManagedProviderModelsState:
    desired: _DesiredProviderModels
    receipt: ManagedStartupProviderModelsReceipt


_STATE_LOCK = threading.Lock()
_STATE: _ManagedProviderModelsState | None = None


@dataclass(slots=True)
class _JsonBudget:
    nodes: int = 0
    string_bytes: int = 0


def _current_process_epoch() -> ProcessEpoch | None:
    pid = os.getpid()
    token = process_start_token(pid)
    if not token:
        return None
    return ProcessEpoch(pid, token)


def _startup_mutations_are_admitted() -> bool:
    try:
        config = _load_config_module()
        admitted = config._startup_mutations_are_admitted
        return bool(admitted())
    except Exception:
        return False


def _load_config_module() -> ModuleType:
    try:
        from api import config
    except ImportError as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model WebUI catalog is unavailable"
        ) from exc
    return config


def _load_core_provider_models() -> object:
    try:
        from hermes_cli.models import _PROVIDER_MODELS
    except ImportError as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model upstream catalog is unavailable"
        ) from exc
    return _PROVIDER_MODELS


def _validate_text(value: object, *, label: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ManagedStartupProviderModelsUnavailable(
            f"{label} is invalid or exceeds its bound"
        )
    return value


def _validate_trimmed_text(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str:
    if type(value) is not str:
        raise ManagedStartupProviderModelsUnavailable(f"{label} is not text")
    trimmed = value.strip()
    return _validate_text(trimmed, label=label, maximum=maximum)


def _validate_json_value(
    value: object,
    *,
    budget: _JsonBudget,
    depth: int = 0,
) -> None:
    budget.nodes += 1
    if budget.nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog exceeds its structural bound"
        )
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model catalog contains a non-finite number"
            )
        return
    if type(value) is str:
        budget.string_bytes += len(value.encode("utf-8"))
        if budget.string_bytes > MAX_CATALOG_BYTES:
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model catalog exceeds its string-byte bound"
            )
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, budget=budget, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_MODEL_FIELDS:
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model object exceeds its field bound"
            )
        for key, item in value.items():
            if type(key) is not str:
                raise ManagedStartupProviderModelsUnavailable(
                    "provider-model object key is not text"
                )
            _validate_json_value(key, budget=budget, depth=depth + 1)
            _validate_json_value(item, budget=budget, depth=depth + 1)
        return
    raise ManagedStartupProviderModelsUnavailable(
        "provider-model catalog contains a non-JSON value"
    )


def _catalog_payload(
    value: object,
    *,
    upstream: bool,
) -> tuple[bytes, bytes, int, int]:
    if type(value) is not dict or len(value) > MAX_PROVIDER_COUNT:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog has an invalid provider bound"
        )
    normalized: dict[str, list[object]] = {}
    json_budget = _JsonBudget()
    model_count = 0
    for provider, models in value.items():
        provider_id = _validate_trimmed_text(
            provider,
            label="provider-model provider id",
            maximum=MAX_PROVIDER_ID_BYTES,
        )
        if provider_id != provider:
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model provider id contains surrounding whitespace"
            )
        if type(models) is not list:
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model provider entry is not a list"
            )
        model_count += len(models)
        if model_count > MAX_MODEL_COUNT:
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model catalog exceeds its model bound"
            )
        normalized_models: list[object] = []
        for model in models:
            if upstream:
                normalized_models.append(
                    _validate_trimmed_text(
                        model,
                        label="provider-model upstream model id",
                        maximum=MAX_MODEL_ID_BYTES,
                    )
                )
                continue
            if type(model) is not dict:
                raise ManagedStartupProviderModelsUnavailable(
                    "provider-model WebUI model entry is not an object"
                )
            model_id = model.get("id")
            _validate_text(
                model_id,
                label="provider-model WebUI model id",
                maximum=MAX_MODEL_ID_BYTES,
            )
            _validate_json_value(model, budget=json_budget)
            normalized_models.append(model)
        normalized[provider_id] = normalized_models
    try:
        payload = json.dumps(
            normalized,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog is not bounded canonical JSON"
        ) from exc
    if len(payload) > MAX_CATALOG_BYTES or len(canonical) > MAX_CATALOG_BYTES:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog exceeds its byte bound"
        )
    return payload, canonical, len(normalized), model_count


def _stable_capture(
    reader: Callable[[], object],
    *,
    upstream: bool,
) -> tuple[bytes, bytes, int, int]:
    try:
        first = _catalog_payload(reader(), upstream=upstream)
        second = _catalog_payload(reader(), upstream=upstream)
    except ManagedStartupProviderModelsUnavailable:
        raise
    except Exception as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog could not be captured"
        ) from exc
    if first != second:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model catalog changed during capture"
        )
    return first


def _capture_upstream() -> tuple[bytes, bytes, int, int]:
    return _stable_capture(_load_core_provider_models, upstream=True)


def _capture_current_catalog(
    config: ModuleType,
) -> tuple[bytes, bytes, int, int]:
    def read_snapshot():
        catalog = config._PROVIDER_MODELS
        snapshot = getattr(
            catalog,
            "_managed_provider_models_snapshot",
            None,
        )
        if not callable(snapshot):
            raise ManagedStartupProviderModelsUnavailable(
                "provider-model atomic catalog proxy is unavailable"
            )
        return snapshot()

    return _stable_capture(
        read_snapshot,
        upstream=False,
    )


def _build_target(
    config: ModuleType,
    base_payload: bytes,
    upstream_payload: bytes,
) -> tuple[bytes, bytes, int, int]:
    try:
        target = json.loads(base_payload)
        upstream = json.loads(upstream_payload)
        resolve_alias = config._resolve_provider_alias
        model_label = config._get_label_for_model
        webui_key_by_canonical: dict[str, str] = {}
        for webui_key in target:
            canonical = _validate_text(
                resolve_alias(webui_key),
                label="provider-model canonical provider id",
                maximum=MAX_PROVIDER_ID_BYTES,
            )
            webui_key_by_canonical.setdefault(canonical, webui_key)

        for provider_id in sorted(upstream):
            core_models = upstream[provider_id]
            webui_key = provider_id
            webui_list = target.get(provider_id)
            if webui_list is None:
                canonical = _validate_text(
                    resolve_alias(provider_id),
                    label="provider-model canonical provider id",
                    maximum=MAX_PROVIDER_ID_BYTES,
                )
                webui_key = webui_key_by_canonical.get(canonical, provider_id)
                webui_list = target.get(webui_key)
            if webui_list is None:
                continue
            existing_ids_raw = [
                model["id"]
                for model in webui_list
                if type(model) is dict and type(model.get("id")) is str and model["id"]
            ]
            prefix = ""
            if existing_ids_raw and all(
                item.startswith("@") and ":" in item for item in existing_ids_raw
            ):
                prefix = existing_ids_raw[0].split(":", 1)[0] + ":"

            def normalized_id(model_id: str, _prefix: str = prefix) -> str:
                if _prefix and model_id.startswith(_prefix):
                    model_id = model_id[len(_prefix) :]
                return model_id.replace("-", ".").lower()

            existing_ids = {normalized_id(item) for item in existing_ids_raw}
            for model_id in core_models:
                normalized = normalized_id(model_id)
                if normalized in existing_ids:
                    continue
                inject_id = prefix + model_id if prefix else model_id
                label = _validate_text(
                    model_label(model_id, []),
                    label="provider-model generated label",
                    maximum=MAX_MODEL_ID_BYTES,
                )
                webui_list.append({"id": inject_id, "label": label})
                existing_ids.add(normalized)
    except ManagedStartupProviderModelsUnavailable:
        raise
    except Exception as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model desired target could not be derived"
        ) from exc
    return _catalog_payload(target, upstream=False)


def _capture_desired(
    config: ModuleType,
    epoch: ProcessEpoch,
) -> _DesiredProviderModels:
    base_payload, base_canonical, _base_providers, _base_models = (
        _capture_current_catalog(config)
    )
    (
        upstream_payload,
        upstream_canonical,
        _upstream_providers,
        _upstream_models,
    ) = _capture_upstream()
    target_payload, target_canonical, provider_count, model_count = _build_target(
        config,
        base_payload,
        upstream_payload,
    )
    return _DesiredProviderModels(
        process_epoch=epoch,
        base_sha256=hashlib.sha256(base_canonical).hexdigest(),
        upstream_sha256=hashlib.sha256(upstream_canonical).hexdigest(),
        desired_sha256=hashlib.sha256(target_canonical).hexdigest(),
        target_payload=target_payload,
        provider_count=provider_count,
        model_count=model_count,
    )


def _receipt_for_desired(
    desired: _DesiredProviderModels,
) -> ManagedStartupProviderModelsReceipt:
    return ManagedStartupProviderModelsReceipt(
        process_epoch=desired.process_epoch,
        desired_sha256=desired.desired_sha256,
        upstream_sha256=desired.upstream_sha256,
        provider_count=desired.provider_count,
        model_count=desired.model_count,
    )


def _current_upstream_sha256() -> str:
    _payload, canonical, _providers, _models = _capture_upstream()
    return hashlib.sha256(canonical).hexdigest()


def _upstream_matches(desired: _DesiredProviderModels) -> bool:
    return _current_upstream_sha256() == desired.upstream_sha256


def _current_catalog_sha256(config: ModuleType) -> str:
    _payload, canonical, _providers, _models = _capture_current_catalog(config)
    return hashlib.sha256(canonical).hexdigest()


def _publish_catalog(config: ModuleType, target_payload: bytes) -> None:
    try:
        target = json.loads(target_payload)
        catalog = config._PROVIDER_MODELS
        replace_snapshot = getattr(
            catalog,
            "_replace_managed_provider_models_snapshot",
            None,
        )
        if not callable(replace_snapshot):
            raise ManagedStartupProviderModelsPostconditionError(
                "provider-model atomic catalog proxy is unavailable"
            )
        replace_snapshot(target)
        if config._PROVIDER_MODELS is not catalog:
            raise ManagedStartupProviderModelsPostconditionError(
                "provider-model catalog proxy identity changed"
            )
    except ManagedStartupProviderModelsPostconditionError:
        raise
    except Exception as exc:
        raise ManagedStartupProviderModelsPostconditionError(
            "provider-model catalog could not be atomically published"
        ) from exc


def _verification_for_state(
    config: ModuleType,
    state: _ManagedProviderModelsState,
) -> ManagedStartupProviderModelsVerification:
    try:
        current_sha256 = _current_catalog_sha256(config)
    except ManagedStartupProviderModelsUnavailable:
        return ManagedStartupProviderModelsVerification(
            ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
            state.receipt,
            "provider_model_catalog_unobservable",
        )
    if current_sha256 == state.desired.desired_sha256:
        return ManagedStartupProviderModelsVerification(
            ManagedStartupProviderModelsVerificationOutcome.PROVED_COMPLETE,
            state.receipt,
            None,
        )
    return ManagedStartupProviderModelsVerification(
        ManagedStartupProviderModelsVerificationOutcome.PARTIAL,
        state.receipt,
        "provider_model_catalog_mismatch",
    )


def reconcile_managed_startup_provider_models() -> ManagedStartupProviderModelsReceipt:
    """Install or repair one stable provider-model target for this process."""

    global _STATE
    if not _startup_mutations_are_admitted():
        raise ManagedStartupProviderModelsAdmissionError(
            "provider-model reconciliation requires admitted startup"
        )
    epoch = _current_process_epoch()
    if epoch is None:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model process epoch is unavailable"
        )
    try:
        config = _load_config_module()
    except (ImportError, ManagedStartupProviderModelsUnavailable) as exc:
        raise ManagedStartupProviderModelsUnavailable(
            "provider-model WebUI catalog is unavailable"
        ) from exc
    with _STATE_LOCK:
        if _STATE is not None and _STATE.desired.process_epoch != epoch:
            _STATE = None
        if _STATE is None:
            desired = _capture_desired(config, epoch)
            _STATE = _ManagedProviderModelsState(
                desired=desired,
                receipt=_receipt_for_desired(desired),
            )
        elif not _upstream_matches(_STATE.desired):
            raise ManagedStartupProviderModelsDesiredDriftError(
                "provider-model upstream changed inside the process epoch"
            )
        current = _verification_for_state(config, _STATE)
        if (
            current.outcome
            is ManagedStartupProviderModelsVerificationOutcome.PROVED_COMPLETE
        ):
            if not _upstream_matches(_STATE.desired):
                raise ManagedStartupProviderModelsDesiredDriftError(
                    "provider-model upstream changed during verification"
                )
            return _STATE.receipt
        if not _startup_mutations_are_admitted():
            raise ManagedStartupProviderModelsAdmissionError(
                "provider-model admission closed before publication"
            )
        try:
            _publish_catalog(config, _STATE.desired.target_payload)
        except ManagedStartupProviderModelsPostconditionError:
            raise
        except Exception as exc:
            raise ManagedStartupProviderModelsPostconditionError(
                "provider-model catalog publication failed"
            ) from exc
        verified = _verification_for_state(config, _STATE)
        if (
            verified.outcome
            is not ManagedStartupProviderModelsVerificationOutcome.PROVED_COMPLETE
        ):
            raise ManagedStartupProviderModelsPostconditionError(
                verified.reason or "provider-model postcondition is incomplete"
            )
        if not _upstream_matches(_STATE.desired):
            raise ManagedStartupProviderModelsDesiredDriftError(
                "provider-model upstream changed during publication"
            )
        return _STATE.receipt


def verify_managed_startup_provider_models(
    receipt: ManagedStartupProviderModelsReceipt | None = None,
) -> ManagedStartupProviderModelsVerification:
    """Verify the exact current-process provider-model target without mutation."""

    epoch = _current_process_epoch()
    if epoch is None:
        return ManagedStartupProviderModelsVerification(
            ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
            None,
            "process_epoch_unavailable",
        )
    try:
        config = _load_config_module()
        upstream_sha256 = _current_upstream_sha256()
    except Exception:
        return ManagedStartupProviderModelsVerification(
            ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
            None,
            "provider_model_source_unavailable",
        )
    with _STATE_LOCK:
        state = _STATE
        if state is None:
            if receipt is not None:
                return ManagedStartupProviderModelsVerification(
                    ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
                    None,
                    "managed_provider_models_receipt_without_epoch_state",
                )
            return ManagedStartupProviderModelsVerification(
                ManagedStartupProviderModelsVerificationOutcome.PROVED_ABSENT,
                None,
                "managed_provider_models_not_installed",
            )
        if state.desired.process_epoch != epoch:
            return ManagedStartupProviderModelsVerification(
                ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
                state.receipt,
                "managed_provider_models_from_foreign_epoch",
            )
        if receipt is not None and receipt != state.receipt:
            return ManagedStartupProviderModelsVerification(
                ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
                state.receipt,
                "managed_provider_models_receipt_mismatch",
            )
        if upstream_sha256 != state.desired.upstream_sha256:
            return ManagedStartupProviderModelsVerification(
                ManagedStartupProviderModelsVerificationOutcome.AMBIGUOUS,
                state.receipt,
                "provider_model_upstream_drift",
            )
        return _verification_for_state(config, state)


def _reset_after_fork() -> None:
    global _STATE_LOCK, _STATE
    _STATE_LOCK = threading.Lock()
    _STATE = None


def _reset_managed_startup_provider_models_for_tests() -> None:
    global _STATE
    with _STATE_LOCK:
        _STATE = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
