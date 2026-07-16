# Bounded Conversation Load Stage 2C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt the negotiated bounded initial view and opaque older-message cursor in the browser without weakening session-switch, viewport, or full-history mutation behavior.

**Architecture:** `static/sessions.js` gains one browser-local paging state object and strict response parser behind an operator-controlled gate. Cursor and legacy numeric paging remain separate modes; any cursor mismatch performs exactly one fresh initial reload, while fork/edit/undo/export continue using the legacy full-history compatibility path.

**Tech Stack:** Vanilla JavaScript, existing `/api/session`, static-source and browser-flow pytest tests via `./scripts/test.sh`.

---

## File structure

- Modify `static/sessions.js`: parser, mode state, one-request open, cursor older-page branch/restart.
- Modify `api/routes.py`: expose non-persisted browser adoption flag in settings/bootstrap payload.
- Read `api/conversation_shadow_evidence.py`: require current durable readiness before exposing adoption.
- Modify `scripts/benchmark_conversation_load.py`: browser-open request-count/SLO gate.
- Create `tests/test_bounded_session_browser_adoption.py`.
- Create `tests/test_bounded_session_cursor_paging.py`.
- Modify existing paging/race/full-history tests.
- Modify `ARCHITECTURE.md` and `TESTING.md` for rollback and manual acceptance.

## Impact gate

`loadSession` is manually HIGH risk; `_ensureMessagesLoaded` and `_loadOlderMessages` are MEDIUM/HIGH. Preserve `_loadSessionGeneration`, `_isCurrentLoad`, `_messagesGeneration`, active-pane ownership, scroll anchoring, numeric offsets, and `_ensureAllMessagesLoaded`. No cursor-mode code may manufacture absolute message indexes.

### Task 1: Strict browser paging parser and gate

**Files:**
- Modify: `static/sessions.js:2751-2809, 3334-3354`
- Modify: `api/routes.py:12697-12752`
- Create: `tests/test_bounded_session_browser_adoption.py`

- [ ] **Step 1: Write failing static/parser tests**

Specify one state object:

```javascript
let _messagePaging = {
  mode: 'legacy',
  beforeCursor: null,
  hasMore: false,
  visibleCount: 0,
  restartAttempted: false,
};
```

Test that only a complete `message_page.mode === 'cursor_v1'` with string/null cursor, boolean `has_more`, and bounded numeric counts is adopted. Malformed/unknown payloads initialize legacy state from `_messages_offset/_messages_truncated`.

- [ ] **Step 2: Run RED**

Run: `./scripts/test.sh tests/test_bounded_session_browser_adoption.py -q`

Expected: FAIL because parser/state/gate are absent.

- [ ] **Step 3: Expose the operator gate additively**

Expose `HERMES_WEBUI_BOUNDED_CONVERSATION_BROWSER`, default false, through the existing settings/bootstrap response as a non-persisted capability. The exposed value is true only when the operator flag requests it **and** the current Stage 2B `ShadowReadiness` cohort is ready (at least 1,000 complete zero-difference samples spanning seven days), the target-content-proof capability exists, and receipt/view-state/cursor server gates are available. Missing/corrupt/stale/latched-disabled evidence exposes false. Do not add a user preference or mutate settings, and do not let an environment flag bypass evidence.

- [ ] **Step 4: Implement strict parser and reset helper**

Reset paging state on every session/load-generation change. Cursor state never updates legacy `_oldestIdx`; legacy state never retains an opaque cursor.

- [ ] **Step 5: Run GREEN**

Run: `./scripts/test.sh tests/test_bounded_session_browser_adoption.py tests/test_topbar_lazy_message_count.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: add strict browser cursor paging state`

### Task 2: One negotiated initial conversation request

**Files:**
- Modify: `static/sessions.js:1464-2195, 2845-2958`
- Modify: `tests/test_bounded_session_browser_adoption.py`

- [ ] **Step 1: Write failing one-request tests**

Under the browser gate, require:

```text
/api/session?session_id=<id>&messages=1&resolve_model=0&msg_limit=30&message_paging=cursor_v1
```

