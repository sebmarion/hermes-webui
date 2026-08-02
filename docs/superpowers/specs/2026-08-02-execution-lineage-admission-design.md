# Execution-Lineage Admission Design

**Date:** 2026-08-02

**Status:** Approved direction; implementation pending

**Contract family:** runtime, streaming, recovery, and compression lineage

## Summary

Hermes WebUI currently prevents two turns from running against the same
physical session ID. That is too narrow for one logical execution that moves
between physical sessions through automatic compression or a tool-limit
continuation.

The fix is to add one immutable `execution_lineage_key` to the existing
in-process run-admission reservation and carry it into `ACTIVE_RUNS`. A second
reservation for the same key is rejected atomically. Existing deferred wakeups
are grouped by the same key while retaining their original physical target.
After a parent run unregisters, WebUI retries its already-durable tool-limit
continuation before draining deferred work.

This reuses the admission lock, reservation registry, active-run registry,
tool-limit receipt, and deferred-wakeup queue that already exist. It adds no
new scheduler, lock family, database table, durable queue, or browser state.

## Incident and root cause

The 2026-08-02 incident produced two genuinely concurrent WebUI runs in one
visible conversation. It was not only a duplicated sidebar row.

The observed sequence was:

1. A process completion for ancestor session `R` was deferred while `R` had an
   active turn.
2. The turn reached the tool limit and durably created continuation child `C`.
3. `handle_terminal()` called `start_session_turn(C, ...)` before the parent
   `ACTIVE_RUNS` row was removed.
4. Child admission checked only physical ID `C`, so it started while physical
   ID `R` was still active.
5. Parent teardown removed the `R` row and drained deferred work keyed by `R`.
6. `_session_has_active_turn(R)` compared exact physical IDs. The active child
   carried `C`, so the check returned false and the deferred `R` wakeup also
   started.

The same exact-ID assumption exists in the short interval between reservation
and `ACTIVE_RUNS` registration. The existing per-session sidecar lock cannot
close either gap because `R` and `C` use different lock keys.

The invariant that failed was:

> At most one admitted or active run may own a logical execution lineage,
> regardless of which physical session segment currently carries the turn.

## Goals

- Enforce one admitted-or-active run per execution lineage.
- Treat compression root/tip aliases and tool-limit parent/child segments as
  the same execution owner.
- Close the pre-`ACTIVE_RUNS` window by counting bound reservations as owners.
- Preserve deferred prompts and durable continuation receipts when admission
  rejects a competing start.
- Start a claimed tool-limit continuation only after its parent unregisters.
- Apply the same lifecycle rule to the deployed native and Gateway-backed
  in-process workers.
- Keep manual forks, delegated/background child sessions, and different
  profiles independent.

## Non-goals

- No new scheduler, queue, lock registry, service, or database schema.
- No change to the undeployed external runner's ownership protocol. An
  external runner does not use this process's `ACTIVE_RUNS` as execution truth;
  it needs its own adapter contract before this key can govern it.
- No sidebar, archive, session-collapse, or browser-state change.
- No stale-spinner cleanup; stale presentation state is a separate bug.
- No deletion or rewriting of archived sessions or compression snapshots.
- No cleanup, adoption, or refactor of the existing unrelated dirty recovery
  work in the checkout.
- No lineage-wide re-indexing of the durable async-delegation store. Its starts
  receive the concurrency gate, while its existing exact-session/runtime and
  startup-replay semantics remain unchanged.

## Terms

| Term | Meaning |
| --- | --- |
| Physical session ID | The concrete session ID supplied to a worker and sidecar lock. |
| Compression lineage | The bounded, validated compression-only chain returned by `resolve_shared_session(..., mode="history")`. |
| Tool-limit lineage | A root plus the child segments whose persisted tool-limit control metadata names that root. |
| Execution root | The validated root ID used to serialize execution across both lineage types. |
| Profile state identity | The resolved absolute path of the session profile's `state.db`; path identity is used instead of inode identity so creating or replacing the database cannot change a live key. |
| Execution lineage key | A versioned opaque digest of `(profile_state_identity, execution_root)`. It is equality-tested only and is not a public API field. |
| Bound reservation | An existing `_RUN_ADMISSION_RESERVATIONS` entry after it has acquired an execution lineage key. |

