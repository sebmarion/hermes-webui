# Managed Release Thread Continuity Design

## Status

Approved behavior. Three-pass adversarial specification review complete;
implementation-ready and awaiting user approval.

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

An **active thread** is one unique WebUI `session_id` with live ownership
evidence from `ACTIVE_RUNS`, `STREAMS` plus `STREAM_SESSION_OWNERS`, or the
matching durable `active_stream_id` registration gap. The receipt binds its
`stream_id`, backend, process identity, evidence source, and, when present,
Gateway `run_id`.

Fencing covers every WebUI and Gateway admission path, including direct
Gateway runs, legacy chat-completions, cron/background work, and child-work
forks. A durable shared pair gate is the cross-process linearization point.
Both services check that gate before creating a reservation, then each service
enters a transaction-pinned local fence and returns its fence generation.

A reservation admitted immediately before the pair gate may still become an
active run afterward. Both services therefore expose durable reservation
identities and ownership metadata, and the transaction has a growing target set
until every pre-gate reservation is either released or upgraded and enrolled.
Enrollment is idempotent by
`(transaction_id, service_generation, session_id, stream_id)`.

Target-population closure is an explicit paired state transition. Under each
service's admission lock, `checkpoint-fenced` becomes `checkpoint-stopping`;
that transition rejects even pre-gate reservation upgrades and atomically
returns the final reservation and active-owner roster. Once both services are
`checkpoint-stopping` and both final rosters are durably enrolled, no later run
can appear. A crash after only one side transitions leaves that side closed and
recovery completes the other side; it never reopens the first.

The **checkpoint acknowledgement** is a terminal assistant message from the
enrolled original run containing the exact transaction-bound marker requested
by the controller. The original stream must also have released ownership. The
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

### Paired fence and checkpoint ledger

The controller first persists a pair-fence intent and atomically creates the
shared pair gate. It then captures one wall-clock and one same-boot monotonic
observation, derives both deadlines by adding exactly 300 seconds, and invokes
the signed `begin_checkpoint` action on WebUI and Gateway with the transaction,
gate identity, fence generations, deadlines, and host boot identity.

`begin_checkpoint` is atomic with respect to local admission. It persists the
service-owned checkpoint ledger and upgrades the local fence to non-expiring
`checkpoint-fenced` before releasing the admission lock. If persistence fails,
the service stays fail-closed. No checkpoint delivery occurs until both
services report the same transaction and deadline tuple.

The service-owned ledgers persist reservation identities, enrolled stream
identities, delivery intent/result, and observed terminal state before replying.
The external release journal mirrors their receipt hashes. This lets recovery
distinguish not-sent, accepted, inactive, and ambiguous delivery after either
the controller or a service crashes.

### Fixed checkpoint control

The signed WebUI release-control API gains a transaction-bound
`checkpoint_threads` action. It is valid only after both exact services are
checkpoint-fenced for the same pair gate and deadline tuple.

The server reconciles the active-owner registries under their established lock
order, enrolls every newly observed target, and sends this fixed control through
a narrow internal helper:

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

Repeated `checkpoint_threads` calls are reconciliation calls, not new
five-minute windows: they enroll a pre-fence reservation that has just upgraded
to a run and send at most one control to each exact stream. A reservation that
has not exposed a session owner remains `pending_reservation`; it is never
silently discarded.

`checkpoint_threads_status` accepts only the durable enrolled identities and
also reports the outstanding pre-fence reservation count and whether the target
population is closed.
For each thread it reports:

- `active`: the original exact stream still owns work;
- `acknowledged`: that stream ended and the exact marker is durable;
- `settled_without_ack`: the stream ended without the marker;
- `owner_changed`: session ownership no longer matches; or
- `unavailable`: status cannot be proved.

`acknowledged` is a cooperative pause success. `settled_without_ack` is also
safe for cutover once exact ownership has been released, but it is not resumed:
starting another turn could duplicate work that already completed naturally.
Every unresolved state is retained in the receipt so recovery can use
conservative wording.

