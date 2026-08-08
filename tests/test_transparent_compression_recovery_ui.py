import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_node(source: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    proc = subprocess.run(
        [
            node,
            "-e",
            textwrap.dedent(source),
            str(ROOT / "static" / "ui.js"),
            str(ROOT / "static" / "messages.js"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_recovery_metadata_is_render_only_and_blocker_has_no_action():
    result = _run_node(
        r"""
        const fs = require('fs');
        const ui = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name, source=ui) {
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
        const esc = value => String(value).replace(/[&<>"']/g, char => ({
          '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
        })[char]);
        eval(extractFunc('_automaticCompressionRecoveryState'));
        eval(extractFunc('_compressionRecoveryHtml'));
        eval(extractFunc('_autoCompressionBaseDetail'));
        eval(extractFunc('_autoCompressionPreviewText'));
        const pending = _automaticCompressionRecoveryState({
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: true,
            phase: 'starting',
            message: 'Recovering context...',
          },
        });
        const blocked = _compressionRecoveryHtml({
          terminal_state: 'compression_exhausted',
          automatic_recovery: false,
          phase: 'blocked',
          title: 'Context recovery stopped',
          summary: 'Unsafe <payload>',
        }, 'same-task');
        console.log(JSON.stringify({
          pending,
          preview: _autoCompressionPreviewText(pending),
          detail: _autoCompressionBaseDetail(pending),
          blocked,
        }));
        """
    )

    assert result["pending"] == {
        "phase": "running",
        "automatic": True,
        "recovery": True,
        "message": "Recovering context...",
    }
    assert result["preview"] == "Recovering context..."
    assert result["detail"] == "Recovering context..."
    assert "Context recovery stopped" in result["blocked"]
    assert "Unsafe &lt;payload&gt;" in result["blocked"]
    assert "<button" not in result["blocked"]
    assert "onclick=" not in result["blocked"]
    assert "compression-recovery/start" not in result["blocked"]


def test_accepted_terminal_frame_settles_same_task_without_error_row():
    result = _run_node(
        r"""
        const fs = require('fs');
        const messages = fs.readFileSync(process.argv[2], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = messages.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = messages.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < messages.length) {
            if (messages[i] === '{') depth++;
            else if (messages[i] === '}') depth--;
            i++;
          }
          return messages.slice(start, i);
        }
        const S = {
          session: {session_id: 'same-task'},
          messages: [{role: 'user', content: 'Fix it'}],
          activeStreamId: 'parent-stream',
        };
        eval(extractFunc('_acceptedAutomaticCompressionRecovery'));
        eval(extractFunc('_applyAcceptedAutomaticCompressionRecovery'));
        const frame = {
          type: 'compression_exhausted',
          session_id: 'same-task',
          automatic_recovery: true,
          phase: 'claimed',
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: true,
            same_session: true,
            source_session_id: 'same-task',
            phase: 'claimed',
          },
          session: {
            session_id: 'same-task',
            compression_recovery: {
              terminal_state: 'compression_exhausted',
              automatic_recovery: true,
              same_session: true,
              source_session_id: 'same-task',
              phase: 'claimed',
            },
            messages: [
              {role: 'user', content: 'Fix it'},
              {role: 'assistant', content: 'Partial safe work', _partial: true},
            ],
          },
        };
        const accepted = _applyAcceptedAutomaticCompressionRecovery(frame, 'same-task');
        const wrongTask = _acceptedAutomaticCompressionRecovery(frame, 'other-task');
        console.log(JSON.stringify({
          accepted,
          wrongTask: !!wrongTask,
          sessionId: S.session.session_id,
          activeStreamId: S.activeStreamId,
          messages: S.messages,
        }));
        """
    )

    assert result["accepted"] is True
    assert result["wrongTask"] is False
    assert result["sessionId"] == "same-task"
    assert result["activeStreamId"] is None
    assert result["messages"] == [
        {"role": "user", "content": "Fix it"},
        {"role": "assistant", "content": "Partial safe work", "_partial": True},
    ]
    assert all("Context compression exhausted" not in row.get("content", "") for row in result["messages"])


def test_accepted_terminal_frame_adopts_canonical_rotated_snapshot_without_navigation():
    result = _run_node(
        r"""
        const fs = require('fs');
        const messages = fs.readFileSync(process.argv[2], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = messages.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = messages.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < messages.length) {
            if (messages[i] === '{') depth++;
            else if (messages[i] === '}') depth--;
            i++;
          }
          return messages.slice(start, i);
        }
        const calls = {url: 0, sidebar: 0, projected: 0, streams: []};
        const _setActiveSessionUrl = () => { calls.url++; };
        const renderSessionList = () => { calls.sidebar++; };
        const _applyToAnchor = () => { calls.projected++; };
        let _sessionStreamHiddenSid = 'old-task';
        const startSessionStream = sid => { calls.streams.push(sid); };
        const S = {
          session: {session_id: 'old-task'},
          messages: [{role: 'user', content: 'Fix it'}],
          activeStreamId: 'parent-stream',
        };
        eval(extractFunc('_acceptedAutomaticCompressionRecovery'));
        eval(extractFunc('_applyAcceptedAutomaticCompressionRecovery'));
        const frame = {
          type: 'compression_exhausted',
          old_session_id: 'old-task',
          session_id: 'canonical-task',
          new_session_id: 'canonical-task',
          automatic_recovery: true,
          phase: 'claimed',
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: true,
            same_session: true,
            source_session_id: 'canonical-task',
            phase: 'claimed',
          },
          session: {
            session_id: 'canonical-task',
            compression_recovery: {
              terminal_state: 'compression_exhausted',
              automatic_recovery: true,
              same_session: true,
              source_session_id: 'canonical-task',
              phase: 'claimed',
            },
            messages: [
              {role: 'user', content: 'Fix it'},
              {role: 'assistant', content: 'Partial safe work', _partial: true},
            ],
          },
        };
        const accepted = _applyAcceptedAutomaticCompressionRecovery(frame, 'old-task');
        const conflicting = {
          ...frame,
          new_session_id: 'wrong-task',
        };
        S.activeStreamId = 'newer-stream';
        S.messages.push({role: 'user', content: 'Newer work'});
        const beforeNoop = JSON.stringify(S);
        const settledFrame = {
          ...frame,
          phase: 'settled',
          compression_recovery: {
            ...frame.compression_recovery,
            automatic_recovery: false,
            phase: 'settled',
          },
          session: {
            ...frame.session,
            messages: [{role: 'user', content: 'Stale parent snapshot'}],
          },
        };
        const settledAccepted = _applyAcceptedAutomaticCompressionRecovery(
          settledFrame,
          'old-task'
        );
        const afterNoop = JSON.stringify(S);
        const supersededAccepted = !!_acceptedAutomaticCompressionRecovery({
          ...settledFrame,
          phase: 'superseded',
          compression_recovery: {
            ...settledFrame.compression_recovery,
            phase: 'superseded',
          },
        }, 'old-task');
        console.log(JSON.stringify({
          accepted,
          conflicting: !!_acceptedAutomaticCompressionRecovery(conflicting, 'old-task'),
          settledAccepted,
          supersededAccepted,
          noopStateUnchanged: beforeNoop === afterNoop,
          sessionId: S.session.session_id,
          activeStreamId: S.activeStreamId,
          messages: S.messages,
          hiddenSessionStreamSid: _sessionStreamHiddenSid,
          calls,
        }));
        """
    )

    assert result["accepted"] is True
    assert result["conflicting"] is False
    assert result["settledAccepted"] is True
    assert result["supersededAccepted"] is True
    assert result["noopStateUnchanged"] is True
    assert result["sessionId"] == "canonical-task"
    assert result["activeStreamId"] == "newer-stream"
    assert result["messages"] == [
        {"role": "user", "content": "Fix it"},
        {"role": "assistant", "content": "Partial safe work", "_partial": True},
        {"role": "user", "content": "Newer work"},
    ]
    assert result["hiddenSessionStreamSid"] is None
    assert result["calls"] == {
        "url": 0,
        "sidebar": 0,
        "projected": 0,
        "streams": ["canonical-task"],
    }


def test_blocked_recovery_is_one_standalone_tail_diagnostic_for_every_transcript_shape():
    result = _run_node(
        r"""
        const fs = require('fs');
        const ui = fs.readFileSync(process.argv[1], 'utf8');
        function extractFunc(name) {
          const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
          const start = ui.search(re);
          if (start < 0) throw new Error(name + ' not found');
          let i = ui.indexOf('{', start), depth = 1; i++;
          while (depth > 0 && i < ui.length) {
            if (ui[i] === '{') depth++;
            else if (ui[i] === '}') depth--;
            i++;
          }
          return ui.slice(start, i);
        }
        const esc = value => String(value).replace(/[&<>"']/g, char => ({
          '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
        })[char]);
        const S = {session: null, messages: []};
        eval(extractFunc('_activeCompressionRecoveryPayload'));
        eval(extractFunc('_blockedCompressionRecoveryPayload'));
        eval(extractFunc('_compressionRecoveryHtml'));
        function renderCase(session, messages) {
          S.session = session;
          S.messages = messages;
          const payload = _blockedCompressionRecoveryPayload();
          return {
            html: payload ? _compressionRecoveryHtml(payload, session.session_id) : '',
            messages: JSON.parse(JSON.stringify(messages)),
            tailRole: messages.length ? messages[messages.length - 1].role : null,
          };
        }
        const assistantThenUser = renderCase({
          session_id: 'assistant-then-user',
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: false,
            phase: 'blocked',
            title: 'Tail diagnostic',
            summary: 'The prior answer remains visible.',
          },
        }, [
          {role: 'assistant', content: 'A valid prior answer'},
          {role: 'user', content: 'A failed follow-up'},
        ]);
        const firstTurnUserOnly = renderCase({
          session_id: 'first-turn',
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: false,
            phase: 'blocked',
            title: 'First-turn diagnostic',
            summary: 'No assistant anchor is required.',
          },
        }, [
          {role: 'user', content: 'First request'},
        ]);
        const authoritativeSession = renderCase({
          session_id: 'same-task',
          compression_recovery: {
            terminal_state: 'compression_exhausted',
            automatic_recovery: false,
            phase: 'blocked',
            title: 'Authoritative blocker',
            summary: 'Keep working here.',
          },
        }, [
          {
            role: 'assistant',
            content: 'Legacy terminal row',
            _compressionRecovery: {
              terminal_state: 'compression_exhausted',
              automatic_recovery: false,
              phase: 'blocked',
              title: 'Stale legacy blocker',
            },
          },
          {role: 'user', content: 'Latest failed request'},
        ]);
        console.log(JSON.stringify({
          assistantThenUser,
          firstTurnUserOnly,
          authoritativeSession,
        }));
        """
    )

    assistant_then_user = result["assistantThenUser"]
    assert assistant_then_user["tailRole"] == "user"
    assert assistant_then_user["messages"][0]["content"] == "A valid prior answer"
    assert "Tail diagnostic" in assistant_then_user["html"]

    first_turn = result["firstTurnUserOnly"]
    assert first_turn["tailRole"] == "user"
    assert first_turn["messages"] == [{"role": "user", "content": "First request"}]
    assert "First-turn diagnostic" in first_turn["html"]

    authoritative = result["authoritativeSession"]
    assert "Authoritative blocker" in authoritative["html"]
    assert "Stale legacy blocker" not in authoritative["html"]

    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    render_block = ui[ui.index("function renderMessages(options)") :]
    assert "if(recoveryHtml) bodyHtml = recoveryHtml" not in render_block
    assert render_block.count("_insertCompressionLikeNode(recoveryBlockedNode, null);") == 1


def test_live_assets_have_no_manual_fork_or_parent_send_interceptor():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

    assert "function startCompressionRecovery" not in ui
    assert "function shouldInterceptCompressionRecoveryContinuation" not in ui
    assert "function redirectCompressionRecoverySend" not in ui
    assert "api('/api/session/compression-recovery/start'" not in ui
    assert "shouldInterceptCompressionRecoveryContinuation(text,S.pendingFiles)" not in messages

    handler = messages[messages.index("source.addEventListener('apperror'") :]
    accepted = handler.index("_applyAcceptedAutomaticCompressionRecovery")
    anchor = handler.index("_applyToAnchor('apperror'")
    synthetic = handler.index("S.messages.push({role:'assistant'")
    assert accepted < anchor
    assert accepted < synthetic
    assert "if(!acceptedAutomaticRecovery) renderSessionList();" in handler
    assert "if(!acceptedAutomaticRecovery){ _markSessionViewed" in " ".join(handler.split())
