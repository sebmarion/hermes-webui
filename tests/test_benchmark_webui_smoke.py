"""Contracts for the periodic WebUI benchmark receipt and safety boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "benchmark_webui_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("benchmark_webui_smoke", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("benchmark script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_help_exposes_periodic_modes():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--quick" in completed.stdout
    assert "--browser" in completed.stdout
    assert "--compare" in completed.stdout
    assert "--output" in completed.stdout


def test_isolated_environment_scrubs_credentials_and_uses_temp_roots(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-copy")
    monkeypatch.setenv("CUSTOM_API_KEY", "do-not-copy")
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "do-not-copy")
    env = module.build_isolated_environment(tmp_path)
    assert env["HERMES_HOME"] == str(tmp_path / "home")
    assert env["HERMES_WEBUI_STATE_DIR"] == str(tmp_path / "state")
    assert env["HERMES_WEBUI_DEFAULT_WORKSPACE"] == str(tmp_path / "workspace")
    assert env["HERMES_WEBUI_SKIP_ONBOARDING"] == "1"
    assert env["HERMES_WEBUI_HOST"] == "127.0.0.1"
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_API_KEY" not in env
    assert "HERMES_WEBUI_PASSWORD" not in env


def test_output_path_rejects_live_and_existing_targets(tmp_path, monkeypatch):
    module = _load_module()
    hermes_home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    with pytest.raises(ValueError, match="Hermes state"):
        module.validate_output_path(hermes_home / "receipt.json", state_roots=(hermes_home,))
    existing = tmp_path / "already.json"
    existing.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        module.validate_output_path(existing, state_roots=(hermes_home,))


def test_stage_receipt_has_stable_nested_shape():
    module = _load_module()
    stage = module.backend_stage_receipt(
        stage_name="resolution",
        raw_receipt={
            "summary": {
                "warm_count": 5,
                "process_cold_count": 2,
                "stress_count": 2,
                "warm_p95_ms": 12.0,
                "process_cold_p95_ms": 30.0,
                "max_ms": 40.0,
            },
            "gates": {"passed": True, "failures": []},
            "fixture": {"scale": "mini", "seed": 4242, "counts": {"messages": 500}},
        },
    )
    assert stage == {
        "status": "passed",
        "counts": {"warm": 5, "process_cold": 2, "stress": 2},
        "metrics_ms": {"p50": 0.0, "p95": 12.0, "max": 40.0},
        "slo": {
            "warm_p95_lt_ms": 250.0,
            "process_cold_p95_lt_ms": 2000.0,
            "max_lt_ms": 5000.0,
        },
        "failures": [],
    }


def test_compare_warns_only_on_material_regression():
    module = _load_module()
    baseline = {
        "stages": {
            "resolution": {"metrics_ms": {"p95": 100.0, "max": 200.0}},
            "message_page": {"metrics_ms": {"p95": 40.0, "max": 80.0}},
        },
        "commit": "old",
    }
    current = {
        "stages": {
            "resolution": {"metrics_ms": {"p95": 135.0, "max": 210.0}},
            "message_page": {"metrics_ms": {"p95": 42.0, "max": 82.0}},
        }
    }
    comparison = module.compare_receipts(current, baseline, baseline_path="/tmp/base.json")
    assert comparison["status"] == "warning"
    assert comparison["baseline_commit"] == "old"
    assert comparison["warnings"] == [
        "resolution.p95 increased from 100.000ms to 135.000ms"
    ]
    assert comparison["regressions"] == []


def test_browser_disabled_is_explicitly_incomplete():
    module = _load_module()
    stage = module.browser_disabled_receipt()
    assert stage["status"] == "disabled"
    assert stage["metrics_ms"] == {
        "sidebar_ready_p95": None,
        "session_switch_p95": None,
        "transcript_render_p95": None,
    }
    assert stage["invariants"]["virtualization_enabled"] is None
    assert stage["failures"] == []
    assert stage["coverage_complete"] is False


def test_receipt_serialization_has_required_top_level_fields():
    module = _load_module()
    receipt = module.new_receipt(
        command=["benchmark_webui_smoke.py", "--quick"],
        commit="abc123",
        started_at_utc="2026-08-04T00:00:00Z",
        duration_ms=12.5,
        environment={"python": "3.11"},
        fixture={"name": "mini", "seed": 4242, "counts": {}},
        stages={"resolution": {}, "message_page": {}, "browser": {}},
        correctness={},
        comparison={},
        coverage_complete=False,
        overall_passed=True,
    )
    assert set(receipt) == {
        "schema_version",
        "command",
        "commit",
        "started_at_utc",
        "duration_ms",
        "environment",
        "fixture",
        "stages",
        "correctness",
        "comparison",
        "coverage_complete",
        "overall_passed",
    }
    assert receipt["schema_version"] == 1
    json.dumps(receipt)
