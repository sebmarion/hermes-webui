import io
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from api import config, models, routes
from api.compression_recovery import (
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


def _node_result(source: str, *paths: Path) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", textwrap.dedent(source), *map(str, paths)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_generic_continuation_intent_is_scoped_to_empty_continue_words():
    assert is_generic_continuation_intent("continue")
    assert is_generic_continuation_intent("继续吧。")
    assert is_generic_continuation_intent("go on")
    assert not is_generic_continuation_intent("continue by summarizing the workspace changes")
    assert not is_generic_continuation_intent("继续修复 4685 的恢复卡")


def test_retired_manual_start_endpoint_never_creates_or_navigates_to_a_child(
    monkeypatch, tmp_path
):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    sid = "recoverysrc1"
    source = Session(
        session_id=sid,
        title="Long task",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "long task"}],
    )
    stamp_compression_exhausted_recovery(source, message="Context length exceeded.")
    source.save()
    models.SESSIONS[sid] = source
    routes.SESSIONS[sid] = source

    handler = _JSONHandler()
    routes._handle_session_compression_recovery_start(handler, {"session_id": sid})

    assert handler.status == 409
    assert _payload(handler) == {
        "error": "Compression recovery is automatic in this conversation. Reload to attach.",
        "type": "reload_required",
        "reload_required": True,
        "session_id": sid,
    }
    assert [path.name for path in session_dir.glob("*.json") if not path.name.startswith("_")] == [
        f"{sid}.json"
    ]


def test_loading_safe_legacy_marker_adopts_recovery_in_the_same_task(
    monkeypatch, tmp_path
):
    session_dir = _isolate_sessions(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    sid = "legacy-parent"
    request = "Ok audit it and do the other steps you said"
    source = Session(
        session_id=sid,
        title="Existing conversation",
        profile="default",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Assess the integration."},
            {
                "role": "assistant",
                "content": (
                    "Audit the archive, run the isolated plugin check, verify strict "
                    "binding, and report the final diff."
                ),
            },
            {"role": "user", "content": request},
        ],
    )
    stamp_compression_exhausted_recovery(source, message="Context length exceeded.")
    source.save()
    models.SESSIONS[sid] = source
    routes.SESSIONS[sid] = source
    starts = []

    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda session_id, prompt, **kwargs: starts.append(
            (session_id, prompt, kwargs)
        )
        or {"_status": 200, "session_id": session_id, "stream_id": "legacy-recovery"},
    )
    monkeypatch.setattr(
        routes,
        "start_admitted_auxiliary_thread",
        lambda *, target, **_kwargs: target(),
    )

    adopted = routes._maybe_adopt_legacy_compression_recovery_on_session_load(source)

    assert adopted is True
    assert len(starts) == 1
    assert starts[0][0] == sid
    assert starts[0][2]["source"] == "compression_recovery"
    assert starts[0][2]["recovery_context_messages"][-1] == {
        "role": "user",
        "content": request,
    }
    assert [path.name for path in session_dir.glob("*.json") if not path.name.startswith("_")] == [
        f"{sid}.json"
    ]


def test_legacy_recovery_metadata_is_still_exposed_for_in_place_adoption():
    session = Session(session_id="recovermeta1", title="x", messages=[])
    recovery = stamp_compression_exhausted_recovery(
        session, message="Context length exceeded."
    )
    compact = session.compact()

    assert recovery["terminal_state"] == "compression_exhausted"
    assert compact["recommended_recovery_action"] == "start_focused_continuation"
    assert compact["compression_recovery"]["recommended_action"] == "start_focused_continuation"


def test_legacy_recovery_child_markers_round_trip_through_state_db(tmp_path):
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
    sidecar = _state_db_row_to_sidecar(
        {"id": "recoverychild1", **row, "messages": []}
    )

    assert sidecar["compression_recovery_source_session_id"] == "recoverysrc3"
    assert sidecar["compression_recovery_action"] == "start_focused_continuation"


def test_recovery_source_metadata_round_trips_through_state_db(tmp_path):
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
    sidecar = _state_db_row_to_sidecar(
        {"id": "recoverysrc4", **row, "messages": []}
    )

    assert sidecar["compression_recovery"] == recovery
    assert sidecar["recommended_recovery_action"] == "start_focused_continuation"


