from dataclasses import replace
import io
from pathlib import Path
import queue
from types import SimpleNamespace
from urllib.parse import quote, urlparse

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


def test_browser_reconnect_rejection_never_requests_full_session_restore():
    messages_source = (
        Path(__file__).resolve().parents[1] / "static" / "messages.js"
    ).read_text(encoding="utf-8")
    restore_start = messages_source.index(
        "async function _restoreSettledSession(source, options=null)"
    )
    full_request = messages_source.index(
        "const data=await api(`/api/session?session_id=",
        restore_start,
    )
    bounded_guard = messages_source.index(
        "if(lazyTailStream){",
        restore_start,
    )

    assert bounded_guard < full_request
    guarded_prefix = messages_source[bounded_guard:full_request]
    assert "bounded_recovery_required" in guarded_prefix
    assert "_showLazyReconnectRecovery" in guarded_prefix


def test_lazy_terminal_sse_projection_strips_complete_transcript():
    from api import routes

    original = {
        "status": "completed",
        "usage": {"input_tokens": 10},
        "session": {
            "session_id": "task-1",
            "title": "Huge task",
            "model": "model",
            "model_provider": "provider",
            "read_only": False,
            "message_count": 42_632,
            "messages": [{"role": "assistant", "content": "secret"}] * 100,
            "tool_calls": [{"id": "secret-call"}],
            "pre_compression_snapshot": {"messages": ["secret"]},
        },
    }

    projected = routes._lazy_tail_terminal_sse_payload(
        "done",
        original,
        enabled=True,
    )

    assert projected["lazy_tail_terminal_v1"] is True
    assert projected["session"]["session_id"] == "task-1"
    assert projected["session"]["message_count"] == 42_632
    assert projected["session"]["_lazy_tail_terminal_v1"] is True
    assert "messages" not in projected["session"]
    assert "tool_calls" not in projected["session"]
    assert "pre_compression_snapshot" not in projected["session"]
    assert "secret" not in repr(projected)
    assert routes._lazy_tail_terminal_sse_payload(
        "done",
        original,
        enabled=False,
    ) is original