## Required invariants

1. Under `ACTIVE_RUNS_LOCK`, no two bound reservations or active runs have the
   same `execution_lineage_key`.
2. Moving a reservation into `ACTIVE_RUNS` is one atomic ownership transfer;
   there is no unowned gap.
3. `update_active_run()` may rotate `session_id` after compression but may not
   replace or remove `execution_lineage_key`.
4. A turn-bearing reservation binds before that turn's pending-sidecar write,
   turn-journal append, stream registration, provider call, or thread launch.
   A lineage-busy rejection therefore performs none of those mutations.
5. A rejected deferred wakeup stays queued, and a rejected tool-limit child
   stays durably claimed.
6. A deferred entry retains the physical `target_session_id` and validated
   `target_profile` on which its prompt must eventually run even though queue
   ownership is lineage-wide.
7. The final active or bound owner in a lineage is the only teardown allowed
   to claim its next deferred item.
8. Profile identity is part of the key; equal session IDs in different profile
   databases do not block one another.
9. Manual forks and ordinary background/delegated child sessions do not inherit
   the execution root merely because they have a `parent_session_id`.

## Execution-lineage resolution

One shared helper resolves the key for admission, active checks, and deferred
work. Separate ad-hoc root calculations are not allowed.

### Profile state identity

The helper validates the persisted session profile with the existing profile
name rules, resolves that profile's Hermes home without changing process-global
profile state, and uses the resolved `<profile-home>/state.db` path as the first
key component.

The path is used even when the database has not been created yet. The key must
not include device or inode values: a first turn may create `state.db`, and a
maintenance operation may replace it, neither of which should create a second
execution namespace.

Invalid or unresolvable profile identity fails closed before mutation. It must
not fall back to the active/default profile.

### Execution root

Resolution is intentionally narrow:

1. Start with the physical session ID.
2. If tool-limit control metadata exists, validate that
   `Session.root_session_id` and
   `Session.tool_limit_continuation.root_session_id` are both non-empty and
   equal. The durable receipt selected by child ID must agree on child ID,
   root ID, execution ID, and normalized profile. That explicit root becomes
   the candidate. A missing receipt, partial identity, or conflict fails closed.
3. Resolve the candidate upward through the existing bounded,
   compression-only `resolve_shared_session(..., mode="history")` helper.
4. On `found`, use its `root_id`.
5. On `missing`, use the candidate ID. This is the expected first-turn case
   before a WebUI session has a shared-state row.
6. On `degraded` or `ambiguous`, reject admission with a retryable typed error.
   Unknown lineage must not take the permissive branch.

Only tool-limit metadata can bridge a non-compression parent/child edge.
`parent_session_id` by itself never changes execution ownership, preserving
manual-fork and subagent independence.

### Resolution result

The internal result contains:

- `execution_lineage_key`;
- `execution_root_session_id`;
- the requested physical session ID; and
- the bounded compression member IDs returned by the existing resolver.

Only `execution_lineage_key` is stored on reservations and active runs. The
other values are short-lived routing data used to retain physical targets and
perform bounded post-run checks.

The key is a versioned SHA-256 digest over a canonical encoding of the two
validated components. The state-database path and root ID are never copied into
`ACTIVE_RUNS`. The lifecycle health response must explicitly remove the opaque
key as well, preserving the rule that internal ownership metadata is not a
public health field.

## Admission and ownership transfer

### Bind the existing reservation

`_start_chat_stream_for_session()` remains the shared native/Gateway defensive
chokepoint. It already runs inside `_admit_stream_start`, so an admission
reservation exists before it can mutate a sidecar or start a worker.

Every local turn-bearing entrypoint binds at its earliest point after physical
session/profile identity exists and before the first turn-state mutation.
`start_session_turn()` therefore binds after loading the session but before
process-pause or delegation-turn mutation. The browser/local-adapter path binds
after read-only backend selection and before pending state. The shared chat
chokepoint repeats the bind idempotently as a defensive assertion.

Resolution, receipt lookup, profile lookup, and state-db reads happen before
taking `ACTIVE_RUNS_LOCK`; the locked section only validates the already-
computed key and compares in-memory owners. External-runner selection bypasses
this local bind because external execution ownership is explicitly out of
scope.

