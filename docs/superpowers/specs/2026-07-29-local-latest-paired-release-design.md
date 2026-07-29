# Local-Latest Paired Hermes Release Design

## Status

This design is ready for implementation planning. Production execution remains
blocked until the Stage 0 preconditions pass. The current r75 WebUI/r72 gateway
split cannot be represented by the existing cutover-plan loader, so r76 must
not be attempted and no Codex task may be steered until Stage 0 is complete.

## Objective

Deploy the newest proven release-line version of Seb's own Hermes WebUI and
Hermes Agent work as a consistent pair, then establish a local release lineage
that can incorporate the rest of the intended local work without fetching,
merging, rebasing, or otherwise importing upstream changes.

Every accepted production release must:

- update WebUI and gateway/Agent through the full paired transaction;
- keep exactly one previous verified rollback unit;
- run rolling retention synchronously after release, without a cron or
  background cleanup agent;
- cooperatively steer visible active Codex tasks when the Codex app control
  surface is available;
- never kill a Codex task or an unrelated host process; and
- recover according to the transaction's real irreversible boundary.

## Non-Goals

- No upstream fetch, merge, rebase, pull, or update.
- No selector-only promotion or rollback during an ordinary release.
- No claim that a timestamp identifies the latest local version.
- No claim that the current Codex task API can exhaustively enumerate or fence
  every local task.
- No always-running release coordinator, cleanup daemon, or new cron.
- No deletion of an unclassifiable rollback payload.

## Current State

- The selector is idle at generation 188 with WebUI r75 as both `current` and
  `last_good`.
- The live WebUI is r75 at commit `afa07ff8`.
- The live gateway still identifies as the r72 pair at WebUI commit
  `f51d2e12`, even though r72 and r75 use the same pinned Agent commit,
  `8fbefbe5`.
- The newest proven forward release-line WebUI commit is `48a0b7f8`, which is
  exactly r75 plus synchronous one-rollback retention.
- WebUI local `main`, the r75/r76 release line, other local heads, and dirty
  worktrees are not one history. The Agent repository has the same class of
  divergence.
- Neither repository currently has `refs/heads/release/current`.
- The full paired `release-commit` transaction is the production deployment
  primitive. Selector-only operations remain recovery/debug primitives.

## Terminology and Authoritative Invariants

### Accepted source pair

`release/current` in each repository names the source commit of the latest
accepted paired production release:

- `hermes-webui:refs/heads/release/current`
- `hermes-agent:refs/heads/release/current`

The two refs are authoritative only together with a durable pair-authority
receipt that records both repository identities, commits, trees, and the
accepted production pair ID. A lone or partially advanced ref is not an
authoritative pair.

### Prepared source pair

A future candidate is first represented by transaction-owned prepared refs or
detached object IDs plus a durable prepared-pair receipt. Raw arbitrary commit
arguments are not accepted by the normal production command.

Each build checkout must prove:

- the expected canonical Git common directory and repository identity;
- `HEAD` equals the captured candidate object ID;
- the captured tree equals the expected tree;
- porcelain status is clean, including untracked files;
- the ref/object identity has not changed since its gate receipt; and
- all test and artifact receipts bind the same commits and trees.

### Local integration frontier

"Our latest" means the prepared pair plus an operator-approved local integration
frontier, not merely the tips of two conveniently named branches.

Before preparing a pair, inventory:

- every local branch head;
- every linked worktree and its `HEAD`;
- dirty, staged, and untracked work;
- the previous `release/current` pair; and
- the local `main` tips.

Each inventory item is classified in a durable inclusion/exclusion ledger as:

- `included`: its history or intentionally reproduced behavior is in the
  candidate;
- `excluded-release-later`: real local work intentionally deferred;
- `scratch`: not intended for Hermes production; or
- `uncommitted`: preserved but not eligible for release.

The ledger requires explicit operator approval. A release described as the
combined local latest must contain every `included` head as an ancestor, or
provide a commit-by-commit reproduction/omission record when a non-ancestral
reconciliation is deliberate. Unclassified items fail the source gate.

The approved ledger includes a canonical `frontier_digest` over repository
common-directory identities, every inventoried ref and object ID, ref/reflog
update cursors where available, worktree `HEAD` values, and path-plus-content
hashes for dirty, staged, and untracked files. The controller re-creates and
compares that digest:

- immediately before prepared-pair creation; and
- immediately before persisting `release-intent`.

