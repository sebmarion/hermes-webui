"""Static cursor older-page behavior for browser bounded conversation loading."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for index in range(brace, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_cursor_older_page_uses_opaque_cursor_not_numeric_offset():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    cursor_branch = body.index("if (_messagePaging.mode === 'cursor_v1')")
    cursor_body = body[cursor_branch:]
    assert "const requestedCursor = _messagePaging.beforeCursor;" in cursor_body
    assert "message_paging=cursor_v1&msg_cursor=${encodeURIComponent(requestedCursor)}&msg_limit=${_INITIAL_MSG_LIMIT}" in cursor_body
    assert "msg_before=" not in cursor_body.split("// Legacy numeric paging", 1)[0]
    assert "const page = _parseMessagePage(data.session.message_page);" in cursor_body
    assert "_state_db_message_id" in cursor_body
    assert "olderMsgs.some(existing => _sameTranscriptMessage(existing, message))" in cursor_body
    assert "renderMessages({ preserveScroll: true });" in cursor_body
    assert "_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)" in cursor_body


def test_cursor_page_vetoes_stale_session_or_generation_before_commit():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    cursor_branch = body.index("if (_messagePaging.mode === 'cursor_v1')")
    cursor_body = body[cursor_branch:]

    assert "const startLoadGeneration = _loadSessionGeneration;" in cursor_body
    assert "if (_loadSessionGeneration !== startLoadGeneration) return;" in cursor_body
    assert "if (_messagesGeneration !== startGeneration) return;" in cursor_body
    assert "if (!S.session || S.session.session_id !== sid) return;" in cursor_body
    assert "if (data.session.session_id !== sid) return;" in cursor_body


def test_cursor_conflict_restarts_once_then_falls_back_to_legacy_safe_flow():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    cursor_branch = body.index("if (_messagePaging.mode === 'cursor_v1')")
    cursor_body = body[cursor_branch:]

    assert "e && (e.status === 400 || e.status === 409)" in cursor_body
    assert "await _recoverCursorPaging(sid, startLoadGeneration, startGeneration)" in cursor_body
    recovery_body = _function_body(SESSIONS_JS, "async function _recoverCursorPaging")
    assert "!_messagePaging.restartAttempted" in recovery_body
    assert "_messagePaging.restartAttempted = true;" in recovery_body
    assert "_messagePaging.beforeCursor = null;" in recovery_body
    assert "await loadSession(sid, {force:true, cursorRestartAttempted:true});" in recovery_body
    assert "await loadSession(sid, {force:true, forceLegacyMessagePaging:true});" in recovery_body


def test_cursor_page_shape_degradation_uses_the_same_bounded_recovery_policy():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    cursor_branch = body.index("if (_messagePaging.mode === 'cursor_v1')")
    cursor_body = body[cursor_branch:]

    assert "if (!page || (page.hasMore && page.beforeCursor === requestedCursor)) {" in cursor_body
    assert "await _recoverCursorPaging(sid, startLoadGeneration, startGeneration);" in cursor_body
    recovery_body = _function_body(SESSIONS_JS, "async function _recoverCursorPaging")
    assert "if (!_messagePaging.restartAttempted)" in recovery_body
    assert "await loadSession(sid, {force:true, cursorRestartAttempted:true});" in recovery_body
    assert "await loadSession(sid, {force:true, forceLegacyMessagePaging:true});" in recovery_body


def test_full_history_loader_resets_cursor_mode_before_using_absolute_indexes():
    body = _function_body(SESSIONS_JS, "async function _ensureAllMessagesLoaded")

    assert "_resetMessagePaging();" in body
    assert "_oldestIdx = 0;" in body