Binding is idempotent for the same reservation and key because nested admitted
helpers reuse the same reservation. Binding a reservation to a different key
is an internal error.

While holding `ACTIVE_RUNS_LOCK`, binding scans:

- every other bound `_RUN_ADMISSION_RESERVATIONS` entry; and
- every `ACTIVE_RUNS` entry.

An equal key rejects the start with HTTP 409 and a typed
`execution_lineage_busy` code. The existing human-facing error remains
"session already has an active stream" so current browser behavior needs no UI
change.

Lineage resolution failure returns a retryable 503
`execution_lineage_unavailable` response. It is distinct from ordinary busy
state and occurs before any turn mutation.

### Reservation to active run

`register_active_run()` already consumes a reservation and inserts the active
row while holding `ACTIVE_RUNS_LOCK`. It carries the bound
`execution_lineage_key` into the active entry during that same critical
section.

For native/Gateway chat workers, a missing key is a rejected upgrade rather
than an unkeyed active run. Existing auxiliary run kinds that do not execute a
conversation remain unchanged.

Only turn-bearing reservations are bound. The required classification is:

| Reservation/worker | Lineage behavior |
| --- | --- |
| Browser/local-adapter chat, process/goal/tool continuation, native worker, Gateway-backed worker | Bind to the addressed conversation lineage. |
| `/btw` and ordinary background agent worker | Bind to the newly created hidden child as an independent lineage before its first save/launch. |
| Manual compression worker | Bind to the addressed conversation lineage before compression mutation. |
| Background finalizer, title worker, recovery sweep, release helper, other sessionless auxiliary work | Remain unkeyed; these intentionally overlap a keyed turn. |

Implementation planning must inventory every direct `register_active_run()`
caller against this table. No direct conversation worker may silently remain
unkeyed, and no overlapping auxiliary helper may accidentally acquire its
owner's key.

If worker registration fails, existing reservation/active-run cleanup releases
the key on every error path. Cancellation, normal completion, and exceptions
release it through `unregister_active_run()`.

### Compression

When automatic compression rotates the physical session ID, the worker keeps
the original key. `update_active_run(stream_id, session_id=new_tip)` updates
presentation/activity routing only. Attempts to change
`execution_lineage_key` through generic metadata update are rejected.

## Deferred wakeups

`DEFERRED_PROCESS_WAKEUPS` remains the existing process-local queue. Its bucket
key changes from physical `session_id` to `execution_lineage_key`; no second
queue is introduced.

Each entry gains `target_session_id` and normalized `target_profile`. Existing
fields such as `process_id`, `wakeup_prompt`, `async_delegation_id`, and
`completion_event` keep their current meaning.

Recording, peeking, claiming, and re-deferring all use the shared lineage
resolver. When recording against a lineage that is already bound or active,
the exact live key is reused so a concurrent database creation or compression
rotation cannot move the deferred entry into another bucket.

The active-turn predicate becomes lineage-aware and checks both bound
reservations and `ACTIVE_RUNS`. This makes a child reservation visible during
the pre-worker window and prevents an ancestor drain from mistaking it for an
idle lineage.

Draining keeps the current one-prompt-per-turn rule:

1. If any reservation or active run owns the lineage, leave the bucket intact.
2. Atomically claim the bucket under `DEFERRED_PROCESS_WAKEUPS_LOCK`.
3. Re-defer entries two through N before starting entry one.
4. Start entry one on its retained `target_session_id` with an
   `expected_profile` check, not on the session whose teardown happened to
   trigger the drain. A missing/mismatched target profile fails closed and
   requeues the entry rather than routing it through the active profile.
5. A 409 or maintenance fence re-records the same entry under the same lineage
   key, preserving current at-least-once retry and exactly-one-claim behavior.

The durable async-delegation SQLite state machine is not replaced or migrated.
Its wakeup start also passes through execution-lineage admission, so it cannot
create a concurrent alias run. Indexing that durable store by every historical
tool segment is a separate durability enhancement. This design's lineage-wide
claim/drain guarantee applies to `DEFERRED_PROCESS_WAKEUPS`, the queue involved
in the reproduced incident; durable delegation is covered here only by the
authoritative no-concurrent-start gate and retains its existing exact-session
runtime retry plus startup replay.

## Parent teardown order

Native and Gateway-backed workers use one shared post-unregister helper so the
ordering cannot drift between backends.

