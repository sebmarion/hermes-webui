# Release-Lite Lazy Tail and Guardrail Design

**Date:** 2026-07-28
**Status:** Approved for specification
**Owners:** Hermes WebUI and Hermes Agent

## Summary

The release-lite goal is perceived task responsiveness, not immediate
full-transcript reconstruction.

Opening a task should show:

1. the newest 30 settled visible messages;
2. the current live/reconnecting state;
3. newly arriving live output.

Older messages load only when the user asks for them. Exact total counts are
not part of the open path. Normal opening never parses and merges the complete
conversation.

The paired guardrail fix is intentionally narrow:

- distinct `terminal` request signatures do not trip the broad same-tool hard
  stop;
- identical failed commands retain the existing exact-signature block;
- a genuine structured guardrail stop is rendered as `Needs recovery`, never
  ordinary completion.

This design delivers the highest-value parts of:

- `2026-07-28-active-bounded-conversation-open-design.md`;
- `2026-07-28-guardrail-strategy-recovery-design.md`.

The deeper exact-count, cross-store projection, automatic effectful-trigger
recovery, and unlimited virtualized history contracts remain deferred.

## User priority

The primary requirement is:

> Show where the task is now and the latest message quickly.

Secondary requirement:

> Older history must remain available, but it may be loaded only when needed.

Exact total message counts may be missing or delayed indefinitely without
blocking task use.

## Scope boundaries

The release contains two independent implementation commits and rollback
switches:

1. **WebUI lazy-tail task opening and older-message paging.**
2. **Agent/WebUI narrow guardrail correction and honest blocked status.**

They may ship in one sealed release after independent verification. They are
not one code refactor and must not be combined into one contributor PR.

## Goals

### Task opening

- First transcript paint uses at most 30 settled visible messages.
- Warm p95 first paint is below 1 second.
- Process-cold p95 first paint is below 2 seconds.
- The exact production-scale task opens without a complete sidecar parse or
  complete lineage-message merge.
- Current in-browser live state is preserved immediately.
- A task opened in another tab/process attaches to the current live stream or
  exposes `Reconnecting to latest turn` until attachment succeeds.
- Older messages load in bounded pages only on demand.
- Normal task switching does not await sidebar cache refresh.

### Guardrail

- Distinct terminal request signatures cannot produce
  `same_tool_failure_halt` solely because they share the tool name.
- Repeating an identical failed terminal command remains blocked by the exact
  signature guardrail.
- Required-policy, authorization, mutation, and maximum-iteration safety remain
  unchanged.
- Any genuine structured guardrail stop settles as a non-success terminal
  state in WebUI.

## Non-goals

- Immediate or exact total message counts.
- Automatic background reconstruction of the full transcript.
- Seamless export of an unloaded complete transcript.
- Unlimited history navigation with constant DOM/memory use.
- Perfect active journal/state overlay in the initial HTTP response.
- The full cross-store projection and exact overlay-count snapshot.
- Failure-family normalization for every tool and error class.
- Automatic no-effect recovery after an effect-capable guardrail trigger.
- Changing provider context or model-visible history.

## Governing contracts

Settled conversation authority remains `state.db`. WebUI sidecars and run
journals remain runtime/recovery layers.

The implementation must preserve:

- canonical compression-lineage resolution;
- profile isolation;
- stable message ordering and identity;
- active/inactive/rewound message state;
- tool-call/result pairing within a returned page;
- stale browser-load vetoes;
- run ownership and SSE/replay truth;
- redaction and bounded payload handling.

Relevant references:

- `docs/CONTRACTS.md`;
- `docs/rfcs/canonical-session-resolution.md`;
- `docs/rfcs/run-state-consistency.md`;
- `docs/superpowers/specs/2026-07-16-bounded-conversation-load-design.md`;
- the two full 2026-07-28 designs named above;
- `ARCHITECTURE.md`;
- `TESTING.md`;
- `docs/UIUX-GUIDE.md`.

## Part A: lazy-tail task opening

### 1. Explicit capability negotiation

The new browser requests:

```text
GET /api/session-window
  ?session_id=<requested-id>
  &msg_limit=30
  &resolve_model=0
```

The server returns the existing task metadata plus:

```text
conversation_window:
  schema                 lazy_tail_v1
  state                  ready|reconnecting|legacy_required|stale
  source                 state_db
  visible_count          <rows in this response>
  has_older              <boolean>
  older_cursor           <opaque|null>
  newest_message_id      <opaque|null>
  active_stream_id       <opaque|null>
  reconnect_token        <opaque|null>
  exact_total_available  false
  status_reason          <stable code|null>
```

Top-level `messages` contains only the returned window. `message_count` is
omitted in `lazy_tail_v1` mode. The new browser does not synthesize or display a
total.

