#!/usr/bin/env python3
"""Generate deterministic, isolated conversation-load benchmark fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


FIXTURE_SCHEMA_VERSION = 1
MIB = 1024 * 1024


@dataclass(frozen=True)
class ScaleSpec:
    session_count: int
    archived_count: int
    target_segments: int
    target_message_count: int
    unrelated_message_count: int
    sidecar_bytes: int


SCALE_SPECS = {
    "mini": ScaleSpec(
        session_count=48,
        archived_count=24,
        target_segments=4,
        target_message_count=200,
        unrelated_message_count=300,
        sidecar_bytes=64 * 1024,
    ),
    "mini-scaling": ScaleSpec(
        session_count=148,
        archived_count=24,
        target_segments=4,
        target_message_count=200,
        unrelated_message_count=2_300,
        sidecar_bytes=64 * 1024,
    ),
    "base": ScaleSpec(
        session_count=2_560,
        archived_count=2_000,
        target_segments=12,
        target_message_count=20_000,
        unrelated_message_count=0,
        sidecar_bytes=100 * MIB,
    ),
    "scaling": ScaleSpec(
        session_count=12_560,
        archived_count=2_000,
        target_segments=12,
        target_message_count=20_000,
        unrelated_message_count=1_000_000,
        sidecar_bytes=100 * MIB,
    ),
}


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _protected_roots() -> tuple[Path, ...]:
    candidates = [Path.home() / ".hermes"]
    for name in ("HERMES_HOME", "HERMES_WEBUI_STATE_DIR"):
        value = os.environ.get(name, "").strip()
        if value:
            candidates.append(Path(value))
    unique = []
    for candidate in candidates:
        resolved = _resolved(candidate)
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def validate_output_path(output: Path) -> Path:
    """Reject real Hermes roots and existing paths before any write."""
    candidate = _resolved(output)
    for protected in _protected_roots():
        if candidate == protected or protected in candidate.parents:
            raise ValueError(
                f"refusing to generate a benchmark inside Hermes state: {protected}"
            )
    if candidate.exists():
        raise ValueError(f"refusing to overwrite existing output: {candidate}")
    return candidate


def _create_schema(conn: sqlite3.Connection, *, agent_contract: str) -> None:
    generation_column = (
        ", message_generation INTEGER NOT NULL DEFAULT 0"
        if agent_contract == "proof-v1"
        else ""
    )
    conn.executescript(
        f"""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA page_size=4096;

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            session_source TEXT,
            title TEXT,
            model TEXT,
            model_config TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            parent_session_id TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            cwd TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            last_activity_at REAL
            {generation_column}
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            reasoning TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            reasoning_content TEXT,
            codex_message_items TEXT
        );
        CREATE INDEX idx_messages_session
            ON messages(session_id, timestamp, id);

        CREATE TABLE session_projection_meta (
            id INTEGER PRIMARY KEY,
            generation INTEGER NOT NULL
        );
        INSERT INTO session_projection_meta(id, generation) VALUES (1, 1);
        """
    )
    if agent_contract == "proof-v1":
        conn.executescript(
            """
            CREATE TABLE agent_contract_capabilities (
                capability TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );
            INSERT INTO agent_contract_capabilities(capability, version)
            VALUES ('target_message_generation', 1);
            """
        )


def _target_ids(spec: ScaleSpec) -> tuple[str, ...]:
    return tuple(f"bench-target-{index:02d}" for index in range(spec.target_segments))


def _insert_sessions(
    conn: sqlite3.Connection,
    spec: ScaleSpec,
    *,
    seed: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    target_ids = _target_ids(spec)
    unrelated_count = spec.session_count - len(target_ids)
    unrelated_ids = tuple(
        f"bench-unrelated-{index:06d}" for index in range(unrelated_count)
    )
    rng = random.Random(seed)

    target_rows = []
    base_per_segment, remainder = divmod(
        spec.target_message_count,
        len(target_ids),
    )
    for index, session_id in enumerate(target_ids):
        message_count = base_per_segment + (1 if index < remainder else 0)
        started_at = float(index * 10 + 1)
        is_tip = index == len(target_ids) - 1
        target_rows.append(
            (
                session_id,
                "webui",
                "webui",
                "Bounded benchmark conversation"
                if index == 0
                else f"Bounded benchmark conversation #{index + 1}",
                "benchmark/model",
                None,
                started_at,
                None if is_tip else started_at + 5,
                None if is_tip else "compression",
                target_ids[index - 1] if index else None,
                message_count,
                "/benchmark/workspace",
                0,
                1 if is_tip else 0,
                started_at + 5,
            )
        )

    unrelated_rows = []
    for index, session_id in enumerate(unrelated_ids):
        started_at = float(100_000 + index)
        unrelated_rows.append(
            (
                session_id,
                "cli" if index % 7 == 0 else "webui",
                "cli" if index % 7 == 0 else "webui",
                f"Unrelated fixture {index}-{rng.randrange(1_000_000):06d}",
                "benchmark/model",
                None,
                started_at,
                started_at + 1,
                "complete",
                None,
                0,
                "/benchmark/unrelated",
                1 if index < spec.archived_count else 0,
                0,
                started_at + 1,
            )
        )

    conn.executemany(
        """
        INSERT INTO sessions (
            id, source, session_source, title, model, model_config,
            started_at, ended_at, end_reason, parent_session_id,
            message_count, cwd, archived, pinned, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [*target_rows, *unrelated_rows],
    )
    return target_ids, unrelated_ids


