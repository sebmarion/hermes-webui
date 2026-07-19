# BestPlan Skill Upgrade: OpenAI Ensemble

## Goal

Upgrade the existing installed `bestplan` skill in place so an explicit
`/bestplan` invocation produces the best evidence-backed plan
from several genuinely independent planning runs, followed by a fresh active
solver that can inspect the target, resolve disagreements, and construct a
better final answer.

For the default `/bestplan 3` case, the host must run three isolated OpenAI Sol
explorers in parallel and then one fresh OpenAI Sol synthesizer. The synthesizer
is not a passive judge and does not merely select a winner. It performs its own
read-only investigation, verifies decisive disagreements, and returns the final
plan without a parent-model rewrite.

This is a host-orchestrated self-ensemble, or Mixture of Agents. It is not a
claim that Hermes controls the model's internal Mixture-of-Experts architecture.

## Existing-skill identity

This design does not create, install, rename, fork, or expose a second skill.
The user-facing artifact remains the existing skill at:

```text
~/.hermes/skills/software-development/bestplan/SKILL.md
```

The existing `/bestplan` command and automatic `/bp` alias remain its only
entry points. The current skill file and its directly affected references are
updated in place to describe the new behavior. The design document in this
repository is only an implementation record; it is not a skill and is never
loaded into the user's skill catalog.

`agent/bestplan_orchestrator.py`, the per-turn marker, and the host command
interception are private runtime machinery added behind the existing skill.
They enforce what that same skill promises; they do not define a separately
selectable feature, command, or skill identity.

## User contract

The explicit command is:

```text
/bestplan [explorer-count] <request>
```

`/bp` remains an alias for `/bestplan` on surfaces that already expose that
dynamic alias.

- The explorer count defaults to `3`.
- The supported range is `2..5`.
- Values outside the range are clamped and the requested and effective counts
  are disclosed in the receipt.
- The number means independent explorer runs, not sequential self-critique
  passes over one draft.
- An explicit invocation is plan-only. Explorers and the synthesizer receive
  read-only tools and must not mutate the workspace or external systems.
- A bare command uses only the immediately preceding visible assistant text row
  as its request. The host does not search farther back or ask a model to decide
  whether an older message looks like a plan. Tool, status, reasoning, system,
  and empty rows are ineligible; if the directly preceding visible row is not a
  non-empty assistant text row, the command returns usage guidance without
  calling a model. When that row carries host-persisted
  `bestplan_receipt_version: 1` metadata and its receipt marker plus body hash
  validate, the deterministic receipt header is stripped and only its canonical
  plan body is supplied for review. Model-authored lookalike text without valid
  host metadata is not stripped. Otherwise the row's complete visible text is
  supplied.
- Natural-language planning that does not use `/bestplan` keeps its existing
  behavior and does not automatically spend the ensemble budget.

The visible conversation contains one user message and one final assistant
message. Child prompts, candidate plans, and the synthesizer's provisional
analysis are private run artifacts rather than extra parent-conversation turns.

## Provider and model policy

Model selection is explicit policy, not inference from a stale model catalog or
the mere presence of a model name in configuration.

For this deployment:

- the only allowed provider is `openai-codex`;
- the only allowed planning model is `gpt-5.6-sol`;
- all explorer and synthesizer runs use maximum supported single-run reasoning;
- DeepSeek, GLM, Kimi, Terra, and every other provider or model are ineligible;
- there is no silent fallback, automatic substitution, or parent-model reuse.

The policy is stored as normal non-secret `config.yaml` state with this exact
contract:

```yaml
bestplan:
  enabled: true
  runtime_route: sota_planner
  allowed_providers: [openai-codex]
  allowed_models: [gpt-5.6-sol]
  explorer_timeout_seconds: 180
  synthesizer_turn_timeout_seconds: 180
  overall_timeout_seconds: 540
```

The entire section has these built-in defaults when absent. `enabled: false`
rejects explicit invocation without a model call. Empty allowlists, an unknown
route, missing credentials, or a resolved provider/model outside the allowlists
fail before fan-out. Invalid timeout values fail configuration validation; they
do not silently revert to defaults. The runtime route is resolved exactly once
through the existing profile-aware lane resolver before the first explorer and
the resulting non-secret runtime specification is pinned for all explorer and
synthesizer stages. There are not separate explorer and synthesizer routes in
this version.

