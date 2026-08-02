import io
import json
from pathlib import Path

from api import models, routes
from api.compression_recovery import (
    compression_recovery_payload_for_session,
    is_generic_continuation_intent,
    stamp_compression_exhausted_recovery,
)
from api.models import Session
from api.session_recovery import _state_db_row_to_sidecar
from api.webui_session_db import WebUIJsonSessionDB


ROOT = Path(__file__).resolve().parents[1]


class _JSONHandler:
    headers = {}

    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass


def _payload(handler):
    raw = handler.wfile.getvalue().decode("utf-8")
    return json.loads(raw) if raw else {}


def _isolate_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    return session_dir


def test_generic_continuation_intent_is_scoped_to_empty_continue_words():
    assert is_generic_continuation_intent("continue")
    assert is_generic_continuation_intent("继续吧。")
    assert is_generic_continuation_intent("go on")
    assert not is_generic_continuation_intent("continue by summarizing the workspace changes")
    assert not is_generic_continuation_intent("继续修复 4685 的恢复卡")


def test_chat_start_blocks_generic_continue_after_compression_exhausted(monkeypatch, tmp_path):
    _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat1"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "继续"})
    payload = _payload(handler)

    assert handler.status == 409
    assert payload["type"] == "compression_recovery_required"
    assert payload["recommended_recovery_action"] == "reduce_current_request"
    assert payload["compression_recovery"]["terminal_state"] == "compression_exhausted"
    assert "narrower request" in payload["error"]
    assert "current session" in payload["error"]


def test_chat_start_keeps_recovery_when_substantive_prompt_fails_validation(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat2"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad workspace")),
    )

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "continue by checking the repo"})
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 400
    assert saved["recommended_recovery_action"] == "reduce_current_request"
    assert saved["compression_recovery"]["terminal_state"] == "compression_exhausted"


def test_chat_start_clears_recovery_when_substantive_prompt_starts(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat3"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args, **_kwargs: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda requested_model, requested_provider, **_kwargs: (requested_model or "gpt-4o", requested_provider or "openai", False),
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)

    def _fake_start_run(run_session, **_kwargs):
        assert compression_recovery_payload_for_session(run_session) is None
        run_session.save()
        return {"session_id": sid, "stream_id": "stream1", "_status": 200}

    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "continue by checking the repo"})
    payload = _payload(handler)
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 200
    assert payload["stream_id"] == "stream1"
    assert saved["compression_recovery"] == {}
    assert saved["recommended_recovery_action"] is None


def test_chat_start_restores_recovery_when_substantive_prompt_start_is_rejected(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat4"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args, **_kwargs: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda requested_model, requested_provider, **_kwargs: (requested_model or "gpt-4o", requested_provider or "openai", False),
    )
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)

    def _fake_start_run(run_session, **_kwargs):
        assert compression_recovery_payload_for_session(run_session) is None
        run_session.save()
        return {"error": "session already has an active stream", "_status": 409}

    monkeypatch.setattr(routes, "_start_run", _fake_start_run)

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "continue by checking the repo"})
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 409
    assert saved["recommended_recovery_action"] == "reduce_current_request"
    assert saved["compression_recovery"]["terminal_state"] == "compression_exhausted"


