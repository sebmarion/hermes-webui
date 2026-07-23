"""Atomic pair-open and pre-managed rollback contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import webui_release_cutover as cutover
from scripts import webui_release_selector as selector


def _pair_plan(tmp_path: Path) -> dict:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    process_store = hermes_home / "process_notifications.json"
    process_store.write_text("{}\n", encoding="utf-8")
    process_store.chmod(0o600)
    return {
        "transaction_id": "pair-atomicity-transaction-00000001",
        "synthetic_process_notifications_path": str(process_store),
        "expected_candidate_identity": {
            "build_id": "webui-candidate",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "manifest_sha256": "3" * 64,
            "agent_source_commit": "4" * 40,
            "agent_source_tree": "5" * 40,
            "agent_source_manifest_sha256": "a" * 64,
            "runtime_manifest_sha256": "6" * 64,
            "selector_generation": 7,
        },
    }


def _pair_identities(plan: dict) -> tuple[dict, dict]:
    candidate = {
        "build_id": "webui-candidate",
        "pid": 202,
        "pid_start_token": "webui-start",
    }
    gateway = {
        "listener_pid": 303,
        "pid_start_token": "gateway-start",
        "health": {
            "release_identity": {
                "release": {
                    "release_pair_id": selector.release_pair_id(
                        plan["expected_candidate_identity"],
                        selector_generation=7,
                        transaction_id=plan["transaction_id"],
                    )
                }
            }
        },
    }
    return candidate, gateway


def test_pair_gate_install_and_atomic_owned_release(tmp_path):
    plan = _pair_plan(tmp_path)
    candidate, gateway = _pair_identities(plan)

    intent = cutover._pair_open_gate_intent_receipt(
        plan,
        candidate,
        gateway,
        created_at="2026-07-23T10:00:00+00:00",
    )
    installed = cutover._install_or_adopt_pair_open_gate(plan, intent)

    path = Path(intent["path"])
    assert path.name == ".pair_open_gate.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == intent["payload"]
    assert installed["owner_hash"] == intent["payload"]["owner_hash"]
    assert installed["payload_sha256"] == intent["payload_sha256"]

    released = cutover._release_owned_pair_open_gate(plan, intent, installed)

    assert released["status"] == "released"
    assert released["owner_hash"] == intent["payload"]["owner_hash"]
    assert not path.exists()


def test_pair_gate_never_overwrites_or_releases_another_owner(tmp_path):
    plan = _pair_plan(tmp_path)
    candidate, gateway = _pair_identities(plan)
    intent = cutover._pair_open_gate_intent_receipt(
        plan,
        candidate,
        gateway,
        created_at="2026-07-23T10:00:00+00:00",
    )
    path = Path(intent["path"])
    path.write_text('{"owner":"someone-else"}\n', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(cutover.ReleaseBuildError, match="another identity"):
        cutover._install_or_adopt_pair_open_gate(plan, intent)
    with pytest.raises(cutover.ReleaseBuildError, match="another owner"):
        cutover._release_owned_pair_open_gate(
            plan,
            intent,
            {
                "owner_hash": intent["payload"]["owner_hash"],
                "payload_sha256": intent["payload_sha256"],
            },
        )


def _control_plan(tmp_path: Path) -> dict:
    control = tmp_path / "control"
    control.mkdir()
    return {
        "transaction_id": "pre-managed-transaction-000000001",
        "transaction_journal": str(control / "transaction.json"),
        "selector_state": str(control / "selector.json"),
        "selector_lock": str(control / "selector.lock"),
        "managed_plist": str(control / "managed.plist"),
    }


@pytest.mark.parametrize("preexisting", [False, True])
def test_pre_managed_control_restore_is_exact_or_removes_owned_creation(
    tmp_path,
    preexisting,
):
    plan = _control_plan(tmp_path)
    selector = Path(plan["selector_state"])
    selector_lock = Path(plan["selector_lock"])
    managed = Path(plan["managed_plist"])
    if preexisting:
        selector.write_bytes(b'{"before":"selector"}\n')
        selector.chmod(0o600)
        selector_lock.write_bytes(b"before-lock\n")
        selector_lock.chmod(0o600)
        managed.write_bytes(b"before-plist\n")
        managed.chmod(0o640)

    captured = cutover._capture_pre_managed_control_state(plan)

    selector.write_bytes(b'{"candidate":"owned"}\n')
    selector.chmod(0o600)
    selector_lock.write_bytes(b"candidate-lock\n")
    selector_lock.chmod(0o600)
    managed.write_bytes(b"candidate-plist\n")
    managed.chmod(0o600)
    staged = cutover._pre_managed_control_stage_receipt(plan)

    restored = cutover._restore_pre_managed_control_state(
        plan,
        captured,
        staged,
    )

    assert restored["status"] == "restored"
    if preexisting:
        assert selector.read_bytes() == b'{"before":"selector"}\n'
        assert selector_lock.read_bytes() == b"before-lock\n"
        assert managed.read_bytes() == b"before-plist\n"
        assert selector.stat().st_mode & 0o777 == 0o600
        assert selector_lock.stat().st_mode & 0o777 == 0o600
        assert managed.stat().st_mode & 0o777 == 0o640
    else:
        assert not selector.exists()
        assert not selector_lock.exists()
        assert not managed.exists()


def test_pre_managed_control_restore_refuses_cas_mismatch(tmp_path):
    plan = _control_plan(tmp_path)
    captured = cutover._capture_pre_managed_control_state(plan)
    selector = Path(plan["selector_state"])
    selector_lock = Path(plan["selector_lock"])
    managed = Path(plan["managed_plist"])
    selector.write_bytes(b"owned-selector\n")
    selector.chmod(0o600)
    selector_lock.write_bytes(b"owned-lock\n")
    selector_lock.chmod(0o600)
    managed.write_bytes(b"owned-plist\n")
    managed.chmod(0o600)
    staged = cutover._pre_managed_control_stage_receipt(plan)
    selector.write_bytes(b"concurrent-owner\n")

    with pytest.raises(cutover.ReleaseBuildError, match="changed before restore"):
        cutover._restore_pre_managed_control_state(plan, captured, staged)
