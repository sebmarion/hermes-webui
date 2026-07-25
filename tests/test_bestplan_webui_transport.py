from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bestplan_marker_is_transported_without_browser_model_selection():
    messages = (ROOT / "static/messages.js").read_text()
    routes = (ROOT / "api/routes.py").read_text()
    streaming = (ROOT / "api/streaming.py").read_text()
    assert "_pendingBestplanConfig" in messages
    assert "bestplan_config" in messages
    assert "bestplan_config" in routes
    assert "bestplan_config" in streaming
    assert "BestPlan is unavailable on gateway-backed sessions" in routes


def test_host_owned_bestplan_does_not_fall_through_to_generic_skill_resolution():
    messages = (ROOT / "static/messages.js").read_text()
    intercept_start = messages.index("Slash command intercept")
    intercept_end = messages.index(
        "const activeSid=S.session.session_id",
        intercept_start,
    )
    intercept = messages[intercept_start:intercept_end]

    assert (
        "const _hostOwnedTurnCommand=['moa','bestplan','bp'].includes(_agentCmdName);"
        in intercept
    )
    assert (
        "const _bundleCmd=!_hostOwnedTurnCommand&&!_agentCmd&&"
        in intercept
    )
    assert (
        "if(!_hostOwnedTurnCommand&&!_agentCmd&&!_bundleCmd){"
        in intercept
    )
