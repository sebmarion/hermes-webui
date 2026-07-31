#!/usr/bin/env python3
"""Receipt-first rolling retention for verified Hermes release snapshots."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import webui_release_selector as release_selector

_UNCONFIGURED_ROOT = Path("/__hermes_release_retention_unconfigured__")
RELIABILITY_ROOT = _UNCONFIGURED_ROOT
PRIVATE_ROOT = RELIABILITY_ROOT / "private"
TRANSACTIONS_ROOT = PRIVATE_ROOT / "transactions"
SELECTOR_ROOT = RELIABILITY_ROOT / "selector"
SELECTOR_RELEASES = SELECTOR_ROOT / "releases"
SELECTOR_STATE = SELECTOR_ROOT / "selector-state.json"
SELECTOR_LOCK = SELECTOR_ROOT / "selector-state.lock"
RECEIPTS_ROOT = PRIVATE_ROOT / "cleanup-receipts"
SNAPSHOT_NAME = re.compile(r"^snapshots-[A-Za-z0-9-]+$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RETAIN_TERMINAL_COUNT = 1


TRANSACTION_PHASE_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "bootstrap_rollback_claimed": (),
    "staged": (),
    "plist_installed": ("staged",),
    "old_fenced": ("plist_installed",),
    "old_committed": ("old_fenced",),
    "selection_activated": ("old_committed",),
    "old_job_booted_out": ("selection_activated",),
    "old_stopped": ("selection_activated",),
    "candidate_job_bootstrapped": ("old_stopped",),
    "replacement_proved": ("old_stopped",),
    "candidate_fenced_health_proved": ("replacement_proved",),
    "pair_ready": ("candidate_fenced_health_proved",),
    "pair_gate_install_intent": ("pair_ready",),
    "pair_gate_installed": ("pair_gate_install_intent",),
    "pair_commit_intent": ("pair_ready",),
    "promoted": ("pair_commit_intent",),
    "gateway_opened": ("promoted",),
    "candidate_accepted": ("gateway_opened",),
    "accepted_health_proved": ("candidate_accepted",),
    "pair_accepted": ("accepted_health_proved",),
    "pair_gate_release_intent": ("pair_accepted",),
    "pair_released": ("pair_gate_release_intent",),
    "pair_opened": ("pair_released",),
    "last_good_split_attested": ("plist_installed",),
    "gateway_last_good_attested": ("last_good_split_attested",),
    "watchdog_cron_disable_intent": ("gateway_last_good_attested",),
    "watchdog_cron_disabled": ("watchdog_cron_disable_intent",),
    "watchdog_state_reconciled": ("watchdog_cron_disabled",),
    "gateway_drain_intent": (
        "gateway_last_good_attested",
        "watchdog_state_reconciled",
    ),
    "gateway_drained": ("gateway_drain_intent",),
    "gateway_stop_intent": ("gateway_drained",),
    "gateway_gracefully_stopped": ("gateway_stop_intent",),
    "gateway_dispatcher_lock_acquired": ("gateway_gracefully_stopped",),
    "gateway_workers_quiescent": ("gateway_dispatcher_lock_acquired",),
    "paired_state_snapshot_created": ("gateway_workers_quiescent",),
    "gateway_dispatcher_lock_released": ("paired_state_snapshot_created",),
    "candidate_gateway_start_intent": ("gateway_dispatcher_lock_released",),
    "candidate_gateway_accepted": ("candidate_gateway_start_intent",),
    "rollback_started": (),
    "state_rolled_back": ("rollback_started",),
    "plist_restored": ("state_rolled_back",),
    "failed_candidate_stopped": ("plist_restored",),
    "state_snapshot_restored": ("failed_candidate_stopped",),
    "rollback_gateway_stop_intent": ("rollback_started",),
    "rollback_gateway_gracefully_stopped": ("rollback_gateway_stop_intent",),
    "rollback_gateway_dispatcher_lock_acquired": (
        "rollback_gateway_gracefully_stopped",
    ),
    "rollback_gateway_workers_quiescent": (
        "rollback_gateway_dispatcher_lock_acquired",
    ),
    "rollback_gateway_plist_restored": (
        "rollback_gateway_workers_quiescent",
        "state_snapshot_restored",
    ),
    "rollback_gateway_drain_cleared": ("rollback_gateway_plist_restored",),
    "rollback_gateway_dispatcher_lock_released": (
        "rollback_gateway_drain_cleared",
    ),
    "last_good_restarted": ("state_snapshot_restored",),
    "rollback_verified": ("last_good_restarted",),
    "watchdog_cron_restore_intent": ("pair_opened",),
    "watchdog_cron_restored": ("watchdog_cron_restore_intent",),
    "watchdog_cron_rollback_restored": ("rollback_verified",),
}

BOOTSTRAP_PHASE_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "prepared": (),
    "pre_managed_controls_stage_intent": ("prepared",),
    "pre_managed_controls_staged": ("pre_managed_controls_stage_intent",),
    "watchdog_cron_disabled": ("pre_managed_controls_staged",),
    "writers_frozen": ("watchdog_cron_disabled",),
    "cli_maintenance_gate_stage_intent": ("writers_frozen",),
    "cli_maintenance_gate_installed": ("cli_maintenance_gate_stage_intent",),
    "legacy_cron_tick_lock_normalize_intent": (
        "cli_maintenance_gate_installed",
    ),
    "legacy_cron_tick_lock_normalized": (
        "legacy_cron_tick_lock_normalize_intent",
    ),
    "legacy_cron_tick_lock_acquired": (
        "legacy_cron_tick_lock_normalized",
    ),
    "legacy_gateway_drain_intent": ("legacy_cron_tick_lock_acquired",),
    "legacy_gateway_drain_acknowledged": ("legacy_gateway_drain_intent",),
    "legacy_gateway_stop_intent": ("legacy_gateway_drain_acknowledged",),
    "legacy_gateway_gracefully_stopped": ("legacy_gateway_stop_intent",),
    "synthetic_store_mode_normalize_intent": (
        "legacy_gateway_gracefully_stopped",
    ),
    "synthetic_store_modes_normalized": (
        "synthetic_store_mode_normalize_intent",
    ),
    "legacy_dispatcher_lock_acquired": ("synthetic_store_modes_normalized",),
    "frozen_boundary_proved": ("legacy_dispatcher_lock_acquired",),
    "legacy_jobs_booted_out": ("frozen_boundary_proved",),
    "ingress_gate_start_intent": ("legacy_jobs_booted_out",),
    "services_stopped": ("ingress_gate_start_intent",),
    "legacy_cron_tick_lock_released": ("services_stopped",),
    "ingress_gate_started": ("legacy_cron_tick_lock_released",),
    "snapshot_created": ("ingress_gate_started",),
    "synthetic_state_quarantine_intent": ("snapshot_created",),
    "synthetic_state_quarantined": ("synthetic_state_quarantine_intent",),
    "ingress_gate_stopped": ("synthetic_state_quarantined",),
    "managed_pair_start_intent": ("ingress_gate_stopped",),
    "legacy_dispatcher_lock_released": ("managed_pair_start_intent",),
    "managed_pair_started": ("legacy_dispatcher_lock_released",),
    "cutover_handed_off": ("managed_pair_started",),
    "watchdog_installed": ("cutover_handed_off",),
    "watchdog_reconciled_once": ("watchdog_installed",),
    "watchdog_reconciled_twice": ("watchdog_reconciled_once",),
    "legacy_gateway_drain_cleared": ("watchdog_reconciled_twice",),
    "candidate_pair_accepted": ("legacy_gateway_drain_cleared",),
    "cli_candidate_activate_intent": ("candidate_pair_accepted",),
    "cli_candidate_activated": ("cli_candidate_activate_intent",),
    "watchdog_cron_restored": ("cli_candidate_activated",),
    "complete": ("watchdog_cron_restored",),
    "aborted_before_cutover": ("prepared",),
    "rollback_started": ("legacy_jobs_booted_out",),
    "rollback_gateway_stop_intent": ("rollback_started",),
    "rollback_services_stopped": ("rollback_gateway_stop_intent",),
    "rollback_cron_tick_lock_released": ("rollback_services_stopped",),
    "rollback_dispatcher_lock_acquired": (
        "rollback_cron_tick_lock_released",
    ),
    "rollback_workers_quiescent": ("rollback_dispatcher_lock_acquired",),
    "rollback_state_restored": ("rollback_workers_quiescent",),
    "rollback_synthetic_state_requarantined": ("rollback_state_restored",),
    "rollback_plists_restored": ("rollback_synthetic_state_requarantined",),
    "rollback_watchdog_restored": ("rollback_plists_restored",),
    "rollback_gateway_drain_cleared": ("rollback_watchdog_restored",),
    "rollback_dispatcher_lock_released": (
        "rollback_gateway_drain_cleared",
    ),
    "rollback_cron_tick_lock_restored": (
        "rollback_dispatcher_lock_released",
    ),
    "rollback_synthetic_store_modes_restored": (
        "rollback_cron_tick_lock_restored",
    ),
    "rollback_services_restarted": (
        "rollback_synthetic_store_modes_restored",
    ),
    "rollback_cron_restored": ("rollback_services_restarted",),
    "rollback_verified": ("rollback_cron_restored",),
}

MANAGED_SUCCESS_REQUIRED = {
    "staged",
    "plist_installed",
    "promoted",
    "candidate_accepted",
    "accepted_health_proved",
    "pair_accepted",
    "pair_released",
    "pair_opened",
    "gateway_dispatcher_lock_released",
    "candidate_gateway_accepted",
    "watchdog_cron_restored",
}
MANAGED_ROLLBACK_REQUIRED = {
    "rollback_started",
    "state_rolled_back",
    "plist_restored",
    "failed_candidate_stopped",
    "state_snapshot_restored",
    "last_good_restarted",
    "rollback_verified",
    "watchdog_cron_rollback_restored",
}
BOOTSTRAP_SUCCESS_REQUIRED = {
    "snapshot_created",
    "candidate_pair_accepted",
    "cli_candidate_activated",
    "watchdog_cron_restored",
    "complete",
}
BOOTSTRAP_ROLLBACK_REQUIRED = {
    "snapshot_created",
    "rollback_state_restored",
    "rollback_cron_restored",
    "rollback_verified",
}

SENSITIVE_JOURNAL_KEYS = {
    "fence_token",
    "authorization",
    "cookie",
    "set-cookie",
    "x-hermes-release-fence",
}


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleasePaths:
    reliability_root: Path
    private_root: Path
    transactions_root: Path
    selector_root: Path
    selector_releases: Path
    selector_state: Path
    selector_lock: Path
    receipts_root: Path


def is_managed_selector_control_pair(
    selector_state: Path | str,
    selector_lock: Path | str,
) -> bool:
    state = Path(selector_state)
    lock = Path(selector_lock)
    return (
        state.is_absolute()
        and lock.is_absolute()
        and state.name == "selector-state.json"
        and lock.name == "selector-state.lock"
        and state.parent == lock.parent
        and state.parent.name == "selector"
    )


def release_paths(
    selector_state: Path | str,
    selector_lock: Path | str,
) -> ReleasePaths:
    state = Path(selector_state)
    lock = Path(selector_lock)
    if not is_managed_selector_control_pair(state, lock):
        raise CleanupError(
            "selector state and lock must be canonical files in a selector directory"
        )
    try:
        if state.resolve(strict=True) != state or lock.resolve(strict=True) != lock:
            raise CleanupError("selector control files must be canonical")
        selector_root = state.parent.resolve(strict=True)
        reliability_root = selector_root.parent.resolve(strict=True)
    except OSError as exc:
        raise CleanupError("selector control files are missing") from exc
    private_root = reliability_root / "private"
    return ReleasePaths(
        reliability_root=reliability_root,
        private_root=private_root,
        transactions_root=private_root / "transactions",
        selector_root=selector_root,
        selector_releases=selector_root / "releases",
        selector_state=state,
        selector_lock=lock,
        receipts_root=private_root / "cleanup-receipts",
    )


def configure_release_paths(paths: ReleasePaths) -> None:
    global RELIABILITY_ROOT
    global PRIVATE_ROOT
    global TRANSACTIONS_ROOT
    global SELECTOR_ROOT
    global SELECTOR_RELEASES
    global SELECTOR_STATE
    global SELECTOR_LOCK
    global RECEIPTS_ROOT

    RELIABILITY_ROOT = paths.reliability_root
    PRIVATE_ROOT = paths.private_root
    TRANSACTIONS_ROOT = paths.transactions_root
    SELECTOR_ROOT = paths.selector_root
    SELECTOR_RELEASES = paths.selector_releases
    SELECTOR_STATE = paths.selector_state
    SELECTOR_LOCK = paths.selector_lock
    RECEIPTS_ROOT = paths.receipts_root


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def read_json_file(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise CleanupError(f"{label} is not an object: {path}")
    return value


def require_private_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        opened = path.lstat()
    except OSError as exc:
        raise CleanupError(f"{label} is missing: {path}") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or opened.st_mode & 0o022
    ):
        raise CleanupError(f"{label} is unsafe: {path}")
    return opened


def require_owned_directory(
    path: Path,
    *,
    label: str,
    exact_mode: int | None = None,
) -> os.stat_result:
    try:
        opened = path.lstat()
    except OSError as exc:
        raise CleanupError(f"{label} is missing: {path}") from exc
    mode = stat.S_IMODE(opened.st_mode)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_mode & 0o022
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise CleanupError(f"{label} is unsafe: {path}")
    return opened


def _directory_identity_record(
    path: Path,
    opened: os.stat_result,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "original_mode": stat.S_IMODE(opened.st_mode),
        "state": "sealed",
    }


def _verify_directory_identity(
    path: Path,
    record: dict[str, Any],
    *,
    label: str,
) -> os.stat_result:
    opened = require_owned_directory(path, label=label)
    if (
        str(path) != record.get("path")
        or opened.st_dev != record.get("device")
        or opened.st_ino != record.get("inode")
    ):
        raise CleanupError(f"{label} identity changed: {path}")
    return opened


def open_snapshot_payload_for_quarantine(
    snapshot_root: Path,
    payload: Path,
    snapshot_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if payload.parent != snapshot_root:
        raise CleanupError(f"snapshot payload has the wrong parent: {payload}")
    root_opened = require_owned_directory(
        snapshot_root,
        label="candidate snapshot directory",
    )
    root_record = snapshot_record or _directory_identity_record(
        snapshot_root,
        root_opened,
    )
    _verify_directory_identity(
        snapshot_root,
        root_record,
        label="candidate snapshot directory",
    )
    payload_opened = require_owned_directory(
        payload,
        label="candidate snapshot payload",
    )
    payload_record = _directory_identity_record(payload, payload_opened)
    try:
        os.chmod(snapshot_root, 0o700)
        os.chmod(payload, 0o700)
    except Exception:
        try:
            os.chmod(payload, payload_record["original_mode"])
        finally:
            os.chmod(snapshot_root, root_record["original_mode"])
        raise
    root_record["state"] = "opened"
    payload_record["state"] = "opened"
    return {
        "snapshot_root": root_record,
        "payload": payload_record,
    }


def reseal_snapshot_root(path: Path, record: dict[str, Any]) -> None:
    _verify_directory_identity(
        path,
        record,
        label="candidate snapshot directory",
    )
    original_mode = record.get("original_mode")
    if not isinstance(original_mode, int) or original_mode & 0o022:
        raise CleanupError(f"snapshot root mode is invalid: {path}")
    os.chmod(path, original_mode)
    record["state"] = "sealed"


def _snapshot_root_records(
    receipt: dict[str, Any],
) -> dict[Path, dict[str, Any]]:
    rows = receipt.get("snapshot_roots")
    if not isinstance(rows, list):
        raise CleanupError("cleanup receipt snapshot roots are invalid")
    records: dict[Path, dict[str, Any]] = {}
    for record in rows:
        if not isinstance(record, dict):
            raise CleanupError("cleanup snapshot root record is invalid")
        path = Path(str(record.get("path") or ""))
        if path in records:
            raise CleanupError(f"cleanup snapshot root is duplicated: {path}")
        if not canonical_child(path, PRIVATE_ROOT):
            raise CleanupError(f"cleanup snapshot root is unsafe: {path}")
        records[path] = record
    return records


def _open_snapshot_root_for_recovery(
    path: Path,
    record: dict[str, Any],
) -> None:
    _verify_directory_identity(
        path,
        record,
        label="cleanup snapshot root",
    )
    os.chmod(path, 0o700)
    record["state"] = "opened"


def _reseal_snapshot_roots(receipt: dict[str, Any]) -> None:
    for path, record in _snapshot_root_records(receipt).items():
        reseal_snapshot_root(path, record)


def canonical_child(path: Path, parent: Path) -> bool:
    if not path.is_absolute() or path.parent != parent:
        return False
    try:
        return path.resolve(strict=True).parent == parent.resolve(strict=True)
    except OSError:
        return False


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def tree_accounting(path: Path) -> tuple[int, int]:
    logical = 0
    allocated = 0
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *files]:
            child = root_path / name
            opened = child.lstat()
            if stat.S_ISLNK(opened.st_mode):
                raise CleanupError(f"snapshot contains a symlink: {child}")
            if not (
                stat.S_ISDIR(opened.st_mode) or stat.S_ISREG(opened.st_mode)
            ):
                raise CleanupError(f"snapshot contains a special file: {child}")
            logical += opened.st_size
            allocated += opened.st_blocks * 512
    return logical, allocated


def journal_contains_sensitive_value(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_JOURNAL_KEYS:
                return True
            if journal_contains_sensitive_value(item):
                return True
    elif isinstance(value, list):
        return any(journal_contains_sensitive_value(item) for item in value)
    return False


def validate_phase_graph(
    phases: object,
    prerequisites: dict[str, tuple[str, ...]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(phases, dict):
        raise CleanupError(f"{label} phases are invalid")
    validated: dict[str, dict[str, Any]] = {}
    for phase, receipt in phases.items():
        if phase not in prerequisites or not isinstance(receipt, dict):
            raise CleanupError(f"{label} phase is invalid: {phase}")
        validated[phase] = receipt
    for phase in validated:
        missing = [
            prerequisite
            for prerequisite in prerequisites[phase]
            if prerequisite not in validated
        ]
        if missing:
            raise CleanupError(
                f"{label} phase prerequisites are missing for {phase}: "
                + ",".join(missing)
            )
    if {
        "pair_commit_intent",
        "bootstrap_rollback_claimed",
    } <= set(validated):
        raise CleanupError(
            f"{label} has conflicting pair commit and bootstrap rollback "
            "claim phases"
        )
    return validated


def cron_receipt_is_scheduled(receipt: object) -> bool:
    return (
        isinstance(receipt, dict)
        and receipt.get("job_enabled") is True
        and receipt.get("job_state") == "scheduled"
    )


def cron_receipt_is_restored(receipt: object) -> bool:
    if cron_receipt_is_scheduled(receipt):
        return True
    if not isinstance(receipt, dict):
        return False
    original = receipt.get("original_controls")
    return (
        receipt.get("backend") == "hermes_internal"
        and receipt.get("control_origin") == "preexisting"
        and isinstance(original, dict)
        and set(original)
        == {"enabled", "state", "paused_at", "paused_reason"}
        and original.get("enabled") is False
        and original.get("state") == "paused"
        and receipt.get("job_enabled") is original["enabled"]
        and receipt.get("job_state") == original["state"]
        and isinstance(receipt.get("job_id"), str)
        and bool(receipt["job_id"])
        and all(
            HEX64.fullmatch(str(receipt.get(key) or ""))
            for key in (
                "job_sha256",
                "stable_job_sha256",
                "crontab_sha256",
            )
        )
    )


def managed_terminal_kind(
    phases: dict[str, Any],
    rollback_receipt: dict[str, Any] | None = None,
) -> str | None:
    validated = validate_phase_graph(
        phases,
        TRANSACTION_PHASE_PREREQUISITES,
        label="managed journal",
    )
    names = set(validated)
    receipt = rollback_receipt or {}
    snapshot_id = receipt.get("state_snapshot_id")
    snapshot_sha = receipt.get("state_snapshot_sha256")
    if not isinstance(snapshot_id, str) or not HEX64.fullmatch(
        str(snapshot_sha or "")
    ):
        raise CleanupError("managed rollback receipt is invalid")
    if "rollback_started" in names:
        if names & {
            "promoted",
            "candidate_accepted",
            "pair_accepted",
            "pair_opened",
            "watchdog_cron_restored",
        }:
            return None
        if not MANAGED_ROLLBACK_REQUIRED <= names:
            return None
        restored = validated["state_snapshot_restored"]
        verified = validated["rollback_verified"]
        if (
            restored.get("status") not in {"restored", "not-required"}
            or restored.get("state_snapshot_id") != snapshot_id
            or restored.get("state_snapshot_sha256") != snapshot_sha
            or verified.get("status") != "verified"
            or verified.get("state_snapshot_id") != snapshot_id
            or verified.get("state_snapshot_sha256") != snapshot_sha
            or not cron_receipt_is_scheduled(
                validated["watchdog_cron_rollback_restored"]
            )
        ):
            return None
        return "verified-managed-rollback"
    if not MANAGED_SUCCESS_REQUIRED <= names:
        return None
    pair_accepted = validated["pair_accepted"]
    binding = pair_accepted.get("binding")
    build = binding.get("build") if isinstance(binding, dict) else None
    if (
        not isinstance(pair_accepted.get("admission"), dict)
        or pair_accepted["admission"].get("state") != "open"
        or not isinstance(binding, dict)
        or binding.get("health_status") != "ok"
        or not isinstance(build, dict)
        or build.get("valid") is not True
        or validated["pair_released"].get("status")
        not in {"released", "already-released"}
        or validated["pair_opened"].get("status") != "verified"
        or validated["gateway_dispatcher_lock_released"].get("status")
        not in {"released", "adopted-bootstrap-receipt"}
        or not isinstance(
            validated["candidate_gateway_accepted"].get("binding"),
            dict,
        )
        or validated["candidate_gateway_accepted"]["binding"].get("status")
        != "verified"
        or not isinstance(validated["promoted"].get("promotion"), dict)
        or not cron_receipt_is_restored(validated["watchdog_cron_restored"])
    ):
        return None
    return "accepted-managed-promotion"


def bootstrap_terminal_kind(phases: dict[str, Any]) -> str | None:
    validated = validate_phase_graph(
        phases,
        BOOTSTRAP_PHASE_PREREQUISITES,
        label="bootstrap journal",
    )
    names = set(validated)
    if "rollback_started" in names and "complete" in names:
        raise CleanupError("bootstrap journal has conflicting terminal phases")
    if "aborted_before_cutover" in names and names & {
        "services_stopped",
        "ingress_gate_started",
        "rollback_started",
        "complete",
    }:
        raise CleanupError("bootstrap journal has conflicting abort phases")
    if "complete" in names:
        if (
            BOOTSTRAP_SUCCESS_REQUIRED <= names
            and validated["complete"].get("status") == "accepted"
            and not cron_receipt_is_scheduled(
                validated["watchdog_cron_restored"]
            )
        ):
            return None
        if (
            BOOTSTRAP_SUCCESS_REQUIRED <= names
            and validated["complete"].get("status") == "accepted"
        ):
            return "accepted-bootstrap-promotion"
        return None
    if not BOOTSTRAP_ROLLBACK_REQUIRED <= names:
        return None
    snapshot = validated["snapshot_created"]
    restored = validated["rollback_state_restored"]
    if (
        snapshot.get("status") != "created"
        or restored.get("status") != "restored"
        or restored.get("state_snapshot_id") != snapshot.get("state_snapshot_id")
        or restored.get("state_snapshot_sha256")
        != snapshot.get("state_snapshot_sha256")
        or validated["rollback_verified"].get("status") != "verified"
        or not cron_receipt_is_scheduled(validated["rollback_cron_restored"])
    ):
        return None
    return "verified-bootstrap-rollback"


def combined_terminal_kind(
    *,
    bootstrap_phases: dict[str, Any] | None,
    managed_phases: dict[str, Any] | None,
    rollback_receipt: dict[str, Any] | None,
) -> str | None:
    bootstrap_kind = (
        bootstrap_terminal_kind(bootstrap_phases)
        if bootstrap_phases is not None
        else None
    )
    managed_kind = (
        managed_terminal_kind(managed_phases, rollback_receipt)
        if managed_phases is not None
        else None
    )
    if bootstrap_phases is not None and managed_phases is not None:
        if (
            bootstrap_kind == "verified-bootstrap-rollback"
            and managed_kind is None
            and set(managed_phases) == {"bootstrap_rollback_claimed"}
            and managed_phases["bootstrap_rollback_claimed"].get(
                "rollback_receipt"
            )
            == rollback_receipt
        ):
            return (
                "verified-bootstrap-rollback"
                "+bootstrap-rollback-claimed"
            )
        if bootstrap_kind is None or managed_kind is None:
            return None
        return f"{bootstrap_kind}+{managed_kind}"
    return bootstrap_kind or managed_kind


def validate_managed_journal(
    journal: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    if (
        set(journal)
        != {
            "version",
            "transaction_id",
            "expected_candidate_identity",
            "rollback_receipt",
            "phases",
        }
        or journal.get("version") != 1
        or not isinstance(journal.get("transaction_id"), str)
        or not journal["transaction_id"]
        or not isinstance(journal.get("expected_candidate_identity"), dict)
        or not isinstance(journal.get("rollback_receipt"), dict)
        or journal_contains_sensitive_value(journal)
    ):
        raise CleanupError(f"transaction journal schema is invalid: {path}")
    receipt = journal["rollback_receipt"]
    if (
        not str(receipt.get("build_id") or "").strip()
        or not HEX64.fullmatch(str(receipt.get("plist_sha256") or ""))
        or not str(receipt.get("state_snapshot_id") or "").strip()
        or not HEX64.fullmatch(
            str(receipt.get("state_snapshot_sha256") or "")
        )
    ):
        raise CleanupError(f"transaction rollback receipt is invalid: {path}")
    phases = journal.get("phases")
    claim = (
        phases.get("bootstrap_rollback_claimed")
        if isinstance(phases, dict)
        else None
    )
    receipt_plist_mode = receipt.get("plist_mode")
    if claim is not None and (
        set(claim)
        != {
            "schema",
            "bootstrap_transaction_id",
            "split_provenance_sha256",
            "split_evidence_sha256",
            "rollback_receipt",
        }
        or claim.get("schema") != "hermes.bootstrap_rollback_claim.v1"
        or claim.get("bootstrap_transaction_id")
        != journal["transaction_id"]
        or not HEX64.fullmatch(
            str(claim.get("split_provenance_sha256") or "")
        )
        or not HEX64.fullmatch(
            str(claim.get("split_evidence_sha256") or "")
        )
        or claim.get("rollback_receipt") != receipt
        or isinstance(receipt_plist_mode, bool)
        or not isinstance(receipt_plist_mode, int)
        or receipt_plist_mode <= 0
        or receipt_plist_mode != stat.S_IMODE(receipt_plist_mode)
        or not str(receipt.get("cli_link_target") or "").strip()
    ):
        raise CleanupError(
            f"bootstrap rollback claim receipt is invalid: {path}"
        )
    legacy_pre_split = (
        isinstance(phases, dict)
        and "gateway_last_good_attested" in phases
        and "last_good_split_attested" not in phases
    )
    prerequisites = TRANSACTION_PHASE_PREREQUISITES
    if legacy_pre_split:
        prerequisites = {
            **TRANSACTION_PHASE_PREREQUISITES,
            "gateway_last_good_attested": ("plist_installed",),
        }
    validate_phase_graph(
        phases,
        prerequisites,
        label=f"managed journal {path.name}",
    )
    journal["_legacy_pre_split"] = legacy_pre_split
    return journal


def validate_bootstrap_journal(
    journal: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    if (
        set(journal) != {"version", "transaction_id", "phases"}
        or journal.get("version") != 1
        or not isinstance(journal.get("transaction_id"), str)
        or not journal["transaction_id"]
        or journal_contains_sensitive_value(journal)
    ):
        raise CleanupError(f"bootstrap journal schema is invalid: {path}")
    validate_phase_graph(
        journal.get("phases"),
        BOOTSTRAP_PHASE_PREREQUISITES,
        label=f"bootstrap journal {path.name}",
    )
    bootstrap_terminal_kind(journal["phases"])
    return journal


def load_journals() -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    journals: dict[str, dict[str, Any]] = {}
    for path in sorted(TRANSACTIONS_ROOT.glob("release*.json")):
        if not canonical_child(path, TRANSACTIONS_ROOT):
            raise CleanupError(f"transaction journal escapes its root: {path}")
        require_private_regular_file(path, label="transaction journal")
        journal = validate_managed_journal(
            read_json_file(path, label="transaction journal"),
            path,
        )
        snapshot_id = journal["rollback_receipt"]["state_snapshot_id"]
        if snapshot_id in journals:
            raise CleanupError(
                f"duplicate transaction snapshot identity: {snapshot_id}"
            )
        journal["_path"] = str(path)
        journal["_mtime"] = path.stat().st_mtime
        journals[snapshot_id] = journal
    bootstraps: dict[str, dict[str, Any]] = {}
    for path in sorted(TRANSACTIONS_ROOT.glob("release*.json.bootstrap")):
        if not canonical_child(path, TRANSACTIONS_ROOT):
            raise CleanupError(f"bootstrap journal escapes its root: {path}")
        require_private_regular_file(path, label="bootstrap journal")
        journal = validate_bootstrap_journal(
            read_json_file(path, label="bootstrap journal"),
            path,
        )
        transaction_id = journal["transaction_id"]
        if transaction_id in bootstraps:
            raise CleanupError(
                f"duplicate bootstrap transaction: {transaction_id}"
            )
        journal["_path"] = str(path)
        journal["_mtime"] = path.stat().st_mtime
        bootstraps[transaction_id] = journal
    return journals, bootstraps


def absolute_canonical_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CleanupError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise CleanupError(f"{label} is not canonical: {value}")
    return path


def state_tree_receipt(path: Path) -> dict[str, Any]:
    try:
        opened = path.lstat()
    except OSError as exc:
        raise CleanupError(f"snapshot payload entry is missing: {path}") from exc
    if stat.S_ISLNK(opened.st_mode):
        raise CleanupError(f"snapshot payload entry is symlinked: {path}")
    rows: list[dict[str, Any]] = []
    if stat.S_ISREG(opened.st_mode):
        kind = "file"
        rows.append(
            {
                "path": ".",
                "kind": "file",
                "mode": stat.S_IMODE(opened.st_mode),
                "sha256": sha256_file(path),
            }
        )
    elif stat.S_ISDIR(opened.st_mode):
        kind = "directory"
        rows.append(
            {
                "path": ".",
                "kind": "directory",
                "mode": stat.S_IMODE(opened.st_mode),
            }
        )
        for child in sorted(path.rglob("*")):
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                raise CleanupError(
                    f"snapshot payload contains a symlink: {child}"
                )
            relative = child.relative_to(path).as_posix()
            if stat.S_ISDIR(child_stat.st_mode):
                rows.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "mode": stat.S_IMODE(child_stat.st_mode),
                    }
                )
            elif stat.S_ISREG(child_stat.st_mode):
                rows.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": stat.S_IMODE(child_stat.st_mode),
                        "sha256": sha256_file(child),
                    }
                )
            else:
                raise CleanupError(
                    f"snapshot payload contains a special file: {child}"
                )
    else:
        raise CleanupError(f"snapshot payload entry is not regular: {path}")
    content_rows = [
        {key: value for key, value in row.items() if key != "mode"}
        for row in rows
    ]
    encoded = json.dumps(
        content_rows,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "kind": kind,
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "rows": rows,
    }


def verify_snapshot_payload(
    manifest: dict[str, Any],
    *,
    root: Path,
    manifest_bytes: bytes,
) -> None:
    require_owned_directory(
        root,
        label="snapshot payload root",
        exact_mode=0o555,
    )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CleanupError("state snapshot entries are invalid")
    seen_targets: set[Path] = set()
    seen_relative: set[str] = set()
    expected_children = {".snapshot-metadata.json"}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise CleanupError("state snapshot entry is invalid")
        target = absolute_canonical_path(
            entry.get("target"),
            label="mutable state target",
        )
        if target in seen_targets:
            raise CleanupError("state snapshot target is duplicated")
        seen_targets.add(target)
        relative = str(entry.get("snapshot_relative_path") or "")
        if relative != f"entry-{index:04d}" or relative in seen_relative:
            raise CleanupError("state snapshot entry path is invalid")
        seen_relative.add(relative)
        snapshot_path = root / relative
        kind = entry.get("kind")
        rows = entry.get("rows")
        if kind == "absent":
            if rows != [] or snapshot_path.exists() or snapshot_path.is_symlink():
                raise CleanupError("state snapshot tombstone does not match")
            continue
        if kind not in {"file", "directory"}:
            raise CleanupError("state snapshot entry kind is invalid")
        if not HEX64.fullmatch(str(entry.get("tree_sha256") or "")):
            raise CleanupError("state snapshot tree hash is invalid")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise CleanupError("state snapshot metadata rows are invalid")
        expected_children.add(relative)
        receipt = state_tree_receipt(snapshot_path)
        if (
            receipt["kind"] != kind
            or receipt["tree_sha256"] != entry["tree_sha256"]
        ):
            raise CleanupError("state snapshot content does not match manifest")
        expected_rows = [
            {
                **row,
                "mode": 0o555
                if row.get("kind") == "directory"
                else 0o444,
            }
            for row in rows
        ]
        if receipt["rows"] != expected_rows:
            raise CleanupError("state snapshot is not sealed")
    actual_children = {child.name for child in root.iterdir()}
    if actual_children != expected_children:
        raise CleanupError("state snapshot payload has unexpected children")
    metadata = root / ".snapshot-metadata.json"
    metadata_stat = require_private_regular_file(
        metadata,
        label="snapshot recovery metadata",
    )
    if stat.S_IMODE(metadata_stat.st_mode) not in {0o400, 0o600}:
        raise CleanupError("snapshot recovery metadata is not owner-only")
    if metadata.read_bytes() != manifest_bytes:
        raise CleanupError("snapshot recovery metadata disagrees with manifest")


def validate_manifest(
    manifest_path: Path,
    expected_root: Path,
    *,
    expected_snapshot_id: str,
    expected_sha256: str,
    verify_payload: bool = True,
) -> float:
    require_private_regular_file(manifest_path, label="snapshot manifest")
    embedded_manifest = manifest_path.parent == expected_root.parent
    external_manifest = (
        manifest_path.parent == PRIVATE_ROOT / "snapshot-manifests"
        and expected_root.parent == PRIVATE_ROOT
    )
    if not embedded_manifest and not external_manifest:
        raise CleanupError(
            f"snapshot manifest is outside its directory: {manifest_path}"
        )
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CleanupError(
            f"snapshot manifest hash does not match its journal: {manifest_path}"
        )
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CleanupError(f"snapshot manifest is invalid: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise CleanupError(f"snapshot manifest is not an object: {manifest_path}")
    if (
        manifest.get("version") != 1
        or manifest.get("metadata_contract") != "path-kind-content-mode"
        or manifest.get("snapshot_id") != expected_snapshot_id
        or manifest.get("snapshot_root") != str(expected_root)
        or not isinstance(manifest.get("entries"), list)
        or not manifest["entries"]
        or raw != canonical_json_bytes(manifest)
    ):
        raise CleanupError(
            f"snapshot manifest identity is invalid: {manifest_path}"
        )
    if (
        embedded_manifest
        and not canonical_child(expected_root, manifest_path.parent)
    ) or (
        external_manifest
        and not canonical_child(expected_root, PRIVATE_ROOT)
    ):
        raise CleanupError(f"snapshot data root is unsafe: {expected_root}")
    if verify_payload:
        verify_snapshot_payload(
            manifest,
            root=expected_root,
            manifest_bytes=raw,
        )
    return manifest_path.stat().st_mtime


def inspect_snapshot(
    path: Path,
    journals: dict[str, dict[str, Any]],
    bootstraps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not SNAPSHOT_NAME.fullmatch(path.name) or not canonical_child(
        path,
        PRIVATE_ROOT,
    ):
        raise CleanupError(f"snapshot directory is outside the allowlist: {path}")
    opened = require_owned_directory(path, label="snapshot directory")
    manifest_path = path / "manifest.json"
    require_private_regular_file(manifest_path, label="snapshot manifest")
    manifest = read_json_file(manifest_path, label="snapshot manifest")
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise CleanupError(f"snapshot identity is invalid: {path}")
    journal = journals.get(snapshot_id)
    bootstrap = bootstraps.get(snapshot_id)
    if journal is None and bootstrap is None:
        logical, allocated = tree_accounting(path)
        return {
            "path": str(path),
            "snapshot_id": snapshot_id,
            "terminal_kind": None,
            "newest_timestamp": max(
                opened.st_mtime,
                manifest_path.stat().st_mtime,
            ),
            "logical_bytes": logical,
            "allocated_bytes": allocated,
            "reason": "no-authoritative-transaction-journal",
        }

    terminal_kind = combined_terminal_kind(
        bootstrap_phases=bootstrap["phases"] if bootstrap else None,
        managed_phases=journal["phases"] if journal else None,
        rollback_receipt=journal["rollback_receipt"] if journal else None,
    )
    if journal is not None and journal.get("_legacy_pre_split") is True:
        terminal_kind = None
    newest_timestamp = opened.st_mtime
    journal_paths: list[str] = []
    descriptors: list[dict[str, str]] = []
    expected_base_sha: str | None = None

    if bootstrap is not None:
        snapshot_receipt = bootstrap["phases"].get("snapshot_created")
        if not isinstance(snapshot_receipt, dict):
            if terminal_kind is not None:
                raise CleanupError(
                    f"terminal bootstrap has no snapshot receipt: "
                    f"{bootstrap['_path']}"
                )
            snapshot_receipt = {}
        expected_base_sha = snapshot_receipt.get("state_snapshot_sha256")
        if snapshot_receipt and (
            snapshot_receipt.get("status") != "created"
            or snapshot_receipt.get("state_snapshot_id") != snapshot_id
            or snapshot_receipt.get("manifest_path") != str(manifest_path)
        ):
            raise CleanupError(
                f"bootstrap snapshot receipt identity is invalid: "
                f"{bootstrap['_path']}"
            )
        newest_timestamp = max(
            newest_timestamp,
            float(bootstrap["_mtime"]),
        )
        journal_paths.append(str(bootstrap["_path"]))

    if journal is not None:
        receipt = journal["rollback_receipt"]
        managed_sha = receipt.get("state_snapshot_sha256")
        if (
            receipt.get("state_snapshot_id") != snapshot_id
            or not HEX64.fullmatch(str(managed_sha or ""))
        ):
            raise CleanupError(
                f"managed snapshot receipt is invalid: {journal['_path']}"
            )
        if expected_base_sha is None:
            expected_base_sha = managed_sha
        elif expected_base_sha != managed_sha:
            raise CleanupError(
                f"bootstrap and managed snapshot receipts disagree: "
                f"{journal['_path']}"
            )
        newest_timestamp = max(newest_timestamp, float(journal["_mtime"]))
        journal_paths.append(str(journal["_path"]))

    if not HEX64.fullmatch(str(expected_base_sha or "")):
        raise CleanupError(f"base snapshot receipt is invalid: {path}")
    base_mtime = validate_manifest(
        manifest_path,
        path / "data",
        expected_snapshot_id=snapshot_id,
        expected_sha256=str(expected_base_sha),
        verify_payload=False,
    )
    newest_timestamp = max(newest_timestamp, base_mtime)
    descriptors.append(
        {
            "manifest_path": str(manifest_path),
            "payload_path": str(path / "data"),
            "snapshot_id": snapshot_id,
            "manifest_sha256": str(expected_base_sha),
        }
    )

    paired = (
        journal["phases"].get("paired_state_snapshot_created")
        if journal is not None
        else None
    )
    if paired is not None:
        if not isinstance(paired, dict):
            raise CleanupError(
                f"paired snapshot receipt is invalid: {journal['_path']}"
            )
        paired_manifest_raw = paired.get("manifest_path")
        paired_sha = paired.get("state_snapshot_sha256")
        if (
            not isinstance(paired_manifest_raw, str)
            or not HEX64.fullmatch(str(paired_sha or ""))
        ):
            raise CleanupError(
                f"paired snapshot receipt is incomplete: {journal['_path']}"
            )
        paired_manifest = Path(paired_manifest_raw)
        if not paired_manifest.is_absolute() or paired_manifest.parent != path:
            raise CleanupError(
                f"paired snapshot manifest is outside its directory: "
                f"{paired_manifest}"
            )
        if paired_manifest == manifest_path:
            if paired_sha != expected_base_sha:
                raise CleanupError(
                    f"adopted paired snapshot hash disagrees with base: "
                    f"{journal['_path']}"
                )
        else:
            paired_root = path / f"data.paired-{snapshot_id}"
            paired_mtime = validate_manifest(
                paired_manifest,
                paired_root,
                expected_snapshot_id=snapshot_id,
                expected_sha256=str(paired_sha),
                verify_payload=False,
            )
            newest_timestamp = max(newest_timestamp, paired_mtime)
            descriptors.append(
                {
                    "manifest_path": str(paired_manifest),
                    "payload_path": str(paired_root),
                    "snapshot_id": snapshot_id,
                    "manifest_sha256": str(paired_sha),
                }
            )

    logical = 0
    allocated = 0
    for descriptor in descriptors:
        payload_logical, payload_allocated = tree_accounting(
            Path(descriptor["payload_path"])
        )
        logical += payload_logical
        allocated += payload_allocated
    return {
        "path": str(path),
        "snapshot_id": snapshot_id,
        "journal_paths": sorted(journal_paths),
        "payload_descriptors": descriptors,
        "payload_paths": [
            descriptor["payload_path"] for descriptor in descriptors
        ],
        "terminal_kind": terminal_kind,
        "newest_timestamp": newest_timestamp,
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "root_device": opened.st_dev,
        "root_inode": opened.st_ino,
        "reason": None if terminal_kind else "nonterminal-transaction",
    }


def select_rolling_candidates(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload_rows = [
        row
        for row in rows
        if row.get("payload_paths")
        and isinstance(row.get("newest_timestamp"), (int, float))
    ]
    terminal = sorted(
        (row for row in payload_rows if row.get("terminal_kind")),
        key=lambda row: (
            float(row["newest_timestamp"]),
            str(row["path"]),
        ),
        reverse=True,
    )
    if not terminal:
        raise CleanupError(
            "cleanup requires one verified terminal rollback payload"
        )
    retained = str(terminal[0]["path"])
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("payload_paths"):
            continue
        if str(row["path"]) == retained:
            row["reason"] = "previous-rollback"
        elif row.get("terminal_kind"):
            row["reason"] = "superseded-terminal"
            candidates.append(row)
        elif row.get("reason") == "nonterminal-transaction":
            row["reason"] = "abandoned-nonterminal"
            candidates.append(row)
        else:
            raise CleanupError(
                f"snapshot payload classification is unsafe: {row['path']}"
            )
    return candidates


def scan(now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    journals, bootstraps = load_journals()
    rows: list[dict[str, Any]] = []
    for path in sorted(PRIVATE_ROOT.iterdir()):
        if path.name.startswith("snapshots-"):
            try:
                rows.append(inspect_snapshot(path, journals, bootstraps))
            except Exception as exc:
                rows.append(
                    {
                        "path": str(path),
                        "terminal_kind": None,
                        "reason": (
                            f"validation-failed:{type(exc).__name__}:{exc}"
                        ),
                    }
                )
    candidates = select_rolling_candidates(rows)
    for row in rows:
        timestamp = row.get("newest_timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        row["age_seconds"] = now - float(timestamp)
        row["timestamp_utc"] = dt.datetime.fromtimestamp(
            float(timestamp),
            tz=dt.timezone.utc,
        ).isoformat()
    return rows, candidates


def verify_candidate(row: dict[str, Any]) -> None:
    source = Path(row["path"])
    opened = require_owned_directory(source, label="candidate snapshot directory")
    if (
        opened.st_dev != row["root_device"]
        or opened.st_ino != row["root_inode"]
    ):
        raise CleanupError(f"candidate snapshot identity changed: {source}")
    for descriptor in row["payload_descriptors"]:
        validate_manifest(
            Path(descriptor["manifest_path"]),
            Path(descriptor["payload_path"]),
            expected_snapshot_id=descriptor["snapshot_id"],
            expected_sha256=descriptor["manifest_sha256"],
            verify_payload=True,
        )


def open_lock(path: Path, *, label: str):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CleanupError(f"{label} cannot be opened: {path}") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise CleanupError(f"{label} is unsafe: {path}")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise CleanupError(f"{label} is busy: {path}") from exc
    return handle


def open_all_transaction_locks() -> list[Any]:
    handles: list[Any] = []
    try:
        journal_paths = sorted(
            [
                *TRANSACTIONS_ROOT.glob("release*.json"),
                *TRANSACTIONS_ROOT.glob("release*.json.bootstrap"),
            ],
            key=str,
        )
        expected_locks = {
            Path(f"{journal_path}.lock") for journal_path in journal_paths
        }
        actual_locks = set(TRANSACTIONS_ROOT.glob("release*.lock"))
        if not expected_locks <= actual_locks:
            missing = sorted(str(path) for path in expected_locks - actual_locks)
            raise CleanupError(
                "transaction journal locks are missing: " + ",".join(missing)
            )
        for lock_path in sorted(actual_locks, key=str):
            if not canonical_child(lock_path, TRANSACTIONS_ROOT):
                raise CleanupError(
                    f"transaction lock escapes its root: {lock_path}"
                )
            handles.append(
                open_lock(lock_path, label="transaction journal lock")
            )
    except Exception:
        release_locks(handles)
        raise
    return handles


def release_locks(handles: list[Any]) -> None:
    for handle in reversed(handles):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def validate_selector_state(
    state: dict[str, Any],
    *,
    expected_release_root: str,
) -> dict[str, Any]:
    if set(state) != {
        "version",
        "generation",
        "release_root",
        "releases",
        "current",
        "last_good",
        "candidate",
        "pending_transaction_id",
        "bootstrap_fallback",
    }:
        raise CleanupError("selector state schema is invalid")
    releases = state.get("releases")
    if (
        state.get("version") != 2
        or not isinstance(state.get("generation"), int)
        or state["generation"] < 0
        or state.get("release_root") != expected_release_root
        or not isinstance(releases, dict)
        or not releases
        or state.get("candidate") is not None
        or state.get("pending_transaction_id") is not None
    ):
        raise CleanupError("selector state is not idle and valid")
    for key in ("current", "last_good", "bootstrap_fallback"):
        value = state.get(key)
        if not isinstance(value, str) or value not in releases:
            raise CleanupError(f"selector reference is invalid: {key}")
    if state["current"] == state["last_good"]:
        raise CleanupError("selector current and last-good releases must differ")
    for build_id, record in releases.items():
        if (
            not isinstance(build_id, str)
            or not build_id
            or not isinstance(record, dict)
            or not HEX64.fullmatch(str(record.get("manifest_sha256") or ""))
            or not re.fullmatch(
                r"[0-9a-f]{40,64}",
                str(record.get("commit") or ""),
            )
            or not re.fullmatch(
                r"[0-9a-f]{40,64}",
                str(record.get("tree") or ""),
            )
        ):
            raise CleanupError(
                f"selector release record is invalid: {build_id}"
            )
    return {
        "version": state["version"],
        "generation": state["generation"],
        "release_root": state["release_root"],
        "current": state["current"],
        "last_good": state["last_good"],
        "candidate": state["candidate"],
        "pending_transaction_id": state["pending_transaction_id"],
        "bootstrap_fallback": state["bootstrap_fallback"],
        "release_count": len(releases),
    }


def selector_state_under_lock() -> dict[str, Any]:
    require_private_regular_file(SELECTOR_STATE, label="selector state")
    state = read_json_file(SELECTOR_STATE, label="selector state")
    return validate_selector_state(
        state,
        expected_release_root=str(SELECTOR_RELEASES),
    )


def lsof_open_rows(path: Path) -> list[str]:
    lsof = shutil.which("lsof")
    if lsof is None:
        raise CleanupError("lsof is unavailable")
    result = subprocess.run(
        [lsof, "+D", str(path)] if path.is_dir() else [lsof, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return []
    if result.returncode == 0:
        return result.stdout.splitlines()
    raise CleanupError(
        f"lsof failed for {path}: "
        f"{result.stderr.strip() or result.returncode}"
    )


def create_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_owned_directory(
        path.parent,
        label="cleanup receipt directory",
        exact_mode=0o700,
    )
    raw = canonical_json_bytes(payload)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def atomic_receipt(path: Path, payload: dict[str, Any]) -> None:
    require_private_regular_file(path, label="cleanup receipt")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    raw = canonical_json_bytes(payload)
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_sealed_tree(path: Path) -> None:
    opened = path.lstat()
    if not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
        raise CleanupError(f"sealed tree root is unsafe: {path}")
    for root, directories, filenames in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(root)
        root_stat = root_path.lstat()
        if stat.S_ISLNK(root_stat.st_mode):
            raise CleanupError(f"sealed tree contains a symlink: {root_path}")
        os.chmod(root_path, 0o700)
        for directory in directories:
            child = root_path / directory
            child_stat = child.lstat()
            if not stat.S_ISDIR(child_stat.st_mode) or stat.S_ISLNK(
                child_stat.st_mode
            ):
                raise CleanupError(
                    f"sealed tree contains an unsafe directory: {child}"
                )
            os.chmod(child, 0o700)
        for filename in filenames:
            child = root_path / filename
            child_stat = child.lstat()
            if not stat.S_ISREG(child_stat.st_mode) or stat.S_ISLNK(
                child_stat.st_mode
            ):
                raise CleanupError(
                    f"sealed tree contains an unsafe file: {child}"
                )
            os.chmod(child, 0o600)
    shutil.rmtree(path)


def plan_digest(
    selector: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    payload = {
        "version": 1,
        "selector": selector,
        "policy": {
            "retain_terminal_count": RETAIN_TERMINAL_COUNT,
            "protect_nonterminal": False,
            "delete_abandoned_nonterminal_when_selector_idle": True,
            "require_all_companion_journals_valid": True,
        },
        "candidates": [
            {
                "path": row["path"],
                "snapshot_id": row["snapshot_id"],
                "terminal_kind": row["terminal_kind"],
                "root_device": row["root_device"],
                "root_inode": row["root_inode"],
                "allocated_bytes": row["allocated_bytes"],
                "payload_descriptors": row["payload_descriptors"],
            }
            for row in candidates
        ],
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def verify_candidates(
    candidates: list[dict[str, Any]],
) -> None:
    for row in candidates:
        verify_candidate(row)
        open_rows = lsof_open_rows(Path(row["path"]))
        if open_rows:
            raise CleanupError(
                f"candidate snapshot has open files: {row['path']}"
            )
        row["open_file_row_count"] = 0
        row["payload_verified"] = True


def recover_quarantine(
    receipt_path: Path,
    receipt: dict[str, Any],
) -> None:
    quarantine = Path(str(receipt.get("quarantine_path") or ""))
    if (
        not quarantine.is_absolute()
        or quarantine.parent != PRIVATE_ROOT
        or not quarantine.name.startswith(".snapshot-cleanup-quarantine-")
    ):
        raise CleanupError(
            f"incomplete cleanup has an unsafe quarantine path: {receipt_path}"
        )
    operations = receipt.get("operations")
    if not isinstance(operations, list):
        raise CleanupError(
            f"incomplete cleanup has invalid operations: {receipt_path}"
        )
    state = receipt.get("status")
    if state in {"deleting", "deletion-failed"}:
        _reseal_snapshot_roots(receipt)
        for operation in operations:
            destination = Path(operation["destination"])
            source = Path(operation["source"])
            if destination.exists():
                opened = destination.lstat()
                if (
                    opened.st_dev != operation["device"]
                    or opened.st_ino != operation["inode"]
                    or not canonical_child(destination, quarantine)
                ):
                    raise CleanupError(
                        f"quarantined payload identity changed: {destination}"
                    )
                remove_sealed_tree(destination)
                fsync_directory(quarantine)
            elif source.exists():
                raise CleanupError(
                    f"deletion-phase payload unexpectedly returned: {source}"
                )
            operation["state"] = "deleted"
            atomic_receipt(receipt_path, receipt)
        if quarantine.exists():
            quarantine.rmdir()
            fsync_directory(PRIVATE_ROOT)
        receipt["status"] = "completed-after-resume"
        receipt["resumed_at_utc"] = dt.datetime.now(
            tz=dt.timezone.utc
        ).isoformat()
        atomic_receipt(receipt_path, receipt)
        return
    root_records = _snapshot_root_records(receipt)
    for operation in reversed(operations):
        source = Path(operation["source"])
        destination = Path(operation["destination"])
        snapshot_root = Path(operation["snapshot_path"])
        root_record = root_records.get(snapshot_root)
        if root_record is None:
            raise CleanupError(
                f"cleanup operation has no snapshot root record: {source}"
            )
        _open_snapshot_root_for_recovery(snapshot_root, root_record)
        source_exists = source.exists()
        destination_exists = destination.exists()
        if destination_exists and not source_exists:
            opened = destination.lstat()
            if (
                opened.st_dev != operation["device"]
                or opened.st_ino != operation["inode"]
                or not canonical_child(destination, quarantine)
                or source.parent != Path(operation["snapshot_path"])
            ):
                raise CleanupError(
                    f"quarantined payload identity changed: {destination}"
                )
            os.replace(destination, source)
            fsync_directory(source.parent)
            fsync_directory(quarantine)
        elif source_exists and not destination_exists:
            opened = source.lstat()
            if (
                opened.st_dev != operation["device"]
                or opened.st_ino != operation["inode"]
            ):
                raise CleanupError(
                    f"restored payload identity changed: {source}"
                )
        elif source_exists and destination_exists:
            raise CleanupError(
                f"cleanup recovery found both payload copies: {source}"
            )
        else:
            raise CleanupError(
                f"cleanup recovery lost both payload copies: {source}"
            )
        payload_mode = operation.get("mode")
        if not isinstance(payload_mode, int) or payload_mode & 0o022:
            raise CleanupError(
                f"cleanup payload mode is invalid: {source}"
            )
        os.chmod(source, payload_mode)
        operation["state"] = "restored"
        atomic_receipt(receipt_path, receipt)
    _reseal_snapshot_roots(receipt)
    if quarantine.exists():
        quarantine.rmdir()
        fsync_directory(PRIVATE_ROOT)
    receipt["status"] = "failed-recovered"
    receipt["recovered_at_utc"] = dt.datetime.now(
        tz=dt.timezone.utc
    ).isoformat()
    atomic_receipt(receipt_path, receipt)


def recover_incomplete_cleanups() -> None:
    quarantines = {
        path.resolve(strict=True): path
        for path in PRIVATE_ROOT.glob(".snapshot-cleanup-quarantine-*")
        if path.is_dir() and not path.is_symlink()
    }
    referenced: set[Path] = set()
    if RECEIPTS_ROOT.exists():
        require_owned_directory(
            RECEIPTS_ROOT,
            label="cleanup receipt directory",
            exact_mode=0o700,
        )
        for receipt_path in sorted(
            RECEIPTS_ROOT.glob("terminal-snapshot-cleanup-*.json")
        ):
            require_private_regular_file(
                receipt_path,
                label="cleanup receipt",
            )
            receipt = read_json_file(
                receipt_path,
                label="cleanup receipt",
            )
            if receipt.get("status") in {
                "completed",
                "completed-after-resume",
                "failed-recovered",
                "dry-run",
            }:
                continue
            quarantine = Path(str(receipt.get("quarantine_path") or ""))
            if quarantine.exists():
                referenced.add(quarantine.resolve(strict=True))
            recover_quarantine(receipt_path, receipt)
    orphaned = set(quarantines) - referenced
    if orphaned:
        raise CleanupError(
            "orphaned cleanup quarantines require inspection: "
            + ",".join(str(quarantines[path]) for path in sorted(orphaned))
        )


def prepare_inventory(
    now: float,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
]:
    selector = selector_state_under_lock()
    rows, candidates = scan(now)
    verify_candidates(candidates)
    digest = plan_digest(selector, candidates)
    return selector, rows, candidates, digest


def apply_cleanup(expected_plan_sha256: str) -> dict[str, Any]:
    if not HEX64.fullmatch(expected_plan_sha256):
        raise CleanupError("an exact dry-run plan digest is required")
    started = time.time()
    stamp = (
        dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + f"-{os.getpid()}"
    )
    receipt_path = (
        RECEIPTS_ROOT / f"terminal-snapshot-cleanup-{stamp}.json"
    )
    quarantine = (
        PRIVATE_ROOT / f".snapshot-cleanup-quarantine-{stamp}"
    )
    disk_before = shutil.disk_usage(PRIVATE_ROOT).free
    receipt: dict[str, Any] = {
        "version": 3,
        "mode": "apply",
        "status": "starting",
        "started_at_utc": dt.datetime.fromtimestamp(
            started,
            tz=dt.timezone.utc,
        ).isoformat(),
        "expected_plan_sha256": expected_plan_sha256,
        "policy": {
            "retain_terminal_count": RETAIN_TERMINAL_COUNT,
            "protect_nonterminal": False,
            "delete_abandoned_nonterminal_when_selector_idle": True,
            "require_all_companion_journals_valid": True,
        },
        "disk_free_before_bytes": disk_before,
        "quarantine_path": str(quarantine),
        "snapshot_roots": [],
        "operations": [],
        "deleted": [],
        "errors": [],
    }

    selector_handle = open_lock(SELECTOR_LOCK, label="selector lock")
    transaction_handles: list[Any] = []
    receipt_created = False
    try:
        transaction_handles = open_all_transaction_locks()
        recover_incomplete_cleanups()
        selector, rows, candidates, digest = prepare_inventory(started)
        if digest != expected_plan_sha256:
            raise CleanupError(
                f"dry-run plan changed: expected {expected_plan_sha256}, "
                f"found {digest}"
            )
        receipt["selector_before"] = selector
        receipt["inventory"] = rows
        receipt["plan_sha256"] = digest
        receipt["planned_candidates"] = [
            row["path"] for row in candidates
        ]
        receipt["status"] = "planned"
        create_receipt(receipt_path, receipt)
        receipt_created = True
        if not candidates:
            receipt["status"] = "completed"
            receipt["selector_after"] = selector_state_under_lock()
            receipt["finished_at_utc"] = dt.datetime.now(
                tz=dt.timezone.utc
            ).isoformat()
            receipt["disk_free_after_bytes"] = shutil.disk_usage(
                PRIVATE_ROOT
            ).free
            receipt["disk_free_delta_bytes"] = (
                receipt["disk_free_after_bytes"]
                - receipt["disk_free_before_bytes"]
            )
            receipt["receipt_path"] = str(receipt_path)
            atomic_receipt(receipt_path, receipt)
            return receipt

        quarantine.mkdir(mode=0o700)
        require_owned_directory(
            quarantine,
            label="cleanup quarantine",
            exact_mode=0o700,
        )
        if not canonical_child(quarantine, PRIVATE_ROOT):
            raise CleanupError("quarantine directory is unsafe")
        fsync_directory(PRIVATE_ROOT)
        receipt["status"] = "quarantining"
        atomic_receipt(receipt_path, receipt)

        for planned in candidates:
            source = Path(planned["path"])
            current_stat = require_owned_directory(
                source,
                label="candidate snapshot directory",
            )
            if (
                current_stat.st_dev != planned["root_device"]
                or current_stat.st_ino != planned["root_inode"]
            ):
                raise CleanupError(
                    f"candidate snapshot identity changed: {source}"
                )
            root_record = _directory_identity_record(source, current_stat)
            receipt["snapshot_roots"].append(root_record)
            atomic_receipt(receipt_path, receipt)
            if lsof_open_rows(source):
                raise CleanupError(f"snapshot has open files: {source}")
            for payload_raw in planned["payload_paths"]:
                payload = Path(payload_raw)
                if (
                    payload.parent != source
                    or (
                        payload.name != "data"
                        and not payload.name.startswith("data.paired-")
                    )
                    or not canonical_child(payload, source)
                    or payload.is_symlink()
                ):
                    raise CleanupError(
                        f"snapshot payload is unsafe: {payload}"
                    )
                payload_stat = payload.lstat()
                destination = (
                    quarantine / f"{source.name}--{payload.name}"
                )
                if destination.exists() or destination.is_symlink():
                    raise CleanupError(
                        f"quarantine target already exists: {destination}"
                    )
                operation = {
                    "snapshot_path": str(source),
                    "source": str(payload),
                    "destination": str(destination),
                    "device": payload_stat.st_dev,
                    "inode": payload_stat.st_ino,
                    "uid": payload_stat.st_uid,
                    "mode": stat.S_IMODE(payload_stat.st_mode),
                    "state": "intent-recorded",
                }
                receipt["operations"].append(operation)
                atomic_receipt(receipt_path, receipt)
                opened = open_snapshot_payload_for_quarantine(
                    source,
                    payload,
                    root_record,
                )
                if (
                    opened["payload"]["device"] != operation["device"]
                    or opened["payload"]["inode"] != operation["inode"]
                    or opened["payload"]["original_mode"]
                    != operation["mode"]
                ):
                    raise CleanupError(
                        f"payload identity changed before quarantine: {payload}"
                    )
                operation["state"] = "opened"
                atomic_receipt(receipt_path, receipt)
                check_stat = payload.lstat()
                if (
                    check_stat.st_dev != operation["device"]
                    or check_stat.st_ino != operation["inode"]
                ):
                    raise CleanupError(
                        f"payload identity changed after intent: {payload}"
                    )
                os.replace(payload, destination)
                fsync_directory(source)
                fsync_directory(quarantine)
                operation["state"] = "quarantined"
                atomic_receipt(receipt_path, receipt)
            reseal_snapshot_root(source, root_record)
            atomic_receipt(receipt_path, receipt)

        receipt["status"] = "deleting"
        atomic_receipt(receipt_path, receipt)
        for operation in receipt["operations"]:
            destination = Path(operation["destination"])
            opened = destination.lstat()
            if (
                not canonical_child(destination, quarantine)
                or stat.S_ISLNK(opened.st_mode)
                or opened.st_dev != operation["device"]
                or opened.st_ino != operation["inode"]
            ):
                raise CleanupError(
                    f"quarantined payload is unsafe: {destination}"
                )
            remove_sealed_tree(destination)
            fsync_directory(quarantine)
            operation["state"] = "deleted"
            receipt["deleted"].append(operation["source"])
            atomic_receipt(receipt_path, receipt)

        quarantine.rmdir()
        fsync_directory(PRIVATE_ROOT)
        selector_after = selector_state_under_lock()
        if selector_after != selector:
            raise CleanupError("selector state changed during cleanup")
        receipt["selector_after"] = selector_after
        receipt["disk_free_after_bytes"] = shutil.disk_usage(
            PRIVATE_ROOT
        ).free
        receipt["disk_free_delta_bytes"] = (
            receipt["disk_free_after_bytes"]
            - receipt["disk_free_before_bytes"]
        )
        receipt["finished_at_utc"] = dt.datetime.now(
            tz=dt.timezone.utc
        ).isoformat()
        receipt["status"] = "completed"
        receipt["receipt_path"] = str(receipt_path)
        atomic_receipt(receipt_path, receipt)
        return receipt
    except Exception as exc:
        if receipt_created:
            receipt["errors"].append(
                {"error": f"{type(exc).__name__}:{exc}"}
            )
            if receipt.get("status") == "quarantining":
                receipt["status"] = "quarantine-failed"
                atomic_receipt(receipt_path, receipt)
                try:
                    recover_quarantine(receipt_path, receipt)
                except Exception as recovery_exc:
                    receipt["status"] = "recovery-failed"
                    receipt["errors"].append(
                        {
                            "error": (
                                f"{type(recovery_exc).__name__}:"
                                f"{recovery_exc}"
                            )
                        }
                    )
                    atomic_receipt(receipt_path, receipt)
            elif receipt.get("status") == "deleting":
                receipt["status"] = "deletion-failed"
                atomic_receipt(receipt_path, receipt)
        raise
    finally:
        release_locks(transaction_handles)
        fcntl.flock(selector_handle.fileno(), fcntl.LOCK_UN)
        selector_handle.close()


def dry_run() -> dict[str, Any]:
    now = time.time()
    selector_handle = open_lock(SELECTOR_LOCK, label="selector lock")
    transaction_handles: list[Any] = []
    try:
        transaction_handles = open_all_transaction_locks()
        recover_incomplete_cleanups()
        selector, rows, candidates, digest = prepare_inventory(now)
    finally:
        release_locks(transaction_handles)
        fcntl.flock(selector_handle.fileno(), fcntl.LOCK_UN)
        selector_handle.close()
    return {
        "version": 2,
        "mode": "dry-run",
        "status": "verified",
        "now_utc": dt.datetime.fromtimestamp(
            now,
            tz=dt.timezone.utc,
        ).isoformat(),
        "selector": selector,
        "policy": {
            "retain_terminal_count": RETAIN_TERMINAL_COUNT,
            "protect_nonterminal": False,
            "delete_abandoned_nonterminal_when_selector_idle": True,
            "require_all_companion_journals_valid": True,
        },
        "inventory": rows,
        "candidate_count": len(candidates),
        "candidate_allocated_bytes": sum(
            int(row["allocated_bytes"]) for row in candidates
        ),
        "plan_sha256": digest,
        "candidates": candidates,
    }


def _accepted_release_journal(
    transaction_id: str,
    expected_current_build: str,
) -> dict[str, Any]:
    if release_selector._TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise CleanupError("accepted release transaction identity is invalid")
    journals, _bootstraps = load_journals()
    matches = [
        journal
        for journal in journals.values()
        if journal["transaction_id"] == transaction_id
    ]
    if len(matches) != 1:
        raise CleanupError("accepted release transaction journal is not exact")
    journal = matches[0]
    expected = journal["expected_candidate_identity"]
    if expected.get("build_id") != expected_current_build:
        raise CleanupError("accepted release current build is not journal-bound")
    phases = journal["phases"]
    if "pair_accepted" not in phases and "candidate_accepted" not in phases:
        raise CleanupError("release transaction is not accepted")
    return journal


def _rolling_release_plan(
    *,
    accepted_transaction_id: str,
    expected_current_build: str,
) -> dict[str, Any]:
    journal = _accepted_release_journal(
        accepted_transaction_id,
        expected_current_build,
    )
    state = release_selector.read_selector_state(
        SELECTOR_STATE,
        lock_path=SELECTOR_LOCK,
    )
    if state["current"] != expected_current_build:
        raise CleanupError("selector current build changed after acceptance")
    current = state["current"]
    last_good = state["last_good"]
    protected_builds = {current, last_good}
    verified = {}
    for build_id in (current, last_good):
        record = state["releases"][build_id]
        try:
            identity = release_selector.verify_release(
                SELECTOR_RELEASES / build_id,
                release_root=SELECTOR_RELEASES,
                expected_manifest_sha256=record["manifest_sha256"],
                selector_path=None,
                verify_selector_identity=False,
            )
        except release_selector.SelectorError as exc:
            raise CleanupError(
                f"protected release manifest is invalid: {build_id}: {exc}"
            ) from exc
        if (
            identity["commit"] != record["commit"]
            or identity["tree"] != record["tree"]
        ):
            raise CleanupError(
                f"protected release record changed: {build_id}"
            )
        verified[build_id] = identity

    candidates: list[dict[str, Any]] = []
    unknown: list[str] = []
    for build_id in sorted(set(state["releases"]) - protected_builds):
        path = SELECTOR_RELEASES / build_id
        record = state["releases"][build_id]
        try:
            identity = release_selector.verify_release(
                path,
                release_root=SELECTOR_RELEASES,
                expected_manifest_sha256=record["manifest_sha256"],
                selector_path=None,
                verify_selector_identity=False,
            )
        except release_selector.SelectorError as exc:
            raise CleanupError(
                f"obsolete release manifest is invalid: {build_id}: {exc}"
            ) from exc
        if (
            identity["commit"] != record["commit"]
            or identity["tree"] != record["tree"]
        ):
            raise CleanupError(f"obsolete release record changed: {build_id}")
        opened = path.stat()
        candidates.append(
            {
                "kind": "webui-release",
                "path": str(path),
                "device": opened.st_dev,
                "inode": opened.st_ino,
            }
        )
    for path in sorted(SELECTOR_RELEASES.iterdir(), key=lambda item: item.name):
        if path.name in state["releases"]:
            continue
        manifest_path = path / release_selector.MANIFEST_NAME
        if path.is_dir() and not path.is_symlink() and manifest_path.is_file():
            try:
                manifest_sha256 = sha256_file(manifest_path)
                identity = release_selector.verify_release(
                    path,
                    release_root=SELECTOR_RELEASES,
                    expected_manifest_sha256=manifest_sha256,
                    selector_path=None,
                    verify_selector_identity=False,
                )
            except (OSError, release_selector.SelectorError):
                unknown.append(str(path))
                continue
            if identity["build_id"] != path.name:
                unknown.append(str(path))
                continue
            opened = path.stat()
            candidates.append(
                {
                    "kind": "orphan-webui-release",
                    "path": str(path),
                    "device": opened.st_dev,
                    "inode": opened.st_ino,
                }
            )
        else:
            unknown.append(str(path))

    protected_artifacts = {
        Path(identity[field])
        for identity in verified.values()
        for field in (
            "agent_source_path",
            "agent_source_manifest_path",
            "runtime_path",
            "runtime_manifest_path",
        )
    }
    artifact_roots = {
        Path(identity[field]).parent.parent
        for identity in verified.values()
        for field in ("agent_source_path", "runtime_path")
    }
    agent_roots = {
        Path(identity["agent_source_path"]).parent.parent
        for identity in verified.values()
    }
    runtime_roots = {
        Path(identity["runtime_path"]).parent.parent
        for identity in verified.values()
    }
    if len(agent_roots) != 1 or len(runtime_roots) != 1:
        raise CleanupError("protected artifact roots are not exact")
    if agent_roots & runtime_roots:
        raise CleanupError("agent and runtime artifact roots are not distinct")
    for root in sorted(artifact_roots, key=str):
        artifact_kind = "agent" if root in agent_roots else "runtime"
        snapshots = root / "snapshots"
        manifests = root / "manifests"
        require_owned_directory(snapshots, label="artifact snapshots root")
        require_owned_directory(manifests, label="artifact manifests root")
        for snapshot in sorted(snapshots.iterdir(), key=lambda item: item.name):
            manifest = manifests / f"{snapshot.name}.json"
            if snapshot in protected_artifacts:
                continue
            if (
                HEX64.fullmatch(snapshot.name) is None
                or not snapshot.is_dir()
            ):
                unknown.append(str(snapshot))
                continue
            if snapshot.is_symlink() or manifest.is_symlink():
                raise CleanupError(
                    f"recognized artifact is symlinked: {snapshot}"
                )
            if not manifest.is_file():
                unknown.append(str(snapshot))
                continue
            try:
                raw_manifest = manifest.read_bytes()
                parsed_manifest = json.loads(raw_manifest)
                if (
                    not isinstance(parsed_manifest, dict)
                    or hashlib.sha256(raw_manifest).hexdigest()
                    != snapshot.name
                ):
                    raise ValueError("manifest identity mismatch")
                if artifact_kind == "agent":
                    release_selector.verify_agent_source(
                        {
                            "path": str(snapshot),
                            "resolved_path": str(snapshot.resolve(strict=True)),
                            "commit": parsed_manifest.get("commit"),
                            "tree": parsed_manifest.get("tree"),
                            "manifest_path": str(manifest),
                            "manifest_sha256": snapshot.name,
                        }
                    )
                else:
                    interpreter_relative = parsed_manifest.get(
                        "interpreter_relative_path"
                    )
                    site_relative = parsed_manifest.get(
                        "site_packages_relative_path"
                    )
                    interpreter = snapshot / str(interpreter_relative)
                    release_selector.verify_runtime(
                        {
                            "path": str(snapshot),
                            "resolved_path": str(snapshot.resolve(strict=True)),
                            "python_home_path": str(snapshot / "python-home"),
                            "site_packages_path": str(snapshot / str(site_relative)),
                            "interpreter_path": str(interpreter),
                            "interpreter_resolved_path": str(
                                interpreter.resolve(strict=True)
                            ),
                            "manifest_path": str(manifest),
                            "manifest_sha256": snapshot.name,
                        }
                    )
            except (
                OSError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
                release_selector.SelectorError,
            ) as exc:
                raise CleanupError(
                    f"recognized {artifact_kind} artifact is malformed: "
                    f"{snapshot}"
                ) from exc
            opened_snapshot = snapshot.stat()
            opened_manifest = manifest.stat()
            if (
                opened_snapshot.st_uid != os.getuid()
                or opened_manifest.st_uid != os.getuid()
                or not stat.S_ISREG(opened_manifest.st_mode)
                or opened_manifest.st_nlink != 1
            ):
                raise CleanupError(
                    f"recognized artifact ownership is unsafe: {snapshot}"
                )
            candidates.extend(
                (
                    {
                        "kind": "artifact-snapshot",
                        "path": str(snapshot),
                        "device": opened_snapshot.st_dev,
                        "inode": opened_snapshot.st_ino,
                    },
                    {
                        "kind": "artifact-manifest",
                        "path": str(manifest),
                        "device": opened_manifest.st_dev,
                        "inode": opened_manifest.st_ino,
                    },
                )
            )
        for manifest in sorted(manifests.glob("*.json"), key=lambda item: item.name):
            snapshot = snapshots / manifest.stem
            if manifest in protected_artifacts or snapshot.exists():
                continue
            unknown.append(str(manifest))
    canonical = {
        "transaction_id": accepted_transaction_id,
        "expected_current_build": expected_current_build,
        "selector_generation": state["generation"],
        "selector_state_sha256": release_selector.selector_state_sha256(state),
        "current": current,
        "last_good": last_good,
        "bootstrap_fallback_identity": verified[last_good],
        "release_records": state["releases"],
        "protected_paths": sorted(
            str(path)
            for path in (
                *(SELECTOR_RELEASES / build for build in protected_builds),
                *protected_artifacts,
            )
        ),
        "candidates": candidates,
        "unknown_protected": sorted(set(unknown)),
        "journal_path": journal["_path"],
    }
    canonical["plan_sha256"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return canonical


def _apply_rolling_release_plan(plan: dict[str, Any]) -> dict[str, Any]:
    RECEIPTS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = (
        RECEIPTS_ROOT
        / f"rolling-release-cleanup-{plan['transaction_id']}.json"
    )
    operations = [
        {
            "source": candidate["path"],
            "destination": str(
                Path(candidate["path"]).with_name(
                    ".hermes-retention-quarantine-"
                    f"{plan['transaction_id']}-{index:04d}-"
                    f"{Path(candidate['path']).name}"
                )
            ),
            "device": candidate["device"],
            "inode": candidate["inode"],
            "state": "intent",
        }
        for index, candidate in enumerate(plan["candidates"])
    ]
    if receipt_path.exists():
        receipt = read_json_file(receipt_path, label="rolling cleanup receipt")
        if (
            receipt.get("version") != 1
            or receipt.get("plan") != plan
            or not isinstance(receipt.get("operations"), list)
            or len(receipt["operations"]) != len(operations)
        ):
            raise CleanupError("rolling cleanup receipt changed")
        for recorded, expected in zip(receipt["operations"], operations):
            if (
                not isinstance(recorded, dict)
                or any(
                    recorded.get(field) != expected[field]
                    for field in (
                        "source",
                        "destination",
                        "device",
                        "inode",
                    )
                )
                or recorded.get("state")
                not in {"intent", "quarantined", "deleted"}
            ):
                raise CleanupError("rolling cleanup operation changed")
        if receipt.get("status") == "completed":
            return receipt
    else:
        receipt = {
            "version": 1,
            "status": "planned",
            "plan": plan,
            "operations": operations,
        }
        create_receipt(receipt_path, receipt)
    resume_status = receipt.get("status")
    deleting_intent = resume_status == "deleting"
    if resume_status not in {
        "planned",
        "selector-pruned",
        "quarantining",
        "deleting",
    }:
        raise CleanupError("rolling cleanup receipt status is invalid")
    for candidate in plan["candidates"]:
        path = Path(candidate["path"])
        if not path.exists():
            continue
        if lsof_open_rows(path):
            raise CleanupError(
                f"rolling retention candidate has open files: {candidate['path']}"
            )
    current_state = release_selector.read_selector_state(
        SELECTOR_STATE, lock_path=SELECTOR_LOCK
    )
    if (
        current_state["generation"] == plan["selector_generation"]
        and release_selector.selector_state_sha256(current_state)
        == plan["selector_state_sha256"]
    ):
        pruned = release_selector.prune_idle_selector_releases(
            SELECTOR_STATE,
            lock_path=SELECTOR_LOCK,
            expected_generation=plan["selector_generation"],
            expected_state_sha256=plan["selector_state_sha256"],
            expected_current=plan["current"],
            expected_last_good=plan["last_good"],
        )
    elif (
        current_state["generation"] == plan["selector_generation"] + 1
        and current_state["current"] == plan["current"]
        and current_state["last_good"] == plan["last_good"]
        and current_state["bootstrap_fallback"] == plan["last_good"]
        and set(current_state["releases"])
        == {plan["current"], plan["last_good"]}
    ):
        pruned = current_state
    else:
        raise CleanupError("selector changed outside rolling cleanup")
    receipt["selector_after_prune"] = pruned
    if not deleting_intent:
        receipt["status"] = "selector-pruned"
        atomic_receipt(receipt_path, receipt)
        receipt["status"] = "quarantining"
        atomic_receipt(receipt_path, receipt)
        for operation in receipt["operations"]:
            source = Path(operation["source"])
            destination = Path(operation["destination"])
            if source.exists() and not destination.exists():
                opened = source.lstat()
                if (
                    stat.S_ISLNK(opened.st_mode)
                    or opened.st_dev != operation["device"]
                    or opened.st_ino != operation["inode"]
                ):
                    raise CleanupError(
                        f"rolling retention candidate changed: {source}"
                    )
                os.replace(source, destination)
            elif destination.exists() and not source.exists():
                opened = destination.lstat()
                if (
                    stat.S_ISLNK(opened.st_mode)
                    or opened.st_dev != operation["device"]
                    or opened.st_ino != operation["inode"]
                ):
                    raise CleanupError(
                        f"rolling quarantine identity changed: {destination}"
                    )
            elif source.exists() == destination.exists():
                raise CleanupError(
                    f"rolling quarantine state is ambiguous: {source}"
                )
            fsync_directory(source.parent)
            operation["state"] = "quarantined"
            atomic_receipt(receipt_path, receipt)
        receipt["status"] = "deleting"
        atomic_receipt(receipt_path, receipt)
    else:
        for operation in receipt["operations"]:
            if operation["state"] not in {"quarantined", "deleted"}:
                raise CleanupError(
                    "rolling deleting receipt has an unquarantined operation"
                )
    for operation in receipt["operations"]:
        source = Path(operation["source"])
        destination = Path(operation["destination"])
        source_exists = source.exists()
        destination_exists = destination.exists()
        if operation["state"] == "deleted":
            if source_exists or destination_exists:
                raise CleanupError(
                    f"rolling deleted operation reappeared: {source}"
                )
            continue
        if source_exists:
            raise CleanupError(
                f"rolling quarantined source reappeared: {source}"
            )
        if not destination.exists():
            operation["state"] = "deleted"
            atomic_receipt(receipt_path, receipt)
            continue
        if destination.is_dir():
            remove_sealed_tree(destination)
        else:
            destination.unlink()
        fsync_directory(destination.parent)
        operation["state"] = "deleted"
        atomic_receipt(receipt_path, receipt)
    final_state = release_selector.read_selector_state(
        SELECTOR_STATE,
        lock_path=SELECTOR_LOCK,
    )
    if (
        final_state["current"] != plan["current"]
        or final_state["last_good"] != plan["last_good"]
        or set(final_state["releases"])
        != {plan["current"], plan["last_good"]}
        or final_state["bootstrap_fallback"] != plan["last_good"]
    ):
        raise CleanupError("selector rolling-retention reverify failed")
    for build_id in (plan["current"], plan["last_good"]):
        record = final_state["releases"][build_id]
        release_selector.verify_release(
            SELECTOR_RELEASES / build_id,
            release_root=SELECTOR_RELEASES,
            expected_manifest_sha256=record["manifest_sha256"],
            selector_path=None,
            verify_selector_identity=False,
        )
    receipt["selector_final"] = final_state
    receipt["status"] = "completed"
    receipt["receipt_path"] = str(receipt_path)
    atomic_receipt(receipt_path, receipt)
    return receipt


def _accepted_snapshot_plan(
    *,
    accepted_transaction_id: str,
    expected_current_build: str,
) -> dict[str, Any]:
    journal = _accepted_release_journal(
        accepted_transaction_id,
        expected_current_build,
    )
    manifest_root = PRIVATE_ROOT / "snapshot-manifests"
    require_owned_directory(manifest_root, label="snapshot manifest root")
    expected: list[tuple[str, str]] = [
        (
            str(journal["rollback_receipt"]["state_snapshot_id"]),
            str(journal["rollback_receipt"]["state_snapshot_sha256"]),
        )
    ]
    paired = journal["phases"].get("paired_state_snapshot_created")
    if paired is not None:
        expected.append(
            (
                str(paired.get("state_snapshot_id") or ""),
                str(paired.get("state_snapshot_sha256") or ""),
            )
        )
    protected_roots: list[dict[str, Any]] = []
    protected_manifests: set[Path] = set()
    for snapshot_id, manifest_sha256 in expected:
        matches = [
            path
            for path in manifest_root.glob("manifest-*.json")
            if sha256_file(path) == manifest_sha256
        ]
        if len(matches) != 1:
            raise CleanupError(
                "accepted rollback snapshot manifest is not exact"
            )
        manifest_path = matches[0]
        manifest = read_json_file(
            manifest_path,
            label="accepted rollback snapshot manifest",
        )
        root = Path(str(manifest.get("snapshot_root") or ""))
        if (
            root.parent != PRIVATE_ROOT
            or not root.name.startswith("snapshots-")
            or root.is_symlink()
        ):
            raise CleanupError("accepted rollback snapshot root is unsafe")
        opened = require_owned_directory(
            root,
            label="accepted rollback snapshot root",
        )
        validate_manifest(
            manifest_path,
            root,
            expected_snapshot_id=snapshot_id,
            expected_sha256=manifest_sha256,
            verify_payload=True,
        )
        protected_roots.append(
            {
                "path": str(root),
                "device": opened.st_dev,
                "inode": opened.st_ino,
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha256,
            }
        )
        protected_manifests.add(manifest_path)
    if len({item["path"] for item in protected_roots}) != len(protected_roots):
        raise CleanupError("accepted rollback snapshot roots are duplicated")

    candidates: list[dict[str, Any]] = []
    protected_paths = {Path(item["path"]) for item in protected_roots}
    for path in sorted(PRIVATE_ROOT.iterdir(), key=lambda item: item.name):
        if path in protected_paths or not path.name.startswith("snapshots-"):
            continue
        opened = require_owned_directory(path, label="obsolete snapshot root")
        if (
            path.is_symlink()
            or path.parent != PRIVATE_ROOT
            or re.fullmatch(r"snapshots-[A-Za-z0-9][A-Za-z0-9._-]{0,190}", path.name)
            is None
        ):
            raise CleanupError(f"obsolete snapshot root is unsafe: {path}")
        candidates.append(
            {
                "kind": "snapshot-root",
                "path": str(path),
                "device": opened.st_dev,
                "inode": opened.st_ino,
            }
        )
    unknown_manifests: list[str] = []
    for path in sorted(manifest_root.iterdir(), key=lambda item: item.name):
        if path in protected_manifests:
            continue
        if re.fullmatch(r"manifest-[A-Za-z0-9][A-Za-z0-9._-]{0,190}\.json", path.name):
            opened = require_private_regular_file(
                path,
                label="obsolete snapshot manifest",
            )
            candidates.append(
                {
                    "kind": "snapshot-manifest",
                    "path": str(path),
                    "device": opened.st_dev,
                    "inode": opened.st_ino,
                }
            )
        else:
            unknown_manifests.append(str(path))
    state = release_selector.read_selector_state(
        SELECTOR_STATE,
        lock_path=SELECTOR_LOCK,
    )
    plan = {
        "transaction_id": accepted_transaction_id,
        "expected_current_build": expected_current_build,
        "selector_generation": state["generation"],
        "selector_state_sha256": release_selector.selector_state_sha256(state),
        "protected_roots": protected_roots,
        "candidates": candidates,
        "unknown_manifests": unknown_manifests,
    }
    plan["plan_sha256"] = hashlib.sha256(
        canonical_json_bytes(plan)
    ).hexdigest()
    return plan


def _apply_accepted_snapshot_plan(plan: dict[str, Any]) -> dict[str, Any]:
    RECEIPTS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = (
        RECEIPTS_ROOT
        / f"rolling-snapshot-cleanup-{plan['transaction_id']}.json"
    )
    operations = [
        {
            "source": item["path"],
            "destination": str(
                Path(item["path"]).with_name(
                    ".hermes-snapshot-retention-"
                    f"{plan['transaction_id']}-{index:04d}-"
                    f"{Path(item['path']).name}"
                )
            ),
            "device": item["device"],
            "inode": item["inode"],
            "state": "intent",
        }
        for index, item in enumerate(plan["candidates"])
    ]
    if receipt_path.exists():
        receipt = read_json_file(
            receipt_path,
            label="rolling snapshot cleanup receipt",
        )
        if receipt.get("plan") != plan:
            raise CleanupError("rolling snapshot cleanup receipt changed")
        if receipt.get("status") == "completed":
            return receipt
    else:
        receipt = {
            "version": 1,
            "status": "planned",
            "plan": plan,
            "operations": operations,
            "disk_free_before_bytes": shutil.disk_usage(PRIVATE_ROOT).free,
        }
        create_receipt(receipt_path, receipt)
    current = release_selector.read_selector_state(
        SELECTOR_STATE,
        lock_path=SELECTOR_LOCK,
    )
    if (
        current["generation"] != plan["selector_generation"]
        or release_selector.selector_state_sha256(current)
        != plan["selector_state_sha256"]
    ):
        raise CleanupError("selector changed during snapshot retention")
    for protected in plan["protected_roots"]:
        root = Path(protected["path"])
        opened = require_owned_directory(
            root,
            label="protected rollback snapshot",
        )
        if (
            opened.st_dev != protected["device"]
            or opened.st_ino != protected["inode"]
        ):
            raise CleanupError("protected rollback snapshot changed")
        validate_manifest(
            Path(protected["manifest_path"]),
            root,
            expected_snapshot_id=plan["transaction_id"],
            expected_sha256=protected["manifest_sha256"],
            verify_payload=True,
        )
    receipt["status"] = "quarantining"
    atomic_receipt(receipt_path, receipt)
    for operation in receipt["operations"]:
        source = Path(operation["source"])
        destination = Path(operation["destination"])
        if source.exists() and not destination.exists():
            if lsof_open_rows(source):
                raise CleanupError(
                    f"obsolete snapshot artifact has open files: {source}"
                )
            opened = source.lstat()
            if (
                stat.S_ISLNK(opened.st_mode)
                or opened.st_dev != operation["device"]
                or opened.st_ino != operation["inode"]
            ):
                raise CleanupError(
                    f"obsolete snapshot artifact changed: {source}"
                )
            os.replace(source, destination)
            fsync_directory(source.parent)
        elif destination.exists() and not source.exists():
            opened = destination.lstat()
            if (
                stat.S_ISLNK(opened.st_mode)
                or opened.st_dev != operation["device"]
                or opened.st_ino != operation["inode"]
            ):
                raise CleanupError("snapshot quarantine identity changed")
        elif operation.get("state") != "deleted":
            raise CleanupError("snapshot quarantine state is ambiguous")
        operation["state"] = "quarantined"
        atomic_receipt(receipt_path, receipt)
    receipt["status"] = "deleting"
    atomic_receipt(receipt_path, receipt)
    for operation in receipt["operations"]:
        destination = Path(operation["destination"])
        if destination.exists():
            if destination.is_dir():
                remove_sealed_tree(destination)
            else:
                destination.unlink()
            fsync_directory(destination.parent)
        operation["state"] = "deleted"
        atomic_receipt(receipt_path, receipt)
    receipt["status"] = "completed"
    receipt["disk_free_after_bytes"] = shutil.disk_usage(PRIVATE_ROOT).free
    receipt["disk_free_delta_bytes"] = (
        receipt["disk_free_after_bytes"]
        - receipt["disk_free_before_bytes"]
    )
    receipt["receipt_path"] = str(receipt_path)
    atomic_receipt(receipt_path, receipt)
    return receipt


def run_after_release(
    selector_state: Path | str,
    selector_lock: Path | str,
    *,
    accepted_transaction_id: str,
    expected_current_build: str,
) -> dict[str, Any]:
    """Apply the exact verified rolling plan after a durable release promotion."""
    try:
        configure_release_paths(release_paths(selector_state, selector_lock))
        rolling_receipt_path = (
            RECEIPTS_ROOT
            / f"rolling-release-cleanup-{accepted_transaction_id}.json"
        )
        if rolling_receipt_path.exists():
            prior = read_json_file(
                rolling_receipt_path,
                label="rolling cleanup receipt",
            )
            rolling_plan = prior.get("plan")
            if (
                not isinstance(rolling_plan, dict)
                or rolling_plan.get("transaction_id")
                != accepted_transaction_id
                or rolling_plan.get("expected_current_build")
                != expected_current_build
            ):
                raise CleanupError("rolling cleanup receipt binding changed")
        else:
            rolling_plan = _rolling_release_plan(
                accepted_transaction_id=accepted_transaction_id,
                expected_current_build=expected_current_build,
            )
        rolling_receipt = _apply_rolling_release_plan(rolling_plan)
        snapshot_plan = _accepted_snapshot_plan(
            accepted_transaction_id=accepted_transaction_id,
            expected_current_build=expected_current_build,
        )
        receipt = _apply_accepted_snapshot_plan(snapshot_plan)
        retained = [
            item["path"] for item in snapshot_plan["protected_roots"]
        ]
        return {
            "status": receipt["status"],
            "receipt_path": receipt["receipt_path"],
            "retained_rollback_roots": retained,
            "deleted_payload_trees": len(snapshot_plan["candidates"]),
            "disk_free_delta_bytes": receipt.get("disk_free_delta_bytes", 0),
            "plan_sha256": snapshot_plan["plan_sha256"],
            "rolling_release_receipt": rolling_receipt["receipt_path"],
            "rolling_deleted": len(rolling_receipt["operations"]),
            "unknown_legacy_artifacts": rolling_plan["unknown_protected"],
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}:{exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes release rollback rolling-retention cleaner"
    )
    parser.add_argument("--selector-state", required=True)
    parser.add_argument("--selector-lock", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--after-release", action="store_true")
    parser.add_argument("--accepted-transaction-id")
    parser.add_argument("--expected-current-build")
    options = parser.parse_args()
    try:
        configure_release_paths(
            release_paths(options.selector_state, options.selector_lock)
        )
        if options.after_release:
            if options.apply or options.expected_plan_sha256 is not None:
                raise CleanupError(
                    "--after-release cannot be combined with manual apply options"
                )
            result = run_after_release(
                options.selector_state,
                options.selector_lock,
                accepted_transaction_id=str(
                    options.accepted_transaction_id or ""
                ),
                expected_current_build=str(
                    options.expected_current_build or ""
                ),
            )
        elif options.apply:
            result = apply_cleanup(str(options.expected_plan_sha256 or ""))
        else:
            if options.expected_plan_sha256 is not None:
                raise CleanupError(
                    "--expected-plan-sha256 requires --apply"
                )
            result = dry_run()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}:{exc}",
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
