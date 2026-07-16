"""End-to-end recurrence guards for cross-thread compaction contamination.

These tests deliberately span the seams that the original incident crossed:
the live composer/send state machine, the HTTP chat-start boundary, the cold
and warm sidebar projections, and transcript preprocessing after compaction.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

import api.models as models
import api.profiles as profiles
import api.routes as routes
from tests._pytest_port import BASE


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(source: str, name: str) -> str:
    """Extract a top-level JS function while tolerating nested blocks."""
    markers = (f"async function {name}", f"function {name}")
    starts = [source.find(marker) for marker in markers]
    start = min(idx for idx in starts if idx >= 0)
    paren = source.index("(", start)
    paren_depth = 1
    signature_end = paren + 1
    while paren_depth:
        char = source[signature_end]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
        signature_end += 1
    brace = source.index("{", signature_end)
    depth = 1
    idx = brace + 1
    while depth:
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    return source[start:idx]


def _run_node(script: str) -> dict:
    if NODE is None:  # pragma: no cover - local test prerequisite
        pytest.skip("node not available")
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_send_race_queues_second_message_for_new_visible_session():
    """Exercise the complete send() state machine across its first async yield.

    A starts uploading, the user switches to B, then sends again. The second
    payload must be queued for B even though A still owns the in-flight send.
    """
    send_fn = _function_source(MESSAGES_JS, "send")
    composer_text_fn = _function_source(
        MESSAGES_JS,
        "_composerTextWithPendingSelections",
    )
    ownership_fn = _function_source(MESSAGES_JS, "_composerOwnershipSnapshot")
    script = textwrap.dedent(
        f"""
        const input={{value:'message for A'}};
        const modelSelect={{value:'model-A'}};
        const queued=[];
        const statuses=[];
        const $=(id)=>id==='msg'?input:(id==='model'?modelSelect:null);
        const document={{
          querySelector:()=>null,
          getElementById:(id)=>id==='msg'?input:null,
        }};
        const window={{}};
        const _pendingSelections=[];
        function _formatSelectedTextReplyQuote(text){{return text;}}
        {composer_text_fn}

        let _loadingSessionId=null;
        let ownerSid='session-A';
        function _composerDraftOwnerSessionId(){{return ownerSid;}}
        {ownership_fn}

        let _sendInProgress=false;
        let _sendInProgressSid=null;
        let _forcedSkillDirectivePending=null;
        const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
        const COMMANDS=[];
        const S={{
          session:{{session_id:'session-A',model:'model-A',model_provider:'provider-A'}},
          messages:[],
          pendingFiles:[],
          activeProfile:'default',
          activeStreamId:null,
          busy:false,
        }};

        function _warnComposerOwnershipMismatch(){{throw new Error('ownership mismatch');}}
        function _chatPayloadModelState(){{
          return {{model:S.session.model,model_provider:S.session.model_provider}};
        }}
        function _clearStaleBusyStateBeforeSend(){{}}
        function _flushSelectionBlocksToComposer(){{}}
        function autoResize(){{}}
        function setComposerStatus(value){{statuses.push(value);}}
        function renderTray(){{}}
        function updateQueueBadge(){{}}
        function showToast(){{}}
        function queueSessionMessage(sid,payload){{queued.push({{sid,payload}});}}
        function _clearComposerAfterQueuedSelectionSend(){{input.value='';}}
        function uploadPendingFiles(){{return new Promise(()=>{{}});}}

        {send_fn}

        (async()=>{{
          const firstSend=send();
          if(!_sendInProgress||_sendInProgressSid!=='session-A'){{
            throw new Error('first send did not reach the upload await for A');
          }}
          S.session={{session_id:'session-B',model:'model-B',model_provider:'provider-B'}};
          ownerSid='session-B';
          input.value='message for B';
          await send();
          process.stdout.write(JSON.stringify({{
            queued,
            inflightSid:_sendInProgressSid,
            input:input.value,
            statuses,
          }}));
          void firstSend;
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )

    result = _run_node(script)

    assert result["inflightSid"] == "session-A"
    assert result["input"] == ""
    assert result["queued"] == [
        {
            "sid": "session-B",
            "payload": {
                "text": "message for B",
                "files": [],
                "model": "model-B",
                "model_provider": "provider-B",
                "profile": "default",
            },
        }
    ]


def _http_json(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as error:
        return json.loads(error.read()), error.code


def test_chat_start_owner_mismatch_is_http_409_and_mutates_neither_session():
    """Prove the public route rejects crossed ownership before any turn starts."""
    created: list[str] = []
    try:
        for _ in range(2):
            payload, status = _http_json("POST", "/api/session/new", {})
            assert status == 200
            created.append(payload["session"]["session_id"])
        session_a, session_b = created

        health_before, health_status = _http_json("GET", "/health")
        assert health_status == 200
        response, status = _http_json(
            "POST",
            "/api/chat/start",
            {
                "session_id": session_b,
                "composer_session_id": session_a,
                "message": "this belongs to A",
            },
        )

        assert status == 409
        assert response["type"] == "composer_session_mismatch"
        assert response["session_id"] == session_b
        assert response["composer_session_id"] == session_a

        for session_id in (session_a, session_b):
            detail, detail_status = _http_json(
                "GET",
                "/api/session?session_id=" + urllib.parse.quote(session_id),
            )
            assert detail_status == 200
            session = detail["session"]
            assert session.get("messages") == []
            assert session.get("context_messages", []) == []
            assert int(session.get("message_count", 0)) == 0
            assert not session.get("active_stream_id")

        health_after, health_status = _http_json("GET", "/health")
        assert health_status == 200
        assert health_after["active_runs"] == health_before["active_runs"]
        assert health_after["active_streams"] == health_before["active_streams"]
    finally:
        for session_id in created:
            _http_json("POST", "/api/session/delete", {"session_id": session_id})


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _handle_sessions(url: str):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


def _sidebar_row(session_id: str, *, draft_text: str = "") -> dict:
    return {
        "session_id": session_id,
        "title": f"Recovered {session_id}",
        "profile": "default",
        "archived": False,
        "message_count": 0,
        "updated_at": 1000,
        "last_message_at": 1000,
        "source": "webui",
        "raw_source": "webui",
        "session_source": "webui",
        "source_tag": "webui",
        "default_hidden": False,
        "composer_draft": {"text": draft_text, "files": []},
    }


def test_bare_sessions_cold_seed_and_warm_cache_keep_draft_only_successor(
    monkeypatch,
):
    """The default cross-client projection must agree before and after rebuild."""
    successor = _sidebar_row("draft-successor", draft_text="continue recovered task")
    empty_ghost = _sidebar_row("empty-ghost")
    rebuilt = threading.Event()
    routes._session_list_cache_clear()
    monkeypatch.setenv("HERMES_WEBUI_SESSION_PROJECTION_V2", "0")
    monkeypatch.setattr(
        routes,
        "load_settings",
        lambda: {
            "show_cli_sessions": False,
            "show_claude_code_sessions": False,
            "show_previous_messaging_sessions": False,
            "show_cron_sessions": False,
            "show_webhook_sessions": False,
            "api_redact_enabled": False,
        },
    )
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))
    monkeypatch.setattr(
        models,
        "read_session_index_projection",
        lambda: [dict(successor)],
    )
    monkeypatch.setattr(
        models,
        "_apply_session_index_state_db_overrides",
        lambda _rows, **_kwargs: None,
    )
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda _cls, _sid: None),
    )
    monkeypatch.setattr(
        routes,
        "all_sessions",
        lambda **_kwargs: [dict(successor), dict(empty_ghost)],
    )
    monkeypatch.setattr(
        routes,
        "_schedule_stale_stream_state_reconciliation",
        lambda _rows: False,
    )
    monkeypatch.setattr(routes, "_session_attention_summary", lambda _sid: None)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(
        routes,
        "agent_session_zero_message_sids",
        lambda ids, profile=None: frozenset(ids),
    )
    monkeypatch.setattr(
        routes,
        "_load_webui_zero_message_orphan_tombstone",
        lambda: frozenset(),
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda _sid: None)
    monkeypatch.setattr(
        routes,
        "_record_webui_zero_message_orphan_tombstone",
        lambda _sid: None,
    )
    monkeypatch.setattr(
        routes,
        "_clear_webui_zero_message_orphan_tombstone",
        lambda _sid: None,
    )
    original_builder = routes._build_session_list_cache_payload

    def observed_builder(*args, **kwargs):
        payload = original_builder(*args, **kwargs)
        rebuilt.set()
        return payload

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", observed_builder)
    try:
        cold = _handle_sessions("http://example.com/api/sessions")
        assert cold.status == 200
        assert rebuilt.wait(5), "background sidebar rebuild did not finish"
        warm = _handle_sessions("http://example.com/api/sessions")
        assert warm.status == 200

        cold_ids = [row["session_id"] for row in cold.json_body()["sessions"]]
        warm_ids = [row["session_id"] for row in warm.json_body()["sessions"]]
        assert cold_ids == ["draft-successor"]
        assert warm_ids == cold_ids
        assert "empty-ghost" not in warm_ids
    finally:
        routes._session_list_cache_clear()


