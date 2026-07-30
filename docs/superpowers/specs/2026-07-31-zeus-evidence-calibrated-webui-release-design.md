# Zeus WebUI Release Actuator MVP

**Date:** 2026-07-31
**Status:** Proposed — corrected after adversarial review; ready for user approval
**Scope:** Hermes WebUI only

## Decision

The MVP has two independent lanes:

1. A deterministic release actuator automatically integrates and releases
   policy-approved WebUI commits from Zeus to the Mac.
2. A finite shadow worker uses whichever local model is active on Zeus to
   research, propose, and review one candidate without gaining production
   authority.

This is deliberately a **release-actuator MVP**, not autonomous promotion of
model-authored patches. It proves the production release and rollback mechanism
while collecting bounded local-model evidence. Automatic promotion of
Zeus-authored patches is a separate Phase 2 contract:

`2026-07-31-zeus-local-model-autonomous-promotion-phase2.md`

The distinction is an acceptance boundary, not wording:

- a trusted release-ref commit may promote automatically in the MVP;
- a local-model-authored commit may not reach the release ref, sign policy, sign
  READY, or trigger Mac activation in the MVP;
- no Phase 2 broker, learned budget, or local-patch promotion requirement can
  block MVP acceptance.

## Why this controls token spend

The observed Hermes sessions show pathological transcript replay. They do not
justify a universal token number for a new local-model workflow. The planning
receipt records the observations and their limitations:

`2026-07-31-zeus-evidence-calibrated-webui-release-evidence.json`

The MVP controls consumption structurally:

- every stage receives a fresh context;
- there are at most three model invocation attempts per shadow candidate:
  research,
  implementation, and review;
- stages cannot recursively create agents or reviewers;
- deterministic test failures and review findings quarantine the candidate;
- there is no autonomous repair, retry, continuation, or conversation loop;
- a shadow candidate requires an immutable externally supplied task receipt;
- `{task digest, source base, model identity}` may run only once unless a new
  task receipt explicitly supersedes it;
- only one shadow model stage may execute at a time;
- interactive local inference immediately preempts shadow work;
- preemption terminates that candidate; the controller does not restart or
  resume the stage automatically;
- the scheduler does not start another shadow stage until the local runtime
  proves the cancelled request released its inference slot; missing proof
  disables shadow scheduling;
- the active runtime's reported context capacity and the stage's bounded output
  schema provide the absolute per-call ceiling; the request sets an explicit
  output cap no larger than the remaining context capacity;
- any missing usage, model identity, cancellation, or progress receipt fails the
  stage closed.

These are fixed safety invariants. They do not depend on statistical calibration
and they do not claim that three calls are an optimal long-term budget. Each call
exists because the MVP requires one independently attributable artifact.

## Goals

- Pull upstream changes into the operator-controlled release ref on Zeus.
- Build and test platform-neutral WebUI candidates on Zeus.
- Use only the active local Zeus model for shadow research, implementation, and
  review.
- Prevent Hermes transcript replay and online-provider fallback.
- Automatically release an eligible trusted release-ref commit without
  per-release approval.
- Keep Gateway and Hermes Agent at their current exact identities.
- Materialize the macOS release on the Mac using the pinned local runtime and
  Agent source already trusted by the selector.
- Fence new work, drain authoritative activity, activate by selector CAS,
  verify exact identity, and either promote or roll back.
- Preserve a deterministic authenticated complaint rollback.

## Non-goals

- Automatically promoting a Zeus-authored patch.
- Automatically resolving an upstream merge conflict.
- Calling an online model or routing model work through Hermes provider
  selection.
- Changing the active local model.
- Releasing Gateway, Hermes Agent, the selector, or the release controller.
- Introducing a privileged Mac helper or a second Mac service account.
- Treating file count, line count, candidate age, or a guessed token number as
  semantic release authority.
- Running deep production health checks during cutover.

## Trust boundaries

### Trusted in the MVP

- the operator-signed static admission policy;
- the deterministic Zeus source synchronizer, evaluator, and publisher;
- the dedicated READY signing key, unavailable to the model worker;
- the Mac user-level release controller;
- the pinned selector, macOS runtime, Agent source, and release-control
  primitives named by policy.

