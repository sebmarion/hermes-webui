import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"could not extract {name}")


def _active_sidebar_rows(rows, session):
    helper = _extract_js_function(SESSIONS_JS, "_sessionRowsWithActiveEphemeralSession")
    script = f"""
const S = {{ session: {json.dumps(session)}, activeProfile: 'default' }};
const rows = {json.dumps(rows)};
{helper}
const result = _sessionRowsWithActiveEphemeralSession(rows);
console.log(JSON.stringify({{ result, rows }}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def test_active_empty_session_is_injected_into_sidebar_rows():
    assert "function _sessionRowsWithActiveEphemeralSession(rows)" in SESSIONS_JS
    helper_start = SESSIONS_JS.index("function _sessionRowsWithActiveEphemeralSession(rows)")
    helper_end = SESSIONS_JS.index("function renderSessionListFromCache()", helper_start)
    helper = SESSIONS_JS[helper_start:helper_end]

    assert "S.session" in helper
    assert "message_count:0" in helper
    assert "title:S.session.title||'New Chat'" in helper
    assert "rows.some(s=>s&&s.session_id===sid)" in helper


@pytest.mark.skipif(NODE is None, reason="node is required for active sidebar helper behavior tests")
def test_compressed_active_session_replaces_requested_cached_alias_in_place():
    cached_rows = [
        {
            "session_id": "before-root",
            "title": "Earlier session",
            "message_count": 3,
        },
        {
            "session_id": "root",
            "title": "Shared title",
            "message_count": 116,
            "active_stream_id": "stream-1",
            "pending_user_message": "keep working",
            "pending_attachments": [{"name": "context.txt"}],
            "pending_started_at": 123,
            "pending_user_source": "composer",
            "attention": {"kind": "approval", "count": 1},
            "activity_phase": "tool",
            "activity_started_at": 110,
            "activity_heartbeat_at": 122,
            "has_pending_user_message": True,
            "is_streaming": True,
            "is_working": True,
        },
        {
            "session_id": "after-root",
            "title": "Later session",
            "message_count": 4,
        },
    ]
    active_session = {
        "session_id": "tip",
        "canonical_session_id": "tip",
        "requested_session_id": "root",
        "title": "Canonical title",
        "display_title": "Canonical title",
        "message_count": 168,
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_attachments": [],
        "is_streaming": False,
        "is_working": False,
    }

    payload = _active_sidebar_rows(cached_rows, active_session)

    assert [row["session_id"] for row in payload["result"]] == ["before-root", "tip", "after-root"]
    row = payload["result"][1]
    assert row["message_count"] == 168
    assert row["title"] == "Canonical title"
    assert row["display_title"] == "Canonical title"
    assert row["active_stream_id"] == "stream-1"
    assert row["pending_user_message"] == "keep working"
    assert row["pending_attachments"] == [{"name": "context.txt"}]
    assert row["pending_started_at"] == 123
    assert row["pending_user_source"] == "composer"
    assert row["attention"] == {"kind": "approval", "count": 1}
    assert row["activity_phase"] == "tool"
    assert row["activity_started_at"] == 110
    assert row["activity_heartbeat_at"] == 122
    assert row["has_pending_user_message"] is True
    assert row["is_streaming"] is True
    assert row["is_working"] is True
    assert payload["rows"] == cached_rows


@pytest.mark.skipif(NODE is None, reason="node is required for active sidebar helper behavior tests")
def test_active_session_without_requested_cached_alias_is_prepended_even_with_same_title():
    cached_rows = [{"session_id": "other", "title": "Shared title", "message_count": 12}]
    active_session = {
        "session_id": "tip",
        "requested_session_id": "missing-root",
        "title": "Shared title",
        "message_count": 168,
    }

    payload = _active_sidebar_rows(cached_rows, active_session)

    assert [row["session_id"] for row in payload["result"]] == ["tip", "other"]
    assert payload["rows"] == cached_rows


@pytest.mark.skipif(NODE is None, reason="node is required for active sidebar helper behavior tests")
def test_active_session_does_not_normalize_requested_alias_before_matching():
    cached_rows = [{"session_id": "root", "title": "Cached title", "message_count": 116}]
    active_session = {
        "session_id": "tip",
        "requested_session_id": " root ",
        "title": "Canonical title",
        "message_count": 168,
    }

    payload = _active_sidebar_rows(cached_rows, active_session)

    assert [row["session_id"] for row in payload["result"]] == ["tip", "root"]
    assert payload["rows"] == cached_rows


@pytest.mark.skipif(NODE is None, reason="node is required for active sidebar helper behavior tests")
def test_compressed_alias_contributes_only_explicit_runtime_overlay_fields():
    cached_rows = [
        {
            "session_id": "root",
            "title": "Alias title",
            "profile": "alias-profile",
            "last_message_at": 123,
            "message_count": 116,
            "active_stream_id": "stream-1",
        }
    ]
    active_session = {
        "session_id": "tip",
        "requested_session_id": "root",
        "title": "Canonical title",
        "message_count": 168,
        "active_stream_id": None,
    }

    payload = _active_sidebar_rows(cached_rows, active_session)

    row = payload["result"][0]
    assert row["session_id"] == "tip"
    assert row["title"] == "Canonical title"
    assert row["message_count"] == 168
    assert row["active_stream_id"] == "stream-1"
    assert "profile" not in row
    assert "last_message_at" not in row


@pytest.mark.skipif(NODE is None, reason="node is required for active sidebar helper behavior tests")
def test_compressed_canonical_pending_attachments_win_when_non_empty():
    cached_rows = [
        {
            "session_id": "root",
            "message_count": 116,
            "pending_attachments": [{"name": "stale-context.txt"}],
        }
    ]
    active_session = {
        "session_id": "tip",
        "requested_session_id": "root",
        "message_count": 168,
        "pending_attachments": [{"name": "fresh-context.txt"}],
    }

    payload = _active_sidebar_rows(cached_rows, active_session)

    assert payload["result"][0]["pending_attachments"] == [{"name": "fresh-context.txt"}]


def test_new_session_does_not_mutate_removed_source_filter():
    new_session = SESSIONS_JS[SESSIONS_JS.index("async function newSession"):SESSIONS_JS.index("async function loadSession")]
    assert "_sessionSourceFilter" not in new_session


def test_sidebar_search_uses_active_ephemeral_rows_before_filtering():
    render_start = SESSIONS_JS.index("function renderSessionListFromCache()")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    render_body = SESSIONS_JS[render_start:render_end]

    assert "const sidebarRows=_sessionRowsWithActiveEphemeralSession(_allSessions);" in render_body
    assert "const searchMatches=_sessionSearchMergeMatches(sidebarRows,searchQueryRaw,_contentSearchResults);" in render_body
    assert "const allMatched=_ensureActiveSessionRowPresent(searchMatches,sidebarRows);" in render_body


def test_active_row_reinjection_gated_to_zero_message_ephemeral_only():
    """#3408 review (Codex): _ensureActiveSessionRowPresent must only re-add the
    active FRESHLY-CREATED 0-message chat after search-merge. An active conversation
    that already has messages and was filtered out by the search query must stay
    filtered — re-adding it would pollute unrelated search results with the current
    chat."""
    start = SESSIONS_JS.index("function _ensureActiveSessionRowPresent(rows, sourceRows)")
    end = SESSIONS_JS.index("function clearOptimisticSessionStreaming", start)
    body = SESSIONS_JS[start:end]

    # The reinjection is gated on a 0-message check, not an unconditional prepend.
    assert "Number(activeRow.message_count||0)<=0" in body
    assert "[activeRow,...rows]" in body
    # The unconditional return that shipped in the original PR must be gone.
    assert "return activeRow?[activeRow,...rows]:rows;" not in body
