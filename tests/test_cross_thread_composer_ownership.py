"""Regression coverage for cross-thread composer ownership and settled transcript noise."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

import api.routes as routes


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    paren = source.index("(", start)
    paren_depth = 1
    signature_end = paren + 1
    while paren_depth:
        if source[signature_end] == "(":
            paren_depth += 1
        elif source[signature_end] == ")":
            paren_depth -= 1
        signature_end += 1
    brace = source.index("{", signature_end)
    depth = 1
    idx = brace + 1
    while depth:
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
        idx += 1
    return source[start:idx]


def _send_reentrant_guard() -> str:
    body = _function_source(MESSAGES_JS, "send")
    start = body.index("if (_sendInProgress) {")
    brace = body.index("{", start)
    depth = 1
    idx = brace + 1
    while depth:
        if body[idx] == "{":
            depth += 1
        elif body[idx] == "}":
            depth -= 1
        idx += 1
    return body[start:idx]


def _run_node(script: str) -> dict:
    if NODE is None:  # pragma: no cover
        pytest.skip("node not available")
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reentrant_send_queues_for_visible_composer_owner_not_inflight_session():
    """A second send typed in B while A is still starting must queue for B."""
    guard = _send_reentrant_guard()
    helper = _function_source(MESSAGES_JS, "_composerTextWithPendingSelections")
    script = textwrap.dedent(
        f"""
        const queued=[];
        const state={{input:{{value:'message for B'}}}};
        const $=(id)=>id==='msg'?state.input:null;
        const _pendingSelections=[];
        function _formatSelectedTextReplyQuote(t){{return t;}}
        {helper}
        let _sendInProgress=true;
        let _sendInProgressSid='session-A';
        const S={{session:{{session_id:'session-B'}},pendingFiles:[],activeProfile:'default'}};
        function _composerDraftOwnerSessionId(){{return 'session-B';}}
        function _composerOwnershipSnapshot(text,files){{
          const targetSid=S.session&&S.session.session_id;
          const ownerSid=_composerDraftOwnerSessionId();
          return {{ownerSid,targetSid,valid:ownerSid===targetSid,hasPayload:!!text||(files||[]).length>0}};
        }}
        function _warnComposerOwnershipMismatch(){{}}
        function _chatPayloadModelState(){{return {{model:'m',model_provider:'p'}};}}
        function queueSessionMessage(sid,payload){{queued.push({{sid,payload}});}}
        function _clearComposerAfterQueuedSelectionSend(){{state.input.value='';}}
        function _clearComposerDraft(){{}}
        function updateQueueBadge(){{}}
        function renderTray(){{}}
        function showToast(){{}}
        (function(){{{guard}}})();
        process.stdout.write(JSON.stringify({{queued}}));
        """
    )
    out = _run_node(script)
    assert [entry["sid"] for entry in out["queued"]] == ["session-B"]


def test_send_validates_owner_before_optimistic_ui_or_chat_start():
    body = _function_source(MESSAGES_JS, "send")
    guard = body.index("const _sendOwnership=_composerOwnershipSnapshot(")
    reject = body.index("_warnComposerOwnershipMismatch(_sendOwnership,", guard)
    optimistic = body.index("S.messages.push({role:'user'", reject)
    network = body.index("const startData=await api('/api/chat/start'", reject)
    assert guard < reject < optimistic < network


def test_chat_start_payload_carries_matching_composer_owner():
    body = _function_source(MESSAGES_JS, "send")
    payload = body[body.index("const startData=await api('/api/chat/start'") :]
    assert "session_id:activeSid" in payload
    assert "composer_session_id:activeSid" in payload


def test_input_claims_visible_session_before_saving_draft():
    start = BOOT_JS.index("// Persist composer draft to server")
    block = BOOT_JS[start : BOOT_JS.index("});", start) + 3]
    claim = block.index("_claimComposerDraftOwner(sid)")
    save = block.index("_saveComposerDraft(sid")
    assert claim < save


def test_empty_restore_claims_owner_and_rapid_switch_save_uses_recorded_owner():
    owner_block_start = SESSIONS_JS.index("let _composerDraftOwnerSid")
    owner_block_end = SESSIONS_JS.index("function _composerDraftFileSignature", owner_block_start)
    owner_block = SESSIONS_JS[owner_block_start:owner_block_end]
    restore = _function_source(SESSIONS_JS, "_restoreComposerDraft")
    script = textwrap.dedent(
        f"""
        const state={{value:'stale from A'}};
        function $(){{return state;}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        function _composerDraftHasPayload(text,files){{return !!String(text||'')||(files||[]).length>0;}}
        function _isComposerDraftRestoreSuppressed(){{return false;}}
        function _clearComposerDraftRestoreSuppression(){{}}
        let _loadingSessionId='session-B';
        const S={{session:{{session_id:'session-B'}}}};
        {owner_block}
        {restore}
        _claimComposerDraftOwner('session-A');
        const saveA=_composerDraftSessionForSave('session-A','draft A',[]);
        _restoreComposerDraft({{text:'',files:[]}},'session-B');
        const ownerAfterEmptyRestore=_composerDraftOwnerSessionId();
        const saveB=_composerDraftSessionForSave('session-B','draft B',[]);
        _claimComposerDraftOwner('session-C');
        const saveC=_composerDraftSessionForSave('session-C','draft C',[]);
        _claimComposerDraftOwner(null);
        const orphan=_composerDraftSessionForSave('session-C','do not infer me',[]);
        process.stdout.write(JSON.stringify({{saveA,ownerAfterEmptyRestore,saveB,saveC,orphan,value:state.value}}));
        """
    )
    out = _run_node(script)
    assert out == {
        "saveA": "session-A",
        "ownerAfterEmptyRestore": "session-B",
        "saveB": "session-B",
        "saveC": "session-C",
        "orphan": None,
        "value": "",
    }


def test_load_session_resolves_owner_before_switch_teardown_and_saves_that_owner():
    body = _function_source(SESSIONS_JS, "loadSession")
    resolve_owner = body.index(
        "_switchDraftSid=_composerDraftSessionForSave(currentSid,_switchDraftText,_switchDraftFiles);"
    )
    orphan_abort = body.index("if(!_switchDraftSid){", resolve_owner)
    mark_loading = body.index("_loadingSessionId = sid;", orphan_abort)
    save = body.index(
        "await _saveComposerDraftNow(_switchDraftSid, _switchDraftText, _switchDraftFiles);",
        mark_loading,
    )
    assert resolve_owner < orphan_abort < mark_loading < save


def test_chat_start_rejects_mismatched_composer_before_session_lookup(monkeypatch):
    looked_up = []

    def fail_lookup(*args, **kwargs):
        looked_up.append((args, kwargs))
        raise AssertionError("mismatched ownership must be rejected before lookup")

    monkeypatch.setattr(routes, "_get_or_materialize_session", fail_lookup)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload},
    )

    response = routes._handle_chat_start(
        object(),
        {
            "session_id": "session-B",
            "composer_session_id": "session-A",
            "message": "belongs to A",
        },
    )

    assert looked_up == []
    assert response["status"] == 409
    assert response["payload"]["type"] == "composer_session_mismatch"
    assert response["payload"]["session_id"] == "session-B"
    assert response["payload"]["composer_session_id"] == "session-A"


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": "session-A", "message": "legacy client"},
        {
            "session_id": "session-A",
            "composer_session_id": "session-A",
            "message": "matching owner",
        },
    ],
)
def test_chat_start_matching_or_omitted_owner_reaches_session_lookup(monkeypatch, body):
    class LookupReached(RuntimeError):
        pass

    def stop_at_lookup(*args, **kwargs):
        raise LookupReached

    monkeypatch.setattr(routes, "_get_or_materialize_session", stop_at_lookup)
    with pytest.raises(LookupReached):
        routes._handle_chat_start(object(), body)


def test_settled_compression_cards_are_manual_only():
    mode_fn = _function_source(UI_JS, "_compressionModeForSession")
    reference_fn = _function_source(UI_JS, "_shouldShowSettledCompressionReference")
    tasks_fn = _function_source(UI_JS, "_latestPreservedCompressionTaskListMessages")
    script = textwrap.dedent(
        f"""
        function _isContextCompactionText(){{return false;}}
        function _isPreservedCompressionTaskListMessage(m){{return !!m.preserved;}}
        function _latestTodoToolItems(){{return null;}}
        function _hasActiveTodoItems(){{return true;}}
        {mode_fn}
        {reference_fn}
        {tasks_fn}
        const messages=[{{role:'user',content:'tasks',preserved:true}}];
        function result(mode){{
          globalThis.S={{session:{{compression_anchor_mode:mode}}}};
          return {{reference:_shouldShowSettledCompressionReference('summary'),tasks:_latestPreservedCompressionTaskListMessages(messages).length}};
        }}
        process.stdout.write(JSON.stringify({{automatic:result('summary_compaction'),manual:result('manual'),legacy:result('')}}));
        """
    )
    out = _run_node(script)
    assert out == {
        "automatic": {"reference": False, "tasks": 0},
        "manual": {"reference": True, "tasks": 1},
        "legacy": {"reference": False, "tasks": 0},
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "[Workspace::v1: /Users/seb]\n[Workspace::v1: /Users/seb]\nGo",
            "Go",
        ),
        (
            "[Workspace: /Users/seb]\n[Workspace::v1: /Users/seb]\nGo",
            "Go",
        ),
    ],
)
def test_workspace_display_prefix_strips_repeated_leading_sentinels(raw, expected):
    fn = _function_source(UI_JS, "_stripWorkspaceDisplayPrefix")
    out = _run_node(
        f"{fn}\nprocess.stdout.write(JSON.stringify({{value:_stripWorkspaceDisplayPrefix({json.dumps(raw)})}}));"
    )
    assert out["value"] == expected
