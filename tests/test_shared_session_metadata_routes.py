from types import SimpleNamespace


def _capture_json(monkeypatch, routes):
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )
    return captured


class _ClaimedSession:
    def __init__(self, sid="shared-cli", profile="work"):
        self.session_id = sid
        self.profile = profile
        self.title = "CLI conversation"
        self.workspace = "/work"
        self.model = "model"
        self.messages = [{"role": "user", "content": "hello"}]
        self.pinned = False
        self.archived = False
        self.source_tag = "cli"
        self.raw_source = "cli"
        self.session_source = "cli"
        self.source_label = "CLI"
        self.is_cli_session = True
        self.read_only = False
        self.saved = 0

    def save(self, **_kwargs):
        self.saved += 1

    def compact(self, **_kwargs):
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "title": self.title,
            "workspace": self.workspace,
            "message_count": len(self.messages),
            "pinned": self.pinned,
            "archived": self.archived,
            "source_tag": self.source_tag,
            "raw_source": self.raw_source,
            "session_source": self.session_source,
            "source_label": self.source_label,
            "is_cli_session": self.is_cli_session,
        }


def test_pin_materializes_claimable_interactive_session_and_syncs_profile(monkeypatch):
    import api.routes as routes
    import api.state_sync as state_sync

    sid = "shared-cli"
    claimed = _ClaimedSession(sid=sid, profile="work")
    captured = {}
    sync_calls = []
    published = []

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": sid, "pinned": True},
    )
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: (_ for _ in ()).throw(KeyError(_sid)))
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda _sid: (claimed, "materialized"),
    )
    monkeypatch.setattr(routes, "all_sessions", lambda: [])
    monkeypatch.setattr(routes, "_visible_pinned_lineage_ids", lambda _rows: set())
    monkeypatch.setattr(routes, "load_settings", lambda: {"pinned_sessions_limit": 3})
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda reason, **kwargs: published.append((reason, kwargs)),
    )
    monkeypatch.setattr(
        state_sync,
        "sync_session_pinned",
        lambda session_id, pinned, profile=None: sync_calls.append(
            (session_id, pinned, profile)
        ),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: captured.update(
            payload={"error": message},
            status=status,
        )
        or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/pin")) is True

    assert captured["status"] == 200
    assert claimed.pinned is True
    assert claimed.saved >= 1
    assert sync_calls == [(sid, True, "work")]
    assert published == [
        ("session_pin", {"profile": "work", "session_id": sid})
    ]


def test_shared_patch_writes_all_metadata_and_invalidates_profile(monkeypatch):
    import api.routes as routes
    import api.state_sync as state_sync

    before = {
        "id": "tip",
        "canonical_session_id": "tip",
        "source": "cli",
        "title": "Before",
        "cwd": "/before",
        "archived": False,
        "pinned": False,
        "model": "model",
        "message_count": 2,
    }
    captured = {}
    sync_calls = []
    cleared = []
    published = []
    details = iter(
        [
            before,
            {
                **before,
                "title": "After",
                "cwd": "/after",
                "archived": True,
                "pinned": True,
            },
        ]
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {
            "title": "After",
            "cwd": "/after",
            "archived": True,
            "pinned": True,
        },
    )
    monkeypatch.setattr(routes, "_shared_session_detail_payload", lambda _sid: next(details))
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: value)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        state_sync,
        "sync_session_metadata",
        lambda **kwargs: sync_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(routes, "_clear_session_list_cache", lambda profile=None: cleared.append(profile))
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda reason, **kwargs: published.append((reason, kwargs)),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    assert routes.handle_patch(object(), SimpleNamespace(path="/api/sessions/tip")) is True

    assert captured["status"] == 200
    assert sync_calls == [
        {
            "session_id": "tip",
            "title": "After",
            "cwd": "/after",
            "archived": True,
            "pinned": True,
            "profile": "work",
        }
    ]
    assert cleared == ["work"]
    assert published == [
        ("session_shared_metadata", {"profile": "work", "session_id": "tip"})
    ]


