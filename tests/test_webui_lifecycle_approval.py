from __future__ import annotations

import types


def test_shutdown_route_requires_external_approved_coordinator(monkeypatch):
    from api import routes

    responses = []

    def fake_json(handler, payload, **kwargs):
        responses.append((payload, kwargs.get("status", 200)))
        return True

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("shutdown route must not spawn a self-kill thread")

    monkeypatch.setattr(routes, "j", fake_json)
    monkeypatch.setattr(routes.threading, "Thread", ForbiddenThread)

    handler = types.SimpleNamespace(
        client_address=("127.0.0.1", 12345),
        command="POST",
        path="/api/shutdown",
        headers={"User-Agent": "pytest-agent"},
    )

    assert routes._handle_shutdown(handler) is True
    assert responses == [
        (
            {
                "ok": False,
                "error": "explicit_restart_approval_required",
                "message": (
                    "WebUI cannot stop or restart itself. Request a restart in chat; "
                    "the external coordinator will require fresh approval and verify recovery."
                ),
            },
            409,
        )
    ]


def test_update_restart_scheduler_never_reexecs_the_webui(monkeypatch):
    from api import updates

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("update path must not spawn a self-restart thread")

    monkeypatch.setattr(updates.threading, "Thread", ForbiddenThread)

    assert updates._schedule_restart() is False


def test_visible_controls_do_not_claim_or_trigger_self_restart():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    boot = (root / "static/boot.js").read_text(encoding="utf-8")
    index = (root / "static/index.html").read_text(encoding="utf-8")
    ui = (root / "static/ui.js").read_text(encoding="utf-8")

    shutdown_function = boot.split("async function shutdownServer()", 1)[1].split(
        "function _showServerStopped", 1
    )[0]
    assert "/api/shutdown" not in shutdown_function
    assert "hermes-webui-server-stopped" not in shutdown_function
    assert 'id="shutdownServerBlock"' not in index
    assert "if(res.restart_required) restartRequired=true" in ui
    assert "Force update staged — ask in chat to restart with fresh approval." in ui
    clear_lock_body = ui.split("async function applyClearUpdateLock", 1)[1].split(
        "function _renderLockManualInstruction", 1
    )[0]
    assert "if(res.restart_required)" in clear_lock_body
