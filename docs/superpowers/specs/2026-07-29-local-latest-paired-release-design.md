# Local-Latest Paired Hermes Release Design

## Objective

Deploy the newest proven version of Seb's own Hermes WebUI and Hermes Agent
work now, then establish a single local release lineage so every future
production release uses the latest tested local pair. Upstream branches are
explicitly out of scope.

The release must update WebUI and gateway/Agent as one transaction, keep
exactly one previous rollback payload, pause active Codex tasks cooperatively,
and resume those tasks after production is verified.

## Current State

- The selector is idle at generation 188 with WebUI r75 as both `current` and
  `last_good`.
- The live WebUI is r75 at commit `afa07ff8`.
- The live gateway still identifies as the r72 pair even though it uses the
  same pinned Agent commit, `8fbefbe5`.
- The newest proven forward WebUI release-line commit is `48a0b7f8`, which is
  exactly r75 plus synchronous one-rollback retention.
- WebUI local `main` and the r75 release line diverged. Agent local `main` and
  the deployed Agent line also diverged. A commit timestamp is therefore not a
  valid definition of "latest."
- The full paired `release-commit` transaction is the production deployment
  primitive. Selector-only promotion is a recovery/debug primitive and must
  not be used for ordinary production releases.

## Definition of "Our Latest"

Each repository will have one authoritative local branch named
`release/current`:

- `hermes-webui:release/current`
- `hermes-agent:release/current`

The deployable version is the exact pair of branch tips after required tests
pass. A production plan must refuse to build when:

- either checkout is dirty;
- the requested commit differs from its `release/current` tip;
- either commit identity changes after planning;
- the release plan names a different Agent or WebUI commit;
- the pair has not passed its required gates.

No upstream fetch, merge, rebase, or update is part of this policy.

## Staged Adoption

### Stage 1: Immediate Consistent r76

Create the initial authoritative local branches before planning:

- WebUI `release/current` at `48a0b7f8`;
- Agent `release/current` at `8fbefbe5`.

Build and deploy a full paired r76 from:

- WebUI `48a0b7f8`;
- Agent `8fbefbe5`;
- the already verified immutable runtime, unless its identity check fails.

This is the smallest forward step from live r75. It makes WebUI and gateway
report one r76 pair and activates synchronous rolling retention. It does not
claim to contain the still-divergent local-main work.

### Stage 2: Canonical Local r77

Create clean integration worktrees and reconcile only the local histories:

- WebUI `main` plus the r75/r76 release line;
- Agent `main` plus the deployed `8fbefbe5` line.

Conflicts are resolved by behavior and release invariants, not timestamps.
The resulting tested tips become `release/current`. A full paired r77 then
deploys that combined local version.

Thereafter, new local work must land on or be intentionally integrated into
`release/current` before it is deployable.

## Cooperative Task Pause Protocol

Raw process killing is not part of normal deployment.

Before the release transaction begins:

1. Generate a unique deployment ID.
2. List every visible local Codex task across projects and select entries with
   `kind=codex`, `hostId=local`, and `status=active`, excluding the deployment
   coordinator task. ChatGPT conversations, remote-host tasks, idle tasks, and
   unloaded tasks are outside this local Hermes restart boundary.
3. Record each selected task's thread ID, host ID, title, project/cwd, and
   pre-pause status in a deployment pause receipt. Persist that receipt
   atomically as private `0600` data and fsync its parent before sending the
   first steering message.
4. Send each selected task a steering message:

   > Deployment `<id>` is preparing a Hermes restart. At your next safe
   > boundary, stop starting new work, persist/checkpoint your current state,
   > and yield with `HERMES_DEPLOY_PAUSED_V1 <id>`. Do not resume until a
   > follow-up explicitly tells you the deployment is verified.

5. Wait for every selected task to yield or request attention. Re-read the
   task and require the deployment-specific pause marker before marking it
   paused. Atomically update the receipt after every steer and acknowledgement.
6. Use a configurable `--pause-timeout` with a default of 300 seconds. If any
   task does not acknowledge before that deadline, stop the deployment, resume
   every task already paused, and report the exact non-acknowledging task. Do
   not kill it automatically.
7. Immediately before `release-commit`, list local Codex tasks again. If a new
   task became active, add it to the receipt and run the same steer/wait
   protocol. Require two consecutive snapshots two seconds apart with no
   unpaused active task before entering the release transaction. Any new
   active task observed before service shutdown reopens this gate.

The release controller's existing activity drain remains authoritative for
Hermes streams, delegations, processes, memory commits, OAuth work, terminal
activity, and undelivered completions. Cooperative Codex steering complements
that drain; it does not replace it.

Unrelated host processes, including the existing local Node server and
Cloudflare tunnel, are outside the deployment target and remain running.

## Paused Task Resume Protocol

After either the new pair or the exact rollback pair reaches a verified
terminal state:

1. For each task recorded as paused, send:

   > Deployment `<id>` reached verified terminal state `<accepted|rolled-back>`.
   > Resume from your persisted checkpoint and revalidate any external/runtime
   > assumptions that may have changed during the restart.

