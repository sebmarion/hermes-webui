import json
import logging
from types import MappingProxyType, SimpleNamespace
from pathlib import Path
from urllib.parse import urlparse

import api.models as models
from api.models import Session
from api.agent_sessions import SharedSessionResolution, shared_state_db_identity
from api.request_diagnostics import RequestDiagnostics


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(name)


def test_request_diagnostics_timeout_record_includes_stage_and_thread_stacks(caplog):
    logger = logging.getLogger("test.issue1855.timeout")
    diag = RequestDiagnostics(
        "GET",
        "/api/sessions?all_profiles=1",
        logger=logger,
        timeout_seconds=5,
        auto_start=False,
    )
    diag.stage("all_sessions.read_index")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        diag._on_timeout()

    assert len(caplog.records) == 1
    record = json.loads(caplog.records[0].args[0])
    assert record["method"] == "GET"
    assert record["path"] == "/api/sessions"
    assert record["current_stage"] == "all_sessions.read_index"
    assert record["elapsed_ms"] >= 0
    assert any(stage["name"] == "all_sessions.read_index" for stage in record["stages"])
    assert record["thread_stacks"]


def test_request_diagnostics_maybe_start_is_limited_to_issue1855_paths():
    assert RequestDiagnostics.maybe_start("GET", "/api/sessions") is not None
    assert RequestDiagnostics.maybe_start("POST", "/api/chat/start") is not None
    assert RequestDiagnostics.maybe_start("GET", "/health") is None
    assert RequestDiagnostics.maybe_start("POST", "/api/session/new") is None


def test_all_sessions_reports_internal_index_stages(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda sessions: None)
    models.SESSIONS.clear()

    s = Session(
        session_id="issue1855_indexed",
        title="Indexed",
        messages=[{"role": "user", "content": "hi", "timestamp": 100}],
    )
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False), encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "updated_at": s.updated_at,
                    "workspace": s.workspace,
                    "model": s.model,
                    "message_count": 1,
                    "created_at": s.created_at,
                    "pinned": False,
                    "archived": False,
                    "last_message_at": 100,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    diag = _StageRecorder()
    rows = models.all_sessions(diag=diag)

    assert [row["session_id"] for row in rows] == [s.session_id]
    assert "all_sessions.read_index" in diag.stages
    assert "all_sessions.overlay_lock" in diag.stages
    assert "all_sessions.lineage_metadata" in diag.stages


def test_issue1855_target_routes_are_wired_to_diagnostics():
    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'RequestDiagnostics.maybe_start("GET", parsed.path' in src
    assert "all_sessions(diag=diag, include_lineage_metadata=False)" in src
    assert 'RequestDiagnostics.maybe_start("POST", parsed.path' in src
    assert "_handle_chat_start(handler, body, diag=diag)" in src
    for stage in (
        "read_body",
        "resolve_model_provider",
        "session_lock_wait",
        "save_pending_state",
        "stream_registration",
        "worker_thread_start",
        "response_write",
    ):
        assert stage in src


def test_session_canonical_resolution_stage_precedes_resolver_and_survives_early_return(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes

    events = []

    class RouteDiag:
        def stage(self, name):
            events.append(f"stage:{name}")

        def finish(self):
            events.append("finish")

    db_path = tmp_path / "state.db"
    row = MappingProxyType(
        {
            "id": "tip",
            "source": "webui",
            "title": "private title",
            "started_at": 1,
            "message_count": 1,
            "archived": False,
            "pinned": False,
        }
    )
    resolution = SharedSessionResolution(
        requested_id="root",
        canonical_id="tip",
        root_id="root",
        tip_id="tip",
        member_ids=("root", "tip"),
        canonical_row=row,
        lineage_fingerprint="sha256:test",
        global_projection_generation_hint=1,
        mode="navigation",
        status="found",
        database_identity=shared_state_db_identity(db_path),
    )
    monkeypatch.setattr(
        routes.RequestDiagnostics,
        "maybe_start",
        staticmethod(lambda *_args, **_kwargs: RouteDiag()),
    )
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)

    def resolve(_db_path, _sid):
        events.append("resolve_shared_session")
        return resolution

    def get_session(_sid, metadata_only=False):
        events.append("get_session")
        return SimpleNamespace(profile="other")

    monkeypatch.setattr(routes, "resolve_shared_session", resolve)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy resolver must not run")
        ),
    )
    monkeypatch.setattr(routes, "get_session", get_session)
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda _profile, _handler: False,
    )
    captured = {}

    def respond(_handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return payload

    monkeypatch.setattr(routes, "j", respond)
    handler = SimpleNamespace(_safe_webui_print=lambda _message: None)

    routes.handle_get(
        handler,
        urlparse("/api/session?session_id=root&messages=0&resolve_model=0"),
    )

    assert captured["status"] == 409
    assert events.index("stage:canonical_resolution") < events.index(
        "resolve_shared_session"
    )
    assert events.index("resolve_shared_session") < events.index("get_session")
    assert events.index("get_session") < events.index("finish")
