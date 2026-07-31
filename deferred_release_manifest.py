"""Canonical ordering contract for deferred paired-release operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass


MANIFEST_VERSION = 1
ALWAYS = "always"
STARTUP_RUN_ADMISSION_CLOSED = "startup_run_admission_closed"


@dataclass(frozen=True, slots=True)
class DeferredReleaseDescriptor:
    name: str
    owner: str
    operation: str
    condition: str


@dataclass(frozen=True, slots=True)
class DeferredReleaseManifest:
    version: int
    descriptors: tuple[DeferredReleaseDescriptor, ...]


_MANIFEST = DeferredReleaseManifest(
    version=MANIFEST_VERSION,
    descriptors=(
        DeferredReleaseDescriptor(
            "candidate_accept_intent",
            "release_controller",
            "record_phase:candidate_accept_intent",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "gateway_opening",
            "release_controller",
            "open_pair_after_promotion",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "credential_permissions",
            "webui_server",
            "api.startup.fix_credential_permissions",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "internal_recovery_key",
            "webui_server",
            "server._materialize_internal_recovery_key",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "state_directories",
            "webui_server",
            "server._create_state_directories",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "startup_profile_state",
            "webui_server",
            "api.config.apply_startup_profile_state",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "provider_model_seed",
            "webui_server",
            "api.config.seed_startup_provider_models",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "startup_configuration",
            "webui_server",
            "api.config.apply_deferred_startup_configuration",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "session_recovery",
            "webui_server",
            "server._recover_startup_sessions",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "plugins",
            "webui_server",
            "server._load_startup_plugins",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "process_completion_recovery",
            "webui_server",
            "server._recover_process_completion_notifications",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "async_delegation_recovery",
            "webui_server",
            "server._recover_async_delegation_notifications",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "tool_limit_continuation_recovery",
            "webui_server",
            "server._recover_tool_limit_continuations_for_startup",
            STARTUP_RUN_ADMISSION_CLOSED,
        ),
        DeferredReleaseDescriptor(
            "goal_continuation_recovery",
            "webui_server",
            "server._recover_goal_continuations_for_startup",
            STARTUP_RUN_ADMISSION_CLOSED,
        ),
        DeferredReleaseDescriptor(
            "background_services",
            "webui_server",
            "server._start_startup_background_services",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "full_open_health",
            "release_controller",
            "inspect_accepted_binding:require_full_health",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "pair_gate_release",
            "release_controller",
            "release_pair_after_acceptance",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "pair_opened",
            "release_controller",
            "record_phase:pair_opened",
            ALWAYS,
        ),
        DeferredReleaseDescriptor(
            "watchdog_restoration",
            "release_controller",
            "_restore_watchdog_cron",
            ALWAYS,
        ),
    ),
)


def deferred_release_manifest() -> DeferredReleaseManifest:
    """Return the immutable canonical manifest."""
    return _MANIFEST


def _descriptor_from_mapping(value: object) -> DeferredReleaseDescriptor:
    if not isinstance(value, Mapping) or set(value) != {
        "name",
        "owner",
        "operation",
        "condition",
    }:
        raise ValueError("deferred release manifest descriptor shape changed")
    fields = tuple(value[key] for key in ("name", "owner", "operation", "condition"))
    if not all(type(field) is str and field for field in fields):
        raise ValueError("deferred release manifest descriptor value changed")
    return DeferredReleaseDescriptor(*fields)


def _coerce_manifest(value: object) -> DeferredReleaseManifest:
    if isinstance(value, DeferredReleaseManifest):
        if (
            type(value) is not DeferredReleaseManifest
            or type(value.version) is not int
            or type(value.descriptors) is not tuple
        ):
            raise ValueError("deferred release manifest typed envelope changed")
        descriptors = []
        for descriptor in value.descriptors:
            if type(descriptor) is not DeferredReleaseDescriptor:
                raise ValueError("deferred release manifest typed descriptor changed")
            fields = (
                descriptor.name,
                descriptor.owner,
                descriptor.operation,
                descriptor.condition,
            )
            if not all(type(field) is str and field for field in fields):
                raise ValueError(
                    "deferred release manifest typed descriptor value changed"
                )
            descriptors.append(DeferredReleaseDescriptor(*fields))
        return DeferredReleaseManifest(value.version, tuple(descriptors))
    if not isinstance(value, Mapping) or set(value) != {"version", "descriptors"}:
        raise ValueError("deferred release manifest envelope changed")
    version = value["version"]
    descriptors = value["descriptors"]
    if type(version) is not int or not isinstance(descriptors, (list, tuple)):
        raise ValueError("deferred release manifest envelope value changed")
    return DeferredReleaseManifest(
        version=version,
        descriptors=tuple(_descriptor_from_mapping(item) for item in descriptors),
    )


def validate_deferred_release_manifest(
    value: object,
) -> DeferredReleaseManifest:
    """Reject any version, descriptor, field, order, or ownership drift."""
    candidate = _coerce_manifest(value)
    if candidate != _MANIFEST:
        raise ValueError("deferred release manifest does not match the canonical contract")
    return _MANIFEST


def _manifest_payload(manifest: DeferredReleaseManifest) -> dict:
    return {
        "version": manifest.version,
        "descriptors": [
            {
                "name": descriptor.name,
                "owner": descriptor.owner,
                "operation": descriptor.operation,
                "condition": descriptor.condition,
            }
            for descriptor in manifest.descriptors
        ],
    }


def canonical_manifest_bytes(value: object | None = None) -> bytes:
    """Return the validated manifest as deterministic canonical JSON bytes."""
    manifest = validate_deferred_release_manifest(
        _MANIFEST if value is None else value
    )
    return json.dumps(
        _manifest_payload(manifest),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deferred_release_manifest_sha256(value: object | None = None) -> str:
    return hashlib.sha256(canonical_manifest_bytes(value)).hexdigest()


def webui_startup_descriptors(
    value: object,
    *,
    startup_admission_closed: bool,
) -> tuple[DeferredReleaseDescriptor, ...]:
    """Select the server-owned subset while preserving canonical order."""
    if type(startup_admission_closed) is not bool:
        raise ValueError("startup_admission_closed must be a bool")
    manifest = validate_deferred_release_manifest(value)
    enabled_conditions = {ALWAYS}
    if startup_admission_closed:
        enabled_conditions.add(STARTUP_RUN_ADMISSION_CLOSED)
    return tuple(
        descriptor
        for descriptor in manifest.descriptors
        if descriptor.owner == "webui_server"
        and descriptor.condition in enabled_conditions
    )
