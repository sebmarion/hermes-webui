"""Regression coverage for legacy source filters and the unified conversation list."""

import io
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

import api.profiles as profiles
import api.routes as routes
import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _session_rows(
    webui_count,
    cli_count,
    archived_webui_count=0,
    archived_cli_count=0,
    start=0,
):
    rows = []
    for index in range(webui_count):
        rows.append(
            {
                "session_id": f"webui-{start + index}",
                "title": "WebUI Session",
                "profile": "default",
                "archived": index < archived_webui_count,
                "message_count": 1,
                "updated_at": 1000 + index,
                "last_message_at": 1000 + index,
                "source": "webui",
                "raw_source": "webui",
                "session_source": "webui",
                "source_tag": "webui",
            }
        )
    for index in range(cli_count):
        rows.append(
            {
                "session_id": f"cli-{start + index + 10000}",
                "title": "Imported CLI session",
                "profile": "default",
                "archived": index < archived_cli_count,
                "message_count": 1,
                "updated_at": 2000 + index,
                "last_message_at": 2000 + index,
                "source": "cli",
                "raw_source": "cli",
                "session_source": "cli",
                "source_tag": "cli",
            }
        )
    return rows


def _handle_sessions(url):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


def _extract_function(source_text, function_name):
    marker = f"function {function_name}("
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


def _ensure_async(function_source, function_name):
    if function_source.startswith("async function "):
        return function_source
    return function_source.replace(
        f"function {function_name}",
        f"async function {function_name}",
        1,
    )


def _run_node(script):
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    # This module exercises the reconciled payload filters directly; the v2
    # cold-seed path has separate contract coverage.
    monkeypatch.setenv("HERMES_WEBUI_SESSION_PROJECTION_V2", "0")
    monkeypatch.setattr(
        routes,
        "_get_cached_session_list_payload",
        lambda *, builder, **_kwargs: builder(),
    )
    routes._session_list_cache_clear()
    yield
    routes._session_list_cache_clear()


def _install_common_monkeypatches(monkeypatch, rows):
    enriched = []
    row_ids = {str(row["session_id"]) for row in rows if row.get("session_id")}
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: enriched.append([r["session_id"] for r in rows]))
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: set(row_ids & {str(sid) for sid in ids}))
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    return enriched


