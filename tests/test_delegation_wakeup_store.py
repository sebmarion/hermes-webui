from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

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
    store.close()

    restarted = DelegationWakeupStore(path)
    assert restarted.recover_claims() == 1
    assert restarted.list_pending()[0]["state"] == "pending"