### Untrusted in the MVP

- model output;
- the model worker's filesystem edits;
- downloaded archives before verification;
- network transport;
- mutable remote refs until re-fetched and bound to a commit;
- stale receipts, stale selector generations, and incomplete activity data.

The model worker has no Git push credential, READY signing key, policy signing
key, Mac credential, sudo, or external network access. The deterministic source
fetcher may use the network but has no model-provider credentials.

## Static admission policy

The operator signs one immutable MVP policy before enabling automation. This is
a one-time policy approval, not a per-release approval.

The policy pins:

- upstream repository: `git@github.com:nesquena/hermes-webui.git`;
- upstream ref: `refs/heads/master`;
- release repository: `git@github.com:sebmarion/hermes-webui.git`;
- release ref: `refs/heads/main`;
- the READY signer public key;
- exact selector digest and selector-state schema;
- exact macOS runtime manifest;
- exact Hermes Agent source identity;
- exact expected Gateway identity;
- WebUI-only archive roots and forbidden surface manifest;
- repository test command and an external protected evaluator-bundle digest;
- isolated smoke contract;
- receipt schemas and byte/count bounds.

The policy is signed with an operator policy key whose private half is not
available to Zeus automation or the Mac controller. The Mac pins the policy
public key and the approved policy digest. A policy change applies only to
future candidates.

The MVP forbidden surface manifest rejects changes to:

- Gateway or Agent source/version declarations;
- authentication, credential, and trust-boundary code;
- state schema, migration, durability, and recovery machinery;
- dependency and packaging declarations;
- bootstrap, installer, Docker, supervisor, CI, and release machinery;
- selector, runtime, policy, signer, controller, or controller-owned gate
  definitions;
- symlinks, submodules, generated binaries, or files outside the WebUI source
  archive.

The implementation must materialize these families as an exact reviewed path
manifest with fixture tests. Unknown paths fail closed. A model cannot propose,
generate, or widen this manifest.

## Authoritative source and integration

### One-time baseline reconciliation

Automation cannot assume that the currently running legacy release is reachable
from the new release ref. Before enabling the scheduler:

1. Record the exact live WebUI commit, tree, manifest, Agent, runtime, selector,
   current, and last-good receipts.
2. Reconcile every intentional live-only change into one reviewed bootstrap
   commit on the release ref, or record its explicit rejection.
3. Run the protected gates against that bootstrap commit.
4. Sign one baseline receipt binding the live selector state to the bootstrap
   release-ref commit and its retained rollback artifacts.
5. Disable automation if the release ref, live tree, or receipt does not match
   that signed reconciliation.
6. Prove that the pinned WebUI/Agent pair exposes every activity source required
   by the release fence. If any source is unavailable, stop; a separately
   approved compatibility prerequisite must land before the WebUI-only actuator
   can be enabled.

The first automated candidate must descend from the signed bootstrap commit.
The baseline exception cannot be reused for a later non-fast-forward update.

### Recurring integration

Zeus performs each integration from clean fetched objects:

1. Fetch the exact upstream and release refs named by policy.
2. Record both fetched object IDs and the prior accepted release commit.
3. Reject an upstream ref that is not a descendant of the last accepted
   upstream tip.
4. Reject a release ref that is not a descendant of the prior accepted commit.
5. If the fetched upstream tip is already an ancestor of the release tip, use
   the release tip as the candidate.
6. Otherwise create a normal merge commit from the fetched release tip and
   fetched upstream tip in a clean worktree.
7. If the merge conflicts, quarantine it. The local model may produce a shadow
   suggestion, but it cannot resolve, push, or release the conflict.
8. Run the protected baseline and candidate gates before any push.
9. Push only a fast-forward update from the exact fetched release tip. Force
   pushes are forbidden.
10. Re-fetch the release ref and require its tip to equal the tested commit.
11. Build READY only from that re-fetched commit and tree.

Any remote movement, source identity change, external evaluator-bundle change,
or policy change invalidates the in-flight candidate. Repository test changes
are recorded and run, but cannot alter the external protected evaluator. Dirty
working-tree files are never copied into a release.

