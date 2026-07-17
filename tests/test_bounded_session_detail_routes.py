"""Bounded-resolution contracts for compatibility session detail routes."""

import json
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlparse

import pytest

from api.agent_sessions import SharedSessionResolution, shared_state_db_identity


def _resolution(
    *,
    db_path,
    requested_id="root",
    canonical_id="tip",
    row=None,
    status="found",
):
    if row is None and status == "found":
        row = {
            "id": canonical_id,
            "title": "Canonical title",
            "source": "webui",
            "started_at": 10,
            "last_activity": 30,
            "message_count": 2,
            "model": "canonical-model",
            "cwd": "/canonical",
            "archived": False,
            "pinned": True,
            "parent_session_id": "middle",
            "_lineage_root_id": "root",
            "_lineage_tip_id": "tip",
            "_compression_segment_count": 3,
        }
    members = ("root", "middle", "tip") if status == "found" else (requested_id,)
    return SharedSessionResolution(
        requested_id=requested_id,
        canonical_id=canonical_id,
        root_id="root" if status == "found" else requested_id,
        tip_id=canonical_id,
        member_ids=members,
        canonical_row=MappingProxyType(dict(row)) if row is not None else None,
        lineage_fingerprint="sha256:test",
        global_projection_generation_hint=1,
        mode="navigation",
        status=status,
        database_identity=shared_state_db_identity(db_path),
    )


def _forbidden(label):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{label} must not run")

    return fail


def test_metadata_detail_resolves_once_without_collection_or_message_reads(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes

    resolution = _resolution(db_path=tmp_path / "state.db")
    calls = []
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda db_path, sid: calls.append((db_path, sid)) or resolution,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "read_shared_session_rows",
        _forbidden("collection"),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "read_resolved_session_history",
        _forbidden("message history"),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        _forbidden("legacy message history"),
    )
    sidecar_calls = []

    def load_sidecar(sid, *, metadata_only=False):
        sidecar_calls.append((sid, metadata_only))
        return SimpleNamespace(
            title="Stale sidecar title",
            workspace="/stale",
            model="stale-model",
            messages=[{"role": "user", "content": "must not load"}],
        )

    monkeypatch.setattr(routes, "_shared_session_sidecar", load_sidecar)

    payload = routes._shared_session_detail_payload("root")

    assert calls == [(tmp_path / "state.db", "root")]
    assert sidecar_calls == [("tip", True)]
    assert payload["requested_session_id"] == "root"
    assert payload["canonical_session_id"] == "tip"
    assert payload["title"] == "Canonical title"
    assert payload["workspace"] == "/canonical"
    assert payload["pinned"] is True
    assert payload["lineage"] == {
        "root_id": "root",
        "tip_id": "tip",
        "segment_count": 3,
    }
    assert "messages" not in payload


def test_supplied_resolution_reuses_members_for_message_history(tmp_path, monkeypatch):
    import api.routes as routes

    resolution = _resolution(db_path=tmp_path / "state.db")
    history_calls = []
    sidecar_messages = [{"role": "user", "content": "sidecar", "timestamp": 1}]
    state_messages = [{"role": "assistant", "content": "state", "timestamp": 2}]
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        _forbidden("duplicate resolution"),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "read_shared_session_rows",
        _forbidden("collection"),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        _forbidden("legacy history reader"),
    )

    def read_history(
        *,
        db_path,
        member_ids,
        include_inactive=False,
        require_available=False,
    ):
        history_calls.append((db_path, tuple(member_ids), include_inactive))
        return state_messages

    monkeypatch.setattr(
        routes,
        "read_resolved_session_history",
        read_history,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda sid, **_kwargs: (
            SimpleNamespace(messages=sidecar_messages) if sid == "tip" else None
        ),
    )
    monkeypatch.setattr(
        routes,
        "merge_session_messages_append_only",
        lambda sidecar, state: [*sidecar, *state],
    )

    payload = routes._shared_session_detail_payload(
        "root",
        include_messages=True,
        resolution=resolution,
    )

    assert history_calls == [
        (tmp_path / "state.db", ("root", "middle", "tip"), False)
    ]
    assert payload["messages"] == [*sidecar_messages, *state_messages]


