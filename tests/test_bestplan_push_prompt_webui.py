from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
INDEX_HTML = ROOT / "static" / "index.html"
I18N_JS = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")


class _Handler:
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

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _install_prompt_runtime(monkeypatch, *, prompt="Push the checked commit?"):
    from api import routes

    observations = {
        "homes": [],
        "stores": [],
        "recoveries": [],
    }

    class Store:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            observations["stores"].append(self)

        def close(self):
            self.closed = True

    state_module = types.ModuleType("agent.bestplan_state")
    state_module.BestplanStore = Store
    push_module = types.ModuleType("agent.bestplan_local_push")

    def recover_local_push_prompt(**kwargs):
        observations["recoveries"].append(kwargs)
        if isinstance(prompt, BaseException):
            raise prompt
        return prompt

    push_module.recover_local_push_prompt = recover_local_push_prompt
    monkeypatch.setitem(sys.modules, "agent.bestplan_state", state_module)
    monkeypatch.setitem(sys.modules, "agent.bestplan_local_push", push_module)
    monkeypatch.setattr(
        routes,
        "get_hermes_home_for_profile",
        lambda profile: observations["homes"].append(profile)
        or Path("/profiles") / (profile or "default"),
        raising=False,
    )
    return routes, observations


def test_push_prompt_route_uses_canonical_server_owned_identity_and_closes_store(
    monkeypatch,
):
    routes, seen = _install_prompt_runtime(monkeypatch)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda db, sid: seen.setdefault("resolutions", []).append((db, sid))
        or SimpleNamespace(canonical_id="canonical-session"),
    )
    canonical = SimpleNamespace(
        session_id="canonical-session",
        profile="coder",
        workspace="/server-owned/workspace",
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: seen.setdefault("sessions", []).append(
            (sid, metadata_only)
        )
        or canonical,
    )
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda profile, handler=None: seen.setdefault("visibility", []).append(
            (profile, handler)
        )
        or True,
    )

    before = time.monotonic()
    handler = _Handler()
    routes.handle_get(
        handler,
        urlparse(
            "/api/bestplan/push-prompt?session_id=requested-session"
            "&profile=attacker&workspace=%2Fevil"
        ),
    )
    after = time.monotonic()

    assert handler.status == 200
    assert handler.payload() == {"prompt": "Push the checked commit?"}
    assert handler.headers["Cache-Control"] == "no-store"
    assert seen["resolutions"] == [(routes._active_state_db_path(), "requested-session")]
    assert seen["sessions"] == [("canonical-session", True)]
    assert seen["visibility"] == [("coder", handler)]
    assert seen["homes"] == ["coder"]
    assert len(seen["stores"]) == 1
    assert seen["stores"][0].kwargs == {
        "db_path": Path("/profiles/coder/state.db"),
        "reconcile_push_state": False,
    }
    assert seen["stores"][0].closed is True
    assert len(seen["recoveries"]) == 1
    recovery = seen["recoveries"][0]
    assert recovery["session_id"] == "canonical-session"
    assert recovery["profile"] == "coder"
    assert recovery["workspace"] == "/server-owned/workspace"
    assert recovery["store"] is seen["stores"][0]
    assert before < recovery["deadline"] <= after + 10.0


def test_push_prompt_route_denies_foreign_canonical_session_before_opening_store(
    monkeypatch,
):
    routes, seen = _install_prompt_runtime(monkeypatch)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda *_a, **_k: SimpleNamespace(canonical_id="foreign-session"),
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_a, **_k: SimpleNamespace(
            session_id="foreign-session",
            profile="reviewer",
            workspace="/reviewer/workspace",
        ),
    )
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda *_a, **_k: False,
    )

    handler = _Handler()
    routes.handle_get(
        handler,
        urlparse("/api/bestplan/push-prompt?session_id=foreign-alias"),
    )

    assert handler.status == 404
    assert handler.payload() == {"error": "Session not found"}
    assert seen["stores"] == []
    assert seen["recoveries"] == []


