# Bounded Conversation Load Design

## Status

Approved for implementation planning on 2026-07-16.

This document defines the permanent performance boundary for opening a
conversation. It does not authorize a new persistence authority, a bulk
sidecar migration, or a change to canonical session semantics.

## Goal

Make conversation-open work proportional to the requested conversation, not
to the total number of sessions or the total transcript length.

For a schema that passes the indexed-paging capability gate, the steady-state
complexity target is:

```text
O(valid compression-lineage depth + bounded raw rows examined)
```

Adding unrelated sessions, archived rows, or older messages must not make a
normal conversation open slower. Legacy compatibility fallbacks are explicitly
identified below and are not allowed to masquerade as the bounded path.

## Evidence and current failure

The live authenticated path reproduced the problem on 2026-07-16:

- `GET /api/session?...&messages=0` took 11.05 seconds;
- 10.95 seconds elapsed before the actual `get_session` stage;
- the actual session lookup took 48.8 milliseconds;
- compact/merge work in that metadata response took 57.0 milliseconds;
- the subsequent 30-message request took 1.97 seconds;
- an overlapping sidebar projection took 19.1 seconds.

The active database snapshot had 2,566 physical sessions, including 2,020
archived rows. `state.db` was 3.42 GiB and WebUI session sidecars occupied
about 5.1 GiB. These sizes amplify cold reads, but the root bug is algorithmic.

`resolve_shared_session_id()` currently claims to perform a bounded lookup but
iterates `read_shared_session_rows(..., include_archived=True)`. That shared
projection calls `read_importable_agent_session_rows(limit=None)`, so opening
one conversation rebuilds the complete canonical list.

The browser then pays for resolution twice: once for metadata and once for the
initial message tail. `msg_limit` limits the serialized response but does not
guarantee bounded physical work: `Session.load()` still parses the complete
JSON sidecar and current todo-state derivation scans the full merged transcript.

## Contract routing

Task type: runtime performance and canonical-session routing.

Touched state layers:

- canonical shared conversation metadata and lineage in `state.db`;
- settled message history in `state.db.messages`;
- WebUI sidecars and run journals as runtime/recovery overlays;
- browser session-load and older-message pagination state.

Relevant contracts:

- `AGENTS.md`;
- `CONTRIBUTING.md`;
- `docs/CONTRACTS.md`;
- `docs/rfcs/canonical-session-resolution.md`;
- `docs/rfcs/webui-run-state-consistency-contract.md`;
- `ARCHITECTURE.md`;
- `TESTING.md`.

This is an implementation and performance-preservation change, not an
intentional contract change.

## Required invariants

The implementation must preserve all of these rules:

1. `state.db` remains the durable shared conversation authority. WebUI JSON
   sidecars remain runtime/recovery, presentation, and legacy-archive data.
2. Ordinary navigation resolves a valid compression snapshot to the canonical
   visible tip. A directly valid non-snapshot ID normally remains stable.
3. Explicit historical inspection can select a physical snapshot without
   changing normal navigation semantics.
4. Parent links alone do not prove continuation. Source must match, and forks,
   delegates, tools, and cross-source children remain distinct conversations.
5. Sidebar collapse and detail loading choose the same visible representative.
6. URL routes, query aliases, local storage, sidebar clicks, direct opens, and
   compatibility endpoints use the same resolver.
7. Missing-session recovery remains separate from present-but-archived lineage
   resolution.
8. Shared title, workspace, archive, pin, source, and lineage metadata cannot be
   overridden by stale sidecars.
9. Runtime activity is an overlay. It cannot alter lineage, counts, ordering,
   titles, archive state, or pins.
10. Reattach, replay, todo state, tool-call pairing, and visible transcript
    ordering remain idempotent and coherent.
11. Legacy sidecar-only history remains lazy. No implementation stage may bulk
    import, rewrite, or delete historical sidecars.

## Chosen architecture

### 1. One bounded shared-session resolution primitive

Introduce one internal primitive in the shared state bridge. A conceptual
result shape is:

```text
SharedSessionResolution
  requested_id
  canonical_id
  root_id
  tip_id
  member_ids
  canonical_row
  global_projection_generation_hint
  lineage_fingerprint
  mode                 navigation | history
  status               found | missing | degraded | ambiguous
```

The exact Python representation is an implementation choice. Callers must not
reconstruct lineage independently after receiving it.

The resolver must:

1. fetch the requested row through the `sessions` primary key;
2. walk ancestors through primary-key lookups;
3. walk descendants through `idx_sessions_parent`;
4. accept only edges approved by the existing continuation guard with
   `compression_only=True`;
5. detect cycles and cap the walk at 256 hops;
6. choose the same deterministic visible tip as the shared list projection;
7. return the canonical row, lineage members, and a deterministic target-lineage
   fingerprint from the same read snapshot;
8. return the requested ID unchanged for missing/unsupported schemas so
   existing 404 and degradation behavior remains available.

The existing bounded traversal in `read_session_lineage_metadata()` is the
implementation precedent. The new primitive should extract or share its
indexed traversal rather than add a second lineage algorithm.

No single-session path may call any of these operations:

- `read_shared_session_rows()`;
- `all_sessions()`;
- session-list cache construction;
- background reconciliation or sidecar discovery;
- an unscoped `SELECT` over all `sessions` or `messages` rows.

`read_shared_session_rows()` remains valid for full list/export callers. The
design separates collection projection from entity lookup instead of deleting
the collection API.

### 2. Reuse one resolution throughout a request

The legacy browser detail route and the compatibility REST detail/message
routes must consume the resolved object directly.

The resolver result supplies:

- canonical ID and aliases;
- canonical metadata row;
- member IDs for message paging;
- root/tip/segment metadata;
- a target-lineage fingerprint plus the global generation as an optional cache
  invalidation hint and diagnostic.

Neither metadata hydration nor message hydration may resolve the same requested
ID again within one request. The compatibility detail builder must not resolve
an ID and then rebuild the complete shared projection to find the resulting
row.

Cross-request caching is optional. If used, the key is the active profile,
requested ID, navigation mode, and target-lineage fingerprint. Agent-owned
`session_projection_meta` generation may invalidate that cache as a conservative
hint, but a global generation change alone does not invalidate a receipt or
cursor. Correctness and acceptable cold performance must not depend on a cache
hit. Old schemas without a generation marker use the indexed resolver, not a
long TTL over potentially stale lineage.

### 3. One negotiated initial-view request from the browser

The browser should open a settled conversation with one `/api/session` request
that explicitly negotiates cursor paging and includes the initial message page:

```text
GET /api/session
  ?session_id=<requested-id>
  &messages=1
  &msg_limit=30
  &message_paging=cursor_v1
  &resolve_model=0
```

The server keeps the current top-level response shape for compatibility and
adds stable paging metadata:

```text
requested_session_id
canonical_session_id
message_page:
  mode                 cursor_v1
  before_cursor
  has_more
  visible_count
  raw_rows_examined
  serialized_bytes
```

`message_paging=cursor_v1` is the capability request. A subsequent page uses
`msg_cursor=<opaque-value>` and repeats `message_paging=cursor_v1`;
`msg_cursor` and legacy `msg_before` are mutually exclusive. The server rejects
a request containing both rather than guessing which coordinate system wins.

Cursor mode and legacy mode have intentionally different count contracts:

- `message_count` keeps the exact current append-only merged display-count
  semantics used by completion, unread, header, and refresh logic. For a
  settled cursor response it comes from the validated reconciliation receipt,
  not a compression-tip row. A proven active overlay applies only its bounded,
  deduplicated display-count delta using the same renderability predicate. If
  the exact count or delta cannot be proven, the initial request selects legacy
  mode and a later cursor request returns `cursor_restart_required` with no
  messages. The browser never uses `message_count` as a page offset.
- `message_page.visible_count` is the number of renderable rows in this page.
- `message_page.has_more` means the bounded reader has a valid older raw-row
  boundary, not that it counted all older renderable messages.
- cursor responses do not expose `_messages_offset` or
  `_messages_truncated`; the negotiated browser uses `message_page` only.
