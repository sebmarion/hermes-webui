"""Regression coverage for Neuralwatt provider env-var mapping.

Mirrors test_issue2025_xiaomi_env_key.py — ensures the provider ID maps to
the correct env var and that key detection works when the env var is set.
"""

from __future__ import annotations

import builtins

import api.config as config
import api.providers as providers


def _force_env_fallback(monkeypatch):
    """Force get_available_models() down its explicit env-var fallback path."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("hermes_cli.models", "hermes_cli.auth"):
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _run_available_models_with_cfg(monkeypatch, tmp_path, cfg):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path, raising=False)
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()


def test_neuralwatt_env_var_mapping():
    """Neuralwatt maps to NEURALWATT_API_KEY in the provider env-var table."""
    assert providers._PROVIDER_ENV_VAR["neuralwatt"] == "NEURALWATT_API_KEY"


def test_neuralwatt_provider_has_key_when_env_set(monkeypatch, tmp_path):
    """Key detection returns True when NEURALWATT_API_KEY is in the environment."""
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("NEURALWATT_API_KEY", "test-neuralwatt-key")

    assert providers._provider_has_key("neuralwatt") is True


def test_neuralwatt_provider_has_key_false_without_env(monkeypatch, tmp_path):
    """Key detection returns False when NEURALWATT_API_KEY is not set."""
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)

    assert providers._provider_has_key("neuralwatt") is False


def test_neuralwatt_model_group_is_hidden_even_when_configured(monkeypatch, tmp_path):
    """Neuralwatt remains routable/configurable but is suppressed from the model picker."""
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("NEURALWATT_API_KEY", "test-neuralwatt-key")

    result = _run_available_models_with_cfg(
        monkeypatch,
        tmp_path,
        {
            "model": {"default": "glm-5.2", "provider": "neuralwatt"},
            "providers": {
                "neuralwatt": {
                    "base_url": "https://api.neuralwatt.com/v1",
                    "key_env": "NEURALWATT_API_KEY",
                    "api_mode": "chat_completions",
                    "default_model": "glm-5.2",
                    "models": ["glm-5.2", "glm-5.2-short"],
                }
            },
        },
    )

    groups = {group["provider_id"]: group for group in result["groups"]}
    assert "neuralwatt" not in groups, f"neuralwatt should be hidden from groups: {list(groups.keys())}"


def test_hidden_picker_providers_removed_but_openai_codex_kept(monkeypatch, tmp_path):
    """Requested hidden providers do not render as dropdown groups; OpenAI Codex still does."""
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("NEURALWATT_API_KEY", "test-neuralwatt-key")

    result = _run_available_models_with_cfg(
        monkeypatch,
        tmp_path,
        {
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "providers": {
                "openai": {"api_key": "test", "models": ["gpt-5.5"]},
                "openai-api": {"api_key": "test", "models": ["gpt-5.5"]},
                "nvidia": {"api_key": "test", "models": ["nvidia/nemotron-3-super-120b-a12b"]},
                "neuralwatt": {"api_key": "test", "models": ["glm-5.2"]},
                "mindai": {"api_key": "test", "models": ["mindai-coder"]},
            },
        },
    )

    provider_ids = {group["provider_id"] for group in result["groups"]}
    assert "openai-codex" not in config._PICKER_HIDDEN_PROVIDER_IDS
    assert "openai-codex" in provider_ids
    assert provider_ids.isdisjoint({"openai", "openai-api", "nvidia", "neuralwatt", "mindai"})


def test_hidden_picker_provider_labels_removed_from_cached_payload(monkeypatch, tmp_path):
    """Post-processing also cleans stale/cache-style groups that only carry provider labels."""
    payload = {
        "configured_model_badges": {
            "gpt-5.5": {"provider": "OpenAI API"},
            "glm-5.2": {"provider": "custom:neuralwatt"},
            "gpt-5.6-sol": {"provider": "openai-codex"},
        },
        "groups": [
            {"provider": "OpenAI API", "models": [{"id": "gpt-5.5"}]},
            {"provider": "Openai", "models": [{"id": "gpt-5.5"}]},
            {"provider": "NVIDIA NIM", "models": [{"id": "nemotron"}]},
            {"provider": "mindai", "models": [{"id": "mindai-coder"}]},
            {"provider_id": "custom:neuralwatt", "provider": "Neuralwatt", "models": [{"id": "glm-5.2"}]},
            {"provider_id": "openai-codex", "provider": "OpenAI Codex", "models": [{"id": "gpt-5.6-sol"}]},
        ],
    }

    cleaned = config._annotate_fast_tier_model_groups(payload)
    groups = cleaned["groups"]
    assert [group.get("provider_id") for group in groups] == ["openai-codex"]
    assert cleaned["configured_model_badges"] == {
        "gpt-5.6-sol": {"provider": "openai-codex"}
    }
