"""Deterministic acceptance contracts for bounded conversation-load benchmarks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "generate_conversation_load_fixture.py"
RUNNER = REPO_ROOT / "scripts" / "benchmark_conversation_load.py"
DIAGNOSTIC_STAGES = {
    "canonical_resolution",
    "state_message_page",
    "runtime_overlay",
    "derived_view_state",
    "redaction_and_serialize",
}


def _load_script(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(path: Path, *args: str, env: dict | None = None, check: bool = True):
    completed = subprocess.run(
        [sys.executable, str(path), *map(str, args)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"{path.name} failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _generate(
    output: Path,
    *,
    contract: str = "current",
    seed: int = 4242,
    scale: str = "mini",
) -> dict:
    _run_script(
        GENERATOR,
        "--scale",
        scale,
        "--agent-contract",
        contract,
        "--seed",
        str(seed),
        "--output",
        str(output),
    )
    return json.loads((output / "manifest.json").read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_fixture_rows(db_path: Path):
    session_columns = (
        "id, source, session_source, title, model, model_config, started_at, "
        "ended_at, end_reason, parent_session_id, message_count, cwd, archived, "
        "pinned, last_activity_at"
    )
    message_columns = (
        "id, session_id, role, content, timestamp, active, compacted, "
        "tool_call_id, tool_calls, tool_name, reasoning, reasoning_details, "
        "codex_reasoning_items, reasoning_content, codex_message_items"
    )
    with sqlite3.connect(db_path) as conn:
        sessions = conn.execute(
            f"SELECT {session_columns} FROM sessions ORDER BY id"
        ).fetchall()
        messages = conn.execute(
            f"SELECT {message_columns} FROM messages ORDER BY id"
        ).fetchall()
    return sessions, messages


def test_fixture_scale_contracts_are_exact():
    generator = _load_script(GENERATOR, "conversation_fixture_scale_contract")

    base = generator.SCALE_SPECS["base"]
    assert base.session_count == 2_560
    assert base.archived_count == 2_000
    assert base.target_segments == 12
    assert base.target_message_count == 20_000
    assert base.unrelated_message_count == 0
    assert base.sidecar_bytes == 100 * 1024 * 1024

    mini = generator.SCALE_SPECS["mini"]
    mini_scaling = generator.SCALE_SPECS["mini-scaling"]
    assert mini_scaling.session_count - mini.session_count == 100
    assert mini_scaling.unrelated_message_count - mini.unrelated_message_count == 2_000
    assert mini_scaling.target_segments == mini.target_segments
    assert mini_scaling.target_message_count == mini.target_message_count

    scaling = generator.SCALE_SPECS["scaling"]
    assert scaling.session_count == 12_560
    assert scaling.archived_count == 2_000
    assert scaling.target_segments == 12
    assert scaling.target_message_count == 20_000
    assert scaling.unrelated_message_count == 1_000_000
    assert scaling.sidecar_bytes == 100 * 1024 * 1024


def test_fixture_generation_is_deterministic_and_manifest_hashes_are_relative(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = _generate(first_dir, seed=7)
    second = _generate(second_dir, seed=7)

    assert first == second
    assert first["fixture_schema_version"] == 1
    assert first["seed"] == 7
    assert first["scale"] == "mini"
    assert first["agent_contract"] == "current"
    assert len(first["target"]["member_ids"]) >= 2
    assert len(first["expected_visible_identity_digest"]) == 64
    assert first["file_hashes"]
    assert all(not Path(relative).is_absolute() for relative in first["file_hashes"])

    for relative, expected_hash in first["file_hashes"].items():
        assert (first_dir / relative).resolve().is_relative_to(first_dir.resolve())
        assert _file_sha256(first_dir / relative) == expected_hash
        assert _file_sha256(second_dir / relative) == expected_hash

    generator = _load_script(GENERATOR, "conversation_fixture_shape_contract")
    sidecar_paths = sorted((first_dir / "sidecars").glob("*.json"))
    assert len(sidecar_paths) == 2
    assert all(
        path.stat().st_size == generator.SCALE_SPECS["mini"].sidecar_bytes
        for path in sidecar_paths
    )
    sidecars = [json.loads(path.read_text(encoding="utf-8")) for path in sidecar_paths]
    statuses = {
        sidecar["reconciliation_receipt"]["status"]: sidecar
        for sidecar in sidecars
    }
    assert set(statuses) == {"valid", "mismatched"}
    assert {
        sidecar["message_count"] for sidecar in sidecars
    } == {first["counts"]["target_messages"]}
    assert all(sidecar["anchor_scene_index"] == {} for sidecar in sidecars)
    assert all(len(sidecar["messages"]) == 1 for sidecar in sidecars)
    assert all(
        len(sidecar["messages"][0]["content"]) > 60 * 1024
        for sidecar in sidecars
    )
    assert (
        statuses["valid"]["reconciliation_receipt"]["visible_identity_digest"]
        == first["expected_visible_identity_digest"]
    )
    assert (
        statuses["mismatched"]["reconciliation_receipt"]["visible_identity_digest"]
        != first["expected_visible_identity_digest"]
    )

    db_path = first_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE archived != 0"
        ).fetchone()[0]
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        active_target_message_count = conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE session_id LIKE 'bench-target-%' "
            "AND (active IS NULL OR active != 0)"
        ).fetchone()[0]
        null_timestamps = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp IS NULL"
        ).fetchone()[0]
        inactive = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE active = 0"
        ).fetchone()[0]
        tool_rows = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tool_call_id IS NOT NULL"
        ).fetchone()[0]
        multimodal = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content LIKE '[%'"
        ).fetchone()[0]
        compacted = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE compacted != 0"
        ).fetchone()[0]
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        capability_table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'agent_contract_capabilities'"
        ).fetchone()
        lineage_rows = conn.execute(
            "SELECT id, parent_session_id, end_reason, message_count "
            "FROM sessions WHERE id LIKE 'bench-target-%' ORDER BY started_at"
        ).fetchall()
        actual_target_counts = dict(
            conn.execute(
                "SELECT session_id, COUNT(*) FROM messages "
                "WHERE session_id LIKE 'bench-target-%' GROUP BY session_id"
            ).fetchall()
        )
        paired_tool_rows = conn.execute(
            "SELECT role, tool_call_id, tool_calls FROM messages "
            "WHERE tool_call_id = 'fixture-call-1' "
            "OR tool_calls LIKE '%fixture-call-1%' ORDER BY id"
        ).fetchall()
        digest = hashlib.sha256()
        target_messages = conn.execute(
            "SELECT id, session_id, role, content, timestamp, active, compacted, "
            "tool_call_id, tool_calls, tool_name FROM messages "
            "WHERE session_id LIKE 'bench-target-%' ORDER BY id"
        ).fetchall()
        for row in target_messages:
            if row[5] == 0:
                continue
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")

    assert session_count == first["counts"]["sessions"]
    assert archived_count == first["counts"]["archived_sessions"]
    assert message_count == first["counts"]["messages"]
    assert active_target_message_count == first["counts"]["active_target_messages"]
    assert null_timestamps and inactive and tool_rows and multimodal and compacted
    assert "message_generation" not in session_columns
    assert capability_table is None
    assert [row[0] for row in lineage_rows] == first["target"]["member_ids"]
    assert lineage_rows[0][1] is None
    assert all(
        row[1] == lineage_rows[index - 1][0]
        for index, row in enumerate(lineage_rows[1:], start=1)
    )
    assert all(row[2] == "compression" for row in lineage_rows[:-1])
    assert lineage_rows[-1][2] is None
    assert all(actual_target_counts[row[0]] == row[3] for row in lineage_rows)
    assert [row[0] for row in paired_tool_rows] == ["assistant", "tool"]
    assert digest.hexdigest() == first["expected_visible_identity_digest"]


def test_fixture_rejects_real_or_configured_hermes_state_paths(tmp_path):
    protected = tmp_path / "real-hermes"
    protected.mkdir()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(protected)
    env["HERMES_WEBUI_STATE_DIR"] = str(protected / "webui")
    output = protected / "benchmark"

    completed = _run_script(
        GENERATOR,
        "--scale",
        "mini",
        "--agent-contract",
        "current",
        "--output",
        str(output),
        env=env,
        check=False,
    )

    assert completed.returncode != 0
    assert not output.exists()
    assert "refus" in (completed.stderr + completed.stdout).lower()

    alias = tmp_path / "state-alias"
    alias.symlink_to(protected, target_is_directory=True)
    alias_output = alias / "benchmark"
    completed = _run_script(
        GENERATOR,
        "--scale",
        "mini",
        "--agent-contract",
        "current",
        "--output",
        str(alias_output),
        env=env,
        check=False,
    )
    assert completed.returncode != 0
    assert not alias_output.exists()


def test_fixture_proof_v1_generation_triggers_cover_all_message_mutations(tmp_path):
    fixture = tmp_path / "proof"
    manifest = _generate(fixture, contract="proof-v1")
    root, destination = manifest["target"]["member_ids"][:2]

    with sqlite3.connect(fixture / "state.db") as conn:
        capability = conn.execute(
            "SELECT version FROM agent_contract_capabilities "
            "WHERE capability = 'target_message_generation'"
        ).fetchone()
        assert capability == (1,)

        def generation(sid):
            return conn.execute(
                "SELECT message_generation FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()[0]

        root_before = generation(root)
        cursor = conn.execute(
            "INSERT INTO messages "
            "(session_id, role, content, timestamp, active, compacted) "
            "VALUES (?, 'user', 'trigger-test', 999999, 1, 0)",
            (root,),
        )
        message_id = cursor.lastrowid
        root_after_insert = generation(root)
        assert root_after_insert > root_before

        conn.execute(
            "UPDATE messages SET active = 0 WHERE id = ?",
            (message_id,),
        )
        root_after_update = generation(root)
        assert root_after_update > root_after_insert

        conn.execute(
            "UPDATE messages SET compacted = 1 WHERE id = ?",
            (message_id,),
        )
        root_after_compacted = generation(root)
        assert root_after_compacted > root_after_update

        conn.execute(
            "UPDATE messages SET role = 'assistant', content = 'edited', "
            "timestamp = 1000000 WHERE id = ?",
            (message_id,),
        )
        root_after_edit = generation(root)
        assert root_after_edit > root_after_compacted

        destination_before = generation(destination)
        conn.execute(
            "UPDATE messages SET session_id = ? WHERE id = ?",
            (destination, message_id),
        )
        assert generation(root) > root_after_edit
        destination_after_move = generation(destination)
        assert destination_after_move > destination_before

        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        assert generation(destination) > destination_after_move


def test_fixture_current_and_proof_v1_share_identical_logical_data(tmp_path):
    current_dir = tmp_path / "current"
    proof_dir = tmp_path / "proof"
    current = _generate(current_dir, contract="current", seed=99)
    proof = _generate(proof_dir, contract="proof-v1", seed=99)

    assert current["counts"] == proof["counts"]
    assert current["target"] == proof["target"]
    assert (
        current["expected_visible_identity_digest"]
        == proof["expected_visible_identity_digest"]
    )
    assert _logical_fixture_rows(current_dir / "state.db") == _logical_fixture_rows(
        proof_dir / "state.db"
    )


def test_resolution_runner_emits_bounded_content_free_receipt(tmp_path):
    fixture = tmp_path / "fixture"
    _generate(fixture)
    output = tmp_path / "result.json"

    _run_script(
        RUNNER,
        "--stage",
        "resolution",
        "--fixture",
        str(fixture),
        "--warm",
        "3",
        "--process-cold",
        "2",
        "--concurrency",
        "2",
        "--stress-rounds",
        "2",
        "--output",
        str(output),
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["receipt_schema_version"] == 1
    assert receipt["stage"] == "resolution"
    assert receipt["primer_count"] == 2
    assert receipt["primer_counts"] == {
        "authenticated_http": 1,
        "mechanical": 1,
    }
    assert receipt["environment"]["authenticated_http"] is True
    assert receipt["environment"]["measurement_scope"] == (
        "authenticated_http_with_resolver_probe"
    )
    assert receipt["environment"]["cpu"]
    assert receipt["environment"]["cpu_count"]
    assert receipt["environment"]["memory_bytes"]
    assert receipt["environment"]["os"]
    assert receipt["environment"]["python"]
    assert receipt["environment"]["sqlite"]
    assert receipt["environment"]["database_size_bytes"] > 0
    assert receipt["environment"]["commit"]
    assert receipt["samples"]
    assert receipt["mechanical_samples"]
    assert receipt["gates"]["passed"] is True
    assert receipt["fixture"]["sidecar_variant"] == "valid"
    assert receipt["fixture"]["sidecar_unchanged"] is True
    assert receipt["summary"]["warm_p95_ms"] < 250
    assert max(sample["elapsed_ms"] for sample in receipt["samples"]) < 5_000

    by_kind = {
        kind: [sample for sample in receipt["samples"] if sample["kind"] == kind]
        for kind in ("warm", "process_cold", "stress")
    }
    assert len(by_kind["warm"]) == 3
    assert len(by_kind["process_cold"]) == 2
    assert len(by_kind["stress"]) == 4
    assert len({sample["server_pid"] for sample in by_kind["process_cold"]}) == 2
    assert {
        (sample["stress_round"], sample["stress_slot"])
        for sample in by_kind["stress"]
    } == {(0, 0), (0, 1), (1, 0), (1, 1)}

    runner = _load_script(RUNNER, "conversation_benchmark_receipt_contract")
    assert receipt["summary"]["warm_p95_ms"] == runner.nearest_rank_p95(
        sample["elapsed_ms"] for sample in by_kind["warm"]
    )
    assert receipt["summary"]["process_cold_p95_ms"] == runner.nearest_rank_p95(
        sample["elapsed_ms"] for sample in by_kind["process_cold"]
    )
    assert receipt["summary"]["stress_p95_ms"] == runner.nearest_rank_p95(
        sample["elapsed_ms"] for sample in by_kind["stress"]
    )

    for sample in receipt["samples"]:
        assert set(sample["stages"]) == DIAGNOSTIC_STAGES
        assert sample["http_status"] == 200
        assert sample["canonical_matches_manifest"] is True
        assert sample["diagnostic_completed_route"] is True
        assert sample["diagnostic_stages_complete"] is True
        assert sample["diagnostic_timed"] is True
        assert sample["resolver_stage_count"] == 1
        assert sample["resolver_call_count"] == 1
        assert sample["duplicate_resolver_count"] == 0
        assert sample["sql_count"] is None
        assert sample["query_plan_indexed"] is None
        assert sample["query_plan_unscoped_scan"] is None
        assert sample["returned_rows"] == 0
        assert sample["messages_field_bytes"] <= 2
        assert sample["serialized_bytes"] < 64 * 1024
        assert sample["message_count_matches_manifest"] is True
        assert sample["message_count_contract"] == "manifest_exact"
        assert sample["sidecar_variant"] == "valid"
        assert "fallback_reason" in sample
        assert sample["serialized_bytes"] > 0

    mechanical_by_kind = {
        kind: [
            sample
            for sample in receipt["mechanical_samples"]
            if sample["kind"] == kind
        ]
        for kind in ("warm", "process_cold", "stress")
    }
    assert len(mechanical_by_kind["warm"]) == 3
    assert len(mechanical_by_kind["process_cold"]) == 2
    assert len(mechanical_by_kind["stress"]) == 4
    assert len(
        {sample["worker_pid"] for sample in mechanical_by_kind["process_cold"]}
    ) == 2

    for sample in receipt["mechanical_samples"]:
        assert set(sample["stages"]) == DIAGNOSTIC_STAGES
        assert sample["duplicate_resolver_count"] == 0
        assert sample["sql_count"] <= 10 + 2 * sample["lineage_depth"]
        if sample["cache_result"] == "hit":
            assert sample["sql_count"] <= 4 + 2 * sample["lineage_depth"]
        assert sample["requested_rows"] >= sample["returned_rows"] >= 0
        assert sample["raw_rows_examined"] >= 0
        assert sample["serialized_bytes"] >= 0
        assert sample["source_mode"] == "legacy"
        assert sample["receipt_generation"] is None
        assert sample["query_plan_indexed"] is True
        assert sample["query_plan_unscoped_scan"] is False
        assert sample["canonical_matches_manifest"] is True
        assert "fallback_reason" in sample
        if sample["kind"] in {"warm", "stress"}:
            assert sample["cache_result"] == "hit"
        else:
            assert sample["cache_result"] == "miss"

    serialized = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert str(fixture) not in serialized
    assert "target-000006-seed-4242" not in serialized


def test_resolution_scaling_comparison_preserves_query_shape_and_passes(tmp_path):
    base_fixture = tmp_path / "base"
    scaling_fixture = tmp_path / "scaling"
    _generate(base_fixture, scale="mini")
    _generate(scaling_fixture, scale="mini-scaling")
    output = tmp_path / "comparison.json"

    _run_script(
        RUNNER,
        "--stage",
        "resolution",
        "--fixture",
        str(base_fixture),
        "--warm",
        "2",
        "--process-cold",
        "1",
        "--concurrency",
        "2",
        "--stress-rounds",
        "1",
        "--compare-fixture",
        str(scaling_fixture),
        "--output",
        str(output),
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    comparison = receipt["comparison"]
    assert receipt["gates"]["passed"] is True
    assert comparison is not None
    assert comparison["fixture"]["scale"] == "mini-scaling"
    assert receipt["gates"]["sql_signature"] == comparison["sql_signature"]
    assert all(
        sample["raw_rows_examined"] == 0
        for sample in comparison["mechanical_samples"]
    )
    for field in ("warm_p95_ms", "process_cold_p95_ms"):
        baseline = receipt["summary"][field]
        observed = comparison["summary"][field]
        assert observed - baseline <= max(100.0, baseline * 0.2)

    serialized = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert "target-000006-seed-4242" not in serialized


def test_message_page_runner_emits_bounded_content_free_receipt(tmp_path):
    fixture = tmp_path / "fixture"
    _generate(fixture)
    output = tmp_path / "message-page.json"

    _run_script(
        RUNNER,
        "--stage",
        "message-page",
        "--fixture",
        str(fixture),
        "--visible-limit",
        "30",
        "--warm",
        "2",
        "--process-cold",
        "1",
        "--concurrency",
        "2",
        "--stress-rounds",
        "1",
        "--output",
        str(output),
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["receipt_schema_version"] == 1
    assert receipt["stage"] == "message-page"
    assert receipt["primer_count"] == 1
    assert receipt["gates"]["passed"] is True
    assert receipt["fixture"]["database_unchanged"] is True
    assert receipt["environment"]["authenticated_http"] is False
    assert receipt["environment"]["measurement_scope"] == (
        "direct_read_only_message_page"
    )

    by_kind = {
        kind: [sample for sample in receipt["samples"] if sample["kind"] == kind]
        for kind in ("warm", "process_cold", "stress")
    }
    assert len(by_kind["warm"]) == 2
    assert len(by_kind["process_cold"]) == 1
    assert len(by_kind["stress"]) == 2
    assert len({sample["worker_pid"] for sample in by_kind["process_cold"]}) == 1

    for sample in receipt["samples"]:
        depth = sample["lineage_depth"]
        raw_ceiling = max(256, min(2048, 8 * sample["requested_rows"])) + 64
        assert set(sample["stages"]) == DIAGNOSTIC_STAGES
        assert sample["resolver_call_count"] == 1
        assert sample["duplicate_resolver_count"] == 0
        assert sample["canonical_matches_manifest"] is True
        assert sample["source_mode"] == "cursor_v1"
        assert sample["fallback_reason"] is None
        assert sample["receipt_generation"] is None
        assert sample["query_plan_indexed"] is True
        assert sample["query_plan_unscoped_scan"] is False
        assert sample["sql_count"] <= 3 + depth
        assert sample["capability_sql_count"] <= 6
        assert sample["raw_rows_examined"] <= raw_ceiling
        assert sample["closure_rows_examined"] <= 64
        assert sample["ordinary_serialized_bytes"] <= 2 * 1024 * 1024
        assert sample["closure_serialized_bytes"] <= 512 * 1024
        assert sample["serialized_bytes"] <= 2_621_440
        assert sample["returned_rows"] >= sample["visible_count"]
        if sample["kind"] in {"warm", "stress"}:
            assert sample["cache_result"] == "hit"
            assert sample["capability_detail_probe_count"] == 0
        else:
            assert sample["cache_result"] == "miss"
            assert sample["capability_detail_probe_count"] > 0

    serialized = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert str(fixture) not in serialized
    assert "target-000006-seed-4242" not in serialized


def test_message_page_scaling_preserves_exact_sql_and_raw_work(tmp_path):
    base_fixture = tmp_path / "base"
    scaling_fixture = tmp_path / "scaling"
    _generate(base_fixture, scale="mini")
    _generate(scaling_fixture, scale="mini-scaling")
    output = tmp_path / "comparison.json"

    _run_script(
        RUNNER,
        "--stage",
        "message-page",
        "--fixture",
        str(base_fixture),
        "--visible-limit",
        "30",
        "--warm",
        "1",
        "--process-cold",
        "1",
        "--concurrency",
        "1",
        "--stress-rounds",
        "1",
        "--compare-fixture",
        str(scaling_fixture),
        "--output",
        str(output),
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    comparison = receipt["comparison"]
    assert receipt["gates"]["passed"] is True
    assert comparison is not None
    assert comparison["fixture"]["scale"] == "mini-scaling"
    assert receipt["gates"]["work_signature"] == comparison["work_signature"]
    for field in ("warm_p95_ms", "process_cold_p95_ms"):
        baseline = receipt["summary"][field]
        observed = comparison["summary"][field]
        assert observed - baseline <= max(100.0, baseline * 0.2)


def test_message_page_gate_rejects_budget_plan_resolver_and_mode_drift():
    runner = _load_script(RUNNER, "conversation_benchmark_message_page_gate")
    sample = {
        "kind": "warm",
        "lineage_depth": 2,
        "requested_rows": 30,
        "sql_count": 6,
        "capability_sql_count": 7,
        "capability_detail_probe_count": 1,
        "raw_rows_examined": 321,
        "closure_rows_examined": 65,
        "ordinary_serialized_bytes": 2 * 1024 * 1024 + 1,
        "closure_serialized_bytes": 512 * 1024 + 1,
        "serialized_bytes": 2_621_441,
        "resolver_call_count": 2,
        "duplicate_resolver_count": 1,
        "query_plan_indexed": False,
        "query_plan_unscoped_scan": True,
        "canonical_matches_manifest": False,
        "source_mode": "legacy_required",
        "fallback_reason": "unsupported_schema",
        "cache_result": "miss",
    }
    summary = {
        "warm_p95_ms": 1_000.0,
        "process_cold_p95_ms": 2_000.0,
        "stress_p95_ms": 1.0,
        "max_ms": 5_000.0,
    }

    failures = runner._evaluate_message_page_samples([sample], summary)

    assert any("capability" in failure for failure in failures)
    assert any("paging SQL" in failure for failure in failures)
    assert any("raw rows" in failure for failure in failures)
    assert any("ordinary bytes" in failure for failure in failures)
    assert any("closure bytes" in failure for failure in failures)
    assert any("resolver" in failure for failure in failures)
    assert any("indexed" in failure for failure in failures)
    assert any("source mode" in failure for failure in failures)
    assert any("5s" in failure for failure in failures)


def test_resolution_state_only_metadata_open_does_not_serialize_history(tmp_path):
    fixture = tmp_path / "fixture"
    _generate(fixture)
    runner = _load_script(RUNNER, "conversation_benchmark_state_only_contract")
    manifest, db_path = runner._load_fixture(fixture)

    samples, summary, database_unchanged, sidecar_unchanged = (
        runner._run_authenticated_http_fixture(
            manifest,
            db_path,
            warm_count=1,
            process_cold_count=1,
            concurrency=1,
            stress_rounds=0,
            install_sidecar=False,
        )
    )

    assert database_unchanged is True
    assert sidecar_unchanged is True
    assert summary["warm_count"] == 1
    assert summary["process_cold_count"] == 1
    assert {sample["returned_rows"] for sample in samples} == {0}
    assert max(sample["serialized_bytes"] for sample in samples) < 64 * 1024
    assert all(sample["canonical_matches_manifest"] for sample in samples)
    assert {sample["message_count_contract"] for sample in samples} == {
        "manifest_exact"
    }
    assert all(sample["message_count_matches_manifest"] for sample in samples)
    assert {
        sample["reported_message_count"] for sample in samples
    } == {manifest["counts"]["active_target_messages"]}
    assert runner._evaluate_http_samples(samples, summary) == []


def test_resolution_runner_exercises_mismatched_fixture_sidecar(tmp_path):
    fixture = tmp_path / "fixture"
    _generate(fixture)
    runner = _load_script(RUNNER, "conversation_benchmark_mismatch_contract")
    manifest, db_path = runner._load_fixture(fixture)

    samples, summary, database_unchanged, sidecar_unchanged = (
        runner._run_authenticated_http_fixture(
            manifest,
            db_path,
            warm_count=1,
            process_cold_count=1,
            concurrency=1,
            stress_rounds=0,
            sidecar_variant="mismatched",
        )
    )

    assert database_unchanged is True
    assert sidecar_unchanged is True
    assert {sample["sidecar_variant"] for sample in samples} == {"mismatched"}
    assert runner._evaluate_http_samples(samples, summary) == []


def test_diagnostics_nearest_rank_p95_contract():
    runner = _load_script(RUNNER, "conversation_benchmark_percentile_contract")

    assert runner.nearest_rank_p95([1]) == 1
    assert runner.nearest_rank_p95(range(1, 21)) == 19
    assert runner.nearest_rank_p95([4, 1, 3, 2]) == 4


def test_query_plan_gate_explains_the_actual_traced_select(tmp_path):
    fixture = tmp_path / "fixture"
    _generate(fixture)
    runner = _load_script(RUNNER, "conversation_benchmark_actual_plan_contract")
    db_path = fixture / "state.db"

    assert runner._captured_resolution_plan_is_indexed(
        db_path,
        ["SELECT id FROM sessions WHERE id = 'target-000000-seed-4242'"],
    )
    assert not runner._captured_resolution_plan_is_indexed(
        db_path,
        ["SELECT id FROM sessions WHERE lower(id) = 'target-000000-seed-4242'"],
    )


def test_diagnostic_stage_mapping_matches_route_boundaries():
    runner = _load_script(RUNNER, "conversation_benchmark_stage_contract")
    record = {
        "elapsed_ms": 28.0,
        "current_stage": "t6_after_json_write",
        "metrics": {"canonical_resolution_calls": 1},
        "stages": [
            {"name": "canonical_resolution", "ms": 1.0},
            {"name": "t1_after_get_session_check", "ms": 2.0},
            {"name": "t2_after_state_db_load", "ms": 3.0},
            {"name": "t3_after_model_resolve", "ms": 4.0},
            {"name": "t4_after_compact_and_merge", "ms": 5.0},
            {"name": "t5_after_redact", "ms": 6.0},
            {"name": "t6_after_json_write", "ms": 7.0},
        ],
    }
    sample = {"completed_ns": 1}

    runner._attach_http_diagnostic(sample, record)

    assert sample["stages"] == {
        "canonical_resolution": 1.0,
        "state_message_page": 2.0,
        "runtime_overlay": 3.0,
        "derived_view_state": 4.0,
        "redaction_and_serialize": 18.0,
    }
    assert sample["diagnostic_stages_complete"] is True
    assert sample["diagnostic_timed"] is True
    assert sample["resolver_stage_count"] == 1
    assert sample["resolver_call_count"] == 1
    assert sample["duplicate_resolver_count"] == 0


def test_http_gate_rejects_serialized_history_missing_stages_and_large_metadata():
    runner = _load_script(RUNNER, "conversation_benchmark_http_gate_contract")
    stages = {name: 1.0 for name in DIAGNOSTIC_STAGES}
    sample = {
        "kind": "warm",
        "http_status": 200,
        "authenticated": True,
        "canonical_matches_manifest": True,
        "stages": stages,
        "diagnostic_completed_route": True,
        "diagnostic_stages_complete": True,
        "diagnostic_timed": True,
        "resolver_stage_count": 1,
        "resolver_call_count": 1,
        "duplicate_resolver_count": 0,
        "returned_rows": 0,
        "messages_field_bytes": 2,
        "serialized_bytes": 1024,
        "message_count_matches_manifest": True,
        "message_count_contract": "manifest_exact",
        "sidecar_variant": "valid",
    }
    summary = {
        "warm_p95_ms": 1.0,
        "process_cold_p95_ms": 1.0,
        "max_ms": 1.0,
    }

    assert runner._evaluate_http_samples([sample], summary) == []
    failures = runner._evaluate_http_samples(
        [
            {
                **sample,
                "returned_rows": 1,
                "messages_field_bytes": 128,
                "serialized_bytes": 64 * 1024,
                "diagnostic_stages_complete": False,
                "diagnostic_timed": False,
                "resolver_stage_count": 2,
                "resolver_call_count": 2,
                "duplicate_resolver_count": 1,
                "message_count_matches_manifest": False,
            }
        ],
        summary,
    )

    assert any("transcript rows" in failure for failure in failures)
    assert any("64KiB" in failure for failure in failures)
    assert any("diagnostic stages" in failure for failure in failures)
    assert any("resolver stage" in failure for failure in failures)
    assert any("message_count" in failure for failure in failures)
