import ast
import inspect
import os
import re
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def test_profile_runtime_env_includes_terminal_config_and_dotenv(tmp_path):
    from api.profiles import get_profile_runtime_env

    home = tmp_path / "profiles" / "server-ops"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "terminal": {
                    "backend": "ssh",
                    "cwd": "/home/dso2ng/repos",
                    "timeout": 180,
                    "ssh_host": "pollux",
                    "ssh_user": "dso2ng",
                    "persistent_shell": True,
                    "lifetime_seconds": 300,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "TERMINAL_TIMEOUT=60\n"
        "TERMINAL_SSH_HOST=pollux-from-env\n"
        "HERMES_MAX_ITERATIONS=90\n",
        encoding="utf-8",
    )

    env = get_profile_runtime_env(home)

    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_CWD"] == "/home/dso2ng/repos"
    assert env["TERMINAL_SSH_USER"] == "dso2ng"
    assert env["TERMINAL_PERSISTENT_SHELL"] == "true"
    assert env["TERMINAL_LIFETIME_SECONDS"] == "300"
    # .env remains the final override source, matching CLI/profile behaviour.
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["TERMINAL_SSH_HOST"] == "pollux-from-env"
    assert env["HERMES_MAX_ITERATIONS"] == "90"


def test_streaming_foreground_never_mirrors_profile_runtime_env_to_process():
    from api.streaming import _run_agent_streaming

    src = textwrap.dedent(inspect.getsource(_run_agent_streaming))

    assert "get_profile_runtime_env" in src
    assert "_profile_runtime_env" in src
    assert "_set_streaming_runtime_env(" in src
    assert "_set_streaming_session_id_mirror_suppression()" in src
    assert "old_profile_env" not in src
    assert "os.environ.update(_safe_profile_runtime_env)" not in src
    assert "os.environ.update(_profile_runtime_env)" not in src
    tree = ast.parse(src)
    mutations = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = list(getattr(node, "targets", ())) or [getattr(node, "target", None)]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "os"
                and target.value.attr == "environ"
            ):
                mutations.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"update", "pop", "setdefault", "clear"}
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.func.value.attr == "environ"
        ):
            mutations.append(node.lineno)
    assert mutations == []


def test_filter_runtime_env_for_gateway_parity_blocks_shell_identity_vars():
    from api.profiles import filter_runtime_env_for_gateway_parity

    env = {
        "HOME": "/tmp/fake-home",
        "PATH": "/tmp/fake-bin:/usr/bin",
        "PWD": "/tmp/fake-pwd",
        "SHELL": "/bin/zsh",
        "OPENAI_API_KEY": "test-key",
        "TERMINAL_ENV": "ssh",
        "TERMINAL_CWD": "/workspace",
    }

    filtered = filter_runtime_env_for_gateway_parity(env)

    assert "HOME" not in filtered
    assert "PATH" not in filtered
    assert "PWD" not in filtered
    assert "SHELL" not in filtered
    assert filtered["OPENAI_API_KEY"] == "test-key"
    assert filtered["TERMINAL_ENV"] == "ssh"
    assert filtered["TERMINAL_CWD"] == "/workspace"


def test_profile_background_worker_uses_gateway_parity_runtime_env_filter():
    src = Path("api/profiles.py").read_text(encoding="utf-8")

    assert "filter_runtime_env_for_gateway_parity" in src
    assert "safe_runtime_env" in src
    assert "_set_thread_env(**thread_env)" in src
    assert "_authoritative_profile_secret_env(thread_env)" in src
    assert "os.environ.update(safe_runtime_env)" not in src
    assert "os.environ.update(runtime_env)" not in src


