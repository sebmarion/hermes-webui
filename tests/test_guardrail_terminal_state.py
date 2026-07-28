from pathlib import Path

from api import streaming
from tests.test_tool_limit_terminal_state import _run_streaming_with_fake_agent


ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_structured_turn_exit_reason_classifies_guardrail_block():
    terminal = streaming._agent_result_guardrail_blocked(
        {
            "turn_exit_reason": "guardrail_halt",
            "guardrail": {
                "action": "halt",
                "code": "same_tool_failure_halt",
                "tool_name": "terminal",
                "exact_count": 1,
                "broad_count": 4,
            },
        }
    )

    assert terminal is not None
    assert terminal.reason == "same_tool_failure_halt"
    assert terminal.tool_name == "terminal"
    assert terminal.exact_count == 1
    assert terminal.broad_count == 4


def test_structured_guardrail_action_classifies_without_exit_reason():
    terminal = streaming._agent_result_guardrail_blocked(
        {
            "guardrail": {
                "action": "block",
                "code": "required_policy_block",
            },
        }
    )

    assert terminal is not None
    assert terminal.reason == "required_policy_block"


def test_guardrail_classifier_never_matches_prose():
    assert (
        streaming._agent_result_guardrail_blocked(
            {
                "final_response": (
                    "I stopped because guardrail halt and same_tool_failure_halt."
                )
            }
        )
        is None
    )


def test_malformed_structured_guardrail_halt_fails_closed():
    terminal = streaming._agent_result_guardrail_blocked(
        {
            "turn_exit_reason": "guardrail_halt",
            "guardrail": {"action": "halt", "code": {"not": "a string"}},
        }
    )

    assert terminal is not None
    assert terminal.reason == "guardrail_halt"


def test_latest_assistant_is_marked_needs_recovery_without_hiding_output():
    messages = [
        {"role": "user", "content": "Run it."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "bash: uv: command not found",
        },
        {
            "role": "assistant",
            "content": "I need a different strategy; this turn is blocked.",
        },
    ]

    assert (
        streaming._mark_latest_assistant_guardrail_status(
            messages,
            "same_tool_failure_halt",
        )
        is True
    )

    assistant = messages[-1]
    assert assistant["content"].startswith("I need a different strategy")
    assert assistant["_terminal_state"] == "guardrail_blocked"
    assert assistant["_terminal_reason"] == "same_tool_failure_halt"
    assert assistant["_statusCard"]["title"] == "Needs recovery"
    assert messages[-2]["content"] == "bash: uv: command not found"


def test_guardrail_halt_without_current_answer_never_relabels_prior_answer():
    messages = [
        {"role": "user", "content": "Earlier task."},
        {"role": "assistant", "content": "Earlier successful answer."},
        {"role": "user", "content": "Run it."},
        {"role": "tool", "content": "bash: uv: command not found"},
    ]

    assert (
        streaming._mark_latest_assistant_guardrail_status(
            messages,
            "same_tool_failure_halt",
            start_index=2,
        )
        is False
    )
    assert "_terminal_state" not in messages[1]

    recovery = streaming._guardrail_recovery_message("same_tool_failure_halt")
    assert recovery["role"] == "assistant"
    assert recovery["_terminal_state"] == "guardrail_blocked"
    assert recovery["_statusCard"]["title"] == "Needs recovery"


def test_guardrail_candidate_survives_display_merge_that_shrinks_history():
    previous_display = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "reasoning": "thinking...",
        },
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "reasoning": "thinking...",
        },
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "reasoning": "thinking...",
        },
        {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "reasoning": "thinking...",
        },
    ]
    previous_context = [{"role": "user", "content": "Hello"}]
    display_result = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Current recovery explanation."},
    ]
    token = "stream-guardrail"

    assert streaming._tag_current_turn_guardrail_candidate(
        display_result,
        previous_context,
        "Hello",
        token,
    )
    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        display_result,
        "Hello",
    )
    assert len(merged) < len(previous_display)
    assert streaming._mark_latest_assistant_guardrail_status(
        merged,
        "same_tool_failure_halt",
        candidate_token=token,
    )

    assert merged[-1]["content"] == "Current recovery explanation."
    assert merged[-1]["_terminal_state"] == "guardrail_blocked"
    assert "_guardrail_current_turn_candidate" not in merged[-1]


