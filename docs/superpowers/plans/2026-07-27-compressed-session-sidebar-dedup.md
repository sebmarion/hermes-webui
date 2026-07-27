# Compressed Session Sidebar Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render one active, streaming sidebar row when opening a stale pre-compression row resolves to its canonical continuation.

**Architecture:** Keep durable identity resolution in the existing backend and repair the browser's transitional stale cache using the explicit `requested_session_id` returned with the canonical detail payload. Replace the requested alias in place with the canonical row and carry live runtime fields from the alias without merging by title or summing message counts.

**Tech Stack:** Vanilla JavaScript, Python pytest, Node-based JavaScript helper harness

---

### Task 1: Replace a requested compression alias with the canonical active row

**Files:**
- Modify: `static/sessions.js:7133-7150`
- Test: `tests/test_active_empty_session_sidebar.py`

- [ ] **Step 1: Write the failing regression test**

Add a Node-backed behavior test that extracts and executes the real helper:

```python
import json
import shutil
import subprocess

import pytest


NODE = shutil.which("node")


def _run_sidebar_helper(source: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_canonical_active_session_replaces_streaming_requested_alias():
    source = f"""
const src = {SESSIONS_JS!r};
const name = '_sessionRowsWithActiveEphemeralSession';
const start = src.indexOf('function ' + name);
let i = src.indexOf('{{', start);
let depth = 1; i++;
while (depth > 0 && i < src.length) {{
  if (src[i] === '{{') depth++;
  else if (src[i] === '}}') depth--;
  i++;
}}
eval(src.slice(start, i));
global.S = {{
  activeProfile: 'default',
  session: {{
    session_id: 'tip',
    canonical_session_id: 'tip',
    requested_session_id: 'root',
    title: 'Long task',
    message_count: 168,
    active_stream_id: null,
    pending_user_message: null,
  }},
}};
const rows = [{{
  session_id: 'root',
  title: 'Long task',
  message_count: 116,
  active_stream_id: 'stream-1',
  pending_user_message: 'keep working',
  pending_started_at: 123,
  is_streaming: true,
}}];
console.log(JSON.stringify(_sessionRowsWithActiveEphemeralSession(rows)));
"""
    rows = _run_sidebar_helper(source)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "tip"
    assert rows[0]["message_count"] == 168
    assert rows[0]["active_stream_id"] == "stream-1"
    assert rows[0]["pending_user_message"] == "keep working"
    assert rows[0]["pending_started_at"] == 123
    assert rows[0]["is_streaming"] is True
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
./scripts/test.sh tests/test_active_empty_session_sidebar.py::test_canonical_active_session_replaces_streaming_requested_alias -q
```

Expected: FAIL because the existing helper returns both the stale root row and an injected tip row.

- [ ] **Step 3: Confirm the pre-edit impact gate**

Run GitNexus upstream impact analysis for
`_sessionRowsWithActiveEphemeralSession` in `static/sessions.js`.

Observed after refreshing the worktree index on 2026-07-27:

- Risk: **MEDIUM**
- Direct caller: `renderSessionListFromCache`
- Transitive symbols: 66 through three levels
- Affected module: `Static`
- Indexed execution processes: none

This is below the repository's HIGH/CRITICAL warning gate, but it confirms that
all sidebar repaints pass through the helper. Keep the implementation guarded by
the explicit requested/canonical mapping so unrelated renders remain unchanged.

- [ ] **Step 4: Implement the minimal alias replacement**

Update `_sessionRowsWithActiveEphemeralSession()` so it:

1. Keeps the existing early return when the canonical active ID is already present.
2. Finds a cached alias only when `S.session.requested_session_id` is non-empty and differs from the canonical active ID.
3. Starts from canonical session metadata and adds only the enumerated runtime fallbacks from the alias.
4. Uses the canonical message count and identity.
5. Carries these live alias fields only when the canonical detail lacks their live value:
   `active_stream_id`, `pending_user_message`, `pending_attachments`,
   `pending_started_at`, `pending_user_source`, and `attention`.
6. ORs boolean runtime state for `has_pending_user_message`, `is_streaming`, and
   `is_working`; carries `activity_phase`, `activity_started_at`, and
   `activity_heartbeat_at` when canonical values are absent.
7. Replaces the alias at the same array index.
8. Retains the existing prepend behavior for a genuinely new active session
   with no requested alias.

Do not merge by title, infer from parent IDs, sum message counts, or mutate
`_allSessions`.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run:

```bash
./scripts/test.sh tests/test_active_empty_session_sidebar.py::test_canonical_active_session_replaces_streaming_requested_alias -q
```

Expected: 1 passed.

- [ ] **Step 6: Run related regression suites**

Run:

```bash
./scripts/test.sh \
  tests/test_active_empty_session_sidebar.py \
  tests/test_session_lineage_collapse.py \
  tests/test_bounded_session_detail_routes.py \
  tests/test_shared_state_db_session_projection.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Check formatting and affected scope**

Run:

```bash
git diff --check
```

Then run GitNexus `detect_changes({scope: "compare", base_ref: "main", worktree: "<worktree>"})`.

Expected: only the sidebar helper, its regression test, and the approved
spec/plan documentation are affected; no unrelated execution flow is reported.

- [ ] **Step 8: Commit the implementation**

```bash
git add static/sessions.js tests/test_active_empty_session_sidebar.py \
  docs/superpowers/plans/2026-07-27-compressed-session-sidebar-dedup.md
git commit -m "fix: deduplicate compressed sidebar continuation"
```
