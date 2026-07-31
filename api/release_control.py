"""Loopback-HMAC release fence for exact-process WebUI cutovers."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from dataclasses import fields, is_dataclass
from enum import Enum

from api.auth import _is_loopback, _signing_key
from api.build_identity import get_build_identity
from api import config
from api.process_identity import process_start_token
import deferred_release_manifest
from deferred_startup_file_driver import DeferredStartupFileAttestation
from managed_startup_coordinator import ManagedStartupReceiptBundle


_AUTH_WINDOW_SECONDS = 60
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SEEN_NONCES: dict[str, float] = {}
_SEEN_NONCES_LOCK = threading.Lock()
_STARTUP_TOKEN_RECEIPT_DOMAIN = (
    b"hermes-webui:managed-deferred-startup:start-token-receipt:v1\x00"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_ACTIVITY_COUNT_KEYS = (
    "running_processes",
    "foreign_owner_active_processes",
    "finalizing_processes",
    "durable_undelivered_completions",
)


def _startup_type_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _canonical_startup_receipt_value(value: object):
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("startup receipt is not JSON-safe")
        return value
    if type(value) is bytes:
        return {"$bytes": value.hex()}
    if isinstance(value, Path):
        return {"$path": os.fspath(value)}
    if isinstance(value, Enum):
        return {
            "$enum": _startup_type_name(value),
            "value": _canonical_startup_receipt_value(value.value),
        }
    if type(value) in {tuple, list}:
        return {
            "$tuple" if type(value) is tuple else "$list": [
                _canonical_startup_receipt_value(item) for item in value
            ]
        }
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("startup receipt mapping is not canonical")
        return {
            "$dict": [
                [key, _canonical_startup_receipt_value(value[key])]
                for key in sorted(value)
            ]
        }
    params = getattr(type(value), "__dataclass_params__", None)
    if (
        not is_dataclass(value)
        or params is None
        or not params.frozen
    ):
        raise ValueError("startup receipt type is not immutable and canonical")
    return {
        "$type": _startup_type_name(value),
        "fields": {
            field.name: _canonical_startup_receipt_value(
                getattr(value, field.name)
            )
            for field in fields(value)
        },
    }


def _canonical_startup_sha256(value: object) -> str:
    canonical = _canonical_startup_receipt_value(value)
    try:
        payload = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("startup receipt is not JSON-safe") from exc
    return hashlib.sha256(payload).hexdigest()


def _validated_startup_evidence(
    evidence: object,
    *,
    transaction_id: str,
) -> dict:
    manifest_sha256 = deferred_release_manifest.deferred_release_manifest_sha256()
    if (
        type(evidence).__module__ != "server"
        or type(evidence).__name__ != "ManagedStartupAcceptanceEvidence"
    ):
        raise ValueError("managed startup acceptance evidence is absent")
    process_receipt = getattr(evidence, "process_receipt", None)
    driver = getattr(evidence, "driver_attestation", None)
    bundle = getattr(evidence, "step_receipt_bundle", None)
    pid = os.getpid()
    token = process_start_token(pid)
    if type(token) is not str or not token:
        raise ValueError("managed startup process identity is unavailable")
    token_bytes = token.encode("ascii")
    expected_token_sha256 = hashlib.sha256(
        _STARTUP_TOKEN_RECEIPT_DOMAIN
        + len(token_bytes).to_bytes(4, "big")
        + token_bytes
    ).hexdigest()
    if (
        type(getattr(process_receipt, "version", None)) is not int
        or process_receipt.version != 1
        or type(getattr(process_receipt, "pid", None)) is not int
        or isinstance(process_receipt.pid, bool)
        or process_receipt.pid != pid
        or type(getattr(process_receipt, "process_epoch", None)) is not str
        or re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", process_receipt.process_epoch
        )
        is None
        or getattr(process_receipt, "process_start_token_sha256", None)
        != expected_token_sha256
    ):
        raise ValueError("managed startup process evidence is mismatched")
    if (
        type(driver) is not DeferredStartupFileAttestation
        or driver.schema_version != 3
        or driver.transaction_id != transaction_id
        or driver.manifest_version != deferred_release_manifest.MANIFEST_VERSION
        or driver.manifest_sha256 != manifest_sha256
        or driver.status != "stable-parent-consistent"
        or driver.latest_process_epoch != process_receipt.process_epoch
        or type(driver.journal_generation) is not int
        or driver.journal_generation <= 0
        or driver.anchor_generation != driver.journal_generation
        or type(driver.attempt_count) is not int
        or isinstance(driver.attempt_count, bool)
        or driver.attempt_count <= 0
        or _SHA256_RE.fullmatch(driver.journal_sha256) is None
        or _SHA256_RE.fullmatch(driver.anchor_sha256) is None
        or _SHA256_RE.fullmatch(driver.attempt_topology_sha256) is None
    ):
        raise ValueError("managed startup driver evidence is mismatched")
    if (
        type(bundle) is not ManagedStartupReceiptBundle
        or bundle.transaction_id != transaction_id
        or bundle.manifest_sha256 != manifest_sha256
        or type(bundle.receipt_journal_generation) is not int
        or isinstance(bundle.receipt_journal_generation, bool)
        or bundle.receipt_journal_generation <= 0
        or _SHA256_RE.fullmatch(bundle.receipt_journal_sha256) is None
        or type(bundle.receipts) is not tuple
    ):
        raise ValueError("managed startup receipt evidence is mismatched")
    canonical_names = tuple(
        descriptor.name
        for descriptor in deferred_release_manifest.webui_startup_descriptors(
            deferred_release_manifest.deferred_release_manifest(),
            startup_admission_closed=True,
        )
    )
    if (
        len(bundle.receipts) != len(canonical_names)
        or tuple(name for name, _receipt in bundle.receipts)
        != canonical_names
        or any(receipt is None for _name, receipt in bundle.receipts)
    ):
        raise ValueError("managed startup receipt evidence is partial or reordered")
    steps = [
        {
            "name": name,
            "receipt_type": _startup_type_name(receipt),
            "receipt_sha256": _canonical_startup_sha256(receipt),
        }
        for name, receipt in bundle.receipts
    ]
    driver_payload = driver.as_dict()
    result = {
        "version": 1,
        "transaction_id": transaction_id,
        "manifest_sha256": manifest_sha256,
        "process": {
            "pid": pid,
            "start_token_sha256": expected_token_sha256,
        },
        "attempt_driver": {
            "type": _startup_type_name(driver),
            "journal_generation": driver.journal_generation,
            "journal_sha256": driver.journal_sha256,
            "anchor_generation": driver.anchor_generation,
            "anchor_sha256": driver.anchor_sha256,
            "attempt_count": driver.attempt_count,
            "attempt_topology_sha256": driver.attempt_topology_sha256,
            "attestation_sha256": _canonical_startup_sha256(driver_payload),
        },
        "receipt_journal": {
            "generation": bundle.receipt_journal_generation,
            "sha256": bundle.receipt_journal_sha256,
        },
        "steps": steps,
    }
    try:
        encoded = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        if json.loads(encoded) != result:
            raise ValueError("managed startup evidence JSON round trip changed")
    except (TypeError, ValueError) as exc:
        raise ValueError("managed startup evidence is not JSON-safe") from exc
    return result


def _release_control_signing_key() -> bytes:
    """Read the pre-provisioned key without mutating startup-fenced state."""
    if not config.startup_run_admission_is_closed():
        return _signing_key()
    key_path = config.STATE_DIR / ".signing_key"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(key_path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RuntimeError("release control signing key is unsafe")
        raw = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(raw) < 32:
        raise RuntimeError("release control signing key is invalid")
    return raw[:32]


def _canonical_request_body(body: dict) -> bytes:
    return json.dumps(
        body,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def release_control_signing_bytes(body: dict, timestamp: str) -> bytes:
    return (
        b"hermes-webui-release-control-v1\n"
        + str(timestamp).encode("ascii")
        + b"\n"
        + _canonical_request_body(body)
    )


def release_control_response_signing_bytes(payload: dict) -> bytes:
    return (
        b"hermes-webui-release-control-response-v1\n"
        + _canonical_request_body(payload)
    )


def _attest_release_control_response(payload: dict) -> dict:
    receipt = dict(payload)
    receipt["attestation"] = hmac.new(
        _release_control_signing_key(),
        release_control_response_signing_bytes(receipt),
        hashlib.sha256,
    ).hexdigest()
    return receipt


def _claim_nonce(nonce: str, now: float) -> bool:
    with _SEEN_NONCES_LOCK:
        cutoff = now - _AUTH_WINDOW_SECONDS
        for prior, seen_at in list(_SEEN_NONCES.items()):
            if seen_at < cutoff:
                _SEEN_NONCES.pop(prior, None)
        if nonce in _SEEN_NONCES:
            return False
        if len(_SEEN_NONCES) >= 4096:
            oldest = min(_SEEN_NONCES, key=_SEEN_NONCES.get)
            _SEEN_NONCES.pop(oldest, None)
        _SEEN_NONCES[nonce] = now
        return True


def verify_release_control_request(handler, body: dict) -> tuple[bool, str | None]:
    """Authenticate one fresh loopback request without trusting proxy headers."""
    try:
        remote = str(handler.client_address[0])
    except Exception:
        return False, "Release control requires a loopback client"
    if not _is_loopback(remote) or not isinstance(body, dict):
        return False, "Release control requires a loopback client"
    timestamp = str(
        getattr(handler, "headers", {}).get("X-Hermes-Release-Timestamp") or ""
    ).strip()
    signature = str(
        getattr(handler, "headers", {}).get("X-Hermes-Release-Signature") or ""
    ).strip().lower()
    nonce = str(body.get("nonce") or "").strip()
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False, "Release control authentication failed"
    now = time.time()
    if abs(now - request_time) > _AUTH_WINDOW_SECONDS:
        return False, "Release control authentication failed"
    if not re.fullmatch(r"[0-9a-f]{64}", signature) or not _NONCE_RE.fullmatch(nonce):
        return False, "Release control authentication failed"
    try:
        expected = hmac.new(
            _release_control_signing_key(),
            release_control_signing_bytes(body, timestamp),
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False, "Release control authentication failed"
    if not hmac.compare_digest(signature, expected):
        return False, "Release control authentication failed"
    if not _claim_nonce(nonce, now):
        return False, "Release control authentication failed"
    return True, None


def current_release_process_identity(*, build_identity: dict | None = None) -> dict:
    """Return the exact flat process/build identity required by fence requests."""
    build = (
        dict(build_identity)
        if isinstance(build_identity, dict)
        # The build-identity cache is keyed by every immutable managed env
        # field and bounded to one PID. Release-control calls are frequent
        # during drain and must not re-hash the sealed runtime on every poll.
        # Candidate/accepted binding still forces a fresh deep /health proof.
        else get_build_identity(refresh=False)
    )
    executable = Path(sys.executable).absolute()
    try:
        resolved_executable = executable.resolve(strict=True)
    except OSError:
        resolved_executable = executable
    try:
        cwd = str(Path.cwd().resolve(strict=True))
    except OSError:
        cwd = str(Path.cwd().absolute())
    pid_start_token = process_start_token(os.getpid()) or ""
    return {
        "pid": os.getpid(),
        "pid_start_token": pid_start_token,
        "started_at": config.SERVER_START_TIME,
        "instance_id": config.SERVER_INSTANCE_ID,
        "cwd": cwd,
        "executable": str(executable),
        "executable_resolved": str(resolved_executable),
        "build_status": str(build.get("status") or "unknown"),
        "build_valid": build.get("valid"),
        "build_id": build.get("build_id"),
        "commit": build.get("commit"),
        "tree": build.get("tree"),
        "manifest_sha256": build.get("manifest_sha256"),
        "agent_commit": build.get("agent_commit"),
        "agent_tree": build.get("agent_tree"),
        "agent_manifest_sha256": build.get("agent_manifest_sha256"),
        "runtime_manifest_sha256": build.get("runtime_manifest_sha256"),
        "selector_generation": build.get("selector_generation"),
        "release_path": build.get("release_path"),
        "launch_mode": build.get("launch_mode"),
        "selector_verified": build.get("selector_verified"),
        "selector_state_path": build.get("selector_state_path"),
        "selector_lock_path": build.get("selector_lock_path"),
        "launchd_label": build.get("launchd_label"),
        "startup_fenced": build.get("startup_fenced"),
        "startup_transaction_id": build.get("startup_transaction_id"),
    }


def _active_async_delegation_count() -> tuple[int, bool]:
    try:
        from tools.async_delegation import active_count

        count = active_count()
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return 0, False
        return count, True
    except Exception:
        return 0, False


def _component_activity_snapshot(loader, *, availability_key: str) -> dict:
    try:
        snapshot = loader()
        if not isinstance(snapshot, dict) or snapshot.get(availability_key) is not True:
            raise ValueError("release activity source is unavailable")
        for key, value in snapshot.items():
            if key == availability_key:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("release activity source returned an invalid count")
        return dict(snapshot)
    except Exception:
        return {availability_key: False}


def _process_completion_activity_snapshot(loader) -> dict:
    """Validate the Agent process barrier without treating metadata as counts."""
    expected_keys = set(_PROCESS_ACTIVITY_COUNT_KEYS) | {
        "process_completion_activity_available",
        "process_checkpoint_available",
        "process_checkpoint_reason",
    }
    try:
        snapshot = loader()
        if not isinstance(snapshot, dict) or set(snapshot) != expected_keys:
            raise ValueError("process activity source schema is invalid")
        if any(
            not isinstance(snapshot[key], int)
            or isinstance(snapshot[key], bool)
            or snapshot[key] < 0
            for key in _PROCESS_ACTIVITY_COUNT_KEYS
        ):
            raise ValueError("process activity source returned an invalid count")
        if not isinstance(
            snapshot["process_completion_activity_available"], bool
        ) or not isinstance(snapshot["process_checkpoint_available"], bool):
            raise ValueError("process activity availability is invalid")
        reason = snapshot["process_checkpoint_reason"]
        if not isinstance(reason, str) or not reason:
            raise ValueError("process checkpoint reason is invalid")
        if (
            snapshot["process_checkpoint_available"] is True
        ) != (reason == "verified"):
            raise ValueError("process checkpoint availability is inconsistent")
        return dict(snapshot)
    except Exception:
        return {
            "process_completion_activity_available": False,
            "process_checkpoint_available": False,
            "process_checkpoint_reason": "unavailable",
        }


def release_activity_snapshot() -> dict:
    with config.STREAMS_LOCK:
        active_streams = len(config.STREAMS)
    active_delegations, available = _active_async_delegation_count()
    snapshot = {
        "active_streams": active_streams,
        "active_async_delegations": active_delegations,
        "async_delegations_available": available,
    }
    from api.oauth import oauth_activity_snapshot
    from api.session_lifecycle import background_commit_activity_snapshot
    from api.terminal import terminal_activity_snapshot

    snapshot.update(
        _component_activity_snapshot(
            background_commit_activity_snapshot,
            availability_key="memory_commit_activity_available",
        )
    )
    snapshot.update(
        _component_activity_snapshot(
            oauth_activity_snapshot,
            availability_key="oauth_activity_available",
        )
    )
    snapshot.update(
        _component_activity_snapshot(
            terminal_activity_snapshot,
            availability_key="terminal_activity_available",
        )
    )
    try:
        from tools.process_registry import process_registry

        process_loader = process_registry.completion_activity_snapshot
    except Exception:
        process_loader = lambda: {"process_completion_activity_available": False}
    snapshot.update(
        _process_completion_activity_snapshot(process_loader)
    )
    return snapshot


def _require_current_identity(expected_identity: dict) -> dict:
    current = current_release_process_identity()
    if not isinstance(expected_identity, dict) or expected_identity != current:
        raise config.RunAdmissionIdentityMismatch(
            "release control process identity changed"
        )
    return current


def _require_external_activity_drained(snapshot: dict) -> None:
    availability = (
        "async_delegations_available",
        "memory_commit_activity_available",
        "oauth_activity_available",
        "terminal_activity_available",
        "process_completion_activity_available",
        "process_checkpoint_available",
    )
    unavailable = [key for key in availability if snapshot.get(key) is not True]
    if unavailable:
        raise config.RunAdmissionBusy(
            "release activity state is unavailable: " + ", ".join(unavailable)
        )
    counts = (
        "active_streams",
        "active_async_delegations",
        "active_background_memory_commits",
        "in_flight_memory_commits",
        "pending_oauth_flows",
        "active_terminals",
        *_PROCESS_ACTIVITY_COUNT_KEYS,
    )
    busy = [key for key in counts if int(snapshot.get(key, -1)) != 0]
    if busy:
        raise config.RunAdmissionBusy(
            "release activity has not drained: " + ", ".join(busy)
        )


def commit_release_control(
    token: str,
    *,
    expected_identity: dict,
    transaction_id: str | None = None,
) -> dict:
    """Commit only after external activity is zero before and after transition."""
    current = _require_current_identity(expected_identity)
    before = release_activity_snapshot()
    _require_external_activity_drained(before)
    admission = config.commit_run_admission(
        token,
        expected_identity=current,
        transaction_id=transaction_id,
    )
    after = release_activity_snapshot()
    try:
        _require_external_activity_drained(after)
        _require_current_identity(expected_identity)
    except Exception:
        config.revert_run_admission_commit(
            token,
            expected_identity=current,
            transaction_id=transaction_id,
        )
        raise
    return {"status": "committing", "admission": admission, "activity": after}


def execute_release_control(body: dict, *, fence_token: str | None = None) -> dict:
    action = str(body.get("action") or "").strip().lower()
    transaction_id = str(body.get("transaction_id") or "").strip()
    request_nonce = str(body.get("nonce") or "").strip()
    if not _NONCE_RE.fullmatch(transaction_id):
        raise ValueError("release control transaction identity is invalid")
    if action == "inspect":
        return _attest_release_control_response(
            {
                "status": "inspected",
                "transaction_id": transaction_id,
                "request_nonce": request_nonce,
                "identity": current_release_process_identity(),
                "admission": config.run_admission_snapshot(),
                "activity": release_activity_snapshot(),
            }
        )
    expected = body.get("expected")
    current = _require_current_identity(expected)
    if action == "fence":
        result = config.fence_run_admission(
            current,
            transaction_id=transaction_id,
        )
        status = str(result["admission"].get("state") or "fenced")
        return _attest_release_control_response(
            {
                "status": status,
                "transaction_id": transaction_id,
                "request_nonce": request_nonce,
                "fence_token": result["token"],
                "admission": result["admission"],
                "identity": current,
                "activity": release_activity_snapshot(),
            }
        )
    if action == "accept":
        startup_evidence: list[dict] = []

        def validate_startup_evidence(value: object) -> None:
            startup_evidence.append(
                _validated_startup_evidence(
                    value,
                    transaction_id=transaction_id,
                )
            )

        admission = config.accept_startup_run_admission(
            str(fence_token or ""),
            expected_identity=current,
            transaction_id=transaction_id,
            evidence_validator=validate_startup_evidence,
        )
        retained = config.startup_acceptance_evidence(transaction_id)
        if (
            len(startup_evidence) != 1
            or retained is None
        ):
            raise config.RunAdmissionBusy(
                "managed startup acceptance evidence is unavailable"
            )
        return _attest_release_control_response(
            {
                "status": "accepted",
                "transaction_id": transaction_id,
                "request_nonce": request_nonce,
                "admission": admission,
                "identity": current,
                "activity": release_activity_snapshot(),
                "startup_evidence": startup_evidence[0],
            }
        )
    if action == "abort":
        admission = config.abort_run_admission(
            str(fence_token or ""),
            expected_identity=current,
            transaction_id=transaction_id,
        )
        return _attest_release_control_response(
            {
                "status": "aborted",
                "transaction_id": transaction_id,
                "request_nonce": request_nonce,
                "admission": admission,
                "identity": current,
            }
        )
    if action == "commit":
        result = commit_release_control(
            str(fence_token or ""),
            expected_identity=current,
            transaction_id=transaction_id,
        )
        return _attest_release_control_response(
            {
                **result,
                "transaction_id": transaction_id,
                "request_nonce": request_nonce,
                "identity": current,
            }
        )
    raise ValueError("release control action is invalid")