## Zeus components

### Deterministic source synchronizer

- owns fetch and fast-forward push credentials;
- creates clean integration worktrees;
- performs conflict detection without model assistance;
- records remote, ref, commit, tree, parent, and ancestry receipts;
- cannot sign policy.

### Local-model shadow worker

- resolves the active local inference process before every call;
- calls one configured loopback/local endpoint directly;
- does not inherit online provider credentials;
- runs under a no-sudo account with no external network;
- writes only to a disposable shadow worktree;
- cannot push, publish, sign, or contact the Mac.

It records:

- model alias and returned model identity;
- model-weight digest or authoritative content-addressed model receipt;
- inference executable digest, PID, start token, listening socket, and arguments;
- context capacity and parallelism;
- logical, cached, and uncached input; output; context high-water mark;
- prompt/decode throughput, wall time, cancellation, and preemption;
- exact stage input and output digests.

If the model process, weights, executable, endpoint, or serving configuration
changes during a candidate, the candidate is quarantined.

### Deterministic evaluator

- loads the policy-pinned evaluator bundle outside the candidate worktree;
- runs that protected bundle plus the repository's baseline and candidate tests
  in clean isolated environments;
- verifies source ancestry and the forbidden surface manifest;
- treats missing or inconsistent evidence as failure;
- produces the only gate result consumed by the publisher.

### Publisher

- has the READY signing key but no policy signing key;
- accepts only an evaluator receipt matching the re-fetched release tip;
- emits one immutable platform-neutral source archive and READY manifest;
- cannot widen policy or substitute another commit after evaluation.

## Finite local-model shadow workflow

The shadow scheduler owns an append-only attempt ledger keyed by
`{task digest, source base, model identity}`. It durably claims the candidate and
then each stage before issuing a model request. A claimed stage is never invoked
again:

- a response is accepted only when it matches the unique claim;
- a crash, lost response, cancellation ambiguity, or missing terminal receipt
  marks that stage `INDETERMINATE` and quarantines the candidate;
- recovery never replays an invocation to discover whether it completed.

This at-most-once rule prefers losing one shadow candidate over silently
multiplying model work.

The shadow workflow is a directed acyclic graph:

```text
DISCOVERED
  → RESEARCHED
  → PATCH_PROPOSED
  → TESTED
  → REVIEWED
  → SHADOW_COMPLETE | QUARANTINED
```

### Research response

One fresh call receives the task contract, bounded fetched research, repository
map, and selected source slices. It emits bounded `research.json`. It receives
no prior Hermes conversation.

### Implementation response

One fresh call receives the task contract, `research.json`, selected source
slices, protected test contract, and output schema. It emits one patch plus
`implementation.json`. It has no interactive tool loop.

### Review response

After deterministic tests, one independent fresh call receives the task
contract, patch, bounded test result, risk manifest, and relevant source slices.
It emits `review.json`.

A failed test, malformed output, non-empty blocking review finding, identity
drift, cancellation failure, or repeated stage request terminates the candidate.
Repair requires a new candidate with a new deterministic task receipt; the MVP
controller never creates it automatically.

## READY bundle

READY contains:

- source remote/ref/commit/tree and ancestry receipt;
- archive digest and canonical file manifest;
- static policy digest and signer identities;
- exact external evaluator and repository test receipts;
- forbidden surface decision;
- required Gateway, Agent, selector, and runtime identities;
- expected WebUI build identity;
- publisher signature.

Shadow artifacts are stored separately and cannot be referenced as release
authority. A READY signature authenticates the deterministic release bundle, not
the quality of model output.

## Mac release controller

The MVP adds one trusted user-level release controller. It is separate from the
existing WebUI LaunchAgent; the existing LaunchAgent remains a selector-to-WebUI
execution path and is not treated as an IPC service.

The controller:

1. pulls READY over an outbound read-only transport;
2. verifies the operator policy signature and pinned policy digest;
3. verifies the READY signer, source identity, archive digest, and compatibility
   identities;
4. extracts through a traversal-safe, symlink-free materializer into a private
   temporary directory;
5. binds the pinned macOS runtime and Agent source;
6. writes files with the UID expected by the selector, read-only modes, one link,
   and no symlinks;
