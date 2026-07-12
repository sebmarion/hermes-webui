"""Durable ownership and delivery state for async delegation wakeups."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_wakeups (
    delegation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    wakeup_prompt TEXT NOT NULL,
    event_json TEXT NOT NULL,
    state TEXT NOT NULL,
    claim_token TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_at REAL,
    delivered_at REAL,
    tracker_acked_at REAL,
    last_error TEXT,
    origin_profile TEXT NOT NULL DEFAULT '',
    origin_tracker_path TEXT NOT NULL DEFAULT '',
    origin_ui_session_id TEXT NOT NULL DEFAULT '',
    claim_owner TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_delegation_wakeups_session_state
    ON delegation_wakeups(session_id, state, created_at);
"""


@dataclass(frozen=True)
class RecordOutcome:
    status: str
    row: dict[str, Any] | None = None


class DelegationWakeupStore:
    """Small SQLite state machine: pending -> claimed -> delivered."""

    def __init__(self, path: str | Path | None = None):
        legacy_path = None
        if path is None:
            from api.config import STATE_DIR

            path = Path(STATE_DIR) / "private" / "delegation_wakeups.sqlite3"
            legacy_path = Path(STATE_DIR) / "delegation_wakeups.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if legacy_path is not None and legacy_path.exists() and legacy_path != self.path:
            self._migrate_legacy_store(legacy_path)
        self._lock = threading.RLock()
        self._conn = self._connect()
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()
        self._repair_modes()
        self.prune_delivered()

    def _migrate_legacy_store(self, legacy_path: Path) -> None:
        """Copy/merge the former public store before opening the private one."""
        legacy_path = Path(legacy_path)
        os.chmod(legacy_path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = legacy_path.with_name(legacy_path.name + suffix)
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
        legacy = sqlite3.connect(str(legacy_path), timeout=30)
        legacy.row_factory = sqlite3.Row
        target = sqlite3.connect(str(self.path), timeout=30)
        target.row_factory = sqlite3.Row
        try:
            if legacy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("legacy delegation wakeup store failed integrity check")
            legacy.execute("PRAGMA wal_checkpoint(FULL)")
            target.executescript(_SCHEMA)
            source_columns = {
                str(row[1]) for row in legacy.execute("PRAGMA table_info(delegation_wakeups)")
            }
            if not source_columns:
                raise sqlite3.DatabaseError(
                    "legacy delegation wakeup store has no wakeup table"
                )
            destination_columns = [
                str(row[1]) for row in target.execute("PRAGMA table_info(delegation_wakeups)")
            ]
            rows = legacy.execute("SELECT * FROM delegation_wakeups").fetchall()
            target.execute("BEGIN IMMEDIATE")
            for source_row in rows:
                source = dict(source_row)
                delegation_id = str(source.get("delegation_id") or "")
                existing = target.execute(
                    "SELECT * FROM delegation_wakeups WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()
                values = {}
                for column in destination_columns:
                    if column in source:
                        values[column] = source[column]
                values.setdefault("origin_profile", "")
                values.setdefault("origin_tracker_path", "")
                values.setdefault("origin_ui_session_id", "")
                if str(values.get("state") or "") == "claimed":
                    values.update({
                        "state": "pending",
                        "claim_token": None,
                        "claim_owner": None,
                        "lease_expires_at": None,
                        "claimed_at": None,
                        "last_error": "migrated_recoverable_claim",
                        "updated_at": time.time(),
                    })
                if existing is not None:
                    current = dict(existing)
                    owner_fields = (
                        "session_id", "session_key", "origin_profile",
                        "origin_tracker_path", "origin_ui_session_id",
                    )
                    if any(str(current.get(key) or "") != str(values.get(key) or "") for key in owner_fields):
                        raise sqlite3.IntegrityError(
                            f"legacy wakeup ownership collision: {delegation_id}"
                        )
                    continue
                columns = sorted(values)
                placeholders = ",".join("?" for _ in columns)
                target.execute(
                    f"INSERT INTO delegation_wakeups ({','.join(columns)}) VALUES ({placeholders})",
                    tuple(values[column] for column in columns),
                )
            for source_row in rows:
                delegation_id = str(source_row["delegation_id"])
                if target.execute(
                    "SELECT 1 FROM delegation_wakeups WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone() is None:
                    raise sqlite3.DatabaseError(
                        f"legacy wakeup verification failed: {delegation_id}"
                    )
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            legacy.close()
            target.close()
        os.chmod(self.path, 0o600)
        for database in (legacy_path, self.path):
            for suffix in ("-wal", "-shm"):
                sidecar = database.with_name(database.name + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)

    def _migrate_schema(self) -> None:
        existing = {
            str(row[1]) for row in self._conn.execute(
                "PRAGMA table_info(delegation_wakeups)"
            )
        }
        for name, ddl in {
            "origin_profile": "TEXT NOT NULL DEFAULT ''",
            "origin_tracker_path": "TEXT NOT NULL DEFAULT ''",
            "origin_ui_session_id": "TEXT NOT NULL DEFAULT ''",
            "claim_owner": "TEXT",
            "lease_expires_at": "REAL",
        }.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE delegation_wakeups ADD COLUMN {name} {ddl}"
                )

    def _repair_modes(self) -> None:
        os.chmod(self.path.parent, 0o700)
        for candidate in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _dict(row) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, delegation_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._dict(self._conn.execute(
                "SELECT * FROM delegation_wakeups WHERE delegation_id=?",
                (str(delegation_id),),
            ).fetchone())

    def record_pending(
        self,
        *,
        delegation_id: str,
        session_id: str,
        session_key: str,
        wakeup_prompt: str,
        event: dict[str, Any],
        origin_profile: str = "",
        origin_tracker_path: str = "",
        origin_ui_session_id: str = "",
    ) -> RecordOutcome:
        delegation_id = str(delegation_id or "").strip()
        session_id = str(session_id or "").strip()
        session_key = str(session_key or "").strip()
        wakeup_prompt = str(wakeup_prompt or "").strip()
        if not all((delegation_id, session_id, session_key, wakeup_prompt)):
            raise ValueError("delegation_id, session_id, session_key, and wakeup_prompt are required")
        event_json = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = time.time()
        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM delegation_wakeups WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()
                if row is not None:
                    existing = dict(row)
                    same_owner = (
                        existing["session_id"] == session_id
                        and existing["session_key"] == session_key
                        and existing["origin_profile"] == str(origin_profile or "")
                        and existing["origin_tracker_path"] == str(origin_tracker_path or "")
                        and existing["origin_ui_session_id"] == str(origin_ui_session_id or "")
                    )
                    same_payload = (
                        existing["wakeup_prompt"] == wakeup_prompt
                        and existing["event_json"] == event_json
                    )
                    conn.commit()
                    if not same_owner:
                        return RecordOutcome("collision", existing)
                    if not same_payload:
                        return RecordOutcome("conflict", existing)
                    return RecordOutcome("duplicate", existing)
                conn.execute(
                    """INSERT INTO delegation_wakeups (
                        delegation_id, session_id, session_key, wakeup_prompt,
                        event_json, state, created_at, updated_at,
                        origin_profile, origin_tracker_path, origin_ui_session_id
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                    (
                        delegation_id, session_id, session_key, wakeup_prompt,
                        event_json, now, now, str(origin_profile or ""),
                        str(origin_tracker_path or ""),
                        str(origin_ui_session_id or ""),
                    ),
                )
                conn.commit()
                return RecordOutcome("inserted", self.get(delegation_id))
            except Exception:
                conn.rollback()
                raise

    def list_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM delegation_wakeups WHERE state='pending'"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " AND session_id=?"
            params = (str(session_id),)
        sql += " ORDER BY created_at, delegation_id"
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def pending_session_ids(self) -> list[str]:
        with self._lock:
            return [str(row[0]) for row in self._conn.execute(
                "SELECT DISTINCT session_id FROM delegation_wakeups "
                "WHERE state='pending' ORDER BY session_id"
            ).fetchall()]

    def claim_next(
        self,
        session_id: str,
        *,
        owner_uuid: str = "",
        lease_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        token = uuid.uuid4().hex
        now = time.time()
        owner = str(owner_uuid or f"pid-{os.getpid()}")
        lease_expires_at = now + max(1.0, float(lease_seconds))
        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT delegation_id FROM delegation_wakeups
                       WHERE session_id=? AND state='pending'
                       ORDER BY created_at, delegation_id LIMIT 1""",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                delegation_id = str(row["delegation_id"])
                changed = conn.execute(
                    """UPDATE delegation_wakeups SET state='claimed', claim_token=?,
                       claim_owner=?, lease_expires_at=?, claimed_at=?, updated_at=?, last_error=NULL
                       WHERE delegation_id=? AND state='pending'""",
                    (token, owner, lease_expires_at, now, now, delegation_id),
                ).rowcount
                if changed != 1:
                    conn.commit()
                    return None
                claimed = conn.execute(
                    "SELECT * FROM delegation_wakeups WHERE delegation_id=?",
                    (delegation_id,),
                ).fetchone()
                conn.commit()
                return dict(claimed)
            except Exception:
                conn.rollback()
                raise

    def release_claim(self, delegation_id: str, claim_token: str, error: str = "") -> bool:
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='pending', claim_token=NULL,
                   claim_owner=NULL, lease_expires_at=NULL, claimed_at=NULL,
                   updated_at=?, last_error=?
                   WHERE delegation_id=? AND state='claimed' AND claim_token=?""",
                (time.time(), str(error or ""), str(delegation_id), str(claim_token)),
            ).rowcount
            self._conn.commit()
            return changed == 1

    def mark_delivered(self, delegation_id: str, claim_token: str) -> bool:
        now = time.time()
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='delivered', delivered_at=?,
                   updated_at=?, last_error=NULL, wakeup_prompt='', event_json='',
                   claim_owner=NULL, lease_expires_at=NULL
                   WHERE delegation_id=? AND state='claimed' AND claim_token=?""",
                (now, now, str(delegation_id), str(claim_token)),
            ).rowcount
            self._conn.commit()
            return changed == 1

    def mark_observed(self, delegation_id: str) -> bool:
        """Terminal observational delivery with no target model turn."""
        now = time.time()
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='observed', delivered_at=?,
                   updated_at=?, last_error=NULL, wakeup_prompt='', event_json='',
                   claim_token=NULL, claim_owner=NULL, lease_expires_at=NULL
                   WHERE delegation_id=? AND state IN ('pending', 'observed')""",
                (now, now, str(delegation_id)),
            ).rowcount
            self._conn.commit()
            return changed == 1

    def mark_tracker_acked(self, delegation_id: str) -> bool:
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET tracker_acked_at=COALESCE(tracker_acked_at, ?),
                   updated_at=? WHERE delegation_id=?""",
                (time.time(), time.time(), str(delegation_id)),
            ).rowcount
            self._conn.commit()
            return changed == 1

    def recover_claims(self, *, owner_uuid: str = "") -> int:
        del owner_uuid  # recovery is lease-proven, never owner-name based
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='pending', claim_token=NULL,
                   claim_owner=NULL, lease_expires_at=NULL, claimed_at=NULL,
                   updated_at=?, last_error='recovered_expired_claim'
                   WHERE state='claimed' AND lease_expires_at IS NOT NULL
                   AND lease_expires_at <= ?""",
                (time.time(), time.time()),
            ).rowcount
            self._conn.commit()
            return int(changed)

    def release_owner_claims(self, owner_uuid: str, error: str = "shutdown_abort") -> int:
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='pending', claim_token=NULL,
                   claim_owner=NULL, lease_expires_at=NULL, claimed_at=NULL,
                   updated_at=?, last_error=?
                   WHERE state='claimed' AND claim_owner=?""",
                (time.time(), str(error), str(owner_uuid)),
            ).rowcount
            self._conn.commit()
            return int(changed)

    def prune_delivered(
        self, *, retention_seconds: float = 45 * 24 * 60 * 60,
    ) -> int:
        cutoff = time.time() - max(float(retention_seconds), 31 * 24 * 60 * 60)
        with self._lock:
            changed = self._conn.execute(
                "DELETE FROM delegation_wakeups WHERE state IN ('delivered', 'observed') "
                "AND delivered_at IS NOT NULL AND delivered_at < ?",
                (cutoff,),
            ).rowcount
            self._conn.commit()
            self._repair_modes()
            return int(changed)