The order is:

1. Finish transcript/state writeback and existing terminal bookkeeping.
2. For a tool-limit terminal, create or update the durable child receipt but do
   not launch the child while the parent owns the lineage.
3. Remove the parent stream and call `unregister_active_run()`.
4. Retry claimed tool-limit receipts filtered to this execution root through
   the existing `recover_pending_continuations()` mechanism.
5. Preserve the existing goal-continuation recovery hook. Tool-limit recovery
   has priority because it continues the turn that just exhausted its tool
   budget; the admission gate keeps a still-pending goal continuation durable.
6. Drain the existing process-local deferred bucket for the lineage. The
   durable delegation store retains its current exact-session retry behavior.

If the tool child starts, its bound reservation makes step 6 a no-op. When that
child later unregisters, the same helper runs again; no tool receipt remains
runnable, so the deferred ancestor wakeup starts exactly once.

If child launch is rejected or fails, its receipt remains claimed. Deferred
work remains protected by admission even if it starts on a later idle boundary;
the two paths cannot run concurrently.

Startup recovery continues to use the existing durable tool-limit receipt
replay. No execution key is persisted because a fresh process recomputes it
from validated profile/session metadata before every recovered start.

## Backend scope

| Path | Behavior |
| --- | --- |
| Native in-process worker | Bind reservation, carry key into `ACTIVE_RUNS`, use shared post-unregister helper. |
| Gateway-backed in-process worker | Same reservation/key lifecycle and shared post-unregister helper. Gateway network work is still owned by its existing worker. |
| Legacy runtime adapter delegating to the local path | Covered because it reaches `_start_chat_stream_for_session()`. |
| External runner adapter | Explicitly out of scope; it does not expose a compatible local ownership lifetime. |
| Manual fork, `/btw`, ordinary background child, delegated subagent | Independent key unless it is explicitly a tool-limit continuation. |

## Error handling and durability

- **Busy lineage:** return 409 before mutation; caller keeps its durable receipt
  or deferred entry.
- **Ambiguous/degraded lineage:** return retryable 503 before mutation and log
  only non-content identity/status fields.
- **Invalid profile or conflicting tool root:** fail closed; never use the
  active profile as a fallback.
- **Worker upgrade failure:** remove the exact active entry inserted by that
  reservation; do not disturb another stream.
- **Deferred start loses a race:** re-defer the exact original entry and target.
- **Process restart:** in-memory reservations, active rows, and generic deferred
  wakeups disappear as they do today; durable tool/delegation receipts retain
  their existing recovery semantics.
- **Logging:** include the safe root/profile identity hash, owner kind, and
  phase; never log prompts, credentials, or full session content.
- **Lock ordering:** never perform profile resolution, state-db reads, sidecar
  I/O, receipt I/O, thread creation, or network work while holding
  `ACTIVE_RUNS_LOCK`.
- **No lock nesting:** compute lineage before either registry lock; compare and
  bind under `ACTIVE_RUNS_LOCK`; release it before touching
  `DEFERRED_PROCESS_WAKEUPS_LOCK`, a receipt lock, or the delegation-store lock.
  Queue operations never call the resolver while holding the queue lock.

## State ownership

| State | Owner | Release/transition rule |
| --- | --- | --- |
| Execution admission | `_RUN_ADMISSION_RESERVATIONS` under `ACTIVE_RUNS_LOCK` | Bind once; atomically transfer to `ACTIVE_RUNS` or release on rejection/failure. |
| Worker liveness | `ACTIVE_RUNS` under `ACTIVE_RUNS_LOCK` | Exists through final durable writeback; removed once in outer teardown. |
| Physical sidecar mutation | existing `_get_session_agent_lock(session_id)` | Remains physical; not promoted to a lineage lock. |
| Tool child durability | existing tool-limit continuation receipt | Claim before launch; failed/busy launch remains claimed. |
| Generic deferred prompt | existing `DEFERRED_PROCESS_WAKEUPS` | Bucketed by lineage, claimed atomically, original target retained. |
| Durable async delegation | existing `delegation_wakeups.sqlite3` | Existing exact-session pending/claimed/delivered state; all starts pass through lineage admission. |
| Compression projection | existing sidecar and `state.db` lineage | Physical ID may rotate; execution key does not. |