def test_title_metadata_sync_never_calls_absolute_usage_writer(monkeypatch):
    import api.routes as routes
    import api.state_sync as state_sync

    calls = []
    monkeypatch.setattr(
        state_sync,
        "sync_session_usage",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only action must not overwrite usage")
        ),
    )
    monkeypatch.setattr(
        state_sync,
        "sync_session_metadata",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    routes._sync_session_title_to_insights(
        SimpleNamespace(
            session_id="shared-cli",
            profile="work",
            title="Renamed",
            workspace="/work",
            archived=True,
        )
    )

    assert calls == [
        {
            "session_id": "shared-cli",
            "title": "Renamed",
            "cwd": "/work",
            "archived": True,
            "profile": "work",
        }
    ]


def test_shared_patch_rejects_non_interactive_owner(monkeypatch):
    import api.routes as routes

    captured = _capture_json(monkeypatch, routes)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"title": "Nope"})
    monkeypatch.setattr(
        routes,
        "_shared_session_detail_payload",
        lambda _sid: {
            "id": "cron_owned",
            "canonical_session_id": "cron_owned",
            "source": "cron",
            "title": "Scheduled run",
            "cwd": "/work",
            "archived": False,
            "pinned": False,
        },
    )

    assert routes.handle_patch(
        object(), SimpleNamespace(path="/api/sessions/cron_owned")
    ) is True
    assert captured["status"] == 403
    assert "interactive" in captured["payload"]["error"].lower()


def test_shared_patch_returns_503_when_canonical_write_fails(monkeypatch):
    import api.routes as routes
    import api.state_sync as state_sync

    before = {
        "id": "shared_cli",
        "canonical_session_id": "shared_cli",
        "source": "cli",
        "title": "Before",
        "cwd": "/work",
        "archived": False,
        "pinned": False,
    }
    captured = _capture_json(monkeypatch, routes)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"title": "After"})
    monkeypatch.setattr(routes, "_shared_session_detail_payload", lambda _sid: before)
    monkeypatch.setattr(routes, "_shared_session_sidecar", lambda _sid: None)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: value)
    monkeypatch.setattr(state_sync, "sync_session_metadata", lambda **_kwargs: False)

    assert routes.handle_patch(
        object(), SimpleNamespace(path="/api/sessions/shared_cli")
    ) is True
    assert captured["status"] == 503


def test_shared_patch_rejects_interactive_row_marked_read_only(monkeypatch):
    import api.routes as routes

    captured = _capture_json(monkeypatch, routes)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"archived": True})
    monkeypatch.setattr(
        routes,
        "_shared_session_detail_payload",
        lambda _sid: {
            "id": "readonly_cli",
            "canonical_session_id": "readonly_cli",
            "source": "cli",
            "title": "Imported",
            "cwd": "/work",
            "archived": False,
            "pinned": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "_shared_session_sidecar",
        lambda _sid: SimpleNamespace(
            read_only=True,
            source_tag="cli",
            raw_source="cli",
            session_source="cli",
        ),
    )

    assert routes.handle_patch(
        object(), SimpleNamespace(path="/api/sessions/readonly_cli")
    ) is True
    assert captured["status"] == 403


def test_all_profile_direct_mutations_switch_to_row_profile_first():
    from pathlib import Path

    src = (
        Path(__file__).parent.parent / "static" / "sessions.js"
    ).read_text(encoding="utf-8")
    rename = src[src.index("function _buildSessionRenameStarter"):src.index("function _appendSessionCopyLinkAction")]
    archive = src[src.index("async function _archiveSession"):src.index("function _openSessionActionMenu")]
    pin_start = src.index("await api('/api/session/pin'")
    pin = src[src.rfind("async()=>", 0, pin_start):pin_start]

    assert "await _ensureSidebarSessionProfile(session)" in rename
    assert "await _ensureSidebarSessionProfile(session)" in archive
    assert "await _ensureSidebarSessionProfile(session)" in pin