Credentials must resolve through the existing OpenAI Codex authentication path.
The host records the actual resolved provider and model in the receipt;
model-generated claims about their own identity are ignored.

The configured parent model is irrelevant. Invoking `/bestplan` while the parent
conversation uses another model still routes every ensemble run through the
validated BestPlan Sol role.

BestPlan uses the OpenAI Codex Responses runtime at canonical maximum reasoning,
not Codex app-server Ultra. The Responses runtime can enforce a narrow Hermes
read-only toolset on each fresh child. Ultra can proactively create an
uncontrolled number of additional subagents and the current app-server thread
startup intentionally delegates permission selection to Codex configuration;
either behavior would weaken the exact-count and plan-only contracts.

## Architecture

### 1. One-shot command ingress

Each interactive surface recognizes explicit `/bestplan` and `/bp` before the
normal dynamic-skill expansion path. It preserves the original slash text for
the visible transcript, strips the command token for the model-facing task, and
sets a one-shot `bestplan_config` marker on that turn.

The browser follows the existing per-turn `/moa` transport shape: JavaScript
marks the request, the chat-start API validates and forwards the marker, and the
streaming layer passes it to the agent run. The browser cannot provide a
provider, model, toolset, or quorum override. All authoritative parsing and
runtime resolution occur server-side.

CLI, TUI, and Desktop command dispatch use the same one-shot marker and send the
raw task through the ordinary turn lifecycle. A marker is consumed exactly once
and cannot leak into the next turn or another conversation.

Gateway-backed WebUI chat must fail closed with a clear unsupported-mode error
until the Gateway protocol carries the same host-owned BestPlan contract. It
must not silently expand the skill text or run an ordinary single-model turn.

### 2. Conversation-loop branch

The normal turn context is built once so persistence, hooks, cancellation, and
conversation identity stay canonical. Before the ordinary Codex app-server,
MoA, or parent-model loop begins, a present `bestplan_config` branches into a
dedicated `agent/bestplan_orchestrator.py` module.

This branch does not mutate the long-lived parent's system prompt, model,
toolset, or history. It returns the same final-result shape as an ordinary
agent run, allowing existing session persistence and SSE finalization to append
exactly one assistant result.

The result still passes through the canonical turn finalizer so hooks and
durable conversation state settle normally. A narrow
`preserve_final_response` finalization path skips model-output transformations,
footer injection, or other text mutation for this turn; observers may see the
result, but the stored and rendered plan body remains byte-for-byte equal to the
second synthesizer response.

The existing dynamic BestPlan skill remains the user-facing guidance document
and is modified in place. Prompt text alone is not trusted to enforce run count,
provider choice, quorum, or synthesis, so the host machinery becomes that
skill's execution backend for explicit invocations. The skill contract is
updated to remove the old claim that the default `3` means three refinement
passes or that one passive reviewer satisfies explicit BestPlan.

### 3. Immutable run packet

Before fan-out, the orchestrator creates one immutable run packet containing:

- the raw planning request;
- the absolute workspace and active profile;
- the parent conversation context needed to interpret the request;
- applicable project instructions already resolved by the normal agent setup;
- repository identity and dirty-state fingerprint when the target is a Git
  workspace;
- requested and effective explorer counts;
- allowed runtime roles and read-only tool policy; and
- a run ID and start time.

All explorers receive the same stable prefix. Strategy-specific instructions
are appended at the tail so shared OpenAI prompt prefixes remain cacheable.
Explorers cannot see sibling outputs or the synthesizer's work.

### 4. Parallel explorers

The host creates the effective number of fresh leaf agents and runs them in
parallel. Every explorer uses the validated Sol runtime and only the inspection
toolsets required for planning, initially `read_only_files` and `web`. It does
not receive write-capable file tools, shell execution, browser interaction,
delegation, messaging, memory mutation, skill mutation, or external side-effect
tools.

