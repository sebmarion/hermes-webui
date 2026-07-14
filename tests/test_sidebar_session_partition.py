"""Regression coverage for single-pass sidebar session partitioning."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(name: str) -> str:
    start = SESSIONS_JS.index(f"function {name}(")
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(SESSIONS_JS)):
        char = SESSIONS_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : idx + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _partition_block() -> str:
    return _function_block("_partitionSidebarSessionRows")


def test_render_uses_single_pass_partition_helper():
    render_body = _function_block("renderSessionListFromCache")

    assert "_partitionSidebarSessionRows(allMatched, activeSidForSidebar)" in render_body
    assert "_renderSidebarRowsFromRawSessions(sessionsRaw, [...referenceRaw, ..._scopedSidebarReferenceRows()])" in render_body
    assert "_sessionSourceTabCount" not in render_body
    assert "renderedWebuiSessionCount" not in render_body
    assert "renderedCliSessionCount" not in render_body
    assert "withMessages.filter(" not in render_body


def test_partition_helper_applies_message_project_and_archive_gates():
    block = _partition_block()

    assert "function _sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in SESSIONS_JS
    assert "_sidebarRowHasVisibleMessages(s, activeSidForSidebar)" in block
    assert "if(!_showArchived&&s.archived) continue;" in block
    assert "if(s.archived) localArchivedCount++;" in block
    assert "archivedCount: Math.max(localArchivedCount, Number(_archivedCount||0))," in block
    assert "return {" in block
    assert "profileFiltered," in block
    assert "sessionsRaw," in block
    assert "_sessionSourceFilter" not in block


def test_partition_helper_returns_one_raw_reference_and_visible_collection():
    render_body = _function_block("renderSessionListFromCache")

    assert "referenceRaw," in _partition_block()
    assert "sessionsRaw," in _partition_block()
    assert "webuiReferenceRaw" not in _partition_block()
    assert "cliReferenceRaw" not in _partition_block()
    assert "webuiSessionsRaw" not in _partition_block()
    assert "cliSessionsRaw" not in _partition_block()
    assert "[...referenceRaw, ..._scopedSidebarReferenceRows()]" in render_body
    assert "function _countRenderedSidebarRowsFromRawSessions" not in SESSIONS_JS
    assert "function _renderSidebarRowsFromRawSessions(sessionsRaw, referenceSessionsRaw){" in SESSIONS_JS
    assert "_attachChildSessionsToSidebarRows(_collapseSessionLineageForSidebar(sessionsRaw), sessionsRaw, referenceRows)" in SESSIONS_JS


def test_archive_load_more_uses_combined_loaded_count_and_hides_under_filters():
    render_body = _function_block("renderSessionListFromCache")

    assert "function _sessionArchivePagingFilterActive()" in SESSIONS_JS
    assert "const archivePagingFilterActive=_sessionArchivePagingFilterActive();" in render_body
    assert "if(_showArchived&&!archivePagingFilterActive){" in render_body
    assert "const activeArchivedTotal=_archivedCount;" in render_body
    assert "const loadedArchivedCount=sidebarRows.filter(s=>s&&s.archived).length;" in render_body
    assert "const archiveLoadCapReached=Number(_archivedRowsLoadedLimit||0)>=SESSION_ARCHIVED_MAX_LOADED_LIMIT;" in render_body
    assert "const remainingArchived=archiveLoadCapReached?0:Math.max(0, Number(activeArchivedTotal||0)-loadedArchivedCount);" in render_body
    assert "const remainingArchived=Math.max(0, Number(activeArchivedTotal||0)-loadedArchivedCount);" not in render_body
    assert "orderedSessions.filter(s=>s&&s.archived).length" not in render_body