def test_lazy_run_journal_replay_strips_complete_terminal_transcript(
    monkeypatch,
):
    from api import routes

    emitted = []
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: {"session_id": "task-1", "terminal": True},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda *_args, **_kwargs: {
            "events": [
                {
                    "event": "done",
                    "event_id": "run-1:42",
                    "payload": {
                        "session": {
                            "session_id": "task-1",
                            "read_only": False,
                            "model_provider": "provider",
                            "messages": [
                                {"role": "assistant", "content": "secret"}
                            ]
                            * 1_000,
                            "tool_calls": [{"id": "secret-call"}],
                        }
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        routes,
        "_sse_with_id",
        lambda _handler, event, payload, event_id: emitted.append(
            (event, payload, event_id)
        ),
    )

    assert routes._replay_run_journal(
        object(),
        "run-1",
        0,
        lazy_tail=True,
    ) is True

    assert emitted[0][0] == "done"
    assert emitted[0][2] == "run-1:42"
    assert emitted[0][1]["lazy_tail_terminal_v1"] is True
    assert "messages" not in emitted[0][1]["session"]
    assert "tool_calls" not in emitted[0][1]["session"]
    assert "secret" not in repr(emitted)


def test_existing_open_stream_cannot_bypass_reconnect_ack():
    messages_source = (
        Path(__file__).resolve().parents[1] / "static" / "messages.js"
    ).read_text(encoding="utf-8")
    reuse_start = messages_source.index(
        "const existingLive=LIVE_STREAMS[activeSid]"
    )
    reuse_end = messages_source.index(
        "closeOtherLiveStreams(activeSid)",
        reuse_start,
    )
    reuse_block = messages_source[reuse_start:reuse_end]

    assert "!reconnectToken&&" in reuse_block
    assert "setComposerStatus('')" not in reuse_block


def test_chat_stream_validates_signed_handoff_and_acks_before_live_events(
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

    class Stream:
        def __init__(self):
            self.queue = queue.Queue()
            self.queue.put_nowait(("stream_end", {}, "run-1:42"))

        def subscribe_with_snapshot(self):
            return self.queue, {"last_event_id": "run-1:41"}

        def unsubscribe(self, _subscriber):
            return None

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _status):
            return None

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    monkeypatch.setattr(
        routes,
        "_validated_lazy_tail_reconnect_claims",
        lambda _token, _sid: claims,
    )
    monkeypatch.setattr(
        routes,
        "_stream_id_visible_to_request_profile",
        lambda _handler, _stream_id: True,
    )
    monkeypatch.setattr(
        routes,
        "_active_run_stream_for_session",
        lambda _sid: "run-1",
    )
    monkeypatch.setattr(
        routes,
        "_sse_replay_run_journal_gap_checked",
        lambda *_args, **_kwargs: (False, 41),
    )
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run-1"] = Stream()
    handler = Handler()
    try:
        routes._handle_sse_stream(
            handler,
            urlparse(
                "/api/chat/stream?stream_id=run-1"
                f"&session_id=requested&reconnect_token={quote('signed-token')}"
            ),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert body.index("event: reconnect_ack\n") < body.index("event: stream_end\n")
    assert '"checkpoint_event_id": "run-1:41"' in body


def test_chat_stream_rejects_authority_flip_after_subscription(monkeypatch):
    from api import routes

    claims = ReconnectClaims(
        version=1,
        profile_id="default",
        canonical_session_id="canonical",
        stream_id="run-1",
        checkpoint_event_id="run-1:41",
        expires_at=1100,
    )

    class Stream:
        def __init__(self):
            self.queue = queue.Queue()
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.queue, {"last_event_id": "run-1:41"}

        def unsubscribe(self, _subscriber):
            self.unsubscribed = True

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    monkeypatch.setattr(
        routes,
        "_validated_lazy_tail_reconnect_claims",
        lambda _token, _sid: claims,
    )
    monkeypatch.setattr(
        routes,
        "_stream_id_visible_to_request_profile",
        lambda _handler, _stream_id: True,
    )
    owners = iter(("run-1", "run-2"))
    monkeypatch.setattr(
        routes,
        "_active_run_stream_for_session",
        lambda _sid: next(owners),
    )
    previous_streams = dict(routes.STREAMS)
    stream = Stream()
    routes.STREAMS.clear()
    routes.STREAMS["run-1"] = stream
    handler = Handler()
    try:
        routes._handle_sse_stream(
            handler,
            urlparse(
                "/api/chat/stream?stream_id=run-1"
                "&session_id=requested&reconnect_token=signed-token"
            ),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 409
    assert "reconnect_stream_changed" in body
    assert "event: reconnect_ack\n" not in body
    assert stream.unsubscribed is True


def test_chat_stream_rejects_missing_transport_before_ack(monkeypatch):
    from api import routes

    claims = ReconnectClaims(
        version=1,
        profile_id="default",
        canonical_session_id="canonical",
        stream_id="run-1",
        checkpoint_event_id="run-1:41",
        expires_at=1100,
    )

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    monkeypatch.setattr(
        routes,
        "_validated_lazy_tail_reconnect_claims",
        lambda _token, _sid: claims,
    )
    monkeypatch.setattr(
        routes,
        "_stream_id_visible_to_request_profile",
        lambda _handler, _stream_id: True,
    )
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    handler = Handler()
    try:
        routes._handle_sse_stream(
            handler,
            urlparse(
                "/api/chat/stream?stream_id=run-1"
                "&session_id=requested&reconnect_token=signed-token"
            ),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 409
    assert "reconnect_stream_changed" in body
    assert "event: reconnect_ack\n" not in body
