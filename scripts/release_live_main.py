#!/usr/bin/env python3
"""Small release and rollback command for the single-user live WebUI checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Direct `python3 scripts/release_live_main.py ...` execution starts with the
# scripts directory on sys.path, not the checkout root.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import sync_live_main


DEFAULT_TEST_PATHS = ("tests/test_release_live_main.py", "tests/test_sync_live_main.py")
DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_RECEIPT = Path.home() / ".hermes" / "webui" / "minimal-release.json"


class ReleaseFailure(RuntimeError):
    """A user-facing failure with the release stage that failed."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class ReleaseConfig:
    repo: Path
    upstream_ref: str
    remote: str
    launchd_label: str | None
    base_url: str
    receipt_path: Path
    test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS
    sync: bool = True
    startup_timeout: float = 30.0


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_text(repo: Path, args: list[str]) -> str:
    result = _run_git(repo, args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseFailure("git", detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_status(repo: Path) -> str:
    return _git_text(repo, ["status", "--porcelain", "--untracked-files=all"])


def _require_main(repo: Path) -> None:
    branch = _git_text(repo, ["symbolic-ref", "--short", "HEAD"])
    if branch != "main":
        raise ReleaseFailure("preflight", f"live checkout must be on main (found {branch or 'detached'})")


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReleaseFailure("receipt", f"refusing to write through symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _read_receipt(path: Path) -> dict[str, Any] | None:
    path = path.expanduser()
    if not path.exists():
        return None
    if path.is_symlink():
        raise ReleaseFailure("receipt", f"refusing to read through symlink: {path}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("receipt", f"could not read receipt {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseFailure("receipt", f"receipt is not a JSON object: {path}")
    return payload


def _run_tests(config: ReleaseConfig) -> dict[str, Any]:
    command = ["./scripts/test.sh", *config.test_paths, "-q"]
    result = _run_command(command, cwd=config.repo)
    if result.returncode != 0:
        raise ReleaseFailure("tests", f"test gate failed with exit code {result.returncode}")
    return {"command": command, "returncode": result.returncode}


def _kickstart(config: ReleaseConfig) -> dict[str, Any]:
    if not config.launchd_label:
        raise ReleaseFailure("restart", "launchd label is required (--launchd-label or HERMES_WEBUI_LAUNCHD_LABEL)")
    target = f"gui/{os.getuid()}/{config.launchd_label}"
    command = ["launchctl", "kickstart", "-k", target]
    result = _run_command(command, cwd=config.repo)
    if result.returncode != 0:
        raise ReleaseFailure("restart", f"launchd kickstart failed with exit code {result.returncode}")
    return {"command": command, "returncode": result.returncode}


def _http_json(url: str, timeout: float) -> tuple[int, dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "hermes-webui-release/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(1_000_000)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
            return int(response.status), payload
    except HTTPError as exc:
        raise ReleaseFailure("health", f"HTTP {exc.code} from {url}") from exc
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("health", f"health request failed: {exc}") from exc


def _http_status(url: str, timeout: float) -> int:
    request = Request(url, headers={"User-Agent": "hermes-webui-release/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(1)
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)
    except (OSError, URLError) as exc:
        raise ReleaseFailure("health", f"smoke request failed: {exc}") from exc


def _probe_live(config: ReleaseConfig) -> dict[str, Any]:
    base_url = config.base_url.rstrip("/")
    health_status, health_payload = _http_json(f"{base_url}/health", timeout=5.0)
    result: dict[str, Any] = {
        "health": {"http_status": health_status, **health_payload},
        "smoke": None,
    }
    if health_status != 200 or health_payload.get("status") != "ok":
        return result
    result["smoke"] = {"status": _http_status(f"{base_url}/", timeout=5.0)}
    return result


def _wait_for_live(config: ReleaseConfig) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, config.startup_timeout)
    last: dict[str, Any] | None = None
    while True:
        try:
            last = _probe_live(config)
        except ReleaseFailure as exc:
            last = {"error": str(exc)}
        health = last.get("health", {})
        smoke = last.get("smoke") or {}
        if health.get("http_status") == 200 and health.get("status") == "ok":
            smoke_status = smoke.get("status")
            if isinstance(smoke_status, int) and 200 <= smoke_status < 400:
                return last
        if time.monotonic() >= deadline:
            raise ReleaseFailure("health", f"live health/smoke did not pass: {last}")
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _failure_receipt(
    config: ReleaseConfig,
    *,
    command: str,
    release_id: str,
    started_at: str,
    stage: str,
    error: str,
    before_commit: str | None = None,
    after_commit: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "command": command,
        "release_id": release_id,
        "started_at": started_at,
        "finished_at": _timestamp(),
        "status": "failed",
        "stage": stage,
        "error": error,
        "before_commit": before_commit,
        "after_commit": after_commit,
    }
    payload.update(extra)
    _write_receipt(config.receipt_path, payload)
    return payload


def perform_release(
    config: ReleaseConfig,
    *,
    sync_function: Callable[..., sync_live_main.SyncResult] = sync_live_main.sync_live_main,
) -> int:
    release_id = _timestamp()
    started_at = release_id
    before_commit: str | None = None
    after_commit: str | None = None
    sync_result: dict[str, Any] | None = None
    try:
        _require_main(config.repo)
        before_commit = _git_text(config.repo, ["rev-parse", "HEAD"])
        if _git_status(config.repo):
            raise ReleaseFailure(
                "preflight",
                "worktree is dirty; commit or review local work before releasing",
            )
        if config.sync:
            result = sync_function(config.repo, config.upstream_ref, remote=config.remote)
            sync_result = {
                "ok": result.ok,
                "conflict": result.conflict,
                "stash_ref": result.stash_ref,
            }
            if not result.ok:
                raise ReleaseFailure("sync", result.message)
        after_commit = _git_text(config.repo, ["rev-parse", "HEAD"])
        if _git_status(config.repo):
            raise ReleaseFailure(
                "worktree",
                "worktree is dirty after sync; commit or resolve local work before releasing",
            )
        tests = _run_tests(config)
        restart = _kickstart(config)
        health = _wait_for_live(config)
        payload = {
            "schema": 1,
            "command": "release",
            "release_id": release_id,
            "started_at": started_at,
            "finished_at": _timestamp(),
            "status": "passed",
            "stage": "complete",
            "before_commit": before_commit,
            "after_commit": after_commit,
            "sync": sync_result,
            "tests": tests,
            "restart": restart,
            "health": health,
        }
        _write_receipt(config.receipt_path, payload)
        print(f"release passed: {after_commit[:12] if after_commit else 'unknown'}")
        return 0
    except ReleaseFailure as exc:
        try:
            _failure_receipt(
                config,
                command="release",
                release_id=release_id,
                started_at=started_at,
                stage=exc.stage,
                error=str(exc),
                before_commit=before_commit,
                after_commit=after_commit,
                sync=sync_result,
            )
        except ReleaseFailure as receipt_error:
            print(f"release failed at {exc.stage}: {exc}; receipt failed: {receipt_error}", file=sys.stderr)
        else:
            print(f"release failed at {exc.stage}: {exc}", file=sys.stderr)
        return 1


def status_snapshot(config: ReleaseConfig) -> dict[str, Any]:
    _require_main(config.repo)
    return {
        "repo": str(config.repo),
        "branch": _git_text(config.repo, ["symbolic-ref", "--short", "HEAD"]),
        "head": _git_text(config.repo, ["rev-parse", "HEAD"]),
        "worktree_clean": not bool(_git_status(config.repo)),
        "last_receipt": _read_receipt(config.receipt_path),
    }


def _git_action(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run_git(repo, args)


def _rollback_release(repo: Path, before_commit: str, after_commit: str) -> str:
    parents = _run_git(repo, ["rev-list", "--parents", "-n", "1", after_commit])
    parent_ids = parents.stdout.strip().split()
    if len(parent_ids) == 3 and parent_ids[1] == before_commit:
        revert = _git_action(repo, ["revert", "--no-edit", "-m", "1", after_commit])
        if revert.returncode != 0:
            _git_action(repo, ["revert", "--abort"])
            detail = (revert.stderr or revert.stdout).strip()
            raise ReleaseFailure("rollback", detail or "merge revert failed")
        return _git_text(repo, ["rev-parse", "HEAD"])

    merges = _git_text(repo, ["rev-list", "--merges", f"{before_commit}..{after_commit}"])
    if merges:
        raise ReleaseFailure("rollback", "last release contains unsupported nested merges; repair forward")
    commits = _git_text(repo, ["rev-list", f"{before_commit}..{after_commit}"]).splitlines()
    if not commits:
        raise ReleaseFailure("rollback", "last release has no commits to revert")
    revert = _git_action(repo, ["revert", "--no-edit", "--no-commit", *commits])
    if revert.returncode != 0:
        _git_action(repo, ["revert", "--abort"])
        detail = (revert.stderr or revert.stdout).strip()
        raise ReleaseFailure("rollback", detail or "release revert failed")
    commit = _git_action(repo, ["commit", "-m", "rollback: restore previous live WebUI"])
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout).strip()
        raise ReleaseFailure("rollback", detail or "rollback commit failed")
    return _git_text(repo, ["rev-parse", "HEAD"])


def perform_rollback(config: ReleaseConfig) -> int:
    started_at = _timestamp()
    rollback_context: dict[str, Any] = {}
    try:
        receipt = _read_receipt(config.receipt_path)
        if not receipt or receipt.get("status") != "passed" or receipt.get("command") != "release":
            raise ReleaseFailure("preflight", "no passed release receipt is available")
        before_commit = receipt.get("before_commit")
        after_commit = receipt.get("after_commit")
        if not isinstance(before_commit, str) or not isinstance(after_commit, str):
            raise ReleaseFailure("preflight", "release receipt has no usable commit range")
        _require_main(config.repo)
        if _git_status(config.repo):
            raise ReleaseFailure("preflight", "worktree is dirty; refusing rollback")
        current = _git_text(config.repo, ["rev-parse", "HEAD"])
        if current != after_commit:
            raise ReleaseFailure(
                "preflight",
                f"current HEAD {current[:12]} is not the released commit {after_commit[:12]}",
            )
        rollback_commit = _rollback_release(config.repo, before_commit, after_commit)
        rollback_context = {
            "rollback_of": after_commit,
            "restored_commit": before_commit,
            "rollback_commit": rollback_commit,
        }
        tests = _run_tests(config)
        restart = _kickstart(config)
        health = _wait_for_live(config)
        payload = {
            "schema": 1,
            "command": "rollback",
            "release_id": receipt.get("release_id"),
            "started_at": started_at,
            "finished_at": _timestamp(),
            "status": "passed",
            "stage": "complete",
            "rollback_of": after_commit,
            "restored_commit": before_commit,
            "rollback_commit": rollback_commit,
            "tests": tests,
            "restart": restart,
            "health": health,
        }
        _write_receipt(config.receipt_path, payload)
        print(f"rollback passed: {rollback_commit[:12]}")
        return 0
    except ReleaseFailure as exc:
        if rollback_context:
            try:
                _failure_receipt(
                    config,
                    command="rollback",
                    release_id=str(receipt.get("release_id")),
                    started_at=started_at,
                    stage=exc.stage,
                    error=str(exc),
                    **rollback_context,
                )
            except ReleaseFailure:
                pass
        print(f"rollback failed at {exc.stage}: {exc}", file=sys.stderr)
        return 1


def _config_from_args(args: argparse.Namespace) -> ReleaseConfig:
    test_paths = DEFAULT_TEST_PATHS + tuple(args.test_path or ())
    return ReleaseConfig(
        repo=args.repo.expanduser().resolve(),
        upstream_ref=args.upstream_ref,
        remote=args.remote,
        launchd_label=args.launchd_label,
        base_url=args.base_url,
        receipt_path=args.receipt.expanduser(),
        test_paths=test_paths,
        sync=not getattr(args, "no_sync", False),
        startup_timeout=args.startup_timeout,
    )


def _add_common_arguments(parser: argparse.ArgumentParser, *, release: bool) -> None:
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--launchd-label",
        default=os.environ.get("HERMES_WEBUI_LAUNCHD_LABEL"),
        help="LaunchAgent label (or HERMES_WEBUI_LAUNCHD_LABEL)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("HERMES_WEBUI_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=Path(os.environ.get("HERMES_WEBUI_RELEASE_RECEIPT", str(DEFAULT_RECEIPT))),
    )
    parser.add_argument("--test-path", action="append", help="Additional focused test path; repeat as needed")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--upstream-ref", default="origin/main")
    parser.add_argument("--remote", default="origin")
    if release:
        parser.add_argument("--no-sync", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    release_parser = subparsers.add_parser("release", help="sync, test, restart, and probe live main")
    _add_common_arguments(release_parser, release=True)
    status_parser = subparsers.add_parser("status", help="show live main and the last local receipt")
    _add_common_arguments(status_parser, release=False)
    rollback_parser = subparsers.add_parser("rollback", help="revert the last passed release and probe it")
    _add_common_arguments(rollback_parser, release=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    try:
        if args.command == "release":
            return perform_release(config)
        if args.command == "rollback":
            return perform_rollback(config)
        print(json.dumps(status_snapshot(config), indent=2, sort_keys=True))
        return 0
    except ReleaseFailure as exc:
        print(f"{args.command} failed at {exc.stage}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