def test_shared_detail_uses_legacy_reader_when_bounded_schema_unavailable(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes
    from api.session_history import ResolvedSessionHistoryUnavailable

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    legacy_messages = [{"role": "user", "content": "legacy", "timestamp": 1}]
    legacy_calls = []

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    monkeypatch.setattr(
        routes,
        "read_resolved_session_history",
        lambda **_kwargs: (_ for _ in ()).throw(
            ResolvedSessionHistoryUnavailable("unsupported_schema")
        ),
    )

    def legacy_reader(sid, **kwargs):
        legacy_calls.append((sid, kwargs))
        return legacy_messages

    monkeypatch.setattr(routes, "get_state_db_session_messages", legacy_reader)
    monkeypatch.setattr(routes, "_shared_session_sidecar", lambda *_args, **_kwargs: None)

    payload = routes._shared_session_detail_payload(
        "root",
        include_messages=True,
        resolution=resolution,
    )

    assert legacy_calls == [
        ("tip", {"stitch_continuations": True, "compression_only": True})
    ]
    assert payload["messages"] == legacy_messages


def test_lazy_import_re_resolves_only_the_requested_session(tmp_path, monkeypatch):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    missing = _resolution(
        db_path=db_path,
        canonical_id="root",
        row=None,
        status="missing",
    )
    found = _resolution(db_path=db_path)
    resolutions = iter((missing, found))
    resolve_calls = []
    import_calls = []
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")

    def resolve(db_path, sid):
        resolve_calls.append((db_path, sid))
        return next(resolutions)

    monkeypatch.setattr(routes, "resolve_shared_session", resolve, raising=False)
    monkeypatch.setattr(
        routes,
        "read_shared_session_rows",
        _forbidden("collection"),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda _sid, **_kwargs: None,
    )
    monkeypatch.setattr(
        routes,
        "_lazy_import_legacy_webui_session",
        lambda sid: import_calls.append(sid) or True,
    )

    payload = routes._shared_session_detail_payload("root")

    assert resolve_calls == [
        (tmp_path / "state.db", "root"),
        (tmp_path / "state.db", "root"),
    ]
    assert import_calls == ["root"]
    assert payload["canonical_session_id"] == "tip"


@pytest.mark.parametrize("status", ["degraded", "ambiguous"])
def test_unproven_resolution_never_triggers_legacy_import(
    status,
    tmp_path,
    monkeypatch,
):
    import api.routes as routes

    resolution = _resolution(
        db_path=tmp_path / "state.db",
        canonical_id="root",
        row=None,
        status=status,
    )
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda _db_path, _sid: resolution,
    )
    monkeypatch.setattr(
        routes,
        "_lazy_import_legacy_webui_session",
        _forbidden("legacy import"),
    )

    assert routes._shared_session_detail_payload("root") is None


def test_metadata_sidecar_reader_never_falls_back_to_full_parse(tmp_path, monkeypatch):
    import api.routes as routes

    sidecar_path = tmp_path / "tip.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "session_id": "tip",
                "title": "Bounded metadata",
                "workspace": str(tmp_path),
                "model": "test-model",
                "created_at": 1,
                "updated_at": 2,
                "messages": [
                    {"role": "user", "content": "must remain unread"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes.Session, "load", _forbidden("full sidecar parse"))
    monkeypatch.setattr(
        routes.Session,
        "load_metadata_only",
        _forbidden("fallback metadata loader"),
    )

    sidecar = routes._shared_session_sidecar("tip", metadata_only=True)

    assert sidecar.title == "Bounded metadata"
    assert sidecar.messages == []


def test_supplied_resolution_is_bound_to_active_database(tmp_path, monkeypatch):
    import api.routes as routes

    active_db = tmp_path / "active" / "state.db"
    foreign_db = tmp_path / "foreign" / "state.db"
    foreign = _resolution(db_path=foreign_db)
    local_row = dict(foreign.canonical_row)
    local_row["title"] = "Active profile title"
    local = _resolution(db_path=active_db, row=local_row)
    calls = []
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: active_db)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda db_path, sid: calls.append((db_path, sid)) or local,
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda _sid, **_kwargs: None,
    )

    payload = routes._shared_session_detail_payload("root", resolution=foreign)

    assert calls == [(active_db, "root")]
    assert payload["title"] == "Active profile title"


