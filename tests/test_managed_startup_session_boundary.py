import os

import pytest

from api import managed_startup_session_boundary as boundary


TRANSACTION_ID = "session-boundary-transaction-000001"
MANIFEST_SHA256 = "b" * 64


def _private_dir(path):
    path.mkdir()
    path.chmod(0o700)
    return path


def _sparse_file(path, size, *, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.ftruncate(descriptor, size)
    finally:
        os.close(descriptor)
    path.chmod(mode)
    return path


def test_managed_boundary_does_not_read_or_size_gate_production_payloads(
    tmp_path,
    monkeypatch,
):
    session_dir = _private_dir(tmp_path / "sessions")
    _sparse_file(
        session_dir / "large-session.json",
        16 * 1024 * 1024,
        mode=0o644,
    )
    state_dir = _private_dir(tmp_path / "state")
    state_db = _sparse_file(
        state_dir / "state.db",
        513 * 1024 * 1024,
    )

    monkeypatch.setattr(
        boundary.os,
        "read",
        lambda *_a, **_k: pytest.fail("managed boundary read payload bytes"),
    )

    receipt = boundary.attest_managed_startup_session_boundary(
        session_dir,
        state_db,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
    )

    assert receipt.outcome is boundary.SessionRecoveryOutcome.PROVED_COMPLETE
    assert receipt.transaction_id == TRANSACTION_ID
    assert receipt.manifest_sha256 == MANIFEST_SHA256
    assert dict(receipt.state_db_bundle)["main"][5] == 513 * 1024 * 1024
    verification = boundary.verify_managed_startup_session_boundary(receipt)
    assert verification.outcome is boundary.SessionRecoveryOutcome.PROVED_COMPLETE
    assert verification.receipt == receipt


def test_managed_boundary_rejects_non_private_database(tmp_path):
    session_dir = _private_dir(tmp_path / "sessions")
    state_dir = _private_dir(tmp_path / "state")
    state_db = _sparse_file(state_dir / "state.db", 4096, mode=0o644)

    with pytest.raises(
        boundary.ManagedStartupSessionBoundaryError,
        match="private",
    ):
        boundary.attest_managed_startup_session_boundary(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_managed_boundary_rejects_symlinked_session_directory(tmp_path):
    actual = _private_dir(tmp_path / "actual-sessions")
    session_dir = tmp_path / "sessions"
    session_dir.symlink_to(actual, target_is_directory=True)
    state_dir = _private_dir(tmp_path / "state")

    with pytest.raises(
        boundary.ManagedStartupSessionBoundaryError,
        match="held safely|identity is unsafe",
    ):
        boundary.attest_managed_startup_session_boundary(
            session_dir,
            state_dir / "state.db",
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_managed_boundary_verifier_detects_database_identity_drift(tmp_path):
    session_dir = _private_dir(tmp_path / "sessions")
    state_dir = _private_dir(tmp_path / "state")
    state_db = _sparse_file(state_dir / "state.db", 4096)
    receipt = boundary.attest_managed_startup_session_boundary(
        session_dir,
        state_db,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
    )

    with state_db.open("ab") as handle:
        handle.write(b"x")

    verification = boundary.verify_managed_startup_session_boundary(receipt)
    assert verification.outcome is boundary.SessionRecoveryOutcome.AMBIGUOUS
    assert verification.receipt == receipt
    assert verification.reason == "managed_session_boundary_receipt_mismatch"


def test_managed_boundary_receipt_round_trips_through_durable_store(tmp_path):
    import deferred_release_manifest as release_manifest
    from managed_startup_coordinator import (
        DurableStartupReceiptStore,
        ManagedStartupReceiptCodec,
    )

    session_dir = _private_dir(tmp_path / "sessions")
    state_dir = _private_dir(tmp_path / "state")
    state_db = _sparse_file(state_dir / "state.db", 0)
    manifest_sha256 = release_manifest.deferred_release_manifest_sha256()
    receipt = boundary.attest_managed_startup_session_boundary(
        session_dir,
        state_db,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=manifest_sha256,
    )
    store = DurableStartupReceiptStore(
        tmp_path / "receipts.json",
        transaction_id=TRANSACTION_ID,
        manifest_sha256=manifest_sha256,
        codecs=(
            ManagedStartupReceiptCodec(
                "webui.session-boundary-receipt.v1",
                boundary.ManagedStartupSessionBoundaryReceipt,
            ),
            ManagedStartupReceiptCodec(
                "webui.session-outcome.v1",
                boundary.SessionRecoveryOutcome,
            ),
        ),
        step_types=(
            (
                "session_recovery",
                "webui.session-boundary-receipt.v1",
            ),
        ),
    )

    store.persist(
        "session_recovery",
        "webui.session-boundary-receipt.v1",
        receipt,
    )

    assert store.load(
        "session_recovery",
        "webui.session-boundary-receipt.v1",
    ) == receipt
    assert store.path.stat().st_size < 4096
