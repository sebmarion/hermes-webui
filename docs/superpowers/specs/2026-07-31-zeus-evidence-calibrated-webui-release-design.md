# Zeus Evidence-Calibrated WebUI Release Design

**Date:** 2026-07-31
**Status:** Draft — ready for user review; implementation blocked by open decisions
**Owners:** Hermes WebUI, Hermes Agent, and Zeus release automation

## Summary

Zeus will research, implement, evaluate, and publish WebUI release candidates
using whichever local model is active at the time. The Mac will pull, verify,
materialize, promote, and roll back the WebUI release. Gateway and Hermes Agent
remain compatibility-checked but are not co-released by this system.

The release system will not use arbitrary global limits such as a fixed token
cap, file-count threshold, line-count threshold, candidate age, or retry
interval. Those values are policy decisions that must be derived from measured
workload and operator capacity. The first version therefore has:

- fixed safety invariants;
- a fresh-context, structured workflow that prevents transcript replay;
- an instrumented shadow phase;
- model- and task-class-specific budgets learned from successful and failed
  traces;
- semantic risk admission instead of file size as the release authority;
- an independently signed admission policy that shadow data cannot change.

This document deliberately separates the MVP actuator from the later
autonomous-promotion policy. Without that boundary, calibration, attestation,
privileged Mac sealing, and crash-safe release become one oversized first
implementation.

The scheduler uses otherwise-idle Zeus capacity and yields immediately when
interactive local inference needs the model. A candidate can pause and resume
while its source, model, runtime, and compatibility identities remain valid.

## Evidence and decision

The previous numerical proposal is withdrawn.

Observed recent data does not justify universal file or token limits. The
planning observations are accompanied by the reproducible collection commands,
population definitions, percentile rank rule, repository/remote identity, and
collection timestamp in the companion evidence receipt:

`2026-07-31-zeus-evidence-calibrated-webui-release-evidence.json`

The receipt is planning evidence only. It is not an admission policy and cannot
authorize a release.

The measured observations are:

- The latest 100 merged WebUI PRs have a median of 3 changed files and 187
  changed lines, but a P95 of 11 files and 1,590 lines.
- The proposed 8-file/400-line rule would admit about 70% of those PRs, but
  the same rule admits about 57% of the last 100 local non-merge commits.
- Recent full test workflow runs have a median duration of about 243 seconds
  and a P95 of about 328 seconds. Full deterministic testing is therefore a
  better control than saving model context by weakening tests.
- Recent Hermes WebUI sessions contain pathological long-context replay. They
  are evidence for eliminating the session-shaped harness, not evidence for a
  production token budget for fresh local-model stages.

Size remains useful as an anomaly signal and emergency circuit breaker, but it
does not decide semantic release risk. A small authentication change can be
more dangerous than a large stylesheet change.

## Goals

- Automate bounded WebUI research and candidate construction on Zeus.
- Use only the currently active local Zeus model for model-driven work.
- Prevent long-lived conversational context from being replayed between steps.
- Measure prompt, cached-prompt, completion, context, wall-time, and GPU-work
  usage per stage and per model identity.
- Derive operating budgets from observed completion and failure distributions.
- Release trusted candidates without changing Gateway or Agent versions.
- Preserve exact identity, idle-drain, selector-CAS, restart, health, and
  rollback guarantees.
- Allow a complaint-triggered deterministic rollback without requiring a
  natural-language classifier in the MVP.

## Non-goals

- Automatically changing the active Zeus model.
- Calling an online model or routing through Hermes provider selection.
- Releasing Gateway, Hermes Agent, or release-controller changes.
- Automatically accepting authentication, security, migration, dependency,
  state-durability, or release-machinery changes in the first local-patch tier.
- Treating file count, line count, token count, or candidate age as proof of
  safety.
- Making the Mac production workspace a mutable Git checkout.
- Requiring the Mac to listen for inbound Zeus SSH connections.

## MVP boundary

