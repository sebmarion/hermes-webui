from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.session_window import (
    ReconnectClaims,
    ReconnectExpected,
    SessionWindowRequest,
    build_session_window,
    decode_reconnect_token,
    encode_reconnect_token,
)
from tests.test_release_lite_session_window import _ready_dependencies


def _snapshot(stream_id, event_id):
    return {
        "schema": "run_snapshot_v1",
        "stream_id": stream_id,
        "through_event_id": event_id,
        "messages": [
            {
                "role": "assistant",
                "content": "live output",
                "_run_event_id": event_id,
            }
        ],
        "status": "running",
    }


def test_reconnect_token_is_signed_and_bound_to_every_authority_field():
    key = b"release-lite-test-signing-key"
    claims = ReconnectClaims(
        version=1,
        profile_id="default",
        canonical_session_id="canonical",
        stream_id="run-1",
        checkpoint_event_id="run-1:41",
        expires_at=1100,
    )
    token = encode_reconnect_token(claims, signing_key=key)
    expected = ReconnectExpected(
        profile_id="default",
        canonical_session_id="canonical",
        stream_id="run-1",
        checkpoint_event_id="run-1:41",
    )

    assert decode_reconnect_token(
        token,
        expected=expected,
        signing_key=key,
        now=1000,
    ) == claims

    with pytest.raises(ValueError):
        decode_reconnect_token(
            token + "x",
            expected=expected,
            signing_key=key,
            now=1000,
        )
    with pytest.raises(ValueError):
        decode_reconnect_token(
            token,
            expected=replace(expected, profile_id="other"),
            signing_key=key,
            now=1000,
        )
    with pytest.raises(ValueError):
        decode_reconnect_token(
            token,
            expected=replace(expected, canonical_session_id="other"),
            signing_key=key,
            now=1000,
        )
    with pytest.raises(ValueError):
        decode_reconnect_token(
            token,
            expected=replace(expected, stream_id="run-2"),
            signing_key=key,
            now=1000,
        )
    with pytest.raises(ValueError):
        decode_reconnect_token(
            token,
            expected=replace(expected, checkpoint_event_id="run-1:42"),
            signing_key=key,
            now=1000,
        )
    with pytest.raises(ValueError):
        decode_reconnect_token(
            token,
            expected=expected,
            signing_key=key,
            now=1101,
        )


def test_active_window_returns_snapshot_and_matching_reconnect_authority():
    snapshot = _snapshot("run-1", "run-1:41")
    deps = replace(
        _ready_dependencies(),
        capture_runtime=lambda _profile, _canonical: snapshot,
        wall_time=lambda: 1000,
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    window = payload["conversation_window"]
    assert payload["runtime_snapshot"] == snapshot
    assert window["state"] == "reconnecting"
    assert window["active_stream_id"] == "run-1"
    assert window["reconnect_token"]
    claims = decode_reconnect_token(
        window["reconnect_token"],
        expected=ReconnectExpected(
            profile_id="default",
            canonical_session_id="canonical",
            stream_id="run-1",
            checkpoint_event_id="run-1:41",
        ),
        now=1000,
    )
    assert claims.checkpoint_event_id == payload["runtime_snapshot"]["through_event_id"]


def test_stream_identity_change_retries_whole_window_once():
    snapshots = iter(
        (
            _snapshot("run-1", "run-1:41"),
            _snapshot("run-2", "run-2:2"),
            _snapshot("run-2", "run-2:3"),
            _snapshot("run-2", "run-2:3"),
        )
    )
    reads = []
    deps = _ready_dependencies()
    original_read = deps.read_state_db_message_page
    deps = replace(
        deps,
        capture_runtime=lambda _profile, _canonical: next(snapshots),
        read_state_db_message_page=lambda **kwargs: (
            reads.append(kwargs) or original_read(**kwargs)
        ),
        wall_time=lambda: 1000,
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert len(reads) == 2
    assert payload["runtime_snapshot"]["stream_id"] == "run-2"
    assert payload["runtime_snapshot"]["through_event_id"] == "run-2:3"


def test_second_stream_identity_change_returns_ambiguous_without_overlay():
    snapshots = iter(
        (
            _snapshot("run-1", "run-1:41"),
            _snapshot("run-2", "run-2:2"),
            _snapshot("run-2", "run-2:3"),
            _snapshot("run-3", "run-3:1"),
        )
    )
    deps = replace(
        _ready_dependencies(),
        capture_runtime=lambda _profile, _canonical: next(snapshots),
        wall_time=lambda: 1000,
    )

    payload = build_session_window(
        SessionWindowRequest("requested", 30, None, False),
        deps=deps,
    )

    assert payload["conversation_window"]["state"] == "reconnecting"
    assert payload["conversation_window"]["status_reason"] == "reconnect_ambiguous"
    assert payload["conversation_window"]["reconnect_token"] is None
    assert payload["runtime_snapshot"] is None


def test_route_reconnect_authority_rejects_wrong_task_stream_and_expiry(
    monkeypatch,
):
    from api import routes

    claims = ReconnectClaims(
        version=1,
        profile_id="default",
        canonical_session_id="canonical",
        stream_id="run-1",
        checkpoint_event_id="run-1:41",
        expires_at=1100,
    )
    token = encode_reconnect_token(claims)
    resolution = SimpleNamespace(status="found", canonical_id="canonical")
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: "/tmp/state.db")
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda _path, _sid: resolution,
    )
    monkeypatch.setattr(
        routes,
        "_active_run_stream_for_session",
        lambda _sid: "run-1",
    )

    assert routes._validated_lazy_tail_reconnect_claims(
        token,
        "requested",
        now=1000,
    ) == claims

    resolution.canonical_id = "other"
    with pytest.raises(ValueError):
        routes._validated_lazy_tail_reconnect_claims(
            token,
            "requested",
            now=1000,
        )
    resolution.canonical_id = "canonical"
    monkeypatch.setattr(
        routes,
        "_active_run_stream_for_session",
        lambda _sid: "run-2",
    )
    with pytest.raises(ValueError):
        routes._validated_lazy_tail_reconnect_claims(
            token,
            "requested",
            now=1000,
        )
    with pytest.raises(ValueError):
        routes._validated_lazy_tail_reconnect_claims(
            token,
            "requested",
            now=1101,
        )


def test_reconnect_ack_contract_precedes_replay_source():
    routes_source = (
        Path(__file__).resolve().parents[1] / "api" / "routes.py"
    ).read_text(encoding="utf-8")
    handler_start = routes_source.index(
        "def _handle_session_run_journal_stream_for_session"
    )
    handler_end = routes_source.index(
        "_handle_session_sse_stream_for_session =",
        handler_start,
    )
    handler = routes_source[handler_start:handler_end]

    ack = handler.index('"schema": "lazy_tail_reconnect_ack_v1"')
    replay = handler.index("emit_replay(replay_events", ack)
    assert ack < replay