def test_sidebar_source_webui_excludes_cli_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    enriched = _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 30
    assert all(r["session_id"].startswith("webui-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20
    assert body["archived_count"] == 0
    expected = {
        row["session_id"] for row in rows
        if not row["archived"] and row["session_id"].startswith("webui-")
    }
    assert set(enriched[0]) == expected


def test_sidebar_source_webui_hides_child_when_archived_parent_is_filtered(monkeypatch):
    rows = [
        {
            "session_id": "webui-parent",
            "title": "Archived parent",
            "profile": "default",
            "archived": True,
            "message_count": 10,
            "updated_at": 1000,
            "last_message_at": 1000,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
        },
        {
            "session_id": "webui-child",
            "title": "Subagent Session",
            "profile": "default",
            "archived": False,
            "message_count": 4,
            "updated_at": 1100,
            "last_message_at": 1100,
            "source": "subagent",
            "raw_source": "subagent",
            "session_source": "other",
            "source_tag": "subagent",
            "parent_session_id": "webui-parent",
            "relationship_type": "child_session",
        },
    ]
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&exclude_hidden=1")

    body = handler.json_body()
    assert handler.status == 200
    assert body["sessions"] == []
    assert body["sidebar_reference_sessions"] == []


def test_sidebar_source_cli_excludes_webui_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=cli")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 20
    assert all(r["session_id"].startswith("cli-") for r in body["sessions"])
    assert body["webui_session_count"] == 30
    assert body["cli_session_count"] == 20


def test_sidebar_source_omitted_returns_all_rows(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions")

    body = handler.json_body()
    assert handler.status == 200
    assert len(body["sessions"]) == 50
    assert len([r for r in body["sessions"] if r["session_id"].startswith("webui-")]) == 30
    assert len([r for r in body["sessions"] if r["session_id"].startswith("cli-")]) == 20


def test_shared_interactive_rows_ignore_legacy_external_setting(monkeypatch):
    rows = _session_rows(webui_count=1, cli_count=1)
    cli_template = rows[-1]
    rows.extend(
        [
            {
                **cli_template,
                "session_id": "tui-shared",
                "source": "tui",
                "raw_source": "tui",
                "source_tag": "tui",
                "source_label": "TUI",
            },
            {
                **cli_template,
                "session_id": "acp-shared",
                "source": "acp",
                "raw_source": "acp",
                "source_tag": "acp",
                "source_label": "ACP",
            },
        ]
    )
    for row in rows:
        row["_shared_interactive"] = True
    _install_common_monkeypatches(monkeypatch, rows)
    monkeypatch.setenv("HERMES_WEBUI_SESSION_PROJECTION_V2", "1")
    monkeypatch.setattr(
        routes,
        "shared_interactive_sidebar_projection",
        lambda sidecars, profile=None, db_path=None: (list(rows), []),
    )
    monkeypatch.setattr(
        routes,
        "_prune_orphaned_webui_zero_message_sessions",
        lambda current, diag_stage=None: list(current),
    )

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    assert {row["session_id"] for row in payload["sessions"]} == {
        "webui-0",
        "cli-10000",
        "tui-shared",
        "acp-shared",
    }


def test_canonical_interactive_rows_are_not_capped_before_archive_paging(monkeypatch):
    rows = _session_rows(
        webui_count=1,
        cli_count=30,
        archived_cli_count=30,
    )
    for row in rows:
        row["_shared_interactive"] = True
    _install_common_monkeypatches(monkeypatch, rows)

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=True,
        archived_limit=25,
    )

    archived = [row for row in payload["sessions"] if row["archived"]]
    assert payload["archived_count"] == 30
    assert len(archived) == 25


def test_all_profiles_projects_interactive_rows_when_external_setting_is_off(monkeypatch):
    sidecars = _session_rows(webui_count=1, cli_count=0)
    canonical = list(sidecars)
    canonical.extend(
        [
            {
                "session_id": "work-tui",
                "title": "Named canonical title",
                "profile": "work",
                "archived": True,
                "pinned": True,
                "message_count": 1,
                "updated_at": 2000,
                "last_message_at": 2000,
                "workspace": "/work",
                "source": "tui",
                "raw_source": "tui",
                "session_source": "cli",
                "source_tag": "tui",
                "source_label": "TUI",
                "is_cli_session": True,
                "_shared_interactive": True,
            }
        ]
    )
    _install_common_monkeypatches(monkeypatch, sidecars)
    monkeypatch.setenv("HERMES_WEBUI_SESSION_PROJECTION_V2", "1")
    monkeypatch.setattr(
        routes,
        "shared_interactive_sidebar_projection_all_profiles",
        lambda current: (list(canonical), []),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "_prune_orphaned_webui_zero_message_sessions",
        lambda current, diag_stage=None: list(current),
    )

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=True,
    )

    named = next(row for row in payload["sessions"] if row["profile"] == "work")
    assert named["session_id"] == "work-tui"
    assert named["archived"] is True
    assert named["title"] == "Named canonical title"
    assert named["pinned"] is True


def test_sidebar_source_returns_cross_bucket_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    webui_rows = [r for r in rows if r["session_id"].startswith("webui-")]
    cli_rows = [r for r in rows if r["session_id"].startswith("cli-")]

    body = handler.json_body()
    assert handler.status == 200
    assert body["webui_session_count"] == len(webui_rows)
    assert body["cli_session_count"] == len(cli_rows)


def test_sidebar_source_preserves_archived_counts(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20, archived_webui_count=2, archived_cli_count=3)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui&include_archived=1")
    body = handler.json_body()

    assert handler.status == 200
    assert body["archived_webui_count"] == 2
    assert body["archived_cli_count"] == 3
    assert body["archived_count"] == 5
    assert len([r for r in body["sessions"] if r["archived"]]) == 2


def test_sidebar_source_varies_cache_key():
    key_webui = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="webui",
    )
    key_cli = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source="cli",
    )
    key_omitted = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        sidebar_source=None,
    )

    assert key_webui != key_cli
    assert key_webui != key_omitted
    assert key_cli != key_omitted


