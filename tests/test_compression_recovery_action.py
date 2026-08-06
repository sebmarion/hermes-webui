import io
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from api import models, routes
from api.compression_recovery import (
    compression_recovery_payload_for_session,
    is_generic_continuation_intent,
    stamp_compression_exhausted_recovery,
)
from api.models import Session
from api.session_recovery import _state_db_row_to_sidecar
from api.webui_session_db import WebUIJsonSessionDB


ROOT = Path(__file__).resolve().parents[1]


class _JSONHandler:
    headers = {}

    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()
        self.headers_sent = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass


def _payload(handler):
    raw = handler.wfile.getvalue().decode("utf-8")
    return json.loads(raw) if raw else {}


def _isolate_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    return session_dir


def test_generic_continuation_intent_is_scoped_to_empty_continue_words():
    assert is_generic_continuation_intent("continue")
    assert is_generic_continuation_intent("继续吧。")
    assert is_generic_continuation_intent("go on")
    assert not is_generic_continuation_intent("continue by summarizing the workspace changes")
    assert not is_generic_continuation_intent("继续修复 4685 的恢复卡")


def test_chat_start_blocks_generic_continue_after_compression_exhausted(monkeypatch, tmp_path):
    _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat1"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "继续"})
    payload = _payload(handler)

    assert handler.status == 409
    assert payload["type"] == "compression_recovery_required"
    assert payload["recommended_recovery_action"] == "start_focused_continuation"
    assert payload["compression_recovery"]["terminal_state"] == "compression_exhausted"


def test_chat_start_blocks_before_substantive_prompt_validation(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat2"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad workspace")),
    )

    handler = _JSONHandler()
    routes._handle_chat_start(handler, {"session_id": sid, "message": "continue by checking the repo"})
    payload = _payload(handler)
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 409
    assert payload["type"] == "compression_recovery_required"
    assert saved["recommended_recovery_action"] == "start_focused_continuation"
    assert saved["compression_recovery"]["terminal_state"] == "compression_exhausted"


def test_chat_start_blocks_substantive_prompt_after_compression_exhausted(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat3"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    start_called = False

    def _unexpected_start(*_args, **_kwargs):
        nonlocal start_called
        start_called = True
        raise AssertionError("exhausted parent must not start another run")

    monkeypatch.setattr(routes, "_start_run", _unexpected_start)

    handler = _JSONHandler()
    routes._handle_chat_start(
        handler,
        {
            "session_id": sid,
            "message": "continue by checking the repo",
            "attachments": [{"name": "evidence.txt"}],
        },
    )
    payload = _payload(handler)
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 409
    assert payload["type"] == "compression_recovery_required"
    assert payload["recommended_recovery_action"] == "start_focused_continuation"
    assert start_called is False
    assert saved["compression_recovery"]["terminal_state"] == "compression_exhausted"
    assert saved["recommended_recovery_action"] == "start_focused_continuation"


def test_chat_sync_blocks_after_compression_exhausted(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverychat4"
    session = Session(
        session_id=sid,
        title="Recovery",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session
    monkeypatch.setattr(
        routes,
        "resolve_trusted_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exhausted parent must fail before workspace resolution")
        ),
    )

    handler = _JSONHandler()
    routes._handle_chat_sync(
        handler,
        {"session_id": sid, "message": "continue by checking the repo"},
    )
    payload = _payload(handler)
    saved = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))

    assert handler.status == 409
    assert payload["type"] == "compression_recovery_required"
    assert payload["recommended_recovery_action"] == "start_focused_continuation"
    assert saved["recommended_recovery_action"] == "start_focused_continuation"
    assert saved["compression_recovery"]["terminal_state"] == "compression_exhausted"


