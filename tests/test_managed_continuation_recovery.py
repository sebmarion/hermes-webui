import json
import multiprocessing
import os
import fcntl
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from api import goal_continuation as goal
from api import tool_limit_continuation as tool
from api import managed_continuation_recovery as managed


MANIFEST = "a" * 64
TRANSACTION = "txn-managed-continuation"


def _write(path: Path, receipts: dict) -> None:
    path.write_text(
        json.dumps({"version": 1, "receipts": receipts}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_root(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _goal_claim():
    session_id = "session-goal"
    parent_run_id = "run-goal"
    key = goal._claim_key(session_id, parent_run_id)
    return key, {
        "claim_key": key,
        "session_id": session_id,
        "parent_run_id": parent_run_id,
        "prompt": "continue goal",
        "goal_revision": 3,
        "profile_home": None,
        "state": "claimed",
        "claimed_at": 1.0,
        "updated_at": 1.0,
    }


def _tool_claim():
    parent_session_id = "session-parent"
    parent_run_id = "run-tool"
    key = tool._claim_key(parent_session_id, parent_run_id)
    return key, {
        "claim_key": key,
        "execution_id": "execution-tool",
        "profile": "default",
        "root_session_id": "session-root",
        "parent_session_id": parent_session_id,
        "parent_run_id": parent_run_id,
        "child_session_id": "session-child",
        "child_snapshot": {"session_id": "session-child"},
        "continuation_prompt": "continue tool work",
        "continuation_index": 1,
        "chain_started_at": 1.0,
        "claimed_at": 1.0,
        "updated_at": 1.0,
        "progress_fingerprint": None,
        "state": "claimed",
    }


@pytest.fixture(params=("goal", "tool"))
def managed_case(request, tmp_path, monkeypatch):
    module = goal if request.param == "goal" else tool
    key, receipt = _goal_claim() if request.param == "goal" else _tool_claim()
    monkeypatch.setattr(module.config, "SESSION_DIR", tmp_path)
    if module is goal:
        monkeypatch.setattr(module, "_goal_revision_is_active", lambda *_a, **_k: True)
        recover = module.recover_managed_goal_continuations_exact
    else:
        monkeypatch.setattr(module, "_ensure_receipt_child", lambda _receipt: object())
        recover = module.recover_managed_continuations_exact
    return module, recover, key, receipt, tmp_path


def _recover(recover, **kwargs):
    return recover(
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
        **kwargs,
    )


def _crash_worker(kind: str, session_dir: str, boundary: str) -> None:
    module = goal if kind == "goal" else tool
    module.config.SESSION_DIR = Path(session_dir)
    if module is goal:
        module._goal_revision_is_active = lambda *_a, **_k: True
        recover = module.recover_managed_goal_continuations_exact
        intended = "session-goal"
    else:
        module._ensure_receipt_child = lambda _receipt: object()
        recover = module.recover_managed_continuations_exact
        intended = "session-child"

    def crash_hook(stage: str) -> None:
        if stage == boundary:
            os._exit(77)

    recover(
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
        start=lambda _sid, _prompt: {
            "_status": 200,
            "session_id": intended,
            "stream_id": "stream-crash",
        },
        crash_hook=crash_hook,
    )
    os._exit(0)


def _verify_worker(kind: str, session_dir: str, receipt, result_queue) -> None:
    module = goal if kind == "goal" else tool
    module.config.SESSION_DIR = Path(session_dir)
    result = module.verify_managed_continuations_exact(
        receipt,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )
    result_queue.put(result.outcome.value)


def test_exact_absent_has_store_identity_and_process_epoch(managed_case):
    _module, recover, _key, _receipt, _tmp_path = managed_case

    result = _recover(recover, start=lambda *_a: pytest.fail("must not start"))

    assert result.outcome.value == "ABSENT"
    assert result.store_identity_before is None
    assert result.store_sha256_before is None
    assert result.process_pid == os.getpid()
    assert result.process_start_token
    assert result.process_epoch
    assert result.transaction_id == TRANSACTION
    assert result.manifest_sha256 == MANIFEST


def test_claimed_receipt_starts_with_exact_session_and_stream(managed_case):
    module, recover, key, receipt, _tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    calls = []

    def start(session_id, prompt):
        calls.append((session_id, prompt))
        intended = (
            receipt["session_id"]
            if module is goal
            else receipt["child_session_id"]
        )
        return {
            "_status": 200,
            "session_id": intended,
            "stream_id": "stream-exact",
        }

    result = _recover(recover, start=start)

    assert result.outcome.value == "COMPLETE"
    assert result.started_receipt_keys == (key,)
    assert result.receipt_classifications == ((key, "started_exact"),)
    assert result.store_identity_before
    assert result.store_identity_after
    durable = json.loads(module._receipt_path().read_text())["receipts"][key]
    assert durable["state"] == "started"
    assert durable["child_stream_id"] == "stream-exact"
    assert stat.S_IMODE(module._receipt_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(module._lock_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(module._receipt_path().parent.stat().st_mode) == 0o700
    assert len(calls) == 1


def test_claimed_receipt_rejects_wrong_response_session(managed_case):
    module, recover, key, receipt, _tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})

    result = _recover(
        recover,
        start=lambda _sid, _prompt: {
            "_status": 200,
            "session_id": "wrong-session",
            "stream_id": "stream-wrong-session",
        },
    )

    assert result.outcome.value == "PARTIAL"
    assert result.retryable_receipt_keys == (key,)
    durable = json.loads(module._receipt_path().read_text())["receipts"][key]
    assert durable["state"] == "claimed"
    assert "child_stream_id" not in durable


def test_live_owner_starting_is_partial_and_never_stolen(
    managed_case, monkeypatch
):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt.update(
        {
            "state": "starting",
            "owner_pid": 4242,
            "owner_start_token": "token:4242",
            "owner_thread": 7,
            "start_token": "start-token",
            "launch_phase": "reserved",
            "starting_at": 2.0,
            "updated_at": 2.0,
        }
    )
    _write(module._receipt_path(), {key: receipt})
    monkeypatch.setattr(module, "_process_start_token", lambda pid: f"token:{pid}")

    result = _recover(
        recover, start=lambda *_a: pytest.fail("live owner must not be stolen")
    )

    assert result.outcome.value == "PARTIAL"
    assert result.retryable_receipt_keys == (key,)
    assert result.receipt_classifications == ((key, "live_owner_starting"),)


def test_pid_reuse_is_ambiguous(managed_case, monkeypatch):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt.update(
        {
            "state": "starting",
            "owner_pid": 4242,
            "owner_start_token": "token:old",
            "owner_thread": 7,
            "start_token": "start-token",
            "launch_phase": "reserved",
            "starting_at": 2.0,
            "updated_at": 2.0,
        }
    )
    _write(module._receipt_path(), {key: receipt})
    monkeypatch.setattr(module, "_process_start_token", lambda _pid: "token:reused")

    result = _recover(recover, start=lambda *_a: pytest.fail("must fail closed"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "PID identity" in result.errors[0]


def test_launch_before_started_write_is_ambiguous(managed_case, monkeypatch):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt.update(
        {
            "state": "starting",
            "owner_pid": 4242,
            "owner_start_token": "token:dead",
            "owner_thread": 7,
            "start_token": "start-token",
            "launch_phase": "launching",
            "starting_at": 2.0,
            "updated_at": 2.0,
        }
    )
    _write(module._receipt_path(), {key: receipt})
    monkeypatch.setattr(module, "_process_start_token", lambda _pid: None)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe retry"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "launch-before-started-write" in result.errors[0]


def test_legacy_started_without_process_token_is_inert_and_verifiable(
    managed_case,
):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt.update(
        {
            "state": "started",
            "child_stream_id": "legacy-stream",
            "started_at": 2.0,
            "updated_at": 2.0,
        }
    )
    receipt.pop("completed_start_token", None)
    _write(module._receipt_path(), {key: receipt})

    recovered = _recover(
        recover,
        start=lambda *_a: pytest.fail("legacy started receipt is historical"),
    )

    assert recovered.outcome.value == "COMPLETE"
    assert recovered.receipt_classifications == (
        (key, "started_legacy_inert"),
    )
    assert recovered.receipt_bindings == ()
    assert recovered.started_receipt_keys == ()
    assert recovered.retryable_receipt_keys == ()
    verified = module.verify_managed_continuations_exact(
        recovered,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )
    assert verified.outcome.value == "COMPLETE"


def test_known_session_metadata_root_fields_are_inert(managed_case):
    module, recover, key, receipt, _tmp_path = managed_case
    if module is goal:
        receipt.update(
            {
                "state": "discarded",
                "discarded_reason": "historical",
                "updated_at": 2.0,
            }
        )
        expected = "terminal_discarded"
    else:
        receipt.update({"state": "completed", "updated_at": 2.0})
        expected = "terminal_completed"
    _write_root(
        module._receipt_path(),
        {
            "version": 1,
            "receipts": {key: receipt},
            "pinned": False,
            "archived": True,
        },
    )

    recovered = _recover(
        recover,
        start=lambda *_a: pytest.fail("terminal receipt must stay inert"),
    )

    assert recovered.outcome.value == "COMPLETE"
    assert recovered.receipt_classifications == ((key, expected),)


def test_unknown_store_fields_and_record_bounds_are_ambiguous(
    managed_case, monkeypatch
):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt["unknown"] = True
    _write(module._receipt_path(), {key: receipt})

    result = _recover(recover, start=lambda *_a: pytest.fail("tampered"))
    assert result.outcome.value == "AMBIGUOUS"

    receipt.pop("unknown")
    _write_root(
        module._receipt_path(),
        {"version": 1, "receipts": {key: receipt}, "unexpected": False},
    )
    result = _recover(recover, start=lambda *_a: pytest.fail("unknown root"))
    assert result.outcome.value == "AMBIGUOUS"

    _write_root(
        module._receipt_path(),
        {"version": 1, "receipts": {key: receipt}, "pinned": 1},
    )
    result = _recover(recover, start=lambda *_a: pytest.fail("malformed metadata"))
    assert result.outcome.value == "AMBIGUOUS"

    _write(module._receipt_path(), {key: receipt})
    monkeypatch.setattr(module, "_MAX_MANAGED_RECEIPTS", 0)
    result = _recover(recover, start=lambda *_a: pytest.fail("unbounded"))
    assert result.outcome.value == "AMBIGUOUS"


def test_strict_route_wrapper_uses_only_exact_managed_api(monkeypatch):
    from api import routes

    exact_calls = []
    legacy_calls = []
    monkeypatch.setattr(
        tool,
        "recover_managed_continuations_exact",
        lambda **kwargs: exact_calls.append(kwargs)
        or type("R", (), {"outcome": type("O", (), {"value": "ABSENT"})(), "to_dict": lambda self: {"status": "complete"}})(),
    )
    monkeypatch.setattr(
        tool,
        "recover_pending_continuations",
        lambda **kwargs: legacy_calls.append(kwargs) or 0,
    )

    routes._recover_tool_limit_continuations_on_startup(
        strict=True,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )

    assert len(exact_calls) == 1
    assert legacy_calls == []


def test_strict_goal_route_wrapper_uses_only_exact_managed_api(monkeypatch):
    from api import routes

    exact_calls = []
    legacy_calls = []
    monkeypatch.setattr(
        goal,
        "recover_managed_goal_continuations_exact",
        lambda **kwargs: exact_calls.append(kwargs)
        or type(
            "R",
            (),
            {
                "outcome": type("O", (), {"value": "ABSENT"})(),
                "to_dict": lambda self: {"status": "complete"},
            },
        )(),
    )
    monkeypatch.setattr(
        goal,
        "recover_pending_goal_continuations",
        lambda **kwargs: legacy_calls.append(kwargs) or 0,
    )

    routes._recover_goal_continuations_on_startup(
        strict=True,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )

    assert len(exact_calls) == 1
    assert legacy_calls == []


@pytest.mark.skipif(os.name == "nt", reason="requires fork and POSIX process identity")
@pytest.mark.parametrize("kind", ("goal", "tool"))
@pytest.mark.parametrize(
    ("boundary", "expected_state", "expected_outcome"),
    (
        ("claim_committed", "starting", "COMPLETE"),
        ("launch_returned", "starting", "AMBIGUOUS"),
        ("started_committed", "started", "COMPLETE"),
    ),
)
def test_real_crash_boundaries_are_classified_exactly(
    tmp_path, monkeypatch, kind, boundary, expected_state, expected_outcome
):
    module = goal if kind == "goal" else tool
    key, receipt = _goal_claim() if kind == "goal" else _tool_claim()
    monkeypatch.setattr(module.config, "SESSION_DIR", tmp_path)
    if module is goal:
        monkeypatch.setattr(module, "_goal_revision_is_active", lambda *_a, **_k: True)
        recover = module.recover_managed_goal_continuations_exact
        intended = receipt["session_id"]
    else:
        monkeypatch.setattr(module, "_ensure_receipt_child", lambda _receipt: object())
        recover = module.recover_managed_continuations_exact
        intended = receipt["child_session_id"]
    _write(module._receipt_path(), {key: receipt})

    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker,
        args=(kind, os.fspath(tmp_path), boundary),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 77
    durable = json.loads(module._receipt_path().read_text())["receipts"][key]
    assert durable["state"] == expected_state

    starts = []
    result = _recover(
        recover,
        start=lambda sid, _prompt: starts.append(sid)
        or {
            "_status": 200,
            "session_id": intended,
            "stream_id": "stream-recovered",
        },
    )

    assert result.outcome.value == expected_outcome
    if boundary == "claim_committed":
        assert starts == [intended]
    else:
        assert starts == []


def test_lock_replacement_after_flock_is_ambiguous(managed_case, monkeypatch):
    module, recover, key, receipt, tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    original_flock = fcntl.flock
    replaced = False

    def replace_after_lock(descriptor, operation):
        nonlocal replaced
        result = original_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX and not replaced:
            replaced = True
            replacement = tmp_path / "replacement.lock"
            replacement.write_bytes(b"")
            replacement.chmod(0o600)
            os.replace(replacement, module._lock_path())
        return result

    monkeypatch.setattr(fcntl, "flock", replace_after_lock)
    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "lock was replaced" in result.errors[0]


def test_managed_authority_rejects_world_writable_parent(managed_case):
    _module, recover, _key, _receipt, tmp_path = managed_case
    tmp_path.chmod(0o777)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "parent" in result.errors[0]


def test_managed_authority_rejects_foreign_owned_parent(
    managed_case, monkeypatch
):
    _module, recover, _key, _receipt, _tmp_path = managed_case
    current_uid = os.getuid()
    monkeypatch.setattr(managed.os, "getuid", lambda: current_uid + 1)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "owner-held" in result.errors[0]


def test_managed_authority_rejects_non_private_store(managed_case):
    module, recover, key, receipt, _tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    module._receipt_path().chmod(0o644)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "store" in result.errors[0]


def test_managed_authority_rejects_non_private_existing_lock(managed_case):
    module, recover, key, receipt, _tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    module._lock_path().write_bytes(b"")
    module._lock_path().chmod(0o644)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert stat.S_IMODE(module._lock_path().stat().st_mode) == 0o644


def test_managed_authority_rejects_store_symlink(managed_case):
    module, recover, key, receipt, tmp_path = managed_case
    target = tmp_path / "outside.json"
    _write(target, {key: receipt})
    module._receipt_path().symlink_to(target)

    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert module._receipt_path().is_symlink()


def test_managed_authority_detects_parent_path_replacement(
    managed_case, monkeypatch
):
    module, recover, key, receipt, tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    original_flock = fcntl.flock
    moved = tmp_path.with_name(f"{tmp_path.name}-moved")
    replaced = False

    def replace_parent_after_lock(descriptor, operation):
        nonlocal replaced
        result = original_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX and not replaced:
            replaced = True
            os.rename(tmp_path, moved)
            tmp_path.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(fcntl, "flock", replace_parent_after_lock)
    result = _recover(recover, start=lambda *_a: pytest.fail("unsafe launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "parent" in result.errors[0]


def test_legacy_store_keeps_historical_non_private_mode(managed_case):
    module, _recover_exact, key, receipt, _tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    module._receipt_path().chmod(0o644)
    module._lock_path().write_bytes(b"")
    module._lock_path().chmod(0o644)

    if module is goal:
        result = module.recover_pending_goal_continuations(
            start=lambda *_a: {"_status": 500}
        )
    else:
        result = module.recover_pending_continuations(
            start=lambda *_a: {"_status": 500}
        )

    assert result == 0
    assert stat.S_IMODE(module._receipt_path().stat().st_mode) == 0o644
    assert stat.S_IMODE(module._lock_path().stat().st_mode) == 0o644


def test_final_started_to_claimed_regression_is_ambiguous(
    managed_case, monkeypatch
):
    module, recover, key, receipt, _tmp_path = managed_case
    receipt.update(
        {
            "state": "started",
            "child_stream_id": "stream-original",
            "completed_start_token": "start-original",
            "started_at": 2.0,
            "updated_at": 2.0,
        }
    )
    _write(module._receipt_path(), {key: receipt})
    original_snapshot = managed.stable_store_snapshot
    calls = 0

    def regress(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            changed = dict(receipt)
            changed["state"] = "claimed"
            for field in (
                "child_stream_id",
                "completed_start_token",
                "started_at",
            ):
                changed.pop(field, None)
            _write(module._receipt_path(), {key: changed})
        return original_snapshot(path, **kwargs)

    monkeypatch.setattr(managed, "stable_store_snapshot", regress)
    result = _recover(recover, start=lambda *_a: pytest.fail("must not launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "regressed" in result.errors[0]


def test_store_created_after_absent_snapshot_is_ambiguous(
    managed_case, monkeypatch
):
    module, recover, key, receipt, _tmp_path = managed_case
    original_snapshot = managed.stable_store_snapshot
    calls = 0

    def create_after_absent(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            _write(module._receipt_path(), {key: receipt})
        return original_snapshot(path, **kwargs)

    monkeypatch.setattr(managed, "stable_store_snapshot", create_after_absent)
    result = _recover(recover, start=lambda *_a: pytest.fail("must not launch"))

    assert result.outcome.value == "AMBIGUOUS"
    assert "receipt set changed" in result.errors[0]


def test_exact_verifier_is_read_only_and_rejects_tamper(
    managed_case, monkeypatch
):
    module, recover, key, receipt, tmp_path = managed_case
    _write(module._receipt_path(), {key: receipt})
    intended = (
        receipt["session_id"] if module is goal else receipt["child_session_id"]
    )
    recovered = _recover(
        recover,
        start=lambda _sid, _prompt: {
            "_status": 200,
            "session_id": intended,
            "stream_id": "stream-verify",
        },
    )
    verify = module.verify_managed_continuations_exact
    before_inventory = {
        path.name: path.read_bytes() for path in tmp_path.iterdir()
    }
    monkeypatch.setattr(
        module,
        "_save_store",
        lambda *_a, **_k: pytest.fail("verifier must not write"),
    )
    if module is goal:
        monkeypatch.setattr(
            module,
            "_start_managed_goal_receipt",
            lambda *_a, **_k: pytest.fail("verifier must not launch"),
        )
    else:
        monkeypatch.setattr(
            module,
            "_start_receipt",
            lambda *_a, **_k: pytest.fail("verifier must not launch"),
        )

    verified = verify(
        recovered,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )
    assert verified.outcome.value == "COMPLETE"
    assert {
        path.name: path.read_bytes() for path in tmp_path.iterdir()
    } == before_inventory

    tampered = replace(recovered, store_sha256_after="0" * 64)
    assert verify(
        tampered,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    ).outcome.value == "AMBIGUOUS"
    foreign_evidence = replace(recovered, process_epoch="foreign-epoch")
    assert verify(
        foreign_evidence,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    ).outcome.value == "COMPLETE"


def test_exact_verifier_never_creates_missing_lock(managed_case):
    module, recover, _key, _receipt, tmp_path = managed_case
    recovered = _recover(
        recover, start=lambda *_a: pytest.fail("absent must not start")
    )
    module._lock_path().unlink()
    before = tuple(sorted(path.name for path in tmp_path.iterdir()))

    verified = module.verify_managed_continuations_exact(
        recovered,
        transaction_id=TRANSACTION,
        manifest_sha256=MANIFEST,
    )

    assert verified.outcome.value == "AMBIGUOUS"
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before


@pytest.mark.parametrize("kind", ("goal", "tool"))
def test_exact_verifier_accepts_typed_receipt_after_process_restart(
    tmp_path, monkeypatch, kind
):
    module = goal if kind == "goal" else tool
    key, claimed = _goal_claim() if kind == "goal" else _tool_claim()
    monkeypatch.setattr(module.config, "SESSION_DIR", tmp_path)
    if module is goal:
        monkeypatch.setattr(module, "_goal_revision_is_active", lambda *_a, **_k: True)
        recover = module.recover_managed_goal_continuations_exact
        intended = claimed["session_id"]
    else:
        monkeypatch.setattr(module, "_ensure_receipt_child", lambda _row: object())
        recover = module.recover_managed_continuations_exact
        intended = claimed["child_session_id"]
    _write(module._receipt_path(), {key: claimed})
    receipt = _recover(
        recover,
        start=lambda *_a: {
            "_status": 200,
            "session_id": intended,
            "stream_id": "stream-restart",
        },
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_verify_worker,
        args=(kind, os.fspath(tmp_path), receipt, result_queue),
    )
    process.start()
    process.join(10)

    assert process.exitcode == 0
    assert result_queue.get(timeout=2) == "COMPLETE"
