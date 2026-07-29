import json
import sqlite3
from pathlib import Path

import pytest

from api import managed_startup_session_recovery as managed


TRANSACTION_ID = "session-audit-transaction-00000001"
MANIFEST_SHA256 = "a" * 64


def _write_clean_sessions(root: Path) -> None:
    (root / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
    )
    (root / "_index.json").write_text(json.dumps([{"session_id": "s1"}]))


def _write_state_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            create table sessions (id text primary key, source text);
            create table messages (
                session_id text,
                role text,
                content text
            );
            insert into sessions values ('s1', 'webui');
            insert into messages values ('s1', 'user', 'hello');
            """
        )


def test_clean_bound_audit_is_complete_and_verifies_independently(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)

    receipt = managed.audit_managed_startup_sessions(
        session_dir,
        state_db,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
    )
    assert receipt.outcome is managed.SessionRecoveryOutcome.PROVED_COMPLETE
    assert receipt.transaction_id == TRANSACTION_ID
    assert receipt.manifest_sha256 == MANIFEST_SHA256
    assert receipt.session_ids == ("s1",)
    assert (
        managed.verify_managed_startup_sessions(receipt).outcome
        is managed.SessionRecoveryOutcome.PROVED_COMPLETE
    )


@pytest.mark.parametrize(
    ("transaction_id", "manifest_sha256"),
    [
        (None, None),
        (TRANSACTION_ID, None),
        (None, MANIFEST_SHA256),
    ],
)
def test_managed_audit_rejects_missing_or_partial_binding(
    tmp_path,
    transaction_id,
    manifest_sha256,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)

    with pytest.raises(managed.ManagedStartupSessionBindingError):
        managed.audit_managed_startup_sessions(
            session_dir,
            None,
            transaction_id=transaction_id,
            manifest_sha256=manifest_sha256,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: (root / "s1.json.bak").write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "messages": [{}, {}],
                }
            )
        ),
        lambda root: (root / "_index.json").write_text("[]"),
        lambda root: (root / "_deleted_webui_sessions.json").write_text("{"),
    ],
)
def test_recoverable_stale_or_malformed_state_is_terminal_ambiguous(
    tmp_path, mutate
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    mutate(session_dir)

    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            None,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_pending_journal_is_ambiguous(tmp_path):
    session_dir = tmp_path / "sessions"
    journal_dir = session_dir / "_turn_journal"
    journal_dir.mkdir(parents=True)
    _write_clean_sessions(session_dir)
    (journal_dir / "s1~123.jsonl").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "turn_id": "turn-1",
                "event": "submitted",
                "content": "not persisted",
                "created_at": 1,
            }
        )
        + "\n"
    )
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            None,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


@pytest.mark.parametrize("db_shape", ["corrupt", "old_schema"])
def test_corrupt_or_old_state_db_fails_closed(tmp_path, db_shape):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    if db_shape == "corrupt":
        state_db.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(state_db) as conn:
            conn.execute("create table sessions (id text)")

    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_locked_database_fails_closed(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)
    owner = sqlite3.connect(state_db, timeout=0)
    owner.execute("begin exclusive")
    try:
        with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
            managed.audit_managed_startup_sessions(
                session_dir,
                state_db,
                transaction_id=TRANSACTION_ID,
                manifest_sha256=MANIFEST_SHA256,
            )
    finally:
        owner.rollback()
        owner.close()


def test_wal_bundle_mutation_during_query_fails_closed(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)
    original = managed._query_state_db_strict

    def mutate_wal(*args, **kwargs):
        result = original(*args, **kwargs)
        Path(str(state_db) + "-wal").write_bytes(b"changed")
        return result

    monkeypatch.setattr(managed, "_query_state_db_strict", mutate_wal)
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_consistent_live_wal_bundle_is_audited_from_held_snapshot(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    owner = sqlite3.connect(state_db)
    owner.execute("pragma journal_mode=wal")
    owner.execute("pragma wal_autocheckpoint=0")
    owner.executescript(
        """
        create table sessions (id text primary key, source text);
        create table messages (session_id text, role text, content text);
        insert into sessions values ('s1', 'webui');
        insert into messages values ('s1', 'user', 'hello');
        """
    )
    owner.commit()
    try:
        receipt = managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )
    finally:
        owner.close()

    assert receipt.outcome is managed.SessionRecoveryOutcome.PROVED_COMPLETE
    assert dict(receipt.state_db_bundle)["-wal"] is not None
    assert dict(receipt.state_db_bundle)["-shm"] is not None


def test_database_path_swap_and_restore_during_query_fails_closed(
    tmp_path,
    monkeypatch,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)
    alternate = tmp_path / "alternate.db"
    _write_state_db(alternate)
    with sqlite3.connect(alternate) as connection:
        connection.execute("pragma user_version=73")
    parked = tmp_path / "parked.db"
    original = managed._query_state_db_strict

    def swap_query(path):
        state_db.rename(parked)
        alternate.rename(state_db)
        try:
            return original(path)
        finally:
            state_db.unlink()
            parked.rename(state_db)

    monkeypatch.setattr(managed, "_query_state_db_strict", swap_query)

    with pytest.raises(
        managed.ManagedStartupSessionAmbiguousError,
        match="database.*snapshot|database.*changed|database.*held",
    ):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_bounds_and_symlink_swap_fail_closed(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    monkeypatch.setattr(managed, "_MAX_FILE_BYTES", 2)
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            None,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )

    monkeypatch.setattr(managed, "_MAX_FILE_BYTES", 8 * 1024 * 1024)
    outside = tmp_path / "outside"
    outside.write_text("{}")
    (session_dir / "s1.json").unlink()
    (session_dir / "s1.json").symlink_to(outside)
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            None,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_database_and_journal_count_bounds_fail_closed(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)
    monkeypatch.setattr(managed, "_MAX_MESSAGES", 0)
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )

    monkeypatch.setattr(managed, "_MAX_MESSAGES", 2_000_000)
    journal_dir = session_dir / "_turn_journal"
    journal_dir.mkdir()
    (journal_dir / "s1~123.jsonl").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "s1",
                "turn_id": "turn-1",
                "event": "completed",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(managed, "_MAX_JOURNAL_EVENTS", 0)
    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_audit_never_calls_legacy_mutators(tmp_path, monkeypatch):
    from api import session_recovery

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    monkeypatch.setattr(
        session_recovery,
        "repair_safe_session_recovery",
        lambda *_a, **_k: pytest.fail("repair called"),
    )
    monkeypatch.setattr(
        session_recovery,
        "recover_all_sessions_on_startup",
        lambda *_a, **_k: pytest.fail("legacy recovery called"),
    )
    managed.audit_managed_startup_sessions(
        session_dir,
        None,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
    )


@pytest.mark.parametrize("guard", ["clear", "compression"])
def test_intentional_shrink_guards_do_not_report_recoverable_loss(
    tmp_path,
    guard,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    live = {
        "session_id": "s1",
        "messages": [],
        "context_messages": [],
    }
    backup = {
        "session_id": "s1",
        "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
        ],
        "context_messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
        ],
    }
    if guard == "clear":
        live.update(
            {
                "clear_generation": "clear-1",
                "truncation_watermark": 0.0,
                "truncation_boundary": 0.0,
                "active_stream_id": None,
                "pending_user_message": None,
                "pending_attachments": [],
                "pending_started_at": None,
                "pending_user_source": None,
            }
        )
    else:
        live.update(
            {
                "messages": [
                    {"role": "user", "content": "visible"},
                    {"role": "assistant", "content": "visible answer"},
                ],
                "context_messages": [
                    {
                        "role": "system",
                        "content": "[context compaction summary]",
                    }
                ],
                "compression_anchor_mode": "manual",
            }
        )
        backup["messages"].append({"role": "user", "content": "third"})
    (session_dir / "s1.json").write_text(json.dumps(live))
    (session_dir / "s1.json.bak").write_text(json.dumps(backup))
    (session_dir / "_index.json").write_text(json.dumps([{"session_id": "s1"}]))

    receipt = managed.audit_managed_startup_sessions(
        session_dir,
        None,
        transaction_id=TRANSACTION_ID,
        manifest_sha256=MANIFEST_SHA256,
    )

    assert receipt.outcome is managed.SessionRecoveryOutcome.PROVED_COMPLETE


@pytest.mark.parametrize("shape", ["empty_missing_sidecar", "orphan_message"])
def test_database_empty_missing_sidecars_and_orphan_messages_are_ambiguous(
    tmp_path,
    shape,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    _write_clean_sessions(session_dir)
    state_db = tmp_path / "state.db"
    _write_state_db(state_db)
    with sqlite3.connect(state_db) as connection:
        if shape == "empty_missing_sidecar":
            connection.execute(
                "insert into sessions values ('missing', 'webui')"
            )
        else:
            connection.execute(
                "insert into messages values ('ghost', 'user', 'orphan')"
            )

    with pytest.raises(managed.ManagedStartupSessionAmbiguousError):
        managed.audit_managed_startup_sessions(
            session_dir,
            state_db,
            transaction_id=TRANSACTION_ID,
            manifest_sha256=MANIFEST_SHA256,
        )


def test_server_routes_unmanaged_to_exact_legacy_and_managed_to_audit(
    tmp_path, monkeypatch
):
    import server
    from api import config, models, session_recovery

    calls = []
    monkeypatch.setattr(server, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    monkeypatch.setattr(
        session_recovery,
        "recover_all_sessions_on_startup",
        lambda *args, **kwargs: calls.append(("legacy", args, kwargs))
        or {"scanned": 0, "restored": 0},
    )
    monkeypatch.setattr(
        config, "_managed_release_selected_from_environment", lambda: False
    )
    assert server._recover_startup_sessions() is None
    assert calls[0][0] == "legacy"
    assert calls[0][2]["rebuild_index"] is True

    receipt = object()
    monkeypatch.setattr(
        config, "_managed_release_selected_from_environment", lambda: True
    )
    monkeypatch.setattr(
        managed,
        "audit_managed_startup_sessions",
        lambda *_a, **_k: calls.append(("managed", _a, _k)) or receipt,
    )
    canonical_manifest = server.release_manifest.deferred_release_manifest_sha256()
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", TRANSACTION_ID)
    monkeypatch.setenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", TRANSACTION_ID)
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", canonical_manifest)
    assert server._recover_startup_sessions() is receipt
    assert calls[-1][0] == "managed"
    assert calls[-1][2] == {
        "transaction_id": TRANSACTION_ID,
        "manifest_sha256": canonical_manifest,
    }


@pytest.mark.parametrize(
    ("active", "environment", "manifest"),
    [
        (TRANSACTION_ID, None, None),
        (TRANSACTION_ID, TRANSACTION_ID, None),
        (TRANSACTION_ID, "other-transaction-" + ("x" * 32), "canonical"),
        (TRANSACTION_ID, TRANSACTION_ID, "wrong"),
    ],
)
def test_server_managed_session_adapter_rejects_noncanonical_binding(
    tmp_path,
    monkeypatch,
    active,
    environment,
    manifest,
):
    import server
    from api import config, models

    canonical = server.release_manifest.deferred_release_manifest_sha256()
    monkeypatch.setattr(server, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    monkeypatch.setattr(
        config, "_managed_release_selected_from_environment", lambda: True
    )
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", active)
    if environment is None:
        monkeypatch.delenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", environment)
    if manifest is None:
        monkeypatch.delenv("HERMES_WEBUI_MANIFEST_SHA256", raising=False)
    else:
        monkeypatch.setenv(
            "HERMES_WEBUI_MANIFEST_SHA256",
            canonical if manifest == "canonical" else MANIFEST_SHA256,
        )

    with pytest.raises(RuntimeError, match="binding"):
        server._recover_startup_sessions()


def test_server_session_reconciler_retains_and_verifies_receipt(
    tmp_path,
    monkeypatch,
):
    import server
    from api import config, models
    from deferred_startup_replay import Reconciliation

    receipt = object()
    canonical = server.release_manifest.deferred_release_manifest_sha256()
    monkeypatch.setattr(server, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: None)
    monkeypatch.setattr(
        config, "_managed_release_selected_from_environment", lambda: True
    )
    monkeypatch.setattr(config, "_RUN_ADMISSION_TRANSACTION_ID", TRANSACTION_ID)
    monkeypatch.setenv("HERMES_WEBUI_STARTUP_TRANSACTION_ID", TRANSACTION_ID)
    monkeypatch.setenv("HERMES_WEBUI_MANIFEST_SHA256", canonical)
    monkeypatch.setattr(server, "_MANAGED_STARTUP_SESSION_RECEIPT", None)
    monkeypatch.setattr(
        managed,
        "audit_managed_startup_sessions",
        lambda *_a, **_k: receipt,
    )
    monkeypatch.setattr(
        managed,
        "verify_managed_startup_sessions",
        lambda observed, **_k: type(
            "Verification",
            (),
            {"outcome": managed.SessionRecoveryOutcome.PROVED_COMPLETE},
        )()
        if observed is receipt
        else pytest.fail("receipt was not retained"),
    )

    assert server._recover_startup_sessions() is receipt
    assert server._reconcile_startup_sessions() is Reconciliation.PROVED_COMPLETE
