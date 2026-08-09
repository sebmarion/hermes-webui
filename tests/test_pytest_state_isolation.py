"""
Regression tests for pytest-process state isolation.

Some tests import api.config/api.models during collection and directly write
sessions from the pytest process. conftest must publish the test state env vars
before those imports, not only for the server subprocess.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_api_config_uses_pytest_state_dir():
    import api.config as config
    from tests.conftest import TEST_STATE_DIR

    test_state_dir = TEST_STATE_DIR.resolve()
    production_state_dir = (Path.home() / ".hermes" / "webui").resolve()

    assert config.STATE_DIR == test_state_dir
    assert config.SESSION_DIR == test_state_dir / "sessions"
    assert config.STATE_DIR != production_state_dir
    assert production_state_dir not in config.SESSION_DIR.resolve().parents


def test_auto_state_dir_name_distinguishes_reused_pid_and_port_across_runs(
    monkeypatch, tmp_path
):
    import tests.conftest as conftest

    repo_root = tmp_path / "repo"
    port = 43210

    monkeypatch.setattr(conftest.os, "getpid", lambda: 111)
    first_name = conftest._auto_state_dir_name(
        repo_root, port=port, run_token="a" * 16
    )
    assert (
        conftest._auto_state_dir_name(
            repo_root, port=port, run_token="a" * 16
        )
        == first_name
    )
    assert first_name.endswith(f"-111-{'a' * 16}-43210")

    second_name = conftest._auto_state_dir_name(
        repo_root, port=port, run_token="b" * 16
    )
    assert second_name != first_name
    assert second_name.endswith(f"-111-{'b' * 16}-43210")


def test_state_dir_initialization_is_early_unique_and_then_ensure_only(
    tmp_path,
):
    import tests.conftest as conftest

    auto_state = tmp_path / "auto-state"
    conftest._initialize_test_state_dir(auto_state, pinned=False)

    assert auto_state.is_dir()
    assert (auto_state / "test-workspace").is_dir()

    auto_marker = auto_state / "async_delegations.json"
    auto_marker.write_text("{}", encoding="utf-8")
    auto_workspace = auto_state / "test-workspace"
    workspace_marker = auto_workspace / "import-initialized"
    workspace_marker.write_text("ready", encoding="utf-8")

    conftest._ensure_test_state_dir(auto_state)

    assert auto_marker.read_text(encoding="utf-8") == "{}"
    assert workspace_marker.read_text(encoding="utf-8") == "ready"

    collision_state = tmp_path / "auto-collision"
    collision_state.mkdir()
    with pytest.raises(FileExistsError):
        conftest._initialize_test_state_dir(collision_state, pinned=False)

    pinned_state = tmp_path / "pinned-state"
    pinned_state.mkdir()
    stale_marker = pinned_state / "stale.json"
    stale_marker.write_text("stale", encoding="utf-8")

    conftest._initialize_test_state_dir(pinned_state, pinned=True)

    assert pinned_state.is_dir()
    assert not stale_marker.exists()
    assert (pinned_state / "test-workspace").is_dir()


def test_pinned_state_dir_must_be_a_dedicated_test_root_child(tmp_path):
    import tests.conftest as conftest

    state_root = tmp_path / "hermes-webui-tests"
    eligible = state_root / "owned-run"

    conftest._validate_pinned_test_state_dir(eligible, state_root=state_root)
    conftest._validate_pinned_test_state_dir(
        Path("/tmp/hermes-webui-tests/documented-pin"),
        state_root=conftest._TEST_STATE_ROOT,
    )
    conftest._validate_pinned_test_state_dir(
        tmp_path / "hermes-r67-isolated-run",
        state_root=state_root,
    )

    with pytest.raises(RuntimeError):
        conftest._validate_pinned_test_state_dir(
            state_root, state_root=state_root
        )
    with pytest.raises(RuntimeError):
        conftest._validate_pinned_test_state_dir(
            tmp_path / "unrelated", state_root=state_root
        )


def test_exit_cleanup_is_owned_by_the_initializing_process(monkeypatch, tmp_path):
    import tests.conftest as conftest

    state = tmp_path / "owned-state"
    state.mkdir()
    owner_pid = 111

    monkeypatch.setattr(conftest.os, "getpid", lambda: 222)
    conftest._cleanup_owned_test_state_dir(state, owner_pid=owner_pid)
    assert state.is_dir()

    monkeypatch.setattr(conftest.os, "getpid", lambda: owner_pid)
    conftest._cleanup_owned_test_state_dir(state, owner_pid=owner_pid)
    assert not state.exists()


def test_preseeded_provenance_cannot_bypass_target_guard(tmp_path):
    import tests.conftest as conftest

    state_root_base = tmp_path / "safe-root"
    unsafe_state = tmp_path / "unrelated-inherited-state"
    unsafe_state.mkdir()
    sentinel = unsafe_state / "must-survive"
    sentinel.write_text("safe", encoding="utf-8")
    env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
        if key in os.environ
    }
    env.update(
        {
            "HERMES_WEBUI_AGENT_DIR": str(conftest.HERMES_AGENT),
            "HERMES_WEBUI_TEST_STATE_ROOT": str(state_root_base),
            "HERMES_WEBUI_TEST_STATE_DIR": str(unsafe_state),
            conftest._TEST_STATE_INITIALIZED_ENV: str(unsafe_state),
            "PYTHONPATH": os.pathsep.join(
                (str(conftest.REPO_ROOT), str(conftest.HERMES_AGENT))
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=conftest.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "REFUSING TO RUN" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_conftest_cleans_pinned_state_before_product_imports(tmp_path):
    import tests.conftest as conftest

    state_root_base = tmp_path / "state-root"
    pinned_state = (
        state_root_base / "hermes-webui-tests" / "subprocess-pinned-state"
    )
    pinned_state.mkdir(parents=True)
    (pinned_state / "stale-before-collection").write_text(
        "stale", encoding="utf-8"
    )
    script = """