Isolation also disables child persistence, memory writes, conversation
compression, session-database ownership, MCP refresh, status emission, and
ordinary plugin lifecycle hooks. A tool whitelist alone is insufficient because
session-start, pre-model, output, and post-turn hooks can otherwise mutate state
outside the child workspace. Child output and host-collected telemetry are the
only data allowed to leave an explorer.

Every explorer must produce a complete proposed plan, not merely criticism.
The first three search protocols are:

1. **Evidence-first:** trace the real implementation and produce the smallest
   executable plan supported by current evidence.
2. **Counterfactual:** seek a materially different solution, challenge the
   obvious architecture, and compare lifecycle and maintenance costs.
3. **Failure-first:** identify hidden assumptions, race conditions, false
   verification, rollback gaps, and user-visible failure modes, then produce a
   plan that closes them.

Counts four and five add simplicity/rollback and operations/recovery protocols.
The protocol name is host-assigned and included in the receipt.

Each result is normalized into an internal candidate artifact containing its
status, plan text, evidence locators, assumptions, risks, falsifiers, tool
summary, usage, and output hash. Candidate text is treated as untrusted advisory
data. Invalid or empty results count as failures rather than invented success.

The model-facing output contract is one `HERMES_BESTPLAN_CANDIDATE_V1` JSON
object with these required fields:

```json
{
  "version": 1,
  "plan_markdown": "non-empty string",
  "evidence": [{"claim": "non-empty string", "locator": "non-empty string"}],
  "assumptions": ["string"],
  "risks": ["string"],
  "falsifiers": ["string"]
}
```

An explorer succeeds only when its host runtime terminates normally without an
interrupt or guardrail/provider error and the host parses exactly one object
matching this schema. `plan_markdown` and every evidence claim/locator must be
non-empty after trimming; `evidence` must contain at least one entry; the three
other arrays must be present but may be empty. Missing fields, extra candidate
objects, wrong types, an unsupported version, or an empty plan/evidence entry
make that slot fail quorum. The host bounds object size and list lengths before
passing candidates to the synthesizer. Strategy, runtime identity, status,
usage, tools, and hashes remain host-derived fields and cannot be supplied or
overridden by the model.

### 5. Quorum

The required successful-explorer quorum is:

```text
max(2, ceil(2 * effective_count / 3))
```

For the default count of three, two successful explorers produce a visible
`2/3 degraded` run and fewer than two fail closed. A failed explorer is not
retried automatically and is never replaced by an unapproved model.

The host waits only for the bounded explorer stage. Once the stage closes,
late results cannot alter the candidate set or final response.

All explorer calls start within one stage and each receives at most
`explorer_timeout_seconds`. The stage closes when all slots settle or the last
per-explorer deadline expires, whichever occurs first. The synthesizer's
reconnaissance and synthesis turns each receive at most
`synthesizer_turn_timeout_seconds`. Every wait is additionally capped by the
remaining `overall_timeout_seconds`, measured from accepted command ingress.
When the overall deadline expires, active children are cancelled and closed,
then the current stage is classified using the normal quorum or synthesizer
failure rule. Defaults therefore allow up to three minutes for the parallel
explorer stage, three minutes for each synthesizer turn, and nine minutes total;
the lower overall deadline is authoritative.

### 6. Active Sol synthesizer

After quorum, the host creates a fourth fresh Sol agent with the same read-only
exploration tools. It runs as one isolated two-turn session:

1. **Independent reconnaissance:** it receives the original immutable run
   packet without candidate outputs, inspects the target, and forms a
   provisional solution. This reduces anchoring on the first candidate.
2. **Adversarial synthesis:** it receives the normalized candidate packet,
   explicitly labeled as untrusted advisory text. It may inspect the target
   again, resolve contradictions, reject unsupported claims, and construct a
   better final plan.

The second synthesizer response is the canonical plan body. No parent model
selects, summarizes, polishes, or rewrites it. The host may add only a
deterministic receipt header around the unchanged body.

Synthesizer reconnaissance succeeds only when the first turn terminates
normally, returns non-empty provisional text, and host telemetry proves at
least one allowed inspection tool completed successfully during that turn. If
reconnaissance fails any of those checks, the host does not send candidate data
and the run fails synthesis. Final synthesis succeeds only when its second turn
terminates normally without interruption or provider/guardrail error and
returns a non-empty final response after trimming. Tool evidence, terminal
state, and text presence are host-observed; the model cannot self-attest them.

