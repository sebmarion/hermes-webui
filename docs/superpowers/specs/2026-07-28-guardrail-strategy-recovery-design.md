# Guardrail Strategy Recovery Design

**Date:** 2026-07-28
**Status:** Approved for specification
**Owners:** Hermes Agent and Hermes WebUI

## Summary

A tool-loop guardrail may stop a proven repeated failing path. It must not
misclassify materially different recovery strategies as one loop, and a
genuine safety stop must not appear in WebUI as a successfully completed task.

This design introduces:

- failure-family-aware streak accounting;
- explicit strategy epochs and progress resets;
- one safe no-effect recovery epoch even when the quarantined trigger was an
  effect-capable tool;
- a typed `guardrail_blocked` terminal contract;
- WebUI presentation that says the task needs recovery instead of `Done`.

Exact-repeat, mutation, policy, authorization, and iteration-budget protections
remain fail-closed.

## Observed production failure

The 2026-07-28 task `dc64ca3397fa` ended with:

> I stopped retrying terminal because it hit the tool-call guardrail
> (`same_tool_failure_halt`) after 4 repeated non-progressing attempts.

The underlying attempts were not one repeated command:

1. a background test command assumed `uv` existed and received
   `uv: command not found`;
2. later terminal work found a missing checkout path;
3. a Python command exposed inherited `PYTHONHOME`/`PYTHONPATH` contamination;
4. the Agent unset those variables and used an absolute interpreter;
5. API/license inspection returned useful output despite a non-zero pipeline
   status;
6. a later clone command discovered that the destination already existed.

The Agent followed its warning guidance by changing paths, environment, and
commands. Nevertheless, `ToolCallGuardrailController` stored the broad streak
as `dict[tool_name, count]`. The configured live hard stop was:

```yaml
tool_loop_guardrails:
  hard_stop_after:
    exact_failure: 3
    same_tool_failure: 4
```

The fourth failed `terminal` assistant batch therefore halted the turn,
regardless of different arguments or failure causes.

The live r67 Agent source already descends from the r52 bounded recovery work,
but that recovery deliberately refuses triggers for which
`tool_may_have_side_effect()` is true. Since `terminal` is effect-capable, no
recovery epoch ran.

WebUI's current terminal classifier recognizes maximum tool iterations, not
Agent guardrail metadata. The host-generated halt prose was persisted as an
ordinary assistant answer, so the UI rendered `Done in 1m 45s`.

## Confirmed hypothesis

**Hypothesis:** Tool-name-only failure accounting combines distinct terminal
strategies into one streak, and missing typed WebUI handling converts the
resulting guardrail halt into apparent completion.

**Falsifier:** The hypothesis would be false if the four observations had the
same guarded argument signature, if the exact-repeat guardrail fired, or if the
Agent result reached WebUI as a non-success terminal state.

The persisted trace and current source falsify none of those conditions:
arguments and failure causes changed, the code was
`same_tool_failure_halt`, and WebUI emitted an ordinary done state.

## Goals

1. Do not halt when the Agent is demonstrably changing strategy across
   materially different failure families.
2. Continue blocking an identical failing tool call at the configured exact
   threshold.
3. Detect cosmetic argument variation that repeats the same underlying
   failure.
4. Preserve fail-closed treatment for unknown, mutating, policy-sensitive, and
   effect-capable tools.
5. Give the model one bounded opportunity to diagnose and pivot safely after a
   genuine broad guardrail trigger.
6. Persist and stream an honest typed blocked outcome when safe recovery fails.
7. Never render a guardrail-blocked task as `Done`.
8. Keep accounting deterministic across parallel tool batches, timeouts,
   middleware argument rewrites, replay, and process restart.

## Non-goals

- Removing tool-loop guardrails.
- Increasing thresholds as the permanent fix.
- Blindly retrying an effect-capable tool after a guardrail stop.
- Treating changed prose as machine-verifiable progress.
- Allowing an automatic child continuation to erase the guardrail quarantine.
- Inferring task completion from a pleasant-sounding assistant sentence.
- Combining this change with active conversation paging in one PR.

## Contract routing

The Agent change touches:

- tool-result classification;
- request-stage guardrail signatures;
- assistant-batch observation accounting;
- guardrail recovery state;
- final Agent result metadata.

The WebUI change touches:

- Agent-result classification;
- stream settlement and run-journal terminal state;
- stable assistant-turn anchors;
- task status language and recovery actions.

