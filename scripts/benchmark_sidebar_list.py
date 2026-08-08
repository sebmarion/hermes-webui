#!/usr/bin/env python3
"""Measure the WebUI sidebar path for five visible threads.

The benchmark builds a disposable Hermes home/state tree containing a small
visible sidebar and a much larger archived history.  It calls the same
session-list builder used by ``GET /api/sessions`` and serializes the response,
but never opens or mutates the user's real state.  Use the JSON receipt for
periodic comparisons; the browser lane in ``benchmark_webui_smoke.py`` covers
network and DOM timing separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SIDEBAR_SLOS = {
    "p95_lt_ms": 1000.0,
    "max_lt_ms": 1500.0,
}
FIXTURE_SCHEMA_VERSION = 1


def nearest_rank_p95(values: Iterable[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


def _git_commit() -> str:
    try:
        import subprocess

        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_signature(root: Path) -> dict[str, str]:
    # The runtime bootstrap creates process coordination files (locks, the
    # generated SOUL.md, and delegation bookkeeping) even for a read-only
    # request.  They are deliberately outside this safety assertion: the
    # benchmark's contract is that the database and persisted sidebar payload
    # stay byte-for-byte unchanged.
    ignored_names = {
        "SOUL.md",
        "async_delegations.json",
        "auth.lock",
    }
    result = {}
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        if path.name in ignored_names or path.suffix == ".lock":
            continue
        relative = path.relative_to(root)
        if not (
            relative == Path("home/state.db")
            or relative == Path("state/settings.json")
            or relative.parts[:2] == ("state", "sessions")
        ):
            continue
        result[str(relative)] = _sha256(path)
    return result


def _validate_output_path(path: Path) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    protected = [Path.home() / ".hermes"]
    for name in ("HERMES_HOME", "HERMES_WEBUI_STATE_DIR"):
        value = os.environ.get(name, "").strip()
        if value:
            protected.append(Path(value).expanduser().resolve(strict=False))
    for root in protected:
        if candidate == root or root in candidate.parents:
            raise ValueError(f"refusing to write benchmark receipt inside Hermes state: {root}")
    if candidate.exists():
        raise ValueError(f"refusing to overwrite existing benchmark receipt: {candidate}")
    return candidate


def _create_fixture(root: Path, *, visible_sessions: int, archived_sessions: int) -> dict:
    """Create a complete, deterministic WebUI + state.db fixture below root."""

    if visible_sessions < 1:
        raise ValueError("visible_sessions must be positive")
    if archived_sessions < 0:
        raise ValueError("archived_sessions must not be negative")

    home = root / "home"
    state = root / "state"
    session_dir = state / "sessions"
    workspace = root / "workspace"
    agent_dir = root / "no-agent"
    for directory in (home, session_dir, workspace, agent_dir):
        directory.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    # Keep the default route synchronous for this backend measurement and keep
    # the fixture free of external CLI rows.
    (state / "settings.json").write_text(
        json.dumps(
            {
                "show_cli_sessions": False,
                "show_previous_messaging_sessions": False,
                "show_cron_sessions": False,
                "show_webhook_sessions": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (home / ".webui-shared-pins-state-db-v2.migrated").touch()

    db_path = home / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=OFF;
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
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp, id);
        CREATE TABLE session_projection_meta (id INTEGER PRIMARY KEY, generation INTEGER NOT NULL);
        INSERT INTO session_projection_meta(id, generation) VALUES (1, 1);
        CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        );
        """
    )

    rows = []
    messages = []
    total = visible_sessions + archived_sessions
    for index in range(total):
        visible = index < visible_sessions
        session_id = (
            f"bench-visible-{index:02d}"
            if visible
            else f"bench-archived-{index - visible_sessions:04d}"
        )
        # Visible rows are newest so they occupy the paint-priority tier; the
        # archived tail deliberately supplies the expensive historical volume.
        activity = float(2_000_000 - index)
        title = "Visible benchmark thread" if visible else "Archived benchmark history"
        rows.append(
            (
                session_id,
                "webui",
                "webui",
                title,
                "benchmark/model",
                None,
                activity,
                activity + 1,
                "complete",
                None,
                2,
                str(workspace),
                0 if visible else 1,
                0,
                activity + 1,
            )
        )
        messages.extend(
            [
                (session_id, "user", f"benchmark request {index}", activity, 1, 0),
                (session_id, "assistant", f"benchmark response {index}", activity + 1, 1, 0),
            ]
        )

    conn.executemany(
        """
        INSERT INTO sessions (
            id, source, session_source, title, model, model_config,
            started_at, ended_at, end_reason, parent_session_id,
            message_count, cwd, archived, pinned, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.executemany(
        """
        INSERT INTO messages(session_id, role, content, timestamp, active, compacted)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        messages,
    )
    conn.commit()
    conn.close()

    index = []
    for row in rows:
        (
            session_id,
            source,
            session_source,
            title,
            _model,
            _model_config,
            started_at,
            _ended_at,
            _end_reason,
            _parent,
            message_count,
            cwd,
            archived,
            pinned,
            last_activity,
        ) = row
        compact = {
            "session_id": session_id,
            "title": title,
            "created_at": started_at,
            "updated_at": last_activity,
            "last_message_at": last_activity,
            "message_count": message_count,
            "archived": bool(archived),
            "pinned": bool(pinned),
            "source": source,
            "session_source": session_source,
            "profile": "default",
            "workspace": cwd,
        }
        index.append(compact)
        (session_dir / f"{session_id}.json").write_text(
            json.dumps(
                {
                    **compact,
                    "messages": [{"role": "user", "content": "fixture"}],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    (session_dir / "_index.json").write_text(
        json.dumps(index, sort_keys=True) + "\n", encoding="utf-8"
    )

    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "visible_sessions": visible_sessions,
        "archived_sessions": archived_sessions,
        "session_dir": str(session_dir),
        "db_path": str(db_path),
        "environment": {
            "HERMES_HOME": str(home),
            "HERMES_BASE_HOME": str(home),
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_CONFIG_PATH": str(config_path),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_SESSION_PROJECTION_V2": "1",
        },
    }


def _measure_sidebar_request(routes, expected_ids: list[str]) -> dict:
    # Bypass the 30-second route cache so each sample measures the actual list
    # builder, not a dictionary copy from a previous sample.
    routes._session_list_cache_clear()
    started = time.perf_counter_ns()
    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_claude_code_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        show_webhook_sessions=False,
        source_filter=None,
        sidebar_source=None,
        archived_limit=None,
        archived_offset=0,
    )
    response = routes._session_list_payload_to_response(payload)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    rows = response.get("sessions") if isinstance(response, dict) else None
    returned_ids = [
        str(row.get("session_id") or row.get("id"))
        for row in rows or []
        if isinstance(row, dict)
    ]
    sample = {
        "kind": "warm",
        "elapsed_ms": round(elapsed_ms, 6),
        "returned_visible_count": len(returned_ids),
        "returned_visible_session_ids": returned_ids,
    }
    if returned_ids != expected_ids:
        sample["failure"] = (
            "sidebar returned unexpected visible rows: "
            f"expected={expected_ids!r} observed={returned_ids!r}"
        )
    return sample


def _new_receipt(
    *,
    command: list[str],
    commit: str,
    fixture: dict,
    samples: list[dict],
    state_unchanged: bool,
    failures: list[str],
) -> dict:
    values = [float(sample.get("elapsed_ms") or 0.0) for sample in samples]
    metrics = {
        "p50": round(statistics.median(values), 6) if values else 0.0,
        "p95": round(nearest_rank_p95(values), 6),
        "max": round(max(values, default=0.0), 6),
    }
    failures = list(failures)
    for sample in samples:
        if sample.get("failure"):
            failures.append(str(sample["failure"]))
    if metrics["p95"] >= SIDEBAR_SLOS["p95_lt_ms"]:
        failures.append(
            f"sidebar p95 reached the {SIDEBAR_SLOS['p95_lt_ms']:.0f}ms ceiling"
        )
    if metrics["max"] >= SIDEBAR_SLOS["max_lt_ms"]:
        failures.append(
            f"sidebar max reached the {SIDEBAR_SLOS['max_lt_ms']:.0f}ms ceiling"
        )
    if not state_unchanged:
        failures.append("isolated fixture state changed during the benchmark")
    returned_ids = next(
        (
            sample.get("returned_visible_session_ids")
            for sample in samples
            if sample.get("returned_visible_session_ids") is not None
        ),
        [],
    )
    return {
        "schema_version": 1,
        "command": command,
        "commit": commit,
        "started_at_utc": _utc_now(),
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "measurement_scope": "in_process_sidebar_builder_and_response_serialization",
        },
        "fixture": {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "visible_sessions": int(fixture["visible_sessions"]),
            "archived_sessions": int(fixture["archived_sessions"]),
        },
        "iterations": len(samples),
        "samples": samples,
        "metrics_ms": metrics,
        "slo": dict(SIDEBAR_SLOS),
        "returned_visible_session_ids": returned_ids,
        "state_unchanged": bool(state_unchanged),
        "failures": failures,
        "overall_passed": not failures,
    }