For `/bestplan 3`, this is four isolated Sol sessions and normally five model
turns: one turn for each of the three explorers and two turns in the final
synthesizer session. Parallel fan-out keeps typical latency near the previously
accepted 30-120 second target, but quality and bounded correctness take priority
over promising a fixed wall-clock duration.

## Receipts and state

The orchestrator writes a profile-scoped, append-only run receipt under
`HERMES_HOME` using atomic persistence. The receipt contains no prompt text,
candidate text, credentials, environment values, or workspace file contents.

It records:

- schema version and BestPlan run ID;
- parent session/run identity and a hash of the request;
- requested/effective count and quorum;
- resolved provider, model, runtime, and reasoning mode for every stage;
- explorer protocol, start/end timestamps, status, tool count, usage, and
  output hash;
- synthesizer-stage statuses and final-output hash;
- degradation, timeout, cancellation, and error classifications; and
- the terminal run status.

The visible deterministic header includes the run ID, actual model identity,
successful explorer count, quorum status, and synthesizer status. It uses this
versioned host-owned form:

```text
<<<HERMES_BESTPLAN_RECEIPT_V1>>>
<one canonical redacted JSON object>
<<<END_HERMES_BESTPLAN_RECEIPT_V1>>>
```

The persisted assistant row also carries `bestplan_receipt_version: 1`, the run
ID, and the canonical plan-body SHA-256 outside model-authored content. Receipt
stripping requires both valid metadata and a matching body hash. The header does
not expose private child reasoning.

## Failure and cancellation behavior

- **Policy/auth failure:** fail before fan-out with the rejected runtime
  identity and non-secret reason. Do not fall back to the parent model.
- **Explorer failure:** wait for the bounded stage, apply quorum, and disclose
  each failed slot. Continue only when quorum is met.
- **Quorum failure:** do not launch the synthesizer and do not promote one
  explorer as the final plan.
- **Synthesizer failure:** fail closed with the run receipt. Do not return a
  randomly selected explorer or ask the parent to repair it. Missing successful
  reconnaissance tool evidence, an empty provisional response, an abnormal
  final turn, or an empty final response are all synthesizer failures.
- **Timeout:** close the affected fresh child, classify it as timed out, and
  apply the same quorum rules. No automatic model-call retry occurs.
- **User cancellation:** propagate cancellation to every active child, close
  their runtimes, persist a cancelled receipt, and emit one terminal cancelled
  result.
- **Process death:** the synchronous run may end incomplete, but the append-only
  receipt must remain visibly non-terminal. Startup does not replay model calls
  automatically.
- **Unexpected provider/model drift:** fail closed if any resolved child runtime
  differs from the allowlisted identity, even when the call itself could
  succeed.

Errors are bounded and literal. Warnings from unrelated skill assets may appear
in diagnostics but do not count as a model-selection or quorum result.

## Prompt caching and conversation invariants

The design preserves these invariants:

1. The long-lived parent system prompt and toolset remain byte-stable.
2. The visible user message is persisted exactly once.
3. The final assistant message is persisted exactly once.
4. Parent history never contains private candidates or provisional synthesis.
5. Every child has an independent context and no sibling can influence its
   first proposal.
6. Child message roles alternate normally; candidate injection is a new user
   turn inside only the private synthesizer session.
7. Stable shared prefixes precede strategy-specific instructions and candidate
   data, preserving as much provider prompt caching as the independent runs
   permit.
8. No model output can alter the host receipt, selected runtime identity, or
   success/quorum accounting.

## Scope boundaries

This design implements high-quality explicit planning. It does not:

- create another skill, skill directory, slash command, alias, or user-facing
  product mode;
- implement or execute a `HERMES_BESTPLAN_V1` plan envelope;
- make bare `go` a durable execution command;
- add write-capable BestPlan explorers or synthesizers;
- change ordinary natural-language planning;
- extend the generic MoA provider or its tool-less advisor contract;
- expose a new core model tool;
- use Codex Ultra's uncontrolled proactive subagents inside an explorer;
- add non-OpenAI provider fallback; or
- promise restart-resumable model execution.