Governing references include:

- Agent tool policy and tool-result classification contracts;
- WebUI `docs/CONTRACTS.md`;
- WebUI run-state consistency and turn-journal RFCs;
- stable assistant-turn anchor contracts;
- current guardrail recovery tests in Hermes Agent;
- current tool-limit and terminal-state tests in Hermes WebUI.

## Safety invariants

### Exact repeat

- The request-stage signature remains tool name plus canonical guarded
  arguments.
- Execution middleware cannot rewrite identity after authorization.
- Repeating the same failing signature reaches the exact-failure block at the
  configured threshold.
- Typed permanent and schema-correctable error policies retain their current
  narrower precedence.

### Failure family

- Broad streaks are not keyed by tool name alone.
- Failure-family metadata contains codes/hashes only, never raw tool output,
  commands, paths, credentials, or transcript text.
- Cosmetic differences cannot create an unbounded series of fresh families.
- A family change may reset the broad hard-stop streak but never the exact
  signature streak.
- Aggregate failed-epoch and family-transition budgets remain monotonic across
  family changes and unrelated successes.
- A verified material patch retains its existing authority to reset historical
  execution-failure streaks.

### Recovery

- Recovery grants no extra effect-capable execution.
- The triggering tool/family is quarantined for the remainder of the turn.
- During recovery, only tools certified by the sealed host-owned no-effect
  capability manifest may execute.
- Unknown, plugin, MCP, terminal, mutation, browser-action, messaging, process,
  and delegation tools remain blocked unless already classified as no-effect.
- Recovery is at most one ordinary model iteration.
- A restart or replay cannot grant the recovery epoch twice.
- Recovery after an effect-capable trigger is diagnostic-only unless a
  host-verifiable task-completion receipt proves no further effects are needed.

### Outcome truth

- A host-controlled halt is not an ordinary successful assistant completion.
- The Agent result, run journal, SSE done/error payload, persisted assistant
  anchor, and reloaded UI agree on terminal state and reason.
- The original tool output remains inspectable.
- WebUI never hides a blocked state behind `Done`, a success badge, or silent
  stream closure.
- Missing, old, or unknown terminal capabilities fail closed to a non-success
  UI state.

## Chosen architecture

### 1. Preserve two distinct guardrails

The controller keeps:

1. **Exact-signature failure streak**
   Key: request-stage `ToolCallSignature`.
   Purpose: stop the same failing call.

2. **Failure-family streak**
   Key: `(tool_name, failure_family)`, scoped to the current strategy/progress
   epoch.
   Purpose: stop cosmetic command churn around the same root blocker.

The existing tool-name-only hard-stop counter is removed. A broad
tool-name-level count may remain for warning/observability, but it cannot by
itself halt execution. Two monotonic aggregate budgets provide the outer bound:

- failed assistant observation epochs per turn, default hard stop 8;
- failure-family transitions per turn, default hard stop 6.

Both warn at half their hard limit. Family changes, argument changes, and
ordinary tool successes do not reset them. Only a host-issued progress receipt
or turn termination does. These limits remain subordinate to stricter exact,
typed-policy, authorization, and global iteration limits.

### 2. Normalized failure families

Add a pure classifier that produces:

```text
FailureFamily:
  tool_name
  category
  structured_code
  stable_detail_hash
```

Classification order:

1. Explicit typed error metadata from the tool result.
2. Tool-specific structured fields.
3. A bounded deterministic fallback category.

For `terminal`, structured categories include:

- `command_not_found` for exit 127, with the missing executable represented by
  a one-way stable hash;
- `cwd_or_path_unavailable`;
- `permission_or_policy_denied`;
- `timeout`;
- `signal`;
- `python_bootstrap_environment`;
- `test_or_verification_failure`;
- `nonzero_exit:<code>`;
- `malformed_result`.

The classifier reads bounded structured fields and a bounded error excerpt only
long enough to assign a category. It stores no excerpt. Volatile values such as
PIDs, timestamps, temporary suffixes, line numbers, absolute paths, UUIDs, and
durations are removed before computing `stable_detail_hash`.

Unknown or unparsable failures use one conservative family per tool and
structured exit/error code. They do not each become a new family.

`stable_detail_hash` refines diagnostics within a bounded semantic category; it
does not create an unbounded counter key. Executable names, paths, URLs, and
other argument-controlled values collapse into a fixed number of category
buckets for safety accounting. The aggregate budgets catch alternation between
otherwise valid categories.

