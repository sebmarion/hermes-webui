#!/usr/bin/env python3
"""Run deterministic mechanical gates for bounded conversation loading."""

from __future__ import annotations

import argparse
import hashlib
import http.cookies
import http.client
import json
import math
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import agent_sessions, session_message_paging  # noqa: E402


RECEIPT_SCHEMA_VERSION = 1
DIAGNOSTIC_STAGES = (
    "canonical_resolution",
    "state_message_page",
    "runtime_overlay",
    "derived_view_state",
    "redaction_and_serialize",
)
_TRACE_LOCAL = threading.local()
_ORIGINAL_OPEN_STATE_DB_READONLY = agent_sessions.open_state_db_readonly
_ORIGINAL_OPEN_MESSAGE_DB_READONLY = session_message_paging.open_state_db_readonly
_DIAGNOSTIC_MARKER = "Slow WebUI request completed:"
_BENCHMARK_PASSWORD = "hermes-isolated-benchmark"
_CREDENTIAL_ENV_PREFIXES = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "DEEPSEEK_API_KEY",
    "XIAOMI_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "OLLAMA_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "PERPLEXITY_API_KEY",
    "CEREBRAS_API_KEY",
    "COHERE_API_KEY",
    "FIREWORKS_API_KEY",
    "NOUS_API_KEY",
    "NOVITA_API_KEY",
    "TENCENT_API_KEY",
    "BIGMODEL_API_KEY",
    "GLM_API_KEY",
    "STEPFUN_API_KEY",
    "MINIMAX_API_KEY",
    "LM_API_KEY",
    "LMSTUDIO_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "MEM0_API_KEY",
    "HONCHO_API_KEY",
    "SUPERMEMORY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SIGNAL_API_TOKEN",
    "WHATSAPP_API_TOKEN",
    "FIRECRAWL_API_KEY",
    "FAL_KEY",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "BRAVE_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


@dataclass
class _IsolatedServer:
    process: subprocess.Popen
    port: int
    cookie_header: str
    log_path: Path
    log_handle: object
    connection: http.client.HTTPConnection
    stress_connections: list[http.client.HTTPConnection]
    diagnostic_index: int = 0


def nearest_rank_p95(values: Iterable[float]):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("p95 requires at least one sample")
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_artifact_signature(path: Path) -> dict[str, str]:
    signature = {}
    for label, suffix in (
        ("main", ""),
        ("wal", "-wal"),
        ("shm", "-shm"),
        ("journal", "-journal"),
    ):
        candidate = Path(f"{path}{suffix}")
        if candidate.is_file():
            signature[label] = _sha256(candidate)
    return signature


def _sidecar_artifact_signature(path: Path) -> dict[str, str]:
    signature = {}
    for label, candidate in (
        ("main", path),
        ("backup", Path(f"{path}.bak")),
    ):
        if candidate.is_file():
            signature[label] = _sha256(candidate)
    return signature