def test_recovery_start_seeds_newest_summary_and_latest_substantive_request(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrc1"
    latest_request = "Fix the cart minimum-order recovery flow."
    old_summary = "[CONTEXT COMPACTION — REFERENCE ONLY] Old summary that must not survive."
    newest_summary = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Newest summary with sk_live_1234567890secret.\n"
        + ("bounded context " * 700)
        + "LATEST SUMMARY TAIL"
    )
    session = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        profile="default",
        project_id="proj_1",
        messages=[
            {"role": "user", "content": latest_request},
            {"role": "user", "content": "handoff"},
            {"role": "user", "content": "continue"},
        ],
        context_messages=[
            {
                "role": "assistant",
                "content": old_summary,
                "_compressed_summary": True,
                "timestamp": 100,
            },
            {"role": "tool", "content": "raw tool output must never be copied"},
            {
                "role": "assistant",
                "content": newest_summary,
                "_compressed_summary": True,
                "timestamp": 200,
            },
        ],
        worktree_path=str(tmp_path / "task-worktree"),
        worktree_branch="codex/task",
        worktree_repo_root=str(tmp_path),
        worktree_created_at=1234.5,
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})
    payload = _payload(handler)

    assert handler.status == 200
    new_session = payload["session"]
    assert new_session["session_id"] != sid
    assert new_session["parent_session_id"] == sid
    assert new_session["workspace"] == str(tmp_path)
    assert new_session["model"] == "gpt-4o"
    assert new_session["model_provider"] == "openai"
    assert new_session["messages"] == []
    assert new_session["session_source"] == "fork"
    assert not new_session.get("active_stream_id")
    assert not new_session.get("pending_user_message")
    assert new_session["worktree_path"] == str(tmp_path / "task-worktree")
    assert new_session["worktree_branch"] == "codex/task"
    assert new_session["composer_draft"]["text"] == (
        f"Continue: {latest_request}\n\n"
        "Context recovery note: inspect the current workspace and existing results "
        "before repeating any action."
    )
    assert new_session["composer_draft"]["files"] == []

    saved = json.loads((session_dir / f"{new_session['session_id']}.json").read_text(encoding="utf-8"))
    assert saved["parent_session_id"] == sid
    assert saved["session_source"] == "fork"
    assert len(saved["context_messages"]) == 1
    recovery_context = saved["context_messages"][0]
    assert recovery_context["role"] == "assistant"
    assert recovery_context["_compressed_summary"] is True
    assert recovery_context["_compression_recovery_reference"] is True
    assert recovery_context["content"].startswith("[CONTEXT COMPACTION — REFERENCE ONLY] Newest summary")
    assert "LATEST SUMMARY TAIL" in recovery_context["content"]
    assert old_summary not in recovery_context["content"]
    assert "raw tool output" not in recovery_context["content"]
    assert "sk_live_1234567890secret" not in recovery_context["content"]
    assert len(recovery_context["content"]) <= 8_000
    assert saved["composer_draft"] == new_session["composer_draft"]
    assert saved["compression_recovery_source_session_id"] == sid
    assert saved["compression_recovery_action"] == "start_focused_continuation"
    assert compression_recovery_payload_for_session(session)["recommended_action"] == "start_focused_continuation"


def test_recovery_child_does_not_merge_parent_transcript(monkeypatch, tmp_path):
    _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrcisolate"
    session = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "long task"},
            {"role": "assistant", "content": "compression exhausted"},
        ],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})
    payload = _payload(handler)
    child_id = payload["session"]["session_id"]
    child = models.SESSIONS[child_id]

    assert child.messages == []
    assert routes._merged_webui_lineage_messages_for_display(child) == []


def test_recovery_start_without_summary_still_preserves_latest_real_request(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverynosummary"
    latest_request = "Repair the payment reconciliation regression."
    session = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": latest_request},
            {"role": "user", "content": "handoff"},
        ],
        context_messages=[
            {"role": "tool", "content": "tool output is not recovery context"},
        ],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})
    payload = _payload(handler)
    saved = json.loads(
        (session_dir / f"{payload['session']['session_id']}.json").read_text(
            encoding="utf-8"
        )
    )

    assert handler.status == 200
    assert saved["context_messages"] == []
    assert latest_request in saved["composer_draft"]["text"]
    assert "inspect the current workspace" in saved["composer_draft"]["text"]
    assert saved["messages"] == []


