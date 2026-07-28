# Release-Lite Lazy Tail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open a Hermes task from a bounded 30-message settled tail, attach its current live stream without a gap, and load older complete render units only on demand.

**Architecture:** Add a dedicated `/api/session-window` capability whose implementation never enters the legacy full-session merge. A focused `api/session_window.py` service composes the existing indexed canonical resolver, state-db cursor reader, bounded runtime snapshot, and signed tokens; the existing route stays thin. The browser adopts a distinct `lazy_tail_v1` source mode, paints before sidebar hydration, reconnects from the server-issued checkpoint, pages upward in bounded units, and invokes the old route only after an explicit legacy action.

**Tech Stack:** Python 3.11+, SQLite state store, existing HMAC cursor/token utilities, vanilla JavaScript, EventSource/SSE, pytest through `./scripts/test.sh`.

---

## Preconditions and invariant ledger

- Work only in `/Users/seb/hermes-webui/.worktrees/release-lite`.
- Preserve the user-owned `AGENTS.md` modification; never stage it.
- Run `superpowers:test-driven-development` for every behavior task.
- Before editing any existing symbol, run GitNexus `impact(..., direction="upstream")`.
- `loadSession` is already HIGH risk: 18 direct and 97 total dependents. Keep its edit to capability selection/adoption and cover stale load generations, profile switches, continuation, and live ownership.
- Before every commit run GitNexus `detect_changes(scope="all", worktree="/Users/seb/hermes-webui/.worktrees/release-lite")`.
- The open-path invariant is observable: a test double that raises from `get_session(..., metadata_only=False)` or the legacy merge must not be called by `/api/session-window`.
- The handoff invariant is observable: every active event belongs either to the returned checkpoint snapshot or to replay strictly after that checkpoint.
- The paging invariant is observable: each returned cursor is strictly older than every source row in the oldest complete render unit, with no tool-call/result split.
- Feature gates are default-off:
  - server: `HERMES_WEBUI_LAZY_TAIL_V1`
  - browser bootstrap capability: `lazy_tail_v1` only when server gate is enabled
  - browser local adoption: `HERMES_WEBUI_LAZY_TAIL_BROWSER_V1`

## File map

- Create `api/session_window.py`: request parsing, typed response states, bounded metadata/tail composition, lineage retry, signed older cursor, signed reconnect token, active-stream revalidation, diagnostics.
- Modify `api/routes.py`: register a thin `GET /api/session-window` handler and validate reconnect tokens in per-session SSE attachment.
- Modify `static/index.html`: expose the independently default-off browser adoption boolean in `window.__HERMES_CONFIG__`.
- Modify `static/sessions.js`: select/adopt `lazy_tail_v1`, retain loaded page state, prepend older pages, explicit legacy replacement, paint before sidebar refresh.
- Modify `static/messages.js`: pass the reconnect token when attaching and apply the returned checkpoint snapshot before declaring the live boundary current.
- Create `tests/test_release_lite_session_window.py`: service-level budgets, lineage/profile/error states, render-unit closure, tokens, and active handoff.
- Create `tests/test_release_lite_session_window_route.py`: route negotiation, default-off gate, no legacy fallback, 404/typed failures, SSE token binding.
- Create `tests/test_release_lite_session_window_browser.py`: static/browser contract checks for first paint, paging, stale-load veto, explicit legacy, sidebar ordering.
- Create `tests/test_release_lite_reconnect_handoff.py`: before/during/after event coverage and token validation.
- Modify `TESTING.md`: focused verification command and live acceptance procedure.

### Task 1: Define the bounded session-window service contract

**Files:**
- Create: `api/session_window.py`
- Create: `tests/test_release_lite_session_window.py`

- [ ] **Step 1: Run GitNexus impact for reused reader and resolver symbols**