- requests without `message_paging=cursor_v1` retain the current exact legacy
  response, including numeric `msg_before`, `message_count`,
  `_messages_offset`, and `_messages_truncated` semantics. That path may still
  perform the current one-conversation full merge and is not covered by the
  bounded-path latency guarantee.

Compatibility and rollback are fail-safe:

| Client | Server | Behavior |
| --- | --- | --- |
| old | new | No negotiation parameter; server returns the unchanged legacy response. |
| new | old | Old server ignores the unknown negotiation parameter; absence of `message_page.mode=cursor_v1` makes the browser retain its existing numeric paging flow for that load. |
| new | new, cursor disabled/degraded | Server returns `message_page.mode=legacy` without a cursor and preserves the legacy coordinates; browser uses those coordinates for that load. |
| new | new, cursor enabled | Browser commits cursor state only after validating `message_page.mode=cursor_v1`. |

Disabling browser adoption therefore does not require reverting the server,
and disabling the server cursor gate returns all clients to the legacy path.

The browser continues deferred model/provider resolution after first paint.
Existing load-generation and active-pane ownership guards remain in force so a
slow response from a previous click cannot replace a newer conversation.

The current metadata-only form remains available to compatibility callers and
background polling, but the interactive browser open does not issue metadata
and messages sequentially.

### 4. Cursor-paged, physically bounded message reads

Add a state-db message-page reader that accepts a
`SharedSessionResolution`. It reads only active messages belonging to the
resolved valid compression members.

The cursor is opaque, integrity-protected by the server, bound to the active
profile, and versioned. It records the target-lineage fingerprint, global
projection generation only as a hint, source mode, reconciliation-receipt
generation, and enough per-lineage raw-row ordering state to continue indexed
reads without exposing an array offset contract. The implementation may use a
stable `(timestamp, id)` key or per-segment positions, but it must prove the
selected query plan uses the available session/message indexes.

Before advertising `cursor_v1`, the reader checks an explicit capability set:

- `sessions.id` supports direct lookup and the parent lookup index is usable;
- `messages.session_id` and an index capable of ordered per-session reads are
  present;
- each message has a stable tie-break key and a normalizable ordering value;
- the schema can distinguish active from compacted/inactive rows where that
  state is present.

On an initial request, missing columns, unusable timestamp types, or a missing
index select the explicit legacy-mode response. On a later cursor request, the
same failure returns `cursor_restart_required` with no messages. The WebUI read
path does not create Agent indexes or migrate the schema.

The default bounded-reader limits are normative:

- requested visible page size: 1 to 100 rows;
- base raw-row budget: `max(256, min(2048, 8 * requested_page_size))`;
- serialized response budget: 2 MiB after redaction and normal bounded-payload
  handling;
- tool-pair closure extension: at most 64 additional raw rows and 512 KiB.

The cursor advances past the last raw row examined, including hidden or
inactive rows, so repeated pages cannot rescan an arbitrarily large hidden
region. Reaching a raw-row or byte budget returns a valid short page with
`has_more=true`. A single oversized payload uses the existing bounded payload
representation; it does not expand the page budget.

If a tool call/result pair crosses the base boundary, the reader may spend only
the closure extension above. If the required partner is still not found on an
initial request, that request selects the exact one-conversation legacy merge,
emits no cursor, and records `tool_pair_outside_bound`. If this occurs after a
cursor was issued, the server returns `cursor_restart_required` with no
messages. It never strands half a required pair, extends the indexed scan
without limit, or changes source modes within a paging sequence.

Each page must:

- read newest-first in bounded raw batches;
- stop after enough renderable rows have been collected;
- preserve chronological order in the returned page;
- keep tool calls and their tool results together when the boundary crosses a
  pair;
- exclude inactive compacted rows unless the caller explicitly requests an
  audit/recovery view;
- preserve exact message identity needed by append-only deduplication;
- return `has_more` without counting the full transcript;
- reject malformed, oversized, cross-profile, or wrong-version cursors.