def test_recovery_start_reuses_existing_focused_session(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrc2"
    session = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    session.save()
    models.SESSIONS[sid] = session
    routes.SESSIONS[sid] = session

    first_handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(first_handler, {"session_id": sid})
    first_payload = _payload(first_handler)
    first_child_id = first_payload["session"]["session_id"]
    edited_draft = {"text": "User edited this recovery draft.", "files": []}
    models.SESSIONS[first_child_id].composer_draft = edited_draft
    models.SESSIONS[first_child_id].save()

    second_handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(second_handler, {"session_id": sid})
    second_payload = _payload(second_handler)

    assert second_handler.status == 200
    assert second_payload["session"]["session_id"] == first_child_id
    assert second_payload["session"]["composer_draft"] == edited_draft
    assert second_payload["message"].startswith("Opened the existing")

    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    third_handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(third_handler, {"session_id": sid})
    third_payload = _payload(third_handler)

    assert third_handler.status == 200
    assert third_payload["session"]["session_id"] == first_child_id
    assert third_payload["session"]["composer_draft"] == edited_draft

    recovery_children = []
    for path in session_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("compression_recovery_source_session_id") == sid:
            recovery_children.append(data)
    assert len(recovery_children) == 1


def test_recovery_start_ignores_existing_child_from_other_profile(monkeypatch, tmp_path):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrcprofile"
    source = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        profile="default",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(source, message="Context length exceeded.")
    source.save()
    foreign_child = Session(
        session_id="foreignchild1",
        title="Foreign focused continuation",
        workspace=str(tmp_path),
        model="gpt-4o",
        profile="other-profile",
        messages=[],
        parent_session_id=sid,
        compression_recovery_source_session_id=sid,
        compression_recovery_action="start_focused_continuation",
    )
    foreign_child.save()
    models.SESSIONS.clear()
    routes.SESSIONS.clear()
    models.SESSIONS[sid] = source
    routes.SESSIONS[sid] = source

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})
    payload = _payload(handler)

    assert handler.status == 200
    assert payload["session"]["session_id"] != "foreignchild1"
    assert payload["session"]["profile"] == "default"

    recovery_children = []
    for path in session_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("compression_recovery_source_session_id") == sid:
            recovery_children.append(data)
    assert {child["profile"] for child in recovery_children} == {"default", "other-profile"}


def test_recovery_metadata_is_persisted_and_exposed_in_compact_session():
    session = Session(session_id="recovermeta1", title="x", messages=[])
    recovery = stamp_compression_exhausted_recovery(session, message="Context length exceeded.")
    compact = session.compact()

    assert recovery["terminal_state"] == "compression_exhausted"
    assert compact["recommended_recovery_action"] == "start_focused_continuation"
    assert compact["compression_recovery"]["recommended_action"] == "start_focused_continuation"


def test_recovery_child_markers_round_trip_through_state_db_sidecar_rebuild(tmp_path):
    db = WebUIJsonSessionDB(tmp_path)
    db.write_session(
        {
            "session_id": "recoverychild1",
            "title": "Focused continuation",
            "model": "gpt-4o",
            "started_at": 1700000000,
            "messages": [],
            "parent_session_id": "recoverysrc3",
            "compression_recovery_source_session_id": "recoverysrc3",
            "compression_recovery_action": "start_focused_continuation",
        }
    )
    row = db.list_sessions()[0]

    assert row["compression_recovery_source_session_id"] == "recoverysrc3"
    assert row["compression_recovery_action"] == "start_focused_continuation"

    sidecar = _state_db_row_to_sidecar({"id": "recoverychild1", **row, "messages": []})

    assert sidecar["compression_recovery_source_session_id"] == "recoverysrc3"
    assert sidecar["compression_recovery_action"] == "start_focused_continuation"


def test_recovery_source_metadata_round_trips_through_state_db_sidecar_rebuild(tmp_path):
    recovery = {
        "type": "compression_recovery_required",
        "terminal_state": "compression_exhausted",
        "recommended_action": "start_focused_continuation",
        "source_session_id": "recoverysrc4",
    }
    db = WebUIJsonSessionDB(tmp_path)
    db.write_session(
        {
            "session_id": "recoverysrc4",
            "title": "Exhausted source",
            "model": "gpt-4o",
            "started_at": 1700000000,
            "messages": [{"role": "user", "content": "long task"}],
            "compression_recovery": recovery,
            "recommended_recovery_action": "start_focused_continuation",
        }
    )
    row = db.list_sessions()[0]

    sidecar = _state_db_row_to_sidecar({"id": "recoverysrc4", **row, "messages": []})

    assert sidecar["compression_recovery"] == recovery
    assert sidecar["recommended_recovery_action"] == "start_focused_continuation"