def _load_fixture(fixture: Path) -> tuple[dict, Path]:
    fixture = fixture.expanduser().resolve()
    manifest_path = fixture / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("fixture manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("fixture_schema_version") != 1:
        raise ValueError("unsupported fixture schema")
    file_hashes = manifest.get("file_hashes")
    if not isinstance(file_hashes, dict) or "state.db" not in file_hashes:
        raise ValueError("fixture file hashes are incomplete")
    for relative, expected in file_hashes.items():
        candidate = (fixture / str(relative)).resolve()
        if not candidate.is_relative_to(fixture):
            raise ValueError("fixture hash path escapes the fixture root")
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise ValueError(f"fixture hash mismatch: {relative}")
    return manifest, fixture / "state.db"


def _traced_open_state_db_readonly(db_path: Path, log=None):
    conn = _ORIGINAL_OPEN_STATE_DB_READONLY(db_path, log=log)
    statements = getattr(_TRACE_LOCAL, "statements", None)
    if statements is not None:
        conn.set_trace_callback(statements.append)
    return conn


def _traced_open_message_db_readonly(db_path: Path, log=None):
    conn = _ORIGINAL_OPEN_MESSAGE_DB_READONLY(db_path, log=log)
    statements = getattr(_TRACE_LOCAL, "capability_statements", None)
    if statements is not None:
        conn.set_trace_callback(statements.append)
    return conn


def _install_trace_adapter() -> None:
    agent_sessions.open_state_db_readonly = _traced_open_state_db_readonly
    session_message_paging.open_state_db_readonly = _traced_open_message_db_readonly


def _normalize_sql(statement: str) -> str:
    return " ".join(str(statement).lower().split())


def _has_unscoped_scan(statements: list[str]) -> bool:
    for raw in statements:
        statement = _normalize_sql(raw)
        if not statement.startswith("select"):
            continue
        if " from messages" in statement:
            return True
        if " from sessions" in statement and " where " not in statement:
            return True
    return False


def _captured_resolution_plan_is_indexed(
    db_path: Path,
    statements: list[str],
) -> bool:
    relevant = [
        str(statement).strip().rstrip(";")
        for statement in statements
        if _normalize_sql(statement).startswith("select")
        and (
            " from sessions" in _normalize_sql(statement)
            or " from messages" in _normalize_sql(statement)
        )
    ]
    if not relevant:
        return False
    try:
        with closing(_ORIGINAL_OPEN_STATE_DB_READONLY(db_path)) as conn:
            plans = []
            for statement in relevant:
                plans.append(
                    " ".join(
                        str(row[3])
                        for row in conn.execute(
                            f"EXPLAIN QUERY PLAN {statement}"
                        ).fetchall()
                    ).upper()
                )
    except (OSError, sqlite3.Error, IndexError):
        return False
    return all("SEARCH " in plan and "SCAN " not in plan for plan in plans)


def _measure_resolution(
    *,
    db_path: Path,
    requested_id: str,
    expected_canonical_id: str,
    kind: str,
) -> dict:
    statements: list[str] = []
    _TRACE_LOCAL.statements = statements
    started = time.perf_counter_ns()
    try:
        resolution = agent_sessions.resolve_shared_session(db_path, requested_id)
    finally:
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        _TRACE_LOCAL.statements = None

    normalized = [
        _normalize_sql(statement)
        for statement in statements
        if statement.strip()
    ]
    capability_miss = any(
        "pragma table_info(sessions)" in statement
        or "pragma index_list(sessions)" in statement
        for statement in normalized
    )
    fallback_reason = None if resolution.status == "found" else resolution.status
    plan_is_indexed = _captured_resolution_plan_is_indexed(db_path, statements)
    return {
        "kind": kind,
        "elapsed_ms": elapsed_ms,
        "stages": {
            "canonical_resolution": elapsed_ms,
            "state_message_page": 0.0,
            "runtime_overlay": 0.0,
            "derived_view_state": 0.0,
            "redaction_and_serialize": 0.0,
        },
        "sql_count": len(normalized),
        "lineage_depth": len(resolution.member_ids),
        "requested_rows": 0,
        "returned_rows": 0,
        "raw_rows_examined": 0,
        "serialized_bytes": 0,
        "source_mode": "legacy",
        "receipt_generation": None,
        "cache_result": "miss" if capability_miss else "hit",
        "fallback_reason": fallback_reason,
        "duplicate_resolver_count": 0,
        "query_plan_unscoped_scan": (
            _has_unscoped_scan(normalized) or not plan_is_indexed
        ),
        "query_plan_indexed": plan_is_indexed,
        "resolution_status": resolution.status,
        "canonical_matches_manifest": (
            resolution.canonical_id == expected_canonical_id
        ),
    }


def _measure_message_page(
    *,
    db_path: Path,
    requested_id: str,
    expected_canonical_id: str,
    visible_limit: int,
    kind: str,
) -> dict:
    resolution_statements: list[str] = []
    capability_statements: list[str] = []
    agent_sessions.begin_shared_resolution_call_tracking()
    total_started = time.perf_counter_ns()
    try:
        _TRACE_LOCAL.statements = resolution_statements
        resolution_started = time.perf_counter_ns()
        try:
            resolution = agent_sessions.resolve_shared_session(db_path, requested_id)
        finally:
            resolution_elapsed_ms = (
                time.perf_counter_ns() - resolution_started
            ) / 1_000_000
            _TRACE_LOCAL.statements = None

        _TRACE_LOCAL.capability_statements = capability_statements
        page_started = time.perf_counter_ns()
        try:
            page = session_message_paging.read_state_db_message_page(
                db_path=db_path,
                resolution=resolution,
                visible_limit=visible_limit,
                cursor=None,
            )
        finally:
            page_elapsed_ms = (time.perf_counter_ns() - page_started) / 1_000_000
            _TRACE_LOCAL.capability_statements = None
    finally:
        resolver_call_count = agent_sessions.end_shared_resolution_call_tracking()
        elapsed_ms = (time.perf_counter_ns() - total_started) / 1_000_000

    normalized_resolution = [
        _normalize_sql(statement)
        for statement in resolution_statements
        if str(statement).strip()
    ]
    normalized_capability = [
        _normalize_sql(statement)
        for statement in capability_statements
        if str(statement).strip()
    ]
    capability_detail_probe_count = sum(
        not statement.startswith("pragma schema_version")
        for statement in normalized_capability
    )
    resolution_plan_indexed = _captured_resolution_plan_is_indexed(
        db_path,
        resolution_statements,
    )
    query_plan_indexed = bool(resolution_plan_indexed and page.query_plan_indexed)
    return {
        "kind": kind,
        "elapsed_ms": elapsed_ms,
        "stages": {
            "canonical_resolution": resolution_elapsed_ms,
            "state_message_page": page_elapsed_ms,
            "runtime_overlay": 0.0,
            "derived_view_state": 0.0,
            "redaction_and_serialize": 0.0,
        },
        "sql_count": page.sql_count,
        "capability_sql_count": len(normalized_capability),
        "capability_detail_probe_count": capability_detail_probe_count,
        "resolution_sql_count": len(normalized_resolution),
        "total_sql_count": (
            len(normalized_resolution) + len(normalized_capability) + page.sql_count
        ),
        "lineage_depth": len(resolution.member_ids),
        "requested_rows": visible_limit,
        "returned_rows": len(page.messages),
        "visible_count": page.visible_count,
        "raw_rows_examined": page.raw_rows_examined,
        "closure_rows_examined": page.closure_rows_examined,
        "ordinary_serialized_bytes": page.ordinary_serialized_bytes,
        "closure_serialized_bytes": page.closure_serialized_bytes,
        "serialized_bytes": page.serialized_bytes,
        "source_mode": page.mode,
        "receipt_generation": None,
        "cache_result": "miss" if capability_detail_probe_count else "hit",
        "fallback_reason": page.fallback_reason,
        "resolver_call_count": resolver_call_count,
        "duplicate_resolver_count": max(0, resolver_call_count - 1),
        "query_plan_unscoped_scan": (
            _has_unscoped_scan(normalized_resolution) or not query_plan_indexed
        ),
        "query_plan_indexed": query_plan_indexed,
        "resolution_status": resolution.status,
        "canonical_matches_manifest": (
            resolution.canonical_id == expected_canonical_id
        ),
        "has_more": page.has_more,
        "tool_pair_status": page.tool_pair_status,
    }


def _worker_sample(
    fixture: Path,
    *,
    stage: str = "resolution",
    visible_limit: int = 30,
) -> dict:
    manifest, db_path = _load_fixture(fixture)
    _install_trace_adapter()
    measure = _measure_resolution
    kwargs = {}
    if stage == "message-page":
        measure = _measure_message_page
        kwargs["visible_limit"] = visible_limit
    sample = measure(
        db_path=db_path,
        requested_id=manifest["target"]["requested_id"],
        expected_canonical_id=manifest["target"]["canonical_id"],
        kind="process_cold",
        **kwargs,
    )
    sample["worker_pid"] = os.getpid()
    return sample


def _process_cold_sample(
    fixture: Path,
    *,
    stage: str = "resolution",
    visible_limit: int = 30,
) -> dict:
    started = time.perf_counter_ns()
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_worker-fixture",
            str(fixture),
            "--_worker-stage",
            stage,
            "--_worker-visible-limit",
            str(visible_limit),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if completed.returncode != 0:
        raise RuntimeError(
            "process-cold worker failed: "
            + (completed.stderr.strip() or completed.stdout.strip())[-1000:]
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("process-cold worker returned no sample")
    sample = json.loads(lines[-1])
    sample["process_startup_elapsed_ms"] = elapsed_ms
    return sample


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _isolated_server_env(home: Path, state_dir: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if any(key.startswith(prefix) for prefix in _CREDENTIAL_ENV_PREFIXES):
            del env[key]
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
            "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_HOME": str(home),
            "HERMES_BASE_HOME": str(home),
            "HERMES_WEBUI_STATE_DIR": str(state_dir),
            "HERMES_CONFIG_PATH": str(home / "config.yaml"),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(home / "workspace"),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(home / "no-agent"),
            "HERMES_WEBUI_PASSWORD": _BENCHMARK_PASSWORD,
            # Force a structured completion diagnostic for every measured
            # request. The watchdog tick is one second, so normal fast requests
            # complete and emit without producing thread-stack timeout records.
            "HERMES_WEBUI_SLOW_REQUEST_SECONDS": "0.000001",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _health_ready(port: int, process: subprocess.Popen, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated WebUI exited during startup")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.05)
        finally:
            connection.close()
    raise RuntimeError("isolated WebUI did not become healthy")


def _login_cookie(connection: http.client.HTTPConnection) -> str:
    payload = json.dumps({"password": _BENCHMARK_PASSWORD}).encode("utf-8")
    connection.request(
        "POST",
        "/api/auth/login",
        body=payload,
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    if response.status != 200 or body.get("ok") is not True:
        raise RuntimeError("isolated WebUI authentication failed")
    jar = http.cookies.SimpleCookie()
    for header in response.headers.get_all("Set-Cookie", []):
        jar.load(header)
    cookie = "; ".join(f"{name}={morsel.value}" for name, morsel in jar.items())
    if not cookie:
        raise RuntimeError("isolated WebUI authentication returned no cookie")
    return cookie


def _start_isolated_server(
    *,
    home: Path,
    state_dir: Path,
    log_path: Path,
) -> _IsolatedServer:
    port = _free_loopback_port()
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "server.py")],
        cwd=REPO_ROOT,
        env=_isolated_server_env(home, state_dir, port),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        _health_ready(port, process)
        cookie = _login_cookie(connection)
    except Exception:
        connection.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_handle.close()
        raise
    return _IsolatedServer(
        process=process,
        port=port,
        cookie_header=cookie,
        log_path=log_path,
        log_handle=log_handle,
        connection=connection,
        stress_connections=[],
    )


def _stop_isolated_server(server: _IsolatedServer) -> None:
    server.connection.close()
    for connection in server.stress_connections:
        connection.close()
    if server.process.poll() is None:
        server.process.terminate()
        try:
            server.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.process.kill()
            server.process.wait(timeout=5)
    server.log_handle.close()


def _completed_diagnostic_records(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = []
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        marker = text.find(_DIAGNOSTIC_MARKER, offset)
        if marker < 0:
            break
        start = text.find("{", marker + len(_DIAGNOSTIC_MARKER))
        if start < 0:
            break
        try:
            record, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            offset = marker + len(_DIAGNOSTIC_MARKER)
            continue
        offset = start + consumed
        if record.get("method") == "GET" and record.get("path") == "/api/session":
            records.append(record)
    return records


def _read_new_diagnostics(
    server: _IsolatedServer,
    count: int,
    *,
    timeout: float = 5,
) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = _completed_diagnostic_records(server.log_path)
        available = records[server.diagnostic_index :]
        if len(available) >= count:
            selected = available[:count]
            server.diagnostic_index += count
            return selected
        if server.process.poll() is not None:
            raise RuntimeError("isolated WebUI exited before emitting diagnostics")
        time.sleep(0.01)
    records = _completed_diagnostic_records(server.log_path)
    available = max(0, len(records) - server.diagnostic_index)
    raise RuntimeError(
        "isolated WebUI emitted incomplete request diagnostics "
        f"(expected={count}, available={available}, total={len(records)})"
    )


def _named_diagnostic_stages(record: dict) -> dict[str, float]:
    by_name: dict[str, float] = {}
    for stage in record.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        name = str(stage.get("name") or "")
        try:
            duration = float(stage.get("ms") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        by_name[name] = by_name.get(name, 0.0) + max(0.0, duration)
    return {
        "canonical_resolution": by_name.get("canonical_resolution", 0.0),
        "state_message_page": by_name.get("t1_after_get_session_check", 0.0),
        "runtime_overlay": by_name.get("t2_after_state_db_load", 0.0),
        "derived_view_state": by_name.get("t3_after_model_resolve", 0.0),
        "redaction_and_serialize": (
            by_name.get("t4_after_compact_and_merge", 0.0)
            + by_name.get("t5_after_redact", 0.0)
            + by_name.get("t6_after_json_write", 0.0)
        ),
    }


def _prepare_stress_connections(
    server: _IsolatedServer,
    concurrency: int,
) -> None:
    for _ in range(concurrency):
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.port,
            timeout=10,
        )
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            connection.close()
            raise RuntimeError("isolated WebUI stress connection failed")
        server.stress_connections.append(connection)


def _issue_authenticated_session_open(
    server: _IsolatedServer,
    manifest: dict,
    *,
    kind: str,
    connection: http.client.HTTPConnection | None = None,
    message_count_contract: str = "manifest_exact",
    sidecar_variant: str = "valid",
) -> dict:
    query = urllib.parse.urlencode(
        {
            "session_id": manifest["target"]["requested_id"],
            "messages": 0,
            "resolve_model": 0,
        }
    )
    connection = connection or server.connection
    started = time.perf_counter_ns()
    connection.request(
        "GET",
        f"/api/session?{query}",
        headers={"Cookie": server.cookie_header, "Accept": "application/json"},
    )
    response = connection.getresponse()
    body = response.read()
    status = int(response.status)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    payload = json.loads(body.decode("utf-8"))
    session_payload = payload.get("session")
    if not isinstance(session_payload, dict):
        session_payload = payload
    returned_id = (
        session_payload.get("canonical_session_id")
        or session_payload.get("session_id")
        or session_payload.get("id")
    )
    page = session_payload.get("message_page")
    fallback_reason = page.get("fallback_reason") if isinstance(page, dict) else None
    response_messages = session_payload.get("messages")
    returned_rows = len(response_messages) if isinstance(response_messages, list) else 0
    expected_count_key = (
        "active_target_messages" if sidecar_variant == "none" else "target_messages"
    )
    expected_message_count = manifest["counts"][expected_count_key]
    field_sizes = {
        str(key): len(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        )
        for key, value in session_payload.items()
    }
    largest_field, largest_field_bytes = max(
        field_sizes.items(),
        key=lambda item: item[1],
        default=("none", 0),
    )
    return {
        "kind": kind,
        "elapsed_ms": elapsed_ms,
        "stages": {stage: 0.0 for stage in DIAGNOSTIC_STAGES},
        "sql_count": None,
        "lineage_depth": len(manifest["target"]["member_ids"]),
        "requested_rows": 0,
        "returned_rows": returned_rows,
        "raw_rows_examined": 0,
        "serialized_bytes": len(body),
        "largest_response_field": largest_field,
        "largest_response_field_bytes": largest_field_bytes,
        "messages_field_bytes": field_sizes.get("messages", 0),
        "reported_message_count": session_payload.get("message_count"),
        "message_count_matches_manifest": (
            session_payload.get("message_count")
            == expected_message_count
        ),
        "message_count_contract": message_count_contract,
        "sidecar_variant": sidecar_variant,
        "source_mode": "legacy",
        "receipt_generation": None,
        "cache_result": "not_observed",
        "fallback_reason": fallback_reason,
        "duplicate_resolver_count": None,
        "query_plan_unscoped_scan": None,
        "query_plan_indexed": None,
        "http_status": status,
        "authenticated": True,
        "canonical_matches_manifest": (
            str(returned_id or "") == manifest["target"]["canonical_id"]
        ),
        "server_pid": int(server.process.pid),
        "completed_ns": time.perf_counter_ns(),
    }


def _attach_http_diagnostic(sample: dict, record: dict) -> dict:
    sample["stages"] = _named_diagnostic_stages(record)
    sample["diagnostic_elapsed_ms"] = float(record.get("elapsed_ms") or 0.0)
    stage_names = [
        str(stage.get("name") or "")
        for stage in (record.get("stages") or [])
        if isinstance(stage, dict)
    ]
    required_stage_names = {
        "canonical_resolution",
        "t1_after_get_session_check",
        "t2_after_state_db_load",
        "t3_after_model_resolve",
        "t4_after_compact_and_merge",
        "t5_after_redact",
        "t6_after_json_write",
    }
    resolver_stage_count = stage_names.count("canonical_resolution")
    resolver_call_count = (record.get("metrics") or {}).get(
        "canonical_resolution_calls"
    )
    sample["resolver_stage_count"] = resolver_stage_count
    sample["resolver_call_count"] = resolver_call_count
    sample["duplicate_resolver_count"] = (
        max(0, int(resolver_call_count) - 1)
        if isinstance(resolver_call_count, int)
        and not isinstance(resolver_call_count, bool)
        else None
    )
    sample["diagnostic_stages_complete"] = required_stage_names.issubset(
        set(stage_names)
    )
    sample["diagnostic_timed"] = (
        sample["diagnostic_elapsed_ms"] > 0
        and sum(sample["stages"].values()) > 0
    )
    sample["diagnostic_completed_route"] = (
        record.get("current_stage") == "t6_after_json_write"
        or "t6_after_json_write" in stage_names
    )
    sample.pop("completed_ns", None)
    return sample


def _install_fixture_sidecar(
    state_dir: Path,
    manifest: dict,
    fixture_root: Path,
    *,
    variant: str,
) -> Path:
    if variant not in {"valid", "mismatched"}:
        raise ValueError("unsupported fixture sidecar variant")
    session_id = str(manifest["target"]["canonical_id"])
    if (
        not session_id
        or Path(session_id).name != session_id
        or "/" in session_id
        or "\\" in session_id
    ):
        raise ValueError("fixture canonical session id is not path-safe")
    relative = f"sidecars/target-{variant}.json"
    if relative not in manifest.get("file_hashes", {}):
        raise ValueError("fixture sidecar hash is missing")
    source = fixture_root / relative
    sessions = state_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    destination = sessions / f"{session_id}.json"
    shutil.copy2(source, destination)
    return destination


def _run_authenticated_http_fixture(
    manifest: dict,
    db_path: Path,
    *,
    warm_count: int,
    process_cold_count: int,
    concurrency: int,
    stress_rounds: int,
    install_sidecar: bool = True,
    sidecar_variant: str = "valid",
) -> tuple[list[dict], dict, bool, bool]:
    with tempfile.TemporaryDirectory(prefix="hermes-conversation-load-") as raw_root:
        root = Path(raw_root)
        home = root / "home"
        state_dir = root / "webui-state"
        home.mkdir()
        state_dir.mkdir()
        working_db = home / "state.db"
        shutil.copy2(db_path, working_db)
        sidecar_path = (
            state_dir
            / "sessions"
            / f"{manifest['target']['canonical_id']}.json"
        )
        if install_sidecar:
            sidecar_path = _install_fixture_sidecar(
                state_dir,
                manifest,
                db_path.parent,
                variant=sidecar_variant,
            )
        count_contract = "manifest_exact"
        observed_sidecar_variant = sidecar_variant if install_sidecar else "none"
        before_signature = _sqlite_artifact_signature(working_db)
        before_sidecar_signature = _sidecar_artifact_signature(sidecar_path)
        samples: list[dict] = []

        warm_server = _start_isolated_server(
            home=home,
            state_dir=state_dir,
            log_path=root / "warm-server.log",
        )
        try:
            _issue_authenticated_session_open(
                warm_server,
                manifest,
                kind="primer",
                message_count_contract=count_contract,
                sidecar_variant=observed_sidecar_variant,
            )
            _read_new_diagnostics(warm_server, 1)
            for _ in range(warm_count):
                sample = _issue_authenticated_session_open(
                    warm_server,
                    manifest,
                    kind="warm",
                    message_count_contract=count_contract,
                    sidecar_variant=observed_sidecar_variant,
                )
                record = _read_new_diagnostics(warm_server, 1)[0]
                samples.append(_attach_http_diagnostic(sample, record))

            if stress_rounds:
                _prepare_stress_connections(warm_server, concurrency)
            for round_index in range(stress_rounds):
                def issue(slot: int, round_index: int = round_index) -> dict:
                    sample = _issue_authenticated_session_open(
                        warm_server,
                        manifest,
                        kind="stress",
                        connection=warm_server.stress_connections[slot],
                        message_count_contract=count_contract,
                        sidecar_variant=observed_sidecar_variant,
                    )
                    sample["stress_round"] = round_index
                    sample["stress_slot"] = slot
                    return sample

                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    round_samples = list(pool.map(issue, range(concurrency)))
                records = _read_new_diagnostics(warm_server, len(round_samples))
                ordered_samples = sorted(
                    round_samples,
                    key=lambda sample: sample["completed_ns"],
                )
                for sample, record in zip(ordered_samples, records, strict=True):
                    _attach_http_diagnostic(sample, record)
                samples.extend(round_samples)
        finally:
            _stop_isolated_server(warm_server)

        for index in range(process_cold_count):
            cold_server = _start_isolated_server(
                home=home,
                state_dir=state_dir,
                log_path=root / f"cold-server-{index}.log",
            )
            try:
                sample = _issue_authenticated_session_open(
                    cold_server,
                    manifest,
                    kind="process_cold",
                    message_count_contract=count_contract,
                    sidecar_variant=observed_sidecar_variant,
                )
                record = _read_new_diagnostics(cold_server, 1)[0]
                samples.append(_attach_http_diagnostic(sample, record))
            finally:
                _stop_isolated_server(cold_server)

        database_unchanged = (
            before_signature == _sqlite_artifact_signature(working_db)
        )
        sidecar_unchanged = (
            before_sidecar_signature
            == _sidecar_artifact_signature(sidecar_path)
        )
        return samples, _summary(samples), database_unchanged, sidecar_unchanged


def _memory_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return int(page_size) * int(page_count)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _environment(db_path: Path) -> dict:
    return {
        "cpu": platform.processor() or platform.machine() or "unknown",
        "cpu_count": os.cpu_count(),
        "memory_bytes": _memory_bytes(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "database_size_bytes": db_path.stat().st_size,
        "commit": _git_commit(),
        "measurement_scope": "authenticated_http_with_resolver_probe",
        "authenticated_http": True,
        "process_cold_definition": "new_server_process_first_authenticated_open",
    }


def _summary(samples: list[dict]) -> dict:
    warm = [sample["elapsed_ms"] for sample in samples if sample["kind"] == "warm"]
    cold = [
        sample["elapsed_ms"]
        for sample in samples
        if sample["kind"] == "process_cold"
    ]
    stress = [
        sample["elapsed_ms"] for sample in samples if sample["kind"] == "stress"
    ]
    return {
        "warm_count": len(warm),
        "process_cold_count": len(cold),
        "stress_count": len(stress),
        "warm_p95_ms": nearest_rank_p95(warm),
        "process_cold_p95_ms": nearest_rank_p95(cold),
        "stress_p95_ms": nearest_rank_p95(stress) if stress else 0.0,
        "max_ms": max(sample["elapsed_ms"] for sample in samples),
    }


def _sql_signature(samples: list[dict]) -> dict[str, list[int]]:
    signature: dict[str, set[int]] = {}
    for sample in samples:
        key = f"{sample['kind']}:{sample['cache_result']}"
        signature.setdefault(key, set()).add(int(sample["sql_count"]))
    return {key: sorted(values) for key, values in sorted(signature.items())}


def _evaluate_mechanical_samples(samples: list[dict]) -> list[str]:
    failures = []
    for sample in samples:
        depth = max(1, int(sample["lineage_depth"]))
        ceiling = (10 if sample["cache_result"] == "miss" else 4) + 2 * depth
        if int(sample["sql_count"]) > ceiling:
            failures.append(
                f"{sample['kind']} SQL {sample['sql_count']} exceeds {ceiling}"
            )
        if sample["query_plan_unscoped_scan"]:
            failures.append(f"{sample['kind']} used an unscoped query")
        if not sample["query_plan_indexed"]:
            failures.append(f"{sample['kind']} query plan was not indexed")
        if sample["duplicate_resolver_count"] != 0:
            failures.append(f"{sample['kind']} duplicated canonical resolution")
        if sample["resolution_status"] != "found":
            failures.append(
                f"{sample['kind']} resolution status={sample['resolution_status']}"
            )
        if not sample["canonical_matches_manifest"]:
            failures.append(f"{sample['kind']} canonical target drifted")
        if sample["kind"] in {"warm", "stress"} and sample["cache_result"] != "hit":
            failures.append(f"{sample['kind']} repeated capability probes")
        if sample["kind"] == "process_cold" and sample["cache_result"] != "miss":
            failures.append("process-cold sample did not start with an empty cache")
    return failures


def _evaluate_http_samples(samples: list[dict], summary: dict) -> list[str]:
    failures = []
    for sample in samples:
        if sample["http_status"] != 200:
            failures.append(f"{sample['kind']} HTTP status was not 200")
        if not sample["authenticated"]:
            failures.append(f"{sample['kind']} request was not authenticated")
        if not sample["canonical_matches_manifest"]:
            failures.append(f"{sample['kind']} HTTP canonical target drifted")
        if set(sample["stages"]) != set(DIAGNOSTIC_STAGES):
            failures.append(f"{sample['kind']} diagnostics stages were incomplete")
        if not sample.get("diagnostic_stages_complete"):
            failures.append(
                f"{sample['kind']} underlying diagnostic stages were incomplete"
            )
        if not sample.get("diagnostic_timed"):
            failures.append(f"{sample['kind']} diagnostic timings were empty")
        if not sample["diagnostic_completed_route"]:
            failures.append(f"{sample['kind']} did not complete the detail route")
        if sample.get("resolver_stage_count") != 1:
            failures.append(
                f"{sample['kind']} resolver stage count was not exactly one"
            )
        if sample.get("resolver_call_count") != 1:
            failures.append(
                f"{sample['kind']} actual resolver call count was not exactly one"
            )
        if sample.get("duplicate_resolver_count") != 0:
            failures.append(f"{sample['kind']} duplicated canonical resolution")
        if sample.get("returned_rows") != 0:
            failures.append(
                f"{sample['kind']} metadata open serialized transcript rows"
            )
        if int(sample.get("messages_field_bytes") or 0) > 2:
            failures.append(
                f"{sample['kind']} metadata open carried a message payload"
            )
        if int(sample.get("serialized_bytes") or 0) >= 64 * 1024:
            failures.append(f"{sample['kind']} metadata response reached 64KiB")
        count_contract = sample.get("message_count_contract")
        if count_contract != "manifest_exact":
            failures.append(f"{sample['kind']} message_count contract was invalid")
        if not sample.get("message_count_matches_manifest"):
            failures.append(
                f"{sample['kind']} metadata message_count drifted from the manifest"
            )
    if summary["warm_p95_ms"] >= 250:
        failures.append("warm p95 is not below 250ms")
    if summary["process_cold_p95_ms"] >= 2_000:
        failures.append("process-cold p95 is not below 2s")
    if summary["max_ms"] >= 5_000:
        failures.append("a request reached the 5s ceiling")
    return failures


def _run_mechanical_fixture(
    fixture: Path,
    *,
    warm_count: int,
    process_cold_count: int,
    concurrency: int,
    stress_rounds: int,
) -> tuple[dict, Path, list[dict], dict, bool]:
    manifest, db_path = _load_fixture(fixture)
    before_signature = _sqlite_artifact_signature(db_path)
    _install_trace_adapter()
    requested_id = manifest["target"]["requested_id"]
    canonical_id = manifest["target"]["canonical_id"]

    _measure_resolution(
        db_path=db_path,
        requested_id=requested_id,
        expected_canonical_id=canonical_id,
        kind="primer",
    )
    samples = [
        _measure_resolution(
            db_path=db_path,
            requested_id=requested_id,
            expected_canonical_id=canonical_id,
            kind="warm",
        )
        for _ in range(warm_count)
    ]
    samples.extend(_process_cold_sample(fixture) for _ in range(process_cold_count))

    def stress_sample(index: int) -> dict:
        sample = _measure_resolution(
            db_path=db_path,
            requested_id=requested_id,
            expected_canonical_id=canonical_id,
            kind="stress",
        )
        sample["stress_slot"] = index
        return sample

    for round_index in range(stress_rounds):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            round_samples = list(pool.map(stress_sample, range(concurrency)))
        for sample in round_samples:
            sample["stress_round"] = round_index
        samples.extend(round_samples)

    after_signature = _sqlite_artifact_signature(db_path)
    summary = _summary(samples)
    return (
        manifest,
        db_path,
        samples,
        summary,
        before_signature == after_signature,
    )


def _run_message_page_fixture(
    fixture: Path,
    *,
    visible_limit: int,
    warm_count: int,
    process_cold_count: int,
    concurrency: int,
    stress_rounds: int,
) -> tuple[dict, Path, list[dict], dict, bool]:
    manifest, db_path = _load_fixture(fixture)
    before_signature = _sqlite_artifact_signature(db_path)
    _install_trace_adapter()
    session_message_paging.clear_message_paging_capability_cache()
    requested_id = manifest["target"]["requested_id"]
    canonical_id = manifest["target"]["canonical_id"]

    _measure_message_page(
        db_path=db_path,
        requested_id=requested_id,
        expected_canonical_id=canonical_id,
        visible_limit=visible_limit,
        kind="primer",
    )
    samples = [
        _measure_message_page(
            db_path=db_path,
            requested_id=requested_id,
            expected_canonical_id=canonical_id,
            visible_limit=visible_limit,
            kind="warm",
        )
        for _ in range(warm_count)
    ]
    samples.extend(
        _process_cold_sample(
            fixture,
            stage="message-page",
            visible_limit=visible_limit,
        )
        for _ in range(process_cold_count)
    )

    def stress_sample(index: int) -> dict:
        sample = _measure_message_page(
            db_path=db_path,
            requested_id=requested_id,
            expected_canonical_id=canonical_id,
            visible_limit=visible_limit,
            kind="stress",
        )
        sample["stress_slot"] = index
        return sample

    for round_index in range(stress_rounds):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            round_samples = list(pool.map(stress_sample, range(concurrency)))
        for sample in round_samples:
            sample["stress_round"] = round_index
        samples.extend(round_samples)

    after_signature = _sqlite_artifact_signature(db_path)
    return (
        manifest,
        db_path,
        samples,
        _summary(samples),
        before_signature == after_signature,
    )


def _evaluate_message_page_samples(samples: list[dict], summary: dict) -> list[str]:
    failures = []
    for sample in samples:
        kind = sample["kind"]
        depth = max(1, int(sample["lineage_depth"]))
        requested_rows = int(sample["requested_rows"])
        raw_ceiling = max(256, min(2048, 8 * requested_rows)) + 64
        if int(sample["capability_sql_count"]) > 6:
            failures.append(f"{kind} capability SQL exceeded 6")
        if sample["cache_result"] == "hit" and int(
            sample["capability_detail_probe_count"]
        ):
            failures.append(f"{kind} repeated capability detail probes")
        if sample["cache_result"] == "miss" and not int(
            sample["capability_detail_probe_count"]
        ):
            failures.append(f"{kind} capability miss did not inspect schema")
        if int(sample["sql_count"]) > 3 + depth:
            failures.append(f"{kind} paging SQL exceeded {3 + depth}")
        if int(sample["raw_rows_examined"]) > raw_ceiling:
            failures.append(f"{kind} raw rows exceeded {raw_ceiling}")
        if int(sample["closure_rows_examined"]) > 64:
            failures.append(f"{kind} closure rows exceeded 64")
        if int(sample["ordinary_serialized_bytes"]) > 2 * 1024 * 1024:
            failures.append(f"{kind} ordinary bytes exceeded 2 MiB")
        if int(sample["closure_serialized_bytes"]) > 512 * 1024:
            failures.append(f"{kind} closure bytes exceeded 512 KiB")
        if int(sample["serialized_bytes"]) > 2_621_440:
            failures.append(f"{kind} combined bytes exceeded 2.5 MiB")
        if sample["resolver_call_count"] != 1:
            failures.append(f"{kind} resolver count was not exactly one")
        if sample["duplicate_resolver_count"] != 0:
            failures.append(f"{kind} duplicated canonical resolution")
        if not sample["query_plan_indexed"]:
            failures.append(f"{kind} query plan was not indexed")
        if sample["query_plan_unscoped_scan"]:
            failures.append(f"{kind} used an unscoped query")
        if not sample["canonical_matches_manifest"]:
            failures.append(f"{kind} canonical target drifted")
        if sample["source_mode"] != "cursor_v1":
            failures.append(f"{kind} source mode was not cursor_v1")
        if sample["fallback_reason"] is not None:
            failures.append(f"{kind} unexpectedly fell back")
        if kind in {"warm", "stress"} and sample["cache_result"] != "hit":
            failures.append(f"{kind} repeated capability probes")
        if kind == "process_cold" and sample["cache_result"] != "miss":
            failures.append("process-cold sample did not start with an empty cache")
    if summary["warm_p95_ms"] >= 1_000:
        failures.append("warm p95 is not below 1s")
    if summary["process_cold_p95_ms"] >= 2_000:
        failures.append("process-cold p95 is not below 2s")
    if summary["max_ms"] >= 5_000:
        failures.append("a message-page request reached the 5s ceiling")
    return failures


def _message_page_work_signature(samples: list[dict]) -> dict[str, dict[str, list]]:
    fields = (
        "sql_count",
        "capability_sql_count",
        "resolution_sql_count",
        "raw_rows_examined",
        "source_mode",
    )
    signature = {}
    for field in fields:
        by_kind: dict[str, set] = {}
        for sample in samples:
            key = f"{sample['kind']}:{sample['cache_result']}"
            by_kind.setdefault(key, set()).add(sample[field])
        signature[field] = {
            key: sorted(values)
            for key, values in sorted(by_kind.items())
        }
    return signature


def _message_page_fixture_receipt(
    manifest: dict,
    db_path: Path,
    samples: list[dict],
    summary: dict,
    database_unchanged: bool,
) -> dict:
    environment = _environment(db_path)
    environment["authenticated_http"] = False
    environment["measurement_scope"] = "direct_read_only_message_page"
    return {
        "fixture": {
            "schema_version": manifest["fixture_schema_version"],
            "scale": manifest["scale"],
            "agent_contract": manifest["agent_contract"],
            "seed": manifest["seed"],
            "state_db_sha256": manifest["file_hashes"]["state.db"],
            "database_unchanged": bool(database_unchanged),
        },
        "environment": environment,
        "samples": samples,
        "summary": summary,
        "primer_count": 1,
        "work_signature": _message_page_work_signature(samples),
    }


def _comparison_failures(base: dict, comparison: dict) -> list[str]:
    failures = []
    if base["sql_signature"] != comparison["sql_signature"]:
        failures.append("scaling fixture changed SQL statement counts")
    if any(
        sample["raw_rows_examined"]
        for sample in comparison["mechanical_samples"]
    ):
        failures.append("resolution stage unexpectedly examined message rows")
    for field in ("warm_p95_ms", "process_cold_p95_ms"):
        baseline = float(base["summary"][field])
        observed = float(comparison["summary"][field])
        allowance = max(100.0, baseline * 0.2)
        if observed - baseline > allowance:
            failures.append(f"scaling {field} regressed beyond allowance")
    return failures


def _fixture_receipt(
    manifest: dict,
    db_path: Path,
    mechanical_samples: list[dict],
    mechanical_summary: dict,
    http_samples: list[dict],
    http_summary: dict,
    database_unchanged: bool,
    sidecar_unchanged: bool,
    sidecar_variant: str,
) -> dict:
    return {
        "fixture": {
            "schema_version": manifest["fixture_schema_version"],
            "scale": manifest["scale"],
            "agent_contract": manifest["agent_contract"],
            "seed": manifest["seed"],
            "state_db_sha256": manifest["file_hashes"]["state.db"],
            "database_unchanged": bool(database_unchanged),
            "sidecar_unchanged": bool(sidecar_unchanged),
            "sidecar_variant": sidecar_variant,
        },
        "environment": _environment(db_path),
        "samples": http_samples,
        "summary": http_summary,
        "mechanical_samples": mechanical_samples,
        "mechanical_summary": mechanical_summary,
        "primer_count": 2,
        "primer_counts": {
            "authenticated_http": 1,
            "mechanical": 1,
        },
        "sql_signature": _sql_signature(mechanical_samples),
    }


def run_resolution_benchmark(args) -> tuple[dict, bool]:
    (
        manifest,
        db_path,
        mechanical_samples,
        mechanical_summary,
        mechanical_unchanged,
    ) = _run_mechanical_fixture(
        args.fixture,
        warm_count=args.warm,
        process_cold_count=args.process_cold,
        concurrency=args.concurrency,
        stress_rounds=args.stress_rounds,
    )
    (
        http_samples,
        http_summary,
        http_database_unchanged,
        http_sidecar_unchanged,
    ) = _run_authenticated_http_fixture(
        manifest,
        db_path,
        warm_count=args.warm,
        process_cold_count=args.process_cold,
        concurrency=args.concurrency,
        stress_rounds=args.stress_rounds,
        sidecar_variant=args.sidecar_variant,
    )
    database_unchanged = mechanical_unchanged and http_database_unchanged
    base = _fixture_receipt(
        manifest,
        db_path,
        mechanical_samples,
        mechanical_summary,
        http_samples,
        http_summary,
        database_unchanged,
        http_sidecar_unchanged,
        args.sidecar_variant,
    )
    failures = _evaluate_mechanical_samples(mechanical_samples)
    failures.extend(_evaluate_http_samples(http_samples, http_summary))
    if not mechanical_unchanged:
        failures.append("benchmark mutated fixture state.db")
    if not http_database_unchanged:
        failures.append("authenticated WebUI mutated its isolated state.db copy")
    if not http_sidecar_unchanged:
        failures.append("authenticated WebUI mutated its isolated input sidecar")

    comparison_receipt = None
    if args.compare_fixture is not None:
        (
            compare_manifest,
            compare_db,
            compare_mechanical_samples,
            compare_mechanical_summary,
            compare_mechanical_unchanged,
        ) = _run_mechanical_fixture(
            args.compare_fixture,
            warm_count=args.warm,
            process_cold_count=args.process_cold,
            concurrency=args.concurrency,
            stress_rounds=args.stress_rounds,
        )
        (
            compare_http_samples,
            compare_http_summary,
            compare_http_database_unchanged,
            compare_http_sidecar_unchanged,
        ) = _run_authenticated_http_fixture(
            compare_manifest,
            compare_db,
            warm_count=args.warm,
            process_cold_count=args.process_cold,
            concurrency=args.concurrency,
            stress_rounds=args.stress_rounds,
            sidecar_variant=args.sidecar_variant,
        )
        compare_database_unchanged = (
            compare_mechanical_unchanged and compare_http_database_unchanged
        )
        comparison_receipt = _fixture_receipt(
            compare_manifest,
            compare_db,
            compare_mechanical_samples,
            compare_mechanical_summary,
            compare_http_samples,
            compare_http_summary,
            compare_database_unchanged,
            compare_http_sidecar_unchanged,
            args.sidecar_variant,
        )
        failures.extend(_evaluate_mechanical_samples(compare_mechanical_samples))
        failures.extend(
            _evaluate_http_samples(compare_http_samples, compare_http_summary)
        )
        failures.extend(_comparison_failures(base, comparison_receipt))
        if not compare_mechanical_unchanged:
            failures.append("comparison benchmark mutated fixture state.db")
        if not compare_http_database_unchanged:
            failures.append(
                "comparison authenticated WebUI mutated its isolated state.db copy"
            )
        if not compare_http_sidecar_unchanged:
            failures.append(
                "comparison authenticated WebUI mutated its isolated input sidecar"
            )

    receipt = {
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "stage": "resolution",
        "environment": base["environment"],
        "fixture": base["fixture"],
        "samples": base["samples"],
        "summary": base["summary"],
        "mechanical_samples": base["mechanical_samples"],
        "mechanical_summary": base["mechanical_summary"],
        "primer_count": base["primer_count"],
        "primer_counts": base["primer_counts"],
        "gates": {
            "passed": not failures,
            "failures": failures,
            "sql_signature": base["sql_signature"],
        },
        "comparison": comparison_receipt,
    }
    return receipt, not failures


def run_message_page_benchmark(args) -> tuple[dict, bool]:
    (
        manifest,
        db_path,
        samples,
        summary,
        database_unchanged,
    ) = _run_message_page_fixture(
        args.fixture,
        visible_limit=args.visible_limit,
        warm_count=args.warm,
        process_cold_count=args.process_cold,
        concurrency=args.concurrency,
        stress_rounds=args.stress_rounds,
    )
    base = _message_page_fixture_receipt(
        manifest,
        db_path,
        samples,
        summary,
        database_unchanged,
    )
    failures = _evaluate_message_page_samples(samples, summary)
    if not database_unchanged:
        failures.append("message-page benchmark mutated fixture state.db")

    comparison_receipt = None
    if args.compare_fixture is not None:
        (
            compare_manifest,
            compare_db,
            compare_samples,
            compare_summary,
            compare_database_unchanged,
        ) = _run_message_page_fixture(
            args.compare_fixture,
            visible_limit=args.visible_limit,
            warm_count=args.warm,
            process_cold_count=args.process_cold,
            concurrency=args.concurrency,
            stress_rounds=args.stress_rounds,
        )
        comparison_receipt = _message_page_fixture_receipt(
            compare_manifest,
            compare_db,
            compare_samples,
            compare_summary,
            compare_database_unchanged,
        )
        failures.extend(_evaluate_message_page_samples(compare_samples, compare_summary))
        if base["work_signature"] != comparison_receipt["work_signature"]:
            failures.append("scaling fixture changed message-page work counts")
        for field in ("warm_p95_ms", "process_cold_p95_ms"):
            baseline = float(base["summary"][field])
            observed = float(comparison_receipt["summary"][field])
            if observed - baseline > max(100.0, baseline * 0.2):
                failures.append(f"scaling {field} regressed beyond allowance")
        if not compare_database_unchanged:
            failures.append("comparison message-page benchmark mutated fixture state.db")

    receipt = {
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "stage": "message-page",
        "environment": base["environment"],
        "fixture": base["fixture"],
        "samples": base["samples"],
        "summary": base["summary"],
        "primer_count": base["primer_count"],
        "gates": {
            "passed": not failures,
            "failures": failures,
            "work_signature": base["work_signature"],
        },
        "comparison": comparison_receipt,
    }
    return receipt, not failures


def _write_receipt(path: Path, receipt: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _positive(name: str, value: int, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("resolution", "message-page"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--visible-limit", type=int, default=30)
    parser.add_argument("--warm", type=int, default=40)
    parser.add_argument("--process-cold", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--stress-rounds", type=int, default=20)
    parser.add_argument("--compare-fixture", type=Path)
    parser.add_argument(
        "--sidecar-variant",
        choices=("valid", "mismatched"),
        default="valid",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker-fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--_worker-stage",
        choices=("resolution", "message-page"),
        default="resolution",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_worker-visible-limit",
        type=int,
        default=30,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args._worker_fixture is not None:
        try:
            if not 1 <= args._worker_visible_limit <= 100:
                raise ValueError("worker visible limit must be from 1 to 100")
            print(
                json.dumps(
                    _worker_sample(
                        args._worker_fixture,
                        stage=args._worker_stage,
                        visible_limit=args._worker_visible_limit,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        except Exception as exc:
            print(f"worker failed: {exc}", file=sys.stderr)
            return 2
    try:
        if args.stage is None or args.fixture is None or args.output is None:
            raise ValueError("--stage, --fixture, and --output are required")
        _positive("warm", args.warm)
        _positive("process-cold", args.process_cold)
        _positive("concurrency", args.concurrency)
        _positive("stress-rounds", args.stress_rounds, allow_zero=True)
        if not 1 <= args.visible_limit <= 100:
            raise ValueError("visible-limit must be from 1 to 100")
        if args.stage == "message-page":
            receipt, passed = run_message_page_benchmark(args)
        else:
            receipt, passed = run_resolution_benchmark(args)
        _write_receipt(args.output, receipt)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": passed,
                "stage": receipt["stage"],
                "summary": receipt["summary"],
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
