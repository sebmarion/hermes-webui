"""Regression coverage for parent-only /api/sessions sidebar payloads."""

from api import models, routes


def _rows():
    base = {
        "profile": "default",
        "archived": False,
        "message_count": 2,
        "updated_at": 100,
        "last_message_at": 100,
    }
    return [
        {
            **base,
            "session_id": "root",
            "title": "Root conversation",
            "source": "webui",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        },
        {
            **base,
            "session_id": "compressed-root",
            "title": "Compressed conversation",
            "source": "webui",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "pinned": True,
        },
        {
            **base,
            "session_id": "compression-tip",
            "title": "Compressed conversation",
            "source": "webui",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "parent_session_id": "compressed-root",
            "_lineage_root_id": "compressed-root",
            "_lineage_tip_id": "compression-tip",
        },
        {
            **base,
            "session_id": "child",
            "title": "Child conversation",
            "source": "webui",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "parent_session_id": "root",
            "relationship_type": "child_session",
        },
        {
            **base,
            "session_id": "tool-row",
            "title": "Tool Session",
            "source": "tool",
            "source_tag": "tool",
            "raw_source": "tool",
            "session_source": "tool",
        },
        {
            **base,
            "session_id": "continuation-control",
            "title": "Repeated parent title",
            "source": "tool_limit_continuation",
            "source_tag": "tool_limit_continuation",
            "raw_source": "tool_limit_continuation",
            "session_source": "tool_limit_continuation",
            "parent_session_id": "root",
        },
    ]


def _build_warm_payload(monkeypatch):
    rows = _rows()
    monkeypatch.setenv("HERMES_WEBUI_SESSION_PROJECTION_V2", "0")
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(
        routes,
        "_schedule_stale_stream_state_reconciliation",
        lambda _rows: False,
    )
    monkeypatch.setattr(
        routes,
        "_prune_orphaned_webui_zero_message_sessions",
        lambda candidate_rows, diag_stage=None: list(candidate_rows),
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    return routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=False,
        visible_only=True,
        show_webhook_sessions=False,
    )


def test_warm_sidebar_payload_is_parent_only_and_drops_control_rows(monkeypatch):
    payload = _build_warm_payload(monkeypatch)

    assert [row["session_id"] for row in payload["sessions"]] == [
        "root",
        "compression-tip",
    ]
    assert {row["session_id"] for row in payload["sidebar_reference_sessions"]} == {
        "compressed-root",
        "child",
    }
    tip = next(row for row in payload["sessions"] if row["session_id"] == "compression-tip")
    assert tip["pinned"] is True
    compressed_root = next(
        row
        for row in payload["sidebar_reference_sessions"]
        if row["session_id"] == "compressed-root"
    )
    assert compressed_root["pinned"] is False
    assert payload["webui_session_count"] == 2
    assert payload["cli_session_count"] == 0


def test_cold_seed_payload_applies_the_same_parent_only_contract(monkeypatch):
    rows = _rows()
    monkeypatch.setattr(models, "read_session_index_projection", lambda: list(rows))
    monkeypatch.setattr(
        models,
        "_apply_session_index_state_db_overrides",
        lambda candidate_rows, all_profiles=False: None,
    )
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)

    payload = routes._build_session_list_seed_payload(
        active_profile="default",
        all_profiles=False,
        include_archived=False,
        exclude_hidden=False,
        sidebar_source=None,
        archived_limit=None,
        archived_offset=0,
    )

    assert [row["session_id"] for row in payload["sessions"]] == [
        "root",
        "compression-tip",
    ]
    assert {row["session_id"] for row in payload["sidebar_reference_sessions"]} == {
        "compressed-root",
        "child",
    }
    tip = next(row for row in payload["sessions"] if row["session_id"] == "compression-tip")
    assert tip["pinned"] is True
    assert payload["webui_session_count"] == 2
    assert payload["cli_session_count"] == 0


def test_child_reference_rows_remain_reference_only_in_serialized_response(monkeypatch):
    monkeypatch.setattr(routes, "load_settings", lambda: {"api_redact_enabled": False})
    body = routes._session_list_payload_to_response(_build_warm_payload(monkeypatch))

    assert [row["session_id"] for row in body["sessions"]] == [
        "root",
        "compression-tip",
    ]
    assert {row["session_id"] for row in body["sidebar_reference_sessions"]} == {
        "compressed-root",
        "child",
    }
    assert all(
        row["_sidebar_reference_only"] is True
        for row in body["sidebar_reference_sessions"]
    )


def test_parent_only_relationship_probe_skips_message_stats(monkeypatch):
    calls = []

    def _fake_lineage_metadata(_db_path, session_ids, *, include_message_stats=True):
        calls.append((set(session_ids), include_message_stats))
        return {
            "legacy-child": {
                "parent_session_id": "root",
                "relationship_type": "child_session",
            }
        }

    monkeypatch.setattr(routes, "read_session_lineage_metadata", _fake_lineage_metadata)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: "/tmp/state.db")
    rows = [
        {
            "session_id": "legacy-child",
            "source": "webui",
            "parent_session_id": "root",
        }
    ]

    routes._enrich_parent_only_sidebar_relationships(rows)

    assert calls == [({"legacy-child"}, False)]
    assert rows[0]["relationship_type"] == "child_session"