def test_push_prompt_route_clears_authoritative_missing_expired_or_ambiguous_prompt(
    monkeypatch,
):
    routes, seen = _install_prompt_runtime(monkeypatch, prompt=None)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda *_a, **_k: SimpleNamespace(canonical_id="session-a"),
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_a, **_k: SimpleNamespace(
            session_id="session-a", profile="default", workspace="/workspace"
        ),
    )
    monkeypatch.setattr(
        routes, "_session_visible_to_active_profile", lambda *_a, **_k: True
    )

    handler = _Handler()
    routes.handle_get(
        handler,
        urlparse("/api/bestplan/push-prompt?session_id=session-a"),
    )

    assert handler.status == 200
    assert handler.payload() == {"prompt": None}
    assert len(seen["stores"]) == 1
    assert seen["stores"][0].closed is True


@pytest.mark.parametrize(
    ("resolution_status", "expected_status", "expected_payload"),
    [
        ("missing", 404, {"error": "Session not found"}),
        (
            "degraded",
            503,
            {"error": "BestPlan push prompt is temporarily unavailable"},
        ),
        ("ambiguous", 200, {"prompt": None}),
    ],
)
def test_push_prompt_route_distinguishes_resolution_outcomes(
    monkeypatch,
    resolution_status,
    expected_status,
    expected_payload,
):
    routes, seen = _install_prompt_runtime(monkeypatch)
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda *_a, **_k: SimpleNamespace(
            canonical_id="session-a",
            status=resolution_status,
        ),
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_a, **_k: SimpleNamespace(
            session_id="session-a", profile="default", workspace="/workspace"
        ),
    )
    monkeypatch.setattr(
        routes, "_session_visible_to_active_profile", lambda *_a, **_k: True
    )

    handler = _Handler()
    routes.handle_get(
        handler,
        urlparse("/api/bestplan/push-prompt?session_id=session-a"),
    )

    assert handler.status == expected_status
    assert handler.payload() == expected_payload
    assert handler.headers["Cache-Control"] == "no-store"
    assert seen["stores"] == []


def test_push_prompt_route_reports_sanitized_unavailable_without_hiding_prompt_state(
    monkeypatch,
):
    routes, seen = _install_prompt_runtime(
        monkeypatch, prompt=RuntimeError("corrupt prompt at /private/secret/path")
    )
    monkeypatch.setattr(
        routes,
        "resolve_shared_session",
        lambda *_a, **_k: SimpleNamespace(canonical_id="session-a"),
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_a, **_k: SimpleNamespace(
            session_id="session-a", profile="default", workspace="/workspace"
        ),
    )
    monkeypatch.setattr(
        routes, "_session_visible_to_active_profile", lambda *_a, **_k: True
    )

    handler = _Handler()
    routes.handle_get(
        handler,
        urlparse("/api/bestplan/push-prompt?session_id=session-a"),
    )

    assert handler.status == 503
    assert handler.payload() == {
        "error": "BestPlan push prompt is temporarily unavailable"
    }
    assert "/private/secret/path" not in handler.wfile.getvalue().decode("utf-8")
    assert len(seen["stores"]) == 1
    assert seen["stores"][0].closed is True


def test_push_prompt_route_is_not_public_when_webui_auth_is_enabled(monkeypatch):
    from api import auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: None)
    monkeypatch.setattr(auth, "ensure_trusted_auth_session", lambda _handler: None)
    handler = _Handler()

    allowed = auth.check_auth(
        handler,
        urlparse("/api/bestplan/push-prompt?session_id=session-a"),
    )

    assert allowed is False
    assert handler.status == 401
    assert handler.payload() == {"error": "Authentication required"}


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    if source[max(0, start - 6) : start] == "async ":
        start -= 6
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unclosed function {name}")