Any drift, including a ref advance-and-reset detected by its update cursor,
invalidates source, test, build, and approval gates. The changed frontier must
be inventoried and approved again; a prior exclusion is not permission for its
contents to change silently.

### Rollback unit

One rollback unit means one newest verified terminal snapshot root and every
payload descriptor required by that root's authoritative journals. A rollback
unit may contain separate WebUI and paired-state payload trees. The invariant
is one retained rollback root/unit and zero bulk payload trees belonging to
older or abandoned roots.

Small journals, manifests, and receipts are retained for audit. Only bulk
rollback payloads are rotated.

## Delivery Sequence

### Stage 0: Make the Current Split State Representable

Stage 0 is a code-and-test prerequisite and performs no production cutover.

#### Split last-good identity

Extend the cutover plan schema and loader so
`last_good_gateway_identity_json` may describe the exact live r72 gateway while
`last_good_identity_json` describes the exact live r75 WebUI.

The loader and one shared pure attester apply this field matrix:

| Field group | r75 WebUI authority | r72 gateway authority | Cross-identity rule |
| --- | --- | --- | --- |
| WebUI build ID, commit, tree, release path, manifest | r75 selector record, sealed release, WebUI health, installed WebUI process | r72 sealed release plus gateway plist, gateway health, and gateway process | May differ; each must match its own authorities exactly |
| Selector generation, release transaction, pair ID | r75 selector and r75 transaction receipts | r72 values embedded in the installed gateway and its originating r72 journal | Independent service provenance; never require the r72 originating transaction to equal r75's |
| Launchd label, arguments, cwd, PID/start token | Installed WebUI job and process | Installed gateway job and process | Service-specific and exact; never copied from the peer |
| Agent source path, commit, tree, manifest | r75 sealed identity | r72 gateway plist, health, and sealed identity | Must be byte-for-byte equal for the known live split |
| Runtime path, runtime manifest, interpreter, Python home, site-packages | r75 sealed identity | r72 gateway plist, health, and sealed identity | Must be byte-for-byte equal for the known live split |
| Selector binary path/resolved path and selector verification evidence | r75 sealed identity and installed selector | r72 sealed identity and installed selector | Must identify the same currently installed immutable selector |

The r72 originating transaction is validated against the r72 gateway's own
plist, health identity, sealed manifest, selector-generation evidence, and
journal. It is not compared to the r75 release transaction.

The plan binds both origin journals independently:

- absolute r75 WebUI origin-journal path and SHA-256; and
- absolute r72 gateway origin-journal path and SHA-256.

Each path must resolve below the trusted private reliability root to a regular,
non-symlink file with the expected owner, mode, schema, and transaction ID. The
attester verifies its hash before parsing it. If either trusted origin journal
cannot be located and bound deterministically, Stage 0 fails; no caller may
substitute an unbound receipt or infer provenance from a process title.

The pure attester returns one immutable evidence object and performs no journal
write. Both dry run and mutating execution call that exact attester. Mutating
execution may then record the returned evidence in its journal; it may not use a
different validation path.

Add an end-to-end fixture whose selector/WebUI is r75 and gateway is r72. Prove:

- the real loader accepts the exact split identities;
- each field group above is independently rejected when its authority changes;
- mismatched shared Agent, runtime, interpreter, or selector identities fail;
- substituting the r75 transaction for the r72 origin fails;
- dry run and the live-path caller compare equivalent pure-attester evidence
  before the caller writes any journal phase;
- the shared attester itself mutates no selector, service, or transaction
  state; only the live-path caller may persist its returned evidence afterward;
  and
- the full transaction can roll back to the exact split pair before its durable
  commit boundary.

#### Post-boundary replay safety

Before r76, make every operation at or after `pair_commit_intent`
crash-replayable.

1. Enumerate the complete ordered deferred-step manifest, including every
   mutation currently hidden inside candidate acceptance as well as gateway
   opening, deferred WebUI startup, background-service startup, full open
   health, shared-gate release/open, and watchdog restoration.
2. Version and hash that manifest. Bind its version/hash into the candidate,
   plan, `release-intent`, and transaction journal before
   `pair_commit_intent`.
3. Make `deferred_manifest_bound` and `pair_gate_installed` explicit
   prerequisites of `pair_commit_intent` in the validated transaction-journal
   phase graph. A journal missing either predecessor is rejected.
4. For every mutation, persist a per-step intent before execution and a
   completion receipt after execution. Alternatively, document and test a
   verifiable idempotence/reconciliation rule that is equivalent to those
   receipts.