def run_benchmark(*, iterations: int, visible_sessions: int, archived_sessions: int) -> dict:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="hermes-webui-sidebar-benchmark-") as raw_root:
        root = Path(raw_root).resolve()
        fixture = _create_fixture(
            root,
            visible_sessions=visible_sessions,
            archived_sessions=archived_sessions,
        )
        before = _state_signature(root)
        os.environ.update(fixture["environment"])
        # Import only after the disposable paths are selected. This prevents a
        # benchmark process from binding the production module globals to the
        # user's real Hermes home.
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import api.routes as routes

        expected_ids = [f"bench-visible-{index:02d}" for index in range(visible_sessions)]
        samples = [
            _measure_sidebar_request(routes, expected_ids)
            for _ in range(iterations)
        ]
        after = _state_signature(root)
        receipt = _new_receipt(
            command=["benchmark_sidebar_list.py", f"--iterations={iterations}"],
            commit=_git_commit(),
            fixture=fixture,
            samples=samples,
            state_unchanged=before == after,
            failures=[],
        )
        receipt["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="run five samples")
    parser.add_argument("--iterations", type=int, help="number of list builds to measure")
    parser.add_argument("--visible-sessions", type=int, default=5)
    parser.add_argument("--archived-sessions", type=int, default=1000)
    parser.add_argument("--output", type=Path, help="write the receipt to a new path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    iterations = args.iterations if args.iterations is not None else (5 if args.quick else 20)
    try:
        receipt = run_benchmark(
            iterations=iterations,
            visible_sessions=args.visible_sessions,
            archived_sessions=args.archived_sessions,
        )
        if args.output is not None:
            output = _validate_output_path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"Sidebar benchmark {'PASS' if receipt['overall_passed'] else 'FAIL'} "
            f"(p95={receipt['metrics_ms']['p95']:.3f}ms, "
            f"max={receipt['metrics_ms']['max']:.3f}ms, "
            f"visible={receipt['fixture']['visible_sessions']}, "
            f"archived={receipt['fixture']['archived_sessions']})"
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt["overall_passed"] else 1
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"sidebar benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