Run impact for `read_state_db_message_page`, `resolve_shared_session`, `confirm_shared_session_target`, and the selected run-journal snapshot function. Stop and warn before editing if any result is HIGH or CRITICAL; this task initially imports them without modifying them.

- [ ] **Step 2: Write failing response-contract tests**

Add tests that construct a `SessionWindowRequest` and assert:

```python
request = SessionWindowRequest.parse(
    {"session_id": ["requested"], "msg_limit": ["30"], "resolve_model": ["0"]}
)
assert request.session_id == "requested"
assert request.visible_limit == 30
assert request.older_cursor is None

payload = build_session_window(request, deps=fake_deps)
assert payload["conversation_window"] == {
    "schema": "lazy_tail_v1",
    "state": "ready",
    "source": "state_db",
    "visible_count": 30,
    "has_older": True,
    "older_cursor": ANY_OPAQUE_TOKEN,
    "newest_message_id": "stable-30",
    "active_stream_id": None,
    "reconnect_token": None,
    "exact_total_available": False,
    "status_reason": None,
}
assert "message_count" not in payload
assert len(payload["messages"]) == 30
```

Also assert limits `0`, `51`, malformed values, absent session IDs, and an `older_cursor` mixed with a non-canonical requested ID fail with typed `SessionWindowRequestError`.

- [ ] **Step 3: Run the new tests and observe the missing module**

Run:

```bash
./scripts/test.sh tests/test_release_lite_session_window.py -q
```

Expected: FAIL during collection because `api.session_window` does not exist.

- [ ] **Step 4: Implement the minimal public types**

Implement these focused interfaces in `api/session_window.py`:

```python
INITIAL_VISIBLE_LIMIT = 30
MAX_VISIBLE_LIMIT = 50
MAX_LINEAGE_DEPTH = 128
MAX_RAW_ROWS = 512
MAX_TOOL_CLOSURE_ROWS = 64
MAX_SERIALIZED_BYTES = 2_621_440
READ_BUDGET_SECONDS = 0.750

@dataclass(frozen=True)
class SessionWindowRequest:
    session_id: str
    visible_limit: int
    older_cursor: str | None
    resolve_model: bool

    @classmethod
    def parse(cls, query: Mapping[str, list[str]]) -> "SessionWindowRequest": ...

class SessionWindowRequestError(ValueError):
    code: str
    status: int

def build_session_window(
    request: SessionWindowRequest,
    *,
    deps: SessionWindowDependencies | None = None,
) -> dict: ...
```

Keep dependencies injectable so tests can prove forbidden legacy functions are not reached. Return typed `ready`, `reconnecting`, `legacy_required`, or `stale` payloads; unknown states are impossible from this module.

- [ ] **Step 5: Run the contract tests**

Run the same focused command. Expected: PASS for parsing/shape tests; behavioral tests added in later tasks may remain deselected by exact test names.

- [ ] **Step 6: Review affected scope and commit**

Run GitNexus `detect_changes(scope="all", worktree=...)`, then:

```bash
git add api/session_window.py tests/test_release_lite_session_window.py
git commit -m "feat: define bounded session window contract"
```

Expected: commit includes exactly those two files.

### Task 2: Read the settled tail without legacy reconstruction

**Files:**
- Modify: `api/session_window.py`
- Modify: `tests/test_release_lite_session_window.py`

- [ ] **Step 1: Write failing bounded-reader tests**

Use a fixture with 45 lineage segments and generated rows. Assert:

```python
assert len(result["messages"]) <= 30
assert result["conversation_window"]["visible_count"] == len(result["messages"])
assert result["conversation_window"]["exact_total_available"] is False
for forbidden in fake_deps.legacy_calls:
    pytest.fail(f"legacy work entered: {forbidden}")
assert fake_deps.raw_rows_examined <= 512 + 64
assert fake_deps.lineage_segments_examined <= 128
assert serialized_message_bytes(result) <= 2_621_440
```

