from __future__ import annotations

import json
import threading
from io import BytesIO
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


def _plugin(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    tab_path: str | None = None,
    extra: dict | None = None,
) -> Path:
    dashboard = root / directory / "dashboard"
    dashboard.mkdir(parents=True, mode=0o700)
    manifest = {
        "name": name or directory,
        "label": directory.title(),
        "version": "1.0.0",
        "tab": {"path": tab_path or f"/{name or directory}"},
    }
    manifest.update(extra or {})
    path = dashboard / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    path.chmod(0o600)
    return dashboard


def _asset(dashboard: Path, rel_path: str, content: bytes) -> Path:
    path = dashboard / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _configure(plugins, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("HERMES_WEBUI_PLUGINS_DIR", str(root))
    monkeypatch.setattr(plugins, "PLUGIN_MANIFESTS", {})
    monkeypatch.setattr(plugins, "_PLUGIN_STATIC_ROOTS", {})
    monkeypatch.setattr(
        plugins,
        "_PLUGIN_RUNTIME_STATE",
        plugins._plugin_runtime_snapshot({}, {}, {}, managed=False),
    )


def test_managed_plugin_install_builds_complete_snapshot_then_swaps_both_maps(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    alpha = _plugin(root, "alpha")
    beta = _plugin(root, "beta")
    old_manifests = {"old": {"name": "old"}}
    old_roots = {"old": tmp_path / "old"}
    plugins.PLUGIN_MANIFESTS = old_manifests
    plugins._PLUGIN_STATIC_ROOTS = old_roots

    receipt = plugins.strict_install_managed_plugins()

    assert receipt.plugin_root == str(root)
    assert receipt.names == ("alpha", "beta")
    assert len(receipt.inventory_sha256) == 64
    assert receipt.asset_count == 0
    assert receipt.total_asset_bytes == 0
    assert receipt.max_asset_files == plugins._MANAGED_PLUGIN_MAX_ASSET_FILES
    assert receipt.max_asset_bytes == plugins._MANAGED_PLUGIN_MAX_ASSET_BYTES
    assert receipt.max_total_asset_bytes == plugins._MANAGED_PLUGIN_MAX_TOTAL_ASSET_BYTES
    assert receipt.manifest_count == 2
    assert receipt.total_manifest_bytes > 0
    assert receipt.max_manifest_files == plugins._MANAGED_PLUGIN_MAX_MANIFEST_FILES
    assert receipt.max_manifest_bytes == plugins._MANAGED_PLUGIN_MAX_MANIFEST_BYTES
    assert (
        receipt.max_total_manifest_bytes
        == plugins._MANAGED_PLUGIN_MAX_TOTAL_MANIFEST_BYTES
    )
    assert receipt.directory_count == 5
    assert receipt.max_directories == plugins._MANAGED_PLUGIN_MAX_DIRECTORIES
    assert receipt.directory_entry_count > 0
    assert (
        receipt.max_total_directory_entries
        == plugins._MANAGED_PLUGIN_MAX_TOTAL_DIRECTORY_ENTRIES
    )
    assert (
        receipt.max_directory_entries
        == plugins._MANAGED_PLUGIN_MAX_DIRECTORY_ENTRIES
    )
    assert receipt.max_depth == plugins._MANAGED_PLUGIN_MAX_DEPTH
    assert set(plugins.PLUGIN_MANIFESTS) == {"alpha", "beta"}
    assert plugins._PLUGIN_STATIC_ROOTS == {"alpha": alpha, "beta": beta}
    assert plugins.PLUGIN_MANIFESTS is not old_manifests
    assert plugins._PLUGIN_STATIC_ROOTS is not old_roots
    with pytest.raises(FrozenInstanceError):
        receipt.inventory_sha256 = "0" * 64


def test_managed_plugin_install_is_idempotent_with_stable_digest(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(
        root,
        "alpha",
        extra={"description": "stable", "metadata": {"z": 1, "a": 2}},
    )

    first = plugins.strict_install_managed_plugins()
    first_manifests = json.loads(json.dumps(plugins.PLUGIN_MANIFESTS))
    first_roots = dict(plugins._PLUGIN_STATIC_ROOTS)
    second = plugins.strict_install_managed_plugins()

    assert second == first
    assert plugins.PLUGIN_MANIFESTS == first_manifests
    assert plugins._PLUGIN_STATIC_ROOTS == first_roots
    assert "stable" not in repr(first)


@pytest.mark.parametrize("failure", ["invalid-json", "invalid-manifest", "symlink"])
def test_managed_plugin_install_rejects_unsafe_plugin_without_partial_globals(
    tmp_path, monkeypatch, failure
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "good")
    if failure == "invalid-json":
        dashboard = root / "bad" / "dashboard"
        dashboard.mkdir(parents=True)
        (dashboard / "manifest.json").write_text("{broken", encoding="utf-8")
    elif failure == "invalid-manifest":
        _plugin(root, "bad", name="../escape")
    else:
        outside = tmp_path / "outside"
        _plugin(outside, "linked")
        (root / "bad").symlink_to(outside / "linked")

    before_manifests = {"sentinel": {"name": "sentinel"}}
    before_roots = {"sentinel": tmp_path / "sentinel"}
    plugins.PLUGIN_MANIFESTS = before_manifests
    plugins._PLUGIN_STATIC_ROOTS = before_roots

    with pytest.raises(plugins.ManagedPluginSnapshotError):
        plugins.strict_install_managed_plugins()

    assert plugins.PLUGIN_MANIFESTS is before_manifests
    assert plugins._PLUGIN_STATIC_ROOTS is before_roots


@pytest.mark.parametrize("conflict", ["name", "tab"])
def test_managed_plugin_install_rejects_conflicts(tmp_path, monkeypatch, conflict):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "one", name="shared" if conflict == "name" else "one", tab_path="/shared")
    _plugin(root, "two", name="shared" if conflict == "name" else "two", tab_path="/shared")

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="conflict"):
        plugins.strict_install_managed_plugins()
    assert plugins.PLUGIN_MANIFESTS == {}
    assert plugins._PLUGIN_STATIC_ROOTS == {}


def test_managed_plugin_snapshot_detects_enumeration_race(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "alpha")
    real_entries = plugins._managed_plugin_bounded_entries
    calls = 0

    def inject_plugin_after_first_enumeration(
        directory_fd,
        label,
        *,
        budget=None,
    ):
        nonlocal calls
        result = real_entries(directory_fd, label, budget=budget)
        calls += 1
        if calls == 1:
            _plugin(root, "late")
        return result

    monkeypatch.setattr(
        plugins,
        "_managed_plugin_bounded_entries",
        inject_plugin_after_first_enumeration,
    )

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="changed"):
        plugins.strict_install_managed_plugins()
    assert plugins.PLUGIN_MANIFESTS == {}
    assert plugins._PLUGIN_STATIC_ROOTS == {}


