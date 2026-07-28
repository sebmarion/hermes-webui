# Active Bounded Conversation Open Design

**Date:** 2026-07-28
**Status:** Approved for specification
**Owner:** Hermes WebUI
**Related design:** `2026-07-16-bounded-conversation-load-design.md`

## Summary

Opening a long, still-running conversation must not reconstruct its complete
compression lineage, parse a large sidecar, rebuild the sidebar cache, or mount
the full transcript before the user can see the newest messages.

This design extends the existing bounded-conversation contract to active
runtime owners. The initial open reads the newest 30 settled visible rows from
the indexed state-db cursor reader, overlays only the proven current live turn,
and renders that bounded window. Older history remains cursor-paged. Sidebar
cache maintenance is removed from the open request's critical path.

The target is:

- warm active open p95 below 1 second;
- process-cold active open p95 below 2 seconds;
- no active-open request above 5 seconds in the acceptance sweep;
- work proportional to compression-lineage depth plus the requested page, not
  total transcript size.

## Relationship to the existing bounded-load design

The 2026-07-16 design remains authoritative for:

- canonical conversation resolution;
- the six-field `message_page` wire shape;
- opaque cursor validation and restart;
- reconciliation receipts;
- exact display-count semantics;
- tool-pair closure;
- todo/view-state projection;
- redaction, compatibility, rollout, and rollback.

This document narrows one unshipped gap: the current bounded route rejects a
conversation with an active runtime owner and falls back to the legacy complete
merge. That exclusion makes the largest, most important live conversations the
slowest ones to open.

No second conversation authority or alternative transcript format is
introduced.

The existing six-field `message_page` remains byte-for-byte compatible.
Active-only gap, count-certainty, and not-ready metadata is exposed through a
separately negotiated top-level `active_open` envelope described below. This is
a narrow amendment to the existing rule that `message_count` is always exact:
when the new active capability is negotiated and exact count cannot be proven,
`message_count` is omitted and the versioned envelope explicitly reports an
unavailable count. Old clients never receive that response shape.

## Observed production failure

On 2026-07-28, opening conversation `20260728_080319_c4d295` produced the
following read-only evidence:

- 45 compression-lineage segments;
- 42,632 raw session-message rows;
- 21,993 tool calls;
- active lineage depth 44;
- current physical tip with only 229 local messages, while the visible merged
  conversation contained roughly 39,247 rows;
- a 5.25 GiB shared `state.db`;
- one `/api/session` request taking 24.55 seconds.

The request spent approximately:

- 15.74 seconds in `get_session()` and active-sidecar/state reconciliation;
- 6.99 seconds compacting and merging the visible transcript;
- 1.73 seconds redacting the resulting payload.

A concurrent `/api/sessions` cache lookup spent another 5.97 seconds rebuilding
sidebar state. The WebUI process saturated a CPU core and the browser could not
answer a DOM inspection request within ten seconds.

The existing cursor and browser-adoption gates were disabled, transcript
virtualization was disabled, and the active-owner eligibility check forced the
legacy path.

## Goals

1. Make initial active-conversation open bounded by lineage depth plus a
   30-visible-row page.
2. Preserve the exact current live turn without scanning or serializing the
   settled history behind it.
3. Keep append-only ordering, tool pairing, compaction lineage, replay, and
   recovery semantics unchanged.
4. Preserve exact display counts and derived todo state through validated
   projections and reconciliation receipts.
5. Keep at most a small bounded transcript window mounted in the DOM during
   initial open.
6. Prevent sidebar cache reconstruction from competing with or delaying a
   conversation open.
7. Retain explicit fail-safe gates and rollback to the legacy reader.

## Non-goals

- Changing provider context assembly or model-visible history.
- Deleting, truncating, summarizing, or rewriting historical messages.
- Making WebUI sidecars canonical.
- Enabling cursor mode by flipping environment flags without proof.
- Running a startup migration over every session or sidecar.
- Loading every historical row merely to make browser find or transcript
  export behave as if the entire conversation were already mounted.
- Combining this work with the guardrail-recovery implementation in one PR.

## Contract routing

This change touches:

- shared session resolution and canonical message projection;
- active run ownership and run-journal replay;
- runtime/settled message reconciliation;
- cursor paging and sidebar metadata;
- transcript rendering and scroll anchoring.

The governing references are:

- `docs/CONTRACTS.md`;
- `docs/rfcs/canonical-session-resolution.md`;
- `docs/rfcs/run-state-consistency.md`;
- `docs/superpowers/specs/2026-07-16-bounded-conversation-load-design.md`;
- `ARCHITECTURE.md`;
- `TESTING.md`;
- `docs/UIUX-GUIDE.md`.

## Required invariants

### Canonical and settled state

- `state.db` remains canonical for settled conversation history.
- The bounded reader uses the already-resolved compression member IDs exactly
  once per request.
- A page never mixes cursor and legacy ordering modes.
- Active and inactive/rewound rows retain the existing state policy.
- Tool-call/result pairs are never split beyond the existing bounded closure
  allowance.
- Cursor and reconciliation state contains no transcript content.

### Active overlay

- Exactly one proven active owner may contribute an overlay.
- Overlay identity is bound to the canonical conversation, physical tip,
  `stream_id`, `run_id`, and journal sequence.
- Only events after the settled reconciliation watermark may be overlaid.
- Stable message/event identity deduplicates rows already present in the
  settled page.
- A stale, foreign-profile, split-brain, or ambiguous owner contributes no
  overlay.
- The overlay has independent row and byte ceilings.
- Provisional live todo state never overwrites a newer settled todo projection.

### Open-path bounds

- The initial request does not parse a complete multi-megabyte sidecar.
- The initial request does not iterate over every historical message.
- The browser does not scan or mount historical rows it has not requested.
- `/api/sessions` cache repair/rebuild is never awaited by conversation open.
- Correctness does not depend on a warm process cache.
- Adopted active open has hard ceilings of 128 lineage members, 512 ms of
  server-side read work, the existing bounded-reader SQL/query budget, and the
  settled/overlay row and byte budgets below.
- Once browser adoption is enabled, missing proof returns bounded typed
  not-ready state; it never invokes the complete legacy merge in the click
  request.

### Presentation

- The newest settled page and current live turn are visible at first paint.
- Error, approval, clarification, and recovery events in the live turn remain
  prominent.
- Older history prepends without losing the user's scroll anchor.
- Initial open never shows a blank transcript while a valid bounded page is
  available.

## Chosen architecture

### 1. Active-aware cursor eligibility

Replace the current `runtime owner must be absent` eligibility rule with:

```text
bounded settled page is eligible
AND reconciliation receipt is current
AND (
  no runtime owner exists
  OR one active owner is proven and overlay-capable
)
```

The server still returns the existing negotiated `message_page` structure. An
active owner does not create another paging mode and does not change cursor
ordering.

An owner is proven only when all of these agree:

- canonical conversation and requested profile;
- physical lineage tip;
- active stream/run registry;
- sidecar active-stream metadata;
- run-journal stream identity;
- receipt settled watermark.

If an owner is ambiguous, the server returns the bounded settled page without
overlay and includes a typed reattach/recovery state. It does not guess which
run wins.

The browser negotiates the extension explicitly:

```text
GET /api/session
  ?session_id=<requested-id>
  &messages=1
  &msg_limit=30
  &message_paging=cursor_v1
  &active_open=active_overlay_v1
```

The existing `message_page` keeps exactly:

```text
mode
before_cursor
has_more
visible_count
raw_rows_examined
serialized_bytes
```

New metadata lives only in:

```text
active_open:
  schema                    active_overlay_v1
  state                     ready|truncated|snapshot_stale|not_ready
  lower_journal_sequence
  upper_journal_sequence
  overlay_gap_cursor
  display_count:
    value                   <integer|null>
    exact                   <boolean>
  recoverable_reason
```

When `active_open.display_count.exact=true`, top-level `message_count` remains
the same exact integer contract. When false, top-level `message_count` is
omitted and the new browser must not synthesize one.

Compatibility is explicit:

| Client | Server | Behavior |
| --- | --- | --- |
| old | new | No `active_open` negotiation; unchanged legacy/cursor-v1 response with integer exact `message_count`. |
| new | old | Server ignores the unknown parameter; absence of `active_open.schema` makes the browser use the existing legacy/cursor-v1 flow and its integer count. |
| new | new, active proof ready | Six-field cursor-v1 page plus validated `active_overlay_v1` envelope. |
| new | new, active proof unavailable | Six-field bounded page plus typed `not_ready`; no legacy merge in the adopted request and no false exact count. |

Unknown envelope schemas/states fail closed: the new browser discards active
overlay/count metadata, keeps only a proven settled page if available, and
requests reattach. It never assumes no gap or an exact count.

### 2. Settled-page and active-overlay seam