## State-space coverage

Implementation and review must cover:

- entry source: browser, process wakeup, goal continuation, tool-limit
  continuation, `/btw`, ordinary background agent, manual compression, and
  durable async-delegation admission (not lineage-wide durable-store draining);
- backend: native and Gateway-backed local workers;
- ownership phase: unbound reservation, bound reservation, active run,
  unregistering run, and idle lineage;
- lineage shape: one session, compression root/tip, tool root/child/multiple
  segments, manual fork, and same ID in another profile;
- exit: success, error, cancellation, provider failure, launch rejection,
  compression rotation, and teardown;
- deferred count: zero, one, and many entries;
- resolution: found, missing first-turn row, degraded, ambiguous, and invalid
  tool metadata.

The external runner and stale UI activity are explicitly marked out of scope
rather than treated as covered.

## Fail-first test plan

The implementation starts with behavioral tests that fail on the current code.
At minimum:

1. **Tool child versus ancestor wakeup:** while root `R` is active, defer a
   process wakeup for `R` and claim tool child `C`. Parent teardown must produce
   exactly one lineage owner. `C` starts first; the `R` wakeup remains queued.
2. **Pre-`ACTIVE_RUNS` reservation:** bind a reservation for `R` but do not
   register its worker. Binding a reservation for compression tip or tool child
   `C` must return busy and perform no sidecar/journal/thread mutation.
3. **Child teardown drains ancestor once:** after `C` completes and unregisters,
   the retained `R` wakeup starts exactly once. Re-running teardown/drain is a
   no-op.

Neighboring coverage:

- compression root and tip resolve to the same key;
- compression rotation changes `ACTIVE_RUNS.session_id` but not its key;
- a manual fork and ordinary background/delegated child remain independent;
- equal physical IDs in two profile databases remain independent;
- conflicting tool root fields and ambiguous/degraded compression resolution
  fail before mutation;
- missing or conflicting durable tool receipts fail before admission;
- failed child start leaves its receipt claimed;
- a wakeup 409 requeues the same physical target;
- deferred dispatch validates the retained target profile and cannot fall back
  to another profile;
- multiple deferred items preserve order and start one per teardown;
- native and Gateway teardown both call the shared ordering helper;
- auxiliary/finalizer reservations may overlap a keyed turn without collision;
- all health, admission, checkpoint, and logging projections either whitelist
  safe fields or remove the key and its source path/root values;
- success, exception, cancellation, and registration failure release ownership;
- existing release-fence and wakeup race suites remain green.

Tests run only through `./scripts/test.sh`. The focused suites are expected to
include `tests/test_tool_limit_continuation.py`,
`tests/test_wakeup_defer_race.py`, admission/release tests, compression-lineage
tests, and new end-to-end lineage-admission tests.

## Rollout and rollback

This is an in-process concurrency guard with no persistent schema migration.
Rollout requires:

- focused fail-before/pass-after evidence for the three incident tests;
- native and Gateway neighboring tests;
- the repository runtime lint; and
- the broad test gate, with any pre-existing unrelated failure reported
  separately rather than hidden.

Rollback is the code revert. Existing sidecars, `state.db`, tool-limit receipts,
and durable delegation rows remain readable because this design changes none of
their schemas. Process-local deferred buckets reset on restart exactly as they
do today.

## Dirty-work isolation

The working checkout already contains unrelated edits across runtime, backend,
frontend, and tests. They are not evidence for, or part of, this fix.

- Do not reset, rebase, discard, or silently adopt those changes.
- Commit this design document by itself.
- Keep the implementation diff path- and hunk-scoped to the lineage fix and its
  tests, even where it must touch a file that was already dirty.
- Report the broad suite's known unrelated failure separately from new focused
  regressions.
- Do not combine archive cleanup, stale-spinner work, or historical recovery
  cleanup with the implementation patch.

## Acceptance

The change is complete when one execution lineage can never have more than one
bound reservation or active run; the reproduced tool-child/ancestor-wakeup race
has exactly one winner; the losing generic wakeup remains in its existing
process-local queue and later runs once while durable tool receipts retain
their existing restart semantics; native and Gateway lifecycles share the same
order; compression, forks, and profiles behave as specified; and no new
scheduler, lock family, queue, schema, or UI state was introduced.