def test_managed_plugin_snapshot_detects_late_same_inode_manifest_rewrite(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    manifest_path = dashboard / "manifest.json"
    real_read_manifest = plugins._managed_plugin_read_manifest
    rewritten = False

    def rewrite_after_first_stable_read(dashboard_fd):
        nonlocal rewritten
        result = real_read_manifest(dashboard_fd)
        if not rewritten:
            rewritten = True
            replacement = {
                "name": "alpha",
                "label": "Changed",
                "version": "2.0.0",
                "tab": {"path": "/alpha"},
            }
            manifest_path.write_text(json.dumps(replacement), encoding="utf-8")
            manifest_path.chmod(0o600)
        return result

    monkeypatch.setattr(
        plugins,
        "_managed_plugin_read_manifest",
        rewrite_after_first_stable_read,
    )

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="changed"):
        plugins.strict_install_managed_plugins()
    assert plugins.PLUGIN_MANIFESTS == {}
    assert plugins._PLUGIN_STATIC_ROOTS == {}


def test_managed_plugin_verifier_reports_restart_absent_complete_and_partial(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "alpha")

    absent = plugins.verify_strict_managed_plugins()
    assert absent.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_ABSENT

    receipt = plugins.strict_install_managed_plugins()
    complete = plugins.verify_strict_managed_plugins()
    assert complete.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_COMPLETE
    assert complete.receipt == receipt

    current = plugins.get_plugin_runtime_snapshot()
    plugins._PLUGIN_RUNTIME_STATE = plugins._plugin_runtime_snapshot(
        {},
        dict(current.static_roots),
        {name: dict(value) for name, value in current.assets.items()},
        managed=True,
    )
    partial = plugins.verify_strict_managed_plugins()
    assert partial.outcome is plugins.ManagedPluginVerificationOutcome.PARTIAL