The WebUI cannot take one physical transaction across SQLite, the in-process
owner registry, sidecar metadata, receipt storage, and run-journal shards.
Instead it uses one optimistic cross-store snapshot token:

```text
ActiveOpenSnapshot:
  profile_id
  canonical_session_id
  target_lineage_fingerprint
  projection_generation
  receipt_generation
  settled_message_watermark
  settled_display_count_generation
  sidecar_generation
  owner_lease_generation
  stream_id
  run_id
  journal_shard_generation
  journal_high_water_sequence
```

The server:

1. Resolves canonical lineage within the depth/query/time ceiling.
2. Captures the complete token from current receipt, owner, sidecar, and
   journal metadata.
3. Reads the newest 30 settled visible rows at or below the token's settled
   watermark.
4. Reads one contiguous newest live interval ending at the captured journal
   high-water sequence.
5. Converts those events through the existing visible-message normalization.
6. Deduplicates using the shared identity contract below.
7. Re-reads every token field after the data reads.
8. Commits the response only if the two tokens are byte-for-byte equal.

One mismatch may retry once while the total request remains inside the 512 ms
server-read budget. A second mismatch returns the last proven bounded settled
page with typed `active_snapshot_stale` state and schedules reattach; it never
falls through to a complete merge. SSE/journal replay begins at exactly the
committed token's `journal_high_water_sequence + 1`.

Events arriving after that sequence are delivered only by normal SSE/replay.
They are not raced into the initial response.

Journal events and settled rows share a collision-resistant host identity:

- the runtime assigns an opaque `event_id` when the journal event is created;
- settlement persists that same `event_id` as the state-db platform-message
  identity;
- identity is scoped by profile, canonical conversation, and run;
- legacy rows without that identity are ineligible for active overlay until
  targeted reconciliation creates a durable mapping;
- content hashing is never used as the primary dedupe key.

The run journal maintains an indexed prefix projection by sequence containing
renderable-row delta, serialized-byte delta, and settlement/dedupe state. It is
updated atomically with journal append. This permits exact overlay counts and a
newest contiguous interval without scanning every event.

The default overlay limits are:

- 128 renderable rows;
- 1,024 raw journal events;
- 2 MiB serialized after normal bounded-payload handling.

Exceeding an overlay limit returns a valid bounded tail and a typed
`active_overlay_truncated` state. The response includes the returned interval's
inclusive lower/upper journal sequences and an opaque `overlay_gap_cursor` for
the immediately preceding interval. The latest live rows are never hidden, and
the missing middle is represented by an explicit gap marker rather than
silently skipped.

`message_count` is exact only when the receipt count plus the journal prefix
projection proves the complete overlay delta after dedupe. The response carries
`active_open.display_count.exact=true`. If that projection is missing, stale,
or truncated without an exact prefix total, top-level `message_count` is
omitted, `active_open.display_count={value:null, exact:false}`, and state is
`not_ready`; the browser must not use a count for completion, unread, header,
or paging logic.
Targeted reconciliation repairs the projection asynchronously. A truncated
rendered interval may still report an exact total only when the independent
prefix projection proves it.

### 3. Receipt readiness and existing long conversations

Browser adoption must not begin until the requested conversation has a valid
reconciliation receipt, shared journal/state identity mapping, and projected
display count.

Receipt writes ship before active browser adoption and are produced:

- after normal settlement/reconciliation;
- after accepted sidecar/truncation changes;
- by a targeted background reconciliation for the currently active or
  explicitly requested conversation.

The targeted reconciler may pay the existing complete-merge cost once, but it
runs outside the click/open request and writes the identity mapping, count
projection, and receipt last. It is single-conversation, deduplicated,
cancel-safe, limited to one concurrent worker, and scheduled through a bounded
CPU/I/O queue that yields to active turns and open requests.

There is no global startup scan. Release enablement waits until the known
power-user active conversation has a current receipt. A missing receipt before
adoption retains the exact legacy behavior; after adoption, a receipt mismatch
returns `bounded_open_not_ready` with the last proven bounded page, marks count
non-exact, schedules targeted reconciliation, and records a structured reason.
It never runs the legacy complete merge in the adopted click path.

### 4. Client page-backed transcript state

The browser stores separate state for:

- loaded settled message pages;
- active live-turn rows;
- paging cursor and restart generation;
- exact projected display count;
- runtime/reattach state.

`renderMessages()` receives only the loaded, deduplicated window. It must not
scan an implicit complete-history array during initial open.

Operations that require more than the loaded window use explicit paths:

- older-history navigation fetches another cursor page;
- transcript export uses a server streaming/export endpoint or an explicit
  bounded materialization flow;
- retry, undo, edit, and fork use stable server message identity, not a
  browser-computed full-history offset;
- browser find searches loaded rows and offers explicit server-backed history
  search for unloaded rows.

No ordinary task open downloads the full transcript to preserve a legacy
client-side convenience.

### 5. DOM mount budget

Initial open mounts the newest 30 settled rows plus the bounded live overlay.

When loaded visible rows exceed 80, the transcript uses the existing virtual
window regardless of the historical user preference that currently forces a
full DOM. The preference may control eager history expansion and an explicit
diagnostic `render loaded rows without virtualization` action, but it cannot
remove the normal safety mount budget.

This is not a return to the prior default-on implementation without fixes.
Acceptance requires stable row measurement and scroll anchoring for:

- tall tool outputs;
- expanded/collapsed activity groups;
- images and delayed media sizing;
- prepend of older pages;
- session switch and return;
- active streaming at the bottom.

The browser must never mount unloaded history.

### 6. Sidebar isolation

Conversation open and sidebar refresh are independent:

- clicking a task starts `/api/session` immediately;
- it never waits for `/api/sessions`;
- the list endpoint returns the last-known-good bounded cache promptly;
- an expired/missing list cache schedules one deduplicated background rebuild;
- a rebuild does not parse complete sidecars in the request thread;
- active open receives scheduling/CPU priority over background list work.

Session-list invalidation continues to be driven by metadata/projection
generation. It does not become a reason to rebuild the current transcript.

## Data flows

### Initial active open

1. Browser increments its load generation and requests one negotiated
   `cursor_v1` page with `msg_limit=30`.
2. Server resolves the canonical lineage once.
3. Server validates cursor capability, receipt, count projection, and the
   current active-owner proof.
4. Server reads the bounded settled page.
5. Server overlays only the captured current-run tail.
6. Server attaches projected todo/count metadata and redacts the bounded
   response.
7. Browser validates `message_page.mode=cursor_v1`, commits only if the load
   generation still owns the pane, renders the bounded window, and attaches to
   SSE/journal replay.
8. Sidebar metadata refresh continues independently.

### Older page

The existing cursor contract applies. The browser prepends one page,
deduplicates stable IDs, preserves its measured anchor, and restarts once on a
typed stale cursor.

### Compression while open

The existing target-lineage fingerprint invalidates the old cursor. The browser
adopts one fresh initial page and retains live-owner/pane checks. Compression
does not trigger a full transcript reconstruction.

### Active owner disappears during open

The captured response remains valid through its journal high-water sequence.
SSE/replay then emits settlement, interruption, or recovery truth. The browser
does not infer completion from connection closure alone.

## Failure behavior

### Receipt missing or stale

Before browser adoption, the named single-conversation legacy fallback may run
as an explicit readiness gate and schedule targeted reconciliation. After
adoption, return `bounded_open_not_ready` with the last proven bounded page,
`message_count_exact=false`, and the exact reason. Never mix cursor-loaded rows
with a legacy tail and never run the complete merge in the request.

### Owner proof mismatch

Return the settled bounded page plus typed reattach/recovery state. Do not
overlay an uncertain run and do not scan complete history to hide the
ambiguity.

### Overlay gap or limit

Return the newest proven contiguous interval, its lower/upper sequence bounds,
and an opaque cursor for the explicit preceding gap. Exact count requires the
independent prefix projection; otherwise mark it unavailable. No gap is
silently skipped.

### Lineage or wall-clock budget exceeded

Return `bounded_open_not_ready` with the last directly proven bounded page and
schedule targeted reconciliation. Do not continue walking the lineage, choose
an arbitrary child, or invoke the legacy complete merge.

### Cursor restart loop

Allow one fresh initial request. A second mismatch stops paging and exposes a
recoverable error; it does not loop or fall through to mixed coordinates.

### Virtual measurement failure

Force one bounded re-measure/re-render of loaded rows while preserving the
anchor identity. Never solve a blank viewport by mounting the complete
transcript.

### Sidebar rebuild failure

Keep the last-known-good list, record the failure, and leave the opened
conversation usable. Sidebar diagnostics do not become chat errors.

## Observability

Active open diagnostics record:

- canonical-resolution time and lineage depth;
- receipt/count-projection generation;
- settled raw rows examined and visible rows returned;
- active owner proof result;
- journal events examined and overlay rows/bytes;
- deduplicated row count;
- redaction/serialization time and bytes;
- browser render start, first transcript paint, mounted row count, and
  virtualization state;
