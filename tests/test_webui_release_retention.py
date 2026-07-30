from pathlib import Path

import pytest

from scripts import webui_release_retention as retention


def test_keeps_only_newest_terminal_rollback_and_collects_abandoned_attempts():
    rows = [
        {
            "path": "/private/snapshots-r72",
            "terminal_kind": "accepted-managed-promotion",
            "newest_timestamp": 72.0,
            "payload_paths": ["/private/snapshots-r72/data"],
        },
        {
            "path": "/private/snapshots-r71",
            "terminal_kind": "accepted-managed-promotion",
            "newest_timestamp": 71.0,
            "payload_paths": ["/private/snapshots-r71/data"],
        },
        {
            "path": "/private/snapshots-failed",
            "terminal_kind": None,
            "newest_timestamp": 70.0,
            "payload_paths": ["/private/snapshots-failed/data"],
            "reason": "nonterminal-transaction",
        },
        {
            "path": "/private/snapshots-invalid",
            "terminal_kind": None,
            "reason": "validation-failed:CleanupError:bad manifest",
        },
    ]

    candidates = retention.select_rolling_candidates(rows)

    assert rows[0]["reason"] == "previous-rollback"
    assert rows[1]["reason"] == "superseded-terminal"
    assert rows[2]["reason"] == "abandoned-nonterminal"
    assert rows[3]["reason"].startswith("validation-failed:")
    assert [row["path"] for row in candidates] == [
        "/private/snapshots-r71",
        "/private/snapshots-failed",
    ]


def test_refuses_cleanup_without_a_verified_terminal_rollback():
    rows = [
        {
            "path": "/private/snapshots-failed",
            "terminal_kind": None,
            "newest_timestamp": 70.0,
            "payload_paths": ["/private/snapshots-failed/data"],
            "reason": "nonterminal-transaction",
        }
    ]

    with pytest.raises(retention.CleanupError, match="verified terminal rollback"):
        retention.select_rolling_candidates(rows)


def test_release_paths_are_derived_from_selector_control_files(tmp_path):
    selector_root = tmp_path / "reliability" / "selector"
    selector_root.mkdir(parents=True)
    state = selector_root / "selector-state.json"
    lock = selector_root / "selector-state.lock"
    state.write_text("{}\n", encoding="utf-8")
    lock.touch()

    paths = retention.release_paths(state, lock)

    assert paths.reliability_root == tmp_path / "reliability"
    assert paths.private_root == tmp_path / "reliability" / "private"
    assert paths.transactions_root == paths.private_root / "transactions"
    assert paths.receipts_root == paths.private_root / "cleanup-receipts"


def test_release_paths_reject_unrelated_control_files(tmp_path):
    state = tmp_path / "selector-state.json"
    lock = tmp_path / "selector-state.lock"
    state.write_text("{}\n", encoding="utf-8")
    lock.touch()

    with pytest.raises(retention.CleanupError, match="selector directory"):
        retention.release_paths(Path(state), Path(lock))


def test_generic_selector_cli_paths_are_not_managed_release_controls(tmp_path):
    assert not retention.is_managed_selector_control_pair(
        tmp_path / "selector.json",
        tmp_path / "selector.lock",
    )


def test_after_release_reports_failure_without_raising(tmp_path):
    result = retention.run_after_release(
        tmp_path / "selector-state.json",
        tmp_path / "selector-state.lock",
        accepted_transaction_id="accepted-release-transaction-000001",
        expected_current_build="candidate",
    )

    assert result["status"] == "failed"
    assert result["error"].startswith("CleanupError:")


def test_transaction_loader_ignores_non_release_operational_receipts(
    tmp_path,
    monkeypatch,
):
    transactions = tmp_path / "transactions"
    transactions.mkdir(mode=0o700)
    adoption = transactions / "adopt-live-r75-r72-tx63.json"
    adoption.write_text('{"schema":"split-adoption"}\n', encoding="utf-8")
    adoption.chmod(0o600)
    monkeypatch.setattr(retention, "TRANSACTIONS_ROOT", transactions)

    assert retention.load_journals() == ({}, {})