The existing numeric `msg_before` parameter remains accepted for older clients
during migration. The current browser moves to the opaque cursor and must not
compute exact full-history offsets.

### 5. Runtime, sidecar, and recovery overlays

Settled history comes from `state.db`. Runtime-only content is then overlaid
from bounded sources:

- an already-loaded active in-memory `Session`;
- the owning stream/run-journal snapshot;
- pending composer and recovery metadata;
- WebUI-only presentation fields.

A normal settled load must not parse a multi-megabyte JSON sidecar merely to
reproduce history already present in `state.db`. Eligibility for that fast path
requires a durable reconciliation receipt; a lightweight suffix heuristic is
not sufficient proof.

The receipt is WebUI-owned recovery metadata and contains no transcript
content. It records:

- active profile, lineage root, member IDs, target-lineage fingerprint, and the
  global projection-generation hint observed at reconciliation;
- the settled state-db message watermark and stable content identity;
- the sidecar generation, file size/mtime identity, and truncation watermark;
- the merged visible-transcript identity, exact merged display message count,
  target-lineage fingerprint, and receipt schema version.

All WebUI write paths that can change a sidecar or truncation watermark must
advance its generation. A settlement/reconciliation path writes the receipt
last, only after the current append-only merge agrees with the state-db
watermark and content identity. A crash before that final write leaves a
missing or mismatched receipt and therefore cannot incorrectly enable the fast
path.

Compatibility remains fail-safe:

- a legacy sidecar-only conversation follows the existing one-conversation
  lazy-import path, then retries the indexed state-db lookup;
- a missing or mismatched reconciliation receipt uses the exact current full
  merge for that requested session only;
- that fallback emits a structured diagnostic reason and never triggers a
  global scan or bulk reconciliation; after exact comparison, that targeted
  request may use the normal accepted reconciliation path to write the receipt
  last so the next load becomes eligible;
- a legacy-mode response emits no `cursor_v1` cursor, so later pages cannot mix
  state-db and sidecar ordering modes;
- a cursor requires restart when its target-lineage fingerprint, source mode,
  sidecar generation, truncation watermark, receipt generation, settled
  message watermark, or exact count no longer matches;
- a global projection-generation change triggers a bounded re-resolution and
  target-fingerprint comparison, but unrelated-session activity does not by
  itself invalidate the receipt or cursor;
- the state-db-first fast path is not enabled by default until shadow/fixture
  comparisons prove its visible output matches the current append-only merge.

The fallback is a correctness bridge, not the target steady state. New writes
must keep state-db history, receipt, and runtime journal sufficiently complete
that ordinary settled loads do not require it. The fast path has a server-side
kill switch independent of browser adoption. Enablement requires exact fixture
equivalence plus at least 1,000 sampled shadow loads over seven days with zero
visible transcript, ordering, truncation, or tool-pair differences. Any such
difference disables adoption and preserves the legacy response.

### 6. Derived view state without full-history rescans

The initial response must preserve current cold-load behavior for state derived
from history, especially the latest todo snapshot, including an explicit empty
todo list. It must not silently omit a todo panel that the current full merge
would show.

Maintain a small rebuildable WebUI view-state projection containing:

- latest normalized `todo_state`;
- the source message identity/watermark;
- the projection version;
- the update timestamp used by browser recency reconciliation.

Only a durably accepted todo tool result updates the settled projection. A live
SSE todo value is provisional runtime state keyed by `run_id` and journal
sequence; it may be overlaid only while the active run/journal owner is proven
and is never committed as settled projection state before durable acceptance.
It is presentation state, not shared conversation authority, and remains
rebuildable from the canonical transcript.

Projection storage is keyed by active profile and lineage root. Updates use an
atomic compare-and-swap on a durable source watermark (stable message identity
plus ordering key), so an older replay cannot overwrite a newer snapshot. An
explicit empty todo list is a first-class tombstone at its durable watermark,
not absence. Projection writes must not change session `updated_at`, unread
state, ordering, titles, pins, archive state, or any canonical metadata.

