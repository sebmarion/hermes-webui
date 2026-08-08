"""Root-backed named-profile config inheritance contract for WebUI."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from api import config, models, onboarding, profiles


@pytest.fixture(autouse=True)
def _reset_config_caches(monkeypatch):
    snapshot = {
        "cache": copy.deepcopy(config._cfg_cache),
        "cfg": config.cfg,
        "path": config._cfg_path,
        "mtime": config._cfg_mtime,
        "signature": config._cfg_signature,
        "fingerprint": config._cfg_fingerprint,
        "yaml": copy.deepcopy(config._yaml_file_cache),
    }
    config._cfg_cache.clear()
    config._cfg_path = None
    config._cfg_mtime = 0.0
    config._cfg_signature = None
    config._cfg_fingerprint = None
    config.cfg = config._cfg_cache
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()
    yield
    config._cfg_cache.clear()
    config._cfg_cache.update(snapshot["cache"])
    config.cfg = snapshot["cfg"]
    config._cfg_path = snapshot["path"]
    config._cfg_mtime = snapshot["mtime"]
    config._cfg_signature = snapshot["signature"]
    config._cfg_fingerprint = snapshot["fingerprint"]
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()
        config._yaml_file_cache.update(snapshot["yaml"])


@pytest.fixture()
def profile_tree(tmp_path):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    return root, profile


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    stat_result = path.stat()
    os.utime(
        path,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
    )


def test_named_profile_loads_root_plus_sparse_override_but_raw_cache_stays_physical(
    profile_tree,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "root-provider", "default": "root-model"},
            "terminal": {"cwd": "/root-workspace"},
        },
    )
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "model": {"default": "child-model"},
        },
    )

    physical = config._load_yaml_config_file_raw(child_path)
    effective = config._load_yaml_config_file(child_path)

    assert physical == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {"default": "child-model"},
    }
    assert effective == {
        "model": {"provider": "root-provider", "default": "child-model"},
        "terminal": {"cwd": "/root-workspace"},
    }


def test_named_profile_masks_root_values(profile_tree):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"nested": {"keep": True, "remove": True}},
    )
    _write_yaml(
        profile / "config.yaml",
        {
            "_profile": {
                "inherits": "default",
                "version": 1,
                "masks": [["nested", "remove"]],
            }
        },
    )

    assert config._load_yaml_config_file(profile / "config.yaml") == {
        "nested": {"keep": True}
    }


def test_invalid_named_profile_metadata_fails_closed(profile_tree):
    root, profile = profile_tree
    _write_yaml(root / "config.yaml", {"model": {"default": "root-model"}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "foreign", "version": 1}},
    )

    with pytest.raises(Exception, match="inherits"):
        config._load_yaml_config_file(profile / "config.yaml")


def test_named_profile_save_persists_only_delta_and_keeps_root_unchanged(profile_tree):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    root_document = {
        "model": {"provider": "root-provider", "default": "root-model"},
        "terminal": {"cwd": "/root-workspace"},
    }
    _write_yaml(root_path, root_document)
    _write_yaml(child_path, {})

    desired = config._load_yaml_config_file(child_path)
    desired["model"]["default"] = "child-model"
    config._save_yaml_config_file(child_path, desired)

    assert yaml.safe_load(root_path.read_text(encoding="utf-8")) == root_document
    assert yaml.safe_load(child_path.read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {"default": "child-model"},
    }
    assert config._load_yaml_config_file(child_path)["terminal"] == {
        "cwd": "/root-workspace"
    }


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_named_profile_save_rejects_config_alias_to_root(
    profile_tree,
    alias_kind,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    root_document = {
        "model": {"provider": "openai-codex", "default": "gpt-root"},
        "terminal": {"cwd": "/root-workspace"},
    }
    _write_yaml(root_path, root_document)
    if alias_kind == "symlink":
        child_path.symlink_to(root_path)
    else:
        os.link(root_path, child_path)
    before = root_path.read_bytes()

    with pytest.raises(Exception, match="alias|link|same file"):
        config._save_yaml_config_file(
            child_path,
            {
                **root_document,
                "model": {
                    "provider": "openai-codex",
                    "default": "gpt-child",
                },
            },
        )

    assert root_path.read_bytes() == before


def test_named_profile_save_rejects_alias_swap_at_write_boundary(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    root_document = {"model": {"default": "root-model"}}
    _write_yaml(root_path, root_document)
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    before = root_path.read_bytes()
    dependency_signature, write_lock, project_override, read_effective = (
        config._agent_profile_config_helpers()
    )

    def _swap_after_projection(*args, **kwargs):
        projected = project_override(*args, **kwargs)
        child_path.unlink()
        child_path.symlink_to(root_path)
        return projected

    # Keep the semantic signature stable to model an inode-only alias swap;
    # the write boundary must independently reject links at point of use.
    frozen_signature = tuple(dependency_signature(child_path))
    monkeypatch.setattr(
        config,
        "_agent_profile_config_helpers",
        lambda: (
            lambda _path: frozen_signature,
            write_lock,
            _swap_after_projection,
            read_effective,
        ),
    )

    with pytest.raises(Exception, match="alias|link|same file"):
        config._save_yaml_config_file(
            child_path,
            {"model": {"default": "child-model"}},
        )

    assert root_path.read_bytes() == before


def test_named_profile_noop_save_preserves_bytes_mtime_and_signature(profile_tree):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"model": {"default": "root-model"}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    desired = config._load_yaml_config_file(child_path)
    config._save_yaml_config_file(child_path, desired)
    before_bytes = child_path.read_bytes()
    before_mtime = child_path.stat().st_mtime_ns
    before_signature = config._config_dependency_signature(child_path)

    config._save_yaml_config_file(child_path, desired)

    assert child_path.read_bytes() == before_bytes
    assert child_path.stat().st_mtime_ns == before_mtime
    assert config._config_dependency_signature(child_path) == before_signature


def test_named_profile_save_preserves_env_template_across_rotation(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "custom_providers": [
                {"name": "Rotating", "api_key": "${ROTATING_SECRET}"}
            ],
            "model": {"default": "root-model"},
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setenv("ROTATING_SECRET", "synthetic-old-value")
    desired = config._load_yaml_config_file(child_path)
    monkeypatch.setenv("ROTATING_SECRET", "synthetic-new-value")
    desired["model"]["default"] = "child-model"

    config._save_yaml_config_file(child_path, desired)

    child_text = child_path.read_text(encoding="utf-8")
    assert "synthetic-old-value" not in child_text
    assert "synthetic-new-value" not in child_text
    assert yaml.safe_load(child_text) == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {"default": "child-model"},
    }
    assert "${ROTATING_SECRET}" in root_path.read_text(encoding="utf-8")


def test_named_profile_save_rejects_stale_root_read_before_lock(profile_tree):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"routing": {"root_value": 0}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    desired = config._load_yaml_config_file(child_path)
    _write_yaml(root_path, {"routing": {"root_value": 1}})
    _bump_mtime(root_path)
    desired["routing"]["child_value"] = True

    with pytest.raises(Exception, match="changed|stale|retry"):
        config._save_yaml_config_file(child_path, desired)

    assert yaml.safe_load(child_path.read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1}
    }
    assert config._load_yaml_config_file(child_path)["routing"] == {"root_value": 1}


def test_named_profile_stale_save_survives_intervening_config_read(
    profile_tree,
    tmp_path,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    other_path = tmp_path / "other" / "config.yaml"
    _write_yaml(root_path, {"routing": {"root_value": 0}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    _write_yaml(other_path, {"model": {"default": "other"}})
    desired = config._load_yaml_config_file(child_path)
    config._load_yaml_config_file(other_path)
    _write_yaml(root_path, {"routing": {"root_value": 1}})
    _bump_mtime(root_path)
    desired["routing"]["child_value"] = True

    with pytest.raises(Exception, match="changed|stale|retry"):
        config._save_yaml_config_file(child_path, desired)

    assert yaml.safe_load(child_path.read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1}
    }


@pytest.mark.parametrize("getter_name", ["get_config", "get_config_snapshot"])
def test_cached_config_objects_cannot_freeze_a_stale_root_into_child_override(
    profile_tree,
    monkeypatch,
    getter_name,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "provider-a", "default": "shared"},
            "mcp_servers": {},
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    config.reload_config()
    desired = getattr(config, getter_name)()
    before_child = child_path.read_bytes()

    _write_yaml(
        root_path,
        {
            "model": {"provider": "provider-b", "default": "shared"},
            "mcp_servers": {},
        },
    )
    desired["mcp_servers"] = {"new": {"command": "synthetic"}}

    with pytest.raises(
        RuntimeError,
        match="shared config cache|changed after it was read",
    ):
        config._save_yaml_config_file(child_path, desired)

    assert child_path.read_bytes() == before_child


def test_active_config_update_rejects_root_change_without_mutating_global_cache(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "provider-a", "default": "shared"},
            "mcp_servers": {},
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profile)
    config._cfg_cache.update({"sentinel": "global-cache"})
    config.cfg = config._cfg_cache
    before_cache = copy.deepcopy(config._cfg_cache)
    before_child = child_path.read_bytes()

    def _operation(config_data, persist):
        _write_yaml(
            root_path,
            {
                "model": {"provider": "provider-b", "default": "shared"},
                "mcp_servers": {},
            },
        )
        config_data["mcp_servers"] = {"new": {"command": "synthetic"}}
        persist(config_data)

    with pytest.raises(RuntimeError, match="changed after it was read"):
        config._with_active_config_update(_operation)

    assert child_path.read_bytes() == before_child
    assert config._cfg_cache == before_cache


def test_active_config_update_rejects_root_creation_after_empty_effective_read(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profile)
    config._cfg_cache.update({"sentinel": "global-cache"})
    config.cfg = config._cfg_cache
    before_cache = copy.deepcopy(config._cfg_cache)
    before_child = child_path.read_bytes()

    def _operation(config_data, persist):
        assert config_data == {}
        _write_yaml(
            root_path,
            {"model": {"provider": "provider-b", "default": "shared"}},
        )
        config_data["mcp_servers"] = {"new": {"command": "synthetic"}}
        persist(config_data)

    with pytest.raises(RuntimeError, match="changed after it was read"):
        config._with_active_config_update(_operation)

    assert child_path.read_bytes() == before_child
    assert config._cfg_cache == before_cache


def test_active_config_update_rejects_profile_change_before_persist(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    first_path = profile / "config.yaml"
    second = root / "profiles" / "second"
    second.mkdir(parents=True)
    second_path = second / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "provider-a", "default": "shared"}},
    )
    marker = {"_profile": {"inherits": "default", "version": 1}}
    _write_yaml(first_path, marker)
    _write_yaml(second_path, marker)
    active_path = {"value": first_path}
    monkeypatch.setattr(config, "_get_config_path", lambda: active_path["value"])
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profile)
    before_first = first_path.read_bytes()
    before_second = second_path.read_bytes()

    def _operation(config_data, persist):
        active_path["value"] = second_path
        config_data["mcp_servers"] = {"new": {"command": "synthetic"}}
        persist(config_data)

    with pytest.raises(RuntimeError, match="Active profile changed"):
        config._with_active_config_update(_operation)

    assert first_path.read_bytes() == before_first
    assert second_path.read_bytes() == before_second


def test_active_config_update_preserves_inherited_env_template_after_rotation(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {"provider": "provider-a", "default": "shared"},
            "custom_providers": [
                {"name": "Synthetic", "api_key": "${ROTATING_SECRET}"}
            ],
            "mcp_servers": {},
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "ROTATING_SECRET=synthetic-old-value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: profile)

    def _operation(config_data, persist):
        (profile / ".env").write_text(
            "ROTATING_SECRET=synthetic-new-value\n",
            encoding="utf-8",
        )
        config_data["mcp_servers"] = {"new": {"command": "synthetic"}}
        persist(config_data)

    config._with_active_config_update(_operation)

    child_text = child_path.read_text(encoding="utf-8")
    assert "synthetic-old-value" not in child_text
    assert "synthetic-new-value" not in child_text
    assert yaml.safe_load(child_text) == {
        "_profile": {"inherits": "default", "version": 1},
        "mcp_servers": {"new": {"command": "synthetic"}},
    }


def test_root_edit_invalidates_active_named_profile_cache(profile_tree, monkeypatch):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"model": {"default": "root-one"}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)

    config.reload_config()
    assert config.get_config()["model"]["default"] == "root-one"

    _write_yaml(root_path, {"model": {"default": "root-two"}})
    _bump_mtime(root_path)

    assert config.get_config()["model"]["default"] == "root-two"


def test_same_path_empty_cache_reload_does_not_discard_rebound_runtime_override(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(root / "config.yaml", {"model": {"default": "root-model"}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    config._cfg_path = child_path
    config._cfg_cache.clear()
    config.cfg = {"runtime_override": True}

    assert config.get_config() == {"runtime_override": True}


def test_profile_path_change_never_cross_serves_rebound_runtime_override(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    first = root / "profiles" / "first"
    second = root / "profiles" / "second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _write_yaml(root / "config.yaml", {"shared": True})
    _write_yaml(
        first / "config.yaml",
        {
            "_profile": {"inherits": "default", "version": 1},
            "profile_name": "first",
        },
    )
    _write_yaml(
        second / "config.yaml",
        {
            "_profile": {"inherits": "default", "version": 1},
            "profile_name": "second",
        },
    )
    active = {"path": first / "config.yaml"}
    monkeypatch.setattr(config, "_get_config_path", lambda: active["path"])
    config.reload_config()
    config.cfg = {"profile_name": "runtime-first"}

    active["path"] = second / "config.yaml"

    assert config.get_config()["profile_name"] == "second"


def test_malformed_root_keeps_same_profile_last_known_good(profile_tree, monkeypatch):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"model": {"default": "root-good"}})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)

    config.reload_config()
    assert config.get_config()["model"]["default"] == "root-good"

    root_path.write_text("model: [\n", encoding="utf-8")
    _bump_mtime(root_path)

    assert config.get_config()["model"]["default"] == "root-good"


@pytest.mark.parametrize("race_layer", ["root", "child"])
def test_reload_retries_when_a_config_layer_changes_during_composition(
    profile_tree,
    monkeypatch,
    race_layer,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "one"})
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "child_value": "one",
        },
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    real_loader = config._load_yaml_config_file_raw
    raced = {"done": False}
    race_path = root_path if race_layer == "root" else child_path

    def racing_loader(path, *args, **kwargs):
        result = real_loader(path, *args, **kwargs)
        if Path(path) == race_path and not raced["done"]:
            raced["done"] = True
            if race_layer == "root":
                _write_yaml(root_path, {"root_value": "two"})
                _bump_mtime(root_path)
            else:
                _write_yaml(
                    child_path,
                    {
                        "_profile": {"inherits": "default", "version": 1},
                        "child_value": "two",
                    },
                )
                _bump_mtime(child_path)
        return result

    monkeypatch.setattr(config, "_load_yaml_config_file_raw", racing_loader)
    config.reload_config()
    monkeypatch.setattr(config, "_load_yaml_config_file_raw", real_loader)

    expected_key = "root_value" if race_layer == "root" else "child_value"
    assert config.get_config()[expected_key] == "two"
    assert config._cfg_signature == config._config_dependency_signature(child_path)


def test_snapshot_churn_exhaustion_retries_on_next_read(profile_tree, monkeypatch):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "old"})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    config.reload_config()
    assert config.get_config()["root_value"] == "old"

    real_loader = config._load_yaml_config_file_raw
    replacements = iter(("two", "three", "final"))

    def churning_loader(path, *args, **kwargs):
        result = real_loader(path, *args, **kwargs)
        if Path(path) == root_path:
            try:
                replacement = next(replacements)
            except StopIteration:
                return result
            _write_yaml(root_path, {"root_value": replacement})
            _bump_mtime(root_path)
        return result

    monkeypatch.setattr(config, "_load_yaml_config_file_raw", churning_loader)
    config.reload_config()
    assert config._cfg_cache["root_value"] == "old"

    monkeypatch.setattr(config, "_load_yaml_config_file_raw", real_loader)
    assert config.get_config()["root_value"] == "final"


def test_transient_invalid_layer_finishing_valid_retries_on_next_read(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "old"})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    config.reload_config()

    real_loader = config._load_yaml_config_file_raw
    injected = {"done": False}

    def transient_invalid_loader(path, *args, **kwargs):
        if Path(path) != root_path or injected["done"]:
            return real_loader(path, *args, **kwargs)
        injected["done"] = True
        root_path.write_text("root_value: [\n", encoding="utf-8")
        _bump_mtime(root_path)
        try:
            return real_loader(path, *args, **kwargs)
        except Exception:
            _write_yaml(root_path, {"root_value": "final"})
            _bump_mtime(root_path)
            raise

    monkeypatch.setattr(
        config,
        "_load_yaml_config_file_raw",
        transient_invalid_loader,
    )
    config.reload_config()
    monkeypatch.setattr(config, "_load_yaml_config_file_raw", real_loader)

    assert config.get_config()["root_value"] == "final"


@pytest.mark.parametrize("race_layer", ["root", "child"])
def test_effective_read_detects_same_size_restored_mtime_edit(
    profile_tree,
    race_layer,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "one"})
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "child_value": "one",
        },
    )
    assert config._load_yaml_config_file(child_path)[f"{race_layer}_value"] == "one"
    race_path = root_path if race_layer == "root" else child_path
    previous_stat = race_path.stat()
    if race_layer == "root":
        _write_yaml(root_path, {"root_value": "two"})
    else:
        _write_yaml(
            child_path,
            {
                "_profile": {"inherits": "default", "version": 1},
                "child_value": "two",
            },
        )
    assert race_path.stat().st_size == previous_stat.st_size
    os.utime(
        race_path,
        ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
    )

    assert config._load_yaml_config_file(child_path)[f"{race_layer}_value"] == "two"


@pytest.mark.parametrize("race_layer", ["root", "child"])
def test_effective_read_retries_same_size_restored_mtime_edit_during_composition(
    profile_tree,
    monkeypatch,
    race_layer,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "one"})
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "child_value": "one",
        },
    )
    race_path = root_path if race_layer == "root" else child_path
    previous_stat = race_path.stat()
    real_loader = config._load_yaml_config_file_raw
    raced = {"done": False}

    def racing_loader(path, *args, **kwargs):
        result = real_loader(path, *args, **kwargs)
        if Path(path) == race_path and not raced["done"]:
            raced["done"] = True
            if race_layer == "root":
                _write_yaml(root_path, {"root_value": "two"})
            else:
                _write_yaml(
                    child_path,
                    {
                        "_profile": {"inherits": "default", "version": 1},
                        "child_value": "two",
                    },
                )
            assert race_path.stat().st_size == previous_stat.st_size
            os.utime(
                race_path,
                ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
            )
        return result

    monkeypatch.setattr(config, "_load_yaml_config_file_raw", racing_loader)

    expected_key = "root_value" if race_layer == "root" else "child_value"
    assert config._load_yaml_config_file(child_path)[expected_key] == "two"


@pytest.mark.parametrize("race_layer", ["root", "child"])
def test_effective_read_detects_changed_bytes_when_all_stat_fields_are_unchanged(
    profile_tree,
    monkeypatch,
    race_layer,
):
    """Content identity is the portable backstop when stat metadata is coarse."""
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(root_path, {"root_value": "one"})
    _write_yaml(
        child_path,
        {
            "_profile": {"inherits": "default", "version": 1},
            "child_value": "one",
        },
    )
    assert config._load_yaml_config_file(child_path)[f"{race_layer}_value"] == "one"
    race_path = root_path if race_layer == "root" else child_path
    frozen_stat = race_path.stat()
    if race_layer == "root":
        _write_yaml(root_path, {"root_value": "two"})
    else:
        _write_yaml(
            child_path,
            {
                "_profile": {"inherits": "default", "version": 1},
                "child_value": "two",
            },
        )
    assert race_path.stat().st_size == frozen_stat.st_size

    real_stat = Path.stat

    def stat_with_frozen_identity(path, *args, **kwargs):
        if Path(path) == race_path:
            return frozen_stat
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_frozen_identity)

    assert config._load_yaml_config_file(child_path)[f"{race_layer}_value"] == "two"


def test_root_env_reference_expands_under_active_profile_scope(profile_tree):
    root, profile = profile_tree
    _write_yaml(root / "config.yaml", {"endpoint": "${PROFILE_ENDPOINT}"})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    previous_env = getattr(config._thread_ctx, "env", None)
    previous_block = getattr(config._thread_ctx, "block_process_env_fallback", False)
    try:
        config._thread_ctx.env = {"PROFILE_ENDPOINT": "https://child.invalid"}
        config._thread_ctx.block_process_env_fallback = True
        assert config._load_yaml_config_file(profile / "config.yaml")["endpoint"] == (
            "https://child.invalid"
        )
    finally:
        config._thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del config._thread_ctx.env
            except AttributeError:
                pass
        else:
            config._thread_ctx.env = previous_env


def test_cached_config_reads_expand_under_active_profile_scope(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(root / "config.yaml", {"secret_probe": "${PROFILE_SECRET}"})
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setenv("PROFILE_SECRET", "synthetic-root-value")
    config.reload_config()
    assert config._cfg_cache["secret_probe"] == "synthetic-root-value"

    previous_env = getattr(config._thread_ctx, "env", None)
    previous_block = getattr(config._thread_ctx, "block_process_env_fallback", False)
    try:
        config._thread_ctx.env = {"PROFILE_SECRET": "synthetic-child-value"}
        config._thread_ctx.block_process_env_fallback = True
        assert config.get_config()["secret_probe"] == "synthetic-child-value"
        assert config.get_config_snapshot()["secret_probe"] == "synthetic-child-value"
    finally:
        config._thread_ctx.block_process_env_fallback = previous_block
        if previous_env is None:
            try:
                del config._thread_ctx.env
            except AttributeError:
                pass
        else:
            config._thread_ctx.env = previous_env

    assert config._cfg_cache["secret_probe"] == "synthetic-root-value"


def test_scoped_models_catalog_uses_child_config_without_replacing_global_cache(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {
            "model": {
                "provider": "${PROFILE_PROVIDER}",
                "default": "shared-model",
            }
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_PROVIDER=novita\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setenv("PROFILE_PROVIDER", "openrouter")
    config._cfg_cache.update(
        {"model": {"provider": "openrouter", "default": "ambient-model"}}
    )
    config.cfg = config._cfg_cache
    config._cfg_path = root / "config.yaml"
    config._cfg_signature = config._config_dependency_signature(root / "config.yaml")
    config._cfg_fingerprint = config._fingerprint_config(config._cfg_cache)
    before_cache = copy.deepcopy(config._cfg_cache)
    before_path = config._cfg_path
    profiles.set_request_profile("worker")
    try:
        with profiles.profile_env_for_active_request_readonly("scoped catalog test"):
            assert config._get_config_path() == child_path
            scoped_snapshot = config.get_config_snapshot()
            assert scoped_snapshot["model"]["provider"] == "novita"
            assert scoped_snapshot["model"]["default"] == "shared-model"
            result = config.get_available_models(prefer_cache=True)
    finally:
        profiles.clear_request_profile()

    assert result["active_provider"] == "novita"
    assert result["default_model"] == "shared-model"
    assert config._cfg_cache == before_cache
    assert config._cfg_path == before_path
    assert os.environ.get("PROFILE_PROVIDER") == "openrouter"


def test_scoped_models_catalog_full_build_ignores_process_catalog_cache(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {
            "model": {
                "provider": "${PROFILE_PROVIDER}",
                "default": "shared-model",
            }
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_PROVIDER=novita\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)
    monkeypatch.setenv("PROFILE_PROVIDER", "openrouter")
    config._cfg_cache.update(
        {"model": {"provider": "openrouter", "default": "ambient-model"}}
    )
    config.cfg = config._cfg_cache
    config._cfg_path = child_path
    config._cfg_signature = config._config_dependency_signature(child_path)
    config._cfg_fingerprint = config._fingerprint_config(config._cfg_cache)
    ambient_catalog = {
        "active_provider": "openrouter",
        "default_model": "ambient-model",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [{"id": "ambient-model", "label": "Ambient Model"}],
            }
        ],
        "aliases": {},
    }
    monkeypatch.setattr(config, "_available_models_cache", ambient_catalog)
    monkeypatch.setattr(
        config,
        "_available_models_cache_ts",
        config.time.monotonic(),
    )
    live_catalog_calls: list[str] = []
    monkeypatch.setattr(
        config,
        "_read_live_provider_model_ids",
        lambda provider_id: live_catalog_calls.append(provider_id) or [],
    )
    before_config_cache = copy.deepcopy(config._cfg_cache)
    profiles.set_request_profile("worker")
    try:
        with profiles.profile_env_for_active_request_readonly("scoped catalog test"):
            monkeypatch.setattr(
                config,
                "_available_models_cache_source_fingerprint",
                config._models_cache_source_fingerprint(),
            )
            before_models_cache = copy.deepcopy(config._available_models_cache)
            result = config.get_available_models()
    finally:
        profiles.clear_request_profile()

    assert result["active_provider"] == "novita"
    assert result["default_model"] == "shared-model"
    assert live_catalog_calls == []
    assert config._cfg_cache == before_config_cache
    assert config._available_models_cache == before_models_cache
    assert os.environ.get("PROFILE_PROVIDER") == "openrouter"


def test_explicit_profile_config_uses_that_profiles_env_not_process_env(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "${PROFILE_PROVIDER}", "default": "shared"}},
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_PROVIDER=novita\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFILE_PROVIDER", "openrouter")

    explicit = config.get_config_for_profile_home(profile)

    assert explicit["model"]["provider"] == "novita"
    assert os.environ.get("PROFILE_PROVIDER") == "openrouter"


def test_per_client_switch_uses_target_profile_env_without_mutating_globals(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "${PROFILE_PROVIDER}", "default": "shared"}},
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_PROVIDER=novita\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("PROFILE_PROVIDER", "openrouter")
    config._cfg_cache.update({"model": {"provider": "openrouter"}})
    config.cfg = config._cfg_cache
    before = {
        "active": profiles.get_active_profile_name(),
        "home": os.environ.get("HERMES_HOME"),
        "provider": os.environ.get("PROFILE_PROVIDER"),
        "cache": copy.deepcopy(config._cfg_cache),
        "path": config._cfg_path,
        "signature": config._cfg_signature,
    }

    result = profiles.switch_profile("worker", process_wide=False)

    assert result["default_model_provider"] == "novita"
    assert profiles.get_active_profile_name() == before["active"]
    assert os.environ.get("HERMES_HOME") == before["home"]
    assert os.environ.get("PROFILE_PROVIDER") == before["provider"]
    assert config._cfg_cache == before["cache"]
    assert config._cfg_path == before["path"]
    assert config._cfg_signature == before["signature"]


def test_explicit_profile_model_readers_use_child_env(
    profile_tree,
    monkeypatch,
):
    from api import routes

    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "${PROFILE_PROVIDER}", "default": "shared"}},
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_PROVIDER=novita\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv("PROFILE_PROVIDER", "openrouter")

    route_cfg = routes._read_profile_config_cached("worker", str(child_path))
    _default_model, model_provider = models._profile_default_model_state("worker")

    assert route_cfg["model"]["provider"] == "novita"
    assert model_provider == "novita"
    assert os.environ.get("PROFILE_PROVIDER") == "openrouter"


def test_invalid_named_profile_config_rejects_default_model_resolution(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "openai-codex", "default": "root-model"}},
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "foreign", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)

    with pytest.raises(Exception, match="inherits"):
        models._profile_default_model_state("worker")

    with pytest.raises(Exception, match="inherits"):
        models.new_session(profile="worker", model=None)


def test_inherited_custom_secret_reference_is_scrubbed_from_profile_worker(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    secret_name = "ROOT_CUSTOM_SECRET"
    _write_yaml(
        root / "config.yaml",
        {
            "custom_providers": [
                {
                    "name": "RootCustom",
                    "api_key": "${ROOT_CUSTOM_SECRET}",
                }
            ]
        },
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv(secret_name, "synthetic-root-only")

    assert secret_name in profiles._profile_secret_env_names(profile)
    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        assert os.environ.get(secret_name) == "synthetic-root-only"
        assert config._thread_local_env_value(secret_name) == ""

    assert os.environ.get(secret_name) == "synthetic-root-only"


def test_child_secret_value_replaces_inherited_process_value_inside_worker(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    secret_name = "ROOT_CUSTOM_SECRET"
    _write_yaml(
        root / "config.yaml",
        {
            "custom_providers": [
                {
                    "name": "RootCustom",
                    "api_key": "${ROOT_CUSTOM_SECRET}",
                }
            ]
        },
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        f"{secret_name}=synthetic-child\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv(secret_name, "synthetic-root-only")

    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        assert os.environ.get(secret_name) == "synthetic-root-only"
        assert config._thread_local_env_value(secret_name) == "synthetic-child"

    assert os.environ.get(secret_name) == "synthetic-root-only"


def test_named_worker_masks_unknown_plugin_secret_from_process_env(
    profile_tree,
    monkeypatch,
):
    from agent import secret_scope

    root, profile = profile_tree
    _write_yaml(root / "config.yaml", {"model": {"default": "root-model"}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)

    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        # A concurrent legacy foreground mirror can add another profile's key
        # after this worker scope is installed. The scope must remain
        # authoritative for names it has never seen.
        monkeypatch.setenv("PLUGIN_PRIVATE_TOKEN", "process-plugin-secret")
        assert secret_scope.get_secret("PLUGIN_PRIVATE_TOKEN") == ""
        assert os.environ.get("PLUGIN_PRIVATE_TOKEN") == "process-plugin-secret"

    assert os.environ.get("PLUGIN_PRIVATE_TOKEN") == "process-plugin-secret"


def test_overlapping_profile_workers_keep_process_env_at_operator_baseline(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    second = root / "profiles" / "second"
    second.mkdir(parents=True)
    marker = {"_profile": {"inherits": "default", "version": 1}}
    _write_yaml(root / "config.yaml", {"probe": "${RACE_SECRET}"})
    _write_yaml(profile / "config.yaml", marker)
    _write_yaml(second / "config.yaml", marker)
    (profile / ".env").write_text("RACE_SECRET=worker-value\n", encoding="utf-8")
    (second / ".env").write_text("RACE_SECRET=second-value\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv("RACE_SECRET", "operator-value")
    monkeypatch.setenv("HERMES_HOME", str(root))

    overlap = threading.Barrier(2)
    observations: dict[str, tuple[str, str | None, str | None]] = {}
    errors: list[BaseException] = []
    observations_lock = threading.Lock()

    def _worker(profile_name: str) -> None:
        try:
            with profiles.profile_env_for_background_worker(
                profile_name,
                patch_skill_modules=False,
            ):
                overlap.wait(timeout=5)
                observed = (
                    config._thread_local_env_value("RACE_SECRET"),
                    os.environ.get("RACE_SECRET"),
                    os.environ.get("HERMES_HOME"),
                )
                with observations_lock:
                    observations[profile_name] = observed
                overlap.wait(timeout=5)
        except BaseException as exc:
            with observations_lock:
                errors.append(exc)

    workers = [
        threading.Thread(target=_worker, args=("worker",)),
        threading.Thread(target=_worker, args=("second",)),
    ]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=10)

    assert not any(worker_thread.is_alive() for worker_thread in workers)
    assert not errors
    assert observations == {
        "worker": ("worker-value", "operator-value", str(root)),
        "second": ("second-value", "operator-value", str(root)),
    }
    assert os.environ.get("RACE_SECRET") == "operator-value"
    assert os.environ.get("HERMES_HOME") == str(root)


@pytest.mark.parametrize(
    "template",
    ["${ROOT_NESTED_SECRET}", "${env:ROOT_NESTED_SECRET}"],
)
def test_worker_scrubs_every_inherited_env_reference_and_child_env_wins(
    profile_tree,
    monkeypatch,
    template,
):
    root, profile = profile_tree
    secret_name = "ROOT_NESTED_SECRET"
    _write_yaml(
        root / "config.yaml",
        {
            "mcp_servers": {
                "demo": {
                    "headers": {"Authorization": f"Bearer {template}"},
                }
            }
        },
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv(secret_name, "synthetic-root-only")

    assert secret_name in profiles._profile_secret_env_names(profile)
    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        assert os.environ.get(secret_name) == "synthetic-root-only"
        rendered = config._load_yaml_config_file(profile / "config.yaml")
        assert "synthetic-root-only" not in rendered["mcp_servers"]["demo"]["headers"][
            "Authorization"
        ]

    (profile / ".env").write_text(
        f"{secret_name}=synthetic-child\n",
        encoding="utf-8",
    )
    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        assert os.environ.get(secret_name) == "synthetic-root-only"
        rendered = config._load_yaml_config_file(profile / "config.yaml")
        assert rendered["mcp_servers"]["demo"]["headers"]["Authorization"] == (
            "Bearer synthetic-child"
        )

    assert os.environ.get(secret_name) == "synthetic-root-only"


@pytest.mark.parametrize("scope_kind", ["background", "readonly"])
def test_invalid_inherited_config_never_yields_unscoped_profile_body(
    profile_tree,
    monkeypatch,
    scope_kind,
):
    root, profile = profile_tree
    _write_yaml(root / "config.yaml", {"model": {"default": "root-model"}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "foreign", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("ROOT_ONLY_SECRET", "synthetic-root-only")
    entered = {"body": False}

    if scope_kind == "background":
        scope = profiles.profile_env_for_background_worker(
            "worker",
            patch_skill_modules=False,
        )
    else:
        profiles.set_request_profile("worker")
        scope = profiles.profile_env_for_active_request_readonly("test")

    try:
        with pytest.raises(Exception, match="inherits"):
            with scope:
                entered["body"] = True
                assert os.environ.get("HERMES_HOME") != str(root)
                assert "ROOT_ONLY_SECRET" not in os.environ
    finally:
        if scope_kind == "readonly":
            profiles.clear_request_profile()

    assert entered["body"] is False
    assert os.environ.get("HERMES_HOME") == str(root)
    assert os.environ.get("ROOT_ONLY_SECRET") == "synthetic-root-only"


def test_inherited_shell_identity_refs_do_not_remove_path_or_home(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"plugin": {"path": "${PATH}", "home": "${HOME}"}},
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    before_path = os.environ.get("PATH")
    before_home = os.environ.get("HOME")

    with profiles.profile_env_for_background_worker(
        "worker",
        patch_skill_modules=False,
    ):
        assert os.environ.get("PATH") == before_path
        assert os.environ.get("HOME") == before_home

    assert os.environ.get("PATH") == before_path
    assert os.environ.get("HOME") == before_home


def test_non_profile_config_path_remains_standalone(tmp_path):
    config_path = tmp_path / "standalone" / "config.yaml"
    _write_yaml(config_path, {"model": {"default": "standalone"}})

    assert config._load_yaml_config_file(config_path) == {
        "model": {"default": "standalone"}
    }
    config._save_yaml_config_file(
        config_path,
        {"model": {"default": "updated"}},
    )
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {
        "model": {"default": "updated"}
    }


def test_profile_runtime_env_inherits_root_terminal_settings(profile_tree):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {"terminal": {"backend": "ssh", "ssh_host": "escha.invalid"}},
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )

    runtime_env = profiles.get_profile_runtime_env(profile)

    assert runtime_env["TERMINAL_ENV"] == "ssh"
    assert runtime_env["TERMINAL_SSH_HOST"] == "escha.invalid"


def test_profile_runtime_terminal_refs_expand_from_child_env_not_process_env(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    _write_yaml(
        root / "config.yaml",
        {
            "terminal": {
                "backend": "ssh",
                "ssh_host": "${PROFILE_HOST}",
            }
        },
    )
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    (profile / ".env").write_text(
        "PROFILE_HOST=child.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFILE_HOST", "root.invalid")

    runtime_env = profiles.get_profile_runtime_env(profile)

    assert runtime_env["TERMINAL_SSH_HOST"] == "child.invalid"
    assert runtime_env["PROFILE_HOST"] == "child.invalid"
    assert os.environ.get("PROFILE_HOST") == "root.invalid"


def _prepare_profile_switch_test(root: Path, monkeypatch) -> tuple[Path, Path]:
    first = root / "profiles" / "first"
    second = root / "profiles" / "second"
    first.mkdir(parents=True, exist_ok=True)
    second.mkdir(parents=True, exist_ok=True)
    _write_yaml(root / "config.yaml", {"shared": True})
    _write_yaml(
        first / "config.yaml",
        {
            "_profile": {"inherits": "default", "version": 1},
            "profile_name": "first",
        },
    )
    _write_yaml(
        second / "config.yaml",
        {"_profile": {"inherits": "foreign", "version": 1}},
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "_set_hermes_home",
        lambda home: os.environ.__setitem__("HERMES_HOME", str(home)),
    )
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return first, second


def test_invalid_process_wide_profile_switch_preserves_previous_profile(
    profile_tree,
    monkeypatch,
):
    root, _profile = profile_tree
    first, second = _prepare_profile_switch_test(root, monkeypatch)
    profiles.switch_profile("first")
    sticky_path = root / "active_profile"
    before = {
        "active": profiles.get_active_profile_name(),
        "home": os.environ.get("HERMES_HOME"),
        "sticky": sticky_path.read_bytes(),
        "config": copy.deepcopy(config.get_config()),
        "path": config._cfg_path,
        "signature": config._cfg_signature,
    }
    assert config._get_config_path() == first / "config.yaml"
    with pytest.raises(Exception, match="inherits"):
        config._load_yaml_config_file(second / "config.yaml")

    with pytest.raises(Exception, match="inherits"):
        profiles.switch_profile("second")

    assert profiles.get_active_profile_name() == before["active"] == "first"
    assert os.environ.get("HERMES_HOME") == before["home"] == str(first)
    assert sticky_path.read_bytes() == before["sticky"]
    assert config.get_config() == before["config"]
    assert config.get_config()["profile_name"] == "first"
    assert config._cfg_path == before["path"] == first / "config.yaml"
    assert config._cfg_signature == before["signature"]
    assert second.is_dir()


def test_invalid_per_client_profile_switch_fails_closed(profile_tree, monkeypatch):
    root, _profile = profile_tree
    _first, _second = _prepare_profile_switch_test(root, monkeypatch)
    with pytest.raises(Exception, match="inherits"):
        config._load_yaml_config_file(_second / "config.yaml")

    with pytest.raises(Exception, match="inherits"):
        profiles.switch_profile("second", process_wide=False)


def test_process_wide_switch_expands_target_with_target_profile_env(
    profile_tree,
    monkeypatch,
):
    root, _profile = profile_tree
    first = root / "profiles" / "first"
    second = root / "profiles" / "second"
    first.mkdir(parents=True, exist_ok=True)
    second.mkdir(parents=True, exist_ok=True)
    _write_yaml(
        root / "config.yaml",
        {"profile_secret_probe": "${PROFILE_SECRET}"},
    )
    for name, home in (("first", first), ("second", second)):
        _write_yaml(
            home / "config.yaml",
            {
                "_profile": {"inherits": "default", "version": 1},
                "profile_name": name,
            },
        )
        (home / ".env").write_text(
            f"PROFILE_SECRET={name}-secret\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "_set_hermes_home",
        lambda home: os.environ.__setitem__("HERMES_HOME", str(home)),
    )
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("PROFILE_SECRET", "first-secret")

    profiles.switch_profile("first")
    assert config.get_config()["profile_secret_probe"] == "first-secret"

    profiles.switch_profile("second")

    assert os.environ.get("PROFILE_SECRET") == "second-secret"
    assert config.get_config()["profile_secret_probe"] == "second-secret"
    assert "first-secret" not in repr(config.get_config())


def test_profile_model_writer_keeps_inherited_root_and_writes_one_override(profile_tree):
    root, profile = profile_tree
    root_document = {
        "model": {"provider": "openai-codex", "default": "gpt-root"},
        "terminal": {"cwd": "/root-workspace"},
    }
    _write_yaml(root / "config.yaml", root_document)
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )

    profiles._write_model_defaults_to_config(profile, default_model="gpt-child")

    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) == root_document
    assert yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {"default": "gpt-child"},
    }


def test_max_tokens_writer_projects_from_effective_profile_config(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_document = {
        "model": {"provider": "openai-codex", "default": "gpt-root"},
        "terminal": {"cwd": "/root-workspace"},
    }
    _write_yaml(root / "config.yaml", root_document)
    child_path = profile / "config.yaml"
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: child_path)

    config.set_max_tokens(4096)

    assert yaml.safe_load(child_path.read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1},
        "max_tokens": 4096,
    }
    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) == root_document


def test_process_wakeup_fingerprint_tracks_inherited_root_config(profile_tree, monkeypatch):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    _write_yaml(root_path, {"model": {"default": "root-one"}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: profile)
    session = SimpleNamespace(profile="worker")

    first = models.process_wakeup_credential_state_fingerprint(session)
    _write_yaml(root_path, {"model": {"default": "root-two"}})
    _bump_mtime(root_path)
    second = models.process_wakeup_credential_state_fingerprint(session)

    assert second != first


def test_root_skill_policy_change_invalidates_future_mtime_skill_cache(
    profile_tree,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    _write_yaml(root_path, {"skills": {"disabled": []}})
    _write_yaml(
        profile / "config.yaml",
        {"_profile": {"inherits": "default", "version": 1}},
    )
    skill_dir = profile / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n",
        encoding="utf-8",
    )
    future = 4_102_444_800
    os.utime(skill_md, (future, future))
    profiles._SKILLS_STATS_CACHE.clear()

    assert profiles._get_profile_skills_stats(profile) == (1, 1)

    _write_yaml(root_path, {"skills": {"disabled": ["demo"]}})
    _bump_mtime(root_path)

    assert profiles._get_profile_skills_stats(profile) == (0, 1)


def test_onboarding_treats_inherited_root_as_existing_config(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "openai-codex", "default": "gpt-root"}},
    )
    runtime = {
        "chat_ready": True,
        "provider_configured": True,
        "provider_ready": True,
        "setup_state": "ready",
    }
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: child_path)
    monkeypatch.setattr(onboarding, "load_settings", lambda: {"onboarding_completed": False})
    monkeypatch.setattr(onboarding, "get_config", lambda: config._load_yaml_config_file(child_path))
    monkeypatch.setattr(onboarding, "verify_hermes_imports", lambda: (True, [], {}))
    monkeypatch.setattr(onboarding, "_status_from_runtime", lambda _cfg, _ok: runtime)
    monkeypatch.setattr(onboarding, "load_workspaces", lambda: [])
    monkeypatch.setattr(onboarding, "get_last_workspace", lambda: None)
    monkeypatch.setattr(onboarding, "get_available_models", lambda: [])
    monkeypatch.setattr(onboarding, "save_settings", lambda _settings: None)
    monkeypatch.setattr(onboarding, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(onboarding, "_build_setup_catalog", lambda _cfg: {})

    status = onboarding.get_onboarding_status()

    assert status["completed"] is True
    assert status["system"]["config_exists"] is True


def test_onboarding_setup_guard_protects_inherited_root_config(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    child_path = profile / "config.yaml"
    _write_yaml(
        root / "config.yaml",
        {"model": {"provider": "openai-codex", "default": "gpt-root"}},
    )
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: child_path)

    def _must_not_load_for_overwrite(_path):
        raise AssertionError("setup must stop before loading or writing a child override")

    monkeypatch.setattr(onboarding, "_load_yaml_config", _must_not_load_for_overwrite)

    result = onboarding.apply_onboarding_setup(
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "synthetic-test-key",
        }
    )

    assert result["error"] == "config_exists"
    assert result["requires_confirm"] is True


def test_confirmed_onboarding_overwrite_keeps_root_and_writes_sparse_child(
    profile_tree,
    monkeypatch,
):
    root, profile = profile_tree
    root_path = root / "config.yaml"
    child_path = profile / "config.yaml"
    _write_yaml(
        root_path,
        {
            "model": {
                "provider": "openai-codex",
                "default": "gpt-root",
                "timeout": 120,
            },
            "terminal": {"cwd": "/root-workspace"},
        },
    )
    _write_yaml(
        child_path,
        {"_profile": {"inherits": "default", "version": 1}},
    )
    root_before = root_path.read_bytes()

    monkeypatch.delenv("HERMES_WEBUI_SKIP_ONBOARDING", raising=False)
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: child_path)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: profile)
    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"ok": True})
    monkeypatch.setattr(onboarding, "reload_config", lambda: None)
    monkeypatch.setattr(profiles, "_reload_dotenv", lambda _home: None)

    onboarding.apply_onboarding_setup(
        {
            "provider": "custom",
            "model": "escha-local",
            "base_url": "http://escha.test:8000/v1/",
            "api_key": "",
            "confirm_overwrite": True,
        }
    )

    assert root_path.read_bytes() == root_before
    assert yaml.safe_load(child_path.read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1},
        "model": {
            "provider": "custom",
            "default": "escha-local",
            "base_url": "http://escha.test:8000/v1",
        },
    }


def test_webui_fallback_clone_keeps_config_sparse(profile_tree, monkeypatch):
    root, _profile = profile_tree
    root_document = {
        "model": {"provider": "openai-codex", "default": "gpt-root"},
        "terminal": {"cwd": "/root-workspace"},
    }
    _write_yaml(root / "config.yaml", root_document)
    (root / ".env").write_text("PROFILE_SECRET=local\n", encoding="utf-8")
    (root / "SOUL.md").write_text("Root soul\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)

    target = profiles._create_profile_fallback(
        "cloned",
        clone_from="default",
        clone_config=True,
    )

    assert yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8")) == {
        "_profile": {"inherits": "default", "version": 1}
    }
    assert config._load_yaml_config_file(target / "config.yaml") == root_document
    assert (target / ".env").read_text(encoding="utf-8") == "PROFILE_SECRET=local\n"
    assert (target / "SOUL.md").read_text(encoding="utf-8") == "Root soul\n"
