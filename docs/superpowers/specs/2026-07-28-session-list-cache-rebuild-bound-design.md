# Session List Cache Rebuild Bound

## Problem

Accepted r66 is healthy and keeps sidebar title projection read-only, but the
live WebUI remains CPU-bound. The visible-page activity fallback polls
`/api/sessions` every five seconds. The idle response cache expires after 2.5
seconds, so stable clients repeatedly trigger the full session projection over
the multi-gigabyte Agent SQLite store. Live evidence on r66 showed:

- WebUI PID 78272 at 108.4% CPU;
- three local Chrome connections and one remote connection;
- repeated session-list calls with slow projections around 1.3 to 2.2 seconds;
- a ten-second process sample dominated by SQLite query and `fetchall` work.

## Design

Change the bounded age-based rebuild interval for both idle and streaming
session-list cache entries to 30 seconds.

This does not make the cache authoritative or indefinite. Existing freshness
mechanisms remain unchanged:

- source-stamp changes invalidate immediately;
- `publish_session_list_changed` clears affected cache entries immediately;
- settings writes and projection-generation changes remain part of the source
  stamp;
- runtime stream, pending-message, and sort state is overlaid on every response.

The age interval remains a safety backstop for changes outside those normal
paths. Thirty seconds matches the convergence bound already documented for
externally driven changes during streaming.

## Alternatives Rejected

- **Focus-aware client polling:** helps duplicate windows on one device but not
  simultaneous local and remote clients. A focused client would still force a
  rebuild every five seconds against the 2.5-second cache.
- **A new activity-only endpoint:** could reduce payload and projection work,
  but adds a new protocol surface when the existing cache already has
  source-aware invalidation.
- **An infinite cache:** removes the safety backstop and is not acceptable.

## Tests

Use test-driven development:

1. Add a failing cache-contract test proving a stable idle entry remains fresh
   after the five-second client poll interval.
2. Prove the same entry expires after the 30-second bound.
3. Retain existing tests proving source changes invalidate immediately and
   stream transitions/settings changes remain visible.
4. Run the focused projection, cache, and long-history suites from an isolated
   temporary Hermes home.

## Release and Rollback

Ship as r67 based exactly on r66. Accept only after:

- managed identity, selector, WebUI, gateway, and pair-gate checks are exact;
- two consecutive 60-second post-open windows have WebUI CPU p95 below 80%;
- over the same interval, `/api/sessions` p95 is below one second and no call
  exceeds five seconds;
- the restored watchdog produces a completed automatic run, persists its
  output/state, clears its lease, and schedules the next run.

Any identity, health, scheduler, or performance failure triggers the managed
rollback path to r66. No frontend polling change, watcher redesign, or unrelated
cleanup is bundled into r67.
