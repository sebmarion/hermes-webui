"""Contract tests for GPT-5.6 Sol's distinct Codex Ultra mode.

These tests deliberately keep Hermes' provider-facing reasoning effort ladder
canonical through ``max``.  ``ultra`` is accepted only as a Codex control-plane
mode (plus a narrow legacy-ingress spelling), never as a raw provider effort.

All stateful tests redirect both Hermes and Codex homes into ``tmp_path`` so a
test failure cannot read or rewrite the operator's live configuration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from api import config as config


REPO_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _required_helper(name: str):
    helper = getattr(config, name, None)
    assert callable(helper), f"api.config.{name} must implement the Sol Ultra contract"
    return helper


def _selection_parts(selection) -> tuple[str, str | None]:
    """Read the semantic pair without prescribing tuple/dict implementation."""
    if isinstance(selection, dict):
        effort = selection.get("reasoning_effort", selection.get("effort"))
        mode = selection.get("reasoning_mode", selection.get("mode"))
    elif isinstance(selection, (tuple, list)) and len(selection) == 2:
        effort, mode = selection
    else:
        effort = getattr(selection, "reasoning_effort", getattr(selection, "effort", None))
        mode = getattr(selection, "reasoning_mode", getattr(selection, "mode", None))
    return str(effort or ""), (str(mode).strip().lower() if mode else None)


def _write_codex_model_cache(codex_home: Path, levels: list[str]) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "supported_reasoning_levels": [
                            {"effort": level, "description": f"{level} contract fixture"}
                            for level in levels
                        ],
                    },
                    {
                        "slug": "gpt-5.5",
                        "supported_reasoning_levels": [{"effort": "xhigh"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def isolated_ultra_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    hermes_home = tmp_path / "hermes-home"
    codex_home = tmp_path / "codex-home"
    config_path = hermes_home / "config.yaml"
    hermes_home.mkdir()
    _write_codex_model_cache(codex_home, ["low", "medium", "high", "xhigh", "max", "ultra"])

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(
        config,
        "_hermes_agent_codex_app_server_available",
        lambda *args, **kwargs: True,
        raising=False,
    )
    # A unit test must not reload global process state after writing its isolated file.
    monkeypatch.setattr(config, "reload_config", lambda: None)
    return {
        "hermes_home": hermes_home,
        "codex_home": codex_home,
        "config_path": config_path,
    }


def _write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _read_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.mark.parametrize(
    ("model_id", "provider_id", "expected"),
    [
        ("gpt-5.6-sol", "openai-codex", "gpt-5.6-sol"),
        ("gpt-5.6", "openai-codex", "gpt-5.6-sol"),
        ("@openai-codex:gpt-5.6-sol", "openai-codex", "gpt-5.6-sol"),
        ("foo/gpt-5.6-sol", "openai-codex", None),
        ("openai/gpt-5.6", "openai-codex", None),
        ("gpt-5.6-sol-preview", "openai-codex", None),
        ("gpt-5.6-sol", "openai", None),
        ("gpt-5.6-sol", "anthropic", None),
    ],
)
def test_codex_ultra_model_identity_is_exact_and_provider_scoped(
    model_id: str,
    provider_id: str,
    expected: str | None,
) -> None:
    identity = _required_helper("_codex_ultra_model_identity")
    assert identity(model_id, provider_id) == expected


def test_codex_model_cache_parser_reads_advertised_reasoning_levels(
    isolated_ultra_state: dict[str, Path],
) -> None:
    read_levels = _required_helper("_read_codex_model_reasoning_levels")

    levels = read_levels("gpt-5.6-sol")

    assert list(levels) == ["low", "medium", "high", "xhigh", "max", "ultra"]


def test_codex_model_cache_parser_fails_closed_for_missing_or_malformed_catalog(
    isolated_ultra_state: dict[str, Path],
) -> None:
    read_levels = _required_helper("_read_codex_model_reasoning_levels")
    cache_path = isolated_ultra_state["codex_home"] / "models_cache.json"

    cache_path.write_text("not json", encoding="utf-8")
    assert list(read_levels("gpt-5.6-sol")) == []

    cache_path.unlink()
    assert list(read_levels("gpt-5.6-sol")) == []


def test_agent_app_server_capability_requires_binary_and_runtime_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capable = _required_helper("_hermes_agent_codex_app_server_available")
    agent_dir = tmp_path / "hermes-agent"
    (agent_dir / "agent" / "transports").mkdir(parents=True)
    (agent_dir / "agent" / "agent_init.py").write_text(
        'api_mode in {"codex_app_server"}; reasoning_config = {}', encoding="utf-8"
    )
    (agent_dir / "agent" / "codex_runtime.py").write_text(
        "def run_codex_app_server_turn():\n"
        "    CodexAppServerSession(enable_multi_agent=True)\n"
        "    session.run_turn(model=model, effort=effort)",
        encoding="utf-8",
    )
    (agent_dir / "agent" / "transports" / "codex_app_server_session.py").write_text(
        'class CodexAppServerSession: pass\nturn_params["model"] = model\n'
        'turn_params["effort"] = effort\n'
        'multi_agent_enabled = True\nextra_args = ["--enable", "multi_agent"]\n',
        encoding="utf-8",
    )
    (agent_dir / "run_agent.py").write_text(
        "class AIAgent:\n"
        "    def _close_codex_app_server_session(self): pass\n"
        "    def release_clients(self): self._close_codex_app_server_session()\n"
        "    def close(self): self._close_codex_app_server_session()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("shutil.which", lambda name: "/tmp/codex" if name == "codex" else None)

    assert capable(agent_dir=agent_dir) is True

    (agent_dir / "agent" / "codex_runtime.py").write_text(
        "def run_codex_app_server_turn(): session.run_turn(model=model)",
        encoding="utf-8",
    )
    assert capable(agent_dir=agent_dir) is False

    (agent_dir / "agent" / "codex_runtime.py").write_text(
        "def run_codex_app_server_turn(): session.run_turn(model=model, effort=effort)",
        encoding="utf-8",
    )
    assert capable(agent_dir=agent_dir) is False

    (agent_dir / "agent" / "codex_runtime.py").write_text(
        "def run_codex_app_server_turn():\n"
        "    CodexAppServerSession(enable_multi_agent=True)\n"
        "    session.run_turn(model=model, effort=effort)",
        encoding="utf-8",
    )
    (agent_dir / "run_agent.py").write_text(
        "class AIAgent:\n    def release_clients(self): pass\n",
        encoding="utf-8",
    )
    assert capable(agent_dir=agent_dir) is False

    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert capable(agent_dir=agent_dir) is False


def test_ultra_status_and_writes_fail_closed_without_agent_app_server(
    isolated_ultra_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(config_path, {"agent": {"reasoning_effort": "high"}})
    before = config_path.read_bytes()
    monkeypatch.setattr(
        config,
        "_hermes_agent_codex_app_server_available",
        lambda *args, **kwargs: False,
    )

    status = config.get_reasoning_status(
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )
    assert status["ultra_available"] is False
    assert "ultra" not in status["supported_efforts"]

    with pytest.raises(ValueError, match="Ultra|ultra"):
        config.set_reasoning_effort(
            "max",
            mode="ultra",
            model_id="gpt-5.6-sol",
            provider_id="openai-codex",
        )
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("effort", "mode", "expected_effort", "expected_mode"),
    [
        ("ultra", None, "max", "ultra"),  # cached-client compatibility
        ("max", "ultra", "max", "ultra"),  # current client shape
        ("max", None, "max", None),
        ("high", None, "high", None),
        ("none", None, "none", None),
    ],
)
def test_canonical_reasoning_selection_separates_effort_from_mode(
    effort: str,
    mode: str | None,
    expected_effort: str,
    expected_mode: str | None,
) -> None:
    canonicalize = _required_helper("_canonical_reasoning_selection")

    assert _selection_parts(
        canonicalize(
            effort,
            mode,
            model_id="gpt-5.6-sol",
            provider_id="openai-codex",
            ultra_available=True,
        )
    ) == (
        expected_effort,
        expected_mode,
    )


def test_current_ultra_selection_persists_canonical_effort_and_separate_mode(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(config_path, {"agent": {"reasoning_effort": "high"}})

    status = config.set_reasoning_effort(
        "max",
        mode="ultra",
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )

    agent = _read_config(config_path)["agent"]
    assert agent["reasoning_effort"] == "max"
    assert agent["reasoning_mode"] == "ultra"
    assert status["reasoning_effort"] == "max"
    assert status["reasoning_mode"] == "ultra"
    assert status["ultra_available"] is True


def test_eligible_sol_status_advertises_ultra_as_the_ui_selection_alias(
    isolated_ultra_state: dict[str, Path],
) -> None:
    status = config.get_reasoning_status(
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )

    assert status["supported_efforts"][-1] == "ultra"


def test_cached_client_ultra_ingress_is_immediately_canonicalized(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(config_path, {"agent": {"reasoning_effort": "medium"}})

    config.set_reasoning_effort(
        "ultra",
        model_id="gpt-5.6",
        provider_id="openai-codex",
    )

    agent = _read_config(config_path)["agent"]
    assert agent == {"reasoning_effort": "max", "reasoning_mode": "ultra"}


def test_legacy_contextless_ultra_ingress_uses_the_unique_sol_codex_context(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {
            "model": {"default": "ornith-35b-iq2m", "provider": "zeus"},
            "agent": {"reasoning_effort": "high"},
        },
    )

    status = config.set_reasoning_effort("ultra")

    agent = _read_config(config_path)["agent"]
    assert agent == {"reasoning_effort": "max", "reasoning_mode": "ultra"}
    assert status["reasoning_effort"] == "ultra"
    assert status["canonical_reasoning_effort"] == "max"
    assert status["reasoning_mode"] == "ultra"
    assert status["ultra_available"] is True


@pytest.mark.parametrize(
    ("model_id", "provider_id"),
    [("gpt-5.6-sol", None), (None, "openai-codex")],
)
def test_legacy_ultra_with_partial_context_remains_fail_closed(
    isolated_ultra_state: dict[str, Path],
    model_id: str | None,
    provider_id: str | None,
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "agent": {"reasoning_effort": "high"},
        },
    )
    before = config_path.read_bytes()

    with pytest.raises(ValueError, match="Ultra|ultra"):
        config.set_reasoning_effort(
            "ultra",
            model_id=model_id,
            provider_id=provider_id,
        )

    assert config_path.read_bytes() == before


@pytest.mark.parametrize("ordinary_effort", ["max", "high", "none"])
def test_selecting_an_ordinary_effort_clears_ultra_mode(
    isolated_ultra_state: dict[str, Path],
    ordinary_effort: str,
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {"agent": {"reasoning_effort": "max", "reasoning_mode": "ultra"}},
    )

    config.set_reasoning_effort(
        ordinary_effort,
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )

    agent = _read_config(config_path)["agent"]
    assert agent["reasoning_effort"] == ordinary_effort
    assert "reasoning_mode" not in agent


@pytest.mark.parametrize(
    ("model_id", "provider_id", "levels"),
    [
        ("gpt-5.5", "openai-codex", ["max", "ultra"]),
        ("gpt-5.6-sol", "openai", ["max", "ultra"]),
        ("gpt-5.6-sol", "openai-codex", ["low", "max"]),
    ],
)
def test_ineligible_ultra_write_is_rejected_without_mutating_config(
    isolated_ultra_state: dict[str, Path],
    model_id: str,
    provider_id: str,
    levels: list[str],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_codex_model_cache(isolated_ultra_state["codex_home"], levels)
    _write_config(config_path, {"agent": {"reasoning_effort": "high"}, "keep": {"me": True}})
    before = config_path.read_bytes()

    status = config.get_reasoning_status(
        model_id=model_id,
        provider_id=provider_id,
    )
    assert status["ultra_available"] is False
    assert "ultra" not in status["supported_efforts"]

    with pytest.raises(ValueError, match="Ultra|ultra"):
        config.set_reasoning_effort(
            "max",
            mode="ultra",
            model_id=model_id,
            provider_id=provider_id,
        )

    assert config_path.read_bytes() == before


def test_gateway_backend_rejects_ultra_selection_without_mutating_config(
    isolated_ultra_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(config_path, {"agent": {"reasoning_effort": "high"}})
    before = config_path.read_bytes()
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")

    status = config.get_reasoning_status(
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )
    assert status["ultra_available"] is False
    assert "ultra" not in status["supported_efforts"]

    with pytest.raises(ValueError, match="native|Gateway|gateway"):
        config.set_reasoning_effort(
            "max",
            mode="ultra",
            model_id="gpt-5.6-sol",
            provider_id="openai-codex",
        )

    assert config_path.read_bytes() == before


def test_legacy_persisted_ultra_reads_as_max_ultra_only_for_eligible_sol(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(config_path, {"agent": {"reasoning_effort": "ultra"}})

    status = config.get_reasoning_status(
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
    )

    assert status["reasoning_effort"] == "max"
    assert status["reasoning_mode"] == "ultra"
    assert status["ultra_available"] is True


def test_legacy_persisted_ultra_is_inert_for_non_sol_context(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {"agent": {"reasoning_effort": "ultra", "reasoning_mode": "ultra"}},
    )

    status = config.get_reasoning_status(
        model_id="gpt-5.5",
        provider_id="openai-codex",
    )

    assert status["reasoning_effort"] == ""
    assert status.get("reasoning_mode") in (None, "")
    assert status["ultra_available"] is False
    assert "ultra" not in status["supported_efforts"]


def test_unrelated_config_write_canonicalizes_eligible_legacy_ultra(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {
            "model": {"default": "gpt-5.6-sol", "provider": "openai-codex"},
            "agent": {"reasoning_effort": "ultra"},
            "keep": {"me": True},
        },
    )

    config.set_reasoning_display(False)

    saved = _read_config(config_path)
    assert saved["agent"]["reasoning_effort"] == "max"
    assert saved["agent"]["reasoning_mode"] == "ultra"
    assert saved["display"]["show_reasoning"] is False
    assert saved["keep"] == {"me": True}


def test_unrelated_config_write_removes_ineligible_legacy_ultra(
    isolated_ultra_state: dict[str, Path],
) -> None:
    config_path = isolated_ultra_state["config_path"]
    _write_config(
        config_path,
        {
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "agent": {"reasoning_effort": "ultra", "reasoning_mode": "ultra"},
        },
    )

    config.set_reasoning_display(True)

    agent = _read_config(config_path).get("agent", {})
    assert "reasoning_effort" not in agent
    assert "reasoning_mode" not in agent


def test_shared_selector_encodes_ultra_as_mode_over_canonical_max() -> None:
    html = _source("static/index.html")
    option_tags = re.findall(r'<div class="reasoning-option"[^>]*>[^<]+</div>', html)
    max_rows = [tag for tag in option_tags if re.search(r">\s*Max\s*</div>$", tag)]
    ultra_rows = [tag for tag in option_tags if re.search(r">\s*Ultra\s*</div>$", tag)]

    assert len(max_rows) == 1 and 'data-effort="max"' in max_rows[0]
    assert len(ultra_rows) == 1
    assert 'data-effort="max"' in ultra_rows[0]
    assert 'data-reasoning-mode="ultra"' in ultra_rows[0]
    assert 'data-effort="ultra"' not in html


def test_selector_posts_mode_separately_and_uses_server_availability() -> None:
    source = _source("static/ui.js")
    selector_start = source.index("if(e.target.closest('.reasoning-option'))")
    selector_end = source.index("// ── Session toolsets chip", selector_start)
    selector = source[selector_start:selector_end]

    assert "dataset.reasoningMode" in selector
    assert "ultra_available" in source
    assert "reasoning_mode" in source
    assert re.search(r"payload\.mode\s*=|mode\s*:\s*(?:mode|reasoningMode)", selector)
    assert "Object.assign({effort:effort}" in selector
    assert ".catch(function(err)" in selector
    assert "err.message" in selector


def test_reasoning_route_forwards_optional_mode_to_config_setter() -> None:
    source = _source("api/routes.py")
    post_start = source.index(
        'if parsed.path == "/api/reasoning":',
        source.index("def handle_post"),
    )
    post_end = source.index('return bad(handler, "reasoning: must supply', post_start)
    block = source[post_start:post_end]

    assert 'body.get("mode")' in block
    assert re.search(r"set_reasoning_effort\([\s\S]*?mode\s*=\s*mode", block)


def test_native_streaming_has_a_distinct_codex_ultra_control_plane() -> None:
    source = _source("api/streaming.py")

    assert "_canonical_reasoning_selection" in source
    assert "reasoning_mode" in source
    assert "codex_app_server" in source
    assert re.search(r"reasoning_config[\s\S]{0,500}effort[\"']?\s*:\s*[\"']ultra[\"']", source)


def test_native_ultra_requires_the_new_agent_constructor_contract() -> None:
    from api import streaming

    require_contract = getattr(streaming, "_require_codex_ultra_agent_contract", None)
    assert callable(require_contract)

    require_contract({"api_mode", "reasoning_config"}, "ultra")
    require_contract(set(), "")
    with pytest.raises(RuntimeError, match="Ultra|ultra"):
        require_contract({"reasoning_config"}, "ultra")
    with pytest.raises(RuntimeError, match="Ultra|ultra"):
        require_contract({"api_mode"}, "ultra")


def test_native_ultra_signature_mismatch_retires_the_prior_cached_agent() -> None:
    source = _source("api/streaming.py")

    pop_marker = "_signature_mismatch_entry = SESSION_AGENT_CACHE.pop(session_id, None)"
    close_marker = (
        "_close_cached_agent_entry_at_session_boundary(session_id, "
        "_signature_mismatch_entry)"
    )
    assert pop_marker in source
    assert close_marker in source
    assert source.index(pop_marker) < source.index(close_marker)


def test_native_ultra_credential_retries_preserve_effective_runtime_model() -> None:
    source = _source("api/streaming.py")
    runtime_start = source.index("_reasoning_runtime =")
    ultra_runtime_and_retries = source[runtime_start:]

    assert "_agent_kwargs['model'] = resolved_model" not in source
    assert "_heal_kwargs['model'] = resolved_model" not in source
    assert source.count("['model'] = _effective_runtime_model") >= 2
    # The initial request may legitimately use the provider-resolved model;
    # only credential self-heal/retry paths must retain the effective runtime
    # model selected by the Ultra control plane.
    retry_block = source[source.index("_attempt_credential_self_heal"):]
    assert retry_block.count("target_model=_effective_runtime_model") >= 2
    assert "_heal_kwargs['model'] = _effective_runtime_model" in ultra_runtime_and_retries


def test_native_ultra_never_replays_through_credential_self_heal() -> None:
    source = _source("api/streaming.py")

    assert source.count("_reasoning_mode != 'ultra'") >= 2


def test_cached_agent_teardown_closes_owned_codex_app_server(monkeypatch) -> None:
    from api import streaming

    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", lambda *a, **k: True)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda _sid: False)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", lambda _sid: None)
    monkeypatch.setattr(streaming, "_lifecycle_discard_session", lambda _sid: True)
    codex_session = MagicMock()
    agent = SimpleNamespace(
        _codex_session=codex_session,
        _session_db=None,
        _session_messages=[],
    )

    assert streaming._close_evicted_agent_at_session_boundary("ultra-cache", agent) is True
    codex_session.close.assert_called_once_with()
    assert agent._codex_session is None


@pytest.mark.parametrize(
    "extra_body",
    [
        {"reasoning": {"effort": "ultra"}},
        {"reasoning.effort": "ULTRA"},
        {"reasoning_effort": "ultra"},
    ],
)
def test_main_model_extra_body_cannot_override_reasoning_with_ultra(extra_body: dict) -> None:
    cfg = {
        "model": {
            "default": "gpt-5.6-sol",
            "provider": "openai-codex",
            "extra_body": extra_body,
        }
    }

    with pytest.raises(ValueError, match="Ultra|ultra|reasoning"):
        config._main_model_request_overrides(cfg)

    with pytest.raises(ValueError, match="Ultra|ultra|reasoning"):
        config._apply_advanced_model_options({}, {"extra_body": extra_body})


def test_native_ultra_runtime_uses_codex_app_server_and_control_effort(
    isolated_ultra_state: dict[str, Path],
) -> None:
    from api import streaming

    resolve_runtime = getattr(streaming, "_reasoning_runtime_for_turn", None)
    assert callable(resolve_runtime)

    runtime = resolve_runtime(
        {"agent": {"reasoning_effort": "max", "reasoning_mode": "ultra"}},
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        resolved_api_mode="codex_responses",
    )

    assert runtime["api_mode"] == "codex_app_server"
    assert runtime["model"] == "gpt-5.6-sol"
    assert runtime["reasoning_mode"] == "ultra"
    assert runtime["reasoning_config"] == {"enabled": True, "effort": "ultra"}


def test_native_ultra_canonicalizes_the_hermes_sol_alias_for_codex(
    isolated_ultra_state: dict[str, Path],
) -> None:
    from api import streaming

    runtime = streaming._reasoning_runtime_for_turn(
        {"agent": {"reasoning_effort": "max", "reasoning_mode": "ultra"}},
        model_id="gpt-5.6",
        provider_id="openai-codex",
        resolved_api_mode="codex_responses",
    )

    assert runtime["model"] == "gpt-5.6-sol"
    assert runtime["api_mode"] == "codex_app_server"


def test_native_max_runtime_stays_on_resolved_transport(
    isolated_ultra_state: dict[str, Path],
) -> None:
    from api import streaming

    runtime = streaming._reasoning_runtime_for_turn(
        {"agent": {"reasoning_effort": "max"}},
        model_id="gpt-5.6-sol",
        provider_id="openai-codex",
        resolved_api_mode="codex_responses",
    )

    assert runtime["api_mode"] == "codex_responses"
    assert runtime["reasoning_mode"] == ""
    assert runtime["reasoning_config"] == {"enabled": True, "effort": "max"}


def test_stale_ultra_mode_is_inert_after_switching_away_from_sol(
    isolated_ultra_state: dict[str, Path],
) -> None:
    from api import streaming

    runtime = streaming._reasoning_runtime_for_turn(
        {"agent": {"reasoning_effort": "max", "reasoning_mode": "ultra"}},
        model_id="gpt-5.5",
        provider_id="openai-codex",
        resolved_api_mode="codex_responses",
    )

    assert runtime["api_mode"] == "codex_responses"
    assert runtime["reasoning_mode"] == ""
    assert runtime["reasoning_config"] != {"enabled": True, "effort": "ultra"}


def test_native_ultra_fails_closed_when_codex_catalog_stops_advertising_it(
    isolated_ultra_state: dict[str, Path],
) -> None:
    from api import streaming

    _write_codex_model_cache(isolated_ultra_state["codex_home"], ["max"])
    with pytest.raises(RuntimeError, match="Ultra|ultra"):
        streaming._reasoning_runtime_for_turn(
            {"agent": {"reasoning_effort": "max", "reasoning_mode": "ultra"}},
            model_id="gpt-5.6-sol",
            provider_id="openai-codex",
            resolved_api_mode="codex_responses",
        )


def test_gateway_legacy_ultra_never_reaches_a_request_effort() -> None:
    from api.gateway_chat import _gateway_reasoning_effort_for_request

    result = _gateway_reasoning_effort_for_request(
        {"agent": {"reasoning_effort": "ultra"}},
        model="gpt-5.6-sol",
        model_provider="openai-codex",
    )

    assert result == "max"


def test_gateway_ultra_fails_closed_before_opening_either_request_path() -> None:
    source = _source("api/gateway_chat.py")
    worker_start = source.index("def _run_gateway_chat_streaming(")
    worker = source[worker_start:]
    first_urlopen = worker.index("urllib.request.urlopen")
    pre_request = worker[:first_urlopen]

    assert "reasoning_mode" in pre_request
    assert re.search(r"Ultra[\s\S]{0,240}native WebUI|native WebUI[\s\S]{0,240}Ultra", pre_request)
    assert "select Max" in pre_request


def test_ephemeral_turn_closes_its_uncached_agent_at_stream_teardown() -> None:
    source = _source("api/streaming.py")
    worker_start = source.index("def _run_agent_streaming(")
    worker_end = source.index("\n# ============================================================", worker_start)
    worker = source[worker_start:worker_end]

    assert "if ephemeral and agent is not None:" in worker
    assert "agent.close()" in worker
