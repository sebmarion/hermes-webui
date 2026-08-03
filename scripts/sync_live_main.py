#!/usr/bin/env python3
"""Merge upstream into the local live ``main`` checkout without clobbering work.

This helper is intentionally agent-owned. It never resets, cleans, restores,
rebases, pulls, or drops the preservation stash. A caller must inspect the
result, resolve any conflicts, run tests, and explicitly clean up the stash
after the live process is healthy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    message: str
    stash_ref: str | None = None
    conflict: bool = False


def _run_git(repo: Path, args: list[str]) -> tuple[str, bool]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return output, completed.returncode == 0


def _git_path(repo: Path, name: str) -> Path | None:
    output, ok = _run_git(repo, ["rev-parse", "--git-path", name])
    if not ok or not output:
        return None
    path = Path(output)
    if not path.is_absolute():
        path = repo / path
    return path


def _failure(message: str, *, stash_ref: str | None = None, conflict: bool = False) -> SyncResult:
    if stash_ref:
        message = f"{message} Preservation stash retained: {stash_ref}."
    return SyncResult(False, message, stash_ref=stash_ref, conflict=conflict)


def sync_live_main(
    repo: str | Path,
    upstream_ref: str = "origin/main",
    *,
    remote: str = "origin",
) -> SyncResult:
    """Fetch and merge ``upstream_ref`` into ``repo`` while preserving edits."""
    path = Path(repo).expanduser().resolve()
    if not (path / ".git").exists():
        return _failure(f"Not a Git checkout: {path}")

    branch, branch_ok = _run_git(path, ["symbolic-ref", "--short", "HEAD"])
    if not branch_ok or branch != "main":
        return _failure(f"Refusing to sync {path}: live checkout must be on main (found {branch or 'detached'}).")

    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD"):
        marker = _git_path(path, operation)
        if marker is not None and marker.exists():
            return _failure(f"Refusing to sync: an in-progress {operation.lower()} operation exists at {marker}.")

    fetched, fetch_ok = _run_git(path, ["fetch", "--prune", remote])
    if not fetch_ok:
        return _failure(f"Fetch failed: {fetched or 'git returned no diagnostic.'}")

    status, status_ok = _run_git(path, ["status", "--porcelain", "--untracked-files=all"])
    if not status_ok:
        return _failure(f"Could not inspect the live worktree: {status or 'git returned no diagnostic.'}")

    stash_ref: str | None = None
    if status:
        stash_name = f"hermes-live-main-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        stashed, stash_ok = _run_git(
            path,
            ["stash", "push", "--include-untracked", "--message", stash_name],
        )
        if not stash_ok:
            return _failure(f"Could not preserve local work before merge: {stashed or 'git returned no diagnostic.'}")
        stash_line, stash_list_ok = _run_git(path, ["stash", "list", "-1", "--format=%H"])
        if not stash_list_ok or not stash_line:
            return _failure("Local work was stashed but its recovery reference could not be read.")
        stash_ref = stash_line.splitlines()[0].strip()

    merged, merge_ok = _run_git(path, ["merge", "--no-edit", upstream_ref])
    if not merge_ok:
        return _failure(
            f"Merge failed; repair the merge in place. {merged or 'git returned no diagnostic.'}",
            stash_ref=stash_ref,
            conflict=True,
        )

    if stash_ref:
        reapplied, apply_ok = _run_git(path, ["stash", "apply", "--index", stash_ref])
        if not apply_ok:
            return _failure(
                f"Local work conflicted while being reapplied; resolve it in place. {reapplied or 'git returned no diagnostic.'}",
                stash_ref=stash_ref,
                conflict=True,
            )

    return SyncResult(
        True,
        "Upstream merged into main and local work was reapplied. Run tests and health checks before dropping the preservation stash.",
        stash_ref=stash_ref,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--upstream-ref", default="origin/main")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    result = sync_live_main(args.repo, args.upstream_ref, remote=args.remote)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
