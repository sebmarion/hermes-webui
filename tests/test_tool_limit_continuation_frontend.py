import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", MESSAGES_JS)
    assert match, f"{name} not found"
    brace = MESSAGES_JS.find("{", match.end())
    assert brace >= 0
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    index = brace
    while index < len(MESSAGES_JS):
        char = MESSAGES_JS[index]
        nxt = MESSAGES_JS[index + 1] if index + 1 < len(MESSAGES_JS) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[match.start():index + 1]
        index += 1
    raise AssertionError(f"unterminated {name}")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tool_limit_continuation_event_races_and_replay_behavior():
    factory = _function_source("_createToolLimitContinuationCoordinator")
    script = textwrap.dedent(
        f"""
        {factory}

        const tick = () => new Promise(resolve => setImmediate(resolve));
        const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
        const continuation = (overrides = {{}}) => ({{
          execution_id: 'exec-1',
          root_session_id: 'parent',
          parent_session_id: 'parent',
          parent_run_id: 'parent-stream',
          child_session_id: 'child',
          continuation_index: 1,
          state: 'continuing',
          ...overrides,
        }});
        const started = (overrides = {{}}) => ({{
          session_id: 'child',
          stream_id: 'stream-1',
          ...overrides,
        }});

        function harness(initialSid, initialActivity = {{}}, customDisposition = null, options = {{}}) {{
          let current = initialSid;
          let activity = {{busy: false, streamId: '', ...initialActivity}};
          let releaseLoad = null;
          const calls = {{load: [], attach: [], blocked: []}};
          const coordinator = _createToolLimitContinuationCoordinator({{
            currentSessionId: () => current,
            migrationDisposition: d => {{
              if (customDisposition) return customDisposition(d);
              if (activity.streamId) {{
                return activity.streamId === String(d.parent_run_id || '') ? 'wait' : 'reject';
              }}
              return activity.busy ? 'wait' : 'allow';
            }},
            loadChild: async (sid, acceptResult) => {{
              calls.load.push(sid);
              if (options.deferLoad) {{
                await new Promise(resolve => {{
                  releaseLoad = () => {{
                    if (acceptResult()) current = sid;
                    resolve();
                  }};
                }});
              }} else if (acceptResult()) {{
                current = sid;
              }}
            }},
            attachChild: (sid, streamId, recovered) => calls.attach.push([sid, streamId, recovered]),
            showBlocked: message => calls.blocked.push(message),
          }});
          return {{
            coordinator,
            calls,
            current: () => current,
            setCurrent: sid => {{ current = sid; }},
            setActivity: next => {{ activity = {{...activity, ...next}}; }},
            releaseLoad: () => {{ if (releaseLoad) releaseLoad(); }},
          }};
        }}

        (async () => {{
          const continuationFirst = harness('parent');
          continuationFirst.coordinator.continuation(continuation());
          await tick();
          continuationFirst.coordinator.started(started());
          await tick();

          const startFirst = harness('parent');
          startFirst.coordinator.started(started());
          await tick();
          startFirst.coordinator.continuation(continuation());
          await tick();

          // EventSource replay delivers both frames repeatedly. Navigation and
          // renderer attachment remain exactly once for this execution/child/stream.
          startFirst.coordinator.started(started({{recovered: true}}));
          startFirst.coordinator.continuation(continuation());
          startFirst.coordinator.started(started());
          await tick();

          const inactive = harness('unrelated');
          inactive.coordinator.started(started());
          inactive.coordinator.continuation(continuation());
          await tick();

          const blocked = harness('parent');
          blocked.coordinator.started(started());
          blocked.coordinator.continuation(continuation({{
            state: 'blocked',
            child_session_id: null,
            blocked_reason: 'no_progress',
          }}));
          await tick();

          // Reconnect after the entire child turn finished has no live-start
          // frame to replay. The latest durable receipt alone must migrate to
          // the final child, and repeated EventSource replay stays idempotent.
          const completed = harness('parent');
          completed.coordinator.continuation(continuation({{state: 'completed'}}));
          completed.coordinator.continuation(continuation({{state: 'completed'}}));
          await tick();

          // On a cold reload the parent-scoped EventSource can deliver its
          // durable replay before loadSession has assigned S.session. The
          // subscribed parent proves initial eligibility; migration waits for
          // hydration and still re-checks that the user did not navigate away.
          const earlyReplay = harness('');
          earlyReplay.coordinator.continuation(continuation({{
            state: 'completed',
            _subscribed_session_id: 'parent',
          }}));
          earlyReplay.setCurrent('parent');
          await delay(80);

          const earlyAway = harness('');
          earlyAway.coordinator.continuation(continuation({{
            state: 'completed',
            _subscribed_session_id: 'parent',
          }}));
          earlyAway.setCurrent('unrelated');
          await delay(80);

          // A delayed continuation event must not take over the pane while a
          // newer turn owns it. The record stays rejected even if that newer
          // stream settles later.
          const newerTurn = harness('parent', {{busy: true, streamId: 'newer-stream'}});
          newerTurn.coordinator.continuation(continuation());
          newerTurn.coordinator.started(started());
          newerTurn.setActivity({{busy: false, streamId: ''}});
          await delay(80);

          // The expected parent stream can still be finishing in the renderer
          // when the server-owned handoff arrives. Wait for that exact stream,
          // then migrate once it settles.
          const parentSettling = harness('parent', {{busy: true, streamId: 'parent-stream'}});
          parentSettling.coordinator.continuation(continuation());
          parentSettling.coordinator.started(started());
          await delay(20);
          parentSettling.setActivity({{busy: false, streamId: ''}});
          await delay(80);

          // Re-check immediately before loading: activity can change between
          // the eligibility decision and the navigation boundary.
          let guardCalls = 0;
          const lastMomentNewerTurn = harness('parent', {{}}, () => (++guardCalls === 1 ? 'allow' : 'reject'));
          lastMomentNewerTurn.coordinator.continuation(continuation());
          await tick();

          // Loading the child is asynchronous. A newer turn that acquires the
          // parent pane while metadata is in flight must veto the load result,
          // not merely suppress the later renderer attach.
          const inFlightNewerTurn = harness('parent', {{}}, null, {{deferLoad: true}});
          inFlightNewerTurn.coordinator.continuation(continuation());
          inFlightNewerTurn.coordinator.started(started());
          await tick();
          inFlightNewerTurn.setActivity({{busy: true, streamId: 'newer-stream'}});
          inFlightNewerTurn.releaseLoad();
          await tick();

          process.stdout.write(JSON.stringify({{
            continuationFirst: {{calls: continuationFirst.calls, current: continuationFirst.current()}},
            startFirst: {{calls: startFirst.calls, current: startFirst.current()}},
            inactive: {{calls: inactive.calls, current: inactive.current()}},
            blocked: {{calls: blocked.calls, current: blocked.current()}},
            completed: {{calls: completed.calls, current: completed.current()}},
            earlyReplay: {{calls: earlyReplay.calls, current: earlyReplay.current()}},
            earlyAway: {{calls: earlyAway.calls, current: earlyAway.current()}},
            newerTurn: {{calls: newerTurn.calls, current: newerTurn.current()}},
            parentSettling: {{calls: parentSettling.calls, current: parentSettling.current()}},
            lastMomentNewerTurn: {{calls: lastMomentNewerTurn.calls, current: lastMomentNewerTurn.current()}},
            inFlightNewerTurn: {{calls: inFlightNewerTurn.calls, current: inFlightNewerTurn.current()}},
          }}));
        }})().catch(error => {{
          process.stderr.write(String(error.stack || error));
          process.exit(1);
        }});
        """
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)

    for race in ("continuationFirst", "startFirst"):
        assert output[race] == {
            "calls": {
                "load": ["child"],
                "attach": [["child", "stream-1", False]],
                "blocked": [],
            },
            "current": "child",
        }

    assert output["inactive"] == {
        "calls": {"load": [], "attach": [], "blocked": []},
        "current": "unrelated",
    }
    assert output["blocked"]["calls"]["load"] == []
    assert output["blocked"]["calls"]["attach"] == []
    assert output["blocked"]["calls"]["blocked"] == [
        "Tool-limit continuation stopped because no machine-verifiable progress was detected."
    ]
    assert output["blocked"]["current"] == "parent"
    assert output["completed"] == {
        "calls": {"load": ["child"], "attach": [], "blocked": []},
        "current": "child",
    }
    assert output["earlyReplay"] == {
        "calls": {"load": ["child"], "attach": [], "blocked": []},
        "current": "child",
    }
    assert output["earlyAway"] == {
        "calls": {"load": [], "attach": [], "blocked": []},
        "current": "unrelated",
    }
    assert output["newerTurn"] == {
        "calls": {"load": [], "attach": [], "blocked": []},
        "current": "parent",
    }
    assert output["parentSettling"] == {
        "calls": {
            "load": ["child"],
            "attach": [["child", "stream-1", False]],
            "blocked": [],
        },
        "current": "child",
    }
    assert output["lastMomentNewerTurn"] == {
        "calls": {"load": [], "attach": [], "blocked": []},
        "current": "parent",
    }
    assert output["inFlightNewerTurn"] == {
        "calls": {"load": ["child"], "attach": [], "blocked": []},
        "current": "parent",
    }


def test_root_session_stream_accepts_later_segment_replay():
    listener = MESSAGES_JS.index("es.addEventListener('tool_limit_continuation'")
    source = MESSAGES_JS[listener:listener + 1100]

    assert "d.parent_session_id" in source
    assert "d.root_session_id" in source
    assert "d._subscribed_session_id" in source


def test_cold_replay_does_not_observe_loading_placeholder_as_stale_html():
    source = _function_source("_loadToolLimitContinuationChild")

    assert "staleText" in source
    assert "staleText !== 'Loading conversation...'" in source
    disconnect = source.index("observer.disconnect()")
    restore = source.index("host.innerHTML = staleHtml")
    assert disconnect < restore


def test_child_load_passes_pane_ownership_guard_through_session_commit():
    child_loader = _function_source("_loadToolLimitContinuationChild")
    load_start = SESSIONS_JS.index("async function loadSession")
    load_end = SESSIONS_JS.index("\nasync function ", load_start + 1)
    load_source = SESSIONS_JS[load_start:load_end]

    assert "acceptResult: canCommit" in child_loader
    assert "typeof opts.acceptResult" in load_source
    assert "if (!_acceptResult())" in load_source