7. fsyncs files and directories, then atomically renames the completed release
   under the selector release root;
8. asks the existing selector to verify the exact manifest;
9. runs isolated smoke with disposable WebUI state and an isolated port;
10. executes the durable activation transaction below.

This MVP trusts the controller's user account. Read-only modes protect against
accidental mutation and against the remote/model boundary; they are not claimed
as protection from a compromised process running as the same trusted user.
Stronger account separation and privileged sealing are Phase 2 hardening.

## Durable activation and rollback transaction

The controller owns one append-only transaction journal and one controller lock.
Selector state remains authoritative for release selection. Every journal write
uses atomic replace plus file and parent-directory fsync.

The normal transaction is:

```text
PREPARED → FENCED → ACTIVATING → ACTIVATED
         → HEALTH_VERIFIED → PROMOTING → ACCEPTED
```

The exact ordering is:

1. Under the controller lock, read and validate selector state.
2. Persist `PREPARED` with transaction ID, policy/READY digests, current and
   last-good identities, selector generation/state digest, and candidate.
3. Acquire the authenticated WebUI admission fence, establish the existing
   process-independent pair-open gate, and wait until every authoritative
   activity source is available and zero.
4. Commit the fence and persist `FENCED`. Admission must remain closed across
   both candidate and rollback restarts.
5. Persist `ACTIVATING` with the expected selector generation.
6. In one selector `update_selector_state` CAS, compose `stage_candidate` and
   `activate_candidate`. This prevents a durable stage-only crash state.
7. Persist the resulting selector generation and state digest as `ACTIVATED`.
8. Restart the existing WebUI LaunchAgent.
9. Require exact shallow health: candidate build/commit/tree/manifest, Agent,
   runtime, selector generation, launch mode, process identity, and closed
   admission transaction. Do not run deep production health.
10. Persist the signed health receipt as `HEALTH_VERIFIED`.
11. Persist `PROMOTING`, then run `promote_candidate` through selector CAS.
12. Persist the promoted selector generation/state digest as `ACCEPTED`.
13. Remove the pair-open gate and reopen admission only after the accepted
    identity is re-read successfully.

Any failure after selector activation first persists `ROLLING_BACK`. Rollback:

1. keeps the process-independent gate closed;
2. runs `rollback_to_last_good` through selector CAS using the last observed
   generation;
3. persists the rollback selector generation and exact target identity;
4. restarts the WebUI;
5. verifies exact shallow health for the recorded prior release while admission
   remains pair-gated;
6. when the rollback follows a failed candidate, restores the journal's
   independently verified pre-activation fallback as `last_good` through one
   more selector CAS;
7. persists `ROLLED_BACK`;
8. removes the gate and reopens admission.

The existing `rollback_to_last_good` transition makes `current` and `last_good`
equal. The controller must not pretend that rollback depth still exists:

- after a failed candidate, it may restore the older pre-activation fallback
  only after verifying its immutable release record; if that fallback is
  missing or invalid, it records `ROLLBACK_DEPTH_EXHAUSTED`;
- after a complaint rollback, the complained-about release is not eligible as
  `last_good`; if no third independently verified release exists, it records the
  same exhausted state.

In either exhausted case, the controller reopens the verified rollback target
for normal use but disables further automatic releases until a new signed
baseline restores a distinct rollback target.

If rollback restart or health verification fails, the state becomes
`ROLLBACK_FAILED` or `MANUAL_RECOVERY_REQUIRED`; admission stays closed.

### Crash reconciliation

On startup the controller scans nonterminal journals before fetching new work:

- selector unchanged from `PREPARED`, `FENCED`, or `ACTIVATING`: abort the
  uncommitted transaction and reopen only after exact prior identity proof;
- selector shows the candidate with the same pending transaction: enter or
  resume `ROLLING_BACK` unless a durable accepted health receipt and
  `PROMOTING` record prove promotion was in progress;
- selector shows promoted candidate with the expected promoted generation:
  finalize `ACCEPTED` only from the already persisted exact health receipt;
- selector shows the recorded rollback target: resume rollback restart and
  health verification, then restore only the journaled verified fallback or
  persist `ROLLBACK_DEPTH_EXHAUSTED`;
