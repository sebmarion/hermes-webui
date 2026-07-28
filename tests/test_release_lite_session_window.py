from dataclasses import replace
from types import SimpleNamespace

import pytest

from api.session_window import (
    SessionWindowDependencies,
    SessionWindowRequest,
    SessionWindowRequestError,
    build_session_window,
)


def _messages(count):
    return tuple(
        {
            "role": "assistant",
            "content": f"message {index}",
            "_state_db_message_id": f"stable-{index}",
        }
        for index in range(1, count + 1)
    )


def _ready_dependencies():
    resolution = SimpleNamespace(
        status="found",
        requested_id="requested",
        canonical_id="canonical",
        root_id="root",
        member_ids=("root", "canonical"),
        lineage_fingerprint="sha256:" + ("a" * 64),
        canonical_row={
            "id": "canonical",
            "title": "Large task",
            "model": "model",
            "cwd": "/workspace",
        },
    )
    page = SimpleNamespace(
        mode="cursor_v1",
        messages=_messages(30),
        before_boundaries=("older-boundary",),
        has_more=True,
        visible_count=30,
        raw_rows_examined=32,
        serialized_bytes=2048,
        sql_count=6,
        query_plan_indexed=True,
    )
    return SessionWindowDependencies(
        active_profile=lambda: "default",
        state_db_path=lambda _profile: "/tmp/state.db",
        resolve_shared_session=lambda _path, _sid: resolution,
        confirm_shared_session_target=lambda _path, _resolution: True,
        read_state_db_message_page=lambda **_kwargs: page,
        encode_older_cursor=lambda **_kwargs: "opaque-older-token",
        decode_older_cursor=lambda **_kwargs: None,
        capture_runtime=lambda _profile, _canonical_id: None,
    )


def test_session_window_request_parses_initial_contract():
    request = SessionWindowRequest.parse(
        {
            "session_id": ["requested"],
            "msg_limit": ["30"],
            "resolve_model": ["0"],
        }
    )

    assert request.session_id == "requested"
    assert request.visible_limit == 30
    assert request.older_cursor is None
    assert request.resolve_model is False


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"msg_limit": ["30"]}, "missing_session_id"),
        ({"session_id": ["requested"], "msg_limit": ["0"]}, "invalid_msg_limit"),
        ({"session_id": ["requested"], "msg_limit": ["51"]}, "invalid_msg_limit"),
        ({"session_id": ["requested"], "msg_limit": ["many"]}, "invalid_msg_limit"),
        (
            {
                "session_id": ["requested"],
                "msg_limit": ["30"],
                "older_cursor": ["opaque"],
                "canonical_session_id": ["different"],
            },
            "cursor_target_mismatch",
        ),
    ],
)
def test_session_window_request_rejects_invalid_inputs(query, code):
    with pytest.raises(SessionWindowRequestError) as caught:
        SessionWindowRequest.parse(query)

    assert caught.value.code == code
    assert caught.value.status == 400


def test_build_session_window_returns_bounded_ready_shape():
    request = SessionWindowRequest.parse(
        {
            "session_id": ["requested"],
            "msg_limit": ["30"],
            "resolve_model": ["0"],
        }
    )

    payload = build_session_window(request, deps=_ready_dependencies())

    assert payload["conversation_window"] == {
        "schema": "lazy_tail_v1",
        "state": "ready",
        "source": "state_db",
        "visible_count": 30,
        "has_older": True,
        "older_cursor": "opaque-older-token",
        "newest_message_id": "stable-30",
        "active_stream_id": None,
        "reconnect_token": None,
        "exact_total_available": False,
        "status_reason": None,
    }
    assert payload["requested_session_id"] == "requested"
    assert payload["canonical_session_id"] == "canonical"
    assert payload["runtime_snapshot"] is None
    assert "message_count" not in payload
    assert len(payload["messages"]) == 30


def test_bounded_reader_metrics_stay_within_release_lite_limits():
    diagnostics = []
    deps = replace(
        _ready_dependencies(),
        diagnostic_sink=diagnostics.append,
        monotonic=iter((10.0, 10.2, 10.25)).__next__,
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert payload["conversation_window"]["state"] == "ready"
    assert diagnostics == [
        {
            "state": "ready",
            "lineage_depth": 2,
            "sql_count": 6,
            "raw_rows_examined": 32,
            "visible_rows": 30,
            "serialized_bytes": 2048,
            "state_db_read_ms": 250,
            "handoff_retry_count": 0,
        }
    ]


def test_lineage_cycle_fails_closed_without_reading_messages():
    deps = _ready_dependencies()
    resolution = deps.resolve_shared_session(None, "requested")
    resolution.member_ids = ("root", "root")
    reads = []
    deps = replace(
        deps,
        read_state_db_message_page=lambda **kwargs: reads.append(kwargs),
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert payload["conversation_window"]["state"] == "legacy_required"
    assert payload["conversation_window"]["status_reason"] == "invalid_lineage"
    assert reads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_rows_examined", 577),
        ("serialized_bytes", 2_621_441),
    ],
)
def test_page_budget_overflow_fails_closed(field, value):
    deps = _ready_dependencies()
    page = deps.read_state_db_message_page()
    setattr(page, field, value)

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert payload["conversation_window"]["state"] == "legacy_required"
    assert payload["conversation_window"]["status_reason"] == "read_budget_exceeded"
    assert payload["messages"] == []


def test_one_target_generation_change_retries_the_whole_bounded_read():
    deps = _ready_dependencies()
    confirms = iter((True, False, True, True))
    resolve_calls = []
    page_calls = []
    original_resolve = deps.resolve_shared_session
    original_read = deps.read_state_db_message_page
    deps = replace(
        deps,
        resolve_shared_session=lambda path, sid: (
            resolve_calls.append((path, sid))
            or original_resolve(path, sid)
        ),
        confirm_shared_session_target=lambda _path, _resolution: next(confirms),
        read_state_db_message_page=lambda **kwargs: (
            page_calls.append(kwargs)
            or original_read(**kwargs)
        ),
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert payload["conversation_window"]["state"] == "ready"
    assert len(resolve_calls) == 2
    assert len(page_calls) == 2
