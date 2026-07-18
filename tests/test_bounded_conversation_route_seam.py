"""Route/bootstrap contracts for the bounded conversation read seam."""

from pathlib import Path
from urllib.parse import urlparse

from tests.test_bounded_session_detail_routes import (
    _BrowserSession,
    _resolution,
    _run_browser_session_route,
)


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
BOOT = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def test_negotiated_session_route_uses_the_proof_first_assembler_only_behind_public_gate():
    assert "evaluate_public_cursor_gate(" in ROUTES
    assert "BoundedSessionViewAssembler(" in ROUTES
    assert "read_current_proof_from_sources(" in ROUTES
    assert "if not public_cursor_gate.public_cursor:" in ROUTES


def test_cursor_route_response_has_only_strict_cursor_page_fields_and_no_legacy_offsets():
    assert '"mode": "cursor_v1"' in ROUTES
    for field in (
        "before_cursor",
        "has_more",
        "visible_count",
        "raw_rows_examined",
        "serialized_bytes",
    ):
        assert f'"{field}":' in ROUTES
    assert 'raw.pop("_messages_offset", None)' in ROUTES
    assert 'raw.pop("_messages_truncated", None)' in ROUTES


def test_browser_capability_is_non_persisted_and_boot_mirrors_it_from_settings():
    assert 'settings["bounded_conversation_browser"]' in ROUTES
    assert 'HERMES_WEBUI_BOUNDED_CONVERSATION_BROWSER' in ROUTES
    assert "window._boundedConversationBrowser=s.bounded_conversation_browser===true" in BOOT


def test_browser_operator_switch_cannot_bypass_missing_public_proof(monkeypatch):
    import api.routes as routes

    monkeypatch.setenv("HERMES_WEBUI_BOUNDED_CONVERSATION_BROWSER", "1")
    monkeypatch.delenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", raising=False)

    assert routes._bounded_conversation_browser_enabled() is False


def test_settings_get_exposes_browser_capability_without_persisting_it(monkeypatch):
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_bounded_conversation_browser_enabled", lambda: True)
    monkeypatch.setattr(routes, "load_settings", lambda: {"theme": "dark"})
    monkeypatch.setattr(routes, "persisted_speech_settings_keys", lambda: [])
    monkeypatch.setattr(
        routes,
        "save_settings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("GET must not persist settings")),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.update(
            payload=payload, status=status
        ) or payload,
    )

    routes.handle_get(object(), urlparse("/api/settings"))

    assert captured["status"] == 200
    assert captured["payload"]["bounded_conversation_browser"] is True


