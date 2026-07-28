import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SESSIONS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _function_source(source, name):
    start = source.index(f"function {name}(")
    opening = re.search(r"\)\s*\{", source[start:])
    assert opening
    brace = start + opening.end() - 1
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _node(script):
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_lazy_tail_payload_adopts_30_newest_without_exact_total():
    helpers = "\n".join(
        _function_source(SESSIONS, name)
        for name in ("_parseLazyTailPayload", "_lazyTailSessionEnvelope")
    )
    result = _node(
        helpers
        + """
const payload={
  requested_session_id:'requested',
  canonical_session_id:'canonical',
  title:'Large task',
  model:'model',
  workspace:'/workspace',
  session_metadata:{
    session_id:'canonical',read_only:true,model_provider:'provider',
    enabled_toolsets:['terminal'],composer_draft:{text:'draft',files:[]}
  },
  messages:Array.from({length:30},(_,i)=>({role:'assistant',content:String(i),_state_db_message_id:String(i)})),
  runtime_snapshot:null,
  conversation_window:{
    schema:'lazy_tail_v1',state:'ready',source:'state_db',
    visible_count:30,has_older:true,older_cursor:'opaque',
    newest_message_id:'29',active_stream_id:null,reconnect_token:null,
    exact_total_available:false,status_reason:null
  }
};
const parsed=_parseLazyTailPayload(payload,'requested');
const session=_lazyTailSessionEnvelope(parsed);
console.log(JSON.stringify({
  sourceMode:parsed.sourceMode,
  count:session.messages.length,
  hasMessageCount:Object.prototype.hasOwnProperty.call(session,'message_count'),
  cursor:session._lazy_tail_window.older_cursor
}));
"""
    )

    assert result == {
        "sourceMode": "lazy_tail_v1",
        "count": 30,
        "hasMessageCount": False,
        "cursor": "opaque",
    }


def test_lazy_tail_payload_accepts_bounded_tool_closure_rows():
    parser = _function_source(SESSIONS, "_parseLazyTailPayload")
    result = _node(
        parser
        + """
const messages=[
  {role:'user',content:'start'},
  {role:'assistant',content:'',tool_calls:[{id:'call-1'}]},
  ...Array.from({length:26},(_,i)=>({role:'tool',content:String(i),tool_call_id:`call-${i+1}`})),
  {role:'assistant',content:'latest'},
  {role:'user',content:'continue'},
  {role:'assistant',content:'done'},
  {role:'tool',content:'closure',tool_call_id:'call-final'}
];
const payload={
  requested_session_id:'requested',
  canonical_session_id:'canonical',
  session_metadata:{
    session_id:'canonical',read_only:false,model_provider:'provider'
  },
  messages,
  runtime_snapshot:null,
  conversation_window:{
    schema:'lazy_tail_v1',state:'ready',source:'state_db',
    visible_count:5,has_older:true,older_cursor:'opaque',
    newest_message_id:'latest',active_stream_id:null,reconnect_token:null,
    exact_total_available:false,status_reason:null
  }
};
const parsed=_parseLazyTailPayload(payload,'requested',{requireMetadata:true});
console.log(JSON.stringify({
  accepted:!!parsed,
  visibleCount:parsed&&parsed.window.visible_count,
  rowCount:parsed&&parsed.payload.messages.length
}));
"""
    )

    assert result == {
        "accepted": True,
        "visibleCount": 5,
        "rowCount": 32,
    }


def test_lazy_tail_envelope_preserves_authoritative_behavior_metadata():
    helpers = "\n".join(
        _function_source(SESSIONS, name)
        for name in ("_parseLazyTailPayload", "_lazyTailSessionEnvelope")
    )
    result = _node(
        helpers
        + """
const payload={
 requested_session_id:'requested',canonical_session_id:'canonical',
 title:'Task',model:'model',workspace:'/workspace',messages:[],
 session_metadata:{
   session_id:'canonical',read_only:true,model_provider:'provider',
   profile:'work',enabled_toolsets:['terminal'],
   composer_draft:{text:'draft',files:[]},project_id:'project',
   worktree_path:'/workspace/.worktrees/task'
 },
 runtime_snapshot:null,
 conversation_window:{schema:'lazy_tail_v1',state:'ready',source:'state_db',
 visible_count:0,has_older:false,older_cursor:null,newest_message_id:null,
 active_stream_id:null,reconnect_token:null,exact_total_available:false,status_reason:null}
};
const parsed=_parseLazyTailPayload(payload,'requested',{requireMetadata:true});
const session=_lazyTailSessionEnvelope(parsed);
console.log(JSON.stringify({
 readOnly:session.read_only,provider:session.model_provider,
 profile:session.profile,toolsets:session.enabled_toolsets,
 draft:session.composer_draft.text,project:session.project_id,
 worktree:session.worktree_path
}));
"""
    )

    assert result == {
        "readOnly": True,
        "provider": "provider",
        "profile": "work",
        "toolsets": ["terminal"],
        "draft": "draft",
        "project": "project",
        "worktree": "/workspace/.worktrees/task",
    }