Add cases for profile mismatch, lineage cycle, inactive/rewound rows, sidecar-only history, oversized rows, stale lineage generation, and one successful generation retry. `legacy_required` and `stale` must contain no false `ready` claim and must not trigger a full merge.

- [ ] **Step 2: Verify the tests fail for missing behavior**

Run:

```bash
./scripts/test.sh tests/test_release_lite_session_window.py -q
```

Expected: FAIL because the service does not yet invoke the bounded resolver/reader.

- [ ] **Step 3: Implement the bounded composition**

Compose the existing indexed APIs:

```python
target = deps.resolve_shared_session(request.session_id, profile=deps.active_profile())
proof = deps.confirm_shared_session_target(target, max_depth=MAX_LINEAGE_DEPTH)
page = deps.read_state_db_message_page(
    proof,
    cursor=request.older_cursor,
    visible_limit=request.visible_limit,
    raw_row_limit=MAX_RAW_ROWS,
    closure_row_limit=MAX_TOOL_CLOSURE_ROWS,
    serialized_byte_limit=MAX_SERIALIZED_BYTES,
    deadline=deps.monotonic() + READ_BUDGET_SECONDS,
)
```

Translate only bounded proof/reader results. Preserve stable state-db message IDs, chronological order, visibility flags, and redaction. Retry once only for a lineage-generation mismatch. On any budget/proof failure, emit a typed fast state rather than importing/calling the full merger.

- [ ] **Step 4: Verify focused and existing cursor tests**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window.py \
  tests/test_state_db_message_cursor_reader.py \
  tests/test_session_message_cursor.py \
  tests/test_bounded_session_cursor_paging.py -q
```

Expected: all PASS.

- [ ] **Step 5: Review affected scope and commit**

Run GitNexus `detect_changes`, then commit:

```bash
git add api/session_window.py tests/test_release_lite_session_window.py
git commit -m "feat: read bounded settled task tails"
```

### Task 3: Add signed older and reconnect tokens with atomic handoff

**Files:**
- Modify: `api/session_window.py`
- Modify: `tests/test_release_lite_session_window.py`
- Create: `tests/test_release_lite_reconnect_handoff.py`

- [ ] **Step 1: Write failing token-binding tests**

Assert an older cursor is rejected after any profile, canonical-task, source-mode, lineage-fingerprint, or stable-position mutation. Assert a reconnect token is rejected after profile, canonical-task, stream-ID, checkpoint, expiry, or signature mutation. Token payloads must never be accepted unsigned.

- [ ] **Step 2: Write failing no-gap handoff tests**

Parameterize events emitted:

1. before checkpoint capture;
2. during the state-db read;
3. after the read but before revalidation;
4. after revalidation.

The test computes:

```python
covered = snapshot_event_ids | replay_event_ids
assert covered == all_active_event_ids
assert snapshot_event_ids.isdisjoint(replay_event_ids)
```

When stream identity changes once, assert one retry. When it changes twice or is ambiguous, assert `state == "reconnecting"` and `status_reason == "reconnect_ambiguous"` with no uncertain overlay.

Pin the transport names in the tests:

```python
assert result["runtime_snapshot"] == {
    "schema": "run_snapshot_v1",
    "stream_id": "run-1",
    "through_event_id": "run-1:41",
    "messages": expected_bounded_inflight_messages,
    "status": "running",
}
assert result["conversation_window"]["reconnect_token"]
```

The SSE attachment test must receive one control event before later run events:

```text
event: reconnect_ack
data: {
  "schema": "lazy_tail_reconnect_ack_v1",
  "stream_id": "run-1",
  "checkpoint_event_id": "run-1:41",
  "next_event_id": "run-1:42"
}
```

`checkpoint_event_id` must equal `runtime_snapshot.through_event_id`. The browser may clear `Reconnecting` only after it has applied that snapshot and received this matching acknowledgment.

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window.py \
  tests/test_release_lite_reconnect_handoff.py -q
```