Compatibility:

| Client | Server | Behavior |
| --- | --- | --- |
| old | new | Old route; unchanged existing response. |
| new | old | New bounded route returns 404 without running the old full merge; browser offers an explicit legacy load. |
| new | new, eligible | Bounded lazy-tail response. |
| new | new, ineligible | Fast typed `legacy_required`; browser shows the bounded status and offers explicit legacy load. |

Unknown schemas or states fail closed. They never cause the browser to treat a
partial window as complete history. The new browser never automatically retries
an unsupported or malformed bounded request through the old `/api/session`
route.

### 2. Bounded settled-tail reader

The server:

1. validates the session/profile;
2. resolves the canonical visible tip using the existing indexed bounded
   lineage resolver;
3. reads newest-first state-db rows from that tip and, only if necessary, its
   proven lineage ancestors until at most 30 complete render units are
   collected;
4. expands the oldest boundary to include its complete tool-call/result render
   unit;
5. applies the existing visibility, ordering, and redaction rules;
6. returns chronological rows with an opaque cursor strictly older than every
   source row in the oldest returned render unit.

Hard request limits:

- lineage depth at most 128;
- requested visible rows 1 to 50 for initial open;
- raw rows examined at most 512 plus the existing 64-row tool-pair closure;
- serialized transcript payload at most 2.5 MiB including closure;
- server read-work budget 750 ms;
- one bounded retry after a lineage-generation mismatch.

The reader does not:

- call `Session.load()` for the complete session;
- parse a complete sidecar;
- build the append-only full visible transcript;
- calculate exact total rows;
- rebuild `/api/sessions`;
- repair shared state in the request.

If proof or a budget fails, return `legacy_required` or `stale` quickly. Do not
fall through to the complete merge.

### 3. Current live state and atomic handoff

The first paint has two live-state sources:

1. **Browser-local `INFLIGHT` state**
   If the same browser already owns a current live snapshot for the task, merge
   its bounded live tail immediately using the existing pane/load-generation
   guards.

2. **Server stream reattachment**
   If no current browser snapshot exists and the response advertises an active
   stream, render the settled tail plus `Reconnecting to latest turn`, then
   attach through the existing authenticated stream/replay path.

The initial HTTP response does not attempt the full active journal/state merge.
It does perform a bounded, atomic handoff:

1. resolve the active stream identity and capture a durable replay snapshot
   checkpoint before reading the settled tail;
2. read the bounded state-db tail;
3. revalidate the active stream identity after the read;
4. if identity changed, retry the bounded handoff once;
5. return an integrity-protected `reconnect_token` bound to the stream snapshot
   checkpoint.

The replay snapshot at that checkpoint contains the bounded current in-flight
turn state as of the checkpoint. Attachment then delivers every later event.
Overlap with the settled tail is allowed and removed by stable identity; a gap
is not allowed. If the stream cannot provide that snapshot-plus-later-events
guarantee, the response is `reconnecting` with a typed reason and does not
claim that the displayed tail is current.

Reattachment:

- remains bound to profile, canonical task, and active stream ID;
- starts from the exact checkpoint in `reconnect_token`;
- uses existing run-journal/SSE ordering and stable dedupe identity;
- replaces `Reconnecting` only after the checkpoint snapshot is applied and
  the live subscription acknowledges its next-event boundary;
- reports interruption/recovery truth instead of guessing completion;
- cannot commit after the user switched to another task.

If active stream identity is ambiguous, return settled tail with a typed
`reconnect_ambiguous` reason. Do not overlay uncertain output.

### 4. Lazy older-message paging

When the user reaches the top or selects `Load earlier messages`, request:

```text
GET /api/session-window
  ?session_id=<canonical-id>
  &older_cursor=<opaque>
  &msg_limit=50
```

The pagination atom is a complete render unit. An ordinary message is one unit;
a tool call and all of its paired result rows are one unit. A page cutoff may
therefore return fewer than the requested visible-message limit, but it never
splits a unit. The bounded 64-row closure is part of the oldest returned unit,
not a look-behind owned by the next page.

Each page:

- uses the same state-db lineage source and cursor version as the initial tail;
- is bounded by the same raw-row/byte/time ceilings;
- preserves tool-call/result closure;
- returns chronological rows and the next older cursor;
- advances the cursor strictly before the oldest source row in the
  closure-expanded oldest render unit;
- prepends using stable identity;
- preserves the first visible row's measured scroll anchor;
- never calculates a full offset or total;
- never mixes a legacy/full-merge page into the lazy window.

The cursor is integrity-protected and bound to:

- active profile;
- canonical task;
- target-lineage fingerprint;
- state-db source mode;
- stable per-lineage row positions.

