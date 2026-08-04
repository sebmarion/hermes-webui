"""Behavioral tests for the single-user live-main release command."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

from scripts import release_live_main


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "app.txt").write_text("base\n")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _config(repo: Path, receipt: Path) -> release_live_main.ReleaseConfig:
    return release_live_main.ReleaseConfig(
        repo=repo,
        upstream_ref="origin/main",
        remote="origin",
        launchd_label="com.example.hermes-webui",
        base_url="http://127.0.0.1:8787",
        receipt_path=receipt,
        test_paths=("tests/test_sync_live_main.py",),
        sync=False,
        startup_timeout=0,
    )


def test_release_runs_gates_restarts_and_writes_receipt(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    config = _config(repo, receipt)
    config = replace(config, sync=True)
    commands = []
    sync_calls = []

    def fake_command(command, *, cwd):
        commands.append((tuple(command), cwd))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_live_main, "_run_command", fake_command)
    monkeypatch.setattr(
        release_live_main,
        "_probe_live",
        lambda _config: {
            "health": {"http_status": 200, "status": "ok"},
            "smoke": {"status": 200},
        },
    )

    def fake_sync(repo_path, upstream_ref, *, remote):
        sync_calls.append((repo_path, upstream_ref, remote))
        return release_live_main.sync_live_main.SyncResult(True, "ok")

    assert release_live_main.perform_release(config, sync_function=fake_sync) == 0

    assert [command[0][0] for command in commands] == ["./scripts/test.sh", "launchctl"]
    assert sync_calls == [(repo, "origin/main", "origin")]
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "passed"
    assert payload["before_commit"] == payload["after_commit"]
    assert payload["health"]["health"]["status"] == "ok"


def test_failed_test_gate_does_not_restart(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    config = _config(repo, receipt)
    commands = []

    def fake_command(command, *, cwd):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, "", "test failed")

    monkeypatch.setattr(release_live_main, "_run_command", fake_command)

    assert release_live_main.perform_release(config) == 1
    assert commands == [("./scripts/test.sh", "tests/test_sync_live_main.py", "-q")]
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "failed"
    assert payload["stage"] == "tests"


def test_dirty_worktree_stops_before_sync_or_restart(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "app.txt").write_text("local work\n")
    receipt = tmp_path / "receipt.json"
    config = _config(repo, receipt)
    commands = []
    sync_calls = []

    monkeypatch.setattr(
        release_live_main,
        "_run_command",
        lambda command, *, cwd: commands.append(tuple(command))
        or subprocess.CompletedProcess(command, 0, "", ""),
    )

    def fake_sync(*_args, **_kwargs):
        sync_calls.append(True)
        return release_live_main.sync_live_main.SyncResult(True, "ok")

    assert release_live_main.perform_release(config, sync_function=fake_sync) == 1
    assert sync_calls == []
    assert commands == []
    payload = json.loads(receipt.read_text())
    assert payload["stage"] == "preflight"


def test_unhealthy_service_fails_after_restart(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    config = _config(repo, receipt)
    commands = []

    def fake_command(command, *, cwd):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_live_main, "_run_command", fake_command)
    monkeypatch.setattr(
        release_live_main,
        "_probe_live",
        lambda _config: {"health": {"status": "degraded"}, "smoke": {"status": 503}},
    )

    assert release_live_main.perform_release(config) == 1
    assert commands[-1][0] == "launchctl"
    payload = json.loads(receipt.read_text())
    assert payload["status"] == "failed"
    assert payload["stage"] == "health"


def test_status_is_read_only_and_reports_receipt(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": "passed", "after_commit": "abc123"}))
    config = _config(repo, receipt)
    monkeypatch.setattr(
        release_live_main,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("status ran a command")),
    )

    result = release_live_main.status_snapshot(config)

    assert result["head"] == _git(repo, "rev-parse", "HEAD")
    assert result["worktree_clean"] is True
    assert result["last_receipt"]["after_commit"] == "abc123"


def test_rollback_refuses_dirty_worktree(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("uncommitted\n")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps({"status": "passed", "before_commit": before, "after_commit": before})
    )
    config = _config(repo, receipt)
    monkeypatch.setattr(
        release_live_main,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dirty rollback ran a command")),
    )

    assert release_live_main.perform_rollback(config) == 1
    assert (repo / "app.txt").read_text() == "uncommitted\n"


def test_rollback_refuses_when_head_moved_after_release(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("released\n")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "release")
    after = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later")
    moved = _git(repo, "rev-parse", "HEAD")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "command": "release",
                "status": "passed",
                "before_commit": before,
                "after_commit": after,
            }
        )
    )
    config = _config(repo, receipt)
    monkeypatch.setattr(
        release_live_main,
        "_run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("moved rollback ran a command")),
    )

    assert release_live_main.perform_rollback(config) == 1
    assert _git(repo, "rev-parse", "HEAD") == moved


def test_rollback_reverts_last_release_then_runs_gates(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("released\n")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "release")
    after = _git(repo, "rev-parse", "HEAD")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "command": "release",
                "release_id": "release-1",
                "status": "passed",
                "before_commit": before,
                "after_commit": after,
            }
        )
    )
    config = _config(repo, receipt)
    commands = []

    def fake_command(command, *, cwd):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_live_main, "_run_command", fake_command)
    monkeypatch.setattr(
        release_live_main,
        "_probe_live",
        lambda _config: {
            "health": {"http_status": 200, "status": "ok"},
            "smoke": {"status": 200},
        },
    )

    assert release_live_main.perform_rollback(config) == 0
    assert (repo / "app.txt").read_text() == "base\n"
    assert _git(repo, "rev-parse", "HEAD") != after
    assert [command[0] for command in commands] == ["./scripts/test.sh", "launchctl"]
    payload = json.loads(receipt.read_text())
    assert payload["command"] == "rollback"
    assert payload["status"] == "passed"