def _target_message(
    index: int,
    *,
    session_id: str,
    seed: int,
) -> tuple:
    role = "user" if index % 2 == 0 else "assistant"
    content = f"target-{index:06d}-seed-{seed}"
    timestamp = float(index + 1)
    active = 1
    compacted = 0
    tool_call_id = None
    tool_calls = None
    tool_name = None
    reasoning = None

    if index == 0:
        timestamp = None
    elif index == 1:
        active = 0
        content = "inactive-history-row"
    elif index == 2:
        content = json.dumps(
            [
                {"type": "text", "text": "fixture multimodal"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
    elif index == 3:
        role = "assistant"
        content = "tool call"
        tool_calls = json.dumps(
            [{"id": "fixture-call-1", "type": "function"}],
            separators=(",", ":"),
            sort_keys=True,
        )
    elif index == 4:
        role = "tool"
        content = "tool result"
        tool_call_id = "fixture-call-1"
        tool_name = "fixture_lookup"
    elif index == 5:
        role = "system"
        content = "hidden-control-row"
        compacted = 1
    elif index % 97 == 0:
        reasoning = "bounded fixture reasoning"

    return (
        session_id,
        role,
        content,
        timestamp,
        active,
        compacted,
        tool_call_id,
        tool_calls,
        tool_name,
        reasoning,
        None,
        None,
        None,
        None,
    )


def _insert_messages(
    conn: sqlite3.Connection,
    spec: ScaleSpec,
    *,
    seed: int,
    target_ids: tuple[str, ...],
    unrelated_ids: tuple[str, ...],
) -> str:
    insert_sql = """
        INSERT INTO messages (
            session_id, role, content, timestamp, active, compacted,
            tool_call_id, tool_calls, tool_name, reasoning,
            reasoning_details, codex_reasoning_items, reasoning_content,
            codex_message_items
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    visible_digest = hashlib.sha256()
    base_per_segment, remainder = divmod(
        spec.target_message_count,
        len(target_ids),
    )
    segment_counts = [
        base_per_segment + (1 if index < remainder else 0)
        for index in range(len(target_ids))
    ]
    segment_index = 0
    segment_remaining = segment_counts[0]
    batch = []
    for index in range(spec.target_message_count):
        row = _target_message(index, session_id=target_ids[segment_index], seed=seed)
        batch.append(row)
        if row[4] != 0:
            identity = json.dumps(
                [index + 1, *row[:9]],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            visible_digest.update(identity)
            visible_digest.update(b"\n")
        if len(batch) >= 5_000:
            conn.executemany(insert_sql, batch)
            batch.clear()
        segment_remaining -= 1
        if segment_remaining == 0 and segment_index < len(target_ids) - 1:
            segment_index += 1
            segment_remaining = segment_counts[segment_index]
    if batch:
        conn.executemany(insert_sql, batch)

    rng = random.Random(seed ^ 0x5A17)
    batch = []
    for index in range(spec.unrelated_message_count):
        session_id = unrelated_ids[index % len(unrelated_ids)]
        batch.append(
            (
                session_id,
                "user" if index % 2 == 0 else "assistant",
                f"unrelated-{index:07d}-{rng.randrange(1_000_000):06d}",
                float(1_000_000 + index),
                1,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
        if len(batch) >= 10_000:
            conn.executemany(insert_sql, batch)
            batch.clear()
    if batch:
        conn.executemany(insert_sql, batch)

    if spec.unrelated_message_count:
        conn.execute(
            """
            UPDATE sessions
            SET message_count = (
                SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id
            )
            WHERE id LIKE 'bench-unrelated-%'
            """
        )
    return visible_digest.hexdigest()


def _install_proof_triggers(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET message_generation = (
            SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id
        )
        """
    )
    conn.executescript(
        """
        CREATE TRIGGER proof_messages_insert
        AFTER INSERT ON messages
        BEGIN
            UPDATE sessions
            SET message_generation = message_generation + 1
            WHERE id = NEW.session_id;
        END;

        CREATE TRIGGER proof_messages_delete
        AFTER DELETE ON messages
        BEGIN
            UPDATE sessions
            SET message_generation = message_generation + 1
            WHERE id = OLD.session_id;
        END;

        CREATE TRIGGER proof_messages_update_same_session
        AFTER UPDATE ON messages
        WHEN OLD.session_id = NEW.session_id
        BEGIN
            UPDATE sessions
            SET message_generation = message_generation + 1
            WHERE id = NEW.session_id;
        END;

        CREATE TRIGGER proof_messages_update_moved_session
        AFTER UPDATE ON messages
        WHEN OLD.session_id != NEW.session_id
        BEGIN
            UPDATE sessions
            SET message_generation = message_generation + 1
            WHERE id = OLD.session_id;
            UPDATE sessions
            SET message_generation = message_generation + 1
            WHERE id = NEW.session_id;
        END;
        """
    )


def _write_exact_sidecar(
    path: Path,
    *,
    size: int,
    session_id: str,
    receipt_status: str,
    receipt_digest: str,
    message_count: int,
) -> None:
    payload = {
        "session_id": session_id,
        "title": "Legacy benchmark sidecar",
        "created_at": 1,
        "updated_at": 2,
        "workspace": "/__hermes_benchmark__/workspace",
        "model": "benchmark/model",
        "profile": "default",
        "archived": False,
        "pinned": False,
        "message_count": int(message_count),
        "anchor_scene_index": {},
        "reconciliation_receipt": {
            "status": receipt_status,
            "visible_identity_digest": receipt_digest,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not encoded.endswith(b"}"):
        raise AssertionError("sidecar encoding must be a JSON object")
    prefix = (
        encoded[:-1]
        + b',"messages":[{"role":"assistant","content":"'
    )
    suffix = b'"}]}'
    padding_size = size - len(prefix) - len(suffix)
    if padding_size < 0:
        raise ValueError("sidecar size is too small for metadata")

    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = b"x" * min(MIB, max(1, padding_size))
    with path.open("wb") as handle:
        handle.write(prefix)
        remaining = padding_size
        while remaining:
            part = chunk if remaining >= len(chunk) else chunk[:remaining]
            handle.write(part)
            remaining -= len(part)
        handle.write(suffix)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_fixture(
    *,
    output: Path,
    scale: str,
    agent_contract: str,
    seed: int,
) -> dict:
    output = validate_output_path(output)
    spec = SCALE_SPECS[scale]
    output.mkdir(parents=True)
    db_path = output / "state.db"

    with sqlite3.connect(db_path) as conn:
        _create_schema(conn, agent_contract=agent_contract)
        target_ids, unrelated_ids = _insert_sessions(conn, spec, seed=seed)
        visible_digest = _insert_messages(
            conn,
            spec,
            seed=seed,
            target_ids=target_ids,
            unrelated_ids=unrelated_ids,
        )
        target_placeholders = ", ".join("?" for _ in target_ids)
        active_target_message_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM messages "
                f"WHERE session_id IN ({target_placeholders}) "
                "AND (active IS NULL OR active != 0)",
                target_ids,
            ).fetchone()[0]
        )
        if agent_contract == "proof-v1":
            _install_proof_triggers(conn)
        conn.commit()
        conn.execute("VACUUM")

    valid_sidecar = output / "sidecars" / "target-valid.json"
    mismatched_sidecar = output / "sidecars" / "target-mismatched.json"
    _write_exact_sidecar(
        valid_sidecar,
        size=spec.sidecar_bytes,
        session_id=target_ids[-1],
        receipt_status="valid",
        receipt_digest=visible_digest,
        message_count=spec.target_message_count,
    )
    _write_exact_sidecar(
        mismatched_sidecar,
        size=spec.sidecar_bytes,
        session_id=target_ids[-1],
        receipt_status="mismatched",
        receipt_digest="0" * 64,
        message_count=spec.target_message_count,
    )

    files = (db_path, valid_sidecar, mismatched_sidecar)
    file_hashes = {
        path.relative_to(output).as_posix(): _sha256(path)
        for path in files
    }
    manifest = {
        "fixture_schema_version": FIXTURE_SCHEMA_VERSION,
        "seed": int(seed),
        "scale": scale,
        "agent_contract": agent_contract,
        "counts": {
            "sessions": spec.session_count,
            "archived_sessions": spec.archived_count,
            "messages": spec.target_message_count + spec.unrelated_message_count,
            "target_messages": spec.target_message_count,
            "active_target_messages": active_target_message_count,
            "unrelated_messages": spec.unrelated_message_count,
            "sidecar_bytes_each": spec.sidecar_bytes,
        },
        "target": {
            "requested_id": target_ids[0],
            "canonical_id": target_ids[-1],
            "root_id": target_ids[0],
            "tip_id": target_ids[-1],
            "member_ids": list(target_ids),
        },
        "expected_visible_identity_digest": visible_digest,
        "file_hashes": file_hashes,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=tuple(SCALE_SPECS), required=True)
    parser.add_argument(
        "--agent-contract",
        choices=("current", "proof-v1"),
        required=True,
    )
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = generate_fixture(
            output=args.output,
            scale=args.scale,
            agent_contract=args.agent_contract,
            seed=args.seed,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"fixture generation refused or failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "scale": manifest["scale"],
                "agent_contract": manifest["agent_contract"],
                "counts": manifest["counts"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