Existing sessions without projected todo state are upgraded lazily one
requested conversation at a time. The first request uses the exact current
one-conversation merged transcript to derive, atomically persist, and return
the latest todo state before responding. That request is explicitly
`legacy_todo_rebuild`, returns no cursor, and is excluded from the steady-state
latency guarantee. It must not alter model context or resurrect an older
non-empty todo list after a newer empty write. No startup or release migration
scans all sidecars.

## Data flow

### Initial conversation open

1. Validate the requested session ID and active profile.
2. Resolve the requested ID through the bounded indexed lineage primitive.
3. Read canonical metadata from the returned row.
4. Validate cursor capability and the reconciliation receipt. If either fails,
   select one-conversation legacy mode before reading messages.
5. Read one indexed state-db message page across returned member IDs, or the
   exact legacy merge selected in step 4. Never mix both sources in one page.
6. Overlay active runtime/recovery state without scanning unrelated sessions.
7. Attach settled projected todo state and any proven provisional live overlay;
   use the named one-conversation rebuild if the settled projection is absent.
8. Redact the complete response through the existing session-data boundary.
9. Return metadata and messages in one response. Return a cursor only if every
   component remained eligible for `cursor_v1`.
10. The browser adopts the canonical ID, renders the page, then attaches SSE or
   journal replay for an active run.

### Older-message page

1. Validate and decode the opaque cursor.
2. Re-resolve the requested/canonical ID and validate the cursor's profile,
   target-lineage fingerprint, source-mode, receipt, sidecar, truncation,
   settled-message, and exact-count state. A changed global generation only
   causes this bounded target comparison.
3. On any mismatch or paging-time fallback condition, return
   `cursor_restart_required` with no messages. The browser discards the old
   paging coordinates and retries one negotiated initial page.
4. Read the next bounded page across current valid compression members.
5. Advance from the last raw row examined and deduplicate overlap using stable
   message identity.
6. Prepend the page while preserving the current scroll anchor.

### Compression during an open conversation

1. The normal compression writer persists the new physical segment and bumps
   projection generation.
2. A later request re-resolves the old requested ID to the new visible tip.
3. The target-lineage fingerprint changes, so the server rejects the old cursor
   with `cursor_restart_required`. A global generation change caused only by an
   unrelated conversation would not do this.
4. The browser retries one fresh negotiated initial page; stable message
   identity prevents duplicate adoption across the newly extended lineage.
5. Active-pane ownership rules decide whether the browser may adopt the new
   canonical ID.

## Failure handling

### Missing or old state-db schema

On an initial request, return a degraded resolution and an explicit legacy-mode
response using the existing requested-session fallback. On a later cursor
request, return `cursor_restart_required` with no messages. Missing message
tie-break columns, normalizable ordering, or usable indexes also fail the
cursor capability gate. Do not create or migrate Agent tables or indexes from
the WebUI read path.

### Locked or unreadable database

Use the existing short, read-only failure path. A single requested sidecar may
degrade the view, but loading must not mutate or repair shared state.

### Cycle, overlong lineage, or ambiguous continuation

Fail closed to the directly requested row, record a bounded diagnostic, and do
not choose an arbitrary child. The browser may still expose explicit history or
recovery actions.

### Invalid cursor

Return a specific client error with no messages. For a well-formed cursor whose
bound state changed, return `cursor_restart_required`; the browser discards its
paging coordinates and may retry one fresh initial page. It must not loop,
prepend a legacy response to cursor-loaded messages, or silently substitute
another profile/session.

### Missing or mismatched reconciliation receipt

On an initial request, select the exact one-conversation legacy merge before
any message page is returned. On a later cursor request, return
`cursor_restart_required` with no messages. Record the mismatch reason, repair
the receipt only through the normal accepted settlement/reconciliation path,
and emit no cursor.

### Missing or corrupt todo projection

Use the named `legacy_todo_rebuild` path for the requested lineage, return the
derived value in the same response, and persist it atomically by durable
watermark. Do not silently return an older projection or omit a known panel.

### Stale response after a session switch

The existing browser load generation and pane-ownership checks veto the commit.
Server speedups must not weaken those guards.

### Redaction or serialization failure