One changed-lineage cursor may restart from one fresh newest window. A second
mismatch stops lazy paging with a visible recovery action; it does not loop.

### 5. Explicit legacy/full transcript

For sidecar-only history, a cursor-ineligible lineage, recovery diagnostics, or
complete export, the UI offers an explicit `Load complete legacy transcript`
action.

That action:

- clearly warns that a very large task may take time;
- runs the existing complete merge only after explicit user intent;
- replaces, rather than mixes with, the lazy window;
- retains stale pane/load-generation vetoes;
- is never started automatically on ordinary open or lazy paging.

### 6. Client state and rendering

Lazy-tail browser state tracks:

- canonical task ID;
- loaded pages and stable message IDs;
- older cursor;
- current active stream/reconnect status;
- load generation;
- source mode.

`renderMessages()` receives only loaded pages plus the bounded live tail.

Release-lite acceptance covers ordinary occasional history use:

- initial 30 rows;
- at least five 50-row older pages;
- stable prepend/scroll anchoring through 280 loaded rows.

Unbounded page retention and bidirectional virtual eviction are deferred. The
explicit legacy/full-transcript action remains available for exceptional deep
history needs.

### 7. Sidebar isolation

Task click starts lazy-tail loading immediately. It does not await
`/api/sessions`, title/model hydration, or a list-cache rebuild.

Sidebar refresh is deferred until after first transcript paint and uses the
existing last-known-good cache. A failed refresh leaves the opened task usable.

## Part B: narrow guardrail correction

### 1. Preserve exact-signature blocking

The request-stage `ToolCallSignature` remains authoritative. Identical failed
terminal arguments still warn/block at the configured exact-failure thresholds.

Middleware argument rewrites, parallel completion order, and timeout snapshots
retain current identity and accounting behavior.

### 2. Distinct terminal request signatures

For `terminal` only, the broad same-tool counter remains a warning signal but
does not produce `same_tool_failure_halt` when the current failed
request-stage signature has not already reached the configured exact-signature
failure threshold.

This intentionally narrow rule means:

- terminal commands with distinct request-stage arguments can continue within
  the existing global iteration budget;
- an identical command is still blocked by exact-signature policy;
- an exact signature is counted across the failure window even when other
  terminal signatures intervene;
- terminal warnings still instruct the model to inspect the first causal error
  and change strategy;
- all other tools retain their current broad same-tool behavior in
  release-lite.

This rule deliberately does not claim that syntactically different shell text
is a materially different strategy. Whitespace, comments, cwd/env changes, or
no-op variants may have different exact signatures. Terminal invocations remain
independently policy- and authorization-screened, and maximum iterations remains
the hard outer bound for a series of distinct failures. The full semantic
failure-family/aggregate-budget design is deferred.

### 3. Honest WebUI terminal state

Current Agents already return structured `guardrail` metadata for a controlled
halt. WebUI classifies, before ordinary settlement:

- `turn_exit_reason=guardrail_halt`; or
- structured `result.guardrail.action=block|halt`.

It persists and emits:

```text
terminal_state: guardrail_blocked
terminal_reason: <guardrail code>
```

The assistant explanation and last tool output remain visible. The task header
and settled anchor say `Needs recovery`, not `Done`.

Release-lite does not add automatic continuation after a genuine guardrail
block. The user can send a new turn with a different strategy. Policy,
authorization, or unrecoverable infrastructure stops therefore cannot be
silently bypassed.

No text matching determines terminal truth.

## Failure behavior

### State-db tail is unavailable

Return `legacy_required` quickly with no false empty-history claim. Offer the
explicit legacy action.

### Active stream reconnect fails

Keep the settled tail visible and show the existing interruption/recovery state.
Do not clear the transcript or mark the task complete.

### Older cursor is stale

Retry one fresh newest lazy window. Preserve the user's current pane and expose
a recovery action if the retry also changes.

### Large or malformed row

Use existing bounded payload representation or return a typed page error. Do
not expand the row/byte budget or split a required tool pair.

### Structured guardrail metadata is malformed

Fail closed to a generic non-success guardrail state. Do not render `Done` and
do not infer details from prose.

## Observability

Lazy-tail diagnostics record:

- lineage depth;
- SQL/query count;
- raw rows examined;
- visible rows and serialized bytes;
- state-db read time;
- first transcript paint;
- active reconnect start/attach/failure;
- older-page request and scroll-anchor result;
- any explicit legacy action.

Guardrail diagnostics record stable code, tool name, exact/broad count, and
WebUI terminal mapping. Commands, arguments, transcript text, paths, and tool
output are not logged.

## Verification

### Lazy-tail server tests

