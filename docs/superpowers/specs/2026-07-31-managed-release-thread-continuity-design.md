# Managed Release Thread Continuity Design

## Status

Approved behavior, ready for adversarial specification review.

This design supersedes the old rule that an unacknowledged active thread aborts
the release indefinitely. It does not weaken identity, artifact, health, or
rollback gates.

## Objective

Make this the default paired-release behavior:

1. fence new Hermes work;
2. send every active WebUI thread one release-checkpoint control;
3. wait no more than 300 seconds for safe checkpoint acknowledgements;
4. if work remains, stop only the exact attested Hermes WebUI and Gateway
   services;
5. start and verify the candidate pair, or restore and verify last-good; and
6. resume the same WebUI session IDs.

The 300-second limit is the operator-specified maximum. The design introduces
no token quota, arbitrary thread-count limit, or model-selected timeout.

## Scope and terminology

An **active thread** is one unique WebUI `session_id` owned by an exact live
`ACTIVE_RUNS` entry when the fenced release snapshot is taken. The receipt also
binds its `stream_id`, backend, process identity, and, when present, Gateway
`run_id`.

The initial active-thread set is immutable for the transaction. Fencing happens
before enumeration, so no newly admitted run can escape the snapshot.

The **checkpoint acknowledgement** is a terminal assistant message from the
snapshotted run containing the exact transaction-bound marker requested by the
controller. The original stream must also have released ownership. The
controller-authored request never counts as an acknowledgement.

The **resume** is a new, explicitly release-owned turn in the same session. It
does not claim to preserve an in-flight provider call across process death.

## Non-goals

- No online model call by the release controller.
- No model choice by deterministic build, test, promotion, health, rollback, or
  retention stages.
- No host-wide process kill, wildcard PID selection, or unrelated task stop.
- No claim that a provider call survives a process restart.
- No reuse of the public user-steer handler or its browser fallback semantics.
- No selector-only shortcut around the paired transaction.

## Control protocol

### Fixed checkpoint control

The signed WebUI release-control API gains a transaction-bound
`checkpoint_threads` action. It is valid only after the exact process is
fenced.

The server snapshots active runs under their existing ownership locks and sends
this fixed control through a narrow internal helper:

> Hermes release `<transaction-id>` is pending. At the next safe boundary,
> stop starting new tools or child work, persist current state, finish this
> turn, and emit exactly:
> `HERMES_RELEASE_PAUSED_V1 transaction=<transaction-id>
> session=<session-id> stream=<stream-id>`.
> Do not continue until the matching release-resume control arrives.

Delivery is backend-specific:

- process-local WebUI run: call the active agent's thread-safe `steer`;
- Gateway-owned run: call a new authenticated Gateway session-steer endpoint
  against the exact active session/run owner;
- unavailable, ambiguous, or unsupported owner: record `undelivered`; never
  report it as queued, acknowledged, or safe.

The public `/api/chat/steer` route and browser queue fallback remain unchanged.

### Status polling

`checkpoint_threads_status` accepts only the durable snapshotted identities.
For each thread it reports:

- `active`: the original exact stream still owns work;
- `acknowledged`: that stream ended and the exact marker is durable;
- `settled_without_ack`: the stream ended without the marker;
- `owner_changed`: session ownership no longer matches; or
- `unavailable`: status cannot be proved.

Only `acknowledged` is a cooperative pause success. Every other non-active state
is retained in the resume receipt so recovery can use conservative wording.

### Five-minute boundary

The controller persists the checkpoint intent and absolute UTC deadline before
the first send. It polls until either:

- every snapshotted thread is `acknowledged`; or
- the persisted deadline is reached.

A controller restart reuses the original deadline. It never grants another five
minutes.

At the deadline the controller persists the exact unresolved set and enters the
forced-service-stop path. This is an expected release transition, not a drain
timeout rollback.

## Exact service stop

The existing launchd and PID/start-token attestations remain authoritative.

For WebUI, the controller bootouts only the fenced launchd job whose PID,
process-start token, build identity, and listener ownership match the durable
receipt.

For Gateway, the controller first asks the exact Gateway owner to interrupt
remaining agents and reject new work, then uses the existing planned-stop and
clean-shutdown path. If it cannot stop within that path's bounded shutdown
window, escalation may target only the same attested Gateway PID/start token.

Before a paired state snapshot, the controller still proves:

- both old service listeners are gone;
- Gateway dispatcher ownership is held;
- no mutable Hermes worker remains; and
- the snapshot source identities are unchanged.

Failure to prove those facts is indeterminate and blocks promotion. The
five-minute rule authorizes stopping unresolved Hermes work; it does not
authorize taking a racy snapshot or killing unrelated processes.

## Durable release phases

New optional phases preserve compatibility with already-created journals:

1. `thread_checkpoint_intent`
2. `thread_checkpoint_dispatched`
3. `thread_checkpoint_closed`
4. either `threads_resume_intent_after_promotion` or
   `threads_resume_intent_after_rollback`
5. either `threads_resumed_after_promotion` or
   `threads_resumed_after_rollback`

New transactions require phases 1-3 before old-service activation/stop.
Already-durable legacy transactions that crossed `old_committed` continue under
their recorded contract rather than being reinterpreted.

Receipts contain IDs, states, timestamps, and hashes only. They never contain
cookies, API keys, full prompts, or model secrets.

Every external mutation follows intent-before-action and
completion-after-verification. A crash between them is reconciled from exact
process, session, stream, and journal evidence; ambiguous sends are not blindly
replayed.

## Resume protocol

Resume runs only after one terminal system state is verified:

- candidate pair accepted, healthy, and pair-open; or
- exact last-good pair restored and healthy.

The controller first persists a resume intent for every snapshotted
`session_id`. The signed candidate/rollback WebUI endpoint then claims each
`(transaction_id, session_id)` in a private durable ledger before starting a
turn.

The fixed hidden control says:

> Hermes release `<transaction-id>` reached verified state `<accepted|rolled
> back>`. Continue this session from its persisted checkpoint. Revalidate
> external state before repeating any side effect. Previous release pause state
> was `<acknowledged|forced|settled-without-ack>`.

The server uses the session's current configured model/provider. On Zeus this
must remain the active local model; the release system never substitutes an
online model. Deterministic stages remain model-free.

The ledger records `claimed`, `started(stream_id)`, and terminal observation.
Repeated resume requests:

- adopt the exact active resume stream;
- return the already-completed receipt; or
- fail closed as ambiguous.

They never start a second resume turn merely because the controller missed an
HTTP response.

## Rollback and recovery

- Failure before `pair_commit_intent`: complete exact rollback, verify
  last-good, then resume the snapshotted sessions with `rolled_back`.
- Failure at or after `pair_commit_intent`: keep the shared gate closed and
  roll forward the same transaction. Threads remain paused until candidate
  acceptance is verified.
- Indeterminate service identity, mutable-worker state, snapshot state, or
  resume ownership: do not claim success; retain the exact unresolved session
  list and recovery action.
- A resume failure does not roll back a healthy accepted release. The release
  result is `accepted-with-unresolved-resumes`, and another release is blocked
  until the durable resume intents are reconciled.

## Gateway companion contract

Hermes Agent/Gateway adds an authenticated session-steer control that:

- resolves exactly one active agent for the supplied session/run identity;
- calls only that agent's thread-safe `steer`;
- returns accepted, inactive, ambiguous, or unsupported;
- exposes no prompt/history/model secret; and
- is covered for `/v1/runs` and the legacy chat-completions execution path.

The paired release must include this Agent capability before WebUI enables
default checkpoint automation. Capability absence is reported honestly and
falls into the forced-stop path at the original deadline.

## Acceptance tests

At minimum:

- zero, one, and many active sessions;
- local and Gateway-owned runs;
- duplicate session rows and ownership changes;
- acknowledgement before deadline;
- natural settlement without acknowledgement;
- undelivered control;
- exact 300-second deadline across controller restart;
- forced WebUI and Gateway stop with PID-reuse rejection;
- no wildcard/unrelated process termination;
- snapshot refusal while a mutable worker remains;
- candidate success and pre-boundary rollback;
- post-boundary roll-forward;
- crash before/after every new intent and completion receipt;
- duplicate resume request without duplicate model turn;
- accepted release with one unresolved resume;
- same session IDs after restart; and
- proof that controller stages make no model/network-provider selection.

## Documentation impact

Update `ARCHITECTURE.md` with the maintenance state machine and ownership
boundaries. Update `TESTING.md` with focused release-thread continuity tests.
Do not edit `CHANGELOG.md`; provide release-note-ready wording in the eventual
PR body.