### 3. Strategy and progress epochs

One assistant tool batch is still one observation epoch. Parallel worker
completion order cannot multiply the count.

For each tool:

- a new request-stage argument signature is evidence of an attempted strategy
  change, not proof of success;
- a different normalized failure family starts a new broad streak;
- the same family across different signatures increments the family streak;
- an ordinary successful execution does not clear historical family or
  aggregate failure accounting;
- only a host-issued `ProgressReceipt` tied to the blocker and current strategy
  epoch may clear active family streaks;
- a verified material patch produces the existing progress receipt and
  historical failure reset;
- raw call count and exact-signature counts remain monotonic as currently
  specified.

`ProgressReceipt` is structured, adapter-owned evidence:

```text
ProgressReceipt:
  tool_name
  strategy_epoch
  blocker_family
  evidence_kind
  durable_identity
  policy_version
```

Allowed evidence kinds are a verified material patch, a durable accepted state
transition, or a tool-specific verification result registered by the host.
Exit code zero, prose, changed arguments, a read-only diagnostic, or an
unrelated success is not sufficient. A mixed parallel batch is frozen first;
only a receipt explicitly tied to the blocker may reset it.

This means:

- `uv foo`, `uv bar`, and `env uv foo` all producing
  `command_not_found:<uv-hash>` remain one family;
- a missing executable followed by a missing directory followed by a poisoned
  Python environment are different families and cannot trip the same broad
  hard stop;
- repeating an unchanged failed command still trips the lower exact-signature
  threshold.

### 4. Typed remediation hints

Warnings use the failure family, not generic “try something else” prose:

- `command_not_found`: verify availability, use an absolute known path, or use
  a documented fallback;
- path/cwd failure: inspect parent/root and resolve the real target;
- Python bootstrap contamination: inspect/unset the relevant interpreter
  environment and verify the selected executable;
- verification failure: read the first causal failure before rerunning.

The hint is advisory. It grants no tool authority and cannot weaken required
policy.

### 5. Safe recovery for any broad trigger

When a genuine failure-family hard stop fires, the runtime may start exactly
one recovery epoch even if the quarantined trigger was effect-capable.

The safety rule changes from:

```text
effect-capable trigger => no recovery
```

to:

```text
any eligible broad trigger => quarantine trigger
recovery execution => known no-effect tools only
```

The blocked effect-capable tool never executes during recovery. The model may:

- inspect already available files/results/session history with known no-effect
  tools;
- select a different no-effect diagnostic;
- produce a specific blocker/request for user action;
- produce an answer if the task can be completed without more effects.

It may not call `terminal`, mutate files, send messages, delegate, operate the
browser, or reset the quarantine.

For a no-effect trigger, a successful certified alternative may clear recovery
and resume the normal loop. For an effect-capable trigger, recovery remains
diagnostic-only: prose or an unrelated successful read cannot clear it. Normal
completion is allowed only when the host receives a typed
`TaskCompletionReceipt` from an already-declared machine-checkable task
contract proving the requested outcome without further effects. Otherwise the
better explanation is persisted with `guardrail_blocked`.

Exact-repeat blocks, required-policy infrastructure failures, malformed
recovery calls, multiple simultaneous guardrail thresholds, and exhausted
iteration budget remain terminal without automatic effectful retry.

### 6. Typed Agent terminal result

If recovery cannot resolve the guardrail state, the Agent returns:

```text
turn_exit_reason: guardrail_blocked
terminal_reason: <guardrail-code>
guardrail:
  state: blocked
  recoverable: true|false
  code: <stable-code>
  tool_name: <tool-name>
  count: <integer>
  failure_family:
    category: <category>
    structured_code: <code>
    stable_detail_hash: <hash>
  recovery:
    attempted: true|false
    outcome: <stable-code>
```

Raw arguments and raw tool output are excluded.

The host may include concise explanation text, but that text is attached to a
blocked assistant anchor and is not the source of terminal truth.

### 7. WebUI blocked-state handling

WebUI adds a dedicated Agent-result classifier before ordinary settlement.
For `turn_exit_reason=guardrail_blocked`:

- persist `_terminal_state="guardrail_blocked"`;
- persist `_terminal_reason` and bounded guardrail metadata;
- append a terminal run-journal event;
- emit the same state in SSE;
- render the assistant text and last tool result;
- show a `Strategy recovery required` status card;
- label the task `Needs recovery`, not `Done`;
- offer `Continue with a new strategy` only when `recoverable=true` and the
  terminal code is in the server-owned recovery allowlist.

