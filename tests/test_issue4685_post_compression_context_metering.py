import json
import subprocess
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_agent_token_estimator_stubs(
    monkeypatch,
    *,
    request_estimator=None,
    message_estimator=None,
    compressor_budget_estimator=None,
):
    """Install only the token-estimation boundary used by the fallback helper.

    These checks must not import the operator's real ``agent`` package: it can
    have optional runtime dependencies that are irrelevant to this display-only
    WebUI estimate.
    """
    agent_package = types.ModuleType("agent")
    agent_package.__path__ = []
    model_metadata = types.ModuleType("agent.model_metadata")
    if callable(request_estimator):
        model_metadata.estimate_request_tokens_rough = request_estimator
    if callable(message_estimator):
        model_metadata.estimate_messages_tokens_rough = message_estimator
    agent_package.model_metadata = model_metadata
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", model_metadata)

    if callable(compressor_budget_estimator):
        context_compressor = types.ModuleType("agent.context_compressor")
        context_compressor._estimate_msg_budget_tokens = compressor_budget_estimator
        agent_package.context_compressor = context_compressor
        monkeypatch.setitem(sys.modules, "agent.context_compressor", context_compressor)


def _run_context_indicator(usage):
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = source.index("function _syncCtxIndicator")
    end = source.index("// ── Touch support: toggle context tooltip on tap", start)
    indicator = source[start:end]
    script = f"""
const nodes = {{}};
for (const id of ['ctxIndicatorWrap', 'ctxIndicator', 'ctxRingValue', 'ctxPercent', 'ctxTooltipUsage', 'ctxTooltipTokens', 'ctxTooltipThreshold', 'ctxTooltipCost', 'ctxTooltipCompress', 'ctxCompressBtn']) {{
  nodes[id] = {{style: {{}}, classList: {{remove(){{}}, toggle(){{}}}}, removeAttribute(){{}}, setAttribute(name, value){{ this[name] = value; }}}};
}}
global.$ = id => nodes[id] || null;
global.window = {{}};
global._syncMobileCtxDisplay = () => {{}};
global._setCtxCompressButton = () => {{}};
global._fmtTokens = value => String(value);
global.t = key => key;
{indicator}
_syncCtxIndicator({json.dumps(usage)});
console.log(JSON.stringify({{percent: nodes.ctxPercent.textContent, label: nodes.ctxIndicator['aria-label'], usage: nodes.ctxTooltipUsage.textContent, tokens: nodes.ctxTooltipTokens.textContent}}));
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_context_indicator_uses_post_compression_estimate():
    indicator = _run_context_indicator(
        {
            "last_prompt_tokens": 100_000,
            "post_compression_context_tokens_estimate": 4_096,
            "context_length": 128_000,
        }
    )

    assert indicator["percent"] == "3"
    assert indicator["label"].startswith("Estimated next model context")
    assert indicator["usage"].startswith("Estimated next model context")


def test_post_compression_estimate_uses_pruned_request_and_preserves_last_prompt(monkeypatch):
    from api.streaming import _estimate_post_compression_context_tokens

    calls = []

    def estimate(messages, *, system_prompt, tools):
        calls.append((messages, system_prompt, tools))
        return 4_096

    _install_agent_token_estimator_stubs(monkeypatch, request_estimator=estimate)
    pruned = [{"role": "assistant", "content": "summary"}]
    agent = type("Agent", (), {"tools": [{"name": "read_file"}]})()

    assert _estimate_post_compression_context_tokens(agent, pruned, "workspace") == 4_096
    assert calls == [(pruned, "workspace", agent.tools)]


def test_post_compression_estimate_falls_back_when_request_estimator_is_unavailable(monkeypatch):
    from api.streaming import _estimate_post_compression_context_tokens

    calls = []

    def estimate_messages(messages):
        calls.append(messages)
        return len(messages) * 100

    _install_agent_token_estimator_stubs(monkeypatch, message_estimator=estimate_messages)
    pruned = [{"role": "assistant", "content": "summary"}]
    agent = type("Agent", (), {"tools": [{"name": "read_file"}]})()

    assert _estimate_post_compression_context_tokens(agent, pruned, "workspace") == 300
    assert calls == [
        pruned,
        [{"role": "system", "content": "workspace"}],
        [{"role": "system", "content": str(agent.tools)}],
    ]


def test_post_compression_estimate_uses_compressor_budget_counter_without_metadata_estimators(monkeypatch):
    from api.streaming import _estimate_post_compression_context_tokens

    calls = []

    def estimate_message_budget(message):
        calls.append(message)
        return {"assistant": 100, "system": 200}[message["role"]]

    _install_agent_token_estimator_stubs(
        monkeypatch,
        compressor_budget_estimator=estimate_message_budget,
    )
    pruned = [{"role": "assistant", "content": "summary"}]
    agent = type("Agent", (), {"tools": [{"name": "read_file"}]})()
    expected_messages = [
        pruned[0],
        {"role": "system", "content": "workspace"},
        {"role": "system", "content": str(agent.tools)},
    ]

    assert _estimate_post_compression_context_tokens(agent, pruned, "workspace") == 500
    assert calls == expected_messages


def test_chat_start_clears_expired_post_compression_estimate(tmp_path, monkeypatch):
    from api.models import Session
    from api.routes import _prepare_chat_start_session_for_stream

    saved = []
    monkeypatch.setattr(Session, "save", lambda self, *args, **kwargs: saved.append(self.post_compression_context_tokens_estimate))
    session = Session(session_id="issue4685-clear", post_compression_context_tokens_estimate=4_096)

    _prepare_chat_start_session_for_stream(
        session,
        msg="next turn",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-4685",
        started_at=1.0,
    )

    assert session.post_compression_context_tokens_estimate is None
    assert saved == [None]


def test_estimate_lineage_matrix(tmp_path, monkeypatch):
    from api import models

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    direct = models.Session(session_id="issue4685-direct", post_compression_context_tokens_estimate=4_096)
    direct.save()
    restored = models.Session.load("issue4685-direct")
    child = models.Session(session_id="issue4685-child", parent_session_id=direct.session_id)
    cron = models.Session(session_id="issue4685-cron", session_source="cron")

    assert restored.compact()["post_compression_context_tokens_estimate"] == 4_096
    assert child.post_compression_context_tokens_estimate is None
    assert cron.post_compression_context_tokens_estimate is None
    assert "post_compression_context_tokens_estimate" not in child.compact() or child.compact()["post_compression_context_tokens_estimate"] is None


def test_context_indicator_without_estimate_preserves_current_behavior():
    historical = _run_context_indicator({"last_prompt_tokens": 100_000, "context_length": 128_000})
    no_data = _run_context_indicator({"input_tokens": 100_000, "output_tokens": 1})

    assert historical["percent"] == "78"
    assert historical["label"].startswith("Context window 78% used")
    assert no_data["percent"] == "\N{MIDDLE DOT}"


def test_reload_hydration_passes_post_compression_estimate_to_context_indicator():
    expected = "post_compression_context_tokens_estimate"
    for path, expected_calls in ((ROOT / "static" / "boot.js", 1), (ROOT / "static" / "sessions.js", 3)):
        source = path.read_text(encoding="utf-8")
        calls = source.split("_syncCtxIndicator({")[1:]

        assert len(calls) == expected_calls
        for call in calls:
            assert expected in call.split("});", 1)[0]

    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;" in boot
    assert "S.session.post_compression_context_tokens_estimate=data.session.post_compression_context_tokens_estimate||null;" in sessions