Fail closed using the current authenticated API behavior. Cursors and
diagnostics must contain identifiers and numeric positions only, never message
content, credentials, workspace paths, or model context.

## Observability and reproducible performance budgets

Request diagnostics must start before canonical resolution and record these
separate stages:

- `canonical_resolution`;
- `state_message_page`;
- `runtime_overlay`;
- `derived_view_state`;
- `redaction_and_serialize`.

Diagnostics include row/query counts, lineage depth, requested/returned page
size, raw rows examined, serialized bytes, source mode, receipt generation,
cache hit/miss, and fallback reason. They do not log transcript content or
secrets.

The checked-in deterministic benchmark-fixture generator materializes into an
ignored temporary state directory containing:

- 2,560 physical sessions, of which 2,000 are archived;
- one target compression lineage of 12 segments and 20,000 raw messages;
- inactive, hidden, multimodal, missing-timestamp, and paired tool rows;
- a 100 MiB legacy sidecar for the target with a valid receipt variant and a
  mismatched-receipt variant;
- a scaling variant adding 10,000 unrelated sessions and 1,000,000 unrelated
  messages without changing the target lineage.

The benchmark runs an isolated WebUI state/home on the supported release Python
and SQLite versions on the same local-SSD host. Results record CPU model,
memory, OS, Python, SQLite, database size, and commit. “Warm” means one
unmeasured primer followed by 40 sequential authenticated opens.
“Process-cold” means 20 independent isolated server-process restarts with all
WebUI in-process caches empty and the first authenticated open recorded; it
does not claim the OS filesystem cache was purged. The core latency gate runs
at concurrency 1. A separate stress gate runs 20 rounds at concurrency 4. p95
uses the nearest-rank value over the stated samples.

For lineage depth `D` and requested visible page size `N`, a successful bounded
request must satisfy all of these mechanical limits:

- schema/index capability validation executes at most six SQL statements on a
  cache miss and is cached by database identity plus `schema_version`;
- canonical resolution executes at most `4 + 2D` SQL statements after that
  validation, or `10 + 2D` including a capability-cache miss;
- message paging executes at most `3 + D` SQL statements;
- raw rows examined do not exceed
  `max(256, min(2048, 8N)) + 64` including tool-pair closure;
- serialized transcript payload does not exceed 2.5 MiB including the closure
  allowance;
- neither query plan contains an unscoped full `sessions` or `messages` scan.

Acceptance budgets on the representative power-user dataset are:

- metadata-only session detail: p95 below 250 ms;
- warm initial 30-row view: p95 below 1 second;
- process-cold initial 30-row view: p95 below 2 seconds;
- no single-session request above 5 seconds in the acceptance sweep;
- no full `sessions` or `messages` table scan in a normal detail query plan;
- no duplicate canonical resolution in one browser open.

The scaling variant must keep the exact same SQL statement and raw-row counts.
Its warm and process-cold p95 may regress by no more than the greater of 100 ms
or 20 percent versus the base fixture. The concurrency-4 stress gate permits no
request above 5 seconds and no cursor/legacy mode drift within a load.

## Staged delivery

### Stage 1: bounded canonical resolution

Scope:

- extract the shared indexed lineage primitive;
- replace `resolve_shared_session_id()`'s full projection;
- make `/api/session` and compatibility detail/message builders reuse the
  returned canonical row;
- add a resolution-aware full-history adapter so existing message routes use
  the returned `member_ids` without reconstructing lineage while preserving
  their current wire response and exact one-conversation merge;
- add query-shape, semantic, and live timing regressions;
- update architecture documentation.

State mutation: none. This stage changes only read behavior and diagnostics.

Enablement: the new resolver/adapter can be shadow-compared against the current
semantic result before becoming the default read path.

Rollback: restore the previous resolver/adapter implementation. No data
migration or schema rollback is required.

### Stage 2A: negotiated server cursor reader

Scope:

- add the physically bounded state-db page reader;
- add explicit `cursor_v1` negotiation, capability checks, cursor validation,
  hard row/byte budgets, and the unchanged legacy response;
