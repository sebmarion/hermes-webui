from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import stat
import time

from api.delegation_wakeup_store import DelegationWakeupStore


def _record(store, *, delegation_id="deleg_1", session_id="session-a"):
    return store.record_pending(
        delegation_id=delegation_id,
        session_id=session_id,
        session_key=session_id,
        wakeup_prompt="[IMPORTANT: child completed]",
        event={"type": "async_delegation", "delegation_id": delegation_id},
    )


def test_pending_record_survives_fresh_store_instance(tmp_path):
    path = tmp_path / "wakeups.sqlite3"
    first = DelegationWakeupStore(path)
    assert _record(first).status == "inserted"
    first.close()

    restarted = DelegationWakeupStore(path)
    rows = restarted.list_pending()
    assert [row["delegation_id"] for row in rows] == ["deleg_1"]
    assert rows[0]["session_id"] == "session-a"


def test_duplicate_is_idempotent_and_cross_session_collision_fails_closed(tmp_path):
    store = DelegationWakeupStore(tmp_path / "wakeups.sqlite3")
    assert _record(store).status == "inserted"
    assert _record(store).status == "duplicate"
    assert _record(store, session_id="session-b").status == "collision"
    assert len(store.list_pending()) == 1


def test_concurrent_duplicate_insert_and_claim_are_atomic(tmp_path):
    store = DelegationWakeupStore(tmp_path / "wakeups.sqlite3")
    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted = list(pool.map(lambda _n: _record(store).status, range(16)))
    assert inserted.count("inserted") == 1
    assert inserted.count("duplicate") == 15

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _n: store.claim_next("session-a"), range(16)))
    claimed = [row for row in claims if row is not None]
    assert len(claimed) == 1
    assert claimed[0]["state"] == "claimed"
    assert claimed[0]["claim_token"]


def test_failed_claim_is_replayable_and_delivered_rows_are_retained(tmp_path):
    path = tmp_path / "wakeups.sqlite3"
    store = DelegationWakeupStore(path)
    _record(store)
    claimed = store.claim_next("session-a")
    assert store.release_claim("deleg_1", claimed["claim_token"], "thread start failed")
    assert [row["delegation_id"] for row in store.list_pending()] == ["deleg_1"]

    claimed = store.claim_next("session-a")
    assert store.mark_delivered("deleg_1", claimed["claim_token"])
    assert store.list_pending() == []
    delivered = store.get("deleg_1")
    assert delivered["state"] == "delivered"
    assert delivered["delivered_at"] is not None

    restarted = DelegationWakeupStore(path)
    assert restarted.get("deleg_1")["state"] == "delivered"


def test_startup_recovery_requeues_interrupted_claim(tmp_path):
    path = tmp_path / "wakeups.sqlite3"
    store = DelegationWakeupStore(path)
    _record(store)
    assert store.claim_next("session-a")["state"] == "claimed"
    store._conn.execute(
        "UPDATE delegation_wakeups SET lease_expires_at=? WHERE delegation_id='deleg_1'",
        (time.time() - 1,),
    )
    store._conn.commit()
    store.close()

    restarted = DelegationWakeupStore(path)
    assert restarted.recover_claims() == 1
    assert restarted.list_pending()[0]["state"] == "pending"


def test_store_repairs_private_permissions_under_permissive_umask(tmp_path):
    private = tmp_path / "private"
    old_umask = os.umask(0)
    try:
        store = DelegationWakeupStore(private / "wakeups.sqlite3")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = store.path.with_name(store.path.name + suffix)
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_live_foreign_claim_is_not_recovered_but_expired_claim_is(tmp_path):
    store = DelegationWakeupStore(tmp_path / "private" / "wakeups.sqlite3")
    _record(store)
    claim = store.claim_next("session-a", owner_uuid="owner-a", lease_seconds=60)
    assert claim["claim_owner"] == "owner-a"
    assert store.recover_claims(owner_uuid="owner-b") == 0
    store._conn.execute(
        "UPDATE delegation_wakeups SET lease_expires_at=? WHERE delegation_id='deleg_1'",
        (time.time() - 1,),
    )
    store._conn.commit()
    assert store.recover_claims(owner_uuid="owner-b") == 1


def test_profile_identity_is_part_of_dedupe_and_delivered_payload_is_compacted(tmp_path):
    store = DelegationWakeupStore(tmp_path / "private" / "wakeups.sqlite3")
    outcome = store.record_pending(
        delegation_id="deleg_1",
        session_id="session-a",
        session_key="session-a",
        wakeup_prompt="secret completion body",
        event={"summary": "secret result"},
        origin_profile="coder",
        origin_tracker_path="/profiles/coder/async_delegations.json",
        origin_ui_session_id="session-a",
    )
    assert outcome.status == "inserted"
    collision = store.record_pending(
        delegation_id="deleg_1",
        session_id="session-a",
        session_key="session-a",
        wakeup_prompt="secret completion body",
        event={"summary": "secret result"},
        origin_profile="other",
        origin_tracker_path="/profiles/other/async_delegations.json",
        origin_ui_session_id="session-a",
    )
    assert collision.status == "collision"
    claim = store.claim_next("session-a", owner_uuid="owner", lease_seconds=60)
    assert store.mark_delivered("deleg_1", claim["claim_token"])
    row = store.get("deleg_1")
    assert row["wakeup_prompt"] == ""
    assert row["event_json"] == ""


def test_legacy_store_is_transactionally_migrated_and_claims_become_recoverable(tmp_path, monkeypatch):
    from api import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    legacy = tmp_path / "delegation_wakeups.sqlite3"
    old = DelegationWakeupStore(legacy)
    _record(old)
    claim = old.claim_next("session-a", owner_uuid="old-process", lease_seconds=3600)
    assert claim["state"] == "claimed"
    old.close()
    os.chmod(legacy, 0o644)

    migrated = DelegationWakeupStore()
    assert migrated.path == tmp_path / "private" / "delegation_wakeups.sqlite3"
    row = migrated.get("deleg_1")
    assert row["state"] == "pending"
    assert row["claim_token"] is None
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600
    assert stat.S_IMODE(migrated.path.stat().st_mode) == 0o600
    migrated.close()

    restarted = DelegationWakeupStore()
    assert restarted.get("deleg_1")["state"] == "pending"