- any unexpected generation, transaction, current, candidate, or last-good
  identity: enter `MANUAL_RECOVERY_REQUIRED` and keep admission closed.

No replacement candidate may overwrite or delete a nonterminal transaction.

## Complaint rollback

The MVP exposes an authenticated local command that creates one durable rollback
request. The request binds:

- current selector generation and state digest;
- current accepted release receipt;
- target last-good receipt;
- requester identity and reason code;
- unique transaction ID.

The controller runs the same rollback algorithm and gate. Duplicate requests for
the same accepted/target pair are idempotent. A stale, missing, or unverified
last-good target is rejected. Natural-language complaint classification is not
part of the MVP.

## Evidence retention

Protect:

- active and last-good release manifests;
- static policy and signer receipts;
- READY and promotion receipts;
- every nonterminal or failed transaction;
- the terminal complaint rollback receipt.

Shadow prompts and outputs are not release evidence. Keep bounded stage metadata,
digests, token/accounting totals, failure tails, and model identity. Pruning a
shadow artifact records a retention receipt; it cannot remove production
rollback evidence.

## MVP acceptance

### Token and local-model controls

- at most three model invocation attempts are possible for one shadow candidate;
- no stage receives a Hermes transcript or another stage's conversation;
- no autonomous retry, repair, continuation, or recursive reviewer exists;
- one immutable task receipt can create at most one candidate for the same
  source base and model identity;
- malformed output, test failure, review finding, or identity drift quarantines;
- preemption cancels the active local call and preserves only terminal compact
  evidence; it cannot resume the candidate;
- cancellation must prove the inference slot is released before another shadow
  stage starts; otherwise shadow scheduling disables itself;
- online-provider credentials and Hermes provider routing are absent;
- direct process, model-weight, executable, endpoint, and socket identity are
  recorded for every call;
- usage accounting reconciles or fails closed.
- crash or response loss after a durable stage claim cannot cause reinvocation.

### Source and policy

- only the pinned release ref can produce READY;
- upstream fast-forward ancestry, prior accepted release ancestry, normal
  fast-forward push, and post-push re-fetch are proved;
- force-push, remote movement, conflict, dirty worktree, unknown path, external
  evaluator drift, and forbidden surface all quarantine;
- model output cannot alter policy, the external evaluator, release ref, or
  signatures.

### Mac and selector

- corrupt, truncated, traversal, symlink, hard-link, ownership, mode, and digest
  failures are rejected;
- isolated smoke uses disposable state and port;
- any unavailable or nonzero activity source blocks activation;
- selector generation races fail closed;
- stage and activate occur in one selector CAS;
- exact candidate health is required before promotion;
- Gateway and Agent identities remain unchanged.

### Recovery

- failure injection at every journal write and external mutation resumes without
  double promotion;
- a candidate never starts ordinary work before acceptance;
- rollback restarts remain pair-gated until exact last-good health passes;
- unexpected selector state requires manual recovery and remains closed;
- complaint rollback restores the exact recorded last-good release;
- rollback never advertises a fallback equal to current, and exhausted rollback
  depth disables subsequent automatic releases;
- a nonterminal transaction blocks replacement and retention cleanup.

Phase 2 broker, statistical calibration, learned-budget, and model-authored
promotion tests are explicitly excluded from MVP acceptance.

## Implementation order

1. Define and sign the static policy schema, exact forbidden path manifest, and
   fixture tests.
2. Implement the deterministic Zeus source synchronizer, evaluator, publisher,
   READY schema, and signer separation.
3. Implement the finite local-model shadow worker and accounting receipts.
4. Implement the trusted Mac user-level controller, safe materializer, isolated
   smoke, and selector adapter.
5. Implement the durable transaction journal, pair-gate integration, exact
   shallow health, complaint rollback, and crash reconciliation.
6. Run failure-injection tests and shadow-only live trials.
7. Enable automatic release only for the pinned trusted release ref.

Implementation starts with a written implementation plan. This design does not
authorize changing the live selector, release state, LaunchAgents, Gateway,
Agent, or production WebUI.