def test_streaming_guardrail_halt_settles_blocked_instead_of_done(
    tmp_path,
    monkeypatch,
):
    result = {
        "turn_exit_reason": "guardrail_halt",
        "guardrail": {
            "action": "halt",
            "code": "same_tool_failure_halt",
            "tool_name": "terminal",
            "exact_count": 1,
            "broad_count": 4,
        },
        "messages": [
            {"role": "user", "content": "Run it."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "bash: uv: command not found",
            },
            {
                "role": "assistant",
                "content": "I need a different strategy; this turn is blocked.",
            },
        ],
    }

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
    )

    done_payloads = [
        event_payload
        for event, event_payload in events
        if event == "done"
    ]
    assert done_payloads
    assert done_payloads[-1]["terminal_state"] == "guardrail_blocked"
    assert done_payloads[-1]["terminal_reason"] == "same_tool_failure_halt"
    assert not [
        event_payload
        for event, event_payload in events
        if event == "apperror"
    ]
    assistant = payload["messages"][-1]
    assert assistant["content"].startswith("I need a different strategy")
    assert assistant["_terminal_state"] == "guardrail_blocked"
    assert assistant["_statusCard"]["title"] == "Needs recovery"
    assert all(
        "_guardrail_current_turn_candidate" not in message
        for message in payload["context_messages"]
    )
    tool = next(
        message
        for message in payload["messages"]
        if message.get("role") == "tool"
    )
    assert tool["content"] == "bash: uv: command not found"


def test_guardrail_halt_skips_goal_judge_and_durable_budget_mutation(
    tmp_path,
    monkeypatch,
):
    from api import goals

    judge_calls = []
    monkeypatch.setattr(goals, "has_active_goal", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        goals,
        "evaluate_goal_after_turn",
        lambda *args, **kwargs: judge_calls.append((args, kwargs)),
    )
    result = {
        "turn_exit_reason": "guardrail_halt",
        "guardrail": {
            "action": "halt",
            "code": "same_tool_failure_halt",
            "tool_name": "terminal",
            "exact_count": 1,
            "broad_count": 4,
        },
        "messages": [
            {"role": "user", "content": "Run it."},
            {"role": "assistant", "content": "I need a different strategy."},
        ],
    }

    events, _payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        goal_related=True,
    )

    assert judge_calls == []
    assert not [payload for event, payload in events if event in {"goal", "goal_continue"}]


def test_browser_done_handler_adopts_structured_guardrail_state_and_reason():
    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_handler = MESSAGES_JS[done_start:done_end]

    assert "terminal_state:d.terminal_state" in done_handler
    assert "terminal_reason:d.terminal_reason" in done_handler
    assert "_adoptStructuredTerminalStateIntoAnchor" in MESSAGES_JS
    assert "_previousTerminalState==='guardrail_blocked'" in MESSAGES_JS


def test_browser_guardrail_done_skips_success_completion_side_effects():
    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_handler = MESSAGES_JS[done_start:done_end]

    assert "const _isGuardrailBlocked=d.terminal_state==='guardrail_blocked';" in done_handler
    assert "if(!_isGuardrailBlocked&&typeof _recordCompletionCandidate==='function')" in done_handler
    assert "if(!_isGuardrailBlocked&&!isSessionViewed" in done_handler
    assert "if(!_isGuardrailBlocked) playNotificationSound();" in done_handler
    assert "if(!_isGuardrailBlocked) sendBrowserNotification('Response complete'" in done_handler


def test_browser_maps_guardrail_to_needs_recovery_and_non_success_sets():
    assert (
        "'guardrail_blocked'" in UI_JS[
            UI_JS.index("const _ANCHOR_SCENE_ERRORED_TERMINAL_STATES"):
            UI_JS.index(
                "function _anchorSceneHasErroredTerminalState",
                UI_JS.index("const _ANCHOR_SCENE_ERRORED_TERMINAL_STATES"),
            )
        ]
    )
    assert (
        "'guardrail_blocked'" in UI_JS[
            UI_JS.index("const isError=["):
            UI_JS.index("node=_activityStatusNode", UI_JS.index("const isError=["))
        ]
    )
    assert "function _assistantMessageStatusCard" in UI_JS
    assert "title:'Needs recovery'" in UI_JS
    assert "function _assistantTurnStatusLabel" in UI_JS
    assert "return 'Needs recovery'" in UI_JS


def test_blocked_browser_render_never_uses_done_label_and_keeps_output_open():
    assert "_assistantTurnStatusLabel(msg)" in UI_JS
    assert "_assistantTurnDurationLabel(msg,durationText)" in UI_JS
    assert "_anchorSceneHasErroredTerminalState(scene)" in UI_JS
    assert "collapsed:!(keepSettledWorklogOpen||erroredWorklogKeepOpen)" in UI_JS
