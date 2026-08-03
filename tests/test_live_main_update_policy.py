"""Live-main mode must make WebUI-originated updates read-only."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import api.updates as updates


@pytest.mark.parametrize("entrypoint", ["apply_update", "apply_force_update", "apply_clear_lock"])
def test_live_main_blocks_mutation_before_any_side_effect(monkeypatch, entrypoint):
    monkeypatch.setenv("HERMES_WEBUI_LIVE_MAIN", "1")
    run_git = Mock()
    acquire = Mock()
    restart = Mock()
    inventory = Mock()
    blocker_snapshot = Mock(side_effect=AssertionError("live-main guard ran too late"))

    with (
        patch.object(updates, "_run_git", run_git),
        patch.object(updates, "_schedule_restart", restart),
        patch.object(updates, "_inventory_locks", inventory),
        patch.object(updates, "_restart_blocker_snapshot", blocker_snapshot),
        patch.object(updates, "_apply_lock") as apply_lock,
    ):
        apply_lock.acquire = acquire
        result = getattr(updates, entrypoint)("webui")

    assert result == {
        "ok": False,
        "agent_merge_required": True,
        "target": "webui",
        "message": result["message"],
    }
    assert result["message"]
    run_git.assert_not_called()
    acquire.assert_not_called()
    restart.assert_not_called()
    inventory.assert_not_called()
    blocker_snapshot.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "FALSE"])
def test_live_main_mode_requires_truthy_opt_in(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("HERMES_WEBUI_LIVE_MAIN", raising=False)
    else:
        monkeypatch.setenv("HERMES_WEBUI_LIVE_MAIN", value)
    assert updates._is_live_main_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_live_main_mode_accepts_only_explicit_truthy_values(monkeypatch, value):
    monkeypatch.setenv("HERMES_WEBUI_LIVE_MAIN", value)
    assert updates._is_live_main_mode() is True


def test_live_main_update_status_marks_payload_read_only(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LIVE_MAIN", "1")
    monkeypatch.setattr(updates, "_check_repo", lambda path, name, channel: {
        "name": name,
        "behind": 1,
    })
    monkeypatch.setattr(updates, "_update_cache", {
        "webui": None,
        "agent": None,
        "checked_at": 0,
        "include_agent": True,
        "channel": "stable",
    })

    result = updates.check_for_updates(force=True, include_agent=False, channel="stable")

    assert result["live_main"] is True
    assert result["webui"]["behind"] == 1


@pytest.mark.parametrize("path", ["/api/updates/apply", "/api/updates/force"])
def test_update_routes_return_agent_handoff_in_live_main(monkeypatch, path):
    monkeypatch.setenv("HERMES_WEBUI_LIVE_MAIN", "1")
    import api.routes as routes

    captured = {}

    def fake_json(_handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"target": "webui"})
    monkeypatch.setattr(routes, "j", fake_json)

    assert routes.handle_post(SimpleNamespace(), SimpleNamespace(path=path)) is True
    assert captured["status"] == 200
    assert captured["payload"]["agent_merge_required"] is True
    assert captured["payload"]["target"] == "webui"
