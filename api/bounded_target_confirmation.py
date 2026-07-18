"""Fail-closed, post-page confirmation of a Stage 1 shared-session receipt.

This deliberately imports Stage 1's private continuation and capability helpers
instead of reimplementing them.  The two readers must agree exactly on branch
eligibility and the declared ``idx_sessions_parent`` capability.  Keep changes
to those helpers synchronized with this confirmation boundary.
"""

from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from api.agent_sessions import (
    _SHARED_RESOLUTION_CAPABILITY_CACHE,
    _SHARED_RESOLUTION_CAPABILITY_CACHE_LOCK,
    _SHARED_RESOLUTION_MAX_ROWS,
    _continuation_child_key,
    _continuation_child_semantic_key,
    _is_continuation_session,
    _selected_importable_continuation,
    _shared_resolution_capabilities,
    _shared_resolution_fingerprint,
    open_state_db_readonly,
    shared_state_db_identity,
)


_MAX_MEMBERS = _SHARED_RESOLUTION_MAX_ROWS
_MAX_SQL_STATEMENTS = _SHARED_RESOLUTION_MAX_ROWS


def confirm_shared_session_target(db_path: Path, original: Any) -> bool:
    """Return whether the original navigation receipt still names one target.

    This is intentionally not a resolver retry: it only reads the receipt's
    member IDs, direct root boundary, and compression-child branches from one
    read-only SQLite snapshot.  Any missing proof, replacement, lock, schema
    drift, ambiguity, or budget exhaustion is a negative confirmation.
    """
    fields = _receipt_fields(original)
    if fields is None:
        return False
    path = Path(db_path)
    if shared_state_db_identity(path) != fields["database_identity"]:
        return False
    if not path.exists():
        return False

    budget = _ReadBudget()
    try:
        with closing(open_state_db_readonly(path)) as conn:
            conn.row_factory = sqlite3.Row
            _execute(conn, budget, "BEGIN")
            identity = shared_state_db_identity(path)
            if identity != fields["database_identity"]:
                return False
            if _cached_schema_version(identity) != _schema_version(conn, budget):
                return False
            capabilities = _shared_resolution_capabilities(conn, identity)
            select_sql = capabilities.select_sql
            if select_sql is None or capabilities.parent_index_usable is not True:
                return False

            rows_by_id = _read_members(conn, budget, select_sql, fields["member_ids"])
            if rows_by_id is None:
                return False
            path_rows = [rows_by_id[member_id] for member_id in fields["member_ids"]]
            if _shared_resolution_fingerprint(path_rows) != fields["lineage_fingerprint"]:
                return False
            if not _same_path(path_rows, fields):
                return False
            if not _root_boundary_unchanged(conn, budget, select_sql, path_rows[0]):
                return False
            if not _branches_unchanged(conn, budget, select_sql, path_rows):
                return False
            return shared_state_db_identity(path) == fields["database_identity"]
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, IndexError):
        return False


class _ReadBudget:
    """Bound raw session rows and statements independently of global DB size."""

    def __init__(self) -> None:
        self.rows = 0
        self.statements = 0

    def execute(self, conn: sqlite3.Connection, sql: str, params: tuple = ()):
        if self.statements >= _MAX_SQL_STATEMENTS:
            raise ValueError("bounded target confirmation SQL budget exhausted")
        self.statements += 1
        return conn.execute(sql, params)

    def rows_from(self, raw_rows: list[sqlite3.Row]) -> list[dict]:
        self.rows += len(raw_rows)
        if self.rows > _MAX_MEMBERS:
            raise ValueError("bounded target confirmation row budget exhausted")
        rows = []
        for raw in raw_rows:
            row = dict(raw)
            row["actual_message_count"] = int(row.get("message_count") or 0)
            if not str(row.get("id") or ""):
                raise ValueError("malformed session id")
            rows.append(row)
        return rows


def _execute(conn: sqlite3.Connection, budget: _ReadBudget, sql: str, params: tuple = ()):
    return budget.execute(conn, sql, params)


def _receipt_fields(original: Any) -> dict[str, Any] | None:
    try:
        members = tuple(original.member_ids)
        requested = original.requested_id
        canonical = original.canonical_id
        root = original.root_id
        tip = original.tip_id
        fingerprint = original.lineage_fingerprint
        identity = tuple(original.database_identity)
        status = original.status
        mode = original.mode
    except (AttributeError, TypeError):
        return None
    if (
        status != "found"
        or mode != "navigation"
        or not isinstance(members, tuple)
        or not 1 <= len(members) <= _MAX_MEMBERS
        or len(set(members)) != len(members)
        or not all(_identifier(member) for member in members)
        or not all(_identifier(value) for value in (requested, canonical, root, tip))
        or requested not in members
        or root != members[0]
        or canonical != members[-1]
        or tip != canonical
        or not isinstance(fingerprint, str)
        or not fingerprint.startswith("sha256:")
        or len(fingerprint) != 71
        or not _identity(identity)
    ):
        return None
    return {
        "member_ids": members,
        "requested_id": requested,
        "canonical_id": canonical,
        "root_id": root,
        "tip_id": tip,
        "lineage_fingerprint": fingerprint,
        "database_identity": identity,
    }


