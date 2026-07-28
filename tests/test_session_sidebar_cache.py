import threading
import time
from types import SimpleNamespace

import pytest

import api.config as config
import api.routes as routes
from api import route_session_list_cache, session_events


def _wait_for_background_refreshes() -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        with routes._SESSIONS_CACHE_LOCK:
            events = list(routes._SESSIONS_CACHE_INFLIGHT.values())
        if not events:
            return
        for event in events:
            event.wait(0.05)
    raise AssertionError("session-list background refresh did not settle")


@pytest.fixture(autouse=True)
def _isolated_session_list_cache_state():
    _wait_for_background_refreshes()
    routes._session_list_cache_clear()
    yield
    _wait_for_background_refreshes()
    routes._session_list_cache_clear()


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(str(name))


def _session_cache_payload(marker: str, *, all_profiles: bool = False) -> dict:
    return {
        "sessions": [{"session_id": marker}],
        "cli_count": 0,
        "all_profiles": all_profiles,
        "active_profile": None,
        "other_profile_count": 0,
    }


def test_session_list_cache_key_separates_profile_and_all_profiles(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    routes._session_list_cache_clear()

    calls = []

    def builder_profile_a():
        calls.append("default")
        return _session_cache_payload("a")

    def builder_profile_a_all():
        calls.append("default_all")
        return _session_cache_payload("a_all", all_profiles=True)

    def builder_profile_b():
        calls.append("other")
        return _session_cache_payload("b")

    key_a = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="other",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_b, builder=builder_profile_b) == _session_cache_payload("b")
    assert calls == ["default", "other"]
    assert routes._get_cached_session_list_payload(key=key_a_all, builder=builder_profile_a_all) == _session_cache_payload("a_all", all_profiles=True)
    assert calls == ["default", "other", "default_all"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default", "other", "default_all"]


def test_session_list_cache_singleflight_rebuild_once(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    started = threading.Event()
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def builder():
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        release.wait()
        return _session_cache_payload("singleflight")

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    results = []
    errors = []

    def reader():
        try:
            results.append(routes._get_cached_session_list_payload(key=key, builder=builder))
        except Exception as exc:
            errors.append(exc)

    owner = threading.Thread(target=reader)
    follower = threading.Thread(target=reader)
    owner.start()
    assert started.wait(1.0)
    follower.start()
    release.set()
    owner.join(2)
    follower.join(2)
    assert not errors
    assert len(results) == 2
    assert results[0] == _session_cache_payload("singleflight")
    assert results[1] == _session_cache_payload("singleflight")
    assert calls == 1


def test_session_list_cache_has_one_refresh_owner_across_variant_keys(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    started = threading.Event()
    release = threading.Event()
    calls = []
    key_a = routes._session_list_cache_key(
        active_profile="default", all_profiles=False,
        show_cli_sessions=False, show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="default", all_profiles=False,
        show_cli_sessions=True, show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    def owner_builder():
        calls.append("owner")
        started.set()
        release.wait(1.0)
        return _session_cache_payload("owner")

    def follower_builder():
        calls.append("follower")
        return _session_cache_payload("follower")

    owner = threading.Thread(
        target=lambda: routes._get_cached_session_list_payload(
            key=key_a,
            builder=owner_builder,
            seed_builder=lambda: _session_cache_payload("seed-a"),
        )
    )
    follower_result = []
    owner.start()
    assert started.wait(1.0)
    follower_result.append(
        routes._get_cached_session_list_payload(
            key=key_b,
            builder=follower_builder,
            seed_builder=lambda: _session_cache_payload("seed-b"),
        )
    )
    release.set()
    owner.join(2.0)
    _wait_for_background_refreshes()

    assert calls == ["owner"]
    assert follower_result == [_session_cache_payload("seed-b")]


def test_session_list_cache_follower_wait_stage_when_rebuild_inflight(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    started = threading.Event()
    release = threading.Event()

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("wait")

    follower_diag = _StageRecorder()
    owner_diag = _StageRecorder()
    wait_seen = threading.Event()
    original_follower_stage = follower_diag.stage

    def follower_stage(name):
        original_follower_stage(name)
        if name == "session_list_cache_wait":
            wait_seen.set()

    follower_diag.stage = follower_stage

    def owner():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=owner_diag,
        )

    def follower():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=follower_diag,
        )

    owner_thread = threading.Thread(target=owner)
    follower_thread = threading.Thread(target=follower)
    try:
        owner_thread.start()
        assert started.wait(1.0)
        follower_thread.start()
        assert wait_seen.wait(1.0)
    finally:
        release.set()
        owner_thread.join(2)
        follower_thread.join(2)

    assert "session_list_cache_wait" in follower_diag.stages
    assert "session_list_cache_hit" in owner_diag.stages or "session_list_cache_stored" in owner_diag.stages


def test_session_list_cache_source_change_returns_stale_and_rebuilds_off_request(monkeypatch):
    routes._session_list_cache_clear()

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    # Source changes must never turn the caller into a rebuild worker.
    monkeypatch.setattr(
        routes,
        "_session_list_cache_source_stamp",
        lambda _key: ("changed",),
    )

    started = threading.Event()
    release = threading.Event()
    diag = _StageRecorder()

    def builder():
        started.set()
        release.wait(1.0)
        return _session_cache_payload("fresh")

    try:
        result = routes._get_cached_session_list_payload(
            key=key, builder=builder, diag=diag
        )
        assert result == _session_cache_payload("stale")
        assert started.wait(1.0)
    finally:
        release.set()

    assert "session_list_cache_stale_background_rebuild" in diag.stages


def test_session_list_cache_cold_seed_returns_before_background_builder(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    started = threading.Event()
    release = threading.Event()

    def builder():
        started.set()
        release.wait(1.0)
        return _session_cache_payload("fresh")

    try:
        result = routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            seed_builder=lambda: _session_cache_payload("index-seed"),
        )
        assert result == _session_cache_payload("index-seed")
        assert started.wait(1.0)
    finally:
        release.set()


def test_ten_cold_callers_share_one_background_refresh(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    started = threading.Event()
    release = threading.Event()
    calls = 0
    seed_calls = 0
    calls_lock = threading.Lock()

    def builder():
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(1.0)
        return _session_cache_payload("fresh")

    def seed_builder():
        nonlocal seed_calls
        with calls_lock:
            seed_calls += 1
        return _session_cache_payload("index-seed")

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                routes._get_cached_session_list_payload(
                    key=key,
                    builder=builder,
                    seed_builder=seed_builder,
                )
            )
        )
        for _ in range(10)
    ]
    try:
        for thread in threads:
            thread.start()
        assert started.wait(1.0)
        for thread in threads:
            thread.join(1.0)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        release.set()

    assert calls == 1
    assert seed_calls == 1
    assert results == [_session_cache_payload("index-seed")] * 10


def test_session_list_cache_owner_returns_stale_and_rebuilds_in_background(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    started = threading.Event()
    release = threading.Event()
    diag = _StageRecorder()

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("fresh")

    try:
        result = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
        assert result == _session_cache_payload("stale")
        assert started.wait(1.0), "stale owner should kick off a background rebuild"
        assert "session_list_cache_stale_background_rebuild" in diag.stages
        # While background rebuild is still blocked, stale is still returned.
        cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
        assert cached == _session_cache_payload("stale")
        assert fresh is False
    finally:
        release.set()

    # The background owner should eventually populate fresh cache.
    deadline = threading.Event()
    for _ in range(20):
        cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
        if cached == _session_cache_payload("fresh"):
            break
        deadline.wait(0.05)
    assert cached == _session_cache_payload("fresh")


def test_session_list_cache_stale_background_rebuild_failure_releases_owner(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    started = threading.Event()
    logged = []

    def record_exception(message):
        logged.append(message)

    monkeypatch.setattr(routes.logger, "exception", record_exception)

    def failing_builder():
        started.set()
        raise RuntimeError("boom")

    result = routes._get_cached_session_list_payload(key=key, builder=failing_builder)

    assert result == _session_cache_payload("stale")
    assert started.wait(1.0), "background rebuild should have attempted the builder"
    for _ in range(20):
        with routes._SESSIONS_CACHE_LOCK:
            inflight_empty = key not in routes._SESSIONS_CACHE_INFLIGHT
        if inflight_empty:
            break
        threading.Event().wait(0.05)
    assert inflight_empty, "failed background rebuild must release the singleflight owner"
    assert logged == ["session list stale-cache background rebuild failed"]
    cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
    assert cached == _session_cache_payload("stale")
    assert fresh is False

    def recovery_builder():
        return _session_cache_payload("fresh")

    # Once the failed owner releases, a later request can retry instead of being
    # pinned behind the dead background rebuild.
    routes._get_cached_session_list_payload(key=key, builder=recovery_builder)
    for _ in range(20):
        cached, _fresh = routes._session_list_cache_get(key, allow_stale=True)
        if cached == _session_cache_payload("fresh"):
            break
        threading.Event().wait(0.05)
    assert cached == _session_cache_payload("fresh")


def test_session_list_cache_invalidated_on_session_list_publish(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    routes._session_list_cache_clear()

    key_a = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="profile-b",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    routes._session_list_cache_set(key_a, _session_cache_payload("a"))
    routes._session_list_cache_set(key_b, _session_cache_payload("b"))
    routes._session_list_cache_set(key_a_all, _session_cache_payload("a_all", all_profiles=True))

    session_events.publish_session_list_changed("session_pin", profile="profile-a")

    assert routes._session_list_cache_get(key_a)[0] is None
    assert routes._session_list_cache_get(key_a_all)[0] is None
    assert routes._session_list_cache_get(key_b)[0] is not None


def test_session_list_cache_rebuild_retries_after_invalidation():
    routes._session_list_cache_clear()

    key = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    calls = []

    def builder():
        calls.append("build")
        if len(calls) == 1:
            routes._session_list_cache_clear("profile-a")
            return _session_cache_payload("stale")
        return _session_cache_payload("fresh")

    payload = routes._get_cached_session_list_payload(key=key, builder=builder)

    assert payload == _session_cache_payload("fresh")
    assert calls == ["build", "build"]


def test_session_list_cache_source_stamp_tracks_projection_generation(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    state_db_wal = tmp_path / "state.db-wal"
    state_db_wal.write_text("wal-1", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)
    generation = {"value": 1}
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_projection_token",
        lambda _path: ("projection", generation["value"]),
    )

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    generation["value"] = 2
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_cache_source_stamp_tracks_settings_file(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"show_cli_sessions": false}', encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    settings_file.write_text('{"show_cli_sessions": true}', encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_cache_source_stamp_tracks_settings_write_version(
    tmp_path,
    monkeypatch,
):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    monkeypatch.setattr(config, "_SETTINGS_WRITE_VERSION", config._SETTINGS_WRITE_VERSION + 1)
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_payload_to_response_overlays_live_stream_runtime(monkeypatch):
    payload = {
        "sessions": [
            {
                "session_id": "streaming-session",
                "title": "Live title",
                "active_stream_id": None,
                "is_streaming": False,
                "has_pending_user_message": False,
            },
            {
                "session_id": "stale-session",
                "title": "Old title",
                "active_stream_id": "stale-stream",
                "is_streaming": True,
                "has_pending_user_message": True,
            },
        ],
        "cli_count": 0,
        "all_profiles": False,
        "active_profile": "default",
        "other_profile_count": 0,
    }

    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"live-stream"})
    with routes.LOCK:
        original = dict(routes.SESSIONS)
        routes.SESSIONS.clear()
        routes.SESSIONS["streaming-session"] = SimpleNamespace(
            active_stream_id="live-stream",
            pending_user_message="queued prompt",
        )
        routes.SESSIONS["stale-session"] = SimpleNamespace(
            active_stream_id=None,
            pending_user_message=None,
        )
    try:
        response = routes._session_list_payload_to_response(payload)
    finally:
        with routes.LOCK:
            routes.SESSIONS.clear()
            routes.SESSIONS.update(original)

    by_id = {row["session_id"]: row for row in response["sessions"]}
    assert by_id["streaming-session"]["active_stream_id"] == "live-stream"
    assert by_id["streaming-session"]["is_streaming"] is True
    assert by_id["streaming-session"]["has_pending_user_message"] is True
    assert by_id["stale-session"]["active_stream_id"] is None
    assert by_id["stale-session"]["is_streaming"] is False
    assert by_id["stale-session"]["has_pending_user_message"] is False


def _build_stamp_env(tmp_path, monkeypatch):
    """Wire a self-contained source-stamp environment and return its key."""
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    state_db_wal = tmp_path / "state.db-wal"
    state_db_wal.write_text("wal-1", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)
    # Make the content fingerprint deterministic and unaffected by the dummy
    # text-file state.db (a real sqlite connect would just return None here).
    fingerprint = {"value": (1, 1)}
    monkeypatch.setattr(
        routes,
        "_session_list_cache_state_db_fingerprint",
        lambda _p: fingerprint["value"],
    )

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return key, state_db_wal, settings_file, fingerprint


def test_source_stamp_freezes_during_streaming_message_writes(tmp_path, monkeypatch):
    """#4672: per-token state.db churn must not bust the cache mid-stream.

    With an active stream the volatile state.db-derived components are collapsed
    to a stream-set marker, so advancing the WAL stat AND the content
    fingerprint (the per-message-write signals) leaves the stamp unchanged.
    Before the fix each of these advanced the stamp and forced a rebuild on
    every poll.
    """
    key, state_db_wal, _settings_file, fingerprint = _build_stamp_env(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    before = routes._session_list_cache_source_stamp(key)
    # Simulate the writes an active chat turn makes to state.db: WAL grows and
    # the messages-table fingerprint advances.
    state_db_wal.write_text("wal-2-grew-a-lot", encoding="utf-8")
    fingerprint["value"] = (2, 99)
    after = routes._session_list_cache_source_stamp(key)

    assert after == before


def test_source_stamp_changes_when_stream_set_transitions(tmp_path, monkeypatch):
    """The hold-down marker tracks the active-stream SET, so a turn starting or
    finishing re-validates the cache and the final title/count is picked up."""
    key, _wal, _settings, _fp = _build_stamp_env(tmp_path, monkeypatch)

    streams = {"value": set()}
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set(streams["value"]))

    idle = routes._session_list_cache_source_stamp(key)
    streams["value"] = {"turn-1"}
    streaming = routes._session_list_cache_source_stamp(key)
    streams["value"] = {"turn-1", "turn-2"}
    two_streams = routes._session_list_cache_source_stamp(key)
    streams["value"] = set()
    idle_again = routes._session_list_cache_source_stamp(key)

    assert idle != streaming
    assert streaming != two_streams
    # Returning to idle re-engages the live state.db stamp path.
    assert idle_again != streaming


def test_source_stamp_tracks_settings_even_while_streaming(tmp_path, monkeypatch):
    """Settings-file / sidebar-toggle changes must still invalidate the cache
    during streaming so user-initiated changes are never held stale."""
    key, _wal, settings_file, _fp = _build_stamp_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    before = routes._session_list_cache_source_stamp(key)
    settings_file.write_text('{"show_cli_sessions": true}', encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_source_stamp_ignores_wal_churn_when_idle(tmp_path, monkeypatch):
    """Agent message WAL writes do not invalidate the sidebar request cache.

    The background projection monitor publishes only the Agent-owned generation,
    so subagent and other non-sidebar writes cannot force request-time work.
    """
    key, state_db_wal, _settings, _fp = _build_stamp_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        route_session_list_cache,
        "_session_list_projection_token",
        lambda _path: ("projection", 9),
    )

    before = routes._session_list_cache_source_stamp(key)
    state_db_wal.write_text("wal-2-more", encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after == before


def _cache_policy_key():
    return routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


def _age_cache_entry(key, seconds):
    """Backdate the cache entry's timestamp by `seconds` (keep stamp+payload)."""
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (ts - seconds, stamp, payload)


def test_cache_age_policy_is_unified_during_streaming(monkeypatch):
    """Idle and streaming reads share the same bounded age policy."""
    routes._session_list_cache_clear()
    key = _cache_policy_key()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))

    for active_stream_ids in (set(), {"turn-1"}):
        monkeypatch.setattr(
            routes,
            "_active_stream_ids",
            lambda active=active_stream_ids: active,
        )
        routes._session_list_cache_set(key, _session_cache_payload("fresh"))
        _age_cache_entry(key, 29.0)
        payload, fresh = routes._session_list_cache_get(key)
        assert fresh is True
        assert payload == _session_cache_payload("fresh")

        routes._session_list_cache_set(key, _session_cache_payload("expired"))
        _age_cache_entry(key, 30.0)
        payload, fresh = routes._session_list_cache_get(key)
        assert payload is None
        assert fresh is False


def test_cache_convergence_bound_still_evicts_during_streaming(monkeypatch):
    """The shared age hold-down remains bounded while streaming."""
    routes._session_list_cache_clear()
    key = _cache_policy_key()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    routes._session_list_cache_set(key, _session_cache_payload("old"))
    _age_cache_entry(key, routes._SESSIONS_CACHE_STREAMING_TTL_SECONDS + 1.0)
    payload, fresh = routes._session_list_cache_get(key)
    assert payload is None and fresh is False


def test_idle_cache_spans_activity_poll_but_expires_at_convergence_bound(monkeypatch):
    routes._session_list_cache_clear()
    key = _cache_policy_key()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    routes._session_list_cache_set(key, _session_cache_payload("idle"))
    _age_cache_entry(key, 5.5)
    payload, fresh = routes._session_list_cache_get(key)
    assert fresh is True
    assert payload == _session_cache_payload("idle")

    routes._session_list_cache_set(key, _session_cache_payload("expired"))
    _age_cache_entry(key, 30.1)
    payload, fresh = routes._session_list_cache_get(key)
    assert payload is None
    assert fresh is False
