from types import SimpleNamespace
from urllib.parse import urlparse


def test_profiles_route_returns_active_profile(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    expected_profiles = [{"name": "default", "is_default": True}]

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: expected_profiles)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )

    response = routes.handle_get(SimpleNamespace(), urlparse("/api/profiles"))

    assert response == {
        "status": 200,
        "payload": {
            "profiles": expected_profiles,
            "active": "default",
            "single_profile_mode": False,
        },
    }


def test_profiles_api_defers_detailed_rebuild(monkeypatch):
    import threading
    import time
    import api.profiles as profiles

    started = threading.Event()
    release = threading.Event()
    minimal = [{"name": "default", "model": None, "skill_count": 0}]
    detailed = [{"name": "default", "model": "test-model", "skill_count": 7}]

    monkeypatch.setattr(profiles, "_LIST_PROFILES_CACHE", None)
    monkeypatch.setattr(profiles, "_LIST_PROFILES_REBUILDING", False)
    monkeypatch.setattr(profiles, "_LIST_PROFILES_CACHE_GENERATION", 0)
    monkeypatch.setattr(profiles, "_build_profile_rows_minimal", lambda: minimal)

    def slow_build():
        started.set()
        assert release.wait(2)
        return detailed

    monkeypatch.setattr(profiles, "_build_profile_rows_fast", slow_build)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    t0 = time.perf_counter()
    first = profiles.list_profiles_api()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.5
    assert started.wait(1)
    assert first[0]["model"] is None

    release.set()
    deadline = time.time() + 2
    while time.time() < deadline:
        if profiles._LIST_PROFILES_CACHE is not None:
            break
        time.sleep(0.01)
    second = profiles.list_profiles_api()
    assert second[0]["model"] == "test-model"