- A 45-segment, 42,632-row, 21,993-tool-call task returns only 30 visible rows.
- Initial request never calls the complete session/sidecar merge.
- An old server returns 404 for the bounded route without entering the old
  `/api/session` merge, and the browser does not retry automatically.
- Initial and older pages obey row, byte, lineage, and wall-time budgets.
- Page boundaries preserve complete render units, tool pairs, and stable
  ordering without skipped or duplicated source rows.
- Events emitted before, during, and after the settled-tail read are covered by
  either the checkpoint snapshot or later replay, with no handoff gap.
- Profile mismatch, inactive/rewound rows, lineage cycles, and stale cursors
  fail closed.
- Sidecar-only/ineligible history returns `legacy_required` without doing the
  legacy work.

### Browser tests

- Lazy-tail first paint commits only for the current load generation.
- Browser `INFLIGHT` tail wins only for the matching task/stream.
- Remote active task renders `Reconnecting` and attaches to the advertised
  stream.
- The browser does not claim current state until it has applied the reconnect
  checkpoint snapshot and crossed the acknowledged live boundary.
- Five older pages prepend without duplication or scroll jump.
- No count is displayed or used as an offset.
- Sidebar refresh begins only after first transcript paint.
- Explicit legacy load replaces the lazy source instead of mixing rows.

### Guardrail tests

- Four different failing terminal signatures do not produce
  `same_tool_failure_halt`.
- Three identical failing terminal signatures still hit the exact block under
  the live configuration.
- Alternating an identical signature with other terminal calls does not reset
  its exact-failure count.
- Whitespace/comment/no-op variants may continue as distinct signatures but
  remain bounded by global maximum iterations.
- Other tool same-tool hard stops remain unchanged.
- Global max-iteration behavior remains unchanged.
- Structured legacy and new guardrail results persist/replay
  `guardrail_blocked`.
- A guardrail-blocked task never renders `Done`.

### Live acceptance

Use the exact production task that originally took 24.55 seconds:

1. Process-cold open.
2. Verify newest settled message appears below 2 seconds.
3. If active, verify live state attaches or clearly remains reconnecting.
4. Load at least five older pages and verify ordering/scroll anchors.
5. Confirm no normal request parses the complete sidecar or merges all 42,632
   rows.
6. Force the recorded different-terminal-failure sequence and verify it
   continues.
7. Force an identical failure and verify `Needs recovery`, not `Done`.

Performance acceptance:

- warm first transcript paint p95 below 1 second over 40 opens;
- process-cold first transcript paint p95 below 2 seconds over 20 restarts;
- no lazy-tail request above 5 seconds;
- page work independent of unrelated-session volume.

## Rollout and rollback

Default-off rollout gates:

- server lazy-tail reader;
- browser lazy-tail adoption;
- distinct-terminal-signature guardrail rule.

The WebUI structured `guardrail_blocked` mapping is backward-compatible
correctness handling, not an experiment gate. Once shipped, it remains enabled
through feature rollback so an Agent guardrail result cannot regress to
`Done`.

Rollout:

1. Ship server reader and tests with browser gate off.
2. Shadow semantic page output against the current state-db-visible oracle.
3. Enable browser lazy-tail for the target production task.
4. Ship and verify the unconditional blocked-state mapping, then enable the
   terminal signature rule.
5. Run the live acceptance sequence.

Rollback disables the relevant lazy-tail or terminal-signature gate. It does
not disable the structured blocked-state mapping. No data migration or
canonical-state rollback is required.

## Deferred hardened work

The following remains in the full designs:

- exact active overlay counts and full cross-store projection tokens beyond
  the minimal bounded reconnect handoff;
- proactive reconciliation receipts for every eligible conversation;
- unlimited cursor history with virtual eviction;
- server-backed full-history export/search ergonomics;
- failure-family normalization and aggregate strategy budgets;
- safe diagnostic recovery epochs after effect-capable triggers;
- server-authorized one-click recovery actions.

Release-lite must not create incompatible state that prevents those later
stages.

## Estimated effort

- lazy-tail server reader and route: 1.5 to 2 days;
- browser first paint, reconnect, and paging: 1.5 to 2 days;
- narrow Agent guardrail and WebUI terminal mapping: 1 day;
- regression, performance, and live release verification: 1 to 1.5 days.

Total: 5 to 7 focused engineering days, including the timed live acceptance
matrix and cross-component handoff verification.

## Release-note wording

Long-running tasks now open on the latest conversation immediately and load
older messages only when requested, instead of rebuilding the entire transcript
before first paint. Distinct terminal recovery commands no longer trigger a
false broad same-tool stop, and genuine guardrail blocks now appear as
`Needs recovery` rather than `Done`.