def _run_node(script: str) -> dict:
    if not NODE:
        pytest.skip("Node is required for executed browser behavior coverage")
    completed = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_prompt_card_executes_reload_clear_and_late_session_switch_guards():
    source = MESSAGES_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "_clearBestplanPushPrompt",
            "_renderBestplanPushPrompt",
            "_beginBestplanPushPromptSessionLoad",
            "_commitBestplanPushPromptSessionLoad",
            "_refreshBestplanPushPrompt",
        )
    )
    script = f"""
const elements = {{
  bestplanPushPromptCard: {{
    hidden: true,
    attrs: {{}},
    dataset: {{}},
    setAttribute(k,v) {{ this.attrs[k]=String(v); }},
    removeAttribute(k) {{ delete this.attrs[k]; }},
  }},
  bestplanPushPromptText: {{textContent: ''}},
}};
const document = {{getElementById: id => elements[id] || null}};
const $ = id => elements[id] || null;
const S = {{session: {{session_id: 'session-a'}}}};
let _bestplanPushPromptGeneration = 0;
let _bestplanPushPromptSessionId = null;
const pending = [];
const calls = [];
function api(path, opts) {{
  calls.push({{path, opts}});
  return new Promise(resolve => pending.push(resolve));
}}
{functions}
(async () => {{
  const first = _refreshBestplanPushPrompt('session-a');
  _beginBestplanPushPromptSessionLoad('session-b');
  pending.shift()({{prompt: 'STALE A'}});
  await first;
  const staleHidden = elements.bestplanPushPromptCard.hidden;
  S.session = {{session_id: 'session-b'}};
  const second = _refreshBestplanPushPrompt('session-b');
  pending.shift()({{prompt: 'Local main is checked. Reply `push` or `no`.'}});
  await second;
  const shown = {{
    hidden: elements.bestplanPushPromptCard.hidden,
    aria: elements.bestplanPushPromptCard.attrs['aria-hidden'],
    session: elements.bestplanPushPromptCard.dataset.sessionId,
    text: elements.bestplanPushPromptText.textContent,
  }};
  const third = _refreshBestplanPushPrompt('session-b');
  pending.shift()({{prompt: null}});
  await third;
  const cleared = elements.bestplanPushPromptCard.hidden && elements.bestplanPushPromptText.textContent === '';
  const fourth = _refreshBestplanPushPrompt('session-b');
  pending.shift()({{prompt: 'KNOWN PROMPT'}});
  await fourth;
  _beginBestplanPushPromptSessionLoad('session-c');
  const preservedDuringFailedSwitch = !elements.bestplanPushPromptCard.hidden && elements.bestplanPushPromptText.textContent === 'KNOWN PROMPT';
  const fifth = _refreshBestplanPushPrompt('session-b');
  pending.shift()(Promise.reject(new Error('503 unavailable')));
  await fifth;
  console.log(JSON.stringify({{
    staleHidden,
    shown,
    cleared,
    preservedDuringFailedSwitch,
    preservedOnError: !elements.bestplanPushPromptCard.hidden && elements.bestplanPushPromptText.textContent === 'KNOWN PROMPT',
    calls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""

    result = _run_node(script)

    assert result["staleHidden"] is True
    assert result["shown"] == {
        "hidden": False,
        "aria": "false",
        "session": "session-b",
        "text": "Local main is checked. Reply `push` or `no`.",
    }
    assert result["cleared"] is True
    assert result["preservedDuringFailedSwitch"] is True
    assert result["preservedOnError"] is True
    assert [call["path"] for call in result["calls"]] == [
        "/api/bestplan/push-prompt?session_id=session-a",
        "/api/bestplan/push-prompt?session_id=session-b",
        "/api/bestplan/push-prompt?session_id=session-b",
        "/api/bestplan/push-prompt?session_id=session-b",
        "/api/bestplan/push-prompt?session_id=session-b",
    ]
    assert all(call["opts"]["timeoutToast"] is False for call in result["calls"])


def test_each_post_dedupe_completion_refreshes_prompt_without_payload_authority():
    source = MESSAGES_JS.read_text(encoding="utf-8")
    handler_source = _extract_function(source, "_handleBgTaskCompleteEvent")
    script = f"""