Assert `_ensureMessagesLoaded` is not called after adopting that response. With gate off, preserve metadata-then-messages requests exactly. An old server response without `message_page` is immediately usable as legacy messages from the same request.

- [ ] **Step 2: Add race/ownership RED cases**

Simulate A -> B -> C responses out of order. Neither A nor B may commit session, messages, todo, paging, runtime journal, or scroll state after C owns the generation.

- [ ] **Step 3: Run RED**

Run: `./scripts/test.sh tests/test_bounded_session_browser_adoption.py tests/test_cross_session_message_load_isolation.py -q`

Expected: FAIL on the current sequential flow.

- [ ] **Step 4: Implement the gated request branch**

Keep existing loading UI and deferred model/provider resolution. Apply `_isCurrentLoad` immediately before every state commit. Parse mode only after the complete response passes profile/session/canonical ownership checks.

- [ ] **Step 5: Run GREEN plus cold todo tests**

Run: `./scripts/test.sh tests/test_bounded_session_browser_adoption.py tests/test_cross_session_message_load_isolation.py tests/test_todo_panel_cold_load_static.py tests/test_parallel_session_switch.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: open conversations with one negotiated request`

### Task 3: Cursor older-page branch and one restart

**Files:**
- Modify: `static/sessions.js:3355-3503`
- Create: `tests/test_bounded_session_cursor_paging.py`

- [ ] **Step 1: Write failing cursor request tests**

Cursor mode must call:

```text
/api/session?...&messages=1&message_paging=cursor_v1&msg_cursor=<opaque>&msg_limit=30
```

Assert no `msg_before`, strict response mode validation, stable `_state_db_message_id` overlap dedupe, chronological prepend, and existing viewport-anchor restoration.

- [ ] **Step 2: Write failing restart tests**

On 409 `cursor_restart_required`, clear opaque coordinates and perform exactly one fresh negotiated initial request. Replace/reconcile the initial page using stable identity, never prepend a legacy response to cursor-loaded rows, and never loop if the retry also degrades/fails.

- [ ] **Step 3: Run RED**

Run: `./scripts/test.sh tests/test_bounded_session_cursor_paging.py -q`

Expected: FAIL because `_loadOlderMessages` is numeric-only.

- [ ] **Step 4: Implement the separate cursor branch**

Leave the current numeric branch intact. Capture session ID plus load/message generations before fetch and veto stale results. `has_more` and cursor come only from the validated page object.

- [ ] **Step 5: Run GREEN and scroll/race regressions**

Run: `./scripts/test.sh tests/test_bounded_session_cursor_paging.py tests/test_parallel_session_switch.py tests/test_issue1937_endless_scroll_jumpstart_race.py tests/test_older_history_viewport_preservation.py tests/test_session_endless_scroll.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: page older conversation history by cursor`

### Task 4: Full-history mutation compatibility

**Files:**
- Modify: `static/sessions.js:3506-3567` only if a mode guard is required
- Modify: `tests/test_bounded_session_cursor_paging.py`

- [ ] **Step 1: Write failing absolute-index compatibility tests**

For fork-from-here, edit, undo, export/share, compression inspection, and any absolute-index operation, assert `_ensureAllMessagesLoaded` obtains the exact legacy full transcript before calculating coordinates. Cursor page counts/positions must not become mutation indexes.

- [ ] **Step 2: Run RED or prove existing behavior**

Run: `./scripts/test.sh tests/test_issue2184_fork_from_here_absolute_index.py tests/test_tool_call_history_paging.py tests/test_bounded_session_cursor_paging.py -q`

Expected: at least one new cursor-mode assertion FAILS before the guard; if all pass because existing full-load behavior is already sufficient, record that evidence and make no production edit.

- [ ] **Step 3: Add the minimal guard if required**

Temporarily use the legacy full-history response for the mutation, then reset paging state consistently. Do not request unbounded cursor pages or derive an offset.

- [ ] **Step 4: Run GREEN**

