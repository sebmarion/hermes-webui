"""Health exposes embedded immutable build identity without trusting selector text."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

from api import routes


def _health_payload(monkeypatch, identity, *, path="/health"):
    captured = {}

    def _identity(*, refresh=False):
        captured["refresh"] = refresh
        return identity

    monkeypatch.setattr(routes, "get_build_identity", _identity)
    monkeypatch.setattr(
        routes,
        "_streams_lock_health",
        lambda: {"status": "ok", "active_streams": 0},
    )
    monkeypatch.setattr(
        routes,
        "_run_lifecycle_health",
        lambda: {"active_runs": 0, "runs": [], "last_run_finished_at": None},
    )
    monkeypatch.setattr(routes, "_accept_loop_health", lambda _handler: {})
    monkeypatch.setattr(routes, "_deep_health_checks", lambda **_kwargs: ({}, True))

    def _capture(_handler, payload, status=200, **_kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _capture)
    routes._handle_health(SimpleNamespace(server=None), urlparse(path))
    return captured


def test_health_exposes_managed_embedded_build_identity(monkeypatch):
    identity = {
        "status": "managed",
        "valid": True,
        "build_id": "release-1",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "manifest_sha256": "c" * 64,
        "agent_commit": "d" * 40,
        "agent_tree": "e" * 40,
        "agent_manifest_sha256": "f" * 64,
        "runtime_manifest_sha256": "1" * 64,
        "selector_generation": 4,
        "release_path": "/immutable/release-1",
    }

    captured = _health_payload(monkeypatch, identity)

    assert captured["status"] == 200
    assert captured["payload"]["status"] == "ok"
    assert captured["payload"]["build"]["build_id"] == "release-1"
    assert captured["payload"]["build"]["manifest_sha256"] == "c" * 64
    assert captured["payload"]["build"]["agent_commit"] == "d" * 40
    assert captured["payload"]["build"]["agent_tree"] == "e" * 40
    assert captured["payload"]["build"]["agent_manifest_sha256"] == "f" * 64
    assert captured["payload"]["build"]["runtime_manifest_sha256"] == "1" * 64
    assert "release_path" not in captured["payload"]["build"]
    assert "process" not in captured["payload"]
    assert "pid" not in captured["payload"]
    assert "fence_token" not in captured["payload"]["admission"]
    assert "transaction_id" not in captured["payload"]["admission"]
    assert captured["refresh"] is False


def test_health_fails_closed_for_invalid_managed_build(monkeypatch):
    captured = _health_payload(
        monkeypatch,
        {
            "status": "invalid",
            "valid": False,
            "error_code": "manifest_verification_failed",
        },
    )

    assert captured["status"] == 503
    assert captured["payload"]["status"] == "degraded"
    assert captured["payload"]["build"]["valid"] is False


def test_deep_health_forces_fresh_build_attestation(monkeypatch):
    captured = _health_payload(
        monkeypatch,
        {"status": "managed", "valid": True},
        path="/health?deep=1",
    )

    assert captured["refresh"] is True