5. Test three crash cuts for every step:
   - intent fsynced, before the mutation;
   - mutation completed, before its completion receipt; and
   - completion receipt fsynced, before the next step.
6. On replay, reconcile external evidence:
   - proved complete: persist/recover completion without re-execution;
   - proved absent: retry the mutation under the same intent; or
   - partial or ambiguous: stop `indeterminate`.
   A completed step is never blindly repeated.
7. A non-retryable post-boundary failure becomes `indeterminate`; rollback
   remains forbidden, task acknowledgements remain paused, and the receipt
   identifies the failed step and operator repair.
8. Apply the same three crash cuts to candidate-accept intent/completion,
   accepted/open-health proof, gate release/open, and watchdog restoration.
   Re-running the same transaction must either reach the exact accepted pair
   once or stop indeterminate without duplicating a mutation.

Finally, run the new pure preflight against the live installed state before any
task steering. Record the verified live r75/r72 identities and the supported
deferred-step manifest in the deployment receipt.

If an exact split-state representation cannot be implemented and tested, stop.
If post-boundary replay safety cannot be implemented and tested, also stop. Do
not repair either gap with raw selector moves or an undocumented manual service
replacement.

### Stage 1: Immediate Consistent r76

Stage 1 is a bootstrap exception because no accepted `release/current` pair
exists yet.

Prepare clean detached worktrees at:

- WebUI `48a0b7f8`;
- Agent `8fbefbe5`; and
- the exact immutable runtime captured during Stage 0.

The r76 bootstrap receipt must name the runtime manifest SHA-256, resolved
immutable runtime path, interpreter identity, Agent-source manifest, WebUI
manifest, commits, and trees. "The already verified runtime" is not a valid
unbound input.

Before building, create the local integration-frontier inventory. For r76,
record all divergent local-main, other-head, and dirty/untracked work as
explicitly deferred or preserved. Therefore r76 is described as the newest
proven release-line pair, not yet as the complete combined local latest.

After Stage 0 preflight, required gates, cooperative steering, and the full
paired transaction:

1. verify WebUI and gateway report one r76 pair;
2. invoke the rolling-retention cleaner in the foreground and wait for its
   completed or failed receipt;
3. create both `release/current` refs using expected-absent compare-and-swap;
4. write the pair-authority receipt only after both refs read back correctly;
5. resume or precisely report steered tasks; and
6. block every later release until any partial ref update is reconciled.

After the production pair is accepted, ref recovery converges forward only to
that accepted pair:

- if both refs equal the accepted object IDs, write or recover the missing
  pair-authority receipt;
- if both refs still equal their expected old values (absent for r76), advance
  them one at a time with compare-and-swap and apply these rules again;
- if exactly one ref equals its accepted object ID and the other still equals
  its expected old value (absent for r76), advance only the latter with
  compare-and-swap;
- if either ref has any other value, never overwrite it, leave the source pair
  non-authoritative, and require operator reconciliation.

Tests inject crashes before the first ref update, after each individual update,
after both read back but before the authority receipt, and after the authority
receipt is fsynced but before it is linked into the deployment receipt. They
also race each compare-and-swap with a concurrent ref change and prove that
divergence is never overwritten.

Failure to create the paired refs does not invalidate an otherwise healthy r76,
but it leaves source authority incomplete and blocks every later release.

### Stage 2: Canonical Local r77

Create clean integration worktrees and reconcile the operator-approved local
integration frontier. At minimum this includes:

- the accepted WebUI r76 tip;
- WebUI local `main`;
- every additional WebUI head classified `included`;
- the accepted Agent `8fbefbe5` tip;
- Agent local `main`; and
- every additional Agent head classified `included`.

Snapshot the exact old `release/current`, `main`, and included-head object IDs.
The new prepared tips must contain the accepted release tips and every included
head as ancestors unless the approved ledger documents a deliberate
non-ancestral reproduction.

Conflicts are resolved by behavior, tests, and release invariants, never by
timestamp. After the full paired r77 is accepted, advance both
`release/current` refs with expected-old compare-and-swap and issue a new
pair-authority receipt. A partial update is non-authoritative and must be
reconciled by the same forward-only rule before another release.

### Stage 3: Reusable Release Interface

The reusable interface has two explicit layers.