def test_lazy_tail_parser_fails_closed_on_unknown_schema_or_state():
    parser = _function_source(SESSIONS, "_parseLazyTailPayload")
    result = _node(
        parser
        + """
const base={
 requested_session_id:'requested',canonical_session_id:'canonical',messages:[],
 runtime_snapshot:null,
 conversation_window:{schema:'lazy_tail_v1',state:'ready',source:'state_db',
 visible_count:0,has_older:false,older_cursor:null,newest_message_id:null,
 active_stream_id:null,reconnect_token:null,exact_total_available:false,status_reason:null}
};
const badSchema={...base,conversation_window:{...base.conversation_window,schema:'future'}};
const badState={...base,conversation_window:{...base.conversation_window,state:'mystery'}};
console.log(JSON.stringify([
  _parseLazyTailPayload(badSchema,'requested'),
  _parseLazyTailPayload(badState,'requested')
]));
"""
    )

    assert result == [None, None]


def test_lazy_tail_parser_requires_consistent_live_handoff_state():
    parser = _function_source(SESSIONS, "_parseLazyTailPayload")
    result = _node(
        parser
        + """
const base={
 requested_session_id:'requested',canonical_session_id:'canonical',messages:[],
 runtime_snapshot:null,
 conversation_window:{schema:'lazy_tail_v1',state:'ready',source:'state_db',
 visible_count:0,has_older:false,older_cursor:null,newest_message_id:null,
 active_stream_id:null,reconnect_token:null,exact_total_available:false,status_reason:null}
};
const missingHandoff={...base,conversation_window:{
  ...base.conversation_window,state:'reconnecting',active_stream_id:'run-1'
}};
const liveReady={...base,conversation_window:{
  ...base.conversation_window,active_stream_id:'run-1'
}};
console.log(JSON.stringify([
  _parseLazyTailPayload(missingHandoff,'requested'),
  _parseLazyTailPayload(liveReady,'requested')
]));
"""
    )

    assert result == [None, None]


def test_five_older_pages_remain_ordered_and_stable_id_deduped():
    merge = _function_source(SESSIONS, "_mergeLazyTailOlderMessages")
    result = _node(
        merge
        + """
let current=Array.from({length:30},(_,i)=>({role:'assistant',content:String(250+i),_state_db_message_id:String(250+i)}));
for(let page=4;page>=0;page--){
  const start=page*50;
  const older=Array.from({length:50},(_,i)=>({role:'assistant',content:String(start+i),_state_db_message_id:String(start+i)}));
  older.push({...current[0]});
  current=_mergeLazyTailOlderMessages(current,older);
}
console.log(JSON.stringify({
  count:current.length,
  first:current[0]._state_db_message_id,
  last:current[current.length-1]._state_db_message_id,
  unique:new Set(current.map(m=>m._state_db_message_id)).size
}));
"""
    )

    assert result == {"count": 280, "first": "0", "last": "279", "unique": 280}


def test_load_session_uses_lazy_route_and_has_explicit_legacy_only():
    load = _function_source(SESSIONS, "loadSession")
    explicit = _function_source(SESSIONS, "loadCompleteLegacyTranscript")

    assert "/api/session-window?session_id=${encodeURIComponent(sid)}&msg_limit=5&resolve_model=0" in load
    assert "window.__HERMES_CONFIG__.lazyTailV1===true" in load
    assert "forceLegacyMessagePaging" in explicit
    assert "forceLegacyMessagePaging:true,completeLegacyTranscript:true" in explicit
    assert "S.session._modelResolutionDeferred=!_useLazyTail" in SESSIONS
    assert "if(!_useLazyTail) _resolveSessionModelForDisplaySoon(sid)" in SESSIONS


def test_lazy_older_path_uses_single_visible_turn_and_opaque_cursor():
    older = _function_source(SESSIONS, "_loadOlderMessages")
    lazy_start = older.index("if (_messagePaging.mode === 'lazy_tail_v1')")
    legacy_start = older.index("if (_messagePaging.mode === 'cursor_v1')")
    lazy = older[lazy_start:legacy_start]

    assert "/api/session-window" in lazy
    assert "msg_limit=1" in lazy
    assert "older_cursor=${encodeURIComponent(requestedCursor)}" in lazy
    assert "/api/session?" not in lazy