The MVP is a deterministic WebUI release actuator plus a local-model shadow
pipeline.

MVP may automatically promote only a trusted upstream candidate that matches a
hand-authored, independently approved immutable admission policy. That path proves source
provenance, Mac pull/materialization, isolated smoke, WebUI fencing, selector
CAS, restart, exact identity verification, and rollback.

The static MVP policy names the authoritative release remote, WebUI-only path
scope, forbidden surfaces, required deterministic gates, expected Gateway/Agent
identities, and exact selector/runtime contract. It is not learned and cannot
authorize a Zeus-authored patch or a policy change.

The Zeus local model is active in the MVP for research, implementation, and
review shadow runs. It may create candidates and evidence, but it cannot create
or widen the admission policy and cannot cause a model-authored candidate to
promote automatically. This gives us real local-model telemetry without making
unvalidated calibration the release authority.

The MVP implementation slice is therefore:

- direct local-model invocation and model-identity telemetry;
- bounded fresh-context research/implementation/review stages;
- deterministic baseline/candidate tests and forbidden-surface checks;
- signed, content-addressed READY bundles;
- Mac pull, sealed materialization, isolated smoke, fence, selector activation,
  exact health verification, and durable rollback;
- append-only evidence receipts with reproducible collection metadata.

The following are Phase 2, not MVP implementation requirements:

- learned model/task-class budgets that authorize promotion;
- automated promotion of Zeus-authored patches;
- the full statistical calibration and held-out policy-fitting loop;
- an attested inference broker lease protocol;
- expansion beyond the initial fixed policy's low-risk WebUI class.

Phase 2 cannot be enabled by a model output or by shadow data. It requires a
separately reviewed and signed policy artifact.

## Architecture

### Zeus

Zeus contains separate deterministic and model-facing components:

1. **Source fetcher**
   Fetches the authoritative upstream and release remotes and creates a clean
   candidate worktree. It may use the network, but has no model-provider
   credentials.

2. **Local-model invoker**
   Calls only the active local inference service through a fixed local endpoint.
   It resolves and records the active model identity before the first model
   call and revalidates it before every later call.

3. **Unprivileged work runner**
   Runs model-requested repository inspection and edits under a dedicated
   no-sudo service account. It cannot publish, sign, alter the release policy,
   access Mac credentials, or modify the controller.

4. **Deterministic evaluator**
   Runs protected baseline tests, candidate tests, static checks, provenance
   checks, and risk admission. Existing tests cannot be weakened or deleted
   without causing quarantine.

5. **Publisher**
   Writes an immutable source archive and signed READY manifest only after the
   deterministic gates pass. The signing key is unavailable to the model
   worker.

### Mac

The Mac has separate pull and activation boundaries. A dedicated unprivileged
puller uses an outbound read-only Zeus SSH identity with a forced command. It
cannot write the selector release root, selector state, launchd configuration,
or last-good artifacts. There is no Zeus-to-Mac inbound SSH requirement.

A narrow privileged activation helper owns only fixed staging and sealing. It
creates release files with the ownership required by the existing selector,
then seals them with an OS-enforced no-write boundary. It hands an exact,
content-addressed activation request to the existing WebUI LaunchAgent, which
continues to execute the selector/CAS and launchd operations as the WebUI user.
The helper does not accept arbitrary shell, paths, commands, model output, or
network requests. The puller cannot invoke the helper except through its typed
local request interface.

The poller:

1. verifies the publisher signature, source digest, candidate manifest, and
   expected WebUI/Gateway/Agent identities;
2. submits the exact candidate to the activation helper for materialization
   against the pinned macOS
   runtime and agent source already trusted by the selector;
3. verifies the helper's sealed, read-only, symlink-free release receipt;
4. runs an isolated WebUI smoke test on a disposable state directory and port;
5. obtains the WebUI admission fence and waits for authoritative zero activity;
6. sends the helper receipt to the existing WebUI LaunchAgent, which stages and
   activates the existing hash-pinned selector through its lock/CAS contract;