The action does not automatically replay the blocked command or clear durable
policy state. It starts a new turn with the blocker summarized and asks the
Agent to verify prerequisites before choosing a materially different path.

The server, not the browser, authorizes that action. It re-reads the latest
persisted terminal state and accepts the new turn only when:

- the authenticated user may operate the task;
- the terminal state still matches the displayed session/run/code;
- `recoverable=true`;
- the code is in the paired server's recovery allowlist;
- no newer user turn or active run superseded it;
- the one-shot recovery transition has not already been consumed.

A crafted, duplicated, or stale client request returns a typed conflict/denial
and cannot reset counters or policy state. For `recoverable=false`, WebUI shows
only the code-specific user/admin remediation path and never renders or accepts
the continuation action.

Automatic continuation is deliberately not used after a genuine unresolved
effect-capable guardrail stop: it would turn a safety circuit breaker into a
retry loop.

### 8. Sealed no-effect authority

Recovery capability comes from an immutable host-owned manifest embedded in the
paired Agent build. Each decision is keyed by:

- exact tool implementation/version identity;
- input-schema hash;
- policy version;
- guarded argument predicate, when effect depends on arguments.

Unknown tools, dynamic plugins, MCP tools, connectors, or version/schema
mismatches default to effect-capable. A connector may be certified no-effect
only when its exact operation and guarded arguments are read-only under the
host contract; a friendly tool name is insufficient.

The recovery receipt persists the manifest hash, policy version, and exact
capability decisions. Restart/replay reuses those decisions only when the
paired build and manifest match; otherwise recovery fails closed.

## State machine

```text
normal
  |
  | failed observation
  v
exact/family accounting
  | \
  |  \ below threshold
  |   -> allow or warn -> normal
  |
  | threshold
  v
quarantined
  |
  | one no-effect recovery epoch available
  v
recovering
  | \
  |  \ no-effect trigger + certified alternative succeeds
  |   -> recovered -> normal loop continues
  |
  | effect-capable trigger + machine completion receipt
  |   -> completed
  |
  | no safe alternative / malformed / budget exhausted
  v
guardrail_blocked
  |
  | recoverable=true + allowlisted explicit new user turn
  v
new turn with fresh per-turn counters and preserved audit history

guardrail_blocked
  |
  | recoverable=false
  v
code-specific user/admin remediation; no recovery turn
```

Required-policy and authorization failures may bypass recovery and go directly
to their existing typed terminal state.

## Failure behavior

### Classifier cannot derive a family

Use the conservative unknown family for that tool/code. Do not create a unique
family from the raw result.

### Different commands, same root blocker

The normalized family remains the same and the family streak advances.

### Different root blockers

Start a new family streak while retaining raw/exact and aggregate failed-epoch
and family-transition accounting.

### Parallel mixed results

Finalize in original assistant-call order. The family advances at most once per
assistant batch. An unrelated success never clears the family. After all
outcomes are frozen, only a host-issued progress receipt explicitly tied to the
blocker may reset it.

### Late timeout completion

The frozen timeout observed by the model remains authoritative. A late worker
cannot rewrite the family, progress, or recovery decision.

### Recovery emits final prose only

For a no-effect trigger, the host may settle normally only after certified
recovery succeeds. For an effect-capable trigger, prose alone never clears the
halt; absent a machine-checkable `TaskCompletionReceipt`, settle as
`guardrail_blocked` with the improved explanation.

### WebUI receives old Agent output

The paired WebUI capability-negotiates terminal metadata before starting a run.
Current legacy Agents already return structured `guardrail` decisions even
when they lack `turn_exit_reason=guardrail_blocked`; WebUI conservatively maps
structured `action=block|halt`, `turn_exit_reason=guardrail_halt`, or an unknown
guardrail terminal enum to a non-success `legacy_guardrail_blocked` state.

No text matching is used. If neither typed capability nor structured legacy
terminal truth is available, an unknown host-controlled halt/error fails closed
to a generic non-success state. Paired status adoption is never enabled while
an untyped guardrail halt can settle as success.

## Observability

Record counters and stable codes only:

- exact-signature warning/block count;
- tool-name warning count;
- failure-family warning/block count;
- family category transitions;
- recovery eligibility, start, and outcome;
- terminal-state mapping in WebUI;
- number of blocked states incorrectly rendered as success, which must remain
  zero.

Do not log commands, arguments, output excerpts, paths, credentials, or
transcript content.

## Verification

### Agent unit tests

- Four different terminal commands with four different failure families do not
  halt.
- Four cosmetically different commands with one normalized root failure do
  halt at the configured family threshold.
- An identical command still blocks at the exact threshold.
- An unrelated successful tool result does not clear the active family or
  aggregate budgets.
- A blocker-linked host progress receipt clears only the intended family.
- Alternating failure families reaches the aggregate transition/failed-epoch
  budget.
- A verified material patch keeps its current reset semantics.
- Parallel failures count once per assistant batch.
- Timeout snapshots and middleware-rewritten arguments retain request-stage
  identity.
- Unknown results collapse into a conservative family.
- Failure-family metadata contains no raw arguments/output.

### Agent runtime tests

Pin the production trace:

1. missing `uv`;
2. missing checkout path;
3. contaminated Python bootstrap;
4. successful environment pivot;
5. destination already exists.

The Agent must not emit `same_tool_failure_halt` for that sequence.

Additional runtime cases:

- repeated missing executable under varied command spelling reaches the family
  threshold;
- an effect-capable family trigger starts a no-effect-only recovery epoch;
- terminal/mutation calls during recovery are blocked without execution;
- a safe read-only alternative resumes the ordinary loop for a no-effect
  trigger;
- a read/prose pivot after an effect-capable trigger remains blocked unless a
  machine completion receipt exists;
- failed recovery emits typed `guardrail_blocked`;
- restart/replay cannot grant a second recovery epoch.
- replay with a mismatched no-effect capability manifest fails closed.

### WebUI tests

- Typed `guardrail_blocked` result persists the matching assistant-anchor,
  journal, SSE, and replay state.
- The task header/status never says `Done`.
- The last tool output remains visible and inspectable.
- `Continue with a new strategy` starts one explicit new turn and does not
  replay the blocked call.
- `recoverable=false`, non-allowlisted codes, stale states, duplicate actions,
  and crafted clients cannot start a recovery turn.
- Old Agent compatibility remains bounded and does not falsely claim typed
  recovery.

### End-to-end acceptance

Run the exact SkillOpt-like fixture through the paired release:

- the missing `uv` prerequisite is diagnosed;
- the Agent changes strategy without false halt;
- useful intermediate output remains visible even when the complete pipeline
  correctly remains classified as non-zero;
- if a genuine repeated family is forced, the unsafe tool is quarantined;
- the recovery epoch uses only no-effect tools;
- unresolved recovery appears as `Needs recovery`, never `Done`;
- reload preserves the same terminal truth.

## Delivery

### Agent commit

- Add failure-family classifier and metadata.
- Replace tool-name-only hard-stop accounting.
- Extend safe recovery to effect-capable triggers while keeping recovery
  execution no-effect-only.
- Emit typed terminal result.
- Add unit/runtime regressions.

### WebUI commit

- Classify typed guardrail terminal results.
- Persist and replay `guardrail_blocked`.
- Add status card and explicit recovery action.
- Add stable-anchor, SSE, reload, desktop, narrow, and mobile evidence.

The two commits are reviewed independently and paired through the sealed release
manifest. Rollback may restore both to the prior Agent/WebUI pair. The active
bounded-open work remains a separate implementation series and rollback gate.

## Alternatives rejected

### Disable hard stops

Rejected. It removes useful protection for exact repeats, cosmetic churn,
mutations, and runaway tool use.

### Raise `same_tool_failure` from 4 to 8

Rejected as the fix. It delays the same false positive without correcting the
classification.

### Treat every changed command as a fresh streak

Rejected. Models can cosmetically vary arguments while hitting the same root
blocker.

### Automatically start a fresh unrestricted child

Rejected. It clears per-turn state and can replay effect-capable failures
forever across child turns.

### Render the existing prose more prominently

Rejected. Better styling cannot repair false loop detection or the incorrect
successful terminal state.

## Release-note wording

Hermes no longer treats different recovery strategies as one repeated tool
loop. Exact repeated failures still stop safely, while distinct failure causes
can be diagnosed and worked around. When a genuine guardrail blocks progress,
the task now shows `Needs recovery` with an explicit strategy-change action
instead of incorrectly appearing `Done`.