def test_proven_initial_cursor_avoids_legacy_history_and_full_sidecar_load(tmp_path, monkeypatch):
    import api.routes as routes
    import api.bounded_conversation_integration as integration
    import api.bounded_target_confirmation as confirmation
    import api.conversation_receipts as receipts
    import api.conversation_shadow_evidence as evidence
    import api.conversation_view_state as view_state
    from api.bounded_conversation_integration import PublicCursorGate
    from api.bounded_session_view import ProofCapability
    from api.session_message_paging import StateDBMessagePage
    from tests.test_bounded_initial_view import _boundaries, _current, _page_bytes, _receipt
    from types import SimpleNamespace
    from dataclasses import replace

    db_path = tmp_path / "state.db"
    resolution = replace(
        _resolution(db_path=db_path),
        member_ids=("root", "tip"),
        lineage_fingerprint="sha256:" + ("a" * 64),
    )
    receipt = _receipt(lineage_fingerprint=resolution.lineage_fingerprint)
    current = _current(receipt)
    page_messages = [{"role": "assistant", "content": "bounded", "_state_db_message_id": 9}]

    class ReceiptStore:
        def __init__(self, *_args):
            pass

        def load(self, *_args):
            return receipt

    class ViewStore:
        def __init__(self, *_args):
            pass

        def read(self, **_kwargs):
            return SimpleNamespace(
                generation=receipt.todo_projection_generation,
                watermark=SimpleNamespace(
                    message_id=receipt.todo_projection_watermark[0],
                    timestamp=receipt.todo_projection_watermark[1],
                ),
                snapshot_digest=receipt.todo_projection_snapshot_digest,
                snapshot={"todos": [], "summary": {}, "version": 1},
            )

    class EvidenceStore:
        def __init__(self, *_args):
            pass

        def readiness(self, _proof):
            return SimpleNamespace(ready=True, reason="ready")

    class CurrentProof:
        state_content_proof = receipt.state_content_proof
        todo_projection = SimpleNamespace(
            generation=receipt.todo_projection_generation,
            timestamp=receipt.todo_projection_watermark[1],
            message_id=receipt.todo_projection_watermark[0],
            snapshot_digest=receipt.todo_projection_snapshot_digest,
        )

        def to_mapping(self):
            return dict(current)

    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "on")
    monkeypatch.setattr(
        integration,
        "detect_readonly_proof_capability",
        lambda _path: ProofCapability(True, "valid", receipts.VERIFIED_AGENT_CONTENT_PROOF_CAPABILITY),
    )
    monkeypatch.setattr(
        integration,
        "evaluate_public_cursor_gate",
        lambda *_args, **_kwargs: PublicCursorGate(True, "ready"),
    )
    monkeypatch.setattr(integration, "read_current_proof_from_sources", lambda **_kwargs: CurrentProof())
    monkeypatch.setattr(receipts, "ConversationReceiptStore", ReceiptStore)
    monkeypatch.setattr(view_state, "ConversationViewStateStore", ViewStore)
    monkeypatch.setattr(evidence, "ConversationShadowEvidenceStore", EvidenceStore)
    monkeypatch.setattr(confirmation, "confirm_shared_session_target", lambda *_args: True)
    monkeypatch.setattr(routes, "_bounded_runtime_owner_absent", lambda *_args: True)
    monkeypatch.setattr(
        routes,
        "read_state_db_message_page",
        lambda **_kwargs: StateDBMessagePage(
            mode="cursor_v1",
            messages=tuple(page_messages),
            before_boundaries=_boundaries(),
            has_more=True,
            visible_count=1,
            raw_rows_examined=2,
            serialized_bytes=_page_bytes(page_messages),
            sql_count=1,
            query_plan_indexed=True,
        ),
    )

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query="session_id=root&messages=1&resolve_model=0&msg_limit=1&message_paging=cursor_v1",
        history_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("proven cursor response must not read legacy history")
        ),
    )

    session = captured["payload"]["session"]
    assert session["messages"] == page_messages
    assert session["todo_state"] == {"todos": [], "summary": {}, "version": 1}
    assert get_calls == [("tip", True)]
    assert "_messages_offset" not in session
    assert "_messages_truncated" not in session
    assert session["message_page"] == {
        "mode": "cursor_v1",
        "before_cursor": session["message_page"]["before_cursor"],
        "has_more": True,
        "visible_count": 1,
        "raw_rows_examined": 2,
        "serialized_bytes": _page_bytes(page_messages),
    }


def test_proven_cursor_continuation_failure_has_no_session_or_messages(tmp_path, monkeypatch):
    import api.routes as routes
    from api.bounded_conversation_integration import PublicCursorGate
    from api.bounded_session_view import BoundedViewResult

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "on")
    monkeypatch.setattr(
        routes,
        "_assemble_bounded_conversation_view",
        lambda **_kwargs: (
            PublicCursorGate(True, "ready"),
            BoundedViewResult(409, "cursor_v1", [], None, None, error="cursor_restart_required"),
            0,
            None,
        ),
    )

    captured, _resolve_calls, _get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=_resolution(db_path=db_path),
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=1"
            "&message_paging=cursor_v1&msg_cursor=abc.def"
        ),
        history_reader=lambda **_kwargs: [{"role": "assistant", "content": "legacy", "timestamp": 1}],
    )

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "cursor_restart_required"
    assert "session" not in captured["payload"]
    assert "messages" not in captured["payload"]