2. Preserve the task's existing model and reasoning settings.
3. Take an immediate task snapshot to confirm the follow-up was accepted.
4. Record the post-resume status in the same deployment receipt.
5. A failure to resume one task does not roll back a healthy release or a
   verified rollback, but it must be reported with its thread ID and recovery
   action.

Tasks that were idle, unloaded, or archived are not resumed by this
deployment. Tasks created or activated during the pause window are captured by
the final quiescence rescan and become normal pause/resume targets.

A nonfatal post-promotion retention failure does not keep tasks paused. It is
included in the resume message and deployment receipt as a warning.

## Pause Receipt Durability and Recovery

Pause receipts live below the private reliability root in
`deployment-receipts/`. They contain task identity and lifecycle state but no
prompt bodies, credentials, or model secrets.

On coordinator startup, an incomplete receipt must be reconciled before a new
deployment begins:

- If no release transaction started, resume every acknowledged task and mark
  the deployment aborted.
- If the selector transaction is nonterminal, resume the existing release
  transaction before steering or resuming tasks.
- If the new release is durably accepted and its pair identity verifies,
  resume paused tasks with the success message even when rolling retention
  reported a nonfatal failure.
- If exact rollback is durably verified, resume paused tasks with a rollback
  message that tells them to revalidate runtime assumptions.
- If neither the candidate nor rollback can be verified, keep the tasks
  paused, report the receipt path and task IDs, and require operator recovery.

Every receipt transition is an atomic replace followed by a parent-directory
fsync so a coordinator crash cannot erase which tasks were paused.

## Paired Release Transaction

For both r76 and r77:

1. Prove clean WebUI and Agent source identities.
2. Run release, selector, retention, and feature-specific tests.
3. Build or reuse verified immutable Agent and runtime artifacts.
4. Build and verify the sealed WebUI release.
5. Generate an exact transaction plan from the two authoritative commits.
6. Inspect the plan and run a non-mutating dry run.
7. Cooperatively pause active Codex tasks.
8. Run `release-commit`, which:
   - fences new work;
   - drains activity;
   - snapshots paired state;
   - stops the old WebUI and gateway safely;
   - starts the candidate pair behind admission fences;
   - proves startup identity and health;
   - atomically activates and promotes the selector;
   - opens the pair;
   - restores watchdog scheduling.
9. Verify WebUI and gateway report the same build, generation, pair ID, WebUI
   commit, Agent commit, and runtime identity.
10. Verify the retention receipt keeps exactly one previous rollback payload.
11. Resume paused tasks.

On a pre-promotion failure, use the transaction's exact rollback. On a
post-promotion retention failure, keep the accepted release, report retention
failure, and do not pretend the release rolled back.

## Future Release Interface

Add one supported high-level command that:

- resolves both `release/current` tips;
- creates the exact plan and deployment ID;
- performs preflight and dry-run validation;
- pauses and resumes active tasks;
- invokes the paired release transaction;
- emits one machine-readable deployment receipt.

The command accepts no arbitrary production commit by default. An explicit
recovery-only override must be separate and noisy. Low-level selector commands
remain available for recovery but are not documented as the normal release
path.

## Delivery Boundaries

Implementation is split into three independently verifiable deliverables:

1. **r76 paired deployment:** establish initial `release/current` branches,
   cooperatively pause active tasks using an operator-managed durable receipt,
   deploy the proven r76 pair, verify one rollback, and resume tasks.
2. **Local-history reconciliation and r77:** reconcile only Seb's local WebUI
   and Agent histories into new tested `release/current` tips, then deploy and
   verify the r77 pair.
3. **Reusable release command:** encode plan generation, canonical-tip checks,
   durable pause recovery, paired release, retention, and targeted resume in
   one supported high-level interface.

Each deliverable has its own tests, receipt, rollback boundary, and acceptance
checkpoint. Failure in a later deliverable does not invalidate an already
verified earlier release.

## Receipts and Audit

The final deployment receipt records:

- deployment ID and timestamps;
- WebUI and Agent source commits and trees;
- immutable artifact and manifest identities;
- selector generations before and after;
- paired release ID;
- pause targets and acknowledgements;
- release transaction receipt;
- retained rollback root;
- deleted payload count and disk delta;
- resume attempts and outcomes;
- verification results and any nonfatal warnings.

Receipts and manifests remain small and durable. Only bulk rollback payloads
are rotated.

## Acceptance Criteria

Stage 1 is accepted only when:

- WebUI and gateway both report r76 and Agent `8fbefbe5`;
- selector `current` and `last_good` are r76 with no candidate/pending
  transaction;
- one previous rollback payload exists;
- the retention implementation is present in the sealed release;
- every paused task was either resumed or reported with an exact recovery
  action;
- no unrelated host process was stopped.

Stage 2 is accepted only when:

- both `release/current` branches contain the intentionally reconciled local
  work;
- required tests pass from clean worktrees;
- WebUI and gateway both report the r77 pair built from those branch tips;
- future plan generation refuses stale or arbitrary commits;
- the same one-rollback and cooperative pause/resume invariants pass.
