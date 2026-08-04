"""Contracts for the isolated five-thread sidebar benchmark."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "benchmark_sidebar_list.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("benchmark_sidebar_list", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("sidebar benchmark script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sidebar_slo_is_sub_second():
    module = _load_module()
    assert module.SIDEBAR_SLOS == {
        "p95_lt_ms": 1000.0,
        "max_lt_ms": 1500.0,
    }


def test_sidebar_receipt_summary_has_stable_shape():
    module = _load_module()
    receipt = module._new_receipt(
        command=["benchmark_sidebar_list.py", "--quick"],
        commit="abc123",
        fixture={"visible_sessions": 5, "archived_sessions": 1000},
        samples=[
            {"kind": "warm", "elapsed_ms": 10.0, "returned_visible_count": 5},
            {"kind": "warm", "elapsed_ms": 20.0, "returned_visible_count": 5},
        ],
        state_unchanged=True,
        failures=[],
    )
    assert receipt["schema_version"] == 1
    assert receipt["metrics_ms"] == {"p50": 15.0, "p95": 20.0, "max": 20.0}
    assert receipt["slo"] == module.SIDEBAR_SLOS
    assert receipt["state_unchanged"] is True
    assert receipt["overall_passed"] is True


def test_cli_runs_isolated_five_thread_probe_without_state_mutation(tmp_path):
    output = tmp_path / "sidebar.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--quick",
            "--visible-sessions",
            "5",
            "--archived-sessions",
            "40",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["fixture"]["visible_sessions"] == 5
    assert receipt["fixture"]["archived_sessions"] == 40
    assert receipt["state_unchanged"] is True
    assert receipt["returned_visible_session_ids"] == [
        "bench-visible-00",
        "bench-visible-01",
        "bench-visible-02",
        "bench-visible-03",
        "bench-visible-04",
    ]
    assert receipt["overall_passed"] is True
