"""
Plugin discovery and static serving for Hermes Web UI.

Scans ~/.hermes/plugins/<name>/dashboard/ for manifest.json files,
matching the official Hermes dashboard plugin format.

Each plugin may have:
  dashboard/
    manifest.json   -- tab definition and entry point
    dist/
      index.js      -- plugin JS bundle (IIFE)
      style.css     -- optional plugin stylesheet
    plugin_api.py   -- optional backend API (not used in WebUI MVP)
"""
import json
import hashlib
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

logger = logging.getLogger(__name__)

# Valid dashboard-plugin name: a safe slug (it becomes a URL path component and
# a settings key). Lowercase alnum + - / _, 1-64 chars, must start with a letter.
_VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Valid tab.path: a clean same-origin absolute path. Must start with a single
# '/' (NOT '//' — a leading '//' is a protocol-relative URL that would resolve
# to a remote origin when assigned to iframe.src), then only safe path chars —
# no quotes, whitespace, control chars, query ('?') or fragment ('#').
_VALID_PLUGIN_TAB_PATH = re.compile(r"^/(?!/)[A-Za-z0-9._~/-]{0,255}$")

# plugin_name -> manifest dict (as loaded from manifest.json)
PLUGIN_MANIFESTS: dict[str, dict] = {}

# plugin_name -> resolved static root dir
_PLUGIN_STATIC_ROOTS: dict[str, Path] = {}
_MANAGED_PLUGIN_LOCK = threading.Lock()
_MANAGED_PLUGIN_OPEN_DIR_FD = os.open in os.supports_dir_fd
_MANAGED_PLUGIN_STAT_DIR_FD = os.stat in os.supports_dir_fd
_MANAGED_PLUGIN_STAT_NOFOLLOW = os.stat in os.supports_follow_symlinks
_MANAGED_PLUGIN_MAX_ASSET_FILES = 4096
_MANAGED_PLUGIN_MAX_ASSET_BYTES = 16 * 1024 * 1024
_MANAGED_PLUGIN_MAX_TOTAL_ASSET_BYTES = 64 * 1024 * 1024
_MANAGED_PLUGIN_MAX_MANIFEST_FILES = 128
_MANAGED_PLUGIN_MAX_MANIFEST_BYTES = 256 * 1024
_MANAGED_PLUGIN_MAX_TOTAL_MANIFEST_BYTES = 4 * 1024 * 1024
_MANAGED_PLUGIN_MAX_DIRECTORIES = 1024
_MANAGED_PLUGIN_MAX_DIRECTORY_ENTRIES = 1024
_MANAGED_PLUGIN_MAX_TOTAL_DIRECTORY_ENTRIES = 8192
_MANAGED_PLUGIN_MAX_DEPTH = 16


class ManagedPluginSnapshotError(RuntimeError):
    """A managed plugin inventory could not be proven stable and valid."""


class ManagedPluginVerificationOutcome(str, Enum):
    PROVED_COMPLETE = "proved-complete"
    PROVED_ABSENT = "proved-absent"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManagedPluginSnapshotReceipt:
    plugin_root: str
    inventory_sha256: str
    names: tuple[str, ...]
    ignored_names: tuple[str, ...]
    asset_count: int
    total_asset_bytes: int
    max_asset_files: int
    max_asset_bytes: int
    max_total_asset_bytes: int
    manifest_count: int
    total_manifest_bytes: int
    max_manifest_files: int
    max_manifest_bytes: int
    max_total_manifest_bytes: int
    directory_count: int
    directory_entry_count: int
    max_directories: int
    max_directory_entries: int
    max_total_directory_entries: int
    max_depth: int


@dataclass(frozen=True)
class ManagedPluginVerification:
    outcome: ManagedPluginVerificationOutcome
    receipt: ManagedPluginSnapshotReceipt | None
    reason: str | None


@dataclass(frozen=True)
class _ManagedPluginEvidence:
    device: int
    inode: int
    mode: int
    owner: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _ManagedPluginSnapshot:
    receipt: ManagedPluginSnapshotReceipt
    manifests: dict[str, dict]
    static_roots: dict[str, Path]
    assets: dict[str, dict[str, bytes]]
    shared_assets: dict[str, bytes]


@dataclass
class _ManagedPluginAssetBudget:
    count: int = 0
    total_bytes: int = 0
    manifest_count: int = 0
    total_manifest_bytes: int = 0
    directory_count: int = 0
    directory_entry_count: int = 0


@dataclass(frozen=True)
class _ManagedIgnoredPlugin:
    directory: str
    plugin_evidence: _ManagedPluginEvidence
    plugin_entries: tuple[tuple[str, _ManagedPluginEvidence], ...]
    dashboard_evidence: _ManagedPluginEvidence | None
    dashboard_entries: tuple[tuple[str, _ManagedPluginEvidence], ...]


@dataclass(frozen=True)
class PluginRuntimeSnapshot:
    manifests: Mapping[str, dict]
    static_roots: Mapping[str, Path]
    assets: Mapping[str, Mapping[str, bytes]]
    shared_assets: Mapping[str, bytes]
    managed: bool


@dataclass(frozen=True)
class PluginPageMaterial:
    html: bytes | None
    has_index_js: bool


def _freeze_plugin_value(value):
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_plugin_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_plugin_value(item) for item in value)
    return value


def _plugin_runtime_snapshot(
    manifests: dict[str, dict],
    static_roots: dict[str, Path],
    assets: dict[str, dict[str, bytes]],
    shared_assets: dict[str, bytes] | None = None,
    *,
    managed: bool,
) -> PluginRuntimeSnapshot:
    return PluginRuntimeSnapshot(
        manifests=MappingProxyType(
            {
                name: _freeze_plugin_value(manifest)
                for name, manifest in manifests.items()
            }
        ),
        static_roots=MappingProxyType(dict(static_roots)),
        assets=MappingProxyType(
            {
                name: MappingProxyType(dict(plugin_assets))
                for name, plugin_assets in assets.items()
            }
        ),
        shared_assets=MappingProxyType(dict(shared_assets or {})),
        managed=managed,
    )