def test_compacted_transcript_preprocessing_removes_incident_noise_as_one_flow():
    """Cover the combined messy screenshot shape, not just each helper alone."""
    names = (
        "_stripWorkspaceDisplayPrefix",
        "_isContextCompactionText",
        "_isPreservedCompressionTaskListMessage",
        "_messageIsRenderable",
        "_getVisibleMessagesWithIdx",
        "_latestTodoToolItems",
        "_hasActiveTodoItems",
        "_compressionModeForSession",
        "_latestPreservedCompressionTaskListMessages",
        "_shouldShowSettledCompressionReference",
    )
    functions = "\n".join(_function_source(UI_JS, name) for name in names)
    script = textwrap.dedent(
        f"""
        function msgContent(message){{
          if(typeof message.content==='string') return message.content;
          if(Array.isArray(message.content)) return message.content
            .filter(part=>part&&part.type==='text')
            .map(part=>part.text||'').join('');
          return '';
        }}
        function _isContextCompactionMessage(message){{
          return !!message && _isContextCompactionText(msgContent(message));
        }}
        function _isRecoveryControlMessage(){{return false;}}
        function _messageHasReasoningPayload(){{return false;}}
        function _assistantMessageHasVisibleContent(message){{return !!msgContent(message);}}
        let _visWithIdxCache=null;
        let _visWithIdxCacheLen=0;
        let _visWithIdxCacheSrc=null;
        let S=null;
        {functions}

        const messages=[
          {{role:'user',content:'[Workspace::v1: /Users/seb]\\n[Workspace: /Users/seb]\\nGo'}},
          {{role:'assistant',content:'[CONTEXT COMPACTION — REFERENCE ONLY] old summary'}},
          {{role:'user',content:'[Your active task list was preserved across context compression]\\n- [>] inspect'}},
          {{role:'assistant',content:'Clean final answer'}},
        ];
        function scenario(mode){{
          S={{session:{{compression_anchor_mode:mode}},messages}};
          _visWithIdxCache=null;
          _visWithIdxCacheLen=0;
          _visWithIdxCacheSrc=null;
          const visible=_getVisibleMessagesWithIdx().map(item=>({{
            role:item.m.role,
            content:item.m.role==='user'
              ? _stripWorkspaceDisplayPrefix(msgContent(item.m))
              : msgContent(item.m),
          }}));
          return {{
            visible,
            preserved:_latestPreservedCompressionTaskListMessages(messages).length,
            settledReference:_shouldShowSettledCompressionReference('human-authored summary'),
            compactionReference:_shouldShowSettledCompressionReference('[CONTEXT COMPACTION] generated'),
          }};
        }}
        process.stdout.write(JSON.stringify({{
          automatic:scenario('summary_compaction'),
          manual:scenario('manual'),
        }}));
        """
    )

    result = _run_node(script)
    expected_visible = [
        {"role": "user", "content": "Go"},
        {"role": "assistant", "content": "Clean final answer"},
    ]
    assert result["automatic"] == {
        "visible": expected_visible,
        "preserved": 0,
        "settledReference": False,
        "compactionReference": False,
    }
    assert result["manual"] == {
        "visible": expected_visible,
        "preserved": 1,
        "settledReference": True,
        "compactionReference": False,
    }