@pytest.fixture(scope="module")
def recovery_ui_node_result():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    harness = textwrap.dedent(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = src.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = src.indexOf('{', start);
          let depth = 1; i++;
          while (depth > 0 && i < src.length) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}') depth--;
            i++;
          }
          return src.slice(start, i);
        }
        const S = {
          session: {
            session_id: 'dead-parent',
            compression_recovery: {
              terminal_state: 'compression_exhausted',
              recommended_action: 'start_focused_continuation',
            },
          },
          messages: [],
        };
        const esc = value => String(value).replace(/[&<>"']/g, char => ({
          '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
        })[char]);
        const li = () => '';
        let starts = 0;
        let hinted = 0;
        async function startCompressionRecovery(btn) {
          if (btn !== null) throw new Error('send redirect must not require a card button');
          starts += 1;
        }
        function showCompressionRecoveryContinuationHint(){ hinted += 1; }
        eval(extractFunc('_activeCompressionRecoveryPayload'));
        eval(extractFunc('shouldInterceptCompressionRecoveryContinuation'));
        eval(extractFunc('redirectCompressionRecoverySend'));
        eval(extractFunc('_compressionRecoverySourceHtml'));
        (async () => {
          const redirectResult = await redirectCompressionRecoverySend();
          console.log(JSON.stringify({
            intercepts: {
              generic: shouldInterceptCompressionRecoveryContinuation('continue', []),
              substantive: shouldInterceptCompressionRecoveryContinuation('inspect the repo', []),
              attachments: shouldInterceptCompressionRecoveryContinuation(
                'inspect this evidence',
                [{name: 'evidence.txt'}],
              ),
            },
            redirect: {result: redirectResult, starts, hinted},
            sourceHtml: _compressionRecoverySourceHtml({
              compression_recovery_source_session_id: 'parent<unsafe>',
            }),
          }));
        })().catch(err => { console.error(err); process.exit(1); });
        """
    )
    proc = subprocess.run(
        [node, "-e", harness, str(ROOT / "static" / "ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_compression_recovery_intercepts_every_parent_send_in_real_js(
    recovery_ui_node_result,
):
    assert recovery_ui_node_result["intercepts"] == {
        "generic": True,
        "substantive": True,
        "attachments": True,
    }


def test_compression_recovery_send_redirect_starts_recovery_without_submit(
    recovery_ui_node_result,
):
    assert recovery_ui_node_result["redirect"] == {
        "result": True,
        "starts": 0,
        "hinted": 1,
    }


@pytest.fixture(scope="module")
def compression_recovery_409_node_result():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    harness = textwrap.dedent(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = src.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = src.indexOf('{', start);
          let depth = 1; i++;
          while (depth > 0 && i < src.length) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}') depth--;
            i++;
          }
          return src.slice(start, i);
        }
        let parsed = null;
        let missing = false;
        try {
          eval(extractFunc('_compressionRecoveryPayloadFrom409'));
          const body = {
            type: 'compression_recovery_required',
            session_id: 'parent-1',
            recommended_recovery_action: 'start_focused_continuation',
            compression_recovery: {
              source_session_id: 'parent-1',
              terminal_state: 'compression_exhausted',
              recommended_action: 'start_focused_continuation',
            },
          };
          parsed = {
            valid: !!_compressionRecoveryPayloadFrom409(
              {status: 409, body: JSON.stringify(body)},
              'parent-1',
            ),
            wrongSession: !!_compressionRecoveryPayloadFrom409(
              {status: 409, body: JSON.stringify(body)},
              'parent-2',
            ),
            malformed: !!_compressionRecoveryPayloadFrom409(
              {status: 409, body: '{not-json'},
              'parent-1',
            ),
          };
        } catch (err) {
          missing = true;
          parsed = {error: String(err && err.message || err)};
        }
        console.log(JSON.stringify({missing, parsed}));
        """
    )
    proc = subprocess.run(
        [node, "-e", harness, str(ROOT / "static" / "messages.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_compression_recovery_409_parser_is_session_scoped(
    compression_recovery_409_node_result,
):
    assert compression_recovery_409_node_result["missing"] is False
    assert compression_recovery_409_node_result["parsed"] == {
        "valid": True,
        "wrongSession": False,
        "malformed": False,
    }


@pytest.fixture(scope="module")
def compression_recovery_409_handler_node_result():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    harness = textwrap.dedent(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
        function extractFunc(name, source = src) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = source.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = source.indexOf('{', start);
          let depth = 1; i++;
          while (depth > 0 && i < source.length) {
            if (source[i] === '{') depth++;
            else if (source[i] === '}') depth--;
            i++;
          }
          return source.slice(start, i);
        }
        const sid = 'parent-1';
        const optimistic = {role: 'user', content: 'draft'};
        const input = {value: ''};
        const S = {
          session: {session_id: sid},
          messages: [optimistic],
          pendingFiles: [],
          toolCalls: [],
          activeStreamId: 'stream-1',
        };
        const INFLIGHT = {[sid]: {messages: [optimistic]}};
        const $ = name => name === 'msg' ? input : null;
        const calls = {load: null, hint: 0, render: 0, starts: 0, shifts: 0, sends: 0};
        const autoResize = () => {};
        const updateSendBtn = () => {};
        const renderTray = () => {};
        const _saveComposerDraftNow = () => {};
        const clearInflightState = () => {};
        const stopApprovalPolling = () => {};
        const stopClarifyPolling = () => {};
        const hideApprovalCard = () => {};
        const hideClarifyCard = () => {};
        const removeThinking = () => {};
        const setComposerStatus = () => {};
        const clearOptimisticSessionStreaming = () => {};
        const renderSessionList = () => {};
        const renderMessages = () => { calls.render += 1; };
        const showCompressionRecoveryContinuationHint = () => { calls.hint += 1; };
        const _clearActivityElapsedTimer = () => {};
        const setStatus = () => {};
        const updateQueueBadge = () => {};
        const shiftQueuedSessionMessage = () => {
          calls.shifts += 1;
          return {text: 'queued follow-up', files: [{name: 'queued.txt'}]};
        };
        const send = () => { calls.sends += 1; };
        const setTimeout = fn => { fn(); return 1; };
        let _queueDrainSid = sid;
        async function loadSession(_sid, opts) { calls.load = opts; }
        let _approvalSessionId = null;
        let _clarifySessionId = null;
        eval(extractFunc('setBusy', uiSrc));
        eval(extractFunc('_restoreComposerDraftAfterFailedSend'));
        eval(extractFunc('_compressionRecoveryPayloadFrom409'));
        eval(extractFunc('_handleCompressionRecovery409'));
        const body = {
          type: 'compression_recovery_required',
          session_id: sid,
          recommended_recovery_action: 'start_focused_continuation',
          compression_recovery: {
            source_session_id: sid,
            terminal_state: 'compression_exhausted',
            recommended_action: 'start_focused_continuation',
          },
        };
        (async () => {
          const handled = await _handleCompressionRecovery409(
            {status: 409, body: JSON.stringify(body)},
            sid,
            optimistic,
            'draft',
            [{name: 'a.txt'}, {name: 'b.txt'}],
            null,
          );
          console.log(JSON.stringify({
            handled,
            messages: S.messages.length,
            draft: input.value,
            files: S.pendingFiles.length,
            load: calls.load,
            hint: calls.hint,
            starts: calls.starts,
            queueShifts: calls.shifts,
            queuedSends: calls.sends,
          }));
        })().catch(err => { console.error(err); process.exit(1); });
        """
    )
    proc = subprocess.run(
        [
            node,
            "-e",
            harness,
            str(ROOT / "static" / "messages.js"),
            str(ROOT / "static" / "ui.js"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_compression_recovery_409_restores_draft_and_never_starts_child(
    compression_recovery_409_handler_node_result,
):
    result = compression_recovery_409_handler_node_result
    assert result["handled"] is True
    assert result["messages"] == 0
    assert result["draft"] == "draft"
    assert result["files"] == 2
    assert result["load"]["force"] is True
    assert result["load"]["keepStaleUntilLoaded"] is True
    assert result["load"]["preserveActiveInput"] is True
    assert result["hint"] == 1
    assert result["starts"] == 0
    assert result["queueShifts"] == 0
    assert result["queuedSends"] == 0


def test_compression_recovery_child_renders_safe_source_history_link(
    recovery_ui_node_result,
):
    html = recovery_ui_node_result["sourceHtml"]
    assert "Open source history" in html
    assert 'data-recovery-source-session-id="parent&lt;unsafe&gt;"' in html
    assert "parent<unsafe>" not in html


def test_compression_recovery_ui_wires_card_action_and_send_intercept():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")

    assert "function _compressionRecoveryHtml" in ui
    assert "data-compression-recovery-card=\"1\"" in ui
    assert "api('/api/session/compression-recovery/start'" in ui
    assert "Compression recovery did not return a session." in ui
    assert "const sid=String(recovery.source_session_id||sessionId||'')" in ui
    assert "function shouldInterceptCompressionRecoveryContinuation" in ui
    assert "shouldInterceptCompressionRecoveryContinuation(text,S.pendingFiles)" in messages
    assert "await redirectCompressionRecoverySend()" in messages
    assert messages.index("await redirectCompressionRecoverySend()") < messages.index(
        "const _failedSendDraftText=text;"
    )
    render_start = ui.index("function renderMessages(options){")
    render_body = ui[render_start:render_start + 30_000]
    assert "const recoverySourceHtml=_compressionRecoverySourceHtml(S.session);" in render_body
    assert "inner.appendChild(recoverySourceNode);" in render_body
    assert "recoverySourceHtml" in render_body[render_body.index("$('emptyState')"):]
    assert "_compressionRecovery:recovery||undefined" in messages


def test_compression_recovery_ui_renders_session_level_recovery_on_terminal_message():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("const recoveryPayload=(!isUser&&m._compressionRecovery)")
    end = ui.index("const statusHtml", start)
    body = ui[start:end]

    assert "? m._compressionRecovery" in body
    assert "_activeCompressionRecoveryPayload()" in body
    assert "isLastAssistant&&isTurnFinalAssistant" in body
    assert "typeof _activeCompressionRecoveryPayload==='function'" in body
    assert body.index("m._compressionRecovery") < body.index("_activeCompressionRecoveryPayload()")


def test_compression_recovery_ui_skips_message_fallback_after_session_clear():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("function _activeCompressionRecoveryPayload(){")
    end = ui.index("function shouldInterceptCompressionRecoveryContinuation", start)
    body = ui[start:end]

    session_guard = "Object.prototype.hasOwnProperty.call(S.session,'compression_recovery')"
    message_scan = "const messages=Array.isArray(S.messages)?S.messages:[]"

    assert session_guard in body
    assert message_scan in body
    assert body.index(session_guard) < body.index(message_scan)


def test_compression_recovery_action_handles_stale_card_409():
    """A 409 (recovery already cleared) must be mapped to a neutral note and the
    stale card retired — not surfaced as a raw 'Compression recovery failed' error.
    """
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    start = ui.index("async function startCompressionRecovery(btn){")
    end = ui.index("\n}", ui.index("finally", start))
    body = ui[start:end]

    # Branches on the HTTP status the api() wrapper attaches (err.status).
    assert "e.status===409" in body
    # Retires the stale persisted card so it is no longer clickable.
    assert "data-compression-recovery-consumed" in body
    # Neutral/info toast, not the generic error path.
    assert "no longer available" in body
    # The 409 branch returns before falling through to the generic error toast.
    assert body.index("e.status===409") < body.index("Compression recovery failed:")
    # The finally-block must NOT re-enable a retired stale-card button.
    assert "retiredRecoveryCard" in body
    assert "if(!retiredRecoveryCard) btn.disabled=false" in body