1. **Core paired-release command.** A local foreground command resolves one
   prepared-pair receipt, verifies source/artifact identities, runs preflight,
   invokes the full paired transaction, reconciles synchronous retention,
   converges the paired `release/current` refs, and emits one machine-readable
   deployment receipt. It does not depend on Codex app task tools and never
   accepts arbitrary production commits by default.
2. **Codex task-continuity orchestrator.** The current Codex coordinator may use
   app tools to steer visible active tasks, invoke the core command, and resume
   those tasks. This is an invoked release workflow, not a resident agent,
   daemon, or cron.

Cleanup belongs to the core command and therefore occurs after every accepted
release even when no Codex tasks are involved.

A future single fully automated action may combine both layers only after a
supported Codex host adapter satisfies the exhaustive task-control contract
defined below. Until then, documentation must call Stage 1 steering
operator-managed and bounded, not exhaustive.

## Cooperative Task Steering

### Safety boundary versus continuity aid

The release transaction's admission fence and activity drain are authoritative
for Hermes safety. They cover Hermes streams, delegations, processes, memory
commits, OAuth work, terminal activity, and undelivered completions.

Codex steering is a continuity aid: it asks visible tasks to checkpoint before
the restart. With the current app surface it cannot prove that every local task
has been enumerated or prevent a new task from activating after a scan.

The immediate r76 receipt must therefore record:

- the app schema version and host availability;
- pinned count and non-pinned count;
- the current non-pinned limit of 50;
- whether the result hit that limit;
- every visible active local Codex task selected;
- the exact coordinator `(hostId, threadId)` excluded; and
- `enumeration_complete=false` whenever completeness cannot be proven.

Proceeding with `enumeration_complete=false` requires an explicit
bounded-visibility operator acknowledgement in the receipt. It never weakens
the release transaction's own fence/drain gates.

### No task killing

No Codex task is sent a signal or killed. A task that does not acknowledge
causes the steering phase to abort; it is reported for manual recovery.

This does not prohibit the paired transaction from stopping its two exact
managed service processes. Existing identity-authorized escalation after
launchd bootout and durable drain is a service lifecycle operation, not a
Codex-task kill. Unrelated Node servers, Cloudflare tunnels, and other host
processes remain outside the target.

### Immediate r76 selection

1. Acquire a global single-writer deployment lease.
2. Generate the deployment ID and bind the coordinator to exact
   `(hostId, threadId)`.
3. List the maximum visible task window plus all pinned tasks.
4. Select entries with `kind=codex`, `hostId=local`, and `status=active`,
   excluding only the exact coordinator identity.
5. Deduplicate by `(hostId, threadId)`.
6. Persist the initial receipt before sending any message.
7. Rescan once immediately before `release-commit` and steer any newly visible
   active task. Do not describe this rescan as a task-admission fence.

Unknown task statuses, unavailable hosts, or unavailable sources are recorded.
If an unknown status could be nonterminal, automated steering refuses and the
operator decides whether to continue under the bounded-visibility exception.

### Pause request and acknowledgement

For each selected task:

1. Record its title, cwd/project, host ID, thread ID, pre-pause status, model
   and reasoning settings when exposed, and the exact pre-send read/wait cursor.
2. Persist `pause-intent` with monotonically increasing `command_seq`.
3. Send one pause request:

   > Hermes deployment `<deployment-id>`, command `<command-seq>`, is preparing
   > a restart. At your next safe boundary, stop starting new work, finish or
   > checkpoint tools and child work, persist the checkpoint, then yield one
   > assistant-authored final response containing this single-line marker:
   > `HERMES_DEPLOY_PAUSED_V2 deployment=<deployment-id> command=<command-seq>
   > thread=<thread-id> checkpoint=<checkpoint-id> quiescent=true`.
   > Do not resume until a later command for this deployment tells you its
   > verified terminal state.

4. Persist `pause-sent` and the transport result.
5. Wait from the pre-send cursor, then re-read the thread.
6. Accept an acknowledgement only when it is:
   - a new assistant-authored terminal event after the pre-send cursor;
   - an exact match for deployment ID, command sequence, and thread ID;
   - accompanied by a non-empty checkpoint ID; and
   - contains exactly `quiescent=true`, asserting that no turn, tool, child
     work, or automatic continuation remains active to the task's knowledge.

The occurrence of the marker in the controller-authored request never counts.
If role, cursor, or terminal-event provenance cannot be established, the task
is not acknowledged.