def test_streaming_thread_env_allows_profile_terminal_cwd_override():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _build_agent_thread_env" in src
    assert "_thread_env = _build_agent_thread_env(" in src
    assert "_set_thread_env(**_thread_env)" in src
    assert "_set_thread_env(\n            **_profile_runtime_env,\n            TERMINAL_CWD" not in src

    match = re.search(
        r"(def _build_agent_thread_env\(.*?\n)(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "_build_agent_thread_env not found in api/streaming.py"
    ns: dict = {}
    exec(compile(match.group(1), "<streaming_extract>", "exec"), ns)

    env = ns["_build_agent_thread_env"](
        {
            "TERMINAL_CWD": "/profile/config/cwd",
            "HERMES_EXEC_ASK": "0",
            "HERMES_SESSION_KEY": "old-session",
            "HERMES_SESSION_ID": "old-session",
            "HERMES_SESSION_PLATFORM": "cli",
            "HERMES_HOME": "/old/profile/home",
            "TERMINAL_ENV": "ssh",
        },
        "/active/workspace",
        "active-session",
        "/active/profile/home",
    )

    assert env["TERMINAL_CWD"] == "/active/workspace"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "active-session"
    assert env["HERMES_SESSION_ID"] == "active-session"
    assert env["HERMES_SESSION_PLATFORM"] == "webui"
    assert env["HERMES_HOME"] == "/active/profile/home"
    assert env["TERMINAL_ENV"] == "ssh"


def test_streaming_thread_env_tombstones_missing_profile_secrets():
    from api.streaming import _build_agent_thread_env

    env = _build_agent_thread_env(
        {"NOVITA_API_KEY": "profile-key"},
        "/active/workspace",
        "active-session",
        "/active/profile/home",
        missing_secret_names={"NOVITA_API_KEY", "OPENROUTER_API_KEY"},
    )

    assert env["NOVITA_API_KEY"] == "profile-key"
    assert env["OPENROUTER_API_KEY"] == ""


def test_streaming_secret_scope_masks_process_default_key(monkeypatch):
    from agent import secret_scope
    from api.streaming import (
        _reset_streaming_secret_scope,
        _set_streaming_secret_scope,
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "process-default-key")
    scope = _set_streaming_secret_scope({"OPENROUTER_API_KEY": ""})
    try:
        assert secret_scope.get_secret("OPENROUTER_API_KEY") == ""
    finally:
        _reset_streaming_secret_scope(scope)


def test_streaming_authoritative_secret_scope_masks_unknown_process_key(monkeypatch):
    from agent import secret_scope
    from api.streaming import (
        _reset_streaming_secret_scope,
        _set_streaming_secret_scope,
    )

    scope = _set_streaming_secret_scope({}, authoritative=True)
    try:
        # The process mirror may gain another profile's key after this scope is
        # installed. Authority must be open-ended, not a snapshot of env names.
        monkeypatch.setenv("PLUGIN_PRIVATE_TOKEN", "process-plugin-secret")
        assert secret_scope.get_secret("PLUGIN_PRIVATE_TOKEN") == ""
    finally:
        _reset_streaming_secret_scope(scope)


def test_streaming_runtime_scope_routes_terminal_without_process_mutation(monkeypatch):
    from agent.runtime_env import get_runtime_env
    from api.streaming import (
        _reset_streaming_runtime_env,
        _set_streaming_runtime_env,
    )

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "process.invalid")
    scope = _set_streaming_runtime_env(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "profile.invalid",
            "TERMINAL_SSH_USER": "profile-user",
            "TERMINAL_CWD": "~",
        },
        authoritative=True,
    )
    try:
        assert get_runtime_env("TERMINAL_SSH_HOST") == "profile.invalid"
        assert get_runtime_env("TERMINAL_SSH_USER") == "profile-user"
        assert os.environ["TERMINAL_SSH_HOST"] == "process.invalid"
    finally:
        _reset_streaming_runtime_env(scope)

    assert get_runtime_env("TERMINAL_SSH_HOST") == "process.invalid"


def test_streaming_session_rotation_suppresses_process_mirror(monkeypatch):
    from gateway.session_context import get_session_env, set_current_session_id
    from api.streaming import (
        _reset_streaming_session_id_mirror_suppression,
        _set_streaming_session_id_mirror_suppression,
    )

    monkeypatch.setenv("HERMES_SESSION_ID", "process-baseline")
    scope = _set_streaming_session_id_mirror_suppression()
    try:
        set_current_session_id("webui-rotated")
        assert get_session_env("HERMES_SESSION_ID") == "webui-rotated"
        assert os.environ["HERMES_SESSION_ID"] == "process-baseline"
    finally:
        _reset_streaming_session_id_mirror_suppression(scope)

    set_current_session_id("legacy-rotated")
    assert os.environ["HERMES_SESSION_ID"] == "legacy-rotated"


