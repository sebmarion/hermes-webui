"""Validated execution identity shared by WebUI run admission and wakeups."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from contextlib import closing

from api.agent_sessions import resolve_shared_session
from api.profiles import _PROFILE_ID_RE, get_hermes_home_for_profile

logger = logging.getLogger(__name__)


class ExecutionLineageUnavailable(RuntimeError):
    """The execution owner cannot be established safely."""


def _session_table_presence(db_path: Path) -> bool | None:
    """Return whether *db_path* has the shared ``sessions`` table.

    Agent-only startup can create ``state.db`` for its durable delegation
    ledger before WebUI has initialized the shared-session schema.  That is an
    empty lineage authority, not a malformed session graph.  Keep actual I/O
    failures distinguishable (``None``) so a corrupt/unreadable database still
    fails closed at the resolver boundary.
    """
    if not db_path.is_file():
        return False
    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sessions' LIMIT 1"
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error):
        return None


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


def _fallback_history_lineage(
    db_path: Path,
    session_id: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Resolve an explicitly supplied recovery DB without its parent index.

    WebUI recovery has already read and validated the same authoritative DB
    before admission.  A fresh Agent async-delegation store can temporarily
    create that DB with only its own table, or a minimal recovery fixture can
    omit ``idx_sessions_parent``.  The normal bounded resolver intentionally
    returns ``degraded`` in that shape.  For this explicit, already-validated
    recovery path, walk the bounded ancestor chain directly; ordinary route
    admission still fails closed on a degraded shared-session resolver.
    """
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            required = {"id", "source", "end_reason"}
            if not required.issubset(columns):
                raise ExecutionLineageUnavailable(
                    "authoritative session schema is unavailable"
                )

            def optional_column(name: str) -> str:
                return name if name in columns else "NULL"

            select = (
                "SELECT id, source, "
                f"{optional_column('parent_session_id')} AS parent_session_id, "
                "end_reason, "
                f"{optional_column('model_config')} AS model_config "
                "FROM sessions WHERE id = ?"
            )
            current = conn.execute(select, (session_id,)).fetchone()
            if current is None:
                return None
            requested_source = str(current[1] or "").strip().lower()
            path = [str(current[0])]
            seen = {path[0]}
            while current[2]:
                parent_id = str(current[2] or "").strip()
                if not parent_id or parent_id in seen:
                    raise ExecutionLineageUnavailable(
                        "execution lineage parent chain is malformed"
                    )
                parent = conn.execute(select, (parent_id,)).fetchone()
                if parent is None:
                    raise ExecutionLineageUnavailable(
                        "execution lineage parent is missing"
                    )
                parent_source = str(parent[1] or "").strip().lower()
                parent_end_reason = str(parent[3] or "").strip().lower()
                try:
                    child_config = json.loads(current[4]) if current[4] else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    child_config = {"_malformed": True}
                if (
                    parent_end_reason != "compression"
                    or parent_source != requested_source
                    or not isinstance(child_config, dict)
                    or child_config.get("_branched_from")
                    or child_config.get("_delegate_from")
                ):
                    break
                path.append(parent_id)
                seen.add(parent_id)
                if len(path) > 256:
                    raise ExecutionLineageUnavailable(
                        "execution lineage exceeds bounded depth"
                    )
                current = parent
            path.reverse()
            return path[0], tuple(path)
    except ExecutionLineageUnavailable:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ExecutionLineageUnavailable(
            "execution lineage resolution failed"
        ) from exc


def resolve_execution_lineage(
    session_id: str,
    *,
    session=None,
    profile: str | None = None,
    state_db_path: str | Path | None = None,
) -> ExecutionLineageResolution:
    """Resolve one physical session to its immutable execution owner."""
    requested = str(session_id or "").strip()
    if not requested:
        raise ExecutionLineageUnavailable("session identity is missing")
    profile_name = _normalize_profile(
        profile if profile is not None else getattr(session, "profile", None)
    )
    home = _profile_home(profile_name)
    resolved_state_db_path = str(
        Path(state_db_path).expanduser().resolve()
        if state_db_path is not None
        else (home / "state.db").resolve()
    )
    execution_root = _session_root(
        session,
        session_id=requested,
        profile=profile_name,
    )
    try:
        resolved = resolve_shared_session(
            Path(resolved_state_db_path),
            execution_root,
            mode="history",
        )
    except Exception as exc:
        raise ExecutionLineageUnavailable(
            "execution lineage resolution failed"
        ) from exc
    status = str(getattr(resolved, "status", "") or "")
    fallback_members: tuple[str, ...] | None = None
    if status == "degraded" and state_db_path is None:
        # The Agent durable-delegation ledger may legitimately be the first
        # writer to a fresh profile state.db. Until WebUI creates its shared
        # ``sessions`` table there is no lineage graph to validate; the
        # in-memory/sidecar session remains the physical owner. Do not apply
        # this relaxation to an explicitly supplied recovery DB: recovery has
        # an authoritative state snapshot and must retain its strict schema
        # contract below.
        if _session_table_presence(Path(resolved_state_db_path)) is False:
            status = "missing"
    if status == "degraded" and state_db_path is not None:
        fallback = _fallback_history_lineage(Path(resolved_state_db_path), execution_root)
        if fallback is None:
            status = "missing"
        else:
            execution_root, fallback_members = fallback
            status = "found"
    if status in {"degraded", "ambiguous"}:
        raise ExecutionLineageUnavailable(
            f"execution lineage is {status}"
        )
    if status == "found":
        if fallback_members is None:
            execution_root = str(getattr(resolved, "root_id", "") or execution_root)
            members = tuple(
                str(item) for item in (getattr(resolved, "member_ids", ()) or ())
            )
        else:
            members = fallback_members
    elif status == "missing":
        members = (execution_root,)
    else:
        raise ExecutionLineageUnavailable("execution lineage status is unknown")
    key = "v1:sha256:" + hashlib.sha256(
        canonical_lineage_payload(resolved_state_db_path, execution_root)
    ).hexdigest()
    return ExecutionLineageResolution(
        requested_session_id=requested,
        profile=profile_name,
        state_db_path=resolved_state_db_path,
        execution_root_session_id=execution_root,
        execution_lineage_key=key,
        compression_member_ids=members,
    )