The default pause timeout is 300 seconds. A timeout aborts before
`release-intent`. Acknowledged tasks receive one abort/resume command. A task
whose send or acknowledgement state is ambiguous is not sent repeated automatic
commands; it remains an explicit manual-reconciliation item.

### Current transport limitations

The current task-message API exposes no idempotency key, durable host pause
latch, exhaustive pagination, host revision, or task-admission lease.
Accordingly:

- immediate r76 does not claim automatic exactly-once pause/resume;
- ambiguous sends are never automatically replayed;
- natural-language `command_seq` is diagnostic, not a transport guarantee;
- crash recovery may require operator reconciliation; and
- the core release command remains usable without Codex steering.

### Contract for future exhaustive automation

Fully automated task steering remains disabled until a versioned Codex host
adapter provides and passes integration tests for:

- exhaustive pagination plus a total count under one host revision;
- a stable host epoch and exact task identity;
- a host admission lease that fences task creation, activation, queued-message
  delivery, and automation wakeups through release terminal state;
- a complete nonterminal/wakeable status lattice;
- transport idempotency keys and per-task ordered command sequences;
- delivery and processed acknowledgements;
- a host-enforced paused/unpaused latch covering tool and child-task trees;
- cursor semantics across reconnects;
- authentication and protocol-version negotiation; and
- deterministic behavior across app restart and host epoch change.

If any capability is absent, the combined automated action refuses task
steering rather than claiming exhaustive quiescence.

## Durable Receipt and Recovery

### Storage contract

Deployment receipts live under the private reliability root in
`deployment-receipts/`. They contain identities and lifecycle state, not prompt
bodies, credentials, cookies, or model secrets.

Every write uses the existing durable-journal pattern:

1. refuse symlinks, wrong owner/mode, oversized data, unknown schema versions,
   invalid phase transitions, or corrupt JSON;
2. create a private `0600` temporary file in the destination directory;
3. write, flush, and fsync the temporary file;
4. atomically replace the receipt;
5. fsync the parent directory; and
6. read back and validate the new state.

The outermost release entry point acquires one durable logical deployment lease:

- the task-continuity orchestrator acquires it before its first task selection;
- the core command acquires it before source resolution when invoked without
  the orchestrator; or
- the core command validates and adopts the orchestrator's exact deployment ID,
  owner nonce, and lease receipt when invoked as its child.

The core command never bypasses or creates a second lease. The one lease is held
through release, retention, source-ref reconciliation, authority-receipt
persistence, and terminal task reconciliation when applicable. No second
deployment begins while an incomplete receipt or nonterminal release journal
exists. An outstanding retention obligation or non-authoritative
`release/current` pair also blocks the next production cutover, although either
may be repaired without restarting services.

The lease receipt binds deployment ID and owner nonce plus one exact owner:

- orchestrated: Codex host epoch, host ID, and coordinator thread ID; or
- standalone: host boot identity, PID, and PID start token.

Recovery may adopt a stale lease only after proving the exact process is absent
or the exact coordinator is no longer active. If the Codex control surface is
unavailable, adoption requires explicit operator recovery and is recorded. The
same deployment ID is retained with a new owner nonce. A lease never expires or
is stolen by wall-clock timeout alone.

### Handoff to the release transaction

Before invoking `release-commit`, persist:

- phase `release-intent`;
- transaction ID;
- exact plan path and plan SHA-256;
- exact transaction-journal path;
- selector path and pre-release generation;
- source, artifact, and runtime identities;
- the revalidated `frontier_digest`;
- the deferred-step manifest version and SHA-256;
- `retention_required=true`; and
- the acknowledged and unresolved task sets.

`release-intent` always means the release may have started. Recovery must
inspect and reconcile the release journal and selector before sending any task
resume message, even if no child-process result was recorded.

The coordinator is a foreground operation independent of the WebUI and gateway
services. No new supervisor, daemon, or cron is added. If the coordinator or
machine dies, possibly paused tasks remain fail-closed until the next explicit
release/recovery invocation reconciles the receipt. Automatic recovery after a
host reboot is not claimed.

### Recovery order

On every release or recovery invocation:

1. acquire the global deployment lease;
2. reconcile any older deployment receipt that binds an accepted transaction,
   has `retention_required=true`, and has no matching completed cleanup receipt
   before admitting a new cutover;
3. locate the newest incomplete deployment receipt;
4. if no `release-intent` exists, reconcile task messages and close the
   deployment as aborted;
5. if `release-intent` exists, reconcile the exact transaction journal and
   selector before touching tasks;