def test_zero_message_rows_remain_undisclosed(tmp_path, monkeypatch):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    row = {
        "id": "empty",
        "title": "Empty internal row",
        "source": "webui",
        "started_at": 1,
        "message_count": 0,
        "actual_message_count": 0,
        "archived": False,
        "pinned": False,
    }
    resolution = _resolution(
        db_path=db_path,
        requested_id="empty",
        canonical_id="empty",
        row=row,
    )
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda _db_path, _sid: resolution,
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        _forbidden("zero-row sidecar"),
    )

    assert routes._shared_session_detail_payload("empty") is None


class _BrowserSession:
    def __init__(self, *, profile="default", messages=None):
        self.session_id = "tip"
        self.profile = profile
        self.title = "Stale sidecar title"
        self.workspace = "/stale-sidecar"
        self.model = "sidecar-model"
        self.model_provider = None
        self.created_at = 1
        self.updated_at = 2
        self.messages = list(messages or [])
        self.tool_calls = []
        self.project_id = None
        self.is_cli_session = True
        self.source_tag = "cli"
        self.raw_source = "cli"
        self.session_source = "cli"
        self.source_label = "CLI"
        self.read_only = False
        self.archived = True
        self.pinned = False
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.context_length = 1
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.anchor_activity_scenes = []
        self.truncation_watermark = None
        self.truncation_boundary = None

    def compact(self, **_kwargs):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "profile": self.profile,
            "archived": self.archived,
            "pinned": self.pinned,
        }


def _run_browser_session_route(
    monkeypatch,
    *,
    db_path,
    resolution,
    session_or_error,
    query,
    visible=True,
    visibility_handler=None,
    history_reader=None,
    legacy_history_reader=None,
    claim_handler=None,
):
    import api.routes as routes

    captured = {}
    resolve_calls = []
    get_calls = []

    def resolve(path, sid):
        resolve_calls.append((path, sid))
        return resolution

    def get_session(sid, metadata_only=False):
        get_calls.append((sid, metadata_only))
        if isinstance(session_or_error, BaseException):
            raise session_or_error
        return session_or_error

    def respond(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        return payload

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    monkeypatch.setattr(routes, "resolve_shared_session", resolve)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session_id",
        _forbidden("legacy canonical resolver"),
    )
    monkeypatch.setattr(
        routes,
        "read_shared_session_rows",
        _forbidden("collection projection"),
        raising=False,
    )
    monkeypatch.setattr(routes, "get_session", get_session)
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        visibility_handler or (lambda _profile, _handler: visible),
    )
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: None)
    monkeypatch.setattr(
        routes,
        "_session_requires_cli_metadata_lookup",
        lambda _session: False,
    )
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        _forbidden("legacy collection metadata fallback"),
    )
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        _forbidden("CLI session collection"),
    )
    monkeypatch.setattr(routes, "get_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda _value: False)
    monkeypatch.setattr(
        routes,
        "_metadata_only_message_summary",
        lambda _sid, profile=None: {"message_count": 2, "last_message_at": 30},
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        legacy_history_reader or _forbidden("legacy state history"),
    )
    monkeypatch.setattr(
        routes,
        "read_resolved_session_history",
        history_reader or _forbidden("resolved state history"),
    )
    monkeypatch.setattr(
        routes,
        "_webui_sidecar_lineage_messages_for_display",
        lambda _session: [],
    )
    monkeypatch.setattr(
        routes,
        "_merged_webui_lineage_messages_for_display",
        lambda _session, messages: list(messages),
    )
    monkeypatch.setattr(
        routes,
        "_hydrate_anchor_activity_scenes",
        lambda messages, *_args, **_kwargs: list(messages),
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        routes,
        "_pre_compression_continuation_session_id",
        lambda _session: None,
    )
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "attach_todo_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        claim_handler
        or (lambda _sid, cli_meta=None, **_kwargs: (None, "no_foreign_state")),
    )
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(routes, "j", respond)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: respond(
            handler,
            {"error": message},
            status=status,
        ),
    )
    monkeypatch.setattr(
        routes.RequestDiagnostics,
        "maybe_start",
        staticmethod(lambda *_args, **_kwargs: None),
    )

    handler = SimpleNamespace(_safe_webui_print=lambda _message: None)
    routes.handle_get(handler, urlparse(f"/api/session?{query}"))
    return captured, resolve_calls, get_calls


