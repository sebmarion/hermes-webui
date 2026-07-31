#!/usr/bin/env python3
"""Fail-closed immutable release selector for the Hermes WebUI launchd job."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Callable


MANIFEST_NAME = ".hermes-release.json"
STATE_VERSION = 2
LEGACY_STATE_VERSION = 1
MANIFEST_VERSION = 1
_HEX_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LAUNCHD_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_AGENT_SOURCE_IDENTITY_KEYS = {
    "path",
    "resolved_path",
    "commit",
    "tree",
    "manifest_path",
    "manifest_sha256",
}
_AGENT_SOURCE_MANIFEST_KEYS = {
    "version",
    "origin_url",
    "base_commit",
    "commit",
    "tree",
    "changed_files",
    "files",
}
_RUNTIME_IDENTITY_KEYS = {
    "path",
    "resolved_path",
    "python_home_path",
    "site_packages_path",
    "interpreter_path",
    "interpreter_resolved_path",
    "manifest_path",
    "manifest_sha256",
}
_RUNTIME_MANIFEST_KEYS = {
    "version",
    "interpreter_relative_path",
    "site_packages_relative_path",
    "files",
}
_ALLOWED_INHERITED_ENV_KEYS = {
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
    "HERMES_WEBUI_TLS_CERT",
    "HERMES_WEBUI_TLS_KEY",
}


class SelectorError(RuntimeError):
    """A fail-closed selector validation or transition error."""


class InjectedCrash(RuntimeError):
    """Deterministic atomic-write crash injection used by tests."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_existing_directory(
    path: Path,
    *,
    label: str,
    trusted: bool = False,
    read_only: bool = False,
) -> Path:
    if not path.is_absolute():
        raise SelectorError(f"{label} must be absolute")
    if path.is_symlink():
        raise SelectorError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SelectorError(f"{label} is missing") from exc
    if not resolved.is_dir():
        raise SelectorError(f"{label} is not a directory")
    if Path(os.path.abspath(path)) != resolved:
        raise SelectorError(f"{label} must be canonical")
    opened = resolved.stat()
    if trusted and (opened.st_uid != os.getuid() or opened.st_mode & 0o022):
        raise SelectorError(f"{label} ownership or mode is unsafe")
    if read_only and opened.st_mode & 0o222:
        raise SelectorError(f"{label} must be read-only")
    return resolved


def _safe_manifest_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise SelectorError("manifest file path is invalid")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise SelectorError("manifest file path escapes the release")
    normalized = relative.as_posix()
    if normalized != raw or normalized == MANIFEST_NAME:
        raise SelectorError("manifest file path is not canonical")
    return normalized