User-visible threads are only one part of the existing release barrier.
Delegations, memory commits, OAuth flows, terminals, process completions, and
other non-thread activity retain their current deterministic counters and
availability proofs. They do not receive a fabricated chat acknowledgement.
They must either drain during the same window or remain in the unresolved
activity receipt for exact-owner shutdown.

### Five-minute boundary

The wait starts at the single clock observation persisted for
`begin_checkpoint`; both deadlines equal that observation plus exactly 300
seconds. The first control send occurs only after the paired checkpoint fence is
durable, so the whole delivery-and-wait interval is inside that one window. The
controller reconciles targets and polls until either:

- the target population is closed and every enrolled thread is either
  `acknowledged` or `settled_without_ack`, every non-thread activity source is
  available, and every corresponding counter is zero; or
- the persisted deadline is reached.

Before the deadline, the controller attempts the paired
`checkpoint-stopping` transition as soon as both reservation counts are zero.
The final rosters enroll any run that won the race immediately before either
local transition. If that adds an unresolved target, controls are delivered to
it and polling continues inside the original window; admission stays closed.
At the deadline, the same transition occurs even with remaining reservations,
which are rejected and recorded as forced.

A deadline is reached when `now >= deadline`, not after the next polling
interval. A controller restart reuses both original deadlines. Expiry of either
one, a host boot-identity change, or inability to prove elapsed time means the
deadline has been reached. Clock rollback or controller restart never grants
another five minutes.

The existing 180-second runtime-fence lease is not sufficient for this
protocol. `begin_checkpoint` atomically replaces that lease with fail-closed,
transaction-pinned state as described above; there is no persistence-to-pin
gap. A managed service restart must reconstruct the shared gate and
transaction-pinned local fence before binding a listener or admitting work. The
fence can reopen only through an authenticated abort after verified rollback or
through the ordered acceptance/resume sequence below.

At the deadline the controller persists the exact unresolved set, including
any ownerless pre-fence reservations, non-zero non-thread activity, and
unavailable activity proofs. It then drives both local admission states to
`checkpoint-stopping` and persists the two final rosters before any stop
action. A reservation that tries to upgrade during this handoff is either
enrolled before that service's atomic transition or rejected and recorded as
forced; it cannot become untracked work. This population-close receipt is
`thread_checkpoint_closed`.

The controller then enters the forced-service-stop path. This is an expected
release transition, not a drain timeout rollback. Cooperative early closure
uses the same `checkpoint-stopping` handoff before planned stop, so deadline and
non-deadline paths share one race-free boundary.

## Exact service stop

The existing launchd and PID/start-token attestations remain authoritative, but
a prior observation is not sufficient authority for a later stop. The
controller holds the exclusive release/selector lock across final identity
validation and the stop operation. A generation-bound launchd target or
self-targeted planned-stop handshake is a required capability, not an
optimization.

Launchd actions may run only when the service manager proves atomically that the
exact domain/label, loaded-job generation, plist identity, PID, and process
start token still match the receipt at execution. If that primitive is absent
or any identity changes, the controller does not call `bootout`; stop is
indeterminate and promotion remains blocked.

For WebUI, the controller first uses an authenticated self-targeted stop
request bound to its PID/start token and transaction. It may bootout only the
held generation-bound managed-job identity after PID, build identity, and
listener ownership also match. A direct signal escalation is allowed only
through a platform primitive that keeps the validated process identity stable
through signal delivery. If the platform cannot provide that guarantee, or any
identity changes, escalation is indeterminate and does not signal a re-resolved
PID.

For Gateway, the controller first asks the exact checkpoint-stopping Gateway
owner to interrupt remaining agents, then uses the same held-job planned-stop
and clean-shutdown path. Escalation follows the identical point-of-use identity
rule; a numeric PID and an earlier start token alone are not enough.

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

1. `pair_checkpoint_fence_intent`
2. `pair_checkpoint_fenced`
3. `thread_checkpoint_dispatched`
4. `thread_checkpoint_stop_intent`
5. `thread_checkpoint_closed`
6. either `threads_resume_intent_after_promotion` or
   `threads_resume_intent_after_rollback`