def test_rolling_cleanup_rejects_both_absent_before_deleting_intent(
    tmp_path, monkeypatch
):
    transaction_id = "rolling-absence-before-delete-transaction-0001"
    source = tmp_path / "obsolete-release"
    source.mkdir()
    opened = source.stat()
    source.rmdir()
    destination = source.with_name(
        ".hermes-retention-quarantine-"
        f"{transaction_id}-0000-{source.name}"
    )
    plan = {
        "transaction_id": transaction_id,
        "selector_generation": 0,
        "selector_state_sha256": "a" * 64,
        "current": "candidate",
        "last_good": "base",
        "candidates": [
            {
                "kind": "webui-release",
                "path": str(source),
                "device": opened.st_dev,
                "inode": opened.st_ino,
            }
        ],
    }
    receipt_root = tmp_path / "receipts"
    monkeypatch.setattr(retention, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(retention, "SELECTOR_STATE", tmp_path / "selector.json")
    monkeypatch.setattr(retention, "SELECTOR_LOCK", tmp_path / "selector.lock")
    monkeypatch.setattr(retention, "SELECTOR_RELEASES", tmp_path / "releases")
    state = {
        "generation": 1,
        "current": "candidate",
        "last_good": "base",
        "bootstrap_fallback": "base",
        "releases": {"candidate": {}, "base": {}},
    }
    monkeypatch.setattr(
        retention.release_selector,
        "read_selector_state",
        lambda *_args, **_kwargs: state,
    )
    receipt_root.mkdir(mode=0o700)
    retention.create_receipt(
        receipt_root / f"rolling-release-cleanup-{transaction_id}.json",
        {
            "version": 1,
            "status": "quarantining",
            "plan": plan,
            "operations": [
                {
                    "source": str(source),
                    "destination": str(destination),
                    "device": opened.st_dev,
                    "inode": opened.st_ino,
                    "state": "intent",
                }
            ],
        },
    )

    with pytest.raises(
        retention.CleanupError,
        match="rolling quarantine state is ambiguous",
    ):
        retention._apply_rolling_release_plan(plan)


def test_retention_requires_split_attestation_before_gateway_binding():
    split_receipt = {
        "last_good_origin_attestation": {
            "webui": {"identity": {"build_id": "last-good"}},
            "gateway": {"identity": {"build_id": "last-good-gateway"}},
        }
    }
    gateway_receipt = {
        "binding": {"status": "verified"},
        "last_good_origin_attestation": split_receipt[
            "last_good_origin_attestation"
        ],
    }

    with pytest.raises(
        retention.CleanupError,
        match=(
            "phase prerequisites are missing for last_good_split_attested: "
            "plist_installed"
        ),
    ):
        retention.validate_phase_graph(
            {"last_good_split_attested": split_receipt},
            retention.TRANSACTION_PHASE_PREREQUISITES,
            label="managed journal",
        )
    with pytest.raises(
        retention.CleanupError,
        match=(
            "phase prerequisites are missing for gateway_last_good_attested: "
            "last_good_split_attested"
        ),
    ):
        retention.validate_phase_graph(
            {
                "staged": {},
                "plist_installed": {},
                "gateway_last_good_attested": gateway_receipt,
            },
            retention.TRANSACTION_PHASE_PREREQUISITES,
            label="managed journal",
        )

    assert retention.validate_phase_graph(
        {
            "staged": {},
            "plist_installed": {},
            "last_good_split_attested": split_receipt,
            "gateway_last_good_attested": gateway_receipt,
        },
        retention.TRANSACTION_PHASE_PREREQUISITES,
        label="managed journal",
    )["gateway_last_good_attested"] == gateway_receipt


def test_retention_mirrors_bootstrap_rollback_claim_exclusion():
    assert (
        retention.TRANSACTION_PHASE_PREREQUISITES[
            "bootstrap_rollback_claimed"
        ]
        == ()
    )
    with pytest.raises(retention.CleanupError, match="conflicting"):
        retention.validate_phase_graph(
            {
                "pair_commit_intent": {},
                "bootstrap_rollback_claimed": {},
            },
            {
                "pair_commit_intent": (),
                "bootstrap_rollback_claimed": (),
            },
            label="managed journal",
        )


def test_retention_accepts_verified_bootstrap_rollback_with_exact_claim(
    monkeypatch,
):
    rollback_receipt = {
        "build_id": "last-good",
        "plist_sha256": "a" * 64,
        "plist_mode": 0o600,
        "cli_link_target": "/previous/hermes",
        "state_snapshot_id": "snapshot-1",
        "state_snapshot_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        retention,
        "bootstrap_terminal_kind",
        lambda _phases: "verified-bootstrap-rollback",
    )
    monkeypatch.setattr(
        retention,
        "managed_terminal_kind",
        lambda _phases, _receipt: None,
    )

    terminal = retention.combined_terminal_kind(
        bootstrap_phases={"rollback_verified": {}},
        managed_phases={
            "bootstrap_rollback_claimed": {
                "rollback_receipt": rollback_receipt,
            }
        },
        rollback_receipt=rollback_receipt,
    )

    assert terminal == (
        "verified-bootstrap-rollback+bootstrap-rollback-claimed"
    )