@pytest.mark.parametrize("load_messages", [False, True])
def test_browser_session_reuses_one_resolution_and_state_metadata(
    load_messages,
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    session = _BrowserSession()
    history_calls = []
    state_messages = [
        {"role": "user", "content": "root", "timestamp": 1},
        {"role": "assistant", "content": "middle", "timestamp": 2},
        {"role": "assistant", "content": "tip", "timestamp": 3},
    ]

    def read_history(
        *,
        db_path,
        member_ids,
        include_inactive=False,
        require_available=False,
    ):
        history_calls.append((db_path, tuple(member_ids), include_inactive))
        return state_messages

    query = f"session_id=root&messages={int(load_messages)}&resolve_model=0"
    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=session,
        query=query,
        history_reader=(read_history if load_messages else None),
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", not load_messages)]
    assert captured["status"] == 200
    payload = captured["payload"]["session"]
    assert payload["canonical_session_id"] == "tip"
    assert payload["requested_session_id"] == "root"
    assert payload["title"] == "Canonical title"
    assert payload["workspace"] == "/canonical"
    assert payload["archived"] is False
    assert payload["pinned"] is True
    if load_messages:
        assert history_calls == [
            (db_path, ("root", "middle", "tip"), False)
        ]
        assert payload["messages"] == state_messages
    else:
        assert history_calls == []
        assert payload["messages"] == []


def test_browser_session_profile_mismatch_does_not_disclose_metadata(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(profile="other"),
        query="session_id=root&messages=1&resolve_model=0",
        visible=False,
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", False)]
    assert captured["status"] == 409
    assert set(captured["payload"]) == {"error", "code", "session_id", "profile"}
    assert "Canonical title" not in json.dumps(captured["payload"])


def test_browser_session_missing_id_keeps_requested_404_without_disclosure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(
        db_path=db_path,
        requested_id="missing",
        canonical_id="missing",
        row=None,
        status="missing",
    )
    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=KeyError("missing"),
        query="session_id=missing&messages=1&resolve_model=0",
    )

    assert resolve_calls == [(db_path, "missing")]
    assert get_calls == [("missing", False)]
    assert captured["status"] == 404
    assert captured["payload"] == {"error": "Session not found"}


def test_browser_session_sidecar_miss_passes_receipt_into_synthesized_fallback(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    state_messages = [
        {"role": "user", "content": "root", "timestamp": 1},
        {"role": "assistant", "content": "tip", "timestamp": 2},
    ]
    history_calls = []
    claim_calls = []
    synth = _BrowserSession(messages=state_messages)

    def read_history(
        *,
        db_path,
        member_ids,
        include_inactive=False,
        require_available=False,
    ):
        history_calls.append((db_path, tuple(member_ids), include_inactive))
        return state_messages

    def claim(sid, cli_meta=None, **kwargs):
        claim_calls.append((sid, cli_meta, kwargs))
        return synth, "materialized"

    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=KeyError("tip"),
        query="session_id=root&messages=1&resolve_model=0",
        history_reader=read_history,
        claim_handler=claim,
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", False)]
    assert history_calls == [
        (db_path, ("root", "middle", "tip"), False)
    ]
    assert len(claim_calls) == 1
    sid, _cli_meta, kwargs = claim_calls[0]
    assert sid == "tip"
    assert kwargs["resolved_messages"] == state_messages
    assert kwargs["resolved_state_row"] == dict(resolution.canonical_row)
    payload = captured["payload"]["session"]
    assert payload["canonical_session_id"] == "tip"
    assert payload["requested_session_id"] == "root"
    assert payload["title"] == "Canonical title"
    assert payload["workspace"] == "/canonical"
    assert payload["archived"] is False
    assert payload["pinned"] is True
    assert payload["messages"] == state_messages


def test_browser_session_sidecar_miss_uses_legacy_reader_when_bounded_schema_unavailable(
    tmp_path,
    monkeypatch,
):
    from api.session_history import ResolvedSessionHistoryUnavailable

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    legacy_messages = [{"role": "user", "content": "legacy", "timestamp": 1}]
    claim_calls = []

    def unavailable_history(**_kwargs):
        raise ResolvedSessionHistoryUnavailable("unsupported_schema")

    def claim(sid, cli_meta=None, **kwargs):
        claim_calls.append((sid, cli_meta, kwargs))
        return _BrowserSession(messages=legacy_messages), "materialized"

    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=KeyError("tip"),
        query="session_id=root&messages=1&resolve_model=0",
        history_reader=unavailable_history,
        claim_handler=claim,
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", False)]
    assert len(claim_calls) == 1
    assert claim_calls[0][0] == "tip"
    assert claim_calls[0][2] == {}
    assert captured["payload"]["session"]["messages"] == legacy_messages


def test_browser_session_state_only_metadata_failure_uses_selected_active_profile(
    tmp_path,
    monkeypatch,
):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    checked_profiles = []
    synth = _BrowserSession(messages=[{"role": "user", "content": "state"}])

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "research")

    def visible(profile, _handler):
        checked_profiles.append(profile)
        return profile == "research"

    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=KeyError("tip"),
        query="session_id=root&messages=1&resolve_model=0",
        visibility_handler=visible,
        history_reader=lambda **_kwargs: list(synth.messages),
        claim_handler=lambda _sid, **_kwargs: (synth, "materialized"),
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", False)]
    assert checked_profiles == ["research"]
    assert captured["status"] == 200


def test_browser_session_sidecar_uses_legacy_reader_when_bounded_schema_unavailable(
    tmp_path,
    monkeypatch,
):
    from api.session_history import ResolvedSessionHistoryUnavailable

    db_path = tmp_path / "state.db"
    resolution = _resolution(db_path=db_path)
    legacy_messages = [{"role": "user", "content": "legacy", "timestamp": 1}]
    legacy_calls = []

    def unavailable_history(**_kwargs):
        raise ResolvedSessionHistoryUnavailable("unsupported_schema")

    def legacy_reader(sid, **kwargs):
        legacy_calls.append((sid, kwargs))
        return legacy_messages

    captured, resolve_calls, get_calls = _run_browser_session_route(
        monkeypatch,
        db_path=db_path,
        resolution=resolution,
        session_or_error=_BrowserSession(),
        query="session_id=root&messages=1&resolve_model=0",
        history_reader=unavailable_history,
        legacy_history_reader=legacy_reader,
    )

    assert resolve_calls == [(db_path, "root")]
    assert get_calls == [("tip", False)]
    assert legacy_calls == [("tip", {"profile": "default"})]
    assert captured["payload"]["session"]["messages"] == legacy_messages