6. if the transaction is before `pair_commit_intent`, finish its exact rollback
   or continue the same transaction according to its journal;
7. if `pair_commit_intent` exists, verify the bound deferred-step manifest,
   reconcile every intent-without-completion from external evidence, and rerun
   the same transaction to roll forward;
8. verify either the accepted candidate pair or an exact pre-commit rollback;
9. invoke and record retention when the candidate is accepted;
10. under the same deployment lease, reconcile the forward-only
    `release/current` compare-and-swap operations and pair-authority receipt
    when the candidate is accepted; and
11. only then terminalize the deployment and resume/reconcile tasks.

If neither candidate acceptance nor exact pre-commit rollback can be verified,
mark the deployment `indeterminate`, keep acknowledged tasks paused, and report
the receipt and task IDs for operator recovery.

If the Codex control plane is unavailable or its host epoch changed, release
state is reconciled first, task state stays unresolved, and no task is inferred
to be resumed.

## Paired Release Transaction

For r76, r77, and later releases:

1. Prove clean detached source worktrees, create the approved integration
   frontier digest, and bind it to the prepared pair.
2. Run release, selector, retention, split-state, and feature-specific tests.
3. Build or reuse exact immutable Agent and runtime artifacts.
4. Build and verify the sealed WebUI release.
5. Generate a plan bound to the prepared-pair and artifact receipts.
6. Run the full non-mutating preflight, including live WebUI and gateway
   last-good attestation.
7. Cooperatively steer visible tasks under the applicable bounded or exhaustive
   task-control contract.
8. Revalidate the complete frontier digest. Drift aborts before release,
   invalidates prior source/test/build gates, and requires new operator
   approval.
9. Persist `release-intent`.
10. Run `release-commit`, whose actual lifecycle is:
   - fence new Hermes work;
   - drain authoritative activity;
   - snapshot paired state;
   - stop only the exact authorized WebUI and gateway services;
   - start the candidate behind admission fences;
   - prove immutable identity and mutation-free startup-fenced health;
   - bind the deferred-step manifest, prepare the pair, and install the shared
     gate;
   - require durable `deferred_manifest_bound` and `pair_gate_installed`
     predecessor phases;
   - durably record `pair_commit_intent`;
   - promote the selector;
   - open the gateway behind the still-closed shared pair gate;
   - durably record candidate-accept intent;
   - invoke WebUI acceptance, which reconciles the bound ordered deferred
     mutations and starts required background services before returning open;
   - durably record `candidate_accepted` only after acceptance returns open;
   - run and record full open-health checks;
   - open the shared pair gate; and
   - restore watchdog scheduling.
11. Verify WebUI and gateway report the same build, generation, pair ID, WebUI
    commit, Agent commit, and runtime identity.
12. Reconcile synchronous rolling retention.
13. Reconcile the forward-only paired `release/current` updates and durable
    pair-authority receipt.
14. Resume or report steered tasks.

### Failure semantics

- Before `pair_commit_intent`, the transaction may use its exact rollback.
- At or after `pair_commit_intent`, rollback callbacks are forbidden. Recovery
  validates the bound deferred-step manifest and reruns the same transaction to
  roll forward.
- Startup-fenced health before the boundary proves immutable identity,
  mutation-free admission, stream/runtime health, and explicitly deferred
  mutable-state checks.
- Deferred sessions/projects/state-database startup, background services, and
  full open health are verified only after promotion.
- Every post-boundary mutation has durable intent/completion evidence or an
  equivalent tested idempotence/reconciliation rule. An unrecognized manifest
  or non-retryable step failure is indeterminate, never silently skipped.
- Tasks remain paused while an at/after-boundary transaction is nonterminal.
- Selector-only rollback is never substituted for transaction recovery.

## Synchronous Rolling Retention

Before `release-intent`, the deployment receipt records
`retention_required=true`. That flag plus the exact accepted transaction linked
by the same deployment receipt is itself a durable cleanup obligation; recovery
does not depend on a later `retention-pending` write. On the normal path,
immediately after acceptance the foreground controller persists
deployment-receipt phase `retention-pending`, invokes the cleaner, and waits for
its completed or failed receipt before reporting the deployment outcome.

The cleaner:

- derives its plan from the locked managed selector/control pair;
- retains the newest verified terminal rollback unit;
- deletes bulk payload trees belonging to older terminal or abandoned units
  only when all required journals and companion descriptors validate;
