"""Regression test for #5567 — cross-profile HERMES_HOME race at the config reader.

Root cause: `profile_env_for_background_worker` mirrors the profile's HERMES_HOME
into the process-global `os.environ`, and the worker body runs outside the setup
lock. A concurrent cross-profile worker can clobber `os.environ["HERMES_HOME"]`
mid-body, so the agent config reader (`hermes_cli.config.get_config_path` /
`load_config`, which read `get_hermes_home()`) resolves the WRONG profile's
config — intermittent turn-init failures referencing another profile's provider.

Fix (#5567): when hermes-agent >= v0.18.0 exposes the context-local home
override (`hermes_constants.set_hermes_home_override`), the worker scope installs
it so `get_hermes_home()` resolves THIS task's profile home from task-local state,
immune to the process-global clobber — without serializing workers.

Per #2321's acceptance criteria, this exercises the REAL
`hermes_cli.config.load_config()` against a non-default profile with an
intentional mid-body `os.environ` clobber and NO mocking of the production reader.

Degrades gracefully on agents without the override (skips with a clear reason).
"""
import os
import queue
import textwrap
from pathlib import Path

import pytest

# The production reader — imported unmocked, exactly as #2321 requires. Skip the
# whole module if the agent isn't importable in this environment.
config_mod = pytest.importorskip("hermes_cli.config")
hermes_constants = pytest.importorskip("hermes_constants")

HAS_OVERRIDE = hasattr(hermes_constants, "set_hermes_home_override") and hasattr(
    hermes_constants, "get_hermes_home"
)

from api import profiles as profiles_api  # noqa: E402
from api import streaming as streaming_api  # noqa: E402