7. either `threads_resumed_after_promotion` or
   `threads_resumed_after_rollback`

New transactions require phases 1-5 before old-service activation/stop.
Already-durable legacy transactions that crossed `old_committed` continue under
their recorded contract rather than being reinterpreted.

Receipts contain IDs, states, timestamps, and hashes only. They never contain
cookies, API keys, full prompts, or model secrets.

Every external mutation follows intent-before-action and
completion-after-verification. A crash between them is reconciled from exact
process, session, stream, and journal evidence; ambiguous sends are not blindly
replayed.

## Resume protocol

Resume preparation runs only after one service outcome is verified while the
shared pair gate is still closed:

- candidate pair healthy and ready for acceptance; or
- exact last-good pair restored and healthy.

The controller first persists a resume intent for every enrolled `session_id`
whose original stream was interrupted or acknowledged as paused. A naturally
settled stream gets a terminal no-resume receipt. If corrupt state exposes
multiple original streams for one session, the ledger folds them into one
session intent and still starts at most one resume job. The conservative order
is `unavailable/owner_changed` (hold, do not start), then `forced/interrupted`,
then `acknowledged`, then `settled_without_ack`. Any forced or acknowledged
stream requires one resume; only an all-settled set gets no resume.

Before global pair-open, the candidate/rollback WebUI atomically installs a
per-session release hold for every resume job. Normal chat admission cannot
claim a held session. A narrow release-only admission bypass accepts only the
matching signed transaction and hidden resume job; it cannot start arbitrary
user or background work.

Each ledger key binds
`(transaction_id, session_id, checkpoint_generation,
checkpoint_fingerprint)`. The release ledger allocates
`checkpoint_generation`; this does not add a revision write to every ordinary
session mutation. The fingerprint covers the enrolled old stream IDs, the
canonical durable transcript hash, active/pending sidecar identity, and pause
state. The session lock validates that fingerprint when the hold is installed
and again when the resume job is accepted. A changed session remains held and
unresolved rather than resuming against different state.

The fixed hidden control says:

> Hermes release `<transaction-id>` reached verified state
> `<candidate-verified|last-good-restored>`. Continue this session from its
> persisted checkpoint. Revalidate
> external state before repeating any side effect. Previous release pause state
> was `<acknowledged|forced|owner-reconciled>`.

Every managed deployment must carry a signed execution-policy manifest bound to
the attested host identity. Its value is exactly `zeus-local-only` or
`standard`; missing, unreadable, invalid, conflicting, or host-mismatched policy
means no model-driven resume may start. There is no inferred "outside Zeus"
fallback.

Only a positively verified `standard` deployment may use the session's current
configured provider. On a positively verified `zeus-local-only` deployment,
the resume endpoint asks the existing Zeus local-model registry for the
currently active route; it does not select a model itself.

The returned route is an attested transport object, not a URL that is later
resolved again. It permits only a Unix socket or loopback listener owned by the
recorded local model-server PID/start token and model fingerprint. Dispatch
disables environment proxies, redirects, DNS targets, provider fallback, and
online retry. The connected route is revalidated at dispatch; replacement
causes a local-route failure and leaves the job held. Thus a local proxy cannot
silently forward the resume online. Deterministic stages remain model-free.

The ledger preallocates a deterministic `resume_job_id`, attempt number, and
`stream_id`, then persists `launch_intent` before creating the worker. The
worker must, under the session lock, register that exact stream and durably
write `worker_accepted` before any model call, tool call, or side effect.
Terminal state is persisted before stream ownership and the per-session hold
are released.

Recovery has four evidence-driven cases:

- no `worker_accepted`: start the same preallocated attempt;
- exact stream still active: adopt it;
- terminal run-journal evidence: mark the job terminal and return it; or
- `worker_accepted` with a provably dead process and no terminal event: mark
  that attempt `interrupted`, but do not allocate another attempt yet.