- shadow-compare bounded settled-history pages with the Stage 1 exact merge;
- keep the browser on numeric paging.

State mutation: none.

Enablement: a server-side cursor-reader gate defaults off. It may return
`cursor_v1` only after query-plan gates and exact fixture comparisons pass.

Rollback: disable the server gate. Legacy wire behavior and numeric paging are
unchanged.

### Stage 2B: bounded initial-view assembly

Scope:

- add and validate reconciliation receipts;
- attach bounded runtime/recovery overlays;
- add crash-safe settled and provisional todo/view-state handling;
- expose a complete negotiated initial-view response while the production
  browser still consumes legacy mode;
- run sampled shadow comparisons against the exact current merge.

State mutation: rebuildable WebUI presentation state only. Canonical
conversation and message authority stays in `state.db`.

Enablement: receipt fast-path and derived-state reads have independent
server-side gates. Receipt/projection writes may ship first because old readers
ignore them. Cursor mode remains unavailable whenever any proof is absent.

Rollback: disable either read gate. The exact one-conversation legacy merge
continues to work; new receipt/projection files or fields are ignored safely.

### Stage 2C: negotiated browser adoption

Scope:

- change the browser to issue one negotiated initial-view request;
- adopt `message_page` only after validating `mode=cursor_v1`;
- cursor-page older messages and handle one stale-cursor retry;
- retain the existing numeric flow for legacy-mode or old-server responses;
- update architecture/testing documentation and release-note wording.

State mutation: browser-local paging state only.

Enablement: a browser adoption gate defaults off until Stage 2A query/semantic
gates and Stage 2B's 1,000-load shadow threshold pass.

Rollback: disable browser adoption. The server keeps accepting legacy
`msg_before` and may retain dormant cursor/receipt support.

### Optional Stage 3: Agent-owned materialized aliases

Do not add a materialized `conversation_projection` or alias table as part of
Stages 1, 2A, 2B, or 2C. Consider it only if production evidence shows the indexed,
bounded resolver cannot meet the budgets.

Any such table would be Agent-owned, additive, transactionally updated at
session/compression writes, and versioned by projection generation. It would
require a separate cross-repository contract and migration design.

## Alternatives considered

### Cache the current full projection

Rejected as the permanent fix. It improves warm requests but every cold start,
invalidation, profile switch, or generation change remains proportional to all
sessions. Correctness also becomes overly dependent on invalidation coverage.

### Add materialized alias tables immediately

Deferred. It can make alias lookup effectively constant-time, but it adds Agent
schema ownership, migration, write-path coordination, and cross-client rollout
risk before the existing indexes have been used correctly.

### Create a new WebUI transcript store

Rejected. It would create another conversation authority and conflict with the
state-db-first shared contract.

### Keep two browser requests after fixing the resolver

Rejected as the end state. It leaves duplicate coordination, canonical-ID
handoff, and stale-response opportunities in the interactive path. A
metadata-only request remains useful for polling and compatibility, not normal
conversation open.

## Verification plan

### Resolver and query-shape tests

- root, middle, and tip IDs resolve to the expected canonical target;
- a valid non-snapshot direct ID remains stable;
- explicit history mode returns the requested physical row;
- fork, delegate, tool, and cross-source children never collapse;
- cycles, missing parents, missing indexes, old schemas, and 256-hop chains
  terminate safely;
- deterministic branch selection matches the shared list projection;
- query spies fail if `read_shared_session_rows()` or an unscoped sessions query
  runs;
- 10,000 unrelated sessions do not increase resolver query count.

### Detail and compatibility tests

- browser detail, REST detail, REST messages, URL/query aliases, and sidebar IDs
  agree on the canonical target;
- metadata-only detail does not load messages;
- one request reuses one resolution object;
- the Stage 1 full-history adapter reuses `member_ids` and preserves the exact
  current wire response without a second lineage walk;
- lazy legacy import retries only the requested session;
- profile mismatch does not disclose foreign-profile metadata or transcript.

### Message-page tests

