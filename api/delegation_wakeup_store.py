"""Durable ownership and delivery state for async delegation wakeups."""

from __future__ import annotations

import json
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
    last_error TEXT
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
        if path is None:
            from api.config import STATE_DIR

            path = Path(STATE_DIR) / "delegation_wakeups.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._connect()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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
                        event_json, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        delegation_id, session_id, session_key, wakeup_prompt,
                        event_json, now, now,
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

    def claim_next(self, session_id: str) -> dict[str, Any] | None:
        token = uuid.uuid4().hex
        now = time.time()
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
                       claimed_at=?, updated_at=?, last_error=NULL
                       WHERE delegation_id=? AND state='pending'""",
                    (token, now, now, delegation_id),
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
                   claimed_at=NULL, updated_at=?, last_error=?
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
                   updated_at=?, last_error=NULL
                   WHERE delegation_id=? AND state='claimed' AND claim_token=?""",
                (now, now, str(delegation_id), str(claim_token)),
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

    def recover_claims(self) -> int:
        with self._lock:
            changed = self._conn.execute(
                """UPDATE delegation_wakeups SET state='pending', claim_token=NULL,
                   claimed_at=NULL, updated_at=?, last_error='recovered_after_restart'
                   WHERE state='claimed'""",
                (time.time(),),
            ).rowcount
            self._conn.commit()
            return int(changed)