def _seed_profile_home(base: Path, name: str, provider: str, model: str) -> Path:
    home = base / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            model:
              default: {model}
            provider: {provider}
            """
        ),
        encoding="utf-8",
    )
    return home


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="hermes-agent < v0.18.0: no set_hermes_home_override; WebUI degrades to the os.environ mirror",
)
def test_load_config_resolves_worker_profile_despite_env_clobber(tmp_path, monkeypatch):
    """The crux (#2321 criterion): inside profile_env_for_background_worker(A),
    a concurrent clobber of os.environ['HERMES_HOME']=B must NOT make the real
    load_config() read B — the context-local override pins A."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    home_b = _seed_profile_home(tmp_path, "beta", provider="ollama", model="llama-y")

    # The CM's INPUT (which profile home to scope to) — this is not the reader
    # under test; the reader is the real hermes_cli.config below.
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    # Establish a benign starting env, then simulate the race: while the worker
    # body for profile A runs, a sibling profile-B worker clobbers the global.
    monkeypatch.setenv("HERMES_HOME", str(home_a))

    # Clear any cached config so load_config actually hits the resolver.
    for fn in ("reload_config", "_reset_config_cache", "clear_config_cache"):
        if hasattr(config_mod, fn):
            try:
                getattr(config_mod, fn)()
            except Exception:
                pass

    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        # The clobber: another profile's worker overwrites the process global.
        os.environ["HERMES_HOME"] = str(home_b)
        # get_config_path must resolve profile A via the context-local override,
        # NOT profile B from the clobbered os.environ.
        resolved = config_mod.get_config_path()
        assert resolved == home_a / "config.yaml", (
            f"config path must resolve profile A ({home_a}) via the context-local "
            f"override despite os.environ clobbered to B ({home_b}); got {resolved}"
        )
        # And the real load_config() must read A's model, not B's.
        cfg = config_mod.load_config()
        model_default = (cfg.get("model") or {}).get("default")
        assert model_default == "claude-x", (
            f"load_config must read profile A's model 'claude-x' despite the "
            f"HERMES_HOME clobber to B; got {model_default!r} (B is 'llama-y')"
        )


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="requires the v0.18.0 override to assert the override is cleared on exit",
)
def test_override_is_cleared_after_worker_exits(tmp_path, monkeypatch):
    """The context-local override must not leak past the worker scope."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    assert hermes_constants.get_hermes_home_override() is None
    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        assert hermes_constants.get_hermes_home_override() == str(home_a)
    # Cleared on exit — no leak into subsequent tasks on this context.
    assert hermes_constants.get_hermes_home_override() is None


def test_graceful_degradation_resolver_is_optional():
    """On an agent WITHOUT the override, the resolver returns None and the CM
    falls back to the pre-existing os.environ mirror — never raises. We assert
    the resolver is import-safe and boolean-clean regardless of agent version."""
    mod = profiles_api._resolve_hermes_home_override()
    if HAS_OVERRIDE:
        assert mod is not None and hasattr(mod, "set_hermes_home_override")
    else:
        assert mod is None  # older agent: graceful no-op, os.environ mirror stays


class _StreamingSession:
    """Small session double for the real streaming worker boundary."""

    def __init__(self, workspace: Path):
        self.session_id = "issue5567-streaming-home"
        self.title = "Profile home override"
        self.workspace = str(workspace)
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.profile = "alpha"
        self.personality = None
        self.messages = []
        self.context_messages = []
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.llm_title_generated = True

    def save(self, *args, **kwargs):
        return None


def _exercise_streaming_home_override(
    monkeypatch,
    tmp_path,
    *,
    failure_stage=None,
    override_available=True,
):
    """Run the real worker with a fake Agent and return its home observations."""
    from api import oauth

    profile_home = tmp_path / "profiles" / "alpha"
    clobber_home = tmp_path / "profiles" / "beta"
    outer_home = tmp_path / "outer"
    for home in (profile_home, clobber_home, outer_home):
        home.mkdir(parents=True, exist_ok=True)

    session = _StreamingSession(tmp_path)
    stream_id = f"issue5567-{failure_stage or 'success'}"
    session.active_stream_id = stream_id
    observations = []
    token_events = []

    real_set_override = hermes_constants.set_hermes_home_override
    real_reset_override = hermes_constants.reset_hermes_home_override

    def tracking_set_override(path):
        token = real_set_override(path)
        token_events.append(("set", str(path), token))
        return token

    def tracking_reset_override(token):
        token_events.append(("reset", token))
        return real_reset_override(token)

    monkeypatch.setattr(
        hermes_constants,
        "set_hermes_home_override",
        tracking_set_override,
    )
    monkeypatch.setattr(
        hermes_constants,
        "reset_hermes_home_override",
        tracking_reset_override,
    )
    if not override_available:
        monkeypatch.setattr(
            profiles_api,
            "_resolve_hermes_home_override",
            lambda: None,
        )

    class ObservingAgent:
        def __init__(self, **kwargs):
            observations.append(
                (
                    "construct",
                    hermes_constants.get_hermes_home(),
                    hermes_constants.get_hermes_home_override(),
                )
            )
            if failure_stage == "construct":
                raise RuntimeError("synthetic constructor failure")
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            # Model a sibling profile worker overwriting the process-global
            # fallback while this turn is already running.
            os.environ["HERMES_HOME"] = str(clobber_home)
            observations.append(
                (
                    "run",
                    hermes_constants.get_hermes_home(),
                    hermes_constants.get_hermes_home_override(),
                )
            )
            if failure_stage == "conversation":
                raise RuntimeError("synthetic conversation failure")
            return {
                "completed": True,
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming_api, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming_api, "_get_ai_agent", lambda: ObservingAgent)
    monkeypatch.setattr(
        streaming_api,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming_api, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda _profile: profile_home)
    monkeypatch.setattr(profiles_api, "get_profile_runtime_env", lambda _home: {})
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _resolver, requested=None, **_kwargs: {
            "provider": requested or "test-provider",
            "api_key": "synthetic-key",
            "base_url": None,
        },
    )
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("api.config.load_settings", lambda: {})

    streaming_api.STREAMS[stream_id] = queue.Queue()
    monkeypatch.setenv("HERMES_HOME", str(clobber_home))

    outer_token = None
    if override_available:
        outer_token = hermes_constants.set_hermes_home_override(outer_home)
    try:
        streaming_api._run_agent_streaming(
            session_id=session.session_id,
            msg_text="hello",
            model="test-model",
            model_provider="test-provider",
            workspace=str(tmp_path),
            stream_id=stream_id,
            ephemeral=True,
        )
        if override_available:
            assert hermes_constants.get_hermes_home_override() == str(outer_home)
            assert hermes_constants.get_hermes_home() == outer_home
        else:
            assert hermes_constants.get_hermes_home_override() is None
    finally:
        if outer_token is not None:
            hermes_constants.reset_hermes_home_override(outer_token)
        streaming_api.STREAMS.pop(stream_id, None)

    return profile_home, observations, token_events


@pytest.mark.skipif(not HAS_OVERRIDE, reason="requires context-local Hermes home support")
def test_streaming_binds_resolved_profile_home_through_agent_run(tmp_path, monkeypatch):
    profile_home, observations, token_events = _exercise_streaming_home_override(
        monkeypatch,
        tmp_path,
    )

    assert observations == [
        ("construct", profile_home, str(profile_home)),
        ("run", profile_home, str(profile_home)),
    ]
    assert [event[:2] for event in token_events] == [
        ("set", str(tmp_path / "outer")),
        ("set", str(profile_home)),
        ("reset", token_events[2][1]),
        ("reset", token_events[3][1]),
    ]
    assert token_events[1][2] is token_events[2][1]
    assert token_events[0][2] is token_events[3][1]


@pytest.mark.skipif(not HAS_OVERRIDE, reason="requires context-local Hermes home support")
@pytest.mark.parametrize("failure_stage", ["construct", "conversation"])
def test_streaming_restores_exact_outer_home_override_on_failure(
    tmp_path, monkeypatch, failure_stage
):
    profile_home, observations, token_events = _exercise_streaming_home_override(
        monkeypatch,
        tmp_path,
        failure_stage=failure_stage,
    )

    assert observations[0] == ("construct", profile_home, str(profile_home))
    if failure_stage == "conversation":
        assert observations[1] == ("run", profile_home, str(profile_home))
    assert token_events[1][2] is token_events[2][1]
    assert token_events[0][2] is token_events[3][1]


@pytest.mark.skipif(not HAS_OVERRIDE, reason="test harness needs the current override API")
def test_streaming_degrades_to_noop_when_agent_has_no_home_override(
    tmp_path,
    monkeypatch,
):
    profile_home, observations, token_events = _exercise_streaming_home_override(
        monkeypatch,
        tmp_path,
        override_available=False,
    )

    assert observations[0][:2] == ("construct", profile_home)
    assert token_events == []
