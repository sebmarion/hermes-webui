"""Bounded-resolution contracts for compatibility session detail routes."""

import json
from types import MappingProxyType, SimpleNamespace

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

    def read_history(*, db_path, member_ids, include_inactive=False):
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