For the MVP, a new attempt is permitted only after the existing full release
activity barrier is available and proves zero live tools, child agents,
delegations, terminals, memory commits, and process-completion work, and any
durable completion events are reconciled into the session. This conservative
global barrier is intentionally reused instead of adding owner tags throughout
the runtime. If any source is unavailable or non-zero, the session stays held
and unresolved. Only after that barrier may the ledger allocate the next
attempt and continue with the fixed revalidation warning.

Inconsistent process/session/journal evidence remains held for explicit
reconciliation; it is never blindly replayed. A missed HTTP response alone
therefore cannot start a second attempt.

Once every resume job is terminal, worker-accepted, or durably held as
unresolved, the controller records candidate acceptance or verified rollback
and opens the global pair gate. Started jobs keep their per-session hold until
terminal, and unresolved jobs keep it until exact reconciliation or an explicit
operator abandonment receipt. Other sessions can admit ordinary work after
pair-open without racing a pending resume.

## Rollback and recovery

- Failure before `pair_commit_intent`: keep the shared gate closed, complete
  exact rollback into transaction-fenced startup, verify last-good, install
  resume holds/intents with `rolled_back`, start or hold each job through the
  release-only bypass, then pair-open in that order.
- Failure at or after `pair_commit_intent`: keep the shared gate closed and
  roll forward the same transaction. Verify the candidate, install
  resume holds/intents with `accepted`, start or hold each job through the
  release-only bypass, then pair-open in that order.
- A restarted candidate or last-good process restores the shared gate,
  checkpoint transaction, and session holds before binding its public listener;
  startup recovery cannot admit ordinary work between service health and
  resume preparation.
- Indeterminate service identity, mutable-worker state, snapshot state, or
  resume ownership: do not claim success; retain the exact unresolved session
  list and recovery action.
- A resume failure does not roll back a healthy accepted release. The release
  result is `accepted-with-unresolved-resumes`, and another release is blocked
  until the durable resume intents are reconciled.

## Gateway companion contract

Hermes Agent/Gateway adds an authenticated session-steer control that:

- checks the same durable pair gate before every direct or delegated admission;
- records transferable reservation identities until active-agent registration;
- resolves exactly one active agent for the supplied session/run identity;
- calls only that agent's thread-safe `steer`;
- returns accepted, inactive, ambiguous, or unsupported;
- exposes no prompt/history/model secret; and
- is covered for `/v1/runs` and the legacy chat-completions execution path.

Its signed release endpoint implements the same
`begin_checkpoint -> checkpoint-fenced -> checkpoint-stopping` state machine as
WebUI and restores that state before startup admission. WebUI cannot declare
the paired target population closed from its own counters alone.

The candidate pair must include this Agent capability and a verified
process-completion/checkpoint activity adapter before pair-open. The currently
running old pair is not required to have either capability: missing old-side
support is recorded as `unsupported` or unavailable, waits only until the
original deadline, and then uses exact-owner shutdown. This removes the
bootstrap deadlock where the compatibility fix could not be released because
the old runtime did not already contain it.

Candidate health must report both capabilities as available before acceptance.
Their absence in the candidate is a rollback/roll-forward failure, never a
reason to silently disable the new default after promotion.

## First activation from a legacy pair

The pair running before this feature is deployed cannot be assumed to support
transaction-pinned fencing or authenticated Gateway steering. The controller
detects those capabilities from signed evidence; it does not pretend they
exist.

For this one transition, it durably records `legacy_bootstrap`, first engages
the old mechanisms that do not auto-expire—the existing durable WebUI pair-open
gate and the Gateway's exact-owner external drain marker—and only then sends
the fixed checkpoint control through every owner path the old pair can prove.
The runtime
WebUI fence is additional identity evidence, but its 180-second lease is not
the admission-safety boundary. Signed inspection must prove the pair gate still
blocks WebUI reservations and the drain marker still blocks Gateway turns
through the stop handoff. The legacy wait therefore uses the same exact
300-second deadline without reopening and re-fencing. Unsupported owners remain
explicit in the unresolved receipt.