import os
import subprocess
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
import tests.conftest as conftest

assert conftest.TEST_STATE_DIR == target
assert not (target / "stale-before-collection").exists()
marker = target / "owned-before-product-import"
marker.write_text("owned", encoding="utf-8")
inode_before = target.stat().st_ino

import api.routes
conftest._ensure_test_state_dir(target)

assert target.stat().st_ino == inode_before
assert marker.read_text(encoding="utf-8") == "owned"

child = subprocess.run(
    [
        sys.executable,
        "-c",
        "import tests.conftest as c; assert c.TEST_STATE_DIR.is_dir()",
    ],
    cwd=conftest.REPO_ROOT,
    env=os.environ.copy(),
    capture_output=True,
    text=True,
    timeout=30,
)
assert child.returncode == 0, child.stdout + child.stderr
assert target.stat().st_ino == inode_before
assert marker.read_text(encoding="utf-8") == "owned"
"""
    env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
        if key in os.environ
    }
    env.update(
        {
            "HERMES_WEBUI_AGENT_DIR": str(conftest.HERMES_AGENT),
            "HERMES_WEBUI_TEST_STATE_ROOT": str(state_root_base),
            "HERMES_WEBUI_TEST_STATE_DIR": str(pinned_state),
            "PYTHONPATH": os.pathsep.join(
                (str(conftest.REPO_ROOT), str(conftest.HERMES_AGENT))
            ),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(pinned_state)],
        cwd=conftest.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not pinned_state.exists()


def test_conftest_initializes_state_before_agent_module_probe(tmp_path):
    import tests.conftest as conftest

    fake_agent = tmp_path / "fake-agent"
    (fake_agent / "cron").mkdir(parents=True)
    (fake_agent / "tools").mkdir()
    (fake_agent / "run_agent.py").write_text("", encoding="utf-8")
    (fake_agent / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (fake_agent / "tools" / "__init__.py").write_text("", encoding="utf-8")
    probe = """
import os
from pathlib import Path

state = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])
assert state.is_dir()
assert (state / "test-workspace").is_dir()
assert not (state / "stale-before-probe").exists()
"""
    (fake_agent / "cron" / "jobs.py").write_text(probe, encoding="utf-8")
    (fake_agent / "tools" / "skills_tool.py").write_text(
        probe, encoding="utf-8"
    )

    state_root_base = tmp_path / "probe-state-root"
    pinned_state = state_root_base / "hermes-webui-tests" / "probe-run"
    pinned_state.mkdir(parents=True)
    (pinned_state / "stale-before-probe").write_text(
        "stale", encoding="utf-8"
    )
    env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
        if key in os.environ
    }
    env.update(
        {
            "HERMES_WEBUI_AGENT_DIR": str(fake_agent),
            "HERMES_WEBUI_TEST_STATE_ROOT": str(state_root_base),
            "HERMES_WEBUI_TEST_STATE_DIR": str(pinned_state),
            "PYTHONPATH": os.pathsep.join(
                (str(conftest.REPO_ROOT), str(fake_agent))
            ),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import tests.conftest as c; "
                f"assert str(c.HERMES_AGENT) == {str(fake_agent)!r}"
            ),
        ],
        cwd=conftest.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not pinned_state.exists()