def test_cached_recovery_409_parser_is_same_task_scoped():
    result = _node_result(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = src.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = src.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < src.length) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}') depth--;
            i++;
          }
          return src.slice(start, i);
        }
        eval(extractFunc('_compressionRecoveryPayloadFrom409'));
        const legacy = {
          type: 'compression_recovery_required',
          session_id: 'parent-1',
          compression_recovery: {
            source_session_id: 'parent-1',
            terminal_state: 'compression_exhausted',
            recommended_action: 'start_focused_continuation',
          },
        };
        const reload = {
          type: 'reload_required',
          reload_required: true,
          session_id: 'parent-1',
        };
        console.log(JSON.stringify({
          legacy: !!_compressionRecoveryPayloadFrom409(
            {status: 409, body: JSON.stringify(legacy)}, 'parent-1'),
          reload: !!_compressionRecoveryPayloadFrom409(
            {status: 409, body: JSON.stringify(reload)}, 'parent-1'),
          wrong: !!_compressionRecoveryPayloadFrom409(
            {status: 409, body: JSON.stringify(reload)}, 'parent-2'),
          malformed: !!_compressionRecoveryPayloadFrom409(
            {status: 409, body: '{bad'}, 'parent-1'),
        }));
        """,
        ROOT / "static" / "messages.js",
    )

    assert result == {"legacy": True, "reload": True, "wrong": False, "malformed": False}


def test_cached_recovery_409_restores_draft_and_reloads_same_task():
    result = _node_result(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        const ui = fs.readFileSync(process.argv[2], 'utf8');
        function extractFunc(name, source=src) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = source.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = source.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < source.length) {
            if (source[i] === '{') depth++;
            else if (source[i] === '}') depth--;
            i++;
          }
          return source.slice(start, i);
        }
        const sid = 'parent-1';
        const optimistic = {role:'user',content:'draft'};
        const input = {value:''};
        const S = {session:{session_id:sid},messages:[optimistic],pendingFiles:[],toolCalls:[],activeStreamId:'old'};
        const INFLIGHT = {[sid]:{}};
        const $ = name => name==='msg' ? input : null;
        const calls = {load:null, renders:0, shifts:0, sends:0};
        const autoResize=()=>{}, updateSendBtn=()=>{}, renderTray=()=>{};
        const _saveComposerDraftNow=()=>{}, clearInflightState=()=>{};
        const stopApprovalPolling=()=>{}, stopClarifyPolling=()=>{};
        const hideApprovalCard=()=>{}, hideClarifyCard=()=>{}, removeThinking=()=>{};
        const setComposerStatus=()=>{}, clearOptimisticSessionStreaming=()=>{};
        const renderSessionList=()=>{}, renderMessages=()=>{calls.renders+=1;};
        const _clearActivityElapsedTimer=()=>{}, setStatus=()=>{}, updateQueueBadge=()=>{};
        const shiftQueuedSessionMessage=()=>{calls.shifts+=1;};
        const send=()=>{calls.sends+=1;};
        const setTimeout=fn=>{fn();return 1;};
        let _queueDrainSid=sid, _approvalSessionId=null, _clarifySessionId=null;
        async function loadSession(_sid,opts){calls.load={sid:_sid,opts};}
        eval(extractFunc('setBusy',ui));
        eval(extractFunc('_restoreComposerDraftAfterFailedSend'));
        eval(extractFunc('_compressionRecoveryPayloadFrom409'));
        eval(extractFunc('_handleCompressionRecovery409'));
        (async()=>{
          const body={type:'reload_required',reload_required:true,session_id:sid};
          const handled=await _handleCompressionRecovery409(
            {status:409,body:JSON.stringify(body)},sid,optimistic,'draft',
            [{name:'a.txt'},{name:'b.txt'}],null);
          console.log(JSON.stringify({
            handled,messages:S.messages.length,draft:input.value,files:S.pendingFiles.length,
            load:calls.load,renders:calls.renders,shifts:calls.shifts,sends:calls.sends,
          }));
        })().catch(error=>{console.error(error);process.exit(1);});
        """,
        ROOT / "static" / "messages.js",
        ROOT / "static" / "ui.js",
    )

    assert result["handled"] is True
    assert result["messages"] == 0
    assert result["draft"] == "draft"
    assert result["files"] == 2
    assert result["load"] == {
        "sid": "parent-1",
        "opts": {"force": True, "keepStaleUntilLoaded": True, "preserveActiveInput": True},
    }
    assert result["renders"] == 0
    assert result["shifts"] == 0
    assert result["sends"] == 0


def test_historical_focused_child_keeps_safe_source_history_link():
    result = _node_result(
        r"""
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('function\\s+' + name + '\\s*\\(');
          const start = src.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = src.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < src.length) {
            if (src[i] === '{') depth++;
            else if (src[i] === '}') depth--;
            i++;
          }
          return src.slice(start, i);
        }
        const esc = value => String(value).replace(/[&<>"']/g, char => ({
          '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
        })[char]);
        const li=()=>'';
        eval(extractFunc('_compressionRecoverySourceHtml'));
        console.log(JSON.stringify({html:_compressionRecoverySourceHtml({
          compression_recovery_source_session_id:'parent<unsafe>',
        })}));
        """,
        ROOT / "static" / "ui.js",
    )
    html = result["html"]
    assert "Open source history" in html
    assert 'data-recovery-source-session-id="parent&lt;unsafe&gt;"' in html
    assert "parent<unsafe>" not in html
    assert "read-only" not in html