- refuses ambiguous, symlinked, corrupt, or unclassifiable targets;
- writes a durable cleanup receipt bound to deployment ID, transaction ID,
  accepted pair ID, selector generation, and selector-state digest; and
- is safe to rerun with the same accepted selector state.

After a crash, the next explicit invocation treats either of these states as
`retention-pending` and reruns cleanup before beginning another release:

- a deployment receipt bound to an accepted transaction with phase
  `retention-pending`; or
- a deployment receipt bound to an accepted transaction with
  `retention_required=true` and no exactly matching completed cleanup receipt,
  including a crash before `retention-pending` was persisted.

Recovery links a cleanup receipt only when all five join fields match the
deployment receipt and current accepted selector evidence. A missing or
mismatched join causes a safe cleanup rerun; it is never accepted by filename or
recency alone.

No cron or resident agent is involved.

A retention failure does not roll back a healthy accepted release and does not
keep tasks paused. The deployment outcome is
`accepted-with-retention-warning`, not fully accepted against the storage
criterion. The receipt names the failed cleanup and retry action. The durable
retention obligation blocks every later production cutover until a foreground
retry verifies exactly one rollback unit. Any unclassifiable bulk payload makes
the storage criterion fail; it is preserved for operator diagnosis, never
silently ignored or deleted.

Crash tests cover:

- acceptance before `retention-pending`;
- `retention-pending` before cleaner invocation;
- cleaner completion before linkage into the deployment receipt;
- failed cleanup followed by task resume;
- retry before the next cutover; and
- an unclassifiable payload blocking storage acceptance and the next release.

## Task Resume Protocol

After the accepted candidate or exact pre-commit rollback reaches a verified
terminal state:

1. For each acknowledged task, capture a new pre-resume cursor.
2. Persist `resume-intent` with the next command sequence.
3. Send one terminal-state message while omitting model/reasoning overrides:

   > Hermes deployment `<deployment-id>`, command `<command-seq>`, reached
   > verified state `<accepted|rolled-back>`. Resume from checkpoint
   > `<checkpoint-id>`, revalidate runtime assumptions, and reply in a new
   > assistant-authored terminal event with this single-line marker:
   > `HERMES_DEPLOY_RESUMED_V2 deployment=<deployment-id>
   > command=<command-seq> thread=<thread-id> checkpoint=<checkpoint-id>`.

4. Require the correlated assistant event after the pre-resume cursor before
   marking the task resumed.
5. Record missing, deleted, archived, identity-changed, or unresponsive tasks
   as unresolved with exact manual recovery actions.

A task-resume failure does not roll back a healthy release or verified rollback.
No repeated automatic send occurs when delivery or ordering is ambiguous.

## Receipts and Audit

The final deployment receipt records:

- deployment ID, lease identity, schema version, and timestamps;
- local integration-frontier inventory, digest, revalidation, and approval;
- canonical repository/common-dir identities;
- source commits, trees, prepared refs, and `release/current` CAS results;
- immutable artifact, runtime, interpreter, and manifest identities;
- exact live split-state preflight evidence and both plan-bound origin-journal
  paths/digests;
- selector generations before and after;
- paired release ID and transaction-journal identity;
- task enumeration bounds, host/schema data, pause targets, cursors,
  acknowledgements, checkpoints, and unresolved entries;
- the durable commit-boundary phase, deferred-step manifest and per-step
  receipts, and recovery path;
- retention state, cleanup-receipt join keys, retained rollback root, deleted
  payload count, and disk delta;
- pair-authority state and any outstanding retention/ref-repair obligation;
- resume attempts and correlated outcomes; and
- verification results and nonfatal warnings.

## Acceptance Criteria

### Stage 0

Stage 0 passes only when:

- an exact r75 WebUI/r72 gateway plan loads through the real plan loader;
- dry run and live-path execution call one pure last-good attester and return
  equivalent evidence;
- independently trusted r75 and r72 origin-journal paths/digests validate below
  the reliability root and unbound, substituted, or changed journals fail;
- every split-identity field group has an independent positive and negative
  test;
- incorrect r72 origin, service provenance, Agent, selector, interpreter, or
  runtime identity is rejected;
- end-to-end tests cover exact pre-boundary rollback from the split pair; and
- the complete versioned deferred-step manifest is plan-bound and crash tests
  before/after every post-boundary mutation prove exact replay or an
  indeterminate stop without duplicate mutation; and
- the live read-only preflight passes before any task is steered.

### Stage 1