def test_failed_older_page_cannot_replace_a_newly_selected_task():
    older = "async " + _function_source(SESSIONS, "_loadOlderMessages")
    result = _node(
        older
        + """
let rejectPage;
let _loadingOlder=false;
let _messagesTruncated=true;
let _messagesGeneration=1;
let _loadSessionGeneration=1;
let _loadingSessionId=null;
let _messagePaging={
  mode:'lazy_tail_v1',hasMore:true,beforeCursor:'opaque',
  canonicalSessionId:'task-a'
};
let S={
  session:{session_id:'task-a'},
  messages:[{role:'assistant',content:'latest'}]
};
let recoveryFor=null;
function api(){
  return new Promise((_resolve,reject)=>{rejectPage=reject;});
}
function _showLazyTailRecovery(sid){recoveryFor=sid;}
async function _recoverLazyTailPaging(){return false;}
const pending=_loadOlderMessages();
setImmediate(()=>{
  S.session={session_id:'task-b'};
  _loadSessionGeneration=2;
  _messagesGeneration=2;
  rejectPage(new Error('late network failure'));
});
pending.then(()=>{
  console.log(JSON.stringify({
    recoveryFor,
    current:S.session.session_id,
    loading:_loadingOlder
  }));
});
"""
    )

    assert result == {
        "recoveryFor": None,
        "current": "task-b",
        "loading": False,
    }


def test_lazy_reconnect_restore_stays_bounded_and_keeps_recovery_ui():
    restore = "async " + _function_source(
        MESSAGES,
        "_restoreSettledSession",
    )
    result = _node(
        restore
        + """
const reconnectToken='';
const lazyTailStream=true;
let apiCalls=[];
let recoveryVisible=false;
function api(path){apiCalls.push(path);throw new Error('must not fetch');}
function _showLazyReconnectRecovery(){recoveryVisible=true;}
(async()=>{
  const status=await _restoreSettledSession({}, {status:true});
  console.log(JSON.stringify({status,apiCalls,recoveryVisible}));
})();
"""
    )

    assert result == {
        "status": "bounded_recovery_required",
        "apiCalls": [],
        "recoveryVisible": True,
    }


def test_lazy_terminal_state_preserves_only_the_bounded_tail():
    helper = _function_source(
        MESSAGES,
        "_boundedLazyTailTerminalState",
    )
    result = _node(
        helper
        + """
const currentSession={session_id:'task-1',title:'Old',messages:['must drop']};
const currentMessages=[{role:'user',content:'old tail'}];
const terminalSession={
  session_id:'task-1',title:'Done',message_count:42632,
  messages:Array.from({length:1000},()=>({role:'assistant',content:'full'})),
  tool_calls:[{id:'secret'}],pre_compression_snapshot:{messages:['secret']}
};
const inflightMessages=[
  {role:'user',content:'latest question'},
  {role:'assistant',content:'latest answer'}
];
const state=_boundedLazyTailTerminalState(
  currentSession,currentMessages,terminalSession,inflightMessages
);
console.log(JSON.stringify({
  title:state.session.title,
  messageCount:state.session.message_count,
  hasSessionMessages:Object.prototype.hasOwnProperty.call(state.session,'messages'),
  hasToolCalls:Object.prototype.hasOwnProperty.call(state.session,'tool_calls'),
  hasSnapshot:Object.prototype.hasOwnProperty.call(state.session,'pre_compression_snapshot'),
  messages:state.messages.map(message=>message.content)
}));
"""
    )

    assert result == {
        "title": "Done",
        "messageCount": 42632,
        "hasSessionMessages": False,
        "hasToolCalls": False,
        "hasSnapshot": False,
        "messages": ["latest question", "latest answer"],
    }


def test_lazy_cancel_never_falls_through_to_full_session_fetch():
    cancel_start = MESSAGES.index("source.addEventListener('cancel',e=>")
    cancel_end = MESSAGES.index(
        "for(const _runJournalEventName",
        cancel_start,
    )
    cancel = MESSAGES[cancel_start:cancel_end]
    guard = cancel.index("if(_lazyTailCancel){")
    full_fetch = cancel.index(
        "const data=await api(`/api/session?session_id="
    )

    assert guard < full_fetch
    assert "_showLazyTailRecovery" in cancel[guard:full_fetch]
    assert "lazy_tail_terminal_v1" in cancel
    assert "lazy_tail=1" in MESSAGES


def test_signed_reconnect_token_flows_to_chat_stream_and_ack_gate():
    attach = _function_source(MESSAGES, "attachLiveStream")

    assert "options.reconnectToken" in attach
    assert "reconnect_token=${encodeURIComponent(reconnectToken)}" in MESSAGES
    assert "source.addEventListener('reconnect_ack'" in MESSAGES
    assert "lazy_tail_reconnect_ack_v1" in MESSAGES
    assert "checkpoint_event_id" in MESSAGES
