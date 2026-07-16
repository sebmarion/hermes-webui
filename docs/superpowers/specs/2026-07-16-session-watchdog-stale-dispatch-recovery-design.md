# Session Watchdog Stale Dispatch Recovery Design

## Goal

Prevent one uncertain WebUI recovery from permanently blocking automatic
recovery for every other Hermes session, while preserving the existing rule
that an uncertain session must never be replayed automatically when its side
effects cannot be proven safe.

## Observed failure

The session watchdog uses one machine-global recovery slot to prevent
overlapping recoveries. Before calling the authenticated WebUI recovery
endpoint it durably moves that slot from `prepared` to `dispatching`.

The cron runner and watchdog currently have incompatible time budgets: the
runner may terminate the watchdog script before the watchdog's own WebUI
recovery wait expires. If termination happens after dispatch, the script never
reaches claim finalization. The slot remains `dispatching`, even after the WebUI
has no live stream or worker for that recovery.

Subsequent watchdog ticks still run, but claim acquisition refuses every new
safe candidate because the global slot is occupied. That refusal produces no
output, so cron records a silent successful run. The result is a healthy
scheduler with an inert recovery fleet.

## Constraints

- Keep at most one live automatic recovery at a time.
- Never replay the uncertain original session automatically.
- Treat `state.db` as the canonical transcript and logical-turn source.
- Use the WebUI turn journal and live sidecar/worker registries only as recovery
  ownership and lifecycle evidence.
- Do not rewrite conversation messages or delete recovery journals.
- Do not clear a slot while its matching WebUI stream or worker is live.
- A failure involving one session must not silently disable recovery for other
  sessions.
- Keep the repair dependency-free and compatible with the existing Python
  watchdog and WebUI recovery endpoint.

## Considered approaches

### 1. Operational slot reset only

Back up the watchdog state, mark the stranded claim for manual attention, clear
the global slot, and trigger the job again.

This restores service quickly, but the next watchdog termination after dispatch
can recreate the same fleet-wide wedge. It is necessary as immediate recovery,
but insufficient as the durable repair.

### 2. Bounded stale-dispatch reconciliation

Before selecting a new candidate, reconcile any existing slot against its
claim, journal reservation, and live WebUI ownership. Keep live ownership,
finalize terminal ownership, and quarantine provably abandoned ownership at the
session level while releasing the machine-global slot. Align the outer cron
timeout above the watchdog's recovery timeout and report blocked/stale state
instead of exiting silently.

This is the chosen approach. It keeps the current synchronous recovery model,
preserves fail-closed behavior for the uncertain session, and prevents that
uncertainty from freezing unrelated work.

### 3. Fully asynchronous dispatch and reconciliation

Return immediately after WebUI accepts a durable reservation, then let later
watchdog ticks reconcile its terminal state.

This removes the long-running cron subprocess and is a reasonable future
architecture, but it changes more lifecycle semantics than required for this
incident and would need a larger migration and compatibility plan.

## Chosen recovery contract

At the start of each watchdog tick, reconcile the global slot before scanning
new candidates.

### Prepared slot

Keep the existing launch-grace behavior. A `prepared` slot older than the grace
period has not crossed the dispatch boundary and can be reclaimed safely. Mark
its claim `prepared_claim_reclaimed`, record an event, and clear the slot.

### Dispatching slot with live ownership

Retain the slot when any authoritative live signal matches the reservation:

- the recovery PID is alive and belongs to the expected recovery command;
- the WebUI sidecar's active stream matches the recovery reservation and the
  stream or worker registry reports it live; or
- the matching journal lifecycle is non-terminal and still within the bounded
  recovery window.

This state is not an error and must not permit another recovery to start.

### Dispatching slot with terminal evidence

When the matching journal reservation has exactly one valid terminal event,
reuse the existing strict completion-marker validation:

- `RECOVERED: ...` marks the claim `recovered`;
- `RECOVERY_BLOCKED: ...` marks the claim `blocked`;
- an interrupted or malformed terminal remains uncertain and is quarantined.

Finalization clears the global slot and appends a durable recovery event.

### Provably abandoned dispatch

A dispatch is abandoned only when all of these are true:

1. its bounded recovery window plus a small reconciliation grace has elapsed;
2. no matching recovery PID exists;
3. the WebUI reports no live matching stream or worker;
4. the matching reservation has no valid terminal event; and
5. the stored slot still matches the same claim token, logical user turn, and
   transcript fingerprint while the state lock is held.

The watchdog must not retry that session. It marks the claim `manual` with an
`abandoned_dispatch` reason, clears the machine-global slot, and emits an alert
identifying the session and reason without including prompt content. Other safe
sessions may then recover normally.

If the evidence is malformed or contradictory, keep the slot fail-closed and
emit a manual-action alert instead of returning silent success.

## Timeout contract

The cron script timeout must exceed the watchdog WebUI recovery timeout plus
finalization margin. For the current one-hour recovery window, use a 4,200
second cron script timeout.

The watchdog also performs startup reconciliation, so an unexpected process or
gateway termination can be repaired on a later tick even when the aligned
timeout contract is not enough.

## Immediate live-state repair

Before changing the durable state:

1. acquire the watchdog state lock;
2. re-read the slot and verify its claim token has not changed;
3. verify there is no recovery PID and no live matching WebUI owner;
4. create a permission-preserving backup of the state file;
5. mark the stranded claim `manual` with an `abandoned_dispatch` event; and
6. clear only `recovery_slot` with the existing atomic state writer.

Do not delete the claim, its event history, or its turn journal. Do not trigger
the uncertain original session again.

## State layers and invariants

- **Canonical transcript and turn identity:** profile `state.db`; never
  rewritten by this repair.
- **WebUI recovery lifecycle:** turn journal plus live stream/worker registries;
  used to prove whether ownership is live or terminal.
- **Watchdog coordination:** recovery state file and lock; owns the machine-wide
  single slot and per-turn retry records.
- **Cron execution budget:** outer process lifetime; must outlive the watchdog's
  bounded synchronous wait.

Required invariants:

1. No two automatic recoveries are live concurrently.
2. An uncertain dispatched session is never replayed automatically.
3. A provably abandoned session can require manual action without blocking
   unrelated safe candidates.
4. Slot reconciliation is token-, turn-, profile-, and fingerprint-specific.
5. Ambiguous evidence produces an alert, not silent `ok`.
6. Every slot release is durable and auditable.

## Verification

Write failing watchdog tests before implementation for:

- a live dispatch retaining the slot;
- a terminal recovered dispatch finalizing and releasing the slot;
- a terminal blocked dispatch finalizing and releasing the slot;
- a provably abandoned dispatch becoming `manual` and releasing the slot;
- an abandoned dispatch never being retried;
- malformed or contradictory evidence retaining the slot and producing a
  manual-action alert;
- a stale slot no longer suppressing a different safe candidate silently; and
- the cron timeout exceeding the watchdog recovery timeout plus margin.

Run the focused local watchdog suite and WebUI atomic-recovery tests. For live
acceptance, back up and reconcile the stranded slot, manually trigger the
watchdog once, and verify:

- the stranded original claim remains manual;
- one eligible safe candidate acquires the slot;
- its reservation becomes terminal or remains observably live;
- the slot is eventually cleared or reports a non-silent manual action;
- the gateway ticker remains healthy; and
- the WebUI listener and `/health` endpoint remain healthy.

## Rollback

Restore the backed-up watchdog state only if no recovery PID, WebUI stream, or
worker is live. Revert the watchdog script and timeout configuration together;
reverting only the script can restore the timeout mismatch. The transcript and
turn journals need no rollback because the repair does not modify them.
