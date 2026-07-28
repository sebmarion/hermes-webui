import copy
import json
from pathlib import Path

from api import streaming
from tests.test_tool_limit_terminal_state import _run_streaming_with_fake_agent


FIXTURE = Path(__file__).parent / "fixtures" / "agent_guardrail_halt_result.json"


def _structured_guardrail_result():
    result = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert result["guardrail"]["tool_name"] == "terminal"
    assert result["guardrail"]["exact_count"] == 1
    assert result["guardrail"]["broad_count"] == 4
    result = copy.deepcopy(result)
    result["guardrail"]["arguments"] = {"command": "cat /secret/path"}
    result["guardrail"]["output"] = "sensitive tool output"
    result["guardrail"]["arbitrary_nested"] = {"transcript": "sensitive transcript"}
    return result


def test_guardrail_mapping_diagnostic_contains_only_bounded_scalars():
    terminal = streaming._agent_result_guardrail_blocked(
        _structured_guardrail_result()
    )

    diagnostic = streaming._guardrail_mapping_diagnostic(terminal)

    assert diagnostic == {
        "event": "guardrail_terminal_mapped",
        "guardrail_code": "same_tool_failure_halt",
        "tool_name": "terminal",
        "exact_count": 1,
        "broad_count": 4,
        "terminal_state": "guardrail_blocked",
    }
    rendered = repr(diagnostic)
    assert "/secret/path" not in rendered
    assert "sensitive tool output" not in rendered
    assert "sensitive transcript" not in rendered
    assert "args_hash" not in rendered


def test_streaming_logs_one_structured_guardrail_mapping(
    tmp_path,
    monkeypatch,
):
    records = []
    original_info = streaming.logger.info

    def capture(message, *args, **kwargs):
        if message == "guardrail terminal mapped: %s":
            records.append(args[0])
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(streaming.logger, "info", capture)

    _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        _structured_guardrail_result(),
    )

    assert records == [
        {
            "event": "guardrail_terminal_mapped",
            "guardrail_code": "same_tool_failure_halt",
            "tool_name": "terminal",
            "exact_count": 1,
            "broad_count": 4,
            "terminal_state": "guardrail_blocked",
        }
    ]
