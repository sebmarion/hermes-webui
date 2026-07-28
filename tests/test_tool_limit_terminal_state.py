import json
import queue
import sys
import types
from pathlib import Path

from api import config, models
from api import streaming
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]


def _run_streaming_with_fake_agent(
    tmp_path,
    monkeypatch,
    agent_result,
    *,
    prior_messages=None,
    prior_context_messages=None,
    goal_related=False,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    streaming.PENDING_GOAL_CONTINUATION.clear()
    try:
        from api.config import SESSION_AGENT_CACHE

        SESSION_AGENT_CACHE.clear()
    except Exception:
        pass

    session_id = "tool_limit_session"
    stream_id = "stream-tool-limit"
    session = Session(
        session_id=session_id,
        title="Tool limit test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=list(prior_messages or []),
        context_messages=list(prior_context_messages or []),
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[session_id] = session
    streaming.SESSIONS[session_id] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            return agent_result

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        import api.routes as routes

        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(
            routes,
            "start_session_turn",
            lambda sid, _prompt, source=None: {
                "session_id": sid,
                "stream_id": f"continuation-{sid}",
                "source": source,
            },
        )
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
            goal_related=goal_related,
        )

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    payload = json.loads((session_dir / f"{session_id}.json").read_text(encoding="utf-8"))
    return events, payload


def test_synthetic_max_iteration_summary_request_is_dropped_from_agent_result():
    synthetic = {
        "role": "user",
        "content": streaming._MAX_ITERATION_SUMMARY_REQUEST,
    }
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        synthetic,
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": messages,
    }

    assert streaming._agent_result_tool_limit_reached(result) is True

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(
        result["messages"],
        enabled=streaming._agent_result_tool_limit_reached(result),
    )

    assert synthetic not in cleaned
    assert cleaned[-1]["role"] == "assistant"
    assert "here is the summary" in cleaned[-1]["content"]


def test_tool_limit_detection_uses_explicit_boolean_grouping():
    streaming_py = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    assert "or ('tool-calling iterations' in haystack and 'maximum' in haystack)" in streaming_py


def test_historical_synthetic_summary_prompt_does_not_mark_normal_result_as_tool_limit():
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Current normal task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    assert streaming._agent_result_tool_limit_reached(result) is False


def test_tool_limit_with_final_answer_marks_latest_assistant_status_card():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]

    assert streaming._session_lacks_final_assistant_answer(messages) is False
    assert streaming._mark_latest_assistant_tool_limit_status(messages) is True

    assistant = messages[-1]
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_terminal_reason"] == "max_iterations"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"


def test_tool_limit_without_final_answer_is_no_final_terminal_state_after_filtering():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
    ]

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(messages)

    assert all(
        not streaming._is_synthetic_max_iteration_summary_request(message)
        for message in cleaned
    )
    assert streaming._session_lacks_final_assistant_answer(cleaned) is True


def test_display_merge_does_not_render_synthetic_summary_prompt():
    previous_display = [{"role": "user", "content": "Do the long task."}]
    previous_context = [{"role": "user", "content": "Do the long task."}]
    result_messages = previous_context + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result_messages = streaming._drop_synthetic_max_iteration_summary_requests(
        result_messages,
        enabled=True,
    )

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Do the long task.",
    )

    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in merged
    )
    assert merged[-1]["role"] == "assistant"
    assert "here is the summary" in merged[-1]["content"]


def test_internal_control_prompt_is_removed_from_model_history_before_current_turn():
    prompt = "Continue the unfinished task from the parent segment."
    control_key = "_tool_limit_continuation_control"
    history = [
        {"role": "user", "content": "original task"},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "legitimate historical answer"},
        {"role": "user", "content": prompt, control_key: {"continuation_index": 1}},
        {"role": "assistant", "content": "prior segment summary"},
    ]

    cleaned = streaming._drop_internal_control_prompts_from_history(
        history,
        prompt,
        control_key,
    )

    assert cleaned == [
        {"role": "user", "content": "original task"},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "legitimate historical answer"},
        {"role": "assistant", "content": "prior segment summary"},
    ]


def test_internal_control_prompt_markerless_history_is_not_removed_by_text_alone():
    prompt = "Continue the unfinished task from the parent segment."
    history = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "historical answer"},
        {"role": "user", "content": f"[Workspace::v1: /tmp/work]\n{prompt}"},
    ]

    cleaned = streaming._drop_internal_control_prompts_from_history(
        history,
        prompt,
        "_tool_limit_continuation_control",
    )

    assert cleaned == history