def _load_manifest(
    release_path: Path,
    *,
    expected_manifest_sha256: str,
) -> tuple[dict, str]:
    if not _SHA256.fullmatch(str(expected_manifest_sha256 or "")):
        raise SelectorError("manifest expected hash is invalid")
    manifest_path = release_path / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise SelectorError("manifest must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(manifest_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or opened.st_mode & 0o222
            ):
                raise SelectorError(
                    "manifest must be a read-only private regular file"
                )
            raw = handle.read(16 * 1024 * 1024 + 1)
    except SelectorError:
        raise
    except OSError as exc:
        raise SelectorError("manifest is missing") from exc
    if len(raw) > 16 * 1024 * 1024:
        raise SelectorError("manifest is too large")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_manifest_sha256:
        raise SelectorError("manifest hash mismatch")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorError("manifest JSON is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise SelectorError("manifest version is unsupported")
    return manifest, actual_hash


def _verify_external_identity(
    spec: object,
    *,
    label: str,
    allow_configured_symlink: bool = False,
) -> tuple[Path, Path]:
    if not isinstance(spec, dict):
        raise SelectorError(f"{label} identity is missing")
    configured = Path(str(spec.get("path") or ""))
    if not configured.is_absolute():
        raise SelectorError(f"{label} path must be absolute")
    if Path(os.path.abspath(configured)) != configured:
        raise SelectorError(f"{label} path must be canonical")
    if configured.is_symlink() and not allow_configured_symlink:
        raise SelectorError(f"{label} path must not be a symlink")
    try:
        configured_parent = configured.parent.resolve(strict=True)
    except OSError as exc:
        raise SelectorError(f"{label} parent is missing") from exc
    if configured_parent != configured.parent:
        raise SelectorError(f"{label} parent must be canonical and symlink-free")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise SelectorError(f"{label} path is missing") from exc
    expected_resolved = str(spec.get("resolved_path") or "")
    if str(resolved) != expected_resolved:
        raise SelectorError(f"{label} resolved path mismatch")
    resolved_stat = resolved.stat()
    if not stat.S_ISREG(resolved_stat.st_mode):
        raise SelectorError(f"{label} is not a file")
    if resolved_stat.st_uid != os.getuid() or resolved_stat.st_mode & 0o022:
        raise SelectorError(f"{label} ownership or mode is unsafe")
    if not resolved_stat.st_mode & 0o111:
        raise SelectorError(f"{label} is not executable")
    expected_hash = str(spec.get("sha256") or "")
    if not _SHA256.fullmatch(expected_hash) or sha256_file(resolved) != expected_hash:
        raise SelectorError(f"{label} hash mismatch")
    return configured, resolved


def _declared_external_identity(spec: object, *, label: str) -> tuple[Path, Path]:
    """Validate a manifest identity without touching the external file.

    Direct-fallback mode deliberately has no runtime dependency on the selector.
    The selector declaration remains covered by the immutable manifest hash, but
    only its canonical shape is parsed on that recovery path.
    """
    if not isinstance(spec, dict) or set(spec) != {"path", "resolved_path", "sha256"}:
        raise SelectorError(f"{label} identity is missing")
    configured = Path(str(spec.get("path") or ""))
    resolved = Path(str(spec.get("resolved_path") or ""))
    if (
        not configured.is_absolute()
        or Path(os.path.abspath(configured)) != configured
        or not resolved.is_absolute()
        or Path(os.path.abspath(resolved)) != resolved
    ):
        raise SelectorError(f"{label} declared path is invalid")
    if not _SHA256.fullmatch(str(spec.get("sha256") or "")):
        raise SelectorError(f"{label} declared hash is invalid")
    return configured, resolved


def _validate_release_admission(manifest: dict) -> None:
    origin_url = manifest.get("origin_url")
    if not isinstance(origin_url, str) or not origin_url.strip() or "\n" in origin_url:
        raise SelectorError("manifest source origin identity is invalid")
    base_commit = str(manifest.get("base_commit") or "")
    if not _HEX_ID.fullmatch(base_commit):
        raise SelectorError("manifest base commit identity is invalid")
    changed = manifest.get("changed_files")
    if not isinstance(changed, list) or changed != sorted(set(changed)):
        raise SelectorError("manifest changed files are invalid")
    canonical_changed = []
    for path in changed:
        canonical_changed.append(_safe_manifest_relative_path(path))
    decisions = manifest.get("patch_decisions")
    if not isinstance(decisions, dict) or set(decisions) != set(canonical_changed):
        raise SelectorError("manifest patch decisions are incomplete")
    for decision in decisions.values():
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "ship"
            or not str(decision.get("rationale") or "").strip()
        ):
            raise SelectorError("manifest patch decision is invalid")
    receipts = manifest.get("test_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise SelectorError("manifest test receipts are missing")
    for receipt in receipts:
        if (
            not isinstance(receipt, dict)
            or not str(receipt.get("name") or "").strip()
            or receipt.get("status") != "passed"
            or not _SHA256.fullmatch(str(receipt.get("receipt_sha256") or ""))
        ):
            raise SelectorError("manifest test receipt is invalid")
    artifacts = manifest.get("artifact_hashes")
    if not isinstance(artifacts, dict) or not artifacts:
        raise SelectorError("manifest preserved artifact hashes are missing")
    if any(
        not str(name).strip() or not _SHA256.fullmatch(str(value or ""))
        for name, value in artifacts.items()
    ):
        raise SelectorError("manifest preserved artifact hash is invalid")


def _actual_release_files(release_path: Path) -> dict[str, Path]:
    actual: dict[str, Path] = {}
    for root, directories, filenames in os.walk(release_path, followlinks=False):
        root_path = Path(root)
        root_stat = root_path.stat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or root_stat.st_mode & 0o222
        ):
            raise SelectorError("release directories must be trusted and read-only")
        for directory in list(directories):
            candidate = root_path / directory
            if candidate.is_symlink():
                raise SelectorError("release contains a symlinked directory")
        for filename in filenames:
            candidate = root_path / filename
            relative = candidate.relative_to(release_path).as_posix()
            if relative == MANIFEST_NAME:
                continue
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise SelectorError("release file disappeared during verification") from exc
            if stat.S_ISLNK(mode):
                raise SelectorError("release contains a symlinked file")
            opened = candidate.stat()
            if (
                not stat.S_ISREG(mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
            ):
                raise SelectorError("release contains an untrusted non-regular file")
            if opened.st_mode & 0o222:
                raise SelectorError("release files must be read-only")
            actual[relative] = candidate
    return actual


def _safe_agent_source_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise SelectorError("agent source manifest file path is invalid")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SelectorError("agent source manifest file path escapes the snapshot")
    normalized = relative.as_posix()
    if normalized != raw:
        raise SelectorError("agent source manifest file path is not canonical")
    return normalized


def _actual_agent_source_files(source_path: Path) -> dict[str, Path]:
    actual: dict[str, Path] = {}
    for root, directories, filenames in os.walk(source_path, followlinks=False):
        root_path = Path(root)
        try:
            root_mode = root_path.lstat().st_mode
            root_stat = root_path.stat()
        except OSError as exc:
            raise SelectorError(
                "agent source directory disappeared during verification"
            ) from exc
        if (
            not stat.S_ISDIR(root_mode)
            or root_stat.st_uid != os.getuid()
            or root_stat.st_mode & 0o222
        ):
            raise SelectorError(
                "agent source directories must be trusted and read-only"
            )
        for directory in list(directories):
            candidate = root_path / directory
            if candidate.is_symlink():
                raise SelectorError("agent source contains a symlinked directory")
        for filename in filenames:
            candidate = root_path / filename
            relative = candidate.relative_to(source_path).as_posix()
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise SelectorError(
                    "agent source file disappeared during verification"
                ) from exc
            if stat.S_ISLNK(mode):
                raise SelectorError("agent source contains a symlinked file")
            opened = candidate.stat()
            if (
                not stat.S_ISREG(mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
            ):
                raise SelectorError(
                    "agent source contains an untrusted non-regular file"
                )
            if opened.st_mode & 0o222:
                raise SelectorError("agent source files must be read-only")
            actual[relative] = candidate
    return actual


def _sha256_agent_source_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or opened.st_mode & 0o222
            ):
                raise SelectorError(
                    "agent source file is not a trusted read-only regular file"
                )
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except SelectorError:
        raise
    except OSError as exc:
        raise SelectorError("agent source file could not be verified") from exc


def verify_agent_source(spec: object) -> dict:
    """Verify one detached, content-addressed Hermes Agent source snapshot."""
    if not isinstance(spec, dict) or set(spec) != _AGENT_SOURCE_IDENTITY_KEYS:
        raise SelectorError("agent source identity is missing or invalid")
    manifest_sha256 = str(spec.get("manifest_sha256") or "")
    if not _SHA256.fullmatch(manifest_sha256):
        raise SelectorError("agent source manifest hash is invalid")

    configured = Path(str(spec.get("path") or ""))
    if (
        not configured.is_absolute()
        or Path(os.path.abspath(configured)) != configured
    ):
        raise SelectorError("agent source path must be absolute and canonical")
    source_path = _canonical_existing_directory(
        configured,
        label="agent source path",
        trusted=True,
        read_only=True,
    )
    if str(source_path) != str(spec.get("resolved_path") or ""):
        raise SelectorError("agent source resolved path mismatch")
    if source_path.name != manifest_sha256:
        raise SelectorError("agent source path is not content-addressed")
    snapshots_root = _canonical_existing_directory(
        source_path.parent,
        label="agent source snapshots root",
        trusted=True,
    )
    if snapshots_root.name != "snapshots":
        raise SelectorError("agent source snapshot hierarchy is invalid")
    release_root = _canonical_existing_directory(
        snapshots_root.parent,
        label="agent source release root",
        trusted=True,
    )
    manifests_root = _canonical_existing_directory(
        release_root / "manifests",
        label="agent source manifests root",
        trusted=True,
    )

    manifest_path = Path(str(spec.get("manifest_path") or ""))
    if (
        not manifest_path.is_absolute()
        or Path(os.path.abspath(manifest_path)) != manifest_path
        or manifest_path.is_symlink()
        or manifest_path.parent != manifests_root
        or manifest_path.name != f"{manifest_sha256}.json"
    ):
        raise SelectorError("agent source detached manifest path is invalid")
    try:
        if manifest_path.parent.resolve(strict=True) != manifest_path.parent:
            raise SelectorError("agent source manifest parent is not canonical")
    except OSError as exc:
        raise SelectorError("agent source manifest parent is missing") from exc

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(manifest_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or opened.st_mode & 0o222
            ):
                raise SelectorError(
                    "agent source manifest must be a trusted read-only regular file"
                )
            raw_manifest = handle.read(16 * 1024 * 1024 + 1)
    except SelectorError:
        raise
    except OSError as exc:
        raise SelectorError("agent source manifest is missing") from exc
    if len(raw_manifest) > 16 * 1024 * 1024:
        raise SelectorError("agent source manifest is too large")
    if hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
        raise SelectorError("agent source manifest hash mismatch")
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorError("agent source manifest JSON is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _AGENT_SOURCE_MANIFEST_KEYS
        or manifest.get("version") != MANIFEST_VERSION
    ):
        raise SelectorError("agent source manifest schema is invalid")
    commit = str(manifest.get("commit") or "")
    tree = str(manifest.get("tree") or "")
    base_commit = str(manifest.get("base_commit") or "")
    if not _HEX_ID.fullmatch(commit) or not _HEX_ID.fullmatch(tree):
        raise SelectorError("agent source commit or tree identity is invalid")
    if not _HEX_ID.fullmatch(base_commit):
        raise SelectorError("agent source base commit identity is invalid")
    origin_url = manifest.get("origin_url")
    if not isinstance(origin_url, str) or not origin_url.strip() or "\n" in origin_url:
        raise SelectorError("agent source origin identity is invalid")
    changed_files = manifest.get("changed_files")
    if not isinstance(changed_files, list) or changed_files != sorted(set(changed_files)):
        raise SelectorError("agent source changed files are invalid")
    for changed_path in changed_files:
        _safe_agent_source_relative_path(changed_path)
    if commit != str(spec.get("commit") or "") or tree != str(spec.get("tree") or ""):
        raise SelectorError("agent source commit or tree identity mismatch")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise SelectorError("agent source manifest files map is invalid")
    expected_files: dict[str, str] = {}
    for raw_path, raw_hash in raw_files.items():
        relative = _safe_agent_source_relative_path(raw_path)
        expected_hash = str(raw_hash or "")
        if not _SHA256.fullmatch(expected_hash):
            raise SelectorError("agent source manifest file hash is invalid")
        if relative in expected_files:
            raise SelectorError("agent source manifest file path is duplicated")
        expected_files[relative] = expected_hash
    if "run_agent.py" not in expected_files:
        raise SelectorError("agent source snapshot has no run_agent.py")

    actual_files = _actual_agent_source_files(source_path)
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    if missing:
        raise SelectorError("agent source manifest has a missing file")
    if extra:
        raise SelectorError("agent source manifest has an extra file")
    for relative, expected_hash in expected_files.items():
        if _sha256_agent_source_file(actual_files[relative]) != expected_hash:
            raise SelectorError("agent source manifest file hash mismatch")

    return {
        "path": str(source_path),
        "resolved_path": str(source_path),
        "commit": commit,
        "tree": tree,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }


def verify_runtime(spec: object) -> dict:
    """Verify a complete read-only content-addressed Python runtime closure."""
    if not isinstance(spec, dict) or set(spec) != _RUNTIME_IDENTITY_KEYS:
        raise SelectorError("runtime identity is missing or invalid")
    manifest_sha256 = str(spec.get("manifest_sha256") or "")
    if not _SHA256.fullmatch(manifest_sha256):
        raise SelectorError("runtime manifest hash is invalid")
    runtime_path = _canonical_existing_directory(
        Path(str(spec.get("path") or "")),
        label="runtime path",
        trusted=True,
        read_only=True,
    )
    if str(runtime_path) != str(spec.get("resolved_path") or ""):
        raise SelectorError("runtime resolved path mismatch")
    if runtime_path.name != manifest_sha256 or runtime_path.parent.name != "snapshots":
        raise SelectorError("runtime path is not content-addressed")
    release_root = _canonical_existing_directory(
        runtime_path.parent.parent,
        label="runtime release root",
        trusted=True,
    )
    manifests_root = _canonical_existing_directory(
        release_root / "manifests",
        label="runtime manifests root",
        trusted=True,
    )
    manifest_path = Path(str(spec.get("manifest_path") or ""))
    if (
        not manifest_path.is_absolute()
        or Path(os.path.abspath(manifest_path)) != manifest_path
        or manifest_path.is_symlink()
        or manifest_path.parent != manifests_root
        or manifest_path.name != f"{manifest_sha256}.json"
    ):
        raise SelectorError("runtime detached manifest path is invalid")
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise SelectorError("runtime manifest is missing") from exc
    opened_manifest = manifest_path.stat()
    if (
        not stat.S_ISREG(opened_manifest.st_mode)
        or opened_manifest.st_uid != os.getuid()
        or opened_manifest.st_mode & 0o222
        or len(raw) > 32 * 1024 * 1024
        or hashlib.sha256(raw).hexdigest() != manifest_sha256
    ):
        raise SelectorError("runtime manifest hash or trust check failed")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorError("runtime manifest JSON is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _RUNTIME_MANIFEST_KEYS
        or manifest.get("version") != MANIFEST_VERSION
    ):
        raise SelectorError("runtime manifest schema is invalid")
    interpreter_relative = _safe_agent_source_relative_path(
        manifest.get("interpreter_relative_path")
    )
    site_relative = _safe_agent_source_relative_path(
        manifest.get("site_packages_relative_path")
    )
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise SelectorError("runtime manifest files map is invalid")
    expected_files = {}
    for raw_path, raw_hash in raw_files.items():
        relative = _safe_agent_source_relative_path(raw_path)
        if not _SHA256.fullmatch(str(raw_hash or "")):
            raise SelectorError("runtime manifest file hash is invalid")
        expected_files[relative] = str(raw_hash)
    actual_files = _actual_agent_source_files(runtime_path)
    if set(actual_files) != set(expected_files):
        raise SelectorError("runtime manifest file set mismatch")
    for relative, expected_hash in expected_files.items():
        if _sha256_agent_source_file(actual_files[relative]) != expected_hash:
            raise SelectorError("runtime file hash mismatch")
    interpreter = runtime_path / interpreter_relative
    python_home = runtime_path / "python-home"
    site_packages = runtime_path / site_relative
    if (
        interpreter_relative not in expected_files
        or not interpreter.is_file()
        or not interpreter.stat().st_mode & 0o111
        or not python_home.is_dir()
        or not site_packages.is_dir()
    ):
        raise SelectorError("runtime interpreter or dependency layout is invalid")
    expected_identity_paths = {
        "python_home_path": str(python_home),
        "site_packages_path": str(site_packages),
        "interpreter_path": str(interpreter),
        "interpreter_resolved_path": str(interpreter.resolve(strict=True)),
    }
    for key, value in expected_identity_paths.items():
        if str(spec.get(key) or "") != value:
            raise SelectorError(f"runtime identity mismatch: {key}")
    return {
        "path": str(runtime_path),
        "resolved_path": str(runtime_path),
        **expected_identity_paths,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
    }


def verify_release(
    release_path: Path | str,
    *,
    release_root: Path | str,
    expected_manifest_sha256: str,
    selector_path: Path | str | None,
    verify_selector_identity: bool = True,
) -> dict:
    """Verify a complete WebUI release and its paired Agent/runtime identities."""
    root = _canonical_existing_directory(
        Path(release_root), label="release root", trusted=True
    )
    candidate = Path(release_path)
    if not candidate.is_absolute():
        raise SelectorError("release path must be absolute")
    if candidate.is_symlink():
        raise SelectorError("release path must not be a symlink")
    try:
        resolved_release = candidate.resolve(strict=True)
    except OSError as exc:
        raise SelectorError("release path is missing") from exc
    if resolved_release.parent != root:
        raise SelectorError("release path escapes the release root")
    if Path(os.path.abspath(candidate)) != resolved_release:
        raise SelectorError("release path must be canonical and symlink-free")
    _canonical_existing_directory(
        resolved_release,
        label="release path",
        trusted=True,
        read_only=True,
    )

    manifest, manifest_hash = _load_manifest(
        resolved_release,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    _validate_release_admission(manifest)
    build_id = manifest.get("build_id")
    if not isinstance(build_id, str) or not build_id or "/" in build_id:
        raise SelectorError("manifest build id is invalid")
    if build_id != resolved_release.name:
        raise SelectorError("manifest build id does not match the release path")
    commit = str(manifest.get("commit") or "")
    tree = str(manifest.get("tree") or "")
    if not _HEX_ID.fullmatch(commit) or not _HEX_ID.fullmatch(tree):
        raise SelectorError("manifest commit or tree identity is invalid")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise SelectorError("manifest files map is invalid")
    expected_files: dict[str, str] = {}
    for raw_path, raw_hash in raw_files.items():
        relative = _safe_manifest_relative_path(raw_path)
        expected_hash = str(raw_hash or "")
        if not _SHA256.fullmatch(expected_hash):
            raise SelectorError("manifest file hash is invalid")
        if relative in expected_files:
            raise SelectorError("manifest file path is duplicated")
        expected_files[relative] = expected_hash

    actual_files = _actual_release_files(resolved_release)
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    if missing:
        raise SelectorError("manifest missing release file")
    if extra:
        raise SelectorError("manifest extra release file")
    for relative, expected_hash in expected_files.items():
        if sha256_file(actual_files[relative]) != expected_hash:
            raise SelectorError("manifest file hash mismatch")

    if verify_selector_identity:
        configured_selector, resolved_selector = _verify_external_identity(
            manifest.get("selector"), label="selector"
        )
        if selector_path is None:
            raise SelectorError("selector invocation path is missing")
        supplied_selector = Path(selector_path)
        if not supplied_selector.is_absolute():
            raise SelectorError("selector invocation path must be absolute")
        if Path(os.path.abspath(supplied_selector)) != supplied_selector:
            raise SelectorError("selector invocation path must be canonical")
        if supplied_selector != configured_selector:
            raise SelectorError("selector invocation path does not match configured path")
        try:
            supplied_selector_resolved = supplied_selector.resolve(strict=True)
        except OSError as exc:
            raise SelectorError("selector path is missing") from exc
        if supplied_selector_resolved != resolved_selector:
            raise SelectorError("selector invocation path mismatch")
    else:
        configured_selector, resolved_selector = _declared_external_identity(
            manifest.get("selector"), label="selector"
        )
    configured_interpreter, resolved_interpreter = _verify_external_identity(
        manifest.get("interpreter"),
        label="interpreter",
        allow_configured_symlink=False,
    )
    runtime = verify_runtime(manifest.get("runtime"))
    if (
        str(configured_interpreter) != runtime["interpreter_path"]
        or str(resolved_interpreter) != runtime["interpreter_resolved_path"]
        or sha256_file(resolved_interpreter)
        != str(manifest.get("interpreter", {}).get("sha256") or "")
    ):
        raise SelectorError("release interpreter is outside the sealed runtime")
    agent_source = verify_agent_source(manifest.get("agent_source"))

    return {
        "build_id": build_id,
        "commit": commit,
        "tree": tree,
        "manifest_sha256": manifest_hash,
        "release_path": str(resolved_release),
        "selector_path": str(configured_selector),
        "selector_resolved_path": str(resolved_selector),
        "selector_verified": verify_selector_identity,
        "interpreter_path": str(configured_interpreter),
        "interpreter_resolved_path": str(resolved_interpreter),
        "runtime_path": runtime["path"],
        "runtime_resolved_path": runtime["resolved_path"],
        "runtime_python_home_path": runtime["python_home_path"],
        "runtime_site_packages_path": runtime["site_packages_path"],
        "runtime_manifest_path": runtime["manifest_path"],
        "runtime_manifest_sha256": runtime["manifest_sha256"],
        "agent_source_path": agent_source["path"],
        "agent_source_resolved_path": agent_source["resolved_path"],
        "agent_source_commit": agent_source["commit"],
        "agent_source_tree": agent_source["tree"],
        "agent_source_manifest_path": agent_source["manifest_path"],
        "agent_source_manifest_sha256": agent_source["manifest_sha256"],
    }


def _validate_release_record(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise SelectorError("selector release record is invalid")
    manifest_hash = str(raw.get("manifest_sha256") or "")
    commit = str(raw.get("commit") or "")
    tree = str(raw.get("tree") or "")
    if not _SHA256.fullmatch(manifest_hash):
        raise SelectorError("selector release manifest hash is invalid")
    if not _HEX_ID.fullmatch(commit) or not _HEX_ID.fullmatch(tree):
        raise SelectorError("selector release commit or tree is invalid")
    return {
        "manifest_sha256": manifest_hash,
        "commit": commit,
        "tree": tree,
    }


def _validate_state(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("version") not in {
        LEGACY_STATE_VERSION,
        STATE_VERSION,
    }:
        raise SelectorError("selector state version is unsupported")
    source_version = int(raw["version"])
    generation = raw.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise SelectorError("selector state generation is invalid")
    release_root = Path(str(raw.get("release_root") or ""))
    if not release_root.is_absolute():
        raise SelectorError("selector release root is invalid")
    releases_raw = raw.get("releases")
    if not isinstance(releases_raw, dict) or not releases_raw:
        raise SelectorError("selector releases map is invalid")
    releases: dict[str, dict] = {}
    for build_id, record in releases_raw.items():
        if not isinstance(build_id, str) or not build_id or "/" in build_id or build_id in {".", ".."}:
            raise SelectorError("selector build id is invalid")
        releases[build_id] = _validate_release_record(record)
    current = raw.get("current")
    last_good = raw.get("last_good")
    bootstrap_fallback = raw.get("bootstrap_fallback")
    candidate = raw.get("candidate")
    for label, build_id in (
        ("current", current),
        ("last_good", last_good),
        ("bootstrap fallback", bootstrap_fallback),
    ):
        if build_id not in releases:
            raise SelectorError(f"selector {label} release is unknown")
    if candidate is not None and candidate not in releases:
        raise SelectorError("selector candidate release is unknown")
    pending_transaction_id = (
        raw.get("pending_transaction_id")
        if source_version == STATE_VERSION
        else None
    )
    if pending_transaction_id is not None and not _TRANSACTION_ID.fullmatch(
        str(pending_transaction_id)
    ):
        raise SelectorError("selector pending transaction identity is invalid")
    if pending_transaction_id is not None and candidate is None:
        raise SelectorError("selector pending transaction has no candidate")
    return {
        "version": STATE_VERSION,
        "generation": generation,
        "release_root": str(release_root),
        "current": current,
        "candidate": candidate,
        "pending_transaction_id": pending_transaction_id,
        "last_good": last_good,
        "bootstrap_fallback": bootstrap_fallback,
        "releases": releases,
    }


def _ensure_control_parent(parent: Path, *, create: bool) -> None:
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SelectorError("selector control path has a symlinked ancestor")
        if current.exists():
            if not current.is_dir():
                raise SelectorError("selector control ancestor is not a directory")
            continue
        if not create:
            raise SelectorError("selector control directory is missing")
        current.mkdir(mode=0o755)
    if parent.resolve(strict=True) != parent:
        raise SelectorError("selector control directory must be canonical")
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
        raise SelectorError("selector control directory ownership or mode is unsafe")


def _state_lock_paths(
    state_path: Path | str,
    lock_path: Path | str,
    *,
    create_parent: bool = False,
) -> tuple[Path, Path]:
    state = Path(state_path)
    lock = Path(lock_path)
    if not state.is_absolute() or not lock.is_absolute():
        raise SelectorError("selector state and lock paths must be absolute")
    if state.parent != lock.parent:
        raise SelectorError("selector state and lock must share one parent")
    _ensure_control_parent(state.parent, create=create_parent)
    if state.is_symlink() or lock.is_symlink():
        raise SelectorError("selector state and lock must not be symlinks")
    return state, lock


def _read_state_unlocked(state_path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(state_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise SelectorError(
                    "selector state is not a private mode 0600 regular file"
                )
            payload = handle.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise SelectorError("selector state is too large")
        raw = json.loads(payload)
    except SelectorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorError("selector state is unreadable") from exc
    state = _validate_state(raw)
    _validate_state_release_root(state, state_path)
    return state


def _validate_state_release_root(state: dict, state_path: Path) -> Path:
    allowed = state_path.parent / "releases"
    try:
        canonical_allowed = _canonical_existing_directory(
            allowed,
            label="allowlisted release root",
            trusted=True,
        )
        configured = _canonical_existing_directory(
            Path(state["release_root"]),
            label="selector release root",
            trusted=True,
        )
    except OSError as exc:
        raise SelectorError("selector release root is unavailable") from exc
    if configured != canonical_allowed:
        raise SelectorError("selector release root is not allowlisted")
    return configured


def _atomic_write_state(
    state_path: Path,
    state: dict,
    *,
    crash_at: str | None = None,
) -> None:
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temp_name: str | None = None
    fd, temp_name = tempfile.mkstemp(prefix=f".{state_path.name}.", dir=state_path.parent)
    replaced = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if crash_at == "after_temp_fsync":
            raise InjectedCrash(crash_at)
        os.replace(temp_name, state_path)
        replaced = True
        if crash_at == "after_replace":
            raise InjectedCrash(crash_at)
        directory_fd = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if not replaced and temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _with_lock(lock_path: Path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    os.set_inheritable(descriptor, False)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise SelectorError("selector lock is not a private regular file")
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def initialize_selector_state(
    state_path: Path | str,
    *,
    lock_path: Path | str,
    release_root: Path | str,
    bootstrap_build_id: str,
    bootstrap_record: dict,
) -> dict:
    state_path, lock_path = _state_lock_paths(
        state_path, lock_path, create_parent=True
    )
    state = _validate_state(
        {
            "version": STATE_VERSION,
            "generation": 0,
            "release_root": str(Path(release_root).absolute()),
            "current": bootstrap_build_id,
            "candidate": None,
            "pending_transaction_id": None,
            "last_good": bootstrap_build_id,
            "bootstrap_fallback": bootstrap_build_id,
            "releases": {bootstrap_build_id: bootstrap_record},
        }
    )
    _validate_state_release_root(state, state_path)
    with _with_lock(lock_path) as lock_handle:
        if state_path.exists():
            raise SelectorError("selector state already exists")
        _atomic_write_state(state_path, state)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return state


def read_selector_state(
    state_path: Path | str,
    *,
    lock_path: Path | str,
) -> dict:
    state_path, lock_path = _state_lock_paths(state_path, lock_path)
    with _with_lock(lock_path) as lock_handle:
        state = _read_state_unlocked(state_path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return state


def update_selector_state(
    state_path: Path | str,
    *,
    lock_path: Path | str,
    expected_generation: int,
    transition: Callable[[dict], dict],
    crash_at: str | None = None,
) -> dict:
    state_path, lock_path = _state_lock_paths(state_path, lock_path)
    with _with_lock(lock_path) as lock_handle:
        current = _read_state_unlocked(state_path)
        if current["generation"] != expected_generation:
            raise SelectorError("selector state generation conflict")
        proposed = _validate_state(transition(copy.deepcopy(current)))
        if proposed["release_root"] != current["release_root"]:
            raise SelectorError("selector release root is immutable")
        if proposed["bootstrap_fallback"] != current["bootstrap_fallback"]:
            raise SelectorError("selector bootstrap fallback is immutable")
        for build_id, record in current["releases"].items():
            if proposed["releases"].get(build_id) != record:
                raise SelectorError("selector release records are immutable")
        if transition is activate_candidate and proposed == current:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return current
        proposed["generation"] = current["generation"] + 1
        proposed = _validate_state(proposed)
        _atomic_write_state(state_path, proposed, crash_at=crash_at)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return proposed


def selector_state_sha256(state: dict) -> str:
    """Digest one canonical validated selector-state value."""
    canonical = _validate_state(copy.deepcopy(state))
    return hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def prune_idle_selector_releases(
    state_path: Path | str,
    *,
    lock_path: Path | str,
    expected_generation: int,
    expected_state_sha256: str,
    expected_current: str,
    expected_last_good: str,
    crash_at: str | None = None,
) -> dict:
    """CAS an idle selector to exactly current plus one prior rollback."""
    if not _SHA256.fullmatch(str(expected_state_sha256 or "")):
        raise SelectorError("selector expected state digest is invalid")
    state_path, lock_path = _state_lock_paths(state_path, lock_path)
    with _with_lock(lock_path) as lock_handle:
        current = _read_state_unlocked(state_path)
        if current["generation"] != expected_generation:
            raise SelectorError("selector state generation conflict")
        if selector_state_sha256(current) != expected_state_sha256:
            raise SelectorError("selector state digest conflict")
        if current["candidate"] is not None or (
            current["pending_transaction_id"] is not None
        ):
            raise SelectorError("selector must be idle before release pruning")
        if (
            current["current"] != expected_current
            or current["last_good"] != expected_last_good
            or expected_current not in current["releases"]
            or expected_last_good not in current["releases"]
        ):
            raise SelectorError("selector protected release identity changed")
        if expected_current == expected_last_good:
            raise SelectorError(
                "selector current and last-good releases must differ"
            )
        proposed = copy.deepcopy(current)
        proposed["releases"] = {
            expected_current: copy.deepcopy(
                current["releases"][expected_current]
            ),
            expected_last_good: copy.deepcopy(
                current["releases"][expected_last_good]
            ),
        }
        proposed["bootstrap_fallback"] = expected_last_good
        proposed["generation"] = current["generation"] + 1
        proposed = _validate_state(proposed)
        _atomic_write_state(state_path, proposed, crash_at=crash_at)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return proposed


def stage_candidate(
    state: dict,
    build_id: str,
    release_record: dict,
    *,
    transaction_id: str | None = None,
) -> dict:
    next_state = copy.deepcopy(state)
    record = _validate_release_record(release_record)
    existing = next_state["releases"].get(build_id)
    if existing is not None and existing != record:
        raise SelectorError("selector candidate build id already has another identity")
    next_state["releases"][build_id] = record
    next_state["candidate"] = build_id
    next_state["pending_transaction_id"] = transaction_id
    return next_state


def activate_candidate(state: dict) -> dict:
    next_state = copy.deepcopy(state)
    if next_state.get("candidate") is None:
        raise SelectorError("selector has no staged candidate")
    transaction_id = str(next_state.get("pending_transaction_id") or "")
    if not _TRANSACTION_ID.fullmatch(transaction_id):
        raise SelectorError("selector candidate startup transaction is invalid")
    prior_current = next_state["current"]
    if prior_current != next_state["candidate"]:
        next_state["last_good"] = prior_current
        next_state["current"] = next_state["candidate"]
    return next_state


def promote_candidate(state: dict) -> dict:
    next_state = copy.deepcopy(state)
    candidate = next_state.get("candidate")
    if candidate is None or next_state.get("current") != candidate:
        raise SelectorError("selector candidate is not active")
    next_state["candidate"] = None
    next_state["pending_transaction_id"] = None
    return next_state


def rollback_to_last_good(state: dict) -> dict:
    next_state = copy.deepcopy(state)
    next_state["current"] = next_state["last_good"]
    next_state["candidate"] = None
    next_state["pending_transaction_id"] = None
    return next_state


def release_pair_id(
    identity: dict,
    *,
    selector_generation: int,
    transaction_id: str,
) -> str:
    """Return the deterministic sealed WebUI/Agent/runtime pair identity."""
    if (
        not isinstance(identity, dict)
        or not isinstance(selector_generation, int)
        or isinstance(selector_generation, bool)
        or selector_generation <= 0
        or not _TRANSACTION_ID.fullmatch(str(transaction_id or ""))
    ):
        raise SelectorError("release pair identity inputs are invalid")
    fields = {
        key: identity.get(key)
        for key in (
            "build_id",
            "commit",
            "tree",
            "manifest_sha256",
            "agent_source_commit",
            "agent_source_tree",
            "agent_source_manifest_sha256",
            "runtime_manifest_sha256",
        )
    }
    if any(not isinstance(value, str) or not value for value in fields.values()):
        raise SelectorError("release pair identity is incomplete")
    payload = {
        **fields,
        "selector_generation": selector_generation,
        "transaction_id": transaction_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"pair_{digest}"


def startup_journal_environment(
    release_root: Path | str,
    transaction_id: str,
) -> dict[str, str]:
    """Bind managed startup replay to private transaction-specific journals."""
    root = Path(release_root)
    if (
        not root.is_absolute()
        or Path(os.path.abspath(root)) != root
        or not _TRANSACTION_ID.fullmatch(str(transaction_id or ""))
    ):
        raise SelectorError("managed startup journal inputs are invalid")
    store_root = root.parent.parent if root.parent.name == "selector" else root.parent
    journal_root = store_root / "private" / "transactions"
    return {
        "HERMES_WEBUI_STARTUP_ATTEMPT_JOURNAL": str(
            journal_root / f"startup-attempt-{transaction_id}.json"
        ),
        "HERMES_WEBUI_STARTUP_CONFIGURATION_JOURNAL": str(
            journal_root / f"startup-configuration-{transaction_id}.json"
        ),
    }


def _selection_from_state(state: dict, *, selector_path: Path | str) -> dict:
    build_id = state["current"]
    record = state["releases"][build_id]
    release_path = Path(state["release_root"]) / build_id
    identity = verify_release(
        release_path,
        release_root=state["release_root"],
        expected_manifest_sha256=record["manifest_sha256"],
        selector_path=selector_path,
    )
    if identity["commit"] != record["commit"] or identity["tree"] != record["tree"]:
        raise SelectorError("selector release record does not match its manifest")
    bootstrap = Path(identity["release_path"]) / "bootstrap.py"
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise SelectorError("selected release bootstrap is invalid")
    environment = {
        "HERMES_WEBUI_RELEASE_ROOT": str(Path(state["release_root"]).resolve()),
        "HERMES_WEBUI_RELEASE_PATH": identity["release_path"],
        "HERMES_WEBUI_MANIFEST_SHA256": identity["manifest_sha256"],
        "HERMES_WEBUI_SELECTOR_GENERATION": str(state["generation"]),
        "HERMES_WEBUI_SELECTOR_PATH": identity["selector_path"],
        "HERMES_WEBUI_INTERPRETER_PATH": identity["interpreter_path"],
        "HERMES_WEBUI_LAUNCH_MODE": "selector",
        "HERMES_WEBUI_AGENT_DIR": identity["agent_source_path"],
        "HERMES_WEBUI_AGENT_COMMIT": identity["agent_source_commit"],
        "HERMES_WEBUI_AGENT_TREE": identity["agent_source_tree"],
        "HERMES_WEBUI_AGENT_MANIFEST_PATH": identity[
            "agent_source_manifest_path"
        ],
        "HERMES_WEBUI_AGENT_MANIFEST_SHA256": identity[
            "agent_source_manifest_sha256"
        ],
        "HERMES_WEBUI_RUNTIME_PATH": identity["runtime_path"],
        "HERMES_WEBUI_RUNTIME_PYTHON_HOME": identity[
            "runtime_python_home_path"
        ],
        "HERMES_WEBUI_RUNTIME_SITE_PACKAGES": identity[
            "runtime_site_packages_path"
        ],
        "HERMES_WEBUI_RUNTIME_MANIFEST_PATH": identity[
            "runtime_manifest_path"
        ],
        "HERMES_WEBUI_RUNTIME_MANIFEST_SHA256": identity[
            "runtime_manifest_sha256"
        ],
        "HERMES_WEBUI_AUTO_INSTALL": "0",
        "HERMES_WEBUI_PYTHON": identity["interpreter_path"],
        "HERMES_WEBUI_SERVER_CWD": identity["release_path"],
        "PYTHONHOME": identity["runtime_python_home_path"],
        "PYTHONPATH": os.pathsep.join(
            [identity["agent_source_path"], identity["runtime_site_packages_path"]]
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    pair_transaction_id = str(state.get("pending_transaction_id") or "")
    if _TRANSACTION_ID.fullmatch(pair_transaction_id):
        environment["HERMES_WEBUI_RELEASE_TRANSACTION_ID"] = pair_transaction_id
        environment["HERMES_WEBUI_RELEASE_PAIR_ID"] = release_pair_id(
            identity,
            selector_generation=state["generation"],
            transaction_id=pair_transaction_id,
        )
    if state.get("candidate") == build_id:
        transaction_id = pair_transaction_id
        if not _TRANSACTION_ID.fullmatch(transaction_id):
            raise SelectorError("selected candidate startup transaction is invalid")
        environment["HERMES_WEBUI_STARTUP_FENCED"] = "1"
        environment["HERMES_WEBUI_STARTUP_TRANSACTION_ID"] = transaction_id
        environment.update(
            startup_journal_environment(state["release_root"], transaction_id)
        )
    return {
        "build_id": build_id,
        "release_path": Path(identity["release_path"]),
        "bootstrap": bootstrap,
        "interpreter": Path(identity["interpreter_path"]),
        "environment": environment,
        "identity": identity,
    }


def _resolve_selection_unlocked(
    state_path: Path,
    *,
    selector_path: Path | str,
) -> dict:
    return _selection_from_state(
        _read_state_unlocked(state_path),
        selector_path=selector_path,
    )


def resolve_selection(
    state_path: Path | str,
    *,
    lock_path: Path | str,
    selector_path: Path | str,
) -> dict:
    """Resolve and attest one selection under the state lock.

    Callers that only inspect a selection release the lock when this returns.
    The executable entry point below instead keeps the same lock through the
    exec transition so state changes have an unambiguous ordering.
    """
    state_path, lock_path = _state_lock_paths(state_path, lock_path)
    with _with_lock(lock_path) as lock_handle:
        selected = _resolve_selection_unlocked(
            state_path,
            selector_path=selector_path,
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    default_state = Path.home() / ".hermes" / "webui" / "release-selector.json"
    parser.add_argument(
        "--selector-state",
        default=os.environ.get("HERMES_WEBUI_SELECTOR_STATE", str(default_state)),
    )
    parser.add_argument("--selector-lock", default=None)
    parser.add_argument(
        "--launchd-label",
        default=os.environ.get("HERMES_WEBUI_LAUNCHD_LABEL"),
    )
    options, bootstrap_args = parser.parse_known_args(argv)
    state_path = Path(options.selector_state)
    lock_path = Path(options.selector_lock) if options.selector_lock else state_path.with_suffix(".lock")
    try:
        state_path, lock_path = _state_lock_paths(state_path, lock_path)
        launchd_label = str(options.launchd_label or "")
        if not _LAUNCHD_LABEL.fullmatch(launchd_label):
            raise SelectorError("launchd label is missing or invalid")
        with _with_lock(lock_path):
            selected = _resolve_selection_unlocked(
                state_path,
                selector_path=Path(__file__),
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in _ALLOWED_INHERITED_ENV_KEYS
            }
            environment.update(selected["environment"])
            environment["HERMES_WEBUI_SELECTOR_STATE"] = str(state_path)
            environment["HERMES_WEBUI_SELECTOR_LOCK"] = str(lock_path)
            environment["HERMES_WEBUI_LAUNCHD_LABEL"] = launchd_label
            environment["HOME"] = str(environment.get("HOME") or Path.home())
            environment["PATH"] = (
                f"{Path(selected['interpreter']).parent}:"
                "/usr/bin:/bin:/usr/sbin:/sbin"
            )
            environment["PYTHONNOUSERSITE"] = "1"
            os.chdir(selected["release_path"])
            interpreter = str(selected["interpreter"])
            os.execve(
                interpreter,
                [interpreter, "-S", str(selected["bootstrap"]), *bootstrap_args],
                environment,
            )
    except SelectorError as exc:
        print(f"Hermes WebUI selector refused startup: {exc}", file=sys.stderr)
        return 78
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