def test_cursor_continuation_that_becomes_runtime_ineligible_restarts_without_legacy(
    tmp_path, monkeypatch
):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    session = _BrowserSession()
    session.active_stream_id = "active-run"
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "on")
    monkeypatch.setattr(
        routes,
        "_assemble_bounded_conversation_view",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an ineligible runtime owner must not enter cursor assembly")
        ),
    )

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=_resolution(db_path=db_path),
        session_or_error=session,
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=1"
            "&message_paging=cursor_v1&msg_cursor=abc.def"
        ),
    )

    assert get_calls == [("tip", True)]
    assert captured["status"] == 409
    assert captured["payload"] == {
        "error": "Message cursor restart required",
        "code": "cursor_restart_required",
    }


def test_runtime_owner_registered_during_initial_assembly_forces_exact_legacy_fallback(
    tmp_path, monkeypatch
):
    import api.routes as routes
    from api.bounded_conversation_integration import PublicCursorGate
    from api.bounded_session_view import BoundedViewResult

    db_path = tmp_path / "state.db"
    ownership = iter((True, False))
    legacy_messages = [
        {"role": "user", "content": "legacy oracle", "timestamp": 1}
    ]
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "on")
    monkeypatch.setattr(
        routes,
        "_bounded_runtime_owner_absent",
        lambda *_args: next(ownership),
    )
    monkeypatch.setattr(
        routes,
        "_assemble_bounded_conversation_view",
        lambda **_kwargs: (
            PublicCursorGate(True, "ready"),
            BoundedViewResult(
                200,
                "cursor_v1",
                [{"role": "assistant", "content": "stale bounded"}],
                1,
                "opaque-next",
                has_more=True,
            ),
            1,
            {"todos": [], "summary": {}, "version": 1},
        ),
    )

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=_resolution(db_path=db_path),
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=1"
            "&message_paging=cursor_v1"
        ),
        history_reader=lambda **_kwargs: legacy_messages,
    )

    assert get_calls == [("tip", True), ("tip", False)]
    assert captured["status"] == 200
    assert captured["payload"]["session"]["messages"] == legacy_messages
    assert captured["payload"]["session"]["message_page"]["mode"] == "legacy"


def test_runtime_owner_registered_during_continuation_assembly_returns_empty_restart(
    tmp_path, monkeypatch
):
    import api.routes as routes
    from api.bounded_conversation_integration import PublicCursorGate
    from api.bounded_session_view import BoundedViewResult

    db_path = tmp_path / "state.db"
    ownership = iter((True, False))
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "on")
    monkeypatch.setattr(
        routes,
        "_bounded_runtime_owner_absent",
        lambda *_args: next(ownership),
    )
    monkeypatch.setattr(
        routes,
        "_assemble_bounded_conversation_view",
        lambda **_kwargs: (
            PublicCursorGate(True, "ready"),
            BoundedViewResult(
                200,
                "cursor_v1",
                [{"role": "assistant", "content": "stale bounded"}],
                1,
                "opaque-next",
                has_more=True,
            ),
            1,
            {"todos": [], "summary": {}, "version": 1},
        ),
    )

    captured, _resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=_resolution(db_path=db_path),
        session_or_error=_BrowserSession(),
        query=(
            "session_id=root&messages=1&resolve_model=0&msg_limit=1"
            "&message_paging=cursor_v1&msg_cursor=abc.def"
        ),
        history_reader=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a raced continuation must not read legacy history")
        ),
    )

    assert get_calls == [("tip", True)]
    assert captured["status"] == 409
    assert captured["payload"] == {
        "error": "Message cursor restart required",
        "code": "cursor_restart_required",
    }
