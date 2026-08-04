#!/usr/bin/env python3
"""Run a small, repeatable WebUI backend and browser benchmark.

The command deliberately owns all temporary state.  It is suitable for a
developer's periodic check, not as a machine-independent performance claim:
use the JSON receipt and its commit/environment metadata when comparing runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GENERATOR = REPO_ROOT / "scripts" / "generate_conversation_load_fixture.py"
LOAD_RUNNER = REPO_ROOT / "scripts" / "benchmark_conversation_load.py"
TEST_RUNNER = REPO_ROOT / "scripts" / "test.sh"
DEFAULT_SEED = 4242
DEFAULT_SCALE = "mini"
BACKEND_SLOS = {
    "resolution": {
        "warm_p95_lt_ms": 250.0,
        "process_cold_p95_lt_ms": 2000.0,
        "max_lt_ms": 5000.0,
    },
    "message_page": {
        "warm_p95_lt_ms": 1000.0,
        "process_cold_p95_lt_ms": 2000.0,
        "max_lt_ms": 5000.0,
    },
}
BROWSER_SLOS = {
    "sidebar_ready_p95_lt_ms": 1500.0,
    "session_switch_p95_lt_ms": 1200.0,
    "transcript_render_p95_lt_ms": 750.0,
}
CORRECTNESS_TESTS = {
    "orphan_prune": (
        "tests/test_issue3238_orphaned_cli_sidecar_prune.py::"
        "test_imported_orphan_repair_is_one_background_batch"
    ),
    "archive_sync": (
        "tests/test_gateway_sync.py::"
        "test_archiving_messaging_session_keeps_state_db_history"
    ),
    "gateway_admission": (
        "tests/test_gateway_status_agent_health.py::"
        "test_gateway_status_exposes_redacted_draining_metadata"
    ),
}


def nearest_rank_p95(values: Iterable[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


def _git_commit() -> str:
    try:
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


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _credential_key(key: str) -> bool:
    upper = key.upper()
    if upper in {
        "API_SERVER_KEY",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_WEBUI_AUTH_TOKEN",
        "AUTH_TOKEN",
        "ACCESS_TOKEN",
        "REFRESH_TOKEN",
    }:
        return True
    if upper.endswith("_API_KEY") or upper.endswith("_TOKEN"):
        return True
    return any(
        marker in upper
        for marker in ("PASSWORD", "SECRET", "CREDENTIAL", "AUTHORIZATION")
    )


def build_isolated_environment(root: Path, *, port: int | None = None) -> dict[str, str]:
    """Return a scrubbed server/test environment rooted below ``root``."""

    root = Path(root).expanduser().resolve()
    home = root / "home"
    state = root / "state"
    workspace = root / "workspace"
    agent_dir = root / "no-agent"
    config = root / "config.yaml"
    for directory in (home, state, workspace, agent_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text("{}\n", encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if not _credential_key(key)
    }
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_BASE_HOME": str(home),
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_CONFIG_PATH": str(config),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_PORT": str(port if port is not None else _free_port()),
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def validate_output_path(path: Path, *, state_roots: Iterable[Path] = ()) -> Path:
    """Reject writes into Hermes roots and refuse to overwrite a receipt."""

    candidate = Path(path).expanduser().resolve(strict=False)
    protected = [Path.home() / ".hermes"]
    for name in ("HERMES_HOME", "HERMES_WEBUI_STATE_DIR"):
        value = os.environ.get(name, "").strip()
        if value:
            protected.append(Path(value).expanduser().resolve(strict=False))
    protected.extend(Path(item).expanduser().resolve(strict=False) for item in state_roots)
    for root in protected:
        if candidate == root or root in candidate.parents:
            raise ValueError(f"refusing to write benchmark receipt inside Hermes state: {root}")
    if candidate.exists():
        raise ValueError(f"refusing to overwrite existing benchmark receipt: {candidate}")
    return candidate


def _sample_values(raw_receipt: dict) -> list[float]:
    samples = raw_receipt.get("samples", [])
    return [
        float(sample["elapsed_ms"])
        for sample in samples
        if isinstance(sample, dict) and sample.get("elapsed_ms") is not None
    ]


def backend_stage_receipt(*, stage_name: str, raw_receipt: dict) -> dict:
    """Normalize the existing load-runner receipt into the v1 public shape."""

    summary = raw_receipt.get("summary") or {}
    values = _sample_values(raw_receipt)
    failures = list((raw_receipt.get("gates") or {}).get("failures") or [])
    status = "passed" if not failures and (raw_receipt.get("gates") or {}).get("passed", False) else "failed"
    key = "message_page" if stage_name == "message-page" else "resolution"
    return {
        "status": status,
        "counts": {
            "warm": int(summary.get("warm_count", 0)),
            "process_cold": int(summary.get("process_cold_count", 0)),
            "stress": int(summary.get("stress_count", 0)),
        },
        "metrics_ms": {
            "p50": round(statistics.median(values), 6) if values else 0.0,
            "p95": round(
                nearest_rank_p95(values)
                if values
                else float(summary.get("warm_p95_ms", 0.0)),
                6,
            ),
            "max": round(float(summary.get("max_ms", max(values, default=0.0))), 6),
        },
        "slo": dict(BACKEND_SLOS[key]),
        "failures": failures,
    }


def browser_disabled_receipt() -> dict:
    return {
        "status": "disabled",
        "browser": None,
        "viewport": None,
        "iterations": 0,
        "metrics_ms": {
            "sidebar_ready_p95": None,
            "session_switch_p95": None,
            "transcript_render_p95": None,
        },
        "slo": dict(BROWSER_SLOS),
        "invariants": {
            "virtualization_enabled": None,
            "max_rendered_rows": None,
            "max_anchor_drift_px": None,
            "state_unchanged": None,
        },
        "selector_evidence": {},
        "state_artifacts": {"before": {}, "after": {}},
        "warnings": [],
        "failures": [],
        "coverage_complete": False,
    }


def compare_receipts(current: dict, baseline: dict, *, baseline_path: str) -> dict:
    warnings: list[str] = []
    for stage_name in ("resolution", "message_page", "browser"):
        current_stage = (current.get("stages") or {}).get(stage_name) or {}
        baseline_stage = (baseline.get("stages") or {}).get(stage_name) or {}
        current_metrics = current_stage.get("metrics_ms") or {}
        baseline_metrics = baseline_stage.get("metrics_ms") or {}
        for metric_name in ("p95", "max", "sidebar_ready_p95", "session_switch_p95", "transcript_render_p95"):
            observed = current_metrics.get(metric_name)
            previous = baseline_metrics.get(metric_name)
            if observed is None or previous is None:
                continue
            observed = float(observed)
            previous = float(previous)
            if previous <= 0:
                continue
            delta = observed - previous
            if delta > previous * 0.20 and delta > 10.0:
                warnings.append(
                    f"{stage_name}.{metric_name} increased from {previous:.3f}ms to {observed:.3f}ms"
                )
    return {
        "status": "warning" if warnings else "passed",
        "baseline_receipt": str(baseline_path),
        "baseline_commit": baseline.get("commit", "unknown"),
        "regressions": [],
        "warnings": warnings,
    }


def new_receipt(
    *,
    command: list[str],
    commit: str,
    started_at_utc: str,
    duration_ms: float,
    environment: dict,
    fixture: dict,
    stages: dict,
    correctness: dict,
    comparison: dict,
    coverage_complete: bool,
    overall_passed: bool,
) -> dict:
    return {
        "schema_version": 1,
        "command": command,
        "commit": commit,
        "started_at_utc": started_at_utc,
        "duration_ms": round(float(duration_ms), 3),
        "environment": environment,
        "fixture": fixture,
        "stages": stages,
        "correctness": correctness,
        "comparison": comparison,
        "coverage_complete": bool(coverage_complete),
        "overall_passed": bool(overall_passed),
    }


def _run_command(command: list[str], *, env: dict[str, str], timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _generate_fixture(fixture_dir: Path, *, env: dict[str, str]) -> dict:
    completed = _run_command(
        [
            sys.executable,
            str(FIXTURE_GENERATOR),
            "--scale",
            DEFAULT_SCALE,
            "--agent-contract",
            "current",
            "--seed",
            str(DEFAULT_SEED),
            "--output",
            str(fixture_dir),
        ],
        env=env,
        timeout=120.0,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fixture generation failed: {completed.stderr[-2000:]}")
    return json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))


def _run_backend_stage(
    *,
    stage: str,
    fixture_dir: Path,
    output_path: Path,
    env: dict[str, str],
    warm: int,
    process_cold: int,
    concurrency: int,
    stress_rounds: int,
) -> dict:
    command = [
        sys.executable,
        str(LOAD_RUNNER),
        "--stage",
        stage,
        "--fixture",
        str(fixture_dir),
        "--warm",
        str(warm),
        "--process-cold",
        str(process_cold),
        "--concurrency",
        str(concurrency),
        "--stress-rounds",
        str(stress_rounds),
        "--output",
        str(output_path),
    ]
    completed = _run_command(command, env=env, timeout=300.0)
    try:
        raw = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {
            "summary": {},
            "gates": {
                "passed": False,
                "failures": [
                    f"load runner exited {completed.returncode}: {completed.stderr[-1000:]}"
                ],
            },
        }
    stage_name = "message-page" if stage == "message-page" else "resolution"
    result = backend_stage_receipt(stage_name=stage_name, raw_receipt=raw)
    if completed.returncode != 0 and not result["failures"]:
        result["status"] = "failed"
        result["failures"].append(f"load runner exited {completed.returncode}")
    return result


def _run_correctness(env: dict[str, str]) -> dict:
    results: dict[str, dict] = {}
    for name, test_path in CORRECTNESS_TESTS.items():
        started = time.perf_counter()
        completed = _run_command(
            [str(TEST_RUNNER), "-q", test_path],
            env=env,
            timeout=180.0,
        )
        entry = {
            "status": "passed" if completed.returncode == 0 else "failed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "test": test_path,
        }
        if completed.returncode != 0:
            entry["failure"] = (completed.stdout + completed.stderr)[-2000:]
        results[name] = entry
    return results


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_tree_signature(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        try:
            result[str(path.relative_to(root))] = _sha256(path)
        except OSError:
            continue
    return result


def _wait_for_stable_dom(page, *, timeout_ms: int = 10_000) -> None:
    page.evaluate(
        """async ({timeoutMs}) => {
          const inner = document.querySelector('#msgInner');
          if (!inner) return;
          await new Promise((resolve, reject) => {
            let stable = 0;
            let timer = setTimeout(() => { observer.disconnect(); reject(new Error('DOM settle timeout')); }, timeoutMs);
            const observer = new MutationObserver(() => { stable = 0; });
            observer.observe(inner, {childList: true, subtree: true});
            const frame = () => requestAnimationFrame(() => {
              stable += 1;
              if (stable >= 2) {
                clearTimeout(timer); observer.disconnect(); resolve();
              } else frame();
            });
            frame();
          });
        }""",
        {"timeoutMs": timeout_ms},
    )


def _seed_browser_session(page, *, workspace: str, messages: list[dict], title: str) -> str:
    payload = page.evaluate(
        """async ({workspace, messages, title}) => {
          const response = await fetch('/api/session/import', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({workspace, messages, title}),
          });
          const body = await response.json();
          if (!response.ok || !body.session || !body.session.session_id) {
            throw new Error(`session import failed: ${response.status}`);
          }
          return body.session.session_id;
        }""",
        {"workspace": workspace, "messages": messages, "title": title},
    )
    return str(payload)


def _browser_anchor_invariants(page) -> dict:
    result = page.evaluate(
        """async () => {
          const container = document.querySelector('#messages');
          const rows = Array.from(document.querySelectorAll('#msgInner .msg-row'));
          const before = document.querySelector('[data-virtual-spacer="before"]');
          const after = document.querySelector('[data-virtual-spacer="after"]');
          if (!container || !rows.length) {
            return {virtualization_enabled: Boolean(before && after), max_rendered_rows: rows.length, max_anchor_drift_px: null, missing: 'message rows'};
          }
          const containerRect = container.getBoundingClientRect();
          const candidates = rows.filter(row => row.dataset.messageAnchorKey && row.getClientRects().length);
          if (!candidates.length) {
            return {virtualization_enabled: Boolean(before && after), max_rendered_rows: rows.length, max_anchor_drift_px: null, missing: 'anchor row'};
          }
          const anchor = candidates.reduce((best, row) => {
            const bestDistance = Math.abs(best.getBoundingClientRect().top - (containerRect.top + container.clientHeight / 2));
            const rowDistance = Math.abs(row.getBoundingClientRect().top - (containerRect.top + container.clientHeight / 2));
            return rowDistance < bestDistance ? row : best;
          });
          const key = anchor.dataset.messageAnchorKey;
          const baseline = anchor.getBoundingClientRect().top;
          const drifts = [];
          for (let index = 0; index < 3; index += 1) {
            container.scrollTop = Math.max(0, container.scrollTop - container.clientHeight * 0.4);
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const current = Array.from(document.querySelectorAll('#msgInner .msg-row[data-message-anchor-key]'))
              .find(row => row.dataset.messageAnchorKey === key && row.getClientRects().length);
            if (!current) return {virtualization_enabled: Boolean(before && after), max_rendered_rows: document.querySelectorAll('#msgInner .msg-row').length, max_anchor_drift_px: null, missing: 'anchor recycled'};
            drifts.push(Math.abs(current.getBoundingClientRect().top - baseline));
          }
          return {
            virtualization_enabled: Boolean(before && after),
            max_rendered_rows: Math.max(rows.length, document.querySelectorAll('#msgInner .msg-row').length),
            max_anchor_drift_px: drifts.length ? Math.max(...drifts) : 0,
            anchor_key_present: true,
          };
        }"""
    )
    return result


def _run_browser_lane(root: Path, env: dict[str, str], *, mode: str) -> dict:
    if mode == "off":
        return browser_disabled_receipt()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result = browser_disabled_receipt()
        result["status"] = "skipped" if mode == "optional" else "failed"
        if mode == "required":
            result["failures"] = [f"Playwright is unavailable: {exc}"]
        else:
            result["warnings"] = [f"Playwright is unavailable: {exc}"]
        return result

    artifact_dir = root / "browser-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    process = log_handle = None
    started = time.perf_counter()
    metrics = {"sidebar": [], "switch": [], "transcript": []}
    failures: list[str] = []
    selector_evidence = {}
    state_before = state_after = {}
    browser_version = None
    viewport = {"width": 1440, "height": 900}
    target_messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Benchmark message {index:03d}",
        }
        for index in range(500)
    ]
    small_messages = [{"role": "user", "content": "Small switch target"}]
    try:
        # ``tests`` is intentionally not a Python package in this repository;
        # load the existing browser harness by its checkout-local module path.
        tests_root = str(REPO_ROOT / "tests")
        if tests_root not in sys.path:
            sys.path.insert(0, tests_root)
        from browser_conversation_lifecycle import (
            _capture_page_errors,
            _start_webui_server,
            _terminate_process,
        )

        process, log_handle, _log_path, base_url = _start_webui_server(
            REPO_ROOT, env, artifact_dir
        )
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                dependency_error = str(exc)
                missing_browser = (
                    "executable doesn't exist" in dependency_error.lower()
                    or "playwright install" in dependency_error.lower()
                )
                if mode == "optional" and missing_browser:
                    result = browser_disabled_receipt()
                    result["status"] = "skipped"
                    result["warnings"] = [
                        f"Playwright browser executable is unavailable: {dependency_error}"
                    ]
                    return result
                raise
            browser_version = browser.version
            page = browser.new_page(viewport=viewport)
            page_errors = _capture_page_errors(page)
            page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
            workspace = env["HERMES_WEBUI_DEFAULT_WORKSPACE"]
            target_id = _seed_browser_session(
                page, workspace=workspace, messages=target_messages, title="Benchmark 500 messages"
            )
            small_id = _seed_browser_session(
                page, workspace=workspace, messages=small_messages, title="Benchmark switch target"
            )
            state_before = _state_tree_signature(Path(env["HERMES_WEBUI_STATE_DIR"]))

            navigation_started = time.perf_counter()
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            target_selector = f'#sessionList .session-item[data-sid="{target_id}"]'
            small_selector = f'#sessionList .session-item[data-sid="{small_id}"]'
            page.wait_for_selector(target_selector, state="visible", timeout=30_000)
            target_row = page.locator(target_selector)
            if "active" not in (target_row.get_attribute("class") or ""):
                target_row.click()
                page.wait_for_function(
                    "sid => window.S && window.S.session && window.S.session.session_id === sid",
                    arg=target_id,
                    timeout=30_000,
                )
                _wait_for_stable_dom(page)
            metrics["sidebar"].append((time.perf_counter() - navigation_started) * 1000)
            selector_evidence["sidebar"] = target_selector

            page.wait_for_selector(small_selector, state="visible", timeout=30_000)
            switch_started = time.perf_counter()
            page.locator(small_selector).click()
            page.wait_for_function(
                "sid => window.S && window.S.session && window.S.session.session_id === sid",
                arg=small_id,
                timeout=30_000,
            )
            _wait_for_stable_dom(page)
            metrics["switch"].append((time.perf_counter() - switch_started) * 1000)
            selector_evidence["switch"] = small_selector

            transcript_started = time.perf_counter()
            page.locator(target_selector).click()
            page.wait_for_function(
                "sid => window.S && window.S.session && window.S.session.session_id === sid",
                arg=target_id,
                timeout=30_000,
            )
            page.wait_for_function(
                "() => document.querySelectorAll('#msgInner .msg-row').length > 0",
                timeout=30_000,
            )
            _wait_for_stable_dom(page)
            metrics["transcript"].append((time.perf_counter() - transcript_started) * 1000)
            selector_evidence["transcript"] = "#msgInner .msg-row"
            invariants = _browser_anchor_invariants(page)
            if not invariants.get("virtualization_enabled"):
                failures.append("transcript virtualization spacers were not present")
            if int(invariants.get("max_rendered_rows") or 0) >= 160:
                failures.append("rendered row count reached the 160-row cap")
            if invariants.get("max_anchor_drift_px") is None:
                failures.append(str(invariants.get("missing") or "anchor drift was not measured"))
            elif float(invariants["max_anchor_drift_px"]) > 4.0:
                failures.append("scroll anchor drift exceeded 4px")
            for metric_name, values, slo_key in (
                ("sidebar_ready_p95", metrics["sidebar"], "sidebar_ready_p95_lt_ms"),
                ("session_switch_p95", metrics["switch"], "session_switch_p95_lt_ms"),
                ("transcript_render_p95", metrics["transcript"], "transcript_render_p95_lt_ms"),
            ):
                if nearest_rank_p95(values) >= BROWSER_SLOS[slo_key]:
                    failures.append(f"{metric_name} reached its {BROWSER_SLOS[slo_key]:.0f}ms ceiling")
            if page_errors:
                failures.extend(f"page {kind}: {message}" for kind, message in page_errors)
            state_after = _state_tree_signature(Path(env["HERMES_WEBUI_STATE_DIR"]))
            browser.close()
            invariant_payload = {
                "virtualization_enabled": bool(invariants.get("virtualization_enabled")),
                "max_rendered_rows": int(invariants.get("max_rendered_rows") or 0),
                "max_anchor_drift_px": invariants.get("max_anchor_drift_px"),
                "state_unchanged": state_before == state_after,
            }
    except Exception as exc:
        failures.append(f"browser lane failed: {type(exc).__name__}: {exc}")
        invariant_payload = {
            "virtualization_enabled": None,
            "max_rendered_rows": None,
            "max_anchor_drift_px": None,
            "state_unchanged": None,
        }
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass
        if process is not None:
            try:
                _terminate_process(process)
            except Exception as exc:
                failures.append(f"server cleanup failed: {exc}")
    return {
        "status": "passed" if not failures else "failed",
        "browser": browser_version,
        "viewport": viewport,
        "iterations": 1,
        "metrics_ms": {
            "sidebar_ready_p95": round(nearest_rank_p95(metrics["sidebar"]), 3) if metrics["sidebar"] else None,
            "session_switch_p95": round(nearest_rank_p95(metrics["switch"]), 3) if metrics["switch"] else None,
            "transcript_render_p95": round(nearest_rank_p95(metrics["transcript"]), 3) if metrics["transcript"] else None,
        },
        "slo": dict(BROWSER_SLOS),
        "invariants": invariant_payload,
        "selector_evidence": selector_evidence,
        "state_artifacts": {"before": state_before, "after": state_after},
        "warnings": [],
        "failures": failures,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "coverage_complete": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="run 5 warm / 2 cold / 1 stress sample")
    parser.add_argument(
        "--browser",
        choices=("optional", "required", "off"),
        default="optional",
        help="run the browser lane, skip it when unavailable, or disable it",
    )
    parser.add_argument("--output", type=Path, help="write the JSON receipt to a new path")
    parser.add_argument("--compare", type=Path, help="compare metrics with a prior JSON receipt")
    return parser


def _print_human_summary(receipt: dict) -> None:
    print(
        f"WebUI benchmark {'PASS' if receipt['overall_passed'] else 'FAIL'} "
        f"({receipt['duration_ms']:.0f}ms, commit {receipt['commit'][:12]})"
    )
    for name, stage in receipt["stages"].items():
        metrics = stage.get("metrics_ms") or {}
        if stage.get("status") in {"disabled", "skipped"}:
            print(f"  {name}: {stage['status']}")
        else:
            print(
                f"  {name}: {stage.get('status')} "
                + ", ".join(f"{key}={value}ms" for key, value in metrics.items() if value is not None)
            )
    if receipt.get("comparison", {}).get("warnings"):
        for warning in receipt["comparison"]["warnings"]:
            print(f"  warning: {warning}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.perf_counter()
    started_at_utc = _utc_now()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-webui-benchmark-") as temporary:
            root = Path(temporary).resolve()
            env = build_isolated_environment(root)
            fixture_dir = root / "fixture"
            manifest = _generate_fixture(fixture_dir, env=env)
            warm, process_cold, stress_rounds = (5, 2, 1) if args.quick else (20, 3, 2)
            resolution = _run_backend_stage(
                stage="resolution",
                fixture_dir=fixture_dir,
                output_path=root / "resolution.json",
                env=env,
                warm=warm,
                process_cold=process_cold,
                concurrency=2,
                stress_rounds=stress_rounds,
            )
            message_page = _run_backend_stage(
                stage="message-page",
                fixture_dir=fixture_dir,
                output_path=root / "message-page.json",
                env=env,
                warm=warm,
                process_cold=process_cold,
                concurrency=2,
                stress_rounds=stress_rounds,
            )
            browser = _run_browser_lane(root, env, mode=args.browser)
            correctness = _run_correctness(env)
            stages = {
                "resolution": resolution,
                "message_page": message_page,
                "browser": browser,
            }
            coverage_complete = bool(browser.get("coverage_complete", False))
            comparison = {
                "status": "not_requested",
                "baseline_receipt": None,
                "baseline_commit": None,
                "regressions": [],
                "warnings": [],
            }
            if args.compare is not None:
                baseline_path = Path(args.compare).expanduser().resolve()
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                current_for_compare = {"stages": stages}
                comparison = compare_receipts(
                    current_for_compare, baseline, baseline_path=str(baseline_path)
                )
            correctness_passed = all(entry.get("status") == "passed" for entry in correctness.values())
            hard_stages_passed = all(
                stage.get("status") in {"passed", "disabled", "skipped"}
                for stage in stages.values()
            )
            receipt = new_receipt(
                command=["benchmark_webui_smoke.py", *([] if argv is None else argv)],
                commit=_git_commit(),
                started_at_utc=started_at_utc,
                duration_ms=(time.perf_counter() - started) * 1000,
                environment={
                    "os": platform.platform(),
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                    "browser_mode": args.browser,
                    "sample_mode": "quick" if args.quick else "default",
                },
                fixture={
                    "name": manifest.get("scale", DEFAULT_SCALE),
                    "seed": manifest.get("seed", DEFAULT_SEED),
                    "counts": manifest.get("counts", {}),
                },
                stages=stages,
                correctness=correctness,
                comparison=comparison,
                coverage_complete=coverage_complete,
                overall_passed=hard_stages_passed and correctness_passed,
            )
            if args.output is not None:
                output_path = validate_output_path(
                    args.output,
                    state_roots=(Path(env["HERMES_HOME"]), Path(env["HERMES_WEBUI_STATE_DIR"])),
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _print_human_summary(receipt)
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0 if receipt["overall_passed"] else 1
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