def test_named_background_worker_binds_agent_runtime_env(monkeypatch, tmp_path):
    from agent.runtime_env import get_runtime_env
    from api import profiles

    root = tmp_path / "hermes-root"
    child = root / "profiles" / "worker"
    child.mkdir(parents=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "terminal": {
                    "backend": "ssh",
                    "cwd": "~",
                    "ssh_host": "profile.invalid",
                    "ssh_user": "worker",
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (child / "config.yaml").write_text(
        yaml.safe_dump(
            {"_profile": {"inherits": "default", "version": 1}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "process.invalid")

    with profiles.profile_env_for_background_worker(
        SimpleNamespace(profile="worker"),
        patch_skill_modules=False,
    ):
        assert get_runtime_env("TERMINAL_ENV") == "ssh"
        assert get_runtime_env("TERMINAL_SSH_HOST") == "profile.invalid"
        assert os.environ["TERMINAL_ENV"] == "local"
        assert os.environ["TERMINAL_SSH_HOST"] == "process.invalid"

    assert get_runtime_env("TERMINAL_ENV") == "local"


@pytest.mark.parametrize("profile", [None, ""])
def test_legacy_empty_profile_keeps_root_process_credentials(monkeypatch, profile):
    from agent import secret_scope
    from api.profiles import _is_root_profile
    from api.streaming import (
        _reset_streaming_secret_scope,
        _set_streaming_secret_scope,
    )

    monkeypatch.setenv("ROOT_ONLY_PLUGIN_TOKEN", "root-process-secret")
    assert _is_root_profile(profile) is True

    scope = _set_streaming_secret_scope(
        {},
        authoritative=not _is_root_profile(profile),
    )
    try:
        assert secret_scope.get_secret("ROOT_ONLY_PLUGIN_TOKEN") == "root-process-secret"
    finally:
        _reset_streaming_secret_scope(scope)


def test_turn_identity_binds_full_webui_session_and_cwd(tmp_path):
    from agent import runtime_cwd
    from gateway import session_context
    from api.streaming import _reset_turn_session_identity, _set_turn_session_identity

    tracked_vars = (
        "_SESSION_PLATFORM",
        "_SESSION_SOURCE",
        "_SESSION_CHAT_ID",
        "_SESSION_KEY",
        "_SESSION_ID",
        "_SESSION_UI_SESSION_ID",
        "_SESSION_PROFILE",
        "_CRON_SESSION",
        "_SESSION_ASYNC_DELIVERY",
    )
    before = {
        name: getattr(session_context, name).get()
        for name in tracked_vars
    }
    before_cwd = runtime_cwd._SESSION_CWD.get()

    tokens = _set_turn_session_identity(
        "session-a",
        profile="worker",
        hermes_home=str(tmp_path / "profile"),
        cwd=str(tmp_path),
        full_context=True,
    )
    try:
        assert session_context.get_session_env("HERMES_SESSION_PLATFORM") == "webui"
        assert session_context.get_session_env("HERMES_SESSION_CHAT_ID") == "session-a"
        assert session_context.get_session_env("HERMES_SESSION_KEY") == "session-a"
        assert session_context.get_session_env("HERMES_SESSION_ID") == "session-a"
        assert session_context.get_session_env("HERMES_UI_SESSION_ID") == "session-a"
        assert session_context.get_session_env("HERMES_SESSION_PROFILE") == "worker"
        assert runtime_cwd.get_session_cwd_override() == str(tmp_path)
    finally:
        _reset_turn_session_identity(tokens)

    assert {
        name: getattr(session_context, name).get()
        for name in tracked_vars
    } == before
    assert runtime_cwd._SESSION_CWD.get() == before_cwd


def test_turn_identity_full_context_fails_closed_when_cwd_snapshot_fails(monkeypatch):
    from agent import runtime_cwd
    from gateway import session_context
    from api.streaming import _set_turn_session_identity

    before = {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    }

    def _fail_cwd_snapshot(*_args, **_kwargs):
        raise RuntimeError("synthetic cwd snapshot failure")

    monkeypatch.setattr(runtime_cwd, "set_session_cwd", _fail_cwd_snapshot)

    with pytest.raises(RuntimeError, match="cwd snapshot"):
        _set_turn_session_identity(
            "session-fail",
            cwd="/synthetic/workspace",
            full_context=True,
        )

    assert {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    } == before


def test_turn_identity_full_context_restores_partial_setter_failure(monkeypatch):
    from gateway import session_context
    from api.streaming import _set_turn_session_identity

    before = {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    }

    def _partially_set_then_fail(**_kwargs):
        session_context._SESSION_PLATFORM.set("leaked-platform")
        session_context._SESSION_KEY.set("leaked-session")
        raise RuntimeError("synthetic partial session bind")

    monkeypatch.setattr(
        session_context,
        "set_session_vars",
        _partially_set_then_fail,
    )

    with pytest.raises(RuntimeError, match="full session-context"):
        _set_turn_session_identity("session-fail", full_context=True)

    assert {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    } == before


def test_nested_full_turn_context_restores_outer_then_original(tmp_path):
    import hermes_constants
    from agent import runtime_cwd
    from gateway import session_context
    from tools import approval
    from api.streaming import _reset_turn_session_identity, _set_turn_session_identity

    session_vars = list(session_context._VAR_MAP.values()) + [
        session_context._SESSION_ASYNC_DELIVERY,
        session_context._SESSION_ASYNC_DELIVERY_VERSION,
        session_context._SESSION_HERMES_HOME,
    ]

    def _snapshot():
        return {
            "session": tuple(var.get() for var in session_vars),
            "cwd": runtime_cwd._SESSION_CWD.get(),
            "home": hermes_constants._HERMES_HOME_OVERRIDE.get(),
            "approval": approval._approval_session_key.get(),
        }

    original = _snapshot()
    outer = _set_turn_session_identity(
        "outer-session",
        profile="outer",
        hermes_home=str(tmp_path / "outer"),
        cwd=str(tmp_path / "outer-workspace"),
        full_context=True,
    )
    try:
        outer_snapshot = _snapshot()
        assert session_context.get_session_env("HERMES_SESSION_KEY") == "outer-session"
        assert session_context.get_session_env("HERMES_SESSION_PROFILE") == "outer"
        assert runtime_cwd.get_session_cwd_override() == str(tmp_path / "outer-workspace")

        inner = _set_turn_session_identity(
            "inner-session",
            profile="inner",
            hermes_home=str(tmp_path / "inner"),
            cwd=str(tmp_path / "inner-workspace"),
            full_context=True,
        )
        try:
            assert session_context.get_session_env("HERMES_SESSION_KEY") == "inner-session"
            assert session_context.get_session_env("HERMES_SESSION_PROFILE") == "inner"
            assert runtime_cwd.get_session_cwd_override() == str(
                tmp_path / "inner-workspace"
            )
        finally:
            _reset_turn_session_identity(inner)

        assert _snapshot() == outer_snapshot
    finally:
        _reset_turn_session_identity(outer)

    assert _snapshot() == original


def test_turn_identity_reset_continues_after_home_reset_failure(monkeypatch, tmp_path):
    import hermes_constants
    from gateway import session_context
    from api.streaming import _reset_turn_session_identity, _set_turn_session_identity

    before = {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    }
    before_home_override = hermes_constants._HERMES_HOME_OVERRIDE.get()
    real_reset_home = hermes_constants.reset_hermes_home_override
    tokens = _set_turn_session_identity(
        "session-home-reset",
        hermes_home=str(tmp_path),
    )

    def _fail_home_reset(_token):
        raise RuntimeError("synthetic home reset failure")

    monkeypatch.setattr(
        hermes_constants,
        "reset_hermes_home_override",
        _fail_home_reset,
    )
    try:
        _reset_turn_session_identity(tokens)
    finally:
        # Ensure a pre-fix failure cannot pollute later tests while preserving
        # the assertion that product cleanup itself must continue.
        monkeypatch.setattr(
            hermes_constants,
            "reset_hermes_home_override",
            real_reset_home,
        )
        home_token = tokens.get("hermes_home_override")
        if home_token is not None:
            try:
                real_reset_home(home_token)
            except Exception:
                pass
        delivery = tokens.get("delivery_context")
        if delivery is not None:
            try:
                session_context.reset_delivery_context(delivery)
            except Exception:
                pass

    assert {
        name: var.get()
        for name, var in session_context._VAR_MAP.items()
    } == before
    assert hermes_constants._HERMES_HOME_OVERRIDE.get() is before_home_override


def test_streaming_turn_cleanup_releases_later_owners_after_reset_failure(monkeypatch):
    from api import streaming

    calls = []

    def _fail_identity(_tokens):
        calls.append("identity")
        raise RuntimeError("synthetic identity reset failure")

    def _reset_home(*_ctx):
        calls.append("home")

    monkeypatch.setattr(streaming, "_reset_turn_session_identity", _fail_identity)
    monkeypatch.setattr(
        streaming,
        "_reset_streaming_hermes_home_override",
        _reset_home,
    )

    streaming._reset_streaming_turn_contexts(
        object(),
        {"identity": "token"},
        ("module", "token", True),
    )

    assert calls == ["identity", "home"]


def test_concurrent_turn_contexts_keep_profile_identity_and_secrets(tmp_path, monkeypatch):
    from agent import secret_scope
    from agent.runtime_cwd import get_session_cwd_override
    from agent.runtime_env import get_runtime_env
    from gateway.session_context import get_session_env, set_current_session_id
    from hermes_constants import get_hermes_home
    from api.streaming import (
        _reset_streaming_secret_scope,
        _reset_streaming_runtime_env,
        _reset_streaming_session_id_mirror_suppression,
        _reset_turn_session_identity,
        _set_streaming_secret_scope,
        _set_streaming_runtime_env,
        _set_streaming_session_id_mirror_suppression,
        _set_turn_session_identity,
    )

    monkeypatch.setenv("ROOT_ONLY_SECRET", "root-value")
    monkeypatch.setenv("TERMINAL_ENV", "process")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "process.invalid")
    monkeypatch.setenv("HERMES_SESSION_ID", "process-session")
    barrier = threading.Barrier(2)
    observed = {}

    def _turn(label: str):
        home = tmp_path / label
        workspace = home / "workspace"
        workspace.mkdir(parents=True)
        scope = _set_streaming_secret_scope(
            {
                "PROFILE_SECRET": f"{label}-secret",
                "ROOT_ONLY_SECRET": "",
            }
        )
        runtime_scope = _set_streaming_runtime_env(
            {
                "TERMINAL_ENV": "ssh",
                "TERMINAL_SSH_HOST": f"{label}.invalid",
            },
            authoritative=True,
        )
        mirror_scope = _set_streaming_session_id_mirror_suppression()
        tokens = _set_turn_session_identity(
            f"session-{label}",
            profile=label,
            hermes_home=str(home),
            cwd=str(workspace),
            full_context=True,
        )
        try:
            set_current_session_id(f"session-{label}-rotated")
            barrier.wait()
            observed[label] = {
                "profile_secret": secret_scope.get_secret("PROFILE_SECRET"),
                "root_secret": secret_scope.get_secret("ROOT_ONLY_SECRET"),
                "home": str(get_hermes_home()),
                "cwd": get_session_cwd_override(),
                "platform": get_session_env("HERMES_SESSION_PLATFORM"),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
                "session_key": get_session_env("HERMES_SESSION_KEY"),
                "session_id": get_session_env("HERMES_SESSION_ID"),
                "terminal_env": get_runtime_env("TERMINAL_ENV"),
                "ssh_host": get_runtime_env("TERMINAL_SSH_HOST"),
                "process_terminal_env": os.environ["TERMINAL_ENV"],
                "process_ssh_host": os.environ["TERMINAL_SSH_HOST"],
                "process_session_id": os.environ["HERMES_SESSION_ID"],
            }
        finally:
            _reset_turn_session_identity(tokens)
            _reset_streaming_session_id_mirror_suppression(mirror_scope)
            _reset_streaming_runtime_env(runtime_scope)
            _reset_streaming_secret_scope(scope)

    threads = [threading.Thread(target=_turn, args=(label,)) for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    for label in ("a", "b"):
        assert observed[label] == {
            "profile_secret": f"{label}-secret",
            "root_secret": "",
            "home": str(tmp_path / label),
            "cwd": str(tmp_path / label / "workspace"),
            "platform": "webui",
            "chat_id": f"session-{label}",
            "session_key": f"session-{label}",
            "session_id": f"session-{label}-rotated",
            "terminal_env": "ssh",
            "ssh_host": f"{label}.invalid",
            "process_terminal_env": "process",
            "process_ssh_host": "process.invalid",
            "process_session_id": "process-session",
        }
    assert os.environ["TERMINAL_ENV"] == "process"
    assert os.environ["TERMINAL_SSH_HOST"] == "process.invalid"
    assert os.environ["HERMES_SESSION_ID"] == "process-session"