Expected: FAIL because tokens/checkpoint handoff are absent.

- [ ] **Step 4: Implement integrity-protected tokens and handoff**

Use the repository’s existing secret/HMAC token convention. Define versioned claims:

```python
OlderCursorClaims(
    version=1, profile_id=..., canonical_session_id=...,
    lineage_fingerprint=..., source="state_db", positions=...
)
ReconnectClaims(
    version=1, profile_id=..., canonical_session_id=...,
    stream_id=..., checkpoint_event_id=..., expires_at=...
)
```

Capture active stream + durable snapshot checkpoint before the tail read, revalidate stream identity after it, and retry the whole bounded operation once if identity changed. Return the snapshot needed to render the bounded in-flight turn and the signed reconnect token; never expose a browser-editable raw checkpoint as authority.

The response/transport contract is:

- top-level `runtime_snapshot` is either `null` or the bounded `run_snapshot_v1` object above;
- `conversation_window.reconnect_token` is the only attachment authority;
- the token binds the same `stream_id` and `checkpoint_event_id` carried by the snapshot;
- successful attachment emits `lazy_tail_reconnect_ack_v1` before any event newer than that checkpoint;
- mismatch among token, snapshot, active owner, or acknowledgment is a typed reconnect failure and cannot clear the reconnecting state.

- [ ] **Step 5: Run focused tests**

Run the Task 3 command. Expected: all PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/session_window.py tests/test_release_lite_session_window.py tests/test_release_lite_reconnect_handoff.py
git commit -m "feat: add atomic lazy-tail reconnect handoff"
```

### Task 4: Expose a non-fallback route and authenticated replay attachment

**Files:**
- Modify: `api/routes.py`
- Modify: `api/session_window.py`
- Modify: `static/index.html`
- Create: `tests/test_release_lite_session_window_route.py`
- Modify: `tests/test_release_lite_reconnect_handoff.py`

- [ ] **Step 1: Run required route/symbol impact checks**

Run GitNexus API impact for `GET /api/session-window` (expected absent), then symbol impact for the route dispatch function, `_handle_session_run_journal_stream_for_session`, and `_session_events_resume_event_id`. Record UNKNOWN results as unindexed, not safe.

- [ ] **Step 2: Write failing route tests**

Assert:

- gate off returns 404 quickly;
- gate on returns `lazy_tail_v1`;
- app-shell injection sets `window.__HERMES_CONFIG__.lazyTailV1` from the separate `HERMES_WEBUI_LAZY_TAIL_BROWSER_V1` gate, defaulting to literal `false`;
- invalid requests return typed 400/404;
- ineligible history returns 200 `legacy_required`;
- patching legacy `get_session(metadata_only=False)` and full merger to raise does not affect the new route;
- `/api/session` behavior remains unchanged;
- SSE attachment with a valid token starts exactly after its checkpoint;
- missing, forged, expired, wrong-profile, wrong-task, or wrong-stream token is rejected without replay.

- [ ] **Step 3: Verify failure**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_route.py \
  tests/test_release_lite_reconnect_handoff.py -q
```

Expected: FAIL because the route is absent.

- [ ] **Step 4: Add a thin route**

Add:

```python
if parsed.path == "/api/session-window":
    if not _lazy_tail_server_enabled():
        return j(handler, {"error": "not found"}, status=404)
    return _handle_session_window(handler, parsed)
```

The handler parses the query, calls `build_session_window`, maps only typed errors, and adds no legacy fallback. Extend the existing session-event attachment to accept `reconnect_token`, validate it server-side, derive the replay checkpoint from claims, and emit the named `reconnect_ack` control event. Keep existing `Last-Event-ID` behavior for old clients.

Expose the independent browser gate through the existing app-shell replacement path:

```html
<script>
window.__HERMES_CONFIG__={
  maxUploadBytes:__MAX_UPLOAD_BYTES__,
  csrfToken:__CSRF_TOKEN_JSON__,
  lazyTailV1:__LAZY_TAIL_BROWSER_V1__
};
</script>
```

`_render_index_shell_base()` replaces `__LAZY_TAIL_BROWSER_V1__` with JSON literal `true` only when `HERMES_WEBUI_LAZY_TAIL_BROWSER_V1=1`; otherwise it replaces it with `false`. JavaScript never reads the process environment directly.

- [ ] **Step 5: Run route and regression tests**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_route.py \
  tests/test_release_lite_reconnect_handoff.py \
  tests/test_session_cursor_paging_route.py \
  tests/test_stream_offline_gap_recovery.py -q
```

Expected: all PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/routes.py api/session_window.py static/index.html tests/test_release_lite_session_window_route.py tests/test_release_lite_reconnect_handoff.py
git commit -m "feat: expose lazy task window route"
```

### Task 5: Adopt lazy-tail first paint without automatic legacy fallback

**Files:**
- Modify: `static/sessions.js`
- Create: `tests/test_release_lite_session_window_browser.py`

- [ ] **Step 1: Reconfirm HIGH-impact navigation scope**

Re-run GitNexus impact for `loadSession`, `_ensureMessagesLoaded`, and `_loadOlderMessages`. Do not broaden the existing HIGH-risk `loadSession` edit beyond the request URL, response validation, source-mode state, paint ordering, and explicit legacy branch.

- [ ] **Step 2: Write failing browser contract tests**

Extract/evaluate the relevant JavaScript helpers and assert:

```javascript
assert.equal(requestedUrl.pathname, "/api/session-window");
assert.equal(requestedUrl.searchParams.get("msg_limit"), "30");
assert.equal(state.sourceMode, "lazy_tail_v1");
assert.equal(state.messages.length, 30);
assert.equal("message_count" in state.session, false);
assert.equal(autoLegacyRequests.length, 0);
```

Cover unknown schema/state fail-closed behavior, 404/malformed response presenting the explicit legacy action, stale load-generation veto, matching-task browser `INFLIGHT` adoption, mismatched task/stream rejection, and `renderMessages()` occurring before sidebar refresh begins.

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
./scripts/test.sh tests/test_release_lite_session_window_browser.py -q
```

Expected: FAIL because `loadSession` still requests `/api/session`.

- [ ] **Step 4: Implement minimal source-mode adoption**

When `window.__HERMES_CONFIG__.lazyTailV1 === true`, request:

```javascript
`/api/session-window?session_id=${encodeURIComponent(sid)}&msg_limit=30&resolve_model=0`
```

Validate `conversation_window.schema === "lazy_tail_v1"` and the finite state set before committing. Store `{sourceMode, olderCursor, hasOlder, stableIds, reconnect}` separately from legacy paging. Commit only when pane/session/load-generation guards still match. Paint the bounded tail and matching local `INFLIGHT` state first; trigger sidebar refresh after paint without awaiting it. A 404, `legacy_required`, `stale`, or malformed response shows an action and never invokes `/api/session` automatically.

- [ ] **Step 5: Run focused and shared navigation tests**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_browser.py \
  tests/test_bounded_session_browser_adoption.py \
  tests/test_bounded_conversation_route_seam.py \
  tests/test_stream_offline_gap_recovery.py -q
```

