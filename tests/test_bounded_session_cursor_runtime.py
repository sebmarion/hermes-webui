"""Executable cursor recovery regression tests for bounded session paging."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found"
    brace = source.find("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


_NODE_TEMPLATE = r'''
let _loadingOlder = false;
let _messagesTruncated = true;
let _oldestIdx = 0;
let _messagesGeneration = 4;
let _loadSessionGeneration = 7;
let _loadingSessionId = null;
let _messagePaging = {};
const _INITIAL_MSG_LIMIT = 30;
const S = { session: null, messages: [] };
const loadCalls = [];
let queued = [];
const window = {};

async function api() {
  const next = queued.shift();
  if (next.reject) throw next.reject;
  return next.value;
}
async function loadSession(sid, opts) {
  loadCalls.push({ sid, opts });
}
function _sameTranscriptMessage(a, b) { return a === b; }

__PARSE__
__RECOVER__
__LOAD_OLDER__

function reset({ restartAttempted = false, cursor = 'cursor-1' } = {}) {
  _loadingOlder = false;
  _messagesTruncated = true;
  _oldestIdx = 0;
  _messagesGeneration = 4;
  _loadSessionGeneration = 7;
  _loadingSessionId = null;
  _messagePaging = {
    mode: 'cursor_v1', beforeCursor: cursor, hasMore: true,
    visibleCount: 30, restartAttempted,
  };
  S.session = { session_id: 'sid-1' };
  S.messages = [{ role: 'assistant', _state_db_message_id: 'current' }];
  loadCalls.length = 0;
  queued = [];
}

function page(beforeCursor) {
  return {
    session: {
      session_id: 'sid-1',
      messages: [],
      message_page: {
        mode: 'cursor_v1', before_cursor: beforeCursor, has_more: true,
        visible_count: 1, raw_rows_examined: 1, serialized_bytes: 1,
      },
    },
  };
}

reset();
queued.push({ reject: { status: 400 } });
await _loadOlderMessages();
const after400 = { calls: loadCalls.slice(), paging: { ..._messagePaging } };

reset({ restartAttempted: true });
queued.push({ reject: { status: 409 } });
await _loadOlderMessages();
const after409 = { calls: loadCalls.slice(), paging: { ..._messagePaging } };

reset({ cursor: 'same-cursor' });
queued.push({ value: page('same-cursor') });
await _loadOlderMessages();
const afterNoProgress = { calls: loadCalls.slice(), paging: { ..._messagePaging } };

console.log(JSON.stringify({ after400, after409, afterNoProgress }));
'''


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cursor_errors_and_no_progress_pages_use_bounded_recovery_policy():
    script = (
        _NODE_TEMPLATE.replace("__PARSE__", _extract_function(SOURCE, "_parseMessagePage"))
        .replace("__RECOVER__", _extract_function(SOURCE, "_recoverCursorPaging"))
        .replace("__LOAD_OLDER__", _extract_function(SOURCE, "_loadOlderMessages"))
    )
    result = _run_node(script)

    assert result["after400"]["calls"] == [
        {"sid": "sid-1", "opts": {"force": True, "cursorRestartAttempted": True}}
    ]
    assert result["after409"]["calls"] == [
        {"sid": "sid-1", "opts": {"force": True, "forceLegacyMessagePaging": True}}
    ]
    assert result["afterNoProgress"]["calls"] == [
        {"sid": "sid-1", "opts": {"force": True, "cursorRestartAttempted": True}}
    ]
    assert result["afterNoProgress"]["paging"]["beforeCursor"] is None
    assert result["afterNoProgress"]["paging"]["hasMore"] is False