_PLUGIN_RUNTIME_STATE = _plugin_runtime_snapshot({}, {}, {}, managed=False)


def get_plugin_runtime_snapshot() -> PluginRuntimeSnapshot:
    """Return the one atomically-published plugin state reference."""

    return _PLUGIN_RUNTIME_STATE


def _managed_plugin_evidence(value: os.stat_result) -> _ManagedPluginEvidence:
    return _ManagedPluginEvidence(
        device=value.st_dev,
        inode=value.st_ino,
        mode=stat.S_IMODE(value.st_mode),
        owner=value.st_uid,
        link_count=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _managed_plugin_same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _managed_plugin_validate_dir(value: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise ManagedPluginSnapshotError(f"{label} is not a directory")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise ManagedPluginSnapshotError(f"{label} has the wrong owner")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise ManagedPluginSnapshotError(f"{label} is group/world writable")


def _managed_plugin_validate_file(value: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise ManagedPluginSnapshotError(f"{label} is not a regular file")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise ManagedPluginSnapshotError(f"{label} has the wrong owner")
    if value.st_nlink != 1:
        raise ManagedPluginSnapshotError(f"{label} has an unsafe link count")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise ManagedPluginSnapshotError(f"{label} is group/world writable")


def _managed_plugin_root_path() -> str:
    raw = os.environ.get(
        "HERMES_WEBUI_PLUGINS_DIR",
        str(Path.home() / ".hermes" / "plugins"),
    )
    return os.path.abspath(os.path.expanduser(raw))


def _managed_plugin_open_root(root: str) -> tuple[int, os.stat_result]:
    if not hasattr(os, "O_NOFOLLOW") or not all(
        (
            _MANAGED_PLUGIN_OPEN_DIR_FD,
            _MANAGED_PLUGIN_STAT_DIR_FD,
            _MANAGED_PLUGIN_STAT_NOFOLLOW,
        )
    ):
        raise ManagedPluginSnapshotError(
            "managed plugin discovery requires no-follow dir_fd primitives"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd: int | None = None
    try:
        path_stat = os.stat(root, follow_symlinks=False)
        root_fd = os.open(root, flags)
        try:
            fd_stat = os.fstat(root_fd)
        except BaseException:
            os.close(root_fd)
            root_fd = None
            raise
    except OSError as exc:
        raise ManagedPluginSnapshotError(
            "managed plugin root could not be opened safely"
        ) from exc
    try:
        _managed_plugin_validate_dir(path_stat, "managed plugin root")
        _managed_plugin_validate_dir(fd_stat, "managed plugin root")
        if not _managed_plugin_same_inode(path_stat, fd_stat):
            raise ManagedPluginSnapshotError(
                "managed plugin root identity changed while opening"
            )
        return root_fd, fd_stat
    except BaseException:
        os.close(root_fd)
        raise


def _managed_plugin_open_dir(parent_fd: int, name: str, label: str) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    child_fd: int | None = None
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            child_stat = os.fstat(child_fd)
        except BaseException:
            os.close(child_fd)
            child_fd = None
            raise
    except OSError as exc:
        raise ManagedPluginSnapshotError(f"{label} could not be opened safely") from exc
    try:
        _managed_plugin_validate_dir(entry_stat, label)
        _managed_plugin_validate_dir(child_stat, label)
        if not _managed_plugin_same_inode(entry_stat, child_stat):
            raise ManagedPluginSnapshotError(f"{label} identity changed while opening")
        return child_fd, child_stat
    except BaseException:
        os.close(child_fd)
        raise


def _managed_plugin_read_pass(fd: int) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise ManagedPluginSnapshotError("plugin manifest could not be reread") from exc
    chunks: list[bytes] = []
    total = 0
    while total <= _MANAGED_PLUGIN_MAX_MANIFEST_BYTES:
        try:
            chunk = os.read(
                fd,
                min(
                    65_536,
                    _MANAGED_PLUGIN_MAX_MANIFEST_BYTES + 1 - total,
                ),
            )
        except OSError as exc:
            raise ManagedPluginSnapshotError("plugin manifest could not be read") from exc
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > _MANAGED_PLUGIN_MAX_MANIFEST_BYTES:
        raise ManagedPluginSnapshotError("plugin manifest exceeds size limit")
    return b"".join(chunks)


def _managed_plugin_read_manifest(
    dashboard_fd: int,
) -> tuple[bytes, _ManagedPluginEvidence]:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        entry_before = os.stat(
            "manifest.json",
            dir_fd=dashboard_fd,
            follow_symlinks=False,
        )
        manifest_fd = os.open("manifest.json", flags, dir_fd=dashboard_fd)
    except OSError as exc:
        raise ManagedPluginSnapshotError("plugin manifest is missing or unsafe") from exc
    try:
        before = os.fstat(manifest_fd)
        _managed_plugin_validate_file(entry_before, "plugin manifest")
        _managed_plugin_validate_file(before, "plugin manifest")
        if not _managed_plugin_same_inode(entry_before, before):
            raise ManagedPluginSnapshotError("plugin manifest identity changed while opening")
        first = _managed_plugin_read_pass(manifest_fd)
        middle = os.fstat(manifest_fd)
        second = _managed_plugin_read_pass(manifest_fd)
        after = os.fstat(manifest_fd)
        entry_after = os.stat(
            "manifest.json",
            dir_fd=dashboard_fd,
            follow_symlinks=False,
        )
        evidence = _managed_plugin_evidence(before)
        if (
            first != second
            or evidence != _managed_plugin_evidence(middle)
            or evidence != _managed_plugin_evidence(after)
            or evidence != _managed_plugin_evidence(entry_after)
        ):
            raise ManagedPluginSnapshotError("plugin manifest changed during read")
        return first, evidence
    except OSError as exc:
        raise ManagedPluginSnapshotError("plugin manifest could not be verified") from exc
    finally:
        os.close(manifest_fd)


def _managed_plugin_json(raw: bytes) -> dict:
    def reject_duplicate_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManagedPluginSnapshotError(
                    f"plugin manifest contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    try:
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
        )
    except ManagedPluginSnapshotError:
        raise
    except Exception as exc:
        raise ManagedPluginSnapshotError("plugin manifest parse failed") from exc
    if not isinstance(manifest, dict):
        raise ManagedPluginSnapshotError("plugin manifest must be an object")
    return manifest


def _managed_plugin_read_asset(parent_fd: int, name: str, label: str) -> bytes:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        entry_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        asset_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ManagedPluginSnapshotError(f"{label} is missing or unsafe") from exc
    try:
        before = os.fstat(asset_fd)
        _managed_plugin_validate_file(entry_before, label)
        _managed_plugin_validate_file(before, label)
        if not _managed_plugin_same_inode(entry_before, before):
            raise ManagedPluginSnapshotError(f"{label} identity changed while opening")

        def read_pass() -> bytes:
            os.lseek(asset_fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            total = 0
            while total <= _MANAGED_PLUGIN_MAX_ASSET_BYTES:
                chunk = os.read(
                    asset_fd,
                    min(262_144, _MANAGED_PLUGIN_MAX_ASSET_BYTES + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > _MANAGED_PLUGIN_MAX_ASSET_BYTES:
                raise ManagedPluginSnapshotError(f"{label} exceeds size limit")
            return b"".join(chunks)

        first = read_pass()
        middle = os.fstat(asset_fd)
        second = read_pass()
        after = os.fstat(asset_fd)
        entry_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        evidence = _managed_plugin_evidence(before)
        if (
            first != second
            or evidence != _managed_plugin_evidence(middle)
            or evidence != _managed_plugin_evidence(after)
            or evidence != _managed_plugin_evidence(entry_after)
        ):
            raise ManagedPluginSnapshotError(f"{label} changed during read")
        return first
    except OSError as exc:
        raise ManagedPluginSnapshotError(f"{label} could not be verified") from exc
    finally:
        os.close(asset_fd)


def _managed_plugin_bounded_entries(
    directory_fd: int,
    label: str,
    *,
    budget: _ManagedPluginAssetBudget | None = None,
) -> tuple[str, ...]:
    entries: list[str] = []
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if (
                    budget is not None
                    and budget.directory_entry_count
                    >= _MANAGED_PLUGIN_MAX_TOTAL_DIRECTORY_ENTRIES
                ):
                    raise ManagedPluginSnapshotError(
                        "managed plugin aggregate directory-entry evidence "
                        "limit exceeded"
                    )
                if budget is not None:
                    budget.directory_entry_count += 1
                entries.append(entry.name)
                if len(entries) > _MANAGED_PLUGIN_MAX_DIRECTORY_ENTRIES:
                    raise ManagedPluginSnapshotError(
                        f"{label} entry limit exceeded"
                    )
    except ManagedPluginSnapshotError:
        raise
    except OSError as exc:
        raise ManagedPluginSnapshotError(f"{label} enumeration failed") from exc
    return tuple(sorted(entries))


def _managed_plugin_snapshot_entry_evidence(
    directory_fd: int,
    label: str,
    budget: _ManagedPluginAssetBudget,
) -> tuple[tuple[str, _ManagedPluginEvidence], ...]:
    evidence: list[tuple[str, _ManagedPluginEvidence]] = []
    for name in _managed_plugin_bounded_entries(
        directory_fd,
        label,
        budget=budget,
    ):
        try:
            value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise ManagedPluginSnapshotError(
                f"{label} entry {name!r} could not be verified"
            ) from exc
        entry_label = f"{label} entry {name!r}"
        if stat.S_ISDIR(value.st_mode):
            _managed_plugin_validate_dir(value, entry_label)
        elif stat.S_ISREG(value.st_mode):
            _managed_plugin_validate_file(value, entry_label)
        else:
            raise ManagedPluginSnapshotError(
                f"{entry_label} has an unsafe type"
            )
        evidence.append((name, _managed_plugin_evidence(value)))
    return tuple(evidence)


def _managed_plugin_count_directory(
    budget: _ManagedPluginAssetBudget,
    label: str,
) -> None:
    budget.directory_count += 1
    if budget.directory_count > _MANAGED_PLUGIN_MAX_DIRECTORIES:
        raise ManagedPluginSnapshotError(
            f"{label} directory-count limit exceeded"
        )


def _managed_plugin_add_manifest_to_budget(
    budget: _ManagedPluginAssetBudget,
    raw: bytes,
) -> None:
    budget.manifest_count += 1
    budget.total_manifest_bytes += len(raw)
    if budget.manifest_count > _MANAGED_PLUGIN_MAX_MANIFEST_FILES:
        raise ManagedPluginSnapshotError(
            "managed plugin manifest count limit exceeded"
        )
    if budget.total_manifest_bytes > _MANAGED_PLUGIN_MAX_TOTAL_MANIFEST_BYTES:
        raise ManagedPluginSnapshotError(
            "managed plugin manifest total-byte limit exceeded"
        )


def _managed_plugin_add_asset_to_budget(
    budget: _ManagedPluginAssetBudget,
    raw: bytes,
) -> None:
    budget.count += 1
    budget.total_bytes += len(raw)
    if budget.count > _MANAGED_PLUGIN_MAX_ASSET_FILES:
        raise ManagedPluginSnapshotError(
            "managed plugin asset file-count limit exceeded"
        )
    if budget.total_bytes > _MANAGED_PLUGIN_MAX_TOTAL_ASSET_BYTES:
        raise ManagedPluginSnapshotError(
            "managed plugin total asset-byte limit exceeded"
        )


def _managed_plugin_collect_asset_tree(
    parent_fd: int,
    directory: str,
    *,
    prefix: str,
    optional: bool,
    budget: _ManagedPluginAssetBudget,
    depth: int = 1,
) -> dict[str, bytes]:
    if depth > _MANAGED_PLUGIN_MAX_DEPTH:
        raise ManagedPluginSnapshotError(
            "managed plugin asset directory depth limit exceeded"
        )
    try:
        os.stat(directory, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return {}
        raise ManagedPluginSnapshotError(
            f"plugin asset directory {directory!r} is missing"
        ) from None
    except OSError as exc:
        if optional and exc.errno == getattr(os, "ENOENT", 2):
            return {}
        raise ManagedPluginSnapshotError(
            f"plugin asset directory {directory!r} is unsafe"
        ) from exc
    directory_fd, directory_stat = _managed_plugin_open_dir(
        parent_fd,
        directory,
        f"plugin asset directory {directory}",
    )
    assets: dict[str, bytes] = {}
    try:
        _managed_plugin_count_directory(
            budget,
            f"plugin asset directory {directory}",
        )
        entries = _managed_plugin_bounded_entries(
            directory_fd,
            f"plugin asset directory {directory}",
            budget=budget,
        )
        for name in entries:
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or name.startswith(".")
            ):
                raise ManagedPluginSnapshotError(
                    f"invalid plugin asset name {name!r}"
                )
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            rel = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(entry.st_mode):
                assets.update(
                    _managed_plugin_collect_asset_tree(
                        directory_fd,
                        name,
                        prefix=rel,
                        optional=False,
                        budget=budget,
                        depth=depth + 1,
                    )
                )
            elif stat.S_ISREG(entry.st_mode):
                raw = _managed_plugin_read_asset(
                    directory_fd,
                    name,
                    f"plugin asset {rel}",
                )
                _managed_plugin_add_asset_to_budget(budget, raw)
                assets[rel] = raw
            else:
                raise ManagedPluginSnapshotError(
                    f"plugin asset {rel} has an unsafe type"
                )
        final_entries = _managed_plugin_bounded_entries(
            directory_fd,
            f"plugin asset directory {directory}",
        )
        if final_entries != entries:
            raise ManagedPluginSnapshotError(
                f"plugin asset directory {directory!r} changed during snapshot"
            )
        final_stat = os.fstat(directory_fd)
        if _managed_plugin_evidence(final_stat) != _managed_plugin_evidence(directory_stat):
            raise ManagedPluginSnapshotError(
                f"plugin asset directory {directory!r} changed during snapshot"
            )
        return assets
    except OSError as exc:
        raise ManagedPluginSnapshotError(
            f"plugin asset directory {directory!r} could not be snapshotted"
        ) from exc
    finally:
        os.close(directory_fd)


def _managed_plugin_confirm_root(root: str, root_stat: os.stat_result) -> None:
    try:
        current = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise ManagedPluginSnapshotError(
            "managed plugin root could not be rechecked"
        ) from exc
    _managed_plugin_validate_dir(current, "managed plugin root")
    if _managed_plugin_evidence(current) != _managed_plugin_evidence(root_stat):
        raise ManagedPluginSnapshotError("managed plugin root changed during discovery")


def _managed_plugin_entry_inventory(
    entries: tuple[tuple[str, _ManagedPluginEvidence], ...],
) -> list[dict]:
    return [
        {"name": name, "evidence": evidence.__dict__}
        for name, evidence in entries
    ]


def _build_managed_plugin_snapshot() -> _ManagedPluginSnapshot:
    root = _managed_plugin_root_path()
    root_fd, root_stat = _managed_plugin_open_root(root)
    canonical_root = os.path.realpath(root)
    try:
        canonical_stat = os.stat(canonical_root, follow_symlinks=False)
    except OSError as exc:
        os.close(root_fd)
        raise ManagedPluginSnapshotError(
            "managed plugin canonical root could not be verified"
        ) from exc
    if not _managed_plugin_same_inode(root_stat, canonical_stat):
        os.close(root_fd)
        raise ManagedPluginSnapshotError(
            "managed plugin canonical root identity mismatch"
        )
    manifests: dict[str, dict] = {}
    static_roots: dict[str, Path] = {}
    assets: dict[str, dict[str, bytes]] = {}
    shared_assets: dict[str, bytes] = {}
    asset_budget = _ManagedPluginAssetBudget()
    tab_paths: set[str] = set()
    inventory: list[dict] = []
    ignored_names: list[str] = []
    ignored_records: list[_ManagedIgnoredPlugin] = []
    verification_records: list[
        tuple[
            str,
            _ManagedPluginEvidence,
            tuple[tuple[str, _ManagedPluginEvidence], ...],
            _ManagedPluginEvidence,
            tuple[tuple[str, _ManagedPluginEvidence], ...],
            _ManagedPluginEvidence,
            bytes,
            dict[str, bytes],
        ]
    ] = []
    try:
        _managed_plugin_count_directory(asset_budget, "managed plugin root")
        entries = _managed_plugin_bounded_entries(
            root_fd,
            "managed plugin root",
            budget=asset_budget,
        )
        for directory in entries:
            if directory == "plugin.css":
                raw_shared = _managed_plugin_read_asset(
                    root_fd,
                    directory,
                    "managed shared plugin asset plugin.css",
                )
                _managed_plugin_add_asset_to_budget(asset_budget, raw_shared)
                shared_assets[directory] = raw_shared
                continue
            if not isinstance(directory, str) or not _VALID_PLUGIN_NAME.fullmatch(directory):
                raise ManagedPluginSnapshotError(
                    f"invalid plugin directory name {directory!r}"
                )
            plugin_fd, plugin_stat = _managed_plugin_open_dir(
                root_fd,
                directory,
                f"plugin {directory}",
            )
            try:
                _managed_plugin_count_directory(
                    asset_budget,
                    f"plugin {directory}",
                )
                plugin_entries = _managed_plugin_snapshot_entry_evidence(
                    plugin_fd,
                    f"plugin {directory}",
                    asset_budget,
                )
                plugin_entry_names = {name for name, _evidence in plugin_entries}
                if "dashboard" not in plugin_entry_names:
                    ignored_names.append(directory)
                    ignored_records.append(
                        _ManagedIgnoredPlugin(
                            directory,
                            _managed_plugin_evidence(plugin_stat),
                            plugin_entries,
                            None,
                            (),
                        )
                    )
                    inventory.append(
                        {
                            "directory": directory,
                            "ignored": "no-dashboard",
                            "plugin": _managed_plugin_evidence(plugin_stat).__dict__,
                            "plugin_entries": _managed_plugin_entry_inventory(
                                plugin_entries
                            ),
                        }
                    )
                    continue
                dashboard_fd, dashboard_stat = _managed_plugin_open_dir(
                    plugin_fd,
                    "dashboard",
                    f"plugin {directory} dashboard",
                )
                try:
                    _managed_plugin_count_directory(
                        asset_budget,
                        f"plugin {directory} dashboard",
                    )
                    dashboard_entries = _managed_plugin_snapshot_entry_evidence(
                        dashboard_fd,
                        f"plugin {directory} dashboard",
                        asset_budget,
                    )
                    dashboard_entry_names = {
                        name for name, _evidence in dashboard_entries
                    }
                    if "manifest.json" not in dashboard_entry_names:
                        ignored_names.append(directory)
                        ignored_records.append(
                            _ManagedIgnoredPlugin(
                                directory,
                                _managed_plugin_evidence(plugin_stat),
                                plugin_entries,
                                _managed_plugin_evidence(dashboard_stat),
                                dashboard_entries,
                            )
                        )
                        inventory.append(
                            {
                                "directory": directory,
                                "ignored": "no-manifest",
                                "plugin": _managed_plugin_evidence(
                                    plugin_stat
                                ).__dict__,
                                "plugin_entries": _managed_plugin_entry_inventory(
                                    plugin_entries
                                ),
                                "dashboard": _managed_plugin_evidence(
                                    dashboard_stat
                                ).__dict__,
                                "dashboard_entries": (
                                    _managed_plugin_entry_inventory(
                                        dashboard_entries
                                    )
                                ),
                            }
                        )
                        continue
                    raw, manifest_evidence = _managed_plugin_read_manifest(dashboard_fd)
                    _managed_plugin_add_manifest_to_budget(asset_budget, raw)
                    manifest = _managed_plugin_json(raw)
                    name = manifest.get("name") or directory
                    if not isinstance(name, str) or not _VALID_PLUGIN_NAME.fullmatch(name):
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} has invalid manifest name"
                        )
                    tab = manifest.get("tab", {})
                    if not isinstance(tab, dict):
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} tab must be an object"
                        )
                    tab_path = tab.get("path", f"/{name}")
                    if (
                        not isinstance(tab_path, str)
                        or not _VALID_PLUGIN_TAB_PATH.fullmatch(tab_path)
                    ):
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} has invalid tab path"
                        )
                    if name in manifests:
                        raise ManagedPluginSnapshotError(
                            f"plugin name conflict for {name!r}"
                        )
                    if tab_path in tab_paths:
                        raise ManagedPluginSnapshotError(
                            f"plugin tab path conflict for {tab_path!r}"
                        )
                    manifests[name] = manifest
                    tab_paths.add(tab_path)
                    static_roots[name] = (
                        Path(canonical_root) / directory / "dashboard"
                    )
                    plugin_assets: dict[str, bytes] = {}
                    plugin_assets.update(
                        _managed_plugin_collect_asset_tree(
                            dashboard_fd,
                            "dist",
                            prefix="dist",
                            optional=True,
                            budget=asset_budget,
                        )
                    )
                    plugin_assets.update(
                        _managed_plugin_collect_asset_tree(
                            dashboard_fd,
                            "static",
                            prefix="static",
                            optional=True,
                            budget=asset_budget,
                        )
                    )
                    sibling_static = _managed_plugin_collect_asset_tree(
                        plugin_fd,
                        "static",
                        prefix="@plugin-static",
                        optional=True,
                        budget=asset_budget,
                    )
                    plugin_assets.update(sibling_static)
                    assets[name] = plugin_assets
                    inventory.append(
                        {
                            "directory": directory,
                            "name": name,
                            "tab_path": tab_path,
                            "manifest": manifest,
                            "plugin": _managed_plugin_evidence(plugin_stat).__dict__,
                            "plugin_entries": _managed_plugin_entry_inventory(
                                plugin_entries
                            ),
                            "dashboard": _managed_plugin_evidence(dashboard_stat).__dict__,
                            "dashboard_entries": _managed_plugin_entry_inventory(
                                dashboard_entries
                            ),
                            "manifest_file": manifest_evidence.__dict__,
                            "assets": {
                                rel: hashlib.sha256(data).hexdigest()
                                for rel, data in sorted(plugin_assets.items())
                            },
                        }
                    )
                    verification_records.append(
                        (
                            directory,
                            _managed_plugin_evidence(plugin_stat),
                            plugin_entries,
                            _managed_plugin_evidence(dashboard_stat),
                            dashboard_entries,
                            manifest_evidence,
                            raw,
                            plugin_assets,
                        )
                    )
                finally:
                    os.close(dashboard_fd)
            finally:
                os.close(plugin_fd)
        confirmation_budget = _ManagedPluginAssetBudget()
        _managed_plugin_count_directory(
            confirmation_budget,
            "managed plugin root",
        )
        final_entries = _managed_plugin_bounded_entries(
            root_fd,
            "managed plugin root",
            budget=confirmation_budget,
        )
        if final_entries != entries:
            raise ManagedPluginSnapshotError(
                "managed plugin inventory changed during discovery"
            )
        if "plugin.css" in shared_assets:
            confirmed_shared = _managed_plugin_read_asset(
                root_fd,
                "plugin.css",
                "managed shared plugin asset plugin.css",
            )
            _managed_plugin_add_asset_to_budget(confirmation_budget, confirmed_shared)
            if confirmed_shared != shared_assets["plugin.css"]:
                raise ManagedPluginSnapshotError(
                    "managed shared plugin asset plugin.css changed during discovery"
                )
        for ignored in ignored_records:
            plugin_fd, plugin_stat = _managed_plugin_open_dir(
                root_fd,
                ignored.directory,
                f"plugin {ignored.directory}",
            )
            try:
                _managed_plugin_count_directory(
                    confirmation_budget,
                    f"plugin {ignored.directory}",
                )
                confirmed_plugin_entries = (
                    _managed_plugin_snapshot_entry_evidence(
                        plugin_fd,
                        f"plugin {ignored.directory}",
                        confirmation_budget,
                    )
                )
                if (
                    _managed_plugin_evidence(plugin_stat)
                    != ignored.plugin_evidence
                    or confirmed_plugin_entries != ignored.plugin_entries
                ):
                    raise ManagedPluginSnapshotError(
                        f"ignored plugin {ignored.directory} changed during discovery"
                    )
                if ignored.dashboard_evidence is None:
                    continue
                dashboard_fd, dashboard_stat = _managed_plugin_open_dir(
                    plugin_fd,
                    "dashboard",
                    f"plugin {ignored.directory} dashboard",
                )
                try:
                    _managed_plugin_count_directory(
                        confirmation_budget,
                        f"plugin {ignored.directory} dashboard",
                    )
                    confirmed_dashboard_entries = (
                        _managed_plugin_snapshot_entry_evidence(
                            dashboard_fd,
                            f"plugin {ignored.directory} dashboard",
                            confirmation_budget,
                        )
                    )
                    if (
                        _managed_plugin_evidence(dashboard_stat)
                        != ignored.dashboard_evidence
                        or confirmed_dashboard_entries
                        != ignored.dashboard_entries
                    ):
                        raise ManagedPluginSnapshotError(
                            f"ignored plugin {ignored.directory} dashboard "
                            "changed during discovery"
                        )
                finally:
                    os.close(dashboard_fd)
            finally:
                os.close(plugin_fd)
        for (
            directory,
            expected_plugin,
            expected_plugin_entries,
            expected_dashboard,
            expected_dashboard_entries,
            expected_manifest,
            expected_raw,
            expected_assets,
        ) in verification_records:
            plugin_fd, plugin_stat = _managed_plugin_open_dir(
                root_fd,
                directory,
                f"plugin {directory}",
            )
            try:
                _managed_plugin_count_directory(
                    confirmation_budget,
                    f"plugin {directory}",
                )
                if _managed_plugin_evidence(plugin_stat) != expected_plugin:
                    raise ManagedPluginSnapshotError(
                        f"plugin {directory} changed during discovery"
                    )
                if (
                    _managed_plugin_snapshot_entry_evidence(
                        plugin_fd,
                        f"plugin {directory}",
                        confirmation_budget,
                    )
                    != expected_plugin_entries
                ):
                    raise ManagedPluginSnapshotError(
                        f"plugin {directory} entries changed during discovery"
                    )
                dashboard_fd, dashboard_stat = _managed_plugin_open_dir(
                    plugin_fd,
                    "dashboard",
                    f"plugin {directory} dashboard",
                )
                try:
                    _managed_plugin_count_directory(
                        confirmation_budget,
                        f"plugin {directory} dashboard",
                    )
                    if _managed_plugin_evidence(dashboard_stat) != expected_dashboard:
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} dashboard changed during discovery"
                        )
                    if (
                        _managed_plugin_snapshot_entry_evidence(
                            dashboard_fd,
                            f"plugin {directory} dashboard",
                            confirmation_budget,
                        )
                        != expected_dashboard_entries
                    ):
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} dashboard entries changed "
                            "during discovery"
                        )
                    confirmed_raw, confirmed_manifest = _managed_plugin_read_manifest(
                        dashboard_fd
                    )
                    _managed_plugin_add_manifest_to_budget(
                        confirmation_budget,
                        confirmed_raw,
                    )
                    if (
                        confirmed_raw != expected_raw
                        or confirmed_manifest != expected_manifest
                    ):
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} manifest changed during discovery"
                        )
                    confirmed_assets: dict[str, bytes] = {}
                    confirmed_assets.update(
                        _managed_plugin_collect_asset_tree(
                            dashboard_fd,
                            "dist",
                            prefix="dist",
                            optional=True,
                            budget=confirmation_budget,
                        )
                    )
                    confirmed_assets.update(
                        _managed_plugin_collect_asset_tree(
                            dashboard_fd,
                            "static",
                            prefix="static",
                            optional=True,
                            budget=confirmation_budget,
                        )
                    )
                    confirmed_assets.update(
                        _managed_plugin_collect_asset_tree(
                            plugin_fd,
                            "static",
                            prefix="@plugin-static",
                            optional=True,
                            budget=confirmation_budget,
                        )
                    )
                    if confirmed_assets != expected_assets:
                        raise ManagedPluginSnapshotError(
                            f"plugin {directory} assets changed during discovery"
                        )
                finally:
                    os.close(dashboard_fd)
            finally:
                os.close(plugin_fd)
        if (
            confirmation_budget.directory_entry_count
            != asset_budget.directory_entry_count
        ):
            raise ManagedPluginSnapshotError(
                "managed plugin directory-entry evidence changed during discovery"
            )
        _managed_plugin_confirm_root(root, root_stat)
    finally:
        os.close(root_fd)

    canonical = json.dumps(
        {
            "plugins": inventory,
            "shared_assets": {
                rel: hashlib.sha256(data).hexdigest()
                for rel, data in sorted(shared_assets.items())
            },
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt = ManagedPluginSnapshotReceipt(
        plugin_root=canonical_root,
        inventory_sha256=hashlib.sha256(canonical).hexdigest(),
        names=tuple(sorted(manifests)),
        ignored_names=tuple(sorted(ignored_names)),
        asset_count=asset_budget.count,
        total_asset_bytes=asset_budget.total_bytes,
        max_asset_files=_MANAGED_PLUGIN_MAX_ASSET_FILES,
        max_asset_bytes=_MANAGED_PLUGIN_MAX_ASSET_BYTES,
        max_total_asset_bytes=_MANAGED_PLUGIN_MAX_TOTAL_ASSET_BYTES,
        manifest_count=asset_budget.manifest_count,
        total_manifest_bytes=asset_budget.total_manifest_bytes,
        max_manifest_files=_MANAGED_PLUGIN_MAX_MANIFEST_FILES,
        max_manifest_bytes=_MANAGED_PLUGIN_MAX_MANIFEST_BYTES,
        max_total_manifest_bytes=_MANAGED_PLUGIN_MAX_TOTAL_MANIFEST_BYTES,
        directory_count=asset_budget.directory_count,
        directory_entry_count=asset_budget.directory_entry_count,
        max_directories=_MANAGED_PLUGIN_MAX_DIRECTORIES,
        max_directory_entries=_MANAGED_PLUGIN_MAX_DIRECTORY_ENTRIES,
        max_total_directory_entries=(
            _MANAGED_PLUGIN_MAX_TOTAL_DIRECTORY_ENTRIES
        ),
        max_depth=_MANAGED_PLUGIN_MAX_DEPTH,
    )
    return _ManagedPluginSnapshot(
        receipt,
        manifests,
        static_roots,
        assets,
        shared_assets,
    )


def strict_install_managed_plugins() -> ManagedPluginSnapshotReceipt:
    """Build a strict snapshot, then replace both process maps as one commit."""

    global PLUGIN_MANIFESTS, _PLUGIN_STATIC_ROOTS, _PLUGIN_RUNTIME_STATE
    snapshot = _build_managed_plugin_snapshot()
    runtime = _plugin_runtime_snapshot(
        snapshot.manifests,
        snapshot.static_roots,
        snapshot.assets,
        snapshot.shared_assets,
        managed=True,
    )
    with _MANAGED_PLUGIN_LOCK:
        _PLUGIN_RUNTIME_STATE = runtime
        PLUGIN_MANIFESTS, _PLUGIN_STATIC_ROOTS = (
            snapshot.manifests,
            snapshot.static_roots,
        )
    return snapshot.receipt


def verify_strict_managed_plugins() -> ManagedPluginVerification:
    """Rebuild expected state without mutating either installed process map."""

    try:
        snapshot = _build_managed_plugin_snapshot()
    except ManagedPluginSnapshotError:
        return ManagedPluginVerification(
            ManagedPluginVerificationOutcome.AMBIGUOUS,
            None,
            "unsafe_or_unstable_plugin_inventory",
        )
    with _MANAGED_PLUGIN_LOCK:
        runtime = _PLUGIN_RUNTIME_STATE
    expected_runtime = _plugin_runtime_snapshot(
        snapshot.manifests,
        snapshot.static_roots,
        snapshot.assets,
        snapshot.shared_assets,
        managed=True,
    )
    if (
        runtime.managed
        and runtime.manifests == expected_runtime.manifests
        and runtime.static_roots == expected_runtime.static_roots
        and runtime.assets == expected_runtime.assets
        and runtime.shared_assets == expected_runtime.shared_assets
    ):
        return ManagedPluginVerification(
            ManagedPluginVerificationOutcome.PROVED_COMPLETE,
            snapshot.receipt,
            None,
        )
    if (
        not runtime.managed
        and not runtime.manifests
        and not runtime.static_roots
        and not runtime.assets
        and not runtime.shared_assets
    ):
        return ManagedPluginVerification(
            ManagedPluginVerificationOutcome.PROVED_ABSENT,
            snapshot.receipt,
            "managed_runtime_not_installed",
        )
    return ManagedPluginVerification(
        ManagedPluginVerificationOutcome.PARTIAL,
        snapshot.receipt,
        "installed_maps_do_not_match_inventory",
    )


def reconcile_strict_managed_plugins() -> ManagedPluginVerification:
    """Install the strict snapshot and prove that the published runtime matches."""

    strict_install_managed_plugins()
    verification = verify_strict_managed_plugins()
    if verification.outcome is not ManagedPluginVerificationOutcome.PROVED_COMPLETE:
        raise ManagedPluginSnapshotError(
            "managed plugin runtime reconciliation could not be proved complete"
        )
    return verification


def _get_plugin_base() -> Path:
    return Path(os.environ.get("HERMES_WEBUI_PLUGINS_DIR", str(Path.home() / ".hermes" / "plugins")))


def load_plugins() -> None:
    """Scan plugin directories and load manifest.json for each dashboard plugin."""
    plugin_base = _get_plugin_base()
    if not plugin_base.is_dir():
        logger.debug("No plugins directory at %s", plugin_base)
        return

    for entry in sorted(plugin_base.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "dashboard" / "manifest.json"
        if not manifest_path.is_file():
            continue

        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            logger.exception("Failed to parse manifest for plugin %s", entry.name)
            continue

        name = manifest.get("name") or entry.name

        # Validate the plugin name: it becomes a URL path component
        # (/dashboard-plugins/<name>/...) and a settings key. Restrict to a safe
        # slug so a manifest like name:"../foo" can't make the URL-space ambiguous.
        if not _VALID_PLUGIN_NAME.match(str(name)):
            logger.warning("Skipping plugin with invalid name %r (must match %s)", name, _VALID_PLUGIN_NAME.pattern)
            continue

        tab = manifest.get("tab", {})
        tab_path = tab.get("path", f"/{name}")

        # Validate tab.path: it's a same-origin route the plugin page is served
        # at AND a value passed into client-side navigation. Require a clean
        # absolute path — no quotes/control chars/query/fragment — so a hostile
        # manifest can't shadow odd routes or inject via the path.
        if not _VALID_PLUGIN_TAB_PATH.match(str(tab_path)):
            logger.warning("Skipping plugin %s with invalid tab.path %r (must match %s)", name, tab_path, _VALID_PLUGIN_TAB_PATH.pattern)
            continue

        if name in PLUGIN_MANIFESTS:
            logger.warning("Duplicate plugin name skipped: %s (already loaded)", name)
            continue
        if tab_path in (m.get("tab", {}).get("path") for m in PLUGIN_MANIFESTS.values()):
            logger.warning("Plugin %s tab.path %r conflicts with another plugin; skipped", name, tab_path)
            continue

        PLUGIN_MANIFESTS[name] = manifest
        logger.info("Loaded dashboard plugin: %s (label=%s)", name, manifest.get("label", ""))

        # Pre-compute static root for fast serving (points to dashboard/)
        dashboard_dir = entry / "dashboard"
        if dashboard_dir.is_dir():
            _PLUGIN_STATIC_ROOTS[name] = dashboard_dir.resolve()
    global _PLUGIN_RUNTIME_STATE
    with _MANAGED_PLUGIN_LOCK:
        _PLUGIN_RUNTIME_STATE = _plugin_runtime_snapshot(
            PLUGIN_MANIFESTS,
            _PLUGIN_STATIC_ROOTS,
            {},
            managed=False,
        )


def serve_plugin_static(plugin_name: str, rel_path: str) -> tuple[bytes, str] | None:
    """
    Serve a built static asset from a plugin's dashboard/dist/ (or static/) dir.

    Returns (file_bytes, content_type) on success, None on not found.

    Security: _PLUGIN_STATIC_ROOTS points at the plugin's whole dashboard/ dir
    (the page route needs that), but the asset route must NOT expose plugin
    source/config — e.g. dashboard/plugin_api.py, manifest.json, .env. So we
    constrain served files to the built-asset subtrees (dist/ or static/), reject
    dotfiles, and require a known static extension.
    """
    runtime = get_plugin_runtime_snapshot()
    root = runtime.static_roots.get(plugin_name)
    if not root:
        return None

    normalized = rel_path.lstrip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
        or parts[0] not in ("dist", "static")
    ):
        return None

    ext = os.path.splitext(normalized.lower())[1]
    _STATIC_EXTS = {
        ".js", ".css", ".html", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".map", ".txt",
    }
    if ext not in _STATIC_EXTS:
        return None

    if runtime.managed:
        data = runtime.assets.get(plugin_name, {}).get(normalized)
        if data is None:
            return None
        content_type = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        return data, content_type

    safe = (root / normalized).resolve()
    try:
        safe.relative_to(root)
    except ValueError:
        return None  # path traversal attempt

    # Only built-asset subtrees are servable (not the dashboard root itself,
    # which holds plugin_api.py / manifest.json / config).
    rel = safe.relative_to(root)
    if not rel.parts or rel.parts[0] not in ("dist", "static"):
        return None
    # No dotfiles (.env, .git, etc.) anywhere in the path.
    if any(part.startswith(".") for part in rel.parts):
        return None

    if not safe.is_file():
        return None

    # Allowlist of static asset extensions — refuse source/config (.py, .json,
    # .toml, .env, .sh, ...) even if somehow placed under dist/.
    data = safe.read_bytes()
    content_type = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")

    return data, content_type


def serve_plugin_shared_static(rel_path: str) -> tuple[bytes, str] | None:
    """Serve an allowlisted shared plugin asset from the active snapshot."""

    if rel_path != "plugin.css":
        return None
    runtime = get_plugin_runtime_snapshot()
    if runtime.managed:
        data = runtime.shared_assets.get(rel_path)
        if data is None:
            return None
        return data, "text/css; charset=utf-8"

    plugin_base = _get_plugin_base()
    safe = (plugin_base / rel_path).resolve()
    try:
        safe.relative_to(plugin_base.resolve())
    except ValueError:
        return None
    if not safe.is_file():
        return None
    return safe.read_bytes(), "text/css; charset=utf-8"


def get_plugin_page_material(
    plugin_name: str,
    runtime: PluginRuntimeSnapshot | None = None,
) -> PluginPageMaterial | None:
    """Return page bytes/capability from one runtime snapshot."""

    if runtime is None:
        runtime = get_plugin_runtime_snapshot()
    if plugin_name not in runtime.manifests:
        return None
    if runtime.managed:
        plugin_assets = runtime.assets.get(plugin_name, {})
        html = plugin_assets.get("dist/index.html")
        if html is None:
            html = plugin_assets.get("@plugin-static/index.html")
        return PluginPageMaterial(
            html=html,
            has_index_js="dist/index.js" in plugin_assets,
        )
    dashboard_dir = runtime.static_roots.get(plugin_name)
    if dashboard_dir is None:
        return None
    index_html = dashboard_dir / "dist" / "index.html"
    if index_html.is_file():
        return PluginPageMaterial(index_html.read_bytes(), True)
    static_html = dashboard_dir.parent / "static" / "index.html"
    if static_html.is_file():
        return PluginPageMaterial(static_html.read_bytes(), True)
    return PluginPageMaterial(
        html=None,
        has_index_js=(dashboard_dir / "dist" / "index.js").is_file(),
    )


def get_plugin_metadata() -> list[dict]:
    """
    Return a list of plugin metadata suitable for the Settings → Plugins tab.
    Each entry includes name, key, version, description, and tab info for linking.

    Per-plugin enabled state is stored in settings.json under `dashboard_plugins`.
    A plugin is enabled only if the user has explicitly toggled it on (default off).
    """
    from api.config import load_settings

    plugin_settings = load_settings().get("dashboard_plugins", {})
    plugins = []
    runtime = get_plugin_runtime_snapshot()
    for name, manifest in sorted(runtime.manifests.items()):
        tab = manifest.get("tab", {})
        path = tab.get("path", f"/{name}")
        plugins.append({
            "name": manifest.get("label") or manifest.get("name") or name,
            "key": name,
            "version": manifest.get("version", "0.0.0"),
            "description": manifest.get("description", ""),
            "tab": {
                "path": path,
                "label": tab.get("label") or manifest.get("label") or name,
            },
            "enabled": bool(plugin_settings.get(name, False)),
            "hooks": [],
        })
    return plugins
