import ast
import re
from pathlib import Path


def test_streaming_appends_worker_started_before_running_phase():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    run_idx = src.index("def _run_agent_streaming(")
    worker_idx = src.index('"event": "worker_started"', run_idx)
    running_match = re.search(
        r'update_active_run\(\s*stream_id,\s*phase="running"',
        src[run_idx:],
    )
    assert running_match is not None
    running_idx = run_idx + running_match.start()

    assert worker_idx < running_idx


def test_streaming_appends_assistant_started_before_final_save():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    save_idx = src.index("s.save()", assistant_idx)

    assert block_idx < assistant_idx < save_idx


def test_streaming_assistant_started_uses_latest_assistant_message():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    block_idx = src.index("if not ephemeral and s.messages:")
    assistant_idx = src.index('"event": "assistant_started"', block_idx)
    block = src[block_idx:assistant_idx]

    assert "range(len(s.messages) - 1, -1, -1)" in block
    assert '"assistant_message_index": _latest_assistant_idx' in src[assistant_idx:src.index("s.save()", assistant_idx)]


def test_streaming_appends_completed_after_final_save():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    assistant_idx = src.index('"event": "assistant_started"')
    save_idx = src.index("s.save()", assistant_idx)
    completed_idx = src.index('"event": "completed"', save_idx)

    assert save_idx < completed_idx


def test_streaming_appends_interrupted_on_provider_error_path():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    err_idx = src.index("err_str = str(e)")
    interrupted_idx = src.index('"event": "interrupted"', err_idx)
    apperror_idx = src.index("put('apperror'", interrupted_idx)

    assert err_idx < interrupted_idx < apperror_idx


def test_stream_lifecycle_journal_keeps_submitted_session_after_rotation():
    """Terminal lifecycle events belong to the submitted session, not a rotated child."""
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="api/streaming.py")
    run_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_run_agent_streaming"
    )

    journal_calls = [
        node
        for node in ast.walk(run_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "append_turn_journal_event_for_stream"
    ]

    assert journal_calls
    assert all(
        call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "session_id"
        for call in journal_calls
    )