Any existing skill text that claims an executable envelope is host-validated or
persisted must be corrected so documentation does not advertise an unshipped
execution contract.

## Verification

Write failing tests before implementation for:

### Parsing and surface transport

- `/bestplan`, `/bp`, default count, explicit count, clamp disclosure, and bare
  invocation behavior;
- preserving the visible slash message while sending only the raw task to the
  orchestrator;
- consuming the one-shot flag exactly once and keeping it session-scoped;
- native WebUI, CLI, TUI, and Desktop ingress;
- gateway-backed WebUI failing closed instead of running an ordinary turn; and
- ordinary skill commands and `/moa` remaining unchanged.

### Runtime policy

- resolving the configured role once before fan-out;
- rejecting a non-OpenAI provider, a non-Sol model, missing credentials, and
  runtime drift before or during a run;
- never selecting DeepSeek or any other discovered model;
- using canonical maximum reasoning through Codex Responses rather than Ultra;
  and
- enforcing the exact read-only child tool whitelist.

### Orchestration

- creating exactly the effective explorer count with isolated contexts;
- starting independent explorers concurrently;
- giving every explorer the same stable packet and a distinct host protocol;
- preventing candidate visibility between explorers;
- normalizing host-derived statuses and ignoring model self-identification;
- `3/3` success, visible `2/3 degraded`, and `<2` fail-closed behavior;
- the generalized two-thirds quorum for counts two through five;
- no retry or unauthorized substitution after failure;
- a fresh synthesizer performing reconnaissance before candidate reveal;
- candidate data entering only the synthesizer's second private user turn;
- returning the second synthesizer response unchanged by a parent model; and
- synthesizer failure never promoting an explorer to final.

### Lifecycle and receipts

- timeout and user cancellation closing all child runtimes;
- late results being ignored after the explorer stage closes;
- one visible user row and one visible assistant row;
- append-only atomic receipts with terminal and non-terminal states;
- receipts excluding raw prompts, candidate text, credentials, and file data;
- usage/tool/model identities coming from host runtime data; and
- process-start reconciliation reporting incomplete receipts without replay.

Run Hermes Agent tests through `scripts/run_tests.sh`, WebUI tests through
`./scripts/test.sh`, `git diff --check`, and GitNexus change-impact review in
both repositories.

Live acceptance uses the real installed OpenAI Codex authentication and a
read-only disposable workspace. It must prove:

1. `/bestplan 3` launches exactly three isolated Sol explorer runs;
2. the final Sol session performs tool-backed reconnaissance and synthesis;
3. the receipt reports the actual OpenAI/Sol identity and quorum;
4. the final plan is the synthesizer output, not a parent rewrite;
5. a forced explorer failure visibly degrades to `2/3`;
6. a forced second failure blocks synthesis;
7. cancellation leaves no child process running; and
8. an ordinary chat turn and `/moa` still behave as before.

## Existing Codex startup repair

The initial screenshot exposed a separate `thread/start` startup-budget failure.
The current dirty installed Hermes Agent already changes that wait from 15 to
60 seconds and adds a focused regression test. Those edits predate this
BestPlan implementation and must be preserved rather than reauthored or folded
into its commit.

Before live acceptance, verify the existing transport change through its focused
test and an isolated Codex handshake. The longer wait is bounded and must not
gain an automatic same-turn retry: no model turn begins before `thread/start`
returns, while a 60-second synchronous startup can delay cancellation longer
than the previous 15-second budget.

## Rollback

Disable the BestPlan host path and remove its one-shot surface markers. Explicit
`/bestplan` must then fail with an honest unavailable message rather than fall
back to prompt-only semantics. Existing ordinary skill loading, `/moa`, parent
model selection, and conversation history remain untouched.

Receipts are retained as historical evidence and require no migration. The
OpenAI provider/model allowlist and role configuration can be removed after the
runtime path is disabled. The pre-existing 60-second Codex startup repair is a
separate transport fix and is not reverted with BestPlan.