def test_recovery_start_returns_current_session_conflict_without_mutating_sessions(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrc1"
    session = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        profile="default",
        project_id="proj_1",
        messages=[{"role": "user", "content": "long task"}],
        context_messages=[{"role": "user", "content": "large context"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    files_before = {
        path.name: path.read_bytes()
        for path in session_dir.iterdir()
        if path.is_file()
    }
    sessions_before = tuple(models.SESSIONS)

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})
    payload = _payload(handler)

    assert handler.status == 409
    assert payload == {
        "error": "This session exhausted context compression. Send a narrower request in the current session.",
        "type": "compression_recovery_required",
        "source_session_id": sid,
        "current_session_id": sid,
        "recommended_recovery_action": "reduce_current_request",
    }
    assert tuple(models.SESSIONS) == sessions_before
    assert tuple(routes.SESSIONS) == sessions_before
    assert {
        path.name: path.read_bytes()
        for path in session_dir.iterdir()
        if path.is_file()
    } == files_before
    assert compression_recovery_payload_for_session(session)["recommended_action"] == "reduce_current_request"


def test_recovery_metadata_is_persisted_and_exposed_in_compact_session():
    session = Session(session_id="recovermeta1", title="x", messages=[])
    recovery = stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    compact = session.compact()

    assert recovery["terminal_state"] == "compression_exhausted"
    assert compact["recommended_recovery_action"] == "reduce_current_request"
    assert compact["compression_recovery"]["recommended_action"] == "reduce_current_request"


def test_recovery_source_metadata_round_trips_through_state_db_sidecar_rebuild(tmp_path):
    recovery = {
        "type": "compression_recovery_required",
        "terminal_state": "compression_exhausted",
        "recommended_action": "reduce_current_request",
        "source_session_id": "recoverysrc4",
    }
    db = WebUIJsonSessionDB(tmp_path)
    db.write_session(
        {
            "session_id": "recoverysrc4",
            "title": "Exhausted source",
            "model": "gpt-4o",
            "started_at": 1700000000,
            "messages": [{"role": "user", "content": "long task"}],
            "compression_recovery": recovery,
            "recommended_recovery_action": "reduce_current_request",
        }
    )
    row = db.list_sessions()[0]

    sidecar = _state_db_row_to_sidecar({"id": "recoverysrc4", **row, "messages": []})

    assert sidecar["compression_recovery"] == recovery
    assert sidecar["recommended_recovery_action"] == "reduce_current_request"


def test_compression_recovery_ui_renders_current_session_guidance_without_fork_action():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    start = ui.index("function _compressionRecoveryHtml")
    end = ui.index("function _activeCompressionRecoveryPayload", start)
    card_body = ui[start:end]

    assert "reduce_current_request" in card_body
    assert "Send a narrower request in this session." in card_body
    assert "<button" not in card_body
    assert "startCompressionRecovery" not in ui
    assert "/api/session/compression-recovery/start" not in ui
    assert "function shouldInterceptCompressionRecoveryContinuation" in ui
    assert "shouldInterceptCompressionRecoveryContinuation(text,S.pendingFiles)" in messages
    assert "_compressionRecovery:recovery||undefined" in messages


def test_compression_recovery_ui_generic_continue_shows_narrow_current_session_hint():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("function shouldInterceptCompressionRecoveryContinuation")
    end = ui.index("const MESSAGE_RENDER_WINDOW_DEFAULT", start)
    body = ui[start:end]

    assert "reduce_current_request" in body
    assert "Send a narrower request in this session." in body
    assert "start focused continuation" not in body.lower()


def test_compression_recovery_ui_renders_session_level_recovery_on_terminal_message():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("const recoveryPayload=(!isUser&&m._compressionRecovery)")
    end = ui.index("const statusHtml", start)
    body = ui[start:end]

    assert "? m._compressionRecovery" in body
    assert "_activeCompressionRecoveryPayload()" in body
    assert "isLastAssistant&&isTurnFinalAssistant" in body
    assert "typeof _activeCompressionRecoveryPayload==='function'" in body
    assert body.index("m._compressionRecovery") < body.index("_activeCompressionRecoveryPayload()")


def test_compression_recovery_ui_skips_message_fallback_after_session_clear():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("function _activeCompressionRecoveryPayload(){")
    end = ui.index("function isGenericCompressionContinuationIntent", start)
    body = ui[start:end]

    session_guard = "Object.prototype.hasOwnProperty.call(S.session,'compression_recovery')"
    message_scan = "const messages=Array.isArray(S.messages)?S.messages:[]"

    assert session_guard in body
    assert message_scan in body
    assert body.index(session_guard) < body.index(message_scan)
