"""Regression coverage for the profile-scoped WebUI provider menu."""

from __future__ import annotations

import builtins
import copy

import api.config as config
import api.providers as providers


def _force_hermes_cli_fallback(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("hermes_cli.models", "hermes_cli.auth"):
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _catalog_with_cfg(monkeypatch, tmp_path, cfg):
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
        return config._static_models_catalog_without_live_probes()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()


def _menu_cfg():
    return {
        "openai-codex",
        "novita",
        "zeus",
    }


def test_configured_provider_menu_keeps_only_codex_novita_and_zeus(monkeypatch, tmp_path):
    """The configured allowlist applies to groups while direct routing remains unchanged."""
    _force_hermes_cli_fallback(monkeypatch)
    result = _catalog_with_cfg(
        monkeypatch,
        tmp_path,
        {
            "webui": {"provider_menu": {"allowed_providers": sorted(_menu_cfg())}},
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "providers": {
                "openai-codex": {
                    "models": ["gpt-5.6-sol", "gpt-5.6-sol-pro"],
                },
                "novita": {"models": ["zai-org/glm-5.2"]},
                "openrouter": {"models": ["openai/gpt-5.6-sol"]},
                "zai": {"models": ["glm-5.2"]},
                "zeus": {"models": ["escha-qwen36-35b-a3b-w2"]},
            },
            "custom_providers": [
                {"name": "Zeus RTX 5080", "model": "escha-qwen36-35b-a3b-w2"},
            ],
        },
    )

    groups = {group["provider_id"]: group for group in result["groups"]}
    assert set(groups) == _menu_cfg()
    assert "gpt-5.6-sol-pro" not in {
        model["id"]
        for model in groups["openai-codex"].get("models", [])
    }
    assert result["active_provider"] == "openai-codex"


def test_codex_pro_models_are_removed_from_visible_and_overflow_payloads(monkeypatch):
    """Codex `-pro` rows disappear from both picker buckets and configured badges."""
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({"webui": {"provider_menu": {"allowed_providers": sorted(_menu_cfg())}}})
    payload = {
        "configured_model_badges": {
            "gpt-5.6-sol-pro": {"provider": "openai-codex"},
            "gpt-5.6-sol": {"provider": "openai-codex"},
        },
        "groups": [
            {
                "provider_id": "openai-codex",
                "provider": "OpenAI Codex",
                "models": [
                    {"id": "gpt-5.6-sol"},
                    {"id": "gpt-5.6-sol-pro", "label": "GPT 5.6 SOL PRO"},
                    {"id": "gpt-5.6-sol-variant", "label": "GPT 5.6 SOL PRO"},
                ],
                "extra_models": [
                    {"id": "gpt-5.6-terra-pro", "label": "GPT 5.6 Terra PRO"},
                    {"id": "gpt-5.6-terra", "label": "GPT 5.6 Terra"},
                ],
            },
            {
                "provider_id": "openrouter",
                "provider": "OpenRouter",
                "models": [{"id": "openai/gpt-5.6-sol-pro"}],
            },
            {
                "provider_id": "zeus",
                "provider": "Zeus",
                "models": [{"id": "zeus-pro", "label": "Zeus Pro"}],
            },
        ],
    }
    try:
        cleaned = config._annotate_fast_tier_model_groups(payload)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)

    codex = cleaned["groups"][0]
    assert [model["id"] for model in codex["models"]] == ["gpt-5.6-sol"]
    assert [model["id"] for model in codex["extra_models"]] == ["gpt-5.6-terra"]
    assert cleaned["groups"][1]["models"] == [{"id": "zeus-pro", "label": "Zeus Pro"}]
    assert cleaned["configured_model_badges"] == {
        "gpt-5.6-sol": {"provider": "openai-codex"}
    }


def test_provider_cards_use_the_same_configured_menu_allowlist(monkeypatch):
    """Settings provider-card data cannot reintroduce providers hidden from the picker."""
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({"webui": {"provider_menu": {"allowed_providers": sorted(_menu_cfg())}}})
    records = [
        {"id": "openai-codex", "display_name": "OpenAI Codex", "models": []},
        {"id": "novita", "display_name": "Novita", "models": []},
        {"id": "zeus", "display_name": "Zeus", "models": []},
        {"id": "openrouter", "display_name": "OpenRouter", "models": []},
        {"id": "custom:zeus-rtx-5080", "display_name": "Zeus RTX 5080", "models": []},
    ]
    try:
        filtered = providers._filter_webui_provider_records(records)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)

    assert [provider["id"] for provider in filtered] == [
        "openai-codex",
        "novita",
        "zeus",
    ]


def test_menu_projection_does_not_change_runtime_provider_resolution():
    """Filtering is presentation-only: Codex routing and config state stay intact."""
    old_cfg = copy.deepcopy(config.cfg)
    config.cfg.clear()
    config.cfg.update(
        {
            "webui": {"provider_menu": {"allowed_providers": sorted(_menu_cfg())}},
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "providers": {"zeus": {"models": ["escha-qwen36-35b-a3b-w2"]}},
        }
    )
    before = copy.deepcopy(config.cfg)
    try:
        model, provider, _base_url = config.resolve_model_provider("gpt-5.6-sol")
        config._static_models_catalog_without_live_probes()
        assert (model, provider) == ("gpt-5.6-sol", "openai-codex")
        assert config.cfg == before
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
