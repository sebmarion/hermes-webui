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
