import json

from tests.test_bounded_session_detail_routes import (
    _BrowserSession,
    _resolution,
    _run_browser_session_route,
)
from tests.test_state_db_message_cursor_reader import _insert, _make_db, _resolution as _db_resolution


def _legacy_transcript(db_path):
    import api.routes as routes
    from api.models import merge_session_messages_append_only
    from api.session_history import read_resolved_session_history

    messages = read_resolved_session_history(
        db_path=db_path,
        member_ids=("root", "tip"),
        require_available=True,
    )
    merged = merge_session_messages_append_only(
        [],
        messages,
    )
    return routes._messages_for_limited_payload(merged)


def test_shadow_observation_matches_exact_legacy_tail_without_retaining_content(
    tmp_path,
):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    for message_id in range(1, 8):
        _insert(
            db_path,
            message_id,
            "root" if message_id < 4 else "tip",
            "user" if message_id % 2 else "assistant",
            f"secret-{message_id}",
            message_id,
        )

    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=3,
        legacy_messages=_legacy_transcript(db_path),
    )

    assert observation.matched is True
    assert observation.mode == "cursor_v1"
    assert observation.fallback_reason is None
    assert observation.raw_rows_examined <= 256
    diagnostic = observation.as_diagnostic()
    assert set(diagnostic) == {
        "stage",
        "mode",
        "matched",
        "fallback_reason",
        "visible_count",
        "raw_rows_examined",
        "serialized_bytes",
        "sql_count",
        "query_plan_indexed",
    }
    assert "secret" not in json.dumps(diagnostic)


def test_shadow_semantic_difference_is_reported_without_changing_payload(
    tmp_path,
):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    _insert(db_path, 1, "tip", "user", "canonical", 1)
    legacy = _legacy_transcript(db_path)
    legacy[0] = dict(legacy[0], content="edited-sidecar")

    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=1,
        legacy_messages=legacy,
    )

    assert observation.matched is False
    assert observation.fallback_reason == "semantic_mismatch"
    assert "canonical" not in json.dumps(observation.as_diagnostic())
    assert "edited-sidecar" not in json.dumps(observation.as_diagnostic())


def test_shadow_checks_duplicates_below_initial_page(tmp_path):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    _insert(db_path, 1, "root", "user", "duplicate", 1)
    _insert(db_path, 2, "tip", "user", "duplicate", 1)
    for message_id in range(3, 7):
        _insert(
            db_path,
            message_id,
            "tip",
            "assistant",
            f"newer-{message_id}",
            message_id,
        )

    legacy = _legacy_transcript(db_path)
    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=2,
        legacy_messages=legacy,
    )

    assert len(legacy) == 5
    assert observation.matched is False
    assert observation.fallback_reason == "semantic_mismatch"


def test_shadow_schema_degradation_records_only_typed_reason(tmp_path):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    db_path.touch()
    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=1,
        legacy_messages=[],
    )

    assert observation.matched is None
    assert observation.mode == "legacy_required"
    assert observation.fallback_reason in {"read_failed", "missing_sessions_table"}
    assert "messages" not in observation.as_diagnostic()


def test_shadow_route_records_bounded_diagnostics_and_keeps_legacy_response(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes
    from api.session_message_paging import MessagePageShadowObservation

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    history = [
        {"role": "user", "content": "never-log-this", "timestamp": 1},
        {"role": "assistant", "content": "tail", "timestamp": 2},
    ]
    observations = []

    def evaluate(**kwargs):
        observations.append(kwargs)
        return MessagePageShadowObservation(
            mode="cursor_v1",
            matched=True,
            fallback_reason=None,
            visible_count=2,
            raw_rows_examined=2,
            serialized_bytes=120,
            sql_count=3,
            query_plan_indexed=True,
        )

    logs = []
    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "shadow")
    monkeypatch.setattr(routes, "evaluate_message_page_shadow", evaluate)
    monkeypatch.setattr(
        routes.logger,
        "info",
        lambda message, payload: logs.append(message % payload),
    )
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

    assert captured["status"] == 200
    session = captured["payload"]["session"]
    assert session["messages"] == history
    assert session["message_page"] == {
        "mode": "legacy",
        "fallback_reason": "receipt_unavailable",
    }
    assert len(observations) == 1
    assert observations[0]["legacy_messages"] == history
    assert len(logs) == 1
    assert "never-log-this" not in logs[0]
    assert '"matched": true' in logs[0]


def test_on_and_off_modes_do_not_run_shadow_reader(tmp_path, monkeypatch):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    history = [{"role": "user", "content": "one", "timestamp": 1}]
    monkeypatch.setattr(
        routes,
        "evaluate_message_page_shadow",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("shadow reader must not run")
        ),
        raising=False,
    )

    for gate in ("off", "on"):
        monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", gate)
        captured, _resolve_calls, _get_calls = _run_browser_session_route(
            monkeypatch,
            db_path=db_path,
            resolution=resolution,
            session_or_error=_BrowserSession(),
            query=(
                "session_id=root&messages=1&resolve_model=0&msg_limit=1"
                "&message_paging=cursor_v1"
            ),
            history_reader=lambda **_kwargs: history,
        )
        assert captured["status"] == 200
