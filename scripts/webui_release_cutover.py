#!/usr/bin/env python3
"""Pure cutover helpers for launchd transformation and activity draining."""

from __future__ import annotations

import argparse
import ctypes
import copy
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from types import MappingProxyType
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import webui_release_selector as release_selector
from scripts import webui_release_retention as release_retention
from api.process_identity import process_start_token
from api.state_sync import SESSION_ACTIVITY_TTL_SECONDS


_ALLOWED_INHERITED_LAUNCH_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_WEBUI_PROFILE",
    "HERMES_WEBUI_STATE_DIR",
    "HERMES_WEBUI_HOST",
    "HERMES_WEBUI_PORT",
    "HERMES_WEBUI_DEFAULT_PROVIDER",
    "HERMES_WEBUI_DEFAULT_MODEL",
    "HERMES_WEBUI_TLS_CERT",
    "HERMES_WEBUI_TLS_KEY",
}
_ROUTING_ENV_KEYS = {
    "HERMES_WEBUI_DEFAULT_PROVIDER",
    "HERMES_WEBUI_DEFAULT_MODEL",
    "HERMES_WEBUI_HOST",
    "HERMES_WEBUI_PORT",
}


def _sanitized_managed_environment(
    environment: object,
    *,
    interpreter_path: str,
) -> dict:
    if environment is None:
        environment = {}
    if not isinstance(environment, dict):
        raise ValueError("launchd EnvironmentVariables must be a dictionary")
    sanitized = {
        key: copy.deepcopy(value)
        for key, value in environment.items()
        if key in _ALLOWED_INHERITED_LAUNCH_ENV_KEYS
    }
    sanitized.update(
        {
            "HOME": str(sanitized.get("HOME") or Path.home()),
            "PATH": f"{Path(interpreter_path).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HERMES_WEBUI_AUTO_INSTALL": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return sanitized
from scripts.webui_release_selector import MANIFEST_NAME, sha256_file


class DrainTimeout(RuntimeError):
    """The maintenance window expired before a continuous idle period."""


class DrainIdentityMismatch(RuntimeError):
    """The final fenced process/listener identity no longer matches."""


class _LastGoodWebUIIdentityMismatch(DrainIdentityMismatch):
    """The live or durable old WebUI is not the authorized last-good build."""


class ReleaseBuildError(RuntimeError):
    """The committed source cannot be materialized as an immutable release."""


class BootstrapSplitProvenanceMismatch(ReleaseBuildError):
    """Durable rollback provenance no longer matches its live origin."""


class ListenerAbsent(DrainIdentityMismatch):
    """No process currently owns the probed listener."""


class LaunchdAbsenceTransient(DrainIdentityMismatch):
    """launchd has not finished publishing a post-bootout service state."""


class ListenerProbeAmbiguous(ReleaseBuildError):
    """The listener owner could not be determined unambiguously."""


class InjectedCutoverCrash(RuntimeError):
    """Deterministic durable-transaction crash point used by tests."""


_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_RESERVED_MANIFEST_KEYS = {
    "version",
    "build_id",
    "base_commit",
    "commit",
    "tree",
    "changed_files",
    "files",
    "selector",
    "interpreter",
    "agent_source",
}


def _canonical_launch_file(
    raw_path: str,
    *,
    label: str,
    executable: bool = False,
    allow_leaf_symlink: bool = False,
) -> Path:
    path = Path(raw_path)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise ValueError(f"{label} must be an absolute canonical path")
    if path.is_symlink() and not allow_leaf_symlink:
        raise ValueError(f"{label} must not be a symlink")
    try:
        if path.parent.resolve(strict=True) != path.parent:
            raise ValueError(f"{label} parent must be canonical and symlink-free")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} is missing") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} is not a file")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{label} is not executable")
    return path


def transform_launchd_target(
    plist: dict,
    program_path: str,
    *,
    expected_label: str,
    expected_old_interpreter: str,
    managed_interpreter: str,
    expected_old_target: str,
    selector_state_path: str,
    selector_lock_path: str,
    managed_routing_environment: dict[str, str] | None = None,
) -> dict:
    """Deep-copy a launchd plist and bind its selector control files."""
    if not isinstance(plist, dict):
        raise ValueError("launchd plist must be a dictionary")
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) < 2:
        raise ValueError("launchd ProgramArguments must include interpreter and script")
    if plist.get("Label") != expected_label:
        raise ValueError("launchd label does not match the expected WebUI job")
    if arguments[0] != expected_old_interpreter:
        raise ValueError("launchd interpreter does not match the frozen job")
    if arguments[1] == expected_old_target:
        inherited_arguments = arguments[2:]
    elif (
        len(arguments) >= 3
        and arguments[1] == "-S"
        and arguments[2] == expected_old_target
    ):
        inherited_arguments = []
        index = 3
        managed_options = {
            "--selector-state",
            "--selector-lock",
            "--launchd-label",
        }
        while index < len(arguments):
            argument = arguments[index]
            if argument in managed_options:
                if index + 1 >= len(arguments):
                    raise ValueError("managed launchd control argument is incomplete")
                index += 2
                continue
            inherited_arguments.append(argument)
            index += 1
    else:
        raise ValueError("launchd script target does not match the frozen job")
    selector_options = ("--selector-state", "--selector-lock")
    if any(
        isinstance(argument, str)
        and any(
            argument == option or argument.startswith(f"{option}=")
            for option in selector_options
        )
        for argument in inherited_arguments
    ):
        raise ValueError("launchd job contains an inherited selector control argument")
    _canonical_launch_file(
        expected_old_interpreter,
        label="launchd interpreter",
        executable=True,
        allow_leaf_symlink=True,
    )
    _canonical_launch_file(
        managed_interpreter,
        label="managed sealed interpreter",
        executable=True,
    )
    _canonical_launch_file(expected_old_target, label="old launchd target")
    _canonical_launch_file(
        program_path,
        label="selector launchd target",
        executable=True,
    )
    selector_state = _canonical_launch_file(
        selector_state_path,
        label="selector state",
    )
    selector_lock = _canonical_launch_file(
        selector_lock_path,
        label="selector lock",
    )
    if selector_state.parent != selector_lock.parent:
        raise ValueError("selector state and lock must share a control directory")
    transformed = copy.deepcopy(plist)
    transformed.pop("Program", None)
    transformed["WorkingDirectory"] = str(selector_state.parent)
    transformed["ProgramArguments"] = [
        managed_interpreter,
        "-S",
        program_path,
        "--selector-state",
        str(selector_state),
        "--selector-lock",
        str(selector_lock),
        "--launchd-label",
        expected_label,
        *inherited_arguments,
    ]
    transformed_environment = _sanitized_managed_environment(
        plist.get("EnvironmentVariables", {}),
        interpreter_path=managed_interpreter,
    )
    if managed_routing_environment is not None:
        if (
            not isinstance(managed_routing_environment, dict)
            or set(managed_routing_environment) != _ROUTING_ENV_KEYS
            or any(
                not isinstance(value, str) or not value.strip() or "\x00" in value
                for value in managed_routing_environment.values()
            )
        ):
            raise ValueError("managed routing environment is invalid")
        transformed_environment.update(copy.deepcopy(managed_routing_environment))
    transformed_environment["HERMES_WEBUI_SELECTOR_STATE"] = str(selector_state)
    transformed_environment["HERMES_WEBUI_SELECTOR_LOCK"] = str(selector_lock)
    transformed_environment["HERMES_WEBUI_LAUNCHD_LABEL"] = expected_label
    transformed["EnvironmentVariables"] = transformed_environment
    return transformed


def build_direct_fallback_plist(
    plist: dict,
    *,
    expected_label: str,
    expected_old_interpreter: str,
    expected_old_target: str,
    release_identity: dict,
    selector_generation: int,
    selector_state_path: str,
    selector_lock_path: str,
    startup_transaction_id: str | None = None,
) -> dict:
    """Build a selector-bypass plist with explicit managed fallback identity."""
    if not isinstance(selector_generation, int) or selector_generation < 0:
        raise ValueError("fallback selector generation is invalid")
    if not isinstance(release_identity, dict):
        raise ValueError("fallback release identity is invalid")
    if startup_transaction_id is not None and not _TRANSACTION_ID.fullmatch(
        str(startup_transaction_id)
    ):
        raise ValueError("fallback startup transaction identity is invalid")
    required = {
        "release_path",
        "manifest_sha256",
        "selector_path",
        "interpreter_path",
        "agent_source_path",
        "agent_source_resolved_path",
        "agent_source_commit",
        "agent_source_tree",
        "agent_source_manifest_path",
        "agent_source_manifest_sha256",
        "runtime_python_home_path",
        "runtime_site_packages_path",
        "runtime_manifest_path",
        "runtime_manifest_sha256",
    }
    if not required.issubset(release_identity):
        raise ValueError("fallback release identity is incomplete")
    if plist.get("Label") != expected_label:
        raise ValueError("launchd label does not match the expected WebUI job")
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or len(arguments) < 2:
        raise ValueError("launchd ProgramArguments must include interpreter and script")
    if arguments[:2] == [expected_old_interpreter, expected_old_target]:
        inherited_arguments = arguments[2:]
    elif (
        len(arguments) >= 3
        and arguments[:3]
        == [expected_old_interpreter, "-S", expected_old_target]
    ):
        inherited_arguments = []
        index = 3
        while index < len(arguments):
            if arguments[index] in {
                "--selector-state",
                "--selector-lock",
                "--launchd-label",
            }:
                if index + 1 >= len(arguments):
                    raise ValueError("managed launchd control argument is incomplete")
                index += 2
                continue
            inherited_arguments.append(arguments[index])
            index += 1
    else:
        raise ValueError("launchd job does not match the frozen source shape")
    _canonical_launch_file(
        expected_old_interpreter,
        label="launchd interpreter",
        executable=True,
        allow_leaf_symlink=True,
    )
    _canonical_launch_file(expected_old_target, label="old launchd target")
    release_path = Path(str(release_identity["release_path"]))
    if not release_path.is_absolute() or release_path.resolve(strict=True) != release_path:
        raise ValueError("fallback release path is not canonical")
    # Re-attest the immutable release here instead of trusting caller-supplied
    # identity JSON. Direct fallback deliberately skips *runtime* selector
    # verification so it can still be generated after selector loss, but it
    # verifies the manifest, complete release file set, and interpreter.
    verified = release_selector.verify_release(
        release_path,
        release_root=release_path.parent,
        expected_manifest_sha256=str(release_identity["manifest_sha256"]),
        selector_path=None,
        verify_selector_identity=False,
    )
    for key in (
        "release_path",
        "manifest_sha256",
        "selector_path",
        "interpreter_path",
        "agent_source_path",
        "agent_source_resolved_path",
        "agent_source_commit",
        "agent_source_tree",
        "agent_source_manifest_path",
        "agent_source_manifest_sha256",
        "runtime_python_home_path",
        "runtime_site_packages_path",
        "runtime_manifest_path",
        "runtime_manifest_sha256",
    ):
        if str(release_identity.get(key) or "") != str(verified.get(key) or ""):
            raise ValueError(f"fallback release identity mismatch: {key}")
    release_path = Path(verified["release_path"])
    bootstrap = release_path / "bootstrap.py"
    _canonical_launch_file(str(bootstrap), label="fallback bootstrap")
    selector_path = str(verified["selector_path"])

    transformed = copy.deepcopy(plist)
    transformed.pop("Program", None)
    managed_interpreter = str(verified["interpreter_path"])
    transformed["ProgramArguments"] = [
        managed_interpreter,
        "-S",
        str(bootstrap),
        *inherited_arguments,
    ]
    transformed["WorkingDirectory"] = str(release_path)
    environment = _sanitized_managed_environment(
        transformed.get("EnvironmentVariables"),
        interpreter_path=managed_interpreter,
    )
    environment.update(
        {
            "HERMES_WEBUI_RELEASE_ROOT": str(release_path.parent),
            "HERMES_WEBUI_RELEASE_PATH": str(release_path),
            "HERMES_WEBUI_MANIFEST_SHA256": str(
                release_identity["manifest_sha256"]
            ),
            "HERMES_WEBUI_SELECTOR_GENERATION": str(selector_generation),
            "HERMES_WEBUI_SELECTOR_PATH": selector_path,
            "HERMES_WEBUI_INTERPRETER_PATH": managed_interpreter,
            "HERMES_WEBUI_LAUNCH_MODE": "direct-fallback",
            "HERMES_WEBUI_AGENT_DIR": verified["agent_source_path"],
            "HERMES_WEBUI_AGENT_COMMIT": verified["agent_source_commit"],
            "HERMES_WEBUI_AGENT_TREE": verified["agent_source_tree"],
            "HERMES_WEBUI_AGENT_MANIFEST_PATH": verified[
                "agent_source_manifest_path"
            ],
            "HERMES_WEBUI_AGENT_MANIFEST_SHA256": verified[
                "agent_source_manifest_sha256"
            ],
            "HERMES_WEBUI_SELECTOR_STATE": str(selector_state_path),
            "HERMES_WEBUI_SELECTOR_LOCK": str(selector_lock_path),
            "HERMES_WEBUI_LAUNCHD_LABEL": expected_label,
            "HERMES_WEBUI_RUNTIME_PATH": verified["runtime_path"],
            "HERMES_WEBUI_RUNTIME_PYTHON_HOME": verified[
                "runtime_python_home_path"
            ],
            "HERMES_WEBUI_RUNTIME_SITE_PACKAGES": verified[
                "runtime_site_packages_path"
            ],
            "HERMES_WEBUI_RUNTIME_MANIFEST_PATH": verified[
                "runtime_manifest_path"
            ],
            "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": verified[
                "runtime_manifest_sha256"
            ],
            "HERMES_WEBUI_PYTHON": managed_interpreter,
            "HERMES_WEBUI_SERVER_CWD": str(release_path),
            "PYTHONHOME": verified["runtime_python_home_path"],
            "PYTHONPATH": os.pathsep.join(
                [
                    verified["agent_source_path"],
                    verified["runtime_site_packages_path"],
                ]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if startup_transaction_id is not None:
        environment["HERMES_WEBUI_STARTUP_FENCED"] = "1"
        environment["HERMES_WEBUI_STARTUP_TRANSACTION_ID"] = str(
            startup_transaction_id
        )
        environment.update(
            release_selector.startup_journal_environment(
                Path(verified["release_path"]).parent,
                str(startup_transaction_id),
            )
        )
    transformed["EnvironmentVariables"] = environment
    return transformed


def transform_gateway_launchd_target(
    plist: dict,
    *,
    expected_label: str,
    expected_old_program: str,
    managed_cli_shim: str,
    release_identity: dict,
    managed_routing_environment: dict[str, str],
    release_transaction_id: str,
) -> dict:
    """Bind the gateway to the same sealed Agent/runtime as managed WebUI."""
    if not isinstance(plist, dict) or plist.get("Label") != expected_label:
        raise ValueError("gateway launchd label does not match")
    arguments = plist.get("ProgramArguments")
    legacy_shape = (
        isinstance(arguments, list)
        and len(arguments) >= 5
        and arguments[1:5] == ["-m", "hermes_cli.main", "gateway", "run"]
    )
    managed_shape = (
        isinstance(arguments, list)
        and len(arguments) >= 3
        and arguments[1:3] == ["gateway", "run"]
    )
    if (
        not isinstance(arguments, list)
        or arguments[0] != expected_old_program
        or not (legacy_shape or managed_shape)
        or any(not isinstance(argument, str) for argument in arguments)
    ):
        raise ValueError("gateway launchd argv does not match frozen source shape")
    shim = _canonical_launch_file(
        managed_cli_shim,
        label="managed Hermes CLI shim",
        executable=True,
    )
    required_identity = {
        "build_id",
        "release_path",
        "manifest_sha256",
        "interpreter_path",
        "agent_source_path",
        "agent_source_manifest_sha256",
        "runtime_path",
        "runtime_python_home_path",
        "runtime_site_packages_path",
        "runtime_manifest_sha256",
        "commit",
        "tree",
        "agent_source_commit",
        "agent_source_tree",
        "selector_generation",
    }
    if not isinstance(release_identity, dict) or not required_identity.issubset(
        release_identity
    ):
        raise ValueError("gateway managed release identity is incomplete")
    try:
        selector_generation = int(release_identity["selector_generation"])
    except (TypeError, ValueError) as exc:
        raise ValueError("gateway selector generation is invalid") from exc
    if (
        isinstance(release_identity["selector_generation"], bool)
        or selector_generation <= 0
        or not _TRANSACTION_ID.fullmatch(str(release_transaction_id or ""))
    ):
        raise ValueError("gateway paired transaction identity is invalid")
    pair_id = release_selector.release_pair_id(
        release_identity,
        selector_generation=selector_generation,
        transaction_id=release_transaction_id,
    )
    if (
        set(managed_routing_environment) != _ROUTING_ENV_KEYS
        or any(not str(value).strip() for value in managed_routing_environment.values())
    ):
        raise ValueError("gateway managed routing identity is invalid")
    transformed = copy.deepcopy(plist)
    transformed.pop("Program", None)
    gateway_arguments = arguments[3:] if legacy_shape else arguments[1:]
    transformed["ProgramArguments"] = [str(shim), *gateway_arguments]
    transformed["WorkingDirectory"] = str(release_identity["agent_source_path"])
    environment = _sanitized_managed_environment(
        plist.get("EnvironmentVariables"),
        interpreter_path=str(release_identity["interpreter_path"]),
    )
    environment.update(copy.deepcopy(managed_routing_environment))
    environment.update(
        {
            "PYTHONHOME": str(release_identity["runtime_python_home_path"]),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(release_identity["agent_source_path"]),
                    str(release_identity["runtime_site_packages_path"]),
                ]
            ),
            "HERMES_WEBUI_RELEASE_PATH": str(release_identity["release_path"]),
            "HERMES_WEBUI_MANIFEST_SHA256": str(
                release_identity["manifest_sha256"]
            ),
            "HERMES_WEBUI_AGENT_DIR": str(release_identity["agent_source_path"]),
            "HERMES_WEBUI_AGENT_MANIFEST_SHA256": str(
                release_identity["agent_source_manifest_sha256"]
            ),
            "HERMES_WEBUI_RUNTIME_PATH": str(release_identity["runtime_path"]),
            "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": str(
                release_identity["runtime_manifest_sha256"]
            ),
            "HERMES_WEBUI_LAUNCH_MODE": "managed-gateway",
            "HERMES_AGENT_COMMIT": str(
                release_identity["agent_source_commit"]
            ),
            "HERMES_AGENT_TREE": str(release_identity["agent_source_tree"]),
            "HERMES_AGENT_MANIFEST_SHA256": str(
                release_identity["agent_source_manifest_sha256"]
            ),
            "HERMES_AGENT_SOURCE_PATH": str(
                release_identity["agent_source_path"]
            ),
            "HERMES_RUNTIME_MANIFEST_SHA256": str(
                release_identity["runtime_manifest_sha256"]
            ),
            "HERMES_RUNTIME_PATH": str(release_identity["runtime_path"]),
            "HERMES_RELEASE_PAIR_ID": pair_id,
            "HERMES_WEBUI_BUILD_ID": str(release_identity["build_id"]),
            "HERMES_WEBUI_COMMIT": str(release_identity["commit"]),
            "HERMES_WEBUI_TREE": str(release_identity["tree"]),
            "HERMES_SELECTOR_GENERATION": str(selector_generation),
            "HERMES_RELEASE_TRANSACTION_ID": str(release_transaction_id),
            "HERMES_GATEWAY_LAUNCHD_LABEL": expected_label,
        }
    )
    transformed["EnvironmentVariables"] = environment
    return transformed


def _is_idle(health: dict) -> bool:
    try:
        return int(health.get("active_runs", -1)) == 0 and int(
            health.get("active_streams", -1)
        ) == 0
    except (AttributeError, TypeError, ValueError):
        return False


def wait_for_zero_activity(
    fetch_health: Callable[[], dict],
    *,
    identity_matches: Callable[[dict], bool],
    continuous_seconds: float,
    timeout_seconds: float,
    interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Require a continuous zero-work window, then recheck work and identity."""
    if continuous_seconds < 0 or timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("drain timing values are invalid")
    started_at = monotonic()
    idle_since: float | None = None
    while True:
        now = monotonic()
        if now - started_at > timeout_seconds:
            raise DrainTimeout("maintenance window expired before drain")
        health = fetch_health()
        if _is_idle(health):
            if idle_since is None:
                idle_since = now
            if now - idle_since >= continuous_seconds:
                final_health = fetch_health()
                if not _is_idle(final_health):
                    idle_since = None
                else:
                    if not identity_matches(final_health):
                        raise DrainIdentityMismatch(
                            "process identity changed at the final drain barrier"
                        )
                    return final_health
        else:
            idle_since = None
        sleep(interval_seconds)


def _run_git(repo: Path, *arguments: str, binary: bool = False):
    git_environment = {
        key: value
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if (value := os.environ.get(key)) is not None
    }
    git_environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            env=git_environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseBuildError("git release input could not be resolved") from exc
    return completed.stdout


def _safe_archive_path(raw_name: str) -> PurePosixPath:
    if not raw_name or "\\" in raw_name:
        raise ReleaseBuildError("archive path is invalid")
    relative = PurePosixPath(raw_name.rstrip("/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReleaseBuildError("archive path escapes release root")
    if relative.as_posix() != raw_name.rstrip("/"):
        raise ReleaseBuildError("archive path is not canonical")
    return relative


def _external_identity(
    path: Path | str,
    *,
    label: str,
    expected: dict,
) -> dict:
    configured = Path(path)
    if not configured.is_absolute() or Path(os.path.abspath(configured)) != configured:
        raise ReleaseBuildError(f"{label} path must be absolute")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} path is missing") from exc
    opened = resolved.stat()
    if not stat.S_ISREG(opened.st_mode):
        raise ReleaseBuildError(f"{label} path is not a file")
    if opened.st_uid != os.getuid() or opened.st_mode & 0o022:
        raise ReleaseBuildError(f"{label} ownership or mode is unsafe")
    if not opened.st_mode & 0o111:
        raise ReleaseBuildError(f"{label} path is not executable")
    actual = {
        "path": str(configured),
        "resolved_path": str(resolved),
        "sha256": sha256_file(resolved),
    }
    if not isinstance(expected, dict) or set(expected) != set(actual):
        raise ReleaseBuildError(f"{label} frozen identity receipt is invalid")
    if actual != expected:
        raise ReleaseBuildError(f"{label} does not match frozen identity receipt")
    return actual


def _prepare_release_root(raw_root: Path | str) -> Path:
    root = Path(raw_root)
    if not root.is_absolute() or Path(os.path.abspath(root)) != root:
        raise ReleaseBuildError("release root must be absolute and canonical")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ReleaseBuildError("release root has a symlinked ancestor")
        if current.exists():
            if not current.is_dir():
                raise ReleaseBuildError("release root ancestor is not a directory")
            continue
        current.mkdir(mode=0o755)
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise ReleaseBuildError("release root must be canonical and symlink-free")
    opened = resolved.stat()
    if opened.st_uid != os.getuid() or opened.st_mode & 0o022:
        raise ReleaseBuildError("release root ownership or mode is unsafe")
    return resolved


def _validated_release_metadata(metadata: dict | None, changed_files: list[str]) -> dict:
    if not isinstance(metadata, dict):
        raise ReleaseBuildError("release admission metadata is required")
    extra = copy.deepcopy(metadata)
    if _RESERVED_MANIFEST_KEYS.intersection(extra):
        raise ReleaseBuildError("release metadata overwrites a reserved field")
    required = {"patch_decisions", "test_receipts", "artifact_hashes"}
    if not required.issubset(extra):
        raise ReleaseBuildError("release admission metadata is incomplete")
    decisions = extra.get("patch_decisions")
    if not isinstance(decisions, dict) or set(decisions) != set(changed_files):
        raise ReleaseBuildError("patch decisions must cover every changed file")
    for path, decision in decisions.items():
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "ship"
            or not str(decision.get("rationale") or "").strip()
            or _safe_archive_path(path).as_posix() != path
        ):
            raise ReleaseBuildError("patch decision is invalid")
    receipts = extra.get("test_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise ReleaseBuildError("test receipts are required")
    for receipt in receipts:
        if (
            not isinstance(receipt, dict)
            or not str(receipt.get("name") or "").strip()
            or receipt.get("status") != "passed"
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("receipt_sha256") or ""))
        ):
            raise ReleaseBuildError("test receipt is invalid")
    artifacts = extra.get("artifact_hashes")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ReleaseBuildError("preserved artifact hashes are required")
    if any(
        not str(name).strip() or not re.fullmatch(r"[0-9a-f]{64}", str(value or ""))
        for name, value in artifacts.items()
    ):
        raise ReleaseBuildError("preserved artifact hash is invalid")
    return extra


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_TRANSACTION_PHASE_PREREQUISITES = {
    "bootstrap_rollback_claimed": (),
    "staged": (),
    "plist_installed": ("staged",),
    "old_fenced": ("plist_installed",),
    "pair_checkpoint_fence_intent": ("old_fenced",),
    "pair_checkpoint_fenced": ("pair_checkpoint_fence_intent",),
    "thread_checkpoint_dispatched": ("pair_checkpoint_fenced",),
    "thread_checkpoint_stop_intent": ("thread_checkpoint_dispatched",),
    "thread_checkpoint_closed": ("thread_checkpoint_stop_intent",),
    # Legacy transactions retain the original predecessor. New cutovers
    # enforce the checkpoint predecessor explicitly in the controller below.
    "old_committed": ("old_fenced",),
    "selection_activated": ("old_committed",),
    "old_job_booted_out": ("selection_activated",),
    "old_stopped": ("selection_activated",),
    "candidate_job_bootstrapped": ("old_stopped",),
    "replacement_proved": ("old_stopped",),
    "candidate_fenced_health_proved": ("replacement_proved",),
    "pair_ready": ("candidate_fenced_health_proved",),
    "pair_gate_install_intent": ("pair_ready",),
    "pair_gate_installed": ("pair_gate_install_intent",),
    "pair_commit_intent": ("pair_ready",),
    "promoted": ("pair_commit_intent",),
    "gateway_opened": ("promoted",),
    "candidate_accepted": ("gateway_opened",),
    "accepted_health_proved": ("candidate_accepted",),
    "pair_accepted": ("accepted_health_proved",),
    "pair_gate_release_intent": ("pair_accepted",),
    "pair_released": ("pair_gate_release_intent",),
    "pair_opened": ("pair_released",),
    "last_good_split_attested": ("plist_installed",),
    "gateway_last_good_attested": ("last_good_split_attested",),
    "watchdog_cron_disable_intent": ("gateway_last_good_attested",),
    "watchdog_cron_disabled": ("watchdog_cron_disable_intent",),
    "watchdog_state_reconciled": ("watchdog_cron_disabled",),
    "gateway_drain_intent": (
        "gateway_last_good_attested",
        "watchdog_state_reconciled",
    ),
    "gateway_drained": ("gateway_drain_intent",),
    "gateway_stop_intent": ("gateway_drained",),
    "gateway_gracefully_stopped": ("gateway_stop_intent",),
    "gateway_dispatcher_lock_acquired": ("gateway_gracefully_stopped",),
    "gateway_workers_quiescent": ("gateway_dispatcher_lock_acquired",),
    "paired_state_snapshot_created": ("gateway_workers_quiescent",),
    "gateway_dispatcher_lock_released": ("paired_state_snapshot_created",),
    "candidate_gateway_start_intent": (
        "gateway_dispatcher_lock_released",
    ),
    "candidate_gateway_accepted": ("candidate_gateway_start_intent",),
    "rollback_started": (),
    "state_rolled_back": ("rollback_started",),
    "plist_restored": ("state_rolled_back",),
    "failed_candidate_stopped": ("plist_restored",),
    "state_snapshot_restored": ("failed_candidate_stopped",),
    "rollback_gateway_stop_intent": ("rollback_started",),
    "rollback_gateway_gracefully_stopped": ("rollback_gateway_stop_intent",),
    "rollback_gateway_dispatcher_lock_acquired": (
        "rollback_gateway_gracefully_stopped",
    ),
    "rollback_gateway_workers_quiescent": (
        "rollback_gateway_dispatcher_lock_acquired",
    ),
    "rollback_gateway_plist_restored": (
        "rollback_gateway_workers_quiescent",
        "state_snapshot_restored",
    ),
    "rollback_gateway_drain_cleared": ("rollback_gateway_plist_restored",),
    "rollback_gateway_dispatcher_lock_released": (
        "rollback_gateway_drain_cleared",
    ),
    "last_good_restarted": ("state_snapshot_restored",),
    "rollback_verified": ("last_good_restarted",),
    "watchdog_cron_restore_intent": ("pair_opened",),
    "watchdog_cron_restored": ("watchdog_cron_restore_intent",),
    "watchdog_cron_rollback_restored": ("rollback_verified",),
}
_SENSITIVE_JOURNAL_KEYS = {
    "fence_token",
    "authorization",
    "cookie",
    "set-cookie",
    "x-hermes-release-fence",
}


def _transaction_journal_paths(path: Path | str) -> tuple[Path, Path]:
    journal_path = Path(path)
    if (
        not journal_path.is_absolute()
        or Path(os.path.abspath(journal_path)) != journal_path
        or journal_path.is_symlink()
    ):
        raise ReleaseBuildError("transaction journal path is invalid")
    parent = _prepare_release_root(journal_path.parent)
    if journal_path.parent != parent:
        raise ReleaseBuildError("transaction journal parent is not canonical")
    lock_path = journal_path.with_suffix(journal_path.suffix + ".lock")
    if lock_path.is_symlink():
        raise ReleaseBuildError("transaction journal lock must not be a symlink")
    return journal_path, lock_path


def _with_transaction_journal_lock(lock_path: Path):
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.set_inheritable(descriptor, False)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise ReleaseBuildError("transaction journal lock is unsafe")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _journal_contains_sensitive_value(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in _SENSITIVE_JOURNAL_KEYS:
                return True
            if _journal_contains_sensitive_value(item):
                return True
    elif isinstance(value, list):
        return any(_journal_contains_sensitive_value(item) for item in value)
    return False


def _validated_transaction_journal(raw: object, transaction_id: str) -> dict:
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "version",
            "transaction_id",
            "expected_candidate_identity",
            "rollback_receipt",
            "phases",
        }
        or raw.get("version") != 1
        or raw.get("transaction_id") != transaction_id
        or not isinstance(raw.get("expected_candidate_identity"), dict)
        or not isinstance(raw.get("rollback_receipt"), dict)
        or not isinstance(raw.get("phases"), dict)
    ):
        raise ReleaseBuildError("transaction journal schema is invalid")
    if _journal_contains_sensitive_value(raw):
        raise ReleaseBuildError("transaction journal contains sensitive data")
    rollback_receipt = raw["rollback_receipt"]
    if (
        not str(rollback_receipt.get("build_id") or "").strip()
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(rollback_receipt.get("plist_sha256") or ""),
        )
        or not str(rollback_receipt.get("state_snapshot_id") or "").strip()
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(rollback_receipt.get("state_snapshot_sha256") or ""),
        )
    ):
        raise ReleaseBuildError("transaction rollback receipt is invalid")
    phases: dict[str, dict] = {}
    for phase, receipt in raw["phases"].items():
        if phase not in _TRANSACTION_PHASE_PREREQUISITES or not isinstance(
            receipt, dict
        ):
            raise ReleaseBuildError("transaction journal phase is invalid")
        phases[phase] = copy.deepcopy(receipt)
    for phase in phases:
        if any(
            prerequisite not in phases
            for prerequisite in _TRANSACTION_PHASE_PREREQUISITES[phase]
        ):
            raise ReleaseBuildError("transaction journal phase order is invalid")
    rollback_claim = phases.get("bootstrap_rollback_claimed")
    rollback_plist_mode = rollback_receipt.get("plist_mode")
    if rollback_claim is not None and (
        set(rollback_claim)
        != {
            "schema",
            "bootstrap_transaction_id",
            "split_provenance_sha256",
            "split_evidence_sha256",
            "rollback_receipt",
        }
        or rollback_claim.get("schema")
        != "hermes.bootstrap_rollback_claim.v1"
        or rollback_claim.get("bootstrap_transaction_id") != transaction_id
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(rollback_claim.get("split_provenance_sha256") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(rollback_claim.get("split_evidence_sha256") or ""),
        )
        or rollback_claim.get("rollback_receipt") != rollback_receipt
        or isinstance(rollback_plist_mode, bool)
        or not isinstance(rollback_plist_mode, int)
        or rollback_plist_mode <= 0
        or rollback_plist_mode != stat.S_IMODE(rollback_plist_mode)
        or not str(rollback_receipt.get("cli_link_target") or "").strip()
    ):
        raise ReleaseBuildError(
            "bootstrap rollback claim receipt is invalid"
        )
    if {
        "pair_commit_intent",
        "bootstrap_rollback_claimed",
    } <= set(phases):
        raise ReleaseBuildError(
            "transaction journal has conflicting pair commit and "
            "bootstrap rollback claim phases"
        )
    return {
        "version": 1,
        "transaction_id": transaction_id,
        "expected_candidate_identity": copy.deepcopy(
            raw["expected_candidate_identity"]
        ),
        "rollback_receipt": copy.deepcopy(raw["rollback_receipt"]),
        "phases": phases,
    }


def _read_transaction_journal_unlocked(path: Path, transaction_id: str) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise ReleaseBuildError("transaction journal is unsafe")
            payload = handle.read(4 * 1024 * 1024 + 1)
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError("transaction journal is unreadable") from exc
    if len(payload) > 4 * 1024 * 1024:
        raise ReleaseBuildError("transaction journal is too large")
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("transaction journal JSON is invalid") from exc
    return _validated_transaction_journal(raw, transaction_id)


def _atomic_write_transaction_journal(
    path: Path,
    journal: dict,
    *,
    crash_at: str | None = None,
) -> None:
    payload = (
        json.dumps(journal, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if crash_at == "after_temp_fsync":
            raise InjectedCutoverCrash(crash_at)
        os.replace(temp_name, path)
        replaced = True
        if crash_at == "after_replace":
            raise InjectedCutoverCrash(crash_at)
        _fsync_directory(path.parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def initialize_transaction_journal(
    path: Path | str,
    *,
    transaction_id: str,
    expected_candidate_identity: dict,
    rollback_receipt: dict,
) -> dict:
    if not _TRANSACTION_ID.fullmatch(str(transaction_id or "")):
        raise ReleaseBuildError("transaction journal identity is invalid")
    proposed = _validated_transaction_journal(
        {
            "version": 1,
            "transaction_id": transaction_id,
            "expected_candidate_identity": copy.deepcopy(
                expected_candidate_identity
            ),
            "rollback_receipt": copy.deepcopy(rollback_receipt),
            "phases": {},
        },
        transaction_id,
    )
    journal_path, lock_path = _transaction_journal_paths(path)
    with _with_transaction_journal_lock(lock_path):
        if journal_path.exists():
            current = _read_transaction_journal_unlocked(
                journal_path,
                transaction_id,
            )
            if current != proposed:
                raise ReleaseBuildError(
                    "transaction journal already has another identity"
                )
            return current
        _atomic_write_transaction_journal(journal_path, proposed)
        return proposed


def read_transaction_journal(
    path: Path | str,
    *,
    transaction_id: str,
) -> dict:
    journal_path, lock_path = _transaction_journal_paths(path)
    with _with_transaction_journal_lock(lock_path):
        return _read_transaction_journal_unlocked(journal_path, transaction_id)


def record_transaction_phase(
    path: Path | str,
    *,
    transaction_id: str,
    phase: str,
    receipt: dict,
    crash_at: str | None = None,
    initialize_if_absent: dict | None = None,
) -> dict:
    if phase not in _TRANSACTION_PHASE_PREREQUISITES:
        raise ReleaseBuildError("transaction journal phase is invalid")
    if not isinstance(receipt, dict) or _journal_contains_sensitive_value(receipt):
        raise ReleaseBuildError("transaction journal receipt contains sensitive data")
    proposed = None
    if initialize_if_absent is not None:
        if (
            not isinstance(initialize_if_absent, dict)
            or set(initialize_if_absent)
            != {"expected_candidate_identity", "rollback_receipt"}
        ):
            raise ReleaseBuildError(
                "transaction journal initialization receipt is invalid"
            )
        proposed = _validated_transaction_journal(
            {
                "version": 1,
                "transaction_id": transaction_id,
                "expected_candidate_identity": copy.deepcopy(
                    initialize_if_absent["expected_candidate_identity"]
                ),
                "rollback_receipt": copy.deepcopy(
                    initialize_if_absent["rollback_receipt"]
                ),
                "phases": {},
            },
            transaction_id,
        )
    journal_path, lock_path = _transaction_journal_paths(path)
    with _with_transaction_journal_lock(lock_path):
        if journal_path.exists():
            current = _read_transaction_journal_unlocked(
                journal_path,
                transaction_id,
            )
            if proposed is not None and any(
                current[key] != proposed[key]
                for key in (
                    "expected_candidate_identity",
                    "rollback_receipt",
                )
            ):
                raise ReleaseBuildError(
                    "transaction journal already has another identity"
                )
        elif proposed is not None:
            current = proposed
        else:
            current = _read_transaction_journal_unlocked(
                journal_path,
                transaction_id,
            )
        conflicting_phase = {
            "pair_commit_intent": "bootstrap_rollback_claimed",
            "bootstrap_rollback_claimed": "pair_commit_intent",
        }.get(phase)
        if conflicting_phase in current["phases"]:
            raise ReleaseBuildError(
                f"transaction phase {phase} conflicts with "
                f"{conflicting_phase}"
            )
        existing = current["phases"].get(phase)
        if existing is not None:
            if existing != receipt:
                raise ReleaseBuildError(
                    "transaction phase already has a different receipt"
                )
            return current
        missing = [
            prerequisite
            for prerequisite in _TRANSACTION_PHASE_PREREQUISITES[phase]
            if prerequisite not in current["phases"]
        ]
        if missing:
            raise ReleaseBuildError(
                "transaction phase prerequisites are missing: " + ", ".join(missing)
            )
        current["phases"][phase] = copy.deepcopy(receipt)
        current = _validated_transaction_journal(current, transaction_id)
        _atomic_write_transaction_journal(
            journal_path,
            current,
            crash_at=crash_at,
        )
        return current


def _remove_staging_tree(path: Path) -> None:
    if not path.exists():
        return
    for root, directories, filenames in os.walk(path):
        root_path = Path(root)
        try:
            os.chmod(root_path, 0o700)
        except OSError:
            pass
        for directory in directories:
            try:
                os.chmod(root_path / directory, 0o700)
            except OSError:
                pass
        for filename in filenames:
            try:
                os.chmod(root_path / filename, 0o600)
            except OSError:
                pass
    shutil.rmtree(path)


def _validate_git_product_admission(
    repo: Path,
    *,
    expected_origin_url: str,
    expected_base_commit: str,
    base_commit: str,
    commit: str,
    label: str,
) -> str:
    expected_origin = str(expected_origin_url or "").strip()
    if not expected_origin or "\n" in expected_origin:
        raise ReleaseBuildError(f"{label} expected origin is invalid")
    actual_origin = str(_run_git(repo, "remote", "get-url", "origin")).strip()
    if actual_origin != expected_origin:
        raise ReleaseBuildError(f"{label} origin identity does not match")
    if (
        not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_base_commit or "")
        or base_commit != expected_base_commit
    ):
        raise ReleaseBuildError(f"{label} base identity does not match")
    try:
        _run_git(repo, "merge-base", "--is-ancestor", base_commit, commit)
    except ReleaseBuildError as exc:
        raise ReleaseBuildError(
            f"{label} base is not an ancestor of the selected commit"
        ) from exc
    return actual_origin


def build_immutable_agent_source(
    repo: Path | str,
    ref: str,
    *,
    release_root: Path | str,
    expected_origin_url: str,
    base_ref: str,
    expected_base_commit: str,
    allowed_changed_paths: set[str],
) -> dict:
    """Freeze one clean committed Agent tree into a detached attested snapshot."""
    try:
        source_repo = Path(repo).resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("agent source repository is missing") from exc
    if not source_repo.is_dir():
        raise ReleaseBuildError("agent source repository is invalid")
    reported_root = Path(
        str(_run_git(source_repo, "rev-parse", "--show-toplevel")).strip()
    ).resolve(strict=True)
    if reported_root != source_repo:
        raise ReleaseBuildError("agent source repository identity is invalid")
    dirty = str(_run_git(source_repo, "status", "--porcelain", "--untracked-files=all"))
    if dirty.strip():
        raise ReleaseBuildError("agent source repository is dirty")
    commit = str(_run_git(source_repo, "rev-parse", f"{ref}^{{commit}}")).strip()
    tree = str(_run_git(source_repo, "rev-parse", f"{commit}^{{tree}}")).strip()
    base_commit = str(
        _run_git(source_repo, "rev-parse", f"{base_ref}^{{commit}}")
    ).strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree
    ):
        raise ReleaseBuildError("agent source Git identity is invalid")
    origin_url = _validate_git_product_admission(
        source_repo,
        expected_origin_url=expected_origin_url,
        expected_base_commit=expected_base_commit,
        base_commit=base_commit,
        commit=commit,
        label="agent source repository",
    )
    changed_output = str(
        _run_git(
            source_repo,
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            base_commit,
            commit,
        )
    )
    changed_files = sorted(line for line in changed_output.splitlines() if line)
    normalized_allowed = sorted(
        {_safe_archive_path(path).as_posix() for path in allowed_changed_paths}
    )
    if changed_files != normalized_allowed:
        raise ReleaseBuildError("agent source changed paths do not match admission")

    root = _prepare_release_root(release_root)
    snapshots_root = _prepare_release_root(root / "snapshots")
    manifests_root = _prepare_release_root(root / "manifests")
    archive = _run_git(source_repo, "archive", "--format=tar", commit, binary=True)
    stage_path = Path(tempfile.mkdtemp(prefix=".agent-source.", dir=snapshots_root))
    manifest_temp_path: Path | None = None
    final_path: Path | None = None
    final_manifest_path: Path | None = None
    published_snapshot = False
    published_manifest = False
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                relative = _safe_archive_path(member.name)
                relative_text = relative.as_posix()
                if relative_text in seen:
                    raise ReleaseBuildError(
                        "agent source archive contains a duplicate path"
                    )
                seen.add(relative_text)
                target = stage_path.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                if member.issym() or member.islnk():
                    raise ReleaseBuildError("agent source archive contains a symlink")
                if not member.isfile():
                    raise ReleaseBuildError(
                        "agent source archive contains a non-regular entry"
                    )
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ReleaseBuildError("agent source archive file could not be read")
                with target.open("xb") as destination:
                    shutil.copyfileobj(extracted, destination, length=1024 * 1024)
                    destination.flush()
                    os.fchmod(
                        destination.fileno(),
                        0o555 if member.mode & 0o111 else 0o444,
                    )
                    os.fsync(destination.fileno())

        file_hashes: dict[str, str] = {}
        for path in sorted(stage_path.rglob("*")):
            if path.is_symlink():
                raise ReleaseBuildError("staged agent source contains a symlink")
            if path.is_file():
                file_hashes[path.relative_to(stage_path).as_posix()] = sha256_file(path)
        required_layout = {
            "run_agent.py",
            "agent/__init__.py",
            "hermes_cli/__init__.py",
            "tools/__init__.py",
            "tools/process_registry.py",
        }
        if not required_layout.issubset(file_hashes):
            raise ReleaseBuildError("agent source archive required layout is incomplete")
        manifest = {
            "version": 1,
            "origin_url": origin_url,
            "base_commit": base_commit,
            "commit": commit,
            "tree": tree,
            "changed_files": changed_files,
            "files": file_hashes,
        }
        encoded_manifest = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        manifest_sha256 = hashlib.sha256(encoded_manifest).hexdigest()
        final_path = snapshots_root / manifest_sha256
        final_manifest_path = manifests_root / f"{manifest_sha256}.json"
        if (
            final_path.exists()
            or final_path.is_symlink()
            or final_manifest_path.exists()
            or final_manifest_path.is_symlink()
        ):
            raise ReleaseBuildError("agent source snapshot is already installed")

        manifest_descriptor, manifest_temp_name = tempfile.mkstemp(
            prefix=f".{manifest_sha256}.",
            dir=manifests_root,
        )
        manifest_temp_path = Path(manifest_temp_name)
        with os.fdopen(manifest_descriptor, "wb") as handle:
            handle.write(encoded_manifest)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())

        directories = sorted(
            (path for path in stage_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            os.chmod(directory, 0o555)
            _fsync_directory(directory)
        os.chmod(stage_path, 0o555)
        _fsync_directory(stage_path)
        os.replace(stage_path, final_path)
        published_snapshot = True
        _fsync_directory(snapshots_root)
        os.replace(manifest_temp_path, final_manifest_path)
        published_manifest = True
        _fsync_directory(manifests_root)
        _fsync_directory(root)

        identity = {
            "path": str(final_path.resolve(strict=True)),
            "resolved_path": str(final_path.resolve(strict=True)),
            "commit": commit,
            "tree": tree,
            "manifest_path": str(final_manifest_path.resolve(strict=True)),
            "manifest_sha256": manifest_sha256,
        }
        return release_selector.verify_agent_source(identity)
    except release_selector.SelectorError as exc:
        raise ReleaseBuildError(
            f"built agent source identity could not be verified: {exc}"
        ) from exc
    finally:
        if stage_path.exists():
            _remove_staging_tree(stage_path)
        if manifest_temp_path is not None and manifest_temp_path.exists():
            manifest_temp_path.unlink()
        # A failed paired publish must not leave a caller-addressable half artifact.
        if sys.exc_info()[0] is not None:
            if published_manifest and final_manifest_path is not None:
                final_manifest_path.unlink(missing_ok=True)
            if published_snapshot and final_path is not None:
                _remove_staging_tree(final_path)


def _runtime_copy_ignored(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        lowered = name.lower()
        if (
            name == "__pycache__"
            or lowered.endswith((".pyc", ".pyo"))
            or (
                "hermes" in lowered
                and (
                    lowered.endswith(".pth")
                    or "editable" in lowered
                    or lowered.endswith("_finder.py")
                )
            )
        ):
            ignored.add(name)
    return ignored


def _validate_runtime_source_symlinks(source_root: Path) -> None:
    for root, directories, filenames in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *filenames]:
            candidate = root_path / name
            if not candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(source_root)
            except (OSError, ValueError) as exc:
                raise ReleaseBuildError("runtime source symlink escapes its root") from exc
            if resolved.is_dir() and (
                resolved == root_path or resolved in root_path.parents
            ):
                raise ReleaseBuildError("runtime source symlink creates a cycle")


def _fsync_runtime_file(path: Path | str) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _probe_sealed_runtime(runtime_identity: dict, agent_identity: dict) -> None:
    verified_agent = release_selector.verify_agent_source(agent_identity)
    interpreter = str(runtime_identity["interpreter_path"])
    python_home = str(runtime_identity["python_home_path"])
    site_packages = str(runtime_identity["site_packages_path"])
    agent_path = str(verified_agent["path"])
    probe = f"""
import encodings
import os
import sys
from pathlib import Path

import agent
import hermes_cli
import httpx
import pydantic
import run_agent
import tools
import tools.process_registry
import yaml

expected_interpreter = Path({interpreter!r}).resolve(strict=True)
expected_python_home = Path({python_home!r}).resolve(strict=True)
expected_agent_path = Path({agent_path!r}).resolve(strict=True)
expected_site_packages = Path({site_packages!r}).resolve(strict=True)


def require_exact(actual, expected, label):
    resolved = Path(actual).resolve(strict=True)
    if resolved != expected:
        raise RuntimeError(f"{{label}} escaped sealed runtime: {{resolved}} != {{expected}}")


def require_under(actual, expected_root, label):
    resolved = Path(actual).resolve(strict=True)
    try:
        resolved.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError(
            f"{{label}} escaped sealed root: {{resolved}} is not under {{expected_root}}"
        ) from exc


require_exact(sys.executable, expected_interpreter, "sys.executable")
require_exact(sys.prefix, expected_python_home, "sys.prefix")
require_exact(sys.base_prefix, expected_python_home, "sys.base_prefix")
require_under(os.__file__, expected_python_home, "os.__file__")
require_under(encodings.__file__, expected_python_home, "encodings.__file__")
require_under(run_agent.__file__, expected_agent_path, "run_agent.__file__")
require_under(agent.__file__, expected_agent_path, "agent.__file__")
require_under(hermes_cli.__file__, expected_agent_path, "hermes_cli.__file__")
require_under(tools.__file__, expected_agent_path, "tools.__file__")
require_under(
    tools.process_registry.__file__,
    expected_agent_path,
    "tools.process_registry.__file__",
)
require_under(yaml.__file__, expected_site_packages, "yaml.__file__")
require_under(pydantic.__file__, expected_site_packages, "pydantic.__file__")
require_under(httpx.__file__, expected_site_packages, "httpx.__file__")
"""
    environment = {
        "HOME": str(Path.home()),
        "PATH": f"{Path(interpreter).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONHOME": python_home,
        "PYTHONPATH": os.pathsep.join([verified_agent["path"], site_packages]),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            [interpreter, "-S", "-c", probe],
            cwd=verified_agent["path"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("sealed runtime import probe could not run") from exc
    if completed.returncode != 0:
        raise ReleaseBuildError("sealed runtime import probe failed")


def build_immutable_runtime(
    python_home: Path | str,
    site_packages: Path | str,
    *,
    release_root: Path | str,
    interpreter_relative_path: str,
    agent_source_identity: dict,
) -> dict:
    """Snapshot a complete CPython home and dependency tree into one closure."""
    try:
        source_python_home = Path(python_home).resolve(strict=True)
        source_site_packages = Path(site_packages).resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("runtime source directory is missing") from exc
    for label, source in (
        ("Python home", source_python_home),
        ("site-packages", source_site_packages),
    ):
        opened = source.stat()
        if (
            not source.is_dir()
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o022
        ):
            raise ReleaseBuildError(f"runtime {label} source is unsafe")
        _validate_runtime_source_symlinks(source)
    interpreter_relative = _safe_archive_path(interpreter_relative_path).as_posix()
    verified_agent = release_selector.verify_agent_source(agent_source_identity)
    root = _prepare_release_root(release_root)
    snapshots_root = _prepare_release_root(root / "snapshots")
    manifests_root = _prepare_release_root(root / "manifests")
    stage_path = Path(tempfile.mkdtemp(prefix=".runtime.", dir=snapshots_root))
    manifest_temp_path: Path | None = None
    final_path: Path | None = None
    final_manifest_path: Path | None = None
    published_snapshot = False
    published_manifest = False
    try:
        shutil.copytree(
            source_python_home,
            stage_path / "python-home",
            symlinks=False,
            ignore=_runtime_copy_ignored,
        )
        shutil.copytree(
            source_site_packages,
            stage_path / "site-packages",
            symlinks=False,
            ignore=_runtime_copy_ignored,
        )
        interpreter = stage_path / "python-home" / interpreter_relative
        if not interpreter.is_file() or not interpreter.stat().st_mode & 0o111:
            raise ReleaseBuildError("runtime interpreter layout is invalid")
        file_hashes = {}
        for path in sorted(stage_path.rglob("*")):
            if path.is_symlink():
                raise ReleaseBuildError("sealed runtime contains a symlink")
            if path.is_file():
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ReleaseBuildError("sealed runtime contains a non-regular file")
                file_hashes[path.relative_to(stage_path).as_posix()] = sha256_file(path)
            elif not path.is_dir():
                raise ReleaseBuildError("sealed runtime contains a non-regular entry")
        manifest = {
            "version": 1,
            "interpreter_relative_path": (
                PurePosixPath("python-home") / interpreter_relative
            ).as_posix(),
            "site_packages_relative_path": "site-packages",
            "files": file_hashes,
        }
        encoded_manifest = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        manifest_sha256 = hashlib.sha256(encoded_manifest).hexdigest()
        final_path = snapshots_root / manifest_sha256
        final_manifest_path = manifests_root / f"{manifest_sha256}.json"
        if final_path.exists() or final_manifest_path.exists():
            raise ReleaseBuildError("sealed runtime snapshot is already installed")
        for file_path in (path for path in stage_path.rglob("*") if path.is_file()):
            os.chmod(file_path, 0o555 if file_path.stat().st_mode & 0o111 else 0o444)
            _fsync_runtime_file(file_path)
        for directory in sorted(
            (path for path in stage_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o555)
            _fsync_directory(directory)
        os.chmod(stage_path, 0o555)
        _fsync_directory(stage_path)
        probe_identity = {
            "path": str(stage_path),
            "python_home_path": str(stage_path / "python-home"),
            "site_packages_path": str(stage_path / "site-packages"),
            "interpreter_path": str(interpreter),
        }
        _probe_sealed_runtime(probe_identity, verified_agent)

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{manifest_sha256}.", dir=manifests_root
        )
        manifest_temp_path = Path(temp_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded_manifest)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        os.replace(stage_path, final_path)
        published_snapshot = True
        _fsync_directory(snapshots_root)
        os.replace(manifest_temp_path, final_manifest_path)
        published_manifest = True
        _fsync_directory(manifests_root)
        identity = {
            "path": str(final_path),
            "resolved_path": str(final_path.resolve(strict=True)),
            "python_home_path": str(final_path / "python-home"),
            "site_packages_path": str(final_path / "site-packages"),
            "interpreter_path": str(
                final_path / "python-home" / interpreter_relative
            ),
            "interpreter_resolved_path": str(
                (final_path / "python-home" / interpreter_relative).resolve(strict=True)
            ),
            "manifest_path": str(final_manifest_path),
            "manifest_sha256": manifest_sha256,
        }
        return release_selector.verify_runtime(identity)
    finally:
        if stage_path.exists():
            _remove_staging_tree(stage_path)
        if manifest_temp_path is not None and manifest_temp_path.exists():
            manifest_temp_path.unlink()
        if sys.exc_info()[0] is not None:
            if published_manifest and final_manifest_path is not None:
                final_manifest_path.unlink(missing_ok=True)
            if published_snapshot and final_path is not None:
                _remove_staging_tree(final_path)


def build_immutable_release(
    repo: Path | str,
    ref: str,
    *,
    release_root: Path | str,
    build_id: str,
    base_ref: str,
    allowed_changed_paths: set[str],
    selector_path: Path | str,
    interpreter_path: Path | str,
    expected_selector_identity: dict,
    expected_interpreter_identity: dict,
    runtime_identity: dict,
    agent_source_identity: dict,
    expected_origin_url: str,
    expected_base_commit: str,
    metadata: dict | None = None,
) -> dict:
    """Build one complete read-only release from a clean committed Git tree."""
    source_repo = Path(repo).resolve(strict=True)
    if not source_repo.is_dir():
        raise ReleaseBuildError("release source repository is invalid")
    reported_root = Path(
        str(_run_git(source_repo, "rev-parse", "--show-toplevel")).strip()
    ).resolve(strict=True)
    if reported_root != source_repo:
        raise ReleaseBuildError("release source repository identity is invalid")
    if not _BUILD_ID.fullmatch(build_id):
        raise ReleaseBuildError("release build id is invalid")
    dirty = str(_run_git(source_repo, "status", "--porcelain", "--untracked-files=all"))
    if dirty.strip():
        raise ReleaseBuildError("release source repository is dirty")
    commit = str(_run_git(source_repo, "rev-parse", f"{ref}^{{commit}}")).strip()
    tree = str(_run_git(source_repo, "rev-parse", f"{commit}^{{tree}}")).strip()
    base_commit = str(_run_git(source_repo, "rev-parse", f"{base_ref}^{{commit}}")).strip()
    origin_url = _validate_git_product_admission(
        source_repo,
        expected_origin_url=expected_origin_url,
        expected_base_commit=expected_base_commit,
        base_commit=base_commit,
        commit=commit,
        label="WebUI source repository",
    )
    changed_output = str(
        _run_git(
            source_repo,
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            base_commit,
            commit,
        )
    )
    changed_files = sorted(line for line in changed_output.splitlines() if line)
    normalized_allowed = {_safe_archive_path(path).as_posix() for path in allowed_changed_paths}
    if set(changed_files) != normalized_allowed:
        raise ReleaseBuildError("release changed paths do not match admission")

    root = _prepare_release_root(release_root)
    final_path = root / build_id
    if final_path.exists() or final_path.is_symlink():
        raise ReleaseBuildError("release build id is already installed")

    archive = _run_git(source_repo, "archive", "--format=tar", commit, binary=True)
    stage_path = Path(tempfile.mkdtemp(prefix=f".{build_id}.", dir=root))
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar:
                relative = _safe_archive_path(member.name)
                relative_text = relative.as_posix()
                if relative_text in seen:
                    raise ReleaseBuildError("archive contains a duplicate path")
                seen.add(relative_text)
                target = stage_path.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                if member.issym() or member.islnk():
                    raise ReleaseBuildError("archive contains a symlink")
                if not member.isfile():
                    raise ReleaseBuildError("archive contains a non-regular entry")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ReleaseBuildError("archive file could not be read")
                with target.open("xb") as destination:
                    shutil.copyfileobj(extracted, destination, length=1024 * 1024)
                    destination.flush()
                    os.fchmod(
                        destination.fileno(),
                        0o555 if member.mode & 0o111 else 0o444,
                    )
                    os.fsync(destination.fileno())

        file_hashes: dict[str, str] = {}
        for path in sorted(stage_path.rglob("*")):
            if path.is_symlink():
                raise ReleaseBuildError("staged release contains a symlink")
            if path.is_file():
                file_hashes[path.relative_to(stage_path).as_posix()] = sha256_file(path)
        if "bootstrap.py" not in file_hashes:
            raise ReleaseBuildError("release archive has no bootstrap.py")

        extra_metadata = _validated_release_metadata(metadata, changed_files)
        try:
            verified_agent_source = release_selector.verify_agent_source(
                agent_source_identity
            )
        except release_selector.SelectorError as exc:
            raise ReleaseBuildError(
                f"agent source identity could not be verified: {exc}"
            ) from exc
        try:
            verified_runtime = release_selector.verify_runtime(runtime_identity)
        except release_selector.SelectorError as exc:
            raise ReleaseBuildError(
                f"sealed runtime identity could not be verified: {exc}"
            ) from exc
        if str(Path(interpreter_path)) != verified_runtime["interpreter_path"]:
            raise ReleaseBuildError("interpreter is outside the sealed runtime")
        _probe_sealed_runtime(verified_runtime, verified_agent_source)
        manifest = {
            "version": 1,
            "build_id": build_id,
            "origin_url": origin_url,
            "base_commit": base_commit,
            "commit": commit,
            "tree": tree,
            "changed_files": changed_files,
            "files": file_hashes,
            "selector": _external_identity(
                selector_path,
                label="selector",
                expected=expected_selector_identity,
            ),
            "interpreter": _external_identity(
                interpreter_path,
                label="interpreter",
                expected=expected_interpreter_identity,
            ),
            "runtime": verified_runtime,
            "agent_source": verified_agent_source,
            **extra_metadata,
        }
        manifest_path = stage_path / MANIFEST_NAME
        encoded_manifest = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        with manifest_path.open("xb") as handle:
            handle.write(encoded_manifest)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
        manifest_hash = hashlib.sha256(encoded_manifest).hexdigest()

        directories = sorted(
            (path for path in stage_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            os.chmod(directory, 0o555)
            _fsync_directory(directory)
        os.chmod(stage_path, 0o555)
        _fsync_directory(stage_path)
        os.replace(stage_path, final_path)
        _fsync_directory(root)
    except Exception:
        _remove_staging_tree(stage_path)
        raise

    return {
        "build_id": build_id,
        "release_path": str(final_path.resolve()),
        "manifest_sha256": manifest_hash,
        "commit": commit,
        "tree": tree,
        "record": {
            "manifest_sha256": manifest_hash,
            "commit": commit,
            "tree": tree,
        },
    }


def freeze_external_identity(
    path: Path | str,
    *,
    label: str,
    allow_leaf_symlink: bool = False,
) -> dict[str, str]:
    """Capture a strict, non-secret identity receipt for a launch executable."""
    configured = Path(path)
    if not configured.is_absolute() or Path(os.path.abspath(configured)) != configured:
        raise ReleaseBuildError(f"{label} path must be absolute and canonical")
    if configured.is_symlink() and not allow_leaf_symlink:
        raise ReleaseBuildError(f"{label} path must not be a symlink")
    try:
        if configured.parent.resolve(strict=True) != configured.parent:
            raise ReleaseBuildError(f"{label} parent must be canonical")
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} path is missing") from exc
    opened = resolved.stat()
    if not stat.S_ISREG(opened.st_mode):
        raise ReleaseBuildError(f"{label} path is not a file")
    if opened.st_uid != os.getuid() or opened.st_mode & 0o022:
        raise ReleaseBuildError(f"{label} ownership or mode is unsafe")
    if not opened.st_mode & 0o111:
        raise ReleaseBuildError(f"{label} path is not executable")
    return {
        "path": str(configured),
        "resolved_path": str(resolved),
        "sha256": sha256_file(resolved),
    }


def install_external_selector(
    source: Path | str,
    destination: Path | str,
    *,
    expected_source_sha256: str,
) -> dict[str, str]:
    """Atomically install the frozen standalone selector as owner-read-only."""
    source_path = Path(source)
    destination_path = Path(destination)
    if (
        not source_path.is_absolute()
        or Path(os.path.abspath(source_path)) != source_path
        or source_path.is_symlink()
    ):
        raise ReleaseBuildError("selector source path is invalid")
    try:
        if source_path.parent.resolve(strict=True) != source_path.parent:
            raise ReleaseBuildError("selector source parent is not canonical")
        source_stat = source_path.stat()
    except OSError as exc:
        raise ReleaseBuildError("selector source is missing") from exc
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_uid != os.getuid():
        raise ReleaseBuildError("selector source is not a trusted regular file")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256 or ""):
        raise ReleaseBuildError("selector source receipt is invalid")
    if sha256_file(source_path) != expected_source_sha256:
        raise ReleaseBuildError("selector source does not match frozen receipt")
    if (
        not destination_path.is_absolute()
        or Path(os.path.abspath(destination_path)) != destination_path
    ):
        raise ReleaseBuildError("selector destination must be absolute and canonical")
    parent = _prepare_release_root(destination_path.parent)
    if destination_path.is_symlink():
        raise ReleaseBuildError("selector destination must not be a symlink")
    if destination_path.exists():
        existing = destination_path.stat()
        if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
            raise ReleaseBuildError("selector destination is not a trusted regular file")

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        dir=parent,
    )
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as target, source_path.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, target, length=1024 * 1024)
            target.flush()
            os.fchmod(target.fileno(), 0o555)
            os.fsync(target.fileno())
        if sha256_file(Path(temp_name)) != expected_source_sha256:
            raise ReleaseBuildError("installed selector copy hash mismatch")
        os.replace(temp_name, destination_path)
        replaced = True
        _fsync_directory(parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return freeze_external_identity(destination_path, label="selector")


def _validated_loopback_base_url(raw_url: str) -> str:
    parsed = urlsplit(str(raw_url or "").strip())
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
        or parsed.port is None
    ):
        raise ReleaseBuildError("release control base URL is invalid")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ReleaseBuildError("release control base URL must be loopback")
    except ValueError as exc:
        raise ReleaseBuildError("release control host must be a loopback IP") from exc
    return raw_url.rstrip("/")


def _read_release_control_key(path: Path | str) -> bytes:
    key_path = Path(path)
    if (
        not key_path.is_absolute()
        or Path(os.path.abspath(key_path)) != key_path
        or key_path.is_symlink()
    ):
        raise ReleaseBuildError("release control key path is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise ReleaseBuildError("release control key permissions are unsafe")
            raw = handle.read(33)
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError("release control key is unreadable") from exc
    if len(raw) < 32:
        raise ReleaseBuildError("release control key is invalid")
    return raw[:32]


def _http_json(request: Request | str, *, timeout_seconds: float = 5.0) -> dict:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as exc:
        try:
            error_raw = exc.read(4 * 1024 * 1024 + 1)
            error_payload = json.loads(error_raw)
            detail = str(
                error_payload.get("error")
                or error_payload.get("message")
                or error_payload.get("code")
                or ""
            ).strip()
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise ReleaseBuildError(
            f"release control HTTP request was refused with status {exc.code}{suffix}"
        ) from exc
    except (OSError, URLError) as exc:
        raise ReleaseBuildError("release control HTTP request failed") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise ReleaseBuildError("release control response is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("release control response is invalid") from exc
    if not isinstance(payload, dict):
        raise ReleaseBuildError("release control response is not an object")
    return payload


def _release_control_response_signing_bytes(payload: dict) -> bytes:
    return (
        b"hermes-webui-release-control-response-v1\n"
        + json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _verify_release_control_receipt(
    payload: dict,
    *,
    signing_key: bytes,
    transaction_id: str,
    request_nonce: str,
) -> dict:
    if not isinstance(payload, dict):
        raise ReleaseBuildError("release control receipt is invalid")
    receipt = dict(payload)
    signature = str(receipt.pop("attestation", "")).lower()
    if (
        receipt.get("transaction_id") != transaction_id
        or receipt.get("request_nonce") != request_nonce
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        raise ReleaseBuildError("release control receipt binding is invalid")
    expected = hmac.new(
        signing_key,
        _release_control_response_signing_bytes(receipt),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ReleaseBuildError("release control receipt attestation is invalid")
    return payload


def _release_control_client(
    base_url: str,
    signing_key: bytes,
    *,
    transaction_id: str | None = None,
    request_timeout_seconds: float = 30.0,
):
    base = _validated_loopback_base_url(base_url)
    transaction = str(transaction_id or secrets.token_urlsafe(32))
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", transaction):
        raise ReleaseBuildError("release transaction identity is invalid")
    if request_timeout_seconds <= 0:
        raise ReleaseBuildError("release control request timeout is invalid")

    def send_control(
        action: str,
        expected: dict | None,
        fence_token: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        nonce = secrets.token_urlsafe(32)
        body = {
            "action": action,
            "nonce": nonce,
            "transaction_id": transaction,
        }
        if expected is not None:
            body["expected"] = expected
        if extra is not None:
            if not isinstance(extra, dict):
                raise ReleaseBuildError("release control payload is invalid")
            body.update(copy.deepcopy(extra))
        encoded = json.dumps(
            body,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signing_bytes = (
            b"hermes-webui-release-control-v1\n"
            + timestamp.encode("ascii")
            + b"\n"
            + encoded
        )
        signature = hmac.new(signing_key, signing_bytes, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            "X-Hermes-Release-Timestamp": timestamp,
            "X-Hermes-Release-Signature": signature,
        }
        if fence_token:
            headers["X-Hermes-Release-Fence"] = fence_token
        request = Request(
            f"{base}/api/internal/release-control",
            data=encoded,
            headers=headers,
            method="POST",
        )
        action_timeout = (
            max(float(request_timeout_seconds), 120.0)
            if action == "accept"
            else float(request_timeout_seconds)
        )
        return _verify_release_control_receipt(
            _http_json(request, timeout_seconds=action_timeout),
            signing_key=signing_key,
            transaction_id=transaction,
            request_nonce=nonce,
        )

    def inspect_control() -> dict:
        return send_control("inspect", None, None)

    return inspect_control, send_control, transaction


def _pid_start_token(pid: int) -> str | None:
    return process_start_token(pid)


def signal_exact_release_process(identity: dict) -> None:
    try:
        pid = int(identity.get("pid"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DrainIdentityMismatch("release signal PID is invalid") from exc
    expected_start = str(identity.get("pid_start_token") or "").strip()
    if pid <= 1 or not expected_start:
        raise DrainIdentityMismatch("release signal identity is incomplete")
    if _pid_start_token(pid) != expected_start:
        raise DrainIdentityMismatch("release signal identity changed")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise DrainIdentityMismatch("release signal failed") from exc


def wait_for_exact_process_exit(
    identity: dict,
    timeout_seconds: float,
    *,
    allow_exact_signaled_zombie: bool = False,
) -> None:
    pid = int(identity.get("pid"))
    expected_start = str(identity.get("pid_start_token") or "").strip()
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        current = _pid_start_token(pid)
        if current is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except OSError as exc:
                raise DrainIdentityMismatch(
                    "release process existence probe failed"
                ) from exc
            if allow_exact_signaled_zombie:
                try:
                    state = _ps_value(pid, "state")
                except DrainIdentityMismatch:
                    if _pid_start_token(pid) is not None:
                        raise
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    except OSError as exc:
                        raise DrainIdentityMismatch(
                            "release process existence re-probe failed"
                        ) from exc
                    raise
                if state.upper().startswith("Z"):
                    return
                time.sleep(0.1)
                continue
            raise DrainIdentityMismatch(
                "release process start-token probe failed while PID is alive"
            )
        if current != expected_start:
            return
        time.sleep(0.1)
    raise DrainTimeout("committed release process did not exit")


_LEGACY_RELEASE_ACTIVITY_KEYS = {
    "active_streams",
    "active_async_delegations",
    "async_delegations_available",
    "active_background_memory_commits",
    "in_flight_memory_commits",
    "memory_commit_activity_available",
    "pending_oauth_flows",
    "oauth_activity_available",
    "active_terminals",
    "terminal_activity_available",
    "process_completion_activity_available",
}
_NATIVE_PROCESS_ACTIVITY_COUNT_KEYS = {
    "running_processes",
    "foreign_owner_active_processes",
    "finalizing_processes",
    "durable_undelivered_completions",
}
_NATIVE_RELEASE_ACTIVITY_KEYS = {
    *_LEGACY_RELEASE_ACTIVITY_KEYS,
    "process_checkpoint_available",
    "process_checkpoint_reason",
    *_NATIVE_PROCESS_ACTIVITY_COUNT_KEYS,
}
_COMPATIBILITY_GAP_RELEASE_ACTIVITY_KEYS = {
    *_LEGACY_RELEASE_ACTIVITY_KEYS,
    "process_checkpoint_available",
    "process_checkpoint_reason",
}
_R90_PROCESS_COMPATIBILITY_IDENTITY = (
    ("build_id", "hermes-candidate-20260730-r90"),
    ("commit", "fa3e484de3f1e55fa88e3654fe8807be4a272533"),
    ("tree", "551085ae2ba4ce05ef9ae49cdcd34868b61948c0"),
    (
        "manifest_sha256",
        "97b04a96aad665a5fbafaf946aaf6a9192d1da925dd587011aeb9bc55cc0fa7c",
    ),
)
_RELEASE_ACTIVITY_COUNT_KEYS = {
    "active_streams",
    "active_async_delegations",
    "active_background_memory_commits",
    "in_flight_memory_commits",
    "pending_oauth_flows",
    "active_terminals",
}
_RELEASE_ACTIVITY_AVAILABILITY_KEYS = {
    "async_delegations_available",
    "memory_commit_activity_available",
    "oauth_activity_available",
    "terminal_activity_available",
}
_RELEASE_ADMISSION_KEYS = {
    "state",
    "effective_state",
    "pair_gate",
    "generation",
    "fenced_at",
    "lease_expires_at",
    "transaction_id",
    "startup_error",
    "reservations",
    "reservation_kinds",
    "active_runs",
}
_PUBLIC_RELEASE_ADMISSION_KEYS = _RELEASE_ADMISSION_KEYS - {"transaction_id"}
_ABSENT_PAIR_GATE = {
    "status": "absent",
    "transaction_id": None,
    "epoch": None,
    "owner_hash": None,
    "payload_sha256": None,
    "agent": None,
    "webui": None,
}


def _strict_nonnegative_release_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReleaseBuildError("release activity counts are invalid")
    return value


def _classify_release_activity_payload(activity: object) -> str:
    if not isinstance(activity, dict):
        raise DrainIdentityMismatch("release activity schema changed")
    activity_keys = set(activity)
    if activity_keys == _LEGACY_RELEASE_ACTIVITY_KEYS:
        schema = "legacy"
    elif activity_keys == _COMPATIBILITY_GAP_RELEASE_ACTIVITY_KEYS:
        schema = "compatibility-gap"
    elif activity_keys == _NATIVE_RELEASE_ACTIVITY_KEYS:
        schema = "native"
    else:
        raise DrainIdentityMismatch("release activity schema changed")
    for key in _RELEASE_ACTIVITY_COUNT_KEYS:
        _strict_nonnegative_release_count(activity.get(key))
    if any(
        activity.get(key) is not True
        for key in _RELEASE_ACTIVITY_AVAILABILITY_KEYS
    ):
        raise ReleaseBuildError("release activity availability proof is invalid")
    process_available = activity.get("process_completion_activity_available")
    if schema == "legacy":
        if process_available is not False:
            raise ReleaseBuildError(
                "legacy process activity gap is not the known schema mismatch"
            )
    elif schema == "compatibility-gap":
        if (
            process_available is not False
            or activity.get("process_checkpoint_available") is not False
            or activity.get("process_checkpoint_reason") != "unavailable"
        ):
            raise ReleaseBuildError(
                "compatibility process activity gap is invalid"
            )
    else:
        for key in _NATIVE_PROCESS_ACTIVITY_COUNT_KEYS:
            _strict_nonnegative_release_count(activity.get(key))
        if (
            process_available is not True
            or activity.get("process_checkpoint_available") is not True
            or activity.get("process_checkpoint_reason") != "verified"
        ):
            raise ReleaseBuildError(
                "native WebUI process activity proof is invalid"
            )
    return schema


def _classify_release_activity_schema(
    inspection: dict,
) -> tuple[str, dict, dict]:
    admission = inspection.get("admission")
    activity = inspection.get("activity")
    if (
        not isinstance(admission, dict)
        or set(admission) != _RELEASE_ADMISSION_KEYS
        or not isinstance(activity, dict)
    ):
        raise DrainIdentityMismatch("release activity schema changed")
    if (
        admission.get("state") != "fenced"
        or admission.get("effective_state") != "fenced"
        or admission.get("pair_gate") != _ABSENT_PAIR_GATE
        or not isinstance(admission.get("generation"), int)
        or isinstance(admission.get("generation"), bool)
        or admission["generation"] < 0
        or not isinstance(admission.get("fenced_at"), (int, float))
        or isinstance(admission.get("fenced_at"), bool)
        or admission["fenced_at"] <= 0
        or not isinstance(admission.get("lease_expires_at"), (int, float))
        or isinstance(admission.get("lease_expires_at"), bool)
        or admission["lease_expires_at"] <= admission["fenced_at"]
        or not isinstance(admission.get("transaction_id"), str)
        or not _TRANSACTION_ID.fullmatch(admission["transaction_id"])
        or admission.get("startup_error") is not None
        or not isinstance(admission.get("reservation_kinds"), dict)
    ):
        raise ReleaseBuildError("release admission proof is invalid")
    for key in ("active_runs", "reservations"):
        _strict_nonnegative_release_count(admission.get(key))
    reservation_kinds = admission["reservation_kinds"]
    if (
        any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for key, value in reservation_kinds.items()
        )
        or sum(reservation_kinds.values()) != admission["reservations"]
    ):
        raise ReleaseBuildError("release admission proof is invalid")
    schema = _classify_release_activity_payload(activity)
    return schema, admission, activity


def _require_candidate_release_activity_drained(
    public_admission: object,
) -> dict:
    expected_keys = (
        _PUBLIC_RELEASE_ADMISSION_KEYS | _NATIVE_RELEASE_ACTIVITY_KEYS
    )
    if (
        not isinstance(public_admission, dict)
        or set(public_admission) != expected_keys
    ):
        raise DrainIdentityMismatch(
            "candidate release activity schema changed"
        )
    if (
        public_admission.get("state") != "startup-fenced"
        or public_admission.get("effective_state") != "startup-fenced"
        or public_admission.get("pair_gate") != _ABSENT_PAIR_GATE
        or not isinstance(public_admission.get("generation"), int)
        or isinstance(public_admission.get("generation"), bool)
        or public_admission["generation"] < 0
        or not isinstance(public_admission.get("fenced_at"), (int, float))
        or isinstance(public_admission.get("fenced_at"), bool)
        or public_admission["fenced_at"] <= 0
        or public_admission.get("lease_expires_at") is not None
        or public_admission.get("startup_error") is not None
        or not isinstance(public_admission.get("reservation_kinds"), dict)
    ):
        raise ReleaseBuildError(
            "candidate release admission proof is invalid"
        )
    reservation_kinds = public_admission["reservation_kinds"]
    if (
        any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for key, value in reservation_kinds.items()
        )
        or sum(reservation_kinds.values())
        != public_admission.get("reservations")
    ):
        raise ReleaseBuildError(
            "candidate release admission proof is invalid"
        )
    activity = {
        key: public_admission[key]
        for key in _NATIVE_RELEASE_ACTIVITY_KEYS
    }
    if _classify_release_activity_payload(activity) != "native":
        raise ReleaseBuildError(
            "candidate release activity is not native"
        )
    counts = {
        "active_runs": _strict_nonnegative_release_count(
            public_admission.get("active_runs")
        ),
        "reservations": _strict_nonnegative_release_count(
            public_admission.get("reservations")
        ),
        **{
            key: _strict_nonnegative_release_count(activity.get(key))
            for key in (
                *_RELEASE_ACTIVITY_COUNT_KEYS,
                *_NATIVE_PROCESS_ACTIVITY_COUNT_KEYS,
            )
        },
    }
    busy = sorted(key for key, value in counts.items() if value != 0)
    if busy:
        raise ReleaseBuildError(
            "candidate release activity has not drained: "
            + ", ".join(busy)
        )
    return {
        "status": "verified",
        "schema": "native",
        "counts": counts,
        "availability": {
            key: activity[key]
            for key in (
                *_RELEASE_ACTIVITY_AVAILABILITY_KEYS,
                "process_completion_activity_available",
                "process_checkpoint_available",
            )
        },
        "process_checkpoint_reason": activity[
            "process_checkpoint_reason"
        ],
    }


def _release_inspection_is_drained(inspection: dict) -> bool:
    schema, admission, activity = _classify_release_activity_schema(inspection)
    if schema != "native":
        return False
    return all(
        admission[key] == 0 for key in ("active_runs", "reservations")
    ) and all(
        activity[key] == 0
        for key in (
            *_RELEASE_ACTIVITY_COUNT_KEYS,
            *_NATIVE_PROCESS_ACTIVITY_COUNT_KEYS,
        )
    )


def _require_bound_control_receipt(
    receipt: object,
    *,
    status: str,
    transaction_id: str,
    identity: dict | None = None,
) -> dict:
    if (
        not isinstance(receipt, dict)
        or receipt.get("status") != status
        or receipt.get("transaction_id") != transaction_id
    ):
        raise ReleaseBuildError(f"release control {status} receipt is invalid")
    if identity is not None and receipt.get("identity") != identity:
        raise DrainIdentityMismatch(
            f"release control {status} receipt identity changed"
        )
    return receipt


_CANDIDATE_PROCESS_IDENTITY_KEYS = {
    "pid",
    "pid_start_token",
    "started_at",
    "instance_id",
    "cwd",
    "executable",
    "executable_resolved",
    "build_status",
    "build_valid",
    "build_id",
    "commit",
    "tree",
    "manifest_sha256",
    "agent_commit",
    "agent_tree",
    "agent_manifest_sha256",
    "runtime_manifest_sha256",
    "selector_generation",
    "release_path",
    "launch_mode",
    "selector_verified",
    "selector_state_path",
    "selector_lock_path",
    "launchd_label",
    "startup_fenced",
    "startup_transaction_id",
}
_CANDIDATE_RELEASE_TO_PROCESS_IDENTITY_KEYS = {
    "agent_source_commit": "agent_commit",
    "agent_source_tree": "agent_tree",
    "agent_source_manifest_sha256": "agent_manifest_sha256",
}
_CANDIDATE_VERIFIED_RELEASE_ONLY_KEYS = {
    "selector_path",
    "selector_resolved_path",
    "interpreter_path",
    "interpreter_resolved_path",
    "runtime_path",
    "runtime_resolved_path",
    "runtime_python_home_path",
    "runtime_site_packages_path",
    "runtime_manifest_path",
    "agent_source_path",
    "agent_source_resolved_path",
    "agent_source_manifest_path",
}


def _candidate_identity_matches(actual: object, expected: dict) -> bool:
    """Match one full sealed release against its slim signed process identity."""
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    projected: dict[str, object] = {}
    for release_key, value in expected.items():
        process_key = _CANDIDATE_RELEASE_TO_PROCESS_IDENTITY_KEYS.get(
            release_key,
            release_key,
        )
        if release_key in _CANDIDATE_VERIFIED_RELEASE_ONLY_KEYS:
            continue
        if process_key not in _CANDIDATE_PROCESS_IDENTITY_KEYS:
            return False
        prior = projected.get(process_key, value)
        if prior != value:
            return False
        projected[process_key] = value
    return all(actual.get(key) == value for key, value in projected.items())


def _require_expected_last_good_webui_identity(
    actual: object,
    expected: dict,
) -> dict:
    if (
        not isinstance(actual, dict)
        or not expected
        or not _promoted_candidate_identity_matches(actual, expected)
    ):
        raise _LastGoodWebUIIdentityMismatch(
            "release control process is not the exact last-good WebUI"
        )
    return actual


def _promoted_candidate_identity_matches(actual: object, expected: dict) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    ignored = {
        "selector_generation",
        "startup_fenced",
        "startup_transaction_id",
    }
    if "selector_generation" not in expected:
        return _candidate_identity_matches(actual, expected)
    actual_generation = actual.get("selector_generation")
    expected_generation = expected.get("selector_generation")
    actual_pid = actual.get("pid")
    immutable_expected = {
        key: value for key, value in expected.items() if key not in ignored
    }
    return (
        isinstance(actual_generation, int)
        and not isinstance(actual_generation, bool)
        and isinstance(expected_generation, int)
        and not isinstance(expected_generation, bool)
        and actual_generation >= expected_generation > 0
        and isinstance(actual_pid, int)
        and not isinstance(actual_pid, bool)
        and actual_pid > 1
        and isinstance(actual.get("pid_start_token"), str)
        and bool(actual["pid_start_token"])
        and actual.get("startup_fenced") in {None, False}
        and actual.get("startup_transaction_id") in {None, ""}
        and _candidate_identity_matches(actual, immutable_expected)
    )


def _resumable_candidate_identity_matches(
    actual: object,
    expected: dict,
    completed_phases: set[str],
) -> bool:
    if _candidate_identity_matches(actual, expected):
        return True
    return {
        "pair_commit_intent",
        "promoted",
        "gateway_opened",
    }.issubset(completed_phases) and _promoted_candidate_identity_matches(
        actual,
        expected,
    )


def _require_startup_fenced_admission(
    receipt: dict,
    *,
    transaction_id: str,
) -> dict:
    admission = receipt.get("admission")
    if (
        not isinstance(admission, dict)
        or admission.get("state") != "startup-fenced"
        or "lease_expires_at" not in admission
        or admission.get("lease_expires_at") is not None
        or admission.get("transaction_id") != transaction_id
        or "startup_error" not in admission
        or admission.get("startup_error") is not None
    ):
        diagnostic = {
            key: admission.get(key)
            for key in (
                "state",
                "lease_expires_at",
                "transaction_id",
                "startup_error",
            )
        } if isinstance(admission, dict) else {"type": type(admission).__name__}
        raise ReleaseBuildError(
            f"candidate startup fence receipt is invalid: {diagnostic}"
        )
    return admission


def _require_candidate_binding(
    evidence: object,
    *,
    candidate_identity: dict,
    expected_candidate_identity: dict,
    admission_state: str = "startup-fenced",
    require_full_health: bool = False,
    allow_promoted_generation: bool = False,
) -> dict:
    if not isinstance(evidence, dict) or evidence.get("status") != "verified":
        raise DrainIdentityMismatch("candidate process binding is unverified")
    try:
        signed_pid = int(candidate_identity.get("pid"))
        bound_pids = [
            int(evidence.get("launchd_pid")),
            int(evidence.get("listener_pid")),
            int(evidence.get("signed_health_pid")),
        ]
    except (TypeError, ValueError) as exc:
        raise DrainIdentityMismatch("candidate process binding PID is invalid") from exc
    if signed_pid <= 1 or any(pid != signed_pid for pid in bound_pids):
        raise DrainIdentityMismatch(
            "launchd PID, listener PID, and signed health PID do not match"
        )
    expected_start = str(candidate_identity.get("pid_start_token") or "")
    if not expected_start or str(evidence.get("pid_start_token") or "") != expected_start:
        raise DrainIdentityMismatch("candidate process start identity does not match")
    deep_health = evidence.get("deep_health")
    if not isinstance(deep_health, dict) or deep_health.get("status") != "ok":
        raise ReleaseBuildError("candidate deep health is not healthy")
    build = deep_health.get("build")
    if (
        not isinstance(build, dict)
        or build.get("status") != "managed"
        or build.get("valid") is not True
    ):
        raise ReleaseBuildError("candidate deep health build is not managed")
    for key in (
        "build_id",
        "manifest_sha256",
        "agent_manifest_sha256",
        "runtime_manifest_sha256",
        "selector_generation",
    ):
        if key not in expected_candidate_identity:
            continue
        observed_value = build.get(key)
        expected_value = expected_candidate_identity[key]
        if (
            key == "selector_generation"
            and allow_promoted_generation
            and isinstance(observed_value, int)
            and not isinstance(observed_value, bool)
            and isinstance(expected_value, int)
            and not isinstance(expected_value, bool)
            and observed_value >= expected_value > 0
        ):
            continue
        if observed_value != expected_value:
            raise DrainIdentityMismatch(
                f"candidate deep health identity mismatch: {key}"
            )
    admission = deep_health.get("admission")
    if not isinstance(admission, dict) or admission.get("state") != admission_state:
        raise ReleaseBuildError(
            f"candidate deep health admission is not {admission_state}"
        )
    if require_full_health:
        checks = deep_health.get("checks")
        required_checks = (
            "streams_lock",
            "stream_runtime",
            "sessions",
            "projects",
            "state_db",
        )
        if (
            not isinstance(checks, dict)
            or not set(required_checks).issubset(checks)
        ):
            raise ReleaseBuildError("accepted candidate full deep health is incomplete")
        state_checks = {"sessions", "projects", "state_db"}
        expected_state_statuses = (
            {"deferred"}
            if admission_state == "startup-fenced"
            else {"ok", "missing"}
        )
        if admission_state == "startup-fenced":
            startup_fence = checks.get("startup_fence")
            if (
                not isinstance(startup_fence, dict)
                or startup_fence.get("status") != "fenced"
                or startup_fence.get("mutation_free") is not True
            ):
                raise ReleaseBuildError(
                    "startup-fenced candidate health is not mutation-free"
                )
        for name in required_checks:
            check = checks.get(name)
            expected_statuses = (
                expected_state_statuses
                if name in state_checks
                else {"ok"}
            )
            if (
                not isinstance(check, dict)
                or check.get("status") not in expected_statuses
            ):
                raise ReleaseBuildError(
                    f"accepted candidate deep health check failed: {name}"
                )
    return evidence


def _release_checkpoint_boot_id() -> str:
    """Return a host-boot identity suitable for a persisted deadline."""
    if sys.platform == "darwin":
        try:
            raw = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            raw = ""
        if raw:
            return "darwin:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        raw = ""
    if raw:
        return "linux:" + raw
    raise ReleaseBuildError("host boot identity is unavailable")


def run_release_control_cutover(
    *,
    initial_inspection: dict | None = None,
    inspect_control: Callable[[], dict],
    send_control: Callable[[str, dict, str | None], dict],
    attest_selector_state: Callable[[], dict],
    attest_installed_plist: Callable[[], dict],
    activate_selection: Callable[[], object],
    promote_selection: Callable[[], object],
    rollback_selection: Callable[[], object],
    restore_plist: Callable[[], object],
    stop_failed_candidate: Callable[[], object],
    restore_state_snapshot: Callable[[], object],
    restart_selection: Callable[[], object],
    verify_rollback: Callable[[], dict],
    signal_process: Callable[[dict], None],
    wait_for_process_exit: Callable[[dict, float], None],
    inspect_candidate_binding: Callable[[dict], dict],
    inspect_accepted_binding: Callable[[dict], dict],
    expected_candidate_identity: dict,
    expected_last_good_identity: dict,
    transaction_id: str,
    transaction_journal_path: Path | str,
    timeout_seconds: float,
    interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    bootout_process: Callable[[dict], object] | None = None,
    bootstrap_candidate_job: Callable[[], object] | None = None,
    prepare_pair_before_commit: Callable[[dict], dict] | None = None,
    pair_gate_intent_before_commit: Callable[[dict, dict], dict] | None = None,
    install_pair_gate_before_commit: Callable[[dict, dict], dict] | None = None,
    open_pair_after_promotion: Callable[[dict], dict] | None = None,
    release_pair_after_acceptance: Callable[[dict, dict], dict] | None = None,
    attest_legacy_activity_drain: (
        Callable[[dict, dict], dict | None] | None
    ) = None,
    begin_pair_checkpoint: Callable[[dict, dict, str], dict] | None = None,
    dispatch_pair_checkpoint: Callable[[dict, dict, str], dict] | None = None,
    poll_pair_checkpoint: Callable[[dict, dict, str], dict] | None = None,
    close_pair_checkpoint: Callable[[dict, bool, dict, str], dict] | None = None,
    force_restart_on_rollback: bool = False,
) -> dict:
    """Replace and accept one exact startup-fenced WebUI process transaction."""
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("release control timing values are invalid")
    checkpoint_callbacks = (
        begin_pair_checkpoint,
        dispatch_pair_checkpoint,
        poll_pair_checkpoint,
        close_pair_checkpoint,
    )
    if any(callback is not None for callback in checkpoint_callbacks) and not all(
        callback is not None for callback in checkpoint_callbacks
    ):
        raise ValueError("paired checkpoint callbacks must be complete")
    if not _TRANSACTION_ID.fullmatch(str(transaction_id or "")):
        raise ValueError("release control transaction identity is invalid")
    if (
        not isinstance(expected_candidate_identity, dict)
        or not expected_candidate_identity
        or expected_candidate_identity.get("startup_fenced") is not True
        or expected_candidate_identity.get("startup_transaction_id")
        != transaction_id
    ):
        raise ValueError("expected candidate transaction identity is invalid")
    if (
        not isinstance(expected_last_good_identity, dict)
        or not expected_last_good_identity
    ):
        raise ValueError("expected last-good WebUI identity is invalid")
    journal = read_transaction_journal(
        transaction_journal_path,
        transaction_id=transaction_id,
    )
    if journal["expected_candidate_identity"] != expected_candidate_identity:
        raise ReleaseBuildError("transaction journal candidate identity mismatch")
    if not {"staged", "plist_installed"}.issubset(journal["phases"]):
        raise ReleaseBuildError("transaction journal is not ready for cutover")
    rollback_receipt = journal["rollback_receipt"]

    def expected_snapshot_receipt() -> dict:
        current = read_transaction_journal(
            transaction_journal_path,
            transaction_id=transaction_id,
        )
        paired = current["phases"].get("paired_state_snapshot_created")
        receipt = paired if isinstance(paired, dict) else rollback_receipt
        if (
            not str(receipt.get("state_snapshot_id") or "").strip()
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(receipt.get("state_snapshot_sha256") or ""),
            )
        ):
            raise ReleaseBuildError("durable rollback snapshot receipt is invalid")
        return receipt

    completed_phases = set(journal["phases"])

    def record_phase(phase: str, receipt: dict) -> dict:
        if phase in completed_phases:
            return read_transaction_journal(
                transaction_journal_path,
                transaction_id=transaction_id,
            )
        result = record_transaction_phase(
            transaction_journal_path,
            transaction_id=transaction_id,
            phase=phase,
            receipt=receipt,
        )
        completed_phases.add(phase)
        return result

    def refresh_completed_phases() -> dict:
        current = read_transaction_journal(
            transaction_journal_path,
            transaction_id=transaction_id,
        )
        completed_phases.update(current["phases"])
        return current

    def stable_binding_receipt(evidence: dict) -> dict:
        deep_health = evidence.get("deep_health")
        build = deep_health.get("build") if isinstance(deep_health, dict) else {}
        receipt = {
            "launchd_pid": evidence.get("launchd_pid"),
            "listener_pid": evidence.get("listener_pid"),
            "signed_health_pid": evidence.get("signed_health_pid"),
            "pid_start_token": evidence.get("pid_start_token"),
            "health_status": deep_health.get("status")
            if isinstance(deep_health, dict)
            else None,
            "build": {
                key: build.get(key)
                for key in (
                    "status",
                    "valid",
                    "build_id",
                    "manifest_sha256",
                    "agent_manifest_sha256",
                    "runtime_manifest_sha256",
                    "selector_generation",
                )
            },
        }
        runtime = evidence.get("runtime")
        if isinstance(runtime, dict):
            receipt["runtime"] = copy.deepcopy(runtime)
        return receipt

    def require_exact_snapshot_receipt(receipt: object, *, status: str) -> dict:
        expected = expected_snapshot_receipt()
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != status
            or receipt.get("state_snapshot_id") != expected["state_snapshot_id"]
            or receipt.get("state_snapshot_sha256")
            != expected["state_snapshot_sha256"]
        ):
            raise ReleaseBuildError(
                f"rollback {status} snapshot receipt does not match journal"
            )
        return receipt

    started_at = monotonic()
    finished = False
    activated = "selection_activated" in completed_phases
    old_process_exited = "old_stopped" in completed_phases
    candidate_may_have_started = activated
    candidate_may_have_mutated = bool(
        {"candidate_accepted", "candidate_gateway_accepted"}
        & completed_phases
    )
    candidate_identity: dict | None = None
    identity: dict = {}
    token = ""
    selection = journal["phases"].get("selection_activated", {}).get("selection")

    def attest_external_drain(
        old_identity: dict,
        inspection: dict,
    ) -> dict | None:
        if attest_legacy_activity_drain is None:
            return None
        receipt = attest_legacy_activity_drain(old_identity, inspection)
        if receipt is None:
            return None
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "verified"
            or receipt.get("identity") != old_identity
        ):
            raise ReleaseBuildError(
                "external legacy activity drain receipt is invalid"
            )
        return receipt

    def run_thread_checkpoint() -> None:
        """Run the paired checkpoint protocol inside one persisted 300s window."""
        if begin_pair_checkpoint is None:
            return
        current = refresh_completed_phases()
        intent = current["phases"].get("pair_checkpoint_fence_intent")
        if isinstance(intent, dict):
            context = intent.get("context")
            if not isinstance(context, dict):
                raise ReleaseBuildError("checkpoint deadline receipt is invalid")
        else:
            wall_started_at = time.time()
            monotonic_started_at = monotonic()
            context = {
                "transaction_id": transaction_id,
                "wall_started_at": wall_started_at,
                "wall_deadline": wall_started_at + 300.0,
                "monotonic_started_at": monotonic_started_at,
                "monotonic_deadline": monotonic_started_at + 300.0,
                "boot_id": _release_checkpoint_boot_id(),
            }
            begun = begin_pair_checkpoint(context, identity, token)
            if not isinstance(begun, dict):
                raise ReleaseBuildError("paired checkpoint begin receipt is invalid")
            record_phase(
                "pair_checkpoint_fence_intent",
                {"context": context, "begin": begun},
            )
        if "pair_checkpoint_fenced" not in completed_phases:
            record_phase(
                "pair_checkpoint_fenced",
                {"context": context},
            )
        if "thread_checkpoint_dispatched" not in completed_phases:
            dispatched = dispatch_pair_checkpoint(context, identity, token)
            if not isinstance(dispatched, dict):
                raise ReleaseBuildError("paired checkpoint dispatch receipt is invalid")
            record_phase(
                "thread_checkpoint_dispatched",
                {"context": context, "dispatch": dispatched},
            )
        forced = False
        while True:
            status = poll_pair_checkpoint(context, identity, token)
            if not isinstance(status, dict):
                raise ReleaseBuildError("paired checkpoint status receipt is invalid")
            if status.get("complete") is True:
                break
            if (
                time.time() >= float(context["wall_deadline"])
                or monotonic() >= float(context["monotonic_deadline"])
            ):
                forced = True
                break
            sleep(min(interval_seconds, max(
                0.01,
                float(context["monotonic_deadline"]) - monotonic(),
            )))
        if "thread_checkpoint_stop_intent" not in completed_phases:
            record_phase(
                "thread_checkpoint_stop_intent",
                {"context": context, "forced": forced},
            )
        if "thread_checkpoint_closed" not in completed_phases:
            closed = close_pair_checkpoint(context, forced, identity, token)
            if not isinstance(closed, dict):
                raise ReleaseBuildError("paired checkpoint close receipt is invalid")
            record_phase(
                "thread_checkpoint_closed",
                {"context": context, "forced": forced, "close": closed},
            )

    def require_external_state_attestations() -> None:
        selector_attestation = attest_selector_state()
        if (
            not isinstance(selector_attestation, dict)
            or selector_attestation.get("status") != "verified"
            or selector_attestation.get("transaction_id") != transaction_id
        ):
            raise ReleaseBuildError("selector state attestation is invalid")
        candidate_build_id = expected_candidate_identity.get("build_id")
        rollback_build_id = journal["rollback_receipt"].get("build_id")
        if not candidate_build_id or not rollback_build_id:
            raise ReleaseBuildError("transaction build identities are incomplete")
        if "promoted" in completed_phases:
            expected_selector = (candidate_build_id, None, None)
        elif "state_rolled_back" in completed_phases:
            expected_selector = (rollback_build_id, None, None)
        elif "selection_activated" in completed_phases:
            expected_selector = (
                candidate_build_id,
                candidate_build_id,
                transaction_id,
            )
        else:
            expected_selector = (
                rollback_build_id,
                candidate_build_id,
                transaction_id,
            )
        actual_selector = (
            selector_attestation.get("current"),
            selector_attestation.get("candidate"),
            selector_attestation.get("pending_transaction_id"),
        )
        allowed_selectors = {expected_selector}
        if (
            "promoted" not in completed_phases
            and "pair_commit_intent" in completed_phases
            and actual_selector == (candidate_build_id, None, None)
        ):
            allowed_selectors.add((candidate_build_id, None, None))
        if (
            "rollback_started" in completed_phases
            and "state_rolled_back" not in completed_phases
        ):
            allowed_selectors.add((rollback_build_id, None, None))
        if actual_selector not in allowed_selectors:
            raise DrainIdentityMismatch(
                "selector state does not match durable transaction phase"
            )

        plist_attestation = attest_installed_plist()
        if (
            not isinstance(plist_attestation, dict)
            or plist_attestation.get("status") != "verified"
            or plist_attestation.get("launchd_label")
            != expected_candidate_identity.get("launchd_label")
        ):
            raise ReleaseBuildError("installed launchd plist attestation is invalid")
        expected_plist_sha = (
            journal["rollback_receipt"].get("plist_sha256")
            if "plist_restored" in completed_phases
            else journal["phases"]["plist_installed"].get("plist_sha256")
        )
        allowed_plist_hashes = {expected_plist_sha}
        if (
            "rollback_started" in completed_phases
            and "plist_restored" not in completed_phases
        ):
            allowed_plist_hashes.add(journal["rollback_receipt"].get("plist_sha256"))
        if (
            not re.fullmatch(r"[0-9a-f]{64}", str(expected_plist_sha or ""))
            or plist_attestation.get("plist_sha256") not in allowed_plist_hashes
        ):
            raise DrainIdentityMismatch(
                "installed launchd plist does not match durable transaction phase"
            )

    def inspect_with_retry(
        first: dict | None = None,
        *,
        window_started_at: float | None = None,
    ) -> dict:
        deadline_started_at = (
            started_at if window_started_at is None else window_started_at
        )
        candidate = first
        while True:
            if monotonic() - deadline_started_at > timeout_seconds:
                raise DrainTimeout("release replacement did not become ready")
            if candidate is None:
                try:
                    candidate = inspect_control()
                except ReleaseBuildError as exc:
                    if "HTTP request failed" not in str(exc):
                        raise
                    sleep(interval_seconds)
                    continue
            return _require_bound_control_receipt(
                candidate,
                status="inspected",
                transaction_id=transaction_id,
            )

    def finish_candidate(candidate_inspection: dict) -> dict:
        nonlocal candidate_identity, candidate_may_have_mutated, finished
        candidate_identity_value = candidate_inspection.get("identity")
        if not _resumable_candidate_identity_matches(
            candidate_identity_value,
            expected_candidate_identity,
            completed_phases,
        ):
            raise DrainIdentityMismatch(
                "startup-fenced candidate identity does not match release"
            )
        if not isinstance(candidate_identity_value, dict):
            raise DrainIdentityMismatch("candidate identity is invalid")
        candidate_identity = candidate_identity_value
        admission = candidate_inspection.get("admission")
        admission_state = (
            admission.get("state") if isinstance(admission, dict) else None
        )
        binding: dict | None = None
        candidate_token = ""
        if admission_state == "startup-fenced":
            if "candidate_accepted" in completed_phases:
                raise ReleaseBuildError(
                    "candidate admission regressed after an accepted journal phase"
                )
            _require_startup_fenced_admission(
                candidate_inspection,
                transaction_id=transaction_id,
            )
            candidate_fence = _require_bound_control_receipt(
                send_control("fence", candidate_identity, None),
                status="startup-fenced",
                transaction_id=transaction_id,
                identity=candidate_identity,
            )
            candidate_token = str(candidate_fence.get("fence_token") or "")
            if not candidate_token:
                raise ReleaseBuildError("candidate startup fence token is missing")
            _require_startup_fenced_admission(
                candidate_fence,
                transaction_id=transaction_id,
            )
            binding = _require_candidate_binding(
                inspect_candidate_binding(candidate_identity),
                candidate_identity=candidate_identity,
                expected_candidate_identity=expected_candidate_identity,
            )
            record_phase(
                "replacement_proved",
                {
                    "identity": candidate_identity,
                    "binding": stable_binding_receipt(binding),
                },
            )
            fenced_health = _require_candidate_binding(
                inspect_accepted_binding(candidate_identity),
                candidate_identity=candidate_identity,
                expected_candidate_identity=expected_candidate_identity,
                admission_state="startup-fenced",
                require_full_health=True,
            )
            candidate_release_barrier = (
                _require_candidate_release_activity_drained(
                    fenced_health["deep_health"].get("admission")
                )
            )
            record_phase(
                "candidate_fenced_health_proved",
                {
                    "identity": candidate_identity,
                    "binding": stable_binding_receipt(fenced_health),
                    "release_barrier": candidate_release_barrier,
                },
            )
        elif admission_state == "open":
            if not {
                "pair_commit_intent",
                "promoted",
                "gateway_opened",
            }.issubset(completed_phases):
                raise ReleaseBuildError(
                    "open candidate has no durable pair-commit proof"
                )
            candidate_may_have_mutated = True
            record_phase(
                "candidate_accepted",
                {
                    "identity": candidate_identity,
                    "admission": {"state": "open"},
                },
            )
        else:
            raise ReleaseBuildError("candidate admission state is invalid")

        release_resume_phases = {
            "pair_ready",
            "pair_gate_install_intent",
            "pair_gate_installed",
            "pair_commit_intent",
            "promoted",
            "gateway_opened",
            "candidate_accepted",
            "pair_accepted",
            "pair_gate_release_intent",
        }
        resuming_pair_release = (
            release_pair_after_acceptance is not None
            and "pair_gate_release_intent" in completed_phases
        )
        if resuming_pair_release:
            if not release_resume_phases.issubset(completed_phases):
                raise ReleaseBuildError(
                    "paired release intent has incomplete durable history"
                )
            current_pair_journal = refresh_completed_phases()
            pair_ready = current_pair_journal["phases"].get("pair_ready")
            pair_receipt = (
                pair_ready.get("pair")
                if isinstance(pair_ready, dict)
                else None
            )
        else:
            pair_receipt = (
                prepare_pair_before_commit(candidate_identity)
                if prepare_pair_before_commit is not None
                else {"status": "not-required"}
            )
        if not isinstance(pair_receipt, dict):
            raise ReleaseBuildError("paired pre-commit receipt is invalid")
        if "pair_ready" not in completed_phases:
            record_phase("pair_ready", {"pair": pair_receipt})
        if pair_gate_intent_before_commit is not None:
            if install_pair_gate_before_commit is None:
                raise ReleaseBuildError("paired release gate installer is missing")
            if "pair_gate_install_intent" not in completed_phases:
                gate_intent = pair_gate_intent_before_commit(
                    candidate_identity,
                    pair_receipt,
                )
                if not isinstance(gate_intent, dict):
                    raise ReleaseBuildError(
                        "paired release gate intent is invalid"
                    )
                record_phase("pair_gate_install_intent", gate_intent)
            gate_intent = read_transaction_journal(
                transaction_journal_path,
                transaction_id=transaction_id,
            )["phases"]["pair_gate_install_intent"]
            if "pair_gate_installed" not in completed_phases:
                installed_gate = install_pair_gate_before_commit(
                    candidate_identity,
                    gate_intent,
                )
                if not isinstance(installed_gate, dict):
                    raise ReleaseBuildError(
                        "paired release gate install receipt is invalid"
                    )
                record_phase("pair_gate_installed", installed_gate)
        refresh_completed_phases()
        if release_pair_after_acceptance is not None and not {
            "pair_gate_install_intent",
            "pair_gate_installed",
        }.issubset(completed_phases):
            raise ReleaseBuildError(
                "paired release has no durable shared-gate installation"
            )
        if "pair_commit_intent" not in completed_phases:
            record_phase(
                "pair_commit_intent",
                {
                    "build_id": candidate_identity.get("build_id"),
                    "pid": candidate_identity.get("pid"),
                    "pid_start_token": candidate_identity.get(
                        "pid_start_token"
                    ),
                },
            )
        if "promoted" in completed_phases:
            promoted = (
                read_transaction_journal(
                    transaction_journal_path,
                    transaction_id=transaction_id,
                )["phases"]["promoted"].get("promotion")
            )
        else:
            promoted = promote_selection()
            record_phase("promoted", {"promotion": promoted})
        if "gateway_opened" not in completed_phases:
            gateway_open = (
                open_pair_after_promotion(candidate_identity)
                if open_pair_after_promotion is not None
                else {"status": "not-required"}
            )
            if not isinstance(gateway_open, dict):
                raise ReleaseBuildError("paired gateway-open receipt is invalid")
            record_phase("gateway_opened", {"gateway": gateway_open})
        if admission_state == "startup-fenced":
            accepted = _require_bound_control_receipt(
                send_control("accept", candidate_identity, candidate_token),
                status="accepted",
                transaction_id=transaction_id,
                identity=candidate_identity,
            )
            accepted_admission = accepted.get("admission")
            if (
                not isinstance(accepted_admission, dict)
                or accepted_admission.get("state") != "open"
            ):
                raise ReleaseBuildError(
                    "candidate accept receipt did not open admission"
                )
            candidate_may_have_mutated = True
            record_phase(
                "candidate_accepted",
                {
                    "identity": candidate_identity,
                    "admission": {"state": "open"},
                },
            )
        accepted_binding = _require_candidate_binding(
            inspect_accepted_binding(candidate_identity),
            candidate_identity=candidate_identity,
            expected_candidate_identity=expected_candidate_identity,
            admission_state="open",
            require_full_health=True,
            allow_promoted_generation="promoted" in completed_phases,
        )
        record_phase(
            "accepted_health_proved",
            {
                "identity": candidate_identity,
                "binding": stable_binding_receipt(accepted_binding),
            },
        )
        final_inspection = _require_bound_control_receipt(
            inspect_control(),
            status="inspected",
            transaction_id=transaction_id,
            identity=candidate_identity,
        )
        final_admission = final_inspection.get("admission")
        if (
            not isinstance(final_admission, dict)
            or final_admission.get("state") != "open"
        ):
            raise ReleaseBuildError("accepted candidate did not remain open")
        record_phase(
            "pair_accepted",
            {
                "identity": candidate_identity,
                "admission": {"state": "open"},
                "binding": stable_binding_receipt(accepted_binding),
            },
        )
        if release_pair_after_acceptance is not None:
            current_pair_journal = refresh_completed_phases()
            gate_installed = current_pair_journal["phases"].get(
                "pair_gate_installed"
            )
            if not isinstance(gate_installed, dict):
                raise ReleaseBuildError(
                    "paired release gate installation receipt is missing"
                )
            if "pair_gate_release_intent" not in completed_phases:
                record_phase(
                    "pair_gate_release_intent",
                    {
                        "owner_hash": gate_installed.get("owner_hash"),
                        "payload_sha256": gate_installed.get("payload_sha256"),
                    },
                )
            release_intent = read_transaction_journal(
                transaction_journal_path,
                transaction_id=transaction_id,
            )["phases"]["pair_gate_release_intent"]
            opened = release_pair_after_acceptance(
                candidate_identity,
                release_intent,
            )
            if (
                not isinstance(opened, dict)
                or not isinstance(opened.get("release"), dict)
                or not isinstance(opened.get("opened"), dict)
            ):
                raise ReleaseBuildError("paired release-open receipt is invalid")
            record_phase("pair_released", opened["release"])
            record_phase("pair_opened", opened["opened"])
        finished = True
        return {
            "status": "accepted",
            "identity": candidate_identity,
            "admission": final_admission,
            "selection": selection,
            "promotion": promoted,
            "binding": binding,
            "accepted_binding": accepted_binding,
            "resumed": bool(journal["phases"]),
        }

    try:
        require_external_state_attestations()
        if "rollback_started" in completed_phases:
            raise ReleaseBuildError("resuming durable rollback")

        if (
            bootout_process is not None
            and "old_job_booted_out" in completed_phases
            and "candidate_job_bootstrapped" not in completed_phases
        ):
            old_identity = journal["phases"].get("old_fenced", {}).get(
                "identity"
            )
            if not isinstance(old_identity, dict):
                raise ReleaseBuildError(
                    "booted-out job has no durable old-process identity"
                )
            _require_expected_last_good_webui_identity(
                old_identity,
                expected_last_good_identity,
            )
            if "old_stopped" not in completed_phases:
                remaining = max(
                    0.1,
                    timeout_seconds - (monotonic() - started_at),
                )
                wait_for_process_exit(old_identity, remaining)
                old_process_exited = True
                record_phase("old_stopped", {"identity": old_identity})
            if bootstrap_candidate_job is None:
                raise ReleaseBuildError(
                    "candidate launchd bootstrap callback is unavailable"
                )
            candidate_probe: dict | None = None
            try:
                probe = _require_bound_control_receipt(
                    inspect_control(),
                    status="inspected",
                    transaction_id=transaction_id,
                )
                if _candidate_identity_matches(
                    probe.get("identity"),
                    expected_candidate_identity,
                ):
                    candidate_probe = probe
                elif isinstance(probe.get("identity"), dict):
                    raise DrainIdentityMismatch(
                        "unexpected process owns candidate endpoint before bootstrap"
                    )
            except ReleaseBuildError as exc:
                if "HTTP request failed" not in str(exc):
                    raise
            if candidate_probe is None:
                bootstrapped = bootstrap_candidate_job()
            else:
                bootstrapped = {
                    "status": "externally-reconciled",
                    "candidate_identity": candidate_probe["identity"],
                }
            record_phase(
                "candidate_job_bootstrapped",
                {"bootstrap": bootstrapped},
            )
            candidate_may_have_started = True
            candidate_inspection = candidate_probe
            if candidate_inspection is None:
                candidate_inspection = inspect_with_retry(
                    window_started_at=monotonic(),
                )
            return finish_candidate(candidate_inspection)

        initial = inspect_with_retry(initial_inspection)
        initial_identity = initial.get("identity")
        if _resumable_candidate_identity_matches(
            initial_identity,
            expected_candidate_identity,
            completed_phases,
        ):
            activated = True
            candidate_may_have_started = True
            old_process_exited = True
            if "selection_activated" not in completed_phases:
                selection = activate_selection()
                record_phase("selection_activated", {"selection": selection})
            if "old_stopped" not in completed_phases:
                old_identity = journal["phases"].get("old_fenced", {}).get(
                    "identity"
                )
                if not isinstance(old_identity, dict):
                    raise ReleaseBuildError(
                        "candidate is live without a durable old-process identity"
                    )
                _require_expected_last_good_webui_identity(
                    old_identity,
                    expected_last_good_identity,
                )
                remaining = max(
                    0.1,
                    timeout_seconds - (monotonic() - started_at),
                )
                wait_for_process_exit(old_identity, remaining)
                if (
                    bootout_process is not None
                    and "old_job_booted_out" not in completed_phases
                ):
                    record_phase(
                        "old_job_booted_out",
                        {
                            "identity": old_identity,
                            "bootout": {
                                "status": "externally-reconciled",
                                "candidate_is_live": True,
                            },
                        },
                    )
                record_phase("old_stopped", {"identity": old_identity})
            if (
                bootstrap_candidate_job is not None
                and "candidate_job_bootstrapped" not in completed_phases
            ):
                record_phase(
                    "candidate_job_bootstrapped",
                    {
                        "bootstrap": {
                            "status": "externally-reconciled",
                            "candidate_identity": initial_identity,
                        }
                    },
                )
            return finish_candidate(initial)

        if not isinstance(initial_identity, dict) or not initial_identity:
            raise DrainIdentityMismatch("release health has no process identity")
        _require_expected_last_good_webui_identity(
            initial_identity,
            expected_last_good_identity,
        )
        identity = initial_identity
        if "old_stopped" in completed_phases:
            raise DrainIdentityMismatch(
                "old process reappeared after durable stop receipt"
            )

        if "old_committed" not in completed_phases:
            fenced = _require_bound_control_receipt(
                send_control("fence", identity, None),
                status="fenced",
                transaction_id=transaction_id,
                identity=identity,
            )
            token = str(fenced.get("fence_token") or "")
            if not token:
                raise ReleaseBuildError("release control fence receipt is invalid")
            record_phase(
                "old_fenced",
                {
                    "identity": identity,
                    "admission": {"state": "fenced"},
                },
            )
            run_thread_checkpoint()
            external_drain: dict | None = None
            inspection: dict = {}
            while True:
                if monotonic() - started_at > timeout_seconds:
                    raise DrainTimeout("release control drain timed out")
                inspection = inspect_control()
                _require_bound_control_receipt(
                    inspection,
                    status="inspected",
                    transaction_id=transaction_id,
                    identity=identity,
                )
                if _release_inspection_is_drained(inspection):
                    break
                external_drain = attest_external_drain(identity, inspection)
                if external_drain is not None:
                    break
                sleep(interval_seconds)
            if external_drain is None:
                receipt = _require_bound_control_receipt(
                    send_control("commit", identity, token),
                    status="committing",
                    transaction_id=transaction_id,
                    identity=identity,
                )
                committed_admission = {"state": "committing"}
                committed_activity = receipt.get("activity")
            else:
                committed_admission = {"state": "fenced-external-drain"}
                committed_activity = inspection.get("activity")
            record_phase(
                "old_committed",
                {
                    "identity": identity,
                    "admission": committed_admission,
                    "activity": committed_activity,
                    **(
                        {"external_activity_drain": external_drain}
                        if external_drain is not None
                        else {}
                    ),
                },
            )
        elif (
            "selection_activated" not in completed_phases
            or (
                bootout_process is not None
                and "old_job_booted_out" not in completed_phases
            )
        ):
            # A durable commit receipt does not preserve the secret token and
            # its bounded lease may have reopened while the controller was
            # down. Re-attest the exact old process, then idempotently fence it
            # again to recover a fresh/in-memory token before activation.
            committed_inspection = _require_bound_control_receipt(
                inspect_control(),
                status="inspected",
                transaction_id=transaction_id,
                identity=identity,
            )
            committed_admission = committed_inspection.get("admission")
            if (
                not isinstance(committed_admission, dict)
                or committed_admission.get("state")
                not in {"open", "fenced", "committing"}
            ):
                raise ReleaseBuildError(
                    "durable old commit admission cannot be resumed"
                )
            refenced_raw = send_control("fence", identity, None)
            refenced_status = str(
                refenced_raw.get("status")
                if isinstance(refenced_raw, dict)
                else ""
            )
            if refenced_status not in {"fenced", "committing"}:
                raise ReleaseBuildError(
                    "durable old commit re-fence receipt is invalid"
                )
            refenced = _require_bound_control_receipt(
                refenced_raw,
                status=refenced_status,
                transaction_id=transaction_id,
                identity=identity,
            )
            token = str(refenced.get("fence_token") or "")
            if not token:
                raise ReleaseBuildError(
                    "durable old commit re-fence token is missing"
                )
            if refenced_status == "fenced":
                external_drain = None
                while True:
                    if monotonic() - started_at > timeout_seconds:
                        raise DrainTimeout("release control drain timed out")
                    inspection = _require_bound_control_receipt(
                        inspect_control(),
                        status="inspected",
                        transaction_id=transaction_id,
                        identity=identity,
                    )
                    if _release_inspection_is_drained(inspection):
                        break
                    external_drain = attest_external_drain(
                        identity,
                        inspection,
                    )
                    if external_drain is not None:
                        break
                    sleep(interval_seconds)
                if external_drain is None:
                    _require_bound_control_receipt(
                        send_control("commit", identity, token),
                        status="committing",
                        transaction_id=transaction_id,
                        identity=identity,
                    )
        if "selection_activated" not in completed_phases:
            selection = activate_selection()
            activated = True
            candidate_may_have_started = True
            record_phase("selection_activated", {"selection": selection})
        else:
            activated = True
            candidate_may_have_started = True
        if bootout_process is not None:
            if "old_job_booted_out" not in completed_phases:
                bootout_receipt = bootout_process(identity)
                record_phase(
                    "old_job_booted_out",
                    {"identity": identity, "bootout": bootout_receipt},
                )
        else:
            signal_process(identity)
        remaining = max(0.1, timeout_seconds - (monotonic() - started_at))
        wait_for_process_exit(identity, remaining)
        old_process_exited = True
        record_phase("old_stopped", {"identity": identity})

        if bootstrap_candidate_job is not None:
            if "candidate_job_bootstrapped" not in completed_phases:
                bootstrapped = bootstrap_candidate_job()
                record_phase(
                    "candidate_job_bootstrapped",
                    {"bootstrap": bootstrapped},
                )
            candidate_may_have_started = True

        candidate_inspection = inspect_with_retry(
            window_started_at=monotonic(),
        )
        return finish_candidate(candidate_inspection)
    except Exception as original:
        if isinstance(original, _LastGoodWebUIIdentityMismatch):
            raise
        durable_now = read_transaction_journal(
            transaction_journal_path,
            transaction_id=transaction_id,
        )
        completed_phases.update(durable_now["phases"])
        if "candidate_gateway_accepted" in completed_phases:
            candidate_may_have_mutated = True
        if "pair_commit_intent" in completed_phases:
            raise ReleaseBuildError(
                "paired release crossed its durable commit boundary; "
                "rerun the same transaction to roll forward"
            ) from original
        recovery_errors: list[str] = []

        durable_old_identity: dict | None = None
        for phase_name, key in (
            ("old_committed", "identity"),
            ("old_fenced", "identity"),
            ("rollback_started", "old_identity"),
        ):
            candidate = journal["phases"].get(phase_name, {}).get(key)
            if isinstance(candidate, dict) and candidate:
                durable_old_identity = candidate
                break
        if durable_old_identity is None and identity:
            durable_old_identity = dict(identity)
        if durable_old_identity is not None:
            _require_expected_last_good_webui_identity(
                durable_old_identity,
                expected_last_good_identity,
            )

        if "rollback_started" not in completed_phases:
            try:
                rollback_started_receipt: dict[str, object] = {
                    "failed_after_activation": activated,
                    "old_process_exited": old_process_exited,
                    "error_type": type(original).__name__,
                }
                if durable_old_identity is not None:
                    rollback_started_receipt["old_identity"] = durable_old_identity
                journal = record_phase(
                    "rollback_started",
                    rollback_started_receipt,
                )
            except Exception as exc:
                recovery_errors.append(f"rollback journal failed: {exc}")

        candidate_may_have_started = candidate_may_have_started or any(
            phase in completed_phases
            for phase in (
                "selection_activated",
                "old_job_booted_out",
                "old_stopped",
                "candidate_job_bootstrapped",
                "replacement_proved",
                "candidate_accepted",
                "accepted_health_proved",
                "promoted",
            )
        )
        candidate_may_have_mutated = candidate_may_have_mutated or any(
            phase in completed_phases
            for phase in (
                "candidate_accepted",
                "accepted_health_proved",
                "pair_accepted",
            )
        )
        old_process_exited = old_process_exited or "old_stopped" in completed_phases
        old_process_alive = False
        old_recovery_ok = True
        old_recovery_receipt: dict[str, object] = {
            "status": "not-required",
            "reason": "old_process_was_never_fenced",
        }

        if durable_old_identity is not None and not old_process_exited:
            try:
                try:
                    old_pid = int(durable_old_identity.get("pid"))
                except (TypeError, ValueError) as exc:
                    raise DrainIdentityMismatch(
                        "durable old-process PID is invalid"
                    ) from exc
                old_start = str(
                    durable_old_identity.get("pid_start_token") or ""
                )
                if old_pid <= 1 or not old_start:
                    raise DrainIdentityMismatch(
                        "durable old-process start identity is invalid"
                    )
                live = _require_bound_control_receipt(
                    inspect_control(),
                    status="inspected",
                    transaction_id=transaction_id,
                )
                live_identity = live.get("identity")
                if live_identity == durable_old_identity:
                    refenced_raw = send_control(
                        "fence",
                        durable_old_identity,
                        None,
                    )
                    refenced_status = str(
                        refenced_raw.get("status")
                        if isinstance(refenced_raw, dict)
                        else ""
                    )
                    if refenced_status not in {"fenced", "committing"}:
                        raise ReleaseBuildError(
                            "old-process re-fence receipt is invalid"
                        )
                    refenced = _require_bound_control_receipt(
                        refenced_raw,
                        status=refenced_status,
                        transaction_id=transaction_id,
                        identity=durable_old_identity,
                    )
                    recovered_token = str(refenced.get("fence_token") or "")
                    if not recovered_token:
                        raise ReleaseBuildError(
                            "old-process re-fence token is missing"
                        )
                    aborted = _require_bound_control_receipt(
                        send_control(
                            "abort",
                            durable_old_identity,
                            recovered_token,
                        ),
                        status="aborted",
                        transaction_id=transaction_id,
                        identity=durable_old_identity,
                    )
                    reopened = _require_bound_control_receipt(
                        inspect_control(),
                        status="inspected",
                        transaction_id=transaction_id,
                        identity=durable_old_identity,
                    )
                    reopened_admission = reopened.get("admission")
                    if (
                        not isinstance(reopened_admission, dict)
                        or reopened_admission.get("state") != "open"
                    ):
                        raise ReleaseBuildError(
                            "old process did not reopen after rollback abort"
                        )
                    old_process_alive = True
                    old_recovery_receipt = {
                        "status": "verified",
                        "pid": old_pid,
                        "pid_start_token": old_start,
                        "admission": "open",
                        "abort_status": aborted.get("status"),
                    }
                elif _candidate_identity_matches(
                    live_identity,
                    expected_candidate_identity,
                ):
                    old_process_exited = True
                    candidate_may_have_started = True
                    live_admission = live.get("admission")
                    if (
                        isinstance(live_admission, dict)
                        and live_admission.get("state") == "open"
                    ):
                        candidate_may_have_mutated = True
                    old_recovery_receipt = {
                        "status": "not-required",
                        "reason": "old_process_already_replaced",
                    }
                else:
                    raise DrainIdentityMismatch(
                        "rollback control process does not match old or candidate identity"
                    )
            except Exception as exc:
                old_recovery_ok = False
                recovery_errors.append(f"old-process recovery failed: {exc}")
        elif old_process_exited:
            old_recovery_receipt = {
                "status": "not-required",
                "reason": "old_process_durably_stopped",
            }

        if "state_rolled_back" not in completed_phases:
            try:
                rollback_result = rollback_selection()
                if not old_recovery_ok:
                    raise ReleaseBuildError(
                        "old process recovery is not durably verified"
                    )
                record_phase(
                    "state_rolled_back",
                    {
                        "rollback": rollback_result,
                        "old_process_recovery": old_recovery_receipt,
                    },
                )
            except Exception as exc:
                recovery_errors.append(f"selector rollback failed: {exc}")

        if "plist_restored" not in completed_phases:
            try:
                restored = restore_plist()
                record_phase("plist_restored", {"restore": restored})
            except Exception as exc:
                recovery_errors.append(f"plist restore failed: {exc}")

        if "failed_candidate_stopped" not in completed_phases:
            try:
                if candidate_may_have_started:
                    stopped = stop_failed_candidate()
                    stopped_receipt = {"status": "stopped", "stop": stopped}
                else:
                    stopped_receipt = {
                        "status": "not-required",
                        "reason": "candidate_never_started",
                    }
                record_phase("failed_candidate_stopped", stopped_receipt)
            except Exception as exc:
                recovery_errors.append(f"failed-candidate stop failed: {exc}")

        if "state_snapshot_restored" not in completed_phases:
            try:
                if candidate_may_have_mutated:
                    state_restored = require_exact_snapshot_receipt(
                        restore_state_snapshot(),
                        status="restored",
                    )
                    snapshot_receipt = dict(state_restored)
                else:
                    expected_snapshot = expected_snapshot_receipt()
                    snapshot_receipt = {
                        "status": "not-required",
                        "reason": "candidate_never_accepted",
                        "state_snapshot_id": expected_snapshot[
                            "state_snapshot_id"
                        ],
                        "state_snapshot_sha256": expected_snapshot[
                            "state_snapshot_sha256"
                        ],
                    }
                record_phase("state_snapshot_restored", snapshot_receipt)
            except Exception as exc:
                recovery_errors.append(f"state snapshot restore failed: {exc}")

        if "last_good_restarted" not in completed_phases:
            try:
                if old_process_exited or force_restart_on_rollback:
                    restarted = restart_selection()
                    restart_receipt = {"status": "restarted", "restart": restarted}
                else:
                    restart_receipt = {
                        "status": "not-required",
                        "reason": (
                            "old_process_verified_open"
                            if old_process_alive
                            else "old_process_was_never_replaced"
                        ),
                    }
                record_phase("last_good_restarted", restart_receipt)
            except Exception as exc:
                recovery_errors.append(f"last-good restart failed: {exc}")

        if "rollback_verified" not in completed_phases:
            try:
                verified_rollback = require_exact_snapshot_receipt(
                    verify_rollback(),
                    status="verified",
                )
                record_phase("rollback_verified", verified_rollback)
            except Exception as exc:
                recovery_errors.append(f"last-good verification failed: {exc}")
        if recovery_errors:
            raise ReleaseBuildError(
                f"release cutover failed: {original}; " + "; ".join(recovery_errors)
            ) from original
        raise
    finally:
        if finished:
            token = ""


def _read_json_object(path: Path | str, *, label: str) -> dict:
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or Path(os.path.abspath(candidate)) != candidate
        or candidate.is_symlink()
    ):
        raise ReleaseBuildError(f"{label} path is invalid")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    if len(raw) > 16 * 1024 * 1024:
        raise ReleaseBuildError(f"{label} is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{label} JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError(f"{label} must be a JSON object")
    return value


def _read_plist(path: Path | str) -> dict:
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or Path(os.path.abspath(candidate)) != candidate
        or candidate.is_symlink()
    ):
        raise ReleaseBuildError("launchd plist input path is invalid")
    try:
        value = plistlib.loads(candidate.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ReleaseBuildError("launchd plist input is invalid") from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError("launchd plist root is invalid")
    return value


def _write_plist_atomic(path: Path | str, value: dict) -> None:
    destination = Path(path)
    if not destination.is_absolute() or Path(os.path.abspath(destination)) != destination:
        raise ReleaseBuildError("launchd plist output path is invalid")
    parent = _prepare_release_root(destination.parent)
    if destination.is_symlink():
        raise ReleaseBuildError("launchd plist output must not be a symlink")
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        replaced = True
        _fsync_directory(parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


_CUTOVER_PLAN_REQUIRED = {
    "version",
    "transaction_id",
    "base_url",
    "signing_key_file",
    "selector_state",
    "selector_lock",
    "transaction_journal",
    "expected_candidate_identity_json",
    "last_good_identity_json",
    "last_good_gateway_identity_json",
    "installed_plist",
    "bootstrap_rollback_plist",
    "managed_plist",
    "launchd_domain",
    "launchd_label",
    "listener_port",
    "snapshot_manifest",
    "snapshot_root",
    "mutable_state_paths",
    "selector_path",
    "managed_interpreter",
    "expected_old_interpreter",
    "expected_old_target",
    "cli_link",
    "cli_old_target",
    "cli_shim_dir",
}
_LAST_GOOD_ORIGIN_JOURNAL_PLAN_KEYS = {
    "last_good_origin_journal",
    "last_good_origin_journal_sha256",
    "last_good_gateway_origin_journal",
    "last_good_gateway_origin_journal_sha256",
}
_LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS = {
    "last_good_split_adoption_receipt",
    "last_good_split_adoption_receipt_sha256",
}
_BOOTSTRAP_GATEWAY_PLAN_KEYS = {
    "gateway_installed_plist",
    "gateway_rollback_plist",
    "managed_gateway_plist",
    "gateway_launchd_domain",
    "gateway_launchd_label",
    "gateway_listener_port",
    "gateway_health_url",
}
_BOOTSTRAP_WATCHDOG_PLAN_KEYS = {
    "watchdog_installed_script",
    "watchdog_candidate_script",
    "watchdog_rollback_script",
    "watchdog_state_file",
    "watchdog_crontab_rollback",
    "watchdog_expected_sha256",
}
_BOOTSTRAP_WATCHDOG_SCHEDULER_PLAN_KEYS = {
    "watchdog_scheduler_backend",
    "watchdog_scheduler_registry",
    "watchdog_scheduler_job_id",
}
_BOOTSTRAP_INGRESS_GATE_PLAN_KEYS = {
    "ingress_gate_script",
    "ingress_gate_expected_sha256",
    "ingress_gate_token_file",
    "ingress_gate_ready_receipt",
}
_BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS = {
    "legacy_state_db",
    "synthetic_process_notifications_path",
    "synthetic_process_notifications_expected_sha256",
    "synthetic_process_notification_ids",
    "synthetic_async_delegations_path",
    "synthetic_async_delegations_expected_sha256",
    "synthetic_async_delegation_ids",
    "synthetic_quarantine_root",
}
_CUTOVER_PLAN_OPTIONAL = {
    "timeout_seconds",
    "interval_seconds",
    *_LAST_GOOD_ORIGIN_JOURNAL_PLAN_KEYS,
    *_LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS,
    *_BOOTSTRAP_GATEWAY_PLAN_KEYS,
    *_BOOTSTRAP_WATCHDOG_PLAN_KEYS,
    *_BOOTSTRAP_WATCHDOG_SCHEDULER_PLAN_KEYS,
    *_BOOTSTRAP_INGRESS_GATE_PLAN_KEYS,
    *_BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS,
}
_CUTOVER_PLAN_PATH_KEYS = {
    "signing_key_file",
    "selector_state",
    "selector_lock",
    "transaction_journal",
    "expected_candidate_identity_json",
    "last_good_identity_json",
    "last_good_gateway_identity_json",
    "last_good_origin_journal",
    "last_good_gateway_origin_journal",
    "last_good_split_adoption_receipt",
    "installed_plist",
    "bootstrap_rollback_plist",
    "managed_plist",
    "snapshot_manifest",
    "snapshot_root",
    "selector_path",
    "managed_interpreter",
    "expected_old_interpreter",
    "expected_old_target",
    "cli_link",
    "cli_shim_dir",
    "gateway_installed_plist",
    "gateway_rollback_plist",
    "managed_gateway_plist",
    "watchdog_installed_script",
    "watchdog_candidate_script",
    "watchdog_rollback_script",
    "watchdog_crontab_rollback",
    "watchdog_state_file",
    "watchdog_scheduler_registry",
    "ingress_gate_script",
    "ingress_gate_token_file",
    "ingress_gate_ready_receipt",
    "legacy_state_db",
    "synthetic_process_notifications_path",
    "synthetic_async_delegations_path",
    "synthetic_quarantine_root",
}
_CUTOVER_MUTABLE_REFERENCE_PATH_KEYS = {
    "watchdog_state_file",
    "watchdog_scheduler_registry",
    "legacy_state_db",
    "synthetic_process_notifications_path",
    "synthetic_async_delegations_path",
}
_VERIFIED_RELEASE_IDENTITY_KEYS = {
    "build_id",
    "commit",
    "tree",
    "manifest_sha256",
    "release_path",
    "selector_path",
    "selector_resolved_path",
    "selector_verified",
    "interpreter_path",
    "interpreter_resolved_path",
    "runtime_path",
    "runtime_resolved_path",
    "runtime_python_home_path",
    "runtime_site_packages_path",
    "runtime_manifest_path",
    "runtime_manifest_sha256",
    "agent_source_path",
    "agent_source_resolved_path",
    "agent_source_commit",
    "agent_source_tree",
    "agent_source_manifest_path",
    "agent_source_manifest_sha256",
}
_LAST_GOOD_SHARED_IDENTITY_KEYS = {
    "selector_path", "selector_resolved_path", "selector_verified",
    "interpreter_path", "interpreter_resolved_path",
    "runtime_path", "runtime_resolved_path", "runtime_python_home_path",
    "runtime_site_packages_path", "runtime_manifest_path", "runtime_manifest_sha256",
    "agent_source_path", "agent_source_resolved_path", "agent_source_commit",
    "agent_source_tree", "agent_source_manifest_path", "agent_source_manifest_sha256",
}
_LAST_GOOD_SELECTOR_IDENTITY_KEYS = {
    "selector_path",
    "selector_resolved_path",
    "selector_verified",
}
_LAST_GOOD_RUNTIME_SHARED_IDENTITY_KEYS = (
    _LAST_GOOD_SHARED_IDENTITY_KEYS - _LAST_GOOD_SELECTOR_IDENTITY_KEYS
)
_LAST_GOOD_ORIGIN_IDENTITY_KEYS = _VERIFIED_RELEASE_IDENTITY_KEYS | {
    "selector_generation", "startup_transaction_id", "launchd_label",
}


def _absolute_plan_path(value: object, *, label: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise ReleaseBuildError(f"cutover plan {label} path is invalid")
    if path.is_symlink() and label not in {
        "managed_interpreter",
        "expected_old_interpreter",
        "cli_link",
    }:
        raise ReleaseBuildError(f"cutover plan {label} must not be a symlink")
    return path


def _attest_expected_release_identity(
    identity: object,
    *,
    selector_path: str,
    label: str,
) -> dict:
    """Reverify every sealed release field before a cutover may stop services."""
    if (
        not isinstance(identity, dict)
        or not _VERIFIED_RELEASE_IDENTITY_KEYS.issubset(identity)
    ):
        raise ReleaseBuildError(f"{label} release identity is incomplete")
    release_path = Path(str(identity.get("release_path") or ""))
    try:
        verified = release_selector.verify_release(
            release_path,
            release_root=release_path.parent,
            expected_manifest_sha256=str(identity["manifest_sha256"]),
            selector_path=selector_path,
        )
    except (OSError, release_selector.SelectorError) as exc:
        raise ReleaseBuildError(
            f"{label} release identity failed verification: {exc}"
        ) from exc
    for key in sorted(_VERIFIED_RELEASE_IDENTITY_KEYS):
        if identity.get(key) != verified.get(key):
            raise ReleaseBuildError(f"{label} release identity mismatch: {key}")
    return verified


def _read_sealed_origin_journal(
    path: str,
    *,
    expected_sha256: object,
    transaction_id: object,
    trusted_root: Path,
    label: str,
) -> dict:
    """Read a bound origin journal without creating a lock or changing state."""
    origin_path = Path(path)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256 or ""))
        or not _TRANSACTION_ID.fullmatch(str(transaction_id or ""))
        or not trusted_root.is_absolute()
        or Path(os.path.abspath(trusted_root)) != trusted_root
        or not origin_path.is_absolute()
        or Path(os.path.abspath(origin_path)) != origin_path
        or not origin_path.is_relative_to(trusted_root)
    ):
        raise ReleaseBuildError(f"{label} origin journal binding is invalid")

    current = Path(trusted_root.anchor)
    for part in trusted_root.parts[1:]:
        current /= part
        try:
            opened = os.lstat(current)
        except OSError as exc:
            raise ReleaseBuildError(f"{label} origin journal root is unreadable") from exc
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
        ):
            raise ReleaseBuildError(f"{label} origin journal root is unsafe")
    if trusted_root.stat().st_uid != os.getuid() or trusted_root.stat().st_mode & 0o022:
        raise ReleaseBuildError(f"{label} origin journal root is unsafe")
    current = trusted_root
    for part in origin_path.relative_to(trusted_root).parts[:-1]:
        current /= part
        try:
            opened = os.lstat(current)
        except OSError as exc:
            raise ReleaseBuildError(f"{label} origin journal root is unreadable") from exc
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o022
        ):
            raise ReleaseBuildError(f"{label} origin journal root is unsafe")
    try:
        descriptor = os.open(
            origin_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} origin journal is unreadable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or len(payload) > 4 * 1024 * 1024
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ReleaseBuildError(f"{label} origin journal identity is invalid")
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{label} origin journal JSON is invalid") from exc
    return _validated_transaction_journal(raw, str(transaction_id))


def _read_sealed_split_adoption_receipt(
    path: str,
    *,
    expected_sha256: object,
    trusted_root: Path,
    webui_identity: dict,
    gateway_identity: dict,
) -> dict:
    """Read one sealed historical observation of an exact live split pair."""
    receipt_path = Path(path)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256 or ""))
        or not trusted_root.is_absolute()
        or Path(os.path.abspath(trusted_root)) != trusted_root
        or not receipt_path.is_absolute()
        or Path(os.path.abspath(receipt_path)) != receipt_path
        or not receipt_path.is_relative_to(trusted_root)
    ):
        raise ReleaseBuildError("last-good adoption receipt binding is invalid")
    current = Path(trusted_root.anchor)
    for part in trusted_root.parts[1:]:
        current /= part
        try:
            opened = os.lstat(current)
        except OSError as exc:
            raise ReleaseBuildError(
                "last-good adoption receipt root is unreadable"
            ) from exc
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode):
            raise ReleaseBuildError("last-good adoption receipt root is unsafe")
    if trusted_root.stat().st_uid != os.getuid() or trusted_root.stat().st_mode & 0o022:
        raise ReleaseBuildError("last-good adoption receipt root is unsafe")
    current = trusted_root
    for part in receipt_path.relative_to(trusted_root).parts[:-1]:
        current /= part
        try:
            opened = os.lstat(current)
        except OSError as exc:
            raise ReleaseBuildError(
                "last-good adoption receipt root is unreadable"
            ) from exc
        if (
            stat.S_ISLNK(opened.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o022
        ):
            raise ReleaseBuildError("last-good adoption receipt root is unsafe")
    try:
        descriptor = os.open(
            receipt_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ReleaseBuildError("last-good adoption receipt is unreadable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or len(payload) > 4 * 1024 * 1024
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ReleaseBuildError("last-good adoption receipt identity is invalid")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseBuildError(
                    "last-good adoption receipt has duplicate keys"
                )
            result[key] = value
        return result

    try:
        raw = json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(
            "last-good adoption receipt JSON is invalid"
        ) from exc
    schema_version = (
        raw.get("schema"),
        raw.get("version"),
    ) if isinstance(raw, dict) else (None, None)
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "schema",
            "version",
            "adoption_id",
            "created_at",
            "selector",
            "webui",
            "gateway",
            "shared_identity_sha256",
        }
        or schema_version
        not in {
            ("hermes.last_good_split_adoption.v1", 1),
            ("hermes.last_good_split_adoption.v2", 2),
        }
        or not _TRANSACTION_ID.fullmatch(str(raw.get("adoption_id") or ""))
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(raw.get("shared_identity_sha256") or "")
        )
        or _journal_contains_sensitive_value(raw)
    ):
        raise ReleaseBuildError("last-good adoption receipt schema is invalid")
    timestamp = str(raw.get("created_at") or "")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ReleaseBuildError(
            "last-good adoption receipt timestamp is invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc).isoformat() != timestamp
    ):
        raise ReleaseBuildError(
            "last-good adoption receipt timestamp is not canonical UTC"
        )
    canonical = json.dumps(
        raw,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != payload:
        raise ReleaseBuildError("last-good adoption receipt is not canonical")
    selector = raw.get("selector")
    selector_state = (
        selector.get("state") if isinstance(selector, dict) else None
    )
    if (
        not isinstance(selector, dict)
        or set(selector) != {"state", "state_sha256"}
        or not isinstance(selector_state, dict)
        or selector.get("state_sha256")
        != _canonical_journal_value_sha256(selector_state)
    ):
        raise ReleaseBuildError(
            "last-good adoption receipt selector authority is invalid"
        )
    _validate_split_adoption_selector_authority(
        selector_state,
        webui_identity=webui_identity,
        gateway_identity=gateway_identity,
        schema_version=schema_version,
    )
    for name, expected_identity, admission in (
        ("webui", webui_identity, "open"),
        ("gateway", gateway_identity, "accepting_new_work"),
    ):
        component = raw.get(name)
        if (
            not isinstance(component, dict)
            or set(component) != {"identity", "identity_sha256", "live_binding"}
            or component.get("identity") != expected_identity
            or component.get("identity_sha256")
            != _canonical_journal_value_sha256(expected_identity)
        ):
            raise ReleaseBuildError(
                f"last-good adoption receipt {name} identity changed"
            )
        binding = component.get("live_binding")
        if (
            not isinstance(binding, dict)
            or set(binding)
            != {
                "listener_pid",
                "pid_start_token",
                "admission_state",
                "evidence",
                "binding_sha256",
            }
            or isinstance(binding.get("listener_pid"), bool)
            or not isinstance(binding.get("listener_pid"), int)
            or binding["listener_pid"] <= 1
            or not str(binding.get("pid_start_token") or "")
            or binding.get("admission_state") != admission
            or not isinstance(binding.get("evidence"), dict)
            or binding.get("binding_sha256")
            != _canonical_journal_value_sha256(binding.get("evidence"))
            or binding["evidence"].get("listener_pid")
            != binding["listener_pid"]
            or binding["evidence"].get("pid_start_token")
            != binding["pid_start_token"]
        ):
            raise ReleaseBuildError(
                f"last-good adoption receipt {name} live binding is invalid"
            )
        observed_admission = (
            binding["evidence"].get("admission", {}).get("state")
            if name == "webui"
            else binding["evidence"].get("health", {}).get("drain", {}).get(
                "admission", {}
            ).get("state")
        )
        if observed_admission != admission:
            raise ReleaseBuildError(
                f"last-good adoption receipt {name} live binding is invalid"
            )
    shared_identity_keys = (
        _LAST_GOOD_RUNTIME_SHARED_IDENTITY_KEYS
        if schema_version == ("hermes.last_good_split_adoption.v2", 2)
        else _LAST_GOOD_SHARED_IDENTITY_KEYS
    )
    shared_identity = {
        key: webui_identity.get(key)
        for key in sorted(shared_identity_keys)
    }
    if (
        raw["shared_identity_sha256"]
        != _canonical_journal_value_sha256(shared_identity)
    ):
        raise ReleaseBuildError(
            "last-good adoption receipt shared identity changed"
        )
    return raw


def _validate_split_adoption_selector_authority(
    selector_state: dict,
    *,
    webui_identity: dict,
    gateway_identity: dict,
    schema_version: tuple[object, object],
) -> None:
    """Validate the exact selector/startup state represented by an adoption."""
    try:
        selector_generation = int(webui_identity.get("selector_generation", -1))
    except (TypeError, ValueError):
        selector_generation = -1
    common_invalid = (
        selector_state.get("current") != webui_identity.get("build_id")
        or selector_state.get("candidate") is not None
        or selector_state.get("pending_transaction_id") is not None
    )
    if schema_version == ("hermes.last_good_split_adoption.v1", 1):
        if (
            common_invalid
            or selector_state.get("last_good") != webui_identity.get("build_id")
            or selector_state.get("generation") != selector_generation + 1
        ):
            raise ReleaseBuildError(
                "last-good adoption receipt selector authority is invalid"
            )
        return
    if schema_version != ("hermes.last_good_split_adoption.v2", 2):
        raise ReleaseBuildError("last-good adoption receipt schema is invalid")
    try:
        validated_state = release_selector._validate_state(selector_state)
    except (TypeError, ValueError, release_selector.SelectorError) as exc:
        raise ReleaseBuildError(
            "last-good adoption receipt selector authority is invalid"
        ) from exc
    fallback_id = validated_state.get("last_good")
    release_root = Path(str(validated_state.get("release_root") or ""))
    release_path = Path(str(webui_identity.get("release_path") or ""))
    current_record = validated_state.get("releases", {}).get(
        webui_identity.get("build_id")
    )
    expected_current_record = {
        key: webui_identity.get(key)
        for key in ("manifest_sha256", "commit", "tree")
    }
    expected_fallback_record = {
        key: gateway_identity.get(key)
        for key in ("manifest_sha256", "commit", "tree")
    }
    if (
        common_invalid
        or validated_state != selector_state
        or selector_state.get("generation") != selector_generation
        or fallback_id != validated_state.get("bootstrap_fallback")
        or fallback_id == validated_state.get("current")
        or release_path != release_root / str(validated_state.get("current"))
        or current_record != expected_current_record
        or fallback_id != gateway_identity.get("build_id")
        or Path(str(gateway_identity.get("release_path") or ""))
        != release_root / str(fallback_id)
        or validated_state.get("releases", {}).get(fallback_id)
        != expected_fallback_record
        or webui_identity.get("startup_fenced") is not False
        or webui_identity.get("startup_transaction_id") is not None
    ):
        raise ReleaseBuildError(
            "last-good adoption receipt selector authority is invalid"
        )
    fallback_record = validated_state["releases"][fallback_id]
    try:
        verified_fallback = release_selector.verify_release(
            release_root / fallback_id,
            release_root=release_root,
            expected_manifest_sha256=fallback_record["manifest_sha256"],
            selector_path=str(gateway_identity.get("selector_path") or ""),
        )
    except (OSError, release_selector.SelectorError) as exc:
        raise ReleaseBuildError(
            "last-good adoption receipt fallback release identity is invalid"
        ) from exc
    if any(
        verified_fallback.get(key) != fallback_record.get(key)
        for key in ("manifest_sha256", "commit", "tree")
    ):
        raise ReleaseBuildError(
            "last-good adoption receipt fallback release identity is invalid"
        )


def _attest_last_good_identity_split(
    *,
    webui_identity: dict,
    gateway_identity: dict,
    trusted_root: Path,
    selector_path: str,
    webui_origin_journal: str | None = None,
    webui_origin_sha256: object = None,
    gateway_origin_journal: str | None = None,
    gateway_origin_sha256: object = None,
    adoption_receipt: str | None = None,
    adoption_receipt_sha256: object = None,
) -> MappingProxyType:
    """Purely attest independently sealed WebUI and gateway last-good identities."""
    journal_presence = tuple(
        value is not None
        for value in (
            webui_origin_journal,
            webui_origin_sha256,
            gateway_origin_journal,
            gateway_origin_sha256,
        )
    )
    adoption_presence = (
        adoption_receipt is not None,
        adoption_receipt_sha256 is not None,
    )
    journal_mode = all(journal_presence)
    adoption_mode = all(adoption_presence)
    if (
        any(journal_presence) != journal_mode
        or any(adoption_presence) != adoption_mode
        or journal_mode == adoption_mode
    ):
        raise ReleaseBuildError("last-good provenance mode is invalid")
    evidence = {}
    shared_identity_keys = _LAST_GOOD_SHARED_IDENTITY_KEYS
    for name, identity in (
        ("WebUI", webui_identity),
        ("gateway", gateway_identity),
    ):
        label = f"last-good {name}"
        if (
            not isinstance(identity, dict)
            or isinstance(identity.get("selector_generation"), bool)
            or not isinstance(identity.get("selector_generation"), int)
            or identity["selector_generation"] <= 0
        ):
            raise ReleaseBuildError(f"{label} provenance is invalid")
        identity_selector_path = str(identity.get("selector_path") or "")
        _attest_expected_release_identity(
            identity,
            selector_path=identity_selector_path,
            label=label,
        )
        evidence[name.lower()] = MappingProxyType(
            {"identity": MappingProxyType(copy.deepcopy(identity))}
        )
    if journal_mode:
        for name, identity, journal_path, journal_sha256 in (
            ("WebUI", webui_identity, webui_origin_journal, webui_origin_sha256),
            ("gateway", gateway_identity, gateway_origin_journal, gateway_origin_sha256),
        ):
            label = f"last-good {name}"
            journal = _read_sealed_origin_journal(
                str(journal_path),
                expected_sha256=journal_sha256,
                transaction_id=identity.get("startup_transaction_id"),
                trusted_root=trusted_root,
                label=label,
            )
            if any(
                journal["expected_candidate_identity"].get(key) != identity.get(key)
                for key in _LAST_GOOD_ORIGIN_IDENTITY_KEYS
            ):
                raise ReleaseBuildError(f"{label} origin journal identity changed")
    else:
        receipt = _read_sealed_split_adoption_receipt(
            str(adoption_receipt),
            expected_sha256=adoption_receipt_sha256,
            trusted_root=trusted_root,
            webui_identity=webui_identity,
            gateway_identity=gateway_identity,
        )
        if receipt.get("schema") == "hermes.last_good_split_adoption.v2":
            shared_identity_keys = _LAST_GOOD_RUNTIME_SHARED_IDENTITY_KEYS
        evidence["provenance"] = MappingProxyType(
            {
                "kind": "live-split-adoption",
                "adoption_id": receipt["adoption_id"],
                "receipt_sha256": str(adoption_receipt_sha256),
            }
        )
    if any(
        webui_identity.get(key) != gateway_identity.get(key)
        for key in shared_identity_keys
    ):
        raise ReleaseBuildError("last-good shared runtime identity changed")
    return MappingProxyType(evidence)


def _load_cutover_plan(path: Path | str) -> dict:
    raw = _read_json_object(path, label="cutover plan")
    keys = set(raw)
    if (
        raw.get("version") != 1
        or not _CUTOVER_PLAN_REQUIRED.issubset(keys)
        or keys - _CUTOVER_PLAN_REQUIRED - _CUTOVER_PLAN_OPTIONAL
    ):
        raise ReleaseBuildError("cutover plan schema is invalid")
    origin_keys = keys.intersection(_LAST_GOOD_ORIGIN_JOURNAL_PLAN_KEYS)
    adoption_keys = keys.intersection(_LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS)
    if (
        origin_keys
        and origin_keys != _LAST_GOOD_ORIGIN_JOURNAL_PLAN_KEYS
    ) or (
        adoption_keys
        and adoption_keys != _LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS
    ):
        raise ReleaseBuildError("cutover plan provenance schema is invalid")
    if bool(origin_keys) == bool(adoption_keys):
        raise ReleaseBuildError("last-good provenance mode is invalid")
    transaction_id = str(raw.get("transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError("cutover plan transaction identity is invalid")
    plan = copy.deepcopy(raw)
    for key in _CUTOVER_PLAN_PATH_KEYS:
        if key in plan:
            plan[key] = str(_absolute_plan_path(plan[key], label=key))
    mutable_paths = plan.get("mutable_state_paths")
    if not isinstance(mutable_paths, list) or not mutable_paths:
        raise ReleaseBuildError("cutover plan mutable state paths are invalid")
    normalized_mutable = [
        str(_absolute_plan_path(value, label="mutable_state"))
        for value in mutable_paths
    ]
    if len(set(normalized_mutable)) != len(normalized_mutable):
        raise ReleaseBuildError("cutover plan mutable state paths are duplicated")
    for left in normalized_mutable:
        for right in normalized_mutable:
            if left != right and Path(left) in Path(right).parents:
                raise ReleaseBuildError(
                    "cutover plan mutable state paths overlap"
                )
    for key in (
        _CUTOVER_PLAN_PATH_KEYS.intersection(plan)
        - _CUTOVER_MUTABLE_REFERENCE_PATH_KEYS
    ):
        artifact = Path(plan[key])
        for raw_mutable in normalized_mutable:
            mutable = Path(raw_mutable)
            if (
                artifact == mutable
                or artifact in mutable.parents
                or mutable in artifact.parents
            ):
                raise ReleaseBuildError(
                    f"cutover artifact {key} overlaps mutable state"
                )
    plan["mutable_state_paths"] = normalized_mutable
    if "watchdog_state_file" in plan:
        watchdog_state = _absolute_plan_path(
            plan["watchdog_state_file"],
            label="watchdog_state_file",
        )
        if not any(
            watchdog_state == Path(mutable)
            or Path(mutable) in watchdog_state.parents
            for mutable in normalized_mutable
        ):
            raise ReleaseBuildError(
                "watchdog state is not covered by the mutable snapshot"
            )
        plan["watchdog_state_file"] = str(watchdog_state)
    domain = str(plan.get("launchd_domain") or "")
    if not re.fullmatch(r"(?:gui|user)/[1-9][0-9]*|system", domain):
        raise ReleaseBuildError("cutover plan launchd domain is invalid")
    label = str(plan.get("launchd_label") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,255}", label):
        raise ReleaseBuildError("cutover plan launchd label is invalid")
    try:
        listener_port = int(plan.get("listener_port"))
        timeout_seconds = float(plan.get("timeout_seconds", 120.0))
        interval_seconds = float(plan.get("interval_seconds", 0.25))
    except (TypeError, ValueError) as exc:
        raise ReleaseBuildError("cutover plan timing or port is invalid") from exc
    if not 1 <= listener_port <= 65535 or timeout_seconds <= 0 or interval_seconds <= 0:
        raise ReleaseBuildError("cutover plan timing or port is invalid")
    plan["listener_port"] = listener_port
    plan["timeout_seconds"] = timeout_seconds
    plan["interval_seconds"] = interval_seconds
    _validated_loopback_base_url(str(plan.get("base_url") or ""))
    configured_gateway = _BOOTSTRAP_GATEWAY_PLAN_KEYS.intersection(plan)
    if configured_gateway and configured_gateway != _BOOTSTRAP_GATEWAY_PLAN_KEYS:
        raise ReleaseBuildError("cutover plan gateway transaction is incomplete")
    if configured_gateway:
        gateway_domain = str(plan.get("gateway_launchd_domain") or "")
        gateway_label = str(plan.get("gateway_launchd_label") or "")
        try:
            gateway_port = int(plan.get("gateway_listener_port"))
        except (TypeError, ValueError) as exc:
            raise ReleaseBuildError("cutover plan gateway port is invalid") from exc
        if (
            not re.fullmatch(r"(?:gui|user)/[1-9][0-9]*|system", gateway_domain)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,255}", gateway_label)
            or not 1 <= gateway_port <= 65535
            or gateway_port == listener_port
        ):
            raise ReleaseBuildError("cutover plan gateway identity is invalid")
        gateway_health = urlsplit(str(plan.get("gateway_health_url") or ""))
        try:
            gateway_host_is_loopback = (
                gateway_health.hostname is not None
                and ipaddress.ip_address(gateway_health.hostname).is_loopback
            )
        except ValueError as exc:
            raise ReleaseBuildError("gateway health host is invalid") from exc
        if (
            gateway_health.scheme != "http"
            or not gateway_host_is_loopback
            or gateway_health.port != gateway_port
            or gateway_health.path != "/health"
            or gateway_health.username is not None
            or gateway_health.password is not None
            or gateway_health.query
            or gateway_health.fragment
        ):
            raise ReleaseBuildError("gateway health URL is not bound to its listener")
        plan["gateway_listener_port"] = gateway_port
    configured_watchdog = _BOOTSTRAP_WATCHDOG_PLAN_KEYS.intersection(plan)
    if configured_watchdog and configured_watchdog != _BOOTSTRAP_WATCHDOG_PLAN_KEYS:
        raise ReleaseBuildError("cutover plan watchdog transaction is incomplete")
    if configured_watchdog and not re.fullmatch(
        r"[0-9a-f]{64}", str(plan.get("watchdog_expected_sha256") or "")
    ):
        raise ReleaseBuildError("cutover plan watchdog identity is invalid")
    configured_watchdog_scheduler = (
        _BOOTSTRAP_WATCHDOG_SCHEDULER_PLAN_KEYS.intersection(plan)
    )
    if configured_watchdog_scheduler and (
        configured_watchdog_scheduler
        != _BOOTSTRAP_WATCHDOG_SCHEDULER_PLAN_KEYS
        or configured_watchdog != _BOOTSTRAP_WATCHDOG_PLAN_KEYS
        or plan.get("watchdog_scheduler_backend") != "hermes_internal"
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{2,255}",
            str(plan.get("watchdog_scheduler_job_id") or ""),
        )
        is None
    ):
        raise ReleaseBuildError(
            "cutover plan watchdog scheduler identity is invalid"
        )
    if configured_watchdog_scheduler:
        watchdog_registry = _absolute_plan_path(
            plan["watchdog_scheduler_registry"],
            label="watchdog_scheduler_registry",
        )
        if not any(
            watchdog_registry == Path(mutable)
            or Path(mutable) in watchdog_registry.parents
            for mutable in normalized_mutable
        ):
            raise ReleaseBuildError(
                "watchdog scheduler registry is not covered by the mutable snapshot"
            )
        plan["watchdog_scheduler_registry"] = str(watchdog_registry)
    configured_ingress = _BOOTSTRAP_INGRESS_GATE_PLAN_KEYS.intersection(plan)
    if (
        configured_ingress
        and configured_ingress != _BOOTSTRAP_INGRESS_GATE_PLAN_KEYS
    ):
        raise ReleaseBuildError("cutover plan ingress gate is incomplete")
    if configured_ingress and not re.fullmatch(
        r"[0-9a-f]{64}", str(plan.get("ingress_gate_expected_sha256") or "")
    ):
        raise ReleaseBuildError("cutover plan ingress gate identity is invalid")
    configured_boundary = _BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS.intersection(plan)
    if (
        configured_boundary
        and configured_boundary != _BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS
    ):
        raise ReleaseBuildError("cutover plan legacy frozen boundary is incomplete")
    if configured_boundary:
        process_ids = plan.get("synthetic_process_notification_ids")
        delegation_ids = plan.get("synthetic_async_delegation_ids")
        if (
            not isinstance(process_ids, list)
            or not process_ids
            or len(process_ids) != len(set(process_ids))
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"proc_[A-Za-z0-9_-]{1,120}", value) is None
                for value in process_ids
            )
            or not isinstance(delegation_ids, list)
            or not delegation_ids
            or len(delegation_ids) != len(set(delegation_ids))
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"deleg_[A-Za-z0-9_-]{1,120}", value) is None
                for value in delegation_ids
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(
                    plan.get(
                        "synthetic_process_notifications_expected_sha256"
                    )
                    or ""
                ),
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(
                    plan.get(
                        "synthetic_async_delegations_expected_sha256"
                    )
                    or ""
                ),
            )
        ):
            raise ReleaseBuildError(
                "cutover plan synthetic completion identity is invalid"
            )
        state_paths = [
            Path(plan["legacy_state_db"]),
            Path(plan["synthetic_process_notifications_path"]),
            Path(plan["synthetic_async_delegations_path"]),
        ]
        mutable = [Path(value) for value in normalized_mutable]
        if any(
            not any(
                state_path == mutable_path
                or mutable_path in state_path.parents
                for mutable_path in mutable
            )
            for state_path in state_paths
        ):
            raise ReleaseBuildError(
                "legacy boundary state is not covered by the mutable snapshot"
            )
        process_path = Path(plan["synthetic_process_notifications_path"])
        delegation_path = Path(plan["synthetic_async_delegations_path"])
        quarantine = Path(plan["synthetic_quarantine_root"])
        if (
            process_path == delegation_path
            or process_path.parent != delegation_path.parent
            or any(
                quarantine == mutable_path
                or quarantine in mutable_path.parents
                or mutable_path in quarantine.parents
                for mutable_path in mutable
            )
        ):
            raise ReleaseBuildError(
                "cutover plan synthetic quarantine boundary is invalid"
            )
        plan["synthetic_process_notification_ids"] = list(process_ids)
        plan["synthetic_async_delegation_ids"] = list(delegation_ids)
    candidate = _read_json_object(
        plan["expected_candidate_identity_json"],
        label="expected candidate identity",
    )
    last_good = _read_json_object(
        plan["last_good_identity_json"],
        label="last-good identity",
    )
    last_good_gateway = _read_json_object(
        plan["last_good_gateway_identity_json"],
        label="last-good gateway identity",
    )
    if (
        candidate.get("startup_fenced") is not True
        or candidate.get("startup_transaction_id") != transaction_id
        or candidate.get("launchd_label") != label
        or not str(candidate.get("build_id") or "")
    ):
        raise ReleaseBuildError("cutover plan candidate identity is invalid")
    if not str(last_good.get("build_id") or ""):
        raise ReleaseBuildError("cutover plan last-good identity is invalid")
    _attest_expected_release_identity(
        candidate,
        selector_path=plan["selector_path"],
        label="candidate",
    )
    provenance_arguments = (
        {
            "webui_origin_journal": plan["last_good_origin_journal"],
            "webui_origin_sha256": plan["last_good_origin_journal_sha256"],
            "gateway_origin_journal": plan["last_good_gateway_origin_journal"],
            "gateway_origin_sha256": plan[
                "last_good_gateway_origin_journal_sha256"
            ],
        }
        if origin_keys
        else {
            "adoption_receipt": plan["last_good_split_adoption_receipt"],
            "adoption_receipt_sha256": plan[
                "last_good_split_adoption_receipt_sha256"
            ],
        }
    )
    attestation = _attest_last_good_identity_split(
        webui_identity=last_good,
        gateway_identity=last_good_gateway,
        trusted_root=Path(plan["transaction_journal"]).parent,
        selector_path=plan["selector_path"],
        **provenance_arguments,
    )
    plan["expected_candidate_identity"] = candidate
    plan["last_good_identity"] = last_good
    plan["last_good_gateway_identity"] = last_good_gateway
    plan["last_good_origin_attestation"] = attestation
    return plan


def _atomic_copy_file(
    source: Path | str,
    destination: Path | str,
    *,
    expected_sha256: str | None = None,
    mode: int | None = None,
) -> dict:
    source_path = Path(source)
    destination_path = Path(destination)
    if (
        not source_path.is_absolute()
        or Path(os.path.abspath(source_path)) != source_path
        or source_path.is_symlink()
        or not source_path.is_file()
    ):
        raise ReleaseBuildError("atomic copy source is invalid")
    actual_sha256 = sha256_file(source_path)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ReleaseBuildError("atomic copy source hash changed")
    if (
        not destination_path.is_absolute()
        or Path(os.path.abspath(destination_path)) != destination_path
        or destination_path.is_symlink()
    ):
        raise ReleaseBuildError("atomic copy destination is invalid")
    parent = _prepare_release_root(destination_path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        dir=parent,
    )
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            with source_path.open("rb") as source_handle:
                shutil.copyfileobj(
                    source_handle,
                    handle,
                    length=8 * 1024 * 1024,
                )
            handle.flush()
            os.fchmod(
                handle.fileno(),
                mode if mode is not None else stat.S_IMODE(source_path.stat().st_mode),
            )
            os.fsync(handle.fileno())
        os.replace(temp_name, destination_path)
        replaced = True
        _fsync_directory(parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    if sha256_file(destination_path) != actual_sha256:
        raise ReleaseBuildError("atomic copy destination verification failed")
    return {"path": str(destination_path), "sha256": actual_sha256}


def _state_tree_receipt(path: Path, *, allow_absent: bool = False) -> dict:
    if not path.exists() and not path.is_symlink():
        if allow_absent:
            encoded = b'[{"kind":"absent","path":"."}]'
            return {
                "kind": "absent",
                "tree_sha256": hashlib.sha256(encoded).hexdigest(),
                "rows": [],
            }
        raise ReleaseBuildError("mutable state target is missing")
    if path.is_symlink():
        raise ReleaseBuildError("mutable state target is symlinked")
    rows: list[dict[str, object]] = []
    if path.is_file():
        rows.append(
            {
                "path": ".",
                "kind": "file",
                "mode": stat.S_IMODE(path.stat().st_mode),
                "sha256": sha256_file(path),
            }
        )
        kind = "file"
    elif path.is_dir():
        kind = "directory"
        rows.append(
            {
                "path": ".",
                "kind": "directory",
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        )
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise ReleaseBuildError("mutable state contains a symlink")
            relative = child.relative_to(path).as_posix()
            opened = child.stat()
            if child.is_dir():
                rows.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "mode": stat.S_IMODE(opened.st_mode),
                    }
                )
            elif child.is_file():
                rows.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": stat.S_IMODE(opened.st_mode),
                        "sha256": sha256_file(child),
                    }
                )
            else:
                raise ReleaseBuildError("mutable state contains a special file")
    else:
        raise ReleaseBuildError("mutable state target is not regular")
    # Modes are restore metadata, not snapshot-content identity.  Snapshot files
    # are deliberately sealed read-only after copying, so including modes in the
    # tree digest would make a valid sealed snapshot fail its own verification.
    # The full rows (including original modes) remain signed by the manifest and
    # are compared exactly after a restore.
    content_rows = [
        {key: value for key, value in row.items() if key != "mode"}
        for row in rows
    ]
    encoded = json.dumps(
        content_rows,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "kind": kind,
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "rows": rows,
    }


def create_state_snapshot(
    targets: list[str],
    *,
    snapshot_root: Path | str,
    manifest_path: Path | str,
    snapshot_id: str,
) -> dict:
    """Freeze exact mutable files/directories into a content-verified snapshot."""
    metadata_name = ".snapshot-metadata.json"
    root = _absolute_plan_path(snapshot_root, label="snapshot_root")
    manifest = _absolute_plan_path(manifest_path, label="snapshot_manifest")
    if not _TRANSACTION_ID.fullmatch(str(snapshot_id or "")):
        raise ReleaseBuildError("state snapshot identity is invalid")
    if manifest.exists():
        existing = _read_json_object(manifest, label="state snapshot manifest")
        existing_sha = sha256_file(manifest)
        if existing.get("snapshot_id") != snapshot_id:
            raise ReleaseBuildError("state snapshot identity changed")
        verify_state_snapshot(existing, manifest_sha256=existing_sha, live=False)
        return {
            "status": "created",
            "state_snapshot_id": snapshot_id,
            "state_snapshot_sha256": existing_sha,
            "manifest_path": str(manifest),
        }
    _prepare_release_root(root.parent)
    if not isinstance(targets, list) or not targets:
        raise ReleaseBuildError("mutable state snapshot targets are invalid")
    normalized_targets = [
        _absolute_plan_path(target, label="mutable_state") for target in targets
    ]
    if len(set(normalized_targets)) != len(normalized_targets):
        raise ReleaseBuildError("mutable state snapshot targets contain duplicates")
    entries: list[dict[str, object]] = []
    encoded: bytes
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ReleaseBuildError("state snapshot root is invalid")
        metadata_path = root / metadata_name
        metadata = _read_json_object(
            metadata_path,
            label="state snapshot recovery metadata",
        )
        encoded = (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if metadata_path.read_bytes() != encoded:
            raise ReleaseBuildError("state snapshot recovery metadata is not canonical")
        if (
            metadata.get("version") != 1
            or metadata.get("snapshot_id") != snapshot_id
            or metadata.get("snapshot_root") != str(root)
            or not isinstance(metadata.get("entries"), list)
        ):
            raise ReleaseBuildError("orphaned state snapshot metadata is invalid")
        entries = copy.deepcopy(metadata["entries"])
        if [entry.get("target") for entry in entries] != [
            str(target) for target in normalized_targets
        ]:
            raise ReleaseBuildError("orphaned state snapshot targets changed")
        expected_entries = {
            f"entry-{index:04d}"
            for index, entry in enumerate(entries)
            if entry.get("kind") != "absent"
        }
        expected_entries.add(metadata_name)
        if {child.name for child in root.iterdir()} != expected_entries:
            raise ReleaseBuildError("orphaned state snapshot root is incomplete")
        verify_state_snapshot(
            metadata,
            manifest_sha256=hashlib.sha256(encoded).hexdigest(),
            live=False,
        )
    else:
        stage = root.with_name(f".{root.name}.{secrets.token_hex(8)}.tmp")
        stage.mkdir(mode=0o700)
        published = False
        try:
            for index, target in enumerate(normalized_targets):
                receipt = _state_tree_receipt(target, allow_absent=True)
                snapshot_target = stage / f"entry-{index:04d}"
                if receipt["kind"] == "absent":
                    pass
                elif receipt["kind"] == "file":
                    shutil.copy2(target, snapshot_target, follow_symlinks=False)
                else:
                    shutil.copytree(target, snapshot_target, symlinks=False)
                if receipt["kind"] != "absent":
                    copied = _state_tree_receipt(snapshot_target)
                    if copied["tree_sha256"] != receipt["tree_sha256"]:
                        raise ReleaseBuildError("state snapshot copy verification failed")
                entries.append(
                    {
                        "target": str(target),
                        "snapshot_relative_path": snapshot_target.name,
                        "kind": receipt["kind"],
                        "tree_sha256": receipt["tree_sha256"],
                        "rows": receipt["rows"],
                    }
                )
            payload = {
                "version": 1,
                "metadata_contract": "path-kind-content-mode",
                "snapshot_id": snapshot_id,
                "snapshot_root": str(root),
                "entries": entries,
            }
            encoded = (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            metadata_path = stage / metadata_name
            with metadata_path.open("xb") as metadata_handle:
                metadata_handle.write(encoded)
                metadata_handle.flush()
                os.fchmod(metadata_handle.fileno(), 0o400)
                os.fsync(metadata_handle.fileno())
            for child in sorted(stage.rglob("*"), reverse=True):
                if child == metadata_path:
                    continue
                os.chmod(child, 0o555 if child.is_dir() else 0o444)
            os.chmod(stage, 0o555)
            os.replace(stage, root)
            published = True
            _fsync_directory(root.parent)
        finally:
            if not published and stage.exists():
                _remove_staging_tree(stage)
    manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{manifest.name}.",
        dir=manifest.parent,
    )
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), 0o400)
            os.fsync(handle.fileno())
        os.replace(temp_name, manifest)
        replaced = True
        _fsync_directory(manifest.parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return {
        "status": "created",
        "state_snapshot_id": snapshot_id,
        "state_snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "manifest_path": str(manifest),
    }


def verify_state_snapshot(
    manifest: dict,
    *,
    manifest_sha256: str,
    live: bool,
) -> dict:
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or manifest.get("metadata_contract") != "path-kind-content-mode"
        or not _TRANSACTION_ID.fullmatch(str(manifest.get("snapshot_id") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest_sha256 or ""))
        or not isinstance(manifest.get("entries"), list)
        or not manifest["entries"]
    ):
        raise ReleaseBuildError("state snapshot manifest is invalid")
    root = _absolute_plan_path(manifest.get("snapshot_root"), label="snapshot_root")
    seen_targets: set[Path] = set()
    seen_relative: set[str] = set()
    for index, entry in enumerate(manifest["entries"]):
        if not isinstance(entry, dict):
            raise ReleaseBuildError("state snapshot entry is invalid")
        target = _absolute_plan_path(entry.get("target"), label="mutable_state")
        if target in seen_targets:
            raise ReleaseBuildError("state snapshot target is duplicated")
        seen_targets.add(target)
        relative = str(entry.get("snapshot_relative_path") or "")
        if relative != f"entry-{index:04d}" or relative in seen_relative:
            raise ReleaseBuildError("state snapshot entry path is invalid")
        seen_relative.add(relative)
        snapshot_path = root / relative
        if entry.get("kind") == "absent":
            if entry.get("rows") != []:
                raise ReleaseBuildError("state snapshot tombstone metadata is invalid")
            if (target if live else snapshot_path).exists() or (
                target if live else snapshot_path
            ).is_symlink():
                raise ReleaseBuildError("state snapshot tombstone does not match")
            continue
        receipt = _state_tree_receipt(target if live else snapshot_path)
        if (
            receipt["kind"] != entry.get("kind")
            or receipt["tree_sha256"] != entry.get("tree_sha256")
        ):
            raise ReleaseBuildError("state snapshot content does not match manifest")
        if live:
            if receipt["rows"] != entry.get("rows"):
                raise ReleaseBuildError("restored state metadata does not match manifest")
        else:
            expected_snapshot_rows = [
                {
                    **row,
                    "mode": 0o555 if row.get("kind") == "directory" else 0o444,
                }
                for row in entry.get("rows", [])
                if isinstance(row, dict)
            ]
            if receipt["rows"] != expected_snapshot_rows:
                raise ReleaseBuildError("state snapshot is not sealed")
    return {
        "status": "verified",
        "state_snapshot_id": manifest["snapshot_id"],
        "state_snapshot_sha256": manifest_sha256,
    }


def _read_verified_state_snapshot(
    manifest_path: Path | str,
    *,
    expected_snapshot_id: str,
    expected_manifest_sha256: str,
    live: bool,
) -> tuple[dict, dict]:
    path = _absolute_plan_path(manifest_path, label="snapshot_manifest")
    if sha256_file(path) != expected_manifest_sha256:
        raise ReleaseBuildError("state snapshot manifest hash changed")
    manifest = _read_json_object(path, label="state snapshot manifest")
    if manifest.get("snapshot_id") != expected_snapshot_id:
        raise ReleaseBuildError("state snapshot manifest identity changed")
    return manifest, verify_state_snapshot(
        manifest,
        manifest_sha256=expected_manifest_sha256,
        live=live,
    )


def restore_state_snapshot_from_manifest(
    manifest_path: Path | str,
    *,
    expected_snapshot_id: str,
    expected_manifest_sha256: str,
) -> dict:
    manifest, _receipt = _read_verified_state_snapshot(
        manifest_path,
        expected_snapshot_id=expected_snapshot_id,
        expected_manifest_sha256=expected_manifest_sha256,
        live=False,
    )
    root = Path(manifest["snapshot_root"])
    snapshot_id = str(manifest["snapshot_id"])
    for index, entry in enumerate(manifest["entries"]):
        target = Path(entry["target"])
        source = root / entry["snapshot_relative_path"]
        parent = target.parent
        if parent.resolve(strict=True) != parent or target.is_symlink():
            raise ReleaseBuildError("mutable state restore target is unsafe")
        stage = parent / (
            f".{target.name}.hermes-restore-{snapshot_id}-{index:04d}.stage"
        )
        backup = parent / (
            f".{target.name}.hermes-restore-{snapshot_id}-{index:04d}.replaced"
        )
        if stage.is_symlink() or backup.is_symlink():
            raise ReleaseBuildError("mutable state restore recovery path is unsafe")
        if entry["kind"] == "absent":
            if target.exists():
                if backup.exists():
                    raise ReleaseBuildError(
                        "mutable tombstone restore has concurrent target state"
                    )
                os.replace(target, backup)
                _fsync_directory(parent)
            if backup.exists():
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
                _fsync_directory(parent)
            continue
        if entry["kind"] == "file":
            mode = int(entry["rows"][0]["mode"])
            _atomic_copy_file(source, target, mode=mode)
            continue
        if stage.exists():
            if not stage.is_dir():
                raise ReleaseBuildError("mutable state restore stage is invalid")
            _remove_staging_tree(stage)
        shutil.copytree(source, stage, symlinks=False)
        for row in entry["rows"]:
            restored_path = stage if row["path"] == "." else stage / row["path"]
            os.chmod(restored_path, int(row["mode"]))
        if backup.exists():
            if target.exists():
                current = _state_tree_receipt(target)
                if (
                    current["kind"] == entry["kind"]
                    and current["tree_sha256"] == entry["tree_sha256"]
                    and current["rows"] == entry["rows"]
                ):
                    _remove_staging_tree(stage)
                else:
                    raise ReleaseBuildError(
                        "mutable state changed during restore recovery"
                    )
            else:
                os.replace(stage, target)
                _fsync_directory(parent)
        else:
            if target.exists():
                os.replace(target, backup)
                _fsync_directory(parent)
            os.replace(stage, target)
            _fsync_directory(parent)
        if backup.exists():
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()
            _fsync_directory(parent)
    _read_verified_state_snapshot(
        manifest_path,
        expected_snapshot_id=expected_snapshot_id,
        expected_manifest_sha256=expected_manifest_sha256,
        live=True,
    )
    return {
        "status": "restored",
        "state_snapshot_id": expected_snapshot_id,
        "state_snapshot_sha256": expected_manifest_sha256,
    }


def _run_launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env={
                key: value
                for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
                if (value := os.environ.get(key)) is not None
            },
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("launchd cutover command failed") from exc


def _launchd_target(plan: dict) -> str:
    return f"{plan['launchd_domain']}/{plan['launchd_label']}"


def _launchd_pid(plan: dict) -> int:
    completed = _run_launchctl("print", _launchd_target(plan))
    matches = re.findall(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", completed.stdout)
    if len(matches) != 1 or int(matches[0]) <= 1:
        raise DrainIdentityMismatch("launchd job PID is unavailable or ambiguous")
    return int(matches[0])


def _bootout_launchd_job(plan: dict, *, required: bool) -> dict:
    completed = _run_launchctl(
        "bootout",
        _launchd_target(plan),
        check=False,
    )
    if completed.returncode != 0 and required:
        raise ReleaseBuildError("launchd job could not be stopped")
    return {"status": "stopped" if completed.returncode == 0 else "not-loaded"}


def _bootstrap_launchd_job(plan: dict, plist_path: Path | str) -> dict:
    plist = _absolute_plan_path(plist_path, label="installed_plist")
    return _bootstrap_launchd_job_with_retry(
        plan,
        plist,
        gateway=False,
    )


def _listener_pid(port: int) -> int:
    try:
        completed = subprocess.run(
            [
                "lsof",
                "-nP",
                f"-iTCP:{int(port)}",
                "-sTCP:LISTEN",
                "-t",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerProbeAmbiguous("listener PID probe failed") from exc
    rows = [row.strip() for row in completed.stdout.splitlines() if row.strip()]
    if (
        completed.returncode == 1
        and not rows
        and not completed.stderr.strip()
    ):
        raise ListenerAbsent(f"no listener owns TCP port {int(port)}")
    if (
        completed.returncode != 0
        or completed.stderr.strip()
        or any(not row.isdigit() for row in rows)
    ):
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise ListenerProbeAmbiguous(f"listener PID probe failed{suffix}")
    pids = {int(row) for row in rows}
    if len(pids) != 1:
        raise ListenerProbeAmbiguous(
            "listener PID is unavailable or ambiguous"
        )
    pid = next(iter(pids))
    if pid <= 1:
        raise ListenerProbeAmbiguous("listener PID is invalid")
    return pid


def _listener_pid_or_none(port: int) -> int | None:
    try:
        return _listener_pid(port)
    except ListenerAbsent:
        return None


def _job_target(plan: dict, *, gateway: bool = False) -> str:
    prefix = "gateway_" if gateway else ""
    return f"{plan[f'{prefix}launchd_domain']}/{plan[f'{prefix}launchd_label']}"


def _launchd_service_override_receipt(
    plan: dict,
    *,
    gateway: bool,
) -> dict:
    prefix = "gateway_" if gateway else ""
    domain = str(plan[f"{prefix}launchd_domain"])
    label = str(plan[f"{prefix}launchd_label"])
    target = _job_target(plan, gateway=gateway)
    if target != f"{domain}/{label}" or not label or any(
        character in label for character in "\"\r\n"
    ):
        raise ReleaseBuildError("launchd service override target is invalid")
    completed = _run_launchctl("print-disabled", domain)
    matches = re.findall(
        rf'(?m)^[ \t]*"{re.escape(label)}"[ \t]*=>[ \t]*'
        r'([^\r\n]*?)[ \t]*$',
        completed.stdout,
    )
    if len(matches) > 1:
        raise DrainIdentityMismatch(
            "launchd service override state is ambiguous"
        )
    if matches:
        override = matches[0].strip()
        if override not in {"enabled", "disabled"}:
            raise DrainIdentityMismatch(
                "launchd service override state is invalid"
            )
    else:
        if re.search(
            rf'(?m)^[ \t]*"{re.escape(label)}"(?:[ \t]|$)',
            completed.stdout,
        ):
            raise DrainIdentityMismatch(
                "launchd service override state is invalid"
            )
        override = "absent"
    return {
        "target": target,
        "domain": domain,
        "label": label,
        "disabled": override == "disabled",
        "override": override,
    }


def _set_launchd_service_disabled(
    plan: dict,
    control: dict,
    *,
    disabled: bool,
) -> dict:
    if not isinstance(control, dict):
        raise ReleaseBuildError(
            "legacy gateway launchd restart control is invalid"
        )
    initial = control.get("initial")
    expected = _job_target(plan, gateway=True)
    if (
        control.get("status") != "prepared"
        or control.get("restore_semantics") != "enabled"
        or not isinstance(initial, dict)
        or initial.get("target") != expected
        or initial.get("disabled") is not False
    ):
        raise ReleaseBuildError(
            "legacy gateway launchd restart control is invalid"
        )
    before = _launchd_service_override_receipt(plan, gateway=True)
    if before["target"] != expected:
        raise DrainIdentityMismatch(
            "legacy gateway launchd restart target changed"
        )
    if before["disabled"] != disabled:
        _run_launchctl(
            "disable" if disabled else "enable",
            expected,
        )
    after = _launchd_service_override_receipt(plan, gateway=True)
    if after["target"] != expected or after["disabled"] != disabled:
        raise DrainIdentityMismatch(
            "legacy gateway launchd restart state did not change exactly"
        )
    return {
        "status": "disabled" if disabled else "enabled",
        "target": expected,
        "before": before,
        "after": after,
    }


def _job_pid(plan: dict, *, gateway: bool = False) -> int | None:
    completed = _run_launchctl("print", _job_target(plan, gateway=gateway), check=False)
    if completed.returncode != 0:
        return None
    matches = re.findall(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", completed.stdout)
    if len(matches) != 1 or int(matches[0]) <= 1:
        raise DrainIdentityMismatch("launchd job PID is unavailable or ambiguous")
    return int(matches[0])


def _bootout_job(plan: dict, *, gateway: bool, required: bool) -> dict:
    completed = _run_launchctl(
        "bootout",
        _job_target(plan, gateway=gateway),
        check=False,
    )
    if completed.returncode != 0 and required:
        raise ReleaseBuildError("launchd job could not be stopped")
    return {
        "status": "stopped" if completed.returncode == 0 else "not-loaded",
        "target": _job_target(plan, gateway=gateway),
    }


def _bootstrap_job(plan: dict, plist_path: Path | str, *, gateway: bool) -> dict:
    label = "gateway_installed_plist" if gateway else "installed_plist"
    plist = _absolute_plan_path(plist_path, label=label)
    return _bootstrap_launchd_job_with_retry(
        plan,
        plist,
        gateway=gateway,
    )


def _bootstrap_launchd_job_with_retry(
    plan: dict,
    plist: Path,
    *,
    gateway: bool,
) -> dict:
    domain_key = "gateway_launchd_domain" if gateway else "launchd_domain"
    target = _job_target(plan, gateway=gateway)
    for attempt in range(1, 21):
        completed = _run_launchctl(
            "bootstrap",
            str(plan[domain_key]),
            str(plist),
            check=False,
        )
        if completed.returncode == 0:
            return {
                "status": "started",
                "target": target,
                "stdout": completed.stdout.strip(),
                "attempts": attempt,
            }
        if completed.returncode != 5:
            raise ReleaseBuildError("launchd cutover command failed")
        # launchd tears a booted-out job down asynchronously.  A bootstrap
        # can therefore return its transient I/O error while ``print`` still
        # reports the old service (or while the absence response is still
        # settling).  Probe the exact, expected absent receipt for a bounded
        # window before retrying.  We remain fail-closed: a service that does
        # not become unambiguously absent still aborts the cutover.
        _wait_for_launchd_job_absent(plan, gateway=gateway)
        if attempt == 20:
            raise ReleaseBuildError(
                "launchd cutover command failed after teardown retry"
            )
        time.sleep(0.25)
    raise AssertionError("unreachable launchd bootstrap retry state")


def _require_launchd_job_absent(
    plan: dict,
    *,
    gateway: bool,
) -> dict:
    target = _job_target(plan, gateway=gateway)
    completed = _run_launchctl("print", target, check=False)
    domain, separator, label = target.partition("/")
    if domain == "gui":
        user_id, separator, label = label.partition("/")
        expected_stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{user_id}\n"
        )
    elif domain == "system" and separator:
        expected_stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for system\n'
        )
    else:
        raise ReleaseBuildError("launchd job target is invalid")
    if (
        completed.returncode != 113
        or completed.stdout != ""
        or completed.stderr != expected_stderr
    ):
        if completed.returncode == 5:
            raise LaunchdAbsenceTransient(
                "launchd service database is still settling"
            )
        raise DrainIdentityMismatch(
            "launchd job absence probe is ambiguous"
        )
    return {
        "status": "absent",
        "target": target,
        "returncode": completed.returncode,
    }


def _wait_for_launchd_job_absent(
    plan: dict,
    *,
    gateway: bool,
    timeout_seconds: float = 5.0,
    interval_seconds: float = 0.1,
) -> dict:
    """Wait for launchd to publish an exact absent receipt after bootout.

    ``launchctl bootstrap`` may report its transient code 5 before the
    service database has finished removing the old instance.  Treating the
    first non-absent probe as permanent ambiguity turns that normal teardown
    race into an unnecessary failed rollback.  This helper only retries the
    read; it never accepts a loaded or malformed response as absent.
    """
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("launchd absence timing values are invalid")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            return _require_launchd_job_absent(plan, gateway=gateway)
        except LaunchdAbsenceTransient as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))
    raise DrainIdentityMismatch(
        f"launchd job absence probe did not settle: {last_error}"
    )


def _ps_value(pid: int, field: str) -> str:
    if field not in {"command", "comm", "state"}:
        raise ValueError("unsupported process field")
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(int(pid)), "-o", f"{field}="],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DrainIdentityMismatch("process identity probe failed") from exc
    value = completed.stdout.strip()
    if not value or "\x00" in value:
        raise DrainIdentityMismatch("process identity probe is empty")
    return value


def _process_cwd(pid: int) -> Path:
    try:
        completed = subprocess.run(
            ["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DrainIdentityMismatch("process working-directory probe failed") from exc
    names = [line[1:] for line in completed.stdout.splitlines() if line.startswith("n")]
    if len(names) != 1:
        raise DrainIdentityMismatch("process working directory is ambiguous")
    cwd = Path(names[0])
    try:
        resolved = cwd.resolve(strict=True)
    except OSError as exc:
        raise DrainIdentityMismatch("process working directory is missing") from exc
    if resolved != cwd or not cwd.is_dir():
        raise DrainIdentityMismatch("process working directory is not canonical")
    return cwd


def _process_executable_path(pid: int) -> Path:
    if int(pid) <= 1:
        raise DrainIdentityMismatch("process executable PID is invalid")
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidpath = libproc.proc_pidpath
            proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            proc_pidpath.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            returned = proc_pidpath(int(pid), buffer, len(buffer))
            if returned <= 0 or returned >= len(buffer):
                raise DrainIdentityMismatch("process executable path is unavailable")
            raw = buffer.raw[:returned].split(b"\x00", 1)[0]
            path = Path(raw.decode("utf-8"))
        except (AttributeError, OSError, UnicodeDecodeError) as exc:
            raise DrainIdentityMismatch("process executable probe failed") from exc
    else:
        try:
            path = Path(os.readlink(f"/proc/{int(pid)}/exe"))
        except OSError as exc:
            raise DrainIdentityMismatch("process executable probe failed") from exc
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DrainIdentityMismatch("process executable path is missing") from exc
    if not resolved.is_file():
        raise DrainIdentityMismatch("process executable is not a file")
    return resolved


_PYTHON_KERNEL_EXECUTABLE_CACHE: dict[tuple[str, str], Path] = {}


def _python_kernel_executable_path(interpreter: Path | str) -> Path:
    configured = Path(interpreter)
    try:
        resolved_interpreter = configured.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("managed Python interpreter is missing") from exc
    if not resolved_interpreter.is_file() or not os.access(configured, os.X_OK):
        raise ReleaseBuildError("managed Python interpreter is invalid")
    key = (str(resolved_interpreter), sha256_file(resolved_interpreter))
    cached = _PYTHON_KERNEL_EXECUTABLE_CACHE.get(key)
    if cached is not None:
        return cached
    if sys.platform != "darwin":
        _PYTHON_KERNEL_EXECUTABLE_CACHE[key] = resolved_interpreter
        return resolved_interpreter
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                str(configured),
                "-I",
                "-S",
                "-c",
                "import time; time.sleep(30)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env={
                key: value
                for key in ("HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")
                if (value := os.environ.get(key)) is not None
            },
        )
        launched_at = time.monotonic()
        deadline = launched_at + 5
        last_error: Exception | None = None
        kernel_path: Path | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ReleaseBuildError(
                    "managed Python executable probe exited early"
                )
            if time.monotonic() - launched_at < 0.2:
                time.sleep(0.01)
                continue
            try:
                kernel_path = _process_executable_path(process.pid)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.01)
        if kernel_path is None:
            raise ReleaseBuildError(
                f"managed Python kernel executable probe timed out: {last_error}"
            )
    except OSError as exc:
        raise ReleaseBuildError("managed Python executable probe failed") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise ReleaseBuildError(
                    "managed Python executable probe could not be reaped"
                ) from exc
    _PYTHON_KERNEL_EXECUTABLE_CACHE[key] = kernel_path
    return kernel_path


def _file_identity_receipt(path: Path | str, *, follow_symlink: bool = True) -> dict:
    configured = Path(path)
    if not configured.is_absolute() or Path(os.path.abspath(configured)) != configured:
        raise ReleaseBuildError("file identity path is invalid")
    try:
        opened = configured.lstat()
        resolved = configured.resolve(strict=True) if follow_symlink else configured
        resolved_stat = resolved.stat()
    except OSError as exc:
        raise ReleaseBuildError("file identity path is unavailable") from exc
    if follow_symlink and not stat.S_ISREG(resolved_stat.st_mode):
        raise ReleaseBuildError("file identity target is not regular")
    receipt = {
        "path": str(configured),
        "lstat_mode": stat.S_IMODE(opened.st_mode),
        "lstat_uid": opened.st_uid,
        "lstat_size": opened.st_size,
    }
    if configured.is_symlink():
        receipt["link_target"] = os.readlink(configured)
    if follow_symlink:
        receipt.update(
            {
                "resolved_path": str(resolved),
                "resolved_mode": stat.S_IMODE(resolved_stat.st_mode),
                "resolved_uid": resolved_stat.st_uid,
                "resolved_size": resolved_stat.st_size,
                "sha256": sha256_file(resolved),
            }
        )
    return receipt


def _source_patch_receipt(cwd: Path) -> dict:
    try:
        repo = Path(str(_run_git(cwd, "rev-parse", "--show-toplevel")).strip())
        repo = repo.resolve(strict=True)
    except (OSError, ReleaseBuildError) as exc:
        manifest_path = cwd / release_selector.MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ReleaseBuildError(
                "legacy WebUI source is not a Git worktree"
            ) from exc
        manifest_sha256 = sha256_file(manifest_path)
        try:
            identity = release_selector.verify_release(
                cwd,
                release_root=cwd.parent,
                expected_manifest_sha256=manifest_sha256,
                selector_path=None,
                verify_selector_identity=False,
            )
        except release_selector.SelectorError as verify_exc:
            raise ReleaseBuildError(
                "managed WebUI source identity is invalid"
            ) from verify_exc
        return {
            "kind": "managed-immutable-release",
            "path": str(cwd),
            "build_id": identity["build_id"],
            "manifest_sha256": manifest_sha256,
            "commit": identity["commit"],
            "tree": identity["tree"],
        }
    if cwd != repo and repo not in cwd.parents:
        raise ReleaseBuildError("legacy WebUI source root is unrelated to process cwd")
    head = str(_run_git(repo, "rev-parse", "HEAD^{commit}")).strip()
    tree = str(_run_git(repo, "rev-parse", "HEAD^{tree}")).strip()
    status = bytes(
        _run_git(
            repo,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            binary=True,
        )
    )
    unstaged = bytes(_run_git(repo, "diff", "--binary", "HEAD", binary=True))
    staged = bytes(_run_git(repo, "diff", "--binary", "--cached", "HEAD", binary=True))
    untracked_raw = bytes(
        _run_git(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            binary=True,
        )
    )
    untracked: list[dict[str, object]] = []
    for raw_relative in [item for item in untracked_raw.split(b"\0") if item]:
        try:
            relative = raw_relative.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseBuildError("legacy source has a non-UTF8 untracked path") from exc
        safe = _safe_archive_path(relative)
        candidate = repo.joinpath(*safe.parts)
        opened = candidate.lstat()
        if stat.S_ISREG(opened.st_mode):
            untracked.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(opened.st_mode),
                    "sha256": sha256_file(candidate),
                }
            )
        elif stat.S_ISLNK(opened.st_mode):
            untracked.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": os.readlink(candidate),
                }
            )
        else:
            raise ReleaseBuildError("legacy source has an unsafe untracked entry")
    return {
        "repo": str(repo),
        "cwd": str(cwd),
        "head": head,
        "tree": tree,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "unstaged_patch_sha256": hashlib.sha256(unstaged).hexdigest(),
        "staged_patch_sha256": hashlib.sha256(staged).hexdigest(),
        "untracked": untracked,
    }


def _read_allowlisted_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ReleaseBuildError("legacy routing dotenv is unsafe")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseBuildError("legacy routing dotenv is unreadable") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key not in _ROUTING_ENV_KEYS:
            continue
        if key in values:
            raise ReleaseBuildError("legacy routing dotenv has duplicate keys")
        parsed = value.strip().strip('"').strip("'")
        if not parsed or "\x00" in parsed or "\n" in parsed:
            raise ReleaseBuildError("legacy routing dotenv value is invalid")
        values[key] = parsed
    return values


def _discover_routing_environment(plan: dict, plist: dict, cwd: Path) -> dict[str, str]:
    inherited = plist.get("EnvironmentVariables")
    inherited = inherited if isinstance(inherited, dict) else {}
    values = {
        key: str(inherited[key])
        for key in _ROUTING_ENV_KEYS
        if key in inherited and str(inherited[key]).strip()
    }
    preserve = str(inherited.get("HERMES_WEBUI_PRESERVE_ENV") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    dotenv = _read_allowlisted_dotenv(cwd / ".env")
    if preserve:
        values = {**dotenv, **values}
    else:
        values.update(dotenv)
    parsed_url = urlsplit(str(plan["base_url"]))
    values.setdefault("HERMES_WEBUI_HOST", str(parsed_url.hostname or "127.0.0.1"))
    values.setdefault("HERMES_WEBUI_PORT", str(parsed_url.port or plan["listener_port"]))
    if set(values) != _ROUTING_ENV_KEYS:
        raise ReleaseBuildError("legacy provider/model routing is incomplete")
    try:
        routed_port = int(values["HERMES_WEBUI_PORT"])
    except ValueError as exc:
        raise ReleaseBuildError("legacy routing port is invalid") from exc
    if routed_port != int(plan["listener_port"]):
        raise ReleaseBuildError("legacy routing port does not match listener")
    return values


def _listener_process_receipt(
    plan: dict,
    *,
    gateway: bool,
    require_git_source: bool,
) -> dict:
    port_key = "gateway_listener_port" if gateway else "listener_port"
    plist_key = "gateway_installed_plist" if gateway else "installed_plist"
    pid = _listener_pid(int(plan[port_key]))
    start = _pid_start_token(pid)
    if not start:
        raise DrainIdentityMismatch("listener process start identity is unavailable")
    job_pid = _job_pid(plan, gateway=gateway)
    if job_pid is not None and job_pid != pid:
        raise DrainIdentityMismatch("launchd job and listener PIDs disagree")
    cwd = _process_cwd(pid)
    plist_path = Path(plan[plist_key])
    plist = _read_plist(plist_path)
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise ReleaseBuildError("legacy launchd argv is invalid")
    receipt: dict[str, object] = {
        "pid": pid,
        "pid_start_token": start,
        "launchd_loaded": job_pid is not None,
        "command": _ps_value(pid, "command"),
        "comm": _ps_value(pid, "comm"),
        "cwd": str(cwd),
        "plist": _file_identity_receipt(plist_path),
        "program_arguments": arguments,
        "program_identity": _file_identity_receipt(arguments[0]),
    }
    if require_git_source:
        receipt["source"] = _source_patch_receipt(cwd)
        receipt["routing_environment"] = _discover_routing_environment(
            plan,
            plist,
            cwd,
        )
    return receipt


def _collect_process_binding(
    plan: dict,
    *,
    inspect_control: Callable[[], dict],
) -> dict:
    control = _require_bound_control_receipt(
        inspect_control(),
        status="inspected",
        transaction_id=plan["transaction_id"],
    )
    identity = control.get("identity")
    if not isinstance(identity, dict):
        raise DrainIdentityMismatch("signed health process identity is missing")
    deep = _http_json(
        f"{str(plan['base_url']).rstrip('/')}/health?deep=1",
        timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    binding = {
        "status": "verified",
        "launchd_pid": _launchd_pid(plan),
        "listener_pid": _listener_pid(int(plan["listener_port"])),
        "signed_health_pid": identity.get("pid"),
        "pid_start_token": identity.get("pid_start_token"),
        "signed_identity": identity,
        "deep_health": deep,
    }
    runtime = _listener_process_receipt(
        plan,
        gateway=False,
        require_git_source=False,
    )
    if (
        runtime.get("pid") != binding["listener_pid"]
        or runtime.get("pid_start_token") != binding["pid_start_token"]
    ):
        raise DrainIdentityMismatch("runtime process receipt changed during binding")
    binding["runtime"] = runtime
    return binding


def _wait_for_expected_binding(
    plan: dict,
    *,
    inspect_control: Callable[[], dict],
    expected_identity: dict,
    admission_state: str,
    previous_pid_start: tuple[int, str] | None = None,
    require_startup_markers_cleared: bool = False,
) -> dict:
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            binding = _collect_process_binding(
                plan,
                inspect_control=inspect_control,
            )
            deep = binding["deep_health"]
            build = deep.get("build") if isinstance(deep, dict) else None
            admission = deep.get("admission") if isinstance(deep, dict) else None
            if (
                binding["launchd_pid"] != binding["listener_pid"]
                or binding["launchd_pid"] != binding["signed_health_pid"]
                or not isinstance(build, dict)
                or build.get("status") != "managed"
                or build.get("valid") is not True
                or any(
                    build.get(key) != expected_identity.get(key)
                    for key in (
                        "build_id",
                        "manifest_sha256",
                        "agent_manifest_sha256",
                        "runtime_manifest_sha256",
                    )
                    if key in expected_identity
                )
                or not isinstance(admission, dict)
                or admission.get("state") != admission_state
            ):
                raise ReleaseBuildError("managed process binding does not match release")
            current_pid_start = (
                int(binding["launchd_pid"]),
                str(binding["pid_start_token"] or ""),
            )
            if previous_pid_start is not None and current_pid_start == previous_pid_start:
                raise DrainIdentityMismatch(
                    "controlled restart did not replace process identity"
                )
            signed_identity = binding.get("signed_identity")
            if require_startup_markers_cleared and (
                not isinstance(signed_identity, dict)
                or signed_identity.get("startup_fenced") not in {None, False}
                or signed_identity.get("startup_transaction_id") not in {None, ""}
                or signed_identity.get("selector_generation")
                != _selector_state_attestation(plan)["generation"]
            ):
                raise ReleaseBuildError(
                    "promoted restart retained startup fence markers"
                )
            return binding
        except Exception as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(f"managed process binding timed out: {last_error}")


def _render_cli_shim(identity: dict) -> bytes:
    required = {
        "build_id",
        "interpreter_path",
        "agent_source_path",
        "runtime_python_home_path",
        "runtime_site_packages_path",
        "manifest_sha256",
        "agent_source_manifest_sha256",
        "runtime_manifest_sha256",
    }
    if not isinstance(identity, dict) or not required.issubset(identity):
        raise ReleaseBuildError("Hermes CLI release identity is incomplete")
    environment = {
        "PYTHONHOME": str(identity["runtime_python_home_path"]),
        "PYTHONPATH": os.pathsep.join(
            [
                str(identity["agent_source_path"]),
                str(identity["runtime_site_packages_path"]),
            ]
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_WEBUI_AGENT_DIR": str(identity["agent_source_path"]),
        "HERMES_WEBUI_MANIFEST_SHA256": str(identity["manifest_sha256"]),
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": str(
            identity["agent_source_manifest_sha256"]
        ),
        "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": str(
            identity["runtime_manifest_sha256"]
        ),
    }
    lines = ["#!/bin/sh", "set -eu"]
    for key, value in environment.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.append(
        "exec "
        + shlex.quote(str(identity["interpreter_path"]))
        + " -S -c "
        + shlex.quote(
            "from hermes_cli.main import main; raise SystemExit(main())"
        )
        + ' "$@"'
    )
    return ("\n".join(lines) + "\n").encode()


_FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION = {
    "scope": "bounded-single-operator-host",
    "cooperating_writers_use_transaction_locks": True,
    "malicious_concurrent_same_uid_actor_excluded": False,
    "statement": (
        "File identity and CAS checks fail closed for observed changes; "
        "they do not exclude a malicious concurrent actor with the same uid."
    ),
}
_CLI_SHIM_MAX_BYTES = 1024 * 1024
_CLI_SYMLINK_IDENTITY_FIELDS = (
    "device",
    "inode",
    "uid",
    "nlink",
    "mode",
    "mtime_ns",
    "ctime_ns",
    "target",
)


def _immutable_cli_shim_receipt(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> dict:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "immutable CLI shim cannot be read without O_NOFOLLOW"
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseBuildError("immutable CLI shim is unreadable") from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    try:
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o555
            or opened.st_size < 1
            or opened.st_size > _CLI_SHIM_MAX_BYTES
        ):
            raise ReleaseBuildError("immutable CLI shim is unsafe")
        payload = b""
        while len(payload) <= _CLI_SHIM_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _CLI_SHIM_MAX_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload += chunk
        if len(payload) > _CLI_SHIM_MAX_BYTES:
            raise ReleaseBuildError("immutable CLI shim is too large")
        finished = os.fstat(descriptor)
        current = path.lstat()
        if any(
            getattr(finished, field) != getattr(opened, field)
            or getattr(current, field) != getattr(opened, field)
            for field in stable_fields
        ):
            raise DrainIdentityMismatch(
                "immutable CLI shim changed while reading"
            )
    except OSError as exc:
        raise DrainIdentityMismatch(
            "immutable CLI shim identity became unavailable"
        ) from exc
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ReleaseBuildError("immutable CLI shim hash changed")
    return {
        "path": str(path),
        "sha256": digest,
        "size": len(payload),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
    }


def _stage_immutable_cli_payload(
    plan: dict,
    *,
    filename: str,
    payload: bytes,
) -> dict:
    shim_dir = _prepare_release_root(plan["cli_shim_dir"])
    digest = hashlib.sha256(payload).hexdigest()
    shim = shim_dir / filename
    if shim.exists() or shim.is_symlink():
        return _immutable_cli_shim_receipt(
            shim,
            expected_sha256=digest,
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "immutable CLI shim cannot be created without O_NOFOLLOW"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(shim, flags, 0o555)
    except FileExistsError:
        return _immutable_cli_shim_receipt(
            shim,
            expected_sha256=digest,
        )
    except OSError as exc:
        raise ReleaseBuildError("immutable CLI shim cannot be created") from exc
    try:
        os.set_inheritable(descriptor, False)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o555)
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        try:
            shim.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
        _fsync_directory(shim_dir)
    return _immutable_cli_shim_receipt(
        shim,
        expected_sha256=digest,
    )


def stage_immutable_cli_shim(plan: dict, identity: dict) -> dict:
    build_id = str(identity.get("build_id") or "")
    if not _BUILD_ID.fullmatch(build_id):
        raise ReleaseBuildError("Hermes CLI build identity is invalid")
    payload = _render_cli_shim(identity)
    digest = hashlib.sha256(payload).hexdigest()
    shim = _stage_immutable_cli_payload(
        plan,
        filename=f"hermes-{build_id}-{digest[:16]}",
        payload=payload,
    )
    return {
        "status": "staged",
        "build_id": build_id,
        "shim_path": shim["path"],
        "shim_sha256": shim["sha256"],
        "shim": shim,
    }


def _render_cli_maintenance_deny_shim(plan: dict) -> bytes:
    transaction_id = str(plan.get("transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError("Hermes CLI maintenance transaction is invalid")
    message = (
        "Hermes is temporarily unavailable while release transaction "
        f"{transaction_id} completes; retry shortly."
    )
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"printf '%s\\n' {shlex.quote(message)} >&2\n"
        "exit 75\n"
    ).encode()


def _stage_cli_maintenance_deny_shim(plan: dict) -> dict:
    payload = _render_cli_maintenance_deny_shim(plan)
    digest = hashlib.sha256(payload).hexdigest()
    shim = _stage_immutable_cli_payload(
        plan,
        filename=(
            f"hermes-maintenance-{plan['transaction_id']}-{digest[:16]}"
        ),
        payload=payload,
    )
    return {
        "status": "staged",
        "transaction_id": plan["transaction_id"],
        **shim,
    }


def _read_cli_symlink_identity(path: Path) -> dict:
    if (
        not path.is_absolute()
        or Path(os.path.abspath(path)) != path
        or not path.parent.is_dir()
        or path.parent.is_symlink()
        or path.parent.resolve(strict=True) != path.parent
    ):
        raise ReleaseBuildError("Hermes CLI link path is invalid")
    try:
        opened = path.lstat()
        target = os.readlink(path)
        finished = path.lstat()
    except OSError as exc:
        raise ReleaseBuildError("Hermes CLI link is unavailable") from exc
    if (
        not stat.S_ISLNK(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or any(
            getattr(opened, field) != getattr(finished, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        )
    ):
        raise ReleaseBuildError("Hermes CLI link is unsafe")
    return {
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "nlink": opened.st_nlink,
        "mode": stat.S_IMODE(opened.st_mode),
        "mtime_ns": opened.st_mtime_ns,
        "ctime_ns": opened.st_ctime_ns,
        "target": target,
    }


def _cli_target_path(link: Path, raw_target: str) -> Path:
    target = Path(raw_target)
    if not target.is_absolute():
        target = link.parent / target
    try:
        return target.resolve(strict=True)
    except OSError as exc:
        raise ReleaseBuildError("Hermes CLI target is unavailable") from exc


def _exact_original_cli_target_receipt(
    plan: dict,
    original: dict,
) -> dict:
    link = Path(plan["cli_link"])
    raw_target = str(original.get("link_target") or "")
    if (
        raw_target != str(plan.get("cli_old_target") or "")
        or original.get("path") != str(link)
    ):
        raise ReleaseBuildError("bootstrap Hermes CLI original receipt is invalid")
    resolved = _cli_target_path(link, raw_target)
    actual = _file_identity_receipt(resolved)
    for field in (
        "resolved_path",
        "resolved_mode",
        "resolved_uid",
        "resolved_size",
        "sha256",
    ):
        if actual.get(field) != original.get(field):
            raise DrainIdentityMismatch(
                "bootstrap Hermes CLI original target changed"
            )
    return actual


def _cas_replace_cli_link(
    plan: dict,
    *,
    allowed_current_targets: set[str],
    desired_target: str,
    desired_immutable_receipt: dict | None,
) -> dict:
    link = Path(plan["cli_link"])
    if not allowed_current_targets or not desired_target:
        raise ReleaseBuildError("Hermes CLI CAS identity is invalid")
    before = _read_cli_symlink_identity(link)
    if before["target"] not in allowed_current_targets:
        raise DrainIdentityMismatch("Hermes CLI link has a foreign target")
    if desired_immutable_receipt is not None:
        desired = _immutable_cli_shim_receipt(
            Path(desired_target),
            expected_sha256=str(desired_immutable_receipt.get("sha256") or ""),
        )
        if desired != desired_immutable_receipt:
            raise DrainIdentityMismatch("Hermes CLI desired shim identity changed")
    if before["target"] == desired_target:
        return {
            "status": "adopted",
            "link_path": str(link),
            "before": before,
            "after": before,
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        }
    temporary = link.parent / f".{link.name}.{secrets.token_hex(8)}.tmp"
    os.symlink(desired_target, temporary)
    try:
        confirmed = _read_cli_symlink_identity(link)
        if any(
            confirmed.get(field) != before.get(field)
            for field in _CLI_SYMLINK_IDENTITY_FIELDS
        ):
            raise DrainIdentityMismatch("Hermes CLI link changed before CAS")
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    after = _read_cli_symlink_identity(link)
    if after["target"] != desired_target:
        raise DrainIdentityMismatch("Hermes CLI link CAS did not persist")
    return {
        "status": "replaced",
        "link_path": str(link),
        "before": before,
        "after": after,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _bootstrap_cli_gate_stage_intent_receipt(
    plan: dict,
    prepared: dict,
) -> dict:
    legacy = prepared.get("legacy") if isinstance(prepared, dict) else None
    original = legacy.get("cli") if isinstance(legacy, dict) else None
    if not isinstance(original, dict):
        raise ReleaseBuildError("bootstrap Hermes CLI original receipt is missing")
    link = Path(plan["cli_link"])
    live = _file_identity_receipt(link)
    if live != original:
        raise DrainIdentityMismatch("bootstrap Hermes CLI changed before gate stage")
    _exact_original_cli_target_receipt(plan, original)
    candidate = stage_immutable_cli_shim(
        plan,
        plan["expected_candidate_identity"],
    )
    maintenance = _stage_cli_maintenance_deny_shim(plan)
    return {
        "status": "prepared",
        "transaction_id": plan["transaction_id"],
        "link_path": str(link),
        "original": copy.deepcopy(original),
        "candidate_shim": copy.deepcopy(candidate["shim"]),
        "maintenance_shim": {
            key: maintenance[key]
            for key in (
                "path",
                "sha256",
                "size",
                "device",
                "inode",
                "uid",
                "mode",
                "nlink",
            )
        },
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _install_or_adopt_bootstrap_cli_gate(plan: dict, intent: dict) -> dict:
    if (
        not isinstance(intent, dict)
        or intent.get("status") != "prepared"
        or intent.get("transaction_id") != plan["transaction_id"]
        or intent.get("link_path") != str(plan["cli_link"])
        or not isinstance(intent.get("original"), dict)
        or not isinstance(intent.get("maintenance_shim"), dict)
    ):
        raise ReleaseBuildError("bootstrap Hermes CLI gate intent is invalid")
    original = intent["original"]
    maintenance = intent["maintenance_shim"]
    _exact_original_cli_target_receipt(plan, original)
    activation = _cas_replace_cli_link(
        plan,
        allowed_current_targets={
            str(original["link_target"]),
            str(maintenance["path"]),
        },
        desired_target=str(maintenance["path"]),
        desired_immutable_receipt=maintenance,
    )
    return {
        "status": "installed",
        "transaction_id": plan["transaction_id"],
        "link_path": str(plan["cli_link"]),
        "target": str(maintenance["path"]),
        "target_sha256": str(maintenance["sha256"]),
        "activation": activation,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _attest_bootstrap_cli_preopen(plan: dict, intent: dict) -> dict:
    observed = _install_or_adopt_bootstrap_cli_gate(plan, intent)
    candidate = intent.get("candidate_shim")
    maintenance = intent.get("maintenance_shim")
    if not isinstance(candidate, dict) or not isinstance(maintenance, dict):
        raise ReleaseBuildError("bootstrap Hermes CLI stage intent is invalid")
    candidate_now = _immutable_cli_shim_receipt(
        Path(str(candidate.get("path") or "")),
        expected_sha256=str(candidate.get("sha256") or ""),
    )
    if candidate_now != candidate:
        raise DrainIdentityMismatch("staged candidate Hermes CLI changed")
    link = _read_cli_symlink_identity(Path(plan["cli_link"]))
    if link["target"] != maintenance.get("path"):
        raise DrainIdentityMismatch(
            "public Hermes CLI opened before durable pair_opened"
        )
    return {
        "status": "maintenance-gated",
        "public_target": link["target"],
        "public_target_sha256": maintenance["sha256"],
        "candidate_shim": candidate_now,
        "gate": {
            key: observed[key]
            for key in (
                "transaction_id",
                "link_path",
                "target",
                "target_sha256",
                "bounded_host_assumption",
            )
        },
    }


def _restore_bootstrap_cli_link(
    plan: dict,
    prepared: dict,
    phases: dict,
) -> dict:
    legacy = prepared.get("legacy") if isinstance(prepared, dict) else None
    original = legacy.get("cli") if isinstance(legacy, dict) else None
    if not isinstance(original, dict):
        raise ReleaseBuildError("bootstrap Hermes CLI original receipt is missing")
    _exact_original_cli_target_receipt(plan, original)
    current = _read_cli_symlink_identity(Path(plan["cli_link"]))
    if current["target"] == original["link_target"]:
        if _file_identity_receipt(plan["cli_link"]) != original:
            raise DrainIdentityMismatch(
                "bootstrap Hermes CLI restored identity changed"
            )
        return {
            "status": "already-restored",
            "link_path": str(plan["cli_link"]),
            "target": str(original["link_target"]),
        }
    intent = phases.get("cli_maintenance_gate_stage_intent")
    if not isinstance(intent, dict):
        raise DrainIdentityMismatch("Hermes CLI link has a foreign target")
    maintenance = intent.get("maintenance_shim")
    candidate = intent.get("candidate_shim")
    if not isinstance(maintenance, dict) or not isinstance(candidate, dict):
        raise ReleaseBuildError("bootstrap Hermes CLI stage intent is invalid")
    allowed = {str(maintenance.get("path") or ""), str(candidate.get("path") or "")}
    if current["target"] not in allowed:
        raise DrainIdentityMismatch("Hermes CLI link has a foreign target")
    restored = _cas_replace_cli_link(
        plan,
        allowed_current_targets=allowed,
        desired_target=str(original["link_target"]),
        desired_immutable_receipt=None,
    )
    exact = _file_identity_receipt(plan["cli_link"])
    if exact != original:
        raise DrainIdentityMismatch("bootstrap Hermes CLI restore is not exact")
    return {
        "status": "restored",
        "link_path": str(plan["cli_link"]),
        "target": str(original["link_target"]),
        "cas": restored,
    }


def _bootstrap_cli_candidate_activation_intent(
    plan: dict,
    stage_intent: dict,
    cutover_phases: dict,
) -> dict:
    pair_opened = (
        cutover_phases.get("pair_opened")
        if isinstance(cutover_phases, dict)
        else None
    )
    candidate = (
        stage_intent.get("candidate_shim")
        if isinstance(stage_intent, dict)
        else None
    )
    maintenance = (
        stage_intent.get("maintenance_shim")
        if isinstance(stage_intent, dict)
        else None
    )
    if (
        not isinstance(pair_opened, dict)
        or pair_opened.get("status") not in {"opened", "verified"}
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(pair_opened.get("owner_hash") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(pair_opened.get("payload_sha256") or ""),
        )
    ):
        raise ReleaseBuildError(
            "public Hermes CLI cannot activate before durable pair_opened"
        )
    if not isinstance(candidate, dict) or not isinstance(maintenance, dict):
        raise ReleaseBuildError("bootstrap Hermes CLI stage intent is invalid")
    return {
        "status": "prepared",
        "transaction_id": plan["transaction_id"],
        "link_path": str(plan["cli_link"]),
        "candidate_shim": copy.deepcopy(candidate),
        "maintenance_shim": copy.deepcopy(maintenance),
        "pair_opened_owner_hash": pair_opened["owner_hash"],
        "pair_opened_payload_sha256": pair_opened["payload_sha256"],
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _activate_or_adopt_bootstrap_cli_candidate(
    plan: dict,
    activation_intent: dict,
) -> dict:
    if (
        not isinstance(activation_intent, dict)
        or activation_intent.get("status") != "prepared"
        or activation_intent.get("transaction_id") != plan["transaction_id"]
        or activation_intent.get("link_path") != str(plan["cli_link"])
    ):
        raise ReleaseBuildError(
            "bootstrap Hermes CLI activation intent is invalid"
        )
    candidate = activation_intent.get("candidate_shim")
    maintenance = activation_intent.get("maintenance_shim")
    if not isinstance(candidate, dict) or not isinstance(maintenance, dict):
        raise ReleaseBuildError(
            "bootstrap Hermes CLI activation identity is invalid"
        )
    activation = _cas_replace_cli_link(
        plan,
        allowed_current_targets={
            str(maintenance.get("path") or ""),
            str(candidate.get("path") or ""),
        },
        desired_target=str(candidate.get("path") or ""),
        desired_immutable_receipt=candidate,
    )
    return {
        "status": "activated",
        "transaction_id": plan["transaction_id"],
        "link_path": str(plan["cli_link"]),
        "target": candidate["path"],
        "target_sha256": candidate["sha256"],
        "pair_opened_owner_hash": activation_intent[
            "pair_opened_owner_hash"
        ],
        "pair_opened_payload_sha256": activation_intent[
            "pair_opened_payload_sha256"
        ],
        "activation": activation,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def install_immutable_cli_shim(plan: dict, identity: dict) -> dict:
    staged = stage_immutable_cli_shim(plan, identity)
    activation = _cas_replace_cli_link(
        plan,
        allowed_current_targets={
            str(plan.get("cli_old_target") or ""),
            staged["shim_path"],
        },
        desired_target=staged["shim_path"],
        desired_immutable_receipt=staged["shim"],
    )
    return {
        **staged,
        "status": "installed",
        "link_path": str(plan["cli_link"]),
        "activation": activation,
    }


def _restore_cli_link(plan: dict) -> dict:
    target = str(plan.get("cli_old_target") or "")
    if not target:
        raise ReleaseBuildError("Hermes CLI rollback target is missing")
    candidate = stage_immutable_cli_shim(
        plan,
        plan["expected_candidate_identity"],
    )
    restored = _cas_replace_cli_link(
        plan,
        allowed_current_targets={candidate["shim_path"], target},
        desired_target=target,
        desired_immutable_receipt=None,
    )
    return {
        "status": "restored",
        "link_path": str(plan["cli_link"]),
        "target": target,
        "cas": restored,
    }


def _attest_cli_link(plan: dict, identity: dict) -> dict:
    link = Path(plan["cli_link"])
    link_identity = _read_cli_symlink_identity(link)
    staged = stage_immutable_cli_shim(plan, identity)
    if link_identity["target"] != staged["shim_path"]:
        raise ReleaseBuildError("Hermes CLI shim does not match release identity")
    target = _immutable_cli_shim_receipt(
        Path(staged["shim_path"]),
        expected_sha256=staged["shim_sha256"],
    )
    return {
        "status": "verified",
        "build_id": identity["build_id"],
        "shim_path": target["path"],
        "shim_sha256": target["sha256"],
    }


def _selector_state_attestation(plan: dict) -> dict:
    state = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    return {
        "status": "verified",
        "transaction_id": plan["transaction_id"],
        "generation": state["generation"],
        "current": state["current"],
        "candidate": state["candidate"],
        "pending_transaction_id": state.get("pending_transaction_id"),
        "last_good": state["last_good"],
    }


def _installed_plist_attestation(plan: dict) -> dict:
    path = _absolute_plan_path(plan["installed_plist"], label="installed_plist")
    plist = _read_plist(path)
    return {
        "status": "verified",
        "launchd_label": plist.get("Label"),
        "plist_sha256": sha256_file(path),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def _selector_origin_last_good_id(plan: dict) -> str:
    """Return the exact selector fallback sealed for the pre-activation state."""
    last_good_id = plan["last_good_identity"]["build_id"]
    adoption_keys = set(plan).intersection(
        _LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS
    )
    if not adoption_keys:
        return last_good_id
    if adoption_keys != _LAST_GOOD_SPLIT_ADOPTION_PLAN_KEYS:
        raise ReleaseBuildError(
            "cutover plan provenance schema is invalid"
        )
    receipt = _read_sealed_split_adoption_receipt(
        plan["last_good_split_adoption_receipt"],
        expected_sha256=plan[
            "last_good_split_adoption_receipt_sha256"
        ],
        trusted_root=Path(plan["transaction_journal"]).parent,
        webui_identity=plan["last_good_identity"],
        gateway_identity=plan["last_good_gateway_identity"],
    )
    selector = receipt.get("selector")
    selector_state = (
        selector.get("state") if isinstance(selector, dict) else None
    )
    origin_last_good_id = (
        selector_state.get("last_good")
        if isinstance(selector_state, dict)
        else None
    )
    if not isinstance(origin_last_good_id, str) or not origin_last_good_id:
        raise ReleaseBuildError(
            "last-good adoption receipt selector authority is invalid"
        )
    return origin_last_good_id


def _selector_transition(plan: dict, transition: str) -> dict:
    state = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    candidate_id = plan["expected_candidate_identity"]["build_id"]
    last_good_id = plan["last_good_identity"]["build_id"]
    if transition == "activate":
        if (
            state["current"] == candidate_id
            and state["candidate"] == candidate_id
            and state.get("pending_transaction_id") == plan["transaction_id"]
        ):
            return state
        callback = release_selector.activate_candidate
    elif transition == "promote":
        bootstrap_path = _bootstrap_journal_path(plan)
        bootstrap_phases: dict = {}
        if bootstrap_path.exists():
            bootstrap_phases = _read_bootstrap_journal(plan)["phases"]
        cli_deferred = (
            "cli_maintenance_gate_installed" in bootstrap_phases
            and "cli_candidate_activated" not in bootstrap_phases
        )
        cli = (
            stage_immutable_cli_shim(
                plan,
                plan["expected_candidate_identity"],
            )
            if cli_deferred
            else install_immutable_cli_shim(
                plan,
                plan["expected_candidate_identity"],
            )
        )
        if (
            state["current"] == candidate_id
            and state["last_good"] == last_good_id
            and state["candidate"] is None
            and state.get("pending_transaction_id") is None
        ):
            selected = state
        else:
            selected = release_selector.update_selector_state(
                plan["selector_state"],
                lock_path=plan["selector_lock"],
                expected_generation=state["generation"],
                transition=release_selector.promote_candidate,
            )
        return {"selector": selected, "cli": cli}
    elif transition == "rollback":
        if (
            state["current"] == last_good_id
            and state["candidate"] is None
            and state.get("pending_transaction_id") is None
        ):
            selected = state
        else:
            def force_rollback(current: dict) -> dict:
                if last_good_id not in current.get("releases", {}):
                    raise ReleaseBuildError(
                        "durable rollback build is absent from selector"
                    )
                rolled_back = copy.deepcopy(current)
                rolled_back["current"] = last_good_id
                rolled_back["last_good"] = last_good_id
                rolled_back["candidate"] = None
                rolled_back["pending_transaction_id"] = None
                return rolled_back

            selected = release_selector.update_selector_state(
                plan["selector_state"],
                lock_path=plan["selector_lock"],
                expected_generation=state["generation"],
                transition=force_rollback,
            )
        bootstrap_path = _bootstrap_journal_path(plan)
        if bootstrap_path.exists():
            bootstrap = _read_bootstrap_journal(plan)
            bootstrap_phases = bootstrap["phases"]
            prepared = bootstrap_phases.get("prepared")
            if (
                isinstance(prepared, dict)
                and "cli_maintenance_gate_stage_intent" in bootstrap_phases
            ):
                cli = _restore_bootstrap_cli_link(
                    plan,
                    prepared,
                    bootstrap_phases,
                )
            else:
                cli = _restore_cli_link(plan)
        else:
            cli = _restore_cli_link(plan)
        return {"selector": selected, "cli": cli}
    else:
        raise ReleaseBuildError("selector transition is invalid")
    if (
        state["candidate"] != candidate_id
        or state.get("pending_transaction_id") != plan["transaction_id"]
    ):
        raise ReleaseBuildError("selector candidate transaction changed")
    return release_selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=state["generation"],
        transition=callback,
    )


def _reconcile_cutover_journal(
    plan: dict,
    *,
    staged_evidence: dict | None = None,
) -> dict:
    selector_attestation = _selector_state_attestation(plan)
    candidate = plan["expected_candidate_identity"]
    candidate_id = candidate["build_id"]
    last_good_id = plan["last_good_identity"]["build_id"]
    origin_last_good_id = _selector_origin_last_good_id(plan)
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(last_good_id, str)
        or not last_good_id
        or candidate_id == last_good_id
    ):
        raise ReleaseBuildError("cutover selector identities are invalid")
    selector_tuple = (
        selector_attestation["current"],
        selector_attestation["candidate"],
        selector_attestation["pending_transaction_id"],
        selector_attestation["last_good"],
    )
    allowed = {
        (
            last_good_id,
            candidate_id,
            plan["transaction_id"],
            origin_last_good_id,
        ): "staged",
        (
            candidate_id,
            candidate_id,
            plan["transaction_id"],
            last_good_id,
        ): "activated",
        (candidate_id, None, None, last_good_id): "promoted",
        (
            last_good_id,
            None,
            None,
            origin_last_good_id,
        ): "last-good",
    }
    selector_phase = allowed.get(selector_tuple)
    if selector_phase is None:
        raise ReleaseBuildError("selector state cannot reconcile cutover journal")
    managed_plist_sha256 = sha256_file(Path(plan["managed_plist"]))
    rollback_plist_path = Path(plan["bootstrap_rollback_plist"])
    rollback_plist_sha256 = sha256_file(rollback_plist_path)
    plist_attestation = _installed_plist_attestation(plan)
    if plist_attestation["plist_sha256"] != managed_plist_sha256:
        raise ReleaseBuildError("installed managed plist cannot reconcile journal")
    snapshot_path = Path(plan["snapshot_manifest"])
    snapshot_sha256 = sha256_file(snapshot_path)
    snapshot = _read_json_object(snapshot_path, label="state snapshot manifest")
    verify_state_snapshot(
        snapshot,
        manifest_sha256=snapshot_sha256,
        live=False,
    )
    try:
        journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
    except (ReleaseBuildError, OSError) as exc:
        if selector_phase == "promoted":
            raise ReleaseBuildError(
                "promoted selector durable journal is unavailable"
            ) from exc
        if not isinstance(exc, ReleaseBuildError) or "unreadable" not in str(exc):
            raise
        journal = initialize_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            expected_candidate_identity=plan["expected_candidate_identity"],
            rollback_receipt={
                "build_id": last_good_id,
                "plist_sha256": rollback_plist_sha256,
                "plist_mode": stat.S_IMODE(rollback_plist_path.stat().st_mode),
                "cli_link_target": str(plan["cli_old_target"]),
                "state_snapshot_id": snapshot["snapshot_id"],
                "state_snapshot_sha256": snapshot_sha256,
            },
        )
    if journal["expected_candidate_identity"] != candidate:
        raise ReleaseBuildError("transaction journal candidate identity mismatch")
    if selector_phase == "promoted":
        phases = journal["phases"]
        durable_promoted = (
            phases.get("promoted", {})
            .get("promotion", {})
            .get("selector_and_cli", {})
            .get("selector")
        )
        live_selector = release_selector.read_selector_state(
            plan["selector_state"],
            lock_path=plan["selector_lock"],
        )
        live_projection = {
            field: live_selector.get(field)
            for field in (
                "generation",
                "current",
                "candidate",
                "pending_transaction_id",
                "last_good",
            )
        }
        attested_projection = {
            field: selector_attestation.get(field)
            for field in live_projection
        }
        startup_generation = candidate.get("selector_generation")
        if live_projection != attested_projection:
            raise DrainIdentityMismatch(
                "promoted selector changed during journal reconciliation"
            )
        if (
            "pair_commit_intent" not in phases
            or live_selector != durable_promoted
            or not isinstance(startup_generation, int)
            or isinstance(startup_generation, bool)
            or live_selector["generation"] != startup_generation + 1
            or live_selector["releases"].get(candidate_id)
            != _release_record_from_identity(candidate)
        ):
            raise DrainIdentityMismatch(
                "promoted selector does not match durable transaction"
            )
    phases = journal["phases"]
    if "staged" not in phases:
        if selector_tuple not in {
            (
                last_good_id,
                candidate_id,
                plan["transaction_id"],
                origin_last_good_id,
            ),
            (
                candidate_id,
                candidate_id,
                plan["transaction_id"],
                last_good_id,
            ),
            (candidate_id, None, None, last_good_id),
        }:
            raise ReleaseBuildError("candidate stage is not externally attested")
        staged_receipt = {
            "build_id": candidate_id,
            "selector_generation": selector_attestation["generation"],
            "external_state_reconciled": True,
        }
        if staged_evidence is not None:
            staged_receipt["bootstrap_evidence"] = staged_evidence
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="staged",
            receipt=staged_receipt,
        )
    if "plist_installed" not in journal["phases"]:
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="plist_installed",
            receipt={
                "plist_sha256": managed_plist_sha256,
                "external_state_reconciled": True,
            },
        )
    phases = journal["phases"]
    if (
        "old_committed" in phases
        and "selection_activated" not in phases
        and selector_tuple
        == (
            candidate_id,
            candidate_id,
            plan["transaction_id"],
            last_good_id,
        )
    ):
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="selection_activated",
            receipt={
                "selection": {
                    "generation": selector_attestation["generation"],
                    "external_state_reconciled": True,
                }
            },
        )
        phases = journal["phases"]
    if "selection_activated" in phases and "old_stopped" not in phases:
        old_identity = phases.get("old_committed", {}).get("identity")
        if not isinstance(old_identity, dict):
            raise ReleaseBuildError(
                "activated selector has no durable old-process identity"
            )
        try:
            old_pid = int(old_identity.get("pid"))
        except (TypeError, ValueError) as exc:
            raise DrainIdentityMismatch("durable old-process PID is invalid") from exc
        old_start = str(old_identity.get("pid_start_token") or "")
        old_absent = bool(old_start) and _pid_start_token(old_pid) != old_start
        try:
            _launchd_pid(plan)
            launchd_absent = False
        except Exception:
            launchd_absent = True
        if old_absent and launchd_absent:
            if "old_job_booted_out" not in phases:
                journal = record_transaction_phase(
                    plan["transaction_journal"],
                    transaction_id=plan["transaction_id"],
                    phase="old_job_booted_out",
                    receipt={
                        "identity": old_identity,
                        "bootout": {
                            "status": "externally-reconciled",
                            "launchd_job_absent": True,
                        },
                    },
                )
            journal = record_transaction_phase(
                plan["transaction_journal"],
                transaction_id=plan["transaction_id"],
                phase="old_stopped",
                receipt={
                    "identity": old_identity,
                    "external_process_absence_reconciled": True,
                },
            )
    return journal


def _preflight_last_good_identity_split(plan: dict) -> MappingProxyType:
    """Read-only, repeatable proof of the independently sealed last-good pair."""
    provenance_arguments = (
        {
            "webui_origin_journal": plan["last_good_origin_journal"],
            "webui_origin_sha256": plan["last_good_origin_journal_sha256"],
            "gateway_origin_journal": plan["last_good_gateway_origin_journal"],
            "gateway_origin_sha256": plan[
                "last_good_gateway_origin_journal_sha256"
            ],
        }
        if _LAST_GOOD_ORIGIN_JOURNAL_PLAN_KEYS.issubset(plan)
        else {
            "adoption_receipt": plan["last_good_split_adoption_receipt"],
            "adoption_receipt_sha256": plan[
                "last_good_split_adoption_receipt_sha256"
            ],
        }
    )
    return _attest_last_good_identity_split(
        webui_identity=plan["last_good_identity"],
        gateway_identity=plan["last_good_gateway_identity"],
        trusted_root=Path(plan["transaction_journal"]).parent,
        selector_path=plan["selector_path"],
        **provenance_arguments,
    )


def _inspect_cutover_plan(plan: dict) -> dict:
    last_good_origin_attestation = _preflight_last_good_identity_split(plan)
    selector_state = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    candidate = plan["expected_candidate_identity"]
    candidate_id = candidate["build_id"]
    last_good_id = plan["last_good_identity"]["build_id"]
    promoted = (
        selector_state["current"] == candidate_id
        and selector_state["last_good"] == last_good_id
        and selector_state["candidate"] is None
        and selector_state.get("pending_transaction_id") is None
    )
    if promoted:
        startup_value = candidate.get("selector_generation")
        if (
            not isinstance(startup_value, int)
            or isinstance(startup_value, bool)
            or startup_value <= 0
            or startup_value > selector_state["generation"]
            or selector_state["releases"].get(candidate_id)
            != _release_record_from_identity(candidate)
        ):
            raise ReleaseBuildError(
                "promoted candidate selector generation changed"
            )
        startup_generation = {
            "status": "already-promoted",
            "staged_generation": startup_value - 1,
            "startup_generation": startup_value,
            "current_generation": selector_state["generation"],
        }
    elif (
        selector_state["current"] == candidate_id
        and selector_state["candidate"] == candidate_id
        and selector_state.get("pending_transaction_id")
        == plan["transaction_id"]
    ):
        if candidate.get("selector_generation") != selector_state["generation"]:
            raise ReleaseBuildError(
                "active candidate selector generation changed"
            )
        startup_generation = {
            "staged_generation": selector_state["generation"] - 1,
            "startup_generation": selector_state["generation"],
        }
    else:
        staged_state = copy.deepcopy(selector_state)
        if staged_state["candidate"] is None:
            staged_state = release_selector.stage_candidate(
                staged_state,
                candidate_id,
                _release_record_from_identity(candidate),
                transaction_id=plan["transaction_id"],
            )
            staged_state["generation"] += 1
        if staged_state["current"] != last_good_id:
            raise ReleaseBuildError(
                "candidate selector is not staged from last-good"
            )
        startup_generation = _attest_candidate_startup_generation(
            plan,
            staged_state,
        )
    result: dict[str, object] = {
        "status": "verified",
        "transaction_id": plan["transaction_id"],
        "selector": {
            "status": "verified",
            "transaction_id": plan["transaction_id"],
            "generation": selector_state["generation"],
            "current": selector_state["current"],
            "candidate": selector_state["candidate"],
            "pending_transaction_id": selector_state.get(
                "pending_transaction_id"
            ),
            "last_good": selector_state["last_good"],
        },
        "candidate_startup_generation": startup_generation,
        "last_good_origin_attestation": last_good_origin_attestation,
        "installed_plist": _installed_plist_attestation(plan),
        "snapshot_manifest_sha256": (
            sha256_file(Path(plan["snapshot_manifest"]))
            if Path(plan["snapshot_manifest"]).is_file()
            else None
        ),
    }
    try:
        result["launchd_pid"] = _launchd_pid(plan)
    except Exception as exc:
        result["launchd_error"] = type(exc).__name__
    link = Path(plan["cli_link"])
    result["cli_target"] = os.readlink(link) if link.is_symlink() else None
    return result


def _bootstrap_rollback_context(
    plan: dict,
    cutover_journal: dict,
) -> dict | None:
    """Return exact legacy rollback evidence for a first activation."""
    phases = cutover_journal.get("phases")
    staged = phases.get("staged") if isinstance(phases, dict) else None
    bootstrap_evidence = (
        staged.get("bootstrap_evidence")
        if isinstance(staged, dict)
        else None
    )
    if bootstrap_evidence is None:
        return None
    prepared = (
        bootstrap_evidence.get("prepared")
        if isinstance(bootstrap_evidence, dict)
        else None
    )
    if not isinstance(prepared, dict):
        raise ReleaseBuildError(
            "bootstrap cutover has no exact legacy rollback receipt"
        )
    bootstrap = _read_bootstrap_journal(plan)
    bootstrap_phases = bootstrap.get("phases")
    durable_prepared = (
        bootstrap_phases.get("prepared")
        if isinstance(bootstrap_phases, dict)
        else None
    )
    if durable_prepared != prepared:
        raise DrainIdentityMismatch(
            "bootstrap legacy rollback receipt changed after handoff"
        )
    drain_intent = bootstrap_phases.get("legacy_gateway_drain_intent")
    if not isinstance(drain_intent, dict):
        raise ReleaseBuildError(
            "bootstrap legacy gateway drain receipt is missing"
        )
    return {
        "prepared": _upgrade_internal_watchdog_prepared_receipt(
            plan,
            prepared,
        ),
        "drain_intent": copy.deepcopy(drain_intent),
    }


def _journal_copy_of_immutable_evidence(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {
            str(key): _journal_copy_of_immutable_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_journal_copy_of_immutable_evidence(item) for item in value]
    if isinstance(value, list):
        return [_journal_copy_of_immutable_evidence(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _journal_copy_of_immutable_evidence(item)
            for key, item in value.items()
        }
    return copy.deepcopy(value)


def _canonical_journal_value_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_BOOTSTRAP_SPLIT_PROVENANCE_PLAN_KEYS = {
    "last_good_identity",
    "last_good_gateway_identity",
    "transaction_journal",
    "selector_path",
}


def _bootstrap_split_provenance_receipt(
    plan: dict,
    last_good_origin_attestation: MappingProxyType,
) -> dict:
    evidence = _journal_copy_of_immutable_evidence(
        last_good_origin_attestation
    )
    webui_identity = evidence.get("webui", {}).get("identity")
    gateway_identity = evidence.get("gateway", {}).get("identity")
    if not isinstance(webui_identity, dict) or not isinstance(
        gateway_identity,
        dict,
    ):
        raise ReleaseBuildError("bootstrap split provenance evidence is invalid")
    provenance = evidence.get("provenance")
    if provenance is None:
        webui = {
            "identity": copy.deepcopy(webui_identity),
            "origin_transaction_id": webui_identity.get(
                "startup_transaction_id"
            ),
            "origin_journal_sha256": plan[
                "last_good_origin_journal_sha256"
            ],
        }
        gateway = {
            "identity": copy.deepcopy(gateway_identity),
            "origin_transaction_id": gateway_identity.get(
                "startup_transaction_id"
            ),
            "origin_journal_sha256": plan[
                "last_good_gateway_origin_journal_sha256"
            ],
        }
        provenance_receipt = None
    elif (
        isinstance(provenance, dict)
        and set(provenance) == {"kind", "adoption_id", "receipt_sha256"}
        and provenance.get("kind") == "live-split-adoption"
        and provenance.get("receipt_sha256")
        == plan.get("last_good_split_adoption_receipt_sha256")
    ):
        webui = {"identity": copy.deepcopy(webui_identity)}
        gateway = {"identity": copy.deepcopy(gateway_identity)}
        provenance_receipt = copy.deepcopy(provenance)
    else:
        raise ReleaseBuildError("bootstrap split provenance source is invalid")
    receipt = {
        "schema": "hermes.bootstrap_split_provenance.v1",
        "webui": webui,
        "gateway": gateway,
        "split_evidence": evidence,
        "split_evidence_sha256": _canonical_journal_value_sha256(evidence),
    }
    if provenance_receipt is not None:
        receipt["provenance"] = provenance_receipt
    return receipt


def _validate_bootstrap_split_provenance(
    plan: dict,
    prepared: object,
) -> dict:
    try:
        if not isinstance(prepared, dict):
            raise ReleaseBuildError(
                "prepared receipt is invalid"
            )
        durable = prepared.get("last_good_split_provenance")
        if not isinstance(durable, dict):
            raise ReleaseBuildError("receipt is missing")
        attested = _preflight_last_good_identity_split(plan)
        expected = _bootstrap_split_provenance_receipt(plan, attested)
        if durable != expected:
            raise DrainIdentityMismatch("receipt changed")
        return copy.deepcopy(expected)
    except BootstrapSplitProvenanceMismatch:
        raise
    except (
        DrainIdentityMismatch,
        ReleaseBuildError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise BootstrapSplitProvenanceMismatch(
            f"bootstrap split provenance mismatch: {exc}"
        ) from exc


def _ensure_last_good_split_attested(
    plan: dict, journal: dict, *, last_good_origin_attestation: MappingProxyType
) -> dict:
    phases = journal.get("phases") if isinstance(journal, dict) else None
    if not isinstance(phases, dict):
        raise ReleaseBuildError("cutover journal phases are invalid")
    evidence = _journal_copy_of_immutable_evidence(last_good_origin_attestation)
    existing = phases.get("last_good_split_attested")
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or existing.get("last_good_origin_attestation") != evidence
        ):
            raise DrainIdentityMismatch("durable last-good split attestation changed")
        return journal
    return record_transaction_phase(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
        phase="last_good_split_attested",
        receipt={"last_good_origin_attestation": evidence},
    )


def _ensure_gateway_last_good_attested(
    plan: dict,
    journal: dict,
    *,
    last_good_origin_attestation: MappingProxyType,
) -> dict:
    phases = journal.get("phases") if isinstance(journal, dict) else None
    if not isinstance(phases, dict):
        raise ReleaseBuildError("cutover journal phases are invalid")
    evidence = _journal_copy_of_immutable_evidence(last_good_origin_attestation)
    split = phases.get("last_good_split_attested")
    if (
        not isinstance(split, dict)
        or split.get("last_good_origin_attestation") != evidence
    ):
        raise DrainIdentityMismatch("durable last-good split attestation changed")
    existing = phases.get("gateway_last_good_attested")
    if existing is None and "rollback_started" not in phases:
        gateway_last_good = _attest_managed_gateway_binding(
            plan,
            plan["last_good_gateway_identity"],
        )
        receipt = {
            "binding": gateway_last_good,
            "last_good_origin_attestation": evidence,
        }
        return record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="gateway_last_good_attested",
            receipt=receipt,
        )
    if (
        not isinstance(existing, dict)
        or existing.get("last_good_origin_attestation") != evidence
    ):
        raise DrainIdentityMismatch("durable gateway last-good split attestation changed")
    binding = existing.get("binding")
    if not isinstance(binding, dict):
        raise DrainIdentityMismatch("durable gateway last-good binding changed")
    candidate_start = phases.get("candidate_gateway_start_intent")
    if candidate_start is not None and (
        not isinstance(candidate_start, dict)
        or candidate_start.get("last_good_binding") != binding
    ):
        raise DrainIdentityMismatch("candidate gateway last-good binding changed")
    drain_intent = phases.get("gateway_drain_intent")
    if drain_intent is None:
        current_binding = _attest_managed_gateway_binding(
            plan,
            plan["last_good_gateway_identity"],
        )
        if binding != current_binding:
            raise DrainIdentityMismatch("durable gateway last-good binding changed")
    elif (
        not isinstance(drain_intent, dict)
        or drain_intent.get("last_good_binding_sha256")
        != _canonical_journal_value_sha256(binding)
    ):
        raise DrainIdentityMismatch(
            "durable gateway drain binding anchor changed"
        )
    return journal


def _run_release_commit_plan_core(
    plan: dict,
    *,
    dry_run: bool = False,
    bootstrap_prepare_pair: Callable[[dict], dict] | None = None,
    bootstrap_open_pair: Callable[[dict], dict] | None = None,
    managed_watchdog_readiness: Callable[[], dict] | None = None,
) -> dict:
    if dry_run:
        inspected = _inspect_cutover_plan(plan)
        inspected["status"] = "dry-run"
        inspected["actions"] = [
            "reconcile-journal",
            "fence-old",
            "activate-selector",
            "replace-process",
            "accept-candidate",
            "promote-selector-and-cli",
        ]
        return inspected
    last_good_origin_attestation = _preflight_last_good_identity_split(plan)
    paired_safety_keys = (
        _BOOTSTRAP_GATEWAY_PLAN_KEYS
        | _BOOTSTRAP_WATCHDOG_PLAN_KEYS
        | _BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS
    )
    if paired_safety_keys.intersection(plan) != paired_safety_keys:
        raise ReleaseBuildError(
            "release commit requires the complete paired safety transaction"
        )
    journal = _reconcile_cutover_journal(plan)
    journal = _ensure_last_good_split_attested(
        plan,
        journal,
        last_good_origin_attestation=last_good_origin_attestation,
    )
    journal = _ensure_gateway_last_good_attested(
        plan,
        journal,
        last_good_origin_attestation=last_good_origin_attestation,
    )
    bootstrap_rollback = _bootstrap_rollback_context(plan, journal)
    inspect_control, send_control, client_transaction = _release_control_client(
        plan["base_url"],
        _read_release_control_key(plan["signing_key_file"]),
        transaction_id=plan["transaction_id"],
        request_timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if client_transaction != plan["transaction_id"]:
        raise ReleaseBuildError("release-control client transaction changed")

    def restore_plist() -> dict:
        expected = journal["rollback_receipt"]["plist_sha256"]
        return _atomic_copy_file(
            plan["bootstrap_rollback_plist"],
            plan["installed_plist"],
            expected_sha256=expected,
            mode=int(journal["rollback_receipt"].get("plist_mode") or 0o600),
        )

    def stop_failed_candidate() -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        identity = None
        for phase in (
            "candidate_accepted",
            "replacement_proved",
        ):
            value = current["phases"].get(phase, {}).get("identity")
            if isinstance(value, dict):
                identity = value
                break
        if identity is None:
            return {"status": "not-running"}
        try:
            pid = int(identity.get("pid"))
        except (TypeError, ValueError) as exc:
            raise DrainIdentityMismatch("candidate stop PID is invalid") from exc
        expected_start = str(identity.get("pid_start_token") or "")
        if _pid_start_token(pid) != expected_start:
            return {"status": "not-running", "pid": pid}
        bootout = _bootout_launchd_job(plan, required=True)
        wait_for_exact_process_exit(identity, float(plan["timeout_seconds"]))
        return {**bootout, "pid": pid, "pid_start_token": expected_start}

    def record_gateway_rollback_phase(phase: str, receipt: dict) -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        if phase in current["phases"]:
            return current
        return record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )

    def ensure_gateway_rollback_boundary() -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        gateway_phases = current["phases"]
        if "rollback_gateway_stop_intent" not in gateway_phases:
            try:
                candidate = _attest_managed_gateway_binding(
                    plan,
                    plan["expected_candidate_identity"],
                    expected_admission="rejecting_new_work",
                )
            except Exception as candidate_error:
                try:
                    last_good = _attest_managed_gateway_binding(
                        plan,
                        plan["last_good_gateway_identity"],
                        expected_admission="accepting_new_work",
                    )
                except Exception:
                    try:
                        drained_last_good = _attest_managed_gateway_binding(
                            plan,
                            plan["last_good_gateway_identity"],
                            expected_admission="rejecting_new_work",
                        )
                    except Exception:
                        try:
                            listener = _listener_pid(
                                int(plan["gateway_listener_port"])
                            )
                        except DrainIdentityMismatch:
                            listener = None
                        if (
                            listener is not None
                            or _job_pid(plan, gateway=True) is not None
                        ):
                            raise DrainIdentityMismatch(
                                "rollback gateway owner is not an authorized build"
                            ) from candidate_error
                        stop_receipt = {"status": "not-running"}
                    else:
                        rollback_prepared = {
                            "gateway": {
                                "pid": int(drained_last_good["listener_pid"]),
                                "pid_start_token": str(
                                    drained_last_good["pid_start_token"]
                                ),
                            }
                        }
                        stop_receipt = {
                            "status": "prepared",
                            "prepared": rollback_prepared,
                            "intent": _legacy_gateway_stop_intent_receipt(
                                plan,
                                rollback_prepared,
                                {
                                    "status": "verified",
                                    "binding": drained_last_good,
                                    "admission": "rejecting_new_work",
                                },
                            ),
                        }
                else:
                    stop_receipt = {
                        "status": "preserve-live-last-good",
                        "binding": last_good,
                    }
            else:
                rollback_prepared = {
                    "gateway": {
                        "pid": int(candidate["listener_pid"]),
                        "pid_start_token": str(candidate["pid_start_token"]),
                    }
                }
                stop_receipt = {
                    "status": "prepared",
                    "prepared": rollback_prepared,
                    "intent": _legacy_gateway_stop_intent_receipt(
                        plan,
                        rollback_prepared,
                        {
                            "status": "verified",
                            "binding": candidate,
                            "admission": "rejecting_new_work",
                        },
                    ),
                }
            current = record_gateway_rollback_phase(
                "rollback_gateway_stop_intent",
                stop_receipt,
            )
            gateway_phases = current["phases"]
        stop_receipt = gateway_phases["rollback_gateway_stop_intent"]
        if "rollback_gateway_gracefully_stopped" not in gateway_phases:
            if stop_receipt.get("status") == "prepared":
                stopped = _gracefully_stop_legacy_gateway(
                    plan,
                    stop_receipt["prepared"],
                    stop_receipt["intent"],
                )
            else:
                stopped = {
                    "status": stop_receipt.get("status"),
                    "gateway": stop_receipt.get("binding"),
                }
            current = record_gateway_rollback_phase(
                "rollback_gateway_gracefully_stopped",
                stopped,
            )
            gateway_phases = current["phases"]
        preserved = (
            stop_receipt.get("status") == "preserve-live-last-good"
        )
        if "rollback_gateway_dispatcher_lock_acquired" not in gateway_phases:
            lock = (
                {"status": "not-required-preserved-last-good"}
                if preserved
                else _acquire_legacy_dispatcher_lock(plan)
            )
            current = record_gateway_rollback_phase(
                "rollback_gateway_dispatcher_lock_acquired",
                lock,
            )
            gateway_phases = current["phases"]
        lock = gateway_phases["rollback_gateway_dispatcher_lock_acquired"]
        if not preserved:
            _verify_legacy_dispatcher_lock(plan, lock)
        if "rollback_gateway_workers_quiescent" not in gateway_phases:
            workers = (
                {"status": "not-required-preserved-last-good"}
                if preserved
                else _wait_for_legacy_kanban_quiescence(plan)
            )
            current = record_gateway_rollback_phase(
                "rollback_gateway_workers_quiescent",
                workers,
            )
            gateway_phases = current["phases"]
        return {
            "preserved": preserved,
            "lock": lock,
            "stop": gateway_phases["rollback_gateway_gracefully_stopped"],
        }

    def restart_selection() -> dict:
        gateway_boundary = ensure_gateway_rollback_boundary()
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        gateway_phases = current["phases"]
        if "state_snapshot_restored" not in gateway_phases:
            raise ReleaseBuildError(
                "gateway rollback cannot reopen before state rollback receipt"
            )
        if "rollback_gateway_plist_restored" not in gateway_phases:
            backup = Path(plan["gateway_rollback_plist"])
            restored_gateway_plist = _atomic_copy_file(
                backup,
                plan["gateway_installed_plist"],
                expected_sha256=sha256_file(backup),
                mode=stat.S_IMODE(backup.stat().st_mode),
            )
            current = record_gateway_rollback_phase(
                "rollback_gateway_plist_restored",
                restored_gateway_plist,
            )
            gateway_phases = current["phases"]
        if "rollback_gateway_drain_cleared" not in gateway_phases:
            drain_phase = gateway_phases.get("gateway_drain_intent")
            drain_intent = (
                drain_phase.get("intent")
                if isinstance(drain_phase, dict)
                else None
            )
            if drain_intent is None and bootstrap_rollback is not None:
                drain_intent = bootstrap_rollback["drain_intent"]
            cleared = (
                _clear_legacy_gateway_drain_marker(plan, drain_intent)
                if isinstance(drain_intent, dict)
                else {"status": "not-required"}
            )
            current = record_gateway_rollback_phase(
                "rollback_gateway_drain_cleared",
                cleared,
            )
            gateway_phases = current["phases"]
        if "rollback_gateway_dispatcher_lock_released" not in gateway_phases:
            if gateway_boundary["preserved"]:
                released = {"status": "not-required-preserved-last-good"}
            else:
                _verify_legacy_dispatcher_lock(
                    plan,
                    gateway_phases["rollback_gateway_dispatcher_lock_acquired"],
                )
                _wait_for_legacy_kanban_quiescence(plan)
                released = _release_legacy_dispatcher_lock(plan)
            current = record_gateway_rollback_phase(
                "rollback_gateway_dispatcher_lock_released",
                released,
            )
            gateway_phases = current["phases"]
        if bootstrap_rollback is not None:
            restored = _restart_or_adopt_restored_legacy_pair(
                plan,
                prepared=bootstrap_rollback["prepared"],
            )
            return {
                "status": "restored-legacy-bootstrap",
                **restored,
            }
        gateway_status: dict
        try:
            gateway_status = {
                "status": "already-running",
                "binding": _attest_managed_gateway_binding(
                    plan,
                    plan["last_good_gateway_identity"],
                ),
            }
        except Exception as exc:
            try:
                gateway_listener = _listener_pid(
                    int(plan["gateway_listener_port"])
                )
            except DrainIdentityMismatch:
                gateway_listener = None
            gateway_job = _job_pid(plan, gateway=True)
            if gateway_listener is not None or gateway_job is not None:
                raise DrainIdentityMismatch(
                    "rollback gateway is live with an unexpected identity"
                ) from exc
            gateway_status = {
                "status": "restarted",
                "start": _bootstrap_job(
                    plan,
                    plan["gateway_installed_plist"],
                    gateway=True,
                ),
                "binding": _attest_managed_gateway_binding(
                    plan,
                    plan["last_good_gateway_identity"],
                ),
            }
        _bootout_launchd_job(plan, required=False)
        webui = _bootstrap_launchd_job(plan, plan["installed_plist"])
        return {"gateway": gateway_status, "webui": webui}

    def bootout_old_process(identity: dict) -> dict:
        try:
            expected_pid = int(identity.get("pid"))
        except (TypeError, ValueError) as exc:
            raise DrainIdentityMismatch("launchd bootout PID is invalid") from exc
        if _launchd_pid(plan) != expected_pid:
            raise DrainIdentityMismatch(
                "launchd PID changed before durable bootout"
            )
        return _bootout_launchd_job(plan, required=True)

    def bootstrap_candidate_job() -> dict:
        gateway = _complete_candidate_gateway_transition(
            plan,
            {"status": "startup-fenced"},
        )
        installed = _installed_plist_attestation(plan)
        managed_sha256 = sha256_file(Path(plan["managed_plist"]))
        if installed["plist_sha256"] != managed_sha256:
            _atomic_copy_file(
                plan["managed_plist"],
                plan["installed_plist"],
                expected_sha256=managed_sha256,
                mode=0o600,
            )
        return {
            "gateway": gateway.get("gateway"),
            "webui": _bootstrap_launchd_job(plan, plan["installed_plist"]),
        }

    def promote_without_opening_admission() -> dict:
        promotion = _selector_transition(plan, "promote")
        attestation = _selector_state_attestation(plan)
        if (
            attestation["current"]
            != plan["expected_candidate_identity"]["build_id"]
            or attestation["candidate"] is not None
            or attestation["pending_transaction_id"] is not None
        ):
            raise ReleaseBuildError(
                "selector promotion did not persist before pair open"
            )
        return {
            "selector_and_cli": promotion,
            "selector_attestation": attestation,
            "admission": "startup-fenced",
        }

    def restore_snapshot() -> dict:
        gateway_boundary = ensure_gateway_rollback_boundary()
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        receipt = current["phases"].get("paired_state_snapshot_created")
        if not isinstance(receipt, dict):
            raise ReleaseBuildError(
                "candidate gateway mutation has no joint-boundary snapshot"
            )
        restored = restore_state_snapshot_from_manifest(
            receipt["manifest_path"],
            expected_snapshot_id=receipt["state_snapshot_id"],
            expected_manifest_sha256=receipt["state_snapshot_sha256"],
        )
        restored["gateway_stop"] = gateway_boundary["stop"]
        restored["gateway_dispatcher_lock"] = gateway_boundary["lock"]
        return restored

    def verify_rollback() -> dict:
        selector_receipt = _selector_state_attestation(plan)
        if (
            selector_receipt["current"]
            != plan["last_good_identity"]["build_id"]
            or selector_receipt["candidate"] is not None
            or selector_receipt["pending_transaction_id"] is not None
        ):
            raise ReleaseBuildError("rollback selector is not last-good")
        plist_receipt = _installed_plist_attestation(plan)
        if (
            plist_receipt["plist_sha256"]
            != journal["rollback_receipt"]["plist_sha256"]
        ):
            raise ReleaseBuildError("rollback plist identity changed")
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        snapshot = current["phases"].get("paired_state_snapshot_created")
        if isinstance(snapshot, dict):
            _manifest, state_receipt = _read_verified_state_snapshot(
                snapshot["manifest_path"],
                expected_snapshot_id=snapshot["state_snapshot_id"],
                expected_manifest_sha256=snapshot["state_snapshot_sha256"],
                live=True,
            )
        else:
            rollback = current["rollback_receipt"]
            state_receipt = {
                "status": "verified",
                "state_snapshot_id": rollback["state_snapshot_id"],
                "state_snapshot_sha256": rollback["state_snapshot_sha256"],
                "reason": "joint boundary was never crossed",
            }
        link = Path(plan["cli_link"])
        if not link.is_symlink() or os.readlink(link) != str(plan["cli_old_target"]):
            raise ReleaseBuildError("rollback Hermes CLI entry is not exact")
        launchd_pid = _launchd_pid(plan)
        if launchd_pid != _listener_pid(int(plan["listener_port"])):
            raise ReleaseBuildError("rollback launchd/listener binding is invalid")
        health = _http_json(
            f"{str(plan['base_url']).rstrip('/')}/health",
            timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
        )
        if health.get("status") != "ok":
            raise ReleaseBuildError("rollback legacy health is not healthy")
        if bootstrap_rollback is not None:
            gateway = _attest_restored_legacy_binding(
                plan,
                prepared=bootstrap_rollback["prepared"],
                gateway=True,
            )
            webui = _attest_restored_legacy_binding(
                plan,
                prepared=bootstrap_rollback["prepared"],
                gateway=False,
            )
            if (
                gateway.get("status") != "verified"
                or webui.get("status") != "verified"
            ):
                raise ReleaseBuildError(
                    "rollback restored legacy pair is not healthy"
                )
        else:
            gateway = _attest_managed_gateway_binding(
                plan,
                plan["last_good_gateway_identity"],
            )
            if gateway.get("status") != "verified":
                raise ReleaseBuildError(
                    "rollback managed gateway is not healthy"
                )
        return state_receipt

    try:
        initial = inspect_control()
    except ReleaseBuildError:
        initial = None

    def attest_managed_watchdog_readiness() -> dict:
        if managed_watchdog_readiness is not None:
            readiness = managed_watchdog_readiness()
            expected_status = "verified-disabled-barrier"
        else:
            readiness = _attest_managed_watchdog_readiness(plan)
            expected_status = "verified"
        if (
            not isinstance(readiness, dict)
            or readiness.get("status") != expected_status
        ):
            raise ReleaseBuildError("managed watchdog readiness receipt is invalid")
        return readiness

    def prepare_pair(candidate_identity: dict) -> dict:
        gateway = _complete_candidate_gateway_transition(
            plan,
            {"status": "startup-fenced"},
        )
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        gateway_binding = current["phases"].get(
            "candidate_gateway_accepted",
            {},
        ).get("binding")
        if not isinstance(gateway_binding, dict):
            raise ReleaseBuildError("paired gateway binding receipt is missing")
        extra = (
            bootstrap_prepare_pair(candidate_identity)
            if bootstrap_prepare_pair is not None
            else attest_managed_watchdog_readiness()
        )
        return {
            "status": "ready",
            "gateway": gateway_binding,
            "gateway_transition": gateway.get("gateway"),
            "pre_open": extra,
        }

    def pair_gate_intent(candidate_identity: dict, pair_receipt: dict) -> dict:
        gateway_binding = pair_receipt.get("gateway")
        if not isinstance(gateway_binding, dict):
            raise ReleaseBuildError("pair-open gate has no gateway binding")
        return _pair_open_gate_intent_receipt(
            plan,
            candidate_identity,
            gateway_binding,
        )

    def install_pair_gate(_candidate_identity: dict, intent: dict) -> dict:
        return _install_or_adopt_pair_open_gate(plan, intent)

    def open_pair(candidate_identity: dict) -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        gate_intent = current["phases"].get("pair_gate_install_intent")
        gate_installed = current["phases"].get("pair_gate_installed")
        if not isinstance(gate_intent, dict) or not isinstance(
            gate_installed,
            dict,
        ):
            raise ReleaseBuildError("paired gateway open has no shared gate")
        observed_gate = _install_or_adopt_pair_open_gate(plan, gate_intent)
        if any(
            observed_gate.get(key) != gate_installed.get(key)
            for key in ("owner_hash", "payload_sha256")
        ):
            raise DrainIdentityMismatch("paired gateway shared gate changed")
        if bootstrap_open_pair is not None:
            extra = bootstrap_open_pair(candidate_identity)
        else:
            live_readiness = attest_managed_watchdog_readiness()
            current = read_transaction_journal(
                plan["transaction_journal"],
                transaction_id=plan["transaction_id"],
            )
            drain_phase = current["phases"].get("gateway_drain_intent")
            drain_intent = (
                drain_phase.get("intent")
                if isinstance(drain_phase, dict)
                else None
            )
            if not isinstance(drain_intent, dict):
                raise ReleaseBuildError(
                    "paired gateway open has no durable drain-marker owner"
                )
            extra = _clear_legacy_gateway_drain_marker(
                plan,
                drain_intent,
            )
            extra = {
                "drain": extra,
                "live_readiness": live_readiness,
            }
        gateway = _attest_managed_gateway_binding(
            plan,
            plan["expected_candidate_identity"],
            expected_admission="rejecting_new_work",
            expected_pair_gate=_expected_agent_pair_gate_receipt(
                gate_intent,
                active=True,
            ),
        )
        return {
            "status": "ready-behind-pair-gate",
            "gateway": gateway,
            "pair_gate": gateway.get("health", {})
            .get("drain", {})
            .get("pair_open_gate"),
            "open": extra,
        }

    def release_pair(
        candidate_identity: dict,
        release_intent: dict,
    ) -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        phases = current["phases"]
        gate_intent = phases.get("pair_gate_install_intent")
        gate_installed = phases.get("pair_gate_installed")
        if (
            not isinstance(gate_intent, dict)
            or not isinstance(gate_installed, dict)
            or release_intent.get("owner_hash")
            != gate_installed.get("owner_hash")
            or release_intent.get("payload_sha256")
            != gate_installed.get("payload_sha256")
        ):
            raise ReleaseBuildError("pair-open release intent is invalid")
        release_state = _pair_open_gate_release_state(
            plan,
            gate_intent,
            gate_installed,
        )
        if release_state == "active":
            gateway_gated = _attest_managed_gateway_binding(
                plan,
                plan["expected_candidate_identity"],
                expected_admission="rejecting_new_work",
                expected_pair_gate=_expected_agent_pair_gate_receipt(
                    gate_intent,
                    active=True,
                ),
            )
            gateway_gate = gateway_gated["health"]["drain"]["pair_open_gate"]
            webui_gated = _require_candidate_binding(
                _collect_process_binding(
                    plan,
                    inspect_control=inspect_control,
                ),
                candidate_identity=candidate_identity,
                expected_candidate_identity=plan["expected_candidate_identity"],
                admission_state="open",
                require_full_health=True,
                allow_promoted_generation=True,
            )
            webui_gate_kwargs = {"active": True}
            if (
                {
                    "pair_accepted",
                    "pair_gate_release_intent",
                }.issubset(phases)
                and _promoted_candidate_identity_matches(
                    candidate_identity,
                    plan["expected_candidate_identity"],
                )
            ):
                webui_gate_kwargs["allow_normalized_restart"] = True
            webui_gate = _require_webui_pair_gate_state(
                webui_gated,
                gate_intent,
                **webui_gate_kwargs,
            )
        else:
            gateway_gate = {
                "status": "adopted-durable-release-intent",
                "active": True,
            }
            webui_gate = {
                "status": "adopted-durable-release-intent",
                "active": True,
            }
        release_barrier = (
            attest_managed_watchdog_readiness()
            if managed_watchdog_readiness is not None
            else None
        )
        released = _release_owned_pair_open_gate(
            plan,
            gate_intent,
            gate_installed,
        )
        expected_release_status = (
            "released" if release_state == "active" else "already-released"
        )
        if released.get("status") != expected_release_status:
            raise DrainIdentityMismatch(
                "pair-open gate release state changed during commit"
            )
        gateway_open = _attest_managed_gateway_binding(
            plan,
            plan["expected_candidate_identity"],
            expected_admission="accepting_new_work",
            expected_pair_gate=_expected_agent_pair_gate_receipt(
                gate_intent,
                active=False,
            ),
        )
        webui_open = _require_candidate_binding(
            _wait_for_expected_binding(
                plan,
                inspect_control=inspect_control,
                expected_identity=plan["expected_candidate_identity"],
                admission_state="open",
                require_startup_markers_cleared=False,
            ),
            candidate_identity=candidate_identity,
            expected_candidate_identity=plan["expected_candidate_identity"],
            admission_state="open",
            require_full_health=True,
            allow_promoted_generation=True,
        )
        webui_released = _require_webui_pair_gate_state(
            webui_open,
            gate_intent,
            active=False,
        )
        return {
            "release": released,
            "opened": {
                "status": "verified",
                "owner_hash": gate_installed["owner_hash"],
                "payload_sha256": gate_installed["payload_sha256"],
                "webui_gated": webui_gate,
                "webui_open": webui_released,
                "gateway_gated": gateway_gate,
                "gateway_open": gateway_open["health"]["drain"][
                    "pair_open_gate"
                ],
                "watchdog_barrier": release_barrier,
            },
        }

    def wait_for_booted_out_process_exit(
        identity: dict,
        timeout_seconds: float,
    ) -> None:
        wait_for_exact_process_exit(
            identity,
            timeout_seconds,
            allow_exact_signaled_zombie=True,
        )

    def begin_pair_checkpoint(context: dict, identity: dict, fence_token: str) -> dict:
        current = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        gateway_drained = current["phases"].get("gateway_drained")
        if not isinstance(gateway_drained, dict):
            gateway_drained = _prepare_legacy_gateway_drain(plan)
        if not isinstance(gateway_drained, dict):
            raise ReleaseBuildError(
                "legacy gateway drain must be durably proved before checkpoint"
            )
        return send_control(
            "begin_checkpoint",
            identity,
            fence_token,
            {"deadline": context},
        )

    def dispatch_pair_checkpoint(
        context: dict,
        identity: dict,
        fence_token: str,
    ) -> dict:
        return send_control(
            "checkpoint_threads",
            identity,
            fence_token,
        )

    def poll_pair_checkpoint(
        context: dict,
        identity: dict,
        fence_token: str,
    ) -> dict:
        status = send_control(
            "checkpoint_threads_status",
            identity,
            fence_token,
        )
        admission = status.get("admission")
        state = status.get("state") if isinstance(status, dict) else None
        ledger = state.get("ledger") if isinstance(state, dict) else None
        targets = ledger.get("targets") if isinstance(ledger, dict) else None
        complete = (
            isinstance(admission, dict)
            and int(admission.get("reservations", -1)) == 0
            and isinstance(targets, dict)
            and all(
                isinstance(target, dict)
                and target.get("state") in {"acknowledged", "settled_without_ack"}
                for target in targets.values()
            )
        )
        return {"status": status, "complete": complete}

    def close_pair_checkpoint(
        context: dict,
        forced: bool,
        identity: dict,
        fence_token: str,
    ) -> dict:
        return send_control(
            "checkpoint_threads_close",
            identity,
            fence_token,
            {"forced": bool(forced)},
        )

    result = run_release_control_cutover(
        initial_inspection=initial,
        inspect_control=inspect_control,
        send_control=send_control,
        attest_selector_state=lambda: _selector_state_attestation(plan),
        attest_installed_plist=lambda: _installed_plist_attestation(plan),
        activate_selection=lambda: _selector_transition(plan, "activate"),
        promote_selection=promote_without_opening_admission,
        rollback_selection=lambda: _selector_transition(plan, "rollback"),
        restore_plist=restore_plist,
        stop_failed_candidate=stop_failed_candidate,
        restore_state_snapshot=restore_snapshot,
        restart_selection=restart_selection,
        verify_rollback=verify_rollback,
        signal_process=signal_exact_release_process,
        wait_for_process_exit=wait_for_booted_out_process_exit,
        inspect_candidate_binding=lambda _identity: _collect_process_binding(
            plan,
            inspect_control=inspect_control,
        ),
        inspect_accepted_binding=lambda _identity: _collect_process_binding(
            plan,
            inspect_control=inspect_control,
        ),
        expected_candidate_identity=plan["expected_candidate_identity"],
        expected_last_good_identity=plan["last_good_identity"],
        transaction_id=plan["transaction_id"],
        transaction_journal_path=plan["transaction_journal"],
        timeout_seconds=float(plan["timeout_seconds"]),
        interval_seconds=float(plan["interval_seconds"]),
        bootout_process=bootout_old_process,
        bootstrap_candidate_job=bootstrap_candidate_job,
        prepare_pair_before_commit=prepare_pair,
        pair_gate_intent_before_commit=pair_gate_intent,
        install_pair_gate_before_commit=install_pair_gate,
        open_pair_after_promotion=open_pair,
        release_pair_after_acceptance=release_pair,
        attest_legacy_activity_drain=lambda identity, inspection: (
            _attest_legacy_webui_activity_drain(
                plan,
                identity,
                inspection,
                inspect_control=inspect_control,
            )
        ),
        begin_pair_checkpoint=begin_pair_checkpoint,
        dispatch_pair_checkpoint=dispatch_pair_checkpoint,
        poll_pair_checkpoint=poll_pair_checkpoint,
        close_pair_checkpoint=close_pair_checkpoint,
        force_restart_on_rollback=True,
    )
    return result


def _run_release_commit_plan(
    plan: dict,
    *,
    dry_run: bool = False,
    bootstrap_prepare_pair: Callable[[dict], dict] | None = None,
    bootstrap_open_pair: Callable[[dict], dict] | None = None,
    watchdog_prepared: dict | None = None,
) -> dict:
    """Prepare watchdog subprocesses, then commit with their writer excluded."""
    if dry_run:
        return _run_release_commit_plan_core(
            plan,
            dry_run=True,
            bootstrap_prepare_pair=bootstrap_prepare_pair,
            bootstrap_open_pair=bootstrap_open_pair,
        )
    prepared_bootstrap_pair: dict | None = None
    core_bootstrap_prepare_pair = bootstrap_prepare_pair
    if bootstrap_prepare_pair is not None:
        expected_identity = plan.get("expected_candidate_identity")
        if not isinstance(expected_identity, dict):
            raise ReleaseBuildError(
                "bootstrap paired readiness has no candidate identity"
            )
        prepared_bootstrap_pair = bootstrap_prepare_pair(
            copy.deepcopy(expected_identity)
        )
        if (
            not isinstance(prepared_bootstrap_pair, dict)
            or prepared_bootstrap_pair.get("status") != "ready"
        ):
            raise ReleaseBuildError(
                "bootstrap paired readiness receipt is invalid"
            )

        def use_prepared_bootstrap_pair(candidate_identity: dict) -> dict:
            if not _candidate_identity_matches(
                candidate_identity,
                expected_identity,
            ):
                raise DrainIdentityMismatch(
                    "bootstrap paired readiness candidate changed"
                )
            return copy.deepcopy(prepared_bootstrap_pair)

        core_bootstrap_prepare_pair = use_prepared_bootstrap_pair
    last_good_origin_attestation = _preflight_last_good_identity_split(plan)
    journal = _reconcile_cutover_journal(plan)
    journal = _ensure_last_good_split_attested(
        plan,
        journal,
        last_good_origin_attestation=last_good_origin_attestation,
    )
    _ensure_gateway_last_good_attested(
        plan,
        journal,
        last_good_origin_attestation=last_good_origin_attestation,
    )
    barrier = _begin_release_watchdog_barrier(
        plan,
        prepared=watchdog_prepared,
    )

    def attest_held_watchdog_barrier() -> dict:
        return _attest_release_watchdog_barrier(plan, barrier)

    try:
        result = _run_release_commit_plan_core(
            plan,
            bootstrap_prepare_pair=core_bootstrap_prepare_pair,
            bootstrap_open_pair=bootstrap_open_pair,
            managed_watchdog_readiness=attest_held_watchdog_barrier,
        )
    except BaseException as original:
        try:
            _finish_release_watchdog_barrier(plan, barrier)
        except Exception as barrier_error:
            raise ReleaseBuildError(
                f"paired release failed: {original}; watchdog barrier finish failed: "
                f"{barrier_error}"
            ) from original
        raise
    result["watchdog_barrier"] = _finish_release_watchdog_barrier(
        plan,
        barrier,
    )
    return result


def _prepare_legacy_gateway_drain(plan: dict) -> dict:
    """Persist the legacy gateway drain proof before paired WebUI checkpointing."""
    journal = read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )
    phases = journal["phases"]

    def record(phase: str, receipt: dict) -> None:
        nonlocal journal, phases
        if phase in phases:
            return
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )
        phases = journal["phases"]

    drained = phases.get("gateway_drained")
    if isinstance(drained, dict):
        return drained

    last_good_binding = phases.get("gateway_last_good_attested", {}).get(
        "binding"
    )
    if not isinstance(last_good_binding, dict):
        raise ReleaseBuildError("candidate gateway has no last-good receipt")
    if "gateway_drain_intent" not in phases:
        last_good_gateway = _attest_managed_gateway_binding(
            plan,
            plan["last_good_gateway_identity"],
        )
        durable_runtime = last_good_binding.get("runtime")
        if (
            not isinstance(durable_runtime, dict)
            or not _runtime_receipt_matches(
                last_good_gateway["runtime"], durable_runtime
            )
            or last_good_gateway.get("listener_pid")
            != last_good_binding.get("listener_pid")
            or last_good_gateway.get("pid_start_token")
            != last_good_binding.get("pid_start_token")
        ):
            raise DrainIdentityMismatch(
                "last-good gateway changed before durable drain intent"
            )
        prepared = {
            "gateway": {
                "pid": int(last_good_gateway["listener_pid"]),
                "pid_start_token": str(last_good_gateway["pid_start_token"]),
            }
        }
        record(
            "gateway_drain_intent",
            {
                "prepared": prepared,
                "intent": _legacy_gateway_drain_intent_receipt(
                    plan,
                    prepared,
                ),
                "last_good_binding_sha256": (
                    _canonical_journal_value_sha256(last_good_binding)
                ),
            },
        )
    drain_phase = phases["gateway_drain_intent"]
    prepared = drain_phase.get("prepared")
    drain_intent = drain_phase.get("intent")
    if not isinstance(prepared, dict) or not isinstance(drain_intent, dict):
        raise ReleaseBuildError("durable gateway drain intent is invalid")
    if "gateway_drained" not in phases:
        _write_legacy_gateway_drain_marker(plan, drain_intent)
        record(
            "gateway_drained",
            _wait_for_legacy_gateway_drain(
                plan,
                prepared,
                drain_intent,
            ),
        )
    drained = phases.get("gateway_drained")
    if not isinstance(drained, dict):
        raise ReleaseBuildError("legacy gateway drain receipt is invalid")
    return drained


def _complete_candidate_gateway_transition(plan: dict, result: dict) -> dict:
    """Finish or adopt the durable candidate-gateway half of a release."""
    journal = read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )
    phases = journal["phases"]

    def record(phase: str, receipt: dict) -> None:
        nonlocal journal, phases
        if phase in phases:
            return
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase=phase,
            receipt=receipt,
        )
        phases = journal["phases"]

    if "candidate_gateway_accepted" in phases:
        accepted_gateway = _attest_managed_gateway_binding(
            plan,
            plan["expected_candidate_identity"],
            expected_admission="rejecting_new_work",
        )
        result["gateway"] = accepted_gateway
        return result

    candidate_gateway: dict | None = None
    try:
        candidate_gateway = _attest_managed_gateway_binding(
            plan,
            plan["expected_candidate_identity"],
            expected_admission="rejecting_new_work",
        )
    except Exception:
        candidate_gateway = None

    if "gateway_gracefully_stopped" not in phases and candidate_gateway is not None:
        bootstrap = _read_bootstrap_journal(plan)
        bootstrap_phases = bootstrap["phases"]
        historical = {
            "gateway_drain_intent": "legacy_gateway_drain_intent",
            "gateway_drained": "legacy_gateway_drain_acknowledged",
            "gateway_stop_intent": "legacy_gateway_stop_intent",
            "gateway_gracefully_stopped": "legacy_gateway_gracefully_stopped",
            "gateway_dispatcher_lock_acquired": (
                "legacy_dispatcher_lock_acquired"
            ),
            "gateway_workers_quiescent": "frozen_boundary_proved",
            "paired_state_snapshot_created": "snapshot_created",
            "gateway_dispatcher_lock_released": (
                "legacy_dispatcher_lock_released"
            ),
        }
        for cutover_phase, bootstrap_phase in historical.items():
            receipt = bootstrap_phases.get(bootstrap_phase)
            if not isinstance(receipt, dict):
                raise ReleaseBuildError(
                    "running candidate gateway has no exact bootstrap stop history"
                )
            if cutover_phase == "paired_state_snapshot_created":
                adopted = {
                    **receipt,
                    "status": "adopted-bootstrap-snapshot",
                    "bootstrap_phase": bootstrap_phase,
                }
            else:
                adopted = {
                    "status": "adopted-bootstrap-receipt",
                    "bootstrap_phase": bootstrap_phase,
                    "receipt": receipt,
                }
                if cutover_phase == "gateway_drain_intent":
                    adopted["last_good_binding_sha256"] = (
                        _canonical_journal_value_sha256(
                            phases["gateway_last_good_attested"]["binding"]
                        )
                    )
            record(cutover_phase, adopted)

    if "gateway_gracefully_stopped" not in phases:
        _prepare_legacy_gateway_drain(plan)
        journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        phases = journal["phases"]
        if "gateway_stop_intent" not in phases:
            drain_phase = phases["gateway_drain_intent"]
            prepared = drain_phase.get("prepared")
            record(
                "gateway_stop_intent",
                {
                    "prepared": prepared,
                    "intent": _legacy_gateway_stop_intent_receipt(
                        plan,
                        prepared,
                        phases["gateway_drained"],
                    ),
                },
            )
        stop_phase = phases["gateway_stop_intent"]
        stop_prepared = stop_phase.get("prepared")
        stop_intent = stop_phase.get("intent")
        if not isinstance(stop_prepared, dict) or not isinstance(stop_intent, dict):
            raise ReleaseBuildError("durable gateway stop intent is invalid")
        record(
            "gateway_gracefully_stopped",
            _gracefully_stop_legacy_gateway(
                plan,
                stop_prepared,
                stop_intent,
            ),
        )

    if "gateway_dispatcher_lock_acquired" not in phases:
        record(
            "gateway_dispatcher_lock_acquired",
            _acquire_legacy_dispatcher_lock(plan),
        )
    elif "gateway_dispatcher_lock_released" not in phases:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["gateway_dispatcher_lock_acquired"],
        )
    if "gateway_workers_quiescent" not in phases:
        record(
            "gateway_workers_quiescent",
            _wait_for_legacy_kanban_quiescence(plan),
        )
    if "paired_state_snapshot_created" not in phases:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["gateway_dispatcher_lock_acquired"],
        )
        _wait_for_legacy_kanban_quiescence(plan)
        writer_barrier = _prove_no_mutable_writers(plan)
        base_root = Path(plan["snapshot_root"])
        base_manifest = Path(plan["snapshot_manifest"])
        paired_root = base_root.with_name(
            f"{base_root.name}.paired-{plan['transaction_id']}"
        )
        paired_manifest = base_manifest.with_name(
            f"{base_manifest.stem}.paired-{plan['transaction_id']}"
            f"{base_manifest.suffix}"
        )
        paired_snapshot = create_state_snapshot(
            plan["mutable_state_paths"],
            snapshot_root=paired_root,
            manifest_path=paired_manifest,
            snapshot_id=plan["transaction_id"],
        )
        paired_snapshot["writer_barrier"] = _prove_no_mutable_writers(
            plan,
            expected=writer_barrier,
        )
        record("paired_state_snapshot_created", paired_snapshot)
    if "gateway_dispatcher_lock_released" not in phases:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["gateway_dispatcher_lock_acquired"],
        )
        _wait_for_legacy_kanban_quiescence(plan)
        record(
            "gateway_dispatcher_lock_released",
            _release_legacy_dispatcher_lock(plan),
        )

    if "candidate_gateway_start_intent" not in phases:
        last_good_binding = (
            phases.get("gateway_last_good_attested", {}).get("binding")
        )
        if not isinstance(last_good_binding, dict):
            raise ReleaseBuildError("candidate gateway has no last-good receipt")
        record(
            "candidate_gateway_start_intent",
            {
                "last_good_binding": last_good_binding,
                "candidate_build_id": plan["expected_candidate_identity"][
                    "build_id"
                ],
                "candidate_shim_sha256": hashlib.sha256(
                    _render_cli_shim(plan["expected_candidate_identity"])
                ).hexdigest(),
            },
        )

    gateway_install: dict | None = None
    gateway_start: dict | None = None
    if candidate_gateway is None:
        gateway_install = _install_managed_gateway_plist(
            plan,
            None,
            plan["expected_candidate_identity"],
        )
        try:
            gateway_listener = _listener_pid(int(plan["gateway_listener_port"]))
        except DrainIdentityMismatch:
            gateway_listener = None
        gateway_job = _job_pid(plan, gateway=True)
        if gateway_listener is None and gateway_job is None:
            gateway_start = _bootstrap_job(
                plan,
                plan["gateway_installed_plist"],
                gateway=True,
            )
        elif gateway_listener is None or gateway_job != gateway_listener:
            raise DrainIdentityMismatch(
                "candidate gateway launch boundary is ambiguous"
            )
        candidate_gateway = _attest_managed_gateway_binding(
            plan,
            plan["expected_candidate_identity"],
            expected_admission="rejecting_new_work",
        )
    journal = record_transaction_phase(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
        phase="candidate_gateway_accepted",
        receipt={
            "binding": candidate_gateway,
            "install": gateway_install or {"status": "externally-reconciled"},
            "start": gateway_start or {"status": "externally-reconciled"},
        },
    )
    result["gateway"] = journal["phases"]["candidate_gateway_accepted"]
    return result


def _release_record_from_identity(identity: dict) -> dict:
    record = {
        key: identity.get(key)
        for key in ("manifest_sha256", "commit", "tree")
    }
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(record["manifest_sha256"] or ""))
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(record["commit"] or ""))
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(record["tree"] or ""))
    ):
        raise ReleaseBuildError("release identity has no selector record")
    return record


_BOOTSTRAP_PHASE_PREREQUISITES = {
    "prepared": (),
    "pre_managed_controls_stage_intent": ("prepared",),
    "pre_managed_controls_staged": ("pre_managed_controls_stage_intent",),
    "watchdog_cron_disabled": ("pre_managed_controls_staged",),
    "writers_frozen": ("watchdog_cron_disabled",),
    "cli_maintenance_gate_stage_intent": ("writers_frozen",),
    "cli_maintenance_gate_installed": (
        "cli_maintenance_gate_stage_intent",
    ),
    "legacy_cron_tick_lock_normalize_intent": (
        "cli_maintenance_gate_installed",
    ),
    "legacy_cron_tick_lock_normalized": (
        "legacy_cron_tick_lock_normalize_intent",
    ),
    "legacy_cron_tick_lock_acquired": (
        "legacy_cron_tick_lock_normalized",
    ),
    "legacy_gateway_drain_intent": ("legacy_cron_tick_lock_acquired",),
    "legacy_gateway_drain_acknowledged": ("legacy_gateway_drain_intent",),
    "legacy_gateway_stop_intent": ("legacy_gateway_drain_acknowledged",),
    "legacy_gateway_gracefully_stopped": ("legacy_gateway_stop_intent",),
    "synthetic_store_mode_normalize_intent": (
        "legacy_gateway_gracefully_stopped",
    ),
    "synthetic_store_modes_normalized": (
        "synthetic_store_mode_normalize_intent",
    ),
    "legacy_dispatcher_lock_acquired": (
        "synthetic_store_modes_normalized",
    ),
    "frozen_boundary_proved": ("legacy_dispatcher_lock_acquired",),
    "legacy_jobs_booted_out": ("frozen_boundary_proved",),
    "ingress_gate_start_intent": ("legacy_jobs_booted_out",),
    "services_stopped": ("ingress_gate_start_intent",),
    "legacy_cron_tick_lock_released": ("services_stopped",),
    "ingress_gate_started": ("legacy_cron_tick_lock_released",),
    "snapshot_created": ("ingress_gate_started",),
    "synthetic_state_quarantine_intent": ("snapshot_created",),
    "synthetic_state_quarantined": ("synthetic_state_quarantine_intent",),
    "ingress_gate_stopped": ("synthetic_state_quarantined",),
    "managed_pair_start_intent": ("ingress_gate_stopped",),
    "legacy_dispatcher_lock_released": ("managed_pair_start_intent",),
    "managed_pair_started": ("legacy_dispatcher_lock_released",),
    "cutover_handed_off": ("managed_pair_started",),
    "watchdog_installed": ("cutover_handed_off",),
    "watchdog_reconciled_once": ("watchdog_installed",),
    "watchdog_reconciled_twice": ("watchdog_reconciled_once",),
    "legacy_gateway_drain_cleared": ("watchdog_reconciled_twice",),
    "candidate_pair_accepted": ("legacy_gateway_drain_cleared",),
    "cli_candidate_activate_intent": ("candidate_pair_accepted",),
    "cli_candidate_activated": ("cli_candidate_activate_intent",),
    "watchdog_cron_restored": ("cli_candidate_activated",),
    "complete": ("watchdog_cron_restored",),
    "aborted_before_cutover": ("prepared",),
    "rollback_started": ("legacy_jobs_booted_out",),
    "rollback_gateway_stop_intent": ("rollback_started",),
    "rollback_services_stopped": ("rollback_gateway_stop_intent",),
    "rollback_cron_tick_lock_released": ("rollback_services_stopped",),
    "rollback_dispatcher_lock_acquired": (
        "rollback_cron_tick_lock_released",
    ),
    "rollback_workers_quiescent": ("rollback_dispatcher_lock_acquired",),
    "rollback_state_restored": ("rollback_workers_quiescent",),
    "rollback_synthetic_state_requarantined": ("rollback_state_restored",),
    "rollback_plists_restored": ("rollback_synthetic_state_requarantined",),
    "rollback_watchdog_restored": ("rollback_plists_restored",),
    "rollback_gateway_drain_cleared": ("rollback_watchdog_restored",),
    "rollback_dispatcher_lock_released": ("rollback_gateway_drain_cleared",),
    "rollback_cron_tick_lock_restored": (
        "rollback_dispatcher_lock_released",
    ),
    "rollback_synthetic_store_modes_restored": (
        "rollback_cron_tick_lock_restored",
    ),
    "rollback_services_restarted": (
        "rollback_synthetic_store_modes_restored",
    ),
    "rollback_cron_restored": ("rollback_services_restarted",),
    "rollback_verified": ("rollback_cron_restored",),
}


def _bootstrap_journal_path(plan: dict) -> Path:
    transaction = Path(plan["transaction_journal"])
    return transaction.with_name(f"{transaction.name}.bootstrap")


def _validated_bootstrap_journal(raw: object, transaction_id: str) -> dict:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "transaction_id", "phases"}
        or raw.get("version") != 1
        or raw.get("transaction_id") != transaction_id
        or not isinstance(raw.get("phases"), dict)
        or _journal_contains_sensitive_value(raw)
    ):
        raise ReleaseBuildError("bootstrap journal schema is invalid")
    phases: dict[str, dict] = {}
    for phase, receipt in raw["phases"].items():
        if phase not in _BOOTSTRAP_PHASE_PREREQUISITES or not isinstance(
            receipt, dict
        ):
            raise ReleaseBuildError("bootstrap journal phase is invalid")
        phases[phase] = copy.deepcopy(receipt)
    for phase in phases:
        if any(
            prerequisite not in phases
            for prerequisite in _BOOTSTRAP_PHASE_PREREQUISITES[phase]
        ):
            raise ReleaseBuildError("bootstrap journal phase order is invalid")
    if "rollback_started" in phases and "complete" in phases:
        raise ReleaseBuildError("bootstrap journal has conflicting terminal phases")
    if "aborted_before_cutover" in phases and any(
        phase in phases
        for phase in (
            "services_stopped",
            "ingress_gate_started",
            "rollback_started",
            "complete",
        )
    ):
        raise ReleaseBuildError("bootstrap journal has conflicting abort phases")
    return {
        "version": 1,
        "transaction_id": transaction_id,
        "phases": phases,
    }


def _can_restore_legacy_before_snapshot_abort(phases: object) -> bool:
    return isinstance(phases, dict) and not any(
        phase in phases
        for phase in (
            "services_stopped",
            "ingress_gate_started",
            "snapshot_created",
            "rollback_started",
            "complete",
        )
    )


def _read_bootstrap_journal(plan: dict) -> dict:
    path, lock_path = _transaction_journal_paths(_bootstrap_journal_path(plan))
    with _with_transaction_journal_lock(lock_path):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_uid != os.getuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                ):
                    raise ReleaseBuildError("bootstrap journal is unsafe")
                payload = handle.read(4 * 1024 * 1024 + 1)
        except ReleaseBuildError:
            raise
        except OSError as exc:
            raise ReleaseBuildError("bootstrap journal is unreadable") from exc
        if len(payload) > 4 * 1024 * 1024:
            raise ReleaseBuildError("bootstrap journal is too large")
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseBuildError("bootstrap journal JSON is invalid") from exc
        return _validated_bootstrap_journal(raw, plan["transaction_id"])


def _record_bootstrap_phase(
    plan: dict,
    phase: str,
    receipt: dict,
    *,
    crash_at: str | None = None,
) -> dict:
    if phase not in _BOOTSTRAP_PHASE_PREREQUISITES:
        raise ReleaseBuildError("bootstrap journal phase is invalid")
    if not isinstance(receipt, dict) or _journal_contains_sensitive_value(receipt):
        raise ReleaseBuildError("bootstrap journal receipt contains sensitive data")
    path, lock_path = _transaction_journal_paths(_bootstrap_journal_path(plan))
    with _with_transaction_journal_lock(lock_path):
        if path.exists():
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600
                    ):
                        raise ReleaseBuildError("bootstrap journal is unsafe")
                    payload = handle.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise ReleaseBuildError("bootstrap journal is too large")
                raw = json.loads(payload)
                journal = _validated_bootstrap_journal(raw, plan["transaction_id"])
            except ReleaseBuildError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseBuildError("bootstrap journal is unreadable") from exc
        else:
            journal = {
                "version": 1,
                "transaction_id": plan["transaction_id"],
                "phases": {},
            }
        existing = journal["phases"].get(phase)
        if existing is not None:
            if existing != receipt:
                raise ReleaseBuildError(
                    "bootstrap phase already has a different receipt"
                )
            return journal
        missing = [
            prerequisite
            for prerequisite in _BOOTSTRAP_PHASE_PREREQUISITES[phase]
            if prerequisite not in journal["phases"]
        ]
        if missing:
            raise ReleaseBuildError(
                "bootstrap phase prerequisites are missing: " + ", ".join(missing)
            )
        journal["phases"][phase] = copy.deepcopy(receipt)
        _atomic_write_transaction_journal(path, journal, crash_at=crash_at)
        return journal


def _prepare_bootstrap_selector(plan: dict) -> dict:
    state_path = Path(plan["selector_state"])
    if not state_path.exists():
        last_good = plan["last_good_identity"]
        release_path = Path(str(last_good.get("release_path") or ""))
        if not release_path.is_absolute() or release_path.name != last_good["build_id"]:
            raise ReleaseBuildError("last-good release path is invalid")
        release_selector.initialize_selector_state(
            state_path,
            lock_path=plan["selector_lock"],
            release_root=release_path.parent,
            bootstrap_build_id=last_good["build_id"],
            bootstrap_record=_release_record_from_identity(last_good),
        )
    state = release_selector.read_selector_state(
        state_path,
        lock_path=plan["selector_lock"],
    )
    candidate = plan["expected_candidate_identity"]
    candidate_id = candidate["build_id"]
    if state["candidate"] is None:
        state = release_selector.update_selector_state(
            state_path,
            lock_path=plan["selector_lock"],
            expected_generation=state["generation"],
            transition=lambda current: release_selector.stage_candidate(
                current,
                candidate_id,
                _release_record_from_identity(candidate),
                transaction_id=plan["transaction_id"],
            ),
        )
    if (
        state["candidate"] != candidate_id
        or state.get("pending_transaction_id") != plan["transaction_id"]
    ):
        raise ReleaseBuildError("bootstrap selector transaction changed")
    return state


def _attest_candidate_startup_generation(
    plan: dict,
    staged_state: dict,
) -> dict:
    candidate = plan.get("expected_candidate_identity")
    last_good = plan.get("last_good_identity")
    if not isinstance(candidate, dict) or not isinstance(last_good, dict):
        raise ReleaseBuildError("candidate startup generation identity is missing")
    selector_generation = candidate.get("selector_generation")
    staged_generation = staged_state.get("generation")
    if (
        not isinstance(selector_generation, int)
        or isinstance(selector_generation, bool)
        or not isinstance(staged_generation, int)
        or isinstance(staged_generation, bool)
        or staged_state.get("current") != last_good.get("build_id")
        or staged_state.get("candidate") != candidate.get("build_id")
        or staged_state.get("pending_transaction_id") != plan.get("transaction_id")
        or selector_generation != staged_generation + 1
    ):
        raise ReleaseBuildError(
            "candidate selector generation is not the post-activation generation"
        )
    return {
        "staged_generation": staged_generation,
        "startup_generation": selector_generation,
    }


def _read_private_regular_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[bytes, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(f"{label} cannot be read without O_NOFOLLOW")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_size < 0
            or opened.st_size > max_bytes
        ):
            raise ReleaseBuildError(f"{label} is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ReleaseBuildError(f"{label} is too large")
        finished = os.fstat(descriptor)
        current = path.lstat()
        if any(
            getattr(finished, field) != getattr(opened, field)
            or getattr(current, field) != getattr(opened, field)
            for field in stable_fields
        ):
            raise ReleaseBuildError(f"{label} changed while reading")
        return b"".join(chunks), opened
    except OSError as exc:
        raise ReleaseBuildError(f"{label} changed while reading") from exc
    finally:
        os.close(descriptor)


def _write_private_bytes_exact(path: Path, payload: bytes, *, label: str) -> None:
    parent = _prepare_release_root(path.parent)
    if path.is_symlink():
        raise ReleaseBuildError(f"{label} path is unsafe")
    if path.exists():
        current, _opened = _read_private_regular_bytes(path, label=label)
        if current != payload:
            raise ReleaseBuildError(f"{label} already has another identity")
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        replaced = True
        _fsync_directory(parent)
    finally:
        if not replaced:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    observed, _opened = _read_private_regular_bytes(path, label=label)
    if observed != payload:
        raise ReleaseBuildError(f"{label} write did not persist")


def _pre_managed_backup_root(plan: dict) -> Path:
    journal = Path(str(plan.get("transaction_journal") or ""))
    transaction_id = str(plan.get("transaction_id") or "")
    if (
        not journal.is_absolute()
        or Path(os.path.abspath(journal)) != journal
        or not _TRANSACTION_ID.fullmatch(transaction_id)
    ):
        raise ReleaseBuildError("pre-managed control backup identity is invalid")
    return journal.parent / f".{journal.name}.{transaction_id}.pre-managed"


def _capture_pre_managed_control_state(plan: dict) -> dict:
    root = _prepare_release_root(_pre_managed_backup_root(plan))
    result: dict[str, object] = {
        "status": "captured",
        "backup_root": str(root),
    }
    for key, backup_name in (
        ("selector_state", "selector-state.before"),
        ("selector_lock", "selector-lock.before"),
        ("managed_plist", "managed-plist.before"),
    ):
        path = Path(str(plan.get(key) or ""))
        if not path.is_absolute() or Path(os.path.abspath(path)) != path:
            raise ReleaseBuildError(f"pre-managed {key} path is invalid")
        if not path.exists() and not path.is_symlink():
            result[key] = {
                "path": str(path),
                "status": "absent",
                "backup_path": None,
            }
            continue
        payload, opened = _read_private_regular_bytes(
            path,
            label=f"pre-managed {key}",
        )
        backup = root / backup_name
        _write_private_bytes_exact(
            backup,
            payload,
            label=f"pre-managed {key} backup",
        )
        result[key] = {
            "path": str(path),
            "status": "present",
            "backup_path": str(backup),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "mode": stat.S_IMODE(opened.st_mode),
            "uid": opened.st_uid,
        }
    return result


def _captured_control_bytes(captured: dict, key: str) -> bytes | None:
    entry = captured.get(key) if isinstance(captured, dict) else None
    if not isinstance(entry, dict):
        raise ReleaseBuildError(f"captured pre-managed {key} is invalid")
    if entry.get("status") == "absent":
        return None
    backup = Path(str(entry.get("backup_path") or ""))
    payload, _opened = _read_private_regular_bytes(
        backup,
        label=f"captured pre-managed {key}",
    )
    if (
        entry.get("status") != "present"
        or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
        or len(payload) != entry.get("size")
    ):
        raise ReleaseBuildError(f"captured pre-managed {key} changed")
    return payload


def _pre_managed_control_stage_intent_receipt(
    plan: dict,
    prepared: dict,
) -> dict:
    captured = prepared.get("pre_managed_controls")
    if not isinstance(captured, dict) or captured.get("status") != "captured":
        raise ReleaseBuildError("pre-managed control snapshot is missing")
    previous_selector = _captured_control_bytes(captured, "selector_state")
    if previous_selector is None:
        last_good = plan["last_good_identity"]
        release_path = Path(str(last_good.get("release_path") or ""))
        selector_state = {
            "version": release_selector.STATE_VERSION,
            "generation": 0,
            "release_root": str(release_path.parent.absolute()),
            "current": last_good["build_id"],
            "candidate": None,
            "pending_transaction_id": None,
            "last_good": last_good["build_id"],
            "bootstrap_fallback": last_good["build_id"],
            "releases": {
                last_good["build_id"]: _release_record_from_identity(last_good)
            },
        }
    else:
        try:
            selector_state = release_selector._validate_state(
                json.loads(previous_selector)
            )
        except (UnicodeDecodeError, json.JSONDecodeError, release_selector.SelectorError) as exc:
            raise ReleaseBuildError(
                "captured selector state cannot be staged"
            ) from exc
    candidate = plan["expected_candidate_identity"]
    candidate_id = candidate["build_id"]
    if selector_state["candidate"] is None:
        selector_state = release_selector.stage_candidate(
            selector_state,
            candidate_id,
            _release_record_from_identity(candidate),
            transaction_id=plan["transaction_id"],
        )
        selector_state["generation"] += 1
    if (
        selector_state["candidate"] != candidate_id
        or selector_state.get("pending_transaction_id") != plan["transaction_id"]
    ):
        raise ReleaseBuildError("pre-managed selector stage intent changed")
    _attest_candidate_startup_generation(plan, selector_state)
    selector_payload = (
        json.dumps(selector_state, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    previous_lock = _captured_control_bytes(captured, "selector_lock")
    lock_payload = previous_lock if previous_lock is not None else b""
    transformed = transform_launchd_target(
        _read_plist(plan["bootstrap_rollback_plist"]),
        plan["selector_path"],
        expected_label=plan["launchd_label"],
        expected_old_interpreter=prepared["legacy"]["program_arguments"][0],
        managed_interpreter=plan["managed_interpreter"],
        expected_old_target=(
            prepared["legacy"]["program_arguments"][2]
            if prepared["legacy"]["program_arguments"][1] == "-S"
            else prepared["legacy"]["program_arguments"][1]
        ),
        selector_state_path=plan["selector_state"],
        selector_lock_path=plan["selector_lock"],
        managed_routing_environment=prepared["legacy"]["routing_environment"],
    )
    plist_payload = plistlib.dumps(
        transformed,
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )

    def expected(path_key: str, payload: bytes) -> dict:
        return {
            "path": str(plan[path_key]),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "mode": 0o600,
            "uid": os.getuid(),
        }

    return {
        "status": "prepared",
        "expected": {
            "status": "staged",
            "selector_state": expected("selector_state", selector_payload),
            "selector_lock": expected("selector_lock", lock_payload),
            "managed_plist": expected("managed_plist", plist_payload),
        },
    }


def _private_control_file_receipt(path: Path, *, label: str) -> dict:
    payload, opened = _read_private_regular_bytes(path, label=label)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "mode": stat.S_IMODE(opened.st_mode),
        "uid": opened.st_uid,
    }


def _pre_managed_control_stage_receipt(plan: dict) -> dict:
    return {
        "status": "staged",
        "selector_state": _private_control_file_receipt(
            Path(plan["selector_state"]),
            label="staged selector state",
        ),
        "selector_lock": _private_control_file_receipt(
            Path(plan["selector_lock"]),
            label="staged selector lock",
        ),
        "managed_plist": _private_control_file_receipt(
            Path(plan["managed_plist"]),
            label="staged managed plist",
        ),
    }


def _stage_pre_managed_controls(plan: dict, prepared: dict) -> dict:
    _prepare_bootstrap_selector(plan)
    transformed = transform_launchd_target(
        _read_plist(plan["bootstrap_rollback_plist"]),
        plan["selector_path"],
        expected_label=plan["launchd_label"],
        expected_old_interpreter=prepared["legacy"]["program_arguments"][0],
        managed_interpreter=plan["managed_interpreter"],
        expected_old_target=(
            prepared["legacy"]["program_arguments"][2]
            if prepared["legacy"]["program_arguments"][1] == "-S"
            else prepared["legacy"]["program_arguments"][1]
        ),
        selector_state_path=plan["selector_state"],
        selector_lock_path=plan["selector_lock"],
        managed_routing_environment=prepared["legacy"][
            "routing_environment"
        ],
    )
    _write_plist_atomic(plan["managed_plist"], transformed)
    return _pre_managed_control_stage_receipt(plan)


def _owned_forward_selector_transition(
    plan: dict,
    intended: dict,
    current_receipt: dict,
) -> dict | None:
    candidate = plan.get("expected_candidate_identity")
    last_good = plan.get("last_good_identity")
    if not isinstance(candidate, dict) or not isinstance(last_good, dict):
        return None
    candidate_id = str(candidate.get("build_id") or "")
    last_good_id = str(last_good.get("build_id") or "")
    transaction_id = str(plan.get("transaction_id") or "")
    try:
        startup_generation = int(candidate.get("selector_generation"))
    except (TypeError, ValueError):
        return None
    if (
        not candidate_id
        or not last_good_id
        or not _TRANSACTION_ID.fullmatch(transaction_id)
        or startup_generation <= 0
        or any(
            current_receipt.get(field) != intended.get(field)
            for field in ("mode", "uid")
        )
    ):
        return None
    try:
        journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=transaction_id,
        )
    except ReleaseBuildError:
        return None
    if journal.get("expected_candidate_identity") != candidate:
        return None
    phases = journal["phases"]
    path = Path(plan["selector_state"])
    payload, opened = _read_private_regular_bytes(
        path,
        label="forward selector state",
    )
    stable_receipt = {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "mode": stat.S_IMODE(opened.st_mode),
        "uid": opened.st_uid,
    }
    if stable_receipt != current_receipt:
        raise DrainIdentityMismatch(
            "pre-managed selector_state changed during forward adoption"
        )
    try:
        state = release_selector._validate_state(json.loads(payload))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        release_selector.SelectorError,
    ):
        return None

    transition: str
    durable_selection: object
    generation_delta: int
    promoted = phases.get("promoted")
    activated = phases.get("selection_activated")
    if isinstance(promoted, dict):
        durable_selection = (
            promoted.get("promotion", {})
            .get("selector_and_cli", {})
            .get("selector")
        )
        transition = "promoted"
        generation_delta = 2
        if "pair_commit_intent" not in phases:
            return None
    elif isinstance(activated, dict):
        durable_selection = activated.get("selection")
        transition = "activated"
        generation_delta = 1
    else:
        return None
    if state != durable_selection:
        return None

    staged_state = copy.deepcopy(state)
    try:
        staged_state["generation"] = int(staged_state["generation"]) - (
            generation_delta
        )
    except (KeyError, TypeError, ValueError):
        return None
    if transition == "promoted":
        if (
            state.get("generation") != startup_generation + 1
            or state.get("current") != candidate_id
            or state.get("last_good") != last_good_id
            or state.get("candidate") is not None
            or state.get("pending_transaction_id") is not None
        ):
            return None
        staged_state["last_good"] = last_good_id
        staged_state["candidate"] = candidate_id
        staged_state["pending_transaction_id"] = transaction_id
    else:
        if (
            state.get("generation") != startup_generation
            or state.get("current") != candidate_id
            or state.get("last_good") != last_good_id
            or state.get("candidate") != candidate_id
            or state.get("pending_transaction_id") != transaction_id
        ):
            return None
    staged_state["current"] = last_good_id
    try:
        staged_state = release_selector._validate_state(staged_state)
    except release_selector.SelectorError:
        return None
    encoded = (
        json.dumps(staged_state, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if (
        len(encoded) != intended.get("size")
        or hashlib.sha256(encoded).hexdigest() != intended.get("sha256")
    ):
        return None
    return {
        "status": "verified",
        "transition": transition,
        "selection": state,
        "identity": stable_receipt,
    }


def _adopt_or_restage_pre_managed_controls(
    plan: dict,
    prepared: dict,
    expected: dict,
) -> dict:
    captured = prepared.get("pre_managed_controls")
    if (
        not isinstance(captured, dict)
        or captured.get("status") != "captured"
        or not isinstance(expected, dict)
        or expected.get("status") != "staged"
    ):
        raise ReleaseBuildError(
            "pre-managed control resume receipt is invalid"
        )
    missing_owned_creation = False
    selector_transition: dict | None = None
    for key in ("selector_state", "selector_lock", "managed_plist"):
        path = Path(str(plan.get(key) or ""))
        before = captured.get(key)
        intended = expected.get(key)
        if (
            not isinstance(before, dict)
            or before.get("path") != str(path)
            or not isinstance(intended, dict)
            or intended.get("path") != str(path)
        ):
            raise ReleaseBuildError(
                f"pre-managed {key} resume identity is invalid"
            )
        if not path.exists() and not path.is_symlink():
            if before.get("status") != "absent":
                raise DrainIdentityMismatch(
                    f"pre-managed {key} disappeared after rollback"
                )
            missing_owned_creation = True
            continue
        current = _private_control_file_receipt(
            path,
            label=f"staged {key}",
        )
        if current != intended:
            if key == "selector_state":
                selector_transition = _owned_forward_selector_transition(
                    plan,
                    intended,
                    current,
                )
                if selector_transition is not None:
                    continue
            raise DrainIdentityMismatch(
                f"pre-managed {key} changed on bootstrap resume"
            )
    if missing_owned_creation and selector_transition is not None:
        raise DrainIdentityMismatch(
            "pre-managed controls disappeared after owned selector transition"
        )
    observed = (
        _stage_pre_managed_controls(plan, prepared)
        if missing_owned_creation
        else _pre_managed_control_stage_receipt(plan)
    )
    if selector_transition is not None:
        if (
            observed.get("selector_lock") != expected.get("selector_lock")
            or observed.get("managed_plist") != expected.get("managed_plist")
            or observed.get("selector_state")
            != selector_transition.get("identity")
        ):
            raise DrainIdentityMismatch(
                "pre-managed forward selector adoption changed"
            )
        return {
            **observed,
            "status": "adopted-owned-forward-transition",
            "selector_transition": selector_transition,
        }
    if observed != expected:
        raise DrainIdentityMismatch(
            "pre-managed control restage changed before commit"
        )
    return observed


def _rollback_exact_owned_selector_activation(
    plan: dict,
    before: dict,
    owned: dict,
    current_receipt: dict,
) -> dict | None:
    candidate = plan.get("expected_candidate_identity")
    last_good = plan.get("last_good_identity")
    if not isinstance(candidate, dict) or not isinstance(last_good, dict):
        return None
    candidate_id = str(candidate.get("build_id") or "")
    last_good_id = str(last_good.get("build_id") or "")
    transaction_id = str(plan.get("transaction_id") or "")
    if not candidate_id or not last_good_id or not transaction_id:
        return None
    if any(
        current_receipt.get(field) != owned.get(field)
        for field in ("mode", "uid")
    ):
        return None
    backup = Path(str(before.get("backup_path") or ""))
    try:
        original_payload, _original_stat = _read_private_regular_bytes(
            backup,
            label="pre-managed selector_state backup",
        )
        if (
            before.get("status") != "present"
            or hashlib.sha256(original_payload).hexdigest()
            != before.get("sha256")
            or len(original_payload) != before.get("size")
        ):
            return None
        original_state = json.loads(original_payload)
        original_last_good = original_state["last_good"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    def is_owned_transition(state: dict, generation_delta: int) -> bool:
        try:
            generation = int(state["generation"])
        except (KeyError, TypeError, ValueError):
            return False
        if generation < generation_delta:
            return False
        staged_state = copy.deepcopy(state)
        staged_state["generation"] = generation - generation_delta
        staged_state["current"] = last_good_id
        staged_state["candidate"] = candidate_id
        staged_state["pending_transaction_id"] = transaction_id
        staged_state["last_good"] = original_last_good
        encoded = (
            json.dumps(staged_state, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        return (
            len(encoded) == owned.get("size")
            and hashlib.sha256(encoded).hexdigest() == owned.get("sha256")
        )

    state = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    activated = (
        state["current"] == candidate_id
        and state["candidate"] == candidate_id
        and state.get("pending_transaction_id") == transaction_id
        and state["last_good"] == last_good_id
        and is_owned_transition(state, 1)
    )
    already_rolled_back = (
        state["current"] == last_good_id
        and state["candidate"] is None
        and state.get("pending_transaction_id") is None
        and state["last_good"] == last_good_id
        and candidate_id in state["releases"]
        and is_owned_transition(state, 2)
    )
    if already_rolled_back:
        exact = _private_control_file_receipt(
            Path(plan["selector_state"]),
            label="rolled-back selector state",
        )
        if release_selector.read_selector_state(
            plan["selector_state"],
            lock_path=plan["selector_lock"],
        ) != state:
            raise DrainIdentityMismatch(
                "rolled-back selector state changed during restore"
            )
        return {
            "status": "already-rolled-back-owned-activation",
            "selection": state,
            "identity": exact,
        }
    if not activated:
        return None

    expected_generation = int(state["generation"])

    def rollback_if_still_owned(current: dict) -> dict:
        if (
            current["current"] != candidate_id
            or current["candidate"] != candidate_id
            or current.get("pending_transaction_id") != transaction_id
            or current["last_good"] != last_good_id
            or not is_owned_transition(current, 1)
        ):
            raise DrainIdentityMismatch(
                "owned selector activation changed before rollback"
            )
        return release_selector.rollback_to_last_good(current)

    selected = release_selector.update_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
        expected_generation=expected_generation,
        transition=rollback_if_still_owned,
    )
    if (
        selected["generation"] != expected_generation + 1
        or selected["current"] != last_good_id
        or selected["last_good"] != last_good_id
        or selected["candidate"] is not None
        or selected.get("pending_transaction_id") is not None
    ):
        raise DrainIdentityMismatch("owned selector rollback receipt is invalid")
    exact = _private_control_file_receipt(
        Path(plan["selector_state"]),
        label="rolled-back selector state",
    )
    return {
        "status": "rolled-back-owned-activation",
        "selection": selected,
        "identity": exact,
    }


def _restore_pre_managed_control_state(
    plan: dict,
    captured: dict,
    staged: dict,
) -> dict:
    if (
        not isinstance(captured, dict)
        or captured.get("status") != "captured"
        or not isinstance(staged, dict)
        or staged.get("status") != "staged"
    ):
        raise ReleaseBuildError("pre-managed control restore receipt is invalid")
    restored: dict[str, dict] = {}
    for key in ("selector_state", "selector_lock", "managed_plist"):
        path = Path(str(plan.get(key) or ""))
        before = captured.get(key)
        owned = staged.get(key)
        if (
            not isinstance(before, dict)
            or before.get("path") != str(path)
            or not isinstance(owned, dict)
            or owned.get("path") != str(path)
        ):
            raise ReleaseBuildError(f"pre-managed {key} restore identity is invalid")
        if not path.exists() and not path.is_symlink():
            if before.get("status") == "absent":
                restored[key] = {"status": "already-absent", "path": str(path)}
                continue
            raise ReleaseBuildError(f"pre-managed {key} disappeared before restore")
        current = _private_control_file_receipt(path, label=f"staged {key}")
        if before.get("status") == "present" and all(
            current.get(field) == before.get(field)
            for field in ("sha256", "size", "mode", "uid")
        ):
            restored[key] = {"status": "already-restored", "identity": current}
            continue
        if any(
            current.get(field) != owned.get(field)
            for field in ("sha256", "size", "mode", "uid")
        ):
            if key == "selector_state":
                selector_rollback = _rollback_exact_owned_selector_activation(
                    plan,
                    before,
                    owned,
                    current,
                )
                if selector_rollback is not None:
                    restored[key] = selector_rollback
                    continue
            raise ReleaseBuildError(f"pre-managed {key} changed before restore")
        if before.get("status") == "absent":
            path.unlink()
            _fsync_directory(path.parent)
            if path.exists() or path.is_symlink():
                raise ReleaseBuildError(f"pre-managed {key} owned file survived")
            restored[key] = {"status": "removed", "path": str(path)}
            continue
        backup = Path(str(before.get("backup_path") or ""))
        backup_payload, _backup_stat = _read_private_regular_bytes(
            backup,
            label=f"pre-managed {key} backup",
        )
        if (
            before.get("status") != "present"
            or hashlib.sha256(backup_payload).hexdigest() != before.get("sha256")
            or len(backup_payload) != before.get("size")
        ):
            raise ReleaseBuildError(f"pre-managed {key} backup changed")
        copied = _atomic_copy_file(
            backup,
            path,
            expected_sha256=str(before["sha256"]),
            mode=int(before["mode"]),
        )
        os.chown(path, int(before["uid"]), -1)
        _fsync_directory(path.parent)
        exact = _private_control_file_receipt(
            path,
            label=f"restored {key}",
        )
        if any(
            exact.get(field) != before.get(field)
            for field in ("sha256", "size", "mode", "uid")
        ):
            raise ReleaseBuildError(f"pre-managed {key} restore is not exact")
        restored[key] = {**copied, "status": "restored", "identity": exact}
    return {"status": "restored", "controls": restored}


def _legacy_idle_health(plan: dict) -> dict:
    rows = []
    for _attempt in range(2):
        health = _http_json(
            f"{str(plan['base_url']).rstrip('/')}/health",
            timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
        )
        try:
            active_runs = int(health.get("active_runs", -1))
            active_streams = int(health.get("active_streams", -1))
        except (TypeError, ValueError) as exc:
            raise ReleaseBuildError("legacy health activity is invalid") from exc
        if health.get("status") != "ok" or active_runs != 0 or active_streams != 0:
            raise ReleaseBuildError("legacy WebUI is not idle for migration")
        rows.append(
            {
                "status": health.get("status"),
                "active_runs": active_runs,
                "active_streams": active_streams,
            }
        )
    return {"status": "verified", "checks": rows}


def _legacy_process_receipt(plan: dict) -> dict:
    receipt = _listener_process_receipt(
        plan,
        gateway=False,
        require_git_source=True,
    )
    receipt["cli"] = _file_identity_receipt(plan["cli_link"])
    return receipt


def _require_bootstrap_extensions(plan: dict) -> None:
    missing = (
        _BOOTSTRAP_GATEWAY_PLAN_KEYS
        | _BOOTSTRAP_WATCHDOG_PLAN_KEYS
        | _BOOTSTRAP_INGRESS_GATE_PLAN_KEYS
        | _BOOTSTRAP_LEGACY_BOUNDARY_PLAN_KEYS
    ) - set(plan)
    if missing:
        raise ReleaseBuildError(
            "bootstrap migration plan is incomplete: " + ", ".join(sorted(missing))
        )


def _copy_exact_backup(source: Path, backup: Path) -> dict:
    source_receipt = _file_identity_receipt(source)
    if backup.exists():
        backup_receipt = _file_identity_receipt(backup)
        if (
            backup_receipt["sha256"] != source_receipt["sha256"]
            or backup_receipt["resolved_size"] != source_receipt["resolved_size"]
        ):
            raise ReleaseBuildError("bootstrap backup conflicts with live artifact")
    else:
        _atomic_copy_file(
            source,
            backup,
            expected_sha256=str(source_receipt["sha256"]),
            mode=int(source_receipt["resolved_mode"]),
        )
        os.chown(backup, int(source_receipt["resolved_uid"]), -1)
        _fsync_directory(backup.parent)
    return source_receipt


def _read_crontab() -> str:
    try:
        completed = subprocess.run(
            ["crontab", "-l"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("watchdog cron receipt is unavailable") from exc
    return completed.stdout


def _watchdog_scheduler_backend(plan: dict) -> str:
    backend = str(plan.get("watchdog_scheduler_backend") or "crontab")
    if backend not in {"crontab", "hermes_internal"}:
        raise ReleaseBuildError("watchdog scheduler backend is unsupported")
    return backend


def _acquire_internal_watchdog_jobs_lock(plan: dict):
    registry = Path(str(plan.get("watchdog_scheduler_registry") or ""))
    if (
        not registry.is_absolute()
        or Path(os.path.abspath(registry)) != registry
        or registry.is_symlink()
        or not registry.exists()
    ):
        raise ReleaseBuildError("internal watchdog registry path is invalid")
    parent = registry.parent
    try:
        parent_info = parent.lstat()
        registry_info = registry.lstat()
    except OSError as exc:
        raise ReleaseBuildError("internal watchdog registry is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_ISLNK(parent_info.st_mode)
        or parent.resolve(strict=True) != parent
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o022
        or not stat.S_ISREG(registry_info.st_mode)
        or stat.S_ISLNK(registry_info.st_mode)
        or registry_info.st_uid != os.getuid()
        or registry_info.st_nlink != 1
        or stat.S_IMODE(registry_info.st_mode) & 0o077
        or not 2 <= registry_info.st_size <= 16 * 1024 * 1024
    ):
        raise ReleaseBuildError("internal watchdog registry is unsafe")
    lock_path = parent / ".jobs.lock"
    if lock_path.is_symlink():
        raise ReleaseBuildError("internal watchdog jobs lock is unsafe")
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise ReleaseBuildError("internal watchdog jobs lock is unavailable") from exc
    os.set_inheritable(descriptor, False)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise ReleaseBuildError("internal watchdog jobs lock is unsafe")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    deadline = time.monotonic() + min(
        30.0,
        max(1.0, float(plan.get("timeout_seconds", 30.0))),
    )
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                handle.close()
                raise ReleaseBuildError(
                    "internal watchdog jobs lock timed out"
                ) from exc
            time.sleep(min(0.05, float(plan.get("interval_seconds", 0.05))))
        except OSError as exc:
            handle.close()
            raise ReleaseBuildError(
                "internal watchdog jobs lock failed"
            ) from exc


_INTERNAL_WATCHDOG_RUNTIME_FIELDS = {
    "failure_class",
    "fire_claim",
    "last_delivery_error",
    "last_error",
    "last_run_at",
    "last_status",
    "manual_action_required",
    "next_retry_at",
    "next_run_at",
    "recovery_claim",
    "recovery_state",
    "run_claim",
}
_INTERNAL_WATCHDOG_CONTROL_FIELDS = {
    "enabled",
    "state",
    "paused_at",
    "paused_reason",
}


def _canonical_internal_watchdog_job(job: dict) -> bytes:
    return (
        json.dumps(
            job,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _internal_watchdog_stable_job(job: dict) -> dict:
    stable = copy.deepcopy(job)
    for field in (
        _INTERNAL_WATCHDOG_RUNTIME_FIELDS
        | _INTERNAL_WATCHDOG_CONTROL_FIELDS
    ):
        stable.pop(field, None)
    repeat = stable.get("repeat")
    if isinstance(repeat, dict):
        repeat = copy.deepcopy(repeat)
        repeat.pop("completed", None)
        stable["repeat"] = repeat
    return stable


def _internal_watchdog_stable_sha256(job: dict) -> str:
    return hashlib.sha256(
        _canonical_internal_watchdog_job(
            _internal_watchdog_stable_job(job)
        )
    ).hexdigest()


def _read_internal_watchdog_registry(
    plan: dict,
) -> tuple[Path, dict, list, int, dict]:
    registry = Path(plan["watchdog_scheduler_registry"])
    try:
        encoded, _opened = _read_private_regular_bytes(
            registry,
            label="internal watchdog registry",
            max_bytes=16 * 1024 * 1024,
        )
        payload = json.loads(encoded)
    except ReleaseBuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(
            "internal watchdog registry is invalid"
        ) from exc
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ReleaseBuildError("internal watchdog registry has no jobs")
    job_id = str(plan.get("watchdog_scheduler_job_id") or "")
    script_name = Path(str(plan["watchdog_installed_script"])).name
    by_id = [
        (index, job)
        for index, job in enumerate(jobs)
        if isinstance(job, dict) and str(job.get("id") or "") == job_id
    ]
    by_script = [
        (index, job)
        for index, job in enumerate(jobs)
        if isinstance(job, dict)
        and str(job.get("script") or "") == script_name
    ]
    if (
        len(by_id) != 1
        or len(by_script) != 1
        or by_id[0][0] != by_script[0][0]
    ):
        raise ReleaseBuildError(
            "internal watchdog job is missing or ambiguous"
        )
    index, job = by_id[0]
    return registry, payload, jobs, index, copy.deepcopy(job)


def _write_internal_watchdog_registry(plan: dict, payload: dict) -> None:
    registry = Path(plan["watchdog_scheduler_registry"])
    encoded = (
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{registry.name}.",
        dir=registry.parent,
    )
    replaced = False
    written_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
            written_identity = (written.st_dev, written.st_ino)
        os.replace(temporary, registry)
        replaced = True
        _fsync_directory(registry.parent)
    finally:
        if not replaced:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    observed, opened = _read_private_regular_bytes(
        registry,
        label="internal watchdog registry",
        max_bytes=16 * 1024 * 1024,
    )
    if (
        written_identity is None
        or (opened.st_dev, opened.st_ino) != written_identity
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or observed != encoded
    ):
        raise ReleaseBuildError(
            "internal watchdog registry write did not persist"
        )


def _internal_watchdog_job_receipt(
    plan: dict,
    *,
    require_active: bool = True,
) -> dict:
    handle = _acquire_internal_watchdog_jobs_lock(plan)
    try:
        registry, _payload, _jobs, _index, job = (
            _read_internal_watchdog_registry(plan)
        )
        if (
            job.get("no_agent") is not True
            or job.get("deliver") != "local"
        ):
            raise ReleaseBuildError(
                "internal watchdog job identity changed"
            )
        if require_active and (
            job.get("enabled") is not True
            or job.get("state") != "scheduled"
        ):
            raise ReleaseBuildError(
                "internal watchdog job is not active"
            )
        if not require_active and (
            (
                job.get("enabled") is not True
                or job.get("state") != "scheduled"
            )
            and (
                job.get("enabled") is not False
                or job.get("state") != "paused"
            )
        ):
            raise ReleaseBuildError(
                "internal watchdog job control state is unsupported"
            )
        canonical = _canonical_internal_watchdog_job(job)
        job_sha256 = hashlib.sha256(canonical).hexdigest()
        job_id = str(plan["watchdog_scheduler_job_id"])
        script_name = Path(str(plan["watchdog_installed_script"])).name
        controls = {
            key: job.get(key)
            for key in _INTERNAL_WATCHDOG_CONTROL_FIELDS
        }
        receipt = {
            "backend": "hermes_internal",
            "registry_path": str(registry),
            "job_id": job_id,
            "job_sha256": job_sha256,
            "stable_job_sha256": _internal_watchdog_stable_sha256(job),
            "job_enabled": job.get("enabled"),
            "job_state": job.get("state"),
            "crontab_sha256": job_sha256,
            "watchdog_command": f"hermes-internal:{job_id}:{script_name}",
            "canonical_job": canonical,
        }
        if not require_active:
            receipt["control_origin"] = (
                "active"
                if job.get("enabled") is True
                else "preexisting"
            )
            receipt["original_controls"] = controls
        return receipt
    finally:
        handle.close()


def _assert_no_os_watchdog_duplicate(plan: dict) -> None:
    installed = str(plan["watchdog_installed_script"])
    script_name = Path(installed).name
    duplicates = []
    for line in _read_crontab().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split(None, 1 if stripped.startswith("@") else 5)
        expected_fields = 2 if stripped.startswith("@") else 6
        if len(fields) != expected_fields:
            if installed in stripped or script_name in stripped:
                raise ReleaseBuildError(
                    "internal watchdog OS cron command is not parseable"
                )
            continue
        command = fields[-1]
        try:
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError as exc:
            if installed in command or script_name in command:
                raise ReleaseBuildError(
                    "internal watchdog OS cron command is not parseable"
                ) from exc
            continue
        pending = list(tokens)
        seen: set[str] = set()
        matched = False
        while pending:
            token = pending.pop(0)
            if token in seen:
                continue
            seen.add(token)
            if token == installed or Path(token).name == script_name:
                matched = True
                break
            if any(character.isspace() for character in token):
                try:
                    pending.extend(shlex.split(token, comments=True, posix=True))
                except ValueError:
                    if installed in token or script_name in token:
                        raise ReleaseBuildError(
                            "internal watchdog nested OS cron command is not parseable"
                        )
        if matched:
            duplicates.append(line)
    if duplicates:
        raise ReleaseBuildError(
            "internal watchdog has an active duplicate OS cron command"
        )


def _cron_watchdog_receipt(
    plan: dict,
    *,
    require_active: bool = True,
) -> dict:
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        _assert_no_os_watchdog_duplicate(plan)
        receipt = _internal_watchdog_job_receipt(
            plan,
            require_active=require_active,
        )
        receipt.pop("canonical_job")
        return receipt
    crontab = _read_crontab()
    installed = str(plan["watchdog_installed_script"])
    lines = [
        line
        for line in crontab.splitlines()
        if installed in line and line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise ReleaseBuildError("watchdog cron command is missing or ambiguous")
    return {
        "crontab_sha256": hashlib.sha256(crontab.encode()).hexdigest(),
        "watchdog_command": lines[0],
    }


def _backup_crontab(plan: dict) -> dict:
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        _assert_no_os_watchdog_duplicate(plan)
        receipt = _internal_watchdog_job_receipt(
            plan,
            require_active=False,
        )
        content = receipt.pop("canonical_job")
        backup = Path(plan["watchdog_crontab_rollback"])
        if backup.exists():
            if backup.is_symlink() or backup.read_bytes() != content:
                raise ReleaseBuildError(
                    "internal watchdog backup conflicts with live job"
                )
        else:
            parent = _prepare_release_root(backup.parent)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{backup.name}.",
                dir=parent,
            )
            replaced = False
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fchmod(handle.fileno(), 0o600)
                    os.fsync(handle.fileno())
                os.replace(temp_name, backup)
                replaced = True
                _fsync_directory(parent)
            finally:
                if not replaced:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass
        return {
            **receipt,
            "backup_path": str(backup),
            "backup_sha256": hashlib.sha256(content).hexdigest(),
        }
    content = _read_crontab().encode()
    backup = Path(plan["watchdog_crontab_rollback"])
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes() != content:
            raise ReleaseBuildError("watchdog crontab backup conflicts with live job")
    else:
        parent = _prepare_release_root(backup.parent)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{backup.name}.", dir=parent)
        replaced = False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())
            os.replace(temp_name, backup)
            replaced = True
            _fsync_directory(parent)
        finally:
            if not replaced:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
    return {
        **_cron_watchdog_receipt(plan),
        "backup_path": str(backup),
        "backup_sha256": hashlib.sha256(content).hexdigest(),
    }


def _read_internal_watchdog_rollback_job(
    plan: dict,
    cron: dict,
) -> dict:
    if not isinstance(cron, dict):
        raise ReleaseBuildError(
            "internal watchdog preparation is missing"
        )
    backup = Path(plan["watchdog_crontab_rollback"])
    payload, _opened = _read_private_regular_bytes(
        backup,
        label="internal watchdog rollback job",
    )
    if hashlib.sha256(payload).hexdigest() != cron.get("backup_sha256"):
        raise ReleaseBuildError(
            "internal watchdog rollback identity changed"
        )
    try:
        original = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(
            "internal watchdog rollback job is invalid"
        ) from exc
    if not isinstance(original, dict):
        raise ReleaseBuildError(
            "internal watchdog rollback job is invalid"
        )
    return original


def _upgrade_internal_watchdog_prepared_receipt(
    plan: dict,
    prepared: dict,
) -> dict:
    upgraded = copy.deepcopy(prepared)
    if _watchdog_scheduler_backend(plan) != "hermes_internal":
        return upgraded
    cron = (
        upgraded.get("watchdog_cron")
        if isinstance(upgraded, dict)
        else None
    )
    original = _read_internal_watchdog_rollback_job(plan, cron)
    stable_sha256 = _internal_watchdog_stable_sha256(original)
    declared = cron.get("stable_job_sha256")
    if declared is not None and declared != stable_sha256:
        raise ReleaseBuildError(
            "internal watchdog stable rollback identity changed"
        )
    cron["stable_job_sha256"] = stable_sha256
    return upgraded


def _install_crontab_file(path: Path) -> None:
    try:
        subprocess.run(
            ["crontab", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("watchdog crontab update failed") from exc


def _attest_internal_watchdog_drain_marker(
    plan: dict,
    prepared: dict,
) -> dict:
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    intent = cron.get("drain_intent") if isinstance(cron, dict) else None
    marker = intent.get("marker") if isinstance(intent, dict) else None
    path = Path(str(marker.get("path") or "")) if isinstance(marker, dict) else None
    if (
        not isinstance(marker, dict)
        or path != _legacy_gateway_drain_marker_path(plan)
        or not isinstance(marker.get("payload"), dict)
        or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("sha256") or ""))
        or not path.exists()
        or path.is_symlink()
        or _read_private_json_value(
            path,
            label="legacy gateway drain marker",
        )
        != marker["payload"]
        or sha256_file(path) != marker["sha256"]
    ):
        raise ReleaseBuildError(
            "internal watchdog gateway drain marker is not exact"
        )
    health = _legacy_gateway_health_with_drain(plan)
    drain = health.get("drain") if isinstance(health, dict) else None
    admission = drain.get("admission") if isinstance(drain, dict) else None
    cron_admission = (
        drain.get("cron_admission") if isinstance(drain, dict) else None
    )
    work = drain.get("work") if isinstance(drain, dict) else None
    quiescence = drain.get("quiescence") if isinstance(drain, dict) else None
    if (
        not isinstance(admission, dict)
        or admission.get("state") != "rejecting_new_work"
        or admission.get("verified") is not True
        or admission.get("drain_requested") is not True
        or not isinstance(cron_admission, dict)
        or cron_admission.get("verified") is not True
        or cron_admission.get("accepting") is not False
        or cron_admission.get("active_count") != 0
        or not isinstance(work, dict)
        or work.get("active_cron_jobs") != 0
        or not isinstance(quiescence, dict)
        or quiescence.get("verified") is not True
        or quiescence.get("quiescent") is not True
        or quiescence.get("blockers") != []
    ):
        raise ReleaseBuildError(
            "internal watchdog gateway drain is not quiescent"
        )
    return {
        "status": "verified",
        "marker_sha256": marker["sha256"],
    }


def _internal_watchdog_pause_fields(
    plan: dict,
    prepared: dict,
) -> dict:
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    intent = cron.get("drain_intent") if isinstance(cron, dict) else None
    marker = intent.get("marker") if isinstance(intent, dict) else None
    marker_payload = (
        marker.get("payload") if isinstance(marker, dict) else None
    )
    transaction_id = str(plan.get("transaction_id") or "")
    if (
        not isinstance(marker_payload, dict)
        or marker_payload.get("release_transaction_id") != transaction_id
    ):
        raise ReleaseBuildError(
            "internal watchdog pause intent is missing"
        )
    reason = f"release-cutover:{transaction_id}"
    paused_at = str(
        marker_payload.get("requested_at")
        or f"transaction:{transaction_id}"
    )
    return {
        "enabled": False,
        "state": "paused",
        "paused_at": paused_at,
        "paused_reason": reason,
    }


def _internal_watchdog_disabled_receipt(
    plan: dict,
    prepared: dict,
    job: dict,
) -> dict:
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    stable_sha256 = _internal_watchdog_stable_sha256(job)
    if isinstance(cron, dict) and cron.get("control_origin") == "preexisting":
        controls = {
            key: job.get(key)
            for key in _INTERNAL_WATCHDOG_CONTROL_FIELDS
        }
        canonical_sha256 = hashlib.sha256(
            _canonical_internal_watchdog_job(job)
        ).hexdigest()
        if (
            stable_sha256 != cron.get("stable_job_sha256")
            or controls != cron.get("original_controls")
            or canonical_sha256 != cron.get("job_sha256")
        ):
            raise DrainIdentityMismatch(
                "preexisting internal watchdog controls changed"
            )
        marker_sha256 = _canonical_journal_value_sha256(
            {
                "control_origin": "preexisting",
                "job_id": plan["watchdog_scheduler_job_id"],
                "job_sha256": canonical_sha256,
                "original_controls": controls,
                "stable_job_sha256": stable_sha256,
            }
        )
        return {
            "status": "disabled",
            "backend": "hermes_internal",
            "control_origin": "preexisting",
            "job_id": str(plan["watchdog_scheduler_job_id"]),
            "job_sha256": canonical_sha256,
            "original_controls": controls,
            "stable_job_sha256": stable_sha256,
            "crontab_sha256": stable_sha256,
            "marker_sha256": marker_sha256,
        }
    pause = _internal_watchdog_pause_fields(plan, prepared)
    if (
        not isinstance(cron, dict)
        or stable_sha256 != cron.get("stable_job_sha256")
        or any(job.get(key) != value for key, value in pause.items())
    ):
        raise DrainIdentityMismatch(
            "internal watchdog job changed from its transaction-owned pause"
        )
    marker_sha256 = hashlib.sha256(
        json.dumps(
            {
                "job_id": plan["watchdog_scheduler_job_id"],
                "pause": pause,
                "stable_job_sha256": stable_sha256,
                "transaction_id": plan["transaction_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "status": "disabled",
        "backend": "hermes_internal",
        "job_id": str(plan["watchdog_scheduler_job_id"]),
        "stable_job_sha256": stable_sha256,
        "crontab_sha256": stable_sha256,
        "marker_sha256": marker_sha256,
    }


def _disable_watchdog_cron(plan: dict, prepared: dict) -> dict:
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        _assert_no_os_watchdog_duplicate(plan)
        cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
        if not isinstance(cron, dict):
            raise ReleaseBuildError(
                "internal watchdog preparation is missing"
            )
        handle = _acquire_internal_watchdog_jobs_lock(plan)
        try:
            _registry, payload, jobs, index, job = (
                _read_internal_watchdog_registry(plan)
            )
            if (
                _internal_watchdog_stable_sha256(job)
                != cron.get("stable_job_sha256")
            ):
                raise DrainIdentityMismatch(
                    "internal watchdog job configuration changed"
                )
            if cron.get("control_origin") == "preexisting":
                disabled_job = job
            else:
                pause = _internal_watchdog_pause_fields(plan, prepared)
                if all(job.get(key) == value for key, value in pause.items()):
                    disabled_job = job
                else:
                    active_claims = any(
                        job.get(field) is not None
                        for field in (
                            "run_claim",
                            "fire_claim",
                            "recovery_claim",
                        )
                    )
                    if (
                        job.get("enabled") is not True
                        or job.get("state") != "scheduled"
                        or active_claims
                    ):
                        raise DrainTimeout(
                            "internal watchdog job is active or not schedulable"
                        )
                    disabled_job = {**job, **pause}
                    jobs[index] = disabled_job
                    payload["jobs"] = jobs
                    _write_internal_watchdog_registry(plan, payload)
            disabled_receipt = _internal_watchdog_disabled_receipt(
                plan,
                prepared,
                disabled_job,
            )
        finally:
            handle.close()
        return disabled_receipt
    original_path = Path(plan["watchdog_crontab_rollback"])
    if sha256_file(original_path) != prepared["watchdog_cron"]["backup_sha256"]:
        raise ReleaseBuildError("watchdog crontab backup identity changed")
    original = original_path.read_text(encoding="utf-8")
    command = prepared["watchdog_cron"]["watchdog_command"]
    marker = f"# HERMES_CUTOVER_DISABLED {plan['transaction_id']} " + command
    disabled = original.replace(command, marker, 1)
    if disabled == original or disabled.count(marker) != 1:
        raise ReleaseBuildError("watchdog cron command cannot be disabled exactly")
    current = _read_crontab()
    if current not in {original, disabled}:
        raise ReleaseBuildError("watchdog crontab changed before writer barrier")
    if current == original:
        parent = _prepare_release_root(original_path.parent)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{original_path.name}.disabled.",
            dir=parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(disabled)
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                os.fsync(handle.fileno())
            _install_crontab_file(Path(temp_name))
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    observed = _read_crontab()
    if observed != disabled:
        raise ReleaseBuildError("watchdog cron disable did not persist")
    return {
        "status": "disabled",
        "crontab_sha256": hashlib.sha256(observed.encode()).hexdigest(),
        "marker_sha256": hashlib.sha256(marker.encode()).hexdigest(),
    }


def _attest_disabled_watchdog_cron(plan: dict, prepared: dict) -> dict:
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        handle = _acquire_internal_watchdog_jobs_lock(plan)
        try:
            _registry, _payload, _jobs, _index, job = (
                _read_internal_watchdog_registry(plan)
            )
            disabled_receipt = _internal_watchdog_disabled_receipt(
                plan,
                prepared,
                job,
            )
        finally:
            handle.close()
        return disabled_receipt
    current = _read_crontab()
    command = prepared["watchdog_cron"]["watchdog_command"]
    marker = f"# HERMES_CUTOVER_DISABLED {plan['transaction_id']} " + command
    active = [
        line
        for line in current.splitlines()
        if command in line and not line.lstrip().startswith("#")
    ]
    if current.count(marker) != 1 or active:
        raise ReleaseBuildError("watchdog cron writer barrier is not active")
    return {
        "status": "disabled",
        "crontab_sha256": hashlib.sha256(current.encode()).hexdigest(),
        "marker_sha256": hashlib.sha256(marker.encode()).hexdigest(),
    }


def _restore_watchdog_cron(plan: dict, prepared: dict) -> dict:
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        cron = (
            prepared.get("watchdog_cron")
            if isinstance(prepared, dict)
            else None
        )
        original = _read_internal_watchdog_rollback_job(plan, cron)
        if (
            _internal_watchdog_stable_sha256(original)
            != cron.get("stable_job_sha256")
        ):
            raise ReleaseBuildError(
                "internal watchdog rollback configuration changed"
            )
        pause = _internal_watchdog_pause_fields(plan, prepared)
        original_controls = {
            key: original.get(key)
            for key in _INTERNAL_WATCHDOG_CONTROL_FIELDS
        }
        handle = _acquire_internal_watchdog_jobs_lock(plan)
        try:
            _registry, payload, jobs, index, job = (
                _read_internal_watchdog_registry(plan)
            )
            if (
                _internal_watchdog_stable_sha256(job)
                != cron.get("stable_job_sha256")
            ):
                raise DrainIdentityMismatch(
                    "internal watchdog job configuration changed"
                )
            if all(
                job.get(key) == value
                for key, value in original_controls.items()
            ):
                pass
            elif all(job.get(key) == value for key, value in pause.items()):
                restored_job = {**job, **original_controls}
                jobs[index] = restored_job
                payload["jobs"] = jobs
                _write_internal_watchdog_registry(plan, payload)
            else:
                raise DrainIdentityMismatch(
                    "internal watchdog job has another pause owner"
                )
        finally:
            handle.close()
        intent = cron.get("drain_intent")
        if not isinstance(intent, dict):
            raise ReleaseBuildError(
                "internal watchdog drain intent is missing"
            )
        marker = intent.get("marker")
        marker_path = (
            Path(str(marker.get("path") or ""))
            if isinstance(marker, dict)
            else None
        )
        if marker_path is None:
            raise ReleaseBuildError(
                "internal watchdog drain marker identity is missing"
            )
        if marker_path.exists() or marker_path.is_symlink():
            _clear_legacy_gateway_drain_marker(plan, intent)
        current = _watchdog_receipt_for_prepared(plan, prepared)
        if not _cron_receipt_matches_prepared(current, prepared):
            raise DrainIdentityMismatch("internal watchdog job changed")
        return current
    backup = Path(plan["watchdog_crontab_rollback"])
    if sha256_file(backup) != prepared["watchdog_cron"]["backup_sha256"]:
        raise ReleaseBuildError("watchdog crontab rollback identity changed")
    original = backup.read_text(encoding="utf-8")
    command = prepared["watchdog_cron"]["watchdog_command"]
    marker = f"# HERMES_CUTOVER_DISABLED {plan['transaction_id']} " + command
    disabled = original.replace(command, marker, 1)
    current = _read_crontab()
    if current == disabled:
        _install_crontab_file(backup)
    elif current != original:
        raise ReleaseBuildError(
            "watchdog crontab changed concurrently; refusing rollback overwrite"
        )
    receipt = _cron_watchdog_receipt(plan)
    if (
        receipt["crontab_sha256"]
        != prepared["watchdog_cron"]["crontab_sha256"]
        or receipt["watchdog_command"]
        != prepared["watchdog_cron"]["watchdog_command"]
    ):
        raise ReleaseBuildError("watchdog crontab rollback is not exact")
    return receipt


def _cron_receipt_matches_prepared(receipt: dict, prepared: dict) -> bool:
    expected = prepared["watchdog_cron"]
    if expected.get("backend") == "hermes_internal":
        common = (
            receipt.get("backend") == "hermes_internal"
            and receipt.get("job_id") == expected.get("job_id")
            and receipt.get("stable_job_sha256")
            == expected.get("stable_job_sha256")
            and receipt.get("watchdog_command")
            == expected.get("watchdog_command")
            and receipt.get("job_enabled") == expected.get("job_enabled")
            and receipt.get("job_state") == expected.get("job_state")
        )
        if not common:
            return False
        if expected.get("control_origin") == "preexisting":
            return (
                receipt.get("control_origin") == "preexisting"
                and receipt.get("original_controls")
                == expected.get("original_controls")
                and receipt.get("job_sha256") == expected.get("job_sha256")
            )
        return True
    return all(
        receipt.get(key) == expected.get(key)
        for key in ("crontab_sha256", "watchdog_command")
    )


def _watchdog_receipt_for_prepared(plan: dict, prepared: dict) -> dict:
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    preexisting = (
        _watchdog_scheduler_backend(plan) == "hermes_internal"
        and isinstance(cron, dict)
        and cron.get("control_origin") == "preexisting"
    )
    if preexisting:
        return _cron_watchdog_receipt(plan, require_active=False)
    return _cron_watchdog_receipt(plan)


def _watchdog_cron_restore_intent_receipt(
    plan: dict,
    prepared: dict,
    disabled: dict,
) -> dict:
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    if (
        not isinstance(cron, dict)
        or disabled.get("status") != "disabled"
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(disabled.get("crontab_sha256") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(disabled.get("marker_sha256") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(cron.get("crontab_sha256") or ""),
        )
        or not str(cron.get("watchdog_command") or "").strip()
    ):
        raise ReleaseBuildError("watchdog cron restore intent is invalid")
    return {
        "status": "prepared",
        "transaction_id": plan["transaction_id"],
        "disabled_crontab_sha256": disabled["crontab_sha256"],
        "disabled_marker_sha256": disabled["marker_sha256"],
        "restore_crontab_sha256": cron["crontab_sha256"],
        "watchdog_command_sha256": hashlib.sha256(
            str(cron["watchdog_command"]).encode()
        ).hexdigest(),
    }


def _prepared_bootstrap_receipt(plan: dict) -> dict:
    split_attestation = _preflight_last_good_identity_split(plan)
    cli_link = Path(plan["cli_link"])
    if not cli_link.is_symlink():
        raise ReleaseBuildError("bootstrap Hermes CLI prior entry is not a symlink")
    discovered_cli_target = os.readlink(cli_link)
    if discovered_cli_target != str(plan["cli_old_target"]):
        raise ReleaseBuildError("bootstrap Hermes CLI prior entry changed")
    legacy_health = _legacy_idle_health(plan)
    legacy = _legacy_process_receipt(plan)
    gateway = _listener_process_receipt(
        plan,
        gateway=True,
        require_git_source=False,
    )
    old_arguments = legacy["program_arguments"]
    legacy_shape = old_arguments[:2] == [
        plan["expected_old_interpreter"],
        plan["expected_old_target"],
    ]
    managed_shape = old_arguments[:3] == [
        plan["expected_old_interpreter"],
        "-S",
        plan["expected_old_target"],
    ]
    if not legacy_shape and not managed_shape:
        raise ReleaseBuildError("discovered legacy launch identity changed")
    watchdog_candidate = _file_identity_receipt(plan["watchdog_candidate_script"])
    if watchdog_candidate["sha256"] != plan["watchdog_expected_sha256"]:
        raise ReleaseBuildError("staged watchdog identity changed")
    webui_plist = _copy_exact_backup(
        Path(plan["installed_plist"]),
        Path(plan["bootstrap_rollback_plist"]),
    )
    gateway_plist = _copy_exact_backup(
        Path(plan["gateway_installed_plist"]),
        Path(plan["gateway_rollback_plist"]),
    )
    watchdog = _copy_exact_backup(
        Path(plan["watchdog_installed_script"]),
        Path(plan["watchdog_rollback_script"]),
    )
    prepared = {
        "legacy_idle": legacy_health,
        "legacy": legacy,
        "gateway": gateway,
        "webui_plist": webui_plist,
        "gateway_plist": gateway_plist,
        "watchdog": watchdog,
        "watchdog_candidate": watchdog_candidate,
        "watchdog_cron": _backup_crontab(plan),
        "pre_managed_controls": _capture_pre_managed_control_state(plan),
        "last_good_split_provenance": (
            _bootstrap_split_provenance_receipt(
                plan,
                split_attestation,
            )
        ),
    }
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        prepared["watchdog_cron"]["drain_intent"] = (
            _legacy_gateway_drain_intent_receipt(plan, prepared)
        )
    return prepared


_INGRESS_GATE_READY_PATH = "/__hermes_first_cutover_gate__/ready"
_INGRESS_GATE_CHILDREN: dict[int, subprocess.Popen] = {}
_LEGACY_DISPATCHER_LOCKS: dict[str, object] = {}
_LEGACY_CRON_TICK_LOCKS: dict[str, object] = {}


def _ingress_gate_token_receipt(plan: dict) -> dict:
    path = Path(plan["ingress_gate_token_file"])
    parent = path.parent
    ancestor = parent.parent
    if path.name in {"", ".", ".."} or parent.name in {"", ".", ".."}:
        raise ReleaseBuildError("ingress gate controller token path is invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if ancestor.resolve(strict=True) != ancestor:
            raise ReleaseBuildError(
                "ingress gate control ancestor is not canonical"
            )
        ancestor_descriptor = os.open(ancestor, directory_flags)
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError(
            "ingress gate control ancestor cannot be opened"
        ) from exc
    try:
        ancestor_opened = os.fstat(ancestor_descriptor)
        ancestor_entry = os.stat(ancestor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(ancestor_opened.st_mode)
            or not stat.S_ISDIR(ancestor_entry.st_mode)
            or ancestor_opened.st_uid != os.getuid()
            or stat.S_IMODE(ancestor_opened.st_mode) & 0o077
            or (ancestor_opened.st_dev, ancestor_opened.st_ino)
            != (ancestor_entry.st_dev, ancestor_entry.st_ino)
        ):
            raise ReleaseBuildError(
                "ingress gate control ancestor is not private"
            )
        try:
            os.mkdir(parent.name, mode=0o700, dir_fd=ancestor_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ReleaseBuildError(
                "ingress gate control directory cannot be created"
            ) from exc
        else:
            os.fsync(ancestor_descriptor)
        try:
            parent_descriptor = os.open(
                parent.name,
                directory_flags,
                dir_fd=ancestor_descriptor,
            )
        except OSError as exc:
            raise ReleaseBuildError(
                "ingress gate control directory is not private"
            ) from exc
        try:
            parent_opened = os.fstat(parent_descriptor)
            parent_entry = os.stat(
                parent.name,
                dir_fd=ancestor_descriptor,
                follow_symlinks=False,
            )
            try:
                parent_canonical = parent.resolve(strict=True)
            except OSError as exc:
                raise ReleaseBuildError(
                    "ingress gate control directory is not private"
                ) from exc
            if (
                not stat.S_ISDIR(parent_opened.st_mode)
                or not stat.S_ISDIR(parent_entry.st_mode)
                or parent_canonical != parent
                or parent_opened.st_uid != os.getuid()
                or stat.S_IMODE(parent_opened.st_mode) & 0o077
                or (parent_opened.st_dev, parent_opened.st_ino)
                != (parent_entry.st_dev, parent_entry.st_ino)
            ):
                raise ReleaseBuildError(
                    "ingress gate control directory is not private"
                )
            read_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                token_descriptor = os.open(
                    path.name,
                    read_flags,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                token = secrets.token_urlsafe(48).encode("ascii") + b"\n"
                try:
                    created_descriptor = os.open(
                        path.name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                except FileExistsError:
                    token_descriptor = os.open(
                        path.name,
                        read_flags,
                        dir_fd=parent_descriptor,
                    )
                except OSError as exc:
                    raise ReleaseBuildError(
                        "ingress gate controller token file is unsafe"
                    ) from exc
                else:
                    try:
                        created_handle = os.fdopen(created_descriptor, "wb")
                    except OSError as exc:
                        try:
                            os.close(created_descriptor)
                        except OSError:
                            pass
                        raise ReleaseBuildError(
                            "ingress gate controller token cannot be written"
                        ) from exc
                    try:
                        with created_handle as handle:
                            handle.write(token)
                            handle.flush()
                            os.fchmod(handle.fileno(), 0o600)
                            os.fsync(handle.fileno())
                    except OSError as exc:
                        raise ReleaseBuildError(
                            "ingress gate controller token cannot be written"
                        ) from exc
                    os.fsync(parent_descriptor)
                    token_descriptor = os.open(
                        path.name,
                        read_flags,
                        dir_fd=parent_descriptor,
                    )
            except OSError as exc:
                raise ReleaseBuildError(
                    "ingress gate controller token file is unsafe"
                ) from exc
            try:
                token_opened = os.fstat(token_descriptor)
                if (
                    not stat.S_ISREG(token_opened.st_mode)
                    or token_opened.st_uid != os.getuid()
                    or token_opened.st_nlink != 1
                    or stat.S_IMODE(token_opened.st_mode) != 0o600
                    or not 32 <= token_opened.st_size <= 257
                ):
                    raise ReleaseBuildError(
                        "ingress gate controller token file is unsafe"
                    )
                raw = os.read(token_descriptor, 258)
                token_after_read = os.fstat(token_descriptor)
            finally:
                os.close(token_descriptor)
            try:
                token_entry = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                parent_after = os.stat(
                    parent.name,
                    dir_fd=ancestor_descriptor,
                    follow_symlinks=False,
                )
                ancestor_after = os.stat(ancestor, follow_symlinks=False)
                parent_canonical_after = parent.resolve(strict=True)
            except OSError as exc:
                raise ReleaseBuildError(
                    "ingress gate control directory changed during token receipt"
                ) from exc
            token_identity = (
                token_opened.st_dev,
                token_opened.st_ino,
                token_opened.st_mode,
                token_opened.st_uid,
                token_opened.st_nlink,
                token_opened.st_size,
                token_opened.st_mtime_ns,
                token_opened.st_ctime_ns,
            )
            if token_identity != (
                token_after_read.st_dev,
                token_after_read.st_ino,
                token_after_read.st_mode,
                token_after_read.st_uid,
                token_after_read.st_nlink,
                token_after_read.st_size,
                token_after_read.st_mtime_ns,
                token_after_read.st_ctime_ns,
            ) or token_identity != (
                token_entry.st_dev,
                token_entry.st_ino,
                token_entry.st_mode,
                token_entry.st_uid,
                token_entry.st_nlink,
                token_entry.st_size,
                token_entry.st_mtime_ns,
                token_entry.st_ctime_ns,
            ):
                raise ReleaseBuildError(
                    "ingress gate controller token changed during receipt"
                )
            if len(raw) != token_opened.st_size:
                raise ReleaseBuildError(
                    "ingress gate controller token changed during receipt"
                )
            if (
                parent_canonical_after != parent
                or (parent_opened.st_dev, parent_opened.st_ino)
                != (parent_after.st_dev, parent_after.st_ino)
                or (ancestor_opened.st_dev, ancestor_opened.st_ino)
                != (ancestor_after.st_dev, ancestor_after.st_ino)
            ):
                raise ReleaseBuildError(
                    "ingress gate control directory changed during token receipt"
                )
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(ancestor_descriptor)
    try:
        token = raw.rstrip(b"\r\n").decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("ingress gate controller token is invalid") from exc
    if (
        not 32 <= len(token) <= 256
        or any(character.isspace() or ord(character) < 0x21 for character in token)
    ):
        raise ReleaseBuildError("ingress gate controller token is invalid")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "token": token,
    }


def _attest_ingress_gate(plan: dict) -> dict:
    token = _ingress_gate_token_receipt(plan)
    request = Request(
        f"{str(plan['base_url']).rstrip('/')}{_INGRESS_GATE_READY_PATH}",
        headers={"Authorization": f"Bearer {token['token']}"},
        method="GET",
    )
    ready = _http_json(request, timeout_seconds=max(5.0, float(plan["timeout_seconds"])))
    expected_keys = {
        "schema_version",
        "ready",
        "mode",
        "pid",
        "process_start",
        "process_start_token",
        "instance_id",
        "host",
        "port",
        "controller_endpoint",
    }
    if (
        set(ready) != expected_keys
        or ready.get("schema_version") != 1
        or ready.get("ready") is not True
        or ready.get("mode") != "deny-all-no-proxy"
        or ready.get("controller_endpoint") != _INGRESS_GATE_READY_PATH
        or int(ready.get("port", -1)) != int(plan["listener_port"])
        or not str(ready.get("instance_id") or "")
    ):
        raise ReleaseBuildError("ingress gate readiness receipt is invalid")
    pid = int(ready["pid"])
    start = _pid_start_token(pid)
    if not start or _listener_pid(int(plan["listener_port"])) != pid:
        raise DrainIdentityMismatch("ingress gate listener identity is invalid")
    receipt_path = Path(plan["ingress_gate_ready_receipt"])
    on_disk = _read_json_object(receipt_path, label="ingress gate ready receipt")
    if on_disk != ready or stat.S_IMODE(receipt_path.stat().st_mode) != 0o600:
        raise ReleaseBuildError("ingress gate disk receipt changed")
    script = _file_identity_receipt(plan["ingress_gate_script"])
    if script.get("sha256") != plan["ingress_gate_expected_sha256"]:
        raise ReleaseBuildError("ingress gate script identity changed")
    command = _ps_value(pid, "command")
    try:
        command_arguments = shlex.split(command)
        expected_interpreter = _python_kernel_executable_path(
            plan["managed_interpreter"]
        )
    except (OSError, ValueError, IndexError) as exc:
        raise DrainIdentityMismatch("ingress gate process command is invalid") from exc
    actual_interpreter = _process_executable_path(pid)
    if (
        actual_interpreter != expected_interpreter
        or str(plan["ingress_gate_script"]) not in command_arguments
    ):
        raise DrainIdentityMismatch(
            "ingress gate process command changed: "
            f"executable={actual_interpreter} expected={expected_interpreter} "
            f"argv_sha256={hashlib.sha256(command.encode()).hexdigest()} "
            f"script_argument_present={str(plan['ingress_gate_script']) in command_arguments}"
        )
    return {
        "status": "verified",
        "pid": pid,
        "pid_start_token": start,
        "listener_pid": pid,
        "instance_id": ready["instance_id"],
        "ready_receipt_sha256": sha256_file(receipt_path),
        "script": script,
        "command": command,
        "token_file_sha256": token["sha256"],
    }


def _ingress_gate_listener_pid_or_none(port: int) -> int | None:
    try:
        completed = subprocess.run(
            [
                "lsof",
                "-nP",
                f"-iTCP:{int(port)}",
                "-sTCP:LISTEN",
                "-t",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DrainIdentityMismatch("ingress gate listener probe failed") from exc
    rows = completed.stdout.split()
    if completed.returncode == 1 and not rows and not completed.stderr.strip():
        return None
    pids = {int(row) for row in rows if row.isdigit()}
    if (
        completed.returncode != 0
        or len(pids) != 1
        or len(rows) != 1
    ):
        raise DrainIdentityMismatch(
            "ingress gate listener PID is unavailable or ambiguous"
        )
    pid = next(iter(pids))
    if pid <= 1:
        raise DrainIdentityMismatch("ingress gate listener PID is invalid")
    return pid


def _ingress_gate_exit_receipt(process: subprocess.Popen) -> dict:
    exit_code = process.poll()
    if exit_code is None:
        raise ReleaseBuildError("ingress gate exit receipt requested while running")
    stderr_stream = process.stderr
    raw = b""
    if stderr_stream is not None:
        try:
            raw = stderr_stream.read(4096)
        finally:
            stderr_stream.close()
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    raw = bytes(raw)
    lowered = raw.lower()
    retryable_bind_hold = (
        b"address already in use" in lowered
        and any(
            marker in lowered
            for marker in (b"errno 48", b"errno 98", b"errno 10048")
        )
    )
    return {
        "exit_code": int(exit_code),
        "stderr_sha256": hashlib.sha256(raw).hexdigest(),
        "stderr_size": len(raw),
        "category": (
            "transient-address-hold"
            if retryable_bind_hold
            else "non-retryable-startup-exit"
        ),
        "retryable": retryable_bind_hold,
    }


def _start_or_adopt_ingress_gate(plan: dict) -> dict:
    try:
        return {"status": "adopted", "binding": _attest_ingress_gate(plan)}
    except Exception as adoption_error:
        listener = _ingress_gate_listener_pid_or_none(
            int(plan["listener_port"])
        )
        if listener is not None:
            raise DrainIdentityMismatch(
                "unexpected process owns WebUI port before ingress gate"
            ) from adoption_error
    script = Path(plan["ingress_gate_script"])
    if sha256_file(script) != plan["ingress_gate_expected_sha256"]:
        raise ReleaseBuildError("ingress gate script identity changed")
    token = _ingress_gate_token_receipt(plan)
    receipt_path = Path(plan["ingress_gate_ready_receipt"])
    if receipt_path.parent != Path(token["path"]).parent:
        raise ReleaseBuildError("ingress gate receipt is outside private control dir")
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    transient_failures: list[dict] = []
    process: subprocess.Popen | None = None
    while time.monotonic() < deadline:
        process = subprocess.Popen(
            [
                str(plan["managed_interpreter"]),
                "-I",
                str(script),
                "--host",
                str(urlsplit(str(plan["base_url"])).hostname),
                "--port",
                str(plan["listener_port"]),
                "--controller-token-file",
                token["path"],
                "--ready-receipt",
                str(receipt_path),
            ],
            cwd=script.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={
                key: value
                for key in ("HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")
                if (value := os.environ.get(key)) is not None
            },
        )
        retry_spawn = False
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                failure = _ingress_gate_exit_receipt(process)
                if not failure["retryable"]:
                    raise ReleaseBuildError(
                        "ingress gate exited before readiness: "
                        f"category={failure['category']} "
                        f"exit_code={failure['exit_code']} "
                        f"stderr_sha256={failure['stderr_sha256']}"
                    )
                listener = _ingress_gate_listener_pid_or_none(
                    int(plan["listener_port"])
                )
                if listener is not None:
                    raise DrainIdentityMismatch(
                        "WebUI port was reacquired during ingress gate retry"
                    )
                transient_failures.append(failure)
                last_error = ReleaseBuildError(
                    "ingress gate address remained transiently unavailable"
                )
                time.sleep(float(plan["interval_seconds"]))
                retry_spawn = True
                break
            try:
                binding = _attest_ingress_gate(plan)
                if binding["pid"] != process.pid:
                    raise DrainIdentityMismatch("ingress gate spawned PID changed")
                _INGRESS_GATE_CHILDREN[process.pid] = process
                return {
                    "status": "started",
                    "binding": binding,
                    "start_attempts": len(transient_failures) + 1,
                    "transient_failures": transient_failures,
                }
            except Exception as exc:
                last_error = exc
                time.sleep(float(plan["interval_seconds"]))
        if not retry_spawn:
            break
    if process is not None and _pid_start_token(process.pid) is not None:
        os.kill(process.pid, signal.SIGKILL)
    if process is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise ReleaseBuildError(
                "failed ingress gate child could not be reaped"
            ) from exc
    raise DrainTimeout(f"ingress gate readiness timed out: {last_error}")


def _stop_ingress_gate(plan: dict, receipt: dict) -> dict:
    expected = receipt.get("binding") if isinstance(receipt, dict) else None
    if not isinstance(expected, dict):
        raise ReleaseBuildError("durable ingress gate receipt is missing")
    try:
        current = _attest_ingress_gate(plan)
    except Exception as probe_error:
        try:
            listener = _listener_pid(int(plan["listener_port"]))
        except DrainIdentityMismatch:
            listener = None
        if listener is not None:
            raise DrainIdentityMismatch(
                "unexpected process owns WebUI port at ingress gate stop"
            ) from probe_error
        ready_path = Path(plan["ingress_gate_ready_receipt"])
        if ready_path.exists():
            stale = _read_json_object(ready_path, label="stale ingress gate receipt")
            if stale.get("instance_id") != expected.get("instance_id"):
                raise ReleaseBuildError("stale ingress gate receipt is not owned")
            ready_path.unlink()
            _fsync_directory(ready_path.parent)
        return {"status": "already-stopped"}
    for key in ("pid", "pid_start_token", "instance_id"):
        if current.get(key) != expected.get(key):
            raise DrainIdentityMismatch("ingress gate identity changed before stop")
    identity = {
        "pid": current["pid"],
        "pid_start_token": current["pid_start_token"],
    }
    os.kill(int(current["pid"]), signal.SIGTERM)
    child = _INGRESS_GATE_CHILDREN.get(int(current["pid"]))
    try:
        if child is not None:
            try:
                child.wait(timeout=float(plan["timeout_seconds"]))
            except subprocess.TimeoutExpired:
                if _exact_process_is_alive(identity):
                    os.kill(int(current["pid"]), signal.SIGKILL)
                child.wait(timeout=float(plan["timeout_seconds"]))
        else:
            try:
                wait_for_exact_process_exit(identity, float(plan["timeout_seconds"]))
            except DrainTimeout:
                if _exact_process_is_alive(identity):
                    os.kill(int(current["pid"]), signal.SIGKILL)
                wait_for_exact_process_exit(identity, float(plan["timeout_seconds"]))
    finally:
        _INGRESS_GATE_CHILDREN.pop(int(current["pid"]), None)
    try:
        replacement = _listener_pid(int(plan["listener_port"]))
    except DrainIdentityMismatch:
        replacement = None
    if replacement is not None:
        raise DrainIdentityMismatch("WebUI port was reacquired while gate stopped")
    ready_path = Path(plan["ingress_gate_ready_receipt"])
    deadline = time.monotonic() + max(2.0, float(plan["timeout_seconds"]))
    while ready_path.exists() and time.monotonic() < deadline:
        time.sleep(float(plan["interval_seconds"]))
    if ready_path.exists():
        raise ReleaseBuildError("ingress gate ready receipt survived exact stop")
    return {"status": "stopped", "identity": identity}


def _exact_process_is_alive(receipt: dict) -> bool:
    try:
        pid = int(receipt.get("pid"))
    except (TypeError, ValueError) as exc:
        raise DrainIdentityMismatch("durable process PID is invalid") from exc
    expected = str(receipt.get("pid_start_token") or "")
    if not expected:
        raise DrainIdentityMismatch("durable process start identity is invalid")
    return _pid_start_token(pid) == expected


def _process_parent_table() -> dict[int, int]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("process tree receipt is unavailable") from exc
    table: dict[int, int] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise ReleaseBuildError("process tree receipt is malformed")
        pid, ppid = (int(field) for field in fields)
        if pid > 1:
            table[pid] = ppid
    return table


def _descendant_pids(table: dict[int, int], root_pid: int) -> set[int]:
    descendants: set[int] = set()
    changed = True
    while changed:
        changed = False
        parents = descendants | {root_pid}
        for pid, ppid in table.items():
            if pid not in descendants and ppid in parents:
                descendants.add(pid)
                changed = True
    descendants.discard(root_pid)
    return descendants


def _live_descendant_pids(
    table: dict[int, int],
    root_pid: int,
    *,
    role: str,
    known_receipts: dict[int, dict] | None = None,
) -> set[int]:
    live: set[int] = set()
    for pid in _descendant_pids(table, root_pid):
        known = (known_receipts or {}).get(pid)
        if known is not None and _exact_process_is_alive(known):
            live.add(pid)
            continue
        if _pid_start_token(pid) is not None:
            live.add(pid)
            continue
        try:
            state = _ps_value(pid, "state").upper().strip()
        except DrainIdentityMismatch:
            if _pid_start_token(pid) is None:
                continue
            raise
        if not state.startswith("Z"):
            raise DrainIdentityMismatch(
                f"{role} descendant identity is unavailable for PID {pid}"
            )
    return live


def _freeze_exact_process_tree(root_receipt: dict, *, role: str) -> dict:
    root_pid = int(root_receipt["pid"])
    if not _exact_process_is_alive(root_receipt):
        return {"role": role, "status": "already-absent", "tree": []}
    frozen: dict[int, dict] = {}

    def freeze(receipt: dict, *, ppid: int | None) -> None:
        pid = int(receipt["pid"])
        if _pid_start_token(pid) != receipt["pid_start_token"]:
            raise DrainIdentityMismatch(f"{role} process identity changed at freeze")
        os.kill(pid, signal.SIGSTOP)
        if _pid_start_token(pid) != receipt["pid_start_token"]:
            raise DrainIdentityMismatch(f"{role} process identity changed after freeze")
        state = _ps_value(pid, "state")
        if not state.upper().strip().startswith("T"):
            raise DrainIdentityMismatch(f"{role} process did not enter STOP barrier")
        frozen[pid] = {
            "pid": pid,
            "ppid": ppid,
            "pid_start_token": receipt["pid_start_token"],
            "state": state,
        }

    try:
        freeze(root_receipt, ppid=None)
        for _attempt in range(32):
            table = _process_parent_table()
            descendants = _live_descendant_pids(
                table,
                root_pid,
                role=role,
            )
            unfrozen = sorted(descendants - set(frozen))
            if not unfrozen:
                final_table = _process_parent_table()
                final_descendants = _live_descendant_pids(
                    final_table,
                    root_pid,
                    role=role,
                )
                if final_descendants.issubset(frozen):
                    break
                continue
            for pid in unfrozen:
                token = _pid_start_token(pid)
                if token is None:
                    continue
                confirmation = _process_parent_table()
                if pid not in _descendant_pids(confirmation, root_pid):
                    continue
                if _pid_start_token(pid) != token:
                    continue
                freeze(
                    {"pid": pid, "pid_start_token": token},
                    ppid=confirmation.get(pid),
                )
        else:
            raise DrainTimeout(f"{role} process tree did not stabilize")
        return {
            "role": role,
            "status": "frozen",
            "tree": [frozen[pid] for pid in sorted(frozen)],
        }
    except Exception:
        for receipt in frozen.values():
            if _exact_process_is_alive(receipt):
                os.kill(int(receipt["pid"]), signal.SIGCONT)
        raise


def _freeze_prepared_writers(plan: dict, prepared: dict) -> dict:
    frozen: list[dict[str, object]] = []
    try:
        receipt = prepared["legacy"]
        if _exact_process_is_alive(receipt):
            frozen.append(_freeze_exact_process_tree(receipt, role="webui"))
        else:
            try:
                listener = _listener_pid(int(plan["listener_port"]))
            except DrainIdentityMismatch:
                listener = None
            if listener is not None:
                raise DrainIdentityMismatch(
                    "WebUI listener was replaced during bootstrap resume"
                )
            frozen.append({"role": "webui", "status": "already-absent", "tree": []})
    except Exception:
        for row in frozen:
            for receipt in row.get("tree", []):
                if _exact_process_is_alive(receipt):
                    os.kill(int(receipt["pid"]), signal.SIGCONT)
        raise
    return {"status": "frozen", "writers": frozen}


def _verify_frozen_prepared_writers(
    plan: dict,
    prepared: dict,
    frozen: dict,
) -> dict:
    rows = frozen.get("writers") if isinstance(frozen, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or rows[0].get("role") != "webui"
    ):
        raise ReleaseBuildError("frozen writer receipt is invalid")
    for row in rows:
        role = row["role"]
        original = prepared["legacy"]
        tree = row.get("tree")
        if row.get("status") == "already-absent":
            if tree != []:
                raise ReleaseBuildError("absent writer receipt has a process tree")
            try:
                listener = _listener_pid(int(plan["listener_port"]))
            except DrainIdentityMismatch:
                listener = None
            if listener is not None:
                raise DrainIdentityMismatch("absent frozen writer reappeared")
            continue
        if row.get("status") != "frozen" or not isinstance(tree, list) or not tree:
            raise ReleaseBuildError("frozen process tree receipt is invalid")
        root = next(
            (
                process
                for process in tree
                if isinstance(process, dict)
                and int(process.get("pid", -1)) == int(original["pid"])
            ),
            None,
        )
        if (
            not isinstance(root, dict)
            or root.get("pid_start_token") != original["pid_start_token"]
        ):
            raise DrainIdentityMismatch("frozen root process identity changed")
        for process in tree:
            if not isinstance(process, dict) or not _exact_process_is_alive(process):
                raise DrainIdentityMismatch("frozen process tree identity changed")
            if not _ps_value(
                int(process["pid"]),
                "state",
            ).upper().strip().startswith("T"):
                raise DrainIdentityMismatch("frozen process resumed unexpectedly")
        current_descendants = _live_descendant_pids(
            _process_parent_table(),
            int(original["pid"]),
            role=role,
            known_receipts={
                int(process["pid"]): process for process in tree
            },
        )
        recorded = {int(process["pid"]) for process in tree}
        expected_recorded = current_descendants | {int(original["pid"])}
        if recorded != expected_recorded:
            raise DrainIdentityMismatch(
                "frozen process tree membership changed after barrier"
            )
    return frozen


def _validated_parent_first_frozen_tree(tree: list) -> list[dict]:
    by_pid: dict[int, dict] = {}
    roots: list[int] = []
    for process in tree:
        if not isinstance(process, dict):
            raise ReleaseBuildError("frozen process tree receipt is invalid")
        try:
            pid = int(process["pid"])
            raw_ppid = process.get("ppid")
            ppid = None if raw_ppid is None else int(raw_ppid)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseBuildError("frozen process tree receipt is invalid") from exc
        if pid <= 1 or pid in by_pid or (ppid is not None and ppid <= 1):
            raise ReleaseBuildError("frozen process tree receipt is invalid")
        by_pid[pid] = process
        if ppid is None:
            roots.append(pid)
    if tree and len(roots) != 1:
        raise ReleaseBuildError("frozen process tree root is ambiguous")
    for pid, process in by_pid.items():
        raw_ppid = process.get("ppid")
        if raw_ppid is not None and int(raw_ppid) not in by_pid:
            raise ReleaseBuildError(
                f"frozen process tree parent is missing for PID {pid}"
            )

    depths: dict[int, int] = {}

    def depth(pid: int, visiting: set[int]) -> int:
        if pid in depths:
            return depths[pid]
        if pid in visiting:
            raise ReleaseBuildError("frozen process tree contains a cycle")
        visiting.add(pid)
        raw_ppid = by_pid[pid].get("ppid")
        value = 0 if raw_ppid is None else depth(int(raw_ppid), visiting) + 1
        visiting.remove(pid)
        depths[pid] = value
        return value

    for pid in by_pid:
        depth(pid, set())
    return sorted(
        by_pid.values(),
        key=lambda process: (depth(int(process["pid"]), set()), int(process["pid"])),
    )


def _resume_frozen_prepared_writers(frozen: dict) -> dict:
    rows = frozen.get("writers") if isinstance(frozen, dict) else None
    if not isinstance(rows, list):
        raise ReleaseBuildError("frozen writer receipt is invalid")
    resumed: list[dict] = []
    terminal: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseBuildError("frozen writer receipt is invalid")
        tree = row.get("tree", [])
        if not isinstance(tree, list):
            raise ReleaseBuildError("frozen process tree receipt is invalid")
        tree = _validated_parent_first_frozen_tree(tree)
        root_pid = int(tree[0]["pid"]) if tree else None
        survivors: list[dict] = []
        for process in tree:
            pid = int(process["pid"])
            if _exact_process_is_alive(process):
                survivors.append(process)
                continue
            if pid == root_pid:
                raise DrainIdentityMismatch(
                    "frozen root process identity changed before SIGCONT"
                )
            current_start = _pid_start_token(pid)
            if current_start is not None:
                raise DrainIdentityMismatch(
                    "frozen child PID was reused before SIGCONT"
                )
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                terminal_status = "absent"
            except OSError as exc:
                raise DrainIdentityMismatch(
                    "frozen child existence probe failed before SIGCONT"
                ) from exc
            else:
                try:
                    state = _ps_value(pid, "state")
                except DrainIdentityMismatch:
                    if _pid_start_token(pid) is not None:
                        raise DrainIdentityMismatch(
                            "frozen child PID was reused before SIGCONT"
                        )
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        terminal_status = "absent"
                    except OSError as exc:
                        raise DrainIdentityMismatch(
                            "frozen child existence re-probe failed before SIGCONT"
                        ) from exc
                    else:
                        raise
                else:
                    if not state.upper().startswith("Z"):
                        raise DrainIdentityMismatch(
                            "frozen child start-token probe failed before SIGCONT"
                        )
                    terminal_status = "zombie"
                if terminal_status not in {"absent", "zombie"}:
                    raise DrainIdentityMismatch(
                        "frozen child start-token probe failed before SIGCONT"
                    )
            terminal.append(
                {
                    "pid": pid,
                    "pid_start_token": str(process["pid_start_token"]),
                    "status": terminal_status,
                }
            )
        for process in survivors:
            pid = int(process["pid"])
            if not _exact_process_is_alive(process):
                raise DrainIdentityMismatch(
                    "frozen process identity changed before SIGCONT"
                )
            os.kill(pid, signal.SIGCONT)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not _exact_process_is_alive(process):
                    raise DrainIdentityMismatch(
                        "frozen process exited during SIGCONT"
                    )
                state = _ps_value(pid, "state")
                if "T" not in state.upper():
                    break
                time.sleep(0.02)
            else:
                raise DrainIdentityMismatch(
                    "frozen process did not leave STOP barrier"
                )
            resumed.append(
                {
                    "pid": pid,
                    "pid_start_token": str(process["pid_start_token"]),
                }
            )
    if terminal:
        return {
            "status": "resumed-with-terminal-children",
            "processes": resumed,
            "terminal_processes": terminal,
        }
    return {"status": "resumed", "processes": resumed}


def _established_socket_boundary_receipt(
    plan: dict,
    *,
    gateway_pid: int | None,
) -> dict:
    ports = (int(plan["listener_port"]), int(plan["gateway_listener_port"]))
    receipts: list[dict] = []
    for port in ports:
        try:
            completed = subprocess.run(
                [
                    "lsof",
                    "-nP",
                    f"-iTCP:{port}",
                    "-sTCP:ESTABLISHED",
                    "-FpfnT",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                env={
                    key: value
                    for key in ("PATH", "LANG", "LC_ALL")
                    if (value := os.environ.get(key)) is not None
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseBuildError(
                "frozen socket boundary is unavailable"
            ) from exc
        if completed.returncode == 0 or completed.stdout.strip():
            raise ReleaseBuildError(
                f"frozen service port {port} still has an established socket"
            )
        if completed.returncode != 1 or completed.stderr.strip():
            raise ReleaseBuildError(
                f"frozen service port {port} socket proof is invalid"
            )
        receipts.append({"port": port, "established_connections": 0})
    result = {
        "status": "verified",
        "ports": receipts,
    }
    if gateway_pid is not None:
        try:
            gateway = subprocess.run(
                [
                    "lsof",
                    "-nP",
                    "-a",
                    "-p",
                    str(gateway_pid),
                    "-iTCP",
                    "-sTCP:ESTABLISHED",
                    "-FpfnT",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                env={
                    key: value
                    for key in ("PATH", "LANG", "LC_ALL")
                    if (value := os.environ.get(key)) is not None
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseBuildError(
                "frozen gateway socket boundary is unavailable"
            ) from exc
        if gateway.returncode == 0 or gateway.stdout.strip():
            raise ReleaseBuildError(
                "frozen gateway still has an established TCP socket"
            )
        if gateway.returncode != 1 or gateway.stderr.strip():
            raise ReleaseBuildError(
                "frozen gateway socket proof is invalid"
            )
        result["gateway"] = {
            "pid": gateway_pid,
            "established_tcp_connections": 0,
        }
    return result


def _read_private_json_value(path: Path, *, label: str) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise ReleaseBuildError(f"{label} is unsafe")
            payload = handle.read(16 * 1024 * 1024 + 1)
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    if len(payload) > 16 * 1024 * 1024:
        raise ReleaseBuildError(f"{label} is too large")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{label} JSON is invalid") from exc


_LEGACY_GATEWAY_STATUS_MAX_BYTES = 1024 * 1024
_LEGACY_GATEWAY_STATUS_MODES = {0o600, 0o644}


def _read_legacy_gateway_status(
    path: Path,
    *,
    label: str,
) -> tuple[dict, dict]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ReleaseBuildError(f"{label} no-follow reads are unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode)
                not in _LEGACY_GATEWAY_STATUS_MODES
            ):
                raise ReleaseBuildError(f"{label} is unsafe")
            payload = handle.read(_LEGACY_GATEWAY_STATUS_MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
            current = path.lstat()
    except ReleaseBuildError:
        raise
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    if len(payload) > _LEGACY_GATEWAY_STATUS_MAX_BYTES:
        raise ReleaseBuildError(f"{label} is too large")
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(
            getattr(before, field) != getattr(after, field)
            or getattr(after, field) != getattr(current, field)
            for field in stable_fields
        )
        or after.st_size != len(payload)
    ):
        raise ReleaseBuildError(f"{label} identity changed during read")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{label} JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError(f"{label} must contain a JSON object")
    return value, {
        "path": str(path),
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "device": after.st_dev,
        "inode": after.st_ino,
        "uid": after.st_uid,
        "mode": stat.S_IMODE(after.st_mode),
        "nlink": after.st_nlink,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
    }


_SYNTHETIC_STORE_MAX_BYTES = 16 * 1024 * 1024
_SYNTHETIC_STORE_STABLE_FIELDS = (
    "path",
    "device",
    "inode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "sha256",
)
_SYNTHETIC_STORE_MOVED_CAS_FIELDS = (
    "device",
    "inode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "sha256",
)


def _synthetic_store_receipt_from_descriptor(
    path: Path,
    descriptor: int,
    *,
    label: str,
    allowed_modes: set[int],
) -> tuple[dict, object]:
    try:
        before = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = bytearray()
        while len(payload) <= _SYNTHETIC_STORE_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    _SYNTHETIC_STORE_MAX_BYTES + 1 - len(payload),
                ),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise DrainIdentityMismatch(f"{label} identity changed") from exc
    same_fd_path_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    mode = stat.S_IMODE(after.st_mode)
    if (
        len(payload) > _SYNTHETIC_STORE_MAX_BYTES
        or not stat.S_ISREG(after.st_mode)
        or after.st_uid != os.getuid()
        or after.st_nlink != 1
        or mode not in allowed_modes
        or any(
            getattr(before, field) != getattr(after, field)
            or getattr(current, field) != getattr(after, field)
            for field in same_fd_path_fields
        )
    ):
        raise ReleaseBuildError(f"{label} is unsafe")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(f"{label} JSON is invalid") from exc
    return (
        {
            "status": "present",
            "path": str(path),
            "device": after.st_dev,
            "inode": after.st_ino,
            "uid": after.st_uid,
            "mode": mode,
            "nlink": after.st_nlink,
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "ctime_ns": after.st_ctime_ns,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        },
        value,
    )


def _read_synthetic_store_receipt(
    path: Path,
    *,
    label: str,
    allowed_modes: set[int],
) -> tuple[dict, object]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(f"{label} cannot be opened without O_NOFOLLOW")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    try:
        os.set_inheritable(descriptor, False)
        return _synthetic_store_receipt_from_descriptor(
            path,
            descriptor,
            label=label,
            allowed_modes=allowed_modes,
        )
    finally:
        os.close(descriptor)


def _synthetic_store_receipts_match_stable(
    actual: dict,
    expected: dict,
    *,
    moved: bool = False,
) -> bool:
    fields = (
        _SYNTHETIC_STORE_MOVED_CAS_FIELDS
        if moved
        else _SYNTHETIC_STORE_STABLE_FIELDS
    )
    return all(actual.get(field) == expected.get(field) for field in fields)


def _synthetic_store_specs(plan: dict) -> dict[str, dict]:
    process_path = Path(plan["synthetic_process_notifications_path"])
    delegation_path = Path(plan["synthetic_async_delegations_path"])
    home = process_path.parent
    try:
        home_state = home.lstat()
    except OSError as exc:
        raise ReleaseBuildError(
            "synthetic completion store parent is unavailable"
        ) from exc
    if (
        delegation_path.parent != home
        or home.is_symlink()
        or not stat.S_ISDIR(home_state.st_mode)
        or home.resolve(strict=True) != home
        or home_state.st_uid != os.getuid()
        or stat.S_IMODE(home_state.st_mode) != 0o700
    ):
        raise ReleaseBuildError("synthetic completion store parent is unsafe")
    return {
        "process_notifications": {
            "path": process_path,
            "label": "synthetic process completion store",
            "expected_sha256": plan[
                "synthetic_process_notifications_expected_sha256"
            ],
        },
        "async_delegations": {
            "path": delegation_path,
            "label": "synthetic async delegation store",
            "expected_sha256": plan[
                "synthetic_async_delegations_expected_sha256"
            ],
        },
    }


def _synthetic_store_mode_normalize_intent_receipt(plan: dict) -> dict:
    transaction_id = str(plan.get("transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError(
            "synthetic store mode normalization transaction is invalid"
        )
    stores: dict[str, dict] = {}
    for name, spec in _synthetic_store_specs(plan).items():
        receipt, _value = _read_synthetic_store_receipt(
            spec["path"],
            label=spec["label"],
            allowed_modes={0o600, 0o644},
        )
        if receipt["sha256"] != spec["expected_sha256"]:
            raise ReleaseBuildError("synthetic completion store CAS changed")
        stores[name] = {
            "path": str(spec["path"]),
            "expected_sha256": spec["expected_sha256"],
            "original": receipt,
            "normalized_mode": 0o600,
        }
    return {
        "status": "prepared",
        "transaction_id": transaction_id,
        "stores": stores,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _set_synthetic_store_mode(
    path: Path,
    *,
    label: str,
    expected: dict,
    allowed_current_modes: set[int],
    target_mode: int,
) -> dict:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(f"{label} cannot be opened without O_NOFOLLOW")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise ReleaseBuildError(f"{label} is unreadable") from exc
    try:
        os.set_inheritable(descriptor, False)
        opened, _value = _synthetic_store_receipt_from_descriptor(
            path,
            descriptor,
            label=label,
            allowed_modes=allowed_current_modes | {target_mode},
        )
        if not _synthetic_store_receipts_match_stable(opened, expected):
            raise DrainIdentityMismatch(f"{label} changed before mode CAS")
        if opened["mode"] == target_mode:
            return opened
        os.fchmod(descriptor, target_mode)
        os.fsync(descriptor)
        changed, _value = _synthetic_store_receipt_from_descriptor(
            path,
            descriptor,
            label=label,
            allowed_modes={target_mode},
        )
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    if not _synthetic_store_receipts_match_stable(changed, expected):
        raise DrainIdentityMismatch(f"{label} changed during mode CAS")
    return changed


def _normalize_synthetic_completion_store_modes(
    plan: dict,
    intent: dict,
) -> dict:
    if (
        not isinstance(intent, dict)
        or intent.get("status") != "prepared"
        or intent.get("transaction_id") != plan.get("transaction_id")
        or not isinstance(intent.get("stores"), dict)
        or set(intent["stores"]) != set(_synthetic_store_specs(plan))
    ):
        raise ReleaseBuildError(
            "synthetic store mode normalization intent is invalid"
        )
    normalized: dict[str, dict] = {}
    for name, spec in _synthetic_store_specs(plan).items():
        durable = intent["stores"][name]
        original = durable.get("original")
        if (
            not isinstance(original, dict)
            or durable.get("path") != str(spec["path"])
            or durable.get("expected_sha256") != spec["expected_sha256"]
            or durable.get("normalized_mode") != 0o600
            or original.get("path") != str(spec["path"])
            or original.get("sha256") != spec["expected_sha256"]
            or original.get("mode") not in {0o600, 0o644}
        ):
            raise ReleaseBuildError(
                "synthetic store mode normalization intent is invalid"
            )
        observed = _set_synthetic_store_mode(
            spec["path"],
            label=spec["label"],
            expected=original,
            allowed_current_modes={int(original["mode"])},
            target_mode=0o600,
        )
        normalized[name] = {
            "original": copy.deepcopy(original),
            "normalized": observed,
        }
    return {
        "status": "normalized",
        "transaction_id": plan["transaction_id"],
        "stores": normalized,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _restore_synthetic_completion_store_modes(
    plan: dict,
    intent: dict,
    normalization: dict | None,
    *,
    quarantined: dict | None = None,
) -> dict:
    if (
        not isinstance(intent, dict)
        or intent.get("transaction_id") != plan.get("transaction_id")
        or not isinstance(intent.get("stores"), dict)
        or (
            normalization is not None
            and (
                not isinstance(normalization, dict)
                or normalization.get("transaction_id")
                != plan.get("transaction_id")
                or not isinstance(normalization.get("stores"), dict)
            )
        )
    ):
        raise ReleaseBuildError(
            "synthetic store mode normalization receipt is invalid"
        )
    restored: dict[str, dict] = {}
    for name, spec in _synthetic_store_specs(plan).items():
        durable = intent["stores"].get(name)
        normalized = (
            normalization["stores"].get(name)
            if isinstance(normalization, dict)
            else None
        )
        if (
            not isinstance(durable, dict)
            or not isinstance(durable.get("original"), dict)
            or (
                normalization is not None
                and (
                    not isinstance(normalized, dict)
                    or normalized.get("original")
                    != durable.get("original")
                    or not isinstance(normalized.get("normalized"), dict)
                )
            )
        ):
            raise ReleaseBuildError(
                "synthetic store mode normalization receipt is invalid"
            )
        original = durable["original"]
        if quarantined is None:
            if normalized is None:
                observed, _value = _read_synthetic_store_receipt(
                    spec["path"],
                    label=spec["label"],
                    allowed_modes={0o600, int(original["mode"])},
                )
                if not _synthetic_store_receipts_match_stable(
                    observed,
                    original,
                ):
                    raise DrainIdentityMismatch(
                        f"{spec['label']} changed before restore"
                    )
                expected_live = observed
            else:
                expected_live = normalized["normalized"]
        else:
            quarantine_store = quarantined.get(name)
            if (
                quarantined.get("status") != "quarantined-never-replay"
                or not isinstance(quarantine_store, dict)
                or not isinstance(quarantine_store.get("source"), dict)
                or not isinstance(quarantine_store.get("quarantine"), dict)
            ):
                raise ReleaseBuildError(
                    "synthetic quarantine receipt is invalid for mode restore"
                )
            expected_live = quarantine_store["source"]
            backup, _value = _read_synthetic_store_receipt(
                Path(quarantine_store["quarantine"]["path"]),
                label=f"{spec['label']} quarantine",
                allowed_modes={0o600},
            )
            if (
                backup["sha256"] != original["sha256"]
                or not _synthetic_store_receipts_match_stable(
                    backup,
                    quarantine_store["quarantine"],
                )
            ):
                raise DrainIdentityMismatch(
                    f"{spec['label']} quarantine changed before restore"
                )
        current, _value = _read_synthetic_store_receipt(
            spec["path"],
            label=spec["label"],
            allowed_modes={0o600, int(original["mode"])},
        )
        if not _synthetic_store_receipts_match_stable(
            current,
            expected_live,
        ):
            raise DrainIdentityMismatch(
                f"{spec['label']} changed before restore"
            )
        changed = _set_synthetic_store_mode(
            spec["path"],
            label=spec["label"],
            expected=current,
            allowed_current_modes={int(current["mode"])},
            target_mode=int(original["mode"]),
        )
        if quarantined is None and changed["sha256"] != original["sha256"]:
            raise DrainIdentityMismatch(
                f"{spec['label']} bytes changed during restore"
            )
        restored[name] = {
            "status": (
                "already-restored"
                if current["mode"] == original["mode"]
                else "restored"
            ),
            "original_mode": original["mode"],
            "live": changed,
            "content_disposition": (
                "restored-exact"
                if quarantined is None
                else "quarantined-never-replayed"
            ),
        }
    return {
        "status": (
            "restored"
            if quarantined is None
            else "restored-with-quarantine"
        ),
        "stores": restored,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _inspect_synthetic_completion_stores(plan: dict) -> dict:
    process_path = Path(plan["synthetic_process_notifications_path"])
    delegation_path = Path(plan["synthetic_async_delegations_path"])
    process_receipt, process_store = _read_synthetic_store_receipt(
        process_path,
        label="synthetic process completion store",
        allowed_modes={0o600},
    )
    delegation_receipt, delegation_store = _read_synthetic_store_receipt(
        delegation_path,
        label="synthetic async delegation store",
        allowed_modes={0o600},
    )
    if (
        process_receipt["sha256"]
        != plan["synthetic_process_notifications_expected_sha256"]
        or delegation_receipt["sha256"]
        != plan["synthetic_async_delegations_expected_sha256"]
    ):
        raise ReleaseBuildError("synthetic completion store CAS changed")
    expected_process_ids = set(plan["synthetic_process_notification_ids"])
    if (
        not isinstance(process_store, dict)
        or set(process_store) != {"version", "events"}
        or process_store.get("version") != 1
        or not isinstance(process_store.get("events"), dict)
    ):
        raise ReleaseBuildError("synthetic process completion schema is invalid")
    events = process_store["events"]
    observed_process_ids: set[str] = set()
    process_delivered = 0
    process_queued = 0
    for event_id, event in events.items():
        if (
            not isinstance(event_id, str)
            or not isinstance(event, dict)
            or event.get("event_id") != event_id
            or event.get("type") != "completion"
            or not isinstance(event.get("delivered"), bool)
        ):
            raise ReleaseBuildError(
                "synthetic process completion record is invalid"
            )
        session_id = event.get("session_id")
        process_start_token = event.get("process_start_token")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id.encode("utf-8")) > 512
            or len(event_id.encode("utf-8")) > 1024
            or (
                process_start_token is not None
                and (
                    not isinstance(process_start_token, str)
                    or not process_start_token
                )
            )
        ):
            raise ReleaseBuildError(
                "synthetic process completion event identity is invalid"
            )
        expected_event_id = (
            "process:"
            f"{session_id}:"
            f"{hashlib.sha256(process_start_token.encode('utf-8')).hexdigest()[:24]}:"
            "completion"
            if process_start_token is not None
            else f"process:{session_id}:completion"
        )
        if event_id != expected_event_id:
            raise ReleaseBuildError(
                "synthetic process completion event identity is invalid"
            )
        observed_process_ids.add(session_id)
        if event["delivered"]:
            process_delivered += 1
        else:
            process_queued += 1
    if observed_process_ids != expected_process_ids:
        raise ReleaseBuildError(
            "synthetic process completion id set changed"
        )

    expected_delegation_ids = set(plan["synthetic_async_delegation_ids"])
    if (
        not isinstance(delegation_store, dict)
        or set(delegation_store) != {"version", "records"}
        or delegation_store.get("version") != 1
        or not isinstance(delegation_store.get("records"), dict)
        or set(delegation_store["records"]) != expected_delegation_ids
    ):
        raise ReleaseBuildError("synthetic async delegation schema is invalid")
    delegation_delivered = 0
    delegation_queued = 0
    terminal_statuses = {"completed", "error", "interrupted", "lost"}
    for delegation_id, entry in delegation_store["records"].items():
        record = entry.get("record") if isinstance(entry, dict) else None
        entry_status = str((entry or {}).get("status") or "")
        record_status = str((record or {}).get("status") or "")
        statuses = {value for value in (entry_status, record_status) if value}
        delivery_status = str((entry or {}).get("delivery_status") or "")
        if (
            not isinstance(entry, dict)
            or str(entry.get("delegation_id") or "") != delegation_id
            or not isinstance(record, dict)
            or str(record.get("delegation_id") or "") != delegation_id
            or len(statuses) != 1
            or not statuses.issubset(terminal_statuses)
            or delivery_status not in {"delivered", "queued"}
        ):
            raise ReleaseBuildError(
                "synthetic async delegation record is not terminal"
            )
        if delivery_status == "delivered":
            delegation_delivered += 1
        else:
            delegation_queued += 1
    return {
        "status": "verified",
        "process_notifications": {
            "path": str(process_path),
            "sha256": plan[
                "synthetic_process_notifications_expected_sha256"
            ],
            "ids": sorted(observed_process_ids),
            "terminal": len(observed_process_ids),
            "delivered": process_delivered,
            "queued": process_queued,
        },
        "async_delegations": {
            "path": str(delegation_path),
            "sha256": plan[
                "synthetic_async_delegations_expected_sha256"
            ],
            "ids": sorted(expected_delegation_ids),
            "terminal": len(expected_delegation_ids),
            "delivered": delegation_delivered,
            "queued": delegation_queued,
            "running": 0,
        },
    }


def _synthetic_quarantine_paths(plan: dict) -> tuple[Path, Path, Path]:
    root = Path(plan["synthetic_quarantine_root"])
    parent = _prepare_release_root(root.parent)
    if root.parent != parent:
        raise ReleaseBuildError("synthetic quarantine parent is not canonical")
    if not root.exists():
        root.mkdir(mode=0o700)
        _fsync_directory(root.parent)
    opened = root.lstat()
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.resolve(strict=True) != root
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        raise ReleaseBuildError("synthetic quarantine root is unsafe")
    transaction_root = root / plan["transaction_id"]
    if not transaction_root.exists():
        transaction_root.mkdir(mode=0o700)
        _fsync_directory(root)
    transaction_stat = transaction_root.lstat()
    if (
        transaction_root.is_symlink()
        or not transaction_root.is_dir()
        or transaction_root.resolve(strict=True) != transaction_root
        or transaction_stat.st_uid != os.getuid()
        or stat.S_IMODE(transaction_stat.st_mode) & 0o077
    ):
        raise ReleaseBuildError("synthetic transaction quarantine is unsafe")
    return (
        transaction_root,
        transaction_root / "process_notifications.original.json",
        transaction_root / "async_delegations.original.json",
    )


def _synthetic_quarantine_intent_receipt(plan: dict) -> dict:
    transaction_root, process_backup, delegation_backup = (
        _synthetic_quarantine_paths(plan)
    )
    source_receipts: dict[str, dict] = {}
    for name, spec in _synthetic_store_specs(plan).items():
        receipt, _value = _read_synthetic_store_receipt(
            spec["path"],
            label=spec["label"],
            allowed_modes={0o600},
        )
        if receipt["sha256"] != spec["expected_sha256"]:
            raise ReleaseBuildError("synthetic completion store CAS changed")
        source_receipts[name] = receipt
    return {
        "status": "prepared",
        "transaction_id": plan["transaction_id"],
        "transaction_root": str(transaction_root),
        "stores": {
            "process_notifications": {
                "source": plan["synthetic_process_notifications_path"],
                "quarantine": str(process_backup),
                "expected_sha256": plan[
                    "synthetic_process_notifications_expected_sha256"
                ],
                "expected_ids": sorted(
                    plan["synthetic_process_notification_ids"]
                ),
                "source_receipt": source_receipts[
                    "process_notifications"
                ],
            },
            "async_delegations": {
                "source": plan["synthetic_async_delegations_path"],
                "quarantine": str(delegation_backup),
                "expected_sha256": plan[
                    "synthetic_async_delegations_expected_sha256"
                ],
                "expected_ids": sorted(
                    plan["synthetic_async_delegation_ids"]
                ),
                "source_receipt": source_receipts["async_delegations"],
            },
        },
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _validate_synthetic_quarantine_intent(plan: dict, intent: dict) -> dict:
    transaction_root, process_backup, delegation_backup = (
        _synthetic_quarantine_paths(plan)
    )
    expected = {
        "process_notifications": {
            "source": plan["synthetic_process_notifications_path"],
            "quarantine": str(process_backup),
            "expected_sha256": plan[
                "synthetic_process_notifications_expected_sha256"
            ],
            "expected_ids": sorted(
                plan["synthetic_process_notification_ids"]
            ),
        },
        "async_delegations": {
            "source": plan["synthetic_async_delegations_path"],
            "quarantine": str(delegation_backup),
            "expected_sha256": plan[
                "synthetic_async_delegations_expected_sha256"
            ],
            "expected_ids": sorted(
                plan["synthetic_async_delegation_ids"]
            ),
        },
    }
    if (
        not isinstance(intent, dict)
        or intent.get("status") != "prepared"
        or intent.get("transaction_id") != plan.get("transaction_id")
        or intent.get("transaction_root") != str(transaction_root)
        or not isinstance(intent.get("stores"), dict)
        or set(intent["stores"]) != set(expected)
    ):
        raise ReleaseBuildError("synthetic quarantine intent changed")
    for name, static in expected.items():
        durable = intent["stores"][name]
        source_receipt = (
            durable.get("source_receipt")
            if isinstance(durable, dict)
            else None
        )
        if (
            not isinstance(durable, dict)
            or any(durable.get(key) != value for key, value in static.items())
            or not isinstance(source_receipt, dict)
            or source_receipt.get("path") != static["source"]
            or source_receipt.get("sha256") != static["expected_sha256"]
            or source_receipt.get("mode") != 0o600
            or source_receipt.get("uid") != os.getuid()
            or source_receipt.get("nlink") != 1
        ):
            raise ReleaseBuildError("synthetic quarantine intent changed")
    return intent


def _write_empty_synthetic_store(
    path: Path,
    payload: dict,
    *,
    label: str,
) -> dict:
    parent = _prepare_release_root(path.parent)
    if (
        parent != path.parent
        or path.exists()
        or path.is_symlink()
    ):
        raise ReleaseBuildError("synthetic empty store path is unsafe")
    _atomic_write_transaction_journal(path, payload)
    receipt, _value = _read_synthetic_store_receipt(
        path,
        label=label,
        allowed_modes={0o600},
    )
    return receipt


def _quarantine_one_synthetic_store(
    *,
    source: Path,
    quarantine: Path,
    expected_sha256: str,
    empty_payload: dict,
    source_receipt: dict,
    label: str,
    allow_snapshot_rebind: bool,
) -> dict:
    empty_bytes = (
        json.dumps(
            empty_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    empty_sha256 = hashlib.sha256(empty_bytes).hexdigest()
    try:
        quarantine.lstat()
        quarantine_exists = True
    except FileNotFoundError:
        quarantine_exists = False
    try:
        source.lstat()
        source_exists = True
    except FileNotFoundError:
        source_exists = False
    quarantine_receipt: dict | None = None
    quarantine_expected_receipt = source_receipt
    if quarantine_exists:
        quarantine_receipt, _value = _read_synthetic_store_receipt(
            quarantine,
            label=f"{label} quarantine",
            allowed_modes={0o600},
        )
        quarantine_matches = _synthetic_store_receipts_match_stable(
            quarantine_receipt,
            source_receipt,
            moved=True,
        )
        quarantine_snapshot_rebound = (
            allow_snapshot_rebind
            and quarantine_receipt.get("uid")
            == source_receipt.get("uid")
            and quarantine_receipt.get("nlink") == 1
            and quarantine_receipt.get("mode") == 0o600
            and quarantine_receipt.get("size")
            == source_receipt.get("size")
            and quarantine_receipt.get("sha256")
            == source_receipt.get("sha256")
        )
        if (
            quarantine_receipt["sha256"] != expected_sha256
            or not (
                quarantine_matches
                or quarantine_snapshot_rebound
            )
        ):
            raise ReleaseBuildError(
                "synthetic quarantine CAS identity changed"
            )
        if quarantine_snapshot_rebound and not quarantine_matches:
            quarantine_expected_receipt = quarantine_receipt
    if source_exists:
        live_receipt, _value = _read_synthetic_store_receipt(
            source,
            label=label,
            allowed_modes={0o600},
        )
        source_sha256 = live_receipt["sha256"]
        if source_sha256 == expected_sha256:
            if not quarantine_exists:
                source_matches = _synthetic_store_receipts_match_stable(
                    live_receipt,
                    source_receipt,
                )
                snapshot_rebound = (
                    allow_snapshot_rebind
                    and live_receipt.get("path")
                    == source_receipt.get("path")
                    and live_receipt.get("uid")
                    == source_receipt.get("uid")
                    and live_receipt.get("nlink") == 1
                    and live_receipt.get("mode") == 0o600
                    and live_receipt.get("size")
                    == source_receipt.get("size")
                    and live_receipt.get("sha256")
                    == source_receipt.get("sha256")
                )
                if not source_matches and not snapshot_rebound:
                    raise DrainIdentityMismatch(
                        "synthetic live store changed before quarantine CAS"
                    )
                if source.stat().st_dev != quarantine.parent.stat().st_dev:
                    raise ReleaseBuildError(
                        "synthetic quarantine is not on the source filesystem"
                    )
                os.replace(source, quarantine)
                _fsync_directory(source.parent)
                if source.parent != quarantine.parent:
                    _fsync_directory(quarantine.parent)
                quarantine_exists = True
                quarantine_receipt, _value = _read_synthetic_store_receipt(
                    quarantine,
                    label=f"{label} quarantine",
                    allowed_modes={0o600},
                )
                if snapshot_rebound and not source_matches:
                    quarantine_expected_receipt = live_receipt
            else:
                verify_live, _value = _read_synthetic_store_receipt(
                    source,
                    label=label,
                    allowed_modes={0o600},
                )
                if not _synthetic_store_receipts_match_stable(
                    verify_live,
                    live_receipt,
                ):
                    raise DrainIdentityMismatch(
                        "synthetic live store changed before quarantine CAS"
                    )
                source.unlink()
                _fsync_directory(source.parent)
            source_exists = False
        elif source_sha256 != empty_sha256 or not quarantine_exists:
            raise ReleaseBuildError("synthetic live store CAS identity changed")
    if not quarantine_exists:
        raise ReleaseBuildError("synthetic quarantine source is missing")
    if not source_exists:
        _write_empty_synthetic_store(
            source,
            empty_payload,
            label=label,
        )
    live_receipt, _value = _read_synthetic_store_receipt(
        source,
        label=label,
        allowed_modes={0o600},
    )
    quarantine_receipt, _value = _read_synthetic_store_receipt(
        quarantine,
        label=f"{label} quarantine",
        allowed_modes={0o600},
    )
    if (
        live_receipt["sha256"] != empty_sha256
        or quarantine_receipt["sha256"] != expected_sha256
        or not _synthetic_store_receipts_match_stable(
            quarantine_receipt,
            quarantine_expected_receipt,
            moved=True,
        )
    ):
        raise ReleaseBuildError("synthetic quarantine verification failed")
    return {
        "source": live_receipt,
        "quarantine": quarantine_receipt,
        "expected_original_sha256": expected_sha256,
        "empty_sha256": empty_sha256,
    }


def _quarantine_synthetic_completion_stores(
    plan: dict,
    intent: dict,
    *,
    crash_at: str | None = None,
    state_restore: dict | None = None,
) -> dict:
    _validate_synthetic_quarantine_intent(plan, intent)
    allow_snapshot_rebind = (
        isinstance(state_restore, dict)
        and state_restore.get("status") == "restored"
        and state_restore.get("state_snapshot_id")
        == plan.get("transaction_id")
    )
    stores = intent["stores"]
    process_spec = stores["process_notifications"]
    delegation_spec = stores["async_delegations"]
    process = _quarantine_one_synthetic_store(
        source=Path(process_spec["source"]),
        quarantine=Path(process_spec["quarantine"]),
        expected_sha256=process_spec["expected_sha256"],
        empty_payload={"version": 1, "events": {}},
        source_receipt=process_spec["source_receipt"],
        label="synthetic process completion store",
        allow_snapshot_rebind=allow_snapshot_rebind,
    )
    if crash_at == "after_process_store":
        raise InjectedCutoverCrash(crash_at)
    delegation = _quarantine_one_synthetic_store(
        source=Path(delegation_spec["source"]),
        quarantine=Path(delegation_spec["quarantine"]),
        expected_sha256=delegation_spec["expected_sha256"],
        empty_payload={"version": 1, "records": {}},
        source_receipt=delegation_spec["source_receipt"],
        label="synthetic async delegation store",
        allow_snapshot_rebind=allow_snapshot_rebind,
    )
    if crash_at == "after_delegation_store":
        raise InjectedCutoverCrash(crash_at)
    return {
        "status": "quarantined-never-replay",
        "transaction_root": intent["transaction_root"],
        "process_notifications": {
            **process,
            "quarantined_ids": process_spec["expected_ids"],
        },
        "async_delegations": {
            **delegation,
            "quarantined_ids": delegation_spec["expected_ids"],
        },
    }


def _legacy_durable_activity_receipt(plan: dict) -> dict:
    state_db = Path(plan["legacy_state_db"])
    if (
        not state_db.is_file()
        or state_db.is_symlink()
        or state_db.resolve(strict=True) != state_db
    ):
        raise ReleaseBuildError("legacy activity database is invalid")
    try:
        connection = sqlite3.connect(
            f"{state_db.as_uri()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'session_activity'"
            ).fetchone()
            if table is None:
                raise ReleaseBuildError(
                    "legacy activity lease table is unavailable"
                )
            rows = connection.execute(
                "SELECT session_id, run_id, source, phase, heartbeat_at "
                "FROM session_activity "
                "WHERE source = 'webui' OR source LIKE 'webui-%'"
            ).fetchall()
        finally:
            connection.close()
    except ReleaseBuildError:
        raise
    except sqlite3.Error as exc:
        raise ReleaseBuildError(
            "legacy activity lease proof is unavailable"
        ) from exc

    try:
        activity_ttl_seconds = float(SESSION_ACTIVITY_TTL_SECONDS)
    except (TypeError, ValueError) as exc:
        raise ReleaseBuildError(
            "legacy activity lease TTL is invalid"
        ) from exc
    if (
        not math.isfinite(activity_ttl_seconds)
        or activity_ttl_seconds <= 0.0
    ):
        raise ReleaseBuildError("legacy activity lease TTL is invalid")
    cutoff = float(time.time()) - activity_ttl_seconds
    active_rows = []
    expired_rows = []
    for row in rows:
        try:
            heartbeat_at = float(row[4])
        except (TypeError, ValueError) as exc:
            raise ReleaseBuildError(
                "legacy activity lease heartbeat is invalid"
            ) from exc
        if not math.isfinite(heartbeat_at):
            raise ReleaseBuildError(
                "legacy activity lease heartbeat is invalid"
            )
        if heartbeat_at >= cutoff:
            active_rows.append(row)
        else:
            expired_rows.append(row)
    if active_rows:
        raise ReleaseBuildError(
            "legacy WebUI still has durable active-run leases"
        )

    checkpoint = _legacy_process_checkpoint_receipt(plan)
    return {
        "status": "verified",
        "state_db": str(state_db),
        "webui_active_run_leases": 0,
        "expired_webui_activity_rows": len(expired_rows),
        "activity_ttl_seconds": activity_ttl_seconds,
        "gateway_process_checkpoint": checkpoint,
    }


def _legacy_gateway_worker_receipt(plan: dict, prepared: dict) -> dict:
    status_path = Path(
        plan["synthetic_process_notifications_path"]
    ).with_name("gateway_state.json")
    status = _read_private_json_value(
        status_path,
        label="legacy gateway runtime status",
    )
    try:
        active_agents = int(status.get("active_agents", -1))
        status_pid = int(status.get("pid", -1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseBuildError(
            "legacy gateway runtime status is invalid"
        ) from exc
    if (
        not isinstance(status, dict)
        or status_pid != int(prepared["gateway"]["pid"])
        or active_agents != 0
        or str(status.get("gateway_state") or "")
        not in {"running", "degraded", "draining"}
    ):
        raise ReleaseBuildError(
            "legacy gateway runtime status has active or mismatched work"
        )
    return {
        "status": "verified",
        "path": str(status_path),
        "sha256": sha256_file(status_path),
        "pid": status_pid,
        "gateway_state": status["gateway_state"],
        "active_agents": 0,
    }


def _legacy_gateway_home(plan: dict) -> Path:
    process_store = Path(plan["synthetic_process_notifications_path"])
    home = process_store.parent
    if (
        not home.is_dir()
        or home.is_symlink()
        or home.resolve(strict=True) != home
        or process_store.parent != home
    ):
        raise ReleaseBuildError("legacy gateway home is not canonical")
    return home


def _legacy_cron_tick_lock_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / "cron" / ".tick.lock"


_LEGACY_CRON_TICK_LOCK_MAX_BYTES = 1024 * 1024
_LEGACY_CRON_TICK_LOCK_STABLE_FIELDS = (
    "path",
    "device",
    "inode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "sha256",
)


def _prepare_legacy_cron_lock_parent(plan: dict) -> Path:
    home = _legacy_gateway_home(plan)
    try:
        home_state = home.lstat()
    except OSError as exc:
        raise ReleaseBuildError("legacy cron tick lock home is unavailable") from exc
    if (
        home.is_symlink()
        or not stat.S_ISDIR(home_state.st_mode)
        or home.resolve(strict=True) != home
        or home_state.st_uid != os.getuid()
        or stat.S_IMODE(home_state.st_mode) != 0o700
    ):
        raise ReleaseBuildError("legacy cron tick lock home is unsafe")
    parent = home / "cron"
    created = False
    try:
        parent.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReleaseBuildError(
            "legacy cron tick lock parent cannot be created"
        ) from exc
    try:
        parent_state = parent.lstat()
    except OSError as exc:
        raise ReleaseBuildError(
            "legacy cron tick lock parent is unavailable"
        ) from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_state.st_mode)
        or parent.resolve(strict=True) != parent
        or parent.parent != home
        or parent_state.st_uid != os.getuid()
        or stat.S_IMODE(parent_state.st_mode) != 0o700
    ):
        raise ReleaseBuildError("legacy cron tick lock parent is unsafe")
    if created:
        _fsync_directory(home)
    return parent


def _legacy_cron_tick_file_receipt(
    path: Path,
    descriptor: int,
    *,
    allowed_modes: set[int],
) -> dict:
    try:
        before = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = bytearray()
        while len(payload) <= _LEGACY_CRON_TICK_LOCK_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    _LEGACY_CRON_TICK_LOCK_MAX_BYTES + 1 - len(payload),
                ),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise DrainIdentityMismatch(
            "legacy cron tick lock identity became unavailable"
        ) from exc
    same_fd_path_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    mode = stat.S_IMODE(after.st_mode)
    if (
        len(payload) > _LEGACY_CRON_TICK_LOCK_MAX_BYTES
        or not stat.S_ISREG(after.st_mode)
        or after.st_uid != os.getuid()
        or after.st_nlink != 1
        or mode not in allowed_modes
        or any(
            getattr(before, field) != getattr(after, field)
            or getattr(current, field) != getattr(after, field)
            for field in same_fd_path_fields
        )
    ):
        raise ReleaseBuildError("legacy cron tick lock is unsafe")
    return {
        "status": "present",
        "path": str(path),
        "device": after.st_dev,
        "inode": after.st_ino,
        "uid": after.st_uid,
        "mode": mode,
        "nlink": after.st_nlink,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _read_legacy_cron_tick_file_receipt(
    plan: dict,
    *,
    allowed_modes: set[int],
    allow_absent: bool,
) -> dict:
    parent = _prepare_legacy_cron_lock_parent(plan)
    path = parent / ".tick.lock"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "legacy cron tick lock cannot be opened without O_NOFOLLOW"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        if not allow_absent:
            raise ReleaseBuildError("legacy cron tick lock is missing")
        return {
            "status": "absent",
            "path": str(path),
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        }
    except OSError as exc:
        raise ReleaseBuildError("legacy cron tick lock is unreadable") from exc
    try:
        os.set_inheritable(descriptor, False)
        return _legacy_cron_tick_file_receipt(
            path,
            descriptor,
            allowed_modes=allowed_modes,
        )
    finally:
        os.close(descriptor)


def _legacy_cron_tick_lock_normalize_intent_receipt(plan: dict) -> dict:
    transaction_id = str(plan.get("transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError("legacy cron tick lock transaction is invalid")
    original = _read_legacy_cron_tick_file_receipt(
        plan,
        allowed_modes={0o600, 0o644},
        allow_absent=True,
    )
    return {
        "status": "prepared",
        "transaction_id": transaction_id,
        "path": str(_legacy_cron_tick_lock_path(plan)),
        "original": original,
        "normalized_mode": 0o600,
        "absent_normalized_sha256": hashlib.sha256(b"").hexdigest(),
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _legacy_cron_tick_receipts_match_stable(
    actual: dict,
    expected: dict,
) -> bool:
    return all(
        actual.get(field) == expected.get(field)
        for field in _LEGACY_CRON_TICK_LOCK_STABLE_FIELDS
    )


def _legacy_cron_tick_receipts_match_locked_mtime_churn(
    plan: dict,
    actual: dict,
    expected: dict,
) -> bool:
    if _legacy_cron_tick_receipts_match_stable(actual, expected):
        return True
    transaction_id = str(plan.get("transaction_id") or "")
    handle = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
    if handle is None or getattr(handle, "closed", True):
        return False
    try:
        held_receipt = _legacy_cron_tick_lock_receipt(
            _legacy_cron_tick_lock_path(plan),
            os.fstat(handle.fileno()),
        )
        _verify_legacy_cron_tick_lock(
            plan,
            held_receipt,
            allowed_modes={0o600, 0o644},
        )
    except (OSError, ReleaseBuildError):
        return False
    return _legacy_cron_tick_receipts_differ_only_empty_mtime(
        actual,
        expected,
    )


def _legacy_cron_tick_receipts_differ_only_empty_mtime(
    actual: dict,
    expected: dict,
) -> bool:
    stable_without_mtime = tuple(
        field
        for field in _LEGACY_CRON_TICK_LOCK_STABLE_FIELDS
        if field != "mtime_ns"
    )
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    return (
        all(
            actual.get(field) == expected.get(field)
            for field in stable_without_mtime
        )
        and actual.get("size") == expected.get("size") == 0
        and actual.get("sha256")
        == expected.get("sha256")
        == empty_sha256
    )


def _legacy_cron_tick_normalizations_match(
    plan: dict,
    actual: dict,
    expected: dict,
) -> bool:
    return (
        isinstance(actual, dict)
        and isinstance(expected, dict)
        and actual.get("status") == expected.get("status") == "normalized"
        and actual.get("transaction_id") == expected.get("transaction_id")
        and actual.get("original") == expected.get("original")
        and actual.get("bounded_host_assumption")
        == expected.get("bounded_host_assumption")
        and isinstance(actual.get("normalized"), dict)
        and isinstance(expected.get("normalized"), dict)
        and _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            actual["normalized"],
            expected["normalized"],
        )
    )


def _normalize_legacy_cron_tick_lock(plan: dict, intent: dict) -> dict:
    if (
        not isinstance(intent, dict)
        or intent.get("status") != "prepared"
        or intent.get("transaction_id") != plan.get("transaction_id")
        or intent.get("path") != str(_legacy_cron_tick_lock_path(plan))
        or intent.get("normalized_mode") != 0o600
        or intent.get("absent_normalized_sha256")
        != hashlib.sha256(b"").hexdigest()
        or not isinstance(intent.get("original"), dict)
    ):
        raise ReleaseBuildError(
            "legacy cron tick lock normalization intent is invalid"
        )
    original = intent["original"]
    parent = _prepare_legacy_cron_lock_parent(plan)
    path = parent / ".tick.lock"
    if original.get("status") == "absent":
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if not isinstance(nofollow, int) or nofollow == 0:
            raise ReleaseBuildError(
                "legacy cron tick lock cannot be opened without O_NOFOLLOW"
            )
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            current = _read_legacy_cron_tick_file_receipt(
                plan,
                allowed_modes={0o600},
                allow_absent=False,
            )
            if (
                current.get("size") != 0
                or current.get("sha256")
                != intent["absent_normalized_sha256"]
            ):
                raise DrainIdentityMismatch(
                    "legacy cron tick lock changed before normalization"
                )
            return {
                "status": "normalized",
                "transaction_id": plan["transaction_id"],
                "original": copy.deepcopy(original),
                "normalized": current,
                "bounded_host_assumption": copy.deepcopy(
                    _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
                ),
            }
        except OSError as exc:
            raise ReleaseBuildError(
                "legacy cron tick lock cannot be created"
            ) from exc
        try:
            os.set_inheritable(descriptor, False)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            normalized = _legacy_cron_tick_file_receipt(
                path,
                descriptor,
                allowed_modes={0o600},
            )
        finally:
            os.close(descriptor)
        _fsync_directory(parent)
    elif original.get("status") == "present":
        original_mode = original.get("mode")
        if original_mode not in {0o600, 0o644}:
            raise ReleaseBuildError(
                "legacy cron tick lock normalization intent is invalid"
            )
        current = _read_legacy_cron_tick_file_receipt(
            plan,
            allowed_modes={0o600, int(original_mode)},
            allow_absent=False,
        )
        if not _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            current,
            original,
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed before normalization"
            )
        if current["mode"] == 0o600:
            normalized = current
        else:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if not isinstance(nofollow, int) or nofollow == 0:
                raise ReleaseBuildError(
                    "legacy cron tick lock cannot be opened without O_NOFOLLOW"
                )
            try:
                descriptor = os.open(
                    path,
                    os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as exc:
                raise ReleaseBuildError(
                    "legacy cron tick lock is unreadable"
                ) from exc
            try:
                os.set_inheritable(descriptor, False)
                opened = _legacy_cron_tick_file_receipt(
                    path,
                    descriptor,
                    allowed_modes={int(original_mode)},
                )
                if opened != current:
                    raise DrainIdentityMismatch(
                        "legacy cron tick lock changed before normalization"
                    )
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                normalized = _legacy_cron_tick_file_receipt(
                    path,
                    descriptor,
                    allowed_modes={0o600},
                )
            finally:
                os.close(descriptor)
            _fsync_directory(parent)
        if not _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            normalized,
            original,
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed during normalization"
            )
    else:
        raise ReleaseBuildError(
            "legacy cron tick lock normalization intent is invalid"
        )
    return {
        "status": "normalized",
        "transaction_id": plan["transaction_id"],
        "original": copy.deepcopy(original),
        "normalized": normalized,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _normalize_and_acquire_legacy_cron_tick_lock(
    plan: dict,
    intent: dict,
) -> tuple[dict, dict]:
    original = intent.get("original") if isinstance(intent, dict) else None
    if not isinstance(original, dict):
        raise ReleaseBuildError(
            "legacy cron tick lock normalization intent is invalid"
        )
    transaction_id = str(plan.get("transaction_id") or "")
    normalization = None
    try:
        if original.get("status") == "present":
            original_mode = original.get("mode")
            if original_mode not in {0o600, 0o644}:
                raise ReleaseBuildError(
                    "legacy cron tick lock normalization intent is invalid"
                )
            _acquire_legacy_cron_tick_lock_modes(
                plan,
                allowed_modes={0o600, int(original_mode)},
            )
        normalization = _normalize_legacy_cron_tick_lock(plan, intent)
        held = _acquire_legacy_cron_tick_lock(plan)
        current = _read_legacy_cron_tick_file_receipt(
            plan,
            allowed_modes={0o600},
            allow_absent=False,
        )
        if not _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            current,
            normalization["normalized"],
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed after normalization"
            )
        return normalization, held
    except Exception:
        try:
            handle = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
            mode_changed = (
                handle is not None
                and not getattr(handle, "closed", True)
                and stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
                != original.get("mode")
            )
            if (
                original.get("status") == "present"
                and handle is not None
                and not getattr(handle, "closed", True)
                and (normalization is not None or mode_changed)
            ):
                _restore_legacy_cron_tick_lock(
                    plan,
                    intent,
                    normalization,
                )
        finally:
            handle = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
            if handle is not None and not getattr(handle, "closed", True):
                _release_legacy_cron_tick_lock(
                    plan,
                    allow_restored_mode=True,
                )
        raise


def _restore_legacy_cron_tick_lock(
    plan: dict,
    intent: dict,
    normalization: dict | None,
    *,
    state_restore: dict | None = None,
) -> dict:
    if (
        not isinstance(intent, dict)
        or intent.get("transaction_id") != plan.get("transaction_id")
        or intent.get("path") != str(_legacy_cron_tick_lock_path(plan))
        or not isinstance(intent.get("original"), dict)
    ):
        raise ReleaseBuildError(
            "legacy cron tick lock normalization intent is invalid"
        )
    original = intent["original"]
    path = _legacy_cron_tick_lock_path(plan)
    if normalization is not None and (
        not isinstance(normalization, dict)
        or normalization.get("transaction_id") != plan.get("transaction_id")
        or normalization.get("original") != original
        or not isinstance(normalization.get("normalized"), dict)
    ):
        raise ReleaseBuildError(
            "legacy cron tick lock normalization receipt is invalid"
        )
    normalized = (
        normalization.get("normalized")
        if isinstance(normalization, dict)
        else None
    )
    allow_snapshot_rebind = (
        isinstance(state_restore, dict)
        and state_restore.get("status") == "restored"
        and state_restore.get("state_snapshot_id") == plan.get("transaction_id")
    )
    if original.get("status") == "absent":
        current = _read_legacy_cron_tick_file_receipt(
            plan,
            allowed_modes={0o600},
            allow_absent=True,
        )
        if current.get("status") == "absent":
            return {
                "status": "already-absent",
                "path": str(path),
                "bounded_host_assumption": copy.deepcopy(
                    _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
                ),
            }
        expected = normalized
        if expected is not None:
            matches = _legacy_cron_tick_receipts_match_stable(
                current,
                expected,
            )
            if not matches and allow_snapshot_rebind:
                matches = (
                    current.get("path") == expected.get("path")
                    and current.get("uid") == expected.get("uid")
                    and current.get("nlink") == 1
                    and current.get("mode") == 0o600
                    and current.get("size") == expected.get("size") == 0
                    and current.get("sha256")
                    == expected.get("sha256")
                    == intent.get("absent_normalized_sha256")
                )
        else:
            matches = (
                current.get("size") == 0
                and current.get("sha256")
                == intent.get("absent_normalized_sha256")
            )
        if not matches:
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed before restore"
            )
        verify = _read_legacy_cron_tick_file_receipt(
            plan,
            allowed_modes={0o600},
            allow_absent=False,
        )
        if not _legacy_cron_tick_receipts_match_stable(verify, current):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed before restore"
            )
        try:
            path.unlink()
        except OSError as exc:
            raise ReleaseBuildError(
                "legacy cron tick lock cannot be removed"
            ) from exc
        _fsync_directory(path.parent)
        return {
            "status": "removed",
            "path": str(path),
            "removed": current,
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        }
    if original.get("status") != "present" or original.get("mode") not in {
        0o600,
        0o644,
    }:
        raise ReleaseBuildError(
            "legacy cron tick lock normalization intent is invalid"
        )
    current = _read_legacy_cron_tick_file_receipt(
        plan,
        allowed_modes={0o600, int(original["mode"])},
        allow_absent=False,
    )
    matches_original = _legacy_cron_tick_receipts_match_locked_mtime_churn(
        plan,
        current,
        original,
    )
    matches_normalized = normalized is None or (
        _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            current,
            normalized,
        )
    )
    snapshot_rebound = False
    if (
        not matches_original
        and not matches_normalized
        and allow_snapshot_rebind
        and normalized is not None
    ):
        snapshot_rebound = (
            current.get("path") == normalized.get("path")
            and current.get("uid") == normalized.get("uid")
            and current.get("nlink") == 1
            and current.get("mode") in {0o600, int(original["mode"])}
            and current.get("size") == normalized.get("size")
            and current.get("sha256") == normalized.get("sha256")
        )
    if not (
        (matches_original and matches_normalized)
        or snapshot_rebound
    ):
        raise DrainIdentityMismatch(
            "legacy cron tick lock changed before restore"
        )
    if current["mode"] == original["mode"]:
        return {
            "status": "already-restored",
            "path": str(path),
            "restored": current,
            "snapshot_rebound": snapshot_rebound,
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        }
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "legacy cron tick lock cannot be opened without O_NOFOLLOW"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise ReleaseBuildError("legacy cron tick lock is unreadable") from exc
    try:
        os.set_inheritable(descriptor, False)
        opened = _legacy_cron_tick_file_receipt(
            path,
            descriptor,
            allowed_modes={0o600},
        )
        if not _legacy_cron_tick_receipts_match_locked_mtime_churn(
            plan,
            opened,
            current,
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed before restore"
            )
        os.fchmod(descriptor, int(original["mode"]))
        os.fsync(descriptor)
        restored = _legacy_cron_tick_file_receipt(
            path,
            descriptor,
            allowed_modes={int(original["mode"])},
        )
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    if not (
        _legacy_cron_tick_receipts_match_stable(restored, original)
        or _legacy_cron_tick_receipts_differ_only_empty_mtime(
            restored,
            original,
        )
    ) and not (
        snapshot_rebound
        and restored.get("path") == original.get("path")
        and restored.get("uid") == original.get("uid")
        and restored.get("nlink") == 1
        and restored.get("mode") == original.get("mode")
        and restored.get("size") == original.get("size")
        and restored.get("sha256") == original.get("sha256")
    ):
        raise DrainIdentityMismatch(
            "legacy cron tick lock changed during restore"
        )
    return {
        "status": "restored",
        "path": str(path),
        "restored": restored,
        "snapshot_rebound": snapshot_rebound,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _legacy_cron_tick_lock_receipt(path: Path, opened: os.stat_result) -> dict:
    return {
        "status": "held",
        "path": str(path),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _verify_legacy_cron_tick_lock(
    plan: dict,
    receipt: dict,
    *,
    allowed_modes: set[int] | None = None,
) -> dict:
    accepted_modes = {0o600} if allowed_modes is None else set(allowed_modes)
    transaction_id = str(plan.get("transaction_id") or "")
    handle = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
    if handle is None or getattr(handle, "closed", True):
        raise DrainIdentityMismatch("legacy cron tick lock is not held")
    path = _legacy_cron_tick_lock_path(plan)
    try:
        opened = os.fstat(handle.fileno())
        current = path.lstat()
    except (AttributeError, OSError) as exc:
        raise DrainIdentityMismatch(
            "legacy cron tick lock identity became unavailable"
        ) from exc
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink")
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) not in accepted_modes
        or any(
            getattr(current, field) != getattr(opened, field)
            for field in fields
        )
    ):
        raise DrainIdentityMismatch("legacy cron tick lock identity changed")
    actual = _legacy_cron_tick_lock_receipt(path, opened)
    if not isinstance(receipt, dict) or actual != receipt:
        raise DrainIdentityMismatch("legacy cron tick lock receipt changed")
    return actual


def _acquire_legacy_cron_tick_lock_modes(
    plan: dict,
    *,
    allowed_modes: set[int],
) -> dict:
    transaction_id = str(plan.get("transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError("legacy cron tick lock transaction is invalid")
    accepted_modes = set(allowed_modes)
    if not accepted_modes or not accepted_modes.issubset({0o600, 0o644}):
        raise ReleaseBuildError("legacy cron tick lock modes are invalid")
    existing = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
    if existing is not None and not getattr(existing, "closed", True):
        opened = os.fstat(existing.fileno())
        receipt = _legacy_cron_tick_lock_receipt(
            _legacy_cron_tick_lock_path(plan),
            opened,
        )
        return _verify_legacy_cron_tick_lock(
            plan,
            receipt,
            allowed_modes=accepted_modes,
        )
    parent = _prepare_legacy_cron_lock_parent(plan)
    path = parent / ".tick.lock"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "legacy cron tick lock cannot be opened without O_NOFOLLOW"
        )
    flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ReleaseBuildError(
            "legacy cron tick lock is not normalized"
        ) from exc
    except OSError as exc:
        raise ReleaseBuildError(
            "legacy cron tick lock is unreadable"
        ) from exc
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        os.set_inheritable(handle.fileno(), False)
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) not in accepted_modes
            or any(
                getattr(current, field) != getattr(opened, field)
                for field in fields
            )
        ):
            raise ReleaseBuildError("legacy cron tick lock is unsafe")
        deadline = time.monotonic() + float(plan["timeout_seconds"])
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DrainTimeout(
                        "legacy cron tick lock acquisition timed out"
                    )
                time.sleep(float(plan["interval_seconds"]))
        locked = os.fstat(handle.fileno())
        current = path.lstat()
        if any(
            getattr(current, field) != getattr(locked, field)
            for field in fields
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock changed while waiting"
            )
        _LEGACY_CRON_TICK_LOCKS[transaction_id] = handle
        receipt = _legacy_cron_tick_lock_receipt(path, locked)
        return _verify_legacy_cron_tick_lock(
            plan,
            receipt,
            allowed_modes=accepted_modes,
        )
    except Exception:
        handle.close()
        raise


def _acquire_legacy_cron_tick_lock(plan: dict) -> dict:
    """Exclude legacy cron admission on one bounded, single-operator host.

    This is an OS lock and exact inode proof, not a claim that a malicious
    concurrent process running under the same uid has been excluded.
    """
    return _acquire_legacy_cron_tick_lock_modes(
        plan,
        allowed_modes={0o600},
    )


def _release_legacy_cron_tick_lock(
    plan: dict,
    *,
    allow_restored_mode: bool = False,
) -> dict:
    transaction_id = str(plan.get("transaction_id") or "")
    handle = _LEGACY_CRON_TICK_LOCKS.get(transaction_id)
    if handle is None or getattr(handle, "closed", True):
        return {
            "status": "already-released",
            "path": str(_legacy_cron_tick_lock_path(plan)),
            "bounded_host_assumption": copy.deepcopy(
                _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
            ),
        }
    opened = os.fstat(handle.fileno())
    held = _legacy_cron_tick_lock_receipt(
        _legacy_cron_tick_lock_path(plan),
        opened,
    )
    if allow_restored_mode:
        path = _legacy_cron_tick_lock_path(plan)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) not in {0o600, 0o644}
            or any(
                getattr(current, field) != getattr(opened, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                )
            )
        ):
            raise DrainIdentityMismatch(
                "legacy cron tick lock identity changed before release"
            )
    else:
        _verify_legacy_cron_tick_lock(plan, held)
    _LEGACY_CRON_TICK_LOCKS.pop(transaction_id, None)
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    return {
        "status": "released",
        "path": held["path"],
        "device": held["device"],
        "inode": held["inode"],
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


_PROCESS_LOCK_IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
    "st_nlink",
)


def _process_registry_lock_path(plan: dict, *, kind: str) -> Path:
    home = _legacy_gateway_home(plan)
    if kind == "admission":
        name = ".processes.json.admission.lock"
    elif kind == "authority":
        name = ".processes.json.lock"
    elif kind == "completion":
        name = ".process_notifications.json.lock"
    else:
        raise ReleaseBuildError("process registry lock kind is invalid")
    return home / name


def _process_registry_lock_identity(path: Path, opened: os.stat_result) -> dict:
    return {
        "path": str(path),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
    }


def _verify_process_registry_lock(plan: dict, held: dict) -> dict:
    if not isinstance(held, dict):
        raise ReleaseBuildError("process registry lock receipt is invalid")
    kind = held.get("kind")
    handle = held.get("handle")
    receipt = held.get("receipt")
    path = _process_registry_lock_path(plan, kind=str(kind or ""))
    if handle is None or not isinstance(receipt, dict):
        raise ReleaseBuildError("process registry lock receipt is invalid")
    try:
        descriptor = handle.fileno()
        opened = os.fstat(descriptor)
        current = path.lstat()
    except (AttributeError, OSError) as exc:
        raise DrainIdentityMismatch(
            "process registry lock identity became unavailable"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or any(
            getattr(current, field) != getattr(opened, field)
            for field in _PROCESS_LOCK_IDENTITY_FIELDS
        )
    ):
        raise DrainIdentityMismatch("process registry lock identity changed")
    actual = _process_registry_lock_identity(path, opened)
    if actual != receipt:
        raise DrainIdentityMismatch("process registry lock receipt changed")
    return actual


def _acquire_process_registry_lock(plan: dict, *, kind: str) -> dict:
    path = _process_registry_lock_path(plan, kind=kind)
    parent = path.parent
    parent_state = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_state.st_mode)
        or parent.resolve(strict=True) != parent
        or parent_state.st_uid != os.getuid()
        or stat.S_IMODE(parent_state.st_mode) != 0o700
    ):
        raise ReleaseBuildError("process registry lock parent is unsafe")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ReleaseBuildError(
            "process registry lock cannot be opened without O_NOFOLLOW"
        )
    flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ReleaseBuildError("process registry lock is unreadable") from exc
    except OSError as exc:
        raise ReleaseBuildError("process registry lock cannot be created") from exc
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        os.set_inheritable(handle.fileno(), False)
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or any(
                getattr(current, field) != getattr(opened, field)
                for field in _PROCESS_LOCK_IDENTITY_FIELDS
            )
        ):
            raise ReleaseBuildError("process registry lock is unsafe")
        if created:
            os.fsync(handle.fileno())
            _fsync_directory(parent)
        deadline = time.monotonic() + float(plan["timeout_seconds"])
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DrainTimeout(
                        f"process registry {kind} lock acquisition timed out"
                    )
                time.sleep(float(plan["interval_seconds"]))
        locked = os.fstat(handle.fileno())
        current = path.lstat()
        if any(
            getattr(current, field) != getattr(locked, field)
            for field in _PROCESS_LOCK_IDENTITY_FIELDS
        ):
            raise DrainIdentityMismatch(
                "process registry lock changed while waiting"
            )
        held = {
            "kind": kind,
            "handle": handle,
            "receipt": _process_registry_lock_identity(path, locked),
        }
        _verify_process_registry_lock(plan, held)
        return held
    except Exception:
        handle.close()
        raise


def _release_process_registry_lock(plan: dict, held: dict) -> dict:
    receipt = _verify_process_registry_lock(plan, held)
    handle = held["handle"]
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    return {
        "status": "released",
        "kind": held["kind"],
        "identity": receipt,
    }


def _run_process_registry_retirement_barrier(
    plan: dict,
    *,
    stop_gateway: Callable[[], dict],
) -> dict:
    """Fence process spawn while allowing shutdown checkpoint finalization."""
    admission = _acquire_process_registry_lock(plan, kind="admission")
    admission_release: dict | None = None
    authority_receipts: list[dict] = []
    pre_stop: dict | None = None
    post_exit: dict | None = None
    stopped: dict | None = None
    try:
        authority = _acquire_process_registry_lock(plan, kind="authority")
        try:
            pre_stop = _legacy_process_checkpoint_receipt(plan)
            authority_receipts.append(copy.deepcopy(authority["receipt"]))
        finally:
            _release_process_registry_lock(plan, authority)

        stopped = stop_gateway()
        if not isinstance(stopped, dict):
            raise ReleaseBuildError("gateway stop receipt is invalid")

        authority = _acquire_process_registry_lock(plan, kind="authority")
        try:
            post_exit = _legacy_process_checkpoint_receipt(plan)
            authority_receipts.append(copy.deepcopy(authority["receipt"]))
        finally:
            _release_process_registry_lock(plan, authority)
    finally:
        admission_release = _release_process_registry_lock(plan, admission)
    if not all(
        isinstance(receipt, dict) and receipt.get("active_records") == 0
        for receipt in (pre_stop, post_exit)
    ):
        raise ReleaseBuildError("process retirement did not preserve zero work")
    return {
        "status": "retired-at-zero",
        "admission_lock": copy.deepcopy(admission["receipt"]),
        "authority_locks": authority_receipts,
        "pre_stop_checkpoint": pre_stop,
        "gateway_stop": stopped,
        "post_exit_checkpoint": post_exit,
        "admission_release": admission_release,
    }


def _legacy_process_checkpoint_receipt(plan: dict) -> dict:
    """Require the Agent process registry to be one exact private empty list."""
    checkpoint_path = _legacy_gateway_home(plan) / "processes.json"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ReleaseBuildError(
            "gateway process checkpoint cannot be read without O_NOFOLLOW"
        )
    flags = os.O_RDONLY | nofollow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(checkpoint_path, flags)
    except OSError as exc:
        raise ReleaseBuildError(
            "gateway process checkpoint is absent or unreadable"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size < 2
            or opened.st_size > 16 * 1024 * 1024
        ):
            raise ReleaseBuildError("gateway process checkpoint is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, 16 * 1024 * 1024 + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise ReleaseBuildError(
                    "gateway process checkpoint is too large"
                )
        payload = b"".join(chunks)
        try:
            finished = os.fstat(descriptor)
        except OSError as exc:
            raise DrainIdentityMismatch(
                "gateway process checkpoint identity became unavailable"
            ) from exc
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(finished, field) != getattr(opened, field)
            for field in stable_fields
        ):
            raise DrainIdentityMismatch(
                "gateway process checkpoint changed while reading"
            )
        try:
            current = checkpoint_path.lstat()
        except OSError as exc:
            raise DrainIdentityMismatch(
                "gateway process checkpoint disappeared while reading"
            ) from exc
        if any(
            getattr(current, field) != getattr(finished, field)
            for field in stable_fields
        ):
            raise DrainIdentityMismatch(
                "gateway process checkpoint changed while reading"
            )
    finally:
        os.close(descriptor)
    try:
        checkpoint = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError(
            "gateway process checkpoint JSON is invalid"
        ) from exc
    if not isinstance(checkpoint, list):
        raise ReleaseBuildError(
            "gateway process checkpoint schema is invalid"
        )
    if checkpoint:
        raise ReleaseBuildError(
            "gateway process checkpoint still has worker activity"
        )
    return {
        "status": "verified",
        "path": str(checkpoint_path),
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "active_records": 0,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
        "size": opened.st_size,
    }


def _attest_legacy_webui_activity_drain(
    plan: dict,
    identity: dict,
    inspection: dict,
    *,
    inspect_control: Callable[[], dict],
) -> dict | None:
    """Bridge one old signed activity schema with exact durable zero proof."""
    def exact_legacy_gap(candidate: object) -> bool:
        if not isinstance(candidate, dict):
            raise ReleaseBuildError(
                "legacy WebUI activity inspection is invalid"
            )
        if (
            candidate.get("status") != "inspected"
            or candidate.get("identity") != identity
        ):
            raise DrainIdentityMismatch(
                "legacy WebUI activity identity or schema changed"
            )
        schema, candidate_admission, candidate_activity = (
            _classify_release_activity_schema(candidate)
        )
        if schema == "native":
            return False
        if schema == "compatibility-gap" and any(
            identity.get(key) != expected
            for key, expected in _R90_PROCESS_COMPATIBILITY_IDENTITY
        ):
            raise DrainIdentityMismatch(
                "compatibility process activity gap is not exact live r90"
            )
        counts = {
            "active_runs": candidate_admission["active_runs"],
            "reservations": candidate_admission["reservations"],
            **{
                key: candidate_activity[key]
                for key in _RELEASE_ACTIVITY_COUNT_KEYS
            },
        }
        if any(value != 0 for value in counts.values()):
            return False
        return True

    if not _candidate_identity_matches(
        identity,
        plan["last_good_identity"],
    ):
        raise DrainIdentityMismatch(
            "legacy activity drain process is not the exact last-good build"
        )
    if not exact_legacy_gap(inspection):
        return None

    gateway_before = _attest_managed_gateway_binding(
        plan,
        plan["last_good_gateway_identity"],
        expected_admission="accepting_new_work",
        expected_pair_gate="absent",
    )
    admission_lock = _acquire_process_registry_lock(
        plan,
        kind="admission",
    )
    completion_lock: dict | None = None
    authority_lock: dict | None = None
    completion_release: dict | None = None
    authority_release: dict | None = None
    try:
        completion_lock = _acquire_process_registry_lock(
            plan,
            kind="completion",
        )
        try:
            authority_lock = _acquire_process_registry_lock(
                plan,
                kind="authority",
            )
            try:
                durable = _legacy_durable_activity_receipt(plan)
                outbox_receipt, outbox = _read_synthetic_store_receipt(
                    Path(plan["synthetic_process_notifications_path"]),
                    label="process completion outbox",
                    allowed_modes={0o600},
                )
                if (
                    not isinstance(outbox, dict)
                    or set(outbox) != {"version", "events"}
                    or outbox.get("version") != 1
                    or not isinstance(outbox.get("events"), dict)
                ):
                    raise ReleaseBuildError(
                        "process completion outbox schema is invalid"
                    )
                undelivered = 0
                for event_id, event in outbox["events"].items():
                    if (
                        not isinstance(event_id, str)
                        or not isinstance(event, dict)
                        or event.get("event_id") != event_id
                        or not isinstance(event.get("delivered"), bool)
                    ):
                        raise ReleaseBuildError(
                            "process completion outbox record is invalid"
                        )
                    undelivered += event["delivered"] is not True
                if undelivered != 0:
                    raise ReleaseBuildError(
                        "process completion outbox still has undelivered work"
                    )
            finally:
                authority_release = _release_process_registry_lock(
                    plan,
                    authority_lock,
                )
        finally:
            completion_release = _release_process_registry_lock(
                plan,
                completion_lock,
            )

        repeated = inspect_control()
        if not exact_legacy_gap(repeated):
            raise ReleaseBuildError(
                "legacy WebUI became busy during external activity proof"
            )
        gateway_after = _attest_managed_gateway_binding(
            plan,
            plan["last_good_gateway_identity"],
            expected_admission="accepting_new_work",
            expected_pair_gate="absent",
        )
        if any(
            gateway_before.get(key) != gateway_after.get(key)
            for key in ("listener_pid", "pid_start_token", "build_id")
        ):
            raise DrainIdentityMismatch(
                "managed gateway changed during legacy activity proof"
            )
    finally:
        admission_release = _release_process_registry_lock(
            plan,
            admission_lock,
        )

    return {
        "status": "verified",
        "identity": copy.deepcopy(identity),
        "proof": "exact-external-process-barrier",
        "activity": copy.deepcopy(inspection["activity"]),
        "durable_activity": durable,
        "outbox": {
            "receipt": outbox_receipt,
            "undelivered": undelivered,
        },
        "gateway": {
            "build_id": gateway_after["build_id"],
            "listener_pid": gateway_after["listener_pid"],
            "pid_start_token": gateway_after["pid_start_token"],
        },
        "locks": {
            "admission": copy.deepcopy(admission_lock["receipt"]),
            "completion": copy.deepcopy(completion_lock["receipt"]),
            "authority": copy.deepcopy(authority_lock["receipt"]),
            "authority_release": authority_release,
            "completion_release": completion_release,
            "admission_release": admission_release,
        },
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }


def _wait_for_legacy_process_checkpoint_empty(
    plan: dict,
    prepared: dict,
) -> dict:
    gateway = prepared.get("gateway") if isinstance(prepared, dict) else None
    try:
        expected_pid = int(gateway.get("pid"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseBuildError(
            "gateway process checkpoint owner is invalid"
        ) from exc
    expected_start = str(gateway.get("pid_start_token") or "")
    if expected_pid <= 1 or not expected_start:
        raise ReleaseBuildError(
            "gateway process checkpoint owner is invalid"
        )
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if (
                _listener_pid(int(plan["gateway_listener_port"])) != expected_pid
                or _job_pid(plan, gateway=True) != expected_pid
                or _pid_start_token(expected_pid) != expected_start
            ):
                raise DrainIdentityMismatch(
                    "gateway changed while waiting for process checkpoint"
                )
            receipt = _legacy_process_checkpoint_receipt(plan)
            if (
                _listener_pid(int(plan["gateway_listener_port"])) != expected_pid
                or _job_pid(plan, gateway=True) != expected_pid
                or _pid_start_token(expected_pid) != expected_start
            ):
                raise DrainIdentityMismatch(
                    "gateway changed after process checkpoint read"
                )
            return receipt
        except DrainIdentityMismatch:
            raise
        except ReleaseBuildError as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(
        f"gateway process checkpoint did not become empty naturally: {last_error}"
    )


def _regular_file_baseline(path: Path, *, label: str) -> dict:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "inode": None,
            "size": 0,
            "mtime_ns": None,
            "sha256": None,
        }
    opened = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
    ):
        raise ReleaseBuildError(f"{label} is unsafe")
    return {
        "path": str(path),
        "exists": True,
        "inode": opened.st_ino,
        "size": opened.st_size,
        "mtime_ns": opened.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def _legacy_gateway_log_baselines(plan: dict) -> list[dict]:
    plist = _read_plist(plan["gateway_rollback_plist"])
    paths: list[Path] = []
    for key in ("StandardOutPath", "StandardErrorPath"):
        raw = plist.get(key)
        if raw is None:
            continue
        path = Path(str(raw))
        if (
            not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            or path in paths
        ):
            raise ReleaseBuildError("legacy gateway log path is invalid")
        paths.append(path)
    if not paths:
        raise ReleaseBuildError("legacy gateway has no durable shutdown log")
    return [
        _regular_file_baseline(path, label="legacy gateway shutdown log")
        for path in paths
    ]


def _legacy_gateway_drain_marker_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / ".drain_request.json"


def _legacy_gateway_planned_stop_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / ".gateway-planned-stop.json"


def _legacy_gateway_clean_shutdown_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / ".clean_shutdown"


def _write_exact_private_json(path: Path, payload: dict, *, label: str) -> dict:
    if path.parent != _prepare_release_root(path.parent) or path.is_symlink():
        raise ReleaseBuildError(f"{label} path is unsafe")
    if path.exists():
        current = _read_private_json_value(path, label=label)
        if current != payload:
            raise ReleaseBuildError(f"{label} already has another identity")
    else:
        _atomic_write_transaction_journal(path, payload)
    if _read_private_json_value(path, label=label) != payload:
        raise ReleaseBuildError(f"{label} write did not persist")
    return _file_identity_receipt(path)


def _pair_open_gate_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / ".pair_open_gate.json"


def _pair_open_gate_intent_receipt(
    plan: dict,
    candidate_identity: dict,
    gateway_binding: dict,
    *,
    created_at: str | None = None,
) -> dict:
    """Build the one durable owner document used to hold both peers closed."""
    expected = plan.get("expected_candidate_identity")
    if not isinstance(expected, dict):
        raise ReleaseBuildError("pair-open gate candidate identity is missing")
    transaction_id = str(plan.get("transaction_id") or "")
    try:
        epoch = int(expected.get("selector_generation"))
        webui_pid = int(candidate_identity.get("pid"))
        gateway_pid = int(gateway_binding.get("listener_pid"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseBuildError("pair-open gate process identity is invalid") from exc
    webui_start = str(candidate_identity.get("pid_start_token") or "")
    gateway_start = str(gateway_binding.get("pid_start_token") or "")
    gateway_health = gateway_binding.get("health")
    release_identity = (
        gateway_health.get("release_identity")
        if isinstance(gateway_health, dict)
        else None
    )
    sealed_release = (
        release_identity.get("release")
        if isinstance(release_identity, dict)
        else None
    )
    release_pair_id = (
        str(sealed_release.get("release_pair_id") or "")
        if isinstance(sealed_release, dict)
        else ""
    )
    expected_pair_id = release_selector.release_pair_id(
        expected,
        selector_generation=epoch,
        transaction_id=transaction_id,
    )
    if (
        not _TRANSACTION_ID.fullmatch(transaction_id)
        or epoch <= 0
        or candidate_identity.get("build_id") != expected.get("build_id")
        or webui_pid <= 1
        or gateway_pid <= 1
        or not webui_start
        or not gateway_start
        or release_pair_id != expected_pair_id
    ):
        raise ReleaseBuildError("pair-open gate identity is not exact")
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ReleaseBuildError("pair-open gate timestamp is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc).isoformat() != timestamp
    ):
        raise ReleaseBuildError("pair-open gate timestamp is not canonical UTC")
    owner_payload = {
        "schema": "hermes.pair_open_gate.v1",
        "action": "hold_pair_open",
        "transaction_id": transaction_id,
        "created_at": timestamp,
        "epoch": epoch,
        "agent": {
            "build_id": str(expected.get("agent_source_manifest_sha256") or ""),
            "pid": gateway_pid,
            "start_time": gateway_start,
            "instance_epoch": release_pair_id,
        },
        "webui": {
            "build_id": str(expected.get("build_id") or ""),
            "pid": webui_pid,
            "start_time": webui_start,
            "instance_epoch": str(epoch),
        },
    }
    if any(
        not str(identity.get(key) or "").strip()
        for identity in (owner_payload["agent"], owner_payload["webui"])
        for key in ("build_id", "start_time", "instance_epoch")
    ):
        raise ReleaseBuildError("pair-open gate build identity is incomplete")
    canonical_owner = json.dumps(
        owner_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    payload = {
        **owner_payload,
        "owner_hash": hashlib.sha256(canonical_owner).hexdigest(),
    }
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return {
        "status": "prepared",
        "path": str(_pair_open_gate_path(plan)),
        "payload": payload,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _install_or_adopt_pair_open_gate(plan: dict, intent: dict) -> dict:
    path = _pair_open_gate_path(plan)
    payload = intent.get("payload") if isinstance(intent, dict) else None
    if (
        not isinstance(payload, dict)
        or intent.get("path") != str(path)
        or payload.get("transaction_id") != plan.get("transaction_id")
    ):
        raise ReleaseBuildError("pair-open gate install intent is invalid")
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    if payload_sha256 != intent.get("payload_sha256"):
        raise ReleaseBuildError("pair-open gate install intent hash changed")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _with_transaction_journal_lock(lock_path):
        existed = path.exists() or path.is_symlink()
        receipt = _write_exact_private_json(
            path,
            payload,
            label="pair-open gate",
        )
        if receipt.get("sha256") != payload_sha256:
            raise ReleaseBuildError("pair-open gate bytes changed during install")
        opened = path.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ReleaseBuildError("pair-open gate install is unsafe")
    return {
        "status": "adopted" if existed else "installed",
        "path": str(path),
        "owner_hash": payload["owner_hash"],
        "payload_sha256": payload_sha256,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "mode": stat.S_IMODE(opened.st_mode),
    }


def _release_owned_pair_open_gate(
    plan: dict,
    intent: dict,
    installed: dict,
) -> dict:
    path = _pair_open_gate_path(plan)
    payload = intent.get("payload") if isinstance(intent, dict) else None
    if (
        not isinstance(payload, dict)
        or intent.get("path") != str(path)
        or installed.get("owner_hash") != payload.get("owner_hash")
        or installed.get("payload_sha256") != intent.get("payload_sha256")
    ):
        raise ReleaseBuildError("pair-open gate release ownership is invalid")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _with_transaction_journal_lock(lock_path):
        if not path.exists() and not path.is_symlink():
            status = "already-released"
        else:
            if _read_private_json_value(path, label="pair-open gate") != payload:
                raise ReleaseBuildError("pair-open gate has another owner")
            if sha256_file(path) != intent.get("payload_sha256"):
                raise ReleaseBuildError("pair-open gate owner bytes changed")
            path.unlink()
            _fsync_directory(path.parent)
            if path.exists() or path.is_symlink():
                raise ReleaseBuildError("pair-open gate survived atomic release")
            status = "released"
    return {
        "status": status,
        "path": str(path),
        "owner_hash": payload["owner_hash"],
        "payload_sha256": intent["payload_sha256"],
    }


def _pair_open_gate_release_state(
    plan: dict,
    intent: dict,
    installed: dict,
) -> str:
    """Return active or released for the exact durable release owner."""
    path = _pair_open_gate_path(plan)
    payload = intent.get("payload") if isinstance(intent, dict) else None
    if (
        not isinstance(payload, dict)
        or intent.get("path") != str(path)
        or installed.get("owner_hash") != payload.get("owner_hash")
        or installed.get("payload_sha256") != intent.get("payload_sha256")
    ):
        raise ReleaseBuildError("pair-open gate release ownership is invalid")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _with_transaction_journal_lock(lock_path):
        if not path.exists() and not path.is_symlink():
            return "released"
        if _read_private_json_value(path, label="pair-open gate") != payload:
            raise ReleaseBuildError("pair-open gate has another owner")
        if sha256_file(path) != intent.get("payload_sha256"):
            raise ReleaseBuildError("pair-open gate owner bytes changed")
    return "active"


def _require_webui_pair_gate_state(
    binding: dict,
    intent: dict,
    *,
    active: bool,
    allow_normalized_restart: bool = False,
) -> dict:
    deep = binding.get("deep_health") if isinstance(binding, dict) else None
    admission = deep.get("admission") if isinstance(deep, dict) else None
    gate = admission.get("pair_gate") if isinstance(admission, dict) else None
    payload = intent.get("payload") if isinstance(intent, dict) else None
    if not isinstance(admission, dict) or not isinstance(gate, dict):
        raise ReleaseBuildError("WebUI pair-open admission receipt is missing")
    if active:
        normalized_restart_gate = {
            "status": "invalid",
            "transaction_id": None,
            "epoch": None,
            "owner_hash": None,
            "payload_sha256": None,
            "agent": None,
            "webui": None,
        }
        if (
            allow_normalized_restart
            and admission.get("state") == "open"
            and admission.get("effective_state") == "pair-gated"
            and gate == normalized_restart_gate
        ):
            return {
                "status": "adopted-normalized-restart",
                "effective_state": "pair-gated",
                "pair_gate": copy.deepcopy(gate),
            }
        if (
            admission.get("state") != "open"
            or admission.get("effective_state") != "pair-gated"
            or gate.get("status") != "active"
            or not isinstance(payload, dict)
            or gate.get("transaction_id") != payload.get("transaction_id")
            or gate.get("epoch") != payload.get("epoch")
            or gate.get("owner_hash") != payload.get("owner_hash")
            or gate.get("payload_sha256") != intent.get("payload_sha256")
            or gate.get("agent") != payload.get("agent")
            or gate.get("webui") != payload.get("webui")
        ):
            raise DrainIdentityMismatch(
                "WebUI did not remain closed on the exact shared pair gate: "
                f"state={admission.get('state')!r}, "
                f"effective_state={admission.get('effective_state')!r}, "
                f"gate={gate!r}, "
                f"allow_normalized_restart={allow_normalized_restart!r}"
            )
    elif (
        admission.get("state") != "open"
        or admission.get("effective_state") != "open"
        or gate
        != {
            "status": "absent",
            "transaction_id": None,
            "epoch": None,
            "owner_hash": None,
            "payload_sha256": None,
            "agent": None,
            "webui": None,
        }
    ):
        raise DrainIdentityMismatch("WebUI pair-open gate did not release exactly")
    return {
        "status": "verified",
        "effective_state": admission["effective_state"],
        "pair_gate": copy.deepcopy(gate),
    }


def _expected_agent_pair_gate_receipt(intent: dict, *, active: bool) -> dict:
    if not active:
        return {
            "active": False,
            "verified": True,
            "reason": "absent",
            "structure_verified": True,
            "local_identity_matches": None,
        }
    payload = intent.get("payload") if isinstance(intent, dict) else None
    if not isinstance(payload, dict):
        raise ReleaseBuildError("Agent pair-open gate expectation is invalid")
    return {
        "active": True,
        "verified": True,
        "reason": "verified",
        "schema": payload["schema"],
        "transaction_id": payload["transaction_id"],
        "owner_hash": payload["owner_hash"],
        "epoch": payload["epoch"],
        "agent": copy.deepcopy(payload["agent"]),
        "webui": copy.deepcopy(payload["webui"]),
        "payload_sha256": intent["payload_sha256"],
        "structure_verified": True,
        "local_identity_matches": True,
    }


def _legacy_gateway_drain_intent_receipt(plan: dict, prepared: dict) -> dict:
    gateway = prepared["gateway"]
    if (
        _listener_pid(int(plan["gateway_listener_port"])) != int(gateway["pid"])
        or _pid_start_token(int(gateway["pid"])) != gateway["pid_start_token"]
    ):
        raise DrainIdentityMismatch(
            "legacy gateway changed before durable drain intent"
        )
    status_path = _legacy_gateway_home(plan) / "gateway_state.json"
    _status, status_baseline = _read_legacy_gateway_status(
        status_path,
        label="legacy gateway status",
    )
    marker_payload = {
        "action": "drain",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "principal": "webui-release-cutover",
        "suppress_notification": True,
        "release_transaction_id": plan["transaction_id"],
    }
    marker_bytes = (
        json.dumps(
            marker_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return {
        "status": "prepared",
        "gateway": {
            "pid": int(gateway["pid"]),
            "pid_start_token": str(gateway["pid_start_token"]),
        },
        "status_baseline": status_baseline,
        "marker": {
            "path": str(_legacy_gateway_drain_marker_path(plan)),
            "payload": marker_payload,
            "sha256": hashlib.sha256(marker_bytes).hexdigest(),
        },
    }


def _prepared_legacy_gateway_drain_intent(
    plan: dict,
    prepared: dict,
) -> dict:
    if _watchdog_scheduler_backend(plan) != "hermes_internal":
        return _legacy_gateway_drain_intent_receipt(plan, prepared)
    cron = prepared.get("watchdog_cron") if isinstance(prepared, dict) else None
    intent = cron.get("drain_intent") if isinstance(cron, dict) else None
    marker = intent.get("marker") if isinstance(intent, dict) else None
    if (
        not isinstance(intent, dict)
        or not isinstance(marker, dict)
        or marker.get("path") != str(_legacy_gateway_drain_marker_path(plan))
        or not isinstance(marker.get("payload"), dict)
        or marker["payload"].get("release_transaction_id")
        != plan["transaction_id"]
        or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("sha256") or ""))
    ):
        raise ReleaseBuildError(
            "prepared internal watchdog gateway drain intent is invalid"
        )
    return copy.deepcopy(intent)


def _write_legacy_gateway_drain_marker(plan: dict, intent: dict) -> dict:
    marker = intent.get("marker") if isinstance(intent, dict) else None
    if (
        not isinstance(marker, dict)
        or marker.get("path") != str(_legacy_gateway_drain_marker_path(plan))
        or not isinstance(marker.get("payload"), dict)
    ):
        raise ReleaseBuildError("legacy gateway drain intent is invalid")
    receipt = _write_exact_private_json(
        Path(marker["path"]),
        marker["payload"],
        label="legacy gateway drain marker",
    )
    if receipt["sha256"] != marker.get("sha256"):
        raise ReleaseBuildError("legacy gateway drain marker hash changed")
    return receipt


def _legacy_gateway_health_with_drain(plan: dict) -> dict:
    public = _http_json(
        str(plan["gateway_health_url"]),
        timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if isinstance(public.get("drain"), dict):
        return public
    gateway_url = urlsplit(str(plan["gateway_health_url"]))
    detailed_url = gateway_url._replace(path="/health/detailed").geturl()
    plist = _read_plist(plan["gateway_rollback_plist"])
    environment = plist.get("EnvironmentVariables")
    api_key = (
        str(environment.get("API_SERVER_KEY") or "")
        if isinstance(environment, dict)
        else ""
    )
    if not api_key:
        return public
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return _http_json(
        Request(detailed_url, headers=headers, method="GET"),
        timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )


def _wait_for_legacy_gateway_drain(
    plan: dict,
    prepared: dict,
    intent: dict,
) -> dict:
    marker_receipt = _write_legacy_gateway_drain_marker(plan, intent)
    status_baseline = intent.get("status_baseline")
    if not isinstance(status_baseline, dict):
        raise ReleaseBuildError("legacy gateway status baseline is missing")
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    status_path = _legacy_gateway_home(plan) / "gateway_state.json"
    expected_pid = int(prepared["gateway"]["pid"])
    expected_start = str(prepared["gateway"]["pid_start_token"])
    while time.monotonic() < deadline:
        try:
            if (
                _listener_pid(int(plan["gateway_listener_port"])) != expected_pid
                or _pid_start_token(expected_pid) != expected_start
            ):
                raise DrainIdentityMismatch(
                    "legacy gateway changed while acknowledging drain"
                )
            status, status_receipt = _read_legacy_gateway_status(
                status_path,
                label="legacy gateway status",
            )
            baseline_mtime = status_baseline.get("mtime_ns")
            if (
                baseline_mtime is not None
                and int(status_receipt["mtime_ns"]) <= int(baseline_mtime)
            ):
                raise ReleaseBuildError(
                    "legacy gateway has not written a post-marker status"
                )
            if (
                status.get("kind") != "hermes-gateway"
                or int(status.get("pid", -1)) != expected_pid
                or status.get("gateway_state") != "draining"
                or int(status.get("active_agents", -1)) != 0
            ):
                raise ReleaseBuildError(
                    "legacy gateway status did not acknowledge zero-work drain"
                )
            health = _legacy_gateway_health_with_drain(plan)
            drain = health.get("drain")
            if isinstance(drain, dict):
                health_mode = "structured-drain"
                admission = drain.get("admission")
                work = drain.get("work")
                work_status = drain.get("work_status")
                quiescence = drain.get("quiescence")
                if (
                    not isinstance(admission, dict)
                    or admission.get("state") != "rejecting_new_work"
                    or admission.get("verified") is not True
                    or admission.get("drain_requested") is not True
                    or not isinstance(work, dict)
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value != 0
                        for value in work.values()
                    )
                    or (
                        isinstance(work_status, dict)
                        and any(
                            value != "verified"
                            for value in work_status.values()
                        )
                    )
                    or not isinstance(quiescence, dict)
                    or quiescence.get("verified") is not True
                    or quiescence.get("quiescent") is not True
                    or quiescence.get("blockers") != []
                ):
                    raise ReleaseBuildError(
                        "legacy gateway drain receipt is incomplete"
                    )
            else:
                readiness = health.get("readiness")
                checks = (
                    readiness.get("checks")
                    if isinstance(readiness, dict)
                    else None
                )
                queues = (
                    checks.get("background_queues")
                    if isinstance(checks, dict)
                    else None
                )
                work = (
                    {
                        "active_api_runs": queues.get("active_api_runs"),
                        "process_completion_queue_depth": queues.get(
                            "process_completions"
                        ),
                        "active_delegations": queues.get(
                            "active_delegations"
                        ),
                    }
                    if isinstance(queues, dict)
                    else None
                )
                if isinstance(readiness, dict):
                    health_mode = "detailed-readiness"
                    if (
                        health.get("gateway_state") != "draining"
                        or int(health.get("active_agents", -1)) != 0
                        or readiness.get("status") not in {"ok", "degraded"}
                        or not isinstance(queues, dict)
                        or queues.get("status") != "ok"
                        or not isinstance(work, dict)
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, int)
                            or value != 0
                            for value in work.values()
                        )
                    ):
                        raise ReleaseBuildError(
                            "legacy gateway detailed readiness is not zero-work"
                        )
                elif (
                    health.get("status") == "ok"
                    and health.get("platform") == "hermes-agent"
                    and isinstance(health.get("version"), str)
                    and health.get("version")
                ):
                    health_mode = "legacy-status-file"
                    work = {"active_agents": 0}
                else:
                    raise ReleaseBuildError(
                        "legacy gateway public health is not usable"
                    )
            health_pid = health.get("pid")
            release = health.get("release_identity")
            if health_pid is None and isinstance(release, dict):
                process = release.get("process")
                health_pid = process.get("pid") if isinstance(process, dict) else None
            if health_pid is not None and int(health_pid) != expected_pid:
                raise DrainIdentityMismatch(
                    "legacy gateway health PID does not own the listener"
                )
            checkpoint = _wait_for_legacy_process_checkpoint_empty(
                plan,
                prepared,
            )
            return {
                "status": "verified",
                "gateway": {
                    "pid": expected_pid,
                    "pid_start_token": expected_start,
                },
                "marker": marker_receipt,
                "runtime_status": {
                    "sha256": status_receipt["sha256"],
                    "mtime_ns": status_receipt["mtime_ns"],
                    "gateway_state": "draining",
                    "active_agents": 0,
                },
                "health_sha256": hashlib.sha256(
                    json.dumps(
                        health,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "health_mode": health_mode,
                "work": copy.deepcopy(work),
                "process_checkpoint": checkpoint,
            }
        except Exception as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(f"legacy gateway drain acknowledgement timed out: {last_error}")


def _legacy_gateway_stop_intent_receipt(
    plan: dict,
    prepared: dict,
    drain_receipt: dict,
) -> dict:
    gateway = prepared["gateway"]
    if (
        _listener_pid(int(plan["gateway_listener_port"])) != int(gateway["pid"])
        or _pid_start_token(int(gateway["pid"])) != gateway["pid_start_token"]
    ):
        raise DrainIdentityMismatch(
            "legacy gateway changed before graceful stop intent"
        )
    checkpoint = _legacy_process_checkpoint_receipt(plan)
    status_path = _legacy_gateway_home(plan) / "gateway_state.json"
    status, status_receipt = _read_legacy_gateway_status(
        status_path,
        label="legacy gateway status",
    )
    restart_control = _launchd_service_override_receipt(
        plan,
        gateway=True,
    )
    if restart_control["disabled"]:
        raise ReleaseBuildError(
            "legacy gateway launchd service is already disabled"
        )
    payload = {
        "target_pid": int(gateway["pid"]),
        "target_start_time": status.get("start_time"),
        "stopper_pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "release_transaction_id": plan["transaction_id"],
    }
    return {
        "status": "prepared",
        "gateway": {
            "pid": int(gateway["pid"]),
            "pid_start_token": str(gateway["pid_start_token"]),
        },
        "drain_receipt_sha256": hashlib.sha256(
            json.dumps(
                drain_receipt,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "planned_stop": {
            "path": str(_legacy_gateway_planned_stop_path(plan)),
            "payload": payload,
        },
        "launchd_restart_control": {
            "status": "prepared",
            "initial": restart_control,
            "restore_semantics": "enabled",
        },
        "clean_shutdown_baseline": _regular_file_baseline(
            _legacy_gateway_clean_shutdown_path(plan),
            label="legacy gateway clean-shutdown marker",
        ),
        "status_baseline": status_receipt,
        "logs": _legacy_gateway_log_baselines(plan),
        "process_checkpoint": checkpoint,
    }


def _read_gateway_log_delta(baseline: dict) -> tuple[str, dict]:
    path = Path(str(baseline.get("path") or ""))
    current = _regular_file_baseline(path, label="legacy gateway shutdown log")
    if (
        not current["exists"]
        or not baseline.get("exists")
        or current["inode"] != baseline.get("inode")
        or int(current["size"]) < int(baseline.get("size", 0))
    ):
        raise ReleaseBuildError("legacy gateway shutdown log changed identity")
    delta_size = int(current["size"]) - int(baseline["size"])
    if delta_size > 4 * 1024 * 1024:
        raise ReleaseBuildError("legacy gateway shutdown receipt is too large")
    with path.open("rb") as handle:
        handle.seek(int(baseline["size"]))
        payload = handle.read(delta_size + 1)
    if len(payload) != delta_size:
        raise ReleaseBuildError("legacy gateway shutdown log read was unstable")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseBuildError("legacy gateway shutdown log is not UTF-8") from exc
    return text, {
        "path": str(path),
        "delta_size": delta_size,
        "delta_sha256": hashlib.sha256(payload).hexdigest(),
        "final_size": current["size"],
        "final_mtime_ns": current["mtime_ns"],
    }


def _legacy_dispatcher_lock_path(plan: dict) -> Path:
    return _legacy_gateway_home(plan) / "kanban" / ".dispatcher.lock"


def _acquire_legacy_dispatcher_lock(plan: dict) -> dict:
    transaction_id = str(plan["transaction_id"])
    existing = _LEGACY_DISPATCHER_LOCKS.get(transaction_id)
    path = _legacy_dispatcher_lock_path(plan)
    if existing is not None and not getattr(existing, "closed", True):
        opened = os.fstat(existing.fileno())
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise DrainIdentityMismatch(
                "legacy dispatcher lock path disappeared"
            ) from exc
        if (
            path.is_symlink()
            or path_stat.st_dev != opened.st_dev
            or path_stat.st_ino != opened.st_ino
        ):
            raise DrainIdentityMismatch(
                "legacy dispatcher lock path no longer names the held inode"
            )
        return {
            "status": "held",
            "path": str(path),
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "uid": opened.st_uid,
            "nlink": opened.st_nlink,
            "mode": stat.S_IMODE(opened.st_mode),
        }
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700)
        _fsync_directory(parent.parent)
    if (
        parent.is_symlink()
        or parent.resolve(strict=True) != parent
        or parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(parent.stat().st_mode) & 0o022
        or path.is_symlink()
    ):
        raise ReleaseBuildError("legacy dispatcher lock path is unsafe")
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
    ):
        os.close(descriptor)
        raise ReleaseBuildError("legacy dispatcher lock is unsafe")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise DrainTimeout(
                    "legacy Kanban dispatcher lock did not become exclusive"
                )
            time.sleep(float(plan["interval_seconds"]))
    _LEGACY_DISPATCHER_LOCKS[transaction_id] = handle
    return {
        "status": "held",
        "path": str(path),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "nlink": opened.st_nlink,
        "mode": stat.S_IMODE(opened.st_mode),
    }


def _verify_legacy_dispatcher_lock(plan: dict, receipt: dict) -> dict:
    current = _acquire_legacy_dispatcher_lock(plan)
    path = _legacy_dispatcher_lock_path(plan)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise DrainIdentityMismatch(
            "legacy dispatcher lock path disappeared"
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_dev != current.get("device")
        or path_stat.st_ino != current.get("inode")
        or path_stat.st_uid != current.get("uid")
        or path_stat.st_nlink != current.get("nlink")
        or stat.S_IMODE(path_stat.st_mode) != current.get("mode")
    ):
        raise DrainIdentityMismatch(
            "legacy dispatcher lock path no longer names the held inode"
        )
    if any(
        current.get(key) != receipt.get(key)
        for key in (
            "status",
            "path",
            "device",
            "inode",
            "uid",
            "nlink",
            "mode",
        )
    ):
        raise DrainIdentityMismatch("legacy dispatcher lock ownership changed")
    return current


def _release_legacy_dispatcher_lock(plan: dict) -> dict:
    transaction_id = str(plan["transaction_id"])
    handle = _LEGACY_DISPATCHER_LOCKS.get(transaction_id)
    if handle is None or getattr(handle, "closed", True):
        return {
            "status": "already-released",
            "path": str(_legacy_dispatcher_lock_path(plan)),
        }
    _verify_legacy_dispatcher_lock(
        plan,
        {
            "status": "held",
            "path": str(_legacy_dispatcher_lock_path(plan)),
            "device": os.fstat(handle.fileno()).st_dev,
            "inode": os.fstat(handle.fileno()).st_ino,
            "uid": os.fstat(handle.fileno()).st_uid,
            "nlink": os.fstat(handle.fileno()).st_nlink,
            "mode": stat.S_IMODE(os.fstat(handle.fileno()).st_mode),
        },
    )
    _LEGACY_DISPATCHER_LOCKS.pop(transaction_id, None)
    opened = os.fstat(handle.fileno())
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    return {
        "status": "released",
        "path": str(_legacy_dispatcher_lock_path(plan)),
        "inode": opened.st_ino,
    }


def _legacy_kanban_worker_receipt(plan: dict) -> dict:
    home = _legacy_gateway_home(plan)
    candidates = {home / "kanban.db"}
    candidates.update((home / "kanban" / "boards").glob("*/kanban.db"))
    candidates.update(
        (home / "kanban" / "boards" / "_archived").glob("*/kanban.db")
    )
    candidates.update((home / "profiles").glob("*/kanban.db"))
    databases: list[dict] = []
    active_rows: list[dict] = []
    for path in sorted(candidates):
        if not path.exists():
            continue
        opened = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or home not in path.resolve(strict=True).parents
        ):
            raise ReleaseBuildError("legacy Kanban database is unsafe")
        try:
            connection = sqlite3.connect(
                f"file:{path}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 1000")
            task_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            task_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            required_task_columns = {
                "id",
                "status",
                "claim_lock",
                "worker_pid",
                "current_run_id",
            }
            if task_table is not None and not required_task_columns.issubset(
                task_columns
            ):
                raise ReleaseBuildError(
                    "legacy Kanban task schema cannot prove worker ownership"
                )
            task_rows = (
                connection.execute(
                    "SELECT id, status, worker_pid, claim_lock, current_run_id "
                    "FROM tasks WHERE status = 'running' "
                    "OR worker_pid IS NOT NULL OR claim_lock IS NOT NULL "
                    "OR current_run_id IS NOT NULL"
                ).fetchall()
                if task_table is not None
                else []
            )
            run_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='task_runs'"
            ).fetchone()
            run_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(task_runs)"
                ).fetchall()
            }
            required_run_columns = {
                "id",
                "task_id",
                "status",
                "claim_lock",
                "worker_pid",
                "ended_at",
            }
            if run_table is not None and not required_run_columns.issubset(
                run_columns
            ):
                raise ReleaseBuildError(
                    "legacy Kanban run schema cannot prove worker ownership"
                )
            run_rows = (
                connection.execute(
                    "SELECT id, task_id, status, worker_pid, claim_lock, ended_at "
                    "FROM task_runs WHERE status = 'running' OR ended_at IS NULL "
                    "OR worker_pid IS NOT NULL OR claim_lock IS NOT NULL"
                ).fetchall()
                if run_table is not None
                else []
            )
        except sqlite3.Error as exc:
            raise ReleaseBuildError(
                "legacy Kanban worker state is unreadable"
            ) from exc
        finally:
            try:
                connection.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
        databases.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "active_task_rows": len(task_rows),
                "active_run_rows": len(run_rows),
            }
        )
        active_rows.extend(
            {
                "database": str(path),
                "record_kind": "task",
                "task_id_sha256": hashlib.sha256(
                    str(row["id"]).encode()
                ).hexdigest(),
                "status": str(row["status"]),
                "worker_pid": row["worker_pid"],
            }
            for row in task_rows
        )
        active_rows.extend(
            {
                "database": str(path),
                "record_kind": "task_run",
                "task_id_sha256": hashlib.sha256(
                    str(row["task_id"]).encode()
                ).hexdigest(),
                "status": str(row["status"]),
                "worker_pid": row["worker_pid"],
            }
            for row in run_rows
        )
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseBuildError("legacy Kanban process proof is unavailable") from exc
    worker_pids: list[int] = []
    dispatcher_pids: list[int] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        command = fields[1].lower()
        if "work kanban task " in command:
            worker_pids.append(pid)
        if re.search(r"\bkanban\s+(?:dispatch|dispatcher)\b", command):
            dispatcher_pids.append(pid)
    if active_rows or worker_pids or dispatcher_pids:
        raise ReleaseBuildError(
            "legacy Kanban workers or dispatchers survived gateway drain"
        )
    return {
        "status": "verified",
        "databases": databases,
        "active_task_rows": 0,
        "active_run_rows": 0,
        "worker_pids": [],
        "dispatcher_pids": [],
    }


def _wait_for_legacy_kanban_quiescence(plan: dict) -> dict:
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _legacy_kanban_worker_receipt(plan)
        except ReleaseBuildError as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(f"legacy Kanban workers did not finish naturally: {last_error}")


def _parse_legacy_gateway_shutdown_log(combined: str) -> dict:
    match = re.search(
        r"Shutdown phase: drain done "
        r"(?:\(|at \+[0-9]+(?:\.[0-9]+)?s "
        r"\(drain took [0-9]+(?:\.[0-9]+)?s, )"
        r"timed_out=(True|False), "
        r"active_at_start=([0-9]+), active_now=([0-9]+), "
        r"cron_at_start=([0-9]+), cron_now=([0-9]+)\)",
        combined,
    )
    if match is None:
        raise ReleaseBuildError(
            "legacy gateway shutdown log does not prove a zero-work clean stop"
        )
    receipt = {
        "timed_out": match.group(1) == "True",
        "active_at_start": int(match.group(2)),
        "active_now": int(match.group(3)),
        "cron_at_start": int(match.group(4)),
        "cron_now": int(match.group(5)),
    }
    if (
        receipt["timed_out"]
        or receipt["active_now"] != 0
        or receipt["cron_now"] != 0
        or "API server stopped" not in combined
        or "Gateway stopped" not in combined
        or re.search(
            r"Gateway drain timed out|Skipping \.clean_shutdown marker",
            combined,
        )
    ):
        raise ReleaseBuildError(
            "legacy gateway shutdown log does not prove a zero-work clean stop"
        )
    return receipt


def _legacy_gateway_terminal_status_receipt(
    status: dict,
    status_receipt: dict,
    *,
    status_baseline: dict,
    gateway_pid: int,
    shutdown_log: str,
) -> dict:
    try:
        fresh = (
            int(status_receipt.get("mtime_ns") or 0)
            > int(status_baseline.get("mtime_ns") or 0)
        )
        observed_pid = int(status.get("pid", -1))
        active_agents = int(status.get("active_agents", -1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseBuildError(
            "legacy gateway terminal status is not a fresh clean stop"
        ) from exc
    gateway_state = status.get("gateway_state")
    compatibility = "terminal-stopped"
    if gateway_state == "running":
        planned_stop_seen = re.search(
            r"Received (?:UNKNOWN|SIGTERM|SIGINT) as a planned gateway stop "
            r"— exiting cleanly",
            shutdown_log,
        )
        run_intent_offset = shutdown_log.find(
            "Gateway stopped by an unexpected signal — persisting "
            "gateway_state=running"
        )
        if (
            planned_stop_seen is None
            or run_intent_offset < 0
            or planned_stop_seen.start() >= run_intent_offset
        ):
            raise ReleaseBuildError(
                "legacy gateway terminal status is not a fresh clean stop"
            )
        compatibility = "planned-stop-double-signal-run-intent"
    elif gateway_state != "stopped":
        raise ReleaseBuildError(
            "legacy gateway terminal status is not a fresh clean stop"
        )
    if (
        not isinstance(status, dict)
        or not isinstance(status_receipt, dict)
        or not isinstance(status_baseline, dict)
        or not fresh
        or status.get("kind") != "hermes-gateway"
        or observed_pid != int(gateway_pid)
        or active_agents != 0
    ):
        raise ReleaseBuildError(
            "legacy gateway terminal status is not a fresh clean stop"
        )
    return {
        "sha256": status_receipt["sha256"],
        "mtime_ns": status_receipt["mtime_ns"],
        "gateway_state": gateway_state,
        "active_agents": 0,
        "compatibility": compatibility,
    }


def _bootout_exact_frozen_legacy_gateway(
    plan: dict,
    gateway_identity: dict,
    *,
    prepare_stop: Callable[[], dict],
) -> dict:
    pid = int(gateway_identity["pid"])
    if not _exact_process_is_alive(gateway_identity):
        raise DrainIdentityMismatch(
            "legacy gateway changed before frozen launchd bootout"
        )
    os.kill(pid, signal.SIGSTOP)
    frozen = True
    try:
        deadline = time.monotonic() + min(
            5.0,
            float(plan["timeout_seconds"]),
        )
        while time.monotonic() < deadline:
            if not _exact_process_is_alive(gateway_identity):
                raise DrainIdentityMismatch(
                    "legacy gateway changed while entering STOP barrier"
                )
            if "T" in _ps_value(pid, "state").upper():
                break
            time.sleep(float(plan["interval_seconds"]))
        else:
            raise DrainTimeout("legacy gateway did not enter STOP barrier")

        prepared_stop = prepare_stop()
        if (
            not _exact_process_is_alive(gateway_identity)
            or _job_pid(plan, gateway=True) != pid
            or "T" not in _ps_value(pid, "state").upper()
        ):
            raise DrainIdentityMismatch(
                "legacy gateway changed before frozen launchd bootout"
            )
        bootout = _bootout_job(plan, gateway=True, required=True)
        remaining_job = _job_pid(plan, gateway=True)
        if remaining_job is None:
            retirement = "verified-absent"
        elif (
            remaining_job == pid
            and _exact_process_is_alive(gateway_identity)
            and "T" in _ps_value(pid, "state").upper()
        ):
            retirement = "pending-exact-frozen-root"
        else:
            raise DrainIdentityMismatch(
                "legacy gateway launchd job changed during frozen bootout"
            )

        if not _exact_process_is_alive(gateway_identity):
            raise DrainIdentityMismatch(
                "legacy gateway changed before SIGCONT"
            )
        os.kill(pid, signal.SIGCONT)
        deadline = time.monotonic() + min(
            5.0,
            float(plan["timeout_seconds"]),
        )
        while time.monotonic() < deadline:
            if not _exact_process_is_alive(gateway_identity):
                frozen = False
                break
            if "T" not in _ps_value(pid, "state").upper():
                frozen = False
                break
            time.sleep(float(plan["interval_seconds"]))
        else:
            raise DrainTimeout(
                "legacy gateway did not leave STOP barrier after bootout"
            )
        wait_for_exact_process_exit(
            gateway_identity,
            float(plan["timeout_seconds"]),
            allow_exact_signaled_zombie=True,
        )
    except Exception:
        if frozen and _exact_process_is_alive(gateway_identity):
            os.kill(pid, signal.SIGCONT)
        raise
    return {
        "status": "stopped",
        "gateway": copy.deepcopy(gateway_identity),
        "prepare_stop": prepared_stop,
        "freeze": {
            "pid": pid,
            "pid_start_token": gateway_identity["pid_start_token"],
            "status": "resumed-for-single-signal-shutdown",
        },
        "bootout": {
            **bootout,
            "retirement": retirement,
        },
        "exact_exit_confirmed": True,
    }


def _retire_exact_legacy_gateway(
    plan: dict,
    gateway_identity: dict,
    intent: dict,
    *,
    prepare_stop: Callable[[], dict],
) -> dict:
    restart_control = (
        intent.get("launchd_restart_control")
        if isinstance(intent, dict)
        else None
    )
    clean_baseline = (
        intent.get("clean_shutdown_baseline")
        if isinstance(intent, dict)
        else None
    )
    if not isinstance(restart_control, dict) or not isinstance(
        clean_baseline,
        dict,
    ):
        raise ReleaseBuildError(
            "legacy gateway restart-control intent is invalid"
        )
    disabled = _set_launchd_service_disabled(
        plan,
        restart_control,
        disabled=True,
    )
    signal_status = "already-cleanly-exited"
    prepared_stop: dict | None = None
    bootout: dict | None = None
    clean: dict | None = None
    try:
        pid = int(gateway_identity["pid"])
        expected_start = str(gateway_identity["pid_start_token"])
        if _exact_process_is_alive(gateway_identity):
            if (
                _job_pid(plan, gateway=True) != pid
                or _listener_pid(int(plan["gateway_listener_port"])) != pid
                or _pid_start_token(pid) != expected_start
            ):
                raise DrainIdentityMismatch(
                    "gateway owner changed immediately before graceful stop"
                )
            prepared_stop = prepare_stop()
            if (
                not _exact_process_is_alive(gateway_identity)
                or _job_pid(plan, gateway=True) != pid
                or _listener_pid(int(plan["gateway_listener_port"])) != pid
                or _pid_start_token(pid) != expected_start
            ):
                raise DrainIdentityMismatch(
                    "gateway owner changed immediately before SIGINT"
                )
            try:
                os.kill(pid, signal.SIGINT)
            except OSError as exc:
                raise DrainIdentityMismatch(
                    "legacy gateway exact SIGINT failed"
                ) from exc
            signal_status = "SIGINT"
            wait_for_exact_process_exit(
                gateway_identity,
                float(plan["timeout_seconds"]),
                allow_exact_signaled_zombie=True,
            )
        elif _pid_start_token(pid) is not None:
            raise DrainIdentityMismatch(
                "retired legacy gateway PID was reused"
            )

        clean = _regular_file_baseline(
            _legacy_gateway_clean_shutdown_path(plan),
            label="legacy gateway clean-shutdown marker",
        )
        baseline_exists = bool(clean_baseline.get("exists"))
        if (
            not clean["exists"]
            or (
                baseline_exists
                and int(clean.get("mtime_ns") or 0)
                <= int(clean_baseline.get("mtime_ns") or 0)
            )
        ):
            raise ReleaseBuildError(
                "legacy gateway has no fresh clean-shutdown receipt"
            )

        bootout = _bootout_job(
            plan,
            gateway=True,
            required=False,
        )
        listener_before = _listener_pid_or_none(
            int(plan["gateway_listener_port"])
        )
        job_pid = _job_pid(plan, gateway=True)
        listener_after = _listener_pid_or_none(
            int(plan["gateway_listener_port"])
        )
        if (
            listener_before is not None
            or job_pid is not None
            or listener_after is not None
        ):
            raise DrainIdentityMismatch(
                "legacy gateway did not reach an absent graceful-stop boundary"
            )
    except BaseException as original:
        try:
            _set_launchd_service_disabled(
                plan,
                restart_control,
                disabled=False,
            )
        except Exception as restore_error:
            raise ReleaseBuildError(
                "legacy gateway graceful stop failed and launchd "
                "restart state could not be restored"
            ) from restore_error
        raise
    enabled = _set_launchd_service_disabled(
        plan,
        restart_control,
        disabled=False,
    )
    return {
        "status": "stopped",
        "gateway": copy.deepcopy(gateway_identity),
        "signal": signal_status,
        "prepare_stop": prepared_stop,
        "clean_shutdown": clean,
        "bootout": bootout,
        "launchd_restart": {
            "disabled": disabled,
            "restored": enabled,
        },
        "exact_exit_confirmed": True,
    }


def _gracefully_stop_legacy_gateway(
    plan: dict,
    prepared: dict,
    intent: dict,
) -> dict:
    planned = intent.get("planned_stop") if isinstance(intent, dict) else None
    if (
        not isinstance(planned, dict)
        or planned.get("path") != str(_legacy_gateway_planned_stop_path(plan))
        or not isinstance(planned.get("payload"), dict)
    ):
        raise ReleaseBuildError("legacy gateway graceful-stop intent is invalid")
    cron_tick_lock = _acquire_legacy_cron_tick_lock(plan)
    _verify_legacy_cron_tick_lock(plan, cron_tick_lock)
    gateway_identity = {
        "pid": int(prepared["gateway"]["pid"]),
        "pid_start_token": str(prepared["gateway"]["pid_start_token"]),
    }

    def stop_gateway() -> dict:
        gateway_alive = _exact_process_is_alive(gateway_identity)
        gateway_job = (
            _job_pid(plan, gateway=True)
            if gateway_alive
            else None
        )
        retired: dict | None = None
        if (
            gateway_alive
            and gateway_job == int(gateway_identity["pid"])
        ) or not gateway_alive:
            def prepare_exact_stop() -> dict:
                checkpoint = _legacy_process_checkpoint_receipt(plan)
                if (
                    _listener_pid(int(plan["gateway_listener_port"]))
                    != int(gateway_identity["pid"])
                    or _job_pid(plan, gateway=True)
                    != int(gateway_identity["pid"])
                    or _pid_start_token(int(gateway_identity["pid"]))
                    != gateway_identity["pid_start_token"]
                ):
                    raise DrainIdentityMismatch(
                        "gateway owner changed immediately before graceful stop"
                    )
                return {"checkpoint": checkpoint}

            retired = _retire_exact_legacy_gateway(
                plan,
                gateway_identity,
                intent,
                prepare_stop=prepare_exact_stop,
            )
            bootout = retired["bootout"]
        elif gateway_alive and gateway_job is None:
            bootout = {"status": "externally-reconciled"}
            wait_for_exact_process_exit(
                gateway_identity,
                float(plan["timeout_seconds"]),
            )
        elif gateway_alive:
            raise DrainIdentityMismatch(
                "legacy gateway launchd owner changed during graceful stop"
            )
        else:
            if gateway_job is not None:
                raise DrainIdentityMismatch(
                    "legacy gateway job survived its process"
                )
            bootout = {"status": "externally-reconciled"}
        try:
            listener = _listener_pid(int(plan["gateway_listener_port"]))
        except DrainIdentityMismatch:
            listener = None
        if listener is not None or _job_pid(plan, gateway=True) is not None:
            raise DrainIdentityMismatch(
                "legacy gateway did not reach an absent graceful-stop boundary"
            )
        receipt = {
            "status": "stopped",
            "gateway": copy.deepcopy(gateway_identity),
            "bootout": bootout,
            "exact_exit_confirmed": True,
        }
        if retired is not None:
            receipt["retired"] = retired
        return receipt

    retirement = _run_process_registry_retirement_barrier(
        plan,
        stop_gateway=stop_gateway,
    )
    _verify_legacy_cron_tick_lock(plan, cron_tick_lock)
    bootout = retirement["gateway_stop"]["bootout"]
    checkpoint = retirement["post_exit_checkpoint"]
    clean = _regular_file_baseline(
        _legacy_gateway_clean_shutdown_path(plan),
        label="legacy gateway clean-shutdown marker",
    )
    clean_baseline = intent.get("clean_shutdown_baseline")
    if (
        not isinstance(clean_baseline, dict)
        or not clean["exists"]
        or (
            clean_baseline.get("exists")
            and int(clean["mtime_ns"]) <= int(clean_baseline.get("mtime_ns") or 0)
        )
    ):
        raise ReleaseBuildError(
            "legacy gateway has no fresh clean-shutdown receipt"
        )
    status_path = _legacy_gateway_home(plan) / "gateway_state.json"
    status, status_receipt = _read_legacy_gateway_status(
        status_path,
        label="legacy gateway terminal status",
    )
    status_baseline = intent.get("status_baseline")
    log_texts: list[str] = []
    log_receipts: list[dict] = []
    for baseline in intent.get("logs", []):
        text, receipt = _read_gateway_log_delta(baseline)
        log_texts.append(text)
        log_receipts.append(receipt)
    combined = "\n".join(log_texts)
    shutdown_drain = _parse_legacy_gateway_shutdown_log(combined)
    terminal_status = _legacy_gateway_terminal_status_receipt(
        status,
        status_receipt,
        status_baseline=status_baseline,
        gateway_pid=int(gateway_identity["pid"]),
        shutdown_log=combined,
    )
    planned_path = Path(planned["path"])
    if planned_path.exists():
        if _read_private_json_value(
            planned_path,
            label="legacy gateway planned-stop marker",
        ) != planned["payload"]:
            raise ReleaseBuildError(
                "legacy gateway planned-stop marker has another owner"
            )
        planned_path.unlink()
        _fsync_directory(planned_path.parent)
    return {
        "status": "gracefully-stopped",
        "gateway": gateway_identity,
        "bootout": bootout,
        "process_checkpoint": checkpoint,
        "process_retirement": retirement,
        "cron_tick_lock": cron_tick_lock,
        "clean_shutdown": clean,
        "terminal_status": terminal_status,
        "logs": log_receipts,
        "shutdown_drain": shutdown_drain,
    }


def _clear_legacy_gateway_drain_marker(plan: dict, intent: dict) -> dict:
    marker = intent.get("marker") if isinstance(intent, dict) else None
    if not isinstance(marker, dict) or not isinstance(marker.get("payload"), dict):
        raise ReleaseBuildError("legacy gateway drain intent is invalid")
    path = Path(str(marker.get("path") or ""))
    if path != _legacy_gateway_drain_marker_path(plan):
        raise ReleaseBuildError("legacy gateway drain marker path changed")
    if path.exists():
        if _read_private_json_value(
            path,
            label="legacy gateway drain marker",
        ) != marker["payload"]:
            raise ReleaseBuildError(
                "legacy gateway drain marker has another owner"
            )
        path.unlink()
        _fsync_directory(path.parent)
    if path.exists():
        raise ReleaseBuildError("legacy gateway drain marker survived clear")
    return {
        "status": "cleared",
        "path": str(path),
        "expected_sha256": marker["sha256"],
    }


def _remove_owned_private_json_marker(
    path: Path,
    payload: dict,
    *,
    label: str,
) -> dict:
    if path.exists():
        if _read_private_json_value(path, label=label) != payload:
            raise ReleaseBuildError(f"{label} has another owner")
        path.unlink()
        _fsync_directory(path.parent)
    return {"status": "cleared", "path": str(path)}


def _restore_or_resume_frozen_legacy_webui(
    plan: dict,
    *,
    prepared: dict,
    frozen: dict,
) -> dict:
    rows = frozen.get("writers") if isinstance(frozen, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("role") != "webui"
        or rows[0].get("status") != "frozen"
    ):
        raise ReleaseBuildError("frozen writer receipt is invalid")
    raw_tree = rows[0].get("tree")
    if not isinstance(raw_tree, list) or not raw_tree:
        raise ReleaseBuildError("frozen process tree receipt is invalid")
    tree = _validated_parent_first_frozen_tree(raw_tree)
    expected_root = prepared.get("legacy")
    if not isinstance(expected_root, dict):
        raise ReleaseBuildError("prepared legacy WebUI identity is invalid")
    root = tree[0]
    if (
        int(root["pid"]) != int(expected_root.get("pid", -1))
        or root.get("pid_start_token") != expected_root.get("pid_start_token")
    ):
        raise DrainIdentityMismatch("frozen WebUI root identity changed")

    if _exact_process_is_alive(root):
        writers = _resume_frozen_prepared_writers(frozen)
        binding = _wait_for_legacy_binding(
            plan,
            prepared=prepared,
            gateway=False,
        )
        return {"writers": writers, "binding": binding}

    if _pid_start_token(int(root["pid"])) is not None:
        raise DrainIdentityMismatch(
            "retired frozen WebUI root PID was reused before abort restore"
        )
    for process in tree[1:]:
        if _exact_process_is_alive(process):
            raise DrainIdentityMismatch(
                "frozen WebUI child survived retired root before abort restore"
            )
        if _pid_start_token(int(process["pid"])) is not None:
            raise DrainIdentityMismatch(
                "retired frozen WebUI child PID was reused before abort restore"
            )

    retired_root = {
        "pid": int(root["pid"]),
        "pid_start_token": str(root["pid_start_token"]),
    }
    try:
        binding = _attest_restored_legacy_binding(
            plan,
            prepared=prepared,
            gateway=False,
        )
    except (DrainIdentityMismatch, ReleaseBuildError) as attestation_error:
        listener = _listener_pid_or_none(int(plan["listener_port"]))
        job_pid = _job_pid(plan, gateway=False)
        if listener is not None or job_pid is not None:
            raise DrainIdentityMismatch(
                "unexpected WebUI owner blocks pre-snapshot abort restore"
            ) from attestation_error
        started = _bootstrap_job(
            plan,
            plan["bootstrap_rollback_plist"],
            gateway=False,
        )
        binding = _wait_for_legacy_binding(
            plan,
            prepared=prepared,
            gateway=False,
        )
        writers = {
            "status": "restarted-after-exact-root-retirement",
            "retired_root": retired_root,
            "restored_root": {
                "pid": binding["pid"],
                "pid_start_token": binding["pid_start_token"],
            },
            "restart": started,
        }
    else:
        writers = {
            "status": "adopted-exact-restored-binding",
            "retired_root": retired_root,
            "restored_root": {
                "pid": binding["pid"],
                "pid_start_token": binding["pid_start_token"],
            },
        }
    return {"writers": writers, "binding": binding}


def _restore_legacy_gateway_before_snapshot_abort(
    plan: dict,
    prepared: dict,
    stop_intent: dict | None,
) -> dict:
    gateway_identity = prepared.get("gateway")
    if not isinstance(gateway_identity, dict):
        raise ReleaseBuildError(
            "prepared legacy gateway identity is invalid"
        )
    restart_control = (
        stop_intent.get("launchd_restart_control")
        if isinstance(stop_intent, dict)
        else None
    )
    if isinstance(restart_control, dict):
        launchd_restart = _set_launchd_service_disabled(
            plan,
            restart_control,
            disabled=False,
        )
    else:
        launchd_restart = {"status": "not-required"}

    try:
        gateway = _attest_restored_legacy_binding(
            plan,
            prepared=prepared,
            gateway=True,
        )
    except (DrainIdentityMismatch, ReleaseBuildError) as attestation_error:
        if _exact_process_is_alive(gateway_identity):
            if _job_pid(plan, gateway=True) != int(gateway_identity["pid"]):
                raise ReleaseBuildError(
                    "legacy gateway graceful stop is incomplete; "
                    "refusing duplicate restart"
                ) from attestation_error
            gateway = _wait_for_legacy_binding(
                plan,
                prepared=prepared,
                gateway=True,
            )
            recovery = {
                "status": "resumed-prepared-binding",
                "pid": gateway["pid"],
                "pid_start_token": gateway["pid_start_token"],
            }
        else:
            retired_pid = int(gateway_identity["pid"])
            if _pid_start_token(retired_pid) is not None:
                raise DrainIdentityMismatch(
                    "retired legacy gateway PID was reused before abort restore"
                ) from attestation_error
            listener = _listener_pid_or_none(
                int(plan["gateway_listener_port"])
            )
            if listener is not None:
                job_pid = _job_pid(plan, gateway=True)
                if job_pid != listener:
                    raise DrainIdentityMismatch(
                        "unexpected gateway owner blocks pre-snapshot abort"
                    ) from attestation_error
                runtime = _listener_process_receipt(
                    plan,
                    gateway=True,
                    require_git_source=False,
                )
                if not _runtime_receipt_matches(
                    runtime,
                    gateway_identity,
                ):
                    raise DrainIdentityMismatch(
                        "unexpected gateway owner blocks pre-snapshot abort"
                    ) from attestation_error
                gateway = _wait_for_legacy_binding(
                    plan,
                    prepared=prepared,
                    gateway=True,
                )
                recovery = {
                    "status": "adopted-starting-restored-binding",
                    "pid": gateway["pid"],
                    "pid_start_token": gateway["pid_start_token"],
                }
            else:
                bootout = _bootout_job(
                    plan,
                    gateway=True,
                    required=False,
                )
                started = _bootstrap_job(
                    plan,
                    plan["gateway_rollback_plist"],
                    gateway=True,
                )
                gateway = {
                    **_wait_for_legacy_binding(
                        plan,
                        prepared=prepared,
                        gateway=True,
                    ),
                    "pre_restart_bootout": bootout,
                    "restart": started,
                }
                recovery = {
                    "status": "restarted-cleanly-absent-binding",
                    "pid": gateway["pid"],
                    "pid_start_token": gateway["pid_start_token"],
                }
    else:
        recovery = {
            "status": "adopted-restored-binding",
            "pid": gateway["pid"],
            "pid_start_token": gateway["pid_start_token"],
        }
    return {
        "gateway": gateway,
        "launchd_restart": launchd_restart,
        "recovery": recovery,
    }


def _restore_legacy_before_snapshot_abort(
    plan: dict,
    prepared: dict,
    frozen: dict,
    phases: dict,
    original: Exception,
) -> dict:
    captured_controls = prepared.get("pre_managed_controls")
    stage_phase = phases.get("pre_managed_controls_staged")
    if not isinstance(stage_phase, dict):
        stage_intent = phases.get("pre_managed_controls_stage_intent")
        stage_phase = (
            stage_intent.get("expected")
            if isinstance(stage_intent, dict)
            else None
        )
    if stage_phase is None and isinstance(captured_controls, dict):
        stage_phase = {
            "status": "staged",
            **{
                key: {"path": captured_controls[key]["path"]}
                for key in ("selector_state", "selector_lock", "managed_plist")
                if isinstance(captured_controls.get(key), dict)
            },
        }
    controls = _restore_pre_managed_control_state(
        plan,
        captured_controls,
        stage_phase,
    )
    cli = _restore_bootstrap_cli_link(
        plan,
        prepared,
        phases,
    )
    marker_receipts: dict[str, dict] = {}
    drain_intent = phases.get("legacy_gateway_drain_intent")
    if isinstance(drain_intent, dict):
        marker = drain_intent.get("marker")
        if isinstance(marker, dict) and isinstance(marker.get("payload"), dict):
            marker_receipts["drain"] = _remove_owned_private_json_marker(
                Path(str(marker["path"])),
                marker["payload"],
                label="legacy gateway drain marker",
            )
    stop_intent = phases.get("legacy_gateway_stop_intent")
    if isinstance(stop_intent, dict):
        planned = stop_intent.get("planned_stop")
        if isinstance(planned, dict) and isinstance(planned.get("payload"), dict):
            marker_receipts["planned_stop"] = _remove_owned_private_json_marker(
                Path(str(planned["path"])),
                planned["payload"],
                label="legacy gateway planned-stop marker",
            )
    dispatcher_lock: dict | None = None
    durable_dispatcher_lock = phases.get("legacy_dispatcher_lock_acquired")
    if isinstance(durable_dispatcher_lock, dict):
        dispatcher_lock = _verify_legacy_dispatcher_lock(
            plan,
            durable_dispatcher_lock,
        )
        _wait_for_legacy_kanban_quiescence(plan)
    if dispatcher_lock is not None and dispatcher_lock.get("status") == "held":
        dispatcher_lock = _release_legacy_dispatcher_lock(plan)
    tick_intent = phases.get("legacy_cron_tick_lock_normalize_intent")
    cron_tick_reacquire: dict
    try:
        if isinstance(tick_intent, dict):
            original_tick_lock = tick_intent.get("original")
            if not isinstance(original_tick_lock, dict):
                raise ReleaseBuildError(
                    "legacy cron tick lock normalization intent is invalid"
                )
            original_status = original_tick_lock.get("status")
            if original_status == "present":
                original_mode = original_tick_lock.get("mode")
                if original_mode not in {0o600, 0o644}:
                    raise ReleaseBuildError(
                        "legacy cron tick lock normalization intent is invalid"
                    )
                cron_tick_reacquire = (
                    _acquire_legacy_cron_tick_lock_modes(
                        plan,
                        allowed_modes={0o600, int(original_mode)},
                    )
                )
            elif original_status == "absent":
                current_tick_lock = _read_legacy_cron_tick_file_receipt(
                    plan,
                    allowed_modes={0o600},
                    allow_absent=True,
                )
                if current_tick_lock.get("status") == "present":
                    cron_tick_reacquire = (
                        _acquire_legacy_cron_tick_lock_modes(
                            plan,
                            allowed_modes={0o600},
                        )
                    )
                else:
                    cron_tick_reacquire = {"status": "already-absent"}
            else:
                raise ReleaseBuildError(
                    "legacy cron tick lock normalization intent is invalid"
                )
            cron_tick_restore = _restore_legacy_cron_tick_lock(
                plan,
                tick_intent,
                phases.get("legacy_cron_tick_lock_normalized"),
            )
        else:
            cron_tick_reacquire = {"status": "not-required"}
            cron_tick_restore = {"status": "not-required"}
    finally:
        cron_tick_release = _release_legacy_cron_tick_lock(
            plan,
            allow_restored_mode=True,
        )
    store_intent = phases.get("synthetic_store_mode_normalize_intent")
    store_normalization = phases.get("synthetic_store_modes_normalized")
    if isinstance(store_intent, dict):
        synthetic_store_modes = (
            _restore_synthetic_completion_store_modes(
                plan,
                store_intent,
                store_normalization,
            )
        )
    else:
        synthetic_store_modes = {"status": "not-required"}
    gateway_restore = _restore_legacy_gateway_before_snapshot_abort(
        plan,
        prepared,
        stop_intent if isinstance(stop_intent, dict) else None,
    )
    gateway = gateway_restore["gateway"]
    restored_webui = _restore_or_resume_frozen_legacy_webui(
        plan,
        prepared=prepared,
        frozen=frozen,
    )
    webui = restored_webui["binding"]
    cron = _restore_watchdog_cron(plan, prepared)
    return {
        "status": "aborted",
        "reason_type": type(original).__name__,
        "reason_sha256": hashlib.sha256(str(original).encode()).hexdigest(),
        "markers": marker_receipts,
        "gateway": gateway,
        "gateway_launchd_restart": gateway_restore["launchd_restart"],
        "gateway_recovery": gateway_restore["recovery"],
        "webui": webui,
        "cli": cli,
        "pre_managed_controls": controls,
        "dispatcher_lock": dispatcher_lock or {"status": "not-acquired"},
        "cron_tick_lock": {
            "reacquire": cron_tick_reacquire,
            "release": cron_tick_release,
            "restore": cron_tick_restore,
        },
        "synthetic_store_modes": synthetic_store_modes,
        "writers": restored_webui["writers"],
        "watchdog_cron": cron,
    }


def _prove_frozen_legacy_boundary(
    plan: dict,
    prepared: dict,
    frozen: dict,
    dispatcher_lock_receipt: dict,
) -> dict:
    _verify_frozen_prepared_writers(plan, prepared, frozen)
    trees = {
        row.get("role"): row.get("tree", [])
        for row in frozen.get("writers", [])
        if isinstance(row, dict)
    }
    tree = trees.get("webui")
    if not isinstance(tree, list) or not tree:
        raise ReleaseBuildError("frozen WebUI process tree is unavailable")
    expected_root = prepared["legacy"]
    root = next(
        (
            process
            for process in tree
            if (
                isinstance(process, dict)
                and int(process.get("pid", -1)) == int(expected_root["pid"])
                and process.get("pid_start_token")
                == expected_root["pid_start_token"]
            )
        ),
        None,
    )
    if root is None:
        raise DrainIdentityMismatch(
            "frozen WebUI root identity changed at boundary"
        )
    try:
        gateway_listener = _listener_pid(int(plan["gateway_listener_port"]))
    except DrainIdentityMismatch:
        gateway_listener = None
    if gateway_listener is not None or _job_pid(plan, gateway=True) is not None:
        raise ReleaseBuildError(
            "legacy gateway is not at a graceful absent boundary"
        )
    dispatcher_lock = _verify_legacy_dispatcher_lock(
        plan,
        dispatcher_lock_receipt,
    )
    kanban = _wait_for_legacy_kanban_quiescence(plan)
    return {
        "status": "verified",
        "processes": {
            "webui": {
                "pid": int(root["pid"]),
                "pid_start_token": str(root["pid_start_token"]),
                "children": len(tree) - 1,
            },
            "gateway": {
                "pid": int(prepared["gateway"]["pid"]),
                "pid_start_token": str(prepared["gateway"]["pid_start_token"]),
                "status": "gracefully-stopped",
            },
        },
        "sockets": _established_socket_boundary_receipt(
            plan,
            gateway_pid=None,
        ),
        "durable_activity": _legacy_durable_activity_receipt(plan),
        "dispatcher_lock": dispatcher_lock,
        "kanban": kanban,
        "synthetic_completions": _inspect_synthetic_completion_stores(plan),
    }


def _bootout_prepared_jobs(plan: dict, prepared: dict) -> dict:
    receipts: dict[str, dict] = {}
    original = prepared["legacy"]
    job_pid = _job_pid(plan, gateway=False)
    if job_pid is not None and job_pid != int(original["pid"]):
        raise DrainIdentityMismatch(
            "WebUI launchd job was replaced before bootout"
        )
    webui_bootout = _bootout_job(plan, gateway=False, required=False)
    receipts["gateway"] = {"status": "already-gracefully-stopped"}
    remaining_webui_job = _job_pid(plan, gateway=False)
    if remaining_webui_job is None:
        webui_bootout["retirement"] = "verified-absent"
        status = "verified-absent"
    else:
        if (
            remaining_webui_job != int(original["pid"])
            or not _exact_process_is_alive(original)
            or "T" not in _ps_value(remaining_webui_job, "state").upper()
        ):
            raise DrainIdentityMismatch(
                "WebUI launchd job changed during frozen bootout"
            )
        webui_bootout["retirement"] = "pending-exact-frozen-root"
        status = "bootout-requested"
    receipts["webui"] = webui_bootout
    if _job_pid(plan, gateway=True) is not None:
        raise DrainIdentityMismatch(
            "gateway launchd job reappeared after graceful stop"
        )
    return {"status": status, "jobs": receipts}


def _stop_prepared_service(
    plan: dict,
    receipt: dict,
    *,
    gateway: bool,
    frozen_tree: dict,
    bootout_receipt: dict,
) -> dict:
    job_pid = _job_pid(plan, gateway=gateway)
    pending_retirement = (
        not gateway
        and bootout_receipt.get("retirement")
        == "pending-exact-frozen-root"
    )
    if job_pid is not None and (
        not pending_retirement
        or job_pid != int(receipt["pid"])
        or not _exact_process_is_alive(receipt)
    ):
        raise DrainIdentityMismatch("launchd job reappeared before exact stop")
    tree = frozen_tree.get("tree") if isinstance(frozen_tree, dict) else None
    if not isinstance(tree, list):
        raise ReleaseBuildError("frozen process tree receipt is missing")
    for process in reversed(_validated_parent_first_frozen_tree(tree)):
        if _exact_process_is_alive(process):
            os.kill(int(process["pid"]), signal.SIGKILL)
            wait_for_exact_process_exit(
                process,
                float(plan["timeout_seconds"]),
                allow_exact_signaled_zombie=True,
            )
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    while time.monotonic() < deadline:
        remaining_job = _job_pid(plan, gateway=gateway)
        if remaining_job is None:
            break
        if remaining_job != int(receipt["pid"]):
            raise DrainIdentityMismatch(
                "launchd job was replaced during exact stop"
            )
        time.sleep(float(plan["interval_seconds"]))
    else:
        raise DrainTimeout("launchd job retirement timed out after exact stop")
    port_key = "gateway_listener_port" if gateway else "listener_port"
    try:
        replacement = _listener_pid(int(plan[port_key]))
    except DrainIdentityMismatch:
        replacement = None
    if replacement is not None or _job_pid(plan, gateway=gateway) is not None:
        raise DrainIdentityMismatch("service did not reach an absent stop boundary")
    return {
        "status": "stopped",
        "pid": receipt["pid"],
        "pid_start_token": receipt["pid_start_token"],
        "bootout": bootout_receipt,
        "launchd_retirement": "verified-absent",
    }


def _stop_prepared_pair(
    plan: dict,
    prepared: dict,
    frozen: dict,
    booted_out: dict,
    gateway_stop: dict,
) -> dict:
    trees = {
        row.get("role"): row
        for row in frozen.get("writers", [])
        if isinstance(row, dict)
    }
    jobs = booted_out.get("jobs") if isinstance(booted_out, dict) else None
    if not isinstance(jobs, dict):
        raise ReleaseBuildError("legacy launchd bootout receipt is invalid")
    return {
        "webui": _stop_prepared_service(
            plan,
            prepared["legacy"],
            gateway=False,
            frozen_tree=trees.get("webui", {}),
            bootout_receipt=jobs.get("webui", {}),
        ),
        "gateway": copy.deepcopy(gateway_stop),
    }


def _stop_prepared_pair_and_bind_gate(
    plan: dict,
    prepared: dict,
    frozen: dict,
    booted_out: dict,
    gateway_stop: dict,
) -> dict:
    stopped = _stop_prepared_pair(
        plan,
        prepared,
        frozen,
        booted_out,
        gateway_stop,
    )
    gate = _start_or_adopt_ingress_gate(plan)
    return {"services": stopped, "gate": gate}


def _managed_gateway_transaction_id(plan: dict, identity: dict) -> str:
    """Return the transaction that created one exact managed gateway."""
    transaction_id = str(
        identity.get("startup_transaction_id")
        or plan.get("transaction_id")
        or ""
    )
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise ReleaseBuildError(
            "managed gateway release transaction identity is invalid"
        )
    return transaction_id


def _gateway_pair_gate_state(
    receipt: object,
    *,
    plan: dict,
    expected_identity: dict,
    expected_release_pair_id: str,
    listener_pid: int,
    listener_start: str,
    expected_pair_gate: str | dict | None,
) -> str:
    if (
        not isinstance(expected_pair_gate, dict)
        and expected_pair_gate not in {None, "active", "absent"}
    ):
        raise ValueError("expected pair-gate state is invalid")
    if (
        isinstance(expected_pair_gate, dict)
        and expected_pair_gate.get("active") is not True
        and expected_pair_gate.get("active") is not False
    ):
        raise ValueError("expected pair-gate receipt has no exact state")
    expected_active = (
        expected_pair_gate == "active"
        or (
            isinstance(expected_pair_gate, dict)
            and expected_pair_gate.get("active") is True
        )
    )
    expected_absent = (
        expected_pair_gate == "absent"
        or (
            isinstance(expected_pair_gate, dict)
            and expected_pair_gate.get("active") is False
        )
    )
    if not isinstance(receipt, dict):
        raise ReleaseBuildError("gateway pair-open gate receipt is missing")
    if receipt.get("active") is False:
        if receipt != {
            "active": False,
            "verified": True,
            "reason": "absent",
            "structure_verified": True,
            "local_identity_matches": None,
        }:
            raise ReleaseBuildError(
                "gateway absent pair-open gate receipt is invalid"
            )
        if expected_active:
            raise DrainIdentityMismatch(
                "gateway pair-open gate disappeared before release"
            )
        if isinstance(expected_pair_gate, dict) and receipt != expected_pair_gate:
            raise DrainIdentityMismatch(
                "gateway absent pair-open gate receipt changed"
            )
        return "absent"
    expected_keys = {
        "active",
        "verified",
        "reason",
        "schema",
        "transaction_id",
        "owner_hash",
        "epoch",
        "agent",
        "webui",
        "payload_sha256",
        "structure_verified",
        "local_identity_matches",
    }
    agent = receipt.get("agent")
    webui = receipt.get("webui")
    identity_keys = {"build_id", "pid", "start_time", "instance_epoch"}
    expected_generation = int(expected_identity["selector_generation"])
    expected_transaction_id = _managed_gateway_transaction_id(
        plan,
        expected_identity,
    )
    if (
        set(receipt) != expected_keys
        or receipt.get("active") is not True
        or receipt.get("verified") is not True
        or receipt.get("reason") != "verified"
        or receipt.get("structure_verified") is not True
        or receipt.get("local_identity_matches") is not True
        or receipt.get("schema") != "hermes.pair_open_gate.v1"
        or receipt.get("transaction_id") != expected_transaction_id
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("owner_hash") or ""))
        or receipt.get("epoch") != expected_generation
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(receipt.get("payload_sha256") or ""),
        )
        or not isinstance(agent, dict)
        or set(agent) != identity_keys
        or agent
        != {
            "build_id": str(
                expected_identity["agent_source_manifest_sha256"]
            ),
            "pid": listener_pid,
            "start_time": listener_start,
            "instance_epoch": expected_release_pair_id,
        }
        or not isinstance(webui, dict)
        or set(webui) != identity_keys
        or webui.get("build_id") != str(expected_identity["build_id"])
        or isinstance(webui.get("pid"), bool)
        or not isinstance(webui.get("pid"), int)
        or int(webui["pid"]) <= 1
        or not str(webui.get("start_time") or "").strip()
        or webui.get("instance_epoch") != str(expected_generation)
    ):
        raise DrainIdentityMismatch(
            "gateway pair-open gate ownership changed"
        )
    if expected_absent:
        raise DrainIdentityMismatch(
            "gateway pair-open gate survived release"
        )
    if isinstance(expected_pair_gate, dict) and receipt != expected_pair_gate:
        raise DrainIdentityMismatch(
            "gateway pair-open gate receipt changed"
        )
    return "active"


def _gateway_health_receipt(
    plan: dict,
    *,
    expected_identity: dict | None = None,
    expected_admission: str = "accepting_new_work",
    expected_pair_gate: str | dict | None = None,
    require_quiescent_work: bool = True,
) -> dict:
    if not isinstance(require_quiescent_work, bool):
        raise ValueError("gateway quiescent-work requirement is invalid")
    health = _http_json(
        str(plan["gateway_health_url"]),
        timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if health.get("status") != "ok":
        raise ReleaseBuildError("gateway health is not ready")
    if expected_identity is None:
        return {
            "status": "ok",
            "body_sha256": hashlib.sha256(
                json.dumps(health, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    if set(health) != {
        "status",
        "platform",
        "version",
        "release_identity",
        "drain",
    } or health.get("platform") != "hermes-agent":
        raise DrainIdentityMismatch("gateway public health schema changed")
    release = health.get("release_identity")
    process = release.get("process") if isinstance(release, dict) else None
    sealed = release.get("release") if isinstance(release, dict) else None
    expected_generation = int(expected_identity["selector_generation"])
    expected_transaction_id = _managed_gateway_transaction_id(
        plan,
        expected_identity,
    )
    expected_release = {
        "agent_commit": str(expected_identity["agent_source_commit"]),
        "agent_tree": str(expected_identity["agent_source_tree"]),
        "agent_manifest_sha256": str(
            expected_identity["agent_source_manifest_sha256"]
        ),
        "runtime_manifest_sha256": str(
            expected_identity["runtime_manifest_sha256"]
        ),
        "release_pair_id": release_selector.release_pair_id(
            expected_identity,
            selector_generation=expected_generation,
            transaction_id=expected_transaction_id,
        ),
        "webui_build_id": str(expected_identity["build_id"]),
        "webui_commit": str(expected_identity["commit"]),
        "webui_tree": str(expected_identity["tree"]),
        "webui_manifest_sha256": str(expected_identity["manifest_sha256"]),
        "selector_generation": str(expected_generation),
        "release_transaction_id": expected_transaction_id,
        "gateway_launchd_label": str(plan["gateway_launchd_label"]),
    }
    try:
        listener_pid = _listener_pid(int(plan["gateway_listener_port"]))
    except DrainIdentityMismatch as exc:
        raise DrainIdentityMismatch(
            "gateway public health has no exact listener owner"
        ) from exc
    listener_start = _pid_start_token(listener_pid)
    if (
        not isinstance(release, dict)
        or set(release) != {"schema", "verified", "release", "process"}
        or release.get("schema") != "hermes.public_release_identity.v1"
        or release.get("verified") is not True
        or sealed != expected_release
        or not isinstance(process, dict)
        or process
        != {
            "pid": listener_pid,
            "start_token": listener_start,
            "start_token_status": "verified",
        }
        or listener_start is None
    ):
        raise DrainIdentityMismatch("gateway public release identity changed")
    drain = health.get("drain")
    admission = drain.get("admission") if isinstance(drain, dict) else None
    work = drain.get("work") if isinstance(drain, dict) else None
    work_status = drain.get("work_status") if isinstance(drain, dict) else None
    quiescence = drain.get("quiescence") if isinstance(drain, dict) else None
    pair_gate = (
        drain.get("pair_open_gate")
        if isinstance(drain, dict)
        else None
    )
    cron_admission = (
        drain.get("cron_admission")
        if isinstance(drain, dict)
        else None
    )
    expected_work = {
        "active_http_requests",
        "active_agent_turns",
        "active_delegations",
        "background_processes",
        "process_completion_queue_depth",
        "active_cron_jobs",
        "gateway_background_tasks",
        "api_background_tasks",
        "running_kanban_workers",
    }
    pair_gate_state = _gateway_pair_gate_state(
        pair_gate,
        plan=plan,
        expected_identity=expected_identity,
        expected_release_pair_id=expected_release["release_pair_id"],
        listener_pid=listener_pid,
        listener_start=listener_start,
        expected_pair_gate=expected_pair_gate,
    )
    expected_pair_gate_active = (
        expected_pair_gate == "active"
        or (
            isinstance(expected_pair_gate, dict)
            and expected_pair_gate.get("active") is True
        )
    )
    expected_pair_gate_absent = (
        expected_pair_gate == "absent"
        or (
            isinstance(expected_pair_gate, dict)
            and expected_pair_gate.get("active") is False
        )
    )
    if expected_admission not in {
        "accepting_new_work",
        "rejecting_new_work",
    }:
        raise ValueError("expected gateway admission state is invalid")
    expected_cron_accepting = expected_admission == "accepting_new_work"
    if expected_admission == "accepting_new_work":
        expected_drain_requested = False
        expected_gate_active = False
        expected_effective_rejection = False
        admission_contract_valid = pair_gate_state == "absent"
    elif expected_pair_gate_active:
        expected_drain_requested = False
        expected_gate_active = True
        expected_effective_rejection = True
        admission_contract_valid = pair_gate_state == "active"
    elif expected_pair_gate_absent:
        expected_drain_requested = True
        expected_gate_active = False
        expected_effective_rejection = True
        admission_contract_valid = pair_gate_state == "absent"
    else:
        expected_gate_active = pair_gate_state == "active"
        expected_drain_requested = not expected_gate_active
        expected_effective_rejection = True
        admission_contract_valid = True
    if (
        not isinstance(drain, dict)
        or set(drain) != {
            "schema",
            "admission",
            "work",
            "work_status",
            "quiescence",
            "pair_open_gate",
            "cron_admission",
        }
        or drain.get("schema") != "hermes.gateway_drain.v1"
        or not isinstance(admission, dict)
        or set(admission) != {
            "state",
            "verified",
            "drain_requested",
            "pair_open_gate_active",
            "effective_rejection_requested",
        }
        or admission.get("state") != expected_admission
        or admission.get("verified") is not True
        or admission.get("drain_requested") is not expected_drain_requested
        or admission.get("pair_open_gate_active") is not expected_gate_active
        or admission.get("effective_rejection_requested")
        is not expected_effective_rejection
        or not admission_contract_valid
        or not isinstance(work, dict)
        or set(work) != expected_work
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in work.values()
        )
        or (
            require_quiescent_work
            and any(value != 0 for value in work.values())
        )
        or not isinstance(work_status, dict)
        or set(work_status) != expected_work
        or any(value != "verified" for value in work_status.values())
        or not isinstance(quiescence, dict)
        or set(quiescence) != {"verified", "quiescent", "blockers"}
        or quiescence.get("verified") is not True
        or not isinstance(quiescence.get("quiescent"), bool)
        or not isinstance(quiescence.get("blockers"), list)
        or any(
            not isinstance(blocker, str) or not blocker
            for blocker in quiescence.get("blockers", [])
        )
        or len(set(quiescence.get("blockers", [])))
        != len(quiescence.get("blockers", []))
        or not isinstance(cron_admission, dict)
        or set(cron_admission) != {
            "schema",
            "verified",
            "accepting",
            "gate_epoch",
            "active_count",
            "active_job_ids",
            "active_leases",
        }
        or cron_admission.get("schema") != "hermes.cron_admission.v1"
        or cron_admission.get("verified") is not True
        or cron_admission.get("accepting") is not expected_cron_accepting
        or isinstance(cron_admission.get("gate_epoch"), bool)
        or not isinstance(cron_admission.get("gate_epoch"), int)
        or cron_admission["gate_epoch"] < 1
        or isinstance(cron_admission.get("active_count"), bool)
        or cron_admission.get("active_count") != 0
        or cron_admission.get("active_job_ids") != []
        or cron_admission.get("active_leases") != []
    ):
        raise ReleaseBuildError("gateway public drain receipt is not quiescent")
    if expected_admission == "rejecting_new_work":
        if (
            quiescence.get("quiescent") is not True
            or quiescence.get("blockers") != []
        ):
            raise ReleaseBuildError("gateway drain did not reach quiescence")
    else:
        expected_open_blockers = {
            "admission_not_rejecting",
            *(
                key
                for key, value in work.items()
                if not require_quiescent_work and value > 0
            ),
        }
        if (
            quiescence.get("quiescent") is not False
            or set(quiescence.get("blockers", []))
            != expected_open_blockers
        ):
            raise ReleaseBuildError("gateway open-admission receipt is invalid")
    return {
        "status": "ok",
        "body_sha256": hashlib.sha256(
            json.dumps(health, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "release_identity": release,
        "drain": drain,
    }


def _wait_for_gateway_binding(
    plan: dict,
    *,
    previous_pid_start: tuple[int, str] | None,
    expected_identity: dict | None = None,
    expected_admission: str = "accepting_new_work",
    expected_pair_gate: str | dict | None = None,
    require_quiescent_work: bool = True,
) -> dict:
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            launchd_pid = _job_pid(plan, gateway=True)
            listener_pid = _listener_pid(int(plan["gateway_listener_port"]))
            if launchd_pid is None or launchd_pid != listener_pid:
                raise DrainIdentityMismatch("gateway launchd/listener binding is invalid")
            start = _pid_start_token(listener_pid)
            if not start:
                raise DrainIdentityMismatch("gateway start identity is unavailable")
            current = (listener_pid, start)
            if previous_pid_start is not None and current == previous_pid_start:
                raise DrainIdentityMismatch("gateway process identity was not replaced")
            runtime = _listener_process_receipt(
                plan,
                gateway=True,
                require_git_source=False,
            )
            if (
                runtime.get("pid") != listener_pid
                or runtime.get("pid_start_token") != start
            ):
                raise DrainIdentityMismatch(
                    "gateway runtime identity changed during binding"
                )
            return {
                "status": "verified",
                "launchd_pid": launchd_pid,
                "listener_pid": listener_pid,
                "pid_start_token": start,
                "health": _gateway_health_receipt(
                    plan,
                    expected_identity=expected_identity,
                    expected_admission=expected_admission,
                    expected_pair_gate=expected_pair_gate,
                    require_quiescent_work=require_quiescent_work,
                ),
                "runtime": runtime,
            }
        except (DrainIdentityMismatch, ReleaseBuildError) as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(f"gateway binding timed out: {last_error}")


def _managed_gateway_routing(plan: dict) -> dict[str, str]:
    plist = _read_plist(plan["installed_plist"])
    environment = plist.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        raise ReleaseBuildError("managed WebUI routing environment is missing")
    routing = {
        key: str(environment.get(key) or "")
        for key in _ROUTING_ENV_KEYS
    }
    if set(routing) != _ROUTING_ENV_KEYS or any(
        not value.strip() for value in routing.values()
    ):
        raise ReleaseBuildError("managed WebUI routing environment is invalid")
    try:
        routed_port = int(routing["HERMES_WEBUI_PORT"])
    except ValueError as exc:
        raise ReleaseBuildError("managed WebUI routing port is invalid") from exc
    if routed_port != int(plan["listener_port"]):
        raise ReleaseBuildError("managed WebUI routing port changed")
    return routing


def _attest_managed_gateway_binding(
    plan: dict,
    identity: dict,
    *,
    expected_admission: str = "accepting_new_work",
    expected_pair_gate: str | dict | None = None,
    require_quiescent_work: bool = True,
) -> dict:
    """Prove the gateway is the exact immutable peer for one WebUI build."""
    binding = _wait_for_gateway_binding(
        plan,
        previous_pid_start=None,
        expected_identity=identity,
        expected_admission=expected_admission,
        expected_pair_gate=expected_pair_gate,
        require_quiescent_work=require_quiescent_work,
    )
    plist_path = Path(plan["gateway_installed_plist"])
    plist = _read_plist(plist_path)
    arguments = plist.get("ProgramArguments")
    environment = plist.get("EnvironmentVariables")
    expected_shim_sha256 = hashlib.sha256(_render_cli_shim(identity)).hexdigest()
    if (
        plist.get("Label") != plan["gateway_launchd_label"]
        or not isinstance(arguments, list)
        or len(arguments) < 2
        or arguments[1] != "gateway"
        or not isinstance(environment, dict)
        or plist.get("WorkingDirectory") != identity.get("agent_source_path")
    ):
        raise DrainIdentityMismatch("managed gateway launch identity changed")
    program = _file_identity_receipt(arguments[0])
    runtime = binding.get("runtime")
    routing = _managed_gateway_routing(plan)
    template = _read_plist(plan["gateway_rollback_plist"])
    template_arguments = template.get("ProgramArguments")
    if not isinstance(template_arguments, list) or not template_arguments:
        raise ReleaseBuildError("gateway rollback template argv is invalid")
    release_transaction_id = _managed_gateway_transaction_id(plan, identity)
    expected_plist = transform_gateway_launchd_target(
        template,
        expected_label=plan["gateway_launchd_label"],
        expected_old_program=str(template_arguments[0]),
        managed_cli_shim=str(arguments[0]),
        release_identity=identity,
        managed_routing_environment=routing,
        release_transaction_id=release_transaction_id,
    )
    selector_generation = int(identity["selector_generation"])
    pair_id = release_selector.release_pair_id(
        identity,
        selector_generation=selector_generation,
        transaction_id=release_transaction_id,
    )
    expected_environment = {
        **routing,
        "PYTHONHOME": str(identity["runtime_python_home_path"]),
        "PYTHONPATH": os.pathsep.join(
            [
                str(identity["agent_source_path"]),
                str(identity["runtime_site_packages_path"]),
            ]
        ),
        "HERMES_WEBUI_RELEASE_PATH": str(identity["release_path"]),
        "HERMES_WEBUI_MANIFEST_SHA256": str(identity["manifest_sha256"]),
        "HERMES_WEBUI_AGENT_DIR": str(identity["agent_source_path"]),
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": str(
            identity["agent_source_manifest_sha256"]
        ),
        "HERMES_WEBUI_RUNTIME_PATH": str(identity["runtime_path"]),
        "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": str(
            identity["runtime_manifest_sha256"]
        ),
        "HERMES_WEBUI_LAUNCH_MODE": "managed-gateway",
        "HERMES_AGENT_COMMIT": str(identity["agent_source_commit"]),
        "HERMES_AGENT_TREE": str(identity["agent_source_tree"]),
        "HERMES_AGENT_MANIFEST_SHA256": str(
            identity["agent_source_manifest_sha256"]
        ),
        "HERMES_AGENT_SOURCE_PATH": str(identity["agent_source_path"]),
        "HERMES_RUNTIME_MANIFEST_SHA256": str(
            identity["runtime_manifest_sha256"]
        ),
        "HERMES_RUNTIME_PATH": str(identity["runtime_path"]),
        "HERMES_RELEASE_PAIR_ID": pair_id,
        "HERMES_WEBUI_BUILD_ID": str(identity["build_id"]),
        "HERMES_WEBUI_COMMIT": str(identity["commit"]),
        "HERMES_WEBUI_TREE": str(identity["tree"]),
        "HERMES_SELECTOR_GENERATION": str(selector_generation),
        "HERMES_RELEASE_TRANSACTION_ID": release_transaction_id,
        "HERMES_GATEWAY_LAUNCHD_LABEL": str(plan["gateway_launchd_label"]),
    }
    if (
        program.get("sha256") != expected_shim_sha256
        or plist != expected_plist
        or not isinstance(runtime, dict)
        or runtime.get("program_identity") != program
        or runtime.get("program_arguments") != arguments
        or runtime.get("cwd") != identity.get("agent_source_path")
        or any(environment.get(key) != value for key, value in expected_environment.items())
        or any(
            unsafe in environment
            for unsafe in (
                "VIRTUAL_ENV",
                "PYTHONUSERBASE",
                "PYTHONSTARTUP",
                "PYTHONINSPECT",
            )
        )
    ):
        raise DrainIdentityMismatch("managed gateway runtime identity changed")
    return {
        **binding,
        "build_id": identity["build_id"],
        "plist": _file_identity_receipt(plist_path),
        "shim_sha256": expected_shim_sha256,
        "routing_environment": routing,
    }


def _probe_managed_webui_binding(plan: dict, identity: dict) -> dict | None:
    """Return one exact live managed binding, or None only at a proven absence."""
    try:
        listener_pid = _listener_pid(int(plan["listener_port"]))
    except DrainIdentityMismatch:
        listener_pid = None
    job_pid = _job_pid(plan, gateway=False)
    if listener_pid is None and job_pid is None:
        return None
    if listener_pid is None or job_pid != listener_pid:
        raise DrainIdentityMismatch("managed WebUI launch boundary is ambiguous")
    inspect_control, _send_control, transaction = _release_control_client(
        plan["base_url"],
        _read_release_control_key(plan["signing_key_file"]),
        transaction_id=plan["transaction_id"],
        request_timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if transaction != plan["transaction_id"]:
        raise ReleaseBuildError("managed WebUI transaction identity changed")
    binding = _collect_process_binding(plan, inspect_control=inspect_control)
    signed_identity = binding.get("signed_identity")
    if not isinstance(signed_identity, dict):
        raise DrainIdentityMismatch("managed WebUI signed identity is missing")
    if not _candidate_identity_matches(signed_identity, identity):
        raise DrainIdentityMismatch("managed WebUI identity does not match release")
    return _require_candidate_binding(
        binding,
        candidate_identity=signed_identity,
        expected_candidate_identity=identity,
        admission_state="open",
        require_full_health=True,
    )


def _probe_live_adoption_webui_binding(
    plan: dict,
    identity: dict,
) -> dict | None:
    """Attest an already-live split without depending on deep-health latency."""
    try:
        listener_pid = _listener_pid(int(plan["listener_port"]))
    except DrainIdentityMismatch:
        listener_pid = None
    job_pid = _job_pid(plan, gateway=False)
    if listener_pid is None and job_pid is None:
        return None
    if listener_pid is None or job_pid != listener_pid:
        raise DrainIdentityMismatch("managed WebUI launch boundary is ambiguous")
    inspect_control, _send_control, transaction = _release_control_client(
        plan["base_url"],
        _read_release_control_key(plan["signing_key_file"]),
        transaction_id=plan["transaction_id"],
        request_timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if transaction != plan["transaction_id"]:
        raise ReleaseBuildError("managed WebUI transaction identity changed")
    inspection = _require_bound_control_receipt(
        inspect_control(),
        status="inspected",
        transaction_id=transaction,
    )
    signed_identity = inspection.get("identity")
    admission = inspection.get("admission")
    if (
        not isinstance(signed_identity, dict)
        or not _candidate_identity_matches(signed_identity, identity)
    ):
        raise DrainIdentityMismatch(
            "managed WebUI identity does not match release"
        )
    try:
        signed_pid = int(signed_identity.get("pid"))
    except (TypeError, ValueError) as exc:
        raise DrainIdentityMismatch(
            "managed WebUI signed PID is invalid"
        ) from exc
    start = str(signed_identity.get("pid_start_token") or "")
    if (
        signed_pid != listener_pid
        or not start
        or not isinstance(admission, dict)
        or admission.get("state") != "open"
        or admission.get("transaction_id") is not None
    ):
        raise DrainIdentityMismatch(
            "managed WebUI signed admission binding is invalid"
        )
    runtime = _listener_process_receipt(
        plan,
        gateway=False,
        require_git_source=False,
    )
    if (
        runtime.get("pid") != listener_pid
        or runtime.get("pid_start_token") != start
    ):
        raise DrainIdentityMismatch(
            "managed WebUI runtime identity changed during adoption"
        )
    return {
        "status": "verified",
        "launchd_pid": job_pid,
        "listener_pid": listener_pid,
        "signed_health_pid": signed_pid,
        "pid_start_token": start,
        "signed_identity": copy.deepcopy(signed_identity),
        "runtime": copy.deepcopy(runtime),
        "admission": copy.deepcopy(admission),
        "release_control_receipt_sha256": _canonical_journal_value_sha256(
            inspection
        ),
    }


def _stable_live_adoption_webui_binding(binding: dict) -> dict:
    return {
        key: copy.deepcopy(binding.get(key))
        for key in (
            "launchd_pid",
            "listener_pid",
            "signed_health_pid",
            "pid_start_token",
            "signed_identity",
            "runtime",
            "admission",
        )
    }


def _stable_live_adoption_gateway_binding(binding: dict) -> dict:
    health = binding.get("health")
    return {
        "listener_pid": binding.get("listener_pid"),
        "pid_start_token": binding.get("pid_start_token"),
        "runtime": copy.deepcopy(binding.get("runtime")),
        "build_id": binding.get("build_id"),
        "plist": copy.deepcopy(binding.get("plist")),
        "shim_sha256": binding.get("shim_sha256"),
        "routing_environment": copy.deepcopy(
            binding.get("routing_environment")
        ),
        "release_identity": copy.deepcopy(
            health.get("release_identity")
            if isinstance(health, dict)
            else None
        ),
        "admission": copy.deepcopy(
            health.get("drain", {}).get("admission")
            if isinstance(health, dict)
            else None
        ),
    }


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish one same-filesystem file without replacing a target."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_raw = os.fsencode(source)
    destination_raw = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise ReleaseBuildError("atomic no-replace rename is unavailable")
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_raw, destination_raw, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ReleaseBuildError("atomic no-replace rename is unavailable")
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, source_raw, -100, destination_raw, 0x00000001)
    else:
        raise ReleaseBuildError("atomic no-replace rename is unsupported")
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(destination)
    raise OSError(error, os.strerror(error), str(destination))


def _write_exclusive_private_json(
    path: Path,
    value: dict,
    *,
    label: str,
) -> tuple[int, int]:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ReleaseBuildError(f"{label} cannot be created safely")
    temporary = path.parent / (
        f".{path.name}.{secrets.token_hex(16)}.adoption-tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise ReleaseBuildError(f"{label} temporary cannot be created") from exc
    try:
        os.set_inheritable(descriptor, False)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
    except BaseException:
        opened = os.fstat(descriptor)
        os.close(descriptor)
        _unlink_exact_created_adoption_file(
            temporary,
            (opened.st_dev, opened.st_ino),
        )
        raise
    else:
        os.close(descriptor)
    try:
        _rename_noreplace(temporary, path)
    except FileExistsError as exc:
        _unlink_exact_created_adoption_file(
            temporary,
            (opened.st_dev, opened.st_ino),
        )
        raise ReleaseBuildError(f"{label} already exists") from exc
    except BaseException:
        _unlink_exact_created_adoption_file(
            temporary,
            (opened.st_dev, opened.st_ino),
        )
        raise
    _fsync_directory(path.parent)
    return opened.st_dev, opened.st_ino


def _read_exact_private_json_for_adoption(
    path: Path,
    expected: dict,
    *,
    label: str,
) -> tuple[int, int]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise ReleaseBuildError(f"{label} cannot be recovered") from exc
    expected_payload = json.dumps(
        expected,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or payload != expected_payload
    ):
        raise ReleaseBuildError(f"{label} conflicts with adoption")
    return opened.st_dev, opened.st_ino


def _unlink_exact_created_adoption_file(
    path: Path,
    identity: tuple[int, int],
) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == identity[0]
        and current.st_ino == identity[1]
        and current.st_uid == os.getuid()
        and current.st_nlink == 1
        and stat.S_IMODE(current.st_mode) == 0o600
    ):
        path.unlink()


def create_live_split_adoption(
    plan: dict,
    *,
    webui_identity: dict,
    gateway_identity: dict,
    webui_identity_path: Path | str,
    gateway_identity_path: Path | str,
    adoption_receipt_path: Path | str,
    adoption_id: str,
    created_at: str | None = None,
) -> dict:
    """Seal one explicit, double-observed live split as historical provenance."""
    paths = tuple(
        Path(value)
        for value in (
            webui_identity_path,
            gateway_identity_path,
            adoption_receipt_path,
        )
    )
    if len(set(paths)) != 3:
        raise ReleaseBuildError("live split adoption output paths are duplicated")
    parent = paths[0].parent
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise ReleaseBuildError("live split adoption root is unavailable") from exc
    if (
        any(
            not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            or path.parent != parent
            or path.is_symlink()
            for path in paths
        )
        or parent.resolve(strict=True) != parent
        or parent_stat.st_uid != os.getuid()
        or not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_mode & 0o022
    ):
        raise ReleaseBuildError("live split adoption output path is unsafe")
    if not _TRANSACTION_ID.fullmatch(str(adoption_id or "")):
        raise ReleaseBuildError("live split adoption identity is invalid")
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ReleaseBuildError("live split adoption timestamp is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc).isoformat() != timestamp
    ):
        raise ReleaseBuildError(
            "live split adoption timestamp is not canonical UTC"
        )
    for name, identity in (
        ("WebUI", webui_identity),
        ("gateway", gateway_identity),
    ):
        if (
            not isinstance(identity.get("selector_generation"), int)
            or isinstance(identity.get("selector_generation"), bool)
            or identity["selector_generation"] <= 0
        ):
            raise ReleaseBuildError(
                f"last-good {name} adoption identity is invalid"
            )
        _attest_expected_release_identity(
            identity,
            selector_path=str(identity.get("selector_path") or ""),
            label=f"last-good {name}",
        )
    if (
        webui_identity.get("startup_fenced") is True
        and _TRANSACTION_ID.fullmatch(
            str(webui_identity.get("startup_transaction_id") or "")
        )
    ):
        receipt_schema_version = (
            "hermes.last_good_split_adoption.v1",
            1,
        )
    elif (
        webui_identity.get("startup_fenced") is False
        and webui_identity.get("startup_transaction_id") is None
    ):
        receipt_schema_version = (
            "hermes.last_good_split_adoption.v2",
            2,
        )
    else:
        raise ReleaseBuildError(
            "last-good WebUI startup identity is invalid"
        )
    shared_identity_keys = (
        _LAST_GOOD_RUNTIME_SHARED_IDENTITY_KEYS
        if receipt_schema_version
        == ("hermes.last_good_split_adoption.v2", 2)
        else _LAST_GOOD_SHARED_IDENTITY_KEYS
    )
    if any(
        webui_identity.get(key) != gateway_identity.get(key)
        for key in shared_identity_keys
    ):
        raise ReleaseBuildError("last-good shared runtime identity changed")

    selector_before = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    if not isinstance(selector_before, dict):
        raise ReleaseBuildError(
            "live split selector authority is not idle on adopted build"
        )
    try:
        _validate_split_adoption_selector_authority(
            selector_before,
            webui_identity=webui_identity,
            gateway_identity=gateway_identity,
            schema_version=receipt_schema_version,
        )
    except ReleaseBuildError as exc:
        raise ReleaseBuildError(
            "live split selector authority is not idle on adopted build"
        ) from exc
    webui_before = _probe_live_adoption_webui_binding(plan, webui_identity)
    if webui_before is None:
        raise DrainIdentityMismatch("managed WebUI is absent during adoption")
    gateway_before = _attest_managed_gateway_binding(
        plan,
        gateway_identity,
        expected_admission="accepting_new_work",
        require_quiescent_work=False,
    )
    webui_after = _probe_live_adoption_webui_binding(plan, webui_identity)
    if webui_after is None:
        raise DrainIdentityMismatch("managed WebUI is absent during adoption")
    gateway_after = _attest_managed_gateway_binding(
        plan,
        gateway_identity,
        expected_admission="accepting_new_work",
        require_quiescent_work=False,
    )
    selector_after = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    if (
        selector_before != selector_after
        or _stable_live_adoption_webui_binding(webui_before)
        != _stable_live_adoption_webui_binding(webui_after)
        or _stable_live_adoption_gateway_binding(gateway_before)
        != _stable_live_adoption_gateway_binding(gateway_after)
    ):
        raise DrainIdentityMismatch(
            "live split process or selector changed during adoption"
        )
    webui_admission = webui_before.get("admission", {})
    if webui_admission.get("state") != "open":
        raise ReleaseBuildError("live split WebUI admission is not open")

    def live_binding(value: dict, admission_state: str) -> dict:
        try:
            listener_pid = int(value["listener_pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DrainIdentityMismatch(
                "live split process binding is invalid"
            ) from exc
        start = str(value.get("pid_start_token") or "")
        if listener_pid <= 1 or not start:
            raise DrainIdentityMismatch("live split process binding is invalid")
        return {
            "listener_pid": listener_pid,
            "pid_start_token": start,
            "admission_state": admission_state,
            "evidence": _journal_copy_of_immutable_evidence(value),
            "binding_sha256": _canonical_journal_value_sha256(
                _journal_copy_of_immutable_evidence(value)
            ),
        }

    shared_identity = {
        key: webui_identity.get(key)
        for key in sorted(shared_identity_keys)
    }
    receipt = {
        "schema": receipt_schema_version[0],
        "version": receipt_schema_version[1],
        "adoption_id": adoption_id,
        "created_at": timestamp,
        "selector": {
            "state": copy.deepcopy(selector_before),
            "state_sha256": _canonical_journal_value_sha256(selector_before),
        },
        "webui": {
            "identity": copy.deepcopy(webui_identity),
            "identity_sha256": _canonical_journal_value_sha256(webui_identity),
            "live_binding": live_binding(webui_before, "open"),
        },
        "gateway": {
            "identity": copy.deepcopy(gateway_identity),
            "identity_sha256": _canonical_journal_value_sha256(gateway_identity),
            "live_binding": live_binding(
                gateway_before,
                "accepting_new_work",
            ),
        },
        "shared_identity_sha256": _canonical_journal_value_sha256(
            shared_identity
        ),
    }
    for path, value, label in (
        (paths[0], webui_identity, "last-good WebUI identity"),
        (paths[1], gateway_identity, "last-good gateway identity"),
        (paths[2], receipt, "last-good adoption receipt"),
    ):
        if os.path.lexists(path):
            _read_exact_private_json_for_adoption(
                path,
                value,
                label=label,
            )
        else:
            _write_exclusive_private_json(
                path,
                value,
                label=label,
            )
    _fsync_directory(parent)
    receipt_sha256 = sha256_file(paths[2])
    _read_sealed_split_adoption_receipt(
        str(paths[2]),
        expected_sha256=receipt_sha256,
        trusted_root=parent,
        webui_identity=webui_identity,
        gateway_identity=gateway_identity,
    )
    return {
        "last_good_identity_json": str(paths[0]),
        "last_good_gateway_identity_json": str(paths[1]),
        "last_good_split_adoption_receipt": str(paths[2]),
        "last_good_split_adoption_receipt_sha256": receipt_sha256,
    }


def _probe_startup_fenced_webui_binding(
    plan: dict,
    identity: dict,
) -> dict | None:
    try:
        listener_pid = _listener_pid(int(plan["listener_port"]))
    except DrainIdentityMismatch:
        listener_pid = None
    job_pid = _job_pid(plan, gateway=False)
    if listener_pid is None and job_pid is None:
        return None
    if listener_pid is None or job_pid != listener_pid:
        raise DrainIdentityMismatch(
            "startup-fenced WebUI launch boundary is ambiguous"
        )
    inspect_control, _send_control, transaction = _release_control_client(
        plan["base_url"],
        _read_release_control_key(plan["signing_key_file"]),
        transaction_id=plan["transaction_id"],
        request_timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
    )
    if transaction != plan["transaction_id"]:
        raise ReleaseBuildError("startup-fenced WebUI transaction changed")
    binding = _collect_process_binding(plan, inspect_control=inspect_control)
    signed_identity = binding.get("signed_identity")
    if not isinstance(signed_identity, dict) or not _candidate_identity_matches(
        signed_identity,
        identity,
    ):
        raise DrainIdentityMismatch(
            "startup-fenced WebUI identity does not match candidate"
        )
    return _require_candidate_binding(
        binding,
        candidate_identity=signed_identity,
        expected_candidate_identity=identity,
        admission_state="startup-fenced",
        require_full_health=False,
    )


def _install_managed_gateway_plist(
    plan: dict,
    prepared: dict | None,
    identity: dict,
) -> dict:
    # The gateway can launch directly from the staged immutable candidate
    # shim. FIRST activation keeps the canonical public CLI on its maintenance
    # deny shim until the paired runtime has durably reached pair_opened.
    cli = stage_immutable_cli_shim(plan, identity)
    template = _read_plist(plan["gateway_rollback_plist"])
    template_arguments = template.get("ProgramArguments")
    if not isinstance(template_arguments, list) or not template_arguments:
        raise ReleaseBuildError("gateway rollback template argv is invalid")
    if prepared is None:
        routing_environment = _managed_gateway_routing(plan)
        expected_old_program = str(template_arguments[0])
        installed_mode = stat.S_IMODE(
            Path(plan["gateway_installed_plist"]).stat().st_mode
        )
    else:
        routing_environment = prepared["legacy"]["routing_environment"]
        expected_old_program = prepared["gateway"]["program_arguments"][0]
        installed_mode = int(prepared["gateway_plist"]["resolved_mode"])
    transformed = transform_gateway_launchd_target(
        template,
        expected_label=plan["gateway_launchd_label"],
        expected_old_program=expected_old_program,
        managed_cli_shim=cli["shim_path"],
        release_identity=identity,
        managed_routing_environment=routing_environment,
        release_transaction_id=plan["transaction_id"],
    )
    _write_plist_atomic(plan["managed_gateway_plist"], transformed)
    installed = _atomic_copy_file(
        plan["managed_gateway_plist"],
        plan["gateway_installed_plist"],
        expected_sha256=sha256_file(Path(plan["managed_gateway_plist"])),
        mode=installed_mode,
    )
    return {"cli": cli, "installed_plist": installed}


def _watchdog_state_receipt(plan: dict) -> dict:
    state_path = Path(plan["watchdog_state_file"])
    if state_path.parent.name != "recovery":
        raise ReleaseBuildError("planned watchdog state path is not canonical")
    hermes_home = state_path.parent.parent
    if hermes_home.resolve(strict=True) != hermes_home:
        raise ReleaseBuildError("planned watchdog home is not canonical")
    if not os.path.lexists(state_path):
        return {
            "path": str(state_path),
            "exists": False,
            "sha256": None,
            "schema_version": None,
            "claim_revision": 0,
            "device": None,
            "inode": None,
            "uid": None,
            "mode": None,
            "size": 0,
        }
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(state_path, flags)
    except OSError as exc:
        raise ReleaseBuildError("watchdog state is unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise ReleaseBuildError("watchdog state is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, 16 * 1024 * 1024 + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise ReleaseBuildError("watchdog state is too large")
        payload = b"".join(chunks)
        current = state_path.lstat()
        if (
            current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or current.st_uid != opened.st_uid
            or current.st_nlink != opened.st_nlink
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(opened.st_mode)
            or current.st_size != opened.st_size
        ):
            raise DrainIdentityMismatch("watchdog state changed while reading")
    finally:
        os.close(descriptor)
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildError("watchdog state JSON is invalid") from exc
    if not isinstance(state, dict):
        raise ReleaseBuildError("watchdog state must be a JSON object")
    try:
        revision = int(state.get("claim_revision", 0))
    except (TypeError, ValueError) as exc:
        raise ReleaseBuildError("watchdog state revision is invalid") from exc
    if revision < 0:
        raise ReleaseBuildError("watchdog state revision is invalid")
    return {
        "path": str(state_path),
        "exists": True,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "schema_version": state.get("schema_version"),
        "claim_revision": revision,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "size": opened.st_size,
    }


def _watchdog_reconcile_receipt(
    plan: dict,
    prepared: dict,
    *,
    script_path: Path | str | None = None,
) -> dict:
    state_path = Path(plan["watchdog_state_file"])
    hermes_home = state_path.parent.parent
    installed_script = Path(plan["watchdog_installed_script"])
    candidate_script = Path(plan["watchdog_candidate_script"])
    reconcile_script = Path(script_path) if script_path is not None else installed_script
    if reconcile_script not in {installed_script, candidate_script}:
        raise ReleaseBuildError("watchdog reconcile script path is invalid")
    script_receipt = _file_identity_receipt(reconcile_script)
    if script_receipt.get("sha256") != plan["watchdog_expected_sha256"]:
        raise DrainIdentityMismatch("watchdog reconcile script identity changed")
    before = _watchdog_state_receipt(plan)
    environment = {
        key: value
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "TZ")
        if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "HOME": str(Path.home()),
            "HERMES_HOME": str(hermes_home),
            "HERMES_SESSION_WATCHDOG_DB": str(hermes_home / "state.db"),
            "HERMES_SESSION_WATCHDOG_WEBUI_STATE_DIR": str(
                Path(plan["signing_key_file"]).parent
            ),
            "HERMES_SESSION_WATCHDOG_WEBUI_SIGNING_KEY": str(
                plan["signing_key_file"]
            ),
            "HERMES_SESSION_WATCHDOG_WEBUI_RECOVERY_URL": (
                f"{str(plan['base_url']).rstrip('/')}/api/internal/recovery/start"
            ),
            "HERMES_WEBUI_PORT": str(plan["listener_port"]),
            "HERMES_SESSION_WATCHDOG_RECONCILE_ONLY": "1",
        }
    )
    retry_window = min(30.0, float(plan["timeout_seconds"]))
    invocation_timeout = max(1.0, retry_window)
    deadline = time.monotonic() + retry_window
    transient_empty_attempts = 0
    while True:
        try:
            completed = subprocess.run(
                [str(reconcile_script)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=invocation_timeout,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseBuildError(
                "watchdog reconcile-only invocation failed"
            ) from exc
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        match = re.fullmatch(
            r"RECOVERY_SLOT_RECONCILE_ONLY status="
            r"(no_reconcilable_slot|superseded_turn_after_abandoned_dispatch)",
            stdout,
        )
        if completed.returncode == 0 and not stderr and match is not None:
            break
        if completed.returncode == 0 and not stdout and not stderr:
            transient_empty_attempts += 1
            now = time.monotonic()
            if now < deadline:
                time.sleep(
                    min(float(plan["interval_seconds"]), deadline - now)
                )
                continue
        raise ReleaseBuildError("watchdog reconcile-only receipt is invalid")
    after = _watchdog_state_receipt(plan)
    if after["claim_revision"] < before["claim_revision"]:
        raise ReleaseBuildError("watchdog claim revision moved backwards")
    return {
        "status": match.group(1),
        "transient_empty_attempts": transient_empty_attempts,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "state_path": str(state_path),
        "state_before": before,
        "state_after": after,
        "installed_script": script_receipt,
        "script_role": (
            "installed" if reconcile_script == installed_script else "candidate"
        ),
        "cron": _attest_disabled_watchdog_cron(plan, prepared),
    }


def _attest_bootstrap_pair_readiness(
    plan: dict,
    prepared: dict,
    phases: dict,
) -> dict:
    required = (
        "watchdog_installed",
        "watchdog_reconciled_once",
        "watchdog_reconciled_twice",
        "watchdog_cron_disabled",
    )
    if any(name not in phases for name in required):
        raise ReleaseBuildError("paired pre-open watchdog proof is incomplete")
    script = _file_identity_receipt(plan["watchdog_installed_script"])
    installed = phases["watchdog_installed"].get("script")
    if (
        not isinstance(installed, dict)
        or script.get("sha256") != plan["watchdog_expected_sha256"]
        or any(
            script.get(key) != installed.get(key)
            for key in ("resolved_path", "sha256", "resolved_mode", "resolved_uid")
        )
    ):
        raise DrainIdentityMismatch(
            "installed watchdog changed after paired readiness proof"
        )
    reconciles = [
        phases["watchdog_reconciled_once"],
        phases["watchdog_reconciled_twice"],
    ]
    prior_revision = -1
    prior_after: dict | None = None
    for receipt in reconciles:
        before = receipt.get("state_before")
        after = receipt.get("state_after")
        receipt_script = receipt.get("installed_script")
        if (
            receipt.get("status")
            not in {
                "no_reconcilable_slot",
                "superseded_turn_after_abandoned_dispatch",
            }
            or not isinstance(before, dict)
            or not isinstance(after, dict)
            or not isinstance(receipt_script, dict)
            or receipt_script.get("sha256") != plan["watchdog_expected_sha256"]
            or int(after.get("claim_revision", -1))
            < int(before.get("claim_revision", -1))
            or int(before.get("claim_revision", -1)) < prior_revision
            or (prior_after is not None and before != prior_after)
        ):
            raise ReleaseBuildError(
                "durable watchdog reconciliation proof is invalid"
            )
        prior_revision = int(after["claim_revision"])
        prior_after = after
    cron = _attest_disabled_watchdog_cron(plan, prepared)
    if cron != phases["watchdog_cron_disabled"]:
        raise DrainIdentityMismatch(
            "disabled watchdog cron changed after paired readiness proof"
        )
    return {
        "status": "verified",
        "script": script,
        "cron": cron,
        "reconcile_claim_revision": prior_revision,
    }


def _attest_managed_watchdog_readiness(plan: dict) -> dict:
    script = _file_identity_receipt(plan["watchdog_installed_script"])
    if (
        script.get("sha256") != plan["watchdog_expected_sha256"]
        or int(script.get("resolved_mode", 0)) & 0o111 == 0
    ):
        raise DrainIdentityMismatch("managed watchdog identity changed")
    cron = _cron_watchdog_receipt(plan)
    if not cron.get("watchdog_command"):
        raise ReleaseBuildError("managed watchdog cron is not active")
    return {
        "status": "verified",
        "script": script,
        "cron": cron,
    }


def _watchdog_recovery_worker_receipt(plan: dict) -> dict:
    state_path = Path(plan["watchdog_state_file"])
    pid_path = state_path.parent / "recovery.pid"
    if not os.path.lexists(pid_path):
        return {
            "status": "absent",
            "path": str(pid_path),
            "pid": None,
            "pid_start_token": None,
        }
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(pid_path, flags)
    except OSError as exc:
        raise ReleaseBuildError("watchdog recovery PID receipt is unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o077
            or opened.st_size > 128
        ):
            raise ReleaseBuildError("watchdog recovery PID receipt is unsafe")
        payload = os.read(descriptor, 129)
        current = pid_path.lstat()
        if (
            current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or current.st_uid != opened.st_uid
            or current.st_nlink != opened.st_nlink
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(opened.st_mode)
            or current.st_size != opened.st_size
        ):
            raise DrainIdentityMismatch(
                "watchdog recovery PID receipt changed while reading"
            )
    finally:
        os.close(descriptor)
    try:
        recovery_pid = int(payload.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseBuildError("watchdog recovery PID receipt is invalid") from exc
    if recovery_pid <= 1:
        raise ReleaseBuildError("watchdog recovery PID receipt is invalid")
    start_token = _pid_start_token(recovery_pid)
    if start_token is not None:
        raise ReleaseBuildError(
            "active watchdog recovery worker blocks release cutover"
        )
    return {
        "status": "stale",
        "path": str(pid_path),
        "pid": recovery_pid,
        "pid_start_token": None,
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _verify_watchdog_state_lock(plan: dict, handle) -> dict:
    if handle is None or getattr(handle, "closed", True):
        raise DrainIdentityMismatch("watchdog state lock is not held")
    lock_path = Path(plan["watchdog_state_file"]).with_suffix(".lock")
    try:
        opened = os.fstat(handle.fileno())
        current = lock_path.lstat()
    except OSError as exc:
        raise DrainIdentityMismatch("watchdog state lock identity is unavailable") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
        or current.st_uid != opened.st_uid
        or current.st_nlink != opened.st_nlink
        or stat.S_IMODE(current.st_mode) != stat.S_IMODE(opened.st_mode)
    ):
        raise DrainIdentityMismatch("watchdog state lock path identity changed")
    return {
        "status": "locked",
        "path": str(lock_path),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "uid": opened.st_uid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
    }


def _release_watchdog_state_lock(plan: dict, handle) -> dict:
    receipt = _verify_watchdog_state_lock(plan, handle)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return {**receipt, "status": "released"}


def _acquire_watchdog_state_lock(plan: dict):
    state_path = Path(plan["watchdog_state_file"])
    lock_path = state_path.with_suffix(".lock")
    parent = _prepare_release_root(lock_path.parent)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
    ):
        os.close(descriptor)
        raise ReleaseBuildError("watchdog state lock is unsafe")
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise DrainTimeout("active watchdog did not release state lock")
            time.sleep(float(plan["interval_seconds"]))
    _fsync_directory(parent)
    try:
        _verify_watchdog_state_lock(plan, handle)
        _watchdog_recovery_worker_receipt(plan)
    except Exception:
        handle.close()
        raise
    return handle


def _validated_watchdog_prepared(plan: dict, prepared: object) -> dict:
    if not isinstance(prepared, dict):
        raise ReleaseBuildError("watchdog release preparation is invalid")
    cron = prepared.get("watchdog_cron")
    backup_path = Path(str(cron.get("backup_path") or "")) if isinstance(cron, dict) else None
    if (
        not isinstance(cron, dict)
        or backup_path != Path(plan["watchdog_crontab_rollback"])
        or not re.fullmatch(r"[0-9a-f]{64}", str(cron.get("backup_sha256") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(cron.get("crontab_sha256") or ""))
        or not str(cron.get("watchdog_command") or "").strip()
    ):
        raise ReleaseBuildError("watchdog release cron preparation is invalid")
    return copy.deepcopy(prepared)


def _prepare_release_watchdog_barrier(plan: dict) -> dict:
    prepared = {"watchdog_cron": _backup_crontab(plan)}
    if _watchdog_scheduler_backend(plan) == "hermes_internal":
        prepared["gateway"] = _listener_process_receipt(
            plan,
            gateway=True,
            require_git_source=False,
        )
        prepared["watchdog_cron"]["drain_intent"] = (
            _legacy_gateway_drain_intent_receipt(plan, prepared)
        )
    return prepared


def _begin_release_watchdog_barrier(
    plan: dict,
    *,
    prepared: dict | None = None,
) -> dict:
    journal = read_transaction_journal(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
    )
    phases = journal["phases"]
    intent = phases.get("watchdog_cron_disable_intent")
    if intent is None:
        durable_prepared = _validated_watchdog_prepared(
            plan,
            prepared
            if prepared is not None
            else _prepare_release_watchdog_barrier(plan),
        )
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="watchdog_cron_disable_intent",
            receipt={"prepared": durable_prepared},
        )
        phases = journal["phases"]
    else:
        durable_prepared = _validated_watchdog_prepared(
            plan,
            intent.get("prepared") if isinstance(intent, dict) else None,
        )
        if prepared is not None and _validated_watchdog_prepared(
            plan,
            prepared,
        ) != durable_prepared:
            raise DrainIdentityMismatch(
                "watchdog release preparation changed on resume"
            )
    if "watchdog_cron_disabled" not in phases:
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="watchdog_cron_disabled",
            receipt=_disable_watchdog_cron(plan, durable_prepared),
        )
        phases = journal["phases"]
    else:
        restoring = bool(
            {
                "watchdog_cron_restore_intent",
                "watchdog_cron_restored",
            }
            & set(phases)
        )
        if restoring:
            if _watchdog_scheduler_backend(plan) == "hermes_internal":
                try:
                    restored = _watchdog_receipt_for_prepared(
                        plan,
                        durable_prepared,
                    )
                except (DrainIdentityMismatch, ReleaseBuildError):
                    disabled = _attest_disabled_watchdog_cron(
                        plan,
                        durable_prepared,
                    )
                    if disabled != phases["watchdog_cron_disabled"]:
                        raise DrainIdentityMismatch(
                            "watchdog cron barrier changed on release resume"
                        )
                else:
                    if not _cron_receipt_matches_prepared(
                        restored,
                        durable_prepared,
                    ):
                        raise DrainIdentityMismatch(
                            "watchdog cron restore adoption changed"
                        )
            else:
                current_cron = _read_crontab()
                backup = Path(plan["watchdog_crontab_rollback"])
                original = backup.read_text(encoding="utf-8")
                command = durable_prepared["watchdog_cron"][
                    "watchdog_command"
                ]
                marker = (
                    f"# HERMES_CUTOVER_DISABLED {plan['transaction_id']} "
                    + command
                )
                disabled_content = original.replace(command, marker, 1)
                if current_cron == disabled_content:
                    disabled = _attest_disabled_watchdog_cron(
                        plan,
                        durable_prepared,
                    )
                    if disabled != phases["watchdog_cron_disabled"]:
                        raise DrainIdentityMismatch(
                            "watchdog cron barrier changed on release resume"
                        )
                elif current_cron == original:
                    restored = _watchdog_receipt_for_prepared(
                        plan,
                        durable_prepared,
                    )
                    if not _cron_receipt_matches_prepared(
                        restored,
                        durable_prepared,
                    ):
                        raise DrainIdentityMismatch(
                            "watchdog cron restore adoption changed"
                        )
                else:
                    raise DrainIdentityMismatch(
                        "watchdog cron changed during restore adoption"
                    )
        else:
            disabled = _attest_disabled_watchdog_cron(plan, durable_prepared)
            if disabled != phases["watchdog_cron_disabled"]:
                raise DrainIdentityMismatch(
                    "watchdog cron barrier changed on release resume"
                )
    if "watchdog_state_reconciled" not in phases:
        journal = record_transaction_phase(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
            phase="watchdog_state_reconciled",
            receipt=_watchdog_reconcile_receipt(
                plan,
                durable_prepared,
                script_path=plan["watchdog_candidate_script"],
            ),
        )
        phases = journal["phases"]
    reconciled = phases["watchdog_state_reconciled"]
    expected_state = (
        reconciled.get("state_after") if isinstance(reconciled, dict) else None
    )
    if not isinstance(expected_state, dict):
        raise ReleaseBuildError("watchdog reconciliation state receipt is invalid")
    handle = _acquire_watchdog_state_lock(plan)
    try:
        lock_receipt = _verify_watchdog_state_lock(plan, handle)
        live_state = _watchdog_state_receipt(plan)
        if live_state != expected_state:
            raise DrainIdentityMismatch(
                "watchdog state changed before writer barrier"
            )
    except Exception:
        _release_watchdog_state_lock(plan, handle)
        raise
    return {
        "status": "held",
        "prepared": durable_prepared,
        "disabled": phases["watchdog_cron_disabled"],
        "reconciled": reconciled,
        "state": live_state,
        "lock": handle,
        "lock_receipt": lock_receipt,
    }


def _attest_release_watchdog_barrier(plan: dict, barrier: dict) -> dict:
    if not isinstance(barrier, dict) or barrier.get("status") != "held":
        raise ReleaseBuildError("watchdog release barrier receipt is invalid")
    handle = barrier.get("lock")
    prepared = barrier.get("prepared")
    expected_disabled = barrier.get("disabled")
    expected_state = barrier.get("state")
    expected_lock = barrier.get("lock_receipt")
    if (
        handle is None
        or not isinstance(prepared, dict)
        or not isinstance(expected_disabled, dict)
        or not isinstance(expected_state, dict)
        or not isinstance(expected_lock, dict)
    ):
        raise ReleaseBuildError("watchdog release barrier receipt is invalid")
    lock = _verify_watchdog_state_lock(plan, handle)
    if lock != expected_lock:
        raise DrainIdentityMismatch(
            "watchdog state lock changed while release barrier was held"
        )
    state = _watchdog_state_receipt(plan)
    if state != expected_state:
        raise DrainIdentityMismatch(
            "watchdog state changed while release barrier was held"
        )
    cron = _attest_disabled_watchdog_cron(plan, prepared)
    if cron != expected_disabled:
        raise DrainIdentityMismatch(
            "disabled watchdog scheduler changed while release barrier was held"
        )
    script = _file_identity_receipt(plan["watchdog_installed_script"])
    if (
        script.get("sha256") != plan["watchdog_expected_sha256"]
        or int(script.get("resolved_mode", 0)) & 0o111 == 0
    ):
        raise DrainIdentityMismatch("managed watchdog identity changed")
    return {
        "status": "verified-disabled-barrier",
        "script": script,
        "cron": cron,
        "state": state,
        "lock": lock,
    }


def _finish_release_watchdog_barrier(plan: dict, barrier: dict) -> dict:
    handle = barrier.get("lock") if isinstance(barrier, dict) else None
    prepared = barrier.get("prepared") if isinstance(barrier, dict) else None
    expected_state = barrier.get("state") if isinstance(barrier, dict) else None
    disabled_receipt = barrier.get("disabled") if isinstance(barrier, dict) else None
    if handle is None or not isinstance(prepared, dict) or not isinstance(
        expected_state,
        dict,
    ):
        raise ReleaseBuildError("watchdog release barrier receipt is invalid")
    outcome: dict
    try:
        _verify_watchdog_state_lock(plan, handle)
        journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        phases = journal["phases"]
        observed_state = _watchdog_state_receipt(plan)
        state_outcome = {
            "status": "unchanged",
            "before": expected_state,
            "after": observed_state,
        }
        if observed_state != expected_state:
            snapshot = phases.get("paired_state_snapshot_created")
            restored = phases.get("state_snapshot_restored")
            verified = phases.get("rollback_verified")
            if (
                not isinstance(snapshot, dict)
                or not isinstance(restored, dict)
                or restored.get("status") != "restored"
                or not isinstance(verified, dict)
                or verified.get("status") != "verified"
                or any(
                    receipt.get("state_snapshot_id")
                    != snapshot.get("state_snapshot_id")
                    or receipt.get("state_snapshot_sha256")
                    != snapshot.get("state_snapshot_sha256")
                    for receipt in (restored, verified)
                )
            ):
                raise DrainIdentityMismatch(
                    "watchdog state changed while release barrier was held"
                )
            _read_verified_state_snapshot(
                snapshot["manifest_path"],
                expected_snapshot_id=snapshot["state_snapshot_id"],
                expected_manifest_sha256=snapshot[
                    "state_snapshot_sha256"
                ],
                live=True,
            )
            if _watchdog_state_receipt(plan) != observed_state:
                raise DrainIdentityMismatch(
                    "watchdog rollback state changed during verification"
                )
            state_outcome = {
                "status": "restored-by-exact-rollback",
                "before": expected_state,
                "after": observed_state,
                "state_snapshot_id": snapshot["state_snapshot_id"],
                "state_snapshot_sha256": snapshot[
                    "state_snapshot_sha256"
                ],
            }
        if "pair_opened" in phases:
            restore_intent = _watchdog_cron_restore_intent_receipt(
                plan,
                prepared,
                disabled_receipt,
            )
            if "watchdog_cron_restore_intent" not in phases:
                journal = record_transaction_phase(
                    plan["transaction_journal"],
                    transaction_id=plan["transaction_id"],
                    phase="watchdog_cron_restore_intent",
                    receipt=restore_intent,
                )
                phases = journal["phases"]
            elif phases["watchdog_cron_restore_intent"] != restore_intent:
                raise DrainIdentityMismatch(
                    "watchdog cron restore intent changed on resume"
                )
            cron = _restore_watchdog_cron(plan, prepared)
            if "watchdog_cron_restored" not in phases:
                journal = record_transaction_phase(
                    plan["transaction_journal"],
                    transaction_id=plan["transaction_id"],
                    phase="watchdog_cron_restored",
                    receipt=cron,
                )
                phases = journal["phases"]
            elif phases["watchdog_cron_restored"] != cron:
                raise DrainIdentityMismatch(
                    "restored watchdog cron receipt changed on resume"
                )
            outcome = {
                "status": "restored-after-pair-opened",
                "cron": phases["watchdog_cron_restored"],
            }
        elif "pair_commit_intent" in phases:
            observed = _attest_disabled_watchdog_cron(plan, prepared)
            if observed != disabled_receipt:
                raise DrainIdentityMismatch(
                    "watchdog cron changed after durable pair commit"
                )
            outcome = {
                "status": "disabled-for-roll-forward",
                "cron": observed,
            }
        elif "rollback_started" in phases:
            if "rollback_verified" not in phases:
                observed = _attest_disabled_watchdog_cron(plan, prepared)
                if observed != disabled_receipt:
                    raise DrainIdentityMismatch(
                        "watchdog cron changed during incomplete rollback"
                    )
                outcome = {
                    "status": "disabled-for-rollback-resume",
                    "cron": observed,
                }
            else:
                cron = _restore_watchdog_cron(plan, prepared)
                if "watchdog_cron_rollback_restored" not in phases:
                    journal = record_transaction_phase(
                        plan["transaction_journal"],
                        transaction_id=plan["transaction_id"],
                        phase="watchdog_cron_rollback_restored",
                        receipt=cron,
                    )
                    phases = journal["phases"]
                elif phases["watchdog_cron_rollback_restored"] != cron:
                    raise DrainIdentityMismatch(
                        "rollback watchdog cron receipt changed on resume"
                    )
                outcome = {
                    "status": "restored-after-rollback",
                    "cron": phases["watchdog_cron_rollback_restored"],
                }
        else:
            outcome = {
                "status": "restored-before-cutover",
                "cron": _restore_watchdog_cron(plan, prepared),
            }
    finally:
        release = _release_watchdog_state_lock(plan, handle)
    return {**outcome, "state": state_outcome, "lock": release}


def _prove_no_mutable_writers(
    plan: dict,
    *,
    expected: dict | None = None,
) -> dict:
    """Prove every writable handle is either absent or exactly STOP-frozen."""
    writers: set[int] = set()
    checked: list[str] = []
    sqlite_wal: list[dict] = []
    for raw_path in plan["mutable_state_paths"]:
        path = Path(raw_path)
        if path.suffix == ".db":
            wal_path = Path(f"{path}-wal")
            try:
                opened = os.lstat(wal_path)
            except FileNotFoundError:
                sqlite_wal.append(
                    {"path": str(wal_path), "status": "absent"}
                )
            except OSError as exc:
                raise ReleaseBuildError(
                    "mutable SQLite WAL identity is unavailable"
                ) from exc
            else:
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or opened.st_nlink != 1
                ):
                    raise ReleaseBuildError(
                        "mutable SQLite WAL identity is unsafe"
                    )
                if opened.st_size != 0:
                    raise ReleaseBuildError(
                        f"mutable SQLite WAL is not checkpointed: {wal_path}"
                    )
                sqlite_wal.append(
                    {
                        "path": str(wal_path),
                        "status": "empty",
                        "device": opened.st_dev,
                        "inode": opened.st_ino,
                        "mode": stat.S_IMODE(opened.st_mode),
                        "uid": opened.st_uid,
                        "nlink": opened.st_nlink,
                        "size": opened.st_size,
                    }
                )
        if not path.exists():
            checked.append(str(path))
            continue
        arguments = ["lsof", "-nP", "-Fpa", "--", str(path)]
        if path.is_dir():
            arguments = ["lsof", "-nP", "-Fpa", "+D", str(path)]
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseBuildError("mutable writer probe failed") from exc
        if completed.returncode not in {0, 1}:
            raise ReleaseBuildError("mutable writer probe is indeterminate")
        current_pid: int | None = None
        for line in completed.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current_pid = int(line[1:])
            elif line in {"aw", "au"} and current_pid is not None:
                writers.add(current_pid)
        checked.append(str(path))
    writers.discard(os.getpid())
    stopped_writers: list[dict] = []
    runnable_writers: list[int] = []
    for pid in sorted(writers):
        start_token = _pid_start_token(pid)
        if start_token is None:
            raise DrainIdentityMismatch(
                f"mutable writer identity is unavailable for PID {pid}"
            )
        state = _ps_value(pid, "state")
        if _pid_start_token(pid) != start_token:
            raise DrainIdentityMismatch(
                f"mutable writer identity changed for PID {pid}"
            )
        if not state.upper().strip().startswith("T"):
            runnable_writers.append(pid)
            continue
        receipt = {
            "pid": pid,
            "pid_start_token": start_token,
            "state": "stopped",
        }
        if (
            not _exact_process_is_alive(receipt)
            or not _ps_value(pid, "state").upper().strip().startswith("T")
        ):
            raise DrainIdentityMismatch(
                f"mutable writer resumed during barrier for PID {pid}"
            )
        stopped_writers.append(receipt)
    if runnable_writers:
        raise ReleaseBuildError(
            "mutable state still has runnable writable handles: "
            + ",".join(str(pid) for pid in runnable_writers)
        )
    receipt = {
        "status": "verified",
        "paths": checked,
        "sqlite_wal": sqlite_wal,
        "writer_pids": [row["pid"] for row in stopped_writers],
        "stopped_writers": stopped_writers,
        "bounded_host_assumption": copy.deepcopy(
            _FIRST_ACTIVATION_BOUNDED_HOST_ASSUMPTION
        ),
    }
    if expected is not None and receipt != expected:
        raise DrainIdentityMismatch("mutable writer barrier changed")
    return receipt


def _stop_current_service(
    plan: dict,
    *,
    gateway: bool,
    authorized_receipts: list[dict] | None = None,
) -> dict:
    authorized_receipts = authorized_receipts or []
    port_key = "gateway_listener_port" if gateway else "listener_port"
    job_pid = _job_pid(plan, gateway=gateway)
    try:
        listener_pid = _listener_pid(int(plan[port_key]))
    except DrainIdentityMismatch:
        listener_pid = None
    if job_pid is not None and listener_pid is not None and job_pid != listener_pid:
        raise DrainIdentityMismatch("current launchd/listener binding is ambiguous")
    identity = None
    if listener_pid is not None:
        start = _pid_start_token(listener_pid)
        if not start:
            raise DrainIdentityMismatch("current listener start identity is unavailable")
        identity = {"pid": listener_pid, "pid_start_token": start}
        require_git_source = not gateway and any(
            isinstance(receipt, dict) and "source" in receipt
            for receipt in authorized_receipts
        )
        actual_runtime = _listener_process_receipt(
            plan,
            gateway=gateway,
            require_git_source=require_git_source,
        )
        authorized = any(
            int(receipt.get("pid", -1)) == listener_pid
            and str(receipt.get("pid_start_token") or "") == start
            and _runtime_receipt_matches(actual_runtime, receipt)
            for receipt in authorized_receipts
            if isinstance(receipt, dict)
        )
        if not authorized:
            raise DrainIdentityMismatch(
                "current listener has no durable stop authorization"
            )
    elif job_pid is not None:
        job_start = _pid_start_token(job_pid)
        if not any(
            int(receipt.get("pid", -1)) == job_pid
            and str(receipt.get("pid_start_token") or "") == str(job_start or "")
            for receipt in authorized_receipts
            if isinstance(receipt, dict)
        ):
            raise DrainIdentityMismatch(
                "listenerless launchd process has no durable stop authorization"
            )
    bootout = _bootout_job(plan, gateway=gateway, required=False)
    if identity is not None and _exact_process_is_alive(identity):
        os.kill(int(identity["pid"]), signal.SIGKILL)
        wait_for_exact_process_exit(
            identity,
            float(plan["timeout_seconds"]),
            allow_exact_signaled_zombie=True,
        )
    try:
        replacement = _listener_pid(int(plan[port_key]))
    except DrainIdentityMismatch:
        replacement = None
    if replacement is not None or _job_pid(plan, gateway=gateway) is not None:
        raise DrainIdentityMismatch("current service failed to stop exactly")
    return {"status": "stopped", "identity": identity, "bootout": bootout}


def _authorized_bootstrap_runtimes(journal: dict, *, gateway: bool) -> list[dict]:
    phases = journal.get("phases", {}) if isinstance(journal, dict) else {}
    prepared = phases.get("prepared", {})
    receipts: list[dict] = []
    original = prepared.get("gateway" if gateway else "legacy")
    if isinstance(original, dict):
        receipts.append(original)
    managed = phases.get("managed_pair_started", {})
    if gateway:
        value = managed.get("gateway_binding", {}).get("runtime")
    else:
        value = managed.get("managed_runtime")
    if isinstance(value, dict):
        receipts.append(value)
    candidate = phases.get("candidate_pair_accepted", {})
    if gateway:
        value = candidate.get("gateway_binding", {}).get("runtime")
    else:
        value = candidate.get("candidate_runtime")
    if isinstance(value, dict):
        receipts.append(value)
    return receipts


def _authorized_cutover_runtimes(plan: dict, *, gateway: bool) -> list[dict]:
    try:
        journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
    except ReleaseBuildError:
        return []
    phases = journal.get("phases", {})
    candidates: list[object]
    if gateway:
        candidates = [
            phases.get("gateway_last_good_attested", {})
            .get("binding", {})
            .get("runtime"),
            phases.get("candidate_gateway_accepted", {})
            .get("binding", {})
            .get("runtime"),
        ]
    else:
        candidates = [
            phases.get("replacement_proved", {})
            .get("binding", {})
            .get("runtime"),
            phases.get("accepted_health_proved", {})
            .get("binding", {})
            .get("runtime"),
            phases.get("promoted", {})
            .get("promotion", {})
            .get("binding", {})
            .get("runtime"),
        ]
    return [value for value in candidates if isinstance(value, dict)]


def _incomplete_managed_webui_stop_authorization(
    plan: dict,
    journal: dict,
) -> dict | None:
    """Reconstruct an exact stop receipt after start but before its journal phase."""
    phases = journal.get("phases", {}) if isinstance(journal, dict) else {}
    if "managed_pair_started" in phases:
        return None
    intent = phases.get("managed_pair_start_intent")
    if not isinstance(intent, dict):
        return None
    candidate = plan.get("expected_candidate_identity")
    last_good = plan.get("last_good_identity")
    selection = intent.get("selection")
    install = intent.get("webui_install")
    if (
        not isinstance(candidate, dict)
        or not isinstance(last_good, dict)
        or not isinstance(selection, dict)
        or not isinstance(install, dict)
        or intent.get("build_id") != candidate.get("build_id")
        or selection.get("generation") != candidate.get("selector_generation")
        or selection.get("current") != candidate.get("build_id")
        or selection.get("candidate") != candidate.get("build_id")
        or selection.get("pending_transaction_id") != plan.get("transaction_id")
        or selection.get("last_good") != last_good.get("build_id")
    ):
        raise ReleaseBuildError(
            "incomplete managed WebUI start intent is invalid"
        )
    current_selection = release_selector.read_selector_state(
        plan["selector_state"],
        lock_path=plan["selector_lock"],
    )
    if current_selection != selection:
        raise DrainIdentityMismatch(
            "incomplete managed WebUI selector state changed"
        )
    if sha256_file(Path(plan["installed_plist"])) != install.get("sha256"):
        raise DrainIdentityMismatch(
            "incomplete managed WebUI install identity changed"
        )
    binding = _probe_startup_fenced_webui_binding(plan, candidate)
    if binding is None:
        return None
    runtime = binding.get("runtime")
    if not isinstance(runtime, dict):
        raise DrainIdentityMismatch(
            "incomplete managed WebUI runtime identity is missing"
        )
    return runtime


def _stop_current_pair(plan: dict, journal: dict | None = None) -> dict:
    source = journal or {}

    def stop(gateway: bool) -> dict:
        authorized = _authorized_bootstrap_runtimes(source, gateway=gateway)
        authorized.extend(_authorized_cutover_runtimes(plan, gateway=gateway))
        try:
            return _stop_current_service(
                plan,
                gateway=gateway,
                authorized_receipts=authorized,
            )
        except DrainIdentityMismatch as original:
            intent = source.get("phases", {}).get("managed_pair_start_intent")
            if not isinstance(intent, dict):
                raise
            if gateway:
                binding = _attest_managed_gateway_binding(
                    plan,
                    plan["last_good_gateway_identity"],
                )
            else:
                binding = _probe_managed_webui_binding(
                    plan,
                    plan["last_good_identity"],
                )
                if binding is None:
                    raise original
            runtime = binding.get("runtime")
            if not isinstance(runtime, dict):
                raise original
            return _stop_current_service(
                plan,
                gateway=gateway,
                authorized_receipts=[runtime],
            )

    return {"webui": stop(False), "gateway": stop(True)}


def _restore_exact_backup(backup: Path, destination: Path, receipt: dict) -> dict:
    if _file_identity_receipt(backup)["sha256"] != receipt.get("sha256"):
        raise ReleaseBuildError("bootstrap rollback backup identity changed")
    copied = _atomic_copy_file(
        backup,
        destination,
        expected_sha256=str(receipt["sha256"]),
        mode=int(receipt["resolved_mode"]),
    )
    os.chown(destination, int(receipt["resolved_uid"]), -1)
    _fsync_directory(destination.parent)
    restored = _file_identity_receipt(destination)
    for key in ("sha256", "resolved_mode", "resolved_uid", "resolved_size"):
        if restored.get(key) != receipt.get(key):
            raise ReleaseBuildError("bootstrap rollback artifact is not exact")
    return copied


def _runtime_receipt_matches(
    actual: dict,
    expected: dict,
    *,
    require_cwd: bool = True,
) -> bool:
    keys = {
        "command",
        "comm",
        "program_arguments",
        "program_identity",
    }
    if require_cwd:
        keys.add("cwd")
    if "source" in expected:
        keys.update({"source", "routing_environment"})
    return all(actual.get(key) == expected.get(key) for key in keys)


def _attest_restored_legacy_binding(
    plan: dict,
    *,
    prepared: dict,
    gateway: bool,
) -> dict:
    if _BOOTSTRAP_SPLIT_PROVENANCE_PLAN_KEYS.issubset(plan):
        _validate_bootstrap_split_provenance(plan, prepared)
    expected = prepared["gateway" if gateway else "legacy"]
    actual = _listener_process_receipt(
        plan,
        gateway=gateway,
        require_git_source=not gateway,
    )
    if not _runtime_receipt_matches(
        actual,
        expected,
        # The legacy gateway may chdir to a selected workspace after launch.
        # Its command, executable and argv are stable rollback authority; its
        # live cwd is not. WebUI keeps cwd plus the exact git-source receipt.
        require_cwd=not gateway,
    ):
        raise ReleaseBuildError("restored legacy runtime identity changed")
    if gateway:
        health = _gateway_health_receipt(plan)
    else:
        health_body = _http_json(
            f"{str(plan['base_url']).rstrip('/')}/health",
            timeout_seconds=max(30.0, float(plan["timeout_seconds"])),
        )
        if health_body.get("status") != "ok":
            raise ReleaseBuildError("restored legacy WebUI is unhealthy")
        health = {
            "status": "ok",
            "body_sha256": hashlib.sha256(
                json.dumps(
                    health_body,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
    return {
        "status": "verified",
        "pid": actual["pid"],
        "pid_start_token": actual["pid_start_token"],
        "runtime": actual,
        "health": health,
    }


def _restored_legacy_runtime_authorization(
    plan: dict,
    *,
    prepared: dict,
    gateway: bool,
) -> dict:
    """Authorize a newly started process only from exact legacy artifacts."""
    installed_key = "gateway_installed_plist" if gateway else "installed_plist"
    receipt_key = "gateway_plist" if gateway else "webui_plist"
    expected_plist = prepared.get(receipt_key)
    if not isinstance(expected_plist, dict):
        raise ReleaseBuildError(
            "restored legacy stop authorization has no plist receipt"
        )
    actual_plist = _file_identity_receipt(plan[installed_key])
    for key in ("sha256", "resolved_mode", "resolved_uid", "resolved_size"):
        if actual_plist.get(key) != expected_plist.get(key):
            raise DrainIdentityMismatch(
                "restored legacy stop authorization plist changed"
            )
    binding = _attest_restored_legacy_binding(
        plan,
        prepared=prepared,
        gateway=gateway,
    )
    runtime = binding.get("runtime")
    if not isinstance(runtime, dict):
        raise DrainIdentityMismatch(
            "restored legacy stop authorization runtime is missing"
        )
    return runtime


def _wait_for_legacy_binding(
    plan: dict,
    *,
    prepared: dict,
    gateway: bool,
) -> dict:
    deadline = time.monotonic() + float(plan["timeout_seconds"])
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _attest_restored_legacy_binding(
                plan,
                prepared=prepared,
                gateway=gateway,
            )
        except Exception as exc:
            last_error = exc
            time.sleep(float(plan["interval_seconds"]))
    raise DrainTimeout(f"restored legacy binding timed out: {last_error}")


def _restart_or_adopt_restored_legacy_pair(
    plan: dict,
    *,
    prepared: dict,
) -> dict:
    try:
        gateway_binding = _attest_restored_legacy_binding(
            plan,
            prepared=prepared,
            gateway=True,
        )
        webui_binding = _attest_restored_legacy_binding(
            plan,
            prepared=prepared,
            gateway=False,
        )
    except BootstrapSplitProvenanceMismatch:
        raise
    except (DrainIdentityMismatch, ReleaseBuildError):
        _bootout_job(plan, gateway=True, required=False)
        gateway_started = _bootstrap_job(
            plan,
            plan["gateway_installed_plist"],
            gateway=True,
        )
        _wait_for_legacy_binding(plan, prepared=prepared, gateway=True)
        _bootout_job(plan, gateway=False, required=False)
        webui_started = _bootstrap_job(
            plan,
            plan["installed_plist"],
            gateway=False,
        )
        gateway_binding = _wait_for_legacy_binding(
            plan,
            prepared=prepared,
            gateway=True,
        )
        webui_binding = _wait_for_legacy_binding(
            plan,
            prepared=prepared,
            gateway=False,
        )
    else:
        gateway_started = {
            "status": "adopted-exact-restored-binding",
            "pid": gateway_binding["pid"],
            "pid_start_token": gateway_binding["pid_start_token"],
        }
        webui_started = {
            "status": "adopted-exact-restored-binding",
            "pid": webui_binding["pid"],
            "pid_start_token": webui_binding["pid_start_token"],
        }
    return {
        "gateway_start": gateway_started,
        "webui_start": webui_started,
        "gateway_binding": gateway_binding,
        "webui_binding": webui_binding,
    }


def _rollback_gateway_stop_intent(
    plan: dict,
    *,
    prepared: dict,
    legacy_drain_intent: dict | None,
) -> dict:
    try:
        listener = _listener_pid(int(plan["gateway_listener_port"]))
    except DrainIdentityMismatch:
        listener = None
    job = _job_pid(plan, gateway=True)
    if listener is None and job is None:
        return {"status": "not-running"}
    if listener is None or job != listener:
        raise DrainIdentityMismatch(
            "rollback gateway launchd/listener boundary is ambiguous"
        )
    installed_sha256 = sha256_file(Path(plan["gateway_installed_plist"]))
    rollback_sha256 = sha256_file(Path(plan["gateway_rollback_plist"]))
    managed_sha256 = sha256_file(Path(plan["managed_gateway_plist"]))
    if installed_sha256 == rollback_sha256:
        if not isinstance(legacy_drain_intent, dict):
            raise ReleaseBuildError(
                "restored legacy gateway has no durable drain intent"
            )
        binding = _attest_restored_legacy_binding(
            plan,
            prepared=prepared,
            gateway=True,
        )
        live_prepared = {
            "gateway": {
                "pid": int(binding["pid"]),
                "pid_start_token": str(binding["pid_start_token"]),
            }
        }
        drained = _wait_for_legacy_gateway_drain(
            plan,
            live_prepared,
            legacy_drain_intent,
        )
        return {
            "status": "prepared",
            "runtime_mode": "restored-legacy",
            "prepared": live_prepared,
            "drain_intent": copy.deepcopy(legacy_drain_intent),
            "drain_receipt": drained,
            "intent": _legacy_gateway_stop_intent_receipt(
                plan,
                live_prepared,
                drained,
            ),
        }
    if installed_sha256 != managed_sha256:
        raise DrainIdentityMismatch(
            "rollback gateway plist is neither managed nor restored legacy"
        )
    binding = _attest_managed_gateway_binding(
        plan,
        plan["expected_candidate_identity"],
        expected_admission="rejecting_new_work",
    )
    prepared = {
        "gateway": {
            "pid": int(binding["listener_pid"]),
            "pid_start_token": str(binding["pid_start_token"]),
        }
    }
    drain_receipt = {
        "status": "verified",
        "binding": binding,
        "admission": "rejecting_new_work",
    }
    return {
        "status": "prepared",
        "runtime_mode": "managed-candidate",
        "prepared": prepared,
        "intent": _legacy_gateway_stop_intent_receipt(
            plan,
            prepared,
            drain_receipt,
        ),
    }


def _stop_bootstrap_pair_for_rollback(
    plan: dict,
    journal: dict,
    gateway_stop_intent: dict,
) -> dict:
    cron_tick_lock = _acquire_legacy_cron_tick_lock(plan)
    _verify_legacy_cron_tick_lock(plan, cron_tick_lock)
    if gateway_stop_intent.get("status") == "not-running":
        try:
            listener = _listener_pid(int(plan["gateway_listener_port"]))
        except DrainIdentityMismatch:
            listener = None
        if listener is not None or _job_pid(plan, gateway=True) is not None:
            raise DrainIdentityMismatch(
                "gateway appeared after rollback stop intent"
            )
        gateway = {"status": "already-stopped"}
    else:
        prepared = gateway_stop_intent.get("prepared")
        intent = gateway_stop_intent.get("intent")
        if not isinstance(prepared, dict) or not isinstance(intent, dict):
            raise ReleaseBuildError("rollback gateway stop intent is invalid")
        gateway = _gracefully_stop_legacy_gateway(
            plan,
            prepared,
            intent,
        )

    authorized = _authorized_bootstrap_runtimes(journal, gateway=False)
    authorized.extend(_authorized_cutover_runtimes(plan, gateway=False))
    if _listener_pid_or_none(int(plan["listener_port"])) is not None:
        installed_sha256 = sha256_file(Path(plan["installed_plist"]))
        rollback_sha256 = sha256_file(Path(plan["bootstrap_rollback_plist"]))
        managed_sha256 = sha256_file(Path(plan["managed_plist"]))
        if installed_sha256 == rollback_sha256:
            authorized.append(
                _restored_legacy_runtime_authorization(
                    plan,
                    prepared=journal["phases"]["prepared"],
                    gateway=False,
                )
            )
        elif installed_sha256 != managed_sha256:
            raise DrainIdentityMismatch(
                "rollback WebUI plist is neither managed nor restored legacy"
            )
    incomplete_runtime = _incomplete_managed_webui_stop_authorization(
        plan,
        journal,
    )
    if incomplete_runtime is not None:
        authorized.append(incomplete_runtime)
    webui = _stop_current_service(
        plan,
        gateway=False,
        authorized_receipts=authorized,
    )
    _verify_legacy_cron_tick_lock(plan, cron_tick_lock)
    return {
        "webui": webui,
        "gateway": gateway,
        "cron_tick_lock": cron_tick_lock,
    }


def _claim_bootstrap_rollback(plan: dict, journal: dict) -> dict:
    phases = journal.get("phases") if isinstance(journal, dict) else None
    if not isinstance(phases, dict):
        raise ReleaseBuildError("bootstrap rollback journal phases are invalid")
    prepared = phases.get("prepared")
    snapshot = phases.get("snapshot_created")
    if not isinstance(prepared, dict) or not isinstance(snapshot, dict):
        raise ReleaseBuildError(
            "bootstrap rollback claim requires prepared split and snapshot"
        )
    provenance = prepared.get("last_good_split_provenance")
    webui = provenance.get("webui") if isinstance(provenance, dict) else None
    identity = webui.get("identity") if isinstance(webui, dict) else None
    webui_plist = prepared.get("webui_plist")
    plist_mode = (
        webui_plist.get("resolved_mode")
        if isinstance(webui_plist, dict)
        else None
    )
    if (
        isinstance(plist_mode, bool)
        or not isinstance(plist_mode, int)
        or plist_mode <= 0
        or plist_mode != stat.S_IMODE(plist_mode)
    ):
        raise ReleaseBuildError(
            "bootstrap rollback claim WebUI plist mode is invalid"
        )
    legacy = prepared.get("legacy")
    legacy_cli = legacy.get("cli") if isinstance(legacy, dict) else None
    cli_link_target = (
        legacy_cli.get("link_target")
        if isinstance(legacy_cli, dict)
        else None
    )
    planned_cli_link_target = plan.get("cli_old_target")
    if (
        not isinstance(cli_link_target, str)
        or not cli_link_target
        or not isinstance(planned_cli_link_target, str)
        or not planned_cli_link_target
        or cli_link_target != planned_cli_link_target
    ):
        raise ReleaseBuildError(
            "bootstrap rollback claim CLI link target is invalid"
        )
    rollback_receipt = {
        "build_id": (
            identity.get("build_id") if isinstance(identity, dict) else None
        ),
        "plist_sha256": (
            webui_plist.get("sha256")
            if isinstance(webui_plist, dict)
            else None
        ),
        "plist_mode": plist_mode,
        "cli_link_target": cli_link_target,
        "state_snapshot_id": snapshot.get("state_snapshot_id"),
        "state_snapshot_sha256": snapshot.get(
            "state_snapshot_sha256"
        ),
    }
    if not isinstance(provenance, dict):
        raise ReleaseBuildError(
            "bootstrap rollback claim split provenance is invalid"
        )
    claim = {
        "schema": "hermes.bootstrap_rollback_claim.v1",
        "bootstrap_transaction_id": plan["transaction_id"],
        "split_provenance_sha256": _canonical_journal_value_sha256(
            provenance
        ),
        "split_evidence_sha256": provenance.get(
            "split_evidence_sha256"
        ),
        "rollback_receipt": copy.deepcopy(rollback_receipt),
    }
    return record_transaction_phase(
        plan["transaction_journal"],
        transaction_id=plan["transaction_id"],
        phase="bootstrap_rollback_claimed",
        receipt=claim,
        initialize_if_absent={
            "expected_candidate_identity": copy.deepcopy(
                plan["expected_candidate_identity"]
            ),
            "rollback_receipt": rollback_receipt,
        },
    )


def _resume_bootstrap_rollback(plan: dict, journal: dict) -> dict:
    phases = journal["phases"]
    _claim_bootstrap_rollback(plan, journal)
    prepared = _upgrade_internal_watchdog_prepared_receipt(
        plan,
        phases["prepared"],
    )
    _validate_bootstrap_split_provenance(plan, prepared)
    if "rollback_gateway_stop_intent" not in phases:
        journal = _record_bootstrap_phase(
            plan,
            "rollback_gateway_stop_intent",
            _rollback_gateway_stop_intent(
                plan,
                prepared=prepared,
                legacy_drain_intent=phases.get(
                    "legacy_gateway_drain_intent"
                ),
            ),
        )
        phases = journal["phases"]
    if "rollback_services_stopped" not in phases:
        if (
            "ingress_gate_started" in phases
            and "ingress_gate_stopped" not in phases
        ):
            gate_stop = _stop_ingress_gate(
                plan,
                phases["ingress_gate_started"],
            )
        else:
            gate_stop = {"status": "not-running"}
        journal = _record_bootstrap_phase(
            plan,
            "rollback_services_stopped",
            {
                "ingress_gate": gate_stop,
                "services": _stop_bootstrap_pair_for_rollback(
                    plan,
                    journal,
                    phases["rollback_gateway_stop_intent"],
                ),
            },
        )
        phases = journal["phases"]
    if "rollback_cron_tick_lock_released" not in phases:
        journal = _record_bootstrap_phase(
            plan,
            "rollback_cron_tick_lock_released",
            _release_legacy_cron_tick_lock(plan),
        )
        phases = journal["phases"]
    if "rollback_dispatcher_lock_acquired" not in phases:
        journal = _record_bootstrap_phase(
            plan,
            "rollback_dispatcher_lock_acquired",
            _acquire_legacy_dispatcher_lock(plan),
        )
        phases = journal["phases"]
    else:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["rollback_dispatcher_lock_acquired"],
        )
    if "rollback_workers_quiescent" not in phases:
        journal = _record_bootstrap_phase(
            plan,
            "rollback_workers_quiescent",
            _wait_for_legacy_kanban_quiescence(plan),
        )
        phases = journal["phases"]
    else:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["rollback_dispatcher_lock_acquired"],
        )
        _wait_for_legacy_kanban_quiescence(plan)
    if "rollback_state_restored" not in phases:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["rollback_dispatcher_lock_acquired"],
        )
        _wait_for_legacy_kanban_quiescence(plan)
        snapshot = phases.get("snapshot_created")
        if snapshot is None:
            state_receipt = {"status": "not-required"}
        else:
            state_receipt = restore_state_snapshot_from_manifest(
                plan["snapshot_manifest"],
                expected_snapshot_id=snapshot["state_snapshot_id"],
                expected_manifest_sha256=snapshot["state_snapshot_sha256"],
            )
        journal = _record_bootstrap_phase(
            plan,
            "rollback_state_restored",
            state_receipt,
        )
        phases = journal["phases"]
    if "rollback_synthetic_state_requarantined" not in phases:
        quarantine_intent = phases.get("synthetic_state_quarantine_intent")
        if quarantine_intent is None:
            quarantine_intent = _synthetic_quarantine_intent_receipt(plan)
        journal = _record_bootstrap_phase(
            plan,
            "rollback_synthetic_state_requarantined",
            _quarantine_synthetic_completion_stores(
                plan,
                quarantine_intent,
                state_restore=phases["rollback_state_restored"],
            ),
        )
        phases = journal["phases"]
    if "rollback_plists_restored" not in phases:
        restored_plists = {
            "webui": _restore_exact_backup(
                Path(plan["bootstrap_rollback_plist"]),
                Path(plan["installed_plist"]),
                prepared["webui_plist"],
            ),
            "gateway": _restore_exact_backup(
                Path(plan["gateway_rollback_plist"]),
                Path(plan["gateway_installed_plist"]),
                prepared["gateway_plist"],
            ),
            "pre_managed_controls": _restore_pre_managed_control_state(
                plan,
                prepared["pre_managed_controls"],
                phases["pre_managed_controls_staged"],
            ),
        }
        journal = _record_bootstrap_phase(
            plan,
            "rollback_plists_restored",
            restored_plists,
        )
        phases = journal["phases"]
    if "rollback_watchdog_restored" not in phases:
        watchdog = _restore_exact_backup(
            Path(plan["watchdog_rollback_script"]),
            Path(plan["watchdog_installed_script"]),
            prepared["watchdog"],
        )
        cli = _restore_bootstrap_cli_link(
            plan,
            prepared,
            phases,
        )
        cron = _attest_disabled_watchdog_cron(plan, prepared)
        journal = _record_bootstrap_phase(
            plan,
            "rollback_watchdog_restored",
            {"watchdog": watchdog, "cli": cli, "cron": cron},
        )
        phases = journal["phases"]
    if "rollback_gateway_drain_cleared" not in phases:
        drain_intent = phases.get("legacy_gateway_drain_intent")
        if isinstance(drain_intent, dict):
            cleared = _clear_legacy_gateway_drain_marker(plan, drain_intent)
        else:
            cleared = {"status": "not-required"}
        journal = _record_bootstrap_phase(
            plan,
            "rollback_gateway_drain_cleared",
            cleared,
        )
        phases = journal["phases"]
    if "rollback_dispatcher_lock_released" not in phases:
        _verify_legacy_dispatcher_lock(
            plan,
            phases["rollback_dispatcher_lock_acquired"],
        )
        _wait_for_legacy_kanban_quiescence(plan)
        journal = _record_bootstrap_phase(
            plan,
            "rollback_dispatcher_lock_released",
            _release_legacy_dispatcher_lock(plan),
        )
        phases = journal["phases"]
    if "rollback_cron_tick_lock_restored" not in phases:
        tick_intent = phases.get(
            "legacy_cron_tick_lock_normalize_intent"
        )
        if isinstance(tick_intent, dict):
            tick_restore = _restore_legacy_cron_tick_lock(
                plan,
                tick_intent,
                phases.get("legacy_cron_tick_lock_normalized"),
                state_restore=phases.get("rollback_state_restored"),
            )
        else:
            tick_restore = {"status": "not-required"}
        journal = _record_bootstrap_phase(
            plan,
            "rollback_cron_tick_lock_restored",
            tick_restore,
        )
        phases = journal["phases"]
    if "rollback_synthetic_store_modes_restored" not in phases:
        store_intent = phases.get(
            "synthetic_store_mode_normalize_intent"
        )
        store_normalization = phases.get(
            "synthetic_store_modes_normalized"
        )
        if isinstance(store_intent, dict):
            store_restore = (
                _restore_synthetic_completion_store_modes(
                    plan,
                    store_intent,
                    (
                        store_normalization
                        if isinstance(store_normalization, dict)
                        else None
                    ),
                    quarantined=phases[
                        "rollback_synthetic_state_requarantined"
                    ],
                )
            )
        else:
            store_restore = {"status": "not-required"}
        journal = _record_bootstrap_phase(
            plan,
            "rollback_synthetic_store_modes_restored",
            store_restore,
        )
        phases = journal["phases"]
    if "rollback_services_restarted" not in phases:
        journal = _record_bootstrap_phase(
            plan,
            "rollback_services_restarted",
            _restart_or_adopt_restored_legacy_pair(
                plan,
                prepared=prepared,
            ),
        )
        phases = journal["phases"]
    if "rollback_cron_restored" not in phases:
        cron = _restore_watchdog_cron(plan, prepared)
        if not _cron_receipt_matches_prepared(cron, prepared):
            raise ReleaseBuildError("watchdog cron changed during rollback")
        journal = _record_bootstrap_phase(
            plan,
            "rollback_cron_restored",
            cron,
        )
        phases = journal["phases"]
    if "rollback_verified" not in phases:
        cli = _file_identity_receipt(plan["cli_link"])
        if cli != prepared["legacy"]["cli"]:
            raise ReleaseBuildError("rollback Hermes CLI identity changed")
        if not _cron_receipt_matches_prepared(
            _watchdog_receipt_for_prepared(plan, prepared),
            prepared,
        ):
            raise ReleaseBuildError("rollback watchdog cron identity changed")
        journal = _record_bootstrap_phase(
            plan,
            "rollback_verified",
            {
                "status": "verified",
                "cli": cli,
                "watchdog": _file_identity_receipt(
                    plan["watchdog_installed_script"]
                ),
            },
        )
    return journal


def _run_bootstrap_migration_plan(plan: dict, *, dry_run: bool = False) -> dict:
    _require_bootstrap_extensions(plan)
    if dry_run:
        return {
            "status": "dry-run",
            "transaction_id": plan["transaction_id"],
            "actions": [
                "attest-legacy-idle-and-identity",
                "backup-exact-plist-and-cli",
                "sigstop-webui-and-gateway-writer-barrier",
                "stage-candidate-cli-and-publish-maintenance-deny-gate",
                "capture-normalize-and-acquire-legacy-cron-tick-lock",
                "terminate-exact-webui-and-gateway-processes",
                "capture-and-normalize-synthetic-store-modes",
                "release-legacy-cron-tick-lock-after-services-stop",
                "bind-deny-all-ingress-gate",
                "snapshot-mutable-state",
                "quarantine-exact-terminal-synthetic-records-without-replay",
                "release-ingress-gate",
                "install-selector-plist-and-bootstrap-managed-pair",
                "prove-managed-last-good-and-gateway",
                "atomically-install-watchdog-and-reconcile-twice",
                "run-release-commit",
                "activate-public-candidate-cli-after-durable-pair-opened",
            ],
        }
    journal_path = _bootstrap_journal_path(plan)
    if journal_path.exists():
        journal = _read_bootstrap_journal(plan)
    else:
        prepared = _prepared_bootstrap_receipt(plan)
        journal = _record_bootstrap_phase(plan, "prepared", prepared)
    phases = journal["phases"]
    if "rollback_started" in phases:
        rolled_back = _resume_bootstrap_rollback(plan, journal)
        return {
            "status": "rolled-back",
            "transaction_id": plan["transaction_id"],
            "bootstrap_journal": str(journal_path),
            "rollback": rolled_back["phases"]["rollback_verified"],
        }
    if "complete" in phases:
        _attest_cli_link(
            plan,
            plan["expected_candidate_identity"],
        )
        return {
            "status": "accepted",
            "transaction_id": plan["transaction_id"],
            "bootstrap_journal": str(journal_path),
            "receipt": phases["complete"],
        }
    if "aborted_before_cutover" in phases:
        return {
            "status": "aborted",
            "transaction_id": plan["transaction_id"],
            "bootstrap_journal": str(journal_path),
            "receipt": phases["aborted_before_cutover"],
        }
    prepared = _upgrade_internal_watchdog_prepared_receipt(
        plan,
        phases["prepared"],
    )
    try:
        if "pre_managed_controls_stage_intent" not in phases:
            journal = _record_bootstrap_phase(
                plan,
                "pre_managed_controls_stage_intent",
                _pre_managed_control_stage_intent_receipt(plan, prepared),
            )
            phases = journal["phases"]
        stage_intent = phases["pre_managed_controls_stage_intent"].get(
            "expected"
        )
        if not isinstance(stage_intent, dict):
            raise ReleaseBuildError("pre-managed control stage intent is invalid")
        if "pre_managed_controls_staged" not in phases:
            staged_controls = _stage_pre_managed_controls(
                plan,
                prepared,
            )
            if staged_controls != stage_intent:
                raise DrainIdentityMismatch(
                    "pre-managed control stage changed before commit"
                )
            journal = _record_bootstrap_phase(
                plan,
                "pre_managed_controls_staged",
                staged_controls,
            )
            phases = journal["phases"]
        else:
            if phases["pre_managed_controls_staged"] != stage_intent:
                raise DrainIdentityMismatch(
                    "pre-managed control state changed on bootstrap resume"
                )
            _adopt_or_restage_pre_managed_controls(
                plan,
                prepared,
                stage_intent,
            )
        if "watchdog_cron_disabled" not in phases:
            journal = _record_bootstrap_phase(
                plan,
                "watchdog_cron_disabled",
                _disable_watchdog_cron(plan, prepared),
            )
            phases = journal["phases"]
        else:
            disabled_cron = _attest_disabled_watchdog_cron(plan, prepared)
            if disabled_cron != phases["watchdog_cron_disabled"]:
                raise ReleaseBuildError("watchdog cron barrier identity changed")
        if "synthetic_state_quarantined" not in phases:
            watchdog_lock = _acquire_watchdog_state_lock(plan)
            try:
                if (
                    "legacy_dispatcher_lock_acquired" in phases
                    and "legacy_dispatcher_lock_released" not in phases
                ):
                    _verify_legacy_dispatcher_lock(
                        plan,
                        phases["legacy_dispatcher_lock_acquired"],
                    )
                if "services_stopped" not in phases:
                    if "writers_frozen" not in phases:
                        frozen = _freeze_prepared_writers(plan, prepared)
                        journal = _record_bootstrap_phase(
                            plan,
                            "writers_frozen",
                            frozen,
                        )
                        phases = journal["phases"]
                    else:
                        frozen = _verify_frozen_prepared_writers(
                            plan,
                            prepared,
                            phases["writers_frozen"],
                        )
                    try:
                        if "cli_maintenance_gate_stage_intent" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "cli_maintenance_gate_stage_intent",
                                _bootstrap_cli_gate_stage_intent_receipt(
                                    plan,
                                    prepared,
                                ),
                            )
                            phases = journal["phases"]
                        cli_gate_intent = phases[
                            "cli_maintenance_gate_stage_intent"
                        ]
                        observed_cli_gate = (
                            _install_or_adopt_bootstrap_cli_gate(
                                plan,
                                cli_gate_intent,
                            )
                        )
                        if "cli_maintenance_gate_installed" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "cli_maintenance_gate_installed",
                                observed_cli_gate,
                            )
                            phases = journal["phases"]
                        else:
                            durable_cli_gate = phases[
                                "cli_maintenance_gate_installed"
                            ]
                            if any(
                                observed_cli_gate.get(key)
                                != durable_cli_gate.get(key)
                                for key in (
                                    "transaction_id",
                                    "link_path",
                                    "target",
                                    "target_sha256",
                                    "bounded_host_assumption",
                                )
                            ):
                                raise DrainIdentityMismatch(
                                    "durable Hermes CLI maintenance gate changed"
                                )
                        if (
                            "legacy_cron_tick_lock_normalize_intent"
                            not in phases
                        ):
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_cron_tick_lock_normalize_intent",
                                _legacy_cron_tick_lock_normalize_intent_receipt(
                                    plan
                                ),
                            )
                            phases = journal["phases"]
                        cron_tick_lock = None
                        if "legacy_cron_tick_lock_normalized" in phases:
                            # A durable normalization may be resumed after a
                            # cooperating legacy scheduler touched only the
                            # empty lock file's mtime. Reacquire the kernel
                            # lock before accepting that bounded drift.
                            cron_tick_lock = _acquire_legacy_cron_tick_lock(
                                plan
                            )
                        if "legacy_cron_tick_lock_normalized" not in phases:
                            (
                                observed_tick_normalization,
                                cron_tick_lock,
                            ) = _normalize_and_acquire_legacy_cron_tick_lock(
                                plan,
                                phases[
                                    "legacy_cron_tick_lock_normalize_intent"
                                ],
                            )
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_cron_tick_lock_normalized",
                                observed_tick_normalization,
                            )
                            phases = journal["phases"]
                        else:
                            observed_tick_normalization = (
                                _normalize_legacy_cron_tick_lock(
                                    plan,
                                    phases[
                                        "legacy_cron_tick_lock_normalize_intent"
                                    ],
                                )
                            )
                            if not _legacy_cron_tick_normalizations_match(
                                plan,
                                observed_tick_normalization,
                                phases["legacy_cron_tick_lock_normalized"],
                            ):
                                raise DrainIdentityMismatch(
                                    "durable legacy cron tick lock "
                                    "normalization changed"
                                )
                        # Always reacquire the kernel lock on resume. A durable
                        # phase proves ordering, not continued lock ownership
                        # across process death.
                        if cron_tick_lock is None:
                            cron_tick_lock = _acquire_legacy_cron_tick_lock(
                                plan
                            )
                        if "legacy_cron_tick_lock_acquired" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_cron_tick_lock_acquired",
                                cron_tick_lock,
                            )
                            phases = journal["phases"]
                        _verify_legacy_cron_tick_lock(
                            plan,
                            cron_tick_lock,
                        )
                        if "legacy_gateway_drain_intent" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_gateway_drain_intent",
                                _prepared_legacy_gateway_drain_intent(
                                    plan,
                                    prepared,
                                ),
                            )
                            phases = journal["phases"]
                        if "legacy_gateway_drain_acknowledged" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_gateway_drain_acknowledged",
                                _wait_for_legacy_gateway_drain(
                                    plan,
                                    prepared,
                                    phases["legacy_gateway_drain_intent"],
                                ),
                            )
                            phases = journal["phases"]
                        if "legacy_gateway_stop_intent" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_gateway_stop_intent",
                                _legacy_gateway_stop_intent_receipt(
                                    plan,
                                    prepared,
                                    phases[
                                        "legacy_gateway_drain_acknowledged"
                                    ],
                                ),
                            )
                            phases = journal["phases"]
                        if "legacy_gateway_gracefully_stopped" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_gateway_gracefully_stopped",
                                _gracefully_stop_legacy_gateway(
                                    plan,
                                    prepared,
                                    phases["legacy_gateway_stop_intent"],
                                ),
                            )
                            phases = journal["phases"]
                        if (
                            "synthetic_store_mode_normalize_intent"
                            not in phases
                        ):
                            journal = _record_bootstrap_phase(
                                plan,
                                "synthetic_store_mode_normalize_intent",
                                _synthetic_store_mode_normalize_intent_receipt(
                                    plan
                                ),
                            )
                            phases = journal["phases"]
                        observed_store_normalization = (
                            _normalize_synthetic_completion_store_modes(
                                plan,
                                phases[
                                    "synthetic_store_mode_normalize_intent"
                                ],
                            )
                        )
                        if "synthetic_store_modes_normalized" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "synthetic_store_modes_normalized",
                                observed_store_normalization,
                            )
                            phases = journal["phases"]
                        elif (
                            phases["synthetic_store_modes_normalized"]
                            != observed_store_normalization
                        ):
                            raise DrainIdentityMismatch(
                                "durable synthetic store mode normalization changed"
                            )
                        if "legacy_dispatcher_lock_acquired" not in phases:
                            journal = _record_bootstrap_phase(
                                plan,
                                "legacy_dispatcher_lock_acquired",
                                _acquire_legacy_dispatcher_lock(plan),
                            )
                            phases = journal["phases"]
                        else:
                            _verify_legacy_dispatcher_lock(
                                plan,
                                phases["legacy_dispatcher_lock_acquired"],
                            )
                        boundary = _prove_frozen_legacy_boundary(
                            plan,
                            prepared,
                            frozen,
                            phases["legacy_dispatcher_lock_acquired"],
                        )
                    except Exception as boundary_error:
                        if not _can_restore_legacy_before_snapshot_abort(
                            phases
                        ):
                            raise
                        abort_receipt = _restore_legacy_before_snapshot_abort(
                            plan,
                            prepared,
                            frozen,
                            phases,
                            boundary_error,
                        )
                        journal = _record_bootstrap_phase(
                            plan,
                            "aborted_before_cutover",
                            abort_receipt,
                        )
                        return {
                            "status": "aborted",
                            "transaction_id": plan["transaction_id"],
                            "bootstrap_journal": str(journal_path),
                            "receipt": journal["phases"][
                                "aborted_before_cutover"
                            ],
                        }
                    if "frozen_boundary_proved" not in phases:
                        journal = _record_bootstrap_phase(
                            plan,
                            "frozen_boundary_proved",
                            boundary,
                        )
                        phases = journal["phases"]
                    elif boundary != phases["frozen_boundary_proved"]:
                        raise DrainIdentityMismatch(
                            "durable frozen boundary evidence changed"
                        )
                    if "legacy_jobs_booted_out" not in phases:
                        journal = _record_bootstrap_phase(
                            plan,
                            "legacy_jobs_booted_out",
                            _bootout_prepared_jobs(plan, prepared),
                        )
                        phases = journal["phases"]
                    else:
                        _verify_frozen_prepared_writers(
                            plan,
                            prepared,
                            frozen,
                        )
                        if any(
                            _job_pid(plan, gateway=gateway) is not None
                            for gateway in (False, True)
                        ):
                            raise DrainIdentityMismatch(
                                "legacy launchd job reappeared after bootout"
                            )
                    if "ingress_gate_start_intent" not in phases:
                        gate_script = _file_identity_receipt(
                            plan["ingress_gate_script"]
                        )
                        if (
                            gate_script["sha256"]
                            != plan["ingress_gate_expected_sha256"]
                        ):
                            raise ReleaseBuildError(
                                "staged ingress gate identity changed"
                            )
                        gate_token = _ingress_gate_token_receipt(plan)
                        journal = _record_bootstrap_phase(
                            plan,
                            "ingress_gate_start_intent",
                            {
                                "script": gate_script,
                                "token_file_sha256": gate_token["sha256"],
                                "host": str(
                                    urlsplit(str(plan["base_url"])).hostname
                                ),
                                "port": int(plan["listener_port"]),
                            },
                        )
                        phases = journal["phases"]
                    stopped_services = _stop_prepared_pair(
                        plan,
                        prepared,
                        frozen,
                        phases["legacy_jobs_booted_out"],
                        phases["legacy_gateway_gracefully_stopped"],
                    )
                    journal = _record_bootstrap_phase(
                        plan,
                        "services_stopped",
                        stopped_services,
                    )
                    phases = journal["phases"]
                    journal = _record_bootstrap_phase(
                        plan,
                        "legacy_cron_tick_lock_released",
                        _release_legacy_cron_tick_lock(plan),
                    )
                    phases = journal["phases"]
                    journal = _record_bootstrap_phase(
                        plan,
                        "ingress_gate_started",
                        _start_or_adopt_ingress_gate(plan),
                    )
                    phases = journal["phases"]
                elif "ingress_gate_started" not in phases:
                    if "legacy_cron_tick_lock_released" not in phases:
                        # A crash can land after exact service stop but before
                        # the release receipt. Reacquire, verify the current
                        # safe inode, then release it rather than trusting the
                        # stale acquisition phase.
                        _acquire_legacy_cron_tick_lock(plan)
                        journal = _record_bootstrap_phase(
                            plan,
                            "legacy_cron_tick_lock_released",
                            _release_legacy_cron_tick_lock(plan),
                        )
                        phases = journal["phases"]
                    journal = _record_bootstrap_phase(
                        plan,
                        "ingress_gate_started",
                        _start_or_adopt_ingress_gate(plan),
                    )
                    phases = journal["phases"]
                else:
                    gate_binding = _attest_ingress_gate(plan)
                    durable_gate = phases["ingress_gate_started"].get("binding")
                    if not isinstance(durable_gate, dict) or any(
                        gate_binding.get(key) != durable_gate.get(key)
                        for key in ("pid", "pid_start_token", "instance_id")
                    ):
                        raise DrainIdentityMismatch(
                            "durable ingress gate binding changed"
                        )
                if "snapshot_created" not in phases:
                    writer_barrier = _prove_no_mutable_writers(plan)
                    snapshot_receipt = create_state_snapshot(
                        plan["mutable_state_paths"],
                        snapshot_root=plan["snapshot_root"],
                        manifest_path=plan["snapshot_manifest"],
                        snapshot_id=plan["transaction_id"],
                    )
                    snapshot_receipt["writer_barrier"] = (
                        _prove_no_mutable_writers(
                            plan,
                            expected=writer_barrier,
                        )
                    )
                    journal = _record_bootstrap_phase(
                        plan,
                        "snapshot_created",
                        snapshot_receipt,
                    )
                    phases = journal["phases"]
                if "synthetic_state_quarantine_intent" not in phases:
                    journal = _record_bootstrap_phase(
                        plan,
                        "synthetic_state_quarantine_intent",
                        _synthetic_quarantine_intent_receipt(plan),
                    )
                    phases = journal["phases"]
                if "synthetic_state_quarantined" not in phases:
                    journal = _record_bootstrap_phase(
                        plan,
                        "synthetic_state_quarantined",
                        _quarantine_synthetic_completion_stores(
                            plan,
                            phases["synthetic_state_quarantine_intent"],
                        ),
                    )
                    phases = journal["phases"]
            finally:
                fcntl.flock(watchdog_lock.fileno(), fcntl.LOCK_UN)
                watchdog_lock.close()
        if "ingress_gate_stopped" not in phases:
            journal = _record_bootstrap_phase(
                plan,
                "ingress_gate_stopped",
                _stop_ingress_gate(plan, phases["ingress_gate_started"]),
            )
            phases = journal["phases"]
        if "managed_pair_started" not in phases:
            if "managed_pair_start_intent" not in phases:
                for gateway in (False, True):
                    port_key = "gateway_listener_port" if gateway else "listener_port"
                    try:
                        listener = _listener_pid(int(plan[port_key]))
                    except DrainIdentityMismatch:
                        listener = None
                    if listener is not None or _job_pid(plan, gateway=gateway) is not None:
                        raise DrainIdentityMismatch(
                            "managed pair appeared before durable start intent"
                        )
                activated_selection = _selector_transition(plan, "activate")
                if (
                    int(activated_selection["generation"])
                    != int(
                        plan["expected_candidate_identity"][
                            "selector_generation"
                        ]
                    )
                ):
                    raise ReleaseBuildError(
                        "candidate selector generation changed before startup"
                    )
                webui_install = _atomic_copy_file(
                    plan["managed_plist"],
                    plan["installed_plist"],
                    expected_sha256=sha256_file(Path(plan["managed_plist"])),
                    mode=0o600,
                )
                gateway_install = _install_managed_gateway_plist(
                    plan,
                    prepared,
                    plan["expected_candidate_identity"],
                )
                journal = _record_bootstrap_phase(
                    plan,
                    "managed_pair_start_intent",
                    {
                        "build_id": plan["expected_candidate_identity"]["build_id"],
                        "selection": activated_selection,
                        "webui_install": webui_install,
                        "gateway_install": gateway_install,
                        "cli": _attest_bootstrap_cli_preopen(
                            plan,
                            phases[
                                "cli_maintenance_gate_stage_intent"
                            ],
                        ),
                    },
                )
                phases = journal["phases"]
            intent = phases["managed_pair_start_intent"]
            if (
                sha256_file(Path(plan["installed_plist"]))
                != intent["webui_install"]["sha256"]
                or sha256_file(Path(plan["gateway_installed_plist"]))
                != intent["gateway_install"]["installed_plist"]["sha256"]
            ):
                raise ReleaseBuildError("managed pair install identity changed")
            if "legacy_dispatcher_lock_released" not in phases:
                journal = _record_bootstrap_phase(
                    plan,
                    "legacy_dispatcher_lock_released",
                    _release_legacy_dispatcher_lock(plan),
                )
                phases = journal["phases"]

            managed_binding = _probe_startup_fenced_webui_binding(
                plan,
                plan["expected_candidate_identity"],
            )
            try:
                gateway_listener = _listener_pid(
                    int(plan["gateway_listener_port"])
                )
            except DrainIdentityMismatch:
                gateway_listener = None
            gateway_job = _job_pid(plan, gateway=True)
            if gateway_listener is None and gateway_job is None:
                gateway_binding = None
            elif gateway_listener is None or gateway_job != gateway_listener:
                raise DrainIdentityMismatch(
                    "managed gateway launch boundary is ambiguous"
                )
            else:
                gateway_binding = _attest_managed_gateway_binding(
                    plan,
                    plan["expected_candidate_identity"],
                    expected_admission="rejecting_new_work",
                )

            gateway_started: dict = {"status": "externally-reconciled"}
            webui_started: dict = {"status": "externally-reconciled"}
            if managed_binding is None or gateway_binding is None:
                if gateway_binding is None:
                    gateway_started = _bootstrap_job(
                        plan,
                        plan["gateway_installed_plist"],
                        gateway=True,
                    )
                if managed_binding is None:
                    webui_started = _bootstrap_job(
                        plan,
                        plan["installed_plist"],
                        gateway=False,
                    )
                gateway_binding = _attest_managed_gateway_binding(
                    plan,
                    plan["expected_candidate_identity"],
                    expected_admission="rejecting_new_work",
                )
                inspect_control, _send_control, _transaction = (
                    _release_control_client(
                        plan["base_url"],
                        _read_release_control_key(plan["signing_key_file"]),
                        transaction_id=plan["transaction_id"],
                        request_timeout_seconds=max(
                            30.0,
                            float(plan["timeout_seconds"]),
                        ),
                    )
                )
                managed_binding = _wait_for_expected_binding(
                    plan,
                    inspect_control=inspect_control,
                    expected_identity=plan["expected_candidate_identity"],
                    admission_state="startup-fenced",
                    previous_pid_start=(
                        int(prepared["legacy"]["pid"]),
                        str(prepared["legacy"]["pid_start_token"]),
                    ),
                    require_startup_markers_cleared=False,
                )
            if managed_binding is None or gateway_binding is None:
                raise ReleaseBuildError("managed pair did not reach exact binding")
            if (
                gateway_binding["listener_pid"] == int(prepared["gateway"]["pid"])
                and gateway_binding["pid_start_token"]
                == str(prepared["gateway"]["pid_start_token"])
            ):
                raise DrainIdentityMismatch(
                    "managed gateway process identity was not replaced"
                )
            cli_receipt = _attest_bootstrap_cli_preopen(
                plan,
                phases["cli_maintenance_gate_stage_intent"],
            )
            journal = _record_bootstrap_phase(
                plan,
                "managed_pair_started",
                {
                    "selector_generation": int(
                        plan["expected_candidate_identity"][
                            "selector_generation"
                        ]
                    ),
                    "admission": {
                        "webui": "startup-fenced",
                        "gateway": "rejecting_new_work",
                    },
                    "gateway_install": intent["gateway_install"],
                    "gateway_start": gateway_started,
                    "webui_start": webui_started,
                    "gateway_binding": gateway_binding,
                    "managed_binding": {
                        key: managed_binding.get(key)
                        for key in (
                            "launchd_pid",
                            "listener_pid",
                            "signed_health_pid",
                            "pid_start_token",
                        )
                    },
                    "managed_runtime": managed_binding.get("runtime"),
                    "cli": cli_receipt,
                    "routing_environment": prepared["legacy"][
                        "routing_environment"
                    ],
                },
            )
            phases = journal["phases"]
        if "cutover_handed_off" not in phases:
            bootstrap_evidence = {
                "prepared": prepared,
                "services_stopped": phases["services_stopped"],
                "snapshot": phases["snapshot_created"],
                "managed_pair": phases["managed_pair_started"],
            }
            cutover_journal = _reconcile_cutover_journal(
                plan,
                staged_evidence=bootstrap_evidence,
            )
            if (
                cutover_journal["phases"]
                .get("staged", {})
                .get("bootstrap_evidence")
                is None
            ):
                raise ReleaseBuildError(
                    "durable bootstrap boundary evidence is missing"
                )
            cutover_phases = cutover_journal["phases"]
            last_good_origin_attestation = (
                prepared["last_good_split_provenance"]["split_evidence"]
            )
            bootstrap_cutover_receipts = (
                (
                    "last_good_split_attested",
                    {
                        "last_good_origin_attestation": (
                            last_good_origin_attestation
                        ),
                    },
                ),
                (
                    "gateway_last_good_attested",
                    {
                        "binding": {
                            "status": "legacy-gracefully-stopped",
                            "listener_pid": prepared["gateway"]["pid"],
                            "pid_start_token": prepared["gateway"][
                                "pid_start_token"
                            ],
                            "runtime": prepared["gateway"],
                        },
                        "last_good_origin_attestation": (
                            last_good_origin_attestation
                        ),
                    },
                ),
                (
                    "old_fenced",
                    {
                        "identity": {
                            "pid": prepared["legacy"]["pid"],
                            "pid_start_token": prepared["legacy"][
                                "pid_start_token"
                            ],
                        },
                        "admission": {
                            "state": "bootstrap-writer-frozen"
                        },
                    },
                ),
                (
                    "old_committed",
                    {
                        "identity": {
                            "pid": prepared["legacy"]["pid"],
                            "pid_start_token": prepared["legacy"][
                                "pid_start_token"
                            ],
                        },
                        "admission": {"state": "bootstrap-stopped"},
                    },
                ),
                (
                    "selection_activated",
                    {
                        "selection": phases[
                            "managed_pair_start_intent"
                        ]["selection"],
                    },
                ),
                (
                    "old_job_booted_out",
                    {
                        "identity": {
                            "pid": prepared["legacy"]["pid"],
                            "pid_start_token": prepared["legacy"][
                                "pid_start_token"
                            ],
                        },
                        "bootout": phases["legacy_jobs_booted_out"],
                    },
                ),
                (
                    "old_stopped",
                    {
                        "identity": {
                            "pid": prepared["legacy"]["pid"],
                            "pid_start_token": prepared["legacy"][
                                "pid_start_token"
                            ],
                        },
                        "bootstrap_receipt": phases["services_stopped"][
                            "webui"
                        ],
                    },
                ),
                (
                    "candidate_job_bootstrapped",
                    {
                        "bootstrap": phases["managed_pair_started"][
                            "webui_start"
                        ],
                    },
                ),
            )
            for phase_name, receipt in bootstrap_cutover_receipts:
                if phase_name not in cutover_phases:
                    cutover_journal = record_transaction_phase(
                        plan["transaction_journal"],
                        transaction_id=plan["transaction_id"],
                        phase=phase_name,
                        receipt=receipt,
                    )
                    cutover_phases = cutover_journal["phases"]
            journal = _record_bootstrap_phase(
                plan,
                "cutover_handed_off",
                {
                    "transaction_journal": str(plan["transaction_journal"]),
                    "transaction_journal_sha256": sha256_file(
                        Path(plan["transaction_journal"])
                    ),
                },
            )
            phases = journal["phases"]

        def prepare_bootstrap_pair(_identity: dict) -> dict:
            nonlocal journal, phases
            journal = _read_bootstrap_journal(plan)
            phases = journal["phases"]
            if "watchdog_installed" not in phases:
                old_watchdog = prepared["watchdog"]
                installed = Path(plan["watchdog_installed_script"])
                if sha256_file(installed) != plan["watchdog_expected_sha256"]:
                    _atomic_copy_file(
                        plan["watchdog_candidate_script"],
                        installed,
                        expected_sha256=plan["watchdog_expected_sha256"],
                        mode=int(old_watchdog["resolved_mode"]),
                    )
                    os.chown(installed, int(old_watchdog["resolved_uid"]), -1)
                    _fsync_directory(installed.parent)
                installed_receipt = _file_identity_receipt(installed)
                if installed_receipt["sha256"] != plan["watchdog_expected_sha256"]:
                    raise ReleaseBuildError(
                        "watchdog activation identity changed"
                    )
                cron = _attest_disabled_watchdog_cron(plan, prepared)
                if cron != phases["watchdog_cron_disabled"]:
                    raise ReleaseBuildError(
                        "watchdog cron barrier changed during activation"
                    )
                journal = _record_bootstrap_phase(
                    plan,
                    "watchdog_installed",
                    {"script": installed_receipt, "cron": cron},
                )
                phases = journal["phases"]
            if "watchdog_reconciled_once" not in phases:
                journal = _record_bootstrap_phase(
                    plan,
                    "watchdog_reconciled_once",
                    _watchdog_reconcile_receipt(plan, prepared),
                )
                phases = journal["phases"]
            if "watchdog_reconciled_twice" not in phases:
                journal = _record_bootstrap_phase(
                    plan,
                    "watchdog_reconciled_twice",
                    _watchdog_reconcile_receipt(plan, prepared),
                )
                phases = journal["phases"]
            cron = _attest_disabled_watchdog_cron(plan, prepared)
            if cron != phases["watchdog_cron_disabled"]:
                raise ReleaseBuildError(
                    "watchdog cron barrier changed before pair open"
                )
            live_readiness = _attest_bootstrap_pair_readiness(
                plan,
                prepared,
                phases,
            )
            return {
                "status": "ready",
                "watchdog": phases["watchdog_installed"],
                "reconcile_once": phases["watchdog_reconciled_once"],
                "reconcile_twice": phases["watchdog_reconciled_twice"],
                "cron": cron,
                "live_readiness": live_readiness,
            }

        def open_bootstrap_gateway(_identity: dict) -> dict:
            nonlocal journal, phases
            journal = _read_bootstrap_journal(plan)
            phases = journal["phases"]
            if "watchdog_reconciled_twice" not in phases:
                raise ReleaseBuildError(
                    "gateway cannot prepare before watchdog reconciliation"
                )
            live_readiness = _attest_bootstrap_pair_readiness(
                plan,
                prepared,
                phases,
            )
            if "legacy_gateway_drain_cleared" not in phases:
                journal = _record_bootstrap_phase(
                    plan,
                    "legacy_gateway_drain_cleared",
                    _clear_legacy_gateway_drain_marker(
                        plan,
                        phases["legacy_gateway_drain_intent"],
                    ),
                )
                phases = journal["phases"]
            return {
                "drain": phases["legacy_gateway_drain_cleared"],
                "live_readiness": live_readiness,
            }

        result: dict = {"status": "resumed"}
        if "candidate_pair_accepted" not in phases:
            result = _run_release_commit_plan(
                plan,
                bootstrap_prepare_pair=prepare_bootstrap_pair,
                bootstrap_open_pair=open_bootstrap_gateway,
                watchdog_prepared=prepared,
            )
            journal = _read_bootstrap_journal(plan)
            phases = journal["phases"]
            inspect_control, _send_control, _transaction = _release_control_client(
                plan["base_url"],
                _read_release_control_key(plan["signing_key_file"]),
                transaction_id=plan["transaction_id"],
                request_timeout_seconds=max(
                    30.0,
                    float(plan["timeout_seconds"]),
                ),
            )
            candidate_binding = _wait_for_expected_binding(
                plan,
                inspect_control=inspect_control,
                expected_identity=plan["expected_candidate_identity"],
                admission_state="open",
                require_startup_markers_cleared=False,
            )
            cutover_journal = read_transaction_journal(
                plan["transaction_journal"],
                transaction_id=plan["transaction_id"],
            )
            gateway_ready = cutover_journal["phases"].get(
                "candidate_gateway_accepted"
            )
            gateway_opened = cutover_journal["phases"].get("gateway_opened")
            if not isinstance(gateway_ready, dict) or not isinstance(
                gateway_opened,
                dict,
            ):
                raise ReleaseBuildError(
                    "paired release commit did not accept the candidate gateway"
                )
            gateway_binding = (
                gateway_opened.get("gateway", {})
                .get("gateway")
            )
            if not isinstance(gateway_binding, dict):
                raise ReleaseBuildError(
                    "candidate gateway binding receipt is missing"
                )
            journal = _record_bootstrap_phase(
                plan,
                "candidate_pair_accepted",
                {
                    "candidate_build_id": plan["expected_candidate_identity"][
                        "build_id"
                    ],
                    "candidate_binding": {
                        key: candidate_binding.get(key)
                        for key in (
                            "launchd_pid",
                            "listener_pid",
                            "signed_health_pid",
                            "pid_start_token",
                        )
                    },
                    "candidate_runtime": candidate_binding.get("runtime"),
                    "gateway_binding": gateway_binding,
                    "gateway_install": gateway_ready.get("install"),
                    "gateway_start": gateway_ready.get("start"),
                    "transaction_journal_sha256": sha256_file(
                        Path(plan["transaction_journal"])
                    ),
                },
            )
            phases = journal["phases"]
        cutover_journal = read_transaction_journal(
            plan["transaction_journal"],
            transaction_id=plan["transaction_id"],
        )
        cutover_phases = cutover_journal["phases"]
        if "cli_candidate_activate_intent" not in phases:
            journal = _record_bootstrap_phase(
                plan,
                "cli_candidate_activate_intent",
                _bootstrap_cli_candidate_activation_intent(
                    plan,
                    phases["cli_maintenance_gate_stage_intent"],
                    cutover_phases,
                ),
            )
            phases = journal["phases"]
        cli_activated = _activate_or_adopt_bootstrap_cli_candidate(
            plan,
            phases["cli_candidate_activate_intent"],
        )
        if "cli_candidate_activated" not in phases:
            journal = _record_bootstrap_phase(
                plan,
                "cli_candidate_activated",
                cli_activated,
            )
            phases = journal["phases"]
        else:
            durable_cli_activation = phases["cli_candidate_activated"]
            if any(
                cli_activated.get(key)
                != durable_cli_activation.get(key)
                for key in (
                    "transaction_id",
                    "link_path",
                    "target",
                    "target_sha256",
                    "pair_opened_owner_hash",
                    "pair_opened_payload_sha256",
                    "bounded_host_assumption",
                )
            ):
                raise DrainIdentityMismatch(
                    "durable public Hermes CLI activation changed"
                )
        if "watchdog_cron_restored" not in phases:
            pair_opened = cutover_phases.get("pair_opened")
            restored_cron = cutover_phases.get("watchdog_cron_restored")
            if not isinstance(pair_opened, dict) or not isinstance(
                restored_cron,
                dict,
            ):
                raise ReleaseBuildError(
                    "paired release did not durably open before watchdog restore"
                )
            live_cron = _watchdog_receipt_for_prepared(plan, prepared)
            if live_cron != restored_cron or not _cron_receipt_matches_prepared(
                live_cron,
                prepared,
            ):
                raise DrainIdentityMismatch(
                    "watchdog cron restore changed after paired release"
                )
            journal = _record_bootstrap_phase(
                plan,
                "watchdog_cron_restored",
                {
                    **restored_cron,
                    "pair_opened_owner_hash": pair_opened.get("owner_hash"),
                    "pair_opened_payload_sha256": pair_opened.get(
                        "payload_sha256"
                    ),
                },
            )
            phases = journal["phases"]
        journal = _record_bootstrap_phase(
            plan,
            "complete",
            {
                "status": "accepted",
                "candidate_build_id": plan["expected_candidate_identity"][
                    "build_id"
                ],
                "transaction_journal_sha256": sha256_file(
                    Path(plan["transaction_journal"])
                ),
            },
        )
        return {
            "status": "accepted",
            "transaction_id": plan["transaction_id"],
            "bootstrap_journal": str(journal_path),
            "bootstrap": journal["phases"]["managed_pair_started"],
            "cutover": result,
        }
    except Exception as original:
        bootstrap_now = _read_bootstrap_journal(plan)
        bootstrap_phases = bootstrap_now["phases"]
        if _can_restore_legacy_before_snapshot_abort(bootstrap_phases):
            try:
                abort_receipt = _restore_legacy_before_snapshot_abort(
                    plan,
                    prepared,
                    bootstrap_phases.get("writers_frozen", {"writers": []}),
                    bootstrap_phases,
                    original,
                )
                aborted = _record_bootstrap_phase(
                    plan,
                    "aborted_before_cutover",
                    abort_receipt,
                )
            except Exception as abort_error:
                raise ReleaseBuildError(
                    f"bootstrap migration failed before snapshot: {original}; "
                    f"exact legacy restore failed: {abort_error}"
                ) from original
            return {
                "status": "aborted",
                "transaction_id": plan["transaction_id"],
                "bootstrap_journal": str(journal_path),
                "receipt": aborted["phases"]["aborted_before_cutover"],
            }
        recovery_errors: list[str] = []
        try:
            journal = _read_bootstrap_journal(plan)
            if "rollback_started" not in journal["phases"]:
                journal = _record_bootstrap_phase(
                    plan,
                    "rollback_started",
                    {"error_type": type(original).__name__},
                )
            _resume_bootstrap_rollback(plan, journal)
        except Exception as exc:
            recovery_errors.append(f"bootstrap rollback failed: {exc}")
        if recovery_errors:
            raise ReleaseBuildError(
                f"bootstrap migration failed: {original}; "
                + "; ".join(recovery_errors)
            ) from original
        raise ReleaseBuildError(
            f"bootstrap migration failed and rolled back exactly: {original}"
        ) from original


def _emit_json(value: object) -> None:
    print(
        json.dumps(
            _journal_copy_of_immutable_evidence(value),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _add_state_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", required=True)
    parser.add_argument("--lock", required=True)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes WebUI immutable cutover driver")
    commands = parser.add_subparsers(dest="command", required=True)

    identity = commands.add_parser("identity")
    identity.add_argument("--path", required=True)
    identity.add_argument("--kind", required=True, choices=("selector", "interpreter"))

    install = commands.add_parser("install-selector")
    install.add_argument("--source", required=True)
    install.add_argument("--destination", required=True)
    install.add_argument("--expected-source-sha256", required=True)

    build_agent_source = commands.add_parser("build-agent-source")
    build_agent_source.add_argument("--repo", required=True)
    build_agent_source.add_argument("--ref", required=True)
    build_agent_source.add_argument("--release-root", required=True)
    build_agent_source.add_argument("--expected-origin-url", required=True)
    build_agent_source.add_argument("--base-ref", required=True)
    build_agent_source.add_argument("--expected-base-commit", required=True)
    build_agent_source.add_argument(
        "--allowed-changed-path", action="append", default=[]
    )

    build_runtime = commands.add_parser("build-runtime")
    build_runtime.add_argument("--python-home", required=True)
    build_runtime.add_argument("--site-packages", required=True)
    build_runtime.add_argument("--release-root", required=True)
    build_runtime.add_argument("--interpreter-relative-path", required=True)
    build_runtime.add_argument("--agent-source-identity-json", required=True)

    for name in ("release-commit", "bootstrap-migrate", "inspect-plan"):
        plan_command = commands.add_parser(name)
        plan_command.add_argument("--plan", required=True)
        if name != "inspect-plan":
            plan_command.add_argument("--dry-run", action="store_true")

    build = commands.add_parser("build")
    build.add_argument("--repo", required=True)
    build.add_argument("--ref", required=True)
    build.add_argument("--release-root", required=True)
    build.add_argument("--build-id", required=True)
    build.add_argument("--base-ref", required=True)
    build.add_argument("--expected-origin-url", required=True)
    build.add_argument("--expected-base-commit", required=True)
    build.add_argument("--allowed-changed-path", action="append", default=[])
    build.add_argument("--selector", required=True)
    build.add_argument("--interpreter", required=True)
    build.add_argument("--selector-identity-json", required=True)
    build.add_argument("--interpreter-identity-json", required=True)
    build.add_argument("--runtime-identity-json", required=True)
    build.add_argument("--agent-source-identity-json", required=True)
    build.add_argument("--metadata-json", required=True)

    state_init = commands.add_parser("state-init")
    _add_state_paths(state_init)
    state_init.add_argument("--release-root", required=True)
    state_init.add_argument("--build-id", required=True)
    state_init.add_argument("--record-json", required=True)

    state_stage = commands.add_parser("state-stage")
    _add_state_paths(state_stage)
    state_stage.add_argument("--expected-generation", required=True, type=int)
    state_stage.add_argument("--build-id", required=True)
    state_stage.add_argument("--record-json", required=True)
    state_stage.add_argument("--transaction-id", required=True)

    for name in ("state-activate", "state-promote", "state-rollback"):
        transition = commands.add_parser(name)
        _add_state_paths(transition)
        transition.add_argument("--expected-generation", required=True, type=int)

    state_show = commands.add_parser("state-show")
    _add_state_paths(state_show)

    verify = commands.add_parser("verify-release")
    verify.add_argument("--release-path", required=True)
    verify.add_argument("--release-root", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    verify.add_argument("--selector", required=True)
    verify.add_argument("--skip-selector-identity", action="store_true")

    plist_selector = commands.add_parser("plist-selector")
    plist_selector.add_argument("--input", required=True)
    plist_selector.add_argument("--output", required=True)
    plist_selector.add_argument("--selector", required=True)
    plist_selector.add_argument("--selector-state", required=True)
    plist_selector.add_argument("--selector-lock", required=True)
    plist_selector.add_argument("--expected-label", required=True)
    plist_selector.add_argument("--expected-interpreter", required=True)
    plist_selector.add_argument("--managed-interpreter", required=True)
    plist_selector.add_argument("--expected-old-target", required=True)

    plist_fallback = commands.add_parser("plist-fallback")
    plist_fallback.add_argument("--input", required=True)
    plist_fallback.add_argument("--output", required=True)
    plist_fallback.add_argument("--release-identity-json", required=True)
    plist_fallback.add_argument("--selector-generation", required=True, type=int)
    plist_fallback.add_argument("--expected-label", required=True)
    plist_fallback.add_argument("--expected-interpreter", required=True)
    plist_fallback.add_argument("--expected-old-target", required=True)
    plist_fallback.add_argument("--selector-state", required=True)
    plist_fallback.add_argument("--selector-lock", required=True)
    plist_fallback.add_argument("--startup-transaction-id")
    return parser


def _run_cli(options: argparse.Namespace) -> dict:
    if options.command == "identity":
        return freeze_external_identity(
            options.path,
            label=options.kind,
            allow_leaf_symlink=options.kind == "interpreter",
        )
    if options.command == "install-selector":
        return install_external_selector(
            options.source,
            options.destination,
            expected_source_sha256=options.expected_source_sha256,
        )
    if options.command == "build-agent-source":
        return build_immutable_agent_source(
            options.repo,
            options.ref,
            release_root=options.release_root,
            expected_origin_url=options.expected_origin_url,
            base_ref=options.base_ref,
            expected_base_commit=options.expected_base_commit,
            allowed_changed_paths=set(options.allowed_changed_path),
        )
    if options.command == "build-runtime":
        return build_immutable_runtime(
            options.python_home,
            options.site_packages,
            release_root=options.release_root,
            interpreter_relative_path=options.interpreter_relative_path,
            agent_source_identity=_read_json_object(
                options.agent_source_identity_json,
                label="agent source identity receipt",
            ),
        )
    if options.command in {"release-commit", "bootstrap-migrate", "inspect-plan"}:
        plan = _load_cutover_plan(options.plan)
        if options.command == "inspect-plan":
            return _inspect_cutover_plan(plan)
        if options.command == "bootstrap-migrate":
            result = _run_bootstrap_migration_plan(
                plan,
                dry_run=options.dry_run,
            )
        else:
            result = _run_release_commit_plan(
                plan,
                dry_run=options.dry_run,
            )
        if not options.dry_run and result.get("status") == "accepted":
            result = {
                **result,
                "rollback_retention": release_retention.run_after_release(
                    plan["selector_state"],
                    plan["selector_lock"],
                    accepted_transaction_id=plan["transaction_id"],
                    expected_current_build=plan[
                        "expected_candidate_identity"
                    ]["build_id"],
                ),
            }
        return result
    if options.command == "build":
        return build_immutable_release(
            options.repo,
            options.ref,
            release_root=options.release_root,
            build_id=options.build_id,
            base_ref=options.base_ref,
            expected_origin_url=options.expected_origin_url,
            expected_base_commit=options.expected_base_commit,
            allowed_changed_paths=set(options.allowed_changed_path),
            selector_path=options.selector,
            interpreter_path=options.interpreter,
            expected_selector_identity=_read_json_object(
                options.selector_identity_json,
                label="selector identity receipt",
            ),
            expected_interpreter_identity=_read_json_object(
                options.interpreter_identity_json,
                label="interpreter identity receipt",
            ),
            runtime_identity=_read_json_object(
                options.runtime_identity_json,
                label="runtime identity receipt",
            ),
            agent_source_identity=_read_json_object(
                options.agent_source_identity_json,
                label="agent source identity receipt",
            ),
            metadata=_read_json_object(options.metadata_json, label="release metadata"),
        )
    if options.command == "state-init":
        return release_selector.initialize_selector_state(
            options.state,
            lock_path=options.lock,
            release_root=options.release_root,
            bootstrap_build_id=options.build_id,
            bootstrap_record=_read_json_object(
                options.record_json,
                label="release record",
            ),
        )
    if options.command == "state-stage":
        record = _read_json_object(options.record_json, label="release record")
        return release_selector.update_selector_state(
            options.state,
            lock_path=options.lock,
            expected_generation=options.expected_generation,
            transition=lambda state: release_selector.stage_candidate(
                state,
                options.build_id,
                record,
                transaction_id=options.transaction_id,
            ),
        )
    transitions = {
        "state-activate": release_selector.activate_candidate,
        "state-promote": release_selector.promote_candidate,
        "state-rollback": release_selector.rollback_to_last_good,
    }
    if options.command in transitions:
        result = release_selector.update_selector_state(
            options.state,
            lock_path=options.lock,
            expected_generation=options.expected_generation,
            transition=transitions[options.command],
        )
        return result
    if options.command == "state-show":
        return release_selector.read_selector_state(options.state, lock_path=options.lock)
    if options.command == "verify-release":
        return release_selector.verify_release(
            options.release_path,
            release_root=options.release_root,
            expected_manifest_sha256=options.manifest_sha256,
            selector_path=options.selector,
            verify_selector_identity=not options.skip_selector_identity,
        )
    source_plist = _read_plist(options.input)
    if options.command == "plist-selector":
        rendered = transform_launchd_target(
            source_plist,
            options.selector,
            expected_label=options.expected_label,
            expected_old_interpreter=options.expected_interpreter,
            managed_interpreter=options.managed_interpreter,
            expected_old_target=options.expected_old_target,
            selector_state_path=options.selector_state,
            selector_lock_path=options.selector_lock,
        )
    elif options.command == "plist-fallback":
        rendered = build_direct_fallback_plist(
            source_plist,
            expected_label=options.expected_label,
            expected_old_interpreter=options.expected_interpreter,
            expected_old_target=options.expected_old_target,
            release_identity=_read_json_object(
                options.release_identity_json,
                label="fallback release identity",
            ),
            selector_generation=options.selector_generation,
            selector_state_path=options.selector_state,
            selector_lock_path=options.selector_lock,
            startup_transaction_id=options.startup_transaction_id,
        )
    else:
        raise ReleaseBuildError("cutover command is unsupported")
    _write_plist_atomic(options.output, rendered)
    return {"status": "ok", "output": str(Path(options.output))}


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    try:
        result = _run_cli(parser.parse_args(argv))
    except (ReleaseBuildError, release_selector.SelectorError, ValueError) as exc:
        print(f"Hermes WebUI cutover refused: {exc}", file=sys.stderr)
        return 78
    _emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