- concurrent sidebar cache state and whether rebuild was scheduled.

Diagnostics never record transcript text, tool arguments, credentials, or
workspace contents.

## Verification

### Server regression tests

- A 45-segment active lineage with 42,000+ rows reads only the bounded page and
  overlay.
- Active owner proof accepts one consistent owner and rejects split-brain,
  stale, cross-profile, and wrong-tip variants.
- Journal/state overlap deduplicates exactly.
- Events after the captured high-water sequence arrive only through replay.
- Cross-store token mutation causes one bounded retry and then
  `active_snapshot_stale`.
- Exact count equals the legacy oracle for settled plus active-overlay cases.
- Truncated overlay with no exact prefix projection reports count unavailable.
- Overlay gap bounds/cursors are contiguous and cannot hide a middle interval.
- Todo overlay respects durable watermarks and explicit empty tombstones.
- Missing receipt never emits a cursor.
- Adopted missing-receipt and over-depth cases never execute the complete merge.
- Mixed old/new client/server combinations never expose nullable legacy counts,
  silently ignore an overlay gap, or treat an unknown active state as ready.
- Query plans and row budgets match the 2026-07-16 design.

### Browser regression tests

- One negotiated request opens the active task.
- Initial `S` state and DOM contain only the bounded window.
- More than 80 loaded rows use the virtual mount budget.
- Prepending older pages preserves the anchor for tall tool rows and delayed
  media.
- Switching tasks vetoes a stale response and stale active overlay.
- Export/find/retry/undo/edit/fork do not assume all history is resident.
- Sidebar rebuild failure does not delay or blank conversation open.

### Performance gate

Use the existing deterministic bounded-load fixture plus:

- 45 physical compression segments;
- 42,632 raw rows;
- 21,993 tool calls;
- one active run with 100 journal events and overlapping settled rows;
- one current 100 MiB sidecar;
- concurrent cold sidebar-cache rebuild pressure.

Measure:

- 40 warm active opens;
- 20 process-cold active opens;
- 20 concurrency-4 rounds with sidebar rebuild scheduled.

Acceptance:

- warm p95 below 1 second;
- process-cold p95 below 2 seconds;
- no request above 5 seconds;
- no complete sidecar parse on an eligible active open;
- raw-row/query counts independent of unrelated-session volume;
- browser first transcript paint below 1.5 seconds warm;
- mounted rows remain within the configured virtual window.

### Live acceptance

Before release promotion:

1. Capture the current slow-task baseline.
2. Build/validate its reconciliation receipt outside the open request.
3. Open the exact task after a process-cold restart.
4. Verify first paint, latest live content, todo/count truth, SSE attachment,
   older-page prepend, and scroll stability.
5. Confirm `/api/sessions` rebuild cannot push the open over 5 seconds.
6. Disable the browser gate and prove immediate legacy rollback.

## Delivery slices

### PR A: active server overlay and proof

- Extend active-owner eligibility.
- Add captured journal overlay and dedupe.
- Add targeted receipt readiness.
- Add server fixtures, diagnostics, and shadow oracle.

No browser adoption.

### PR B: page-backed browser and mount budget

- Adopt active cursor mode.
- Separate loaded-page/live-overlay state.
- Route full-history operations explicitly.
- Enforce the >80-row mount budget.
- Add desktop, narrow, and mobile evidence.

### PR C: sidebar critical-path isolation

- Make list rebuild last-known-good and background-only.
- Add contention and failure tests.

Each PR has an independent gate and rollback. They may ship in one release only
after the combined active-open acceptance matrix is green.

## Alternatives rejected

### Flip the existing cursor environment flags

Rejected. The current active-owner exclusion would still force the legacy
merge, and bypassing receipt/evidence gates would weaken correctness.

### Cache the complete merged transcript

Rejected. It helps only warm loads and preserves cold-start, invalidation, and
memory costs proportional to transcript size.

### Enable full transcript virtualization only

Rejected as the primary fix. It reduces DOM work but leaves the 15.7-second
server load, 7-second merge, and large serialization cost.

### Preload every conversation at startup

Rejected. It moves the same unbounded work to process start and makes unrelated
history affect availability.

## Release-note wording

Opening very long, still-running tasks is now bounded and responsive: Hermes
loads the newest conversation page and current live turn first, pages older
history on demand, keeps large transcripts within a safe DOM mount budget, and
no longer lets sidebar cache rebuilding block the task you clicked.