Expected: all PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add static/sessions.js tests/test_release_lite_session_window_browser.py
git commit -m "feat: paint lazy task tails first"
```

### Task 6: Page older render units and explicitly replace with legacy history

**Files:**
- Modify: `static/sessions.js`
- Modify: `tests/test_release_lite_session_window_browser.py`

- [ ] **Step 1: Write failing five-page paging tests**

Simulate five 50-row pages and assert 280 loaded rows remain ordered, deduped by stable ID, and preserve the measured first-visible-row anchor after every prepend. Assert no offset or total is calculated. A stale cursor gets one fresh-window restart; a second mismatch stops with a visible recovery action.

Add an explicit-legacy test:

```javascript
await loadCompleteLegacyTranscript();
assert.equal(legacyRequests.length, 1);
assert.equal(state.sourceMode, "legacy");
assert.deepEqual(state.messages, completeLegacyMessages);
assert.equal(mixedLazyAndLegacyRows, false);
```

- [ ] **Step 2: Verify failure**

Run the browser test file. Expected: FAIL because existing paging targets `/api/session` cursor mode and existing recovery may auto-force legacy.

- [ ] **Step 3: Implement lazy older paging**

For `sourceMode === "lazy_tail_v1"`, `_loadOlderMessages` calls `/api/session-window` with `older_cursor` and `msg_limit=50`, validates the same schema/canonical task, prepends stable unseen units, and updates only the opaque cursor/has-older state. Preserve the scroll anchor with the existing measurement mechanism. Keep legacy paging branches unchanged.

Add `Load complete legacy transcript`; warn that very large tasks may take time, then call the old full route only from that click and replace the lazy state atomically under stale-load guards.

- [ ] **Step 4: Run browser regression tests**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_browser.py \
  tests/test_bounded_session_cursor_paging.py \
  tests/test_bounded_session_browser_adoption.py -q
```

Expected: all PASS.

- [ ] **Step 5: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add static/sessions.js tests/test_release_lite_session_window_browser.py
git commit -m "feat: page older task messages on demand"
```

### Task 7: Reattach live output from the signed checkpoint

**Files:**
- Modify: `static/messages.js`
- Modify: `static/sessions.js`
- Modify: `tests/test_release_lite_session_window_browser.py`
- Modify: `tests/test_release_lite_reconnect_handoff.py`

- [ ] **Step 1: Run symbol impact**

Run GitNexus impact for `startSessionStream` (currently LOW, four direct callers) and any helper changed to apply stream snapshots.

- [ ] **Step 2: Write failing browser reconnect tests**

Assert a remote-active response renders `Reconnecting to latest turn`, applies the bounded checkpoint snapshot, then connects using only the returned token. The reconnect label clears only after snapshot application and live-boundary acknowledgment. Assert old-task events cannot commit after a task switch and overlap is deduped by stable identity.

- [ ] **Step 3: Verify failure**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_browser.py \
  tests/test_release_lite_reconnect_handoff.py -q
```

Expected: FAIL because `startSessionStream` has no reconnect-token handoff.

- [ ] **Step 4: Implement token-aware attachment**

Extend `startSessionStream(sid, options = {})` without changing old callers. When `options.reconnectToken` exists, add it to the authenticated session event URL; apply `options.runtimeSnapshot` only when it is `run_snapshot_v1` and matches the task/stream/load generation. Listen for `reconnect_ack`; require schema `lazy_tail_reconnect_ack_v1`, matching stream ID, and `checkpoint_event_id === options.runtimeSnapshot.through_event_id` before clearing the label. Events at or before the snapshot boundary dedupe by stable identity; events after it apply normally. Preserve existing browser-local `INFLIGHT` priority for the exact task/stream.

- [ ] **Step 5: Run live-stream regression tests**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window_browser.py \
  tests/test_release_lite_reconnect_handoff.py \
  tests/test_stream_offline_gap_recovery.py \
  tests/test_session_run_journal_stream.py -q
```

If the last file does not exist, use the repository’s run-journal stream route test returned by `rg --files tests | rg 'run_journal|session.*stream'`. Expected: all selected tests PASS.

- [ ] **Step 6: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add static/messages.js static/sessions.js tests/test_release_lite_session_window_browser.py tests/test_release_lite_reconnect_handoff.py
git commit -m "feat: reattach lazy task windows without gaps"
```

### Task 8: Add bounded, non-sensitive rollout observability