Run: `./scripts/test.sh tests/test_issue2184_fork_from_here_absolute_index.py tests/test_tool_call_history_paging.py tests/test_topbar_lazy_message_count.py tests/test_webui_external_refresh_frontend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit only if code changed**

Commit: `fix: preserve full history for indexed mutations`

### Task 5: Rollback matrix and browser acceptance

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `TESTING.md`
- Modify: `tests/test_bounded_session_browser_adoption.py`
- Modify: `tests/test_bounded_session_cursor_paging.py`
- Modify: `scripts/benchmark_conversation_load.py`
- Modify: `tests/test_conversation_load_benchmark.py`

- [ ] **Step 1: Lock the compatibility matrix**

Automate old-client/new-server, new-client/old-server, server cursor disabled/degraded, browser gate disabled, receipt/view-state read gates disabled, and one-restart behavior.

- [ ] **Step 2: Prove the evidence gate and document enablement/rollback**

Automate bootstrap/readiness cases for 999 samples, 1,000 samples under seven days, 1,000 samples at seven days, one semantic difference, clock rollback, corrupt evidence, evidence from a previous build/schema cohort, and current ready evidence. Only the last case may expose adoption. Document the read-only operational status command and evidence receipt fields; never document a command that forges/increments samples.

Browser adoption remains off until Stage 2A query/semantic gates and Stage 2B's sampled zero-diff threshold pass in real shadow traffic. Disabling the browser gate restores the two-request numeric flow without removing dormant server support. A latched semantic difference keeps adoption off for that implementation cohort even if the flag remains set.

- [ ] **Step 3: Run the Stage 2C regression bundle**

Run:

```bash
./scripts/test.sh \
  tests/test_bounded_session_browser_adoption.py \
  tests/test_bounded_session_cursor_paging.py \
  tests/test_cross_session_message_load_isolation.py \
  tests/test_parallel_session_switch.py \
  tests/test_issue1937_endless_scroll_jumpstart_race.py \
  tests/test_older_history_viewport_preservation.py \
  tests/test_todo_live_frontend_static.py \
  tests/test_todo_panel_cold_load_static.py \
  tests/test_issue2184_fork_from_here_absolute_index.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the shared browser-open benchmark and request-shape gate**

Extend the checked-in runner's `browser-open` stage to capture the authenticated network request sequence. Against the manifest-declared synthetic `proof-v1` cohort with ready injected acceptance evidence, assert one initial `/api/session?...messages=1&message_paging=cursor_v1` request, one server canonical resolution, no follow-up `_ensureMessagesLoaded` request, and all Stage 2B SQL/row/byte/SLO limits. Against the `current` cohort or any non-ready evidence case, assert adoption is not exposed and the unchanged two-request numeric flow remains exact. Run proof base/scaling warm 40, process-cold 20, and concurrency-4 stress 20; no request exceeds 5 seconds, scaling regression is at most `max(100ms,20%)`, and no load changes source mode mid-flight.

Run:

```bash
./scripts/test.sh tests/test_conversation_load_benchmark.py -k "browser_open or duplicate_resolution or readiness" -q
.venv/bin/python scripts/benchmark_conversation_load.py --stage browser-open --fixture .verify/conversation-load/current-base --expect-mode legacy --warm 40 --process-cold 20 --output .verify/conversation-load/stage-2c-current.json
.venv/bin/python scripts/benchmark_conversation_load.py --stage browser-open --fixture .verify/conversation-load/proof-base --expect-mode cursor_v1 --synthetic-ready-evidence --visible-limit 30 --warm 40 --process-cold 20 --concurrency 4 --stress-rounds 20 --compare-fixture .verify/conversation-load/proof-scaling --output .verify/conversation-load/stage-2c-proof.json
```

The runner rejects synthetic evidence for a `current` manifest. This proves the browser code and bounded SLOs without claiming that today's production Agent schema can enable cursor mode.

- [ ] **Step 5: Run static and diff checks**

Run: `git diff --check`

Run the repository's JavaScript/static regression commands documented in `TESTING.md` if separate from the pytest bundle.

Expected: exit 0.

- [ ] **Step 6: Run GitNexus detect-changes and commit**

Confirm only session-load/paging/browser flows changed.

Commit: `docs: document bounded browser conversation loading`

- [ ] **Step 7: Run full supported verification**

Run: `./scripts/test.sh`

Expected: all tests pass, with any reproduction of the recorded baseline Agent-import anomaly reported separately and rerun in isolation.