def test_internal_control_context_keeps_one_workspace_decorated_prompt():
    prompt = "Continue the unfinished task from the parent segment."
    control_key = "_tool_limit_continuation_control"
    control = {"continuation_index": 2}
    workspace_prompt = f"[Workspace::v1: /tmp/work]\n{prompt}"
    messages = [
        {"role": "user", "content": "original task"},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "legitimate historical answer"},
        {"role": "user", "content": workspace_prompt},
        {"role": "user", "content": prompt, control_key: {"continuation_index": 1}},
        {"role": "assistant", "content": "current segment summary"},
    ]

    canonical = streaming._canonicalize_internal_control_context(
        messages,
        prompt,
        control_key,
        control,
        previous_messages=messages[:3],
    )

    matching = [
        message
        for message in canonical
        if message.get("role") == "user"
        and streaming._internal_control_prompt_matches(message, prompt)
    ]
    assert len(matching) == 2
    current = [message for message in matching if control_key in message]
    assert len(current) == 1
    assert current[0]["content"] == workspace_prompt
    assert current[0][control_key] == control
    assert matching[0] == {"role": "user", "content": prompt}


def test_internal_control_context_uses_current_turn_boundary_for_identical_workspace_text():
    prompt = "Continue the unfinished task from the parent segment."
    control_key = "_tool_limit_continuation_control"
    workspace_prompt = f"[Workspace::v1: /tmp/work]\n{prompt}"
    previous = [
        {"role": "user", "content": workspace_prompt},
        {"role": "assistant", "content": "Legitimate historical answer."},
    ]
    result = [
        *previous,
        {"role": "user", "content": workspace_prompt},
        {"role": "assistant", "content": "Current continuation answer."},
    ]

    canonical = streaming._canonicalize_internal_control_context(
        result,
        prompt,
        control_key,
        {"continuation_index": 2},
        previous_messages=previous,
    )

    matches = [message for message in canonical if streaming._internal_control_prompt_matches(message, prompt)]
    assert len(matches) == 2
    assert control_key not in matches[0]
    assert matches[1][control_key] == {"continuation_index": 2}


def test_max_iteration_cleanup_preserves_legitimate_identical_user_turns():
    prompt = streaming._MAX_ITERATION_SUMMARY_REQUEST
    previous = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "Historical legitimate answer."},
    ]
    result = [
        *previous,
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "Current summary."},
    ]

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(
        result,
        previous_messages=previous,
    )

    matching = [message for message in cleaned if message.get("content") == prompt]
    assert len(matching) == 2
    assert matching == [previous[0], result[2]]


def test_internal_control_display_merge_preserves_older_identical_user_turn():
    prompt = "Continue the unfinished task from the parent segment."
    previous_display = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "That was a legitimate earlier request."},
    ]
    result = [
        *previous_display,
        {"role": "user", "content": f"[Workspace::v1: /tmp/work]\n{prompt}"},
        {"role": "assistant", "content": "Current continuation result."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_display,
        result,
        prompt,
        source="tool_limit_continuation",
    )

    assert merged[0] == previous_display[0]
    assert sum(
        1 for message in merged
        if message.get("role") == "user" and message.get("content") == prompt
    ) == 1
    assert merged[-1]["content"] == "Current continuation result."


def test_frontend_handles_tool_limit_apperror_label():
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = messages_js.find("source.addEventListener('apperror'")
    end = messages_js.find("source.addEventListener('warning'", start)
    assert start != -1 and end != -1
    block = messages_js[start:end]

    assert "const isToolLimitReached=d.type==='tool_limit_reached';" in block
    assert "Tool iteration limit reached" in block
    assert "Terminal state details" in block


def test_streaming_tool_limit_with_final_answer_reports_continuation_pending(tmp_path, monkeypatch):
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["terminal_reason"] == "max_iterations"
    assert done_payloads[-1]["continuation_pending"] is True
    assert "auto_continuing" not in done_payloads[-1]
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["context_messages"]
    )
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert "_terminal_state" not in assistant
    assert "_statusCard" not in assistant