7. restarts the WebUI and verifies exact shallow health/build identity;
8. promotes the candidate, or resumes one durable rollback transaction if the
   candidate is not accepted.

The existing selector remains the authority for immutable release files,
runtime identity, selector generation, atomic state updates, and last-good
selection. The helper must prove that the puller cannot rewrite a staged or
last-good tree after sealing. The new controller must not replace or weaken
those primitives.

## Model identity and local-only enforcement

Each model-driven run records:

- model-serving alias and returned model identity;
- model-weight digest or an authoritative content-addressed model receipt;
- inference executable digest;
- exact serving arguments, including context size and parallelism;
- service PID and start identity;
- local endpoint identity.

The invoker rejects any endpoint that is not the configured local endpoint,
does not inherit online provider credentials, and never accepts a model name or
endpoint supplied by model output. In the MVP, local inference is verified by
the serving process executable and service identity, PID lineage and start
token, exact configuration and model-weight identity, listening socket, and
the absence of Hermes provider routing. This direct-process proof is recorded
for shadow telemetry; it is not treated as the Phase 2 broker attestation.

The MVP work runner has no external network access and source research that
needs the network is performed by a separate fetcher. Phase 2 may replace the
MVP process proof with an attested broker over a Unix socket with peer
credentials, a signed short-lived lease, and enforced egress denial. A Phase 2
broker must have one pinned local inference target and no general proxy
behavior.

If any model identity changes during a candidate, the candidate is quarantined
and the run ends. Budgets are never carried from one model identity to another.

## Workflow and state

Zeus states:

```text
DISCOVERED → RESEARCHED → PATCHED → TESTED → REVIEWED → READY
```

Mac states:

```text
READY → FETCHED → MATERIALIZED → SMOKE_VERIFIED → WAITING_FOR_IDLE
      → FENCED → ACTIVATED → ACCEPTED
```

Any pre-activation failure becomes `QUARANTINED`. A post-activation failure
enters a durable `ROLLING_BACK` transaction. Rollback is idempotent and
crash-resumable; transport retries are bounded by the scheduler's current
resource policy, while the semantic rollback operation remains one transaction.

The durable ledger also records explicit exits for `CANCELLED`, `REPLACED`,
`IDENTITY_INVALIDATED`, `POLICY_INVALIDATED`, `FETCH_FAILED`,
`MATERIALIZATION_FAILED`, `FENCE_FAILED`, `ACTIVATION_FAILED`,
`ROLLBACK_FAILED`, and `MANUAL_RECOVERY_REQUIRED`. Each terminal state records
the owner, last durable phase, failure class, retry eligibility, and the exact
receipt needed for recovery. A replacement cannot delete or overwrite an
active transaction; it must first reach a durable cancellation or quarantine
state.

A candidate becomes invalid immediately if its source base, model identity,
runtime identity, compatibility identity, signature, or policy digest changes.
No arbitrary time-to-live is required.

## Evidence-calibrated budgets

### Structural controls

Every model stage receives a fresh context assembled from:

- the stage contract;
- the compact candidate state;
- relevant file slices;
- the current diff;
- the latest deterministic test result;
- bounded research evidence.

It never receives the entire preceding conversation. Prompt and output usage,
including cached prompt usage, is reconciled against the model response. Missing
or inconsistent usage accounting fails closed.

The context assembler fills the active model's available context using exact
measured system/tool overhead and reserves the declared structured output
space. It does not use a universal 16k, 32k, or 64k prompt limit.

Implementation progress is event-driven. A new repair request requires a new
deterministic failure signature or a concrete review finding. Repeating a
failure without a meaningful state change quarantines the candidate. The
reviewer gets a fresh context and cannot recursively create more reviewers.

There is no unbounded retry loop. Each stage has a signed policy containing an
absolute resource ceiling, preemption behavior, retry/backoff rule, and
exhaustion state. If the policy is absent, stale, or inconsistent with the
active model identity, the candidate remains shadow-only or is quarantined.