**Files:**
- Modify: `api/session_window.py`
- Modify: `api/routes.py`
- Modify: `static/sessions.js`
- Modify: `static/messages.js`
- Create: `tests/test_release_lite_observability.py`

- [ ] **Step 1: Write failing server-diagnostic tests**

Inject a diagnostic sink and assert each session-window request records only bounded scalar metrics:

```python
assert diagnostic == {
    "state": "ready",
    "lineage_depth": 45,
    "sql_count": expected_sql_count,
    "raw_rows_examined": expected_raw_rows,
    "visible_rows": 30,
    "serialized_bytes": expected_bytes,
    "state_db_read_ms": expected_ms,
    "handoff_retry_count": 0,
}
assert not (set(diagnostic) & {
    "command", "arguments", "messages", "transcript", "path", "tool_output"
})
```

Use `RequestDiagnostics` at the route boundary and a dependency-injected monotonic clock in the service. Add reconnect start/attach/failure counters with typed reason only.

- [ ] **Step 2: Write failing browser-event tests**

Assert the browser emits bounded client events for:

- `lazy_tail_first_paint`;
- `lazy_tail_reconnect_start|attached|failed`;
- `lazy_tail_older_page` with page index and anchor result;
- `lazy_tail_legacy_explicit`.

Payloads may contain only event/source/session ID/stream ID/state/reason, bounded counts, elapsed milliseconds, and `anchor_result=preserved|lost`. They must not contain messages, commands, arguments, paths, URLs with query strings, or tool output.

- [ ] **Step 3: Run and verify failure**

Run:

```bash
./scripts/test.sh tests/test_release_lite_observability.py -q
```

Expected: FAIL because release-lite diagnostics are absent.

- [ ] **Step 4: Implement server metrics**

Add a small `SessionWindowDiagnostics` accumulator to `api/session_window.py` and publish it through `RequestDiagnostics` in the route. Log one structured summary at INFO only when the existing diagnostics policy allows it. Values are counts, milliseconds, enumerated state/reason, and stream/session opaque IDs; never include transcript or request arguments.

- [ ] **Step 5: Implement browser metrics through the sanitized endpoint**

Add `recordLazyTailEvent(event, details)` in `static/sessions.js`, calling `/api/client-events/log`. Extend `_CLIENT_EVENT_ALLOWED_FIELDS` and `_sanitize_client_event_payload` only for bounded scalar fields:

```text
state, source_mode, page_index, visible_count, elapsed_ms,
anchor_result, reconnect_status
```

Reuse it from `static/messages.js` for reconnect events. Clamp numeric strings client-side and server-side; keep the existing 4 KiB body limit and rate limit.

- [ ] **Step 6: Run observability and sanitizer regressions**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_observability.py \
  tests/test_client_event_logging.py \
  tests/test_release_lite_session_window_route.py \
  tests/test_release_lite_session_window_browser.py -q