def test_streaming_tool_limit_without_final_answer_continues_without_apperror(tmp_path, monkeypatch):
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert not apperror_payloads
    done_payloads = [event_payload for event, event_payload in events if event == "done"]
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["continuation_pending"] is True
    assert "auto_continuing" not in done_payloads[-1]
    assert not [message for message in payload["messages"] if message.get("_error")]
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_with_fallback_final_response_surfaces_closure_text(tmp_path, monkeypatch):
    """#5494 — handle_max_iterations() guarantees a non-empty ``final_response``
    on iteration-limit exhaustion. This test pins the WebUI contract that,
    when ``messages`` ends without a final assistant turn and ``final_response``
    is set, the user sees that closure text instead of a bare
    ``tool_limit_reached`` error. Serves both as a live-bug fix pin and as a
    regression guard for the agent's "delivered final_response ⇒ assistant
    row" invariant: if a future agent regression drops that invariant, this
    test still passes because the WebUI honors the contract locally.
    """
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": graceful,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    # The user sees the graceful fallback, not a bare tool_limit_reached error.
    # Either (a) we synthesized the fallback here, or (b) the agent guarantee
    # already added an assistant row and `_mark_latest_assistant_tool_limit_status`
    # attached the status card. Both routes satisfy the contract.
    assert not [
        ap for ev, ap in events if ev == "apperror"
        and ap.get("type") == "tool_limit_reached"
    ], "expected no tool_limit_reached apperror when fallback was returned"
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"

    # Fallback text remains visible as segment context, but it is not marked as
    # the task's terminal answer because the durable successor owns completion.
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == graceful
    assert "_terminal_state" not in assistant
    assert "_statusCard" not in assistant
    # Synthetic scaffolding turn was still dropped, even after fallback injection.
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_with_fallback_does_not_double_inject_when_assistant_exists(tmp_path, monkeypatch):
    """#5494 — when the agent already appended a model-generated summary
    AND ``final_response`` carries the same text, the WebUI must not duplicate
    the assistant turn. Pins the no-op contract on the synthesis path.
    """
    summary = "I reached the limit; here is the summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": summary,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": summary},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads
    assistant_msgs = [
        m for m in payload["messages"]
        if m.get("role") == "assistant" and m.get("content") == summary
    ]
    assert len(assistant_msgs) == 1, "fallback must not duplicate the existing summary"
    assert "_terminal_state" not in assistant_msgs[0]


def test_maybe_inject_max_iteration_summary_fallback_unit():
    """Unit-level coverage for the injection helper."""
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {"final_response": graceful}

    injected = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)

    assert injected[-1]["role"] == "assistant"
    assert injected[-1]["content"] == graceful
    assert injected[-1]["_max_iteration_summary_fallback"] is True


def test_maybe_inject_max_iteration_summary_fallback_skips_when_assistant_present():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "real summary"},
    ]
    result = {"final_response": "fallback text"}

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)
    assert out == messages


def test_maybe_inject_max_iteration_summary_fallback_skips_when_no_fallback():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
    ]

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, {})
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(
        messages, {"final_response": "   "}
    )
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, None)
    assert out == messages


def test_streaming_tool_limit_partial_result_still_requests_continuation(tmp_path, monkeypatch):
    result = {
        "status": "partial",
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert not apperror_payloads
    done_payloads = [event_payload for event, event_payload in events if event == "done"]
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["continuation_pending"] is True
    assert "auto_continuing" not in done_payloads[-1]
    assistant = next(
        message
        for message in payload["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "I reached the limit; here is the summary."
    )
    assert "_terminal_state" not in assistant
    assert "_statusCard" not in assistant
    assert not [message for message in payload["messages"] if message.get("_error")]


def test_streaming_persists_visible_blocker_when_continuation_claim_fails(tmp_path, monkeypatch):
    import api.tool_limit_continuation as tlc

    def fail_claim(*_args, **_kwargs):
        raise tlc.ContinuationReceiptStoreError("receipt unavailable")

    monkeypatch.setattr(tlc, "handle_terminal", fail_claim)
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "Segment summary before handoff."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [event_payload for event, event_payload in events if event == "done"]
    assert done_payloads[-1]["continuation_pending"] is True
    assert "auto_continuing" not in done_payloads[-1]
    terminal = payload["messages"][-1]
    assert terminal["_terminal_state"] == "tool_limit_reached"
    assert terminal["_terminal_reason"] == "continuation_state_unavailable"
    assert terminal["_statusCard"]["title"] == "Tool-limit continuation stopped"


def test_streaming_historical_synthetic_prompt_normal_result_does_not_emit_tool_limit(tmp_path, monkeypatch):
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    assert not [payload for event, payload in events if event == "apperror"]
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected normal done SSE payload"
    assert "terminal_state" not in done_payloads[-1]
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["content"] == "Current task completed normally."


def test_streaming_empty_result_messages_do_not_treat_prior_assistant_as_current_answer(tmp_path, monkeypatch):
    prior = [
        {"role": "user", "content": "Earlier task."},
        {"role": "assistant", "content": "Earlier answer."},
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
    )

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [payload for event, payload in events if event == "done"]
    assert payload["messages"][-1]["_error"] is True
