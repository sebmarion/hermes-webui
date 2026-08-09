"""
Regression tests for pytest-process state isolation.

Some tests import api.config/api.models during collection and directly write
sessions from the pytest process. conftest must publish the test state env vars
before those imports, not only for the server subprocess.
"""

from pathlib import Path


def test_api_config_uses_pytest_state_dir():
    import api.config as config
    from tests.conftest import TEST_STATE_DIR

    test_state_dir = TEST_STATE_DIR.resolve()
    production_state_dir = (Path.home() / ".hermes" / "webui").resolve()

    assert config.STATE_DIR == test_state_dir
    assert config.SESSION_DIR == test_state_dir / "sessions"
    assert config.STATE_DIR != production_state_dir
    assert production_state_dir not in config.SESSION_DIR.resolve().parents


def test_auto_state_dir_name_distinguishes_reused_port_across_processes(
    monkeypatch, tmp_path
):
    import tests.conftest as conftest

    repo_root = tmp_path / "repo"
    port = 43210

    monkeypatch.setattr(conftest.os, "getpid", lambda: 111)
    first_name = conftest._auto_state_dir_name(repo_root, port=port)
    assert conftest._auto_state_dir_name(repo_root, port=port) == first_name
    assert first_name.endswith("-111-43210")

    monkeypatch.setattr(conftest.os, "getpid", lambda: 222)
    second_name = conftest._auto_state_dir_name(repo_root, port=port)
    assert second_name != first_name
    assert second_name.endswith("-222-43210")


def test_prepare_state_dir_preserves_auto_initialization_but_cleans_pinned_state(
    tmp_path,
):
    import tests.conftest as conftest

    auto_state = tmp_path / "auto-state"
    auto_state.mkdir()
    auto_marker = auto_state / "async_delegations.json"
    auto_marker.write_text("{}", encoding="utf-8")
    auto_workspace = auto_state / "test-workspace"
    auto_workspace.mkdir()
    workspace_marker = auto_workspace / "import-initialized"
    workspace_marker.write_text("ready", encoding="utf-8")

    conftest._prepare_test_state_dir(auto_state, pinned=False)

    assert auto_marker.read_text(encoding="utf-8") == "{}"
    assert workspace_marker.read_text(encoding="utf-8") == "ready"

    pinned_state = tmp_path / "pinned-state"
    pinned_state.mkdir()
    stale_marker = pinned_state / "stale.json"
    stale_marker.write_text("stale", encoding="utf-8")

    conftest._prepare_test_state_dir(pinned_state, pinned=True)

    assert pinned_state.is_dir()
    assert not stale_marker.exists()
    assert (pinned_state / "test-workspace").is_dir()