- the first page returns the newest N renderable rows in chronological order;
- older cursors reproduce the complete current merged transcript;
- exact duplicate, restamped duplicate, edited/truncated, inactive, missing
  timestamp, multimodal, tool-call, and tool-result cases preserve semantics;
- a page boundary never strands a required tool result;
- hidden/inactive runs exhaust the raw-row budget with a short advancing page,
  not an unbounded scan;
- a tool pair outside the closure allowance selects legacy mode and emits no
  cursor on the initial request, but returns `cursor_restart_required` with no
  messages on a later page;
- `has_more` does not require a full count;
- compression between pages changes the target-lineage fingerprint and one
  fresh-page retry neither loses nor duplicates messages;
- unrelated-session activity may change global projection generation but does
  not invalidate a matching target-lineage fingerprint or restart pagination;
- cursor-mode `message_count` equals the current exact merged display count
  from the receipt plus only a proven bounded live delta; stale or unavailable
  count proof cannot enter or continue cursor mode;
- missing indexes, tie-break keys, or usable timestamps select legacy mode;
- invalid and cross-profile cursors fail closed.

### Runtime and derived-state tests

- active in-memory messages, pending user turns, run-journal replay, and settled
  state-db history produce one ordered visible timeline;
- current empty todo state beats older non-empty state;
- live todo snapshots beat older cold projections by timestamp;
- provisional todo state requires matching active `run_id` and journal
  sequence and cannot survive as settled state after an unaccepted crash;
- durable watermark compare-and-swap rejects older replay and preserves an
  explicit empty tombstone;
- missing/corrupt todo projection takes the named one-conversation rebuild,
  returns the correct panel, and emits no cursor;
- projection writes do not change recency, unread state, or canonical metadata;
- missing/mismatched receipts use the exact legacy merge and cursor source mode
  cannot change between pages; after a cursor exists, every would-be fallback
  returns a no-message restart response instead;
- redaction covers transcript, todo state, and runtime overlay together;
- maintenance reads do not move sidebar recency or unread state.

### Browser and live acceptance

- signed-in process-cold and warm conversation opens meet the latency budgets
  under the documented sample and concurrency procedure;
- rapid A -> B -> C switching cannot let A or B overwrite C;
- same-session reselect, reload, reconnect, active stream, compressed lineage,
  archived history, narrow/mobile, and older-message scroll anchoring work;
- server logs prove no full projection or duplicate resolution occurred;
- live verification uses the launchd-managed service, the 8787 listener,
  `/health`, and exact endpoint stage timings after restart.
- old-client/new-server, new-client/old-server, each server kill switch, and the
  browser rollback gate follow the compatibility matrix.

All Python tests run through `./scripts/test.sh`. Browser acceptance uses
isolated state directories unless the operator explicitly authorizes live-state
validation.

## Expected implementation surface

Stage 1 is expected to touch:

- `api/agent_sessions.py`;
- `api/routes.py`;
- canonical session projection and performance tests;
- `ARCHITECTURE.md`.

Stage 2A is expected to touch:

- `api/models.py` or a focused state-db message-page bridge;
- `api/routes.py`;
- cursor/query-shape and compatibility tests.

Stage 2B is expected to touch:

- the focused state-db message-page bridge and reconciliation receipt storage;
- `api/routes.py`;
- `api/todo_state.py` and rebuildable view-state persistence;
- runtime, replay, receipt, todo, and redaction tests.

Stage 2C is expected to touch:

- `static/sessions.js`;
- session-load, pagination, rollback, and browser tests;
- `ARCHITECTURE.md` and `TESTING.md`.

No new dependency, frontend framework, build step, long-lived worker, or bulk
migration is justified by this design.

## Release-note-ready wording

Opening a conversation now performs indexed work only for that conversation's
valid compression lineage and initial message page, instead of rebuilding the
entire session projection or parsing the full settled sidecar. Large installs
keep fast cold and warm conversation switching as session history grows.

## Approval boundary

Approval of this design permits an implementation plan for Stage 1 and Stages
2A, 2B, and 2C as separate reviewable changes. It does not permit Optional
Stage 3 without new measured evidence and an explicit cross-repository design
review.