def test_managed_plugin_verifier_empty_policy_requires_managed_install(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    manifests = plugins.PLUGIN_MANIFESTS
    roots = plugins._PLUGIN_STATIC_ROOTS

    absent = plugins.verify_strict_managed_plugins()

    assert absent.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_ABSENT
    assert plugins.PLUGIN_MANIFESTS is manifests
    assert plugins._PLUGIN_STATIC_ROOTS is roots

    receipt = plugins.strict_install_managed_plugins()
    complete = plugins.verify_strict_managed_plugins()

    assert complete.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_COMPLETE
    assert complete.receipt == receipt


def test_managed_plugin_verifier_unsafe_input_is_ambiguous(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    target = tmp_path / "target"
    _plugin(target, "alpha")
    (root / "alpha").symlink_to(target / "alpha")

    result = plugins.verify_strict_managed_plugins()

    assert result.outcome is plugins.ManagedPluginVerificationOutcome.AMBIGUOUS
    assert result.receipt is None


def test_managed_plugin_serving_uses_validated_bytes_after_root_replacement(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    _asset(dashboard, "dist/app.js", b"validated")
    replacement = tmp_path / "replacement"
    replacement_dashboard = _plugin(replacement, "alpha")
    _asset(replacement_dashboard, "dist/app.js", b"unvalidated")
    displaced = tmp_path / "displaced"
    real_confirm = plugins._managed_plugin_confirm_root
    replaced = False

    def replace_after_confirmation(root_path, root_stat):
        nonlocal replaced
        real_confirm(root_path, root_stat)
        if not replaced:
            replaced = True
            root.rename(displaced)
            replacement.rename(root)

    monkeypatch.setattr(
        plugins,
        "_managed_plugin_confirm_root",
        replace_after_confirmation,
    )

    plugins.strict_install_managed_plugins()
    served = plugins.serve_plugin_static("alpha", "dist/app.js")

    assert served is not None
    assert served[0] == b"validated"


def test_managed_plugin_runtime_state_is_single_atomically_observed_snapshot(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    first = tmp_path / "first"
    second = tmp_path / "second"
    _configure(plugins, monkeypatch, first)
    first_dashboard = _plugin(first, "alpha")
    _asset(first_dashboard, "dist/app.js", b"alpha")
    second.mkdir(mode=0o700)
    second_dashboard = _plugin(second, "beta")
    _asset(second_dashboard, "dist/app.js", b"beta")
    failures: list[object] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                state = plugins.get_plugin_runtime_snapshot()
            except BaseException as exc:
                failures.append(exc)
                stop.set()
                return
            names = set(state.manifests)
            roots = set(state.static_roots)
            assets = set(state.assets)
            if names != roots or names != assets:
                failures.append((names, roots, assets))
                stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(12):
            monkeypatch.setenv(
                "HERMES_WEBUI_PLUGINS_DIR",
                str(first if index % 2 == 0 else second),
            )
            plugins.strict_install_managed_plugins()
    finally:
        stop.set()
        thread.join(timeout=5)

    assert failures == []


def test_managed_plugin_receipt_and_roots_canonicalize_ancestor_alias(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    aliased_root = alias_parent / "plugins"
    _configure(plugins, monkeypatch, aliased_root)
    dashboard = _plugin(aliased_root, "alpha")

    receipt = plugins.strict_install_managed_plugins()
    state = plugins.get_plugin_runtime_snapshot()

    assert receipt.plugin_root == str((real_parent / "plugins").resolve())
    assert state.static_roots["alpha"] == dashboard.resolve()


@pytest.mark.parametrize("limit", ["file-bytes", "file-count", "total-bytes"])
def test_managed_plugin_asset_snapshot_enforces_explicit_memory_limits(
    tmp_path, monkeypatch, limit
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    if limit == "file-bytes":
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_ASSET_BYTES", 3)
        _asset(dashboard, "dist/app.js", b"four")
    elif limit == "file-count":
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_ASSET_FILES", 1)
        _asset(dashboard, "dist/app.js", b"a")
        _asset(dashboard, "dist/style.css", b"b")
    else:
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_TOTAL_ASSET_BYTES", 3)
        _asset(dashboard, "dist/app.js", b"aa")
        _asset(dashboard, "dist/style.css", b"bb")

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="limit"):
        plugins.strict_install_managed_plugins()
    assert plugins.get_plugin_runtime_snapshot().manifests == {}


@pytest.mark.parametrize("state_gap", ["assets", "managed-flag"])
def test_managed_plugin_verifier_never_accepts_incomplete_runtime_state(
    tmp_path, monkeypatch, state_gap
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    _asset(dashboard, "dist/app.js", b"validated")
    plugins.strict_install_managed_plugins()
    current = plugins.get_plugin_runtime_snapshot()
    plugins._PLUGIN_RUNTIME_STATE = plugins._plugin_runtime_snapshot(
        dict(current.manifests),
        dict(current.static_roots),
        {"alpha": {}} if state_gap == "assets" else {
            name: dict(value) for name, value in current.assets.items()
        },
        managed=state_gap != "managed-flag",
    )

    result = plugins.verify_strict_managed_plugins()

    assert result.outcome is plugins.ManagedPluginVerificationOutcome.PARTIAL
    if state_gap == "assets":
        assert plugins.serve_plugin_static("alpha", "dist/app.js") is None


def test_managed_shared_plugin_css_uses_validated_bytes_after_root_replacement(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "alpha")
    shared = root / "plugin.css"
    shared.write_bytes(b"validated")
    shared.chmod(0o600)
    replacement = tmp_path / "replacement"
    _plugin(replacement, "alpha")
    replacement_shared = replacement / "plugin.css"
    replacement_shared.write_bytes(b"unvalidated")
    replacement_shared.chmod(0o600)
    displaced = tmp_path / "displaced"
    real_confirm = plugins._managed_plugin_confirm_root

    def replace_after_confirmation(root_path, root_stat):
        real_confirm(root_path, root_stat)
        root.rename(displaced)
        replacement.rename(root)

    monkeypatch.setattr(
        plugins,
        "_managed_plugin_confirm_root",
        replace_after_confirmation,
    )

    plugins.strict_install_managed_plugins()

    assert plugins.serve_plugin_shared_static("plugin.css") == (
        b"validated",
        "text/css; charset=utf-8",
    )


@pytest.mark.parametrize("limit", ["directories", "entries", "depth"])
def test_managed_plugin_asset_snapshot_bounds_directory_traversal(
    tmp_path, monkeypatch, limit
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    if limit == "directories":
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_DIRECTORIES", 3)
        _asset(dashboard, "dist/nested/app.js", b"x")
    elif limit == "entries":
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_DIRECTORY_ENTRIES", 1)
        _asset(dashboard, "dist/app.js", b"x")
        _asset(dashboard, "dist/style.css", b"x")
    else:
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_DEPTH", 1)
        _asset(dashboard, "dist/nested/app.js", b"x")

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="limit"):
        plugins.strict_install_managed_plugins()


def test_managed_plugin_install_ignores_bounded_non_dashboard_plugins(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    helper = root / "helper"
    helper.mkdir(mode=0o700)
    config = helper / "plugin.yaml"
    config.write_text("name: helper\n", encoding="utf-8")
    config.chmod(0o600)
    _plugin(root, "dashboard")

    receipt = plugins.reconcile_strict_managed_plugins()

    assert receipt.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_COMPLETE
    assert receipt.receipt.names == ("dashboard",)
    assert receipt.receipt.ignored_names == ("helper",)
    assert set(plugins.get_plugin_runtime_snapshot().manifests) == {"dashboard"}


def test_managed_plugin_ignored_entry_tamper_is_detected(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    helper = root / "helper"
    helper.mkdir(mode=0o700)
    config = helper / "plugin.yaml"
    config.write_text("name: helper\n", encoding="utf-8")
    config.chmod(0o600)
    real_snapshot_entries = plugins._managed_plugin_snapshot_entry_evidence
    scans = 0

    def tamper_after_first_scan(directory_fd, label, budget):
        nonlocal scans
        result = real_snapshot_entries(directory_fd, label, budget)
        if label == "plugin helper":
            scans += 1
            if scans == 1:
                config.write_text("name: changed\n", encoding="utf-8")
                config.chmod(0o600)
        return result

    monkeypatch.setattr(
        plugins,
        "_managed_plugin_snapshot_entry_evidence",
        tamper_after_first_scan,
    )

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="changed"):
        plugins.strict_install_managed_plugins()


def test_managed_plugin_ignored_entry_rejects_symlink(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    helper = root / "helper"
    helper.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("unsafe", encoding="utf-8")
    (helper / "plugin.yaml").symlink_to(outside)

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="unsafe"):
        plugins.strict_install_managed_plugins()


def test_managed_plugin_verifier_normalizes_manifest_arrays(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "alpha", extra={"slots": ["header-banner"]})

    plugins.strict_install_managed_plugins()
    result = plugins.verify_strict_managed_plugins()

    assert result.outcome is plugins.ManagedPluginVerificationOutcome.PROVED_COMPLETE


@pytest.mark.parametrize(
    ("maximum", "expected_open_dirs"),
    [(1, 1), (2, 2), (3, 3)],
)
def test_managed_plugin_directory_limit_closes_every_opened_fd(
    tmp_path, monkeypatch, maximum, expected_open_dirs
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha")
    _asset(dashboard, "dist/app.js", b"x")
    real_open_dir = plugins._managed_plugin_open_dir
    real_close = plugins.os.close
    opened: set[int] = set()
    open_count = 0

    def track_open(parent_fd, name, label):
        nonlocal open_count
        result = real_open_dir(parent_fd, name, label)
        opened.add(result[0])
        open_count += 1
        return result

    def track_close(fd):
        opened.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(plugins, "_managed_plugin_open_dir", track_open)
    monkeypatch.setattr(plugins.os, "close", track_close)
    monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_DIRECTORIES", maximum)

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="limit"):
        plugins.strict_install_managed_plugins()

    assert open_count == expected_open_dirs
    assert opened == set()


def test_managed_plugin_root_directory_limit_closes_root_fd(tmp_path, monkeypatch):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    real_open_root = plugins._managed_plugin_open_root
    real_close = plugins.os.close
    opened: set[int] = set()

    def track_open(root_path):
        result = real_open_root(root_path)
        opened.add(result[0])
        return result

    def track_close(fd):
        opened.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(plugins, "_managed_plugin_open_root", track_open)
    monkeypatch.setattr(plugins.os, "close", track_close)
    monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_DIRECTORIES", 0)

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="limit"):
        plugins.strict_install_managed_plugins()

    assert opened == set()


def test_managed_plugin_snapshot_bounds_aggregate_directory_entry_evidence(
    tmp_path, monkeypatch
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    for plugin_index in range(4):
        helper = root / f"helper-{plugin_index}"
        helper.mkdir(mode=0o700)
        for entry_index in range(3):
            entry = helper / f"entry-{entry_index}.yaml"
            entry.write_text("safe: true\n", encoding="utf-8")
            entry.chmod(0o600)
    monkeypatch.setattr(
        plugins,
        "_MANAGED_PLUGIN_MAX_TOTAL_DIRECTORY_ENTRIES",
        10,
    )

    with pytest.raises(
        plugins.ManagedPluginSnapshotError,
        match="aggregate directory-entry evidence limit",
    ):
        plugins.strict_install_managed_plugins()

    assert plugins.get_plugin_runtime_snapshot().manifests == {}


@pytest.mark.parametrize("limit", ["count", "total-bytes"])
def test_managed_plugin_snapshot_bounds_aggregate_manifests(
    tmp_path, monkeypatch, limit
):
    import api.plugins as plugins

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    _plugin(root, "alpha", extra={"description": "a" * 128})
    _plugin(root, "beta", extra={"description": "b" * 128})
    if limit == "count":
        monkeypatch.setattr(plugins, "_MANAGED_PLUGIN_MAX_MANIFEST_FILES", 1)
    else:
        monkeypatch.setattr(
            plugins,
            "_MANAGED_PLUGIN_MAX_TOTAL_MANIFEST_BYTES",
            200,
        )

    with pytest.raises(plugins.ManagedPluginSnapshotError, match="manifest.*limit"):
        plugins.strict_install_managed_plugins()


def test_plugins_shared_namespace_miss_fails_before_plugin_page_routing(
    tmp_path, monkeypatch
):
    import api.plugins as plugins
    import api.routes as routes

    root = tmp_path / "plugins"
    _configure(plugins, monkeypatch, root)
    dashboard = _plugin(root, "alpha", tab_path="/plugins/not-allowed")
    _asset(dashboard, "dist/index.html", b"must-not-serve")
    plugins.strict_install_managed_plugins()

    class Handler:
        def __init__(self):
            self.wfile = BytesIO()
            self.statuses = []

        def send_response(self, status):
            self.statuses.append(status)

        def send_header(self, *_args):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    with patch("api.routes._dashboard_plugin_enabled", return_value=True):
        result = routes.handle_get(handler, urlparse("/plugins/not-allowed"))

    assert result is False
    assert handler.statuses == []
    assert handler.wfile.getvalue() == b""