### Calibration

The shadow recorder records, per `{model_identity, task_class, stage}`:

- logical input tokens;
- cached and uncached input tokens;
- output tokens;
- context high-water mark;
- prompt and decode throughput;
- estimated GPU work and wall time;
- tool-call count and deterministic state transitions;
- test outcome, failure signatures, repair outcome, and final release outcome.

Phase 2 calibration has two sources:

1. **Historical replay** covering every task class intended for automatic
   release, including representative successful and failed changes.
2. **Live shadow runs** that build real candidates and execute every gate but
   do not promote them.

Calibration is failure-inclusive. Model failures, policy failures, timeouts,
budget exhaustion, and infrastructure failures are labelled separately;
infrastructure failures do not count as model success, and still count against
the resource-capacity SLO. Censored or interrupted runs remain visible in the
denominator rather than disappearing from the sample.

The calibration corpus is content-addressed and split into training/shadow and
held-out partitions before any policy is fitted. A policy declares its task
classes, evidence-window digest, sample floor, target completion SLO,
non-interference SLO, confidence method, and exhaustion behavior. The
completion lower bound uses the declared exact binomial confidence method; no
policy may use point estimates alone. The sample floor and confidence target
are policy inputs that require independent approval, not values inferred by
the model.

The budget is the smallest measured resource allowance whose failure-inclusive
held-out lower bound meets the signed policy SLO. A model/task-class policy is
invalid until the held-out result and its lower-bound calculation are signed
by the independent policy authority. Shadow evidence is advisory only: it can
never write, widen, or activate production admission policy.

Prompt and decode work are measured from stage telemetry. GPU work is labelled
`measured` only when device telemetry covers the complete stage; otherwise it
is labelled `estimated` and cannot be used as proof of a GPU-work limit. A
model change or material runtime change invalidates the policy and returns the
class to shadow mode.

The MVP scheduler is independently simple: release work is low priority,
preemptible, and resumes from durable stage state when interactive work
appears. Its idle-capacity behavior is not a statistical admission SLO.
Phase 2 may add the measured resource SLO and learned budgets after the signed
policy contract exists.

## Semantic admission policy

Candidates are classified by provenance and touched behavior:

- **Trusted upstream candidate:** an already-reviewed upstream/release commit;
  eligible for automatic deterministic release after compatibility, forbidden-
  surface, WebUI-only, and smoke gates. Human upstream review does not exempt
  a candidate from local safety gates.
- **Local low-risk candidate:** a Zeus-authored change in a calibrated class
  with no forbidden surfaces; eligible only after shadow evidence supports the
  class.
- **Stateful or privileged candidate:** changes to lifecycle, persistence,
  authentication, security, dependencies, Gateway/Agent contracts, bootstrap,
  or release machinery; proposal/quarantine only in the MVP.

Every class is rejected if the archive contains non-WebUI product changes,
Gateway/Agent version changes, forbidden paths, dependency changes, symlinks,
or release-controller changes. Provenance never overrides path or semantic
exclusion gates.

Graph impact, changed behavior, test coverage, and ownership boundaries are
inputs to classification. File and line counts are recorded as anomaly data,
not as the decision authority.

## Durable evidence contracts

Each candidate publishes immutable, bounded records:

- `model-identity.json` — exact active local model and serving identity;
- `research.json` — source references and compact evidence digests;
- `evaluation.json` — tests, risk classification, usage, and calibration links;
- `candidate-manifest.json` — source/tree/archive identity and policy digest;
- `promotion-receipt.json` — Mac materialization, fence, selector, health, and
  rollback evidence.

Receipts are content-addressed, append-only, authenticated by the candidate or
policy receipt that references them, and rejected when they exceed the signed
policy's byte/count bounds. Receipt references form a hash chain from model
identity through evaluation, candidate manifest, materialization, activation,
and rollback. Existing receipts cannot be overwritten or silently replaced.
Retention is policy-controlled: active, last-good, policy, and rollback
receipts are protected; expendable shadow traces may be pruned only with a
recorded retention receipt. Receipts contain counts, digests, identities,
status and bounded failure tails; they do not contain full prompts,
credentials, or unbounded transcripts.