Legacy journals use explicit alternate predecessors:
`legacy_checkpoint_gate_intent -> legacy_checkpoint_gated ->
legacy_checkpoint_dispatched -> legacy_checkpoint_stop_intent ->
legacy_checkpoint_closed`. The last phase satisfies the standard
`thread_checkpoint_closed` predecessor only for a transaction whose signed
capability probe proved legacy mode before the first mutation. A candidate that
advertises the full capability set cannot enter this branch.

The controller then stops only the attested old pair, starts the candidate
startup-fenced, and completes the normal snapshot, verification, acceptance,
and resume protocol. Pair-open is forbidden unless the candidate proves the
full pinned-fence, Gateway-steer, activity, and resume capabilities. Therefore
the legacy exception is usable only to install the implementation; every later
release uses the default 300-second protocol.

## Token and model-work control

The release controller, polling, health checks, artifact work, state transfer,
promotion, and rollback consume zero model tokens. Each exact active stream gets
at most one fixed checkpoint control; reconciliation never resends merely
because a response was missed. A naturally settled session gets no resume turn.
An interrupted or acknowledged session has at most one active local resume
attempt, and a later attempt is allowed only after durable proof that the prior
worker process died.

Receipts expose control-send counts, resume-attempt counts, and locally reported
input/output token usage when the active model server provides it. These are
observability values, not invented release quotas. No research loop or model
review runs in the release transaction.

## Acceptance tests

Every scenario asserts observable outcomes, not only a returned status:

- fence races end with the work durably enrolled before
  `checkpoint-stopping` or rejected after it, never admitted untracked;
- journal recovery preserves one deadline tuple and legal predecessor order
  across every injected crash;
- resume recovery has at most one `worker_accepted` live attempt and cannot
  retry a dead attempt until the full activity barrier proves zero;
- identity mismatch at stop produces no signal/bootout and no promotion, while
  a matching stop proves both exact listeners and mutable workers gone; and
- Zeus-local tests prove zero proxy, redirect, DNS, fallback, or remote network
  dispatch.

The scenario matrix includes at minimum:

- zero, one, and many active sessions;
- local and Gateway-owned runs;
- simultaneous WebUI and direct-Gateway admission at the shared-gate boundary;
- crash after pair-gate creation and after either service pins its local fence;
- a pre-fence reservation that upgrades after the first reconciliation;
- an ownerless pre-fence reservation that survives until the deadline;
- reservation upgrade racing the atomic `checkpoint-stopping` transition;
- disagreement among `ACTIVE_RUNS`, `STREAMS` ownership, and durable
  `active_stream_id` evidence;
- non-thread activity that drains early, remains active, or is unavailable;
- duplicate session rows and ownership changes;
- acknowledgement before deadline;
- natural settlement without acknowledgement;
- undelivered control;
- exact 300-second deadline across controller restart;
- wall-clock rollback and host reboot without deadline extension;
- transaction-pinned fence surviving the old 180-second lease boundary;
- forced WebUI and Gateway stop with PID-reuse rejection;
- managed-job/process replacement between inspection and stop;
- no wildcard/unrelated process termination;
- snapshot refusal while a mutable worker remains;
- candidate success and pre-boundary rollback;
- post-boundary roll-forward;
- crash before/after every new intent and completion receipt;
- duplicate resume request without duplicate model turn;
- crash before and after `worker_accepted`, with evidence-driven recovery;
- normal chat racing resume preparation for the same session;
- checkpoint fingerprint/session-generation mismatch;
- accepted release with one unresolved resume;
- same session IDs after restart;
- proof that controller stages make no model/network-provider selection;
- proof that Zeus resume rejects redirects, proxies, remote DNS, owner
  replacement, and provider fallback;
- no resume turn for a stream that settled naturally without acknowledgement;
- one legacy bootstrap followed by rejection of that path once the candidate
  advertises the full capability set.

## Documentation impact

Update `ARCHITECTURE.md` with the maintenance state machine and ownership
boundaries. Update `TESTING.md` with focused release-thread continuity tests.
Do not edit `CHANGELOG.md`; provide release-note-ready wording in the eventual
PR body.
