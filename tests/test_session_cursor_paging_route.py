import json
from dataclasses import replace
from urllib.parse import quote, urlparse

import pytest

from tests.test_bounded_session_detail_routes import (
    _BrowserSession,
    _forbidden,
    _resolution,
    _run_browser_session_route,
)


def _state_messages():
    return [
        {"role": "user", "content": "one", "timestamp": 1},
        {"role": "assistant", "content": "two", "timestamp": 2},
        {"role": "user", "content": "three", "timestamp": 3},
    ]


def test_absent_negotiation_is_exact_legacy_and_gate_off_adds_only_mode(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = replace(
        _resolution(db_path=db_path),
        lineage_fingerprint="sha256:" + ("a" * 64),
    )
    history = _state_messages()
    base_query = "session_id=root&messages=1&resolve_model=0&msg_limit=2"

    legacy, _resolve_calls, _get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query=base_query,
        history_reader=lambda **_kwargs: history,
    )
    monkeypatch.delenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", raising=False)
    negotiated, _resolve_calls, _get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query=f"{base_query}&message_paging=cursor_v1",
        history_reader=lambda **_kwargs: history,
    )

    legacy_session = legacy["payload"]["session"]
    negotiated_session = negotiated["payload"]["session"]
    assert "message_page" not in legacy_session
    assert negotiated_session["message_page"] == {
        "mode": "legacy",
        "fallback_reason": "gate_off",
    }
    assert {
        key: value
        for key, value in negotiated_session.items()
        if key != "message_page"
    } == legacy_session


@pytest.mark.parametrize(
    "suffix",
    [
        "message_paging=cursor_v1&msg_limit=0",
        "message_paging=cursor_v1&msg_limit=101",
        "message_paging=cursor_v1&msg_limit=-1",
        "message_paging=cursor_v1&msg_limit=1.5",
        "message_paging=cursor_v1&msg_limit=text",
        "message_paging=cursor_v1&msg_limit=",
        "message_paging=cursor_v1&msg_limit=30&msg_limit=31",
        "message_paging=cursor_v1&message_paging=cursor_v1&msg_limit=30",
        "message_paging=unknown&msg_limit=30",
        "msg_cursor=abc.def&msg_limit=30",
        "message_paging=cursor_v1&msg_limit=30&msg_cursor=abc.def&msg_before=2",
        "message_paging=cursor_v1&msg_limit=30&msg_cursor=not-a-token",
    ],
)
def test_invalid_negotiation_returns_400_before_database_access(
    suffix,
    monkeypatch,
):
    import api.routes as routes

    captured = {}

    def respond(_handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return payload

    monkeypatch.setattr(routes, "_active_state_db_path", _forbidden("database"))
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        _forbidden("canonical resolution"),
    )
    monkeypatch.setattr(routes, "j", respond)
    monkeypatch.setattr(
        routes.RequestDiagnostics,
        "maybe_start",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    handler = type("Handler", (), {"_safe_webui_print": lambda *_args: None})()

    routes.handle_get(handler, urlparse(f"/api/session?session_id=root&{suffix}"))

    assert captured["status"] == 400
    assert captured["payload"]["code"] == "invalid_message_paging"
    assert "session" not in captured["payload"]


def _cursor_token(*, resolution, profile="default", lineage_fingerprint=None):
    from api.session_message_paging import (
        MESSAGE_CURSOR_VERSION,
        MessageCursorBoundary,
        MessageCursorClaims,
        encode_message_cursor,
        message_cursor_database_identity_digest,
    )

    return encode_message_cursor(
        MessageCursorClaims(
            version=MESSAGE_CURSOR_VERSION,
            profile=profile,
            canonical_id=resolution.canonical_id,
            lineage_fingerprint=(
                lineage_fingerprint or resolution.lineage_fingerprint
            ),
            source_mode="state_db",
            database_identity_digest=message_cursor_database_identity_digest(
                resolution.database_identity
            ),
            global_generation_hint=resolution.global_projection_generation_hint,
            receipt_generation=None,
            receipt_proof_digest=None,
            boundaries=(MessageCursorBoundary("tip", 10, 1),),
        ),
        member_ids=resolution.member_ids,
    )


@pytest.mark.parametrize(
    ("token_factory", "expected_status"),
    [
        (lambda resolution: "abc.def", 400),
        (lambda resolution: _cursor_token(resolution=resolution, profile="other"), 400),
        (
            lambda resolution: _cursor_token(
                resolution=resolution,
                lineage_fingerprint="sha256:" + ("b" * 64),
            ),
            409,
        ),
        (lambda resolution: _cursor_token(resolution=resolution), 409),
    ],
)
def test_cursor_errors_are_typed_and_never_read_history(
    token_factory,
    expected_status,
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = replace(
        _resolution(db_path=db_path),
        lineage_fingerprint="sha256:" + ("a" * 64),
    )
    token = token_factory(resolution)

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=30"
            f"&message_paging=cursor_v1&msg_cursor={quote(token)}"
        ),
    )

    assert captured["status"] == expected_status
    assert get_calls == [("tip", True)]
    assert "session" not in captured["payload"]
    assert "messages" not in json.dumps(captured["payload"])
    assert captured["payload"]["code"] == (
        "invalid_message_cursor"
        if expected_status == 400
        else "cursor_restart_required"
    )


