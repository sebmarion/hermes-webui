"""Read-only activity sources used by the atomic release barrier."""

from __future__ import annotations


class _Thread:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _Terminal:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


def test_memory_commit_activity_snapshot_counts_live_and_inflight(monkeypatch):
    from api import session_lifecycle

    monkeypatch.setattr(
        session_lifecycle,
        "_background_commit_threads",
        {_Thread(True), _Thread(False)},
    )
    monkeypatch.setattr(
        session_lifecycle,
        "_sessions",
        {
            "active": {"in_flight": True},
            "idle": {"in_flight": False},
        },
    )

    assert session_lifecycle.background_commit_activity_snapshot() == {
        "active_background_memory_commits": 1,
        "in_flight_memory_commits": 1,
        "memory_commit_activity_available": True,
    }


def test_oauth_activity_snapshot_counts_only_pending_flows(monkeypatch):
    from api import oauth

    monkeypatch.setattr(
        oauth,
        "_OAUTH_FLOWS",
        {
            "pending": {"status": "pending"},
            "done": {"status": "success"},
            "failed": {"status": "error"},
        },
    )

    assert oauth.oauth_activity_snapshot() == {
        "pending_oauth_flows": 1,
        "oauth_activity_available": True,
    }


def test_terminal_activity_snapshot_counts_only_live_processes(monkeypatch):
    from api import terminal

    monkeypatch.setattr(
        terminal,
        "_TERMINALS",
        {
            "live": _Terminal(True),
            "dead": _Terminal(False),
        },
    )

    assert terminal.terminal_activity_snapshot() == {
        "active_terminals": 1,
        "terminal_activity_available": True,
    }