const seen = new Set();
const refreshes = [];
const acks = [];
const S = {{session: {{session_id:'session-a',message_count:0}}, messages:[]}};
function _bgTaskCompleteRingBufferAdd(sid,eventId) {{
  const key=sid+'|'+eventId;
  if(seen.has(key)) return true;
  seen.add(key);
  return false;
}}
function _refreshBestplanPushPrompt(sid) {{ refreshes.push(sid); return Promise.resolve(); }}
function _isSessionActivelyViewed() {{ return false; }}
function showToast() {{}}
function _apiUrl(path) {{ return path; }}
function fetch(path) {{ acks.push(path); return Promise.resolve(); }}
{handler_source}
const ordinary={{data:JSON.stringify({{session_id:'session-a',event_id:'event-1',task_id:'ordinary'}})}};
const bestplan={{data:JSON.stringify({{session_id:'session-a',event_id:'event-2',task_id:'bp',bestplan_plan_id:'plan-1'}})}};
_handleBgTaskCompleteEvent(ordinary,'session-a',{{source:'session'}});
_handleBgTaskCompleteEvent(bestplan,'session-a',{{source:'session'}});
_handleBgTaskCompleteEvent(ordinary,'session-a',{{source:'stream'}});
console.log(JSON.stringify({{refreshes,ackCount:acks.length}}));
"""

    result = _run_node(script)

    assert result == {"refreshes": ["session-a", "session-a"], "ackCount": 2}


def test_normal_turn_done_reconciles_prompt_for_exact_completed_session():
    source = MESSAGES_JS.read_text(encoding="utf-8")
    done_start = source.index("source.addEventListener('done',e=>{")
    done_end = source.index("source.addEventListener('stream_end'", done_start)
    done_handler = source[done_start:done_end]

    assert "void _refreshBestplanPushPrompt(completedSid)" in done_handler
    assert done_handler.index("S.session=d.session;") < done_handler.index(
        "void _refreshBestplanPushPrompt(completedSid)"
    )


def test_prompt_card_is_outside_transcript_and_session_load_owns_refresh():
    index = INDEX_HTML.read_text(encoding="utf-8")
    sessions = SESSIONS_JS.read_text(encoding="utf-8")
    i18n = I18N_JS.read_text(encoding="utf-8")

    card = index.index('id="bestplanPushPromptCard"')
    transcript_tail = index.index('id="liveToolCards"')
    reconnect = index.index('<div class="reconnect-banner"')
    composer = index.index('<div class="composer-wrap"')
    assert transcript_tail < card < reconnect < composer
    assert index[transcript_tail:card].count("</div>") >= 2
    assert 'data-i18n="bestplan_push_prompt_heading"' in index
    assert "bestplan_push_prompt_heading: 'Push local main?'" in i18n
    assert "const val = _locale[key] ?? LOCALES.en[key];" in i18n
    load = _extract_function(sessions, "loadSession")
    assert "_beginBestplanPushPromptSessionLoad(sid)" in load
    assert "_commitBestplanPushPromptSessionLoad(S.session.session_id)" in load
    assert "_refreshBestplanPushPrompt(S.session.session_id)" in load
    commit = load.index("_commitBestplanPushPromptSessionLoad(S.session.session_id)")
    refresh = load.index("_refreshBestplanPushPrompt(S.session.session_id)")
    assert load.index("S.session=data.session;") < commit < refresh


def test_alias_load_commits_and_refreshes_canonical_session_identity():
    source = MESSAGES_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "_clearBestplanPushPrompt",
            "_commitBestplanPushPromptSessionLoad",
        )
    )
    script = f"""
const elements = {{
  bestplanPushPromptCard: {{
    hidden: false,
    attrs: {{'aria-hidden':'false'}},
    dataset: {{sessionId:'session-a'}},
    setAttribute(k,v) {{ this.attrs[k]=String(v); }},
    removeAttribute(k) {{ delete this.attrs[k]; }},
  }},
  bestplanPushPromptText: {{textContent:'PROMPT A'}},
}};
const $ = id => elements[id] || null;
const S = {{session:{{session_id:'canonical-c'}}}};
let _bestplanPushPromptSessionId = 'session-a';
const refreshes = [];
function _refreshBestplanPushPrompt(sid) {{ refreshes.push(sid); }}
{functions}
_commitBestplanPushPromptSessionLoad(S.session.session_id);
void _refreshBestplanPushPrompt(S.session.session_id);
console.log(JSON.stringify({{
  hidden: elements.bestplanPushPromptCard.hidden,
  text: elements.bestplanPushPromptText.textContent,
  owner: _bestplanPushPromptSessionId,
  refreshes,
}}));
"""

    assert _run_node(script) == {
        "hidden": True,
        "text": "",
        "owner": None,
        "refreshes": ["canonical-c"],
    }
