"""Validated execution identity shared by WebUI run admission and wakeups."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from api.agent_sessions import resolve_shared_session
from api.profiles import _PROFILE_ID_RE, get_hermes_home_for_profile

logger = logging.getLogger(__name__)


class ExecutionLineageUnavailable(RuntimeError):
    """The execution owner cannot be established safely."""


@dataclass(frozen=True)
class ExecutionLineageResolution:
    """Short-lived routing result for one physical session."""

    requested_session_id: str
    profile: str
    state_db_path: str
    execution_root_session_id: str
    execution_lineage_key: str
    compression_member_ids: tuple[str, ...]


def canonical_lineage_payload(state_db_path: str, execution_root: str) -> bytes:
    """Return the stable bytes hashed into an opaque lineage key."""
    payload = {
        "execution_root": str(execution_root or ""),
        "profile_state_identity": str(state_db_path or ""),
        "version": 1,
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_profile(profile: str | None) -> str:
    normalized = str(profile or "default").strip() or "default"
    if not _PROFILE_ID_RE.fullmatch(normalized):
        raise ExecutionLineageUnavailable("session profile identity is invalid")
    return normalized


def _profile_home(profile: str) -> Path:
    try:
        return Path(get_hermes_home_for_profile(profile)).expanduser().resolve()
    except Exception as exc:
        raise ExecutionLineageUnavailable(
            "session profile identity cannot be resolved"
        ) from exc


def _load_receipt_for_child(child_id: str) -> dict | None:
    """Find the exact durable receipt that created *child_id*."""
    try:
        from api.tool_limit_continuation import load_receipts

        store = load_receipts()
    except Exception as exc:
        raise ExecutionLineageUnavailable(
            "tool-limit continuation receipt is unavailable"
        ) from exc
    receipts = store.get("receipts") if isinstance(store, dict) else None
    if not isinstance(receipts, dict):
        raise ExecutionLineageUnavailable("tool-limit continuation receipt is invalid")
    matches = [
        row
        for row in receipts.values()
        if isinstance(row, dict)
        and str(row.get("child_session_id") or "") == child_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ExecutionLineageUnavailable(
            "tool-limit child receipt identity is ambiguous"
        )
    return matches[0]


def _session_root(session, *, session_id: str, profile: str) -> str:
    """Resolve tool-child authority, otherwise retain the physical ID."""
    control = getattr(session, "tool_limit_continuation", None)
    if not isinstance(control, dict) or not control:
        return session_id

    session_root = str(getattr(session, "root_session_id", "") or "").strip()
    control_root = str(control.get("root_session_id") or "").strip()
    execution_id = str(control.get("execution_id") or "").strip()
    if not session_root or session_root != control_root or not execution_id:
        raise ExecutionLineageUnavailable("tool-limit root identity conflicts")

    receipt = _load_receipt_for_child(session_id)
    if receipt is None:
        raise ExecutionLineageUnavailable("tool-limit child receipt is missing")
    if (
        str(receipt.get("child_session_id") or "") != session_id
        or str(receipt.get("root_session_id") or "") != session_root
        or str(receipt.get("execution_id") or "") != execution_id
    ):
        raise ExecutionLineageUnavailable("tool-limit child receipt conflicts")
    receipt_profile = _normalize_profile(receipt.get("profile"))
    expected_home = _profile_home(profile)
    receipt_home = _profile_home(receipt_profile)
    if receipt_home != expected_home:
        raise ExecutionLineageUnavailable("tool-limit receipt profile conflicts")
    return session_root


def resolve_execution_lineage(
    session_id: str,
    *,
    session=None,
    profile: str | None = None,
) -> ExecutionLineageResolution:
    """Resolve one physical session to its immutable execution owner."""
    requested = str(session_id or "").strip()
    if not requested:
        raise ExecutionLineageUnavailable("session identity is missing")
    profile_name = _normalize_profile(
        profile if profile is not None else getattr(session, "profile", None)
    )
    home = _profile_home(profile_name)
    state_db_path = str((home / "state.db").resolve())
    execution_root = _session_root(
        session,
        session_id=requested,
        profile=profile_name,
    )
    try:
        resolved = resolve_shared_session(
            Path(state_db_path),
            execution_root,
            mode="history",
        )
    except Exception as exc:
        raise ExecutionLineageUnavailable(
            "execution lineage resolution failed"
        ) from exc
    status = str(getattr(resolved, "status", "") or "")
    if status in {"degraded", "ambiguous"}:
        raise ExecutionLineageUnavailable(
            f"execution lineage is {status}"
        )
    if status == "found":
        execution_root = str(getattr(resolved, "root_id", "") or execution_root)
        members = tuple(str(item) for item in (getattr(resolved, "member_ids", ()) or ()))
    elif status == "missing":
        members = (execution_root,)
    else:
        raise ExecutionLineageUnavailable("execution lineage status is unknown")
    key = "v1:sha256:" + hashlib.sha256(
        canonical_lineage_payload(state_db_path, execution_root)
    ).hexdigest()
    return ExecutionLineageResolution(
        requested_session_id=requested,
        profile=profile_name,
        state_db_path=state_db_path,
        execution_root_session_id=execution_root,
        execution_lineage_key=key,
        compression_member_ids=members,
    )
