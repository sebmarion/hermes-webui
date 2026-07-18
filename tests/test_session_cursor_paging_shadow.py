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


def test_shadow_exact_match_consumer_receives_normalized_tuples_and_sequence_counts(
    tmp_path,
    monkeypatch,
):
    import api.session_message_paging as paging

    page = paging.StateDBMessagePage(
        mode="cursor_v1",
        messages=(
            {
                "role": "user",
                "content": "local-only",
                "_state_db_message_id": 1,
                "_content_original_chars": 10,
                "_content_original_bytes": 10,
            },
        ),
        before_boundaries=(),
        has_more=False,
        visible_count=97,
        raw_rows_examined=1,
        serialized_bytes=10,
        sql_count=1,
        query_plan_indexed=True,
    )
    monkeypatch.setattr(
        paging,
        "read_state_db_message_page",
        lambda **_kwargs: page,
    )
    received = []

    def consume(candidate, oracle, candidate_display_count, oracle_display_count):
        received.append(
            (candidate, oracle, candidate_display_count, oracle_display_count)
        )

    observation = paging.evaluate_message_page_shadow(
        db_path=tmp_path / "state.db",
        resolution=object(),
        visible_limit=1,
        legacy_messages=[{"role": "user", "content": "local-only"}],
        on_exact_match=consume,
    )

    assert observation.matched is True
    assert observation.visible_count == 97
    assert len(received) == 1
    candidate, oracle, candidate_count, oracle_count = received[0]
    assert candidate == oracle
    assert candidate_count == len(candidate) == 1
    assert oracle_count == len(oracle) == 1
    assert "local-only" not in json.dumps(observation.as_diagnostic())


def test_shadow_exact_match_consumer_exception_is_observational(tmp_path):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    _insert(db_path, 1, "tip", "user", "local-only", 1)

    def failing_consumer(*_args):
        raise RuntimeError("local-only")

    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=1,
        legacy_messages=_legacy_transcript(db_path),
        on_exact_match=failing_consumer,
    )

    assert observation.matched is True
    assert observation.fallback_reason is None
    assert "local-only" not in json.dumps(observation.as_diagnostic())


def test_shadow_semantic_difference_is_reported_without_changing_payload(
    tmp_path,
):
    from api.session_message_paging import evaluate_message_page_shadow

    db_path = tmp_path / "state.db"
    _make_db(db_path)
    _insert(db_path, 1, "tip", "user", "canonical", 1)
    legacy = _legacy_transcript(db_path)
    legacy[0] = dict(legacy[0], content="edited-sidecar")
    consumer_calls = []

    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=1,
        legacy_messages=legacy,
        on_exact_match=lambda *_args: consumer_calls.append(True),
    )

    assert observation.matched is False
    assert observation.fallback_reason == "semantic_mismatch"
    assert consumer_calls == []
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
    consumer_calls = []
    observation = evaluate_message_page_shadow(
        db_path=db_path,
        resolution=_db_resolution(db_path),
        visible_limit=1,
        legacy_messages=[],
        on_exact_match=lambda *_args: consumer_calls.append(True),
    )

    assert observation.matched is None
    assert observation.mode == "legacy_required"
    assert observation.fallback_reason in {"read_failed", "missing_sessions_table"}
    assert consumer_calls == []
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


def test_shadow_route_exact_match_bootstraps_without_changing_legacy_response(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes
    from api.session_message_paging import MessagePageShadowObservation
    from types import SimpleNamespace

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    history = [
        {"role": "user", "content": "one", "timestamp": 1},
        {"role": "assistant", "content": "two", "timestamp": 2},
    ]
    publications = []
    comparison_epoch = SimpleNamespace(profile="default")

    def evaluate(**kwargs):
        kwargs["on_exact_match"](
            tuple(history),
            tuple(history),
            len(history),
            len(history),
        )
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

    monkeypatch.setenv("HERMES_WEBUI_MESSAGE_CURSOR_V1", "shadow")
    monkeypatch.setattr(routes, "evaluate_message_page_shadow", evaluate)
    monkeypatch.setattr(
        routes,
        "_bounded_runtime_owner_absent",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        routes,
        "_capture_exact_shadow_comparison_epoch",
        lambda **_kwargs: comparison_epoch,
    )
    monkeypatch.setattr(
        routes,
        "_publish_exact_shadow_settlement_for_route",
        lambda **kwargs: publications.append(kwargs) or "published_and_recorded",
    )
    monkeypatch.setattr(routes.logger, "info", lambda *_args, **_kwargs: None)

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
    assert captured["payload"]["session"]["messages"] == history
    assert captured["payload"]["session"]["message_page"]["mode"] == "legacy"
    assert len(publications) == 1
    assert publications[0]["comparison_epoch"] is comparison_epoch
    assert publications[0]["candidate_messages"] == tuple(history)
    assert publications[0]["oracle_messages"] == tuple(history)
    assert publications[0]["candidate_count"] == 2
    assert publications[0]["oracle_count"] == 2


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
