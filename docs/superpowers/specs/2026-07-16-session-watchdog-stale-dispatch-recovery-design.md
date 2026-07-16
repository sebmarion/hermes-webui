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
- Reuse the internal recovery signing key for status checks; do not expose
  recovery ownership through an unauthenticated endpoint.

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

### Authenticated ownership status

Add a signed loopback-only `POST /api/internal/recovery/status` endpoint beside
the existing recovery-start endpoint. It accepts the same reservation-binding
fields as recovery start: session ID, profile, logical user-message ID,
transcript fingerprint, and recovery claim token. The WebUI validates the HMAC,
request age, loopback origin, canonical `state.db` turn, and matching journal
reservation before returning lifecycle state.

The response is one of these bounded states:

- `live`: the reservation's stream is present in `STREAMS` or `ACTIVE_RUNS`;
- `terminal_recovered`: the matching non-live reservation has exactly one valid
  completed event and a strict `RECOVERED: ...` marker;
- `terminal_blocked`: the matching non-live reservation has exactly one valid
  blocked or interrupted terminal outcome;
- `absent`: the matching reservation exists, has no live stream or worker, and
  has no terminal event;
- `unknown`: reservation binding, journal structure, sidecar state, or worker
  ownership is malformed, contradictory, or cannot be read.

An unavailable endpoint is not equivalent to `absent`; the watchdog treats it
as `unknown` and retains the slot.

Evidence precedence is explicit:

| Status evidence | Watchdog action |
|---|---|
| Any matching live stream or worker, even with a terminal journal row | Retain the slot and alert on the contradiction |
| Non-live, uniquely bound, strict recovered terminal | Finalize recovered and release the slot |
| Non-live, uniquely bound, blocked/interrupted terminal | Finalize blocked and release the slot |
| Non-live, uniquely bound, non-terminal, within abandonment age | Retain the slot |
| Non-live, uniquely bound, non-terminal, past abandonment age | Quarantine the session and release the slot |
| Malformed, contradictory, unavailable, or unbound evidence | Retain the slot and emit manual action |

### Prepared slot

Keep the existing launch-grace behavior. A `prepared` slot older than the grace
period has not crossed the dispatch boundary and can be reclaimed safely. Mark
its claim `prepared_claim_reclaimed`, record an event, and clear the slot.

### Dispatching slot with live ownership

Retain the slot when any authoritative live signal matches the reservation:

- the recovery PID is alive and belongs to the expected recovery command;
- the authenticated WebUI status reports the reservation's stream or worker
  live; or
- the matching journal lifecycle is non-terminal and still within the bounded
  recovery window.

This state is not an error and must not permit another recovery to start.

### Dispatching slot with terminal evidence

Only after the authenticated status endpoint proves that no matching stream or
worker is live, a matching journal reservation with exactly one valid terminal
event reuses the existing strict completion-marker validation:

- `RECOVERED: ...` marks the claim `recovered`;
- `RECOVERY_BLOCKED: ...` marks the claim `blocked`;
- an interrupted or malformed terminal remains uncertain and is quarantined.

Finalization clears the global slot and appends a durable recovery event.

### Provably abandoned dispatch

A dispatch is abandoned only when all of these are true:

1. its bounded recovery window plus a small reconciliation grace has elapsed;
2. no matching recovery PID exists;
3. the authenticated WebUI status is `absent` for the exact reservation;
4. the matching reservation has no terminal event; and
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
reconciliation grace and finalization margin. Define
`RECONCILIATION_GRACE_SECONDS = 120` and require:

```text
cron_script_timeout >= recovery_timeout + reconciliation_grace + 300 seconds
```

For the current 3,600-second recovery window, use a 4,200-second cron script
timeout. The remaining 480 seconds cover the 120-second reconciliation grace
plus 360 seconds of status/finalization margin.

The watchdog also performs startup reconciliation, so an unexpected process or
gateway termination can be repaired on a later tick even when the aligned
timeout contract is not enough.

## Immediate live-state repair

Deploy and restart the WebUI status endpoint before changing durable watchdog
state. Then:

1. acquire the watchdog state lock;
2. re-read the slot and verify its claim token, profile, logical user turn, and
   transcript fingerprint have not changed;
3. verify the slot age exceeds recovery timeout plus reconciliation grace;
4. verify there is no matching recovery PID;
5. require the authenticated status endpoint to return `absent` for the exact
   reservation, not merely an unavailable or ambiguous response;
6. create a permission-preserving backup of the state file;
7. mark the stranded claim `manual` with an `abandoned_dispatch` event; and
8. clear only `recovery_slot` with the existing atomic state writer.

Do not delete the claim, its event history, or its turn journal. Do not trigger
the uncertain original session again.

## State layers and invariants

- **Canonical transcript and turn identity:** profile `state.db`; never
  rewritten by this repair.
- **WebUI recovery lifecycle:** turn journal plus live stream/worker registries;
  exposed to the watchdog only through the signed reservation-bound status
  endpoint.
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

Write failing WebUI tests before implementation for:

- status requests requiring loopback HMAC authentication and request freshness;
- claim, profile, logical-turn, and fingerprint mismatch returning `unknown` or
  rejection without leaking transcript content;
- live registry ownership taking precedence over terminal-looking journal
  evidence;
- strict recovered and blocked terminal classification only after ownership is
  non-live;
- exact-reservation non-terminal state returning `absent`; and
- malformed, contradictory, or missing evidence returning `unknown` rather
  than `absent`.

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
