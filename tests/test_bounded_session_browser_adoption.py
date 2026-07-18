"""Static browser adoption contract for bounded conversation loading."""

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


def test_browser_has_one_explicit_cursor_paging_state_object():
    assert "let _messagePaging = {" in SESSIONS_JS
    for field in ("mode: 'legacy'", "beforeCursor: null", "hasMore: false", "visibleCount: 0", "restartAttempted: false"):
        assert field in SESSIONS_JS


def test_message_page_parser_accepts_only_complete_cursor_v1_shape():
    body = _function_body(SESSIONS_JS, "function _parseMessagePage")

    assert "Object.keys(page).length !== 6" in body
    for field in ("mode", "before_cursor", "has_more", "visible_count", "raw_rows_examined", "serialized_bytes"):
        assert f"'{field}'" in body
    assert "page.mode !== 'cursor_v1'" in body
    assert "typeof page.before_cursor !== 'string'" in body
    assert "typeof page.has_more !== 'boolean'" in body
    assert "Number.isSafeInteger(page.visible_count)" in body
    assert "Number.isSafeInteger(page.raw_rows_examined)" in body
    assert "Number.isSafeInteger(page.serialized_bytes)" in body


def test_message_page_parser_enforces_cursor_coherence_and_server_work_bounds():
    body = _function_body(SESSIONS_JS, "function _parseMessagePage")

    assert "page.has_more && (page.before_cursor === null || page.before_cursor === '')" in body
    assert "!page.has_more && page.before_cursor !== null" in body
    assert "page.visible_count > 100" in body
    assert "page.raw_rows_examined > 864" in body
    assert "page.serialized_bytes > 2621440" in body


def test_adoption_falls_back_to_legacy_offsets_when_page_is_missing_or_invalid():
    body = _function_body(SESSIONS_JS, "function _adoptMessagePaging")

    assert "_messagePaging.mode = 'legacy';" in body
    assert "_messagePaging.beforeCursor = null;" in body
    assert "_messagesTruncated = !!(session && session._messages_truncated);" in body
    assert "_oldestIdx = Number(session && session._messages_offset) || 0;" in body


def test_enabled_browser_gate_uses_one_negotiated_initial_request_and_reuses_it():
    load_body = _function_body(SESSIONS_JS, "async function loadSession")
    ensure_body = _function_body(SESSIONS_JS, "async function _ensureMessagesLoaded")

    assert "window._boundedConversationBrowser === true" in load_body
    assert "messages=1&resolve_model=0&msg_limit=${_INITIAL_MSG_LIMIT}&message_paging=cursor_v1" in load_body
    assert "initialData:data" in load_body
    assert "let data = opts.initialData || null;" in ensure_body
    assert "if (!data) {" in ensure_body
    assert "_adoptMessagePaging(data.session);" in ensure_body


def test_disabled_gate_preserves_legacy_metadata_then_messages_flow():
    load_body = _function_body(SESSIONS_JS, "async function loadSession")

    assert "messages=0&resolve_model=0" in load_body
    assert "initialData:data" in load_body