## Admission policy authority

The production admission policy is a separate, signed, immutable artifact. It
is generated offline from the calibration evidence and held-out result, then
approved by the operator or an explicitly trusted policy service. The Zeus
controller, local model, candidate manifest, and shadow recorder cannot sign or
modify it. Mac verifies the policy digest before accepting READY.

Policy changes are versioned and take effect only for future candidates. A
policy cannot retroactively authorize an already-quarantined candidate.

## Complaint rollback

The MVP exposes an authenticated local rollback command and a durable rollback
marker. The marker contains the target last-good receipt, request identity,
requester identity, current selector generation, and reason code. Duplicate
markers for the same target are idempotent; a marker targeting a stale or
unverified last-good receipt is rejected. Rollback acquires the same activation
lock, preserves the current receipt for forensics, verifies the restored exact
identity, and only then reopens admission. Natural-language complaint parsing
is outside the MVP.

## Verification

The acceptance suite must prove:

- local-only model endpoint enforcement and rejection of online provider paths;
- MVP direct local-process identity and endpoint proof;
- Phase 2 broker lease, socket-peer, and egress-denial proof;
- model identity drift quarantine;
- exact token/cache accounting and replay amplification detection;
- context assembly excludes prior transcripts and respects measured model
  overhead;
- shadow telemetry and historical replay produce reproducible, non-gating
  calibration records;
- evidence receipts include population, timestamp, repository/remote identity,
  inclusion rules, percentile method, denominator, and raw-artifact digests;
- budget policy invalidates on model/runtime/task-class changes;
- shadow results cannot modify production admission policy;
- protected tests cannot be weakened by candidate changes;
- forbidden semantic classes cannot reach READY;
- corrupt, truncated, path-traversal, symlinked, or mismatched archives fail;
- Mac materialization preserves selector ownership and read-only invariants;
- the unprivileged puller cannot rewrite sealed or last-good artifacts;
- busy WebUI, fence races, selector races, restart failures and health identity
  mismatches fail closed;
- crash recovery resumes activation or rollback without double promotion;
- Gateway identity drift is rejected while Gateway remains unreleased;
- complaint-triggered rollback restores the exact last-good release.

Live verification uses isolated WebUI state and an isolated port. It does not
run deep health checks on the production WebUI during the release transaction.

## Rollout

1. Implement the deterministic trusted-upstream actuator, static policy,
   evidence records, local-process identity telemetry, and local-model
   shadow-only candidate execution.
2. Run live shadow candidates while the deterministic trusted-upstream release
   path remains separately usable. Shadow results are non-gating.
3. Replay representative historical tasks and validate calibration output as a
   Phase 2 input, not as MVP release authority.
4. Have the independent policy authority sign an immutable Phase 2 policy only after
   its declared sample floor, held-out lower bound, and non-interference SLO
   pass.
5. Enable automatic promotion only for task classes whose signed policy meets
   the agreed SLO and has passed held-out validation.
6. Expand classes only through another shadow-and-review cycle.

No implementation should begin until the static MVP policy, ownership/helper
boundary, and authoritative release remote are explicitly confirmed. The MVP
defaults to trusted upstream candidates only; all Zeus-authored candidates are
shadow-only until Phase 2 policy approval.

## Open decisions

- Confirm the static MVP policy signer and exact WebUI-only allowlist/forbidden
  surface manifest.
- Confirm the authoritative upstream/release remote used for READY provenance.
- Confirm the privileged Mac activation helper boundary and the OS-level
  sealing primitive it will use.
- Phase 2 decisions: independent policy authority, sample floor, confidence,
  completion/non-interference SLOs, retention, and absolute resource ceiling.