def test_cursor_for_previous_compression_tip_requires_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    current = replace(
        _resolution(db_path=db_path),
        canonical_id="new-tip",
        lineage_fingerprint="sha256:" + ("a" * 64),
    )
    previous = replace(current, canonical_id="old-tip")
    token = _cursor_token(resolution=previous)

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=current,
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=30"
            f"&message_paging=cursor_v1&msg_cursor={quote(token)}"
        ),
    )

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "cursor_restart_required"
    assert get_calls == [("new-tip", True)]


def test_cursor_for_contracted_lineage_requires_restart_not_invalid(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    previous = replace(
        _resolution(db_path=db_path),
        canonical_id="tip",
        member_ids=("root", "tip"),
        lineage_fingerprint="sha256:" + ("a" * 64),
    )
    current = replace(
        previous,
        canonical_id="root",
        member_ids=("root",),
        lineage_fingerprint="sha256:" + ("b" * 64),
    )
    token = _cursor_token(resolution=previous)

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=current,
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=30"
            f"&message_paging=cursor_v1&msg_cursor={quote(token)}"
        ),
    )

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "cursor_restart_required"
    assert get_calls == [("root", True)]


@pytest.mark.parametrize(
    ("gate", "reason"),
    [("shadow", "receipt_unavailable"), ("on", "receipt_unavailable")],
)
def test_proof_unavailable_never_exposes_cursor_success(
    gate,
    reason,
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    history = _state_messages()
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", gate)

    captured, _resolve_calls, _get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=2"
            "&message_paging=cursor_v1"
        ),
        history_reader=lambda **_kwargs: history,
    )

    session = captured["payload"]["session"]
    assert session["message_page"] == {
        "mode": "legacy",
        "fallback_reason": reason,
    }
    assert "before_cursor" not in session["message_page"]
    assert session["messages"]
    assert "_messages_offset" in session
    assert "_messages_truncated" in session


def test_synthesized_fallback_preserves_negotiated_legacy_metadata(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    history = _state_messages()

    captured, _resolve_calls, _get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=KeyError("tip"),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=2"
            "&message_paging=cursor_v1"
        ),
        history_reader=lambda **_kwargs: history,
        claim_handler=lambda _sid, **_kwargs: (
            _BrowserSession(messages=history),
            "materialized",
        ),
    )

    session = captured["payload"]["session"]
    assert session["message_page"] == {
        "mode": "legacy",
        "fallback_reason": "gate_off",
    }
    assert session["messages"] == history