def _read_members(
    conn: sqlite3.Connection,
    budget: _ReadBudget,
    select_sql: str,
    member_ids: tuple[str, ...],
) -> dict[str, dict] | None:
    placeholders = ",".join("?" for _ in member_ids)
    raw_rows = _execute(
        conn,
        budget,
        f"{select_sql} WHERE s.id IN ({placeholders}) LIMIT ?",
        (*member_ids, len(member_ids) + 1),
    ).fetchall()
    rows = budget.rows_from(raw_rows)
    rows_by_id = {str(row["id"]): row for row in rows}
    if len(rows) != len(member_ids) or len(rows_by_id) != len(member_ids):
        return None
    return rows_by_id


def _cached_schema_version(identity: tuple[str, int | None, int | None]) -> int | None:
    """Recover the Stage 1 schema epoch associated with the frozen receipt.

    ``SharedSessionResolution`` predates the confirmation seam and does not
    carry this immutable field itself.  Stage 1 records its capability epoch in
    its bounded LRU, so losing that entry intentionally degrades confirmation.
    """
    with _SHARED_RESOLUTION_CAPABILITY_CACHE_LOCK:
        versions = {
            key[1]
            for key in _SHARED_RESOLUTION_CAPABILITY_CACHE
            if key[0] == identity and isinstance(key[1], int)
        }
    return next(iter(versions)) if len(versions) == 1 else None


def _schema_version(conn: sqlite3.Connection, budget: _ReadBudget) -> int | None:
    raw = _execute(conn, budget, "PRAGMA schema_version").fetchone()
    if raw is None:
        return None
    value = raw[0]
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _same_path(path_rows: list[dict], fields: dict[str, Any]) -> bool:
    member_ids = fields["member_ids"]
    if tuple(str(row["id"]) for row in path_rows) != member_ids:
        return False
    if str(path_rows[0]["id"]) != fields["root_id"]:
        return False
    if str(path_rows[-1]["id"]) != fields["canonical_id"]:
        return False
    for index, row in enumerate(path_rows):
        parent_id = str(row.get("parent_session_id") or "")
        if index == 0:
            continue
        parent = path_rows[index - 1]
        if parent_id != str(parent["id"]):
            return False
        if not _is_continuation_session(parent, row, compression_only=True):
            return False
    return True


def _root_boundary_unchanged(
    conn: sqlite3.Connection,
    budget: _ReadBudget,
    select_sql: str,
    root: dict,
) -> bool:
    parent_id = str(root.get("parent_session_id") or "")
    if not parent_id:
        return True
    parent_rows = budget.rows_from(
        _execute(
            conn,
            budget,
            f"{select_sql} WHERE s.id = ? LIMIT 2",
            (parent_id,),
        ).fetchall()
    )
    if len(parent_rows) != 1:
        return False
    return not _is_continuation_session(parent_rows[0], root, compression_only=True)


def _branches_unchanged(
    conn: sqlite3.Connection,
    budget: _ReadBudget,
    select_sql: str,
    path_rows: list[dict],
) -> bool:
    compression_parents = [row for row in path_rows if row.get("end_reason") == "compression"]
    if not compression_parents:
        return True
    member_ids = tuple(str(row["id"]) for row in path_rows)
    parent_ids = tuple(str(row["id"]) for row in compression_parents)
    remaining = _MAX_MEMBERS - budget.rows
    if remaining < 0:
        return False
    parent_placeholders = ",".join("?" for _ in parent_ids)
    member_placeholders = ",".join("?" for _ in member_ids)
    raw_rows = _execute(
        conn,
        budget,
        f"{select_sql} WHERE s.parent_session_id IN ({parent_placeholders}) "
        f"AND s.id NOT IN ({member_placeholders}) LIMIT ?",
        (*parent_ids, *member_ids, remaining + 1),
    ).fetchall()
    extra_rows = budget.rows_from(raw_rows)
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for child in extra_rows:
        by_parent[str(child.get("parent_session_id") or "")].append(child)

    for index, parent in enumerate(path_rows):
        if parent.get("end_reason") != "compression":
            continue
        candidates = list(by_parent.get(str(parent["id"]), ()))
        expected = path_rows[index + 1] if index + 1 < len(path_rows) else None
        if expected is not None:
            candidates.append(expected)
        candidates = [
            child
            for child in candidates
            if _is_continuation_session(parent, child, compression_only=True)
        ]
        if not candidates:
            return expected is None
        ranked = sorted(candidates, key=_continuation_child_key)
        if (
            len(ranked) > 1
            and _continuation_child_semantic_key(ranked[0])
            == _continuation_child_semantic_key(ranked[1])
        ):
            return False
        selected = ranked[0]
        if expected is None or str(selected["id"]) != str(expected["id"]):
            return False
        importable = _selected_importable_continuation(path_rows[0], selected)
        if importable is None or importable is not selected:
            return False
    return True


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _identity(value: tuple[Any, ...]) -> bool:
    return (
        len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and isinstance(value[2], int)
    )