def test_frontend_uses_one_unfiltered_source_list():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    render_block = _extract_function(src, "renderSessionListFromCache")
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    assert "function _sessionListQueryString()" in src
    assert "qs.set('sidebar_source'" not in src
    assert "_sessionSourceFilter" not in src
    assert "session-source-tabs" not in render_block
    assert "_archivedCount" in src
    assert 'data-i18n="settings_label_optional_external_sessions"' in index
    assert "Hermes One, CLI, TUI, and ACP conversations are always shared." in i18n


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_external_sidebar_toggle_hides_cli_rows_by_default_and_restores_them():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="btnToggleExternalSessions"' in index
    assert "function _sidebarRowsForDisplay(" in src

    is_cli_fn = _extract_function(src, "_isCliSession")
    display_rows_fn = _extract_function(src, "_sidebarRowsForDisplay")
    script = f"""
global._showExternalSessions = false;
function _isMessagingSession(session) {{
  return session && session.source === 'messaging';
}}
{is_cli_fn}
{display_rows_fn}
const rows = [
  {{ session_id: 'webui-1', source: 'webui' }},
  {{ session_id: 'cli-1', source: 'cli' }},
  {{ session_id: 'claude-1', source: 'external_agent', is_cli_session: true }},
  {{ session_id: 'telegram-1', source: 'messaging' }},
];
const hidden = _sidebarRowsForDisplay(rows).map(row => row.session_id);
global._showExternalSessions = true;
const shown = _sidebarRowsForDisplay(rows).map(row => row.session_id);
console.log(JSON.stringify({{ hidden, shown }}));
"""
    body = _run_node(script)

    assert body["hidden"] == ["webui-1", "telegram-1"]
    assert body["shown"] == ["webui-1", "cli-1", "claude-1", "telegram-1"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_list_query_string_uses_unified_source_and_flags():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    archive_filter_fn = _extract_function(src, "_sessionArchivePagingFilterActive")
    query_fn = _extract_function(src, "_sessionListQueryString")
    script = f"""
global._activeProject = null;
global._showAllProfiles = true;
global._showArchived = false;
global.SESSION_ARCHIVED_PAGE_SIZE = 100;
global.SESSION_ARCHIVED_MAX_LOADED_LIMIT = 2000;
global._archivedRowsLoadedLimit = 100;
global.NO_PROJECT_FILTER = '__none__';
let searchValue = '';
global.$ = (id) => id === 'sessionSearch' ? {{ value: searchValue }} : null;
{exclude_hidden_fn}
{archive_filter_fn}
{query_fn}
const first = _sessionListQueryString();
global._showArchived = true;
const second = _sessionListQueryString();
searchValue = 'old archived title';
const searchFiltered = _sessionListQueryString();
searchValue = '';
global._activeProject = 'project-1';
const projectFiltered = _sessionListQueryString();
global._activeProject = null;
global._archivedRowsLoadedLimit = 2500;
const capped = _sessionListQueryString();
global._activeProject = '__none__';
global._showAllProfiles = false;
global._showArchived = false;
const third = _sessionListQueryString();
console.log(JSON.stringify({{ first, second, searchFiltered, projectFiltered, capped, third }}));
"""
    body = _run_node(script)

    assert body["first"] == "?exclude_hidden=1&all_profiles=1"
    assert body["second"] == "?exclude_hidden=1&all_profiles=1&include_archived=1&archived_limit=100"
    assert body["searchFiltered"] == "?exclude_hidden=1&all_profiles=1&include_archived=1"
    assert body["projectFiltered"] == "?include_hidden=1&all_profiles=1&include_archived=1"
    assert body["capped"] == "?exclude_hidden=1&all_profiles=1&include_archived=1&archived_limit=2000"
    assert body["third"] == "?exclude_hidden=1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_archived_search_input_refetches_uncapped_then_restores_paging():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    archive_filter_fn = _extract_function(src, "_sessionArchivePagingFilterActive")
    query_fn = _extract_function(src, "_sessionListQueryString")
    sync_archive_fn = _extract_function(src, "_syncArchivedSearchPagingRefresh")
    filter_fn = _extract_function(src, "filterSessions")
    script = f"""
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global._showAllProfiles = false;
global._showArchived = true;
global.SESSION_ARCHIVED_PAGE_SIZE = 100;
global.SESSION_ARCHIVED_MAX_LOADED_LIMIT = 2000;
global._archivedRowsLoadedLimit = 100;
global._archivedSearchPagingQueryActive = false;
global._lastSessionSearchQuery = '';
global._hideSearchPreviewsAfterSelect = false;
global._contentSearchResults = [];
global._searchDebounceTimer = null;
const calls = [];
let searchValue = '';
global.$ = (id) => id === 'sessionSearch' ? {{ value: searchValue }} : null;
global.syncSessionSearchClear = () => {{}};
global.renderSessionList = () => {{ calls.push(_sessionListQueryString()); return Promise.resolve(); }};
global.renderSessionListFromCache = () => {{}};
global.clearTimeout = () => {{}};
global.setTimeout = () => 1;
global.api = () => Promise.resolve({{ sessions: [] }});
{exclude_hidden_fn}
{archive_filter_fn}
{query_fn}
{sync_archive_fn}
{filter_fn}
searchValue = 'page two title';
filterSessions();
searchValue = '';
filterSessions();
console.log(JSON.stringify({{ calls }}));
"""
    body = _run_node(script)

    assert body["calls"] == [
        "?exclude_hidden=1&include_archived=1",
        "?exclude_hidden=1&include_archived=1&archived_limit=100",
    ]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_partition_keeps_webui_and_hermes_one_rows_in_one_list():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    partition_fn = _extract_function(src, "_partitionSidebarSessionRows")
    script = f"""
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global._showArchived = false;
global._archivedCount = 2;
global._sidebarRowHasVisibleMessages = () => true;
{partition_fn}
const result = _partitionSidebarSessionRows([
  {{ session_id: 'webui-1', source_tag: 'webui', archived: false }},
  {{ session_id: 'cli-1', source_tag: 'cli', archived: false }},
  {{ session_id: 'tui-1', source_tag: 'tui', archived: true }},
], null);
console.log(JSON.stringify({{
  profileFiltered: result.profileFiltered.map(row => row.session_id),
  referenceRaw: result.referenceRaw.map(row => row.session_id),
  sessionsRaw: result.sessionsRaw.map(row => row.session_id),
  archivedCount: result.archivedCount,
}}));
"""
    body = _run_node(script)

    assert body["profileFiltered"] == ["webui-1", "cli-1", "tui-1"]
    assert body["referenceRaw"] == ["webui-1", "cli-1", "tui-1"]
    assert body["sessionsRaw"] == ["webui-1", "cli-1"]
    assert body["archivedCount"] == 2


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_payload_tracks_combined_archive_count_and_unified_scope():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    apply_fn = _extract_function(src, "_applySessionListPayload")
    script = f"""
global._otherProfileCount = 0;
global._archivedCount = 0;
global._serverTimeDelta = 0;
global._serverTz = null;
global._optimisticallyRemovedSessionIds = new Set();
global._allSessions = [];
global._allSessionsScope = null;
global._allProjects = [];
global._sessionListLoadError = null;
global._sessionListHasLoadedOnce = false;
global._sessionListFirstRenderAnimated = true;
global._sessionListSkeletonActive = true;
global._sessionListRefreshAnimationPending = false;
global._lastSessionListRenderSig = null;
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global._showAllProfiles = false;
global._renamingSid = null;
global._sessionActionMenu = null;
global.S = {{ activeProfile: 'default' }};
global._reconcileActiveSessionIdleStateFromList = rows => rows;
global._mergeOptimisticFirstTurnSessions = rows => rows;
global._reconcileDurableCompletionReceipts = () => {{}};
global._sessionListRenderSignature = () => '';
global._purgeStaleInflightEntries = () => {{}};
global._syncSessionAttentionSoundState = () => {{}};
global._pruneLineageReportCacheToVisibleSessions = () => {{}};
global._markPollingCompletionUnreadTransitions = () => {{}};
global._recordSessionProfileCount = () => {{}};
global._isSessionEffectivelyStreaming = () => false;
global.startStreamingPoll = () => {{}};
global.stopStreamingPoll = () => {{}};
global.ensureSessionTimeRefreshPoll = () => {{}};
global.ensureActiveSessionExternalRefreshPoll = () => {{}};
    global.ensureSessionEventsSSE = () => {{}};
    global.animateNextSessionListRefresh = () => {{}};
    global.renderSessionListFromCache = () => {{}};
    {exclude_hidden_fn}
    {apply_fn}
const sessions = [{{ session_id: 'webui-1' }}, {{ session_id: 'cli-1', source_tag: 'cli' }}];
_applySessionListPayload({{
  sessions,
  other_profile_count: 0,
  archived_count: 17,
  active_profile: 'default',
}}, {{ projects: [] }});
console.log(JSON.stringify({{
  archivedCount: _archivedCount,
  sessions: _allSessions.map(row => row.session_id),
  scope: _allSessionsScope,
}}));
"""
    body = _run_node(script)

    assert body["archivedCount"] == 17
    assert body["sessions"] == ["webui-1", "cli-1"]
    assert body["scope"] == {
        "profile": "default",
        "allProfiles": False,
        "excludeHidden": True,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_unified_cache_purges_runtime_state_for_rows_missing_from_full_list():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    is_cli_fn = _extract_function(src, "_isCliSession")
    remember_source_fn = _extract_function(src, "_rememberSessionListSource")
    remember_streaming_fn = _extract_function(src, "_rememberRenderedStreamingState")
    remember_snapshot_fn = _extract_function(src, "_rememberRenderedSessionSnapshot")
    purge_fn = _extract_function(src, "_purgeStaleInflightEntries")
    mark_fn = _extract_function(src, "_markPollingCompletionUnreadTransitions")
    script = f"""
global._allSessions = [{{
  session_id: 'cli-1',
  source_tag: 'cli',
  raw_source: 'cli',
  session_source: 'cli',
  is_streaming: false,
  message_count: 1,
  last_message_at: 10,
}}];
global._sessionListSourceById = new Map([['webui-live', 'webui']]);
global._sessionStreamingById = new Map([['webui-live', true]]);
global._sessionListSnapshotById = new Map([['webui-live', {{ message_count: 1, last_message_at: 1 }}]]);
global._sendInProgress = false;
global._sendInProgressSid = null;
global.INFLIGHT = {{ 'webui-live': {{ lastAssistantText: 'working' }} }};
const cleared = [];
global.clearInflightState = sid => cleared.push(sid);
global._isSessionEffectivelyStreaming = s => Boolean(s.is_streaming);
global._getSessionObservedStreaming = () => ({{}});
global._hasPendingUserMessageSignal = () => false;
global._isSessionActivelyViewedForList = () => false;
global._markSessionCompletionUnread = () => {{}};
global._setSessionViewedCount = () => {{}};
global._rememberObservedStreamingSession = () => {{}};
global._forgetObservedStreamingSession = () => {{}};
{is_cli_fn}
{remember_source_fn}
{remember_streaming_fn}
{remember_snapshot_fn}
{purge_fn}
{mark_fn}
const cliStale = {{
  session_id: 'cli-stale',
  source_tag: 'cli',
  raw_source: 'cli',
  session_source: 'cli',
  is_streaming: false,
  message_count: 2,
  last_message_at: 2,
}};
_rememberRenderedStreamingState(cliStale, true);
_rememberRenderedSessionSnapshot(cliStale);
INFLIGHT['cli-stale'] = {{ lastAssistantText: 'stale' }};
_purgeStaleInflightEntries();
_markPollingCompletionUnreadTransitions(global._allSessions);
console.log(JSON.stringify({{
  inflightKeys: Object.keys(INFLIGHT),
  cleared,
  streamingKeys: Array.from(_sessionStreamingById.keys()).sort(),
  snapshotKeys: Array.from(_sessionListSnapshotById.keys()).sort(),
  sourceKeys: Array.from(_sessionListSourceById.keys()).sort(),
}}));
"""
    body = _run_node(script)

    assert body["inflightKeys"] == []
    assert body["cleared"] == ["webui-live", "cli-stale"]
    assert body["streamingKeys"] == ["cli-1"]
    assert body["snapshotKeys"] == ["cli-1"]
    assert body["sourceKeys"] == ["cli-1"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_sid_only_source_remembering_skips_scope_fallback():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    is_cli_fn = _extract_function(src, "_isCliSession")
    remember_source_fn = _extract_function(src, "_rememberSessionListSource")
    script = f"""
global._allSessions = [];
global._sessionListSourceById = new Map();
{is_cli_fn}
{remember_source_fn}
_rememberSessionListSource(null, 'detached-sid', false);
console.log(JSON.stringify({{
  hasDetached: _sessionListSourceById.has('detached-sid'),
  remembered: Array.from(_sessionListSourceById.entries()),
}}));
"""
    body = _run_node(script)

    assert body["hasDetached"] is False
    assert body["remembered"] == []


def test_session_list_response_omits_bucket_counts_when_missing(monkeypatch):
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)
    monkeypatch.setattr(routes, "_sidebar_session_response_item", lambda row, *, redact_enabled=None: row)

    body = routes._session_list_payload_to_response(
        {
            "sessions": [{"session_id": "webui-1", "title": "WebUI Session"}],
            "cli_count": 0,
            "archived_count": 0,
            "archived_webui_count": 0,
            "archived_cli_count": 0,
            "include_archived": False,
            "all_profiles": False,
            "active_profile": "default",
            "other_profile_count": 0,
        }
    )

    assert "webui_session_count" not in body
    assert "cli_session_count" not in body
    assert body["sessions"][0]["session_id"] == "webui-1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scope_mismatch_error_path_respects_profile_without_source_buckets():
    src = SESSIONS_JS.read_text(encoding="utf-8")
    purge_fn = _extract_function(src, "_purgeStaleInflightEntries")
    exclude_hidden_fn = _extract_function(src, "_sessionListExcludeHiddenEnabled")
    query_fn = _extract_function(src, "_sessionListQueryString")
    fetch_helper_fn = _ensure_async(
        _extract_function(src, "_loadSidebarSessionListPayload"),
        "_loadSidebarSessionListPayload",
    )
    refresh_fn = _ensure_async(
        _extract_function(src, "_runRenderSessionListRefresh"),
        "_runRenderSessionListRefresh",
    )
    script = f"""
global._showAllProfiles = false;
global._showArchived = false;
global._sessionListHasLoadedOnce = true;
global._SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
global._renderSessionListGen = 1;
global._profileSwitchListEmbargo = false;
global._pendingSessionListPayload = null;
global._allProjects = [];
global._contentSearchResults = ['stale'];
global._activeProject = null;
global.NO_PROJECT_FILTER = '__none__';
global.S = {{ activeProfile: 'default' }};
global.$ = () => ({{ value: '' }});
global._isSessionListUserInteracting = () => false;
global._schedulePendingSessionListApply = () => {{}};
global._showSessionListLoadError = error => {{
  global._lastError = error.message;
}};
const renders = [];
const cleared = [];
global.renderSessionListFromCache = () => {{
  _purgeStaleInflightEntries();
  renders.push({{
    sessions: Array.isArray(global._allSessions) ? global._allSessions.map(s => s.session_id) : null,
    scope: global._allSessionsScope ? {{ ...global._allSessionsScope }} : null,
    skeleton: global._sessionListSkeletonActive,
    inflightKeys: Object.keys(global.INFLIGHT || {{}}).sort(),
  }});
}};
global.api = () => Promise.reject(new Error('boom'));
    global.clearInflightState = sid => cleared.push(sid);
    {purge_fn}
    {exclude_hidden_fn}
    {query_fn}
    {fetch_helper_fn}
    {refresh_fn}
async function runCase(requestedProfile, cachedProfile) {{
  global.S.activeProfile = requestedProfile;
  global._allSessions = [{{ session_id: cachedProfile + '-1' }}];
  global._allSessionsScope = {{
    profile: cachedProfile,
    allProfiles: false,
    excludeHidden: true,
  }};
  global._sessionListSourceById = new Map([['webui-live', 'webui']]);
  global.INFLIGHT = {{ 'webui-live': {{ lastAssistantText: 'working' }} }};
  cleared.length = 0;
  global._sessionListSkeletonActive = true;
  global._lastError = null;
  renders.length = 0;
  await _runRenderSessionListRefresh({{}}, 1);
  return {{
    sessions: Array.isArray(global._allSessions) ? global._allSessions.map(s => s.session_id) : null,
    scope: global._allSessionsScope ? {{ ...global._allSessionsScope }} : null,
    skeleton: global._sessionListSkeletonActive,
    error: global._lastError,
    cleared: [...cleared],
    inflightKeys: Object.keys(global.INFLIGHT || {{}}).sort(),
    render: renders[0] || null,
  }};
}}
(async () => {{
  const mismatch = await runCase('other', 'default');
  const match = await runCase('default', 'default');
  console.log(JSON.stringify({{ mismatch, match }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""
    body = _run_node(script)

    assert body["mismatch"]["sessions"] == []
    assert body["mismatch"]["scope"] == {
        "profile": "other",
        "allProfiles": False,
        "excludeHidden": True,
    }
    assert body["mismatch"]["skeleton"] is False
    assert body["mismatch"]["render"]["sessions"] == []
    assert body["mismatch"]["inflightKeys"] == []
    assert body["mismatch"]["cleared"] == ["webui-live"]
    assert body["mismatch"]["render"]["inflightKeys"] == []
    assert body["match"]["sessions"] == ["default-1"]
    assert body["match"]["scope"] == {
        "profile": "default",
        "allProfiles": False,
        "excludeHidden": True,
    }
    assert body["match"]["render"]["sessions"] == ["default-1"]


def test_payload_row_count_regression(monkeypatch):
    rows = _session_rows(webui_count=30, cli_count=20)
    _install_common_monkeypatches(monkeypatch, rows)

    handler = _handle_sessions("http://example.com/api/sessions?sidebar_source=webui")
    body = handler.json_body()

    assert handler.status == 200
    assert len(body["sessions"]) == 30
