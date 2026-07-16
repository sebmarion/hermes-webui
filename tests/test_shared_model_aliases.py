from types import SimpleNamespace

from api import config, routes


def test_set_model_aliases_round_trips_shared_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "model:\n  default: gpt-5.6-sol\nmodel_aliases:\n  sol:\n    model: old\n    provider: openai-codex\n    base_url: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    result = config.set_model_aliases({"sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"})

    assert result == {"ok": True, "aliases": {"sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna"}}
    saved = config._load_yaml_config_file(path)
    assert saved["model"]["default"] == "gpt-5.6-sol"
    assert {key: value["model"] for key, value in saved["model_aliases"].items()} == result["aliases"]
    assert saved["model_aliases"]["sol"]["provider"] == "openai-codex"
    assert saved["model_aliases"]["sol"]["base_url"] == ""


def test_set_model_aliases_rejects_ambiguous_names():
    for aliases in ({"bad alias": "model"}, {"Bad": "one", "bad": "two"}):
        try:
            config.set_model_aliases(aliases)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid aliases must be rejected before writing")


def _capture_json(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    return captured


def test_model_alias_routes_follow_active_profile(tmp_path, monkeypatch):
    default_path = tmp_path / "default.yaml"
    active_path = tmp_path / "named.yaml"
    default_path.write_text(
        "model_aliases:\n  default-only:\n    model: default-model\n",
        encoding="utf-8",
    )
    active_path.write_text(
        "model_aliases:\n  luna:\n    model: gpt-5.6-luna\n    provider: openai-codex\n    base_url: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: active_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: default_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    captured = _capture_json(monkeypatch)
    assert routes.handle_get(object(), SimpleNamespace(path="/api/model-aliases")) is True
    assert captured["payload"] == {"aliases": {"luna": "gpt-5.6-luna"}}

    captured.clear()
    monkeypatch.setattr(routes, "read_body", lambda handler: {"aliases": {"sol": "gpt-5.6-sol"}})
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    assert routes.handle_post(object(), SimpleNamespace(path="/api/model-aliases")) is True
    assert captured["status"] == 200
    assert captured["payload"] == {"ok": True, "aliases": {"sol": "gpt-5.6-sol"}}

    active = config._load_yaml_config_file(active_path)
    default = config._load_yaml_config_file(default_path)
    assert active["model_aliases"]["sol"]["model"] == "gpt-5.6-sol"
    assert default["model_aliases"]["default-only"]["model"] == "default-model"
