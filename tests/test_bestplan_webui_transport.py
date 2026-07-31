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


def test_server_recovers_bestplan_routing_from_stale_browser_tabs():
    routes = (ROOT / "api" / "routes.py").read_text()

    assert 'r"^/(?:bestplan|bp)(?:\\s+|$)"' in routes
    assert 'recovered_bestplan_config = {"count": bestplan_count}' in routes
    assert "bestplan_parts[0].isascii()" in routes
    assert "bestplan_parts[0].isdigit()" in routes
    assert 'raw_bestplan = body.get("bestplan_config") or recovered_bestplan_config' in routes
    assert 'msg = " ".join(bestplan_parts).strip()' in routes


def test_bestplan_stream_withholds_machine_deltas_until_terminal_capture():
    source = (ROOT / "api" / "streaming.py").read_text()
    callback = source[source.index("            def on_token(text):"):]
    callback = callback[:callback.index("\n            def ", 1)]

    gate = callback.index("if bestplan_config is not None:")
    assert gate < callback.index("STREAM_PARTIAL_TEXT[stream_id] +=")
    assert gate < callback.index("put('token'")