```

If the sanitizer test has a different name, select the existing file found by `rg --files tests | rg 'client.*event'`. Expected: all PASS.

- [ ] **Step 7: Review and commit**

Run GitNexus `detect_changes`, then:

```bash
git add api/session_window.py api/routes.py static/sessions.js static/messages.js tests/test_release_lite_observability.py
git commit -m "feat: add lazy task window diagnostics"
```

### Task 9: Document, benchmark, and seal the lazy-tail change

**Files:**
- Modify: `TESTING.md`
- Create: `tests/test_release_lite_session_window_performance.py`

- [ ] **Step 1: Add a deterministic scale/performance test**

Build or reuse a 45-segment, 42,632-row, 21,993-tool-call fixture. Instrument legacy functions, raw rows, bytes, query count, and elapsed reader time. Assert exactly the bounded properties; use a generous CI ceiling while recording elapsed time rather than encoding the live p95 target as a flaky unit-test threshold.

Run the same target-page read with zero unrelated sessions and after inserting at least 10,000 unrelated-session rows. Assert query count, lineage depth, and raw rows examined are identical, returned stable IDs are identical, and the query plan continues to use the session/lineage indexes. Use a generous elapsed-time ratio only as a smoke check; the structural work counters and query plan are the deterministic proof that page work is independent of unrelated-session volume.

- [ ] **Step 2: Run the performance test**

Run:

```bash
./scripts/test.sh tests/test_release_lite_session_window_performance.py -q
```

Expected: PASS and diagnostic output showing at most 30 visible rows, at most 576 raw rows including closure, and zero legacy calls.

- [ ] **Step 3: Update testing guidance**

Document the focused command, both feature gates, isolated-state live start, explicit legacy check, five-page scroll-anchor check, and the 40 warm/20 process-cold timing matrix. Do not edit `CHANGELOG.md`.

- [ ] **Step 4: Run the full focused suite**

Run:

```bash
./scripts/test.sh \
  tests/test_release_lite_session_window.py \
  tests/test_release_lite_session_window_route.py \
  tests/test_release_lite_session_window_browser.py \
  tests/test_release_lite_reconnect_handoff.py \
  tests/test_release_lite_observability.py \
  tests/test_release_lite_session_window_performance.py \
  tests/test_state_db_message_cursor_capability.py \
  tests/test_state_db_message_cursor_reader.py \
  tests/test_session_message_cursor.py \
  tests/test_session_cursor_paging_route.py \
  tests/test_session_cursor_paging_shadow.py \
  tests/test_bounded_conversation_route_seam.py \
  tests/test_bounded_session_browser_adoption.py \
  tests/test_bounded_session_cursor_paging.py \
  tests/test_stream_offline_gap_recovery.py -q
```

Expected: all PASS.

- [ ] **Step 5: Run full repository verification**

Run:

```bash
./scripts/test.sh
```

Expected: exit 0. Record exact passed/skipped counts.

- [ ] **Step 6: Run change detection and commit docs/performance test**

Run GitNexus `detect_changes(scope="compare", base_ref="master", worktree=...)`. Verify only session-window, route, browser session/stream, tests, and `TESTING.md` flows are affected. Then:

```bash
git add TESTING.md tests/test_release_lite_session_window_performance.py
git commit -m "test: seal lazy task window rollout"
```

- [ ] **Step 7: Run semantic shadow comparison before browser enablement**

With the server reader gate on and browser gate off, compare the new initial page and five older pages against the existing state-db-visible oracle for the same stable snapshot. Compare chronological stable IDs, visible content after redaction, active/inactive/rewound filtering, and complete render-unit/tool-pair boundaries. Record exact match plus the bounded reader diagnostics. A mismatch blocks browser enablement; do not publish a receipt or silently fall back.

For the exact production task, take the shadow comparison only at a proven stable lineage generation. If it changes during comparison, discard the sample and retry once; a second change is a failed acceptance sample, not a match.

- [ ] **Step 8: Perform isolated and production-task acceptance**

First validate with isolated `HERMES_HOME`/`HERMES_WEBUI_STATE_DIR`. Then, without mutating or repairing production state, enable the server/browser gates and time the exact task `20260728_080319_c4d295`: 20 cold opens and 40 warm opens. Verify newest settled content is below the agreed p95s, active state attaches or truthfully stays reconnecting, five pages preserve order/anchor, and no request enters the complete sidecar/full merge.

Record every initial and older-page request duration and fail acceptance if any lazy-tail request exceeds 5 seconds. Repeat a bounded page read before and after adding a large unrelated-session fixture in isolated state; query count, raw rows examined, lineage depth, stable IDs, and query plan must remain unchanged.

- [ ] **Step 9: Request code review**

Use `superpowers:requesting-code-review`, address blocking findings, rerun the focused suite, and do not claim completion without `superpowers:verification-before-completion`.
