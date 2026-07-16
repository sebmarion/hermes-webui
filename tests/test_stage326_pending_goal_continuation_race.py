"""Regression guards for the server-owned goal continuation chain."""
import re
from pathlib import Path


def _read_streaming():
    return Path(__file__).parents[1].joinpath("api", "streaming.py").read_text(encoding="utf-8")


def _read_routes():
    return Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")


def test_streaming_settles_goal_only_after_parent_ownership_cleanup():
    src = _read_streaming()
    pop_idx = src.find("STREAM_GOAL_RELATED.pop(stream_id")
    unregister_idx = src.find("unregister_active_run(stream_id)", pop_idx)
    settle_idx = src.find("settle_goal_continuation(session_id, stream_id)", unregister_idx)
    assert -1 not in (pop_idx, unregister_idx, settle_idx)
    assert pop_idx < unregister_idx < settle_idx


def test_routes_server_start_marks_goal_continuation_goal_related():
    src = _read_routes()
    assert 'goal_related=source == "goal_continuation"' in src


def test_goal_continuation_receipt_store_is_process_locked_and_atomic():
    src = Path(__file__).parents[1].joinpath("api", "goal_continuation.py").read_text(
        encoding="utf-8"
    )
    assert "flock" in src and "os.replace" in src


def test_stream_goal_related_pop_keyed_by_stream_id():
    """STREAM_GOAL_RELATED.pop in the cleanup must be keyed by stream_id
    (the ending stream's id), not session_id — a different stream's flag
    must not be erased."""
    src = _read_streaming()
    # Search for the cleanup line.
    m = re.search(r"STREAM_GOAL_RELATED\.pop\(([^,)]+)", src)
    assert m is not None, "STREAM_GOAL_RELATED.pop not found in streaming.py"
    key = m.group(1).strip()
    assert key == "stream_id", (
        f"STREAM_GOAL_RELATED.pop must be keyed by stream_id, got {key!r}. "
        "Using session_id would erase a different stream's flag if two "
        "streams overlap on the same session."
    )


def test_goal_continue_claim_is_durable_before_emitting_event():
    src = _read_streaming()
    claim_idx = src.find("claim_goal_continuation(")
    event_idx = src.find("put('goal_continue'", claim_idx)
    assert claim_idx != -1 and event_idx != -1
    assert claim_idx < event_idx