Stage 1 is fully accepted only when:

- WebUI and gateway both report r76 built from WebUI `48a0b7f8`, Agent
  `8fbefbe5`, and the exact captured runtime;
- selector `current` and `last_good` are r76 with no candidate or pending
  transaction;
- one verified previous rollback unit exists and no older bulk rollback payload
  remains;
- the retention implementation is present in the sealed release and its
  completed receipt is read back;
- task enumeration limitations and any bounded-visibility approval are
  recorded truthfully;
- every acknowledged task is resumed or reported with an exact recovery action;
- both `release/current` refs and the pair-authority receipt identify the
  accepted r76 pair; and
- no unrelated host process was stopped.

An otherwise healthy r76 with a retention warning or incomplete source-authority
pair is operationally accepted but not Stage-1-complete; cleanup/ref recovery
continues without another service restart.

### Stage 2

Stage 2 passes only when:

- every local frontier item is classified and approved;
- the frontier digest is unchanged at preparation and immediately before
  `release-intent`, with any drift forcing reapproval;
- the prepared WebUI and Agent tips contain the accepted r76 tips and every
  included local head, or have an approved non-ancestral reproduction ledger;
- required tests pass from clean detached worktrees;
- WebUI and gateway both report the r77 pair built from those exact tips;
- `release/current` advances by expected-old compare-and-swap and the new
  pair-authority receipt reads back;
- one rollback unit remains; and
- the same bounded or exhaustive task-steering contract is reported truthfully.

### Core reusable command

The core command passes before Stage 2 only when fixtures prove:

- prepared-pair-only source resolution and refusal of raw arbitrary commits;
- clean `HEAD == captured OID` worktree enforcement;
- rejection of changed frontier digests, refs, reflog/update cursors, worktree
  contents, trees, manifests, plans, and runtime identities;
- exact split-state preflight parity with independently plan-bound r75/r72
  origin-journal paths and SHA-256 values;
- global lease acquisition in standalone mode and exact lease adoption in
  orchestrated mode;
- correct recovery on both sides of `pair_commit_intent`;
- complete deferred-step manifest replay across every post-boundary crash point,
  plus rejection of `pair_commit_intent` without both
  `deferred_manifest_bound` and `pair_gate_installed`;
- retention-obligation replay across acceptance, pending, cleanup, and receipt
  linkage using exact deployment/transaction/pair/selector join keys, with
  later releases blocked until exactly-one-rollback verification;
- deterministic forward-only two-repository ref CAS recovery and refusal to
  overwrite concurrent divergence, including crashes around authority-receipt
  persistence and linkage;
- one durable machine-readable receipt covering plan, transaction, retention,
  and source-authority results; and
- production execution always uses the full paired transaction.

### Bounded immediate Codex steering

The current operator-managed steering path passes only when tests:

- reject controller-authored, pre-cursor, wrong-role, wrong-deployment,
  wrong-command, wrong-thread, empty-checkpoint, and nonterminal pause/resume
  markers, plus pause markers with missing or non-true `quiescent`;
- prove exact coordinator exclusion by `(hostId, threadId)`;
- prove pinned/non-pinned deduplication and truthful handling when the
  non-pinned result reaches the 50-task limit;
- refuse an unknown possibly nonterminal status unless the bounded-visibility
  operator acknowledgement is durably recorded;
- verify pause and resume sends omit model and reasoning overrides;
- inject crashes before and after pause intent, pause send, pause
  acknowledgement, `release-intent`, transaction terminal persistence,
  resume intent, resume send, and resume acknowledgement;
- prove an ambiguous send is never automatically replayed;
- prove no resume is sent until the exact transaction journal, selector, pair
  identity, and applicable retention outcome have been reconciled;
- intercept every task/process-control operation and prove the steering layer
  never invokes cancellation, signal, kill, archive, or destructive thread
  control; and
- seed unrelated PID/start-token and launchd-label sentinels, then prove their
  identities and liveness remain unchanged while only the two exact
  identity-authorized managed services may be targeted by the paired
  transaction.

### Fully automated Codex steering

Fully automated steering is a separate acceptance gate. It must remain disabled
until the versioned host adapter proves exhaustive pagination, a task-admission
lease, ordered idempotent commands, processed acknowledgements, host-enforced
pause state, child-work coverage, reconnect semantics, and restart recovery.

Until then, immediate steering is operator-managed and bounded, while release
safety remains enforced by the Hermes admission fence and activity drain.
