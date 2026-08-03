"""Real-repository tests for the agent-owned live-main sync helper."""

import subprocess
from pathlib import Path

import pytest

from scripts import sync_live_main


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")


@pytest.fixture
def repos(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _configure(seed)
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")

    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(bare), str(repo))
    _configure(repo)
    return bare, seed, repo


def _forbidden(calls):
    forbidden = {"reset", "restore", "clean", "rebase", "pull"}
    return [args for args in calls if args and (args[0] in forbidden or args[:2] == ("checkout", "."))]


def test_sync_preserves_staged_unstaged_and_untracked_edits(repos, monkeypatch):
    _bare, seed, repo = repos
    (seed / "upstream.txt").write_text("upstream\n")
    _git(seed, "add", "upstream.txt")
    _git(seed, "commit", "-m", "upstream")
    _git(seed, "push", "origin", "main")

    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    (repo / "base.txt").write_text("base plus local\n")
    (repo / "untracked.txt").write_text("untracked\n")

    calls = []
    real_run_git = sync_live_main._run_git

    def logged_run_git(path, args):
        calls.append(tuple(args))
        return real_run_git(path, args)

    monkeypatch.setattr(sync_live_main, "_run_git", logged_run_git)
    result = sync_live_main.sync_live_main(repo)

    assert result.ok is True, result
    assert (repo / "upstream.txt").read_text() == "upstream\n"
    assert (repo / "staged.txt").read_text() == "staged\n"
    assert (repo / "base.txt").read_text() == "base plus local\n"
    assert (repo / "untracked.txt").read_text() == "untracked\n"
    assert result.stash_ref
    assert _git(repo, "stash", "list")
    assert not _forbidden(calls)


def test_sync_merges_diverged_local_and_remote_commits(repos):
    _bare, seed, repo = repos
    (repo / "local.txt").write_text("local\n")
    _git(repo, "add", "local.txt")
    _git(repo, "commit", "-m", "local-only")
    (seed / "remote.txt").write_text("remote\n")
    _git(seed, "add", "remote.txt")
    _git(seed, "commit", "-m", "remote-only")
    _git(seed, "push", "origin", "main")

    result = sync_live_main.sync_live_main(repo)

    assert result.ok is True, result
    assert (repo / "local.txt").read_text() == "local\n"
    assert (repo / "remote.txt").read_text() == "remote\n"
    assert "Merge" in _git(repo, "log", "--format=%s", "-3")


def test_sync_keeps_stash_when_reapplying_local_conflict(repos):
    _bare, seed, repo = repos
    (repo / "base.txt").write_text("local version\n")
    (seed / "base.txt").write_text("remote version\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-m", "remote edit")
    _git(seed, "push", "origin", "main")

    result = sync_live_main.sync_live_main(repo)

    assert result.ok is False
    assert result.conflict is True
    assert result.stash_ref
    assert _git(repo, "stash", "list")
    assert "UU base.txt" in _git(repo, "status", "--short")


def test_sync_rejects_non_main_branch_without_touching_worktree(repos):
    _bare, _seed, repo = repos
    _git(repo, "branch", "feature")
    _git(repo, "switch", "feature")
    before = _git(repo, "rev-parse", "HEAD")

    result = sync_live_main.sync_live_main(repo)

    assert result.ok is False
    assert "main" in result.message
    assert _git(repo, "rev-parse", "HEAD") == before
